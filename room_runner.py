"""1卓ぶんの人狼ゲーム進行 (RoomRunner)

GameCog (game.py) が全卓を束ね、Discordイベントを各卓へdispatchする。
"""
from __future__ import annotations

import asyncio
import logging
import random
import secrets
from typing import TYPE_CHECKING, Callable, Optional

import discord

from config import (
    Role, Team, Phase, ROLE_TEAM, ROLE_DISTRIBUTION, MAX_PLAYERS,
    DISCORD_MESSAGE_LIMIT,
    PREPARATION_TIME, CHANNEL_DELETE_DELAY, VOTE_TIMEOUT,
    CH_VILLAGE, CH_SPIRIT, CH_LOBBY, VC_GAME,
    RUNOFF_SPEECH_TIME, LAST_WILL_TIME, DISCUSSION_GRACE_TIME,
    MUTE_GRACE_TIME, MUTE_RETRY_DELAY,
    POSTGAME_RECOMMENDATION_TIMEOUT,
    SE_ENABLED,
    ADOPT_EXISTING_LAYOUT,
    PRIVATE_ROOM_CREATOR_ROLE_NAME,
    RATED_ROOM_IDS, RoomDefinition,
)
from models import Player, GameState, by_number
from views import (
    LobbyView, GMPanelEntryView, VoteView, RunoffVoteView,
    WolfVoteView, SeerView, GuardView, SpeechDoneView,
    MorningReadyView, PrepReadyView, PostgameRecommendationView,
    build_vote_result_embed,
)
import database
import rating as rating_lib

if TYPE_CHECKING:
    from discord.ext import commands

    from game import GameCog

log = logging.getLogger(__name__)


class StateDurabilityError(RuntimeError):
    """ゲーム結果をDiscord副作用より先に保存できなかった。

    通常の予期せぬ例外と違い、自動廃村で状態を捨てず、
    GMが状況確認できる安全停止のまま残す。
    """

    def __init__(self, message: str, *, state_committed: bool = False) -> None:
        super().__init__(message)
        self.state_committed = state_committed


ROLE_EMOJI: dict[Role, str] = {
    Role.WEREWOLF: "🐺",
    Role.MADMAN: "🃏",
    Role.SEER: "🔮",
    Role.MEDIUM: "👻",
    Role.GUARD: "🛡️",
    Role.VILLAGER: "👤",
}

# Bot所有のサーバーmuteだけを、Discord側でも識別するマーカー。
# muteとrolesはdiscord.pyのMember.editで同一PATCHに入るため、
# API成功後・DB checkpoint前にプロセスが落ちても曖昧にならない。
MUTE_MARKER_ROLE_PREFIX = "人狼Botミュート:"

# Discordのニックネーム上限
NICK_MAX_LEN = 32

# 死亡者のニックネームへ付ける死因マーカー (接尾辞)
DEATH_NICK_MARKERS: dict[str, str] = {
    "処刑": "(処刑)",
    "襲撃": "(襲撃)",
    "除外": "(除外)",
}
DEATH_NICK_FALLBACK_MARKER = "(死亡)"


def death_nick(display_name: str, method: str) -> str:
    """死亡者のニックネーム「01.名前(処刑)」を組み立てる。

    Discordのボイスチャンネル参加者一覧は表示名の辞書順に並ぶため、
    マーカーを前置すると死亡した人だけが番号順から外れて飛んでしまう。
    番号を先頭に残す接尾辞にすることで、処刑・襲撃されても並び順が動かない。

    末尾から切ると32字上限でマーカーごと消え、生存中のニックネームと
    同一文字列になって死亡が見えなくなる (例外もログも出ない)。
    そのため名前側を先に切り詰めてからマーカーを連結する。
    """
    marker = DEATH_NICK_MARKERS.get(method, DEATH_NICK_FALLBACK_MARKER)
    return f"{display_name[:NICK_MAX_LEN - len(marker)]}{marker}"


def _action_subject_id(
    entry: dict,
    key: str,
    label_to_ids: dict[str, list[int]],
) -> Optional[int]:
    """新形式のIDを優先し、旧スナップショットは一意な表示名だけ補完する。"""
    value = entry.get(f"{key}_id")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    label = entry.get(key)
    candidates = label_to_ids.get(str(label), []) if label else []
    return candidates[0] if len(candidates) == 1 else None


def build_game_stats(
    action_log: list[dict],
    players: list[dict],
    *,
    days: int,
) -> tuple[dict, dict[int, dict]]:
    """進行ログから試合統計とプレイヤーごとの死亡記録を組み立てる。

    Discord表示名は重複し得るため、新規ログのactor_id/target_idを正本にする。
    v0.31から継続中の試合は、表示名が一意な場合だけ旧ログを補完する。
    """
    safe_days = max(0, int(days))
    role_by_id = {int(row["player_id"]): str(row["role"]) for row in players}
    label_to_ids: dict[str, list[int]] = {}
    for row in players:
        label = str(row.get("display_name") or "")
        if label:
            label_to_ids.setdefault(label, []).append(int(row["player_id"]))

    seer_actions: set[tuple] = set()
    guard_success_days: set[int] = set()
    peaceful_days: set[int] = set()
    night_days: set[int] = set()
    attack_death_days: set[int] = set()
    death_events: set[tuple] = set()
    death_records: dict[int, dict] = {}
    executions_total = 0
    executions_wolf = 0
    day1_execution_was_wolf: Optional[int] = None
    night1_kill_had_role: Optional[int] = None
    wolf_death_days: list[int] = []
    guard_id = next(
        (player_id for player_id, role in role_by_id.items() if role == Role.GUARD.value),
        None,
    )
    guard_death: Optional[tuple[int, str, str]] = None

    for entry in action_log:
        if not isinstance(entry, dict):
            continue
        try:
            day = int(entry.get("day", 0))
        except (TypeError, ValueError):
            continue
        kind = str(entry.get("kind") or "")
        detail = str(entry.get("detail") or "")
        if str(entry.get("phase") or "") == Phase.NIGHT.name and day > 0:
            night_days.add(day)

        actor_id = _action_subject_id(entry, "actor", label_to_ids)
        target_id = _action_subject_id(entry, "target", label_to_ids)
        if kind == "占い":
            seer_actions.add((day, actor_id, target_id, detail))
            continue
        if kind == "護衛":
            continue
        if kind == "護衛成功" and day > 0:
            guard_success_days.add(day)
            peaceful_days.add(day)
            continue
        if kind == "平和" and day > 0:
            peaceful_days.add(day)
            continue
        if kind == "襲撃先" and detail == "噛みなし" and day > 0:
            peaceful_days.add(day)
            continue
        if kind != "死亡":
            continue

        cause = detail.split("/", 1)[0].strip()
        if cause not in {"処刑", "襲撃", "除外"}:
            continue
        event_key = (day, cause, target_id, str(entry.get("target") or ""))
        if event_key in death_events:
            continue
        death_events.add(event_key)

        role = role_by_id.get(target_id) if target_id is not None else None
        if role is None and "役職=" in detail:
            role = detail.split("役職=", 1)[1].strip().split()[0]
        if target_id is not None and target_id not in death_records:
            death_records[target_id] = {
                "died_on_day": day,
                "death_cause": cause,
            }
        if target_id == guard_id and guard_death is None:
            guard_death = (day, cause, str(entry.get("phase") or ""))
        if role == Role.WEREWOLF.value:
            wolf_death_days.append(day)

        if cause == "処刑":
            executions_total += 1
            is_wolf = role == Role.WEREWOLF.value
            executions_wolf += int(is_wolf)
            if day == 1 and day1_execution_was_wolf is None:
                day1_execution_was_wolf = int(is_wolf)
        elif cause == "襲撃":
            if day > 0:
                attack_death_days.add(day)
            if day == 1 and night1_kill_had_role is None and role is not None:
                night1_kill_had_role = int(role != Role.VILLAGER.value)

    # 明示的な「平和」は新規ゲームの正本。v0.31から継続中の旧ログは、
    # 夜行動が残る日から襲撃死の日を引いて補完する。
    peaceful_days.update(night_days - attack_death_days)
    seer_wolf_hits = sum(
        1 for _day, _actor, _target, detail in seer_actions
        if "結果=人狼" in detail
    )
    # 「護衛を選んだ回数」ではなく、狩人がその夜を生存状態で迎えた回数を
    # 分母にする。未行動も成功ではない一夜として数える。
    guard_alive_nights = set(night_days)
    if guard_death is not None:
        death_day, death_cause, death_phase = guard_death
        guard_alive_nights = {
            day for day in guard_alive_nights
            if day < death_day
            or (
                day == death_day
                and (
                    death_cause == "襲撃"
                    or (death_cause == "除外" and death_phase == Phase.NIGHT.name)
                )
            )
        }

    initial_wolves = sum(1 for role in role_by_id.values() if role == Role.WEREWOLF.value)
    wolf_alive_by_day = [
        max(0, initial_wolves - sum(1 for death_day in wolf_death_days if death_day < day))
        for day in range(1, safe_days + 1)
    ]
    return (
        {
            "days": safe_days,
            "peaceful_mornings": len({day for day in peaceful_days if 0 < day <= safe_days}),
            "guard_successes": len(guard_success_days),
            "guard_checks": len(guard_alive_nights),
            "seer_checks": len(seer_actions),
            "seer_wolf_hits": seer_wolf_hits,
            "day1_execution_was_wolf": day1_execution_was_wolf,
            "executions_total": executions_total,
            "executions_wolf": executions_wolf,
            "night1_kill_had_role": night1_kill_had_role,
            "wolf_alive_by_day": wolf_alive_by_day,
            "rank_bucket": None,
        },
        death_records,
    )


