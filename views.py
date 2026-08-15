"""全UIコンポーネント定義"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import discord

import database
import rating as rating_lib
from config import (
    MAX_PLAYERS, Role, Team, Phase,
    RUNOFF_SPEECH_TIME, LAST_WILL_TIME, VOTE_TIMEOUT,
    NIGHT_BASE, NIGHT_MIN,
    CH_LOBBY, CH_STATS, CH_VILLAGE, CH_SPIRIT,
    LOG_CATEGORY_VILLAGE, LOG_CATEGORY_SPIRIT, LOG_CATEGORY_LIMIT,
    SEASON_RANK_MIN_GAMES, GRANDMASTER_PERCENTAGE,
    RANK_SPECS, SEASON_RANK_PERCENTAGES,
    RATING_FLOOR, INITIAL_RATING, WIN_PARTICIPATION_BONUS,
    WOLF_GUESS_TIMEOUT, BONUS_WOLF_GUESS_SLOTS,
    POSTGAME_RECOMMENDATION_TIMEOUT, BONUS_POSTGAME_VOTE,
    BONUS_WOLF_EXECUTION_VOTE, BONUS_FINAL_DAY_WOLF,
    BONUS_WOLF_GUESS_HIT, BONUS_WOLF_GUESS_EARLY_MULTIPLIER,
    BONUS_WOLF_GUESS_EARLY_MAX_DAY, BONUS_GUARD_SUCCESS, BONUS_NIGHT1_SEER_KILL,
    PRIVATE_ROOM_CREATOR_ROLE_LABEL, BOT_VERSION,
    ACTIVE_ROOM_DEFINITIONS, STATS_MIN_SAMPLES,
    SLOW_INTERACTION_SECONDS,
    DEFAULT_VARIANT_ID, VariantDefinition, get_variant_definition,
    VARIANT_DEFINITIONS, LADDER_DEFINITIONS, USER_VISIBLE_VARIANT_IDS,
)
from models import by_number, parse_select_id

if TYPE_CHECKING:
    from game import GameCog
    from room_runner import RoomRunner

log = logging.getLogger(__name__)


class InteractionTimer:
    """ボタン押下の所要時間を段階ごとに記録する (詰まりの切り分け用)。

    全員が同時に押すボタン (朝を迎える / 役職を確認した / 投票) は、
    Discordへの応答 → 卓ロックの取得 → 状態更新とDB保存 → 本人への返信、
    という4段階を通る。「押したのに反応しない」と言われたとき、どの段階で
    詰まったのかはログが無いと分からない。とくに卓ロックは他の処理
    (ミュート整列など) も取るため、待ちが数秒に伸びうる。

    平常時に13人ぶんのログを毎晩出しても読めないので、
    SLOW_INTERACTION_SECONDS を超えたときだけWARNINGへ上げる。
    """

    __slots__ = ("label", "user_id", "_started", "_marks")

    def __init__(self, label: str, user_id: int) -> None:
        self.label = label
        self.user_id = user_id
        self._started = time.perf_counter()
        self._marks: list[tuple[str, float]] = []

    def mark(self, stage: str) -> None:
        """段階の到達時刻を記録する (押下からの経過秒)。"""
        self._marks.append((stage, time.perf_counter() - self._started))

    def finish(self, *, note: str = "") -> None:
        total = time.perf_counter() - self._started
        if total < SLOW_INTERACTION_SECONDS:
            return
        stages = " ".join(f"{stage}={sec:.2f}s" for stage, sec in self._marks)
        suffix = f" {note}" if note else ""
        log.warning(
            f"ボタン応答が遅延: {self.label} user={self.user_id} "
            f"total={total:.2f}s {stages}{suffix}"
        )


class DangerConfirmView(discord.ui.View):
    """破壊的操作を二度押しさせず、本人だけに最終確認する共通View。"""

    def __init__(
        self,
        actor_id: int,
        action: Callable[[discord.Interaction], Awaitable[None]],
        *,
        confirm_label: str = "実行する",
    ) -> None:
        super().__init__(timeout=30)
        self.actor_id = actor_id
        self.action = action
        self.confirm_btn.label = confirm_label

    @discord.ui.button(label="実行する", style=discord.ButtonStyle.danger)
    async def confirm_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message(
                "この確認は操作を開始した本人だけが実行できます。", ephemeral=True
            )
        for item in self.children:
            item.disabled = True
        self.stop()
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.action(interaction)

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message(
                "この確認は操作を開始した本人だけが変更できます。", ephemeral=True
            )
        for item in self.children:
            item.disabled = True
        self.stop()
        await interaction.response.edit_message(
            content="↩️ 操作をキャンセルしました。", view=self
        )


async def _prompt_wolf_surrender(
    cog: RoomRunner,
    interaction: discord.Interaction,
    expected_game_run_id: str,
) -> None:
    """実人狼本人のDMでだけ、サレンダー同意の最終確認を出す。"""
    state = cog.state
    player = state.get_player(interaction.user.id)
    if (
        not cog.is_current_game_view(expected_game_run_id)
        or player is None
        or not player.alive
        or player.role != Role.WEREWOLF
    ):
        return await interaction.response.send_message(
            "⏳ 現在この操作はできません。", ephemeral=True
        )
    if state.surrender_confirmed:
        return await interaction.response.send_message(
            "🏳️ サレンダーは既に成立しています。", ephemeral=True
        )
    if interaction.user.id in state.surrender_ids:
        living_wolves = {p.user_id for p in state.alive_wolves()}
        agreed = len(living_wolves & state.surrender_ids)
        return await interaction.response.send_message(
            f"🏳️ あなたは同意済みです（**{agreed} / {len(living_wolves)}人**）。",
            ephemeral=True,
        )

    async def execute(confirm_interaction: discord.Interaction) -> None:
        async with cog.action_lock:
            result = await cog.submit_surrender(
                confirm_interaction.user,
                expected_game_run_id=expected_game_run_id,
            )
        await confirm_interaction.followup.send(result, ephemeral=True)

    await interaction.response.send_message(
        "⚠️ 生存中の実人狼全員が同意すると、村陣営の勝利として試合を終了し、"
        "通常どおりレートへ反映します。サレンダーに同意しますか？",
        view=DangerConfirmView(
            interaction.user.id,
            execute,
            confirm_label="サレンダーに同意",
        ),
        ephemeral=True,
    )


class PrivateRoomInfoView(discord.ui.View):
    def __init__(self, manager: GameCog) -> None:
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(label="村・募集を作成", style=discord.ButtonStyle.success, custom_id="mayor_room_create")
    async def create_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.manager.recruitment_manager.start_village_creation(interaction)

    @discord.ui.button(label="村名変更", style=discord.ButtonStyle.secondary, custom_id="mayor_room_rename")
    async def rename_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("サーバー内でのみ使用できます。", ephemeral=True)
        if not self.manager._has_private_room_creator_role(interaction.user):
            return await interaction.response.send_message(
                f"村名を変更できるのは **{PRIVATE_ROOM_CREATOR_ROLE_LABEL}** ロール保持者だけです。",
                ephemeral=True,
            )
        await interaction.response.send_modal(PrivateRoomRenameModal(self.manager))

    @discord.ui.button(label="村を削除", style=discord.ButtonStyle.danger, custom_id="mayor_room_delete")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.manager.delete_private_room_for_member(interaction)


class PrivateRoomRenameModal(discord.ui.Modal, title="GM村名変更"):
    new_name = discord.ui.TextInput(
        label="新しい村名",
        placeholder="例: Aくん村",
        min_length=1,
        max_length=90,
    )

    def __init__(self, manager: GameCog) -> None:
        super().__init__()
        self.manager = manager

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.manager.rename_private_room_for_member(interaction, str(self.new_name.value))


# ============================================================
# ロビー: 参加・GM・ゲーム開始
# ============================================================

class LobbyView(discord.ui.View):
    """参加受付画面のUI"""

    _GM_VILLAGE_RECRUITMENT_ONLY_CONTROLS = frozenset({
        "join_game",
        "leave_game",
        "get_gm",
        "release_gm",
        "rematch_game",
    })

    def __init__(self, cog: RoomRunner) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        if self.cog.is_private_room():
            # GM村は募集カードでのみ参加者・GMを受け付ける。
            # 開始直前に一時的に戻すLobbyViewでは「ゲーム開始」を
            # 残し、募集を通さない参加や次村だけを閉じる。
            for item in tuple(self.children):
                if getattr(item, "custom_id", None) in self._GM_VILLAGE_RECRUITMENT_ONLY_CONTROLS:
                    self.remove_item(item)
        # 再起動復元などでUIを再投稿した時点で13人+GMが揃っている場合に備え、
        # 生成時に開始ボタンの有効/無効を計算する
        self._refresh_start_button()
        if self.cog.state.phase != Phase.LOBBY:
            for item in self.children:
                # 募集通知はゲーム状態と無関係な本人設定なので常に操作できる。
                if getattr(item, "custom_id", None) != "recruitment_notification_toggle":
                    item.disabled = True

    @property
    def player_count(self) -> int:
        """卓の定員。簡易Viewテストでは従来値へフォールバックする。"""
        variant = getattr(self.cog, "variant", None)
        return int(getattr(variant, "player_count", MAX_PLAYERS))

    def _refresh_start_button(self) -> None:
        start_btn = discord.utils.get(self.children, custom_id="start_game")
        if start_btn:
            # ゲーム進行中の復元でロビーUIを再投稿するケースがあるため、
            # ロビー中のみ有効化する
            state = self.cog.state
            start_btn.disabled = not (
                state.phase == Phase.LOBBY
                and len(state.players) == self.player_count
                and state.gm_id is not None
            )

    def _build_embed(self) -> discord.Embed:
        state = self.cog.state
        players = list(state.players.values())
        player_list = "\n".join(
            f"`{i+1}.` {p.member.display_name}" for i, p in enumerate(players)
        ) or "なし"
        gm_name = "なし"
        if state.gm_id:
            gm_member = state.guild.get_member(state.gm_id)
            if gm_member:
                gm_name = gm_member.display_name

        room_note = "制限なし"
        if self.cog.room_def.allowed_ranks is not None:
            room_note = " / ".join(
                sorted(self.cog.room_def.allowed_ranks, key=rating_lib.rank_order_value)
            )
        elif self.cog.room_def.allowed_gm_user_ids:
            room_note = "参加制限なし / GMは指定ユーザー専用"
        elif self.cog.room_def.owner_only_gm:
            room_note = "サーバーオーナー専用GM卓"

        if self.cog.is_private_room():
            description = (
                "参加者とGMの登録は、公開中の募集カードから行います。\n"
                f"参加条件: **{room_note}**"
            )
        else:
            description = (
                f"参加者が{self.player_count}人揃ったらGMが「ゲーム開始」を押してください。\n"
                f"参加条件: **{room_note}**"
            )

        embed = discord.Embed(
            title=f"{state.room_name} - 参加受付",
            description=description,
            color=discord.Color.dark_gold(),
        )
        embed.add_field(
            name=f"参加者 ({len(players)}/{self.player_count})",
            value=player_list,
            inline=False,
        )
        variant = getattr(self.cog, "variant", None)
        variant_label = getattr(
            variant,
            "label",
            get_variant_definition(DEFAULT_VARIANT_ID).label,
        )
        embed.add_field(name="ゲーム形式", value=variant_label, inline=False)
        embed.add_field(name="GM", value=gm_name, inline=False)
        return embed

    async def _update(self, interaction: discord.Interaction) -> None:
        embed = self._build_embed()
        self._refresh_start_button()
        try:
            await interaction.message.edit(embed=embed, view=self)
        except (discord.NotFound, discord.HTTPException):
            pass

    @discord.ui.button(label="参加", style=discord.ButtonStyle.success, custom_id="join_game", row=0)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.cog.is_private_room():
            return await interaction.response.send_message(
                "GM村への参加は、公開中の募集カードから行ってください。",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        # join_lock (全卓共通) → action_lock (卓ローカル) の順で取る。
        # 二重参加チェックからstate.playersへの書き込みまでの間にDM送信テストの
        # awaitが挟まるため、卓ローカルのロックだけでは別卓が割り込めてしまう。
        # 取得順序はGM取得側と揃えること (逆順で取るとデッドロックする)
        async with self.cog.manager.join_lock, self.cog.action_lock:
            state = self.cog.state
            user_id = interaction.user.id

            if state.phase != Phase.LOBBY:
                return await interaction.followup.send("現在ゲーム中です。", ephemeral=True)
            if user_id in state.players:
                return await interaction.followup.send("既に参加しています。", ephemeral=True)
            if len(state.players) >= self.player_count:
                return await interaction.followup.send(
                    f"参加者が上限（{self.player_count}人）に達しています。",
                    ephemeral=True,
                )
            join_error = await self.cog.validate_join(interaction.user)
            if join_error:
                return await interaction.followup.send(join_error, ephemeral=True)

            # DM送信テスト
            try:
                await interaction.user.send("人狼ゲームへの参加を受け付けました。")
            except (discord.Forbidden, discord.HTTPException):
                await interaction.followup.send(
                    "DMを開放してください。DMが受け取れないと参加できません。", ephemeral=True
                )
                return

            from models import Player
            state.players[user_id] = Player(
                user_id=user_id,
                member=interaction.user,
                original_nickname=interaction.user.nick,
            )
            await self._update(interaction)
            await self.cog._persist_room_state()
            await interaction.followup.send("参加しました。", ephemeral=True)

    @discord.ui.button(label="参加取消", style=discord.ButtonStyle.danger, custom_id="leave_game", row=0)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.cog.is_private_room():
            return await interaction.response.send_message(
                "GM村の参加取消は、公開中の募集カードから行ってください。",
                ephemeral=True,
            )
        async with self.cog.action_lock:
            state = self.cog.state
            if state.phase != Phase.LOBBY:
                return await interaction.response.send_message("ゲーム中は取り消せません。", ephemeral=True)
            if interaction.user.id not in state.players:
                return await interaction.response.send_message("参加していません。", ephemeral=True)

            del state.players[interaction.user.id]
            if not state.players and state.gm_id is None:
                state.recruitment_id = None
            await interaction.response.defer()
            await self._update(interaction)
            await self.cog._persist_room_state()

    @discord.ui.button(label="GM取得", style=discord.ButtonStyle.success, custom_id="get_gm", row=0)
    async def gm_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.cog.is_private_room():
            return await interaction.response.send_message(
                "GM村のGM登録は、公開中の募集カードから行ってください。",
                ephemeral=True,
            )
        # 参加ボタンと同じ理由で全卓共通のjoin_lockを先に取る。
        # validate_gm_claim はランク確認でDBを待つため、卓ローカルのロック
        # だけでは別卓が同じ人をGMにできてしまう。取得順序は参加側と揃える
        async with self.cog.manager.join_lock, self.cog.action_lock:
            state = self.cog.state
            if state.phase != Phase.LOBBY:
                return await interaction.response.send_message("ゲーム中は変更できません。", ephemeral=True)
            if state.gm_id is not None:
                gm = state.guild.get_member(state.gm_id)
                return await interaction.response.send_message(
                    f"既にGMがいます: {gm.display_name if gm else 'unknown'}", ephemeral=True
                )
            gm_error = await self.cog.validate_gm_claim(interaction.user)
            if gm_error:
                return await interaction.response.send_message(gm_error, ephemeral=True)

            state.gm_id = interaction.user.id
            await interaction.response.defer()
            await self._update(interaction)
            await self.cog._persist_room_state()

    @discord.ui.button(label="GM放棄", style=discord.ButtonStyle.danger, custom_id="release_gm", row=0)
    async def gm_release_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.cog.is_private_room():
            return await interaction.response.send_message(
                "GM村のGM登録解除は、公開中の募集カードから行ってください。",
                ephemeral=True,
            )
        async with self.cog.action_lock:
            state = self.cog.state
            if state.phase != Phase.LOBBY:
                return await interaction.response.send_message("ゲーム中は変更できません。", ephemeral=True)
            if interaction.user.id != state.gm_id:
                return await interaction.response.send_message("あなたはGMではありません。", ephemeral=True)

            state.gm_id = None
            if not state.players:
                state.recruitment_id = None
            await interaction.response.defer()
            await self._update(interaction)
            await self.cog._persist_room_state()

    @discord.ui.button(
        label="通知", emoji="🔔", style=discord.ButtonStyle.primary,
        custom_id="recruitment_notification_toggle", row=0,
    )
    async def notification_button(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        await self.cog.manager.recruitment_manager.toggle_notification_role(interaction)

    @discord.ui.button(label="ゲーム開始", style=discord.ButtonStyle.success, custom_id="start_game",
                       disabled=True, row=1)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self.cog.action_lock:
            state = self.cog.state
            if state.phase != Phase.LOBBY:
                return await interaction.response.send_message("現在ゲーム中です。", ephemeral=True)
            if interaction.user.id != state.gm_id:
                return await interaction.response.send_message("GMのみがゲームを開始できます。", ephemeral=True)
            if len(state.players) != self.player_count:
                return await interaction.response.send_message(
                    f"参加者が揃っていません ({len(state.players)}/{self.player_count})",
                    ephemeral=True,
                )

            # ボタン無効化
            for child in self.children:
                child.disabled = True
            try:
                await interaction.response.edit_message(view=self)
            except (discord.NotFound, discord.HTTPException):
                # メッセージが既に削除されていてもゲームは開始する
                pass
            await self.cog.start_game(interaction)

    @discord.ui.button(label="次村", style=discord.ButtonStyle.primary, custom_id="rematch_game", row=1)
    async def rematch_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.cog.is_private_room():
            return await interaction.response.send_message(
                "GM村の次回参加は、新しい募集カードから受け付けます。",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.cog.action_lock:
            result = await self.cog.rematch(interaction.user)
            await self._update(interaction)
            await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="GM管理", style=discord.ButtonStyle.secondary, custom_id="lobby_gm_menu", row=1)
    async def gm_menu_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = self.cog.state
        if state.gm_id is None:
            return await interaction.response.send_message(
                "GMが決まっていません。先に「GM取得」を押してください。", ephemeral=True
            )
        if interaction.user.id != state.gm_id:
            return await interaction.response.send_message("GMのみ操作可能です。", ephemeral=True)
        if state.phase != Phase.LOBBY:
            return await interaction.response.send_message(
                "ゲーム中は #昼 のGMコントロールから操作してください。", ephemeral=True
            )
        await interaction.response.send_message(
            "受付中のGM操作です。",
            view=LobbyGMMenuView(self.cog),
            ephemeral=True,
        )

    @discord.ui.button(label="ルール", style=discord.ButtonStyle.secondary, custom_id="rule_btn", row=1)
    async def rule_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embeds = build_rule_embeds(getattr(self.cog, "variant", None))
        await interaction.response.send_message(embeds=embeds, ephemeral=True)

    @discord.ui.button(label="ヘルプ", style=discord.ButtonStyle.secondary, custom_id="help_btn", row=1)
    async def help_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embeds = build_help_embeds(getattr(self.cog, "variant", None))
        await interaction.response.send_message(embeds=embeds, ephemeral=True)

class LobbyGMMenuView(discord.ui.View):
    """ロビーを3段以内に保つため、低頻度のGM操作を一時表示する。"""

    def __init__(self, cog: RoomRunner) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        if self.cog.is_private_room():
            variant_button = discord.ui.Button(
                label="ゲーム形式を変更",
                style=discord.ButtonStyle.primary,
                custom_id=f"lobby_variant_change:{self.cog.state.room_id}",
            )
            variant_button.callback = self.change_variant_btn
            self.add_item(variant_button)

    def _is_gm(self, interaction: discord.Interaction) -> bool:
        state = self.cog.state
        return state.phase == Phase.LOBBY and interaction.user.id == state.gm_id

    async def change_variant_btn(self, interaction: discord.Interaction) -> None:
        if not self._is_gm(interaction):
            return await interaction.response.send_message(
                "現在のGMだけが操作できます。", ephemeral=True
            )
        await interaction.response.send_message(
            "変更先のゲーム形式を選んでください。",
            view=LobbyVariantSelectView(self.cog, interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(label="参加者を除外", style=discord.ButtonStyle.secondary)
    async def remove_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._is_gm(interaction):
            return await interaction.response.send_message(
                "現在のGMだけが操作できます。", ephemeral=True
            )
        players = list(self.cog.state.players.values())
        if not players:
            return await interaction.response.send_message(
                "参加者がいません。", ephemeral=True
            )
        options = [
            discord.SelectOption(label=p.member.display_name, value=str(p.user_id))
            for p in players[:25]
        ]
        await interaction.response.send_message(
            "参加を取り消すプレイヤーを選択してください。選択後に確認が出ます。",
            view=RemovePlayerSelectView(self.cog, options),
            ephemeral=True,
        )

    @discord.ui.button(label="受付をリセット", style=discord.ButtonStyle.danger)
    async def reset_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._is_gm(interaction):
            return await interaction.response.send_message(
                "現在のGMだけが操作できます。", ephemeral=True
            )

        async def execute(confirm_interaction: discord.Interaction) -> None:
            if not self._is_gm(confirm_interaction):
                await confirm_interaction.followup.send(
                    "受付状態が変わったため実行できません。", ephemeral=True
                )
                return
            result = await self.cog.reset_game()
            await confirm_interaction.followup.send(result, ephemeral=True)

        await interaction.response.send_message(
            "⚠️ 参加者とGMを全員解除して、参加受付を作り直します。実行しますか？",
            view=DangerConfirmView(
                interaction.user.id,
                execute,
                confirm_label="受付をリセット",
            ),
            ephemeral=True,
        )


class LobbyVariantSelectView(discord.ui.View):
    """GM村の参加受付中に、公開済みの3形式から選択する。"""

    _VARIANT_IDS = ("v13_cross", "v9_cross", "v9_turn")

    def __init__(self, cog: RoomRunner, actor_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.actor_id = actor_id
        current_variant_id = getattr(self.cog.variant, "variant_id", "")
        self.variant_select = discord.ui.Select(
            placeholder="ゲーム形式を選択",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=VARIANT_DEFINITIONS[variant_id].label,
                    value=variant_id,
                    default=variant_id == current_variant_id,
                )
                for variant_id in self._VARIANT_IDS
            ],
            custom_id=f"lobby_variant_select:{self.cog.state.room_id}",
        )
        self.variant_select.callback = self.select_callback
        self.add_item(self.variant_select)

    async def select_callback(self, interaction: discord.Interaction) -> None:
        state = self.cog.state
        if (
            interaction.user.id != self.actor_id
            or state.phase != Phase.LOBBY
            or state.gm_id != interaction.user.id
        ):
            return await interaction.response.send_message(
                "現在のGMだけが操作できます。", ephemeral=True
            )
        variant_id = self.variant_select.values[0]
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await self.cog.change_lobby_variant(
            interaction.user.id,
            variant_id,
        )
        await interaction.followup.send(message, ephemeral=True)


# ============================================================
# GM コントロールパネル
# ============================================================

_PHASE_LABELS = {
    Phase.LOBBY: "参加受付",
    Phase.PREPARATION: "役職確認・開始準備",
    Phase.INITIAL_NIGHT: "0日目初夜（人狼の挨拶）",
    Phase.DAY_DISCUSSION: "昼の議論",
    Phase.DAY_VOTE: "投票",
    Phase.DAY_RUNOFF_SPEECH: "決戦弁明",
    Phase.DAY_RUNOFF_VOTE: "決戦投票",
    Phase.DAY_LAST_WILL: "遺言",
    Phase.NIGHT: "夜",
    Phase.MORNING: "朝の結果発表",
    Phase.GAME_OVER: "終了処理",
    Phase.PAUSED: "一時停止",
}


def build_gm_status_embed(cog: RoomRunner) -> discord.Embed:
    """役職や未行動者を漏らさず、GMが必要な進行状況だけを表示する。"""
    state = cog.state
    effective_phase = state.phase_before_pause if state.phase == Phase.PAUSED else state.phase
    phase_label = _PHASE_LABELS.get(effective_phase, effective_phase.name)
    if state.phase == Phase.PAUSED:
        phase_label = f"一時停止中（停止前: {phase_label}）"

    alive = state.alive_players()
    embed = discord.Embed(
        title=f"🎮 {state.room_name} - GM状況",
        description=f"現在: **{phase_label}**",
        color=discord.Color.orange() if state.paused else discord.Color.blurple(),
    )
    embed.add_field(name="日数", value=f"{state.day_number}日目", inline=True)
    embed.add_field(
        name="生存者", value=f"{len(alive)} / {len(state.players)}人", inline=True
    )

    if effective_phase in (Phase.DAY_VOTE, Phase.DAY_RUNOFF_VOTE):
        if effective_phase == Phase.DAY_VOTE:
            # 列は押した人だけなので、分母は生存者数で出す。GMは
            # 「あと何人が押していないか」を見て締切の要否を判断する。
            queued = set(state.vote_order)
            not_pressed = [
                player for player in alive if player.user_id not in queued
            ]
            embed.add_field(
                name="投票発言",
                value=(
                    f"{min(state.vote_slot_index, len(state.vote_order))}"
                    f" / {len(alive)}人完了"
                    + (f"（未押下 {len(not_pressed)}人）" if not_pressed else "")
                    + ("\n締切済み" if state.vote_closed else "")
                ),
                inline=True,
            )
        else:
            alive_ids = {player.user_id for player in alive}
            alive_ids -= set(state.runoff_candidates)
            voted = len(alive_ids & set(state.votes))
            embed.add_field(name="投票", value=f"{voted} / {len(alive_ids)}人", inline=True)
        if effective_phase == Phase.DAY_VOTE and state.current_speaker_id is not None:
            speaker = state.get_player(state.current_speaker_id)
            embed.add_field(
                name="現在の投票者",
                value=speaker.display_name if speaker is not None else "確認中",
                inline=True,
            )
    elif effective_phase == Phase.NIGHT:
        required = {player.user_id for player in alive}
        ready = len(required & state.morning_ready_ids)
        embed.add_field(
            name="朝を迎える宣言", value=f"{ready} / {len(required)}人", inline=True
        )
    elif effective_phase == Phase.PREPARATION:
        required = {player.user_id for player in alive}
        ready = len(required & state.prep_ready_ids)
        embed.add_field(
            name="役職確認の宣言", value=f"{ready} / {len(required)}人", inline=True
        )
    elif effective_phase == Phase.DAY_DISCUSSION:
        turn_mode = getattr(cog, "is_turn_discussion_mode", None)
        if callable(turn_mode) and turn_mode():
            speaker = state.get_player(getattr(state, "current_speaker_id", None))
            durations = tuple(getattr(cog.variant, "turn_round_seconds", ()))
            daily_rounds = durations[:2] if state.day_number == 1 else durations[-1:]
            round_index = int(getattr(state, "turn_round_index", 0))
            remaining_interrupts = max(
                0,
                cog.variant.turn_interrupts_per_day
                - int(getattr(state, "turn_interrupts_used", 0)),
            )
            speaker_text = speaker.display_name if speaker is not None else "再開待ち"
            embed.add_field(
                name="ターン進行",
                value=(
                    f"話者: **{speaker_text}**\n"
                    f"巡: {min(round_index + 1, len(daily_rounds))} / {len(daily_rounds)}"
                    f" ／ 割り込み残り: {remaining_interrupts}回"
                ),
                inline=False,
            )

    if state.disconnected_players:
        waiting = [
            player.display_name
            for player in by_number(state.players.values())
            if player.user_id in state.disconnected_players
        ]
        embed.add_field(
            name="復帰待ち",
            value=", ".join(waiting) if waiting else "確認中",
            inline=False,
        )

    manager = cog.manager
    api_waiting = bool(
        getattr(manager, "bulk_api_lock", None)
        and manager.bulk_api_lock.locked()
    )
    start_waiting = bool(
        getattr(manager, "start_lock", None)
        and manager.start_lock.locked()
        and effective_phase == Phase.PREPARATION
    )
    if api_waiting or start_waiting:
        embed.add_field(
            name="処理状況",
            value="Discord APIへの変更を順番に処理しています。",
            inline=False,
        )
    embed.set_footer(text="役職・夜行動の内容は表示しません")
    return embed


class GMPanelEntryView(discord.ui.View):
    """#昼へ常設する、GM専用メニューへの小さな入口。"""

    def __init__(self, cog: RoomRunner) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.game_run_id = cog.state.game_run_id
        if self.game_run_id:
            cog.register_game_view(self)

    @discord.ui.button(
        label="GMメニュー・状況",
        style=discord.ButtonStyle.secondary,
        custom_id="gm_menu_open",
    )
    async def open_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        state = self.cog.state
        if (
            not self.game_run_id
            or state.game_run_id != self.game_run_id
            or state.phase == Phase.GAME_OVER
        ):
            return await interaction.response.send_message(
                "⏳ このゲームの操作パネルは終了しています。", ephemeral=True
            )
        if interaction.user.id != state.gm_id:
            return await interaction.response.send_message(
                "GMのみ操作可能です。", ephemeral=True
            )
        await interaction.response.send_message(
            embed=build_gm_status_embed(self.cog),
            view=GMControlView(self.cog),
            ephemeral=True,
        )


