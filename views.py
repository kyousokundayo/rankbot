"""全UIコンポーネント定義"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import discord

import database
import rating as rating_lib
from config import (
    MAX_PLAYERS, Role, ROLE_TEAM, Team, Phase,
    RUNOFF_SPEECH_TIME, LAST_WILL_TIME, DISCUSSION_GRACE_TIME, MUTE_GRACE_TIME,
    PREPARATION_TIME, DAY_DISCUSSION_BASE,
    DAY_DISCUSSION_DECREASE, DAY_DISCUSSION_MIN, VOTE_TIMEOUT,
    NIGHT_BASE, NIGHT_MIN,
    CH_LOBBY, CH_STATS, CH_VILLAGE,
    SEASON_RANK_MIN_GAMES, GRANDMASTER_PERCENTAGE, GRANDMASTER_SLOTS,
    RANK_SPECS, SEASON_RANK_PERCENTAGES,
    RATING_FLOOR, INITIAL_RATING, WIN_PARTICIPATION_BONUS,
    PRIVATE_ROOM_CREATOR_ROLE_NAME, BOT_VERSION,
    ROOM_DEFINITIONS, RATED_ROOM_NAMES, STATS_MIN_SAMPLES, PLAYER_BLOCK_LIMIT,
)
from models import parse_select_id

if TYPE_CHECKING:
    from game import GameCog
    from room_runner import RoomRunner

log = logging.getLogger(__name__)


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


class MayorInfoView(discord.ui.View):
    def __init__(self, manager: GameCog) -> None:
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(label="専用村を作成", style=discord.ButtonStyle.success, custom_id="mayor_room_create")
    async def create_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.manager.create_private_room_for_member(interaction)

    @discord.ui.button(label="村名変更", style=discord.ButtonStyle.secondary, custom_id="mayor_room_rename")
    async def rename_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("サーバー内でのみ使用できます。", ephemeral=True)
        if not self.manager._has_private_room_creator_role(interaction.user):
            return await interaction.response.send_message(
                f"村名を変更できるのは **{PRIVATE_ROOM_CREATOR_ROLE_NAME}** ロール保持者だけです。",
                ephemeral=True,
            )
        await interaction.response.send_modal(PrivateRoomRenameModal(self.manager))

    @discord.ui.button(label="専用村を削除", style=discord.ButtonStyle.danger, custom_id="mayor_room_delete")
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.manager.delete_private_room_for_member(interaction)


class PrivateRoomRenameModal(discord.ui.Modal, title="専用村名変更"):
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

    def __init__(self, cog: RoomRunner) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        if self.cog.is_private_room():
            manage_button = discord.ui.Button(
                label="専用村管理",
                style=discord.ButtonStyle.secondary,
                custom_id=f"private_room_manage:{self.cog.state.room_id}",
                row=1,
            )
            manage_button.callback = self.private_room_manage_button
            self.add_item(manage_button)
        # 再起動復元などでUIを再投稿した時点で13人+GMが揃っている場合に備え、
        # 生成時に開始ボタンの有効/無効を計算する
        self._refresh_start_button()
        if self.cog.state.phase != Phase.LOBBY:
            for item in self.children:
                item.disabled = True

    def _refresh_start_button(self) -> None:
        start_btn = discord.utils.get(self.children, custom_id="start_game")
        if start_btn:
            # ゲーム進行中の復元でロビーUIを再投稿するケースがあるため、
            # ロビー中のみ有効化する
            state = self.cog.state
            start_btn.disabled = not (
                state.phase == Phase.LOBBY
                and len(state.players) == MAX_PLAYERS
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

        embed = discord.Embed(
            title=f"{state.room_name} - 参加受付",
            description=(
                f"参加者が13人揃ったらGMが「ゲーム開始」を押してください。\n"
                f"参加条件: **{room_note}**"
            ),
            color=discord.Color.dark_gold(),
        )
        embed.add_field(
            name=f"参加者 ({len(players)}/{MAX_PLAYERS})",
            value=player_list,
            inline=False,
        )
        embed.add_field(name="GM", value=gm_name, inline=False)
        if self.cog.is_private_room():
            embed.add_field(
                name="専用村",
                value="村主だけが「専用村管理」から招待と削除を操作できます。",
                inline=False,
            )
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
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.cog.action_lock:
            state = self.cog.state
            user_id = interaction.user.id

            if state.phase != Phase.LOBBY:
                return await interaction.followup.send("現在ゲーム中です。", ephemeral=True)
            if user_id in state.players:
                return await interaction.followup.send("既に参加しています。", ephemeral=True)
            if len(state.players) >= MAX_PLAYERS:
                return await interaction.followup.send("参加者が上限（13人）に達しています。", ephemeral=True)
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

    @discord.ui.button(label="GM取得", style=discord.ButtonStyle.primary, custom_id="get_gm", row=1)
    async def gm_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self.cog.action_lock:
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

    @discord.ui.button(label="GM放棄", style=discord.ButtonStyle.primary, custom_id="release_gm", row=1)
    async def gm_release_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
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

    @discord.ui.button(label="ゲーム開始", style=discord.ButtonStyle.primary, custom_id="start_game",
                       disabled=True, row=2)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self.cog.action_lock:
            state = self.cog.state
            if state.phase != Phase.LOBBY:
                return await interaction.response.send_message("現在ゲーム中です。", ephemeral=True)
            if interaction.user.id != state.gm_id:
                return await interaction.response.send_message("GMのみがゲームを開始できます。", ephemeral=True)
            if len(state.players) != MAX_PLAYERS:
                return await interaction.response.send_message(
                    f"参加者が揃っていません ({len(state.players)}/{MAX_PLAYERS})", ephemeral=True
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

    @discord.ui.button(label="次村", style=discord.ButtonStyle.primary, custom_id="rematch_game", row=2)
    async def rematch_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.cog.action_lock:
            result = await self.cog.rematch(interaction.user)
            await self._update(interaction)
            await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="GM管理", style=discord.ButtonStyle.secondary, custom_id="lobby_gm_menu", row=2)
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

    @discord.ui.button(label="ルール", style=discord.ButtonStyle.secondary, custom_id="rule_btn", row=2)
    async def rule_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embeds = build_rule_embeds()
        await interaction.response.send_message(embeds=embeds, ephemeral=True)

    @discord.ui.button(label="ヘルプ", style=discord.ButtonStyle.secondary, custom_id="help_btn", row=2)
    async def help_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embeds = build_help_embeds()
        await interaction.response.send_message(embeds=embeds, ephemeral=True)

    async def private_room_manage_button(self, interaction: discord.Interaction) -> None:
        if not self.cog.can_manage_private_room(interaction.user):
            return await interaction.response.send_message("この専用村を管理できるのは村主だけです。", ephemeral=True)
        await interaction.response.send_message(
            "専用村の参加権限を管理します。",
            view=PrivateRoomManageView(self.cog),
            ephemeral=True,
        )


class LobbyGMMenuView(discord.ui.View):
    """ロビーを3段以内に保つため、低頻度のGM操作を一時表示する。"""

    def __init__(self, cog: RoomRunner) -> None:
        super().__init__(timeout=180)
        self.cog = cog

    def _is_gm(self, interaction: discord.Interaction) -> bool:
        state = self.cog.state
        return state.phase == Phase.LOBBY and interaction.user.id == state.gm_id

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


class PrivateRoomManageView(discord.ui.View):
    def __init__(self, cog: RoomRunner) -> None:
        super().__init__(timeout=180)
        self.cog = cog

    def _can_manage(self, interaction: discord.Interaction) -> bool:
        return self.cog.can_manage_private_room(interaction.user)

    @discord.ui.button(label="招待", style=discord.ButtonStyle.success, custom_id="private_room_invite")
    async def invite_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._can_manage(interaction):
            return await interaction.response.send_message("この専用村を管理できるのは村主だけです。", ephemeral=True)
        await interaction.response.send_message(
            "招待するユーザーを選んでください。",
            view=PrivateRoomMemberSelectView(self.cog, mode="invite"),
            ephemeral=True,
        )

    @discord.ui.button(label="削除", style=discord.ButtonStyle.danger, custom_id="private_room_remove")
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._can_manage(interaction):
            return await interaction.response.send_message("この専用村を管理できるのは村主だけです。", ephemeral=True)
        await interaction.response.send_message(
            "削除するユーザーを選んでください。",
            view=PrivateRoomMemberSelectView(self.cog, mode="remove"),
            ephemeral=True,
        )


class PrivateRoomMemberSelectView(discord.ui.View):
    def __init__(self, cog: RoomRunner, *, mode: str) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.mode = mode
        select = discord.ui.UserSelect(
            placeholder="ユーザーを選択",
            min_values=1,
            max_values=1,
            custom_id=f"private_room_{mode}_select",
        )
        select.callback = self.select_callback
        self.add_item(select)

    def _resolve_member(self, interaction: discord.Interaction, selected) -> Optional[discord.Member]:
        if isinstance(selected, discord.Member):
            return selected
        if interaction.guild is None:
            return None
        return interaction.guild.get_member(selected.id)

    async def select_callback(self, interaction: discord.Interaction) -> None:
        if not self.cog.can_manage_private_room(interaction.user):
            return await interaction.response.send_message("この専用村を管理できるのは村主だけです。", ephemeral=True)
        if interaction.guild is None:
            return await interaction.response.send_message("サーバー内でのみ使用できます。", ephemeral=True)

        selected = self.children[0].values[0]
        member = self._resolve_member(interaction, selected)
        if member is None:
            return await interaction.response.send_message("対象メンバーが見つかりません。", ephemeral=True)

        if self.mode == "invite":
            message = await self.cog.manager.add_private_room_member(self.cog, member)
        else:
            message = await self.cog.manager.remove_private_room_member(self.cog, member)
        await interaction.response.send_message(message, ephemeral=True)


# ============================================================
# GM コントロールパネル
# ============================================================

_PHASE_LABELS = {
    Phase.LOBBY: "参加受付",
    Phase.PREPARATION: "役職確認・開始準備",
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
        alive_ids = {player.user_id for player in alive}
        voted = len(alive_ids & set(state.votes))
        embed.add_field(name="投票", value=f"{voted} / {len(alive_ids)}人", inline=True)
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

    if state.disconnected_players:
        waiting = [
            player.display_name
            for player in state.players.values()
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
    def __init__(self, cog: RoomRunner) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.game_run_id = cog.state.game_run_id
        state = cog.state
        effective_phase = state.phase_before_pause if state.phase == Phase.PAUSED else state.phase
        self.pause_btn.disabled = state.paused or self._settlement_locked()
        self.resume_btn.disabled = not state.paused and state.pending_winner is None
        self.force_morning_btn.disabled = effective_phase != Phase.NIGHT or self._settlement_locked()
        self.force_prep_btn.disabled = (
            effective_phase != Phase.PREPARATION
            or state.paused
            or self._settlement_locked()
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
            "⚠️ 未確認者がいても役職確認を締め切り、議論を開始しますか？",
            view=DangerConfirmView(
                interaction.user.id, execute, confirm_label="締め切って開始"
            ),
            ephemeral=True,
        )

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
            players = list(state.players.values())
        else:
            players = state.alive_players()

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
    """通常投票・決戦投票に共通する、本人だけの最終確認。"""

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
        for item in self.children:
            item.disabled = True
        self.stop()
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.source.commit_vote(interaction, self.target_id)

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


class _BaseVoteView(discord.ui.View):
    expected_phase: Phase
    button_prefix: str
    button_style: discord.ButtonStyle
    persist_label: str

    def __init__(self, cog: RoomRunner, candidates: list, voters: list) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.game_run_id = cog.state.game_run_id
        self.day_generation = cog.state.day_generation
        cog.register_game_view(self)
        self.voters = {v.user_id for v in voters}

        # 13人なら5・5・3の3段。確認ボタンはephemeralなので公開行を増やさない。
        for player in candidates:
            btn = discord.ui.Button(
                label=player.display_name,
                style=self.button_style,
                custom_id=f"{self.button_prefix}_{player.user_id}",
            )
            btn.callback = self._make_callback(player.user_id)
            self.add_item(btn)

    def _vote_error(self, voter_id: int, target_id: int) -> Optional[str]:
        state = self.cog.state
        if (
            not self.cog.is_current_day_view(self.game_run_id, self.day_generation)
            or state.phase != self.expected_phase
        ):
            return "⏳ 現在この操作はできません。"
        if voter_id not in self.voters:
            return "投票権がありません。"
        if voter_id in state.votes:
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
            await interaction.followup.send(
                f"**{target_name}** に投票しますか？確定後は変更できません。",
                view=VoteConfirmView(self, interaction.user.id, target_id),
                ephemeral=True,
            )

        return callback

    async def commit_vote(
        self, interaction: discord.Interaction, target_id: int
    ) -> None:
        async with self.cog.action_lock:
            state = self.cog.state
            voter_id = interaction.user.id
            error = self._vote_error(voter_id, target_id)
            if error:
                await interaction.followup.send(error, ephemeral=True)
                return

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
                await interaction.followup.send(
                    "❌ 投票を保存できませんでした。もう一度投票してください。",
                    ephemeral=True,
                )
                return

            alive_voters = {
                uid
                for uid in self.voters
                if state.get_player(uid) is not None and state.get_player(uid).alive
            }
            if alive_voters <= state.votes.keys():
                state.vote_complete_event.set()
        await interaction.followup.send("✅ 投票しました。", ephemeral=True)


class VoteView(_BaseVoteView):
    expected_phase = Phase.DAY_VOTE
    button_prefix = "vote"
    button_style = discord.ButtonStyle.primary
    persist_label = "投票"


class RunoffVoteView(_BaseVoteView):
    expected_phase = Phase.DAY_RUNOFF_VOTE
    button_prefix = "runoff"
    button_style = discord.ButtonStyle.danger
    persist_label = "決戦投票"


# ============================================================
# 夜アクション: 人狼 (DM)
# ============================================================

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

        if target_id == state.guard_previous:
            return await interaction.response.send_message(
                "⚠️ 前回と同じ対象は選択できません。別の対象を選んでください。", ephemeral=True
            )

        target = state.get_player(target_id)
        if target is None or not target.alive:
            return await interaction.response.send_message(
                "❌ その対象は護衛できません (存在しないか、既に死亡しています)。", ephemeral=True
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
        if target.user_id == state.guard_previous:
            return "⚠️ 前回と同じ対象は護衛できません。", False
        if not target.alive:
            return "❌ その対象は護衛できません (既に死亡しています)。", False

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

    参加者全員が「役職を確認した」を押すと議論が始まる。目安時間が切れても
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
        # 13人同時押下でも Discord の3秒応答期限を失わないよう先にACKする。
        await interaction.response.defer()
        async with self.cog.action_lock:
            content, error = await self.cog.toggle_prep_ready(interaction.user)
            if error:
                return await interaction.followup.send(error, ephemeral=True)
            try:
                await interaction.edit_original_response(content=content, view=self)
            except (discord.NotFound, discord.HTTPException):
                pass


# ============================================================
# 朝を迎える (夜フェーズの終了宣言)
# ============================================================

class MorningReadyView(discord.ui.View):
    """夜の間、生存者のDMへ1通ずつ配るパネル。

    生存者全員が「朝を迎える」を押すと夜が明ける。制限時間が切れても
    自動では明けないので、離席したい人は押さずに待たせればよい
    (一時停止の代わり)。AFKで止まったままにならないための強制夜明けは
    GMコントロールパネルの「朝」だけに置く (このパネルには参加者用の
    ボタンしか出さず、押せないボタンで紛らわせない)。

    **夜の終わりにstopしない** (night=Trueで登録しない) 点が夜の役職UIと違う。
    View.stop() 後は discord.py がコールバックを起動しないため、押した人には
    Discordの「インタラクションに失敗しました」しか出ない。DMには前夜以前の
    パネルが残り続けて誤タップしやすいので、Viewは生かしたまま
    night_generation で弾き、理由を本人へ返す。ゲーム終了時に
    _stop_all_game_views でまとめて停止する。
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
        ):
            return await interaction.response.send_message("⏳ この夜のパネルは終了しています。", ephemeral=True)
        # 13人同時押下で後続がlock+DB保存待ちになっても
        # Discordの3秒応答期限を失わないよう先にACKする。
        await interaction.response.defer()
        async with self.cog.action_lock:
            content, error = await self.cog.toggle_morning_ready(interaction.user)
            if error:
                return await interaction.followup.send(error, ephemeral=True)
            # 状態更新とメッセージ編集を同じロック内で直列化し、同時押しで
            # 新しい人数表示が古い表示に巻き戻るのを防ぐ。
            try:
                await interaction.edit_original_response(content=content, view=self)
            except (discord.NotFound, discord.HTTPException):
                pass