def build_rank_bucket(
    before_rank_map: Optional[dict[int, rating_lib.RankContext]],
    player_ids: list[int],
) -> Optional[str]:
    """参加者ランクの中央値を返す。偶数時は下位側を採用する。"""
    if not before_rank_map or not player_ids:
        return None
    contexts = [before_rank_map.get(int(player_id)) for player_id in player_ids]
    if any(context is None for context in contexts):
        return None
    try:
        ordered = sorted(
            contexts,
            key=lambda context: rating_lib.rank_order_value(context.rank_name),
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return ordered[(len(ordered) - 1) // 2].rank_name


def member_roles_for_edit(member: discord.Member) -> list[discord.Role]:
    """Member.edit(roles=)用に@everyoneを除いた現在ロールを返す。"""
    result: list[discord.Role] = []
    for role in getattr(member, "roles", []):
        is_default = getattr(role, "is_default", None)
        if callable(is_default) and is_default():
            continue
        result.append(role)
    return result


class RoomRunner:
    def __init__(self, bot: commands.Bot, manager: "GameCog", room_def: RoomDefinition) -> None:
        self.bot = bot
        self.manager = manager
        self.room_def = room_def
        self.action_lock = asyncio.Lock()
        # 再起動復元後のGM「再開」はDiscord側で二重配送・連打され得る。
        # resume_game内部でも直列化し、復元ゲームタスクを二本起動しない。
        self.resume_lock = asyncio.Lock()
        self.state = GameState()
        self.state.room_id = self.room_def.room_id
        self.state.room_name = self.room_def.name
        # 次村用: 直前のゲームの参加者とGM (スナップショットへ永続化)
        self.last_game_roster: list[int] = []
        self.last_game_gm: Optional[int] = None
        # 夜の「朝を迎える」パネルのView (#昼 に1枚。夜ごとに作り直す)
        self._morning_view: Optional[MorningReadyView] = None
        # 役職確認タイムの「役職を確認した」パネルのView (ゲームごとに作り直す)
        self._prep_view: Optional[PrepReadyView] = None
        # その夜に配った役職UI (夜の終わりに全てstopする。
        # 確認UIのキャンセルで作り直したセレクトもここへ足される)
        self._night_views: list[discord.ui.View] = []
        # ゲーム中に作成した全View。強制終了・外部勝敗確定・例外終了でも
        # 古いボタンが次ゲームへ作用しないよう、一元的に停止する。
        self._game_views: list[discord.ui.View] = []

    def is_private_room(self) -> bool:
        return self.room_def.private_owner_id is not None and self.room_def.private_role_name is not None

    def is_rated_room(self) -> bool:
        return self.room_def.room_id in RATED_ROOM_IDS and not self.is_private_room()

    def register_game_view(self, view: discord.ui.View, *, night: bool = False) -> None:
        """ゲーム進行Viewを終了時の一括停止対象へ登録する。"""
        if view not in self._game_views:
            self._game_views.append(view)
        if night and view not in self._night_views:
            self._night_views.append(view)

    def _stop_night_views(self) -> None:
        # 夜UIを畳むときは中継窓も必ず閉じる (異常終了で開いたまま残さない)
        self.state.wolf_relay_window_open = False
        stopped = list(self._night_views)
        for view in stopped:
            try:
                view.stop()
            except Exception:
                pass
        if stopped:
            stopped_ids = {id(view) for view in stopped}
            self._game_views = [
                view for view in self._game_views if id(view) not in stopped_ids
            ]
        self._night_views.clear()

    def _stop_all_game_views(self) -> None:
        for view in list(self._game_views):
            try:
                view.stop()
            except Exception:
                pass
        self._game_views.clear()
        self._night_views.clear()

    def is_current_game_view(self, game_run_id: str) -> bool:
        state = self.state
        return (
            bool(game_run_id)
            and state.game_run_id == game_run_id
            and state.phase != Phase.GAME_OVER
            and not state.ending
            and state.pending_winner is None
        )

    def is_current_day_view(self, game_run_id: str, day_generation: int) -> bool:
        return (
            self.is_current_game_view(game_run_id)
            and self.state.day_generation == day_generation
        )

    def is_current_night_view(self, game_run_id: str, night_generation: int) -> bool:
        return (
            self.is_current_game_view(game_run_id)
            and self.state.night_generation == night_generation
            and self.night_actions_open()
        )

    async def _discord_api_call(self, func, *args, **kwargs):
        async with self.manager.discord_api_sem:
            return await func(*args, **kwargs)

    async def _paced_discord_api_call(self, func, *args, **kwargs):
        """高負荷な連続変更を全卓共通で間隔制御し、Semaphore内で呼ぶ。"""
        return await self.manager.paced_discord_api_call(func, *args, **kwargs)

    def can_manage_private_room(self, member: discord.Member) -> bool:
        if not self.is_private_room():
            return False
        if member.id != self.room_def.private_owner_id:
            return False
        return any(role.name == PRIVATE_ROOM_CREATOR_ROLE_NAME for role in getattr(member, "roles", []))

    # ============================================================
    # セットアップ
    # ============================================================

    async def setup_channels(
        self,
        guild: discord.Guild,
        snapshot: Optional[dict] = None,
        stats_channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """起動時にロビーとVCを確認/作成"""
        self.state.guild = guild
        self.state.stats_channel = stats_channel

        # 許可ロールのtypo/改名をwarningだけで流すと、@everyone拒否と
        # 古いallow除去だけが適用されて卓が見えなくなる。変更前に止める。
        self.manager._validate_room_access_roles(guild, self.room_def)

        if self.is_private_room():
            role = await self.manager._ensure_private_room_role(guild, self.room_def)
            if role is None:
                raise RuntimeError(
                    f"専用村ロールを作成または取得できません: {self.room_def.private_role_name}"
                )
            await self.manager._sync_private_room_member_roles(guild, self.room_def)

        channel_ids = snapshot.get("channel_ids", {}) if snapshot else {}

        # 保存済みIDを所有境界の正本にする。新規導入先の同名カテゴリは、
        # 明示採用なしに権限変更・残骸削除の対象へしない。
        saved_category_id = channel_ids.get("category")
        category = next(
            (item for item in guild.categories if item.id == saved_category_id),
            None,
        )
        if category is None:
            named_category = discord.utils.get(guild.categories, name=self.room_def.name)
            if named_category is not None and snapshot is None and not ADOPT_EXISTING_LAYOUT:
                raise RuntimeError(
                    f"同名カテゴリ「{self.room_def.name}」が既にあります。"
                    "無関係な構成を変更しないため自動採用しません。"
                    "既存構成をこのBotへ明示的に引き継ぐ場合だけ "
                    "ADOPT_EXISTING_LAYOUT=1 を設定してください"
                )
            category = named_category
        if category is None:
            # 作成後に閲覧拒否を付けると、Discord API応答間だけ
            # 新規カテゴリが公開になる。作成リクエスト自体に完成形の
            # overwriteを含め、最初からfail-closedにする。
            category = await guild.create_category(
                self.room_def.name,
                overwrites=self.manager._build_room_overwrites(guild, self.room_def),
            )
        self.state.category = category
        await self.manager._apply_room_visibility(guild, category, self.room_def)

        # 起動時の孤立 #昼 / #霊界 チャンネル削除 (同名の重複残骸も全て掃除する)
        # (前回起動でクラッシュ等によりゲーム途中終了した場合、残骸が残っている可能性)
        active_snapshot = snapshot is not None and snapshot.get("phase") not in (Phase.LOBBY.name, Phase.GAME_OVER.name)
        if not active_snapshot:
            orphans = [
                ch for ch in guild.text_channels
                if ch.category is not None and ch.category.id == category.id
                and ch.name in (CH_VILLAGE, CH_SPIRIT)
            ]
            for orphan in orphans:
                try:
                    await orphan.delete(reason="起動時クリーンアップ: 前回ゲームの残骸")
                    log.info(f"起動時に孤立した #{orphan.name} チャンネルを削除しました")
                except (discord.Forbidden, discord.HTTPException) as e:
                    raise RuntimeError(
                        f"機密情報を含む可能性がある孤立 #{orphan.name} を削除できません"
                    ) from e

        # 作成順 = 表示順: 参加受付 → 人狼ゲーム(VC)
        saved_lobby_id = channel_ids.get("lobby")
        lobby = next(
            (item for item in guild.text_channels if item.id == saved_lobby_id),
            None,
        )
        if lobby is None:
            lobby = discord.utils.get(guild.text_channels, name=CH_LOBBY, category=category)
        if lobby is None:
            lobby = await guild.create_text_channel(CH_LOBBY, category=category)
        self.state.lobby_channel = lobby

        saved_voice_id = channel_ids.get("voice")
        vc = next(
            (item for item in guild.voice_channels if item.id == saved_voice_id),
            None,
        )
        if vc is None:
            vc = discord.utils.get(guild.voice_channels, name=VC_GAME, category=category)
        if vc is None:
            vc = await guild.create_voice_channel(VC_GAME, category=category)
        self.state.voice_channel = vc

        # 前回クラッシュ等で残ったVCのメンバー個別上書き (GM/弁明者のspeak許可)
        # を掃除する。残すと次ゲームの夜に該当者だけ発言できてしまう。
        # (@everyoneのspeak残留は上の _apply_room_visibility の上書きで消えている)
        if not active_snapshot:
            for target in list(vc.overwrites):
                if not isinstance(target, discord.Member):
                    continue
                try:
                    await self._paced_discord_api_call(
                        vc.set_permissions, target, overwrite=None,
                        reason="人狼: 起動時クリーンアップ (VC個別権限残骸)",
                    )
                    log.info(f"起動時にVC個別権限の残骸を削除しました ({target.display_name})")
                except (discord.Forbidden, discord.HTTPException) as e:
                    raise RuntimeError(
                        f"VC個別権限残骸を削除できません ({target.display_name})"
                    ) from e

            # 前回クラッシュ等で残った「Bot自身の」サーバーミュートだけを掃除する。
            # VC内の全ミュートを解除すると、モデレーターが手動でミュートした人まで
            # 発言可能にしてしまうため、スナップショットの所有記録を必ず照合する。
            bot_muted_ids = self._resolve_nonactive_owned_mutes(
                snapshot or {}, list(getattr(guild, "members", [])),
                marker_role_name=self._mute_marker_role_name(),
            )
            marker = discord.utils.get(guild.roles, name=self._mute_marker_role_name())
            cleanup_targets, pending_unmutes = self._partition_nonactive_owned_mutes(
                guild, bot_muted_ids
            )
            for member in cleanup_targets:
                voice = member.voice
                edit_kwargs: dict = {}
                if voice.mute:
                    edit_kwargs["mute"] = False
                if marker is not None and any(
                    getattr(role, "id", None) == marker.id
                    for role in getattr(member, "roles", [])
                ):
                    edit_kwargs["roles"] = self._roles_with_mute_marker(
                        member, marker, present=False
                    )
                if not edit_kwargs:
                    continue
                try:
                    await self._paced_discord_api_call(
                        member.edit, **edit_kwargs,
                        reason="人狼: 起動時クリーンアップ (残留ミュート解除)",
                    )
                    log.info(f"起動時に残留サーバーミュートを解除しました ({member.display_name})")
                except (discord.Forbidden, discord.HTTPException) as e:
                    raise RuntimeError(
                        f"Bot所有の残留ミュートを解除できません ({member.display_name})"
                    ) from e
            if pending_unmutes:
                await self.manager.register_pending_unmutes(guild, pending_unmutes)

        # 進行中snapshotは、この時点ではまだ空のLOBBY GameStateである。
        # ここで操作可能なUIを出すと、復元前の参加/GM操作が進行中snapshotを
        # 空ロビーで上書きし得る。active時はrestore完了後だけ投稿する。
        if not active_snapshot:
            await self._post_lobby_ui()

    @staticmethod
    def _resolve_nonactive_owned_mutes(
        snapshot: dict,
        members: list,
        *,
        marker_role_name: Optional[str] = None,
    ) -> set[int]:
        """非active復元で解除してよいBot所有muteだけを返す。"""
        owned = set(snapshot.get("bot_muted_ids", []))
        if snapshot.get("mute_marker_enabled") and marker_role_name:
            member_by_id = {member.id: member for member in members}
            marker_holders = {
                member.id for member in members
                if any(
                    getattr(role, "name", None) == marker_role_name
                    for role in getattr(member, "roles", [])
                )
            }
            # メンバーを取得できる限りDiscord側マーカーを正とする。
            # キャッシュ不在IDは安全側でpendingに持ち越す。
            owned = {
                member_id for member_id in owned
                if member_id not in member_by_id
            }
            owned.update(marker_holders)
        intent_ids = set(snapshot.get("bot_mute_intent_ids", []))
        expected_nicks = {
            int(row["user_id"]): f"{int(row.get('number', 0)):02d}.{row.get('base_name') or row.get('original_nickname') or ''}"[:32]
            for row in snapshot.get("players", [])
            if row.get("user_id") is not None and row.get("number")
        }
        for member in members:
            voice = getattr(member, "voice", None)
            if member.id not in intent_ids or voice is None or not voice.mute:
                continue
            if snapshot.get("mute_marker_enabled"):
                if marker_role_name and any(
                    getattr(role, "name", None) == marker_role_name
                    for role in getattr(member, "roles", [])
                ):
                    owned.add(member.id)
                else:
                    log.warning(f"非active復元時の手動mute候補を保護: {member.display_name}")
                continue
            expected = expected_nicks.get(member.id, "観戦者")
            if member.nick == expected:
                # nick+mute同一PATCHの成功が確認できる場合だけ
                # Bot所有として残留muteを解除する。
                owned.add(member.id)
            else:
                log.warning(f"非active復元時の手動mute候補を保護: {member.display_name}")
        return owned

    def _partition_nonactive_owned_mutes(
        self, guild: discord.Guild, owned_ids: set[int]
    ) -> tuple[list[discord.Member], set[int]]:
        """非active卓の所有muteを「今解除」と「pending」に分ける。"""
        immediate: list[discord.Member] = []
        pending: set[int] = set()
        for member_id in owned_ids:
            member = guild.get_member(member_id)
            if member is None:
                pending.add(member_id)
                continue
            if member.bot:
                continue
            voice = member.voice
            if voice is None or voice.channel is None:
                pending.add(member_id)
                continue
            if self.manager.is_other_active_game_vc(
                voice.channel.id, exclude_room_id=self.state.room_id
            ):
                pending.add(member_id)
                continue
            # 自卓VCに限らず、通常の別VCに移動済みでも即回収する。
            immediate.append(member)
        return immediate, pending

    async def _purge_bot_messages(self, ch: discord.TextChannel, label: str) -> None:
        """チャンネル内のBot投稿を最大50件まで削除"""
        try:
            await ch.purge(limit=50, check=lambda m: m.author == self.bot.user)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"{label}メッセージ削除失敗: {e}")

    def log_action(self, kind: str, actor: Optional[object] = None,
                   target: Optional[object] = None, detail: str = "") -> None:
        """進行ログへ1件積む (終了時に #昼 へ貼る)。

        二重実行などの不具合を後から追えるよう、**拒否された操作も残す**。
        Player でも discord.Member でも表示名を引けるようにしておく。
        """
        state = self.state

        def label(who: Optional[object]) -> str:
            if who is None:
                return ""
            name = getattr(who, "display_name", None)
            uid = getattr(who, "user_id", None) or getattr(who, "id", None)
            return f"{name}" if name else f"ID:{uid}"

        def subject_id(who: Optional[object]) -> Optional[int]:
            if who is None:
                return None
            value = getattr(who, "user_id", None) or getattr(who, "id", None)
            return int(value) if isinstance(value, int) and value > 0 else None

        state.action_log.append({
            "day": state.day_number,
            "phase": (self._effective_phase() or state.phase).name,
            "kind": kind,
            "actor": label(actor),
            "target": label(target),
            # 表示名は重複・改名があり得るため、統計集計はIDを正本にする。
            "actor_id": subject_id(actor),
            "target_id": subject_id(target),
            "detail": detail,
        })

    def build_action_log_text(self) -> str:
        """進行ログを日ごとに整形する (#昼 への掲示用)。

        「占い師が2回実行した」などの不具合を、その場で疑いを晴らせる粒度で出す。
        拒否された操作も残すので、ガードが効いていることも読み取れる。
        """
        state = self.state
        if not state.action_log:
            return "（記録された行動がありません）"

        lines: list[str] = []
        current_day: Optional[int] = None
        for entry in state.action_log:
            day = entry.get("day")
            if day != current_day:
                current_day = day
                lines.append(f"\n**{day}日目**")
            parts = [entry.get("kind", "")]
            if entry.get("actor"):
                parts.append(entry["actor"])
            if entry.get("target"):
                parts.append(f"→ {entry['target']}")
            if entry.get("detail"):
                parts.append(f"({entry['detail']})")
            lines.append("・" + " ".join(p for p in parts if p))
        return "\n".join(lines).strip()

    async def _post_action_log(self) -> None:
        """終了時に進行ログを #昼 へ貼る (結果発表の直前)。

        Discordの2000字上限を超える場合はテキストファイルとして添付する。
        """
        ch = self.state.village_channel
        if ch is None:
            return
        body = self.build_action_log_text()
        header = "📋 **進行ログ（全役職の行動・投票）**"
        try:
            if len(body) + len(header) + 2 <= 1990:
                await ch.send(f"{header}\n{body}")
            else:
                import io
                plain = body.replace("**", "")
                await ch.send(
                    f"{header}\n（長いためファイルにしました）",
                    file=discord.File(
                        io.BytesIO(plain.encode("utf-8")),
                        filename=f"{self.state.room_id}_進行ログ.txt",
                    ),
                )
        except Exception as e:
            # 進行ログは調査用の付加機能。ここで例外を通すと _end_game が
            # 落ちてゲームループが廃村扱いになるため、どんな失敗でも握る。
            log.warning(f"進行ログの掲示に失敗 ({self.state.room_name}): {e}")

    def _play_se(self, scene: str) -> None:
        """シーン切替SEをバックグラウンド再生する (失敗してもゲーム進行に影響しない)"""
        if not SE_ENABLED:
            return
        vc = self.state.voice_channel
        if vc is None:
            return
        self.manager.spawn_bg_task(self.manager.sound_player.play(vc, scene))

    async def _repost_gm_panel(self) -> bool:
        """GMメニュー入口を #昼 の末尾へ再掲示する (古いパネルは削除)。

        公開面は入口1個だけにし、状況と操作ボタンはGM本人へephemeralで返す。
        フェーズ切替、とくに書き込みが止まる夜に再掲示して見失いにくくする。
        """
        state = self.state
        ch = state.village_channel
        if ch is None:
            return False
        old_msg = state.gm_panel_message
        old_view = state.gm_panel_view
        gm_view = GMPanelEntryView(self)
        try:
            new_msg = await ch.send(
                "🎮 **GMコントロール**（GMのみ操作できます）",
                view=gm_view,
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"GMパネル投稿失敗 ({state.room_name}): {e}")
            gm_view.stop()
            return False
        state.gm_panel_message = new_msg
        state.gm_panel_view = gm_view
        if old_view is not None:
            try:
                old_view.stop()
            except Exception:
                pass
            if old_view in self._game_views:
                self._game_views.remove(old_view)
        if old_msg is not None:
            try:
                await self._discord_api_call(old_msg.delete)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"旧GMパネル削除失敗 ({state.room_name}): {e}")
        return True

    async def _post_lobby_ui(self) -> None:
        ch = self.state.lobby_channel
        await self._purge_bot_messages(ch, "ロビー")

        view = LobbyView(self)
        embed = view._build_embed()
        self.state.lobby_message = await ch.send(embed=embed, view=view)

    def _build_room_snapshot(self) -> dict:
        state = self.state
        return {
            "room_name": state.room_name,
            "day_number": state.day_number,
            "gm_id": state.gm_id,
            "game_run_id": state.game_run_id,
            "recruitment_id": state.recruitment_id,
            "day_generation": state.day_generation,
            "night_generation": state.night_generation,
            "day_execution_resolved": state.day_execution_resolved,
            "day_executed_target": state.day_executed_target,
            "pending_execution_target": state.pending_execution_target,
            "night_resolved": state.night_resolved,
            "pending_winner": state.pending_winner.name if state.pending_winner else None,
            "ending": state.ending,
            "preparation_dm_sent_ids": list(state.preparation_dm_sent_ids),
            "initial_seer_target": state.initial_seer_target,
            "initial_seer_result_sent": state.initial_seer_result_sent,
            "pending_death_effects": [dict(item) for item in state.pending_death_effects],
            "day1_executed_id": state.day1_executed_id,
            "night1_killed_id": state.night1_killed_id,
            "recovery_phase": (
                state.recovery_phase.name
                if state.recovery_phase is not None
                else state.phase_before_pause.name if state.phase_before_pause is not None else None
            ),
            "players": [
                {
                    "user_id": p.user_id,
                    "role": p.role.name if p.role else None,
                    "alive": p.alive,
                    "number": p.number,
                    "original_nickname": p.original_nickname,
                    "base_name": p.base_name,
                }
                for p in state.players.values()
            ],
            "original_nicknames": [
                {"user_id": user_id, "nickname": nickname}
                for user_id, nickname in state.original_nicknames.items()
            ],
            "guard_previous": state.guard_previous,
            "votes": [
                {"voter_id": voter_id, "target_id": target_id}
                for voter_id, target_id in state.votes.items()
            ],
            "runoff_candidates": list(state.runoff_candidates),
            "wolf_target": state.wolf_target,
            "wolf_voters": [
                {"user_id": user_id, "target_id": target_id}
                for user_id, target_id in state.wolf_voters.items()
            ],
            "seer_target": state.seer_target,
            "guard_target": state.guard_target,
            "action_log": state.action_log,
            "morning_ready_ids": list(state.morning_ready_ids),
            "morning_warned_ids": list(state.morning_warned_ids),
            "morning_confirmed": state.morning_confirmed,
            "morning_panel_message_id": state.morning_panel_message_id,
            "prep_ready_ids": list(state.prep_ready_ids),
            "prep_confirmed": state.prep_confirmed,
            "disconnected_players": list(state.disconnected_players),
            "bot_muted_ids": list(state.bot_muted_ids),
            "bot_mute_intent_ids": list(state.bot_mute_intent_ids),
            "mute_marker_enabled": state.mute_marker_enabled,
            "last_game_roster": self.last_game_roster,
            "last_game_gm": self.last_game_gm,
            "last_executed": state._last_executed.user_id if state._last_executed else None,
            "last_killed": state._last_killed.user_id if state._last_killed else None,
            "last_guarded": state._last_guarded,
            "channel_ids": {
                "category": state.category.id if state.category else None,
                "lobby": state.lobby_channel.id if state.lobby_channel else None,
                "stats": state.stats_channel.id if state.stats_channel else None,
                "voice": state.voice_channel.id if state.voice_channel else None,
                "village": state.village_channel.id if state.village_channel else None,
                "spirit": state.spirit_channel.id if state.spirit_channel else None,
            },
        }

    async def _persist_room_state(self) -> None:
        state = self.state
        if state.guild is None:
            return
        await database.save_room_state(
            state.guild.id,
            state.room_id,
            state.phase.name,
            self._build_room_snapshot(),
        )

    async def _fetch_member_for_restore(self, user_id: int) -> Optional[discord.Member]:
        """復元時、メンバーキャッシュに載っていない相手をAPIへ確認する。

        起動直後のchunking未完了とサーバー退出を区別するために使う。
        NotFound だけが「本当に退出済み」で、それ以外の失敗は不明として
        Noneを返す (従来どおり不在扱いになるが、ログで切り分けられる)。
        """
        guild = self.state.guild
        fetch = getattr(guild, "fetch_member", None)
        if guild is None or not callable(fetch):
            return None
        try:
            return await self._discord_api_call(fetch, user_id)
        except discord.NotFound:
            return None
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(
                "復元時のメンバー照会に失敗 (%s / user=%s): %s",
                self.state.room_name, user_id, e,
            )
            return None

    async def restore_from_snapshot(self, payload: Optional[dict]) -> None:
        state = self.state
        if payload is not None:
            # 次村用の直前ゲーム記録 (ロビー復帰でも保持する)
            self.last_game_roster = payload.get("last_game_roster", [])
            self.last_game_gm = payload.get("last_game_gm")
        if payload is None:
            # 新規/スナップショット無し: 前回の残骸ロールがあれば掃除
            await self._delete_alive_role()
            await self._persist_room_state()
            return

        state.day_number = payload.get("day_number", 0)
        state.gm_id = payload.get("gm_id")
        state.game_run_id = payload.get("game_run_id") or secrets.token_hex(16)
        state.recruitment_id = payload.get("recruitment_id")
        state.day_generation = int(payload.get("day_generation", state.day_number or 0))
        state.night_generation = int(payload.get("night_generation", 0))
        state.day_execution_resolved = bool(payload.get("day_execution_resolved", False))
        state.day_executed_target = payload.get("day_executed_target")
        state.pending_execution_target = payload.get("pending_execution_target")
        state.night_resolved = bool(payload.get("night_resolved", False))
        pending_winner_name = payload.get("pending_winner")
        state.pending_winner = Team[pending_winner_name] if pending_winner_name else None
        # 処理中にプロセスが落ちた場合は、新しいプロセスが
        # GM再試行を担うため実行中ロック自体は解除する。
        state.ending = False
        state.preparation_dm_sent_ids = set(payload.get("preparation_dm_sent_ids", []))
        state.initial_seer_target = payload.get("initial_seer_target")
        state.initial_seer_result_sent = bool(payload.get("initial_seer_result_sent", False))
        state.pending_death_effects = [
            dict(item) for item in payload.get("pending_death_effects", [])
            if isinstance(item, dict)
        ]
        state.day1_executed_id = payload.get("day1_executed_id")
        state.night1_killed_id = payload.get("night1_killed_id")
        state.players.clear()
        state.original_nicknames = {
            row["user_id"]: row.get("nickname")
            for row in payload.get("original_nicknames", [])
        }

        dropped_players: list[str] = []
        for row in payload.get("players", []):
            member = state.guild.get_member(row["user_id"]) if state.guild else None
            if member is None and state.guild is not None:
                # 起動直後はメンバーキャッシュのchunkingが間に合わないことがある。
                # discord.pyは最大 max(5秒, 人数/10000) 待つだけで、揃わなくても
                # 警告ログを出して ready を発火する (ConnectionState._delay_ready)。
                # ここでキャッシュだけを信じると、在籍している進行中ゲームの
                # 参加者を「不在」と誤判定して投票権ごと消してしまうため、
                # APIへ直接問い合わせて確かめる (通常は get_member で足り、
                # この経路は走らない)
                member = await self._fetch_member_for_restore(row["user_id"])
            if member is None:
                # サーバー不在者は復元できない (Memberオブジェクトを作れない)。
                # 黙って消すと人数が変わったことに誰も気付けないため後で告知する
                fallback_name = row.get("base_name") or f"ID:{row['user_id']}"
                dropped_players.append(f"{row.get('number', '?')}.{fallback_name}")
                continue
            player = Player(
                user_id=row["user_id"],
                member=member,
                role=Role[row["role"]] if row.get("role") else None,
                alive=row.get("alive", True),
                number=row.get("number", 0),
                original_nickname=row.get("original_nickname"),
                base_name=row.get("base_name"),
            )
            state.players[player.user_id] = player

        channel_ids = payload.get("channel_ids", {})
        if state.guild:
            village_id = channel_ids.get("village")
            if village_id:
                village = state.guild.get_channel(village_id)
                if isinstance(village, discord.TextChannel):
                    state.village_channel = village
            spirit_id = channel_ids.get("spirit")
            if spirit_id:
                spirit = state.guild.get_channel(spirit_id)
                if isinstance(spirit, discord.TextChannel):
                    state.spirit_channel = spirit

        saved_phase_name = payload.get("phase", Phase.LOBBY.name)
        recovery_phase_name = payload.get("recovery_phase")
        if saved_phase_name == Phase.PAUSED.name and recovery_phase_name:
            saved_phase_name = recovery_phase_name
        saved_phase = Phase[saved_phase_name]
        if saved_phase in (Phase.LOBBY, Phase.GAME_OVER):
            state.phase = Phase.LOBBY
            state.recovery_phase = None
            state.recovered_from_restart = False
            # ロビー復帰時は前ゲームの一時ロールを掃除
            await self._delete_alive_role()
            await self._post_lobby_ui()
            await self._persist_room_state()
            return

        last_executed_id = payload.get("last_executed")
        last_killed_id = payload.get("last_killed")
        state._last_executed = state.get_player(last_executed_id) if last_executed_id else None
        state._last_killed = state.get_player(last_killed_id) if last_killed_id else None
        state._last_guarded = payload.get("last_guarded", False)
        state.guard_previous = payload.get("guard_previous")
        state.votes = {
            int(row["voter_id"]): int(row["target_id"])
            for row in payload.get("votes", [])
            if row.get("voter_id") is not None and row.get("target_id") is not None
        }
        state.runoff_candidates = [int(pid) for pid in payload.get("runoff_candidates", [])]
        state.wolf_target = payload.get("wolf_target")
        state.wolf_voters = {
            int(row["user_id"]): int(row["target_id"])
            for row in payload.get("wolf_voters", [])
            if row.get("user_id") is not None and row.get("target_id") is not None
        }
        state.seer_target = payload.get("seer_target")
        state.guard_target = payload.get("guard_target")
        state.action_log = list(payload.get("action_log", []))
        state.morning_ready_ids = set(payload.get("morning_ready_ids", []))
        state.morning_warned_ids = set(payload.get("morning_warned_ids", []))
        state.morning_confirmed = bool(payload.get("morning_confirmed", False))
        state.morning_panel_message_id = payload.get("morning_panel_message_id")
        state.prep_ready_ids = set(payload.get("prep_ready_ids", []))
        state.prep_confirmed = bool(payload.get("prep_confirmed", False))
        state.disconnected_players = set(payload.get("disconnected_players", []))
        state.bot_muted_ids = set(payload.get("bot_muted_ids", []))
        state.bot_mute_intent_ids = set(payload.get("bot_mute_intent_ids", []))
        state.mute_marker_enabled = bool(payload.get("mute_marker_enabled", False))
        # 復元時にサーバー不在で落としたプレイヤーへの票を
        # 残すと、集計結果が存在しない処刑対象になる。生存中の
        # 復元プレイヤー同士の票だけを保持する。
        alive_ids = {p.user_id for p in state.alive_players()}
        state.votes = {
            voter_id: target_id
            for voter_id, target_id in state.votes.items()
            if voter_id in alive_ids and target_id in alive_ids
        }
        state.runoff_candidates = [
            player_id for player_id in state.runoff_candidates if player_id in alive_ids
        ]
        vc = state.voice_channel
        if vc is not None:
            for player in state.alive_players():
                vs = getattr(player.member, "voice", None)
                if vs is None or vs.channel is None or vs.channel.id != vc.id:
                    state.disconnected_players.add(player.user_id)

        state.phase = Phase.PAUSED
        state.phase_before_pause = saved_phase
        state.recovery_phase = saved_phase
        state.recovered_from_restart = True
        state.paused = True
        state.pause_event.clear()
        await self._enable_mute_markers()
        await self._reconcile_mute_marker_ownership()
        await self._reconcile_mute_intents()
        if state.morning_confirmed:
            state.morning_ready_event.set()
        if state.prep_confirmed:
            state.prep_ready_event.set()

        if (state.village_channel is None or state.spirit_channel is None) and state.guild is not None:
            try:
                await self._create_game_channels(state.guild)
            except Exception as e:
                log.warning(f"復元用ゲームチャンネル作成失敗 ({state.room_name}): {e}")

        await self._reconcile_pending_death_effects()

        # 勝敗確定後の保存失敗スナップショットは、通常ゲームとして
        # 復元しない。GMが退出済みでも、保存済みのwinner/run_idで
        # 終了精算を自動再試行し、進行デッドロックを作らない。
        if state.pending_winner is not None:
            winner = state.pending_winner
            state.recovered_from_restart = False
            state.recovery_phase = None
            await self._safe_village_send(
                "♻️ **保存途中だったゲーム結果の精算を自動再試行します。**"
            )
            await self._end_game(winner)
            return

        gm_member = state.guild.get_member(state.gm_id) if state.guild and state.gm_id else None
        if gm_member is None or not state.players or not state.alive_players():
            reason = (
                "復元時にGMがサーバーに存在しないためゲームを中断します。"
                if gm_member is None
                else "復元可能な生存プレイヤーがいないためゲームを中断します。"
            )
            await self._safe_village_send(f"⚠️ {reason}")
            await self.force_end(reason)
            return

        # 復元: 一時「生存」ロールを再付与 (再開時に発言制御が効くように)
        # #霊界 の生存者ブロックも現在の生死に合わせて再適用する
        await self._assign_alive_role()
        await self._restrict_vc_for_game()
        await self._grant_alive_vc_access()
        await self._apply_spirit_blocks()

        await self._post_lobby_ui()
        if state.village_channel:
            restore_text = (
                f"♻️ **{state.room_name}** の進行中ゲームを復元しました。\n"
                "現在は一時停止中です。GMがこのチャンネルの「GMメニュー・状況」から「再開」を押すと、"
                "現在のフェーズを最初から再開します。"
            )
            if dropped_players:
                restore_text += (
                    f"\n⚠️ サーバー不在のため復元できなかった参加者: {', '.join(dropped_players)}\n"
                    "(生存者数が変わっているため、続行するかはGMが判断してください)"
                )
            await self._safe_village_send(restore_text)
        # 復元案内より後に置き、GM入口を #昼 の末尾にする。
        if not await self._repost_gm_panel():
            await self._safe_village_send(
                "⚠️ GMコントロールパネルを表示できないため、安全のためゲームを中断します。"
            )
            await self.force_end("GMコントロールパネルを表示できないため中断")
            return
        await self._persist_room_state()

    async def get_join_rank_info(self, user_id: int) -> dict:
        if self.state.guild is None:
            return {"rank_name": "ブロンズ", "provisional": True}
        info = await database.get_player_current_rank_info(user_id, self.state.guild.id)
        if info is None:
            return {"rank_name": "ブロンズ", "provisional": True}
        return info

    async def validate_join(self, member: discord.Member) -> Optional[str]:
        # サーバーオーナーと管理者権限持ちはプレイヤー参加不可。
        # 両者ともチャンネル権限上書きを無視するため、
        #   - 生存中でも #霊界 (死亡者の会話) が見えてしまう
        # さらにオーナーはbotから一切編集できない (常に403) ため、
        #   - 番号付きニックネームが付かない
        #   - 生存ロールが付かず、夜でも #昼 に発言できる
        # という状態になる。いずれも例外は握り潰されるため無言で壊れる。
        # GMはこれらの操作対象外なので、GMとしてなら問題なく参加できる。
        if self.state.guild is not None and member.id == self.state.guild.owner_id:
            return "サーバーオーナーはプレイヤー参加できません (GMとしてなら参加できます)。"
        if member.guild_permissions.administrator:
            return "管理者権限を持つメンバーはプレイヤー参加できません (GMとしてなら参加できます)。"

        other_room = self.manager.find_user_room(member.id, exclude_room_id=self.state.room_id)
        if other_room is not None:
            return f"既に **{other_room.state.room_name}** に参加またはGM登録しています。"

        if self.is_private_room():
            member_role_names = {role.name for role in member.roles}
            if self.room_def.private_role_name not in member_role_names:
                return "この専用村に参加する権限がありません。村主に招待してもらってください。"

        access_role_names = frozenset(
            getattr(self.room_def, "access_role_names", None) or ()
        )
        if (
            access_role_names
            and not member.guild_permissions.manage_guild
            and not access_role_names.intersection(role.name for role in member.roles)
        ):
            allowed = " / ".join(sorted(access_role_names))
            return f"この卓への参加には **{allowed}** のいずれかのロールが必要です。"

        if self.room_def.allowed_ranks is not None:
            info = await self.get_join_rank_info(member.id)
            rank_name = info["rank_name"]
            if rank_name not in self.room_def.allowed_ranks:
                allowed = " / ".join(
                    sorted(self.room_def.allowed_ranks, key=rating_lib.rank_order_value)
                )
                return (
                    f"この卓は **{allowed}** 向けです。\n"
                    f"現在ランク: **{rank_name}**"
                )
        return None

    async def validate_gm_claim(self, member: discord.Member) -> Optional[str]:
        other_room = self.manager.find_user_room(member.id, exclude_room_id=self.state.room_id)
        if other_room is not None:
            return f"既に **{other_room.state.room_name}** に参加またはGM登録しています。"
        member_role_names = {role.name for role in member.roles}
        if self.is_private_room():
            if member.id != self.room_def.private_owner_id:
                return "この専用村では村主だけがGM取得できます。"
            if PRIVATE_ROOM_CREATOR_ROLE_NAME not in member_role_names:
                return f"専用村のGM取得には **{PRIVATE_ROOM_CREATOR_ROLE_NAME}** ロールが必要です。"
            return None
        if self.room_def.allowed_gm_user_ids and member.id not in self.room_def.allowed_gm_user_ids:
            return "この卓のGMは指定ユーザー専用です。"
        if self.room_def.owner_only_gm and self.state.guild and member.id != self.state.guild.owner_id:
            return "この卓のGMはサーバーオーナー専用です。"
        if self.room_def.allowed_ranks is not None:
            info = await self.get_join_rank_info(member.id)
            rank_name = info["rank_name"]
            if rank_name not in self.room_def.allowed_ranks:
                allowed = " / ".join(
                    sorted(self.room_def.allowed_ranks, key=rating_lib.rank_order_value)
                )
                return (
                    f"この卓でGM取得できるのは **{allowed}** の参加可能者だけです。\n"
                    f"現在ランク: **{rank_name}**"
                )
        return None

    # ============================================================
    # 次村 (直前のゲームと同じメンバーを一括再登録)
    # ============================================================

    async def rematch(self, user: discord.Member) -> str:
        """直前のゲームの参加者とGMをロビーへ一括再登録する (次村)。

        押せるのは直前のゲームのGMのみ (進行の主導権をGMに一本化する)。
        既に参加済みの人はそのまま、サーバー不在や参加条件を
        満たさなくなった人 (ランク変動など) はスキップして名前を報告する。
        抜けたい人は「参加取消」、GMは「参加者を除外」で外せる。
        """
        state = self.state
        if state.phase != Phase.LOBBY:
            return "ゲーム進行中は次村を組めません。"
        if not self.last_game_roster:
            return "直前のゲーム記録がありません。"
        if user.id != self.last_game_gm:
            return "次村を組めるのは直前のゲームのGMだけです。"
        guild = state.guild
        if guild is None:
            return "サーバー情報を取得できないため次村を組めません。"

        added: list[str] = []
        skipped: list[str] = []

        # GMの再登録 (空席の場合のみ)
        if state.gm_id is None and self.last_game_gm is not None:
            gm_member = guild.get_member(self.last_game_gm)
            if gm_member is None:
                skipped.append("前回GM (サーバー不在)")
            elif await self.validate_gm_claim(gm_member) is None:
                state.gm_id = gm_member.id
            else:
                skipped.append(f"前回GM: {gm_member.display_name}")

        # 参加条件を満たす候補を先に絞る
        # (GMを兼ねている人も参加者として登録する: 同じ卓での兼任は許可されている)
        candidates: list[discord.Member] = []
        for uid in self.last_game_roster:
            if uid in state.players:
                continue
            if len(state.players) + len(candidates) >= MAX_PLAYERS:
                skipped.append("(定員に達したため以降を打ち切り)")
                break
            member = guild.get_member(uid)
            if member is None:
                skipped.append(f"ID:{uid} (サーバー不在)")
                continue
            if await self.validate_join(member) is not None:
                skipped.append(member.display_name)
                continue
            candidates.append(member)

        # DM開放チェック (通常の「参加」と同じ条件。ここで弾かないと
        # ゲーム開始時の役職DM送信で失敗して中断になる)
        dm_failed: set[int] = set()

        async def dm_check(member: discord.Member) -> None:
            try:
                await self._discord_api_call(
                    member.send, "人狼ゲームへの次村参加を受け付けました。"
                )
            except (discord.Forbidden, discord.HTTPException):
                dm_failed.add(member.id)

        if candidates:
            await asyncio.gather(*(dm_check(m) for m in candidates))

        for member in candidates:
            if member.id in dm_failed:
                skipped.append(f"{member.display_name} (DM不可)")
                continue
            state.players[member.id] = Player(
                user_id=member.id,
                member=member,
                original_nickname=member.nick,
            )
            added.append(member.display_name)

        await self._persist_room_state()

        # ロビーチャンネルにも告知 (本人以外にも見えるように)
        if state.lobby_channel is not None:
            try:
                await state.lobby_channel.send(
                    f"🔁 **次村**: GM {user.display_name} が直前のメンバー {len(added)}人を再登録しました。\n"
                    "抜ける人は「参加取消」を押してください (GMは「プレイヤー除外」でも外せます)。"
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"次村の告知送信失敗 ({state.room_name}): {e}")

        lines = [f"🔁 次村: {len(added)}人を再登録しました。"]
        if skipped:
            lines.append("登録できなかった人: " + ", ".join(skipped))
        return "\n".join(lines)

    # ============================================================
    # ゲーム開始
    # ============================================================

    async def start_game(self, interaction: discord.Interaction) -> None:
        """ゲーム開始。API負荷の高い処理 (ニックネーム/ロール/DM一斉) を含むため、
        全卓共有の start_lock で同時1卓に直列化する。
        (ギルド共有のメンバー編集バケット ≈10回/10秒 の取り合いで
        複数卓の開始が共倒れに遅くなるのを防ぎ、順番に確実に開始させる)
        """
        if self.manager.start_lock.locked():
            try:
                await interaction.followup.send(
                    "⏳ 他の卓の開始処理が進行中です。順番に開始します (最大1分ほどお待ちください)。",
                    ephemeral=True,
                )
            except (discord.NotFound, discord.HTTPException):
                pass
        async with self.manager.start_lock:
            try:
                await self._start_game_locked(interaction)
            except StateDurabilityError as e:
                # 外部Discord副作用後のcheckpoint保存失敗。
                # _stop_for_durability_errorでPREPARATION復元フラグは済んでいるので
                # GMパネルを確保し、コールバックを例外終了させない。
                log.error(f"開始フェーズを安全停止: {e}")
                await self._repost_gm_panel()
                try:
                    await interaction.followup.send(
                        "⚠️ 開始状態の保存に失敗したため安全停止しました。DB復旧後にGMパネルの「再開」を押してください。",
                        ephemeral=True,
                    )
                except (discord.NotFound, discord.HTTPException):
                    pass

    def _start_was_aborted(self, state: GameState) -> bool:
        """開始処理中に強制終了/GM退出でゲームが畳まれたか。

        force_end は self.state を新しい GameState に差し替え、
        旧state の phase を GAME_OVER にする。その後も開始処理を続けると
        復元済みニックネームの再変更などの副作用が漏れるため、
        各段階の後に確認して中断する。
        """
        active_preparation = (
            state.phase == Phase.PREPARATION
            or (
                state.phase == Phase.PAUSED
                and state.phase_before_pause == Phase.PREPARATION
            )
        )
        return self.state is not state or not active_preparation

    async def _start_game_locked(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        state = self.state
        state.guild = guild
        state.game_run_id = secrets.token_hex(16)
        state.day_generation = 0
        state.night_generation = 0
        state.day_execution_resolved = False
        state.day_executed_target = None
        state.pending_execution_target = None
        state.night_resolved = False
        state.pending_winner = None
        state.ending = False
        state.preparation_dm_sent_ids.clear()
        state.initial_seer_target = None
        state.initial_seer_result_sent = False
        state.pending_death_effects.clear()
        state.day1_executed_id = None
        state.night1_killed_id = None
        state.prep_ready_ids.clear()
        state.prep_confirmed = False
        state.prep_ready_event.clear()
        state.phase = Phase.PREPARATION
        state.recovery_phase = None
        state.recovered_from_restart = False

        # ゲーム用チャンネル作成 (昼チャンネルのみ)
        # 失敗したらロビーに戻して通知 (権限不足等で停止しないように)
        try:
            await self._create_game_channels(guild)
        except (discord.Forbidden, discord.HTTPException, RuntimeError) as e:
            log.exception(f"ゲームチャンネル作成失敗: {e}")
            state.phase = Phase.LOBBY
            # 部分的に作成済みのチャンネルを残骸にしない
            for ch in (state.village_channel, state.spirit_channel):
                if ch is not None:
                    try:
                        await ch.delete(reason="人狼: ゲーム開始失敗のため削除")
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
            state.spirit_channel = None
            state.village_channel = None
            try:
                await interaction.followup.send(
                    f"❌ ゲームチャンネルの作成に失敗しました。\n詳細: {e}",
                    ephemeral=True,
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            try:
                await self._post_lobby_ui()
            except Exception:
                pass
            return

        progress_message = await self._safe_village_send(
            "⏳ **ゲーム開始準備中 (1/3)**\n"
            "ニックネーム・初期ミュート・参加ロールを順番に設定しています。"
        )

        # これ以降のBot muteは、muteと同一PATCHで専用ロールを
        # 付ける。ロールを確保できないまま開始すると、
        # API成功後・DB保存前クラッシュで手動muteと区別できない。
        mute_marker = await self._ensure_mute_marker_role()
        if mute_marker is None:
            await self._safe_village_send(
                "⚠️ mute所有マーカーを作成できないため、安全のため開始を中断します。"
            )
            await self.force_end("mute所有マーカーを確保できないため中断")
            return

        # 生存ロールも先に確保し、参加者のnick/muteと同じMember.editへ統合する。
        # これにより、通常の13人開始では add_roles 13件を丸ごと省ける。
        alive_role = await self._ensure_alive_role()
        if alive_role is None:
            await self._safe_village_send(
                "⚠️ 生存者用ロールを作成できないため、安全のため開始を中断します。"
            )
            await self.force_end("生存者用ロールを確保できないため中断")
            return

        # 番号割り当て & ニックネーム変更
        numbers = list(range(1, MAX_PLAYERS + 1))
        secrets.SystemRandom().shuffle(numbers)
        for i, player in enumerate(state.players.values()):
            # interaction由来のMemberは参加時点のスナップショットで、
            # 以後のロール変更等が反映されない。キャッシュ済みメンバーへ差し替える
            cached_member = guild.get_member(player.user_id)
            if cached_member is not None:
                player.member = cached_member
            player.number = numbers[i]
            player.original_nickname = player.member.nick
            player.base_name = player.member.nick or player.member.display_name

        # ニックネーム変更・初期ミュート・生存ロールは同じPATCHにまとめる。
        # メンバー編集はギルド共有バケット (429の主因) なので、
        # 「改名13件 + ミュート13件 + ロール付与13件」を13件に減らす。
        # VC未接続の人は mute を指定できないが、入室時に
        # on_voice_state_update がフェーズに合わせて同期する。
        vc = state.voice_channel
        vc_member_ids = {m.id for m in vc.members} if vc else set()

        def should_initial_mute(member: discord.Member) -> bool:
            # GMはミュート自動制御の対象外 (参加者を兼ねていても)
            if member.id == state.gm_id:
                return False
            if member.id not in vc_member_ids:
                return False
            vs = member.voice
            if vs is None or vs.mute:
                return False
            return True

        def initial_roles(
            member: discord.Member,
            *,
            include_alive: bool,
            include_mute_marker: bool,
        ) -> list[discord.Role]:
            roles = member_roles_for_edit(member)
            role_ids = {getattr(role, "id", None) for role in roles}
            if include_alive and alive_role is not None and alive_role.id not in role_ids:
                roles.append(alive_role)
                role_ids.add(alive_role.id)
            if include_mute_marker and mute_marker.id not in role_ids:
                roles.append(mute_marker)
            return roles

        # VCにいるメンバーのニックネーム保存 & 観戦者設定
        # (setdefault: VC入室リスナーが先に保存した「本来の名前」を上書きしない)
        vc_members = vc.members if vc else []
        # まず「誰をどの名前/役職/mute所有にするか」を完成させ、
        # Discordへの最初のmember.edit/DMより前に永続化する。
        initial_mute_targets: list[tuple[discord.Member, bool]] = []
        for member in vc_members:
            if member.bot:
                continue
            state.original_nicknames.setdefault(member.id, member.nick)

        for player in state.players.values():
            state.original_nicknames.setdefault(player.user_id, player.member.nick)

        await self._assign_roles()
        state.mute_marker_enabled = True
        seer = next((p for p in state.players.values() if p.role == Role.SEER), None)
        if seer is not None:
            non_wolves = [
                p for p in state.players.values()
                if p.role != Role.WEREWOLF and p.user_id != seer.user_id
            ]
            if non_wolves:
                state.initial_seer_target = secrets.choice(non_wolves).user_id

        # 現在Botが新たにmuteする意図の相手を先に所有記録する。
        # 既にmuteの相手は手動muteとして所有しない。
        for member in vc_members:
            if member.bot or member.id == state.gm_id:
                continue
            vs = getattr(member, "voice", None)
            if vs is not None and not vs.mute:
                state.bot_mute_intent_ids.add(member.id)

        try:
            await self._persist_room_state()
        except Exception as e:
            log.exception(f"開始意図の保存に失敗: {e}")
            await self._safe_village_send(
                "⚠️ 役職配布前の開始状態を保存できないため、安全のため中断します。"
            )
            await self.force_end("開始状態を保存できないため中断")
            return

        for member in vc_members:
            if member.bot:
                continue
            # GM (参加者を兼ねない) は改名もミュートもしない
            if member.id == state.gm_id and member.id not in state.players:
                continue
            if member.id in state.players:
                continue  # 参加者は次のループでまとめて処理
            edit_kwargs: dict = {}
            will_mute = should_initial_mute(member)
            if will_mute:
                edit_kwargs["mute"] = True
                edit_kwargs["roles"] = initial_roles(
                    member,
                    include_alive=False,
                    include_mute_marker=True,
                )
            if member.nick != "観戦者":
                edit_kwargs["nick"] = "観戦者"
            if not edit_kwargs:
                continue
            try:
                await self._paced_discord_api_call(member.edit, **edit_kwargs)
                if "mute" in edit_kwargs:
                    initial_mute_targets.append((member, True))
                    state.bot_muted_ids.add(member.id)
                    state.bot_mute_intent_ids.discard(member.id)
                    await self._persist_mute_ownership_checkpoint("観戦者のmute所有保存")
            except (discord.Forbidden, discord.HTTPException) as e:
                if "mute" in edit_kwargs:
                    state.bot_mute_intent_ids.discard(member.id)
                    await self._persist_mute_ownership_checkpoint("観戦者のmute失敗状態保存")
                log.warning(f"観戦者設定失敗: {member.display_name} ({e})")

        # 参加者のニックネーム保存 & 変更 (Discordのnick上限32字に切り詰め)
        alive_role_assigned_ids: set[int] = set()
        for player in state.players.values():
            will_mute = should_initial_mute(player.member)
            edit_kwargs: dict = {}
            if alive_role is not None or will_mute:
                edit_kwargs["roles"] = initial_roles(
                    player.member,
                    include_alive=alive_role is not None,
                    include_mute_marker=will_mute,
                )
            if will_mute:
                edit_kwargs["mute"] = True
            edit_kwargs["nick"] = player.display_name[:32]
            try:
                updated_member = await self._paced_discord_api_call(
                    player.member.edit, **edit_kwargs
                )
                if getattr(updated_member, "id", None) == player.user_id:
                    # Member.editは更新後Memberを返す。Gateway反映前でも、この卓が
                    # 続けてrolesを編集するときに統合済みロールを落とさない。
                    player.member = updated_member
                if alive_role is not None:
                    alive_role_assigned_ids.add(player.user_id)
                if "mute" in edit_kwargs:
                    initial_mute_targets.append((player.member, True))
                    state.bot_muted_ids.add(player.user_id)
                    state.bot_mute_intent_ids.discard(player.user_id)
                    await self._persist_mute_ownership_checkpoint("参加者のmute所有保存")
            except (discord.Forbidden, discord.HTTPException) as e:
                if "mute" in edit_kwargs:
                    state.bot_mute_intent_ids.discard(player.user_id)
                    await self._persist_mute_ownership_checkpoint("参加者のmute失敗状態保存")
                log.warning(f"参加者初期設定失敗: {player.member.display_name} ({e})")

        # 開始処理と並行して強制終了/GM退出 (force_end) が走った場合はここで中断する。
        # force_end のニックネーム復元より後に走った改名が残留しうるため、
        # 旧stateの記録を使ってもう一度戻す (二重復元は同じ値なので無害)
        if self._start_was_aborted(state):
            log.info(f"開始処理が中断されたため改名を巻き戻します ({state.room_name})")
            await self._restore_nicknames(state)
            return

        progress_message = await self._safe_timer_edit(
            progress_message,
            "⏳ **ゲーム開始準備中 (2/3)**\n役職DMを送信しています。",
        )

        # DM送信
        try:
            dm_failed = await self._send_role_dms()
        except Exception as e:
            log.exception(f"役職DM送信状態の保存に失敗: {e}")
            await self._safe_village_send(
                "役職DMの送信状態を保存できないため、安全のため開始を中断します。"
            )
            await self.force_end("役職DM状態の保存失敗により中断")
            return
        if dm_failed:
            failed_names = ", ".join(member.display_name for member in dm_failed)
            await self._safe_village_send(
                f"役職DMを送れない参加者がいるため開始を中断します: {failed_names}"
            )
            await self.force_end("役職DM送信失敗により中断")
            return

        # GMコントロールパネル投稿
        if not await self._repost_gm_panel():
            await self._safe_village_send(
                "⚠️ GMコントロールパネルを表示できないため、安全のためゲームを中断します。"
            )
            await self.force_end("GMコントロールパネルを表示できないため中断")
            return

        # DM送信中に中断された場合もロール付与前に止める
        if self._start_was_aborted(state):
            log.info(f"開始処理が中断されたため改名を巻き戻します ({state.room_name})")
            await self._restore_nicknames(state)
            return

        # 一時「生存」ロール付与 + VC権限の下準備
        # (@everyoneの発言禁止で途中入室の観戦者・死亡者の発言も塞ぐ)
        # 初期ミュートは上のニックネームPATCHへ統合済みなので、ここでは
        # 取りこぼし (改名失敗・直前入室など) だけを同期する
        progress_message = await self._safe_timer_edit(
            progress_message,
            "⏳ **ゲーム開始準備中 (3/3)**\nVC権限を仕上げています。",
        )
        # 統合PATCHに失敗した参加者、またはロール作成が一時失敗した場合だけ
        # add_rolesへフォールバックする。
        await self._assign_alive_role(exclude_ids=alive_role_assigned_ids)
        await self._restrict_vc_for_game()
        await self._grant_alive_vc_access()
        # member.edit完了直後はGateway上のvoice.mute反映が遅れる場合がある。
        # 反映前に_mute_allを呼んで同じ13件を再送しないよう先に待つ。
        if not await self._await_mute_applied(initial_mute_targets, MUTE_GRACE_TIME):
            await self._safe_village_send(
                "⚠️ 初期ミュートを確認できないため、安全のため開始を中断します。"
            )
            await self.force_end("初期ミュートを確認できないため中断")
            return
        remaining_mutes = await self._mute_all(
            skip_ids={member.id for member, _ in initial_mute_targets}
        )
        if not await self._await_mute_applied(remaining_mutes, MUTE_GRACE_TIME):
            await self._safe_village_send(
                "⚠️ 開始時の発言制御を確認できないため、安全のため開始を中断します。"
            )
            await self.force_end("開始時の発言制御を確認できないため中断")
            return

        # ゲームループ開始
        # (セットアップ中に強制終了/GM退出が走った場合は起動しない。
        #  起動すると切り離された旧state上で止められないループが回り続ける)
        if self._start_was_aborted(state):
            log.info(f"開始処理中にゲームが終了されたためループを起動しません ({state.room_name})")
            return
        await self._persist_room_state()
        await self._safe_timer_edit(
            progress_message,
            "✅ **ゲーム開始準備が完了しました。**\n役職確認タイムへ進みます。",
        )
        state.game_task = asyncio.create_task(self._game_loop())

    async def _create_game_channels(self, guild: discord.Guild) -> None:
        state = self.state
        category = state.category

        # 事前チェック: カテゴリ未設定 / Bot権限不足は早めに弾く
        # (start_game 側の except に拾わせて、ユーザに明確な理由を伝える)
        if category is None:
            raise RuntimeError(
                "人狼カテゴリが見つかりません。Botを再起動してください。"
            )
        if not guild.me.guild_permissions.manage_channels:
            raise RuntimeError(
                "Botに「チャンネル管理」権限がありません。"
            )

        # 前ゲームの削除待ち残骸 (300秒窓) を先に掃除する
        # (復元時に state が参照しているチャンネルは残す)
        keep_ids = {
            ch.id for ch in (state.village_channel, state.spirit_channel) if ch
        }
        for ch in list(guild.text_channels):
            if ch.category is None or ch.category.id != category.id:
                continue
            if ch.name not in (CH_VILLAGE, CH_SPIRIT) or ch.id in keep_ids:
                continue
            try:
                await self._paced_discord_api_call(
                    ch.delete, reason="人狼: 前ゲームの残骸削除"
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"残骸チャンネル削除失敗 (#{ch.name}): {e}")

        # #昼
        if state.village_channel is None:
            overwrites_village = self.manager._build_room_overwrites(
                guild,
                self.room_def,
                send_messages=False,
            )
            state.village_channel = await guild.create_text_channel(
                CH_VILLAGE, category=category, overwrites=overwrites_village,
            )

        # #霊界: 死亡者+観戦者の雑談チャンネル。
        # 部屋の表示権限をベースに書き込みを許可し、生存者には
        # 「メンバー個別上書き」で閲覧拒否を付ける。
        # 注意: Discordの権限解決はロール層でdenyを集約→allowを集約の順に
        # 適用するため、ロールdenyは別ロール (ランクロール/専用村ロール) の
        # view allowに打ち消される。メンバー上書きはロール層の後に適用される
        # ので、制限卓でも確実に生存者から隠せる。
        if state.spirit_channel is None:
            overwrites_spirit = self.manager._build_room_overwrites(
                guild,
                self.room_def,
                send_messages=True,
            )
            for player in state.players.values():
                if player.alive:
                    overwrites_spirit[player.member] = discord.PermissionOverwrite(
                        view_channel=False,
                        read_messages=False,
                    )
            state.spirit_channel = await guild.create_text_channel(
                CH_SPIRIT, category=category, overwrites=overwrites_spirit,
            )
            try:
                await state.spirit_channel.send(
                    "👻 ここは **霊界** です。死亡したプレイヤーと観戦者だけが見えます。\n"
                    "生存者には見えないので、役職や推理の話も自由にどうぞ。\n"
                    "(ネタバレ防止のため、生存者へのDM等での情報共有は禁止です)"
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"霊界の案内送信失敗 ({state.room_name}): {e}")

    async def _assign_roles(self) -> None:
        state = self.state
        roles = []
        for role, count in ROLE_DISTRIBUTION.items():
            roles.extend([role] * count)
        secrets.SystemRandom().shuffle(roles)

        for player, role in zip(state.players.values(), roles):
            player.role = role

    async def _send_role_dms(self) -> list[discord.Member]:
        state = self.state
        wolves = by_number([p for p in state.players.values() if p.is_wolf])
        wolf_names = ", ".join(w.display_name for w in wolves)
        failed: list[discord.Member] = []

        async def send_one(player: Player) -> None:
            if player.user_id in state.preparation_dm_sent_ids:
                return
            emoji = ROLE_EMOJI.get(player.role, "")
            team = ROLE_TEAM[player.role].value
            msg = f"{emoji} あなたの役職は **{player.role.value}** ({team}) です。"
            if player.is_wolf:
                msg += f"\n🐺 他の人狼: {wolf_names}"
                msg += (
                    "\n\n夜の制限時間中、このDMにメッセージを送ると他の人狼に中継されます。"
                )
            msg += (
                "\n\n━━━━━━━━━━\n"
                "📩 **議論は参加者全員の宣言で始まります。**\n"
                "この役職を確認したら #昼 の「役職を確認した」を押してください。\n"
                "\n"
                "🌅 **朝は生存者全員の宣言で明けます。**\n"
                "夜の行動が終わったら、毎晩 #昼 に出るパネルの「朝を迎える」を押してください。\n"
                "席を外したいときは押さずにお待ちください (どちらも押し直しで取り消せます)。"
            )
            try:
                await self._discord_api_call(player.member.send, msg)
                state.preparation_dm_sent_ids.add(player.user_id)
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"DM送信失敗: {player.member.display_name} ({e})")
                failed.append(player.member)

        # 全員に並列送信
        await asyncio.gather(*(send_one(p) for p in state.players.values()))

        # 成功したDMを保存。送信と保存の間で落ちた場合は
        # 同じ役職DMが重複する可能性はあるが、送信欠落は起こさない
        # at-least-onceを選ぶ (Discord DMに冪等キーはないため)。
        await self._persist_room_state()

        if failed:
            # gather の完了順で積まれるため並びが非決定的になる。
            # 中断メッセージも番号順で読めるように揃える。
            return sorted(
                failed,
                key=lambda m: getattr(state.get_player(m.id), "number", 0),
            )

        # 占い師: 初日ランダム白結果。対象は最初のDMより前に
        # スナップショット済みなので、再起動後も同じ結果を再送する。
        seer = next((p for p in state.players.values() if p.role == Role.SEER), None)
        if seer and not state.initial_seer_result_sent:
            random_white = state.get_player(state.initial_seer_target)
            if random_white is None:
                # 旧スナップショット互換。選ぶ場合も送信前に保存する。
                non_wolves = [p for p in state.players.values()
                              if p.role != Role.WEREWOLF and p.user_id != seer.user_id]
                if not non_wolves:
                    failed.append(seer.member)
                    return failed
                random_white = secrets.choice(non_wolves)
                state.initial_seer_target = random_white.user_id
                await self._persist_room_state()
            try:
                await self._discord_api_call(
                    seer.member.send,
                    f"🔮 初日占い結果: {random_white.display_name} は **村人**"
                )
                state.initial_seer_result_sent = True
                await self._persist_room_state()
            except (discord.Forbidden, discord.HTTPException):
                failed.append(seer.member)
        return failed

    # ============================================================
    # 人狼DM中継
    # ============================================================

    def _current_wolf_target_label(self) -> str:
        """現在の襲撃先の表示文字列 (未選択 / 噛みなし / 対象名)"""
        state = self.state
        if state.wolf_target is None:
            return "まだ決まっていません"
        if state.wolf_target == -1:
            return "**噛みなし**"
        target = state.get_player(state.wolf_target)
        return f"**{target.display_name}**" if target else "不明"

    def build_wolf_dm_content(self, duration: float) -> str:
        """人狼DMの本文。現在の襲撃先を常に最新の状態で表示する"""
        return (
            f"🌙 **夜フェーズ** (制限時間 {self._format_timer(duration)})\n"
            "襲撃対象をプルダウンから選んでください。夜の間は何度でも変更できます。\n"
            "**最後に選択された対象** が夜明けに襲撃されます。\n"
            "このDMに送ったメッセージは他の人狼に中継されます。\n"
            "**制限時間を過ぎると、中継も襲撃先の変更通知も止まります**"
            "（選び直しはできますが仲間には伝わりません）。\n"
            f"\n🎯 現在の襲撃先: {self._current_wolf_target_label()}"
        )

    async def refresh_wolf_dm_displays(
        self, duration: float, exclude_id: Optional[int] = None
    ) -> None:
        """全人狼のDM UIを編集して「現在の襲撃先」を最新化する。

        exclude_id は自分の応答で既に更新済みの人狼 (二重編集の回避)。
        """
        state = self.state
        content = self.build_wolf_dm_content(duration)

        async def edit_one(user_id: int, msg: discord.Message) -> None:
            try:
                await self._discord_api_call(msg.edit, content=content)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"人狼DM表示更新失敗 (ID:{user_id}): {e}")

        targets = [
            (uid, msg) for uid, msg in state.wolf_dm_messages.items()
            if uid != exclude_id
        ]
        if targets:
            await asyncio.gather(*(edit_one(uid, msg) for uid, msg in targets))

    async def _relay_to_wolves(self, message: str, exclude_id: Optional[int] = None) -> None:
        """人狼全員のDMにメッセージを中継 (並列)"""
        state = self.state

        async def relay_one(wolf: Player) -> None:
            try:
                await self._discord_api_call(wolf.member.send, message)
            except (discord.Forbidden, discord.HTTPException):
                pass

        targets = [w for w in state.alive_wolves() if w.user_id != exclude_id]
        if targets:
            await asyncio.gather(*(relay_one(w) for w in targets))

    async def on_message(self, message: discord.Message) -> None:
        """人狼のDMメッセージを他の人狼に中継 (GameCogのリスナーから各卓へdispatchされる)"""
        # Bot自身のメッセージは無視
        if message.author.bot:
            return
        # DMチャンネルのみ
        if not isinstance(message.channel, discord.DMChannel):
            return

        state = self.state
        # 夜の制限時間内のみ中継
        if not self.wolf_relay_open():
            return

        # 送信者が生存中の人狼か確認
        sender = state.get_player(message.author.id)
        if not sender or not sender.is_wolf or not sender.alive:
            return

        # 他の人狼に中継。Discordの2000字上限は「送信するメッセージ全体」に
        # かかるため、名前のプレフィックス分だけ本文を詰める。超えると
        # HTTPException(50035)で送信が失敗し、_relay_to_wolves がそれを
        # 握り潰すので、相談が黙って仲間に届かなくなる
        prefix = f"🐺 **{sender.display_name}**: "
        budget = DISCORD_MESSAGE_LIMIT - len(prefix)
        content = message.content
        if len(content) > budget:
            content = content[: budget - 1] + "…"
        await self._relay_to_wolves(prefix + content, exclude_id=sender.user_id)

    # ============================================================
    # ゲームループ
    # ============================================================

    async def _game_loop(self) -> None:
        state = self.state
        try:
            # 役職確認フェーズ (pause対応)。夜と同じく制限時間は目安で、
            # 参加者全員が「役職を確認した」を押すまで議論に入らない。
            def prep_content(remaining: float) -> str:
                return (
                    f"⏳ 役職確認タイム（{PREPARATION_TIME}秒）\n"
                    + self._timer_line(remaining, "目安")
                )

            # 復元で「確認済み」のまま戻ってきた場合はパネルもタイマーも出さない
            if not state.prep_confirmed:
                prep_msg = await self._safe_village_send(prep_content(PREPARATION_TIME))
                # 宣言パネルは役職DM配布後に出す (DM到着前に全員が押して
                # 議論が始まってしまわないよう、夜の朝パネルと同じ順序にする)
                await self._post_prep_panel()
                await self._repost_gm_panel()
                if self._check_prep_ready():
                    await self._persist_room_state()
                    state.prep_ready_event.set()
                await self._pausable_countdown(
                    prep_msg, prep_content, PREPARATION_TIME, state.prep_ready_event
                )
                await self._wait_for_prep_ready()
                # 待機解除と同時にGMが一時停止した場合、パネル終了やSEだけが
                # 停止中に先行しないよう、進行再開を待ってから副作用を出す。
                await state.pause_event.wait()
                await self._close_prep_panel()
                self._play_se("prep_end")

            # メインループ
            while True:
                state.day_number += 1
                state.day_generation += 1
                state.day_execution_resolved = False
                state.day_executed_target = None
                state.pending_execution_target = None

                # 朝のログ (2日目以降)
                if state.day_number > 1:
                    await self._morning_log()

                # 昼フェーズ: 議論
                await self._day_discussion()

                # 昼フェーズ: 投票
                executed_id = await self._day_vote()

                # 処刑処理 (遺言タイム → 処刑)
                if executed_id:
                    await self._checkpoint_pending_execution(executed_id)
                    executed_player = state.get_player(executed_id)
                    if executed_player and executed_player.alive:
                        await self._last_will(executed_player)
                    await self._execute_player(executed_id, "処刑")
                    state.day_execution_resolved = True
                    state.day_executed_target = executed_id
                    await self._persist_room_state()
                    winner = state.check_win()
                    if winner:
                        await self._end_game(winner)
                        return
                else:
                    state.day_execution_resolved = True
                    state.day_executed_target = None
                    state.pending_execution_target = None
                    await self._persist_room_state()

                # 夜フェーズ
                await self._night_phase()

                # 襲撃処理
                killed = await self._process_night()
                winner = state.check_win()
                if winner:
                    await self._end_game(winner)
                    return

        except asyncio.CancelledError:
            log.info("ゲームタスクがキャンセルされました")
        except StateDurabilityError as e:
            # _stop_for_durability_errorが既に安全停止・告知済み。
            # ここでforce_endすると、守ったスナップショットを捨ててしまう。
            log.error(f"永続化失敗のためゲームを安全停止: {e}")
        except Exception as e:
            log.exception(f"ゲームループエラー: {e}")
            # GMが手動リセットしなくても済むよう自動廃村
            # ただし force_end は内部で game_task を await するため、
            # 自身を await しないよう create_task で別タスクに分離する
            try:
                if state.village_channel:
                    await state.village_channel.send(
                        f"⚠️ エラーが発生したため自動的に廃村します: {e}"
                    )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            self.manager.spawn_bg_task(self.force_end("予期せぬエラーにより中断"))

    # ============================================================
    # 昼フェーズ: 議論
    # ============================================================

    async def _day_discussion(self) -> None:
        state = self.state
        await state.pause_event.wait()
        state.phase = Phase.DAY_DISCUSSION
        await self._persist_room_state()

        # チャンネル権限: 生存者のみ書き込み可
        await self._unlock_village_for_alive()

        # ボイスミュート解除 (生存者のみ)
        unmute_progress = await self._safe_village_send(
            "⏳ **議論開始準備中**\n生存者のミュートを順番に解除しています。"
        )
        changed = await self._unmute_alive()

        # 議論開始前の猶予。ミュート解除はギルド共有バケットのため
        # 人数分のAPIが捌けるまで数秒かかることがある。ここで待つことで
        # 「議論が始まったのにまだ喋れない人がいる」状態を防ぎ、
        # 開始の合図 (SE) で話し始めるタイミングを揃える。
        # 解除が全員へ反映されたことを確認したうえで、さらに固定の猶予を置く
        # (SEを合図に一斉に話し始められるようにするため)
        await self._wait_for_mute_sync_or_pause(
            changed,
            {p.user_id for p in state.alive_players()},
            "議論開始時のミュート解除",
        )
        if DISCUSSION_GRACE_TIME > 0:
            ready_text = (
                f"🔊 ミュートを解除しました。**{DISCUSSION_GRACE_TIME}秒後に議論開始**です。"
            )
            if unmute_progress is None:
                await self._safe_village_send(ready_text)
            else:
                await self._safe_timer_edit(unmute_progress, ready_text)
            await self._pausable_sleep(DISCUSSION_GRACE_TIME)
        elif changed:
            ready_text = "🔊 **ミュート解除が完了しました。**"
            if unmute_progress is None:
                await self._safe_village_send(ready_text)
            else:
                await self._safe_timer_edit(unmute_progress, ready_text)

        self._play_se("discussion")

        duration = state.get_day_discussion_time()
        def discussion_content(remaining: float) -> str:
            return (
                f"☀️ **{state.day_number}日目 - 議論タイム** (生存者: {len(state.alive_players())}人)\n"
                + self._timer_line(remaining)
            )

        timer_msg = await self._safe_village_send(discussion_content(duration))
        await self._repost_gm_panel()

        # 一時停止対応のタイマー
        await self._pausable_countdown(timer_msg, discussion_content, duration)

        # 議論終了。ミュートが行き渡るまで数秒かかるので、
        # 「終了 = もう喋れない」と誤解させない文言にする
        self._play_se("discussion_end")
        await self._safe_village_send("⏰ **議論時間終了！** 全員をミュートしています…")
        await self._lock_village()
        await self._mute_phase("まもなく投票フェーズに入ります。")

    # ============================================================
    # 昼フェーズ: 投票
    # ============================================================

    async def _day_vote(self, *, resume_existing: bool = False) -> Optional[int]:
        state = self.state
        await state.pause_event.wait()
        state.phase = Phase.DAY_VOTE
        if not resume_existing:
            state.votes.clear()
        state.vote_complete_event.clear()
        await self._persist_room_state()

        alive = state.alive_players()
        view = VoteView(self, candidates=by_number(alive), voters=alive)
        alive_voter_ids = {p.user_id for p in alive}
        if alive_voter_ids and alive_voter_ids <= state.votes.keys():
            state.vote_complete_event.set()
        # 投票開始のSEは鳴らさない。直前の議論終了SEと数秒差で連続し、
        # 合図として区別できないため (議論終了SEに一本化)

        def vote_content(remaining: float) -> str:
            return (
                "🗳️ **投票フェーズ** — 処刑する人を選んでください。\n"
                + self._timer_line(remaining, "制限時間")
            )

        timer_msg = await self._safe_village_send(vote_content(VOTE_TIMEOUT), view=view)
        await self._repost_gm_panel()

        # 全員投票完了 or タイムアウト (pause対応)
        completed = await self._pausable_countdown(
            timer_msg, vote_content, VOTE_TIMEOUT, state.vote_complete_event
        )
        if not completed:
            not_voted = [
                state.get_player(uid) for uid in view.voters if uid not in state.votes
            ]
            # フェーズ中に退出/除外で死亡した人は未投票者に載せない
            not_voted = [p for p in not_voted if p and p.alive]
            not_voted.sort(key=lambda p: p.number)
            names = ", ".join(p.display_name for p in not_voted)
            await self._safe_village_send(
                f"⏰ **投票時間切れ** — 未投票者: {names}\n既投票分で集計します。"
            )

        # 翌日の投票フェーズで古いボタンが反応しないよう停止する
        view.stop()

        if not state.votes:
            await self._safe_village_send("⚠️ 投票が1票もなかったため、処刑なしとなります。")
            return None

        # 結果表示
        self._play_se("reveal")
        embed = build_vote_result_embed(state.votes, state.players)
        await self._safe_village_send(embed=embed)

        # 集計
        tally = self._tally_votes(state.votes)
        max_votes = max(tally.values())
        top = [pid for pid, cnt in tally.items() if cnt == max_votes]

        if len(top) == 1:
            return top[0]
        else:
            # 決戦投票
            old_candidates = list(state.runoff_candidates)
            state.runoff_candidates = list(top)
            try:
                await self._persist_room_state()
            except Exception as e:
                state.runoff_candidates = old_candidates
                await self._stop_for_durability_error("決戦投票候補の保存", e)
                raise StateDurabilityError("決戦投票候補を保存できませんでした") from e
            return await self._runoff(top)

    def _tally_votes(self, votes: dict) -> dict[int, int]:
        tally: dict[int, int] = {}
        for target_id in votes.values():
            tally[target_id] = tally.get(target_id, 0) + 1
        return tally

    async def _runoff(
        self, candidate_ids: list[int], *, resume_vote: bool = False
    ) -> Optional[int]:
        state = self.state
        await state.pause_event.wait()
        state.runoff_candidates = list(candidate_ids)
        if not resume_vote:
            state.phase = Phase.DAY_RUNOFF_SPEECH
            await self._persist_room_state()

        # 念のため None を除去 (state.players からの取得失敗に備える)
        candidates = [
            p for p in (state.get_player(pid) for pid in candidate_ids)
            if p is not None
        ]
        if not candidates:
            log.error("_runoff: 候補者プレイヤーが取得できません")
            await self._safe_village_send(
                "⚠️ 決戦投票の候補者が存在しないため、処刑なしとなります。"
            )
            return None
        if not resume_vote:
            random.shuffle(candidates)

        if not resume_vote:
            await self._safe_village_send(
                "⚖️ **同票のため決戦投票！** 候補者が順番に弁明します。"
            )

        # 弁明フェーズ
        for candidate in candidates if not resume_vote else []:
            # 前の弁明中に退出/GM除外で死亡した候補はスキップ
            if not candidate.alive:
                continue
            state.speech_done_event.clear()
            state.current_speaker_id = candidate.user_id

            # 弁明者のみ発言許可 (生存ロールのミュートを個別に上書き)。
            # SEは候補ごとに、ミュート解除が反映され「実際に話せるようになった
            # 瞬間」へ鳴らす。_grant_speaker より前だと、直前の投票開示SE
            # (reveal) と1.5秒以内に重なってギルドロック待ちで破棄されうる
            await self._grant_speaker(candidate.member)
            self._play_se("speech")

            view = SpeechDoneView(self, candidate.user_id)
            def speech_content(remaining: float, candidate=candidate) -> str:
                return (
                    f"🎤 **{candidate.display_name}** の弁明\n"
                    + self._timer_line(remaining)
                )

            msg = await self._safe_village_send(speech_content(RUNOFF_SPEECH_TIME), view=view)
            await self._repost_gm_panel()

            # 弁明終了待ち (タイムアウト or ボタン, pause対応)
            # タイマーは _pausable_countdown だけが管理する (View側timeoutは廃止済み)
            await self._pausable_countdown(
                msg, speech_content, RUNOFF_SPEECH_TIME, state.speech_done_event
            )

            view.stop()
            state.current_speaker_id = None
            await self._clear_speaker(candidate.member)
            await self._safe_village_send(f"🎤 {candidate.display_name} の弁明終了")

        # 決戦投票
        state.phase = Phase.DAY_RUNOFF_VOTE
        if not resume_vote:
            state.votes.clear()
        state.vote_complete_event.clear()
        await self._persist_room_state()

        alive = state.alive_players()
        # 投票ボタンだけ番号順にする。candidates 自体のランダム順は
        # 弁明順の仕様 (上の random.shuffle) で、0票時のランダム処刑も
        # この順を使うため上書きしない。
        view = RunoffVoteView(self, candidates=by_number(candidates), voters=alive)
        alive_voter_ids = {p.user_id for p in alive}
        if alive_voter_ids and alive_voter_ids <= state.votes.keys():
            state.vote_complete_event.set()

        def runoff_vote_content(remaining: float) -> str:
            return (
                "🗳️ **決戦投票** — 候補者から選んでください。\n"
                + self._timer_line(remaining, "制限時間")
            )

        timer_msg = await self._safe_village_send(
            runoff_vote_content(VOTE_TIMEOUT), view=view
        )
        await self._repost_gm_panel()

        # 決戦投票待ち (pause対応)
        completed = await self._pausable_countdown(
            timer_msg, runoff_vote_content, VOTE_TIMEOUT, state.vote_complete_event
        )
        if not completed:
            not_voted = [
                state.get_player(uid) for uid in view.voters if uid not in state.votes
            ]
            # フェーズ中に退出/除外で死亡した人は未投票者に載せない
            not_voted = [p for p in not_voted if p and p.alive]
            not_voted.sort(key=lambda p: p.number)
            names = ", ".join(p.display_name for p in not_voted)
            await self._safe_village_send(
                f"⏰ **決戦投票時間切れ** — 未投票者: {names}\n既投票分で集計します。"
            )

        # 以降の投票フェーズで古いボタンが反応しないよう停止する
        view.stop()

        if not state.votes:
            # 0票: ランダム処刑 (None を除去済みの candidates から選ぶ。
            # 弁明中の退出/GM除外で死亡した候補は対象から外す)
            alive_candidates = [c for c in candidates if c.alive]
            if not alive_candidates:
                await self._safe_village_send(
                    "⚠️ 決戦投票の候補者が全員死亡・除外済みのため、処刑なしとなります。"
                )
                return None
            chosen_player = secrets.choice(alive_candidates)
            # 抽選結果を全体告知より先にdurableにする。
            # 告知後・caller側checkpoint前に落ちると、復元時に
            # 別人を再抽選してしまうため、この順序は崩さない。
            await self._checkpoint_pending_execution(chosen_player.user_id)
            await self._safe_village_send(
                f"⚠️ 決戦投票が行われなかったため、ランダムで **{chosen_player.display_name}** が処刑されます。"
            )
            return chosen_player.user_id

        self._play_se("reveal")
        embed = build_vote_result_embed(state.votes, state.players, title="決戦投票結果")
        await self._safe_village_send(embed=embed)

        tally = self._tally_votes(state.votes)
        max_votes = max(tally.values())
        top = [pid for pid, cnt in tally.items() if cnt == max_votes]

        if len(top) == 1:
            return top[0]
        else:
            # 再同票 → ランダム処刑
            chosen = secrets.choice(top)
            chosen_player = state.get_player(chosen)
            display = chosen_player.display_name if chosen_player else f"ID {chosen}"
            await self._checkpoint_pending_execution(chosen)
            await self._safe_village_send(
                f"⚖️ 再び同票のため、ランダムで **{display}** が処刑されます。"
            )
            return chosen

    # ============================================================
    # 遺言タイム (投票開示後・処刑処理前)
    # ============================================================

    async def _last_will(self, player: Player) -> None:
        """処刑が確定したプレイヤーの遺言タイム。

        本人だけ発言を許可し、本人かGMのボタンで短縮できる。
        襲撃死には遺言はない (投票開示後のみ)。
        """
        state = self.state
        await state.pause_event.wait()
        state.phase = Phase.DAY_LAST_WILL
        state.speech_done_event.clear()
        state.current_speaker_id = player.user_id
        await self._persist_room_state()

        # 遺言者のみ発言許可。SEは決戦弁明と揃えて、ミュート解除が反映され
        # 実際に話せるようになってから鳴らす (直前の投票開示SEとの競合回避)
        await self._grant_speaker(player.member)
        self._play_se("lastwill")

        view = SpeechDoneView(self, player.user_id, label="遺言終了")

        def will_content(remaining: float) -> str:
            return (
                f"🕯️ **{player.display_name}** の遺言タイム\n"
                + self._timer_line(remaining)
            )

        msg = await self._safe_village_send(will_content(LAST_WILL_TIME), view=view)
        await self._repost_gm_panel()

        # 遺言終了待ち (タイムアウト or ボタン, pause対応)
        await self._pausable_countdown(
            msg, will_content, LAST_WILL_TIME, state.speech_done_event
        )

        view.stop()
        state.current_speaker_id = None
        await self._clear_speaker(player.member)

    async def _checkpoint_pending_execution(self, player_id: int) -> None:
        """投票集計済みの処刑対象を遺言より前にdurableにする。"""
        state = self.state
        old_target = state.pending_execution_target
        state.pending_execution_target = player_id
        try:
            await self._persist_room_state()
        except Exception as e:
            state.pending_execution_target = old_target
            await self._stop_for_durability_error("処刑対象checkpointの保存", e)
            raise StateDurabilityError("処刑対象を保存できませんでした") from e

    # ============================================================
    # 処刑処理
    # ============================================================

    async def _execute_player(self, player_id: int, method: str) -> None:
        state = self.state
        await state.pause_event.wait()
        player = state.get_player(player_id)
        if not player or not player.alive:
            # 退出/GM除外で既に死亡済みのケース (二重処理防止)
            return

        # ゲーム上の死亡・チェックポイント・朝ログ・副作用
        # outboxをまず1つの部屋スナップショットに保存する。
        # Discordの改名/DM/通知を先に行うと、その途中クラッシュで
        # 死者が復活し再投票できてしまう。
        old_day_resolved = state.day_execution_resolved
        old_day_target = state.day_executed_target
        old_pending_execution_target = state.pending_execution_target
        old_last_executed = state._last_executed
        old_last_killed = state._last_killed
        old_day1_executed_id = state.day1_executed_id
        old_night1_killed_id = state.night1_killed_id
        self.log_action("死亡", target=player, detail=f"{method} / 役職={player.role.value if player.role else '不明'}")
        if method == "処刑":
            state.day_execution_resolved = True
            state.day_executed_target = player_id
            state.pending_execution_target = None
            state._last_executed = player
            if state.day_number == 1:
                state.day1_executed_id = player_id
        else:
            state._last_killed = player
            if state.day_number == 1:
                state.night1_killed_id = player_id
        player.alive = False
        generation = state.day_generation if method == "処刑" else state.night_generation
        effect = {
            "event_id": f"{state.game_run_id}:{method}:{generation}:{player_id}",
            "player_id": player_id,
            "method": method,
            "reason": None,
        }
        if not any(item.get("event_id") == effect["event_id"] for item in state.pending_death_effects):
            state.pending_death_effects.append(effect)
        try:
            await self._persist_room_state()
        except Exception as e:
            player.alive = True
            state.day_execution_resolved = old_day_resolved
            state.day_executed_target = old_day_target
            state.pending_execution_target = old_pending_execution_target
            state._last_executed = old_last_executed
            state._last_killed = old_last_killed
            state.day1_executed_id = old_day1_executed_id
            state.night1_killed_id = old_night1_killed_id
            state.pending_death_effects = [
                item for item in state.pending_death_effects
                if item.get("event_id") != effect["event_id"]
            ]
            # 夜は_process_nightがnight_resolved/guard_previousも含めて
            # ロールバックしてから安全停止する。
            if method != "襲撃":
                await self._stop_for_durability_error("死亡状態の保存", e)
            raise StateDurabilityError("死亡状態を保存できませんでした") from e

        await self._apply_death_effect(effect)

    async def _stop_for_durability_error(self, context: str, error: Exception) -> None:
        """勝敗に関わる保存失敗で状態を捨てず安全停止する。"""
        state = self.state
        resume_phase = self._effective_phase() or Phase.DAY_DISCUSSION
        if state.phase != Phase.PAUSED:
            state.phase_before_pause = state.phase
        state.recovery_phase = resume_phase
        state.recovered_from_restart = True
        state.phase = Phase.PAUSED
        state.paused = True
        state.pause_event.clear()
        try:
            await self._persist_room_state()
        except Exception:
            log.exception(f"{context}失敗後の停止状態も保存できませんでした")
        log.exception(f"{context}に失敗: {error}")
        await self._safe_village_send(
            f"⚠️ **{context}に失敗したため安全停止しました。**\n"
            "状態を捨てずに停止しています。DB復旧後にGMが再開してください。"
        )

    async def _persist_mute_ownership_checkpoint(self, context: str) -> None:
        """mute API結果と所有記録をdurableに同期する。"""
        try:
            await self._persist_room_state()
        except Exception as e:
            await self._stop_for_durability_error(context, e)
            raise StateDurabilityError(f"{context}に失敗しました") from e

    async def _apply_death_effect(self, effect: dict) -> None:
        """先に永続化した死亡/除外のDiscord副作用を冪等再適用する。"""
        state = self.state
        player = state.get_player(int(effect.get("player_id", 0)))
        if player is None:
            # サーバー退出でMemberを復元できない場合もゲーム進行を
            # 永久に止めない。通知は復元時の欠落メンバー告知で補う。
            state.pending_death_effects = [
                item for item in state.pending_death_effects
                if item.get("event_id") != effect.get("event_id")
            ]
            await self._persist_room_state()
            return

        method = str(effect.get("method"))
        reason = effect.get("reason")

        # interaction由来の古いMemberでroles全置換すると、開始後に付いた
        # 専用村等のロールまで落としうる。必ず最新のguild cacheを優先する。
        if state.guild is not None:
            cached_member = state.guild.get_member(player.user_id)
            if cached_member is not None:
                player.member = cached_member

        # ニックネーム更新・サーバーミュート・生存ロール剥奪を同じPATCHへ
        # まとめる。再適用しても同じ値になる。
        edit_kwargs: dict = {"nick": death_nick(player.display_name, method)}
        vs = getattr(player.member, "voice", None)
        vc = state.voice_channel
        will_mute = (
            player.user_id != state.gm_id  # GMはミュート自動制御の対象外
            and vs is not None and vs.channel is not None
            and vc is not None and vs.channel.id == vc.id
            and not vs.mute
        )
        marker: Optional[discord.Role] = None
        if will_mute:
            edit_kwargs["mute"] = True
            if state.guild is not None:
                marker = await self._enable_mute_markers()
        alive_role = (
            discord.utils.get(state.guild.roles, name=self._alive_role_name())
            if state.guild is not None else None
        )
        current_roles = member_roles_for_edit(player.member)
        desired_roles = [
            role for role in current_roles
            if alive_role is None or getattr(role, "id", None) != alive_role.id
        ]
        if marker is not None and not any(
            getattr(role, "id", None) == marker.id for role in desired_roles
        ):
            desired_roles.append(marker)
        role_ids_before = {getattr(role, "id", None) for role in current_roles}
        role_ids_after = {getattr(role, "id", None) for role in desired_roles}
        removes_alive_in_patch = (
            alive_role is not None
            and alive_role.id in role_ids_before
            and alive_role.id not in role_ids_after
        )
        if role_ids_after != role_ids_before:
            edit_kwargs["roles"] = desired_roles

        edit_succeeded = False
        try:
            updated_member = await self._discord_api_call(
                player.member.edit, **edit_kwargs
            )
            if getattr(updated_member, "id", None) == player.user_id:
                player.member = updated_member
            edit_succeeded = True
            if "mute" in edit_kwargs:
                state.bot_muted_ids.add(player.user_id)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"死亡者ニックネーム更新失敗 ({player.display_name}): {e}")
        else:
            # 復元再適用時、nick+mute同一PATCH成功後のクラッシュで
            # 既にmute=Trueならedit_kwargsにmuteが入らない。期待nickが
            # 反映済みならBot所有と照合する。
            vs_after = getattr(player.member, "voice", None)
            if (
                vs_after is not None
                and vs_after.mute
                and (
                    self._has_own_mute_marker(player.member)
                    if state.mute_marker_enabled
                    else player.member.nick == edit_kwargs["nick"]
                )
            ):
                state.bot_muted_ids.add(player.user_id)

        # チャンネル権限: 読み取り専用。死亡者が生存ロールを保持する
        # 取りこぼしがあっても、メンバー個別denyを最後の防壁にする。
        village_blocked = False
        if state.village_channel is not None:
            try:
                await self._discord_api_call(
                    state.village_channel.set_permissions,
                    player.member, read_messages=True, send_messages=False,
                )
                village_blocked = True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"死亡者権限変更失敗 ({player.display_name}): {e}")
        else:
            log.warning(
                "#昼が無いため死亡者の読み取り専用権限を適用できません: %s",
                player.display_name,
            )

        # 生存ロール剥奪 & 霊界の閲覧ブロック解除。
        # 役職剥奪は再接続後の発言権バックストップ、直上の
        # サーバーミュートは現在接続中の即時遮断として両方行う。
        alive_role_removed = edit_succeeded and removes_alive_in_patch
        if not alive_role_removed:
            alive_role_removed = await self._remove_alive_role(player.member)

        vs_after = getattr(player.member, "voice", None)
        in_game_vc = (
            vs_after is not None
            and vs_after.channel is not None
            and state.voice_channel is not None
            and vs_after.channel.id == state.voice_channel.id
        )
        mute_applied = bool(
            not in_game_vc
            or getattr(vs_after, "mute", False)
            or (edit_succeeded and "mute" in edit_kwargs)
        )
        # textは個別denyを必須、VCはserver muteまたは生存ロール剥奪の
        # どちらかを必須にする。満たせない副作用はoutboxから消さない。
        if not village_blocked or not (mute_applied or alive_role_removed):
            error = RuntimeError(
                "死亡者の発言遮断を確認できません "
                f"(village={village_blocked}, mute={mute_applied}, "
                f"alive_role_removed={alive_role_removed})"
            )
            await self._stop_for_durability_error("死亡者の発言権剥奪", error)
            raise StateDurabilityError(
                "死亡者の発言権を安全に剥奪できませんでした",
                state_committed=True,
            )
        await self._open_spirit_for(player.member)
        await self._safe_spirit_send(
            f"👻 **{player.display_name}** が霊界へやってきました。"
        )

        if method == "処刑":
            self._play_se("execution")
            await self._safe_village_send(f"⚰️ **{player.display_name}** が処刑されました。")

            # 霊媒師にDM
            medium = next(
                (p for p in state.players.values()
                 if p.role == Role.MEDIUM and p.alive),
                None,
            )
            if medium:
                result = "**人狼**" if player.is_wolf else "**村人**"
                try:
                    await medium.member.send(
                        f"👻 霊能結果: {player.display_name} は {result} でした。"
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
        elif method == "除外":
            await self._safe_village_send(
                f"🚪 **{player.display_name}** が{reason or ''}ゲームから除外されました "
                "(死亡扱い / 役職は非公開)。"
            )

        old_outbox = list(state.pending_death_effects)
        state.pending_death_effects = [
            item for item in state.pending_death_effects
            if item.get("event_id") != effect.get("event_id")
        ]
        try:
            await self._persist_room_state()
        except Exception as e:
            # 死亡本体は既にdurable。outboxだけを戻し、自動廃村せず
            # 安全停止する。通知/DMはat-least-onceなので、この
            # 障害窓では再適用時に重複する可能性がある。
            state.pending_death_effects = old_outbox
            await self._stop_for_durability_error("死亡後通知outboxの保存", e)
            raise StateDurabilityError(
                "死亡後通知outboxを保存できませんでした",
                state_committed=True,
            ) from e

    async def _reconcile_pending_death_effects(self) -> None:
        """クラッシュ前に永続化済みのDiscord副作用を復元時に完了する。"""
        for effect in list(self.state.pending_death_effects):
            try:
                await self._apply_death_effect(effect)
            except Exception as e:
                # outboxは残す。次回の復元または運用者の再試行で再適用する。
                log.exception(f"死亡副作用outboxの再適用失敗: {e}")

    # ============================================================
    # 夜フェーズ
    # ============================================================

    async def _night_phase(self, *, resume_existing: bool = False) -> None:
        state = self.state
        await state.pause_event.wait()
        state.phase = Phase.NIGHT

        # 前の夜・異常終了経路で残ったViewを先に停止する。
        self._stop_night_views()
        if not resume_existing:
            state.night_generation += 1
            state.wolf_target = None
            state.wolf_voters.clear()
            state.seer_target = None
            state.guard_target = None
            state.night_complete_event.clear()
            state.morning_ready_ids.clear()
            state.morning_warned_ids.clear()
            state.morning_confirmed = False
            state.morning_ready_event.clear()
            state.night_resolved = False
        elif state.morning_confirmed:
            state.morning_ready_event.set()
        await self._persist_room_state()

        # #昼チャンネル ロック & ミュート
        await self._lock_village()
        await self._mute_phase("夜フェーズに入ります。")
        self._play_se("night")

        duration = state.get_night_time()
        state.night_duration = duration

        def night_content(remaining: float) -> str:
            return (
                f"🌙 **{state.day_number}日目 - 夜フェーズ**\n"
                + self._timer_line(remaining, "目安")
            )

        timer_msg = await self._safe_village_send(night_content(duration))

        # 夜UIの参照を保持し、夜終了時に必ずstopする
        # (未行動タイムアウト等で有効なまま残ったUIが後の夜に再利用されるのを防ぐ)
        self._night_views = []

        # 人狼: 各狼のDMに襲撃UIを送信 (並列)
        non_wolves_alive = by_number(
            [p for p in state.alive_players() if not p.is_wolf]
        )
        state.wolf_dm_messages.clear()

        async def send_wolf_dm(wolf: Player) -> None:
            wolf_view = WolfVoteView(self, non_wolves_alive, wolf)
            self.register_game_view(wolf_view, night=True)
            try:
                msg = await self._discord_api_call(
                    wolf.member.send,
                    self.build_wolf_dm_content(duration),
                    view=wolf_view,
                )
                state.wolf_dm_messages[wolf.user_id] = msg
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"人狼DM送信失敗: {wolf.member.display_name} ({e})")

        wolves_alive = state.alive_wolves()
        if wolves_alive:
            await asyncio.gather(*(send_wolf_dm(w) for w in wolves_alive))

        # 占い師DM
        seer = next(
            (p for p in state.players.values()
             if p.role == Role.SEER and p.alive),
            None,
        )
        if seer:
            targets = by_number(
                [p for p in state.alive_players() if p.user_id != seer.user_id]
            )
            if state.seer_target is None:
                seer_view = SeerView(self, targets)
                self.register_game_view(seer_view, night=True)
                try:
                    await self._discord_api_call(
                        seer.member.send,
                        "🔮 **占う対象を選んでください。**\n"
                        "選ぶと実行確認が出ます。確定すると**その場で結果が表示**され、今夜は変更できません。",
                        view=seer_view,
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"占い師DM送信失敗: {seer.member.display_name} ({e})")
            else:
                target = state.get_player(state.seer_target)
                if target is not None:
                    result = "**人狼**" if target.role == Role.WEREWOLF else "**村人**"
                    try:
                        await self._discord_api_call(
                            seer.member.send,
                            f"♻️ 復元: 今夜の占いは確定済みです。\n"
                            f"🔮 占い結果: **{target.display_name}** は {result} でした。",
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass

        # 狩人DM
        guard = next(
            (p for p in state.players.values()
             if p.role == Role.GUARD and p.alive),
            None,
        )
        if guard:
            targets = by_number([
                p for p in state.alive_players()
                if p.user_id != guard.user_id and p.user_id != state.guard_previous
            ])
            if state.guard_target is None:
                guard_view = GuardView(self, targets)
                self.register_game_view(guard_view, night=True)
                try:
                    await self._discord_api_call(
                        guard.member.send,
                        "🛡️ **護衛対象を選んでください。**\n"
                        "選ぶと実行確認が出ます。確定すると今夜は変更できません。",
                        view=guard_view,
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"狩人DM送信失敗: {guard.member.display_name} ({e})")
            else:
                target = state.get_player(state.guard_target)
                if target is not None:
                    try:
                        await self._discord_api_call(
                            guard.member.send,
                            f"♻️ 復元: 今夜の護衛は **{target.display_name}** で確定済みです。",
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass

        # 「朝を迎える」パネル (夜の終了はこのボタンで決まる)。#昼 へ1枚掲示する。
        # 役職DMを配り終えてから出す: 先に出すと、役職DMが届く前に
        # 全員が押して夜が終わってしまう。
        # 前夜のパネルは夜の終わり (_close_morning_panel) で閉じて参照を
        # 手放しているので、ここは常に新規掲示になる。同じ夜をやり直す
        # resume_existing では参照が残っていれば冪等ガードで再利用する。
        await self._post_morning_panel()
        # 夜は #昼 の書き込みが止まるため、GM入口を最後に整理する。
        await self._repost_gm_panel()
        if self._check_morning_ready():
            # requiredが空の異常系も、夜待ちを解放する前に
            # 夜明け確定をdurableにする。
            await self._persist_room_state()
            state.morning_ready_event.set()

        # 全員の夜UIが届いてから中継窓と制限時間を同時に開始する。
        # UI配送時間を相談時間へ上乗せせず、例外・キャンセルでも必ず閉じる。
        state.wolf_relay_window_open = True
        try:
            # 夜の制限時間は目安 (pause対応)。生存者全員が「朝を迎える」を押したら
            # 時間内でも切り上げる。時間切れでは自動的に朝にしない
            await self._pausable_countdown(
                timer_msg, night_content, duration, state.morning_ready_event
            )
        finally:
            state.wolf_relay_window_open = False

        # 人狼DMの中継は制限時間で打ち切る。「朝を迎える」の宣言状況では
        # 開閉しない (全員が押すのを待っている間も相談できてしまうため)。
        # 襲撃・占い・護衛の選択は夜明けまで受け付けたままにする
        # (未行動者に警告DMを送って選ばせる設計のため)
        # 時間切れ後は未行動者へ警告DMを送り、全員の宣言 (かGMの強制) を待つ
        await self._wait_for_morning()

        # 宣言待ち解除と同時に一時停止した場合、夜UI終了・朝SEだけが
        # 停止中に先行しないよう、再開後に夜明け副作用を適用する。
        await state.pause_event.wait()
        # 夜UIを停止 (以降の操作は届かない)
        self._stop_night_views()
        await self._close_morning_panel()
        # 夜明けのSEはここで鳴らす。朝ログ(_morning_log)まで待つと、
        # 襲撃死の改名・ロール剥奪・DMを挟んで数秒ずれる
        self._play_se("morning")

    def _pending_night_actions(self) -> list[tuple[Player, str]]:
        """未行動の役職と行動名の一覧 (警告DMの宛先。村へは出さない)"""
        state = self.state
        pending: list[tuple[Player, str]] = []

        if state.wolf_target is None:
            for wolf in state.alive_wolves():
                pending.append((wolf, "襲撃先"))

        seer = next(
            (p for p in state.players.values() if p.role == Role.SEER and p.alive),
            None,
        )
        if seer is not None and state.seer_target is None:
            pending.append((seer, "占い先"))

        guard = next(
            (p for p in state.players.values() if p.role == Role.GUARD and p.alive),
            None,
        )
        if guard is not None and state.guard_target is None:
            pending.append((guard, "護衛先"))

        return pending

    def _check_night_complete(self) -> None:
        """夜アクションが揃ったら完了イベントを立てる (未行動警告の抑制用)"""
        state = self.state
        if not self.night_actions_open():
            return
        if not self._pending_night_actions():
            state.night_complete_event.set()

    async def deliver_seer_result(self, seer_id: int, text: str) -> None:
        """占い結果を占い師の通常DMへ送る (手元に残す用)。

        確定時の表示はエフェメラルなので、閉じるかクライアントを再読み込み
        すると消える。夜が明けると夜UIも停止するため、再表示の手段も無い。
        送信に失敗しても占いは成立済みなので、ここでは進行を止めない。
        """
        seer = self.state.get_player(seer_id)
        if seer is None:
            return
        try:
            await self._discord_api_call(seer.member.send, text)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"占い結果DM送信失敗 ({seer.display_name}): {e}")

    async def _wait_for_morning(self) -> None:
        """夜の制限時間が切れた後、生存者全員の「朝を迎える」宣言を待つ。

        時間切れでは自動的に朝にしない (離席したい人はボタンを押さずに
        待たせることで、一時停止の代わりになる)。誰が押していないかは
        村へ出さない。**宣言人数もここで初めて公開する** (夜の間ずっと
        見えていると、未宣言者から生存役職を推定されるため)。
        全員が戻らない場合はGMがGMコントロールパネルの「朝」で進行できる。
        """
        state = self.state
        if state.morning_ready_event.is_set():
            return

        # パネルを掲示できていないと誰も押せない。取りこぼしをここで補完する
        await self._post_morning_panel()

        # パネルを掲示できないなら、誰も夜を明けられない。待ち続けても
        # 永久に明けないので、自動で明けて進行を止めない
        missing = self._morning_required_ids() - state.morning_ready_ids
        if missing and state.morning_panel_message is None:
            log.error(
                f"朝パネルを掲示できません ({state.room_name}): 自動で夜を明けます"
            )
            # この分岐も「夜明け確定」として保存する。イベント
            # だけを立てずにreturnすると、再起動後に同じ夜を再実行
            # する可能性がある。
            state.morning_confirmed = True
            try:
                await self._persist_room_state()
            except Exception:
                state.morning_confirmed = False
                raise
            state.morning_ready_event.set()
            await self._safe_village_send(
                "⚠️ 「朝を迎える」パネルを掲示できないため、自動で夜を明けます。"
            )
            return

        # 未行動の役職には個別に警告DM (誰が未行動かは村へ出さない)
        pending = self._pending_night_actions()
        if pending:
            async def warn(player: Player, action: str) -> None:
                try:
                    await self._discord_api_call(
                        player.member.send,
                        f"⚠️ **まだ{action}を選んでいません。**\n"
                        "夜の目安時間が終了しました。選ばないまま朝になると、"
                        "今夜の行動はなかったことになります。",
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"未行動の警告DM送信失敗 ({player.display_name}): {e}")

            await asyncio.gather(*(warn(p, action) for p, action in pending))

        # 通知を新規メッセージで送るとパネルが上へ流れて見失うので、
        # パネル本文そのものを人数入りへ書き換える。
        await self._reveal_morning_count()
        await self._pausable_wait_forever(state.morning_ready_event)

    # ============================================================
    # 朝を迎える宣言
    # ============================================================

    def _morning_required_ids(self) -> set[int]:
        """「朝を迎える」を押す必要がある人 (生存者。GMは参加者のときだけ)"""
        return {p.user_id for p in self.state.alive_players()}

    def _morning_ready_count(self) -> tuple[int, int]:
        """(宣言済み人数, 必要人数)"""
        required = self._morning_required_ids()
        return len(required & self.state.morning_ready_ids), len(required)

    def _morning_panel_content(self, *, reveal_count: bool = False) -> str:
        """#昼 に1枚だけ掲示する「朝を迎える」パネルの本文。

        **夜の間は人数を出さない。** 13人固定構成で夜に行動があるのは
        狼3・占い師・狩人の5人だけなので、宣言人数を常時公開すると
        「まだ押していない役職者が何人いるか」が毎晩測れてしまい、
        占い師・狩人の生存がCO前に推定できる。人狼は互いを知っていて
        夜のDM中継で相談できるため、押す順番を申し合わせれば
        カウンタを能動的に読む道具にもなる。

        押した本人にも人数は返さない (_morning_feedback_text)。押せば
        見えるなら、押して確かめて押し直すだけで測れてしまうため。
        人数を出すのは**目安時間が切れた後の1回だけ** (reveal_count=True)
        で、これは従来の「時間切れ後に #昼 へ人数を1回通知する」露出量と
        同じ。全員が同時に知るので不公平も生まない。
        """
        body = (
            "🌅 **朝を迎える**\n"
            "夜の行動が終わったら押してください。\n"
            "**生存者全員が押すと朝になります。**\n"
            "押し直すと取り消せます。離席したいときは押さずにお待ちください。\n"
            "宣言した人数は、夜の目安時間が切れたときにここへ表示されます。"
        )
        if not reveal_count:
            return body
        ready, required = self._morning_ready_count()
        return (
            f"{body}\n"
            "\n"
            "⏳ **夜の目安時間が終了しました。**"
            f"（現在 **{ready} / {required}人**）\n"
            "(戻らない人がいる場合はGMがGMメニューの「朝」で進行できます)"
        )

    def _morning_feedback_text(self, *, declared: bool) -> str:
        """押した本人にだけ ephemeral で返す文面。

        **人数は出さない。** 押せば人数が見えると、押して確かめて押し直す
        だけで進捗を測れてしまい、夜の間は公開しない意味が薄れる。
        自分の操作が通ったかどうかだけを返し、人数は目安時間が切れた
        ときにパネルへ一度だけ全員へ出す (_reveal_morning_count)。
        """
        ready, required = self._morning_ready_count()
        if required and ready >= required:
            return "🌅 **全員が「朝を迎える」を押しました。夜が明けます。**"
        if declared:
            return (
                "✅ **「朝を迎える」を宣言しました。**\n"
                "他の生存者を待っています。押し直すと取り消せます。"
            )
        return (
            "↩️ **宣言を取り消しました。**\n"
            "もう一度押すと宣言し直せます。"
        )

    async def _post_morning_panel(self) -> None:
        """夜の「朝を迎える」パネルを #昼 へ1枚だけ掲示する。

        夜は #昼 が書き込み禁止だが、**ボタン押下は送信権限と無関係**なので
        押せる (役職確認タイムの PrepReadyView が同じ条件で動いている)。
        生存者13人へDMを配っていた頃と比べ、送信APIが13→1になる。
        減るのはグローバルのレート制限枠を使う通常送信で、押下時の
        defer/followup は interaction 専用ルートなので数以上に効く。

        既に掲示済みなら何もしない (再掲示は取りこぼしの補完に使う)。
        """
        state = self.state
        if state.morning_panel_message is not None:
            return
        # 再起動をまたいだ前回のパネルを先に消す (下の説明を参照)
        await self._delete_stale_morning_panel()
        self._morning_view = MorningReadyView(self)
        state.morning_panel_message = await self._safe_village_send(
            self._morning_panel_content(), view=self._morning_view
        )
        if state.morning_panel_message is None:
            # 掲示できなかった場合はViewを残さない (押される先が無い)
            self._morning_view.stop()
            self._morning_view = None
            return
        # 掲示直後に落ちても次回消せるよう、IDを永続化してから先へ進む
        state.morning_panel_message_id = getattr(
            state.morning_panel_message, "id", None
        )
        try:
            await self._persist_room_state()
        except Exception as e:
            # 消し損ねたパネルが1枚残るだけで進行は続けられる
            log.warning(f"朝パネルIDの保存に失敗 ({state.room_name}): {e}")

    async def _delete_stale_morning_panel(self) -> None:
        """再起動前に掲示した朝パネルを消す。

        Viewはプロセスをまたいで復元されない (ボタンに custom_id が無く
        永続View化もできない) ため、残ったパネルを押すとDiscordの
        「インタラクションに失敗しました」しか出ない。#昼 に新旧2枚が
        並ぶと誤タップの元なので、次の掲示より前に消す。

        get_partial_message ならメッセージを取得せず1回のAPIで消せる。
        """
        state = self.state
        message_id = state.morning_panel_message_id
        state.morning_panel_message_id = None
        ch = state.village_channel
        if message_id is None or ch is None:
            return
        get_partial = getattr(ch, "get_partial_message", None)
        if not callable(get_partial):
            return
        try:
            await self._discord_api_call(get_partial(message_id).delete)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            # 既に消えている・権限が無いなら放置してよい
            log.warning(f"前回の朝パネル削除に失敗 ({state.room_name}): {e}")

    async def _reveal_morning_count(self) -> None:
        """目安時間が切れたときだけ、パネル本文に人数を出す。

        新しいメッセージを送らずパネル自体を編集するのは、通知でパネルが
        上へ流れて見失うのを避けるため。
        """
        state = self.state
        msg = state.morning_panel_message
        if msg is None:
            return
        try:
            await self._discord_api_call(
                msg.edit, content=self._morning_panel_content(reveal_count=True)
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"朝パネルの人数表示に失敗 ({state.room_name}): {e}")

    async def _close_morning_panel(self) -> None:
        """夜明け後にパネルのボタンを無効化して閉じる。

        DMへ13通配っていた頃は「前夜のパネルが各自のDMに残る」ため
        View を stop できなかった (stopするとコールバックが起動せず、
        押した人にはDiscordの「インタラクションに失敗しました」しか出ない)。
        #昼 の1枚になったことで、その場で無効化して閉じられる。
        """
        state = self.state
        view = self._morning_view
        msg = state.morning_panel_message
        self._morning_view = None
        state.morning_panel_message = None
        # 閉じたパネルは次の夜に消す対象ではない (ボタンは無効化済み)。
        # 無効化に失敗した場合もViewをstopするので押しても動かない。
        state.morning_panel_message_id = None
        if view is None:
            return
        # 先に表示を無効化してから stop する。逆順だと、編集が着地するまでの
        # 数百msに押した人へ汎用エラーが出る。
        if msg is not None and not all(item.disabled for item in view.children):
            for item in view.children:
                item.disabled = True
            try:
                await self._discord_api_call(msg.edit, view=view)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        view.stop()

    async def toggle_morning_ready(self, member: discord.Member) -> tuple[str, Optional[str]]:
        """「朝を迎える」の宣言をトグルする。

        Returns:
            (押した本人へephemeralで返す本文, エラー文字列)
            — どちらも本人にしか見えない。公開パネルは編集しない
              (夜の宣言人数を村へ出さないため)
        """
        state = self.state
        if state.morning_ready_event.is_set():
            # 既に夜明けが確定している (全員宣言済み / GMの強制)。
            # ここで取り消しを受け付けると表示だけ巻き戻って紛らわしい
            return "", "🌅 まもなく朝になります。"
        if not self.night_actions_open():
            return "", "⏳ 現在この操作はできません。"
        player = state.get_player(member.id)
        if player is None or not player.alive:
            return "", "⏳ 生存中の参加者だけが押せます。"

        if member.id in state.morning_ready_ids:
            state.morning_ready_ids.discard(member.id)
            # 警告は1晩に1度だけ。宣言を取り消しても警告済み記録は残す。
            try:
                await self._persist_room_state()
            except Exception as e:
                state.morning_ready_ids.add(member.id)
                log.exception(f"朝宣言の取り消し保存に失敗: {e}")
                return "", "❌ 宣言の取り消しを保存できませんでした。もう一度お試しください。"
            return self._morning_feedback_text(declared=False), None

        # 未行動の役職には1度だけ警告し、2度目の押下で確定させる (誤タップ防止)
        pending_ids = {p.user_id for p, _ in self._pending_night_actions()}
        if member.id in pending_ids and member.id not in state.morning_warned_ids:
            state.morning_warned_ids.add(member.id)
            action = next(a for p, a in self._pending_night_actions() if p.user_id == member.id)
            try:
                await self._persist_room_state()
            except Exception as e:
                state.morning_warned_ids.discard(member.id)
                log.exception(f"朝宣言の未行動警告保存に失敗: {e}")
                return "", "❌ 状態を保存できませんでした。もう一度お試しください。"
            return "", (
                f"⚠️ **まだ{action}を選んでいません。**\n"
                "もう一度「🌅 朝を迎える」を押すと、未行動のまま朝を迎えます。"
            )

        was_confirmed = state.morning_confirmed
        was_event_set = state.morning_ready_event.is_set()
        state.morning_ready_ids.add(member.id)
        release_morning = self._check_morning_ready()
        try:
            await self._persist_room_state()
        except Exception as e:
            state.morning_ready_ids.discard(member.id)
            state.morning_confirmed = was_confirmed
            if not was_event_set:
                state.morning_ready_event.clear()
            log.exception(f"朝宣言の保存に失敗: {e}")
            return "", "❌ 宣言を保存できませんでした。もう一度お試しください。"
        if release_morning:
            state.morning_ready_event.set()
        return self._morning_feedback_text(declared=True), None

    async def force_morning(self, member: discord.Member) -> tuple[str, Optional[str]]:
        """GMによる強制的な夜明け (AFKで止まったままにならないための逃げ道)"""
        state = self.state
        if member.id != state.gm_id:
            return "", "GMのみ操作可能です。"
        if state.morning_ready_event.is_set():
            # 連打・パネルとGMパネルの二重操作で告知が重複しないようにする
            return "", "🌅 既に夜明けが確定しています。"
        if not self.night_actions_open():
            return "", "⏳ 現在は夜フェーズではありません。"
        if state.paused:
            # 停止中に朝にすると、再開直後に朝が流れて誰も追えない
            return "", "⏸️ 一時停止中です。先に「再開」を押してください。"
        state.morning_confirmed = True
        try:
            await self._persist_room_state()
        except Exception as e:
            state.morning_confirmed = False
            log.exception(f"GM強制夜明けの保存に失敗: {e}")
            return "", "❌ 夜明けを保存できませんでした。もう一度お試しください。"
        state.morning_ready_event.set()
        await self._safe_village_send("⏭️ **GMの操作で朝を迎えます。**")
        return "🌅 **GMの操作で朝を迎えました。**", None

    async def force_prep_complete(self, member: discord.Member) -> tuple[str, Optional[str]]:
        """GMが確認付きで役職確認待ちを締め切る運用上の逃げ道。"""
        state = self.state
        if member.id != state.gm_id:
            return "", "GMのみ操作可能です。"
        if state.prep_ready_event.is_set() or state.prep_confirmed:
            return "", "▶️ 既に役職確認が確定しています。"
        if not self.prep_actions_open():
            return "", "⏳ 現在は役職確認フェーズではありません。"
        if state.paused:
            return "", "⏸️ 一時停止中です。先に「再開」を押してください。"
        state.prep_confirmed = True
        try:
            await self._persist_room_state()
        except Exception as e:
            state.prep_confirmed = False
            log.exception(f"GM役職確認締切の保存に失敗: {e}")
            return "", "❌ 役職確認の締切を保存できませんでした。もう一度お試しください。"
        state.prep_ready_event.set()
        await self._safe_village_send(
            "⏭️ **GMの操作で役職確認を締め切り、議論を開始します。**"
        )
        return "▶️ **役職確認を締め切りました。**", None

    def _check_morning_ready(self) -> bool:
        """生存者全員が宣言済みなら確定状態にする。

        Eventは呼び出し先がこの状態を永続化した後にだけsetする。
        DB保存中に待機中のゲームループが先へ進むのを防ぐ。
        """
        state = self.state
        if not self.night_actions_open():
            return False
        required = self._morning_required_ids()
        if not required or required <= state.morning_ready_ids:
            state.morning_confirmed = True
            return True
        return False

    # ============================================================
    # 役職確認の宣言 (初日はここが揃うまで議論に入らない)
    # ============================================================

    def prep_actions_open(self) -> bool:
        """役職確認の宣言を受け付けるか。

        夜の night_actions_open と同じく一時停止中も受け付ける
        (止まっている間に押せないと、再開後に押し直しが必要になる)。
        """
        state = self.state
        return (
            self._effective_phase() == Phase.PREPARATION
            and not state.ending
            and state.pending_winner is None
            and not state.prep_confirmed
            and not state.prep_ready_event.is_set()
        )

    def _prep_required_ids(self) -> set[int]:
        """「役職を確認した」を押す必要がある人 (参加者全員)"""
        return {p.user_id for p in self.state.alive_players()}

    def _prep_panel_content(self) -> str:
        state = self.state
        required = self._prep_required_ids()
        ready = len(required & state.prep_ready_ids)
        if required and ready >= len(required):
            return (
                "✅ **全員が役職を確認しました。議論を開始します。**\n"
                f"**{ready} / {len(required)}人**"
            )
        return (
            "📩 **役職を確認した**\n"
            "DMで届いた役職を確認したら押してください。\n"
            "**参加者全員が押すと議論が始まります。**\n"
            "押し直すと取り消せます。まだ準備できていないときは押さずにお待ちください。\n"
            f"現在 **{ready} / {len(required)}人**"
        )

    async def _post_prep_panel(self) -> None:
        """役職確認タイムの宣言パネルを #昼 に掲示する"""
        state = self.state
        self._prep_view = PrepReadyView(self)
        state.prep_panel_message = await self._safe_village_send(
            self._prep_panel_content(), view=self._prep_view
        )

    async def _close_prep_panel(self) -> None:
        """議論開始後にパネルのボタンを無効化する"""
        state = self.state
        view = self._prep_view
        msg = state.prep_panel_message
        self._prep_view = None
        state.prep_panel_message = None
        if view is None:
            return
        view.stop()
        if msg is None:
            return
        if all(item.disabled for item in view.children):
            return
        for item in view.children:
            item.disabled = True
        try:
            await self._discord_api_call(msg.edit, view=view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def toggle_prep_ready(self, member: discord.Member) -> tuple[str, Optional[str]]:
        """「役職を確認した」の宣言をトグルする。

        Returns:
            (パネルの新しい本文, エラー文字列) — エラー時は本文を使わない
        """
        state = self.state
        if state.prep_ready_event.is_set():
            return "", "▶️ まもなく議論が始まります。"
        if not self.prep_actions_open():
            return "", "⏳ 現在この操作はできません。"
        player = state.get_player(member.id)
        if player is None or not player.alive:
            return "", "⏳ 参加者だけが押せます。"

        if member.id in state.prep_ready_ids:
            state.prep_ready_ids.discard(member.id)
            try:
                await self._persist_room_state()
            except Exception as e:
                state.prep_ready_ids.add(member.id)
                log.exception(f"役職確認の取り消し保存に失敗: {e}")
                return "", "❌ 宣言の取り消しを保存できませんでした。もう一度お試しください。"
            return self._prep_panel_content(), None

        was_confirmed = state.prep_confirmed
        was_event_set = state.prep_ready_event.is_set()
        state.prep_ready_ids.add(member.id)
        release_prep = self._check_prep_ready()
        try:
            await self._persist_room_state()
        except Exception as e:
            state.prep_ready_ids.discard(member.id)
            state.prep_confirmed = was_confirmed
            if not was_event_set:
                state.prep_ready_event.clear()
            log.exception(f"役職確認の保存に失敗: {e}")
            return "", "❌ 宣言を保存できませんでした。もう一度お試しください。"
        if release_prep:
            state.prep_ready_event.set()
        return self._prep_panel_content(), None

    def _check_prep_ready(self) -> bool:
        """参加者全員が宣言済みなら確定状態にする。

        Eventは呼び出し先がこの状態を永続化した後にだけsetする
        (_check_morning_ready と同じ理由)。
        """
        state = self.state
        if not self.prep_actions_open():
            return False
        required = self._prep_required_ids()
        if not required or required <= state.prep_ready_ids:
            state.prep_confirmed = True
            return True
        return False

    async def _wait_for_prep_ready(self) -> None:
        """役職確認タイムの目安時間が切れた後、全員の宣言を待つ。

        時間切れでは自動的に議論へ進まない (夜の _wait_for_morning と同じ)。
        誰が押していないかは村へ出さず、人数だけ表示する。
        """
        state = self.state
        if state.prep_ready_event.is_set():
            return

        # パネルが出ていないと誰も議論を始められない
        if state.prep_panel_message is None:
            await self._post_prep_panel()
        if state.prep_panel_message is None:
            log.error(f"役職確認パネルを掲示できません ({state.room_name}): 自動で議論へ進みます")
            state.prep_confirmed = True
            try:
                await self._persist_room_state()
            except Exception:
                state.prep_confirmed = False
                raise
            state.prep_ready_event.set()
            await self._safe_village_send(
                "⚠️ 「役職を確認した」パネルを表示できないため、自動で議論を開始します。"
            )
            return

        not_ready = [
            player for player in state.alive_players()
            if player.user_id not in state.prep_ready_ids
        ]
        if not_ready:
            async def warn(player: Player) -> None:
                try:
                    await self._discord_api_call(
                        player.member.send,
                        "⚠️ **まだ「役職を確認した」を押していません。**\n"
                        "役職確認タイムの目安時間が終了しました。"
                        "#昼 のパネルを押すと議論が始まります。",
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"役職確認の催促DM送信失敗 ({player.display_name}): {e}")

            await asyncio.gather(*(warn(p) for p in not_ready))

        await self._safe_village_send(
            "⏳ **役職確認タイムの目安時間が終了しました。**\n"
            "参加者全員が **「📩 役職を確認した」** を押すと議論が始まります。"
        )
        await self._pausable_wait_forever(state.prep_ready_event)


    async def _process_night(self) -> Optional[int]:
        """夜の結果処理。襲撃が成功したらplayer_idを返す"""
        state = self.state
        await state.pause_event.wait()
        if state.night_resolved:
            return None

        old_night_resolved = state.night_resolved
        old_guard_previous = state.guard_previous
        old_last_guarded = state._last_guarded
        old_last_killed = state._last_killed
        old_action_log_len = len(state.action_log)

        try:
            # この印は襲撃死の alive=False と同じスナップショットへ保存される。
            # _execute_player 内のpersist後にクラッシュしても同じ夜を再解決しない。
            state.night_resolved = True
            state._last_killed = None
            state._last_guarded = False

            killed_id = None

            # 連続護衛判定もnight_resolved/襲撃死と同じ最初の
            # スナップショットに入れる。
            if state.guard_target and state.guard_target != -1:
                state.guard_previous = state.guard_target
            else:
                state.guard_previous = None

            if state.wolf_target and state.wolf_target != -1:
                if state.guard_target == state.wolf_target:
                    state._last_guarded = True
                    self.log_action(
                        "護衛成功", target=state.get_player(state.guard_target),
                        detail="襲撃を防いだ",
                    )
                else:
                    killed_id = state.wolf_target
                    await self._execute_player(killed_id, "襲撃")

            # 被害者がいない場合もresolved/護衛履歴を原子的に保存。
            if killed_id is None:
                if state._last_guarded:
                    peace_detail = "護衛成功"
                elif state.wolf_target == -1:
                    peace_detail = "噛みなし"
                else:
                    peace_detail = "襲撃なし"
                self.log_action("平和", detail=peace_detail)
                await self._persist_room_state()
            return killed_id
        except StateDurabilityError as e:
            if e.state_committed:
                # 死亡本体は保存済み。outbox側の安全停止なので
                # 夜解決を巻き戻さない。
                raise
            state.night_resolved = old_night_resolved
            state.guard_previous = old_guard_previous
            state._last_guarded = old_last_guarded
            state._last_killed = old_last_killed
            del state.action_log[old_action_log_len:]
            await self._stop_for_durability_error("夜解決状態の保存", e)
            raise
        except Exception as e:
            state.night_resolved = old_night_resolved
            state.guard_previous = old_guard_previous
            state._last_guarded = old_last_guarded
            state._last_killed = old_last_killed
            del state.action_log[old_action_log_len:]
            await self._stop_for_durability_error("夜解決状態の保存", e)
            raise StateDurabilityError("夜解決状態を保存できませんでした") from e

    # ============================================================
    # 朝のログ
    # ============================================================

    async def _morning_log(self) -> None:
        state = self.state
        await state.pause_event.wait()
        state.phase = Phase.MORNING
        await self._persist_room_state()
        # 夜明けSEは _night_phase の末尾 (夜が終わった瞬間) で鳴らす。
        # ここで二重に鳴らさない

        lines = []

        if state._last_executed:
            lines.append(f"☀️ 処刑: {state._last_executed.display_name}（役職は非公開）")

        # 護衛成功・噛みなし・未行動はすべて同じ文言に統一する
        # (理由を出し分けると護衛の生存状況などが村へ漏れるため)
        if state._last_killed:
            lines.append(f"🌙 襲撃: {state._last_killed.display_name}（死亡）")
        else:
            lines.append("🕊️ 平和な朝を迎えました")

        lines.append(f"\n現在の生存者: **{len(state.alive_players())}人**")

        embed = discord.Embed(
            title=f"🌅 {state.day_number}日目の朝",
            description="\n".join(lines),
            color=discord.Color.yellow(),
        )
        await self._safe_village_send(embed=embed)

        # ログクリア
        state._last_executed = None
        state._last_killed = None
        state._last_guarded = False

    # ============================================================
    # ゲーム終了
    # ============================================================

    async def _end_game(self, winner: Team) -> None:
        state = self.state
        if state.phase == Phase.GAME_OVER:
            # 退出起因の外部終了とゲームループの終了が競合した場合の二重実行防止
            return
        if state.ending:
            # stage payloadを同じrun_idへ二重に上書きしない。このフラグは
            # 最初のawaitより前に立つため、同一イベントループ上で原子的。
            return
        state.ending = True

        player_meta = [
            {
                "player_id": p.user_id,
                "role": p.role.value,
                "display_name": p.display_name,
            }
            for p in state.players.values()
        ]
        game_stats: Optional[dict] = None
        death_records: dict[int, dict] = {}
        try:
            game_stats, death_records = build_game_stats(
                state.action_log, player_meta, days=state.day_number,
            )
        except Exception as e:
            # 集計の不具合で勝敗のstage自体を失わない。
            log.exception(f"ゲーム統計の組み立てに失敗 (精算は継続): {e}")

        player_records = []
        for p in state.players.values():
            won = 1 if ROLE_TEAM[p.role] == winner else 0
            death = death_records.get(p.user_id, {})
            player_records.append({
                "player_id": p.user_id,
                "role": p.role.value,
                "team": ROLE_TEAM[p.role].value,
                "won": won,
                "died_on_day": death.get("died_on_day"),
                "death_cause": death.get("death_cause"),
                # 試合直前ランクはsettle直前に既存のbefore_rank_mapから補う。
                "rank_at_game": None,
                "rank_provisional": None,
            })

        before_rank_map = None
        rank_records: Optional[dict[int, dict]] = None
        rank_bucket: Optional[str] = None
        rank_snapshot_staged = False

        # 通常Viewを同期的に閉じ、終了意図をまず部屋スナップショットへ
        # 永続化する。stage成功直後にプロセスが落ちても、次回起動時は
        # pending_winnerを見てゲーム進行ではなく精算再試行だけを行う。
        state.pending_winner = winner
        state.paused = True
        if state.phase != Phase.PAUSED:
            state.phase_before_pause = state.phase
        state.phase = Phase.PAUSED
        state.pause_event.clear()
        try:
            await self._persist_room_state()
            staged_records = player_records
            staged_stats = game_stats
            if self.is_rated_room():
                # 最初のdurable stageより前に、その試合時点の表示ランクを
                # 捕捉する。stage直後にプロセスが落ちても、起動時回収で
                # rank_at_game/rank_bucketをNULLへ失わない。
                async with self.manager.rating_lock:
                    try:
                        before_rank_map = await database.get_current_rank_map(
                            state.guild.id
                        )
                    except Exception as e:
                        # ランクは付加情報。取得不能でも勝敗stageは止めない。
                        log.exception(f"精算前ランク取得失敗 (精算は継続): {e}")
                    if before_rank_map is not None:
                        rank_records = {
                            player_id: {
                                "rank_at_game": context.rank_name,
                                "rank_provisional": context.provisional,
                            }
                            for player_id in state.players
                            if (context := before_rank_map.get(player_id)) is not None
                        }
                        rank_bucket = build_rank_bucket(
                            before_rank_map, list(state.players),
                        )
                        staged_records = []
                        for record in player_records:
                            staged = dict(record)
                            rank = rank_records.get(int(record["player_id"]))
                            if rank is not None:
                                staged.update(rank)
                            staged_records.append(staged)
                        staged_stats = (
                            {**game_stats, "rank_bucket": rank_bucket}
                            if game_stats is not None else None
                        )
                        rank_snapshot_staged = True
                    await database.stage_game_settlement(
                        state.guild.id,
                        state.room_id,
                        state.game_run_id,
                        room_name=state.room_name,
                        rated=True,
                        winner_team=winner.value,
                        player_records=staged_records,
                        game_stats=staged_stats,
                        gm_id=state.gm_id,
                        base_room_id=state.room_id,
                        recruitment_id=state.recruitment_id,
                    )
            else:
                await database.stage_game_settlement(
                    state.guild.id,
                    state.room_id,
                    state.game_run_id,
                    room_name=state.room_name,
                    rated=False,
                    winner_team=winner.value,
                    player_records=staged_records,
                    game_stats=staged_stats,
                    gm_id=state.gm_id,
                    base_room_id=state.room_id,
                    recruitment_id=state.recruitment_id,
                )
        except Exception as e:
            log.exception(f"ゲーム結果の事前保存に失敗: {e}")
            # 結果を失ったままGAME_OVERへ進めない。GMパネルを残し、再試行可能な
            # 安全停止状態にする (DB復旧後に「再開」で_end_gameを再試行)。
            state.ending = False
            try:
                await self._persist_room_state()
            except Exception:
                log.exception("精算失敗後の停止状態も保存できませんでした")
            await self._safe_village_send(
                "⚠️ **ゲーム結果を保存できないため、安全停止しました。**\n"
                "DB復旧後にGMが「再開」を押すと精算を再試行します。"
            )
            return

        state.phase = Phase.GAME_OVER
        state.paused = False
        self._stop_all_game_views()
        # pending settlementのstage完了後にGAME_OVERを確定する。
        game_over_saved = False
        for delay in (0, 1, 2):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._persist_room_state()
                game_over_saved = True
                break
            except Exception as e:
                log.exception(f"GAME_OVER checkpoint保存失敗 (再試行中): {e}")
        if game_over_saved:
            state.pending_winner = None
        else:
            # settlement stageは既にdurableなので廃村にはしない。
            # この後のロビー初期化スナップショットでもう一度
            # 保存を試み、途中クラッシュ時は事前のpending_winner
            # スナップショットからGM再試行できる。
            log.error("GAME_OVER checkpointを3回保存できませんでしたが、settlement済みのため終了処理を継続します")

        # 掲示順は「進行ログ → 勝利陣営 + 全役職公開 → ランク変動」。
        # 結果とレート変動を最後にまとめ、長い進行ログで押し流されない
        # ようにする。ログを先に出しても、この時点で既にGAME_OVERへ移り
        # _stop_all_game_views で全UIを止めており、直後に役職を全公開
        # するため新たな情報漏洩にはならない。
        await self._post_action_log()

        # 全役職公開 (embedのtitleが勝利陣営、descriptionが役職一覧)
        role_lines = []
        for p in sorted(state.players.values(), key=lambda x: x.number):
            status = "✅" if p.alive else "💀"
            team_emoji = "🐺" if ROLE_TEAM[p.role] == Team.WOLF else "🏠"
            role_lines.append(
                f"{status} {p.display_name} — {team_emoji} {p.role.value}"
            )

        winner_emoji = "🐺" if winner == Team.WOLF else "🏠"
        embed = discord.Embed(
            title=f"{winner_emoji} **{winner.value}の勝利！**",
            description="\n".join(role_lines),
            color=discord.Color.red() if winner == Team.WOLF else discord.Color.green(),
        )
        await self._safe_village_send(embed=embed)

        game_id = None
        results = None
        settled = False
        # 短いDB busy/一時障害なら再起動を待たず回復する。
        # ランク対象卓はbefore取得からsettleまで同じ排他内に置き、
        # 別卓の精算が間に割り込んで昇降格比較がずれるのを防ぐ。
        for attempt, delay in enumerate((0, 1, 2), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                if self.is_rated_room():
                    async with self.manager.rating_lock:
                        if not rank_snapshot_staged:
                            try:
                                before_rank_map = await database.get_current_rank_map(state.guild.id)
                            except Exception as e:
                                before_rank_map = None
                                rank_records = None
                                rank_bucket = None
                                log.exception(f"精算前ランク取得失敗 (精算は継続): {e}")
                            if before_rank_map is not None:
                                rank_records = {
                                    player_id: {
                                        "rank_at_game": context.rank_name,
                                        "rank_provisional": context.provisional,
                                    }
                                    for player_id in state.players
                                    if (context := before_rank_map.get(player_id)) is not None
                                }
                                rank_bucket = build_rank_bucket(
                                    before_rank_map, list(state.players),
                                )
                                # 即時settleの直前に同じ排他内でstageへも保存する。
                                # この後にプロセスが落ち、起動時回収へ回っても
                                # 試合時ランクと中央値をNULLへ失わない。
                                staged_records = []
                                for record in player_records:
                                    staged = dict(record)
                                    rank = rank_records.get(int(record["player_id"]))
                                    if rank is not None:
                                        staged.update(rank)
                                    staged_records.append(staged)
                                staged_stats = (
                                    {**game_stats, "rank_bucket": rank_bucket}
                                    if game_stats is not None else None
                                )
                                await database.stage_game_settlement(
                                    state.guild.id,
                                    state.room_id,
                                    state.game_run_id,
                                    room_name=state.room_name,
                                    rated=True,
                                    winner_team=winner.value,
                                    player_records=staged_records,
                                    game_stats=staged_stats,
                                    gm_id=state.gm_id,
                                    base_room_id=state.room_id,
                                    recruitment_id=state.recruitment_id,
                                )
                                rank_snapshot_staged = True
                        game_id, results, _ = await database.settle_game_settlement(
                            state.guild.id, state.room_id, state.game_run_id,
                            rank_records=rank_records,
                            rank_bucket=rank_bucket,
                        )
                else:
                    game_id, _, _ = await database.settle_game_settlement(
                        state.guild.id, state.room_id, state.game_run_id
                    )
                settled = True
                break
            except Exception as e:
                log.exception(
                    f"ゲーム結果の精算失敗 ({attempt}/3回目): {e}"
                )

        if not settled:
            # pendingはDBに残るため、起動時の冪等再精算へ委ねる。
            # 運用者がログを見ないまま気付かない事故を避けるため、
            # GMが見ている#昼にも明示する。
            await self._safe_village_send(
                "⚠️ **戦績・レートの即時精算に3回失敗しました。**\n"
                "結果は精算待ちキューに保存済みで、Bot起動時に自動再試行します。"
            )

        if results is not None:
            try:
                await self._post_rating_results(results, before_rank_map=before_rank_map)
            except Exception as e:
                log.exception(f"レーティング通知失敗: {e}")
        elif settled and not self.is_rated_room():
            await self._safe_village_send(
                "この卓はランク対象外です。レート、ランク、ランクロール、今季戦績は変動しません。"
            )

        # 通常の勝敗精算を確定してから、推薦だけを独立した冪等処理として受け付ける。
        # バックグラウンドにすることで、ニックネーム/VC復元と次村受付を3分止めない。
        if settled and self.is_rated_room() and game_id is not None:
            recommendation_voters = self._postgame_recommendation_voters(state)
            if recommendation_voters:
                try:
                    # タスク起動前に行を作り、直後のシーズンリセットとの隙間を塞ぐ。
                    await database.create_game_recommendation_ballots(
                        int(game_id),
                        state.guild.id,
                        recommendation_voters,
                        timeout_seconds=POSTGAME_RECOMMENDATION_TIMEOUT,
                    )
                except Exception as e:
                    log.exception(f"終了後推薦の受付作成に失敗: {e}")
                    await self._safe_village_send(
                        "⚠️ 終了後推薦の受付を開始できませんでした。ログを確認してください。"
                    )
                else:
                    self.manager.spawn_bg_task(
                        self._run_postgame_recommendations(
                            state, int(game_id), recommendation_voters
                        )
                    )

        cleanup_progress = await self._safe_village_send(
            "⏳ **ゲーム終了処理中**\n"
            "ニックネーム・ミュート・VC権限を順番に復元しています。"
        )

        # ニックネーム復元
        await self._restore_nicknames()

        # ゲーム用の一時ロール・VC個別権限を全て撤去
        await self._teardown_game_roles_and_perms()
        await self._safe_timer_edit(
            cleanup_progress,
            "✅ **ゲーム終了処理が完了しました。**\nニックネームとVC設定を復元しました。",
        )

        # チャンネル削除予約 (#昼 / #霊界)
        game_channels = [ch for ch in (state.village_channel, state.spirit_channel) if ch]

        await self._safe_village_send(
            f"🕐 このチャンネルは {CHANNEL_DELETE_DELAY}秒後 に削除されます。"
        )

        async def delete_channels():
            await asyncio.sleep(CHANNEL_DELETE_DELAY)
            for ch in game_channels:
                try:
                    await ch.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"#{ch.name} チャンネル削除失敗: {e}")

        self.manager.spawn_bg_task(delete_channels())

        # 次村用に直前のメンバーを記録
        if state.players:
            self.last_game_roster = list(state.players.keys())
            self.last_game_gm = state.gm_id

        # ロビーリセット
        room_id = state.room_id
        room_name = state.room_name
        self.state = GameState()
        self.state.room_id = room_id
        self.state.room_name = room_name
        self.state.guild = state.guild
        self.state.category = state.category
        self.state.lobby_channel = state.lobby_channel
        self.state.stats_channel = state.stats_channel
        self.state.voice_channel = state.voice_channel
        await self._post_lobby_ui()
        for attempt, delay in enumerate((0, 1, 2), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._persist_room_state()
                break
            except Exception as e:
                log.exception(f"ロビー初期化状態の保存失敗 ({attempt}/3): {e}")

    # ============================================================
    # レーティング処理
    # ============================================================

    @staticmethod
    def _postgame_recommendation_voters(state: GameState) -> set[int]:
        """霊媒師・初日処刑者・初夜襲撃死者を重複なしで返す。"""
        voters = {
            player.user_id
            for player in state.players.values()
            if player.role == Role.MEDIUM
        }
        if state.day1_executed_id in state.players:
            voters.add(int(state.day1_executed_id))
        if state.night1_killed_id in state.players:
            voters.add(int(state.night1_killed_id))
        return voters

    async def _run_postgame_recommendations(
        self,
        finished_state: GameState,
        game_id: int,
        voter_ids: set[int],
    ) -> None:
        """推薦DMを送り、全員確定または3分経過後に匿名で集計する。"""
        guild = finished_state.guild
        if guild is None:
            return
        pending = set(voter_ids)
        all_done = asyncio.Event()

        def on_confirmed(voter_id: int) -> None:
            pending.discard(voter_id)
            if not pending:
                all_done.set()

        candidates = list(finished_state.players.values())
        for voter_id in sorted(voter_ids):
            voter = finished_state.players.get(voter_id)
            if voter is None:
                pending.discard(voter_id)
                await database.cancel_game_recommendation_ballot(
                    game_id, guild.id, voter_id
                )
                continue
            view = PostgameRecommendationView(
                game_id=game_id,
                guild_id=guild.id,
                voter_id=voter_id,
                candidates=candidates,
                timeout=POSTGAME_RECOMMENDATION_TIMEOUT,
                on_confirmed=on_confirmed,
            )
            try:
                message = await self._discord_api_call(
                    voter.member.send,
                    "👏 **終了後推薦（+1レート）**\n"
                    "霊媒師・初日の処刑者・初夜の襲撃死者には、"
                    "この試合の参加者1人へ+1を贈る推薦票があります。\n"
                    "自分自身は選べません。条件が重なっても1人1票です。\n"
                    f"受付は **{POSTGAME_RECOMMENDATION_TIMEOUT // 60}分間**。"
                    "推薦者名は公開されません。",
                    view=view,
                )
                view.message = message
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"終了後推薦DM送信失敗 (ID:{voter_id}): {e}")
                pending.discard(voter_id)
                await database.cancel_game_recommendation_ballot(
                    game_id, guild.id, voter_id
                )

        if not pending:
            all_done.set()
        try:
            await asyncio.wait_for(
                all_done.wait(), timeout=POSTGAME_RECOMMENDATION_TIMEOUT
            )
        except TimeoutError:
            pass

        async with self.manager.rating_lock:
            before_rank_map = await database.get_current_rank_map(guild.id)
            results = await database.finalize_game_recommendations(
                game_id, guild.id, close_pending=True
            )
            after_rank_map = await database.get_current_rank_map(guild.id)
        if not results:
            return

        roles_map = await self.manager._ensure_rank_roles(guild)
        changed_rank_ids = {
            player_id
            for player_id, after_ctx in after_rank_map.items()
            if player_id not in before_rank_map
            or before_rank_map[player_id].rank_name != after_ctx.rank_name
        }
        for player_id in changed_rank_ids:
            member = guild.get_member(player_id)
            rank_ctx = after_rank_map.get(player_id)
            if member is None or rank_ctx is None:
                continue
            try:
                await self.manager._sync_rank_role(
                    member, rank_ctx.rank_name, roles_map=roles_map
                )
            except Exception as e:
                log.warning(f"推薦後ランクロール同期失敗 (ID:{player_id}): {e}")

        lines = []
        for result in results:
            player = finished_state.players.get(result["player_id"])
            name = player.display_name if player else f"ID:{result['player_id']}"
            lines.append(
                f"👏 **{name}** +{result['bonus']} "
                f"({result['rating_before']} → **{result['rating_after']}**)"
            )
        embed = discord.Embed(
            title="終了後推薦の結果",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="推薦者名は非公開です。推薦は1票につきレート+1。")
        channel = finished_state.village_channel
        if channel is not None:
            try:
                await channel.send(embed=embed)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"終了後推薦結果の投稿失敗: {e}")

    async def _post_rating_results(
        self,
        results: list[dict],
        before_rank_map: Optional[dict[int, rating_lib.RankContext]] = None,
    ) -> None:
        """
        既にDBに保存済みの計算結果を元に、
        埋め込み通知 + ロール付与 + 昇降格表示を行う

        DBへの書き込みはここでは行わない (アトミック保存は呼び出し側)
        """
        state = self.state
        guild = state.guild
        if not guild:
            return

        after_rank_map = await database.get_current_rank_map(guild.id)
        roles_map = await self.manager._ensure_rank_roles(guild)

        # 結果表示用 + 昇降格収集
        result_lines = []
        promotions = []  # (member, old_rank_name, new_rank_name, promoted_bool)
        for r in sorted(results, key=lambda x: -x["delta"]):
            pid = r["player_id"]
            player = state.players.get(pid)
            if not player:
                continue
            before = r["rating_before"]
            after = r["rating_after"]
            delta = r["delta"]
            elo_delta = r["elo_delta"]
            bonus = r["bonus"]
            recommendation_bonus = r.get("recommendation_bonus", 0)
            sign = "+" if delta >= 0 else ""
            elo_sign = "+" if elo_delta >= 0 else ""
            detail_txt = ""
            if bonus > 0:
                detail_txt = f" (本体{elo_sign}{elo_delta} / +{bonus}🎁)"
            if recommendation_bonus > 0:
                detail_txt = (
                    f" (本体{elo_sign}{elo_delta} / 勝利+{bonus} / "
                    f"推薦+{recommendation_bonus})"
                )
            after_rank = after_rank_map.get(pid)
            new_rank_name = after_rank.rank_name if after_rank else "ブロンズ"
            new_emoji = after_rank.emoji if after_rank else "🥉"
            result_lines.append(
                f"{new_emoji} {player.display_name}: "
                f"{before} → **{after}** ({sign}{delta}{detail_txt}) [{new_rank_name}]"
            )

            old_rank_name = before_rank_map[pid].rank_name if before_rank_map and pid in before_rank_map else new_rank_name

            if rating_lib.is_promoted(old_rank_name, new_rank_name):
                promotions.append((
                    player.member,
                    old_rank_name,
                    new_rank_name,
                    True,
                ))
            elif rating_lib.is_demoted(old_rank_name, new_rank_name):
                promotions.append((
                    player.member,
                    old_rank_name,
                    new_rank_name,
                    False,
                ))

        # ランクロール同期は「参加者」＋「ランクが実際に変わった人」だけに限定する。
        # 全員ループはDiscord APIへの負荷が高く、レート制限の原因になりやすい。
        target_pids: set[int] = set(state.players.keys())
        if before_rank_map is not None:
            for pid, after_ctx in after_rank_map.items():
                before_ctx = before_rank_map.get(pid)
                if before_ctx is None or before_ctx.rank_name != after_ctx.rank_name:
                    target_pids.add(pid)
        for pid in target_pids:
            member = guild.get_member(pid)
            rank_ctx = after_rank_map.get(pid)
            if member is None or rank_ctx is None:
                continue
            try:
                await self.manager._sync_rank_role(member, rank_ctx.rank_name, roles_map=roles_map)
            except Exception as e:
                log.warning(f"ロール付与失敗 ({member.display_name}): {e}")

        # レート変動エンベッド
        if state.village_channel and result_lines:
            embed = discord.Embed(
                title="📈 レーティング変動",
                description="\n".join(result_lines),
                color=discord.Color.blue(),
            )
            await self._safe_village_send(embed=embed)

        # 昇降格通知
        if promotions and state.village_channel:
            promo_lines = []
            for member, old_name, new_name, is_up in promotions:
                if is_up:
                    promo_lines.append(
                        f"🎉 **{member.display_name}** が "
                        f"{old_name} → **{new_name}** に昇格！"
                    )
                else:
                    promo_lines.append(
                        f"⬇️ {member.display_name} が "
                        f"{old_name} → {new_name} に降格..."
                    )
            embed = discord.Embed(
                title="🏅 ランク変動",
                description="\n".join(promo_lines),
                color=discord.Color.gold(),
            )
            await self._safe_village_send(embed=embed)

    # ============================================================
    # GM操作
    # ============================================================

    def _is_game_in_progress(self) -> bool:
        """ゲーム進行中。開始処理中・再起動復元後の停止状態も含む。"""
        state = self.state
        if state.phase in (Phase.LOBBY, Phase.GAME_OVER):
            return False
        if state.recovered_from_restart:
            return True
        if state.phase == Phase.PREPARATION or state.phase_before_pause == Phase.PREPARATION:
            return True
        return state.game_task is not None and not state.game_task.done()

    async def pause_game(self, reason: Optional[str] = None) -> str:
        state = self.state
        if not self._is_game_in_progress():
            return "進行中のゲームがありません。"
        if state.paused:
            return "既に一時停止中です。"
        state.paused = True
        state.phase_before_pause = state.phase
        state.phase = Phase.PAUSED
        state.pause_event.clear()
        await self._persist_room_state()

        # タイマーだけでなく会話も停止する。切断者不在のまま議論が続くのを防ぐ。
        changed = await self._sync_server_mutes(set())
        await self._await_mute_applied(changed, MUTE_GRACE_TIME)

        text = "⏸️ **ゲームが一時停止されました。**"
        if reason:
            text += f"\n{reason}"
        await self._safe_village_send(text)
        return "⏸️ 一時停止しました。"

    async def resume_game(self) -> str:
        async with self.resume_lock:
            return await self._resume_game_locked()

    async def _resume_game_locked(self) -> str:
        """GM再開を直列化した本体。復元タスクの二重起動を防ぐ。"""
        state = self.state
        # 勝敗確定後、DBのpending保存に失敗して安全停止した
        # ゲームは、通常フェーズに戻さず終了処理だけを再試行する。
        # 再起動復元後もpending_winnerがスナップショットに残るため
        # 同じ経路で回復できる。
        if state.pending_winner is not None:
            if state.game_task is not None and not state.game_task.done():
                return "⏳ 終了処理の停止完了待ちです。数秒後にもう一度押してください。"
            winner = state.pending_winner
            state.paused = False
            state.phase = state.phase_before_pause or Phase.NIGHT
            state.pause_event.set()
            state.game_task = asyncio.create_task(self._end_game(winner))
            await self._safe_village_send(
                "▶️ **保存に失敗したゲーム結果の精算を再試行します。**"
            )
            return "▶️ ゲーム結果の精算を再試行します。"

        if state.recovered_from_restart and (
            state.game_task is None or state.game_task.done()
        ):
            resume_phase = state.recovery_phase or state.phase_before_pause or Phase.DAY_DISCUSSION
            if state.pending_death_effects:
                await self._reconcile_pending_death_effects()
                if state.pending_death_effects:
                    return "⚠️ 死亡通知の復元状態を保存できていません。DB復旧後に再度「再開」を押してください。"
            state.paused = False
            state.phase = resume_phase
            changed = await self._sync_server_mutes(self._current_speaker_ids())
            if not await self._await_mute_applied(changed, MUTE_GRACE_TIME):
                await self._restore_pause_after_failed_mute_resume(
                    resume_phase, "復元再開時の発言制御"
                )
                return "⚠️ 発言制御を確認できないため停止を継続します。権限確認後に再度「再開」を押してください。"
            await self._persist_room_state()
            state.pause_event.set()
            # create_taskの実行開始を待たず、この呼び出しが復元再開を占有する。
            # 直後の二重操作がdone済み旧taskを見て二本目を作らないようにする。
            state.recovered_from_restart = False
            if resume_phase == Phase.PREPARATION:
                state.game_task = asyncio.create_task(self._resume_preparation())
                await self._safe_village_send(
                    "▶️ **中断した役職配布・開始処理を同じ内容で再開します。**"
                )
            else:
                state.game_task = asyncio.create_task(self._resume_recovered_game())
                await self._safe_village_send("▶️ **復元ゲームを再開します。現在フェーズを最初から再開します。**")
            return "▶️ 復元ゲームを再開しました。"

        if not state.paused:
            if not self._is_game_in_progress():
                return "進行中のゲームがありません。"
            return "一時停止していません。"
        state.paused = False
        # フェーズを戻すのは state.phase が PAUSED のままのときだけ。
        # 一時停止はフェーズ境界の非pausable区間 (遺言終了〜夜入り、
        # ミュート整列中など) にも差し込まれる。その場合ゲームループは
        # 止まらずに進み、次のフェーズ関数が冒頭で state.phase を上書きする。
        # ここで無条件に phase_before_pause を復元すると、実際の進行位置と
        # state.phase が恒久的にズレ、夜なのに night_actions_open() が False に
        # なって占い・護衛・朝の宣言・GMの強制夜明けまで全て拒否され、
        # 廃村以外に復帰できなくなる。
        if state.phase == Phase.PAUSED and state.phase_before_pause:
            state.phase = state.phase_before_pause

        # 一時停止中に入退室したメンバーのミュート状態をフェーズに合わせ直す
        changed = await self._sync_server_mutes(self._current_speaker_ids())
        if not await self._await_mute_applied(changed, MUTE_GRACE_TIME):
            failed_phase = self._effective_phase() or state.phase_before_pause or Phase.DAY_DISCUSSION
            await self._restore_pause_after_failed_mute_resume(
                failed_phase, "再開時の発言制御"
            )
            return "⚠️ 発言制御を確認できないため停止を継続します。権限確認後に再度「再開」を押してください。"
        await self._persist_room_state()
        # 発言状態が反映された後でタイマーを再開する。
        state.pause_event.set()

        text = "▶️ **ゲームが再開されました。**"
        if state.disconnected_players:
            waiting = ", ".join(
                p.display_name
                for p in by_number(state.players.values())
                if p.user_id in state.disconnected_players
            )
            # GM判断での未復帰のまま再開も許可する (復帰待ちの記録だけ残す)
            text += f"\n⚠️ 未復帰のまま再開: {waiting}"
        await self._safe_village_send(text)
        return "▶️ 再開しました。"

    async def _resume_preparation(self) -> None:
        """PREPARATION中断を保存済みの番号/役職/初日白で再開する。"""
        state = self.state
        try:
            state.recovered_from_restart = False
            state.recovery_phase = None
            state.phase = Phase.PREPARATION
            if any(p.role is None or not p.number for p in state.players.values()):
                await self._safe_village_send(
                    "⚠️ 開始意図の役職または番号が保存されていないため、安全に復元できません。"
                )
                self.manager.spawn_bg_task(self.force_end("開始フェーズの復元データ不足により中断"))
                return

            guild = state.guild
            if guild is None:
                raise StateDurabilityError("サーバー情報がありません")

            # 改名は同じ値への冪等再適用。muteはresume_gameで
            # 既に全員ミュートへ同期済み。
            for user_id in state.original_nicknames:
                member = guild.get_member(user_id)
                if member is None or member.bot:
                    continue
                player = state.get_player(user_id)
                if user_id == state.gm_id and player is None:
                    continue
                target_nick = player.display_name[:32] if player else "観戦者"
                if member.nick == target_nick:
                    continue
                try:
                    await self._paced_discord_api_call(member.edit, nick=target_nick)
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"開始復元の改名失敗 ({member.display_name}): {e}")

            dm_failed = await self._send_role_dms()
            if dm_failed:
                failed_names = ", ".join(member.display_name for member in dm_failed)
                await self._safe_village_send(
                    f"役職DMを再送できない参加者がいるため中断します: {failed_names}"
                )
                self.manager.spawn_bg_task(self.force_end("役職DM復元失敗により中断"))
                return

            await self._assign_alive_role()
            await self._restrict_vc_for_game()
            await self._grant_alive_vc_access()
            await self._mute_all()
            await self._persist_room_state()
            await self._safe_village_send(
                "♻️ **保存済みの役職配布内容で開始処理を復元しました。**"
            )
            await self._game_loop()
        except asyncio.CancelledError:
            log.info("開始復元タスクがキャンセルされました")
        except Exception as e:
            log.exception(f"開始フェーズ復元エラー: {e}")
            await self._stop_for_durability_error("開始フェーズの復元", e)

    async def _resume_recovered_game(self) -> None:
        state = self.state
        try:
            resume_phase = state.recovery_phase or state.phase_before_pause or Phase.DAY_DISCUSSION
            state.recovered_from_restart = False
            state.recovery_phase = None

            async def finish_night(*, resume_existing: bool) -> bool:
                """夜を解決して朝へ進む。勝敗確定ならTrue。"""
                if not state.night_resolved:
                    # 夜明け確定済みなら保存済み行動をそのまま解決する。
                    # 未確定なら同じnight_generationのUIを再掲示する。
                    if not state.morning_confirmed:
                        await self._night_phase(resume_existing=resume_existing)
                    await self._process_night()
                winner = state.check_win()
                if winner:
                    await self._end_game(winner)
                    return True
                state.day_number += 1
                state.day_generation += 1
                state.day_execution_resolved = False
                state.day_executed_target = None
                state.pending_execution_target = None
                await self._morning_log()
                return False

            async def finish_recovered_vote(executed_id: Optional[int]) -> bool:
                """復元した投票結果を処理し、夜まで進める。"""
                if executed_id:
                    await self._checkpoint_pending_execution(executed_id)
                    executed_player = state.get_player(executed_id)
                    if executed_player and executed_player.alive:
                        await self._last_will(executed_player)
                    await self._execute_player(executed_id, "処刑")
                    state.day_execution_resolved = True
                    state.day_executed_target = executed_id
                    await self._persist_room_state()
                    winner = state.check_win()
                    if winner:
                        await self._end_game(winner)
                        return True
                else:
                    state.day_execution_resolved = True
                    state.day_executed_target = None
                    state.pending_execution_target = None
                    await self._persist_room_state()
                return await finish_night(resume_existing=False)

            if resume_phase == Phase.NIGHT:
                await self._safe_village_send(
                    f"♻️ **{state.day_number}日目の夜フェーズ** を保存状態から再開します。"
                )
                if await finish_night(resume_existing=True):
                    return
            elif resume_phase == Phase.MORNING:
                await self._safe_village_send(
                    f"♻️ **{state.day_number}日目の朝** から再開します。"
                )
                await self._morning_log()
            elif resume_phase == Phase.DAY_VOTE and not state.day_execution_resolved:
                await self._safe_village_send(
                    f"♻️ **{state.day_number}日目の投票フェーズ** を投票済み状態から再開します。"
                )
                if await finish_recovered_vote(
                    await self._day_vote(resume_existing=True)
                ):
                    return
            elif resume_phase in (Phase.DAY_RUNOFF_SPEECH, Phase.DAY_RUNOFF_VOTE) and not state.day_execution_resolved:
                # ランダム処刑の抽選直後に落ちた場合は、
                # 告知済み/未告知にかかわらず保存済み対象を使う。
                # ここで決戦投票をやり直すと別人を再抽選し得る。
                if state.pending_execution_target is not None:
                    await self._safe_village_send(
                        "♻️ **保存済みの決戦投票結果** から処刑処理を再開します。"
                    )
                    if await finish_recovered_vote(state.pending_execution_target):
                        return
                    # finish_recovered_voteは処理後に夜まで進む。
                    # 以下の決戦投票再開には入らない。
                    resume_phase = Phase.NIGHT
                elif not state.runoff_candidates:
                    error = StateDurabilityError("決戦投票候補が保存されていません")
                    await self._stop_for_durability_error("決戦投票の復元", error)
                    raise error
                else:
                    resume_vote = resume_phase == Phase.DAY_RUNOFF_VOTE
                    await self._safe_village_send(
                        f"♻️ **{state.day_number}日目の決戦投票** を"
                        + ("投票済み状態から" if resume_vote else "弁明から")
                        + "再開します。"
                    )
                    if await finish_recovered_vote(
                        await self._runoff(state.runoff_candidates, resume_vote=resume_vote)
                    ):
                        return
            elif resume_phase == Phase.DAY_LAST_WILL and not state.day_execution_resolved:
                if state.pending_execution_target is None:
                    error = StateDurabilityError("確定済みの処刑対象が保存されていません")
                    await self._stop_for_durability_error("遺言フェーズの復元", error)
                    raise error
                if await finish_recovered_vote(state.pending_execution_target):
                    return
            else:
                if state.day_execution_resolved:
                    await self._safe_village_send(
                        f"♻️ **{state.day_number}日目の処刑処理は完了済み**のため、夜から再開します。"
                    )
                    if await finish_night(resume_existing=False):
                        return
                else:
                    await self._safe_village_send(
                        f"♻️ **{state.day_number}日目の昼フェーズ** を最初から再開します。"
                    )

            while True:
                await self._day_discussion()
                executed_id = await self._day_vote()
                if executed_id:
                    await self._checkpoint_pending_execution(executed_id)
                    executed_player = state.get_player(executed_id)
                    if executed_player and executed_player.alive:
                        await self._last_will(executed_player)
                    await self._execute_player(executed_id, "処刑")
                    state.day_execution_resolved = True
                    state.day_executed_target = executed_id
                    await self._persist_room_state()
                    winner = state.check_win()
                    if winner:
                        await self._end_game(winner)
                        return
                else:
                    state.day_execution_resolved = True
                    state.day_executed_target = None
                    state.pending_execution_target = None
                    await self._persist_room_state()

                if await finish_night(resume_existing=False):
                    return
        except asyncio.CancelledError:
            log.info("復元ゲームタスクがキャンセルされました")
        except StateDurabilityError as e:
            log.error(f"永続化失敗のため復元ゲームを安全停止: {e}")
        except Exception as e:
            log.exception(f"復元ゲームループエラー: {e}")
            self.manager.spawn_bg_task(self.force_end("復元ゲーム中にエラーが発生したため中断"))

    async def _eliminate_player_mid_game(self, player: Player, reason: str) -> None:
        """ゲーム中のプレイヤーを死亡扱いで除外する (サーバー退出 / GM除外)。

        役職は公開しない。除外で勝敗が決した場合はゲームループを止めて終了処理を行う。
        """
        state = self.state
        if state.phase in (Phase.LOBBY, Phase.GAME_OVER):
            # 廃村/終了処理と競合した場合は何もしない
            return
        if not player.alive:
            return
        old_votes = dict(state.votes)
        old_disconnected = set(state.disconnected_players)
        old_ready = set(state.morning_ready_ids)
        old_morning_confirmed = state.morning_confirmed
        old_morning_event = state.morning_ready_event.is_set()
        old_prep_ready = set(state.prep_ready_ids)
        old_prep_confirmed = state.prep_confirmed
        old_prep_event = state.prep_ready_event.is_set()
        old_night_complete = state.night_complete_event.is_set()
        old_vote_complete = state.vote_complete_event.is_set()
        old_action_log_len = len(state.action_log)
        self.log_action(
            "死亡", target=player,
            detail=f"除外 / 役職={player.role.value if player.role else '不明'}",
        )
        player.alive = False
        state.votes.pop(player.user_id, None)
        # 復帰待ちのまま除外された場合は待ちリストからも外す
        state.disconnected_players.discard(player.user_id)
        # 除外者に入っていた票も無効化する。残すと集計で死亡者が最多得票になり、
        # _execute_player の死亡済みガードで処刑が「無言で不発」になる。
        # (票を失った人は投票し直せる)
        for voter_id in [v for v, t in state.votes.items() if t == player.user_id]:
            del state.votes[voter_id]

        # 弁明/遺言フェーズ中に本人が除外された場合は、タイムアウトを
        # 待たずに即終了する
        release_speech = (
            state.phase in (Phase.DAY_RUNOFF_SPEECH, Phase.DAY_LAST_WILL)
            and state.current_speaker_id == player.user_id
        )

        # 夜の未行動警告と「朝を迎える」宣言は、除外で条件が揃った可能性を再チェック
        # (除外された人の宣言・行動を待ち続けないようにする)
        state.morning_ready_ids.discard(player.user_id)
        release_night_complete = (
            self.night_actions_open() and not self._pending_night_actions()
        )
        release_morning = self._check_morning_ready()

        # 役職確認タイム中の除外も同様に、除外者の宣言を待ち続けない
        state.prep_ready_ids.discard(player.user_id)
        release_prep = self._check_prep_ready()

        # 投票フェーズ中なら、除外により「残り生存者全員投票済み」になった可能性を再チェック
        # (VoteViewのtotal_votersはフェーズ開始時点で固定のため、ここで補完する)
        release_vote_complete = False
        if state.phase in (Phase.DAY_VOTE, Phase.DAY_RUNOFF_VOTE):
            alive_ids = {p.user_id for p in state.alive_players()}
            release_vote_complete = bool(
                alive_ids and alive_ids.issubset(state.votes.keys())
            )

        effect = {
            "event_id": f"{state.game_run_id}:除外:{state.day_generation}:{state.night_generation}:{player.user_id}",
            "player_id": player.user_id,
            "method": "除外",
            "reason": reason,
        }
        if not any(item.get("event_id") == effect["event_id"] for item in state.pending_death_effects):
            state.pending_death_effects.append(effect)
        try:
            # alive/votes/ready/outboxをDiscord改名・通知より必ず先に保存。
            await self._persist_room_state()
        except Exception as e:
            player.alive = True
            state.votes = old_votes
            state.disconnected_players = old_disconnected
            state.morning_ready_ids = old_ready
            state.morning_confirmed = old_morning_confirmed
            if old_morning_event:
                state.morning_ready_event.set()
            else:
                state.morning_ready_event.clear()
            state.prep_ready_ids = old_prep_ready
            state.prep_confirmed = old_prep_confirmed
            if old_prep_event:
                state.prep_ready_event.set()
            else:
                state.prep_ready_event.clear()
            if old_night_complete:
                state.night_complete_event.set()
            else:
                state.night_complete_event.clear()
            if old_vote_complete:
                state.vote_complete_event.set()
            else:
                state.vote_complete_event.clear()
            state.pending_death_effects = [
                item for item in state.pending_death_effects
                if item.get("event_id") != effect["event_id"]
            ]
            del state.action_log[old_action_log_len:]
            await self._stop_for_durability_error("プレイヤー除外状態の保存", e)
            return

        await self._apply_death_effect(effect)

        winner = state.check_win()
        if winner and state.phase != Phase.GAME_OVER:
            await self._finish_game_externally(winner)
            return

        # 待機側は、死亡本体だけでなくDiscord副作用outboxの
        # 除去保存まで成功し、かつ勝敗確定でない場合にだけ解放する。
        # それより前にsetすると、副作用保存失敗の安全停止中や
        # 外部終了前にゲームループが先へ進み得る。
        if release_speech:
            state.speech_done_event.set()
        if release_night_complete:
            state.night_complete_event.set()
        if release_morning:
            state.morning_ready_event.set()
        if release_prep:
            state.prep_ready_event.set()
        if release_vote_complete:
            state.vote_complete_event.set()

    async def _finish_game_externally(self, winner: Team) -> None:
        """ゲームループの外側から勝敗を確定して終了する (退出/除外起因)"""
        state = self.state
        # 夜UIは即座に閉じるが、GMパネルは_end_gameの事前保存が
        # 成功するまで残す。保存失敗で安全停止した際に、GMが
        # 「再開」から精算を再試行できなくなるのを防ぐ。
        self._stop_night_views()
        if state.paused:
            state.paused = False
            state.pause_event.set()
        if state.game_task and not state.game_task.done():
            state.game_task.cancel()
            try:
                await state.game_task
            except (asyncio.CancelledError, Exception):
                pass
        # キャンセル中にループ側で終了処理が走り切った場合は何もしない
        if state.phase == Phase.GAME_OVER:
            return
        await self._end_game(winner)

    async def force_end(self, reason: str = "廃村") -> None:
        state = self.state

        # ゲーム進行中以外 (ロビー/終了済み) は無視
        if state.phase in (Phase.LOBBY, Phase.GAME_OVER):
            return

        # 最初のawaitより前に「終了状態の確定」と「ループのキャンセル指示」を
        # 同期的に行う。これで退出処理 (_eliminate_player_mid_game)・
        # start_gameのループ起動・force_endの二度押しが旧stateのGAME_OVERガードで
        # 遮断され、ゲームループがこれ以降 phase を書き換えることもなくなる。
        state.phase = Phase.GAME_OVER
        self._stop_all_game_views()
        if state.game_task and not state.game_task.done():
            state.game_task.cancel()
        initial_saved = False
        for attempt, delay in enumerate((0, 1, 2), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._persist_room_state()
                initial_saved = True
                break
            except Exception as e:
                log.exception(f"廃村GAME_OVER保存失敗 ({attempt}/3): {e}")
        if not initial_saved:
            # 精算を伴わない廃村は、DB一時障害でDiscord後始末まで
            # 止めるより、改名/mute/ロールを必ず元に戻す。最後の
            # LOBBY保存で再度永続化を試みる。
            log.error("廃村開始checkpointを保存できませんがcleanupを継続します")

        # pause中だと再開待ちで止まったままになるので解除しておく
        if state.paused:
            state.paused = False
            state.pause_event.set()

        # 夜の「朝を迎える」宣言待ちで止まったままにならないよう解除する
        # (パネルは編集しない。Viewの停止は _stop_all_game_views が行う)
        state.morning_ready_event.set()
        if self._morning_view is not None:
            self._morning_view.stop()
            self._morning_view = None
        state.morning_panel_message = None
        state.morning_panel_message_id = None

        # 役職確認の宣言待ちも同様に解除する
        state.prep_ready_event.set()
        if self._prep_view is not None:
            self._prep_view.stop()
            self._prep_view = None
            state.prep_panel_message = None

        # ゲームループタスクの完了まで待つ
        # (待たないと残った game_task が古い state を見続けて副作用を起こしうる)
        if state.game_task and not state.game_task.done():
            try:
                await state.game_task
            except (asyncio.CancelledError, Exception):
                pass

        if state.village_channel:
            try:
                await state.village_channel.send(f"⏹️ **{reason}**")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        cleanup_progress = await self._safe_village_send(
            "⏳ **終了処理中**\n"
            "ニックネーム・ミュート・VC権限を順番に復元しています。"
        )

        # ニックネーム復元
        await self._restore_nicknames()

        # ゲーム用の一時ロール・VC個別権限を全て撤去
        await self._teardown_game_roles_and_perms()
        await self._safe_timer_edit(
            cleanup_progress,
            "✅ **終了処理が完了しました。**\nニックネームとVC設定を復元しました。",
        )

        # チャンネル削除予約 (#昼 / #霊界)
        village = state.village_channel
        game_channels = [ch for ch in (state.village_channel, state.spirit_channel) if ch]

        if village:
            try:
                await village.send(
                    f"🕐 このチャンネルは {CHANNEL_DELETE_DELAY}秒後 に削除されます。"
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        async def delete_channels():
            await asyncio.sleep(CHANNEL_DELETE_DELAY)
            for ch in game_channels:
                try:
                    await ch.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"#{ch.name} チャンネル削除失敗: {e}")

        self.manager.spawn_bg_task(delete_channels())

        # 次村用に直前のメンバーを記録 (リセット/廃村からのやり直しにも使える)
        if state.players:
            self.last_game_roster = list(state.players.keys())
            self.last_game_gm = state.gm_id

        # ロビーリセット
        guild = state.guild
        category = state.category
        lobby = state.lobby_channel
        stats = state.stats_channel
        vc = state.voice_channel
        room_id = state.room_id
        room_name = state.room_name

        self.state = GameState()
        self.state.room_id = room_id
        self.state.room_name = room_name
        self.state.guild = guild
        self.state.category = category
        self.state.lobby_channel = lobby
        self.state.stats_channel = stats
        self.state.voice_channel = vc
        await self._post_lobby_ui()
        for attempt, delay in enumerate((0, 1, 2), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._persist_room_state()
                break
            except Exception as e:
                log.exception(f"廃村後LOBBY保存失敗 ({attempt}/3): {e}")

    async def reset_game(self) -> str:
        """やり直し。

        ゲーム中: 廃村して参加受付へ戻す。
        ロビー中: 参加者とGMを解除して受付を作り直す (無反応にしない)。
        どちらの場合も直前メンバーの記録は残るので「次村」で組み直せる。
        """
        state = self.state
        if state.phase in (Phase.LOBBY, Phase.GAME_OVER):
            had_entries = bool(state.players) or state.gm_id is not None
            state.players.clear()
            state.gm_id = None
            state.recruitment_id = None
            await self._post_lobby_ui()
            await self._persist_room_state()
            if had_entries:
                return "🔄 参加受付をリセットしました (参加者とGMを解除)。"
            return "🔄 参加受付をリセットしました (登録はありませんでした)。"

        await self.force_end("ゲームがリセットされました。")
        return "🔄 ゲームをリセットし、参加受付へ戻しました。"

    # ============================================================
    # 発言制御
    # ============================================================
    #
    # 接続中メンバーの発言制御はサーバーミュート (member.edit(mute=)) で行う。
    # 理由: チャンネル権限のspeak変更は「接続済みメンバー」には反映されず、
    # 再接続時に初めて再計算されるというDiscord仕様のため、フェーズ切替の
    # ミュート/解除は接続中メンバーへの member.edit(mute=) でしか実現できない。
    #
    # チャンネル権限は「これから接続してくる人」向けのバックストップとして併用:
    #   - @everyone: speak拒否 → 観戦者・死亡者は接続した時点で発言不可
    #   - 生存ロール: view/connect/speak許可 (固定・トグルしない) →
    #     生存者の再入室を常に保証しつつ、接続時にsuppressされない状態にする。
    #     発言可否そのものはサーバーミュートで制御する。
    #   - 途中入室した生存者へのミュート同期は on_voice_state_update で行う
    #
    # 残留事故対策 (旧実装がサーバーミュートを避けた理由への対処):
    #   - botがミュートした相手は state.bot_muted_ids に記録しスナップショットへ永続化
    #   - ゲーム終了時: 接続中は即解除 / 未接続は pending_unmutes (bot_meta) に退避し、
    #     どこかのVCへ入った時点でGameCogのリスナーが解除する
    #   - 起動時: 進行中ゲームのない部屋のVCに残ったサーバーミュートを掃除する

    def _mute_marker_role_name(self) -> str:
        return f"{MUTE_MARKER_ROLE_PREFIX}{self.room_def.room_id}"

    async def _ensure_mute_marker_role(self) -> Optional[discord.Role]:
        """mute所有権をDiscord側に残す無権限ロールを確保する。"""
        guild = self.state.guild
        if guild is None:
            return None
        role = discord.utils.get(guild.roles, name=self._mute_marker_role_name())
        if role is not None:
            return role
        try:
            return await self._paced_discord_api_call(
                guild.create_role,
                name=self._mute_marker_role_name(),
                reason="人狼: Bot所有muteの耐障害マーカー",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.error(f"mute所有マーカー作成失敗 ({self.state.room_name}): {e}")
            return None

    @staticmethod
    def _roles_with_mute_marker(
        member: discord.Member,
        marker: discord.Role,
        *,
        present: bool,
    ) -> list[discord.Role]:
        roles = [
            role for role in member_roles_for_edit(member)
            if getattr(role, "id", None) != marker.id
        ]
        if present:
            roles.append(marker)
        return roles

    def _has_own_mute_marker(self, member: discord.Member) -> bool:
        marker_name = self._mute_marker_role_name()
        return any(
            getattr(role, "name", None) == marker_name
            for role in getattr(member, "roles", [])
        )

    async def _enable_mute_markers(self) -> discord.Role:
        """旧snapshotの所有記録をマーカー方式へ一度だけ移行する。

        旧bot_muted_idsはすでにdurableな所有記録なので、この移行では
        rolesだけを付与しても手動muteを新たに奪うことはない。
        enabledのcheckpointは全員への付与後に行う。
        """
        state = self.state
        guild = state.guild
        existing_marker = (
            discord.utils.get(guild.roles, name=self._mute_marker_role_name())
            if guild is not None else None
        )
        marker = await self._ensure_mute_marker_role()
        if marker is None:
            raise StateDurabilityError("mute所有マーカーを確保できません")
        if state.mute_marker_enabled and existing_marker is not None:
            return marker

        if guild is not None:
            for member_id in list(state.bot_muted_ids):
                member = guild.get_member(member_id)
                if member is None or self._has_own_mute_marker(member):
                    continue
                try:
                    await self._paced_discord_api_call(
                        member.edit,
                        roles=self._roles_with_mute_marker(member, marker, present=True),
                        reason="人狼: 旧mute所有記録のマーカー移行",
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    await self._stop_for_durability_error("mute所有マーカー移行", e)
                    raise StateDurabilityError(
                        f"mute所有マーカーを付与できません: {member_id}"
                    ) from e

        old_enabled = state.mute_marker_enabled
        state.mute_marker_enabled = True
        try:
            await self._persist_room_state()
        except Exception as e:
            state.mute_marker_enabled = old_enabled
            await self._stop_for_durability_error("muteマーカー方式のcheckpoint", e)
            raise StateDurabilityError("muteマーカー方式を保存できません") from e
        return marker

    async def _reconcile_mute_marker_ownership(self) -> None:
        """Discord側マーカーを正としてDB所有キャッシュを収束させる。"""
        state = self.state
        guild = state.guild
        if not state.mute_marker_enabled or guild is None:
            return
        old_owned = set(state.bot_muted_ids)
        known_ids = set(old_owned)
        known_ids.update(
            member.id for member in getattr(guild, "members", [])
            if self._has_own_mute_marker(member)
        )
        for member_id in known_ids:
            member = guild.get_member(member_id)
            if member is None:
                # キャッシュ欠落/一時退出で証拠を捸てない。
                continue
            if self._has_own_mute_marker(member):
                state.bot_muted_ids.add(member_id)
            else:
                # unmute+marker除去の同一PATCH成功後にDB保存前で
                # 落ちた場合はここに来る。古い所有記録を捸てる。
                state.bot_muted_ids.discard(member_id)
                state.bot_mute_intent_ids.discard(member_id)
        if state.bot_muted_ids != old_owned:
            await self._persist_mute_ownership_checkpoint("muteマーカー所有照合の保存")

    def _alive_role_name(self) -> str:
        return f"人狼進行中:{self.room_def.room_id}"

    async def _ensure_alive_role(self) -> Optional[discord.Role]:
        guild = self.state.guild
        if guild is None:
            return None
        role = discord.utils.get(guild.roles, name=self._alive_role_name())
        if role is not None:
            return role
        try:
            return await self._paced_discord_api_call(
                guild.create_role,
                name=self._alive_role_name(),
                reason="人狼: ゲーム中の発言制御用ロール",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"進行中ロール作成失敗 ({self.state.room_name}): {e}")
            return None

    async def _assign_alive_role(self, exclude_ids: Optional[set[int]] = None) -> None:
        """生存プレイヤー全員に生存ロールを付与 (ゲーム開始/復元時)"""
        role = await self._ensure_alive_role()
        if role is None:
            return
        exclude_ids = exclude_ids or set()
        for player in self.state.alive_players():
            if player.user_id in exclude_ids:
                continue
            if role in getattr(player.member, "roles", []):
                continue
            try:
                await self._paced_discord_api_call(
                    player.member.add_roles, role, reason="人狼: ゲーム参加"
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"進行中ロール付与失敗 ({player.display_name}): {e}")

    async def _remove_alive_role(self, member: discord.Member) -> bool:
        """死亡/除外したメンバーから生存ロールを外す (発言権を確実に失わせる)"""
        guild = self.state.guild
        if guild is None:
            return False
        role = discord.utils.get(guild.roles, name=self._alive_role_name())
        if role is None:
            return True
        # interactionスナップショットのrolesには後付けの生存ロールが現れないため、
        # 判定はキャッシュ済みメンバーで行う (未キャッシュなら除去を試みる)
        cached = guild.get_member(member.id)
        if cached is not None:
            if role not in cached.roles:
                return True
            member = cached
        try:
            await self._discord_api_call(member.remove_roles, role, reason="人狼: 死亡/除外")
            return True
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"進行中ロール除去失敗 ({member.display_name}): {e}")
            return False

    async def _delete_alive_role(self) -> None:
        """生存ロールを削除 (メンバーからの除去・チャンネル上書きもDiscord側で自動消去)"""
        guild = self.state.guild
        if guild is None:
            return
        role = discord.utils.get(guild.roles, name=self._alive_role_name())
        if role is None:
            return
        try:
            await self._paced_discord_api_call(
                role.delete, reason="人狼: ゲーム終了で一時ロール削除"
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"進行中ロール削除失敗 ({self.state.room_name}): {e}")

    async def _grant_alive_vc_access(self, vc: Optional[discord.VoiceChannel] = None) -> None:
        """生存ロールにVCの表示/接続/発言許可を付与する (ゲーム開始/復元時に1回)。

        speak=True は固定でトグルしない。接続時にsuppressされない状態を保証し、
        実際の発言可否は _sync_server_mutes が制御する。
        connect=True により、ランクロール喪失や制限卓でも生存者の再入室を保証する。
        """
        role = await self._ensure_alive_role()
        vc = vc or self.state.voice_channel
        if role is None or vc is None:
            return
        try:
            await self._paced_discord_api_call(
                vc.set_permissions, role,
                view_channel=True, connect=True, speak=True,
                reason="人狼: 生存者のVCアクセス保証",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"VC生存ロール権限更新失敗 ({self.state.room_name}): {e}")

    def _effective_phase(self) -> Optional[Phase]:
        """一時停止中は停止前のフェーズを返す (PAUSEDを透過して扱う)"""
        state = self.state
        if state.phase == Phase.PAUSED:
            return state.phase_before_pause
        return state.phase

    def night_actions_open(self) -> bool:
        """夜の操作 (占い・護衛・襲撃・朝の宣言) を受け付けるか。

        一時停止中も受け付ける: 止まっている間に選べないと、再開後に
        「選んだはずの行動が消えた」ように見えるため
        """
        state = self.state
        return (
            self._effective_phase() == Phase.NIGHT
            and not state.ending
            and state.pending_winner is None
            and not state.morning_confirmed
            and not state.morning_ready_event.is_set()
            and not state.night_resolved
        )

    def wolf_relay_open(self) -> bool:
        """人狼DMの中継を受け付けるか。

        夜の制限時間が切れた時点で閉じる。night_actions_open と分けているのは、
        「朝を迎える」の宣言待ちで夜が延びている間まで相談を許すと、
        制限時間の意味が無くなるため。襲撃・占い・護衛の選択は
        night_actions_open のまま夜明けまで受け付ける。
        """
        state = self.state
        return (
            state.wolf_relay_window_open
            and self._effective_phase() == Phase.NIGHT
            and not state.ending
            and state.pending_winner is None
            and not state.night_resolved
        )

    def _current_speaker_ids(self) -> set[int]:
        """現在のフェーズで発言できるべきプレイヤーIDの集合"""
        state = self.state
        phase = self._effective_phase()
        if phase == Phase.DAY_DISCUSSION:
            return {p.user_id for p in state.alive_players()}
        if phase in (Phase.DAY_RUNOFF_SPEECH, Phase.DAY_LAST_WILL):
            return {state.current_speaker_id} if state.current_speaker_id else set()
        return set()

    async def _sync_server_mutes(
        self,
        speakers: set[int],
        *,
        skip_ids: Optional[set[int]] = None,
    ) -> list[tuple[discord.Member, bool]]:
        """VC接続中メンバーのサーバーミュートを目標状態に同期する。

        speakers に含まれるメンバーだけ発言可、他は全てミュート。
        既に目標状態のメンバーはAPIを呼ばずスキップする (メンバー編集
        バケット ≈10回/10秒 の節約)。権限suppressで既に発言不可の人も
        ミュートしない (観戦者の途中入室など)。
        skip_ids は直前の統合PATCHが成功済みで、Gateway反映待ちだけの相手を
        再送対象から外すためにゲーム開始時だけ指定する。

        13人分のPATCHはギルド共有バケットで429を食らいやすく、
        リトライを使い切って失敗する人が出ることがある。取りこぼすと
        「1人だけ喋れる/喋れない」事故になるため、失敗分は少し待って
        1度だけ再試行する (成功していれば追加のAPIは発生しない)。

        Returns:
            実際に変更を要した (member, 目標mute)の一覧。
            空なら全員が既に目標状態。
        """
        state = self.state
        vc = state.voice_channel
        if vc is None:
            return []
        skip_ids = skip_ids or set()

        marker: Optional[discord.Role] = None
        if state.guild is not None:
            # 新規ゲームは開始時に有効化済み。旧snapshotだけ
            # ここで移行し、その後のmute/unmuteは全て同一PATCHで
            # マーカーを付け外しする。
            marker = await self._enable_mute_markers()

        owned_before = set(state.bot_muted_ids)

        failed: list[tuple[discord.Member, bool]] = []

        async def set_mute(member: discord.Member, mute: bool, *, retry: bool = True) -> None:
            try:
                edit_kwargs: dict = {"mute": mute}
                if marker is not None:
                    edit_kwargs["roles"] = self._roles_with_mute_marker(
                        member, marker, present=mute
                    )
                await self._paced_discord_api_call(
                    member.edit, **edit_kwargs,
                    reason="人狼: フェーズ発言制御",
                )
                if mute:
                    state.bot_muted_ids.add(member.id)
                    state.bot_mute_intent_ids.discard(member.id)
                else:
                    state.bot_muted_ids.discard(member.id)
                    state.bot_mute_intent_ids.discard(member.id)
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(
                    f"サーバーミュート変更失敗 ({member.display_name} → mute={mute}): {e}"
                )
                if retry:
                    failed.append((member, mute))

        targets: list[tuple[discord.Member, bool]] = []
        for member in list(vc.members):
            if member.bot:
                continue
            if member.id in skip_ids:
                continue
            # GMは常にミュート自動制御の対象外 (参加者を兼ねていても)。
            # Discordのサーバーミュートは本人では解除できない仕様のため、
            # 進行役が不意に発言不能になる事故を避け、GM自身の手動管理に委ねる
            if member.id == state.gm_id:
                continue
            vs = member.voice
            if vs is None or vs.channel is None or vs.channel.id != vc.id:
                continue
            should_speak = member.id in speakers
            if should_speak and vs.mute:
                if member.id in state.bot_muted_ids:
                    targets.append((member, False))
                else:
                    # Botが所有記録を持たないmuteはモデレーターによる
                    # 手動制御とみなし、フェーズ遷移で勝手に解除しない。
                    log.warning(
                        f"手動サーバーミュートを保護しました: {member.display_name}"
                    )
            elif not should_speak and not vs.mute:
                if getattr(vs, "suppress", False):
                    continue  # 権限側で既に発言不可
                targets.append((member, True))

        if not targets:
            return []
        # Member.editはギルド共有バケットのため、Semaphore(5)だけで並列化すると
        # 13人の末尾が429になりやすい。全卓共通ペーサーで順番に流す。
        for member, mute in targets:
            await set_mute(member, mute)

        if failed:
            retry_targets = list(failed)
            failed.clear()
            await asyncio.sleep(MUTE_RETRY_DELAY)
            log.info(f"サーバーミュートを再試行します ({len(retry_targets)}人)")
            for member, mute in retry_targets:
                await set_mute(member, mute, retry=False)
        # mute操作と所有記録を別々の障害窓にしない。
        # ここで落ちても、再起動後にBot自身がmuteした相手を
        # 手動muteと誤認せず解除できる。
        if state.bot_muted_ids != owned_before:
            await self._persist_mute_ownership_checkpoint("フェーズmute所有保存")
        return targets

    async def _reconcile_mute_intents(self) -> None:
        """PREPARATIONのPATCH結果保存前クラッシュを所有記録へ照合する。"""
        state = self.state
        if not state.bot_mute_intent_ids or state.guild is None:
            return
        changed = False
        for user_id in list(state.bot_mute_intent_ids):
            member = state.guild.get_member(user_id)
            if member is None:
                state.bot_mute_intent_ids.discard(user_id)
                changed = True
                continue
            vs = getattr(member, "voice", None)
            if vs is None or not vs.mute:
                # 未実行。意図は残し、次の_sync_server_mutesで実行する。
                continue
            player = state.get_player(user_id)
            expected_nick = player.display_name[:32] if player else "観戦者"
            if state.mute_marker_enabled and self._has_own_mute_marker(member):
                # mute+専用ロールを同一PATCHで送っているため、
                # ニックネームより強い一意な成功証拠になる。
                state.bot_muted_ids.add(user_id)
            elif not state.mute_marker_enabled and member.nick == expected_nick:
                # nick+muteを1PATCHで送ったため、期待nickも反映済みなら
                # BotのPATCH成功と判定できる。
                state.bot_muted_ids.add(user_id)
            else:
                # muteだけが付いている場合は手動muteの可能性がある。
                # 所有せず、終了時に解除しない。
                log.warning(f"開始中断後の手動mute候補を保護: {member.display_name}")
            state.bot_mute_intent_ids.discard(user_id)
            changed = True
        if changed:
            await self._persist_room_state()

    async def _await_mute_applied(
        self, targets: list[tuple[discord.Member, bool]], timeout: float
    ) -> bool:
        """サーバーミュートの変更がゲートウェイに反映されるまで待つ。

        member.edit のPATCHが返っても、各クライアントに VOICE_STATE_UPDATE が
        届くまでは実際の発言可否が切り替わらない。弁明・遺言でここを待たずに
        タイマーを始めると、喋れない時間の分だけ持ち時間が削られてしまう。

        反映を確認した時点で即座に返るので、速いときは待ち時間ゼロで通る。
        VCから抜けたメンバーは対象から外す (待ち続けない)。

        Returns:
            True: 全て反映済み / False: timeout秒以内に反映を確認できなかった
        """
        if not targets:
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            pending = []
            for member, mute in targets:
                vs = getattr(member, "voice", None)
                if vs is None or vs.channel is None:
                    continue  # VCから抜けた人は待たない
                vc = self.state.voice_channel
                if vc is None or vs.channel.id != vc.id:
                    continue  # 待機中に別VCへ移動した人も対象外
                if vs.mute != mute:
                    pending.append(member)
            if not pending:
                return True
            if loop.time() >= deadline:
                names = ", ".join(m.display_name for m in pending)
                log.warning(f"ミュート反映待ちがタイムアウトしました: {names}")
                return False
            await asyncio.sleep(0.25)

    async def _restore_pause_after_failed_mute_resume(
        self, resume_phase: Phase, context: str
    ) -> None:
        """再開時の発言制御が揃わなければタイマーを解放せず停止へ戻す。"""
        state = self.state
        state.paused = True
        state.phase_before_pause = resume_phase
        state.phase = Phase.PAUSED
        state.pause_event.clear()
        await self._persist_room_state()
        await self._safe_village_send(
            f"⚠️ **{context}を確認できないため停止を継続します。**\n"
            "Botのメンバーミュート権限とロール順位を確認し、解消後にGMが再開してください。"
        )

    async def _wait_for_mute_sync_or_pause(
        self,
        changed: list[tuple[discord.Member, bool]],
        speakers: set[int],
        context: str,
    ) -> None:
        """進行中の発言制御が揃うまで安全停止とGM再開を繰り返す。"""
        state = self.state
        while not await self._await_mute_applied(changed, MUTE_GRACE_TIME):
            resume_phase = self._effective_phase() or Phase.DAY_DISCUSSION
            state.paused = True
            state.phase_before_pause = resume_phase
            state.phase = Phase.PAUSED
            state.pause_event.clear()
            await self._persist_room_state()
            await self._safe_village_send(
                f"⚠️ **{context}を確認できないため安全停止しました。**\n"
                "Botのメンバーミュート権限とロール順位を確認し、解消後にGMが再開してください。"
            )
            # resume_game側も同じ発言状態を確認してからだけEventを開く。
            await state.pause_event.wait()
            changed = await self._sync_server_mutes(speakers)

    async def _mute_phase(self, note: str) -> None:
        """全員ミュートへ移るときの整列フェーズ。

        ミュートを流し切り、**全員へ反映されたことを確認してから**次へ進む。
        「終了と表示されたのにまだ喋れる」状態をなくすのが目的
        (member.edit は429で数秒〜十数秒遅れることがある)。

        反映が速ければすぐ抜けるので、待ちは最大 `MUTE_GRACE_TIME` 秒。
        既に全員ミュート済み (遺言後の夜入りなど) のときは、
        待ち時間もメッセージも増やさない。
        """
        changed = await self._sync_server_mutes(set())
        if not changed:
            return

        await self._safe_village_send(f"🔇 **全員をミュートしました。**\n{note}")
        await self._wait_for_mute_sync_or_pause(
            changed, set(), "全員ミュートへの切り替え"
        )

    async def _set_alive_can_send(self, can: bool) -> None:
        role = await self._ensure_alive_role()
        ch = self.state.village_channel
        if role is None or ch is None:
            return
        try:
            # read=True を併記して、別卓の終了に伴うランクロール再同期で
            # 昼チャンネルの閲覧権限が巻き込まれて消えないようにする
            await self._discord_api_call(
                ch.set_permissions, role,
                read_messages=True, send_messages=can,
                reason="人狼: 昼チャンネル書き込み制御",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"#昼書き込み権限更新失敗 ({self.state.room_name}): {e}")

    async def _grant_speaker(self, member: discord.Member) -> None:
        """弁明者だけ発言を許可 (サーバーミュート解除。他は全員ミュートのまま)。

        解除が実際に反映されるまで待ってから返る。待たずにタイマーを始めると、
        429で解除が遅れたときに弁明・遺言の持ち時間がその分削られてしまう。
        """
        changed = await self._sync_server_mutes({member.id})
        await self._wait_for_mute_sync_or_pause(
            changed, {member.id}, "発言者のミュート解除"
        )

    async def _clear_speaker(self, member: discord.Member) -> None:
        """弁明終了: 弁明者を再びミュートする"""
        changed = await self._sync_server_mutes(set())
        await self._wait_for_mute_sync_or_pause(
            changed, set(), "発言者の再ミュート"
        )

    async def _restrict_vc_for_game(self) -> None:
        """ゲーム中はVCの@everyoneを発言禁止にする (観戦者・死亡者対策)。

        生存者は「生存ロール」への許可上書き、弁明者は個別上書きで発言する。
        注意: set_permissions のkwargs指定は既存上書きの「置換」であり
        マージではない。制限卓のVCが持つ表示権限 (view/connect拒否) を
        消さないよう、overwrites_for で現在値を取得し必要な項目だけ
        変更して書き戻す。

        音声(speak)と併せて**VCの内蔵テキストチャット(send_messages)も塞ぐ**。
        ここを開けたままだと、サーバーミュートで音声を封じた死亡者・観戦者が
        VCのチャット欄へ書き込め、それが生存者全員に見えてしまう
        (霊界で知った役職をそのまま伝えられる)。昼の会話は #昼 で行うため、
        VCのテキストは生存者にも開けない。
        スレッド系の権限は触らない: VCの内蔵チャットはスレッドを作れず、
        受け付けられない権限ビットでこの呼び出し全体が失敗すると
        speak の制限まで巻き添えで外れてしまう。
        """
        state = self.state
        vc = state.voice_channel
        if vc is None or state.guild is None:
            return
        try:
            ow = vc.overwrites_for(state.guild.default_role)
            ow.speak = False
            ow.send_messages = False
            await self._paced_discord_api_call(
                vc.set_permissions, state.guild.default_role,
                overwrite=ow, reason="人狼: ゲーム中の観戦者発言禁止",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"VC観戦者制限失敗 ({state.room_name}): {e}")

        # GMが参加者を兼ねていない場合は個別に発言許可 (進行のため常時発言可)
        if state.gm_id is not None and state.gm_id not in state.players:
            gm_member = state.guild.get_member(state.gm_id)
            if gm_member is not None:
                try:
                    await self._paced_discord_api_call(
                        vc.set_permissions, gm_member,
                        speak=True, reason="人狼: GM発言許可",
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"GM発言許可失敗 ({state.room_name}): {e}")

    async def _release_vc_after_game(self) -> None:
        """ゲーム終了時に@everyoneの発言制限だけを解除する (表示権限は維持)。

        _restrict_vc_for_game で伏せた音声とテキストを揃って None
        (未設定=サーバー既定) へ戻す。片方だけ残すと、ゲーム外でも
        VCのチャットが使えないままになる。
        """
        state = self.state
        vc = state.voice_channel
        if vc is None or state.guild is None:
            return
        try:
            ow = vc.overwrites_for(state.guild.default_role)
            ow.speak = None
            ow.send_messages = None
            await self._paced_discord_api_call(
                vc.set_permissions, state.guild.default_role,
                overwrite=None if ow.is_empty() else ow,
                reason="人狼: ゲーム終了で発言制限解除",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"VC発言制限解除失敗 ({state.room_name}): {e}")

    async def _mute_all(
        self, *, skip_ids: Optional[set[int]] = None
    ) -> list[tuple[discord.Member, bool]]:
        """VC接続中の全員をミュート (GMを除く)"""
        return await self._sync_server_mutes(set(), skip_ids=skip_ids)

    async def _unmute_alive(self) -> list[tuple[discord.Member, bool]]:
        """生存者だけ発言許可 (死亡者・観戦者はミュート継続)"""
        return await self._sync_server_mutes(
            {p.user_id for p in self.state.alive_players()}
        )

    async def _teardown_game_roles_and_perms(self) -> None:
        """ゲームで使った一時ロール・VC個別権限・サーバーミュートを全て元に戻す"""
        state = self.state
        vc = state.voice_channel

        # サーバーミュートの解除 (botがミュートした人だけ。手動ミュートは触らない)
        # 接続中は即解除 / 未接続はpendingに退避してVC入室時に解除する
        if state.bot_muted_ids:
            remaining = set(state.bot_muted_ids)
            marker = (
                discord.utils.get(
                    state.guild.roles, name=self._mute_marker_role_name()
                )
                if state.guild is not None else None
            )

            async def unmute_one(member: discord.Member) -> None:
                try:
                    edit_kwargs: dict = {"mute": False}
                    if marker is not None:
                        edit_kwargs["roles"] = self._roles_with_mute_marker(
                            member, marker, present=False
                        )
                    await self._paced_discord_api_call(
                        member.edit, **edit_kwargs,
                        reason="人狼: ゲーム終了でミュート解除",
                    )
                    remaining.discard(member.id)
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"終了時ミュート解除失敗 ({member.display_name}): {e}")

            # ギルド内のどこかのVCに接続中なら即解除できる
            # (別VCへ移動した人はpending行きにするとVCへ入り直すまで
            #  解除されないため、ここで直接解除する)
            targets: list[discord.Member] = []
            if state.guild is not None:
                for member_id in list(remaining):
                    member = state.guild.get_member(member_id)
                    if member is None or member.voice is None or member.voice.channel is None:
                        continue
                    # 進行中の別卓のVCにいる人は触らない (その卓の発言制御を壊さない)。
                    # 待ちリストへ回して、別の場所へ移った時点で解除する
                    if self.manager.is_other_active_game_vc(
                        member.voice.channel.id, exclude_room_id=state.room_id
                    ):
                        continue
                    if member.voice.mute or self._has_own_mute_marker(member):
                        targets.append(member)
                    else:
                        remaining.discard(member_id)  # 既に解除済み
            if targets:
                for member in targets:
                    await unmute_one(member)
            if remaining:
                await self.manager.register_pending_unmutes(state.guild, remaining)
            state.bot_muted_ids.clear()

        if vc is not None:
            async def clear_one(member: discord.Member) -> None:
                try:
                    await self._paced_discord_api_call(
                        vc.set_permissions, member, overwrite=None,
                        reason="人狼: ゲーム終了クリーンアップ",
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"VC個別権限解除失敗 ({member.display_name}): {e}")

            perm_targets = [p.member for p in state.players.values()]
            # 参加者を兼ねていないGMの個別発言許可も撤去
            if state.gm_id is not None and state.gm_id not in state.players and state.guild:
                gm_member = state.guild.get_member(state.gm_id)
                if gm_member is not None:
                    perm_targets.append(gm_member)
            # 実際に個別上書きが存在するメンバーだけAPIを呼ぶ
            # (現行実装で上書きを持つのは通常GMのみ。全員分DELETEすると
            #  チャンネル権限バケットを浪費して429の温床になる)
            perm_targets = [
                m for m in perm_targets
                if self.manager._has_permission_overwrite(vc, m)
            ]
            if perm_targets:
                for member in perm_targets:
                    await clear_one(member)
        await self._release_vc_after_game()
        await self._delete_alive_role()

    # ============================================================
    # チャンネル権限管理
    # ============================================================

    async def _lock_village(self) -> None:
        await self._set_alive_can_send(False)

    async def _unlock_village_for_alive(self) -> None:
        await self._set_alive_can_send(True)

    # ============================================================
    # ニックネーム管理
    # ============================================================

    async def _restore_nicknames(self, state: Optional[GameState] = None) -> None:
        # force_end が self.state を差し替えた後に旧stateの改名を戻すケースが
        # あるため、明示指定できるようにする (省略時は現在のstate)
        state = state or self.state
        marker = (
            discord.utils.get(
                state.guild.roles,
                name=f"{MUTE_MARKER_ROLE_PREFIX}{state.room_id}",
            )
            if state.guild is not None else None
        )

        async def restore_one(member_id: int, nick: Optional[str]) -> None:
            member = state.guild.get_member(member_id)
            if not member or member.bot:
                return
            # 同じPATCHでサーバーミュートも解除する (メンバー編集バケット節約)。
            # ミュート指定はVC接続中しか受け付けられないため接続確認する
            kwargs: dict = {}
            if member.nick != nick:
                kwargs["nick"] = nick
            vs = member.voice
            vc = state.voice_channel
            if (
                member_id in state.bot_muted_ids
                and vs is not None and vs.channel is not None
                and vc is not None and vs.channel.id == vc.id
            ):
                if vs.mute:
                    kwargs["mute"] = False
                if marker is not None and any(
                    getattr(role, "id", None) == marker.id
                    for role in getattr(member, "roles", [])
                ):
                    kwargs["roles"] = self._roles_with_mute_marker(
                        member, marker, present=False
                    )
            try:
                if not kwargs:
                    return
                await self._paced_discord_api_call(member.edit, **kwargs)
                if "mute" in kwargs or "roles" in kwargs:
                    state.bot_muted_ids.discard(member_id)
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"ニックネーム復元失敗 (ID:{member_id}, nick:{nick}): {e}")

        if state.original_nicknames:
            for member_id, nickname in state.original_nicknames.items():
                await restore_one(member_id, nickname)

    # ============================================================
    # 安全な村チャンネル送信ヘルパー
    # ============================================================
    #
    # _game_loop は予期せぬ例外を捕まえると force_end を呼ぶため、
    # 一時的な Discord 側エラー (500/HTTPException) で送信が失敗した
    # だけでゲームが廃村されてしまうのを避けるための薄いラッパ。
    # 失敗時は warning ログを出して None を返す。
    async def _safe_village_send(
        self, *args, **kwargs
    ) -> Optional[discord.Message]:
        ch = self.state.village_channel
        if ch is None:
            return None
        try:
            return await ch.send(*args, **kwargs)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"#昼 への送信失敗: {e}")
            return None

    async def _open_spirit_for(self, member: discord.Member) -> None:
        """死亡/除外したメンバーの #霊界 閲覧ブロック (メンバー個別上書き) を解除する"""
        ch = self.state.spirit_channel
        if ch is None:
            return
        try:
            await self._discord_api_call(
                ch.set_permissions, member, overwrite=None,
                reason="人狼: 死亡により霊界を開放",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"霊界開放失敗 ({member.display_name}): {e}")

    async def _apply_spirit_blocks(self) -> None:
        """#霊界 の生存者ブロックを現在の生死に合わせて再適用する (復元時の冪等処理)"""
        ch = self.state.spirit_channel
        if ch is None:
            return
        for player in self.state.players.values():
            overwrite = (
                discord.PermissionOverwrite(view_channel=False, read_messages=False)
                if player.alive else None
            )
            try:
                await self._paced_discord_api_call(
                    ch.set_permissions, player.member, overwrite=overwrite,
                    reason="人狼: 復元時の霊界権限再適用",
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"霊界権限再適用失敗 ({player.display_name}): {e}")

    async def _safe_spirit_send(
        self, *args, **kwargs
    ) -> Optional[discord.Message]:
        ch = self.state.spirit_channel
        if ch is None:
            return None
        try:
            return await ch.send(*args, **kwargs)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"#霊界 への送信失敗: {e}")
            return None

    @staticmethod
    def _format_timer(seconds: float) -> str:
        total = max(0, int(seconds + 0.999))
        return f"{total // 60}:{total % 60:02d}"

    def _timer_line(self, remaining: float, label: str = "残り時間") -> str:
        """残り時間の強調表示行 (見出しマークダウンで大きく表示する)"""
        return f"# ⏱️ {label} `{self._format_timer(remaining)}`"

    async def _safe_timer_edit(
        self,
        message: Optional[discord.Message],
        content: str,
    ) -> Optional[discord.Message]:
        if message is None:
            return None
        try:
            await self._discord_api_call(message.edit, content=content)
            return message
        except (discord.NotFound, discord.Forbidden) as e:
            # メッセージ消失・権限不足は回復しないので以降の更新を諦める
            log.warning(f"タイマー表示更新失敗 (更新を停止): {e}")
            return None
        except discord.HTTPException as e:
            # 一時的な障害 (500等) では参照を保持して次の秒読みで再試行する
            log.warning(f"タイマー表示更新失敗 (再試行継続): {e}")
            return message

    async def _pausable_countdown(
        self,
        message: Optional[discord.Message],
        build_content: Callable[[float], str],
        seconds: float,
        event: Optional[asyncio.Event] = None,
    ) -> bool:
        """
        一時停止対応のカウントダウン。
        表示は30秒ごと、残り60秒から1秒ごとに更新する。

        Returns:
            True  : event が発生した
            False : 時間切れ
        """
        if event is not None and event.is_set():
            return True

        remaining = seconds
        last_display = max(0, int(remaining + 0.999))
        loop = asyncio.get_running_loop()

        while remaining > 0:
            await self.state.pause_event.wait()
            if event is not None and event.is_set():
                return True

            chunk = min(remaining, self._PAUSE_POLL)
            start = loop.time()
            if event is None:
                await asyncio.sleep(chunk)
            else:
                try:
                    await asyncio.wait_for(event.wait(), timeout=chunk)
                    return True
                except asyncio.TimeoutError:
                    pass

            remaining -= loop.time() - start
            display = max(0, int(remaining + 0.999))
            # 60秒超: 30秒ごと / 残り60秒からは毎秒の秒読み。
            # メッセージ編集はチャンネル単位のバケット (約5回/5秒) なので
            # 毎秒1回なら他の卓と競合せずに収まる
            should_update = display != last_display and (
                display == 0
                or display <= 60
                or display % 30 == 0
            )
            if should_update:
                message = await self._safe_timer_edit(message, build_content(display))
                last_display = display

        return False

    # ============================================================
    # 一時停止対応の待機ヘルパー (全フェーズ共通)
    # ============================================================
    #
    # 設計:
    #   どちらも 0.5秒のチャンクに区切って実行し、
    #   各チャンクの先頭で pause_event を待つ。
    #   pause が起きた場合は次のチャンク開始時にブロック → 時間消費が止まる。
    #   反応速度: 最大 ~0.5秒 (POLL定数)
    #
    _PAUSE_POLL = 0.5

    async def _pausable_sleep(self, seconds: float) -> None:
        """一時停止対応の sleep。pause 中はカウントダウン停止。"""
        remaining = seconds
        loop = asyncio.get_running_loop()
        while remaining > 0:
            # pause中ならここでブロック (時間消費しない)
            await self.state.pause_event.wait()
            chunk = min(remaining, self._PAUSE_POLL)
            start = loop.time()
            await asyncio.sleep(chunk)
            remaining -= loop.time() - start

    async def _pausable_wait_forever(self, event: asyncio.Event) -> None:
        """一時停止対応の無期限イベント待機 (「朝を迎える」宣言待ちで使う)"""
        while not event.is_set():
            # pause中ならここでブロック
            await self.state.pause_event.wait()
            try:
                await asyncio.wait_for(event.wait(), timeout=self._PAUSE_POLL)
            except asyncio.TimeoutError:
                continue

    # ============================================================
    # VC入室検知 (GameCogのリスナーから各卓へdispatchされる)
    # ============================================================

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """この卓のVCの出入りを処理する。

        - 生存プレイヤーの切断: 自動一時停止して復帰を待つ (回線落ち対策)
        - 復帰待ちプレイヤーの再入室: 告知してGMの「再開」を促す
        - 観戦者の途中入室: 「観戦者」へリネーム (終了時に復元)
        """
        state = self.state
        if state.phase in (Phase.LOBBY, Phase.GAME_OVER):
            return
        vc = state.voice_channel
        if vc is None or member.bot:
            return
        # チャンネル移動のみ対象 (ミュート変更等では発火させない)
        if before.channel == after.channel:
            return

        joined = after.channel is not None and after.channel.id == vc.id
        left = (
            before.channel is not None and before.channel.id == vc.id
            and not joined
        )
        player = state.get_player(member.id)

        # 生存プレイヤーのVC切断 → 自動一時停止
        if left and player is not None and player.alive and self._is_game_in_progress():
            await self._pause_for_disconnect(player, "通話から切断されました")
            return

        if not joined:
            return

        # 復帰待ちプレイヤーのVC復帰
        if player is not None:
            text = await self._handle_disconnect_return(player)
            if text:
                await self._safe_village_send(text)
            # サーバーミュートの残留/不足を現在フェーズに同期する
            # (例: 夜に切断→議論中に復帰した場合の残留ミュート解除)
            if self._is_game_in_progress():
                await self._sync_server_mutes(self._current_speaker_ids())
            return

        if member.id == state.gm_id:
            return

        # 観戦者の途中入室: 「観戦者」へリネーム + サーバーミュート
        # (権限のspeak拒否だけに頼ると、環境によっては発言できてしまうため
        #  プレイヤーと同様にミュートも明示的に適用する)
        if member.id not in state.original_nicknames:
            state.original_nicknames[member.id] = member.nick
            await self._persist_room_state()

        in_game = self._is_game_in_progress()
        edit_kwargs: dict = {}
        if member.nick != "観戦者":
            edit_kwargs["nick"] = "観戦者"
        vs = member.voice
        if in_game and vs is not None and not vs.mute:
            edit_kwargs["mute"] = True
            if state.guild is not None:
                marker = await self._enable_mute_markers()
                edit_kwargs["roles"] = self._roles_with_mute_marker(
                    member, marker, present=True
                )
        if not edit_kwargs:
            return
        if "mute" in edit_kwargs:
            state.bot_mute_intent_ids.add(member.id)
            await self._persist_mute_ownership_checkpoint("途中入室観戦者のmute意図保存")
        try:
            await self._discord_api_call(member.edit, **edit_kwargs)
            if "mute" in edit_kwargs:
                state.bot_muted_ids.add(member.id)
                state.bot_mute_intent_ids.discard(member.id)
                await self._persist_mute_ownership_checkpoint("途中入室観戦者のmute所有保存")
        except (discord.Forbidden, discord.HTTPException) as e:
            if "mute" in edit_kwargs:
                state.bot_mute_intent_ids.discard(member.id)
                await self._persist_mute_ownership_checkpoint("途中入室観戦者のmute失敗保存")
            log.warning(f"途中入室の観戦者設定失敗: {member.display_name} ({e})")

    # ============================================================
    # メンバー再参加検知 (GameCogのリスナーから各卓へdispatchされる)
    # ============================================================

    async def on_member_join(self, member: discord.Member) -> None:
        """ゲーム中の参加者がサーバーへ戻ってきた場合の復元。

        退出時にDiscord側で消えるもの (ニックネーム・生存ロール・
        #霊界 のメンバー個別ブロック) を再適用する。特に霊界ブロックを
        再適用しないと、生存中に死亡者の会話が見えてしまう。
        """
        state = self.state
        player = state.get_player(member.id)
        if player is None or not self._is_game_in_progress():
            return

        # 退出前の Member オブジェクトは無効なので差し替える
        player.member = member

        if not player.alive:
            # 死亡者の復帰: 霊界は開放済み扱いのままでよい
            return

        # 再参加直後はDiscord側でメンバー個別overwriteが消えている。
        # ニックネーム/ロール復旧を先に待つと、その間に生存者が霊界履歴を
        # 読めるため、最初のDiscord副作用として閲覧拒否を戻す。
        if state.spirit_channel is not None:
            try:
                await self._discord_api_call(
                    state.spirit_channel.set_permissions,
                    member,
                    overwrite=discord.PermissionOverwrite(
                        view_channel=False, read_messages=False
                    ),
                    reason="人狼: サーバー復帰 (生存者の霊界ブロック再適用)",
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.error(f"復帰者の霊界ブロック再適用失敗 ({player.display_name}): {e}")
                async with self.action_lock:
                    if self._is_game_in_progress():
                        await self._pause_for_disconnect(
                            player, "霊界の閲覧防止を再適用できませんでした"
                        )
                await self._safe_village_send(
                    "⚠️ **復帰者の霊界閲覧を遮断できないため安全停止しました。**\n"
                    "Botのチャンネル管理権限を確認し、問題解消後にGMが再開してください。"
                )
                return

        role = await self._ensure_alive_role()
        target_nick = player.display_name[:32]
        edit_kwargs: dict = {"nick": target_nick}
        needs_role = role is not None and role not in getattr(member, "roles", [])
        if needs_role:
            edit_kwargs["roles"] = [*member_roles_for_edit(member), role]
        try:
            updated_member = await self._discord_api_call(member.edit, **edit_kwargs)
            if getattr(updated_member, "id", None) == player.user_id:
                player.member = updated_member
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"復帰者初期設定の統合PATCH失敗 ({player.display_name}): {e}")
            # 一方だけ権限不足等で失敗した場合にも、従来どおり可能な設定は
            # 回収する。正常系は1回、失敗時だけ個別APIへフォールバックする。
            try:
                if member.nick != target_nick:
                    await self._discord_api_call(member.edit, nick=target_nick)
            except (discord.Forbidden, discord.HTTPException) as nick_error:
                log.warning(
                    f"復帰者ニックネーム再設定失敗 ({player.display_name}): {nick_error}"
                )
            if needs_role and role is not None:
                try:
                    await self._discord_api_call(
                        member.add_roles, role, reason="人狼: サーバー復帰"
                    )
                except (discord.Forbidden, discord.HTTPException) as role_error:
                    log.warning(
                        f"復帰者への進行中ロール再付与失敗 "
                        f"({player.display_name}): {role_error}"
                    )

        text = await self._handle_disconnect_return(player)
        if text:
            await self._safe_village_send(text)

    # ============================================================
    # メンバー退出検知 (GameCogのリスナーから各卓へdispatchされる)
    # ============================================================

    async def on_member_remove(self, member: discord.Member) -> None:
        state = self.state

        # ロビー中の退出: GM枠/参加枠を解放してUIを更新する
        # (これがないと、退出した人が GM/参加者のままロックされ
        #  ボット再起動以外で復旧できなくなる)
        if state.phase == Phase.LOBBY:
            changed = False
            if state.gm_id == member.id:
                state.gm_id = None
                changed = True
            if member.id in state.players:
                del state.players[member.id]
                changed = True
            if changed and not state.players and state.gm_id is None:
                state.recruitment_id = None
            if changed:
                try:
                    await self._post_lobby_ui()
                    await self._persist_room_state()
                except Exception as e:
                    log.warning(f"ロビーUI更新失敗 (退出処理): {e}")
            return

        # ゲーム中のGM退出: 強制終了
        if state.phase != Phase.GAME_OVER and member.id == state.gm_id:
            if state.ending:
                # 終了処理は既に自走中。勝敗確定済みの結果を
                # 廃村で上書きしない。
                return
            if state.pending_winner is not None:
                # 保存失敗停止中にGMが退出しても、同じrun_idの
                # 精算を自動再試行する。
                winner = state.pending_winner
                if state.game_task is None or state.game_task.done():
                    state.game_task = asyncio.create_task(self._end_game(winner))
                return
            await self.force_end("GMが退出したためゲームを中断します。")
            return

        # ゲーム中の参加者退出: 即死亡ではなく自動一時停止して復帰を待つ
        # (回線トラブル等での誤爆死を防ぐ。戻らない場合はGMが除外して再開する)
        async with self.action_lock:
            state = self.state
            player = state.get_player(member.id)
            if player is not None and player.alive and self._is_game_in_progress():
                await self._pause_for_disconnect(player, "サーバーから退出しました")

    async def _pause_for_disconnect(self, player: Player, what_happened: str) -> None:
        """生存プレイヤーの離脱 (VC切断/サーバー退出) で自動一時停止する。

        既に一時停止中なら復帰待ちリストへの追加と告知だけ行う。
        """
        state = self.state
        already_waiting = player.user_id in state.disconnected_players
        state.disconnected_players.add(player.user_id)
        if not state.paused:
            await self.pause_game()
        elif not already_waiting:
            # 2人目以降の切断はpause_gameが呼ばれないため、
            # 復帰待ち集合の変化をここで永続化する。
            await self._persist_room_state()
        await self._safe_village_send(
            f"🔌 **{player.display_name}** が{what_happened}。復帰を待っています。\n"
            "・復帰したら GM が「再開」を押してください\n"
            "・戻らない場合は GM が「プレイヤー除外」で死亡扱いにしてから「再開」できます"
        )

    async def _handle_disconnect_return(self, player: Player) -> Optional[str]:
        """復帰待ちプレイヤーの復帰を記録し、告知文を返す (復帰待ちでなければ None)"""
        state = self.state
        if player.user_id not in state.disconnected_players:
            return None
        state.disconnected_players.discard(player.user_id)
        # 復帰直後の再起動で「未復帰」へ巻き戻らないよう、
        # 告知やmute同期より前に保存する。
        await self._persist_room_state()
        text = f"🔌 **{player.display_name}** が復帰しました。"
        if state.disconnected_players:
            waiting = ", ".join(
                p.display_name
                for p in by_number(state.players.values())
                if p.user_id in state.disconnected_players
            )
            text += f"\n復帰待ち: {waiting}"
        elif state.paused:
            text += "\n全員復帰しました。GMは「再開」を押してください。"
        return text