class GMControlView(discord.ui.View):
    # GMがパネルを開いたまま待つのは、たいてい8分の議論の途中。
    # 180秒だと事故が起きた頃には失効していて、一番押したい「一時停止」が
    # Discordの「インタラクションに失敗しました」になる。ephemeralの応答を
    # 編集できるのは15分までなので、その手前まで延ばして実用上失効させない。
    TIMEOUT_SECONDS = 870

    def __init__(self, cog: RoomRunner) -> None:
        super().__init__(timeout=self.TIMEOUT_SECONDS)
        self.cog = cog
        self.game_run_id = cog.state.game_run_id
        state = cog.state
        self.turn_token = int(getattr(state, "turn_slot_token", 0))
        effective_phase = state.phase_before_pause if state.phase == Phase.PAUSED else state.phase
        self.pause_btn.disabled = state.paused or self._settlement_locked()
        self.resume_btn.disabled = not state.paused and state.pending_winner is None
        self.force_morning_btn.disabled = (
            effective_phase != Phase.NIGHT
            or not getattr(state, "morning_ready_open", False)
            or self._settlement_locked()
        )
        self.force_prep_btn.disabled = (
            effective_phase != Phase.PREPARATION
            or state.paused
            or self._settlement_locked()
        )
        turn_actions_open = getattr(cog, "turn_actions_open", None)
        self.next_turn_btn.disabled = not (
            callable(turn_actions_open) and turn_actions_open()
        )
        self.skip_wait_btn.disabled = not (
            not state.paused
            and (
                (
                    effective_phase == Phase.INITIAL_NIGHT
                    and not getattr(state, "initial_night_completed", False)
                )
                or (
                    # 発言中は現在の枠を、列が空の待機中は投票そのものを締め切る
                    effective_phase == Phase.DAY_VOTE
                    and not getattr(state, "vote_closed", False)
                )
                or (
                    effective_phase in (Phase.DAY_RUNOFF_SPEECH, Phase.DAY_LAST_WILL)
                    and getattr(state, "current_speaker_id", None) is not None
                )
            )
            and not self._settlement_locked()
        )
        for item in (self.remove_btn, self.end_btn, self.reset_btn):
            item.disabled = self._settlement_locked()

    def _is_current(self) -> bool:
        # 終了精算中/保存失敗の安全停止中でも、GMの
        # 「再開(精算再試行)」と「強制終了」は逃げ道として残す。
        state = self.cog.state
        return (
            bool(self.game_run_id)
            and state.game_run_id == self.game_run_id
            and state.phase != Phase.GAME_OVER
        )

    def _settlement_locked(self) -> bool:
        state = self.cog.state
        return state.ending or state.pending_winner is not None

    # 操作は数秒かかることがある (ゲームループの停止待ちなど) ため、
    # 先にephemeralでdeferしてから結果をGMだけに返す。
    # 実際の告知 (一時停止しました等) は従来どおり #昼 に出る
    def _is_gm(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.cog.state.gm_id

    @discord.ui.button(label="状況更新", style=discord.ButtonStyle.secondary, custom_id="gm_status", row=0)
    async def status_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._is_current() or not self._is_gm(interaction):
            return await interaction.response.send_message(
                "現在のGMだけが状況を確認できます。", ephemeral=True
            )
        await interaction.response.edit_message(
            embed=build_gm_status_embed(self.cog),
            view=GMControlView(self.cog),
        )

    @discord.ui.button(label="一時停止", style=discord.ButtonStyle.primary, custom_id="gm_pause", row=0)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_current():
            return await interaction.response.send_message("⏳ このゲームの操作パネルは終了しています。", ephemeral=True)
        if interaction.user.id != self.cog.state.gm_id:
            return await interaction.response.send_message("GMのみ操作可能です。", ephemeral=True)
        if self._settlement_locked():
            return await interaction.response.send_message("結果保存・精算中です。一時停止はできません。", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.cog.pause_game()
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="再開", style=discord.ButtonStyle.success, custom_id="gm_resume", row=0)
    async def resume_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_current():
            return await interaction.response.send_message("⏳ このゲームの操作パネルは終了しています。", ephemeral=True)
        if interaction.user.id != self.cog.state.gm_id:
            return await interaction.response.send_message("GMのみ操作可能です。", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.cog.resume_game()
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="朝", style=discord.ButtonStyle.primary, custom_id="gm_force_morning", row=0)
    async def force_morning_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # 強制夜明けの唯一の入口。参加者向けのDMパネルには押せないボタンを
        # 並べないため、GM操作はここへ集約している。
        # 参加者のDMが届かない/誰かが戻らない場合の逃げ道でもある
        if not self._is_current():
            return await interaction.response.send_message("⏳ このゲームの操作パネルは終了しています。", ephemeral=True)
        if self._settlement_locked():
            return await interaction.response.send_message("結果保存・精算中のため夜明け操作はできません。", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.cog.action_lock:
            _, error = await self.cog.force_morning(interaction.user)
        await interaction.followup.send(
            error or "🌅 朝を迎えました。", ephemeral=True
        )

    @discord.ui.button(
        label="役職確認を締切",
        style=discord.ButtonStyle.primary,
        custom_id="gm_force_prep",
        row=0,
    )
    async def force_prep_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._is_current() or not self._is_gm(interaction):
            return await interaction.response.send_message(
                "現在のGMだけが操作できます。", ephemeral=True
            )
        if self._settlement_locked():
            return await interaction.response.send_message(
                "結果保存・精算中は操作できません。", ephemeral=True
            )

        async def execute(confirm_interaction: discord.Interaction) -> None:
            if not self._is_current() or not self._is_gm(confirm_interaction):
                await confirm_interaction.followup.send(
                    "ゲーム状態が変わったため実行できません。", ephemeral=True
                )
                return
            async with self.cog.action_lock:
                _, error = await self.cog.force_prep_complete(confirm_interaction.user)
            await confirm_interaction.followup.send(
                error or "▶️ 役職確認を締め切りました。", ephemeral=True
            )

        await interaction.response.send_message(
            "⚠️ 未確認者がいても役職確認を締め切り、0日目初夜へ進みますか？",
            view=DangerConfirmView(
                interaction.user.id, execute, confirm_label="締め切って開始"
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="次の発言へ",
        style=discord.ButtonStyle.secondary,
        custom_id="gm_force_next_turn",
        row=1,
    )
    async def next_turn_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._is_current() or not self._is_gm(interaction):
            return await interaction.response.send_message(
                "現在のGMだけが操作できます。", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.cog.force_next_turn(
            interaction.user.id, self.turn_token
        )
        await interaction.followup.send(
            result or "⏭️ 現在の発言を終了しました。", ephemeral=True
        )

    @discord.ui.button(
        label="スキップ",
        style=discord.ButtonStyle.secondary,
        custom_id="gm_skip_wait",
        row=1,
    )
    async def skip_wait_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._is_current() or not self._is_gm(interaction):
            return await interaction.response.send_message(
                "現在のGMだけが操作できます。", ephemeral=True
            )
        if self._settlement_locked():
            return await interaction.response.send_message(
                "結果保存・精算中は操作できません。", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.cog.action_lock:
            result = await self.cog.force_skip_wait(interaction.user)
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="強制終了", style=discord.ButtonStyle.danger, custom_id="gm_end", row=1)
    async def end_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_current():
            return await interaction.response.send_message("⏳ このゲームの操作パネルは終了しています。", ephemeral=True)
        if interaction.user.id != self.cog.state.gm_id:
            return await interaction.response.send_message("GMのみ操作可能です。", ephemeral=True)
        if self._settlement_locked():
            return await interaction.response.send_message("結果保存・精算中です。強制終了せず「再開」で精算を再試行してください。", ephemeral=True)
        async def execute(confirm_interaction: discord.Interaction) -> None:
            if not self._is_current() or not self._is_gm(confirm_interaction):
                await confirm_interaction.followup.send(
                    "ゲーム状態が変わったため実行できません。", ephemeral=True
                )
                return
            # 確認を待つ間に自然決着してしまう窓がある。精算中の
            # force_end は走行中の _end_game をキャンセルし、CancelledError は
            # _end_game の except Exception に捕まらないため、確定済みの勝敗と
            # レートを取りこぼしたまま廃村になる。押下時と同じ条件で再確認する。
            if self._settlement_locked():
                await confirm_interaction.followup.send(
                    "結果保存・精算中に勝敗が確定したため強制終了しませんでした。"
                    "「再開」で精算を再試行してください。",
                    ephemeral=True,
                )
                return
            await self.cog.force_end("GMにより強制終了されました。")
            await confirm_interaction.followup.send(
                "⏹️ ゲームを強制終了しました。", ephemeral=True
            )

        await interaction.response.send_message(
            "⚠️ 戦績を残さず廃村にします。本当に強制終了しますか？",
            view=DangerConfirmView(
                interaction.user.id, execute, confirm_label="強制終了する"
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="リセット", style=discord.ButtonStyle.danger, custom_id="gm_reset", row=1)
    async def reset_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_current():
            return await interaction.response.send_message("⏳ このゲームの操作パネルは終了しています。", ephemeral=True)
        if interaction.user.id != self.cog.state.gm_id:
            return await interaction.response.send_message("GMのみ操作可能です。", ephemeral=True)
        if self._settlement_locked():
            return await interaction.response.send_message("結果保存・精算中はリセットできません。", ephemeral=True)
        async def execute(confirm_interaction: discord.Interaction) -> None:
            if not self._is_current() or not self._is_gm(confirm_interaction):
                await confirm_interaction.followup.send(
                    "ゲーム状態が変わったため実行できません。", ephemeral=True
                )
                return
            # リセットも内部で force_end を呼ぶため、強制終了と同じ理由で
            # 確認待ちの間に確定した勝敗を潰さないよう再確認する
            if self._settlement_locked():
                await confirm_interaction.followup.send(
                    "結果保存・精算中に勝敗が確定したためリセットしませんでした。"
                    "「再開」で精算を再試行してください。",
                    ephemeral=True,
                )
                return
            result = await self.cog.reset_game()
            await confirm_interaction.followup.send(result, ephemeral=True)

        await interaction.response.send_message(
            "⚠️ 現在のゲームを廃村にして参加受付へ戻します。実行しますか？",
            view=DangerConfirmView(
                interaction.user.id, execute, confirm_label="ゲームをリセット"
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="プレイヤー除外", style=discord.ButtonStyle.secondary, custom_id="gm_remove", row=1)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_current():
            return await interaction.response.send_message("⏳ このゲームの操作パネルは終了しています。", ephemeral=True)
        if interaction.user.id != self.cog.state.gm_id:
            return await interaction.response.send_message("GMのみ操作可能です。", ephemeral=True)
        if self._settlement_locked():
            return await interaction.response.send_message("結果保存・精算中はプレイヤーを除外できません。", ephemeral=True)

        state = self.cog.state
        if state.phase == Phase.LOBBY:
            # ロビー中は番号未割り当て (全員0)。参加順のまま出す
            players = list(state.players.values())
        else:
            players = by_number(state.alive_players())

        if not players:
            return await interaction.response.send_message("対象プレイヤーがいません。", ephemeral=True)

        options = [
            discord.SelectOption(
                label=p.display_name if p.number else p.member.display_name,
                value=str(p.user_id),
            )
            for p in players[:25]
        ]
        view = RemovePlayerSelectView(self.cog, options)
        await interaction.response.send_message("除外するプレイヤーを選択:", view=view, ephemeral=True)


class RemovePlayerSelectView(discord.ui.View):
    def __init__(self, cog: RoomRunner, options: list[discord.SelectOption]) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.game_run_id = cog.state.game_run_id
        select = discord.ui.Select(
            placeholder="プレイヤーを選択",
            options=options,
            custom_id="remove_player_select",
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction) -> None:
        async with self.cog.action_lock:
            user_id = parse_select_id(interaction.data["values"][0])
            if user_id is None:
                return await interaction.response.send_message(
                    "❌ 不正な選択です。", ephemeral=True
                )
            state = self.cog.state
            if self.game_run_id and not self.cog.is_current_game_view(self.game_run_id):
                return await interaction.response.send_message("⏳ このゲームの操作は終了しています。", ephemeral=True)
            if state.gm_id != interaction.user.id:
                return await interaction.response.send_message("GMのみ操作可能です。", ephemeral=True)

            if state.phase == Phase.LOBBY:
                player = state.players.get(user_id)
                if player is None:
                    return await interaction.response.send_message(
                        "対象が見つかりません。", ephemeral=True
                    )
                display_name = player.member.display_name
            else:
                player = state.get_player(user_id)
                if player is None or not player.alive:
                    return await interaction.response.send_message(
                        "対象が見つかりません。", ephemeral=True
                    )
                display_name = player.display_name

        async def execute(confirm_interaction: discord.Interaction) -> None:
            async with self.cog.action_lock:
                state = self.cog.state
                if state.gm_id != confirm_interaction.user.id:
                    await confirm_interaction.followup.send(
                        "現在のGMだけが操作できます。", ephemeral=True
                    )
                    return
                if self.game_run_id:
                    if not self.cog.is_current_game_view(self.game_run_id):
                        await confirm_interaction.followup.send(
                            "ゲーム状態が変わったため実行できません。", ephemeral=True
                        )
                        return
                    player = state.get_player(user_id)
                    if player is None or not player.alive:
                        await confirm_interaction.followup.send(
                            "対象が既に死亡・除外されています。", ephemeral=True
                        )
                        return
                    await self.cog._eliminate_player_mid_game(
                        player, "GMの操作により"
                    )
                    await confirm_interaction.followup.send(
                        f"{display_name} をゲームから除外しました。", ephemeral=True
                    )
                    return

                if state.phase != Phase.LOBBY or user_id not in state.players:
                    await confirm_interaction.followup.send(
                        "受付状態が変わったため実行できません。", ephemeral=True
                    )
                    return
                del state.players[user_id]
                if not state.players and state.gm_id is None:
                    state.recruitment_id = None
                await self.cog._persist_room_state()
                if state.lobby_message:
                    try:
                        lobby_view = LobbyView(self.cog)
                        embed = lobby_view._build_embed()
                        await state.lobby_message.edit(embed=embed, view=lobby_view)
                    except discord.HTTPException:
                        pass
                await confirm_interaction.followup.send(
                    f"{display_name} の参加を取り消しました。", ephemeral=True
                )

        label = "ゲームから除外" if self.game_run_id else "参加を取り消す"
        await interaction.response.send_message(
            f"⚠️ **{display_name}** を{label}操作です。実行しますか？",
            view=DangerConfirmView(
                interaction.user.id,
                execute,
                confirm_label=label,
            ),
            ephemeral=True,
        )


# ============================================================
# 投票UI
# ============================================================

class VoteConfirmView(discord.ui.View):
    """通常投票・決戦投票に共通する、本人だけの最終確認。

    確定結果は新しいephemeralを足さず**この確認メッセージを書き換えて**残す。
    「押したのに反応が分からない」を消しつつ、ephemeralの枚数を増やさない。
    """

    def __init__(self, source: "_BaseVoteView", actor_id: int, target_id: int) -> None:
        super().__init__(timeout=30)
        self.source = source
        self.actor_id = actor_id
        self.target_id = target_id

    @discord.ui.button(label="この人に投票", style=discord.ButtonStyle.danger)
    async def confirm_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message(
                "投票を選択した本人だけが確定できます。", ephemeral=True
            )
        timer = InteractionTimer("投票の確定", interaction.user.id)
        # 引数なしのdeferはコンポーネント用の deferred_message_update。
        # この確認メッセージ自体を編集対象にできる (thinking=Trueだと
        # 別枠のephemeralが増えて確認が2枚になる)。DB保存を挟むので
        # 3秒の応答期限対策としてdefer自体は必要。
        await interaction.response.defer()
        timer.mark("ack")
        for item in self.children:
            item.disabled = True
        result, committed = await self.source.commit_vote(
            self.actor_id, self.target_id, timer
        )
        timer.mark("state")
        if committed:
            self.stop()
        else:
            # DBの一時失敗などで未確定なら、もう一度確定を試せるよう戻す
            for item in self.children:
                item.disabled = False
        try:
            await interaction.edit_original_response(content=result, view=self)
        except (discord.NotFound, discord.HTTPException):
            pass
        timer.mark("reply")
        timer.finish(note=f"committed={committed}")

    @discord.ui.button(label="選び直す", style=discord.ButtonStyle.secondary)
    async def cancel_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.actor_id:
            return await interaction.response.send_message(
                "投票を選択した本人だけが変更できます。", ephemeral=True
            )
        for item in self.children:
            item.disabled = True
        self.stop()
        await interaction.response.edit_message(
            content="↩️ 投票を確定せず、選び直します。", view=self
        )


class _VoteQueueButton(discord.ui.Button):
    """「投票」= 投票発言の列へ並ぶ。誰かの発言中でも先に並べる。"""

    def __init__(self, cog: RoomRunner) -> None:
        # custom_idは候補ボタン (vote_<id>) と前方一致しない名前にする。
        super().__init__(
            label="🗳️ 投票", style=discord.ButtonStyle.success, custom_id="join_vote"
        )
        self.cog = cog
        self.game_run_id = cog.state.game_run_id
        self.day_generation = cog.state.day_generation

    async def callback(self, interaction: discord.Interaction) -> None:
        state = self.cog.state
        # 公開パネルなので死亡者・観戦者にもボタンが見える。deferより前に
        # 弾き、押されるたびにDiscord APIを2回使わないようにする。
        if (
            not self.cog.is_current_day_view(self.game_run_id, self.day_generation)
            or self.cog._effective_phase() != Phase.DAY_VOTE
            or state.vote_closed
        ):
            return await interaction.response.send_message(
                "⏳ 今は投票を受け付けていません。", ephemeral=True
            )
        player = state.get_player(interaction.user.id)
        if player is None or not player.alive:
            return await interaction.response.send_message(
                "⏳ 生存中の参加者だけが押せます。", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.cog.join_vote_queue(interaction.user)
        await interaction.followup.send(result, ephemeral=True)


class VoteQueueView(discord.ui.View):
    """列が空のときの投票待ちパネル (並ぶボタンだけ)。"""

    def __init__(self, cog: RoomRunner) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        cog.register_game_view(self)
        self.add_item(_VoteQueueButton(cog))


class _BaseVoteView(discord.ui.View):
    expected_phase: Phase
    button_prefix: str
    button_style: discord.ButtonStyle
    persist_label: str
    # 通常投票のパネルだけ、発言中の人の候補ボタンと並べて
    # 「投票」(列へ並ぶ) を置く。決戦は一斉投票なので置かない。
    with_queue_button: bool = False

    def __init__(
        self,
        cog: RoomRunner,
        candidates: list,
        voters: list,
        *,
        provisional: bool = False,
    ) -> None:
        """通常投票・決戦投票の候補パネルを作る。

        provisionalは旧snapshot/UIを安全に拒否する互換引数として残すが、
        現行進行では議論中の仮投票パネルを生成しない。
        """
        super().__init__(timeout=None)
        self.cog = cog
        self.game_run_id = cog.state.game_run_id
        self.day_generation = cog.state.day_generation
        cog.register_game_view(self)
        self.voters = {v.user_id for v in voters}
        self.provisional = provisional
        self.vote_slot_token = int(getattr(cog.state, "vote_slot_token", 0))
        # 仮投票は議論フェーズ、確定は本来のフェーズでだけ受け付ける
        self.accept_phase = Phase.DAY_DISCUSSION if provisional else self.expected_phase

        # 13人なら5・5・3の3段。確認ボタンはephemeralなので公開行を増やさない。
        for player in candidates:
            btn = discord.ui.Button(
                label=player.display_name,
                style=self.button_style,
                custom_id=f"{self.button_prefix}_{player.user_id}",
            )
            btn.callback = self._make_callback(player.user_id)
            self.add_item(btn)
        if self.with_queue_button:
            self.add_item(_VoteQueueButton(cog))

    def _vote_error(self, voter_id: int, target_id: int) -> Optional[str]:
        state = self.cog.state
        if (
            not self.cog.is_current_day_view(self.game_run_id, self.day_generation)
            or state.phase != self.accept_phase
        ):
            return "⏳ 現在この操作はできません。"
        if voter_id not in self.voters:
            return "投票権がありません。"
        # 通常投票は列の先頭1人だけ受け付ける。slot tokenも照合し、
        # 1つ前の投票者に残った古いボタンが次の人の枠へ作用しないようにする。
        if (
            not self.provisional
            and self.expected_phase == Phase.DAY_VOTE
            and (
                not state.vote_slot_active
                or state.current_speaker_id != voter_id
                or self.vote_slot_token != state.vote_slot_token
            )
        ):
            return "自分の番になってから投票できます。"
        # 仮投票は入れ替え自由。確定後 (投票フェーズ) だけ二重投票を弾く
        if voter_id in state.votes and not self.provisional:
            return "投票済みです。"
        if voter_id == target_id:
            return "自分には投票できません。"
        voter = state.get_player(voter_id)
        if voter is None or not voter.alive:
            return "投票権がありません。"
        target = state.get_player(target_id)
        if target is None or not target.alive:
            return "その対象は既にゲームから除外されています。"
        return None

    def _make_callback(self, target_id: int):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            async with self.cog.action_lock:
                error = self._vote_error(interaction.user.id, target_id)
                target = self.cog.state.get_player(target_id)
                target_name = target.display_name if target is not None else "選択した相手"
            if error:
                return await interaction.followup.send(error, ephemeral=True)
            if self.provisional:
                prompt = (
                    f"**{target_name}** に投票します。よろしいですか？\n"
                    "投票フェーズに入るまでは、別の人を押せば入れ替えられます。"
                )
            else:
                prompt = f"**{target_name}** に投票しますか？確定後は変更できません。"
            await interaction.followup.send(
                prompt,
                view=VoteConfirmView(self, interaction.user.id, target_id),
                ephemeral=True,
            )

        return callback

    async def commit_vote(
        self,
        voter_id: int,
        target_id: int,
        timer: Optional[InteractionTimer] = None,
    ) -> tuple[str, bool]:
        """投票を確定する。(確認メッセージへ出す本文, 確定したか) を返す。

        表示は呼び出し側 (VoteConfirmView) が確認メッセージの書き換えで行う。
        ここでDiscordへ送らないのは、確定できたかどうかで確認ボタンを
        戻すか無効化するかが変わるため。

        timer を渡すと、卓ロックの待ちとDB保存を分けて計測できる。
        """
        async with self.cog.action_lock:
            if timer is not None:
                timer.mark("lock")
            state = self.cog.state
            error = self._vote_error(voter_id, target_id)
            if error:
                return error, False

            target = state.get_player(target_id)
            target_name = target.display_name if target is not None else "選択した相手"

            old_action_log_len = len(state.action_log)
            state.votes[voter_id] = target_id
            self.cog.log_action(
                self.persist_label, actor=state.get_player(voter_id),
                target=state.get_player(target_id),
            )
            try:
                await self.cog._persist_room_state()
            except Exception as e:
                state.votes.pop(voter_id, None)
                del state.action_log[old_action_log_len:]
                log.exception(f"{self.persist_label}の保存に失敗: {e}")
                return (
                    "❌ 投票を保存できませんでした。もう一度投票してください。",
                    False,
                )

            if not self.provisional and self.expected_phase == Phase.DAY_VOTE:
                # 保存した票を同じ公開パネルへ反映してから、現在の20秒枠を
                # 終了する。次の人へ進んだ後で表示すると、発言順と公開票の
                # 対応が一瞬ずれるため、この順序は崩さない。
                if not await self.cog._refresh_sequential_vote_panel():
                    return (
                        "⚠️ 投票は保存しましたが、公開できなかったため安全停止しました。"
                        "GMが再開すると同じ順番から続けます。",
                        True,
                    )
                state.speech_done_event.set()
            else:
                alive_voters = {
                    uid
                    for uid in self.voters
                    if state.get_player(uid) is not None and state.get_player(uid).alive
                }
                if alive_voters <= state.votes.keys():
                    state.vote_complete_event.set()
        if self.provisional:
            return (
                f"✅ **{target_name}** に投票しました。\n"
                "投票フェーズに入るまでは入れ替えられます。",
                True,
            )
        return f"✅ **{target_name}** に投票しました。", True


class VoteView(_BaseVoteView):
    expected_phase = Phase.DAY_VOTE
    button_prefix = "vote"
    button_style = discord.ButtonStyle.primary
    persist_label = "投票"
    with_queue_button = True


class RunoffVoteView(_BaseVoteView):
    expected_phase = Phase.DAY_RUNOFF_VOTE
    button_prefix = "runoff"
    button_style = discord.ButtonStyle.danger
    persist_label = "決戦投票"


# ============================================================
# 夜アクション: 人狼 (DM)
# ============================================================

class WolfSurrenderView(discord.ui.View):
    """実人狼の役職DMへ付け、試合中いつでも使えるサレンダー操作。"""

    def __init__(self, cog: RoomRunner) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.game_run_id = cog.state.game_run_id
        cog.register_game_view(self)

    @discord.ui.button(
        label="🏳️ サレンダー",
        style=discord.ButtonStyle.danger,
        custom_id="wolf_surrender",
    )
    async def surrender_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self.cog.is_current_game_view(self.game_run_id):
            return await interaction.response.send_message(
                "⏳ このゲームの操作は終了しています。", ephemeral=True
            )
        await _prompt_wolf_surrender(self.cog, interaction, self.game_run_id)


class WolfVoteView(discord.ui.View):
    """各人狼のDMに送信される襲撃選択UI。

    選択は即時反映され、夜の間は何度でも変更できる。
    複数の人狼がいる場合は「最後に選択された対象」が実行される。
    選択のたびに他の生存人狼へ通知される。
    """

    def __init__(self, cog: RoomRunner, targets: list, wolf_player) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.game_run_id = cog.state.game_run_id
        self.night_generation = cog.state.night_generation

        options = [
            discord.SelectOption(label=p.display_name, value=str(p.user_id))
            for p in targets
        ]
        options.append(
            discord.SelectOption(label="噛みなし (襲撃しない)", value="-1")
        )
        select = discord.ui.Select(
            placeholder="襲撃対象を選択 (夜の間は変更可)",
            options=options,
        )
        select.callback = self.select_callback
        self.add_item(select)

    def _validate_actor(self, interaction: discord.Interaction):
        """
        生存中の人狼 + NIGHTフェーズ かを判定
        古いDMビューからの操作 (死亡した人狼など) を弾く

        Returns:
            sender Player か None
        """
        state = self.cog.state
        if not self.cog.is_current_night_view(self.game_run_id, self.night_generation):
            return None
        sender = state.get_player(interaction.user.id)
        if sender is None or not sender.alive or not sender.is_wolf:
            return None
        return sender

    @discord.ui.button(
        label="🏳️ サレンダー",
        style=discord.ButtonStyle.danger,
        row=1,
    )
    async def surrender_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await _prompt_wolf_surrender(self.cog, interaction, self.game_run_id)

    async def select_callback(self, interaction: discord.Interaction) -> None:
        state = self.cog.state
        if self._validate_actor(interaction) is None:
            return await interaction.response.send_message(
                "⏳ 現在この操作はできません。", ephemeral=True
            )
        await interaction.response.defer()

        # 朝確定・他の人狼の選択と直列化し、保存した状態と
        # Discord上の表示が逆順になるのを防ぐ。
        async with self.cog.action_lock:
            sender = self._validate_actor(interaction)
            if sender is None:
                return await interaction.followup.send(
                    "⏳ 現在この操作はできません。", ephemeral=True
                )

            target_id = parse_select_id(interaction.data["values"][0])
            if target_id is None:
                return await interaction.followup.send(
                    "❌ 不正な対象です。", ephemeral=True
                )

            if target_id != -1:
                target = state.get_player(target_id)
                if target is None or not target.alive or target.is_wolf:
                    return await interaction.followup.send(
                        "❌ 不正な対象です。", ephemeral=True
                    )
                label = f"**{target.display_name}**"
            else:
                label = "**噛みなし**"

            # 選択即反映・上書き可: 最後に選択された対象が夜終了時に実行される
            # 同じ相手を選び直しただけならDM中継を省く (連打によるDM大量送信の抑制)
            old_vote = state.wolf_voters.get(interaction.user.id)
            old_target = state.wolf_target
            old_action_log_len = len(state.action_log)
            changed = old_vote != target_id
            state.wolf_voters[interaction.user.id] = target_id
            state.wolf_target = target_id
            self.cog.log_action(
                "襲撃先", actor=sender,
                target=state.get_player(target_id),
                detail="噛みなし" if target_id == -1 else ("変更" if old_vote is not None else "選択"),
            )
            try:
                await self.cog._persist_room_state()
            except Exception as e:
                if old_vote is None:
                    state.wolf_voters.pop(interaction.user.id, None)
                else:
                    state.wolf_voters[interaction.user.id] = old_vote
                state.wolf_target = old_target
                del state.action_log[old_action_log_len:]
                log.exception(f"襲撃先の保存に失敗: {e}")
                return await interaction.followup.send(
                    "❌ 襲撃先を保存できませんでした。もう一度選んでください。",
                    ephemeral=True,
                )

            # 時間切れ後の追加猶予を打ち切れるよう完了判定する
            self.cog._check_night_complete()

            # 自分のDM UIを「現在の襲撃先」入りに更新 (新規メッセージを増やさない)
            try:
                await interaction.edit_original_response(
                    content=self.cog.build_wolf_dm_content(state.night_duration),
                    view=self,
                )
            except (discord.NotFound, discord.HTTPException):
                pass

        if not changed:
            return

        # 他の狼へ届く分は自由文の中継と同じ窓で閉じる。
        # 制限時間後もこれが通ると、襲撃先を選び直すだけで合図を送れてしまい、
        # 「中継は夜の制限時間まで」が有名無実になる。
        # 自分の襲撃先の選択・変更自体は夜明けまで受け付けたまま
        # (未行動者へ警告DMを送って選ばせる設計のため)。
        if not self.cog.wolf_relay_open():
            # 他の狼の表示は古いままになるので、黙って食い違わせず本人に伝える
            try:
                await interaction.followup.send(
                    "⏰ 夜の制限時間を過ぎているため、この変更は**他の人狼へ通知されません**。\n"
                    f"あなたの選択 {label} は襲撃先として反映されています。",
                    ephemeral=True,
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            return

        # 他の生存人狼のDM UIも更新し、変更を通知する
        await self.cog.refresh_wolf_dm_displays(
            state.night_duration, exclude_id=interaction.user.id
        )
        await self.cog._relay_to_wolves(
            f"🐺 {sender.display_name} が襲撃先を {label} に変更しました。",
            exclude_id=interaction.user.id,
        )


# ============================================================
# 夜アクション: 占い師 (DM)
# ============================================================

class NightActionConfirmView(discord.ui.View):
    """占い・護衛の実行確認 (誤タップ防止)。

    選択直後にエフェメラルで表示し、「実行する」を押して初めて確定する。
    確定した行動は取り消せない (占いは確定と同時に結果を開示するため)。
    キャンセル時は元のDMのセレクトを作り直す: Discordのセレクトは
    「既に選択済みの項目」を選び直しても操作が飛ばないため、
    作り直さないと同じ相手を選べなくなる。
    """

    def __init__(
        self,
        origin: SeerView | GuardView,
        target,
        *,
        label: str,
        origin_message: Optional[discord.Message],
    ) -> None:
        # 夜は「全員が朝を迎えるを押すまで」続くので長さが読めない。
        # View側のtimeoutは設けず、夜の終わりに _night_views で一括stopする
        super().__init__(timeout=None)
        origin.cog.register_game_view(self, night=True)
        self.origin = origin
        self.cog = origin.cog
        self.target = target
        self.label = label
        # 元のセレクトが載っているDMメッセージ。
        # 確認UIはエフェメラルなので、そのコールバックの interaction.message は
        # エフェメラル側を指す。元のDMを触るにはここで持っておく必要がある
        self.origin_message = origin_message
        self.game_run_id = origin.game_run_id
        self.night_generation = origin.night_generation

    async def on_timeout(self) -> None:
        # ephemeral Viewはdiscord.pyにより最大15分へ補正される。
        # 無期限の夜でも同じ対象を選び直せるよう、失効時に元セレクトを再生成する。
        if self.cog.is_current_night_view(self.game_run_id, self.night_generation):
            await self._rebuild_origin(locked=False)

    async def _rebuild_origin(self, *, locked: bool) -> None:
        """元のDMのセレクトを作り直す。

        locked=True : 確定済みとして無効化する
        locked=False: 未選択状態に戻す (Discordのセレクトは選択済みの項目を
                      選び直しても操作が飛ばないため、作り直さないと同じ相手を選べない)
        """
        if self.origin_message is None:
            return
        state = self.cog.state
        # 同じセレクトから確認ダイアログが複数開かれた場合、
        # 一方が確定した後に他方のキャンセル/タイムアウトが
        # 元セレクトを再度有効化しないよう、実状態を最終決定にする。
        if isinstance(self.origin, SeerView) and state.seer_target is not None:
            locked = True
        elif isinstance(self.origin, GuardView) and state.guard_target is not None:
            locked = True
        if not locked and not self.cog.is_current_night_view(
            self.game_run_id, self.night_generation
        ):
            return
        view = type(self.origin)(self.cog, self.origin.targets)
        self.cog.register_game_view(view, night=True)
        if locked:
            for item in view.children:
                item.disabled = True
        try:
            await self.origin_message.edit(view=view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"夜アクションUI再生成失敗: {e}")

    @discord.ui.button(label="実行する", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.cog.is_current_night_view(self.game_run_id, self.night_generation):
            return await interaction.response.send_message(
                "⏳ この夜の操作受付は終了しています。", ephemeral=True
            )
        # SQLite busy_timeout中でも3秒のInteraction応答期限を超えない。
        await interaction.response.defer()
        async with self.cog.action_lock:
            for item in self.children:
                item.disabled = True
            result, committed = await self.origin.commit(self.target)
            self.stop()
            try:
                await interaction.edit_original_response(content=result, view=self)
            except (discord.NotFound, discord.HTTPException):
                pass
            # 確定したらセレクトを無効化する (二重操作の見た目上の防止)
            if committed:
                await self._rebuild_origin(locked=True)
            else:
                # DB一時失敗等で未確定なら、同じ項目をもう一度
                # 選べるよう元セレクトを作り直す。
                await self._rebuild_origin(locked=False)

    @discord.ui.button(label="やめる", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.cog.is_current_night_view(self.game_run_id, self.night_generation):
            return await interaction.response.send_message(
                "⏳ この夜の操作受付は終了しています。", ephemeral=True
            )
        await interaction.response.defer()
        async with self.cog.action_lock:
            for item in self.children:
                item.disabled = True
            self.stop()
            try:
                await interaction.edit_original_response(
                    content=f"↩️ キャンセルしました。{self.label}を選び直してください。", view=self
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            await self._rebuild_origin(locked=False)


class SeerView(discord.ui.View):
    """占い師のDMに送る占い先セレクト。

    選択 → 実行確認 → 確定と同時に結果を開示する (1晩1回・変更不可)。
    """

    def __init__(self, cog: RoomRunner, targets: list) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.targets = targets
        self.game_run_id = cog.state.game_run_id
        self.night_generation = cog.state.night_generation
        # 確認UIから確定するときの操作者 (このViewは本人のDMにしか届かない)
        self.actor_id: Optional[int] = None

        options = [
            discord.SelectOption(label=p.display_name, value=str(p.user_id))
            for p in targets
        ]
        select = discord.ui.Select(
            placeholder="占う対象を選択 (確認後に確定)",
            options=options,
        )
        select.callback = self.select_callback
        self.add_item(select)

    def _validate(self, user_id: int) -> Optional[str]:
        """操作できない理由を返す (操作可能なら None)"""
        state = self.cog.state
        # 古いDMビューからの操作を弾く (死亡した占い師など)
        if not self.cog.is_current_night_view(self.game_run_id, self.night_generation):
            return "⏳ 現在この操作はできません。"
        sender = state.get_player(user_id)
        if sender is None or not sender.alive or sender.role != Role.SEER:
            return "⏳ 現在この操作はできません。"
        if state.seer_target is not None:
            self.cog.log_action(
                "占い(拒否)", actor=state.get_player(user_id),
                detail="既に確定済みのため拒否",
            )
            return "✅ 今夜の占いは既に確定しています。変更はできません。"
        return None

    async def select_callback(self, interaction: discord.Interaction) -> None:
        state = self.cog.state

        error = self._validate(interaction.user.id)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)

        target_id = parse_select_id(interaction.data["values"][0])
        if target_id is None:
            return await interaction.response.send_message(
                "❌ 不正な対象です。", ephemeral=True
            )
        target = state.get_player(target_id)
        if target is None or not target.alive:
            return await interaction.response.send_message(
                "❌ その対象は占えません (存在しないか、既に死亡しています)。", ephemeral=True
            )

        self.actor_id = interaction.user.id
        await interaction.response.send_message(
            f"🔮 **{target.display_name}** を占います。よろしいですか？\n"
            "確定すると結果がすぐ表示され、今夜は変更できません。",
            view=NightActionConfirmView(
                self, target, label="占い先", origin_message=interaction.message
            ),
            ephemeral=True,
        )

    async def commit(self, target) -> tuple[str, bool]:
        """確認後の確定処理。(表示文字列, 確定したか) を返す (結果は即時開示)"""
        state = self.cog.state

        error = self._validate(self.actor_id)
        if error:
            return error, False
        if not target.alive:
            return "❌ その対象は占えません (既に死亡しています)。", False

        old_action_log_len = len(state.action_log)
        state.seer_target = target.user_id
        result = "**人狼**" if target.role == Role.WEREWOLF else "**村人**"
        self.cog.log_action(
            "占い", actor=state.get_player(self.actor_id), target=target,
            detail=f"結果={'人狼' if target.role == Role.WEREWOLF else '村人'}",
        )
        try:
            await self.cog._persist_room_state()
        except Exception as e:
            state.seer_target = None
            del state.action_log[old_action_log_len:]
            log.exception(f"占い結果の保存に失敗: {e}")
            return "❌ 占い結果を保存できませんでした。もう一度実行してください。", False
        # 未行動警告の対象から外す
        self.cog._check_night_complete()

        text = f"🔮 占い結果: **{target.display_name}** は {result} でした。"
        # 通常DMでも送って手元に残す。確認UIはエフェメラルなので、
        # 閉じるかクライアントを再読み込みすると結果が消える。一方
        # seer_target は確定済みで占い直せないため、DMが無いと
        # その夜の占いを失う。霊媒結果・初日白と同じ扱いに揃える。
        await self.cog.deliver_seer_result(self.actor_id, text)
        return text, True


# ============================================================
# 夜アクション: 狩人 (DM)
# ============================================================

class GuardView(discord.ui.View):
    """狩人のDMに送る護衛先セレクト。

    選択 → 実行確認 → 確定 (1晩1回・変更不可)。
    """

    def __init__(self, cog: RoomRunner, targets: list) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.targets = targets
        # Discordのセレクト表示だけを信頼せず、確認確定時にもこの夜に
        # 提示した候補だけを受け付ける。
        self.target_ids = frozenset(player.user_id for player in targets)
        self.game_run_id = cog.state.game_run_id
        self.night_generation = cog.state.night_generation
        # 確認UIから確定するときの操作者 (このViewは本人のDMにしか届かない)
        self.actor_id: Optional[int] = None

        options = [
            discord.SelectOption(label=p.display_name, value=str(p.user_id))
            for p in targets
        ]
        select = discord.ui.Select(
            placeholder="護衛対象を選択 (確認後に確定)",
            options=options,
        )
        select.callback = self.select_callback
        self.add_item(select)

    def _validate(self, user_id: int) -> Optional[str]:
        """操作できない理由を返す (操作可能なら None)"""
        state = self.cog.state
        # 古いDMビューからの操作を弾く (死亡した狩人など)
        if not self.cog.is_current_night_view(self.game_run_id, self.night_generation):
            return "⏳ 現在この操作はできません。"
        sender = state.get_player(user_id)
        if sender is None or not sender.alive or sender.role != Role.GUARD:
            return "⏳ 現在この操作はできません。"
        if state.guard_target is not None:
            self.cog.log_action(
                "護衛(拒否)", actor=state.get_player(user_id),
                detail="既に確定済みのため拒否",
            )
            return "✅ 今夜の護衛は既に確定しています。変更はできません。"
        return None

    def _validate_target(self, actor_id: int, target) -> Optional[str]:
        """表示候補を迂回した護衛先も最終的に拒否する。"""
        state = self.cog.state
        if target is None or target.user_id not in self.target_ids:
            return "❌ その対象は護衛できません。"
        if target.user_id == actor_id:
            return "⚠️ 自分は護衛できません。"
        if target.user_id == state.guard_previous:
            return "⚠️ 前回と同じ対象は護衛できません。"
        if not target.alive:
            return "❌ その対象は護衛できません (既に死亡しています)。"
        return None

    async def select_callback(self, interaction: discord.Interaction) -> None:
        state = self.cog.state

        error = self._validate(interaction.user.id)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)

        target_id = parse_select_id(interaction.data["values"][0])
        if target_id is None:
            return await interaction.response.send_message(
                "❌ 不正な対象です。", ephemeral=True
            )

        target = state.get_player(target_id)
        target_error = self._validate_target(interaction.user.id, target)
        if target_error:
            return await interaction.response.send_message(
                target_error, ephemeral=True
            )

        self.actor_id = interaction.user.id
        await interaction.response.send_message(
            f"🛡️ **{target.display_name}** を護衛します。よろしいですか？\n"
            "確定すると今夜は変更できません。",
            view=NightActionConfirmView(
                self, target, label="護衛先", origin_message=interaction.message
            ),
            ephemeral=True,
        )

    async def commit(self, target) -> tuple[str, bool]:
        """確認後の確定処理。(表示文字列, 確定したか) を返す"""
        state = self.cog.state

        error = self._validate(self.actor_id)
        if error:
            return error, False
        target_error = self._validate_target(self.actor_id, target)
        if target_error:
            return target_error, False

        old_action_log_len = len(state.action_log)
        state.guard_target = target.user_id
        self.cog.log_action(
            "護衛", actor=state.get_player(self.actor_id), target=target,
        )
        try:
            await self.cog._persist_room_state()
        except Exception as e:
            state.guard_target = None
            del state.action_log[old_action_log_len:]
            log.exception(f"護衛先の保存に失敗: {e}")
            return "❌ 護衛先を保存できませんでした。もう一度実行してください。", False
        # 未行動警告の対象から外す
        self.cog._check_night_complete()
        return f"🛡️ **{target.display_name}** の護衛を確定しました。", True


# ============================================================
# 役職を確認した (役職確認タイムの終了宣言)
# ============================================================

class PrepReadyView(discord.ui.View):
    """役職確認タイムの間 #昼 に掲示するパネル。

    参加者全員が「役職を確認した」を押すと0日目初夜へ進む。目安時間が切れても
    自動では進まないので、DMを開けていない人を待てる。
    """

    def __init__(self, cog: RoomRunner) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.game_run_id = cog.state.game_run_id
        cog.register_game_view(self)

    def _is_current(self) -> bool:
        return self.cog.is_current_game_view(self.game_run_id)

    @discord.ui.button(label="📩 役職を確認した", style=discord.ButtonStyle.success)
    async def ready_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_current():
            return await interaction.response.send_message(
                "⏳ このパネルは終了しています。", ephemeral=True
            )
        timer = InteractionTimer("役職を確認した", interaction.user.id)
        # 13人同時押下でも Discord の3秒応答期限を失わないよう先にACKする。
        await interaction.response.defer()
        timer.mark("ack")
        try:
            async with self.cog.action_lock:
                timer.mark("lock")
                content, error = await self.cog.toggle_prep_ready(interaction.user)
                timer.mark("state")
                if error:
                    return await interaction.followup.send(error, ephemeral=True)
                try:
                    await interaction.edit_original_response(content=content, view=self)
                except (discord.NotFound, discord.HTTPException):
                    pass
                timer.mark("reply")
        finally:
            timer.finish()


# ============================================================
# 朝を迎える (夜フェーズの終了宣言)
# ============================================================

class MorningReadyView(discord.ui.View):
    """夜の制限時間終了後、`#昼` に1枚だけ掲示するパネル。

    生存者全員が「朝を迎える」を押すと夜が明ける。宣言は一方向で、
    公開パネルの人数を押下ごとに更新する。AFKで止まったままにならないための強制夜明けは
    GMコントロールパネルの「朝」だけに置く (このパネルには参加者用の
    ボタンしか出さず、押せないボタンで紛らわせない)。

    夜は `#昼` の書き込みが止まっているが、**ボタン押下は送信権限とは
    無関係**なので押せる (同じ条件で PrepReadyView が動いている)。

    夜時間中はパネル自体が存在しないため、人数表示から夜行動の進捗を
    読まれることはない。受付開始後は準備確認と同じ0/N表示にする。
    """

    def __init__(self, cog: RoomRunner) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.game_run_id = cog.state.game_run_id
        self.night_generation = cog.state.night_generation
        cog.register_game_view(self)

    @discord.ui.button(label="🌅 朝を迎える", style=discord.ButtonStyle.success)
    async def ready_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if (
            not self.cog.is_current_game_view(self.game_run_id)
            or self.cog.state.night_generation != self.night_generation
            or self.cog._effective_phase() != Phase.NIGHT
            or not self.cog.state.morning_ready_open
        ):
            return await interaction.response.send_message("⏳ この夜のパネルは終了しています。", ephemeral=True)
        # 公開チャンネルのパネルなので死亡者・観戦者にもボタンが見える。
        # deferより前に弾き、押されるたびに2回APIを使わないようにする。
        player = self.cog.state.get_player(interaction.user.id)
        if player is None or not player.alive:
            return await interaction.response.send_message(
                "⏳ 生存中の参加者だけが押せます。", ephemeral=True
            )
        timer = InteractionTimer("朝を迎える", interaction.user.id)
        # 13人同時押下で後続がlock+DB保存待ちになっても
        # Discordの3秒応答期限を失わないよう先にACKする。
        await interaction.response.defer()
        timer.mark("ack")
        async with self.cog.action_lock:
            timer.mark("lock")
            content, error = await self.cog.toggle_morning_ready(interaction.user)
            timer.mark("state")
        # 成否は本人へ返し、公開人数はrunner側が保存後に同じパネルへ反映する。
        await interaction.followup.send(error or content, ephemeral=True)
        timer.mark("reply")
        timer.finish()


# ============================================================
# 弁明終了ボタン
# ============================================================

class WolfGuessSelectView(discord.ui.View):
    """死亡者本人のDMにだけ送る人狼予想UI。"""

    def __init__(self, cog: RoomRunner, user_id: int, death_event_id: str) -> None:
        super().__init__(timeout=WOLF_GUESS_TIMEOUT)
        self.cog = cog
        self.user_id = user_id
        self.game_run_id = cog.state.game_run_id
        self.death_event_id = death_event_id
        self.guess_slots = int(
            getattr(getattr(cog, "variant", None), "wolf_guess_slots", BONUS_WOLF_GUESS_SLOTS)
        )
        options = [
            discord.SelectOption(
                # display_name自体が「01.名前」形式なので番号を重ねない。
                label=player.display_name[:100],
                value=str(player.user_id),
            )
            for player in sorted(cog.state.players.values(), key=lambda p: p.number)
            if player.user_id != user_id
        ]
        self.select = discord.ui.Select(
            placeholder=f"人狼だと思う{self.guess_slots}人を選ぶ",
            min_values=self.guess_slots,
            max_values=self.guess_slots,
            options=options[:25],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)
        cog.register_game_view(self)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "この提出は本人だけが操作できます。", ephemeral=True
            )
            return
        targets = [int(value) for value in self.select.values]
        accepted = await self.cog.submit_wolf_guess(
            self.user_id,
            targets,
            game_run_id=self.game_run_id,
            death_event_id=self.death_event_id,
        )
        self.stop()
        if not accepted:
            await interaction.response.edit_message(
                content="⏳ この人狼予想の受付は終了しているか、既に提出済みです。",
                view=None,
            )
            return
        names = "、".join(
            player.display_name
            for player in (self.cog.state.get_player(pid) for pid in targets)
            if player is not None
        )
        await interaction.response.edit_message(
            content=(
                f"✅ **{names}** で提出しました。\n"
                "実際の人狼本人を除き、的中数が試合終了後のレート変動に反映されます。"
                "霊界へどうぞ。"
            ),
            view=None,
        )


class SpeechDoneView(discord.ui.View):
    """決戦弁明・遺言の終了ボタン。

    タイマー管理はゲームループの _pausable_countdown に一本化しているため、
    Viewにはtimeoutを設定しない (一時停止中に弁明が終了扱いになるのを防ぐ)。
    """

    def __init__(self, cog: RoomRunner, speaker_id: int, *, label: str = "弁明終了") -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.speaker_id = speaker_id
        self.game_run_id = cog.state.game_run_id
        self.day_generation = cog.state.day_generation
        cog.register_game_view(self)
        self.done_btn.label = label

    @discord.ui.button(label="弁明終了", style=discord.ButtonStyle.secondary, custom_id="speech_done")
    async def done_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        state = self.cog.state
        if not self.cog.is_current_day_view(self.game_run_id, self.day_generation):
            return await interaction.response.send_message(
                "⏳ この発言時間は終了しています。", ephemeral=True
            )
        # 弁明/遺言フェーズ中かつ「このViewの発言者の番」のみ有効
        # (過去の弁明者のボタンが現在の弁明を終了させるのを防ぐ)
        if (
            state.phase not in (Phase.DAY_RUNOFF_SPEECH, Phase.DAY_LAST_WILL)
            or state.current_speaker_id != self.speaker_id
        ):
            return await interaction.response.send_message(
                "⏳ 現在この操作はできません。", ephemeral=True
            )
        if interaction.user.id != self.speaker_id and interaction.user.id != state.gm_id:
            return await interaction.response.send_message("本人またはGMのみ操作可能です。", ephemeral=True)
        button.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except (discord.NotFound, discord.HTTPException):
            pass
        state.speech_done_event.set()


class TurnSpeechView(discord.ui.View):
    """ターン制の発言終了・公開CO・村全体の30秒割り込み。"""

    def __init__(
        self,
        cog: RoomRunner,
        speaker_id: int,
        turn_token: int,
        *,
        allow_interrupt: bool,
        allow_co_declaration: bool,
    ) -> None:
        # pause中にViewだけ失効しないよう、発言タイマーと同じく無期限にする。
        super().__init__(timeout=None)
        self.cog = cog
        self.speaker_id = speaker_id
        self.turn_token = turn_token
        self.game_run_id = cog.state.game_run_id
        self.day_generation = cog.state.day_generation
        self.interrupt_btn.disabled = not allow_interrupt
        if not allow_co_declaration:
            self.remove_item(self.co_declaration_btn)
        cog.register_game_view(self)

    def _is_current(self) -> bool:
        return self.cog.is_current_day_view(
            self.game_run_id, self.day_generation
        )

    @discord.ui.button(
        label="発言終了（パス）",
        style=discord.ButtonStyle.secondary,
        custom_id="turn_speech_pass",
    )
    async def pass_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._is_current():
            return await interaction.response.send_message(
                "⏳ この発言枠は終了しています。", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        error = await self.cog.request_turn_pass(
            interaction.user.id, self.speaker_id, self.turn_token
        )
        await interaction.followup.send(
            error or "✅ 発言を終了しました。", ephemeral=True
        )

    @discord.ui.button(
        label="30秒割り込み",
        style=discord.ButtonStyle.primary,
        custom_id="turn_speech_interrupt",
    )
    async def interrupt_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._is_current():
            return await interaction.response.send_message(
                "⏳ この発言枠は終了しています。", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        error, remaining = await self.cog.request_turn_interrupt(
            interaction.user.id, self.turn_token
        )
        await interaction.followup.send(
            error or f"⚡ 割り込みを受け付けました（本日の残り **{remaining}回**）。",
            ephemeral=True,
        )

    @discord.ui.button(
        label="COを宣言",
        style=discord.ButtonStyle.success,
        custom_id="turn_speech_co_declaration",
    )
    async def co_declaration_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._is_current():
            return await interaction.response.send_message(
                "⏳ この発言枠は終了しています。", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        error = await self.cog.request_turn_co_declaration(
            interaction.user.id, self.turn_token
        )
        await interaction.followup.send(
            error or "📣 COを公開しました。役職・内容はVCで話してください。",
            ephemeral=True,
        )


# ============================================================
# 終了後推薦 (DM)
# ============================================================

class PostgameRecommendationView(discord.ui.View):
    """対象選択と最終確認を1つの ephemeral 内で完結させる投票UI。

    kind で2種類の票を使い分ける:
      recommend — 霊媒師・初日処刑者・初夜襲撃死者が参加者の誰かへ+1
      postgame  — 勝利陣営が敗北陣営の誰かへ+1
    どちらも匿名で、1票につき設定されたレートボーナスを加える。
    """

    def __init__(
        self,
        *,
        game_id: int,
        guild_id: int,
        voter_id: int,
        candidates: list,
        timeout: float,
        on_confirmed: Callable[[int, str], None],
        kind: str = "recommend",
        title: str = "終了後推薦（+1レート）",
    ) -> None:
        super().__init__(timeout=timeout)
        self.game_id = game_id
        self.guild_id = guild_id
        self.voter_id = voter_id
        self.kind = kind
        self.title_text = title
        self.candidates = {player.user_id: player for player in candidates}
        self.selected_id: Optional[int] = None
        self.on_confirmed = on_confirmed
        self.message: Optional[discord.Message] = None

        options = [
            discord.SelectOption(
                label=player.display_name[:100],
                value=str(player.user_id),
            )
            for player in sorted(candidates, key=lambda item: item.number)
            if player.user_id != voter_id
        ]
        select = discord.ui.Select(
            placeholder="+1を贈る参加者を選択",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )
        select.callback = self._select_target
        self.add_item(select)
        self.confirm_btn.disabled = True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="⏳ 終了後推薦の受付時間は終了しました。",
                view=self,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.voter_id:
            return True
        await interaction.response.send_message(
            "この推薦票は本人だけが操作できます。", ephemeral=True
        )
        return False

    async def _select_target(self, interaction: discord.Interaction) -> None:
        select = next(
            item for item in self.children if isinstance(item, discord.ui.Select)
        )
        target_id = int(select.values[0])
        target = self.candidates.get(target_id)
        if target is None or target_id == self.voter_id:
            return await interaction.response.send_message(
                "その参加者には推薦できません。", ephemeral=True
            )
        self.selected_id = target_id
        self.confirm_btn.disabled = False
        await interaction.response.edit_message(
            content=(
                f"👏 **{self.title_text}**\n"
                f"**{target.display_name}** に確定しますか？\n"
                "確定後は変更できません。投票者名は公開されません。"
            ),
            view=self,
        )

    @discord.ui.button(
        label="この人に+1を贈る",
        style=discord.ButtonStyle.success,
        row=1,
    )
    async def confirm_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.selected_id is None:
            return await interaction.response.send_message(
                "先に参加者を選んでください。", ephemeral=True
            )
        await interaction.response.defer()
        try:
            result = await database.confirm_game_recommendation(
                self.game_id,
                self.guild_id,
                self.voter_id,
                self.selected_id,
                kind=self.kind,
            )
        except Exception as e:
            log.exception("終了後推薦の確定保存に失敗: %s", e)
            try:
                await interaction.edit_original_response(
                    content="❌ 推薦を保存できませんでした。少し待ってもう一度お試しください。",
                    view=self,
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            return
        if result == "confirmed":
            for item in self.children:
                item.disabled = True
            self.stop()
            self.on_confirmed(self.voter_id, self.kind)
            target = self.candidates[self.selected_id]
            content = (
                f"✅ **{target.display_name}** への推薦を確定しました。\n"
                "集計後に、推薦者名を伏せて結果だけが公開されます。"
            )
        elif result in {"already", "already_other"}:
            for item in self.children:
                item.disabled = True
            self.stop()
            content = "✅ この試合の推薦は既に確定済みです。"
        elif result == "self":
            content = "自分には推薦できません。"
        elif result == "expired":
            for item in self.children:
                item.disabled = True
            self.stop()
            content = "⏳ 推薦の受付時間は終了しました。"
        else:
            content = "❌ 推薦を保存できませんでした。対象を確認してもう一度お試しください。"
        try:
            await interaction.edit_original_response(content=content, view=self)
        except (discord.NotFound, discord.HTTPException):
            pass


# ============================================================
# 統計UI
# ============================================================

class FeedbackModal(discord.ui.Modal, title="不具合・改善を報告"):
    summary = discord.ui.TextInput(
        label="内容",
        placeholder="何が起きたか、どこが使いにくかったかを書いてください",
        style=discord.TextStyle.paragraph,
        min_length=3,
        max_length=1000,
    )
    details = discord.ui.TextInput(
        label="発生状況や改善案（任意）",
        placeholder="いつ・どの操作で起きたか、理想の動きなど",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, cog: GameCog, category: str) -> None:
        super().__init__()
        self.cog = cog
        self.category = category

    def _find_room_context(
        self, interaction: discord.Interaction
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        user_id = interaction.user.id
        channel_id = getattr(interaction, "channel_id", None)
        if channel_id is None and getattr(interaction, "channel", None) is not None:
            channel_id = interaction.channel.id

        member_room = None
        for room in self.cog.rooms.values():
            state = room.state
            channel_ids = {
                getattr(channel, "id", None)
                for channel in (
                    state.lobby_channel,
                    state.village_channel,
                    state.spirit_channel,
                )
                if channel is not None
            }
            if channel_id in channel_ids:
                return state.room_id, state.room_name, state.phase.name
            if user_id == state.gm_id or user_id in state.players:
                member_room = state
        if member_room is None:
            return None, None, None
        return member_room.room_id, member_room.room_name, member_room.phase.name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "サーバー内でのみ報告できます。", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        room_id, room_name, phase = self._find_room_context(interaction)
        try:
            report_id = await database.save_feedback_report(
                guild_id=interaction.guild.id,
                user_id=interaction.user.id,
                category=self.category,
                summary=str(self.summary.value).strip(),
                details=str(self.details.value).strip() or None,
                bot_version=BOT_VERSION,
                room_id=room_id,
                room_name=room_name,
                phase=phase,
                source_channel_id=getattr(interaction, "channel_id", None),
            )
        except database.FeedbackRateLimited as e:
            # 障害ではなく上限。スタックトレースは残さず理由をそのまま返す
            log.info("フィードバック上限: user=%s (%s)", interaction.user.id, e)
            await interaction.followup.send(f"⏳ {e}", ephemeral=True)
            return
        except Exception as e:
            log.exception("フィードバック保存失敗: %s", e)
            await interaction.followup.send(
                "❌ 報告を保存できませんでした。時間を置いてもう一度お試しください。",
                ephemeral=True,
            )
            return
        log.info(
            "フィードバック受付 ID:%s / category=%s / user=%s / room=%s / phase=%s",
            report_id,
            self.category,
            interaction.user.id,
            room_id or "none",
            phase or "none",
        )
        # #運営 への通知は付随処理。失敗しても報告そのものは受理済みなので、
        # 送信者へのお礼を止めない (DBには残っていて「報告の一覧」から読める)
        try:
            await self.cog.recruitment_manager.notify_feedback_report(
                interaction.guild,
                {
                    "report_id": report_id,
                    "user_id": interaction.user.id,
                    "category": self.category,
                    "summary": str(self.summary.value).strip(),
                    "details": str(self.details.value).strip() or None,
                    "bot_version": BOT_VERSION,
                    "room_name": room_name,
                    "phase": phase,
                },
            )
        except Exception as e:
            log.warning("報告の運営通知に失敗 (報告は保存済み): %s", e)
        await interaction.followup.send(
            f"✅ 報告を保存しました。ありがとうございます。（報告ID: `{report_id}`）",
            ephemeral=True,
        )


class PostgameVotePanelView(discord.ui.View):
    """終了後の投票を `#昼` の1枚のパネルで受ける。

    **DMは送らない。** 投票権を持つのは最大13人になるので、DMだと1試合で
    13通になる。パネルなら送信APIは1回で済み、押下は interaction 専用ルートを
    通るのでグローバルのレート制限枠も使わない。

    ボタン自体は全員に見えるが、押した人が持っている票だけを ephemeral で
    出し分ける。2票持つ人 (勝利陣営の霊媒師など) は続けてもう一度押す。
    """

    def __init__(
        self,
        *,
        game_id: int,
        guild_id: int,
        ballots: dict[int, list[str]],
        players: list,
        loser_ids: set[int],
        timeout: float,
        on_confirmed: Callable[[int, str], None],
    ) -> None:
        super().__init__(timeout=timeout)
        self.game_id = game_id
        self.guild_id = guild_id
        self.ballots = ballots
        self.players = list(players)
        self.loser_ids = set(loser_ids)
        self.on_confirmed = on_confirmed
        self.used: set[tuple[int, str]] = set()
        self.message: Optional[discord.Message] = None

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except (discord.NotFound, discord.HTTPException):
            pass

    @discord.ui.button(label="🗳️ 投票する", style=discord.ButtonStyle.primary)
    async def vote_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        voter_id = interaction.user.id
        remaining_kinds = [
            kind for kind in self.ballots.get(voter_id, [])
            if (voter_id, kind) not in self.used
        ]
        if not remaining_kinds:
            await interaction.response.send_message(
                "この試合であなたが使える票はありません（使用済みか、対象外です）。",
                ephemeral=True,
            )
            return

        kind = remaining_kinds[0]
        if kind == "postgame":
            candidates = [p for p in self.players if p.user_id in self.loser_ids]
            title = "手強かった相手へ（+1レート）"
            note = "**敗北陣営**から1人を選べます。"
        else:
            candidates = list(self.players)
            title = "終了後推薦（+1レート）"
            note = "**参加者**から1人を選べます。"
        if not candidates:
            await interaction.response.send_message(
                "選べる相手がいません。", ephemeral=True
            )
            return

        def mark_used(used_voter_id: int, used_kind: str) -> None:
            self.used.add((used_voter_id, used_kind))
            self.on_confirmed(used_voter_id, used_kind)

        view = PostgameRecommendationView(
            game_id=self.game_id,
            guild_id=self.guild_id,
            voter_id=voter_id,
            candidates=candidates,
            timeout=POSTGAME_RECOMMENDATION_TIMEOUT,
            on_confirmed=mark_used,
            kind=kind,
            title=title,
        )
        extra = (
            "\nこのあともう1票あります。確定したらもう一度パネルを押してください。"
            if len(remaining_kinds) > 1 else ""
        )
        await interaction.response.send_message(
            f"👏 **{title}**\n{note}自分自身は選べません。"
            f"投票者名は公開されません。{extra}",
            view=view,
            ephemeral=True,
        )


class FeedbackCategoryView(discord.ui.View):
    def __init__(self, cog: GameCog) -> None:
        super().__init__(timeout=180)
        self.cog = cog

    async def _open(self, interaction: discord.Interaction, category: str) -> None:
        await interaction.response.send_modal(FeedbackModal(self.cog, category))

    @discord.ui.button(label="不具合", style=discord.ButtonStyle.danger)
    async def bug_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._open(interaction, "不具合")

    @discord.ui.button(label="分かりにくい", style=discord.ButtonStyle.secondary)
    async def confusing_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._open(interaction, "分かりにくい")

    @discord.ui.button(label="改善要望", style=discord.ButtonStyle.primary)
    async def request_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._open(interaction, "改善要望")

    @discord.ui.button(label="その他", style=discord.ButtonStyle.secondary)
    async def other_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._open(interaction, "その他")


def _stats_variant(variant_id: str) -> VariantDefinition:
    """統計UIで未知の変種を既定値へ黙って混ぜず、明示的に検証する。"""
    return get_variant_definition(variant_id)


def _variant_scope_note(variant_id: str) -> str:
    variant = _stats_variant(variant_id)
    ladder = LADDER_DEFINITIONS[variant.ladder_id]
    shared_variants = [
        definition.label
        for definition in VARIANT_DEFINITIONS.values()
        if definition.ladder_id == variant.ladder_id
    ]
    if len(shared_variants) == 1:
        rating_scope = f"レート・順位も **{ladder.label}専用ラダー** です。"
    else:
        rating_scope = (
            f"レート・順位は **{ladder.label}ラダーで共通** "
            f"（{' / '.join(shared_variants)}）です。"
        )
    return (
        f"試合成績は **{variant.label}** だけを集計します。\n"
        + rating_scope
    )


def _variant_scope_footer(variant_id: str) -> str:
    """Markdownを描画しないDiscord footer用の同内容テキスト。"""
    return _variant_scope_note(variant_id).replace("**", "").replace("\n", " ")


class StatsVariantSelect(discord.ui.Select):
    """統計パネルを増やさず、公開中の変種を子Viewの先頭で切り替える。"""

    def __init__(self, parent_view) -> None:
        self.parent_view = parent_view
        super().__init__(
            placeholder="変種を選ぶ",
            options=[
                discord.SelectOption(
                    label=definition.label,
                    value=variant_id,
                    description=(
                        f"{definition.player_count}人 / "
                        f"{LADDER_DEFINITIONS[definition.ladder_id].label}ラダー"
                    ),
                    default=parent_view.variant_id == variant_id,
                )
                for variant_id, definition in VARIANT_DEFINITIONS.items()
                if variant_id in USER_VISIBLE_VARIANT_IDS
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.set_variant(self.values[0])
        await self.parent_view.refresh(interaction)


def _rank_role_display(variant_id: str, rank_name: str) -> str:
    variant = _stats_variant(variant_id)
    if variant.ladder_id == "l13":
        return rating_lib.get_rank_role_name(rank_name)
    if rank_name == "グランドマスター":
        return rating_lib.special_grandmaster_role_name(variant.ladder_id)
    return "付与なし（9人村はグランドマスターのみ付与）"


def _build_unplayed_variant_embed(
    user: discord.abc.User,
    variant_id: str,
    rating_info: Optional[dict],
) -> discord.Embed:
    variant = _stats_variant(variant_id)
    color = discord.Color(rating_info["color"]) if rating_info else discord.Color.blue()
    embed = discord.Embed(
        title=f"{user.display_name} の統計 — {variant.label}",
        description=(
            "**この変種はまだプレイしていません。**\n"
            + _variant_scope_note(variant_id)
        ),
        color=color,
    )
    if rating_info is not None:
        provisional = "（暫定）" if rating_info["provisional"] else ""
        embed.add_field(
            name=f"レート（{LADDER_DEFINITIONS[variant.ladder_id].label}）",
            value=(
                f"{rating_info['emoji']} **{rating_info['rating']}** "
                f"[{rating_info['rank_name']}{provisional}]\n"
                f"Discordロール: **{_rank_role_display(variant_id, rating_info['rank_name'])}**"
            ),
            inline=False,
        )
    return embed


class PlayerStatsVariantView(discord.ui.View):
    """自分/選択ユーザーの統計を変種別に表示する。"""

    def __init__(
        self,
        cog: GameCog,
        guild_id: int,
        user: discord.abc.User,
        *,
        variant_id: str = DEFAULT_VARIANT_ID,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.user = user
        self.variant_id = _stats_variant(variant_id).variant_id
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        self.add_item(StatsVariantSelect(self))

    def set_variant(self, variant_id: str) -> None:
        self.variant_id = _stats_variant(variant_id).variant_id
        self._rebuild()

    async def _sync_rank_role(
        self,
        guild: discord.Guild,
        rating_info: Optional[dict],
    ) -> None:
        sync = getattr(self.cog, "_sync_rank_role", None)
        if rating_info is None or not callable(sync):
            return
        member = guild.get_member(self.user.id)
        if member is None and isinstance(self.user, discord.Member):
            member = self.user
        if member is None:
            return
        variant = _stats_variant(self.variant_id)
        try:
            await sync(
                member,
                rating_info["rank_name"],
                ladder_id=variant.ladder_id,
            )
        except Exception as error:
            log.warning(
                "統計表示時のロール同期失敗 (%s/%s): %s",
                member.display_name,
                variant.ladder_id,
                error,
            )

    async def load_embed(self, guild: discord.Guild) -> discord.Embed:
        variant = _stats_variant(self.variant_id)
        stats = await database.get_player_stats(
            self.user.id,
            self.guild_id,
            variant_id=self.variant_id,
        )
        rating_info = await database.get_player_current_rank_info(
            self.user.id,
            self.guild_id,
            ladder_id=variant.ladder_id,
        )
        last_season = await database.get_player_latest_season_result(
            self.user.id,
            self.guild_id,
            ladder_id=variant.ladder_id,
        )
        await self._sync_rank_role(guild, rating_info)
        if stats is None:
            return _build_unplayed_variant_embed(
                self.user, self.variant_id, rating_info,
            )
        return build_stats_embed(
            self.user,
            stats,
            rating_info,
            last_season,
            variant_id=self.variant_id,
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ サーバー内でのみ使用できます。", ephemeral=True,
            )
        await interaction.response.defer()
        embed = await self.load_embed(interaction.guild)
        await interaction.edit_original_response(embed=embed, view=self)


def _history_delta_text(row: dict) -> str:
    if row["rating_before"] is None or row["rating_after"] is None:
        return ""
    delta = row["rating_after"] - row["rating_before"]
    sign = "+" if delta >= 0 else ""
    elo_delta = row["elo_delta"] or 0
    elo_sign = "+" if elo_delta >= 0 else ""
    parts = [f"本体{elo_sign}{elo_delta}"]
    for key, label in (
        ("bonus", "勝利"),
        ("play_bonus", "活躍"),
        ("recommendation_bonus", "投票"),
    ):
        value = row[key] or 0
        if value:
            parts.append(f"{label}+{value}")
    return (
        f" / {row['rating_before']}→{row['rating_after']} ({sign}{delta}; "
        + " / ".join(parts) + ")"
    )


class RecentGamesVariantView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        *,
        variant_id: str = DEFAULT_VARIANT_ID,
    ) -> None:
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.variant_id = _stats_variant(variant_id).variant_id
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        self.add_item(StatsVariantSelect(self))

    def set_variant(self, variant_id: str) -> None:
        self.variant_id = _stats_variant(variant_id).variant_id
        self._rebuild()

    async def load_embed(self, guild: discord.Guild) -> discord.Embed:
        del guild
        variant = _stats_variant(self.variant_id)
        rows = await database.get_recent_games(
            self.guild_id, limit=10, variant_id=self.variant_id,
        )
        description = "\n".join(
            f"`{row['seq']:>4}` {row['room_name']} / {row['winner_team']} / "
            f"{format_played_at(row['played_at'])}"
            for row in rows
        )
        if not description:
            description = "この変種の試合履歴はまだありません。"
        embed = discord.Embed(
            title=f"最近の試合 — {variant.label}",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=_variant_scope_footer(self.variant_id))
        return embed

    async def refresh(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ サーバー内でのみ使用できます。", ephemeral=True,
            )
        await interaction.response.defer()
        embed = await self.load_embed(interaction.guild)
        await interaction.edit_original_response(embed=embed, view=self)


class PlayerHistoryVariantView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        player_id: int,
        *,
        variant_id: str = DEFAULT_VARIANT_ID,
    ) -> None:
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.player_id = player_id
        self.variant_id = _stats_variant(variant_id).variant_id
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        self.add_item(StatsVariantSelect(self))

    def set_variant(self, variant_id: str) -> None:
        self.variant_id = _stats_variant(variant_id).variant_id
        self._rebuild()

    async def load_embed(self, guild: discord.Guild) -> discord.Embed:
        del guild
        variant = _stats_variant(self.variant_id)
        rows = await database.get_player_recent_games(
            self.player_id,
            self.guild_id,
            limit=10,
            variant_id=self.variant_id,
        )
        lines = []
        for row in rows:
            result = "勝利" if row["won"] else "敗北"
            lines.append(
                f"`{row['seq']:>4}` {row['room_name']} / {row['role']} / "
                f"{result}{_history_delta_text(row)}"
            )
        embed = discord.Embed(
            title=f"自分の最近の試合 — {variant.label}",
            description=(
                "\n".join(lines)
                if lines else "この変種はまだプレイしていません。"
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text=_variant_scope_footer(self.variant_id))
        return embed

    async def refresh(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ サーバー内でのみ使用できます。", ephemeral=True,
            )
        await interaction.response.defer()
        embed = await self.load_embed(interaction.guild)
        await interaction.edit_original_response(embed=embed, view=self)


class OverallRoomStatsSelect(discord.ui.Select):
    def __init__(self, owner: "OverallStatsFilterView") -> None:
        self.owner = owner
        options = [
            discord.SelectOption(
                label="全卓",
                value="all",
                default=owner.room_id is None,
            )
        ]
        options.extend(
            discord.SelectOption(
                label=room.name,
                value=room.room_id,
                default=owner.room_id == room.room_id,
            )
            for room in ACTIVE_ROOM_DEFINITIONS
            if room.variant_id == owner.variant_id
        )
        super().__init__(
            placeholder="試合指標を表示する卓",
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        self.owner.room_id = None if value == "all" else value
        for option in self.options:
            option.default = option.value == value
        await self.owner.refresh(interaction)


class OverallRankStatsSelect(discord.ui.Select):
    def __init__(self, owner: "OverallStatsFilterView") -> None:
        self.owner = owner
        options = [
            discord.SelectOption(label="確定ランク全体", value="all", default=True),
        ]
        options.extend(
            discord.SelectOption(label=name, value=name)
            for name, _emoji, _color in RANK_SPECS
        )
        super().__init__(
            placeholder="プレイヤー指標を表示する試合時ランク",
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        self.owner.rank_name = None if value == "all" else value
        for option in self.options:
            option.default = option.value == value
        await self.owner.refresh(interaction)


class OverallStatsFilterView(discord.ui.View):
    """変種を先に選び、卓/試合時表示ランクの2軸で絞る。"""

    def __init__(
        self,
        guild_id: int,
        *,
        variant_id: str = DEFAULT_VARIANT_ID,
    ) -> None:
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.variant_id = _stats_variant(variant_id).variant_id
        self.room_id: Optional[str] = None
        self.rank_name: Optional[str] = None
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        self.add_item(StatsVariantSelect(self))
        self.add_item(OverallRoomStatsSelect(self))
        self.add_item(OverallRankStatsSelect(self))

    def set_variant(self, variant_id: str) -> None:
        self.variant_id = _stats_variant(variant_id).variant_id
        valid_rooms = {
            room.room_id
            for room in ACTIVE_ROOM_DEFINITIONS
            if room.variant_id == self.variant_id
        }
        if self.room_id not in valid_rooms:
            self.room_id = None
        self._rebuild()

    async def load_embed(self, guild: discord.Guild) -> discord.Embed:
        game_stats = await database.get_overall_game_stats(
            self.guild_id,
            room_id=self.room_id,
            variant_id=self.variant_id,
        )
        rank_stats = await database.get_rank_player_stats(
            self.guild_id,
            rank_name=self.rank_name,
            variant_id=self.variant_id,
        )
        room_label = "全卓" if self.room_id is None else next(
            (room.name for room in ACTIVE_ROOM_DEFINITIONS if room.room_id == self.room_id),
            self.room_id,
        )
        rank_label = self.rank_name or "確定ランク全体"
        return build_overall_stats_embed(
            game_stats, rank_stats,
            room_label=room_label,
            rank_label=rank_label,
            guild=guild,
            variant_id=self.variant_id,
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ サーバー内でのみ使用できます。", ephemeral=True,
            )
        await interaction.response.defer()
        embed = await self.load_embed(interaction.guild)
        await interaction.edit_original_response(embed=embed, view=self)


_RATING_METRIC = "rating"


async def _build_rating_leaderboard_embed(
    guild: discord.Guild,
    variant_id: str = DEFAULT_VARIANT_ID,
) -> discord.Embed:
    """選択変種に対応するラダーの「今シーズンランキング」。"""
    variant = _stats_variant(variant_id)
    top = await database.get_current_season_leaderboard(
        guild.id, limit=20, ladder_id=variant.ladder_id,
    )
    if not top:
        return discord.Embed(
            title=f"今シーズンランキング — {variant.label}",
            description=(
                "レーティングデータがありません。\n"
                + _variant_scope_note(variant_id)
            ),
            color=discord.Color.gold(),
        )
    lines = []
    for i, d in enumerate(top, 1):
        member = guild.get_member(d["player_id"])
        name = member.display_name if member else f"ID:{d['player_id']}"
        provisional_txt = " 暫定" if d["provisional"] else ""
        if d["top_percent"] is None:
            rank_meta = " / 計測中"
        else:
            rank_meta = f" / {d['position']}位 / 上位{d['top_percent']:.1f}%"
        lines.append(
            f"`{i:>2}.` {d['emoji']} **{d['rating']}** [{d['rank_name']}{provisional_txt}] "
            f"{name} — 今季{d['season_winrate']}% ({d['season_wins']}/{d['season_games']}){rank_meta}"
        )
    embed = discord.Embed(
        title=f"今シーズンランキング — {variant.label}",
        description=_variant_scope_note(variant_id) + "\n\n" + "\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(
        text=f"相対ランクは通算{SEASON_RANK_MIN_GAMES}戦以上のプレイヤーのみ対象"
    )
    return embed


def _format_metric_value(value: float, unit: str) -> str:
    if unit == "per_game":
        return f"{value:.2f}票/試合"
    return f"{value * 100:.1f}%"


def _format_metric_detail(entry: dict, unit: str) -> str:
    if unit == "per_game":
        return f"{entry['numerator']}票 / {entry['denominator']}戦"
    return f"{entry['numerator']}/{entry['denominator']}"


def build_metric_leaderboard_embed(board: dict, guild: discord.Guild) -> discord.Embed:
    """項目別ランキング + 閲覧者自身の位置。"""
    variant_id = str(board.get("variant_id", DEFAULT_VARIANT_ID))
    variant = _stats_variant(variant_id)
    unit = board["unit"]
    title = f"{board['label']} — {variant.label}"
    if board.get("role"):
        title = f"{title}（{board['role']}）"

    if board["top"]:
        lines = []
        for entry in board["top"]:
            member = guild.get_member(entry["player_id"])
            name = member.display_name if member else f"ID:{entry['player_id']}"
            lines.append(
                f"`{entry['position']:>2}.` **{_format_metric_value(entry['value'], unit)}** "
                f"{name} — {_format_metric_detail(entry, unit)}"
            )
        description = "\n".join(lines)
    else:
        description = (
            f"まだ{board['min_samples']}回以上の記録を持つプレイヤーがいません。"
        )

    embed = discord.Embed(
        title=title,
        description=_variant_scope_note(variant_id) + "\n\n" + description,
        color=discord.Color.gold(),
    )
    embed.add_field(name="この項目について", value=board["note"], inline=False)

    viewer = board.get("viewer")
    if viewer is None:
        own = "この項目の記録がまだありません。"
    elif board.get("viewer_position") is not None:
        own = (
            f"**{_format_metric_value(viewer['value'], unit)}** "
            f"（{_format_metric_detail(viewer, unit)}） — "
            f"**{board['viewer_position']}位 / {board['ranked_count']}人中**"
        )
    else:
        need = board["min_samples"] - viewer["samples"]
        own = (
            f"**{_format_metric_value(viewer['value'], unit)}** "
            f"（{_format_metric_detail(viewer, unit)}）\n"
            f"ランキング掲載まであと **{need}回**（{board['min_samples']}回以上で掲載）"
        )
    embed.add_field(name="あなた", value=own, inline=False)
    embed.set_footer(
        text=f"掲載は{board['min_samples']}回以上。同率は母数の多い順"
    )
    return embed


_SEASON_MODE_PREVIOUS = "previous"
_SEASON_MODE_GRANDMASTERS = "grandmasters"


async def _build_previous_season_embed(
    guild: discord.Guild,
    variant_id: str = DEFAULT_VARIANT_ID,
) -> discord.Embed:
    variant = _stats_variant(variant_id)
    reset_id, rows = await database.get_latest_season_results(
        guild.id, limit=20, ladder_id=variant.ladder_id,
    )
    if reset_id == 0 or not rows:
        return discord.Embed(
            title=f"前シーズン最終順位 — {variant.label}",
            description=(
                "前シーズンの結果はまだありません。\n"
                + _variant_scope_note(variant_id)
            ),
            color=discord.Color.purple(),
        )
    lines = []
    for i, row in enumerate(rows, 1):
        member = guild.get_member(row["player_id"])
        name = member.display_name if member else f"ID:{row['player_id']}"
        top_pct = (
            f" / 上位{row['top_percent']:.1f}%"
            if row["top_percent"] is not None else ""
        )
        lines.append(
            f"`{i:>2}.` {row['emoji']} **{row['final_rating']}** [{row['rank_name']}] "
            f"{name} — {row['season_winrate']}% ({row['season_wins']}/{row['season_games']}){top_pct}"
        )
    embed = discord.Embed(
        title=f"前シーズン最終順位 — {variant.label}",
        description=_variant_scope_note(variant_id) + "\n\n" + "\n".join(lines),
        color=discord.Color.purple(),
    )
    embed.set_footer(text=f"シーズンリセットID: {reset_id}")
    return embed


async def _build_grandmaster_history_embed(
    guild: discord.Guild,
    variant_id: str = DEFAULT_VARIANT_ID,
) -> discord.Embed:
    variant = _stats_variant(variant_id)
    seasons = await database.get_grandmaster_history(
        guild.id, ladder_id=variant.ladder_id,
    )
    grandmaster_emoji = rating_lib.get_rank_emoji_by_name("グランドマスター")
    grandmaster_name = LADDER_DEFINITIONS[variant.ladder_id].grandmaster_role_name
    if not seasons:
        return discord.Embed(
            title=f"{grandmaster_emoji} 歴代{grandmaster_name} — {variant.label}",
            description=(
                "まだシーズンが終了していません。\n"
                f"シーズンリセットの時点で{grandmaster_name}だった人がここに残ります。\n"
                + _variant_scope_note(variant_id)
            ),
            color=discord.Color.red(),
        )
    blocks = []
    for season in seasons:
        header = f"**シーズン{season['season_number']}**"
        reset_at = str(season.get("reset_at") or "")[:10]
        if reset_at:
            header += f"（{reset_at} 終了）"
        lines = [header]
        for member_row in season["members"]:
            member = guild.get_member(member_row["player_id"])
            name = member.display_name if member else f"ID:{member_row['player_id']}"
            position = (
                f"`{member_row['position']:>2}.`"
                if member_row["position"] is not None else "`  -`"
            )
            lines.append(
                f"{position} {name} — **{member_row['rating']}** "
                f"({member_row['season_wins']}/{member_row['season_games']})"
            )
        blocks.append("\n".join(lines))
    embed = discord.Embed(
        title=f"{grandmaster_emoji} 歴代{grandmaster_name} — {variant.label}",
        description=_variant_scope_note(variant_id) + "\n\n" + "\n\n".join(blocks),
        color=discord.Color.red(),
    )
    embed.set_footer(
        text=(
            f"各シーズンのリセット時点で {grandmaster_name} だった人。"
            "レートはリセット前の値"
        )
    )
    return embed


class SeasonHistorySelect(discord.ui.Select):
    def __init__(self, parent: "SeasonHistoryView") -> None:
        self.parent_view = parent
        super().__init__(
            placeholder="見たいものを選ぶ",
            options=[
                discord.SelectOption(
                    label="前シーズン最終順位",
                    value=_SEASON_MODE_PREVIOUS,
                    description="直前のシーズンの上位20人",
                    default=parent.mode == _SEASON_MODE_PREVIOUS,
                ),
                discord.SelectOption(
                    label="歴代グランドマスター",
                    value=_SEASON_MODE_GRANDMASTERS,
                    description="シーズンごとの到達者",
                    default=parent.mode == _SEASON_MODE_GRANDMASTERS,
                ),
            ],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.mode = self.values[0]
        await self.parent_view.refresh(interaction)


class SeasonHistoryView(discord.ui.View):
    """「前シーズン」の中身。変種を選び、対応ラダーの履歴を表示する。"""

    def __init__(self, *, variant_id: str = DEFAULT_VARIANT_ID) -> None:
        super().__init__(timeout=300)
        self.variant_id = _stats_variant(variant_id).variant_id
        self.mode: str = _SEASON_MODE_PREVIOUS
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        self.add_item(StatsVariantSelect(self))
        self.add_item(SeasonHistorySelect(self))

    def set_variant(self, variant_id: str) -> None:
        self.variant_id = _stats_variant(variant_id).variant_id
        self._rebuild()

    async def load_embed(self, guild: discord.Guild) -> discord.Embed:
        if self.mode == _SEASON_MODE_GRANDMASTERS:
            return await _build_grandmaster_history_embed(guild, self.variant_id)
        return await _build_previous_season_embed(guild, self.variant_id)

    async def refresh(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ サーバー内でのみ使用できます。", ephemeral=True,
            )
        await interaction.response.defer()
        self._rebuild()
        embed = await self.load_embed(interaction.guild)
        await interaction.edit_original_response(embed=embed, view=self)


class LeaderboardMetricSelect(discord.ui.Select):
    """見たい指標を選ぶ。ボタンを増やさずここで切り替える。"""

    def __init__(self, parent: "LeaderboardView") -> None:
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label="レート順位",
                value=_RATING_METRIC,
                description="今シーズンの相対ランク順",
                default=parent.metric == _RATING_METRIC,
            )
        ] + [
            discord.SelectOption(
                label=spec["label"][:100],
                value=metric,
                description=spec["note"][:100],
                default=parent.metric == metric,
            )
            for metric, spec in database.LEADERBOARD_METRICS.items()
        ]
        super().__init__(placeholder="見たい項目を選ぶ", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.metric = self.values[0]
        await self.parent_view.refresh(interaction)


class LeaderboardRoleSelect(discord.ui.Select):
    """役職別勝率のときだけ出す絞り込み。"""

    def __init__(self, parent: "LeaderboardView") -> None:
        self.parent_view = parent
        super().__init__(
            placeholder="役職を選ぶ",
            options=[
                discord.SelectOption(
                    label=role.value,
                    value=role.value,
                    default=parent.role == role.value,
                )
                for role in _stats_variant(parent.variant_id).role_distribution
            ],
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.role = self.values[0]
        await self.parent_view.refresh(interaction)


class LeaderboardView(discord.ui.View):
    """「全体ランキング」の中身。項目をセレクトで切り替える。"""

    def __init__(
        self,
        guild_id: int,
        viewer_id: int,
        *,
        variant_id: str = DEFAULT_VARIANT_ID,
    ) -> None:
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.viewer_id = viewer_id
        self.variant_id = _stats_variant(variant_id).variant_id
        self.metric: str = _RATING_METRIC
        self.role: str = Role.WEREWOLF.value
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        self.add_item(StatsVariantSelect(self))
        self.add_item(LeaderboardMetricSelect(self))
        needs_role = database.LEADERBOARD_METRICS.get(self.metric, {}).get("needs_role")
        if needs_role:
            self.add_item(LeaderboardRoleSelect(self))

    def set_variant(self, variant_id: str) -> None:
        variant = _stats_variant(variant_id)
        self.variant_id = variant.variant_id
        valid_roles = {role.value for role in variant.role_distribution}
        if self.role not in valid_roles:
            self.role = next(iter(variant.role_distribution)).value
        self._rebuild()

    async def load_embed(self, guild: discord.Guild) -> discord.Embed:
        if self.metric == _RATING_METRIC:
            return await _build_rating_leaderboard_embed(guild, self.variant_id)
        needs_role = bool(
            database.LEADERBOARD_METRICS.get(self.metric, {}).get("needs_role")
        )
        board = await database.get_metric_leaderboard(
            self.guild_id,
            self.metric,
            role=self.role if needs_role else None,
            viewer_id=self.viewer_id,
            variant_id=self.variant_id,
        )
        return build_metric_leaderboard_embed(
            {**board, "variant_id": self.variant_id}, guild,
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ サーバー内でのみ使用できます。", ephemeral=True,
            )
        await interaction.response.defer()
        self._rebuild()
        embed = await self.load_embed(interaction.guild)
        await interaction.edit_original_response(embed=embed, view=self)


class StatsView(discord.ui.View):
    def __init__(self, cog: GameCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @staticmethod
    async def _require_guild(interaction: discord.Interaction) -> bool:
        """guild コンテキストのガード。False を返したら呼び出し側は return する"""
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ サーバー内でのみ使用できます。", ephemeral=True
            )
            return False
        return True

    @classmethod
    async def _defer_ephemeral_query(
        cls, interaction: discord.Interaction
    ) -> bool:
        """DB照会前にinteractionを受理し、以後をephemeral followupへ統一する。"""
        if not await cls._require_guild(interaction):
            return False
        await interaction.response.defer(ephemeral=True, thinking=True)
        return True

    @discord.ui.button(label="自分の統計", style=discord.ButtonStyle.secondary, custom_id="stats_self", row=0)
    async def self_stats(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._defer_ephemeral_query(interaction):
            return
        view = PlayerStatsVariantView(
            self.cog, interaction.guild.id, interaction.user,
        )
        embed = await view.load_embed(interaction.guild)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="全体ランキング", style=discord.ButtonStyle.secondary, custom_id="stats_all", row=0)
    async def all_stats(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._defer_ephemeral_query(interaction):
            return
        view = LeaderboardView(interaction.guild.id, interaction.user.id)
        embed = await view.load_embed(interaction.guild)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="全体データ", style=discord.ButtonStyle.secondary, custom_id="stats_overall_data", row=0)
    async def overall_data(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        if not await self._defer_ephemeral_query(interaction):
            return
        view = OverallStatsFilterView(interaction.guild.id)
        embed = await view.load_embed(interaction.guild)
        await interaction.followup.send(
            embed=embed, view=view, ephemeral=True,
        )

    @discord.ui.button(label="不具合・改善を報告", style=discord.ButtonStyle.primary, custom_id="feedback_report", row=0)
    async def feedback_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not await self._require_guild(interaction):
            return
        await interaction.response.send_message(
            "報告の種類を選んでください。内容は管理用データベースへ保存されます。",
            view=FeedbackCategoryView(self.cog),
            ephemeral=True,
        )

    @discord.ui.button(label="前シーズン", style=discord.ButtonStyle.secondary, custom_id="stats_previous", row=1)
    async def previous_season(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._defer_ephemeral_query(interaction):
            return
        view = SeasonHistoryView()
        embed = await view.load_embed(interaction.guild)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="最近の試合", style=discord.ButtonStyle.secondary, custom_id="stats_recent_games", row=1)
    async def recent_games(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._defer_ephemeral_query(interaction):
            return
        view = RecentGamesVariantView(interaction.guild.id)
        embed = await view.load_embed(interaction.guild)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="自分の履歴", style=discord.ButtonStyle.secondary, custom_id="stats_my_history", row=1)
    async def my_history(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._defer_ephemeral_query(interaction):
            return
        view = PlayerHistoryVariantView(
            interaction.guild.id, interaction.user.id,
        )
        embed = await view.load_embed(interaction.guild)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="ランク仕様", style=discord.ButtonStyle.secondary, custom_id="stats_rank_spec", row=1)
    async def rank_spec_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embeds=build_rank_spec_embeds(), ephemeral=True
        )

    @discord.ui.button(label="同村拒否", style=discord.ButtonStyle.secondary, custom_id="stats_player_blocks", row=1)
    async def player_blocks_button(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        if not await self._require_guild(interaction):
            return
        manager = getattr(self.cog, "recruitment_manager", None)
        if manager is None:
            return await interaction.response.send_message(
                "募集システムを利用できません。", ephemeral=True,
            )
        from database import list_player_blocks
        from recruitment import PlayerBlockSettingsView

        # DB問い合わせより先にACKする。インタラクションの応答期限は3秒で、
        # 書き込み側のBEGIN IMMEDIATEと競合して待たされると
        # Unknown interaction (10062) になり、押した人には何も返せなくなる
        await interaction.response.defer(ephemeral=True, thinking=True)
        if getattr(interaction.guild, "chunked", True) is False:
            try:
                # UserSelectの名前検索だけでなく候補ページにも全在籍者を載せる。
                # 通常は起動時に完了済みで、未完の時だけGatewayへ1回要求する。
                await interaction.guild.chunk(cache=True)
            except Exception as exc:
                # 名前検索は引き続き使えるため、UI自体は止めない。
                log.warning("同村拒否の全メンバー候補取得に失敗: %s", exc)
        blocked_ids = await list_player_blocks(interaction.guild.id, interaction.user.id)
        settings_view = PlayerBlockSettingsView(
            manager, interaction.guild.id, interaction.user.id, blocked_ids,
        )
        await interaction.followup.send(
            settings_view.summary_content
            + "\nこの設定と解除は本人にだけ表示されます。",
            view=settings_view,
            ephemeral=True,
        )

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="プレイヤーを選択して統計を表示",
        custom_id="stats_user_select",
        row=2,
    )
    async def user_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        if not await self._defer_ephemeral_query(interaction):
            return
        target = select.values[0]
        view = PlayerStatsVariantView(
            self.cog, interaction.guild.id, target,
        )
        embed = await view.load_embed(interaction.guild)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ============================================================
# ヘルパー関数
# ============================================================

def _parse_db_timestamp(text: Optional[str]) -> Optional[datetime]:
    """SQLiteのCURRENT_TIMESTAMP (UTC, 'YYYY-MM-DD HH:MM:SS') をaware datetimeへ"""
    if not text:
        return None
    try:
        return datetime.strptime(str(text), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_played_at(text: Optional[str]) -> str:
    """DBのUTC時刻をDiscordタイムスタンプにする (閲覧者のローカル時刻で表示される)"""
    dt = _parse_db_timestamp(text)
    if dt is None:
        return str(text or "N/A")
    return f"<t:{int(dt.timestamp())}:f>"


def format_played_at_jst(text: Optional[str]) -> str:
    """embedフッター用 (フッターはタイムスタンプ書式が効かないためJST文字列)"""
    dt = _parse_db_timestamp(text)
    if dt is None:
        return str(text or "N/A")
    jst = dt.astimezone(timezone(timedelta(hours=9)))
    return jst.strftime("%Y-%m-%d %H:%M JST")


def _format_rate(
    numerator: int,
    denominator: int,
    *,
    samples: Optional[int] = None,
) -> str:
    sample_count = denominator if samples is None else samples
    if sample_count < STATS_MIN_SAMPLES or denominator <= 0:
        return f"試合数不足（{sample_count}/{STATS_MIN_SAMPLES}）"
    return f"{numerator / denominator * 100:.1f}%（{numerator}/{denominator}）"


def _format_average(value: Optional[float], samples: int, *, suffix: str = "日") -> str:
    if samples < STATS_MIN_SAMPLES or value is None:
        return f"試合数不足（{samples}/{STATS_MIN_SAMPLES}）"
    return f"{value:.2f}{suffix}（n={samples}）"


def _wilson_interval(successes: int, samples: int) -> tuple[float, float]:
    """二項比率の95% Wilson信頼区間。少数試合の運営判断を過敏にしない。"""
    if samples <= 0:
        return 0.0, 1.0
    z = 1.959963984540054
    proportion = successes / samples
    denominator = 1 + z * z / samples
    center = (proportion + z * z / (2 * samples)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / samples
            + z * z / (4 * samples * samples)
        )
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def build_variant_balance_embed(rows: list[dict]) -> discord.Embed:
    """9人公開変種の試合数・狼勝率・固定プール均衡を運営向けに表示する。"""
    embed = discord.Embed(
        title="9人村 変種別均衡モニター",
        description=(
            "既存の正常終了した試合履歴を変種別に集計します。\n"
            "50戦以上の見直し要否は、狼勝率の95% Wilson信頼区間に"
            "設定上の均衡勝率が含まれるかで判定します。"
        ),
        color=discord.Color.orange(),
    )
    by_variant = {str(row["variant_id"]): row for row in rows}
    for variant_id in ("v9_cross", "v9_turn"):
        variant = _stats_variant(variant_id)
        row = by_variant.get(variant_id, {})
        games = int(row.get("games", 0))
        wolf_wins = int(row.get("wolf_wins", 0))
        target = variant.village_win_pool / (
            variant.wolf_win_pool + variant.village_win_pool
        )
        pool_ratio = variant.wolf_win_pool / variant.village_win_pool
        if games:
            wolf_rate = wolf_wins / games
            difference = wolf_rate - target
            rate_text = f"{wolf_rate * 100:.1f}%（{wolf_wins}/{games}）"
            difference_text = f"{difference * 100:+.1f}ポイント"
        else:
            wolf_rate = 0.0
            rate_text = "記録なし"
            difference_text = "算出不可"

        if games < 30:
            decision = "🟡 観測中（30戦未満のため結論を出しません）"
        elif games < 50:
            decision = "🟠 再評価候補（50戦到達後に見直し要否を判定）"
        else:
            low, high = _wilson_interval(wolf_wins, games)
            if low <= target <= high:
                decision = (
                    "🟢 現時点で見直し不要 "
                    f"（95%区間 {low * 100:.1f}〜{high * 100:.1f}%）"
                )
            else:
                decision = (
                    "🔴 見直し要検討 "
                    f"（95%区間 {low * 100:.1f}〜{high * 100:.1f}%）"
                )
        embed.add_field(
            name=variant.label,
            value=(
                f"試合数: **{games}戦**\n"
                f"狼勝率: **{rate_text}**\n"
                f"均衡勝率との差: **{difference_text}**（基準 {target * 100:.1f}%）\n"
                f"レートプール: 狼{variant.wolf_win_pool} / 村{variant.village_win_pool} "
                f"（W/V = {pool_ratio:.3f}）\n"
                f"判定: {decision}"
            ),
            inline=False,
        )
    embed.set_footer(text="この表示は参照専用です。レート設定や試合履歴は変更しません。")
    return embed


def build_overall_stats_embed(
    game_stats: dict,
    rank_stats: dict,
    *,
    room_label: str,
    rank_label: str,
    guild: discord.Guild,
    variant_id: str = DEFAULT_VARIANT_ID,
) -> discord.Embed:
    """卓単位の試合指標と、試合時ランク単位の個人指標を分けて表示する。"""
    variant = _stats_variant(variant_id)
    games = int(game_stats["games"])
    detailed_games = int(game_stats["detailed_games"])
    village_wins = int(game_stats["wins"].get(Team.VILLAGE.value, 0))
    wolf_wins = int(game_stats["wins"].get(Team.WOLF.value, 0))
    embed = discord.Embed(
        title=f"全体データ — {variant.label} / {room_label}",
        description=(
            ("**この変種の試合はまだありません。**\n" if games == 0 else "")
            + _variant_scope_note(variant_id)
            + "\n\n試合指標は卓メニューで**卓別**、プレイヤー指標はランクメニューで"
            "**その試合時点の表示ランク**別に切り替えます。\n"
            "表示ランクはシーズン内の相対評価（上位%）です。現在のランクロールとは"
            "時期によって異なる場合があります。暫定ランクはランク別集計から除外します。"
        ),
        color=discord.Color.teal(),
    )
    embed.add_field(
        name=f"試合結果（{room_label}）",
        value=(
            f"記録試合: {games}戦\n"
            f"村勝率: {_format_rate(village_wins, games)}\n"
            f"狼勝率: {_format_rate(wolf_wins, games)}\n"
            f"平均日数: {_format_average(game_stats['days']['average'], game_stats['days']['count'])}"
        ),
        inline=False,
    )
    embed.add_field(
        name="進行指標（詳細記録のある試合）",
        value=(
            f"平和発生確率: {_format_rate(game_stats['peaceful']['numerator'], game_stats['peaceful']['denominator'], samples=game_stats['peaceful']['sample_games'])}\n"
            f"初日処刑が人狼: {_format_rate(game_stats['day1_execution']['numerator'], game_stats['day1_execution']['denominator'])}\n"
            f"処刑成功率: {_format_rate(game_stats['executions']['numerator'], game_stats['executions']['denominator'], samples=game_stats['executions']['sample_games'])}\n"
            f"初夜噛みが役職持ち: {_format_rate(game_stats['night1_role_kill']['numerator'], game_stats['night1_role_kill']['denominator'])}"
        ),
        inline=False,
    )
    wolf_lines = []
    for row in game_stats["wolf_alive_by_day"]:
        wolf_lines.append(
            f"{row['day']}日目: {_format_average(row['average'], row['count'], suffix='人')}"
        )
    embed.add_field(
        name="朝時点の平均生存狼数",
        value="\n".join(wolf_lines) if wolf_lines else "詳細記録なし",
        inline=False,
    )

    time_lines = [f"{name}: {count}戦" for name, count in game_stats["time_counts"].items()]
    gm_lines = []
    for gm_id, count in game_stats["gm_counts"][:10]:
        member = guild.get_member(gm_id)
        name = member.display_name if member is not None else f"ID:{gm_id}"
        gm_lines.append(f"{name}: {count}戦")
    embed.add_field(
        name="時間帯別（JST）",
        value="\n".join(time_lines) if time_lines else "記録なし",
        inline=True,
    )
    embed.add_field(
        name="GM別",
        value="\n".join(gm_lines) if gm_lines else "GM記録なし",
        inline=True,
    )

    seer = rank_stats["seer"]
    guard = rank_stats["guard"]
    wolf = rank_stats["wolf"]
    embed.add_field(
        name=f"プレイヤー指標（{rank_label}）",
        value=(
            f"占い的中率: {_format_rate(seer['wolf_hits'], seer['checks'])}\n"
            f"狩人の護衛成功率: {_format_rate(guard['successes'], guard['checks'])}"
            f"（成功{guard['successes']}回）\n"
            f"占い師の平均生存日数: {_format_average(seer['survival_average'], seer['survival_count'])}\n"
            f"人狼の平均生存日数: {_format_average(wolf['survival_average'], wolf['survival_count'])}"
        ),
        inline=False,
    )
    role_lines = []
    for role in Role:
        row = rank_stats["roles"].get(role.value, {"count": 0, "wins": 0})
        role_lines.append(
            f"{role.value}: {_format_rate(row['wins'], row['count'])}"
        )
    embed.add_field(
        name=f"役職別勝率（{rank_label}）",
        value="\n".join(role_lines),
        inline=False,
    )
    embed.set_footer(
        text=(
            f"率・平均は最低{STATS_MIN_SAMPLES}試合（または対象プレイヤー）から表示 / "
            f"詳細記録 {detailed_games}戦 / 暫定除外 {rank_stats['provisional_excluded']}件"
        )
    )
    return embed


def build_stats_embed(
    user: discord.User,
    stats: dict,
    rating_info: Optional[dict] = None,
    last_season: Optional[dict] = None,
    *,
    variant_id: str = DEFAULT_VARIANT_ID,
) -> discord.Embed:
    variant = _stats_variant(variant_id)
    ladder = LADDER_DEFINITIONS[variant.ladder_id]
    # ランクに合わせた色
    if rating_info:
        embed_color = discord.Color(rating_info["color"])
    else:
        embed_color = discord.Color.blue()

    embed = discord.Embed(
        title=f"{user.display_name} の統計 — {variant.label}",
        description=_variant_scope_note(variant_id),
        color=embed_color,
    )

    # レート/ランク (最上部)
    if rating_info:
        provisional_txt = " (暫定)" if rating_info["provisional"] else ""
        role_name = _rank_role_display(variant_id, rating_info["rank_name"])
        if rating_info["top_percent"] is None:
            top_txt = (
                f"計測中 / 今季 {rating_info['season_games']}戦\n"
                f"通算{SEASON_RANK_MIN_GAMES}戦到達で相対ランクと順位が確定します"
            )
        else:
            top_txt = (
                f"{rating_info['position']}位 / {rating_info['active_count']}人中 / "
                f"上位 {rating_info['top_percent']:.1f}% / 今季 {rating_info['season_games']}戦"
            )
            if rating_info["rank_name"] in ("マスター", "グランドマスター"):
                top_txt += "\nマスター帯の順位表示対象です"
        embed.add_field(
            name=f"現在シーズン（{ladder.label}ラダー）",
            value=(
                f"{rating_info['emoji']} **{rating_info['rating']}** [{rating_info['rank_name']}{provisional_txt}]\n"
                f"Discordロール: **{role_name}**\n"
                f"{top_txt}\n"
                f"今季勝率: {rating_info['season_winrate']}% ({rating_info['season_wins']}/{rating_info['season_games']})\n"
                f"通算最高到達レート: {rating_info['peak_rating']}"
            ),
            inline=False,
        )

    if last_season:
        top_txt = (
            f" / {last_season['position']}位 / 上位 {last_season['top_percent']:.1f}%"
            if last_season["top_percent"] is not None else ""
        )
        embed.add_field(
            name=f"前シーズン結果（{ladder.label}ラダー）",
            value=(
                f"{last_season['emoji']} {last_season['rank_name']} / 最終レート {last_season['final_rating']}{top_txt}\n"
                f"勝率: {last_season['season_winrate']}% ({last_season['season_wins']}/{last_season['season_games']})"
            ),
            inline=False,
        )

    embed.add_field(
        name="総合成績",
        value=(
            f"参加: {stats['total']}戦\n"
            f"勝利: {stats['wins']}勝\n"
            f"勝率: {_format_rate(stats['wins'], stats['total'])}"
        ),
        inline=False,
    )

    # 役職別
    role_lines = []
    for role_name, data in stats["roles"].items():
        role_lines.append(f"{role_name}: {_format_rate(data['wins'], data['count'])}")
    if role_lines:
        embed.add_field(name="役職別成績", value="\n".join(role_lines), inline=False)

    # 陣営別
    team_lines = []
    for team_name, data in stats["teams"].items():
        team_lines.append(f"{team_name}: {_format_rate(data['wins'], data['count'])}")
    if team_lines:
        embed.add_field(name="陣営別成績", value="\n".join(team_lines), inline=False)

    season_lines = []
    for season in stats.get("season_roles", [])[:3]:
        offset = int(season["offset"])
        if offset == 0:
            label = "今シーズン"
        elif offset == 1:
            label = "前シーズン"
        else:
            label = f"{offset}シーズン前"
        parts = []
        for role in Role:
            data = season["roles"].get(role.value)
            if data is not None:
                parts.append(f"{role.value} {_format_rate(data['wins'], data['count'])}")
        season_lines.append(f"**{label}**\n" + " / ".join(parts))
    if season_lines:
        embed.add_field(
            name="シーズンごとの役職別勝率",
            value="\n".join(season_lines),
            inline=False,
        )

    embed.add_field(
        name="行動・生存記録（詳細記録導入後）",
        value=(
            f"占い的中率: {_format_rate(stats.get('seer_wolf_hits', 0), stats.get('seer_checks', 0))}\n"
            f"護衛成功: {stats.get('guard_successes', 0)}回\n"
            f"初夜に襲撃された回数: {stats.get('first_night_kills', 0)}回\n"
            f"詳細記録のある参加試合: {stats.get('detailed_games', 0)}戦"
        ),
        inline=False,
    )
    embed.add_field(
        name="連続記録・推薦",
        value=(
            f"最長連勝: {stats.get('max_win_streak', 0)}戦\n"
            f"最長連敗: {stats.get('max_loss_streak', 0)}戦\n"
            f"村人の最長連続: {stats.get('max_villager_streak', 0)}戦\n"
            f"受け取った推薦: {stats.get('recommendations_received', 0)}回"
        ),
        inline=False,
    )

    embed.set_footer(text=f"最終プレイ: {format_played_at_jst(stats['last_played'])}")
    return embed


def build_vote_result_embed(votes: dict, players: dict, title: str = "投票結果") -> discord.Embed:
    """投票結果を棒グラフで表示"""
    # 得票集計
    tally: dict[int, int] = {}
    for target_id in votes.values():
        tally[target_id] = tally.get(target_id, 0) + 1

    max_votes = max(tally.values()) if tally else 1

    # 番号順に並べる (得票順ではなく番号順。誰に何票入ったかを
    # 名簿と同じ並びで追えるようにする。最多得票はバーの長さで分かる)。
    # 生存者は0票でも出し、死亡・除外済みでも票が残っている人は隠さない。
    lines = []
    for player in by_number(players.values()):
        count = tally.get(player.user_id, 0)
        if not player.alive and count == 0:
            continue
        bar_len = 10
        filled = round(count / max_votes * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        lines.append(f"{player.display_name} {bar} {count}票")

    embed = discord.Embed(
        title=f"📋 {title}",
        description="```\n" + "\n".join(lines) + "\n```",
        color=discord.Color.orange(),
    )

    # 投票内訳 (投票者の番号順。dictの反復順は投票が届いた順なので、
    # そのまま出すと「誰が先に投票したか」まで公開してしまう)
    detail_lines = []
    for voter_id, target_id in sorted(
        votes.items(),
        key=lambda kv: getattr(players.get(kv[0]), "number", 0),
    ):
        voter = players.get(voter_id)
        target = players.get(target_id)
        if voter and target:
            detail_lines.append(f"{voter.display_name} → {target.display_name}")
    if detail_lines:
        embed.add_field(name="投票内訳", value="\n".join(detail_lines), inline=False)

    return embed


def _display_variant(
    variant: Optional[VariantDefinition],
) -> VariantDefinition:
    return variant or get_variant_definition(DEFAULT_VARIANT_ID)


def _role_rule_lines(variant: VariantDefinition) -> str:
    descriptions = {
        Role.WEREWOLF: "夜に1人を襲撃。相方が誰か分かり、DMの発言は狼同士に中継",
        Role.MADMAN: "能力なし。狼が誰かは分からない",
        Role.SEER: "夜に1人を占い「人狼 / 村人」を判定。開始時に初日白が1件届く",
        Role.MEDIUM: "処刑された人が「人狼 / 村人」かをDMで受信",
        Role.GUARD: "毎夜1人を護衛（放棄不可）。自分と前夜と同じ人は選べない",
        Role.VILLAGER: "能力なし",
    }
    return "\n".join(
        f"**{role.value} ×{count}** — {descriptions[role]}"
        for role, count in variant.role_distribution.items()
    )


def build_rule_embeds(
    variant: Optional[VariantDefinition] = None,
) -> list[discord.Embed]:
    """ルールボタン用: ゲームに必要なレギュレーションだけをまとめる"""
    variant = _display_variant(variant)
    wolf_count = int(variant.role_distribution.get(Role.WEREWOLF, 0))

    if variant.discussion_mode == "turn":
        first, second, later = variant.turn_round_seconds
        channel_description = (
            f"**{variant.player_count}人固定**。昼の議論はVCのみで、"
            f"`#{CH_VILLAGE}` へのテキスト投稿はできません。夜の役職行動はDMです。"
        )
        discussion_rule = (
            "朝の結果発表 → ターン制議論 → 投票 →（同票なら弁明と決戦投票）→ 遺言 → 処刑 → 夜\n"
            f"初日 **{first}秒 → {second}秒** の2巡 ／ 2日目以降 **{later}秒** の1巡。"
            "番号順にVCで話します。\n"
            "初日はランダム起点。翌日以降は襲撃死の次、護衛成功なら襲撃対象、"
            "それ以外はランダムです。\n"
            "COは初日2巡目と2日目以降に名前のみ公開。詳細はVCで話します。"
            f"本人はパス可、割り込みは村全体で1日 **{variant.turn_interrupts_per_day}回**（各30秒）。\n"
            "**仮投票はありません。** 規定の発言後に投票します。\n"
            "通常投票 **1人20秒**（「投票」を押した順・確定ごとに公開） ／ "
            f"弁明 **{RUNOFF_SPEECH_TIME}秒** ／ 遺言 **{LAST_WILL_TIME}秒**（本人かGMが短縮可）\n"
            f"夜 **初日{NIGHT_BASE}秒 / 以降{NIGHT_MIN}秒**（目安。朝は全員の宣言で明ける）"
        )
    else:
        day_base, day_drop, day_minimum = variant.crosstalk_discussion_seconds
        day_base_min = day_base // 60
        day_drop_min = day_drop // 60
        day_min_min = day_minimum // 60
        channel_description = (
            f"**{variant.player_count}人固定**。昼はVCと `#{CH_VILLAGE}`、"
            "夜の役職行動はDMで進行します。"
        )
        discussion_rule = (
            "朝の結果発表 → 議論 → 投票 →（同票なら弁明と決戦投票）→ 遺言 → 処刑 → 夜\n"
            "**議論中の仮投票はありません。** 議論後に「投票」を押した順で発言します。\n"
            f"議論 **初日{day_base_min}分 / 毎日{day_drop_min}分短縮 / 最低{day_min_min}分**"
            " ／ 通常投票 **1人20秒**（確定ごとに公開）\n"
            f"弁明 **{RUNOFF_SPEECH_TIME}秒** ／ 遺言 **{LAST_WILL_TIME}秒**（本人かGMが短縮可）\n"
            f"夜 **初日{NIGHT_BASE}秒 / 以降{NIGHT_MIN}秒**（目安。朝は全員の宣言で明ける）"
        )

    embed = discord.Embed(
        title="レギュレーション",
        description=channel_description,
        color=discord.Color.dark_gold(),
    )
    embed.add_field(
        name="勝利条件",
        value=(
            f"**村陣営** — 人狼{wolf_count}人を全滅させる\n"
            "**狼陣営** — 生存人狼数 ≧ 生存非人狼数\n"
            "※ 狂人は狼陣営の勝ちですが、**占い・霊媒・襲撃・人数判定では「村人」扱い**です"
        ),
        inline=False,
    )
    embed.add_field(
        name=f"役職 ({variant.player_count}人)",
        value=_role_rule_lines(variant),
        inline=False,
    )
    embed.add_field(
        name="1日の流れ",
        value=discussion_rule,
        inline=False,
    )
    embed.add_field(
        name="投票と処刑",
        value=(
            "「🗳️ 投票」を押した順に1人20秒。自分の番に名前を押して確定すると"
            "投票がすぐ公開され、次の人へ進みます（時間切れは棄権）。\n"
            "ボタンはいつでも押せます。押した人がいなくなると全員ミュートで待機するので、"
            "必ず押してください（棄権ボタンはなく、自分には投票できません）。\n"
            f"同票なら候補者が順番に弁明し、候補者以外が一斉に**{VOTE_TIMEOUT}秒**で決戦投票します。"
            "**再び同票ならランダム**で処刑します。\n"
            "1票もなければ処刑なし。処刑が確定した人には遺言時間があります。"
            "**処刑・襲撃された人の役職は非公開**です。"
        ),
        inline=False,
    )
    embed.add_field(
        name="亡くなったら",
        value=(
            f"陣営に関係なく、DMで**人狼だと思う{variant.wolf_guess_slots}人**を提出できます"
            "（実際の人狼本人を除き、的中するとレートに加点。既に亡くなった人も選べます）。\n"
            f"受付は**死亡から{WOLF_GUESS_TIMEOUT // 60}分**で、"
            f"**提出するか時間切れになるまで `#{CH_SPIRIT}` へ入れません。**"
        ),
        inline=False,
    )
    embed.add_field(
        name="夜の行動",
        value=(
            "占い・護衛は**実行確認**を挟んで確定し、今夜は変更できません。"
            "**占い結果は確定と同時に表示**されます。\n"
            "人狼は最後に選んだ対象を襲撃します（「噛みなし」も可）。**人狼同士は噛めません。**\n"
            "襲撃先の変更は制限時間中だけ狼全員に伝わります。"
            "**時間後も変更はできますが、他の狼には伝わりません。**\n"
            "襲撃がなかった朝は、理由を問わず「平和な朝を迎えました」と表示されます。"
        ),
        inline=False,
    )
    embed.add_field(
        name="宣言で進みます",
        value=(
            f"どちらも `#{CH_VILLAGE}` のパネル。宣言待ちは**時間切れでは進みません**。\n"
            "**📩 役職を確認した** — 参加者全員が押すと0日目初夜へ（**一度きり**）\n"
            "**🌅 朝を迎える** — 夜時間終了後に0/生存人数で表示。"
            "押下ごとに人数更新し、全員が押すと朝（**取り消し不可**）\n"
            "離席中は押さずに待たせてください。"
        ),
        inline=False,
    )
    embed.set_footer(text=BOT_VERSION)

    return [embed]


def build_help_embeds(
    variant: Optional[VariantDefinition] = None,
) -> list[discord.Embed]:
    """ヘルプボタン用: Botの使い方と、統計・レート・ランク"""
    variant = _display_variant(variant)
    if variant.discussion_mode == "turn":
        speech_help = (
            "ターン制は**現在の話者だけ**発言できます。本人はパス、ほかの生存者は村全体で"
            f"1日{variant.turn_interrupts_per_day}回まで30秒割り込みができます。\n"
            "COは初日2巡目と2日目以降の「COを宣言」で名前のみ公開し、詳細はVCで話します。\n"
            "投票・弁明・遺言は発言中の本人だけ。夜と一時停止中は全員ミュート。\n"
            "死亡者・観戦者は終了まで発言できません（GMのミュートだけは手動）。"
        )
        gm_turn_help = " / 次の発言へ"
    else:
        speech_help = (
            "議論中は生存者のみ、投票・弁明・遺言は発言中の本人のみ発言できます。\n"
            "夜と一時停止中は全員ミュート。死亡者・観戦者は終了まで発言できません"
            "（GMのミュートだけは手動）。"
        )
        gm_turn_help = ""
    embed3 = discord.Embed(
        title="ヘルプ",
        color=discord.Color.dark_gold(),
    )
    embed3.set_footer(text=BOT_VERSION)
    embed3.add_field(
        name=f"{BOT_VERSION}の変更",
        value=(
            "**0日目初夜30秒**（人狼の挨拶のみ）を追加しました。\n"
            "通常投票は「🗳️ 投票」を押した順に**1人20秒**で発言・確定する方式になり、"
            "決戦は弁明30秒→候補者以外の一斉投票になりました。\n"
            "「朝を迎える」は夜の時間が終わってから **0/生存人数** で出ます（取消不可）。\n"
            "GMメニューに**スキップ**、人狼のDMに**サレンダー**を追加。"
            "人狼予想は陣営に関係なくDMだけで受け付けます。"
        ),
        inline=False,
    )
    embed3.add_field(
        name="DMに届くもの",
        value=(
            "役職確認、人狼の相談と襲撃、占い・護衛、霊媒結果、"
            f"死亡後の**{variant.wolf_guess_slots}狼予想**はDMです。\n"
            "生存中の人狼はDMの**🏳️ サレンダー**に全員同意すると村陣営の勝利になります。\n"
            f"終了後の投票は `#{CH_VILLAGE}` のボタンで行います"
            "（投票内容は本人にしか見えません）。\n"
            "未行動でも朝を迎えられますが、**狩人だけは護衛先を確定するまで進めません。**"
        ),
        inline=False,
    )
    embed3.add_field(
        name="発言とミュート",
        value=speech_help,
        inline=False,
    )
    embed3.add_field(
        name="困ったとき",
        value=(
            "VC切断・脱退で**自動停止**します。復帰後はGMが「再開」、"
            "戻れない人はGMが除外します。\n"
            "停止中も夜の操作と朝の宣言はできます。GMが抜けた場合は廃村です。"
        ),
        inline=False,
    )
    embed3.add_field(
        name="GMの操作",
        value=(
            f"受付中は `#{CH_LOBBY}` の「GM管理」で除外・リセット。\n"
            f"ゲーム中は `#{CH_VILLAGE}` の「GMメニュー・状況」から一時停止 / 再開 / 朝"
            f" / 役職確認を締切 / スキップ{gm_turn_help} / 強制終了 / リセット / 除外ができます。"
        ),
        inline=False,
    )
    embed3.add_field(
        name="終了後の進行ログ",
        value=(
            f"結果発表前に、占い・護衛・襲撃・投票・死亡の記録を `#{CH_VILLAGE}` に貼ります。\n"
            "確定後の再操作も記録されます。"
        ),
        inline=False,
    )
    embed3.add_field(
        name="終わった試合を読み返す",
        value=(
            f"全村で `#{CH_VILLAGE}` / `#{CH_SPIRIT}` を"
            f"**{LOG_CATEGORY_VILLAGE}** / **{LOG_CATEGORY_SPIRIT}** へ保存します。\n"
            f"終了後は全員が読み返せますが書き込みはできません。\n"
            f"試合番号で `#統計` と照合できます（直近{LOG_CATEGORY_LIMIT}試合）。"
        ),
        inline=False,
    )
    embed3.add_field(
        name="不具合・改善の報告",
        value=(
            f"`#{CH_STATS}` の **「不具合・改善を報告」** から送れます"
            "（不具合 / 分かりにくい / 改善要望 / その他）。\n"
            "報告内容はバージョンと一緒に保存され、改善の参考にします。"
        ),
        inline=False,
    )

    return [embed3]


def _format_delta_range(deltas: list[int]) -> str:
    """変動値の集合を「+20」「-13〜-14」の形にする (絶対値の小さい側から)"""
    ordered = sorted(deltas, key=abs)
    if ordered[0] == ordered[-1]:
        return f"{ordered[0]:+d}"
    return f"{ordered[0]:+d}〜{ordered[-1]:+d}"


def _rating_swing(
    variant: VariantDefinition,
    winner_team: Team,
) -> tuple[str, str]:
    """変種の構成とプールでの勝者側・敗者側の変動を表示用に返す。

    手書きするとプール定数を変えたときに必ずズレるので、実際の計算関数から求める。
    卓帯補正はかからない状態 (同帯 = 等倍) の値。
    """
    village_won = winner_team is Team.VILLAGE
    winner_count = (
        variant.village_team_size if village_won else variant.wolf_team_size
    )
    loser_count = (
        variant.wolf_team_size if village_won else variant.village_team_size
    )
    sample = [
        {"player_id": i, "rating": INITIAL_RATING, "won": i < winner_count}
        for i in range(winner_count + loser_count)
    ]
    results = rating_lib.calculate_game_results(
        sample,
        winner_team=winner_team,
        variant_id=variant.variant_id,
        village_win_pool=variant.village_win_pool,
        wolf_win_pool=variant.wolf_win_pool,
    )
    return (
        _format_delta_range(
            [r["delta"] for r in results if r["player_id"] < winner_count]
        ),
        _format_delta_range(
            [r["delta"] for r in results if r["player_id"] >= winner_count]
        ),
    )


def build_rank_spec_embeds() -> list[discord.Embed]:
    """#統計 の「ランク仕様」ボタン用: レート / ランク / 対象とシーズン"""
    rate = discord.Embed(
        title="レート",
        color=discord.Color.blue(),
    )
    rate.add_field(
        name="共通ルール",
        value=(
            f"初期値 **{INITIAL_RATING}**、下限 **{RATING_FLOOR}**（これ以上は下がりません）。\n"
            "試合成績は変種別です。レート・順位も、13人村／9人クロストーク／"
            "9人ターン制の3ラダーで分けます。\n"
            f"下の変動値は勝った陣営へのボーナス +{WIN_PARTICIPATION_BONUS} を含み、"
            "端数は決まったルールで分配します。"
        ),
        inline=False,
    )
    for variant_id in USER_VISIBLE_VARIANT_IDS:
        variant = VARIANT_DEFINITIONS[variant_id]
        village_win_village, village_win_wolf = _rating_swing(
            variant, Team.VILLAGE,
        )
        wolf_win_wolf, wolf_win_village = _rating_swing(
            variant, Team.WOLF,
        )
        ladder = LADDER_DEFINITIONS[variant.ladder_id]
        rate.add_field(
            name=f"{variant.label}（{ladder.label}ラダー）",
            value=(
                f"村勝ちプール **{variant.village_win_pool}**: "
                f"村 {village_win_village} / 狼 {village_win_wolf}\n"
                f"狼勝ちプール **{variant.wolf_win_pool}**: "
                f"狼 {wolf_win_wolf} / 村 {wolf_win_village}\n"
                f"人狼予想 **{variant.wolf_guess_slots}人** / "
                f"終盤ボーナス **{variant.final_day_threshold}回目**の議論から"
            ),
            inline=False,
        )
    rate.add_field(
        name="卓帯補正",
        value=(
            "**格上の陣営が勝つと変動は小さく、格下の陣営が勝つと大きくなります。**\n"
            "陣営ごとに所属ランクの帯（初心者 / 中級者 / 上級者）の中央値を出し、"
            "その差で倍率が決まります。\n"
            "同じ帯 **1.0倍** ／ 1段差 **0.9倍・1.1倍** ／ 2段差 **0.8倍・1.2倍**\n"
            "同じ区分の参加条件プリセットで集めた村は帯が揃うため、常に1.0倍です。"
        ),
        inline=False,
    )
    rate.add_field(
        name="活躍ボーナス（勝敗とは別枠）",
        value=(
            f"🗳️ **処刑された人狼に投票していた村側** … +{BONUS_WOLF_EXECUTION_VOTE}\n"
            "　（処刑を決めた最終ラウンドの票のみ。ランダム処刑は対象外）\n"
            "🐺 **変種ごとの終盤回数に到達したときの人狼** … "
            f"+{BONUS_FINAL_DAY_WOLF}\n"
            f"🔎 **人狼予想の的中1人につき** … +{BONUS_WOLF_GUESS_HIT}"
            f"（初日・{BONUS_WOLF_GUESS_EARLY_MAX_DAY}日目の死亡は"
            f"**{BONUS_WOLF_GUESS_EARLY_MULTIPLIER}倍**）\n"
            f"🛡️ **狩人の護衛成功1回につき** … +{BONUS_GUARD_SUCCESS}\n"
            f"🌙 **初夜に占い師を襲撃できた人狼** … +{BONUS_NIGHT1_SEER_KILL}"
        ),
        inline=False,
    )
    rate.add_field(
        name="人狼予想",
        value=(
            "処刑・襲撃で亡くなり、まだ勝敗が決まっていなければ、陣営に関係なくDMで"
            "**人狼だと思う人数を変種ごとの枠数で**提出できます。\n"
            f"受付は**死亡から{WOLF_GUESS_TIMEOUT // 60}分**で、"
            "**提出するか時間切れになるまで霊界へ入れません**"
            "（霊界で答えを聞けてしまわないようにするためです）。\n"
            "選ぶ相手は既に亡くなった人でも構いません。狂人は正解に含みません。"
            "実際の人狼本人は提出できますが、採点対象外です。"
        ),
        inline=False,
    )
    rate.add_field(
        name="終了後の投票",
        value=(
            f"レート対象卓の終了後、`#{CH_VILLAGE}` にパネルが1枚出ます"
            f"（受付 **{POSTGAME_RECOMMENDATION_TIMEOUT // 60}分**・"
            f"1票につき **+{BONUS_POSTGAME_VOTE}**）。\n"
            "・**勝利陣営**は、手強かった**敗北陣営**の1人へ1票\n"
            "・**霊媒師 / 初日の処刑者 / 初夜の襲撃死者**は、参加者の1人へ1票\n"
            "両方に当てはまる人は2票持ちます。初夜が平和なら襲撃死者枠はありません。\n"
            "GMもプレイヤー参加していれば対象です。**投票者名は公開されません。**"
        ),
        inline=False,
    )
    rate.add_field(
        name="対象",
        value=(
            "正常終了した**すべての村**で、レート・ランク・統計・ランキングが更新されます。\n"
            "各試合は変種に対応するラダーだけを更新します。シーズン境界は3ラダー共通ですが、"
            "順位・レート・履歴は混ぜません。"
        ),
        inline=False,
    )

    rank = discord.Embed(
        title="ランク",
        color=discord.Color.blue(),
    )
    # 並びも比率も config から生成する (手書きすると設定変更で必ずズレる)
    ladder = " → ".join(f"{emoji} {name}" for name, emoji, _ in RANK_SPECS)
    ratios = " / ".join(
        f"{name}{pct * 100:g}%" for name, pct in SEASON_RANK_PERCENTAGES.items()
    )
    rank.add_field(
        name=f"{len(RANK_SPECS)}段階",
        value=ladder,
        inline=False,
    )
    rank.add_field(
        name="決まり方（ラダー別）",
        value=(
            f"レート順の**相対評価**で決まります（{ratios}）。\n"
            f"グランドマスターは全体上位{GRANDMASTER_PERCENTAGE * 100:g}%相当、"
            f"13人村ラダーは最大{LADDER_DEFINITIONS['l13'].grandmaster_slots}人、"
            f"9人クロストークは最大{LADDER_DEFINITIONS['l9_cross'].grandmaster_slots}人、"
            f"9人ターン制は最大{LADDER_DEFINITIONS['l9_turn'].grandmaster_slots}人です。\n"
            f"**1戦目からランクが付き**、通算{SEASON_RANK_MIN_GAMES}戦以上で順位と上位%も確定します。\n"
            "13人村は9段階のDiscordロールを同期します。9人は各ラダーのGM到達時だけ"
            f" **{LADDER_DEFINITIONS['l9_cross'].grandmaster_role_name}**／"
            f"**{LADDER_DEFINITIONS['l9_turn'].grandmaster_role_name}**を付与し、"
            "ほかの段階は統計画面に表示します。3種類のグランドマスターロールを同時に保持できます。\n"
            "**シーズンリセットで全員ブロンズには戻りません**。"
            "レート圧縮の丸めで同点になった場合や暫定ランクでは、"
            "順位・ランクが変わることがあります。"
        ),
        inline=False,
    )

    rank.add_field(
        name="シーズン（管理者向け）",
        value=(
            "`/season_reset` でレートをハーフリセットし、前シーズンの結果を保存します。\n"
            "必要な権限: チャンネル管理 / ロール管理 / ニックネーム変更 / "
            "メンバーをミュート / DM送信。"
        ),
        inline=False,
    )

    # どのバージョンの仕様を見ているかが分かるようにする
    rank.set_footer(text=BOT_VERSION)
    return [rate, rank]