# ============================================================
# 弁明終了ボタン
# ============================================================

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


# ============================================================
# 終了後推薦 (DM)
# ============================================================

class PostgameRecommendationView(discord.ui.View):
    """対象選択と最終確認を1つのDM内で完結させる推薦UI。"""

    def __init__(
        self,
        *,
        game_id: int,
        guild_id: int,
        voter_id: int,
        candidates: list,
        timeout: float,
        on_confirmed: Callable[[int], None],
    ) -> None:
        super().__init__(timeout=timeout)
        self.game_id = game_id
        self.guild_id = guild_id
        self.voter_id = voter_id
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
                "👏 **終了後推薦（+1レート）**\n"
                f"**{target.display_name}** に推薦を確定しますか？\n"
                "確定後は変更できません。推薦者名は公開されません。"
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
            self.on_confirmed(self.voter_id)
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
        await interaction.followup.send(
            f"✅ 報告を保存しました。ありがとうございます。（報告ID: `{report_id}`）",
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


class OverallRoomStatsSelect(discord.ui.Select):
    def __init__(self, owner: "OverallStatsFilterView") -> None:
        self.owner = owner
        options = [discord.SelectOption(label="全卓", value="all", default=True)]
        options.extend(
            discord.SelectOption(label=room.name, value=room.room_id)
            for room in ROOM_DEFINITIONS
        )
        super().__init__(
            placeholder="試合指標を表示する卓",
            options=options,
            row=0,
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
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        self.owner.rank_name = None if value == "all" else value
        for option in self.options:
            option.default = option.value == value
        await self.owner.refresh(interaction)


class OverallStatsFilterView(discord.ui.View):
    """試合指標は卓、プレイヤー指標は試合時表示ランクで独立に絞る。"""

    def __init__(self, guild_id: int) -> None:
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.room_id: Optional[str] = None
        self.rank_name: Optional[str] = None
        self.add_item(OverallRoomStatsSelect(self))
        self.add_item(OverallRankStatsSelect(self))

    async def load_embed(self, guild: discord.Guild) -> discord.Embed:
        game_stats = await database.get_overall_game_stats(
            self.guild_id, room_id=self.room_id,
        )
        rank_stats = await database.get_rank_player_stats(
            self.guild_id, rank_name=self.rank_name,
        )
        room_label = "全卓" if self.room_id is None else next(
            (room.name for room in ROOM_DEFINITIONS if room.room_id == self.room_id),
            self.room_id,
        )
        rank_label = self.rank_name or "確定ランク全体"
        return build_overall_stats_embed(
            game_stats, rank_stats,
            room_label=room_label, rank_label=rank_label, guild=guild,
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ サーバー内でのみ使用できます。", ephemeral=True,
            )
        await interaction.response.defer()
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

    async def _sync_member_rank_role(
        self,
        member: Optional[discord.Member],
        rating_info: Optional[dict],
    ) -> None:
        if member is None or rating_info is None:
            return
        try:
            await self.cog._sync_rank_role(member, rating_info["rank_name"])
        except Exception as e:
            log.warning(f"統計表示時のロール同期失敗 ({member.display_name}): {e}")

    @discord.ui.button(label="自分の統計", style=discord.ButtonStyle.secondary, custom_id="stats_self", row=0)
    async def self_stats(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._defer_ephemeral_query(interaction):
            return
        from database import (
            get_player_current_rank_info,
            get_player_latest_season_result,
            get_player_stats,
        )
        stats = await get_player_stats(interaction.user.id, interaction.guild.id)
        if stats is None:
            return await interaction.followup.send("まだゲームに参加していません。", ephemeral=True)
        rating_info = await get_player_current_rank_info(interaction.user.id, interaction.guild.id)
        last_season = await get_player_latest_season_result(interaction.user.id, interaction.guild.id)
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        await self._sync_member_rank_role(member, rating_info)
        embed = build_stats_embed(interaction.user, stats, rating_info, last_season)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="全体ランキング", style=discord.ButtonStyle.secondary, custom_id="stats_all", row=0)
    async def all_stats(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._defer_ephemeral_query(interaction):
            return
        from database import get_current_season_leaderboard

        top = await get_current_season_leaderboard(interaction.guild.id, limit=20)
        if not top:
            return await interaction.followup.send("レーティングデータがありません。", ephemeral=True)

        lines = []
        for i, d in enumerate(top, 1):
            member = interaction.guild.get_member(d["player_id"])
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
            title="今シーズンランキング",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"相対ランクは今シーズン{SEASON_RANK_MIN_GAMES}戦以上のプレイヤーのみ対象")
        await interaction.followup.send(embed=embed, ephemeral=True)

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
        from database import get_latest_season_results

        reset_id, rows = await get_latest_season_results(interaction.guild.id, limit=20)
        if reset_id == 0 or not rows:
            return await interaction.followup.send("前シーズンの結果はまだありません。", ephemeral=True)

        lines = []
        for i, row in enumerate(rows, 1):
            member = interaction.guild.get_member(row["player_id"])
            name = member.display_name if member else f"ID:{row['player_id']}"
            top_pct = f" / 上位{row['top_percent']:.1f}%" if row["top_percent"] is not None else ""
            lines.append(
                f"`{i:>2}.` {row['emoji']} **{row['final_rating']}** [{row['rank_name']}] "
                f"{name} — {row['season_winrate']}% ({row['season_wins']}/{row['season_games']}){top_pct}"
            )

        embed = discord.Embed(
            title="前シーズン最終順位",
            description="\n".join(lines),
            color=discord.Color.purple(),
        )
        embed.set_footer(text=f"シーズンリセットID: {reset_id}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="最近の試合", style=discord.ButtonStyle.secondary, custom_id="stats_recent_games", row=1)
    async def recent_games(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._defer_ephemeral_query(interaction):
            return
        from database import get_recent_games

        rows = await get_recent_games(interaction.guild.id, limit=10)
        if not rows:
            return await interaction.followup.send("試合履歴はまだありません。", ephemeral=True)

        lines = [
            f"`{row['game_id']:>4}` {row['room_name']} / {row['winner_team']} / {format_played_at(row['played_at'])}"
            for row in rows
        ]
        embed = discord.Embed(
            title="最近の試合",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="自分の履歴", style=discord.ButtonStyle.secondary, custom_id="stats_my_history", row=1)
    async def my_history(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._defer_ephemeral_query(interaction):
            return
        from database import get_player_recent_games

        rows = await get_player_recent_games(interaction.user.id, interaction.guild.id, limit=10)
        if not rows:
            return await interaction.followup.send("まだ試合履歴がありません。", ephemeral=True)

        lines = []
        for row in rows:
            result = "勝利" if row["won"] else "敗北"
            delta_txt = ""
            if row["rating_before"] is not None and row["rating_after"] is not None:
                delta = row["rating_after"] - row["rating_before"]
                sign = "+" if delta >= 0 else ""
                elo_delta = row["elo_delta"] or 0
                win_bonus = row["bonus"] or 0
                recommendation_bonus = row["recommendation_bonus"] or 0
                elo_sign = "+" if elo_delta >= 0 else ""
                parts = [f"本体{elo_sign}{elo_delta}"]
                if win_bonus:
                    parts.append(f"勝利+{win_bonus}")
                if recommendation_bonus:
                    parts.append(f"推薦+{recommendation_bonus}")
                delta_txt = (
                    f" / {row['rating_before']}→{row['rating_after']} ({sign}{delta}; "
                    + " / ".join(parts) + ")"
                )
            lines.append(
                f"`{row['game_id']:>4}` {row['room_name']} / {row['role']} / {result}{delta_txt}"
            )

        embed = discord.Embed(
            title="自分の最近の試合",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

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

        blocked_ids = await list_player_blocks(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(
            f"同村拒否リスト: {len(blocked_ids)}/{PLAYER_BLOCK_LIMIT}\n"
            "この設定と解除は本人にだけ表示されます。",
            view=PlayerBlockSettingsView(
                manager, interaction.guild.id, interaction.user.id, blocked_ids,
            ),
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
        from database import (
            get_player_current_rank_info,
            get_player_latest_season_result,
            get_player_stats,
        )
        stats = await get_player_stats(target.id, interaction.guild.id)
        if stats is None:
            return await interaction.followup.send(
                f"{target.display_name} はまだゲームに参加していません。", ephemeral=True
        )
        rating_info = await get_player_current_rank_info(target.id, interaction.guild.id)
        last_season = await get_player_latest_season_result(target.id, interaction.guild.id)
        member = target if isinstance(target, discord.Member) else interaction.guild.get_member(target.id)
        await self._sync_member_rank_role(member, rating_info)
        embed = build_stats_embed(target, stats, rating_info, last_season)
        await interaction.followup.send(embed=embed, ephemeral=True)


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


def build_overall_stats_embed(
    game_stats: dict,
    rank_stats: dict,
    *,
    room_label: str,
    rank_label: str,
    guild: discord.Guild,
) -> discord.Embed:
    """卓単位の試合指標と、試合時ランク単位の個人指標を分けて表示する。"""
    games = int(game_stats["games"])
    detailed_games = int(game_stats["detailed_games"])
    village_wins = int(game_stats["wins"].get(Team.VILLAGE.value, 0))
    wolf_wins = int(game_stats["wins"].get(Team.WOLF.value, 0))
    embed = discord.Embed(
        title=f"全体データ — {room_label}",
        description=(
            "試合指標は上のメニューで**卓別**、プレイヤー指標は下のメニューで"
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
) -> discord.Embed:
    # ランクに合わせた色
    if rating_info:
        embed_color = discord.Color(rating_info["color"])
    else:
        embed_color = discord.Color.blue()

    embed = discord.Embed(
        title=f"{user.display_name} の統計",
        color=embed_color,
    )

    # レート/ランク (最上部)
    if rating_info:
        provisional_txt = " (暫定)" if rating_info["provisional"] else ""
        role_name = rating_lib.get_rank_role_name(rating_info["rank_name"])
        if rating_info["top_percent"] is None:
            top_txt = (
                f"計測中 / 今季 {rating_info['season_games']}戦\n"
                f"{SEASON_RANK_MIN_GAMES}戦到達で相対ランクと順位が確定します"
            )
        else:
            top_txt = (
                f"{rating_info['position']}位 / {rating_info['active_count']}人中 / "
                f"上位 {rating_info['top_percent']:.1f}% / 今季 {rating_info['season_games']}戦"
            )
            if rating_info["rank_name"] in ("マスター", "グランドマスター"):
                top_txt += "\nマスター帯の順位表示対象です"
        embed.add_field(
            name="現在シーズン",
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
            name="前シーズン結果",
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

    # 得票順ソート
    sorted_tally = sorted(tally.items(), key=lambda x: x[1], reverse=True)
    max_votes = sorted_tally[0][1] if sorted_tally else 1

    lines = []
    for target_id, count in sorted_tally:
        player = players.get(target_id)
        if player is None:
            continue
        bar_len = 10
        filled = round(count / max_votes * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        lines.append(f"{player.display_name} {bar} {count}票")

    # 0票のプレイヤーも表示
    for pid, p in players.items():
        if p.alive and pid not in tally:
            lines.append(f"{p.display_name} {'░' * 10} 0票")

    embed = discord.Embed(
        title=f"📋 {title}",
        description="```\n" + "\n".join(lines) + "\n```",
        color=discord.Color.orange(),
    )

    # 投票内訳
    detail_lines = []
    for voter_id, target_id in votes.items():
        voter = players.get(voter_id)
        target = players.get(target_id)
        if voter and target:
            detail_lines.append(f"{voter.display_name} → {target.display_name}")
    if detail_lines:
        embed.add_field(name="投票内訳", value="\n".join(detail_lines), inline=False)

    return embed


def build_rule_embeds() -> list[discord.Embed]:
    """ルールボタン用: ゲームに必要なレギュレーションだけをまとめる"""
    day_base_min = DAY_DISCUSSION_BASE // 60
    day_drop_min = DAY_DISCUSSION_DECREASE // 60
    day_min_min = DAY_DISCUSSION_MIN // 60

    embed = discord.Embed(
        title="レギュレーション",
        description=f"**{MAX_PLAYERS}人固定**。昼はVCと `#{CH_VILLAGE}`、夜の役職行動はDMで進行します。",
        color=discord.Color.dark_gold(),
    )
    embed.add_field(
        name="勝利条件",
        value=(
            "**村陣営** — 人狼3人を全滅させる\n"
            "**狼陣営** — 生存人狼数 ≧ 生存非人狼数\n"
            "※ 狂人は狼陣営の勝ちですが、**占い・霊媒・人数判定では「村人」扱い**です"
        ),
        inline=False,
    )
    embed.add_field(
        name=f"役職 ({MAX_PLAYERS}人)",
        value=(
            "**人狼 ×3** — 夜に1人を襲撃。相方が誰か分かり、DMの発言は狼同士に中継\n"
            "**狂人 ×1** — 能力なし。狼が誰かは分からない\n"
            "**占い師 ×1** — 夜に1人を占い「人狼 / 村人」を判定。開始時に初日白が1件届く\n"
            "**霊媒師 ×1** — 処刑された人が「人狼 / 村人」かをDMで受信\n"
            "**狩人 ×1** — 夜に1人を護衛。自分と前夜と同じ人は選べない\n"
            "**村人 ×6** — 能力なし"
        ),
        inline=False,
    )
    embed.add_field(
        name="1日の流れ",
        value=(
            "朝の結果発表 → 議論 → 投票 →（同票なら弁明と決戦投票）→ 遺言 → 処刑 → 夜\n"
            "初日は**参加者全員が「📩 役職を確認した」を押すと議論が始まります**。\n"
            f"夜明けにミュートが解除され、**{DISCUSSION_GRACE_TIME}秒後**に音が鳴って議論が始まります。\n"
            f"議論終了・投票前には**{MUTE_GRACE_TIME}秒のミュート整列**を挟みます。"
        ),
        inline=False,
    )
    embed.add_field(
        name="時間",
        value=(
            f"役職確認 **{PREPARATION_TIME}秒**（目安。議論は全員の「役職を確認した」で始まる）\n"
            f"議論 **初日{day_base_min}分 / 毎日{day_drop_min}分短縮 / 最低{day_min_min}分**\n"
            f"投票 **{VOTE_TIMEOUT}秒**（全員が投票したら即開示）\n"
            f"弁明 **{RUNOFF_SPEECH_TIME}秒** / 遺言 **{LAST_WILL_TIME}秒**（本人かGMが短縮可）\n"
            f"夜 **初日{NIGHT_BASE}秒 / 以降{NIGHT_MIN}秒固定**（目安。朝はDMでの全員の宣言で明ける）"
        ),
        inline=False,
    )
    embed.add_field(
        name="投票と処刑",
        value=(
            "自分には投票できず、棄権もできません。\n"
            "同票なら候補者が順番に弁明してから決戦投票、**再び同票ならランダム**で処刑します。\n"
            "処刑が確定した人には遺言時間があります。**処刑・襲撃された人の役職は非公開**です。"
        ),
        inline=False,
    )
    embed.add_field(
        name="夜の行動",
        value=(
            "**占い・護衛は「実行確認」を挟んで確定します（誤タップ防止）。**\n"
            "確定すると今夜は変更できません。**占い結果は確定と同時に表示**されます。\n"
            "人狼は最後に選ばれた対象を襲撃します（「噛みなし」も選べます）。"
            "現在の襲撃先はDMに常に表示され、制限時間中の変更は狼全員に伝わります。\n"
            "**人狼どうしのやり取りは夜の制限時間で終わります**（会話の中継も、"
            "襲撃先の変更通知も止まります）。**襲撃先の選択自体は夜明けまで可能**ですが、"
            "その変更は他の人狼へは伝わりません。\n"
            "襲撃がなかった朝は、理由を問わず「平和な朝を迎えました」と表示されます。"
        ),
        inline=False,
    )
    embed.add_field(
        name="役職を確認した",
        value=(
            "ゲーム開始直後は時間切れでは議論が始まりません。"
            "**#昼 のパネルで参加者全員が「📩 役職を確認した」を押す**と議論が始まります。\n"
            "押し直しで取り消せます。DMを開くのが遅れても待ってもらえます。"
        ),
        inline=False,
    )
    embed.add_field(
        name="朝を迎える",
        value=(
            "夜は時間切れでは明けません。**DMに届くパネルで生存者全員が「朝を迎える」を押す**と朝になります。\n"
            "押し直しで取り消せます。離席したいときは押さずに待たせてください。\n"
            "**人数はあなたが押したときにDMへ反映**されます（全体の進捗はGMが確認します）。\n"
            "未行動の役職が押した場合は警告が出て、もう一度押すと未行動のまま確定します。\n"
            "戻らない人がいる場合は、GMがGMメニューの「朝」で進行できます。"
        ),
        inline=False,
    )
    embed.set_footer(text=BOT_VERSION)

    return [embed]


def build_help_embeds() -> list[discord.Embed]:
    """ヘルプボタン用: Botの使い方と、統計・レート・ランク"""
    embed3 = discord.Embed(
        title="ヘルプ",
        color=discord.Color.dark_gold(),
    )
    embed3.set_footer(text=BOT_VERSION)
    embed3.add_field(
        name="DMでやること",
        value=(
            "役職の確認、人狼の相談と襲撃、占い・護衛の選択、霊媒結果の受信。\n"
            "占い・護衛は選択後に**実行確認**があり、確定すると変更できません。\n"
            "**占い結果は確定した瞬間にDMへ表示**されます。"
        ),
        inline=False,
    )
    embed3.add_field(
        name="離席したいとき",
        value=(
            "夜は生存者全員が **DMの「朝を迎える」** を押すまで明けません。\n"
            "席を外したいときは押さずに待たせてください（昼は口頭でGMに伝えてください）。\n"
            "**一時停止中も占い・護衛・襲撃・朝の宣言は操作できます**"
            "（止まっている間に選べないと、再開後に消えたように見えるため）。"
        ),
        inline=False,
    )
    embed3.add_field(
        name="発言とミュート",
        value=(
            "議論中は生存者のみ、投票と夜は全員ミュート、弁明と遺言は本人だけ発言できます。\n"
            "死亡者と観戦者はゲーム中発言できません（終了時に解除）。\n"
            "**一時停止すると全員ミュートされます**（切断者不在のまま議論が続くのを防ぐため）。\n"
            "**GMのみミュート / ミュート解除は手動です。**"
        ),
        inline=False,
    )
    embed3.add_field(
        name="困ったとき",
        value=(
            "VCから落ちる・サーバーを抜けると**自動で一時停止**し、復帰を待ちます。\n"
            "戻ったらGMが「再開」を押します。戻れない場合はGMが「プレイヤー除外」で外して再開できます。\n"
            "ゲーム中にGMが抜けた場合は廃村になります。"
        ),
        inline=False,
    )
    embed3.add_field(
        name="GMの操作",
        value=(
            f"**受付中** — `#{CH_LOBBY}` の「GM管理」から参加者を除外 / 受付をリセット。\n"
            f"**ゲーム中** — `#{CH_VILLAGE}` 末尾の「GMメニュー・状況」を押すとGM専用の画面が開き、"
            "フェーズ・日数・生存人数・投票や宣言の進捗・復帰待ちを確認しながら、"
            "一時停止 / 再開 / 朝（強制で夜を明ける）/ 強制終了 / リセット / プレイヤー除外ができます。\n"
            "（役職と夜の行動内容は表示されません）"
        ),
        inline=False,
    )
    embed3.add_field(
        name="終了後の推薦",
        value=(
            "レート対象卓の終了後、**霊媒師・初日の処刑者・初夜の襲撃死者**にDMが届き、"
            "自分以外の1人へ **+1** を贈れます（3分以内・推薦者名は非公開）。"
        ),
        inline=False,
    )
    embed3.add_field(
        name="専用村",
        value=(
            f"**{PRIVATE_ROOM_CREATOR_ROLE_NAME}** ロールを持っていると自分の村を作れます。\n"
            "作成・村名変更・削除は専用村作成チャンネルから、"
            "参加者の招待・除外は自分の村の受付にある「専用村管理」から行います。"
        ),
        inline=False,
    )
    embed3.add_field(
        name="終了後の進行ログ",
        value=(
            f"ゲームが終わると、結果発表の直後に**その村の全行動**が `#{CH_VILLAGE}` に貼られます。\n"
            "占い・護衛・襲撃先・投票・処刑・襲撃死を発生順に並べたものです。\n"
            "**確定済みの操作をやり直そうとした記録も残る**ので、"
            "「2回占えてしまったのでは？」といった疑いをその場で確かめられます。"
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


def build_rank_spec_embeds() -> list[discord.Embed]:
    """#統計 の「ランク仕様」ボタン用: レート / ランク / 対象とシーズン"""
    rate = discord.Embed(
        title="レート",
        color=discord.Color.blue(),
    )
    rate.add_field(
        name="増減",
        value=(
            f"初期値 **{INITIAL_RATING}**、下限 **{RATING_FLOOR}**（これ以上は下がりません）。\n"
            "**村が勝つ** → 村 +11 / 狼 -22〜-23\n"
            "**狼が勝つ** → 狼 +16 / 村 -6〜-7\n"
            f"（勝った陣営へのボーナス +{WIN_PARTICIPATION_BONUS} を含む。"
            "端数は決まったルールで分配）"
        ),
        inline=False,
    )
    rate.add_field(
        name="終了後推薦",
        value=(
            "レート対象卓の終了後、**霊媒師・初日の処刑者・初夜の襲撃死者**が、"
            "自分以外の参加者1人へ **+1** を贈れます。初夜が平和なら襲撃死者枠はありません。\n"
            "同じ人が複数条件に当てはまっても1票です。GMもプレイヤー参加していれば推薦対象です。\n"
            "DMで3分以内に確定し、推薦者名は公開されません。"
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
        name="決まり方",
        value=(
            f"レート順の**相対評価**で決まります（{ratios}）。\n"
            f"グランドマスターは全体上位{GRANDMASTER_PERCENTAGE * 100:g}%相当、"
            f"最大{GRANDMASTER_SLOTS}人です。\n"
            f"**1戦目からランクが付き**、{SEASON_RANK_MIN_GAMES}戦以上で順位と上位%も表示されます。\n"
            "ランクに応じたロールが自動で付き、見える卓もそれに連動します。"
        ),
        inline=False,
    )

    misc = discord.Embed(
        title="対象とシーズン",
        color=discord.Color.blue(),
    )
    misc.add_field(
        name="対象と例外",
        value=(
            f"レートが動くのは **{' / '.join(RATED_ROOM_NAMES)}** "
            f"の{len(RATED_ROOM_NAMES)}卓です。\n"
            "現在、初心者・中級者・上級者は準備中のため、サーバー管理者だけに表示されます。"
        ),
        inline=False,
    )
    misc.add_field(
        name="シーズン（管理者向け）",
        value=(
            "`/season_reset` でレートをハーフリセットし、前シーズンの結果を保存します。\n"
            "Botにはチャンネル管理 / ロール管理 / ニックネーム変更 / メンバーをミュート / "
            "DM送信の権限が必要です。"
        ),
        inline=False,
    )

    # どのバージョンの仕様を見ているかが分かるようにする
    misc.set_footer(text=BOT_VERSION)
    return [rate, rank, misc]
