"""1卓ぶんの人狼ゲーム進行 (RoomRunner)

GameCog (game.py) が全卓を束ね、Discordイベントを各卓へdispatchする。
"""
from __future__ import annotations

import asyncio
import logging
import random
import secrets
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Optional

import discord

from config import (
    Role, Team, Phase, ROLE_TEAM,
    DISCORD_MESSAGE_LIMIT,
    PREPARATION_TIME, INITIAL_NIGHT_GREETING_TIME,
    CHANNEL_DELETE_DELAY, VOTE_TIMEOUT, VOTE_SPEECH_TIME,
    VOTE_TRANSITION_GRACE, VOTE_SE_MAX_WAIT,
    LOG_CATEGORY_VILLAGE,
    LOG_CATEGORY_LIMIT, LOG_CATEGORY_TRIM_TO,
    CH_VILLAGE, CH_SPIRIT, CH_LOBBY, VC_GAME,
    RUNOFF_SPEECH_TIME, LAST_WILL_TIME, DISCUSSION_GRACE_TIME,
    MUTE_GRACE_TIME, MUTE_RETRY_DELAY,
    POSTGAME_RECOMMENDATION_TIMEOUT, BONUS_POSTGAME_VOTE,
    WOLF_GUESS_TIMEOUT, BONUS_WOLF_GUESS_DEATH_CAUSES,
    SE_ENABLED,
    ADOPT_EXISTING_LAYOUT,
    GM_INFO_CATEGORY_NAME,
    PRIVATE_ROOM_CREATOR_ROLE_NAMES, PRIVATE_ROOM_CREATOR_ROLE_LABEL,
    RECRUITMENT_UNRANKED_LABEL,
    RoomDefinition, VariantDefinition, get_variant_definition,
    USER_VISIBLE_VARIANT_IDS,
)
from models import Player, GameState, by_number
from views import (
    LobbyView, GMPanelEntryView, VoteView, VoteQueueView,
    SequentialVoteSpeechView, RunoffVoteView,
    WolfVoteView, WolfSurrenderView, SpeechDoneView,
    TurnSpeechView,
    MorningReadyView, PrepReadyView, PostgameVotePanelView,
    VillagePanelView,
    build_vote_result_embed,
)
import database
import rating as rating_lib

if TYPE_CHECKING:
    from discord.ext import commands

    from game import GameCog

log = logging.getLogger(__name__)

# 人狼予想の提出案内。死亡者だけへ届ける手段は個別DMしかないが、DMは廃止した
# ため、既存の死亡告知へ1行だけ相乗りさせる (新しい送信を増やさない)。
# 全員が死亡時に同じ条件で提出するので、公開しても誰の情報も漏れない。
WOLF_GUESS_NOTICE = (
    "🐺 亡くなった方は、このチャンネルの村パネル [人狼予想] から"
    f"{WOLF_GUESS_TIMEOUT // 60}分以内に提出してください"
    "（提出するか時間切れで霊界へ入れます）。"
)


class StateDurabilityError(RuntimeError):
    """進行継続に必要な外部副作用または保存を安全に完了できなかった。

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


# カウントダウン表示を書き換える間隔。メッセージ編集はチャンネル単位の
# バケット (約5回/5秒) を消費し、1ゲーム (13人・5日) で議論・投票・夜・遺言を
# 合わせると800〜1100回に達する。秒読みが要るのは終盤だけなので、
# 残り30秒までは粗く刻む。
TIMER_TICK_PER_SECOND_FROM = 30   # 残りこの秒数からは毎秒
TIMER_TICK_PER_5_SECONDS_FROM = 60  # ここまでは5秒ごと
TIMER_TICK_COARSE_INTERVAL = 30   # それ以前は30秒ごと

# 1人50〜90秒のターンを通常タイマーと同じ粒度で編集すると、13人初日だけで
# 約923回のmessage.editになる。ターン表示は60/30/10/5秒と終了時だけ更新する。
TURN_TIMER_CHECKPOINTS = frozenset({60, 30, 10, 5})


def timer_should_update(display: int, last_display: int) -> bool:
    """カウントダウンの表示を書き換えるべきか。

    毎秒の秒読みを残り60秒から30秒へ縮めることで、1フェーズあたりの
    メッセージ編集が61回から37回へ減る (約4割減)。緊張感が要る最後の
    30秒は毎秒のまま残す。
    """
    if display == last_display:
        return False
    if display == 0 or display <= TIMER_TICK_PER_SECOND_FROM:
        return True
    if display <= TIMER_TICK_PER_5_SECONDS_FROM:
        return display % 5 == 0
    return display % TIMER_TICK_COARSE_INTERVAL == 0


def turn_timer_should_update(display: int, last_display: int) -> bool:
    """短い発言枠向けの疎なタイマー更新判定。"""
    if display == last_display:
        return False
    return display == 0 or display in TURN_TIMER_CHECKPOINTS


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


def _action_subject_id(entry: dict, key: str) -> Optional[int]:
    """現行action logの安定IDだけを統計集計に使う。"""
    value = entry.get(f"{key}_id")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def build_game_stats(
    action_log: list[dict],
    players: list[dict],
    *,
    days: int,
) -> tuple[dict, dict[int, dict]]:
    """進行ログから試合統計とプレイヤーごとの死亡記録を組み立てる。

    Discord表示名は重複・改名し得るため、actor_id/target_idだけを正本にする。
    """
    safe_days = max(0, int(days))
    role_by_id = {int(row["player_id"]): str(row["role"]) for row in players}
    seer_actions: set[tuple] = set()
    guard_success_days: set[int] = set()
    peaceful_days: set[int] = set()
    night_days: set[int] = set()
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

        actor_id = _action_subject_id(entry, "actor")
        target_id = _action_subject_id(entry, "target")
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
            if day == 1 and night1_kill_had_role is None and role is not None:
                night1_kill_had_role = int(role != Role.VILLAGER.value)

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
        # VC復帰・ボタン受付・進行checkpointは別taskから同時に保存される。
        # snapshot構築からDB commitまでを直列化し、古い内容の遅い保存が
        # 新しい受付済み状態を後勝ちで上書きしないようにする。
        self.state_persist_lock = asyncio.Lock()
        # 人狼予想の受付が終わったら霊界を開けるタイマー。GCで消えないよう保持する
        self._spirit_release_tasks: set[asyncio.Task] = set()
        # 終了後投票中に次ゲームを始めると旧#昼の投票パネルが削除されるため、
        # 受付が終わるまでは開始だけを止める。
        self._postgame_vote_pending = False
        # サレンダー成立後の公開・ゲーム停止・通常精算を二重起動しない。
        self._surrender_finish_task: Optional[asyncio.Task] = None
        # サレンダー操作DMは1プロセスにつき各狼へ1通。再起動後は新しい
        # game_run_id照合Viewが必要なので、この集合は永続化しない。
        self._surrender_control_sent_ids: set[int] = set()
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
        # 投票待ちパネルのView。待機中だけ保持し、列が伸びたときの
        # 公開更新でボタンを消さずに貼り直すために使う。
        self._vote_queue_view: Optional[discord.ui.View] = None
        # #昼 の常設村パネル (CO/役職行動の入口)。昼夜をまたいで
        # 生きるため、朝パネルやターン話者パネルと違いゲーム中は作り直さない。
        self._village_panel_view: Optional[VillagePanelView] = None
        # CO等の連続押下でedit APIを連打しないための最新1件デバウンス。
        self._village_panel_refresh_task: Optional[asyncio.Task] = None
        self._village_panel_refresh_pending = False
        # 終了直後のDiscord一時障害で参加受付パネルを失わないための
        # バックグラウンド再掲。stateが次へ変われば自動終了する。
        self._lobby_panel_recovery_task: Optional[asyncio.Task] = None
        # 名前村の強制終了後に、前回設定の0人募集カードを再公開する。
        # DB上の待機フラグを正本にし、再起動後も同じ処理を再開する。
        self._recruitment_recovery_task: Optional[asyncio.Task] = None

    def is_private_room(self) -> bool:
        """GMが作成した名前村か。保存済みの村主IDで判定する。"""
        return self.room_def.private_owner_id is not None

    def uses_manual_static_permissions(self) -> bool:
        """カテゴリ・参加受付・VCの平常時権限をDiscordへ委ねる卓か。"""
        return not getattr(self.room_def, "sync_permissions", True)

    def _configured_public_access_boundary(self) -> bool:
        """開始時点でカテゴリ・VCの閲覧境界が一般公開だった卓か。"""
        return not bool(
            getattr(self.room_def, "strict_access_role_names", None)
            or getattr(self.room_def, "access_role_names", None)
            or self.uses_manual_static_permissions()
        )

    @staticmethod
    def _carry_pending_vc_restore(source: GameState, target: GameState) -> None:
        """Discord一時失敗で未完のVC復元記録を次ロビーへ引き継ぐ。"""
        target.vc_default_permissions_captured = (
            source.vc_default_permissions_captured
        )
        target.vc_default_speak_before_game = source.vc_default_speak_before_game
        target.vc_default_send_before_game = source.vc_default_send_before_game
        target.vc_gm_speak_captured = source.vc_gm_speak_captured
        target.vc_gm_speak_user_id = source.vc_gm_speak_user_id
        target.vc_gm_speak_before_game = source.vc_gm_speak_before_game

    _PENDING_CHANNEL_UI_FIELDS = (
        "morning_panel_message_id",
        "prep_panel_message_id",
        "speech_panel_message_id",
        "turn_panel_message_id",
        "vote_panel_message_id",
        "village_panel_message_id",
    )
    _PENDING_CHANNEL_UI_DELETE_FIELDS = frozenset({
        "morning_panel_message_id",
        "prep_panel_message_id",
        "village_panel_message_id",
    })

    def _ui_cleanup_channel(self):
        """現行ゲームの#昼だけを返す。旧ゲームの掃除先とは混ぜない。"""
        return self.state.village_channel

    @staticmethod
    def _copy_dm_cleanup_ids(mapping: object) -> dict[int, list[int]]:
        """保留掃除用DM IDを、参照共有せず正整数だけ複製する。"""
        if not isinstance(mapping, dict):
            return {}
        copied: dict[int, list[int]] = {}
        for user_id, message_ids in mapping.items():
            if (
                isinstance(user_id, bool)
                or not isinstance(user_id, int)
                or user_id <= 0
                or not isinstance(message_ids, list)
            ):
                continue
            valid_ids = [
                message_id
                for message_id in message_ids
                if isinstance(message_id, int)
                and not isinstance(message_id, bool)
                and message_id > 0
            ]
            if valid_ids:
                copied[user_id] = list(dict.fromkeys(valid_ids))
        return copied

    @classmethod
    def _append_pending_ui_cleanup_batch(
        cls,
        batches: list[dict],
        batch: dict,
    ) -> None:
        """同じ試合・チャンネルの互換バッチを安全に統合して二重登録を防ぐ。"""
        game_run_id = batch.get("game_run_id")
        if not isinstance(game_run_id, str) or not game_run_id:
            game_run_id = None
        channel_id = batch.get("channel_id")
        if (
            isinstance(channel_id, bool)
            or not isinstance(channel_id, int)
            or channel_id <= 0
        ):
            channel_id = None
        raw_channel_ids = batch.get("channel_message_ids", {})
        channel_message_ids = {
            field_name: message_id
            for field_name, message_id in (
                raw_channel_ids.items()
                if isinstance(raw_channel_ids, dict)
                else ()
            )
            if field_name in cls._PENDING_CHANNEL_UI_FIELDS
            and isinstance(message_id, int)
            and not isinstance(message_id, bool)
            and message_id > 0
        }
        wolf_ids = cls._copy_dm_cleanup_ids(
            batch.get("wolf_dm_message_ids", {})
        )
        surrender_ids = cls._copy_dm_cleanup_ids(
            batch.get("surrender_dm_message_ids", {})
        )
        if not (channel_message_ids or wolf_ids or surrender_ids):
            return

        for existing in batches:
            if (
                existing.get("game_run_id") != game_run_id
                or existing.get("channel_id") != channel_id
            ):
                continue
            existing_channel_ids = existing["channel_message_ids"]
            if any(
                field_name in existing_channel_ids
                and existing_channel_ids[field_name] != message_id
                for field_name, message_id in channel_message_ids.items()
            ):
                # 同じ種類の別メッセージは両方掃除する必要があるため、
                # 1値のmapへ潰さず別バッチのまま残す。
                continue
            existing_channel_ids.update(channel_message_ids)
            for key, incoming in (
                ("wolf_dm_message_ids", wolf_ids),
                ("surrender_dm_message_ids", surrender_ids),
            ):
                target_mapping = existing[key]
                for user_id, message_ids in incoming.items():
                    target_mapping[user_id] = list(dict.fromkeys(
                        target_mapping.get(user_id, []) + message_ids
                    ))
            return

        batches.append({
            "game_run_id": game_run_id,
            "channel_id": channel_id,
            "channel_message_ids": channel_message_ids,
            "wolf_dm_message_ids": wolf_ids,
            "surrender_dm_message_ids": surrender_ids,
        })

    def _carry_pending_ui_cleanup(
        self,
        source: GameState,
        target: GameState,
        *,
        channel_id_override: Optional[int] = None,
    ) -> None:
        """旧ゲームの掃除失敗を、現在UIと独立したバッチで引き継ぐ。"""
        target.pending_ui_cleanup_batches = []
        for batch in getattr(source, "pending_ui_cleanup_batches", []):
            if not isinstance(batch, dict):
                continue
            self._append_pending_ui_cleanup_batch(
                target.pending_ui_cleanup_batches,
                batch,
            )

        current_channel_ids = {
            field_name: message_id
            for field_name in self._PENDING_CHANNEL_UI_FIELDS
            if isinstance((message_id := getattr(source, field_name, None)), int)
            and not isinstance(message_id, bool)
            and message_id > 0
        }
        current_wolf_ids = self._copy_dm_cleanup_ids(source.wolf_dm_message_ids)
        current_surrender_ids = self._copy_dm_cleanup_ids(
            source.surrender_dm_message_ids
        )
        if current_channel_ids or current_wolf_ids or current_surrender_ids:
            channel_id = channel_id_override or getattr(
                source.village_channel, "id", None
            )
            if (
                isinstance(channel_id, bool)
                or not isinstance(channel_id, int)
                or channel_id <= 0
            ):
                channel_id = None
            self._append_pending_ui_cleanup_batch(
                target.pending_ui_cleanup_batches,
                {
                    "game_run_id": source.game_run_id or None,
                    "channel_id": channel_id,
                    "channel_message_ids": current_channel_ids,
                    "wolf_dm_message_ids": current_wolf_ids,
                    "surrender_dm_message_ids": current_surrender_ids,
                },
            )

    def _has_pending_ui_cleanup(self) -> bool:
        return bool(getattr(self.state, "pending_ui_cleanup_batches", []))

    def _make_empty_lobby_state(
        self,
        source: GameState,
        *,
        nickname_failures: Optional[dict[int, Optional[str]]] = None,
        cleanup_channel_id: Optional[int] = None,
        preserve_players: bool = False,
        preserve_gm: bool = False,
    ) -> GameState:
        """終了済みstateから、指定した受付登録だけを持つLOBBYを作る。"""
        if preserve_players and not preserve_gm:
            raise ValueError("参加者を残す場合はGMも残す必要があります")
        target = GameState()
        target.room_id = source.room_id
        target.room_name = source.room_name
        target.guild = source.guild
        target.category = source.category
        target.lobby_channel = source.lobby_channel
        target.stats_channel = source.stats_channel
        target.voice_channel = source.voice_channel
        # 新しいパネルをsend-firstで確保できるまでは、従来の参照を残す。
        target.lobby_message = source.lobby_message
        target.original_nicknames = dict(nickname_failures or {})
        if preserve_gm:
            target.gm_id = source.gm_id
        if preserve_players:
            # 役職・生死・番号など前ゲームの情報は一切持ち越さず、
            # 参加順とDiscordメンバーだけを新しい受付へ戻す。
            for player in source.players.values():
                member = (
                    source.guild.get_member(player.user_id)
                    if source.guild is not None
                    else None
                ) or player.member
                target.players[player.user_id] = Player(
                    user_id=player.user_id,
                    member=member,
                    original_nickname=player.original_nickname,
                )
        target.pending_recruitment_reopen = bool(
            source.pending_recruitment_reopen
            and preserve_gm
            and not preserve_players
            and self.is_private_room()
        )
        if target.pending_recruitment_reopen:
            target.lobby_return_mode = "gm"
        self._carry_pending_vc_restore(source, target)
        self._carry_pending_ui_cleanup(
            source,
            target,
            channel_id_override=cleanup_channel_id,
        )
        target.managed_game_channel_ids = set(source.managed_game_channel_ids)
        return target

    def _build_snapshot_for_state(self, state: GameState) -> dict:
        """awaitを挟まず、指定stateのsnapshotを組み立てる。"""
        current = self.state
        self.state = state
        try:
            return self._build_room_snapshot()
        finally:
            self.state = current

    async def _transition_to_empty_lobby(
        self,
        source: GameState,
        *,
        nickname_failures: Optional[dict[int, Optional[str]]] = None,
        cleanup_channel_id: Optional[int] = None,
        log_label: str,
        preserve_players: bool = False,
        preserve_gm: bool = False,
    ) -> bool:
        """募集終了とLOBBY保存を原子的に確定してからstateを差し替える。"""
        if source.guild is None:
            log.error("%s: guildが無いためLOBBYへ移行できません", log_label)
            return False
        target = self._make_empty_lobby_state(
            source,
            nickname_failures=nickname_failures,
            cleanup_channel_id=cleanup_channel_id,
            preserve_players=preserve_players,
            preserve_gm=preserve_gm,
        )
        payload = self._build_snapshot_for_state(target)
        recruitment_id = source.recruitment_id
        transitioned = False
        for attempt, delay in enumerate((0, 1, 2), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with self.state_persist_lock:
                    if self.state is not source:
                        log.warning("%s: stateが先に差し替わったため中断", log_label)
                        return False
                    await database.archive_linked_recruitment_and_save_lobby_state(
                        source.guild.id,
                        source.room_id,
                        recruitment_id,
                        payload,
                        preserve_players=preserve_players,
                        preserve_gm=preserve_gm,
                    )
                    # DBが確定する前にメモリだけ空LOBBYへ進めない。
                    self.state = target
                transitioned = True
                break
            except Exception as error:
                log.exception(
                    "%sの原子保存失敗 (%s/3): %s",
                    log_label,
                    attempt,
                    error,
                )
        if not transitioned:
            return False

        # 空LOBBY snapshotへ引き継いだ後始末IDですぐ再試行する。
        # 一時失敗が続いてもIDはLOBBYに残り、再起動時にまた試せる。
        await self._retry_pending_ui_cleanup()

        panel_ready = False
        for attempt, delay in enumerate((0, 1, 2), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._post_lobby_ui()
                panel_ready = True
                break
            except Exception as error:
                log.exception(
                    "%s後の参加受付パネル再掲失敗 (%s/3): %s",
                    log_label,
                    attempt,
                    error,
                )
        if panel_ready and recruitment_id is not None:
            try:
                # 新パネルを確保し、旧カードをpurgeした後に追跡IDを外す。
                await database.clear_recruitment_message_id(recruitment_id)
            except Exception as error:
                log.warning(
                    "%s後の旧募集カードID解除失敗 (%s): %s",
                    log_label,
                    recruitment_id,
                    error,
                )
        elif not panel_ready:
            self._schedule_lobby_panel_recovery(
                target,
                recruitment_id=recruitment_id,
                log_label=log_label,
            )
        if target.pending_recruitment_reopen:
            self._schedule_recruitment_recovery(target)
        return True

    def _schedule_lobby_panel_recovery(
        self,
        expected_state: GameState,
        *,
        recruitment_id: Optional[int],
        log_label: str,
    ) -> None:
        """参加受付パネルをDiscord復旧まで再掲し続ける。"""
        current = self._lobby_panel_recovery_task
        if current is not None and not current.done():
            current.cancel()
        task = self.manager.spawn_bg_task(
            self._recover_lobby_panel(
                expected_state,
                recruitment_id=recruitment_id,
                log_label=log_label,
            )
        )
        self._lobby_panel_recovery_task = (
            task if isinstance(task, asyncio.Task) else None
        )

    async def _recover_lobby_panel(
        self,
        expected_state: GameState,
        *,
        recruitment_id: Optional[int],
        log_label: str,
    ) -> None:
        """終了直後の一時障害から参加・取消ボタンを自動回復する。"""
        delay = 5
        try:
            while self.state is expected_state and expected_state.phase == Phase.LOBBY:
                await asyncio.sleep(delay)
                try:
                    await self._post_lobby_ui()
                except Exception as error:
                    log.exception(
                        "%s後の参加受付パネルを再試行できませんでした: %s",
                        log_label,
                        error,
                    )
                    delay = min(delay * 2, 60)
                    continue
                if recruitment_id is not None:
                    try:
                        await database.clear_recruitment_message_id(recruitment_id)
                    except Exception as error:
                        log.warning(
                            "%s後の旧募集カードID解除失敗 (%s): %s",
                            log_label,
                            recruitment_id,
                            error,
                        )
                return
        finally:
            if self._lobby_panel_recovery_task is asyncio.current_task():
                self._lobby_panel_recovery_task = None

    def _schedule_recruitment_recovery(self, expected_state: GameState) -> None:
        """名前村の強制終了後募集を、成功するまでバックグラウンド回収する。"""
        manager = getattr(self.manager, "recruitment_manager", None)
        recover = getattr(manager, "recover_pending_recruitment", None)
        if not callable(recover):
            log.error(
                "参加受付の自動再開機能を利用できません (%s)",
                expected_state.room_name,
            )
            return
        current = self._recruitment_recovery_task
        if current is not None and not current.done():
            return
        task = self.manager.spawn_bg_task(
            self._recover_pending_recruitment(expected_state, recover)
        )
        self._recruitment_recovery_task = (
            task if isinstance(task, asyncio.Task) else None
        )

    async def _recover_pending_recruitment(
        self,
        expected_state: GameState,
        recover,
    ) -> None:
        """Discord/DBの一時障害と再起動をまたいで募集カードを再公開する。"""
        delay = 1
        try:
            while (
                self.state is expected_state
                and expected_state.phase == Phase.LOBBY
                and expected_state.pending_recruitment_reopen
            ):
                await asyncio.sleep(delay)
                try:
                    result = await recover(self)
                except Exception as error:
                    log.exception(
                        "強制終了後の参加受付を自動再開できませんでした: %s",
                        error,
                    )
                else:
                    if expected_state.pending_recruitment_reopen:
                        log.warning(
                            "強制終了後の参加受付再開を継続します: %s",
                            result,
                        )
                if not expected_state.pending_recruitment_reopen:
                    return
                delay = min(delay * 2, 60)
        finally:
            if self._recruitment_recovery_task is asyncio.current_task():
                self._recruitment_recovery_task = None

    @property
    def variant(self) -> VariantDefinition:
        return get_variant_definition(self.room_def.variant_id)

    def is_turn_discussion_mode(self) -> bool:
        return self.variant.discussion_mode == "turn"

    def uses_sequential_vote(self) -> bool:
        """通常投票を「投票発言」(1人ずつ) で行う変種か。

        クロストークは議論の流れをそのまま投票へ繋げるため1人ずつ発言して
        確定する。ターン制は発言順そのものが進行の骨格で、投票まで順番に
        すると1日が長くなりすぎるため、規定の発言を終えてから一斉に投票する。
        決戦投票はどちらも一斉、決戦弁明はどちらも1人30秒で共通。
        """
        return not self.is_turn_discussion_mode()

    async def change_lobby_variant(self, actor_id: int, variant_id: str) -> str:
        """GM村の開始前形式を、募集カードと同じtransactionで変更する。"""
        if not self.is_private_room():
            return "ゲーム形式を変更できるのはGM村だけです。"
        if variant_id not in USER_VISIBLE_VARIANT_IDS:
            return "公開されていないゲーム形式には変更できません。"
        # 募集の参加・取消・開催と同じ manager→room の順で固定し、
        # 変更前の定員で確定したrosterを新形式へ持ち込む競合を作らない。
        async with self.manager.recruitment_manager.lock, self.action_lock:
            state = self.state
            if state.phase != Phase.LOBBY or self._is_game_in_progress():
                return "ゲーム開始後は形式を変更できません。"
            if state.players:
                return "参加者を確定した後はゲーム形式を変更できません。"
            if state.recruitment_id is not None:
                linked_recruitment = await database.get_recruitment(
                    int(state.recruitment_id)
                )
                if (
                    linked_recruitment is None
                    or linked_recruitment["status"] != database.RECRUITMENT_OPEN
                ):
                    return (
                        "開催処理中または開催済みの募集が紐づいているため、"
                        "ゲーム形式を変更できません。"
                    )
            if actor_id not in {state.gm_id, self.room_def.private_owner_id}:
                return "現在のGMだけがゲーム形式を変更できます。"
            new_variant = get_variant_definition(variant_id)
            if len(state.players) > new_variant.player_count:
                return (
                    f"参加者が{new_variant.player_count}人を超えているため変更できません。"
                    "参加者を自動では外しません。"
                )
            old_variant_id = self.room_def.variant_id
            if old_variant_id == variant_id:
                return f"現在も **{new_variant.label}** です。"
            guild = state.guild
            if guild is None or self.room_def.private_owner_id is None:
                return "村の保存先を確認できないため変更できません。"
            open_recruitment = await database.get_open_recruitment_for_room(
                guild.id, state.room_id,
            )
            before_entry_kinds: dict[int, str] = {}
            if open_recruitment is not None:
                before_entries = await database.list_recruitment_entries(
                    int(open_recruitment["id"])
                )
                before_entry_kinds = {
                    int(entry["user_id"]): str(entry["kind"])
                    for entry in before_entries
                }
                allowed_ranks = open_recruitment["allowed_ranks"]
                if allowed_ranks is not None:
                    rank_map = await database.get_current_rank_map(
                        guild.id, new_variant.ladder_id,
                    )
                    ineligible_ids = [
                        user_id
                        for user_id in before_entry_kinds
                        if (
                            rank_map[user_id].rank_name
                            if user_id in rank_map
                            else RECRUITMENT_UNRANKED_LABEL
                        ) not in allowed_ranks
                    ]
                    if ineligible_ids:
                        names = [
                            (
                                member.display_name
                                if (member := guild.get_member(user_id)) is not None
                                else f"ID:{user_id}"
                            )
                            for user_id in ineligible_ids
                        ]
                        return (
                            "変更先ラダーでは募集の参加ランク条件外になる人がいます: "
                            + ", ".join(names)
                            + "。参加者・補欠を自動では外しません。"
                        )
            variant_db_changed = False
            recruitment_id: Optional[int] = None
            try:
                recruitment_id = (
                    await database.update_private_room_and_open_recruitment_variant(
                        guild.id,
                        state.room_id,
                        self.room_def.private_owner_id,
                        variant_id,
                    )
                )
                variant_db_changed = True
                self.room_def = replace(self.room_def, variant_id=variant_id)
                await self._persist_room_state()
            except database.RecruitmentConflict as exc:
                return str(exc)
            except Exception as exc:
                self.room_def = replace(self.room_def, variant_id=old_variant_id)
                if variant_db_changed:
                    try:
                        if recruitment_id is None:
                            await database.update_private_room_variant(
                                guild.id, state.room_id, old_variant_id,
                            )
                        else:
                            await database.restore_private_room_and_open_recruitment_variant(
                                guild.id,
                                state.room_id,
                                self.room_def.private_owner_id,
                                recruitment_id,
                                old_variant_id,
                                before_entry_kinds,
                            )
                    except Exception:
                        log.exception(
                            "GM村形式変更の巻き戻しに失敗 (%s)", state.room_id
                        )
                log.exception("GM村形式変更の保存に失敗 (%s): %s", state.room_id, exc)
                detail = f" ({exc})" if isinstance(exc, RuntimeError) else ""
                return "ゲーム形式を安全に保存できなかったため変更しませんでした。" + detail

        if recruitment_id is not None:
            await self.manager.recruitment_manager.refresh_message(recruitment_id)
            await self.manager.recruitment_manager.notify_ready_if_needed(
                await database.get_recruitment(recruitment_id)
            )
        else:
            await self._post_lobby_ui()
        return f"✅ ゲーム形式を **{new_variant.label}** へ変更しました。"

    def is_rated_room(self) -> bool:
        # 正常終了した全村を対象にする。作成時にIDが決まるGM名前村は
        # private属性、固定・ローカル卓は**自分が持つ定義**で判定する。
        # IDでグローバル集合を引くと、同じroom_idの別定義 (環境ごとに
        # rated が違うローカル卓など) を取り違える。
        return self.is_private_room() or (
            self.room_def.enabled and self.room_def.rated
        )

    def turn_actions_open(self) -> bool:
        """現在の発言枠がターン用ボタンを受け付けるか。"""
        state = self.state
        return (
            self.is_turn_discussion_mode()
            and self._effective_phase() == Phase.DAY_DISCUSSION
            and state.turn_slot_active
            and state.turn_window_open
            and state.current_speaker_id is not None
            and not state.paused
            and not state.ending
            and state.pending_winner is None
        )

    # 旧CO機構 (_turn_co_declaration_round_index / turn_co_declaration_open) は
    # 撤去した。CO宣言は #昼 常設パネル (VillagePanelView) の [CO] ボタンへ
    # 一本化している (declare_co / withdraw_co を参照)。
    # ただし state.turn_co_declarations フィールドと _validate_turn_snapshot
    # の検証は旧 snapshot 互換のため残す (今後は常に空になる)。

    def register_game_view(self, view: discord.ui.View, *, night: bool = False) -> None:
        """ゲーム進行Viewを終了時の一括停止対象へ登録する。"""
        if view not in self._game_views:
            self._game_views.append(view)
        if night and view not in self._night_views:
            self._night_views.append(view)

    @staticmethod
    def _remember_dm_view_message(
        message_ids_by_user: dict[int, list[int]], user_id: int, message,
    ) -> bool:
        """DM Viewを後で表示上も閉じられるよう、メッセージIDを控える。"""
        message_id = getattr(message, "id", None)
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
        ):
            return False
        ids = message_ids_by_user.setdefault(user_id, [])
        if message_id in ids:
            return False
        ids.append(message_id)
        return True

    async def _disable_persisted_dm_views(
        self, message_ids_by_user: dict[int, list[int]], *, label: str,
    ) -> None:
        """再起動をまたいだDMコンポーネントを、可能な範囲で取り外す。

        DiscordはDMメッセージのViewだけを直接無効化できないため、対象メッセージを
        ``view=None`` で編集してボタン自体を消す。DM作成/取得・編集のどこかが
        失敗してもゲーム進行は止めず、次の終了・復元で再試行できるようIDは残す。
        """
        state = self.state

        async def disable_one(user_id: int, message_id: int) -> tuple[int, int, bool]:
            player = state.get_player(user_id)
            guild = state.guild
            member = getattr(player, "member", None)
            if member is None and guild is not None:
                get_member = getattr(guild, "get_member", None)
                if callable(get_member):
                    member = get_member(user_id)
            if member is None:
                bot = getattr(self.manager, "bot", None)
                get_user = getattr(bot, "get_user", None)
                if callable(get_user):
                    member = get_user(user_id)
                if member is None:
                    fetch_user = getattr(bot, "fetch_user", None)
                    if callable(fetch_user):
                        try:
                            member = await self._discord_api_call(fetch_user, user_id)
                        except discord.NotFound:
                            # ユーザー自体が存在しなければ、そのDM操作口も残らない。
                            return user_id, message_id, True
                        except (discord.Forbidden, discord.HTTPException) as error:
                            log.warning(
                                "%s DMユーザーの取得に失敗 (ID:%s): %s",
                                label, user_id, error,
                            )
                            return user_id, message_id, False
            if member is None:
                return user_id, message_id, False
            display_name = (
                getattr(player, "display_name", None)
                or getattr(member, "display_name", None)
                or f"ID:{user_id}"
            )
            channel = getattr(member, "dm_channel", None)
            if channel is None:
                create_dm = getattr(member, "create_dm", None)
                if not callable(create_dm):
                    return user_id, message_id, False
                try:
                    channel = await self._discord_api_call(create_dm)
                except (discord.Forbidden, discord.HTTPException) as error:
                    log.warning(
                        "%s DMチャンネルの取得に失敗 (%s): %s",
                        label, display_name, error,
                    )
                    return user_id, message_id, False
            get_partial = getattr(channel, "get_partial_message", None)
            if not callable(get_partial):
                return user_id, message_id, False
            try:
                await self._discord_api_call(get_partial(message_id).edit, view=None)
            except discord.NotFound:
                return user_id, message_id, True
            except (discord.Forbidden, discord.HTTPException) as error:
                log.warning(
                    "%s DM操作の無効化に失敗 (%s): %s",
                    label, display_name, error,
                )
                return user_id, message_id, False
            return user_id, message_id, True

        targets = [
            (user_id, message_id)
            for user_id, message_ids in message_ids_by_user.items()
            for message_id in message_ids
        ]
        if not targets:
            return
        results = await asyncio.gather(
            *(disable_one(user_id, message_id) for user_id, message_id in targets)
        )
        completed = {
            (user_id, message_id)
            for user_id, message_id, success in results if success
        }
        if not completed:
            return
        for user_id, message_ids in list(message_ids_by_user.items()):
            remaining = [
                message_id for message_id in message_ids
                if (user_id, message_id) not in completed
            ]
            if remaining:
                message_ids_by_user[user_id] = remaining
            else:
                message_ids_by_user.pop(user_id, None)

    async def _disable_live_wolf_vote_dms(self) -> None:
        """このプロセスで配った襲撃DMから、夜終了前にViewを取り外す。"""
        state = self.state
        messages = list(state.wolf_dm_messages.items())
        if not messages:
            return

        async def disable_one(user_id: int, message) -> tuple[int, Optional[int], bool]:
            message_id = getattr(message, "id", None)
            try:
                await self._discord_api_call(message.edit, view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                log.warning("人狼襲撃DM操作の無効化に失敗 (ID:%s): %s", user_id, error)
                return user_id, message_id, False
            return user_id, message_id, True

        results = await asyncio.gather(
            *(disable_one(user_id, message) for user_id, message in messages)
        )
        for user_id, message_id, success in results:
            if not success or not isinstance(message_id, int):
                continue
            ids = state.wolf_dm_message_ids.get(user_id, [])
            state.wolf_dm_message_ids[user_id] = [
                recorded_id for recorded_id in ids if recorded_id != message_id
            ]
            if not state.wolf_dm_message_ids[user_id]:
                state.wolf_dm_message_ids.pop(user_id, None)
        state.wolf_dm_messages.clear()

    async def _close_game_views_for_shutdown(self) -> None:
        """通常終了・強制終了・GAME_OVER復旧で、古い操作口を表示から外す。"""
        state = self.state
        await self._disable_live_wolf_vote_dms()
        await self._disable_persisted_dm_views(
            state.wolf_dm_message_ids, label="人狼襲撃",
        )
        await self._disable_persisted_dm_views(
            state.surrender_dm_message_ids, label="サレンダー",
        )
        await self._close_morning_panel()
        await self._close_prep_panel()
        await self._disable_recovered_speech_panel()
        await self._disable_recovered_turn_panel(clear_on_success=True)
        await self._disable_recovered_vote_panel()
        await self.close_village_panel()
        self._stop_all_game_views()

    async def _retry_pending_ui_cleanup(self) -> None:
        """旧ゲームごとのDM/パネルを再掃除し、失敗分だけ保存する。"""
        state = self.state
        if not self._has_pending_ui_cleanup():
            return

        remaining_batches: list[dict] = []
        guild = state.guild
        for raw_batch in list(getattr(state, "pending_ui_cleanup_batches", [])):
            if not isinstance(raw_batch, dict):
                continue
            game_run_id = raw_batch.get("game_run_id")
            if not isinstance(game_run_id, str) or not game_run_id:
                game_run_id = None
            channel_id = raw_batch.get("channel_id")
            channel_message_ids = {
                field_name: message_id
                for field_name, message_id in (
                    raw_batch.get("channel_message_ids", {}).items()
                    if isinstance(raw_batch.get("channel_message_ids"), dict)
                    else ()
                )
                if field_name in self._PENDING_CHANNEL_UI_FIELDS
                and isinstance(message_id, int)
                and not isinstance(message_id, bool)
                and message_id > 0
            }
            wolf_ids = self._copy_dm_cleanup_ids(
                raw_batch.get("wolf_dm_message_ids", {})
            )
            surrender_ids = self._copy_dm_cleanup_ids(
                raw_batch.get("surrender_dm_message_ids", {})
            )

            channel = None
            channel_lookup_available = False
            valid_channel_id = (
                isinstance(channel_id, int)
                and not isinstance(channel_id, bool)
                and channel_id > 0
            )
            if guild is not None:
                get_channel = getattr(guild, "get_channel", None)
                if callable(get_channel):
                    channel_lookup_available = True
                    if valid_channel_id:
                        channel = get_channel(channel_id)
            if (
                channel_message_ids
                and valid_channel_id
                and channel_lookup_available
                and channel is None
            ):
                # 保存した#昼が既に削除済みなら、中のボタンも存在しない。
                channel_message_ids.clear()
            elif channel_message_ids and channel is not None:
                get_partial_message = getattr(channel, "get_partial_message", None)
                if callable(get_partial_message):
                    for field_name, message_id in list(channel_message_ids.items()):
                        message = get_partial_message(message_id)
                        try:
                            if field_name in self._PENDING_CHANNEL_UI_DELETE_FIELDS:
                                await self._discord_api_call(message.delete)
                            else:
                                await self._discord_api_call(message.edit, view=None)
                        except discord.NotFound:
                            channel_message_ids.pop(field_name, None)
                        except (discord.Forbidden, discord.HTTPException) as error:
                            log.warning(
                                "旧ゲームUIの後始末に失敗 (%s, %s, %s): %s",
                                state.room_name,
                                channel_id,
                                field_name,
                                error,
                            )
                        else:
                            channel_message_ids.pop(field_name, None)

            await self._disable_persisted_dm_views(
                wolf_ids, label="旧ゲーム人狼襲撃",
            )
            await self._disable_persisted_dm_views(
                surrender_ids, label="旧ゲームサレンダー",
            )
            if channel_message_ids or wolf_ids or surrender_ids:
                self._append_pending_ui_cleanup_batch(remaining_batches, {
                    "game_run_id": game_run_id,
                    "channel_id": (
                        channel_id
                        if isinstance(channel_id, int)
                        and not isinstance(channel_id, bool)
                        and channel_id > 0
                        else None
                    ),
                    "channel_message_ids": channel_message_ids,
                    "wolf_dm_message_ids": wolf_ids,
                    "surrender_dm_message_ids": surrender_ids,
                })

        state.pending_ui_cleanup_batches = remaining_batches
        try:
            await self._persist_room_state()
        except Exception as error:
            # 掃除は完了していても、保存失敗時は次回も再実行するだけで安全。
            log.warning("UI後始末の再試行結果を保存できません (%s): %s", state.room_name, error)

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
        # PREPARATION中の初日挨拶はnight viewを作らないため、全停止側でも
        # 中継窓を明示的に閉じる。
        self.state.wolf_relay_window_open = False
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
        return any(
            role.name in PRIVATE_ROOM_CREATOR_ROLE_NAMES
            for role in getattr(member, "roles", [])
        )

    # ============================================================
    # セットアップ
    # ============================================================

    async def _restore_active_private_room_visibility(
        self,
        guild: discord.Guild,
        channel_ids: dict,
    ) -> None:
        """進行中GM村の公開観戦権限を、個人制御を残したまま復元する。

        復元処理が完了した後、保存済みIDでBot所有を確認できる進行中の
        #昼/#霊界と、現在のカテゴリ・受付・VCを公開基準へ収束させる。
        生存者の霊界denyや生存ロールの書込許可は保持する。復元中にゲームが
        終了した場合は、まだ元カテゴリにある終了チャンネルも退避まで
        読み取り専用で公開する。
        """
        if not self.is_private_room():
            return

        async def sync_targets(channel, desired) -> None:
            if channel is None:
                return
            for target in (guild.default_role, guild.me):
                try:
                    await self.manager._set_permission_if_changed(
                        channel,
                        target,
                        desired[target],
                        reason="GM村の公開観戦権限を復元",
                    )
                except (discord.Forbidden, discord.HTTPException) as error:
                    raise RuntimeError(
                        f"{self.room_def.name}/{getattr(channel, 'name', 'カテゴリ')} "
                        "の公開観戦権限を復元できません"
                    ) from error

        # カテゴリを先に公開すると、子チャンネルの個別制御より先に見える。
        # 子を安全な完成形へ収束させ、最後にカテゴリの公開観戦権限を復元する。
        lobby = self.state.lobby_channel
        if lobby is not None:
            await sync_targets(
                lobby,
                self.manager._build_room_overwrites(
                    guild, self.room_def, send_messages=False,
                ),
            )

        game_still_active = self.state.phase not in (Phase.LOBBY, Phase.GAME_OVER)
        vc = self.state.voice_channel
        if vc is not None:
            default_vc = vc.overwrites_for(guild.default_role)
            default_vc.view_channel = True
            default_vc.read_messages = True
            default_vc.connect = True
            default_vc.speak = False if game_still_active else None
            default_vc.send_messages = False if game_still_active else None
            bot_vc = vc.overwrites_for(guild.me)
            bot_vc.view_channel = True
            bot_vc.read_messages = True
            bot_vc.connect = True
            bot_vc.speak = True
            bot_vc.send_messages = True
            bot_vc.manage_channels = True
            await sync_targets(
                vc,
                {guild.default_role: default_vc, guild.me: bot_vc},
            )

        for key, village in (("village", True), ("spirit", False)):
            channel_id = channel_ids.get(key)
            if not isinstance(channel_id, int):
                continue
            channel = next(
                (item for item in guild.text_channels if item.id == channel_id),
                None,
            )
            if channel is None:
                continue
            desired = self.manager._build_room_overwrites(
                guild,
                self.room_def,
                # 進行中の霊界だけ会話可。復元中に終了・廃村へ移った
                # チャンネルは、削除/公開ログ退避まで読み取り専用にする。
                send_messages=(not village and game_still_active),
            )
            await sync_targets(channel, desired)

        await sync_targets(
            self.state.category,
            self.manager._build_room_overwrites(guild, self.room_def),
        )

    async def _new_category_position(
        self, guild: discord.Guild,
    ) -> Optional[int]:
        """GM名前村を GM カテゴリのすぐ下へ作るための position を返す。

        position を渡さないとDiscordはサーバー最下部へ足すため、作った本人が
        一番下まで辿らないと自分の村を見つけられない。**新しい村ほど上** と
        なる作成順に積むのは、開始時刻順にすると募集の時刻変更や試合ごとに
        既存カテゴリの並べ替えPATCHが増えるため。作成時の1パラメータで済み、
        既にある村のpositionを一切触らないこの方式を採る。

        固定卓と、GMカテゴリを特定できない場合は従来どおり最下部へ作る。
        """
        if not self.is_private_room():
            return None
        anchor = None
        try:
            stored_id = await database.get_meta(guild.id, "gm_hub_category_id")
        except Exception as e:
            log.warning(f"GMカテゴリIDを取得できません: {e}")
            stored_id = None
        if stored_id and str(stored_id).isdigit():
            candidate = guild.get_channel(int(stored_id))
            if isinstance(candidate, discord.CategoryChannel):
                anchor = candidate
        if anchor is None:
            anchor = discord.utils.get(
                guild.categories, name=GM_INFO_CATEGORY_NAME,
            )
        position = getattr(anchor, "position", None)
        if not isinstance(position, int):
            return None
        return position + 1

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

        channel_ids = snapshot.get("channel_ids", {}) if snapshot else {}
        active_snapshot = (
            snapshot is not None
            and snapshot.get("phase") not in (Phase.LOBBY.name, Phase.GAME_OVER.name)
        )
        # public_log_archive_allowedは、ここでは進行中カテゴリ・VCのアクセス境界を
        # 守るためだけに使う。終了ログはこの値にかかわらず全村で公開ログへ退避する。
        # DB境界では現行snapshotの必須キーだが、直接呼出しで欠損しても公開側へ
        # 倒さない。固定卓は保存済みカテゴリのアクセス境界を維持する。
        # GM名前村は公開観戦型なので、復元完了後に現在の公開基準へ収束させる。
        preserve_snapshot_access_boundary = (
            active_snapshot
            and not self.is_private_room()
            and snapshot.get("public_log_archive_allowed") is not True
        )

        # 保存済みIDを所有境界の正本にする。新規導入先の同名カテゴリは、
        # 明示採用なしに権限変更・残骸削除の対象へしない。
        saved_category_id = channel_ids.get("category")
        if preserve_snapshot_access_boundary and saved_category_id is None:
            raise RuntimeError(
                "進行中ゲームの開始時アクセス境界を確認できません "
                "(保存済みカテゴリIDがありません)。安全のため起動を停止しました"
            )
        category = next(
            (item for item in guild.categories if item.id == saved_category_id),
            None,
        )
        if preserve_snapshot_access_boundary and category is None:
            raise RuntimeError(
                "進行中ゲームの開始時アクセス境界を保つ保存済みカテゴリが見つかりません。"
                "限定中のVCを公開しないため起動を停止しました"
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
            if self.uses_manual_static_permissions():
                raise RuntimeError(
                    f"{self.room_def.name} は権限を手動管理する卓です。"
                    "既存カテゴリが見つからないため、Botでは自動作成しません"
                )
            # 作成後に閲覧拒否を付けると、Discord API応答間だけ
            # 新規カテゴリが公開になる。作成リクエスト自体に完成形の
            # overwriteを含め、最初からfail-closedにする。
            create_options: dict[str, object] = {
                "overwrites": self.manager._build_room_overwrites(guild, self.room_def),
            }
            position = await self._new_category_position(guild)
            if position is not None:
                create_options["position"] = position
            category = await guild.create_category(
                self.room_def.name, **create_options,
            )
        self.state.category = category
        if preserve_snapshot_access_boundary:
            log.warning(
                "進行中ゲームの開始時アクセス境界を維持します (%s)",
                self.room_def.name,
            )
        elif active_snapshot and self.is_private_room():
            # 進行中GM村はrestore側で霊界の生存者denyとVC発言禁止を確定してから、
            # 子チャンネル→カテゴリの順で公開観戦権限を復元する。ここでは先に戻さない。
            log.info(
                "進行中GM村の公開観戦権限復元を復元完了まで保留します (%s)",
                self.room_def.name,
            )
        else:
            await self.manager._apply_room_visibility(guild, category, self.room_def)

        # 起動時の孤立 #昼 / #霊界 チャンネル削除 (同名の重複残骸も全て掃除する)
        # (前回起動でクラッシュ等によりゲーム途中終了した場合、残骸が残っている可能性)
        if not active_snapshot:
            owned_game_channel_ids = {
                int(channel_id)
                for channel_id in (snapshot or {}).get(
                    "managed_game_channel_ids", []
                )
                if isinstance(channel_id, int)
            }
            owned_game_channel_ids.update(
                channel_id
                for channel_id in (
                    channel_ids.get("village"),
                    channel_ids.get("spirit"),
                )
                if isinstance(channel_id, int)
            )
            orphans = [
                ch for ch in guild.text_channels
                if ch.category is not None and ch.category.id == category.id
                and ch.name in (CH_VILLAGE, CH_SPIRIT)
            ]
            for orphan in orphans:
                if (
                    self.uses_manual_static_permissions()
                    and orphan.id not in owned_game_channel_ids
                ):
                    raise RuntimeError(
                        f"{self.room_def.name}/#{orphan.name} はBot所有IDを確認できない"
                        "手動チャンネルです。誤削除を避けるため起動を停止しました"
                    )
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
            if self.uses_manual_static_permissions():
                raise RuntimeError(
                    f"{self.room_def.name}/#{CH_LOBBY} は手動管理対象です。"
                    "既存チャンネルが見つからないため、Botでは自動作成しません"
                )
            lobby = await guild.create_text_channel(CH_LOBBY, category=category)
        self.state.lobby_channel = lobby
        if self.is_private_room():
            # GM名前村はカテゴリ・VC・ゲーム中チャンネルまで公開観戦型。
            # #参加受付は全員が読める一方、募集カード以外の書込みは許可しない。
            await self.manager._set_permission_if_changed(
                lobby,
                guild.default_role,
                discord.PermissionOverwrite(
                    view_channel=True,
                    read_messages=True,
                    read_message_history=True,
                    send_messages=False,
                ),
                reason="GM村の募集受付を公開",
            )
            await self.manager._set_permission_if_changed(
                lobby,
                guild.me,
                discord.PermissionOverwrite(
                    view_channel=True,
                    read_messages=True,
                    read_message_history=True,
                    send_messages=True,
                    manage_channels=True,
                ),
                reason="GM村の募集受付を公開",
            )

        saved_voice_id = channel_ids.get("voice")
        vc = next(
            (item for item in guild.voice_channels if item.id == saved_voice_id),
            None,
        )
        if vc is None:
            vc = discord.utils.get(guild.voice_channels, name=VC_GAME, category=category)
        if vc is None:
            if self.uses_manual_static_permissions():
                raise RuntimeError(
                    f"{self.room_def.name}/{VC_GAME} は手動管理対象です。"
                    "既存VCが見つからないため、Botでは自動作成しません"
                )
            vc = await guild.create_voice_channel(VC_GAME, category=category)
        self.state.voice_channel = vc

        # 前回クラッシュ等で残ったVCの一時権限を掃除する。
        if not active_snapshot:
            if self.uses_manual_static_permissions():
                # 手動値の所有記録がある項目だけを三値へ戻す。記録がない
                # 個別overwriteは、Bot所有と断定できないため触らない。
                raw_snapshot = snapshot or {}
                if raw_snapshot.get("vc_default_permissions_captured") is True:
                    default_ow = vc.overwrites_for(guild.default_role)
                    speak_before = raw_snapshot.get(
                        "vc_default_speak_before_game"
                    )
                    send_before = raw_snapshot.get(
                        "vc_default_send_before_game"
                    )
                    default_ow.speak = (
                        speak_before if isinstance(speak_before, bool) else None
                    )
                    default_ow.send_messages = (
                        send_before if isinstance(send_before, bool) else None
                    )
                    await self._paced_discord_api_call(
                        vc.set_permissions,
                        guild.default_role,
                        overwrite=None if default_ow.is_empty() else default_ow,
                        reason="人狼: 起動時クリーンアップ (VC発言権限復元)",
                    )
                    raw_snapshot["vc_default_permissions_captured"] = False
                    raw_snapshot["vc_default_speak_before_game"] = None
                    raw_snapshot["vc_default_send_before_game"] = None

                if raw_snapshot.get("vc_gm_speak_captured") is True:
                    gm_user_id = raw_snapshot.get("vc_gm_speak_user_id")
                    gm_target = next(
                        (
                            target for target in vc.overwrites
                            if getattr(target, "id", None) == gm_user_id
                        ),
                        guild.get_member(gm_user_id)
                        if isinstance(gm_user_id, int) else None,
                    )
                    if gm_target is not None:
                        gm_ow = vc.overwrites_for(gm_target)
                        gm_speak_before = raw_snapshot.get(
                            "vc_gm_speak_before_game"
                        )
                        gm_ow.speak = (
                            gm_speak_before
                            if isinstance(gm_speak_before, bool) else None
                        )
                        await self._paced_discord_api_call(
                            vc.set_permissions,
                            gm_target,
                            overwrite=None if gm_ow.is_empty() else gm_ow,
                            reason="人狼: 起動時クリーンアップ (GM発言権限復元)",
                        )
                    raw_snapshot["vc_gm_speak_captured"] = False
                    raw_snapshot["vc_gm_speak_user_id"] = None
                    raw_snapshot["vc_gm_speak_before_game"] = None
            else:
                # 自動管理卓では、従来どおりゲーム中のメンバー個別許可を掃除。
                for target in list(vc.overwrites):
                    if not isinstance(target, discord.Member):
                        continue
                    try:
                        await self._paced_discord_api_call(
                            vc.set_permissions, target, overwrite=None,
                            reason="人狼: 起動時クリーンアップ (VC個別権限残骸)",
                        )
                        log.info(
                            "起動時にVC個別権限の残骸を削除しました (%s)",
                            target.display_name,
                        )
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
            # 起動時は前回のパネルへ編集で当て、再起動しても未読・通知を出さない。
            await self._post_lobby_ui(reuse_existing=True)

    @staticmethod
    def _resolve_nonactive_owned_mutes(
        snapshot: dict,
        members: list,
        *,
        marker_role_name: Optional[str] = None,
    ) -> set[int]:
        """非active復元で解除してよいBot所有muteだけを返す。"""
        if snapshot.get("mute_marker_enabled") is not True:
            # 現行方式の所有証拠がなければ、手動muteを誤解除しない。
            return set()
        owned = set(snapshot.get("bot_muted_ids", []))
        if marker_role_name:
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
        for member in members:
            voice = getattr(member, "voice", None)
            if member.id not in intent_ids or voice is None or not voice.mute:
                continue
            if marker_role_name and any(
                getattr(role, "name", None) == marker_role_name
                for role in getattr(member, "roles", [])
            ):
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

    async def _purge_bot_messages(
        self,
        ch: discord.TextChannel,
        label: str,
        *,
        keep_message_ids: Optional[set[int]] = None,
    ) -> None:
        """チャンネル内の古いBot投稿を最大50件まで削除する。"""
        keep_message_ids = keep_message_ids or set()
        try:
            await ch.purge(
                limit=50,
                check=lambda m: (
                    m.author == self.bot.user
                    and getattr(m, "id", None) not in keep_message_ids
                ),
            )
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

    async def _play_transition_se(self, scene: str) -> None:
        """SEと約2秒の切替間を進行順序に組み込む。

        通常のSEは非同期だが、投票発言の開始・終了と結果開示は
        音の前に次の操作へ進まないことが仕様。VC不調は上限で打ち切り、
        無音でも同じ切替間は保つ。
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        vc = self.state.voice_channel
        if SE_ENABLED and vc is not None:
            try:
                await asyncio.wait_for(
                    self.manager.sound_player.play(vc, scene),
                    timeout=VOTE_SE_MAX_WAIT,
                )
            except asyncio.TimeoutError:
                log.warning("SE待機を打ち切りました (%s)", scene)
            except Exception as error:
                log.warning("SE待機中の例外 (%s): %s", scene, error)
        remaining = max(0.0, VOTE_TRANSITION_GRACE - (loop.time() - started))
        if remaining:
            await self._pausable_sleep(remaining)

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

    def _lobby_panel_meta_key(self) -> str:
        return f"lobby_panel_message_id:{self.state.room_id}"

    async def _post_lobby_ui(self, *, reuse_existing: bool = False) -> None:
        """#参加受付 の常設パネルを掲示する。

        削除→再投稿だと再起動のたびに新着メッセージが増えて未読・通知が出る。
        起動中 (`manager._startup_in_progress`) と reuse_existing=True では、
        前回と同じメッセージへ編集で当てて静かに戻す (#運営の運営パネルと
        同じ考え方)。起動時の掲示はここだけでなく、snapshotからのロビー復帰・
        進行中ゲームの復元・募集カードの復旧からも走るため、呼び出し口ごとの
        指定ではなく起動中フラグでまとめて切り替える。
        ゲーム終了後など運用中の再掲示は、末尾に出したいので従来どおり。
        """
        ch = self.state.lobby_channel
        view = LobbyView(self)
        embed = view._build_embed()
        if reuse_existing or getattr(self.manager, "_startup_in_progress", False):
            message = await self._reuse_lobby_message(ch, embed=embed, view=view)
            if message is not None:
                self.state.lobby_message = message
                return
        # 運用中は新しい操作面を先に確保する。送信またはID保存に失敗しても、
        # それまで使えていた参加受付を先に消さない。
        new_message = await ch.send(embed=embed, view=view)
        await self._remember_lobby_message(new_message)
        self.state.lobby_message = new_message
        await self._purge_bot_messages(
            ch,
            "ロビー",
            keep_message_ids={new_message.id},
        )

    async def _reuse_lobby_message(
        self, ch, *, embed: discord.Embed, view: discord.ui.View,
    ) -> Optional[discord.Message]:
        """保存済みのロビーパネルへ編集で当てる。無ければNone。"""
        stored = await database.get_meta(ch.guild.id, self._lobby_panel_meta_key())
        get_partial = getattr(ch, "get_partial_message", None)
        if not stored or not str(stored).isdigit() or not callable(get_partial):
            return None
        try:
            # fetchせず編集すればAPIは1回。消えていれば例外で分かる。
            message = await get_partial(int(stored)).edit(embed=embed, view=view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            log.info(
                "ロビーパネルを再利用できないため投稿し直します (%s): %s",
                self.state.room_name,
                e,
            )
            return None
        add_view = getattr(self.bot, "add_view", None)
        if callable(add_view):
            add_view(view, message_id=message.id)
        return message

    async def _remember_lobby_message(self, message) -> None:
        message_id = getattr(message, "id", None)
        guild = getattr(self.state.lobby_channel, "guild", None)
        if message_id is None or guild is None:
            return
        await database.set_meta(
            guild.id, self._lobby_panel_meta_key(), str(message_id),
        )

    def _build_room_snapshot(self) -> dict:
        state = self.state
        return {
            "room_name": state.room_name,
            "variant_id": self.variant.variant_id,
            "day_number": state.day_number,
            "gm_id": state.gm_id,
            "game_run_id": state.game_run_id,
            "recruitment_id": state.recruitment_id,
            "public_log_archive_allowed": state.public_log_archive_allowed,
            "vc_default_permissions_captured": state.vc_default_permissions_captured,
            "vc_default_speak_before_game": state.vc_default_speak_before_game,
            "vc_default_send_before_game": state.vc_default_send_before_game,
            "vc_gm_speak_captured": state.vc_gm_speak_captured,
            "vc_gm_speak_user_id": state.vc_gm_speak_user_id,
            "vc_gm_speak_before_game": state.vc_gm_speak_before_game,
            "managed_game_channel_ids": sorted(state.managed_game_channel_ids),
            "day_generation": state.day_generation,
            "night_generation": state.night_generation,
            "day_execution_resolved": state.day_execution_resolved,
            "day_executed_target": state.day_executed_target,
            "pending_execution_target": state.pending_execution_target,
            "night_resolved": state.night_resolved,
            "pending_winner": state.pending_winner.name if state.pending_winner else None,
            "ending": state.ending,
            "day_discussion_skip_generation": state.day_discussion_skip_generation,
            "lobby_return_mode": state.lobby_return_mode,
            "pending_recruitment_reopen": state.pending_recruitment_reopen,
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
            "runoff_speech_index": state.runoff_speech_index,
            "vote_day_generation": state.vote_day_generation,
            "vote_order": list(state.vote_order),
            "vote_closed": state.vote_closed,
            "vote_requeue_ids": list(state.vote_requeue_ids),
            "vote_slot_index": state.vote_slot_index,
            "vote_slot_token": state.vote_slot_token,
            "vote_slot_active": state.vote_slot_active,
            "vote_speech_finished": state.vote_speech_finished,
            "vote_slot_forced_abstain": state.vote_slot_forced_abstain,
            "vote_current_speaker_id": (
                state.current_speaker_id
                if self._effective_phase() == Phase.DAY_VOTE
                else None
            ),
            "vote_panel_message_id": state.vote_panel_message_id,
            "turn_day_generation": state.turn_day_generation,
            "turn_anchor_number": state.turn_anchor_number,
            "next_turn_anchor_number": state.next_turn_anchor_number,
            "turn_order": list(state.turn_order),
            "turn_round_index": state.turn_round_index,
            "turn_slot_index": state.turn_slot_index,
            "turn_slot_token": state.turn_slot_token,
            "turn_slot_active": state.turn_slot_active,
            "turn_current_speaker_id": (
                state.current_speaker_id if self.is_turn_discussion_mode() else None
            ),
            "turn_original_speaker_id": state.turn_original_speaker_id,
            "turn_interrupt_active": state.turn_interrupt_active,
            "turn_interrupt_pending_id": state.turn_interrupt_pending_id,
            "turn_interrupts_used": state.turn_interrupts_used,
            "turn_co_declarations": [
                dict(declaration) for declaration in state.turn_co_declarations
            ],
            "turn_panel_message_id": state.turn_panel_message_id,
            "decisive_executions": list(state.decisive_executions),
            "wolf_guesses": [
                {"player_id": player_id, "targets": list(targets)}
                for player_id, targets in state.wolf_guesses.items()
            ],
            "spirit_hold_ids": list(state.spirit_hold_ids),
            "spirit_hold_events": [
                {"player_id": player_id, "event_id": event_id}
                for player_id, event_id in state.spirit_hold_events.items()
            ],
            "wolf_target": state.wolf_target,
            "wolf_voters": [
                {"user_id": user_id, "target_id": target_id}
                for user_id, target_id in state.wolf_voters.items()
            ],
            "wolf_dm_message_ids": [
                {"user_id": user_id, "message_ids": list(message_ids)}
                for user_id, message_ids in state.wolf_dm_message_ids.items()
            ],
            "surrender_dm_message_ids": [
                {"user_id": user_id, "message_ids": list(message_ids)}
                for user_id, message_ids in state.surrender_dm_message_ids.items()
            ],
            "seer_target": state.seer_target,
            "guard_target": state.guard_target,
            "action_log": state.action_log,
            "initial_night_completed": state.initial_night_completed,
            "surrender_ids": list(state.surrender_ids),
            "surrender_confirmed": state.surrender_confirmed,
            "surrender_announced": state.surrender_announced,
            "morning_ready_open": state.morning_ready_open,
            "morning_ready_ids": list(state.morning_ready_ids),
            "morning_warned_ids": list(state.morning_warned_ids),
            "morning_confirmed": state.morning_confirmed,
            "morning_panel_message_id": state.morning_panel_message_id,
            "prep_ready_ids": list(state.prep_ready_ids),
            "prep_confirmed": state.prep_confirmed,
            "prep_panel_message_id": state.prep_panel_message_id,
            "speech_panel_message_id": state.speech_panel_message_id,
            "pending_ui_cleanup_batches": (
                self._serialize_pending_ui_cleanup_batches(
                    getattr(state, "pending_ui_cleanup_batches", [])
                )
            ),
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
            "co_claims": [
                {
                    "user_id": user_id,
                    "number": claim.get("number"),
                    "display_name": claim.get("display_name"),
                    "role": claim.get("role"),
                    "day": claim.get("day"),
                }
                for user_id, claim in state.co_claims.items()
            ],
            "co_result_claims": [
                dict(result) for result in state.co_result_claims
                if isinstance(result, dict)
            ],
            "medium_results": [dict(result) for result in state.medium_results],
            "record_event_seq": state.record_event_seq,
            "village_panel_message_id": state.village_panel_message_id,
            "started_at": (
                state.started_at.isoformat() if state.started_at is not None else None
            ),
        }

    def _validate_turn_snapshot(
        self,
        payload: dict,
        saved_phase: Phase,
        raw_order: list[int],
    ) -> None:
        """ターンcursorを推測修復せず、復元前に完全性を検証する。"""
        if not self.is_turn_discussion_mode():
            return

        try:
            snapshot_player_ids = [
                int(row["user_id"]) for row in payload.get("players", [])
            ]
            snapshot_numbers = [
                int(row["number"]) for row in payload.get("players", [])
            ]
            round_index = int(payload.get("turn_round_index", 0))
            slot_index = int(payload.get("turn_slot_index", 0))
            slot_token = int(payload.get("turn_slot_token", 0))
            turn_generation = int(payload.get("turn_day_generation", 0))
            interrupts_used = int(payload.get("turn_interrupts_used", 0))
        except (KeyError, TypeError, ValueError) as error:
            raise StateDurabilityError("ターン制snapshotの数値形式が不正です") from error

        if len(snapshot_player_ids) != len(set(snapshot_player_ids)):
            raise StateDurabilityError("ターン制snapshotの参加者IDが重複しています")
        if (
            len(snapshot_numbers) != len(set(snapshot_numbers))
            or any(
                number < 1 or number > self.variant.player_count
                for number in snapshot_numbers
            )
        ):
            raise StateDurabilityError("ターン制snapshotの席番号が不正です")
        if min(round_index, slot_index, slot_token, turn_generation, interrupts_used) < 0:
            raise StateDurabilityError("ターン制snapshotのcursorに負数があります")
        if turn_generation > self.state.day_generation:
            raise StateDurabilityError("ターン制snapshotの日世代が未来を指しています")
        if interrupts_used > self.variant.turn_interrupts_per_day:
            raise StateDurabilityError("ターン制snapshotの割り込み回数が上限を超えています")

        raw_co_declarations = payload.get("turn_co_declarations", [])
        if not isinstance(raw_co_declarations, list):
            raise StateDurabilityError("ターン制snapshotのCO一覧が配列ではありません")
        snapshot_number_by_id = dict(zip(snapshot_player_ids, snapshot_numbers))
        declared_ids: set[int] = set()
        for declaration in raw_co_declarations:
            if not isinstance(declaration, dict):
                raise StateDurabilityError("ターン制snapshotのCO一覧要素が不正です")
            user_id = declaration.get("user_id")
            number = declaration.get("number")
            display_name = declaration.get("display_name")
            if (
                isinstance(user_id, bool)
                or not isinstance(user_id, int)
                or user_id not in snapshot_number_by_id
                or isinstance(number, bool)
                or not isinstance(number, int)
                or number != snapshot_number_by_id[user_id]
                or not isinstance(display_name, str)
                or not display_name
            ):
                raise StateDurabilityError("ターン制snapshotのCO宣言が不正です")
            if user_id in declared_ids:
                raise StateDurabilityError("ターン制snapshotのCO宣言が重複しています")
            declared_ids.add(user_id)
        if raw_co_declarations and (
            turn_generation == 0
            or (turn_generation == 1 and round_index < 1)
        ):
            raise StateDurabilityError("CO宣言が受け付け開始前のターンcursorにあります")

        for key in ("turn_anchor_number", "next_turn_anchor_number"):
            value = payload.get(key)
            if value is None:
                continue
            try:
                number = int(value)
            except (TypeError, ValueError) as error:
                raise StateDurabilityError(f"{key}の形式が不正です") from error
            if not 1 <= number <= self.variant.player_count:
                raise StateDurabilityError(f"{key}が席番号の範囲外です")

        panel_id = payload.get("turn_panel_message_id")
        if panel_id is not None and (
            isinstance(panel_id, bool)
            or not isinstance(panel_id, int)
            or panel_id <= 0
        ):
            raise StateDurabilityError("ターン話者パネルIDが不正です")

        has_turn_cursor = bool(raw_order) or turn_generation > 0
        if not has_turn_cursor:
            if saved_phase == Phase.DAY_DISCUSSION and snapshot_player_ids:
                raise StateDurabilityError("進行中のターン順序が保存されていません")
            return

        if (
            len(raw_order) != len(snapshot_player_ids)
            or len(raw_order) != len(set(raw_order))
            or set(raw_order) != set(snapshot_player_ids)
        ):
            raise StateDurabilityError(
                "保存済みターン順序が参加者全員の重複なし順列ではありません"
            )

        durations = (
            tuple(self.variant.turn_round_seconds[:2])
            if turn_generation == 1
            else (self.variant.turn_round_seconds[-1],)
        )
        if round_index > len(durations):
            raise StateDurabilityError("ターン巡cursorが範囲外です")
        if round_index == len(durations):
            if slot_index != 0 or bool(payload.get("turn_slot_active", False)):
                raise StateDurabilityError("完了済みターンcursorの状態が不正です")
        elif slot_index >= len(raw_order):
            raise StateDurabilityError("ターン発言枠cursorが範囲外です")

        if saved_phase == Phase.DAY_DISCUSSION and turn_generation != self.state.day_generation:
            raise StateDurabilityError("進行中ターンの日世代が一致しません")

        active = bool(payload.get("turn_slot_active", False))
        interrupt_active = bool(payload.get("turn_interrupt_active", False))
        current_id = payload.get("turn_current_speaker_id")
        original_id = payload.get("turn_original_speaker_id")
        pending_id = payload.get("turn_interrupt_pending_id")
        snapshot_id_set = set(snapshot_player_ids)
        try:
            normalized_pending_id = int(pending_id) if pending_id is not None else None
            normalized_current_id = int(current_id) if current_id is not None else None
            normalized_original_id = int(original_id) if original_id is not None else None
        except (TypeError, ValueError) as error:
            raise StateDurabilityError("ターン話者IDの形式が不正です") from error
        if normalized_pending_id is not None and normalized_pending_id not in snapshot_id_set:
            raise StateDurabilityError("割り込み待機者が参加者に存在しません")
        if interrupt_active and not active:
            raise StateDurabilityError("非active枠が割り込み中になっています")
        if active and round_index < len(durations):
            expected_speaker_id = raw_order[slot_index]
            base_speaker_id = (
                normalized_original_id if interrupt_active else normalized_current_id
            )
            if base_speaker_id is None or base_speaker_id != expected_speaker_id:
                raise StateDurabilityError("active枠の話者とcursorが一致しません")
            if interrupt_active and (
                normalized_current_id is None
                or normalized_current_id not in snapshot_id_set
                or normalized_current_id == expected_speaker_id
            ):
                raise StateDurabilityError("割り込み中の話者状態が不正です")

    def _validate_vote_snapshot(
        self,
        payload: dict,
        saved_phase: Phase,
        raw_order: list[int],
    ) -> None:
        """通常投票の発言順とcursorを推測修復せず検証する。"""
        try:
            generation = int(payload.get("vote_day_generation", 0))
            slot_index = int(payload.get("vote_slot_index", 0))
            slot_token = int(payload.get("vote_slot_token", 0))
        except (TypeError, ValueError) as error:
            raise StateDurabilityError("投票順snapshotの数値形式が不正です") from error
        if min(generation, slot_index, slot_token) < 0:
            raise StateDurabilityError("投票順snapshotのcursorに負数があります")
        if generation > self.state.day_generation:
            raise StateDurabilityError("投票順snapshotの日世代が未来を指しています")

        snapshot_player_ids = {
            int(row["user_id"])
            for row in payload.get("players", [])
            if isinstance(row, dict) and row.get("user_id") is not None
        }
        if len(raw_order) != len(set(raw_order)) or not set(raw_order) <= snapshot_player_ids:
            raise StateDurabilityError("保存済み投票順序が参加者の重複なし順列ではありません")
        if saved_phase == Phase.DAY_VOTE and generation != self.state.day_generation:
            raise StateDurabilityError("進行中の通常投票の日世代が一致しません")
        if slot_index > len(raw_order):
            raise StateDurabilityError("投票順snapshotのcursorが順序の範囲外です")
        # 発言順は「投票参加」を押した人だけの部分列なので、生存者が全員
        # 入っている必要はない。まだ押していない人は復元後に押して並ぶ。
        for key in (
            "vote_closed", "vote_slot_active", "vote_speech_finished",
            "vote_slot_forced_abstain",
        ):
            if key in payload and not isinstance(payload[key], bool):
                raise StateDurabilityError(f"{key}がboolではありません")

        active = bool(payload.get("vote_slot_active", False))
        speaker_id = payload.get("vote_current_speaker_id")
        vote_voter_ids = {
            int(row["voter_id"])
            for row in payload.get("votes", [])
            if isinstance(row, dict) and row.get("voter_id") is not None
        }
        # v0.51までは発言と投票が同じ枠。active話者の票が既にある
        # 旧snapshotだけは「発言・投票完了」と読み替える。
        speech_finished = (
            bool(payload.get("vote_speech_finished", False))
            if "vote_speech_finished" in payload
            else bool(active and speaker_id in vote_voter_ids)
        )
        forced_abstain = bool(payload.get("vote_slot_forced_abstain", False))
        if active:
            if saved_phase != Phase.DAY_VOTE or slot_index >= len(raw_order):
                raise StateDurabilityError("投票発言中snapshotのフェーズまたはcursorが不正です")
            if not self.uses_sequential_vote():
                raise StateDurabilityError("一斉投票snapshotに投票発言枠が残っています")
            if speaker_id != raw_order[slot_index]:
                raise StateDurabilityError("投票発言中snapshotの話者が順序と一致しません")
            if (
                "vote_speech_finished" in payload
                and not speech_finished
                and speaker_id in vote_voter_ids
            ):
                raise StateDurabilityError("発言終了前の投票snapshotに確定票があります")
            if forced_abstain and (
                not speech_finished or speaker_id in vote_voter_ids
            ):
                raise StateDurabilityError("GM棄権snapshotの発言・票状態が不正です")
        elif speaker_id is not None or speech_finished or forced_abstain:
            raise StateDurabilityError("非アクティブな投票snapshotに現在枠の状態が残っています")

        panel_id = payload.get("vote_panel_message_id")
        if panel_id is not None and (
            isinstance(panel_id, bool)
            or not isinstance(panel_id, int)
            or panel_id <= 0
        ):
            raise StateDurabilityError("投票パネルIDが不正です")

    def _validate_night_surrender_snapshot(self, payload: dict) -> None:
        """初夜・朝待機・サレンダーの勝敗境界を型変換せず検証する。"""
        for key in (
            "initial_night_completed",
            "surrender_confirmed",
            "surrender_announced",
            "morning_ready_open",
        ):
            value = payload.get(key, False)
            if not isinstance(value, bool):
                raise StateDurabilityError(f"{key}がboolではありません")

        rows = payload.get("players", [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise StateDurabilityError("サレンダーsnapshotの参加者一覧が不正です")
        try:
            snapshot_ids = {int(row["user_id"]) for row in rows}
        except (KeyError, TypeError, ValueError) as error:
            raise StateDurabilityError("サレンダーsnapshotの参加者IDが不正です") from error

        raw_surrender_ids = payload.get("surrender_ids", [])
        if not isinstance(raw_surrender_ids, list) or any(
            isinstance(player_id, bool) or not isinstance(player_id, int)
            for player_id in raw_surrender_ids
        ):
            raise StateDurabilityError("surrender_idsが整数ID配列ではありません")
        if len(raw_surrender_ids) != len(set(raw_surrender_ids)):
            raise StateDurabilityError("surrender_idsに重複があります")
        surrender_ids = set(raw_surrender_ids)
        if not surrender_ids <= snapshot_ids:
            raise StateDurabilityError("surrender_idsに参加者外のIDがあります")

        real_wolf_ids = {
            int(row["user_id"])
            for row in rows
            if row.get("role") == Role.WEREWOLF.name
        }
        if not surrender_ids <= real_wolf_ids:
            raise StateDurabilityError("実人狼以外のサレンダー同意が保存されています")

        announced = payload.get("surrender_announced", False)
        confirmed = payload.get("surrender_confirmed", False)
        if announced and not confirmed:
            raise StateDurabilityError("未成立のサレンダーが告知済みになっています")
        if confirmed:
            living_real_wolves = {
                int(row["user_id"])
                for row in rows
                if row.get("role") == Role.WEREWOLF.name
                and row.get("alive", True) is True
            }
            if not living_real_wolves or not living_real_wolves <= surrender_ids:
                raise StateDurabilityError(
                    "成立済みサレンダーと生存実人狼の同意が一致しません"
                )
            if payload.get("pending_winner") != Team.VILLAGE.name:
                raise StateDurabilityError(
                    "成立済みサレンダーの村勝利checkpointがありません"
                )

        raw_ready_ids = payload.get("morning_ready_ids", [])
        if not isinstance(raw_ready_ids, list) or any(
            isinstance(player_id, bool) or not isinstance(player_id, int)
            for player_id in raw_ready_ids
        ):
            raise StateDurabilityError("morning_ready_idsが整数ID配列ではありません")
        if len(raw_ready_ids) != len(set(raw_ready_ids)):
            raise StateDurabilityError("morning_ready_idsに重複があります")
        if raw_ready_ids and not payload.get("morning_ready_open", False):
            raise StateDurabilityError("朝待機開始前の宣言が保存されています")

    @staticmethod
    def _restore_dm_view_message_ids(payload: dict, key: str) -> dict[int, list[int]]:
        """UI掃除用DMメッセージIDを、旧snapshot互換で読み戻す。

        この情報はゲーム進行に影響しないベストエフォートの後始末用なので、
        新フィールドを持たない旧snapshotや壊れた行を理由に復元全体を止めない。
        """
        restored: dict[int, list[int]] = {}
        rows = payload.get(key, [])
        if not isinstance(rows, list):
            return restored
        for row in rows:
            if not isinstance(row, dict):
                continue
            user_id = row.get("user_id")
            message_ids = row.get("message_ids")
            if (
                isinstance(user_id, bool)
                or not isinstance(user_id, int)
                or user_id <= 0
                or not isinstance(message_ids, list)
            ):
                continue
            valid_ids = [
                message_id
                for message_id in message_ids
                if isinstance(message_id, int)
                and not isinstance(message_id, bool)
                and message_id > 0
            ]
            if valid_ids:
                restored[user_id] = list(dict.fromkeys(valid_ids))
        return restored

    @staticmethod
    def _restore_optional_message_id(payload: dict, key: str) -> Optional[int]:
        """旧snapshotの欠損をNoneとして扱う、UI用IDの安全な読込。"""
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None

    @classmethod
    def _serialize_pending_ui_cleanup_batches(
        cls,
        batches: object,
    ) -> list[dict]:
        """旧ゲームUIの掃除待ちをJSON安全な形へ変換する。"""
        if not isinstance(batches, list):
            return []

        def serialize_dm(mapping: object) -> list[dict]:
            normalized = cls._copy_dm_cleanup_ids(mapping)
            return [
                {"user_id": user_id, "message_ids": list(message_ids)}
                for user_id, message_ids in sorted(normalized.items())
            ]

        serialized: list[dict] = []
        for batch in batches:
            if not isinstance(batch, dict):
                continue
            channel_id = batch.get("channel_id")
            if (
                isinstance(channel_id, bool)
                or not isinstance(channel_id, int)
                or channel_id <= 0
            ):
                channel_id = None
            raw_channel_ids = batch.get("channel_message_ids", {})
            channel_rows = []
            if isinstance(raw_channel_ids, dict):
                for field_name in cls._PENDING_CHANNEL_UI_FIELDS:
                    message_id = raw_channel_ids.get(field_name)
                    if (
                        isinstance(message_id, int)
                        and not isinstance(message_id, bool)
                        and message_id > 0
                    ):
                        channel_rows.append({
                            "kind": field_name,
                            "message_id": message_id,
                        })
            wolf_rows = serialize_dm(batch.get("wolf_dm_message_ids", {}))
            surrender_rows = serialize_dm(
                batch.get("surrender_dm_message_ids", {})
            )
            if channel_rows or wolf_rows or surrender_rows:
                game_run_id = batch.get("game_run_id")
                if not isinstance(game_run_id, str) or not game_run_id:
                    game_run_id = None
                serialized.append({
                    "game_run_id": game_run_id,
                    "channel_id": channel_id,
                    "channel_message_ids": channel_rows,
                    "wolf_dm_message_ids": wolf_rows,
                    "surrender_dm_message_ids": surrender_rows,
                })
        return serialized

    @classmethod
    def _restore_pending_ui_cleanup_batches(cls, payload: dict) -> list[dict]:
        """掃除待ちをベストエフォートで復元し、不正な行だけ捨てる。"""
        rows = payload.get("pending_ui_cleanup_batches", [])
        if not isinstance(rows, list):
            return []
        restored: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            channel_id = row.get("channel_id")
            if (
                isinstance(channel_id, bool)
                or not isinstance(channel_id, int)
                or channel_id <= 0
            ):
                channel_id = None
            channel_message_ids: dict[str, int] = {}
            raw_channel_rows = row.get("channel_message_ids", [])
            if isinstance(raw_channel_rows, list):
                for channel_row in raw_channel_rows:
                    if not isinstance(channel_row, dict):
                        continue
                    field_name = channel_row.get("kind")
                    message_id = channel_row.get("message_id")
                    if (
                        field_name in cls._PENDING_CHANNEL_UI_FIELDS
                        and isinstance(message_id, int)
                        and not isinstance(message_id, bool)
                        and message_id > 0
                    ):
                        channel_message_ids[field_name] = message_id
            wolf_ids = cls._restore_dm_view_message_ids(
                {"rows": row.get("wolf_dm_message_ids", [])}, "rows"
            )
            surrender_ids = cls._restore_dm_view_message_ids(
                {"rows": row.get("surrender_dm_message_ids", [])}, "rows"
            )
            if channel_message_ids or wolf_ids or surrender_ids:
                cls._append_pending_ui_cleanup_batch(restored, {
                    "game_run_id": row.get("game_run_id"),
                    "channel_id": channel_id,
                    "channel_message_ids": channel_message_ids,
                    "wolf_dm_message_ids": wolf_ids,
                    "surrender_dm_message_ids": surrender_ids,
                })
        return restored

    async def _persist_room_state(self) -> None:
        async with self.state_persist_lock:
            # lockを取った後の最新stateから構築する。呼び出し前にpayloadを
            # 固定すると、待機中に確定した割り込み等を再び消してしまう。
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

        saved_variant_id = payload.get("variant_id", self.room_def.variant_id)
        if saved_variant_id != self.room_def.variant_id:
            saved_phase_name = str(payload.get("phase") or "")
            safe_dynamic_lobby_change = (
                self.is_private_room()
                and saved_phase_name in {Phase.LOBBY.name, Phase.GAME_OVER.name}
                and not payload.get("players")
            )
            if not safe_dynamic_lobby_change:
                raise StateDurabilityError(
                    "進行中ゲームの変種が現在の卓設定と一致しません "
                    f"({saved_variant_id} != {self.room_def.variant_id})"
                )
            # private_rooms+募集のtransaction完了後、room_state保存前に停止した
            # 場合だけ成立する差。ゲーム中のsnapshotは従来どおりfail-closed。
            log.info(
                "GM村ロビーの形式変更をDB正本から復旧: %s (%s -> %s)",
                state.room_id,
                saved_variant_id,
                self.room_def.variant_id,
            )
            payload["variant_id"] = self.room_def.variant_id

        state.day_number = payload.get("day_number", 0)
        state.gm_id = payload.get("gm_id")
        state.game_run_id = payload.get("game_run_id") or secrets.token_hex(16)
        state.recruitment_id = payload.get("recruitment_id")
        # 進行中カテゴリ・VCの開始時アクセス境界を復元するために保持する。
        # 終了ログはこの値にかかわらず全村で公開ログへ退避する。
        state.public_log_archive_allowed = (
            payload.get("public_log_archive_allowed") is True
            and self._configured_public_access_boundary()
        )
        state.vc_default_permissions_captured = (
            payload.get("vc_default_permissions_captured") is True
        )
        speak_before = payload.get("vc_default_speak_before_game")
        send_before = payload.get("vc_default_send_before_game")
        state.vc_default_speak_before_game = (
            speak_before if isinstance(speak_before, bool) else None
        )
        state.vc_default_send_before_game = (
            send_before if isinstance(send_before, bool) else None
        )
        state.vc_gm_speak_captured = (
            payload.get("vc_gm_speak_captured") is True
        )
        gm_speak_user_id = payload.get("vc_gm_speak_user_id")
        state.vc_gm_speak_user_id = (
            gm_speak_user_id if isinstance(gm_speak_user_id, int) else None
        )
        gm_speak_before = payload.get("vc_gm_speak_before_game")
        state.vc_gm_speak_before_game = (
            gm_speak_before if isinstance(gm_speak_before, bool) else None
        )
        state.managed_game_channel_ids = {
            int(channel_id)
            for channel_id in payload.get("managed_game_channel_ids", [])
            if isinstance(channel_id, int)
        }
        state.day_generation = int(payload.get("day_generation", state.day_number or 0))
        state.night_generation = int(payload.get("night_generation", 0))
        state.day_execution_resolved = bool(payload.get("day_execution_resolved", False))
        state.day_executed_target = payload.get("day_executed_target")
        state.pending_execution_target = payload.get("pending_execution_target")
        state.night_resolved = bool(payload.get("night_resolved", False))
        pending_winner_name = payload.get("pending_winner")
        state.pending_winner = Team[pending_winner_name] if pending_winner_name else None
        skip_generation = payload.get("day_discussion_skip_generation")
        if skip_generation is not None and (
            isinstance(skip_generation, bool)
            or not isinstance(skip_generation, int)
            or skip_generation < 0
        ):
            raise StateDurabilityError(
                f"議論スキップ世代が不正です: {skip_generation!r}"
            )
        state.day_discussion_skip_generation = skip_generation
        lobby_return_mode = payload.get("lobby_return_mode", "empty")
        if lobby_return_mode not in {"empty", "gm", "roster"}:
            raise StateDurabilityError(
                f"終了後のLOBBY復帰種別が不正です: {lobby_return_mode!r}"
            )
        state.lobby_return_mode = str(lobby_return_mode)
        state.pending_recruitment_reopen = (
            payload.get("pending_recruitment_reopen") is True
        )
        if (
            state.pending_recruitment_reopen
            and state.lobby_return_mode != "gm"
        ):
            raise StateDurabilityError(
                "参加受付再開待ちとLOBBY復帰種別が一致しません"
            )
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
        saved_village_channel_id = (
            channel_ids.get("village")
            if isinstance(channel_ids, dict)
            and isinstance(channel_ids.get("village"), int)
            and not isinstance(channel_ids.get("village"), bool)
            and channel_ids.get("village") > 0
            else None
        )
        if state.guild:
            village_id = saved_village_channel_id
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
        self._validate_night_surrender_snapshot(payload)
        # GAME_OVERはこの直後に空LOBBYへ回収するため、通常の
        # 進行状態復元より先にUI掃除用IDだけを読み戻す。
        state.wolf_dm_message_ids = self._restore_dm_view_message_ids(
            payload, "wolf_dm_message_ids"
        )
        state.surrender_dm_message_ids = self._restore_dm_view_message_ids(
            payload, "surrender_dm_message_ids"
        )
        state.morning_panel_message_id = self._restore_optional_message_id(
            payload, "morning_panel_message_id"
        )
        state.prep_panel_message_id = self._restore_optional_message_id(
            payload, "prep_panel_message_id"
        )
        state.speech_panel_message_id = self._restore_optional_message_id(
            payload, "speech_panel_message_id"
        )
        state.turn_panel_message_id = self._restore_optional_message_id(
            payload, "turn_panel_message_id"
        )
        state.vote_panel_message_id = self._restore_optional_message_id(
            payload, "vote_panel_message_id"
        )
        state.village_panel_message_id = self._restore_optional_message_id(
            payload, "village_panel_message_id"
        )
        state.pending_ui_cleanup_batches = (
            self._restore_pending_ui_cleanup_batches(payload)
        )
        if saved_phase == Phase.GAME_OVER:
            # 終了checkpoint後〜空LOBBYの原子保存前に停止した窓を回収する。
            # 旧実装のように参加者・GM・募集IDを残したままLOBBY扱いにすると、
            # 次村が開始不能になり、番号付きニックネームも持ち越される。
            state.phase = Phase.GAME_OVER
            state.ending = True
            had_entries = bool(state.players) or state.gm_id is not None
            if had_entries:
                self.last_game_roster = list(state.players)
                self.last_game_gm = state.gm_id
            await self._close_game_views_for_shutdown()
            nickname_failures = await self._restore_nicknames(state)
            await self._teardown_game_roles_and_perms()
            preserve_gm = (
                state.lobby_return_mode in {"gm", "roster"}
                and state.gm_id is not None
            )
            # GMを復元できないsnapshotで参加者だけ残すと保存契約に反するため、
            # その場合は空LOBBYへ落とす (実運用ではGM必須なので通常は通らない)。
            preserve_players = (
                state.lobby_return_mode == "roster" and preserve_gm
            )
            if not await self._transition_to_empty_lobby(
                state,
                nickname_failures=nickname_failures,
                cleanup_channel_id=saved_village_channel_id,
                log_label="再起動時の終了処理回収",
                preserve_players=preserve_players,
                preserve_gm=preserve_gm,
            ):
                log.error(
                    "終了済みゲームを空LOBBYへ回収できませんでした (%s)",
                    state.room_name,
                )
            return
        if saved_phase == Phase.LOBBY:
            state.phase = Phase.LOBBY
            state.recovery_phase = None
            state.recovered_from_restart = False
            await self._retry_pending_ui_cleanup()
            # ロビー復帰時は前ゲームの一時ロールを掃除
            await self._delete_alive_role()
            try:
                await self._post_lobby_ui()
            except Exception as error:
                log.exception(
                    "再起動後の参加受付パネル再掲に失敗: %s",
                    error,
                )
                self._schedule_lobby_panel_recovery(
                    state,
                    recruitment_id=state.recruitment_id,
                    log_label="再起動復元",
                )
            await self._persist_room_state()
            if state.pending_recruitment_reopen:
                self._schedule_recruitment_recovery(state)
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
        state.runoff_speech_index = int(payload.get("runoff_speech_index", 0))
        if state.runoff_speech_index > len(state.runoff_candidates):
            raise StateDurabilityError("決戦弁明cursorが候補順の範囲外です")
        if saved_phase == Phase.DAY_RUNOFF_SPEECH and not state.runoff_candidates:
            raise StateDurabilityError("進行中の決戦弁明順序が保存されていません")
        try:
            raw_vote_order = [
                int(player_id) for player_id in payload.get("vote_order", [])
            ]
        except (TypeError, ValueError) as error:
            raise StateDurabilityError("保存済み投票順序の形式が不正です") from error
        self._validate_vote_snapshot(payload, saved_phase, raw_vote_order)
        state.vote_day_generation = int(payload.get("vote_day_generation", 0))
        raw_vote_slot_index = int(payload.get("vote_slot_index", 0))
        state.vote_order = [
            player_id for player_id in raw_vote_order if player_id in state.players
        ]
        state.vote_slot_index = sum(
            1
            for player_id in raw_vote_order[:raw_vote_slot_index]
            if player_id in state.players
        )
        state.vote_slot_token = int(payload.get("vote_slot_token", 0))
        state.vote_closed = bool(payload.get("vote_closed", False))
        state.vote_requeue_ids = {
            int(player_id) for player_id in payload.get("vote_requeue_ids", [])
            if int(player_id) in state.players
        }
        saved_vote_slot_active = bool(payload.get("vote_slot_active", False))
        raw_vote_speaker_id = payload.get("vote_current_speaker_id")
        restored_vote_speaker_id = (
            int(raw_vote_speaker_id)
            if raw_vote_speaker_id is not None
            and int(raw_vote_speaker_id) in state.players
            and state.vote_slot_index < len(state.vote_order)
            and state.vote_order[state.vote_slot_index] == int(raw_vote_speaker_id)
            else None
        )
        state.vote_slot_active = bool(
            saved_vote_slot_active and restored_vote_speaker_id is not None
        )
        state.vote_speech_finished = (
            (
                bool(payload.get("vote_speech_finished", False))
                if "vote_speech_finished" in payload
                else restored_vote_speaker_id in state.votes
            )
            if state.vote_slot_active else False
        )
        state.vote_slot_forced_abstain = (
            bool(payload.get("vote_slot_forced_abstain", False))
            if state.vote_slot_active else False
        )
        # 復元直後は必ず全員ミュート。再開タスクがSE後に明示的に窓を開ける。
        state.vote_speech_window_open = False
        state.speech_done_event.clear()
        state.vote_choice_event.clear()
        state.vote_panel_message_id = (
            int(payload["vote_panel_message_id"])
            if payload.get("vote_panel_message_id") is not None else None
        )
        try:
            raw_turn_order = [
                int(player_id) for player_id in payload.get("turn_order", [])
            ]
        except (TypeError, ValueError) as error:
            raise StateDurabilityError("保存済みターン順序の形式が不正です") from error
        self._validate_turn_snapshot(payload, saved_phase, raw_turn_order)
        state.turn_day_generation = int(payload.get("turn_day_generation", 0))
        state.turn_anchor_number = (
            int(payload["turn_anchor_number"])
            if payload.get("turn_anchor_number") is not None else None
        )
        state.next_turn_anchor_number = (
            int(payload["next_turn_anchor_number"])
            if payload.get("next_turn_anchor_number") is not None else None
        )
        state.turn_order = [
            player_id for player_id in raw_turn_order if player_id in state.players
        ]
        state.turn_round_index = int(payload.get("turn_round_index", 0))
        raw_slot_index = int(payload.get("turn_slot_index", 0))
        state.turn_slot_index = sum(
            1
            for player_id in raw_turn_order[:raw_slot_index]
            if player_id in state.players
        )
        if state.turn_order and state.turn_slot_index >= len(state.turn_order):
            state.turn_slot_index = 0
            state.turn_round_index += 1
        state.turn_slot_token = int(payload.get("turn_slot_token", 0))
        state.turn_slot_active = bool(payload.get("turn_slot_active", False))
        state.current_speaker_id = (
            int(payload["turn_current_speaker_id"])
            if (
                self.is_turn_discussion_mode()
                and payload.get("turn_current_speaker_id") is not None
            )
            else None
        )
        state.turn_original_speaker_id = (
            int(payload["turn_original_speaker_id"])
            if payload.get("turn_original_speaker_id") is not None else None
        )
        state.turn_interrupt_active = bool(payload.get("turn_interrupt_active", False))
        state.turn_interrupt_pending_id = (
            int(payload["turn_interrupt_pending_id"])
            if payload.get("turn_interrupt_pending_id") is not None else None
        )
        state.turn_interrupts_used = int(payload.get("turn_interrupts_used", 0))
        state.turn_co_declarations = [
            {
                "user_id": int(declaration["user_id"]),
                "number": int(declaration["number"]),
                "display_name": str(declaration["display_name"]),
            }
            for declaration in payload.get("turn_co_declarations", [])
        ]
        state.turn_panel_message_id = (
            int(payload["turn_panel_message_id"])
            if payload.get("turn_panel_message_id") is not None else None
        )
        restored_ids = set(state.players)
        if state.turn_interrupt_pending_id not in restored_ids:
            if state.turn_interrupt_pending_id is not None:
                state.turn_interrupts_used = max(0, state.turn_interrupts_used - 1)
            state.turn_interrupt_pending_id = None
        base_speaker_id = (
            state.turn_original_speaker_id
            if state.turn_interrupt_active
            else state.current_speaker_id
        )
        if state.turn_slot_active and base_speaker_id not in restored_ids:
            state.turn_slot_active = False
            state.current_speaker_id = None
            state.turn_original_speaker_id = None
            state.turn_interrupt_active = False
        state.turn_window_open = False
        state.turn_remaining_seconds = 0.0
        state.turn_done_event.clear()
        state.turn_interrupt_event.clear()
        state.turn_signal_event.clear()
        if self.is_turn_discussion_mode() and state.turn_interrupt_active:
            # 再起動中だった割り込み自体は再生せず、その要求は使用済みのまま
            # 元のslotを満額でやり直す。
            state.current_speaker_id = state.turn_original_speaker_id
            state.turn_interrupt_active = False
            state.turn_interrupt_pending_id = None
            state.turn_original_speaker_id = None
        elif self.is_turn_discussion_mode() and state.turn_interrupt_pending_id is not None:
            # 受付保存後、割り込み枠を開始する前に落ちた要求は実行されていない。
            # 元slotを満額でやり直す際に日次枠も返却する。
            state.turn_interrupt_pending_id = None
            state.turn_interrupts_used = max(0, state.turn_interrupts_used - 1)
        if (
            saved_phase == Phase.DAY_VOTE
            and self.uses_sequential_vote()
            and state.vote_slot_active
        ):
            # 共通current_speakerをターン復元で初期化した後、逐次投票の現在者を戻す。
            state.current_speaker_id = restored_vote_speaker_id
        state.decisive_executions = [
            row for row in payload.get("decisive_executions", []) if isinstance(row, dict)
        ]
        state.wolf_guesses = {
            int(row["player_id"]): [int(t) for t in row.get("targets", [])]
            for row in payload.get("wolf_guesses", [])
            if row.get("player_id") is not None
        }
        # 保留中に落ちた場合は復元時に解放する (`_release_spirit_holds`)。
        # 再起動を挟んでまで提出を待つと、その間に霊界の話を聞けてしまう
        state.spirit_hold_ids = {int(pid) for pid in payload.get("spirit_hold_ids", [])}
        state.spirit_hold_events = {
            int(row["player_id"]): str(row["event_id"])
            for row in payload.get("spirit_hold_events", [])
            if isinstance(row, dict)
            and row.get("player_id") is not None
            and row.get("event_id")
        }
        state.wolf_target = payload.get("wolf_target")
        state.wolf_voters = {
            int(row["user_id"]): int(row["target_id"])
            for row in payload.get("wolf_voters", [])
            if row.get("user_id") is not None and row.get("target_id") is not None
        }
        state.wolf_dm_message_ids = self._restore_dm_view_message_ids(
            payload, "wolf_dm_message_ids"
        )
        state.surrender_dm_message_ids = self._restore_dm_view_message_ids(
            payload, "surrender_dm_message_ids"
        )
        state.seer_target = payload.get("seer_target")
        state.guard_target = payload.get("guard_target")
        state.action_log = list(payload.get("action_log", []))
        state.initial_night_completed = bool(
            payload.get("initial_night_completed", False)
        )
        state.surrender_ids = {
            int(player_id) for player_id in payload.get("surrender_ids", [])
            if isinstance(player_id, int)
        }
        state.surrender_confirmed = bool(payload.get("surrender_confirmed", False))
        state.surrender_announced = bool(payload.get("surrender_announced", False))
        state.morning_ready_open = bool(payload.get("morning_ready_open", False))
        state.morning_ready_ids = set(payload.get("morning_ready_ids", []))
        state.morning_warned_ids = set(payload.get("morning_warned_ids", []))
        state.morning_confirmed = bool(payload.get("morning_confirmed", False))
        state.morning_panel_message_id = self._restore_optional_message_id(
            payload, "morning_panel_message_id"
        )
        state.prep_ready_ids = set(payload.get("prep_ready_ids", []))
        state.prep_confirmed = bool(payload.get("prep_confirmed", False))
        state.prep_panel_message_id = self._restore_optional_message_id(
            payload, "prep_panel_message_id"
        )
        state.speech_panel_message_id = self._restore_optional_message_id(
            payload, "speech_panel_message_id"
        )
        state.disconnected_players = set(payload.get("disconnected_players", []))
        state.bot_muted_ids = set(payload.get("bot_muted_ids", []))
        state.bot_mute_intent_ids = set(payload.get("bot_mute_intent_ids", []))
        state.mute_marker_enabled = bool(payload.get("mute_marker_enabled", False))
        # 公開CO宣言・結果自己申告・霊能結果・記録seq・昼パネルID・開始時刻。
        # 壊れた要素は例外を投げず読み飛ばす (旧payloadを隔離しないため)。
        state.co_claims = {}
        for row in payload.get("co_claims", []):
            if not isinstance(row, dict):
                continue
            try:
                state.co_claims[int(row["user_id"])] = {
                    "number": int(row.get("number")),
                    "display_name": str(row.get("display_name")),
                    "role": str(row.get("role")),
                    "day": int(row.get("day")),
                }
            except (KeyError, TypeError, ValueError):
                continue
        state.co_result_claims = []
        result_indexes: dict[tuple[int, int, str], int] = {}
        for row in payload.get("co_result_claims", []):
            if not isinstance(row, dict):
                continue
            try:
                restored = {
                    "user_id": int(row["user_id"]),
                    "number": int(row["number"]),
                    "display_name": str(row["display_name"]),
                    "role": str(row["role"]),
                    "target_id": int(row["target_id"]),
                    "target_number": int(row["target_number"]),
                    "target_name": str(row["target_name"]),
                    "judgement": str(row["judgement"]),
                    "day": int(row["day"]),
                }
            except (KeyError, TypeError, ValueError):
                continue
            allowed = self.CO_RESULT_JUDGEMENTS.get(restored["role"])
            if (
                restored["user_id"] <= 0
                or restored["target_id"] <= 0
                or restored["user_id"] == restored["target_id"]
                or allowed is None
                or restored["judgement"] not in allowed
            ):
                continue
            key = (restored["user_id"], restored["day"], restored["role"])
            previous_index = result_indexes.get(key)
            if previous_index is None:
                result_indexes[key] = len(state.co_result_claims)
                state.co_result_claims.append(restored)
            else:
                state.co_result_claims[previous_index] = restored
        state.medium_results = [
            dict(result) for result in payload.get("medium_results", [])
            if isinstance(result, dict)
        ]
        try:
            state.record_event_seq = int(payload.get("record_event_seq", 0))
        except (TypeError, ValueError):
            state.record_event_seq = 0
        village_panel_id = payload.get("village_panel_message_id")
        state.village_panel_message_id = (
            int(village_panel_id) if village_panel_id is not None else None
        )
        raw_started_at = payload.get("started_at")
        state.started_at = None
        if isinstance(raw_started_at, str) and raw_started_at:
            try:
                state.started_at = datetime.fromisoformat(raw_started_at)
            except ValueError:
                state.started_at = None
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
        if self._has_pending_ui_cleanup():
            # 進行中ゲームの復元でも、さらに次の終了まで古い操作口を
            # 放置せず、現在UIとは独立したバッチだけをすぐ再掃除する。
            await self._retry_pending_ui_cleanup()
        if state.morning_confirmed:
            state.morning_ready_event.set()
        if state.prep_confirmed:
            state.prep_ready_event.set()

        if (
            self.is_private_room()
            and (state.village_channel is None or state.spirit_channel is None)
        ):
            # 進行中GM村の復元中に新しい公開チャンネルを作ると、生存者の
            # 霊界denyを付け終えるまで閲覧できる隙間が生じる。欠損時は
            # 勝手に作り直さず、保存状態を保ったまま停止する。
            raise StateDurabilityError(
                "進行中GM名前村の #昼 / #霊界 が見つかりません"
            )

        if (state.village_channel is None or state.spirit_channel is None) and state.guild is not None:
            try:
                await self._create_game_channels(state.guild)
            except Exception as e:
                log.warning(f"復元用ゲームチャンネル作成失敗 ({state.room_name}): {e}")
            if state.village_channel is None or state.spirit_channel is None:
                raise StateDurabilityError(
                    "進行中ゲームの #昼 / #霊界 を安全に復元できません"
                )

        await self._disable_recovered_turn_panel()
        await self._disable_recovered_village_panel()
        # これらは永続View化していないため、再起動後に古いボタンだけが
        # 見えても正常には受け付けられない。GMの再開を待つ間に表示から外す。
        await self._delete_stale_prep_panel()
        await self._disable_recovered_speech_panel()
        await self._disable_persisted_dm_views(
            state.wolf_dm_message_ids, label="人狼襲撃",
        )

        await self._reconcile_pending_death_effects()

        # 死亡checkpoint直後・サレンダー再判定前に落ちた場合も、復元した
        # 生存実人狼の集合で全員同意を補完し、通常進行へ戻さない。
        living_wolf_ids = {wolf.user_id for wolf in state.alive_wolves()}
        if state.surrender_confirmed and (
            not living_wolf_ids
            or not living_wolf_ids <= state.surrender_ids
        ):
            # snapshot自体が整合していても、復元時にサーバー不在者を除いた
            # 実際の参加者集合が変わることがある。生存実人狼がいない状態を
            # サレンダー村勝利として精算せず、推測修復しない。
            raise StateDurabilityError(
                "復元後の生存実人狼と成立済みサレンダー同意が一致しません"
            )
        if (
            not state.surrender_confirmed
            and living_wolf_ids
            and living_wolf_ids <= state.surrender_ids
        ):
            state.surrender_confirmed = True
            state.pending_winner = Team.VILLAGE
            await self._persist_room_state()

        # サレンダーは成立理由の公開を伴うため、汎用pending_winnerより先に
        # 回収する。成立checkpoint直後〜finish task起動前のクラッシュにも対応。
        if state.surrender_confirmed:
            state.recovered_from_restart = False
            state.recovery_phase = None
            await self._safe_village_send(
                "♻️ **成立済みのサレンダー結果を自動精算します。**"
            )
            await self._finish_surrender()
            return

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

        await self._send_surrender_controls()

        # 復元: 一時「生存」ロールを再付与 (再開時に発言制御が効くように)
        # #霊界 の生存者ブロックも現在の生死に合わせて再適用する
        await self._assign_alive_role()
        await self._prepare_game_vc_permissions("復元時のVC権限設定")
        # _apply_spirit_blocks は死亡者の霊界を開けるので、保留も同時に解ける。
        # 再起動をまたいでまで提出を待つと、その間に霊界の話を聞けてしまうため、
        # 未提出のぶんは0点で確定させる
        state.spirit_hold_ids.clear()
        state.spirit_hold_events.clear()
        await self._apply_spirit_blocks(required=self.is_private_room())
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
        info = await database.get_player_current_rank_info(
            user_id,
            self.state.guild.id,
            ladder_id=self.variant.ladder_id,
        )
        if info is None:
            return {"rank_name": "ブロンズ", "provisional": True}
        return info

    def _strict_access_error(
        self,
        member: discord.Member,
        *,
        action: str,
    ) -> Optional[str]:
        """厳格ロール限定卓の参加・GM取得を同じ境界で検証する。

        通常の ``access_role_names`` と異なり、``manage_guild`` は迂回権限に
        しない。カテゴリ作成時にも同名重複・欠損を検査するが、古いViewや
        設定変更直後の操作でも安全側へ倒せるよう、ここでも再確認する。
        """
        required = frozenset(
            getattr(self.room_def, "strict_access_role_names", None) or ()
        )
        if not required:
            return None

        guild = self.state.guild
        if guild is not None:
            matches_by_name = {name: [] for name in required}
            for role in getattr(guild, "roles", ()) or ():
                role_name = getattr(role, "name", None)
                if role_name in matches_by_name:
                    matches_by_name[role_name].append(role)
            if any(len(matches) != 1 for matches in matches_by_name.values()):
                return (
                    "この卓の限定ロール設定を確認できないため、"
                    f"{action}できません。運営へ連絡してください。"
                )

        member_role_names = {
            getattr(role, "name", None) for role in getattr(member, "roles", ())
        }
        if required.isdisjoint(member_role_names):
            allowed = " / ".join(sorted(required))
            return f"この卓への{action}には **{allowed}** のロールが必要です。"
        return None

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

        strict_access_error = self._strict_access_error(member, action="参加")
        if strict_access_error:
            return strict_access_error

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
        member_role_names = {role.name for role in member.roles}
        if self.is_private_room():
            if member.id != self.room_def.private_owner_id:
                return "このGM村では村主だけがGM取得できます。"
            if not (PRIVATE_ROOM_CREATOR_ROLE_NAMES & member_role_names):
                return f"GM村のGM取得には **{PRIVATE_ROOM_CREATOR_ROLE_LABEL}** ロールが必要です。"
            return None
        strict_access_error = self._strict_access_error(member, action="GM取得")
        if strict_access_error:
            return strict_access_error
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
        """全卓共通の所属ロックを取って、直前のメンバーを再登録する。"""
        # 通常参加・GM取得と同じ join_lock → action_lock の順に固定する。
        # DM確認のawait中に別卓の参加/次村が割り込むと、同じ人を複数卓へ
        # 登録できてしまうため、判定からstate更新までを全卓で直列化する。
        async with self.manager.join_lock, self.action_lock:
            return await self._rematch_locked(user)

    async def _rematch_locked(self, user: discord.Member) -> str:
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

        # 同村拒否は募集カードのDB制約側にしかないため、カードを通さない
        # 次村では自前で見る。試合直後は「今の卓のあの人とはもう組みたくない」
        # を登録する典型的なタイミングなので、素通りさせない。
        blocked_pairs = await database.list_player_blocks_between(
            guild.id, [*state.players, *self.last_game_roster],
        )
        blockers_of: dict[int, set[int]] = {}
        for blocker_id, blocked_id in blocked_pairs:
            blockers_of.setdefault(blocked_id, set()).add(blocker_id)
            blockers_of.setdefault(blocker_id, set()).add(blocked_id)

        # 前回の並び順で1人ずつ、参加条件 → 同村拒否 → DMの順に確定する。
        # DM確認は全卓共有のAPIペーシングで元から直列化される。先に候補全員を
        # 仮登録扱いにすると、先行候補のDM失敗後も、その候補との拒否関係だけで
        # 後続まで余計に除外してしまうため、成功した人だけregisteredへ足す。
        # (GMを兼ねている人も参加者として登録する: 同じ卓での兼任は許可されている)
        registered_ids = set(state.players)
        for uid in self.last_game_roster:
            if uid in state.players:
                continue
            if len(state.players) >= self.variant.player_count:
                skipped.append("(定員に達したため以降を打ち切り)")
                break
            member = guild.get_member(uid)
            if member is None:
                skipped.append(f"ID:{uid} (サーバー不在)")
                continue
            if await self.validate_join(member) is not None:
                skipped.append(member.display_name)
                continue
            # 募集と同じく「先に入っている人」を優先し、前回の並び順で
            # 決まる安定した結果にする。理由は本人にも卓にも出さない
            # (誰が誰を拒否したかが分かってしまうため)。
            if blockers_of.get(uid, set()) & registered_ids:
                skipped.append(member.display_name)
                continue
            try:
                await self._discord_api_call(
                    member.send, "人狼ゲームへの次村参加を受け付けました。"
                )
            except (discord.Forbidden, discord.HTTPException):
                skipped.append(f"{member.display_name} (DM不可)")
                continue
            state.players[member.id] = Player(
                user_id=member.id,
                member=member,
                original_nickname=member.nick,
            )
            registered_ids.add(member.id)
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
                # 外部Discord副作用またはそのcheckpointを安全に完了できなかった。
                # _stop_for_durability_errorでPREPARATION復元フラグは済んでいるので
                # GMパネルを確保し、コールバックを例外終了させない。
                log.error(f"開始フェーズを安全停止: {e}")
                await self._repost_gm_panel()
                try:
                    await interaction.followup.send(
                        "⚠️ 開始処理を安全に完了できなかったため停止しました。"
                        "原因を解消後、GMパネルの「再開」を押してください。",
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
        if self._postgame_vote_pending:
            try:
                await interaction.followup.send(
                    "終了後投票を集計中です。受付終了後にもう一度開始してください。",
                    ephemeral=True,
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            return
        if (
            state.phase != Phase.LOBBY
            or state.ending
            or self.manager.rooms.get(state.room_id) is not self
        ):
            try:
                await interaction.followup.send(
                    "村の状態が変わったためゲームを開始しませんでした。",
                    ephemeral=True,
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            return
        if self._has_pending_ui_cleanup():
            await self._retry_pending_ui_cleanup()
        participant_ids = set(state.players)
        if state.gm_id is not None:
            participant_ids.add(state.gm_id)
        conflicts: dict[str, list[str]] = {}
        find_active_user_room = getattr(
            self.manager,
            "find_active_user_room",
            None,
        )
        for user_id in sorted(participant_ids):
            active_room = (
                find_active_user_room(
                    user_id,
                    exclude_room_id=state.room_id,
                )
                if callable(find_active_user_room)
                else None
            )
            if active_room is None:
                continue
            member = guild.get_member(user_id) if guild is not None else None
            player = state.players.get(user_id)
            display_name = (
                getattr(member, "display_name", None)
                or getattr(getattr(player, "member", None), "display_name", None)
                or f"ID:{user_id}"
            )
            conflicts.setdefault(active_room.state.room_name, []).append(display_name)
        if conflicts:
            details = "\n".join(
                f"・**{room_name}**: {', '.join(names)}"
                for room_name, names in conflicts.items()
            )
            try:
                await interaction.followup.send(
                    "次のメンバーは別のゲームが進行中です。"
                    "待機村への複数登録はできますが、同時に進行できるゲームは1つです。\n"
                    f"{details}",
                    ephemeral=True,
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            return

        # 前ゲームで一時的に戻せなかった名前は、次の開始前に再試行する。
        # そのゲームへ参加する人だけが未復元なら開始を止め、不在者など
        # 今回の参加者でない失敗はLOBBY snapshotへ持ち越す。
        pending_nickname_targets = dict(state.original_nicknames)
        if pending_nickname_targets:
            nickname_failures = await self._restore_nicknames(
                state,
                member_ids=set(pending_nickname_targets),
            )
            state.original_nicknames = nickname_failures
            try:
                await self._persist_room_state()
            except Exception as error:
                log.exception("開始前の名前復元結果を保存できません: %s", error)
                try:
                    await interaction.followup.send(
                        "前ゲームの名前復元結果を保存できないため、開始しませんでした。",
                        ephemeral=True,
                    )
                except (discord.NotFound, discord.HTTPException):
                    pass
                return
            blocked_ids = participant_ids & set(nickname_failures)
            if blocked_ids:
                blocked_names = []
                for user_id in sorted(blocked_ids):
                    member = guild.get_member(user_id) if guild is not None else None
                    blocked_names.append(
                        getattr(member, "display_name", None) or f"ID:{user_id}"
                    )
                try:
                    await interaction.followup.send(
                        "前ゲームの名前をまだ復元できない参加者がいるため、開始しませんでした: "
                        + ", ".join(blocked_names),
                        ephemeral=True,
                    )
                except (discord.NotFound, discord.HTTPException):
                    pass
                return
        if (
            state.vc_default_permissions_captured
            or state.vc_gm_speak_captured
        ):
            # 前ゲーム終了時の一時HTTP失敗を、そのまま次ゲームの開始前値として
            # 再捕捉しない。まず保存済みの本当の開始前値へ戻してからだけ進める。
            await self._release_vc_after_game()
            if (
                state.vc_default_permissions_captured
                or state.vc_gm_speak_captured
            ):
                try:
                    await interaction.followup.send(
                        "前ゲームのVC権限を開始前へ戻せていません。"
                        "Discordのチャンネル管理権限を確認してから、もう一度開始してください。",
                        ephemeral=True,
                    )
                except (discord.NotFound, discord.HTTPException):
                    pass
                return
        if len(state.players) != self.variant.player_count:
            try:
                await interaction.followup.send(
                    f"参加者が揃っていません "
                    f"({len(state.players)}/{self.variant.player_count})",
                    ephemeral=True,
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            return

        # 音声人狼は全参加者が同じVCにいることを開始条件にする。
        # 開始後に初めて不在が分かると、その人だけ役職確認・発言・投票を
        # 音声なしで進められてしまうため、役職配布より前にまとめて止める。
        game_vc = state.voice_channel
        game_vc_id = getattr(game_vc, "id", None)
        missing_vc_players: list[str] = []
        for player in state.players.values():
            member = (
                guild.get_member(player.user_id)
                if guild is not None
                else None
            ) or player.member
            voice = getattr(member, "voice", None)
            joined_channel_id = getattr(
                getattr(voice, "channel", None), "id", None,
            )
            if game_vc_id is None or joined_channel_id != game_vc_id:
                missing_vc_players.append(player.display_name)
        if missing_vc_players:
            try:
                await interaction.followup.send(
                    "ゲームVCに接続していない参加者がいるため、開始しませんでした: "
                    + ", ".join(missing_vc_players)
                    + "\n全員がゲームVCへ入ってから、もう一度開始してください。",
                    ephemeral=True,
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            return
        state.guild = guild
        state.game_run_id = secrets.token_hex(16)
        state.started_at = datetime.now(timezone.utc)
        # 進行中カテゴリ・VCの開始時アクセス境界を再起動後も守るために保存する。
        # 終了ログは全村で
        # 共通の公開ログへ退避するため、この値では制限しない。
        state.public_log_archive_allowed = (
            self._configured_public_access_boundary()
        )
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
        state.initial_night_completed = False
        state.initial_night_skip_event.clear()
        state.surrender_ids.clear()
        state.surrender_confirmed = False
        state.surrender_announced = False
        self._surrender_control_sent_ids.clear()
        self._surrender_finish_task = None
        state.wolf_dm_messages.clear()
        state.wolf_dm_message_ids.clear()
        state.surrender_dm_message_ids.clear()
        state.morning_ready_open = False
        state.morning_ready_ids.clear()
        state.morning_warned_ids.clear()
        state.morning_confirmed = False
        state.morning_ready_event.clear()
        state.prep_ready_ids.clear()
        state.prep_confirmed = False
        state.prep_ready_event.clear()
        state.prep_panel_message = None
        state.prep_panel_message_id = None
        state.speech_panel_message_id = None
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
            "⏳ **ゲーム開始準備中 (1/3)** プレイヤー設定中…"
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
        numbers = list(range(1, self.variant.player_count + 1))
        secrets.SystemRandom().shuffle(numbers)
        for i, player in enumerate(state.players.values()):
            # interaction由来のMemberは参加時点のスナップショットで、
            # 以後のロール変更等が反映されない。キャッシュ済みメンバーへ差し替える
            cached_member = guild.get_member(player.user_id)
            if cached_member is not None:
                player.member = cached_member
            player.number = numbers[i]
            original_nickname = pending_nickname_targets.get(
                player.user_id, player.member.nick,
            )
            player.original_nickname = original_nickname
            player.base_name = (
                original_nickname
                or getattr(player.member, "global_name", None)
                or getattr(player.member, "name", None)
                or player.member.display_name
            )

        # ニックネーム変更・初期ミュート・生存ロールは同じPATCHにまとめる。
        # メンバー編集はギルド共有バケット (429の主因) なので、
        # 「改名13件 + ミュート13件 + ロール付与13件」を13件に減らす。
        # VC未接続の人は mute を指定できないが、入室時に
        # on_voice_state_update がフェーズに合わせて同期する。
        vc = state.voice_channel
        vc_member_ids = {m.id for m in vc.members} if vc else set()

        def should_initial_mute(member: discord.Member) -> bool:
            # GMは参加者を兼ねる場合も、説明しながら進行できるよう
            # Botの自動制御から外す。参加者としての改名・役職付与は行う。
            if self._uses_manual_gm_mute(member.id):
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
            state.original_nicknames.setdefault(
                player.user_id,
                pending_nickname_targets.get(player.user_id, player.member.nick),
            )

        await self._assign_roles()
        state.mute_marker_enabled = True
        seer = next((p for p in state.players.values() if p.role == Role.SEER), None)
        if seer is not None:
            non_wolves = self._initial_seer_white_candidates(seer)
            if non_wolves:
                state.initial_seer_target = secrets.choice(non_wolves).user_id

        # 現在Botが新たにmuteする意図の相手を先に所有記録する。
        # 既にmuteの相手は手動muteとして所有しない。
        for member in vc_members:
            if member.bot or self._uses_manual_gm_mute(member.id):
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
            "⏳ **ゲーム開始準備中 (2/3)** 役職DMを送信中…",
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
            "⏳ **ゲーム開始準備中 (3/3)** VC権限を調整中…",
        )
        # 統合PATCHに失敗した参加者、またはロール作成が一時失敗した場合だけ
        # add_rolesへフォールバックする。
        await self._assign_alive_role(exclude_ids=alive_role_assigned_ids)
        await self._prepare_game_vc_permissions("ゲーム開始時のVC権限設定")
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

    def _build_game_channel_overwrites(
        self,
        guild: discord.Guild,
        *,
        village: bool,
    ) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
        """#昼/#霊界の開始時overwriteを組み立てる。

        手動管理卓ではカテゴリの現在値を静的な正本として複製し、ゲーム中に
        必須な書込制御とBot自身の権限だけを重ねる。カテゴリ側の閲覧許可・拒否を
        access_role_namesから作り直さないため、Discordでの手動設定が保たれる。
        """
        if not self.uses_manual_static_permissions():
            return self.manager._build_room_overwrites(
                guild,
                self.room_def,
                send_messages=False if village else True,
            )

        category = self.state.category
        source = getattr(category, "overwrites", {}) if category is not None else {}
        overwrites = {
            target: discord.PermissionOverwrite.from_pair(*overwrite.pair())
            for target, overwrite in source.items()
        }

        bot_ow = overwrites.get(guild.me, discord.PermissionOverwrite())
        bot_ow.view_channel = True
        bot_ow.read_messages = True
        bot_ow.read_message_history = True
        bot_ow.send_messages = True
        bot_ow.manage_channels = True
        overwrites[guild.me] = bot_ow

        if village:
            # #昼は生存ロールだけをフェーズに応じて開閉する。カテゴリで手動allow
            # された観戦者・死亡者が書けないよう、他targetは開始時に閉じる。
            player_ids = set(self.state.players)
            if guild.default_role not in overwrites:
                overwrites[guild.default_role] = discord.PermissionOverwrite()
            for target, overwrite in overwrites.items():
                if target == guild.me:
                    continue
                # メンバー個別denyはロールallowより強い。参加者に既存の個別
                # overwriteがある場合だけ未設定へ戻し、生存ロールで開けるようにする。
                overwrite.send_messages = (
                    None
                    if getattr(target, "id", None) in player_ids
                    else False
                )
        return overwrites

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
            if (
                self.uses_manual_static_permissions()
                and ch.id not in state.managed_game_channel_ids
            ):
                raise RuntimeError(
                    f"#{ch.name} はBot所有IDを確認できない手動チャンネルです。"
                    "誤削除を避けるためゲームを開始しません"
                )
            try:
                await self._paced_discord_api_call(
                    ch.delete, reason="人狼: 前ゲームの残骸削除"
                )
                state.managed_game_channel_ids.discard(ch.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"残骸チャンネル削除失敗 (#{ch.name}): {e}")

        # #昼
        if state.village_channel is None:
            overwrites_village = self._build_game_channel_overwrites(
                guild, village=True,
            )
            state.village_channel = await guild.create_text_channel(
                CH_VILLAGE, category=category, overwrites=overwrites_village,
            )
            state.managed_game_channel_ids.add(state.village_channel.id)

        # #霊界: 死亡者+観戦者の雑談チャンネル。
        # 部屋の表示権限をベースに書き込みを許可し、生存者には
        # 「メンバー個別上書き」で閲覧拒否を付ける。
        # 注意: Discordの権限解決はロール層でdenyを集約→allowを集約の順に
        # 適用するため、ロールdenyは別ロール (ランクロール等) の
        # view allowに打ち消される。メンバー上書きはロール層の後に適用される
        # ので、制限卓でも確実に生存者から隠せる。
        if state.spirit_channel is None:
            overwrites_spirit = self._build_game_channel_overwrites(
                guild, village=False,
            )
            for player in state.players.values():
                if player.alive:
                    player_ow = overwrites_spirit.get(
                        player.member, discord.PermissionOverwrite()
                    )
                    player_ow.view_channel = False
                    player_ow.read_messages = False
                    overwrites_spirit[player.member] = player_ow
            state.spirit_channel = await guild.create_text_channel(
                CH_SPIRIT, category=category, overwrites=overwrites_spirit,
            )
            state.managed_game_channel_ids.add(state.spirit_channel.id)
            try:
                await state.spirit_channel.send(
                    "👻 **霊界** — 死亡者と観戦者だけが見えます。役職や推理の話も自由に"
                    "どうぞ（生存者へのDMなどでの共有は禁止）。"
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"霊界の案内送信失敗 ({state.room_name}): {e}")

    async def _assign_roles(self) -> None:
        state = self.state
        roles = []
        for role, count in self.variant.role_distribution.items():
            roles.extend([role] * count)
        secrets.SystemRandom().shuffle(roles)

        for player, role in zip(state.players.values(), roles):
            player.role = role

    def _initial_seer_white_candidates(self, seer: Player) -> list[Player]:
        """初日白の候補を返す。

        初日白は占い師本人と人狼を除く全員から選ぶ。狂人は占い結果では
        村人なので候補に含む。変種ごとの配役をこの条件から逆算しないよう、
        開始時と復元時の両方で同じ関数を使う。
        """
        return [
            player for player in self.state.players.values()
            if player.role != Role.WEREWOLF and player.user_id != seer.user_id
        ]

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
                    "\n🐺 役職確認タイムの最初の30秒と夜の制限時間中は、"
                    "このDMが他の人狼へ中継されます。"
                )
            msg += (
                "\n\n📩 確認したら #昼 の「役職を確認した」を押してください"
                "（全員が押すと1日目へ進みます）。"
            )
            try:
                surrender_view = (
                    WolfSurrenderView(self)
                    if player.role == Role.WEREWOLF
                    else None
                )
                sent_message = await self._discord_api_call(
                    player.member.send,
                    msg,
                    **({"view": surrender_view} if surrender_view is not None else {}),
                )
                if surrender_view is not None:
                    self._surrender_control_sent_ids.add(player.user_id)
                    self._remember_dm_view_message(
                        state.surrender_dm_message_ids, player.user_id, sent_message,
                    )
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
                raise StateDurabilityError(
                    "初日占い対象が開始checkpointにありません"
                )
            # 初日白はDMを送らず、記録テーブルへ書くだけにする。占い師は
            # #昼パネルの[占い]からいつでも確認でき、DM不達で初日白を
            # 受け取れない事故も無くなる。開始checkpointで対象は確定済み
            # なので、この経路が再試行で何度走っても1行に保てるよう
            # (run, actor, action, night) で重複を弾く …_once を使う。
            # event_seq=0 は「0日目初夜より前」を意味する予約値。
            try:
                await database.record_night_action_once(
                    state.guild.id, state.room_id, state.game_run_id,
                    event_seq=0, night_number=state.day_number,
                    actor_id=seer.user_id, actor_number=seer.number,
                    actor_role="占い師", action="初日白",
                    target_id=random_white.user_id,
                    target_number=random_white.number,
                    result="村人",
                )
            except Exception as error:
                log.exception(f"初日白のDB記録に失敗 ({state.room_name}): {error}")
            # フラグはDM送信済みではなく「初日白を確定・記録済み」の意味で
            # 使い続ける (snapshotの項目を減らさず、復元経路も変えない)。
            state.initial_seer_result_sent = True
            await self._persist_room_state()
        return failed

    async def _send_surrender_controls(self) -> None:
        """復元後も使える、試合中常設のサレンダー操作を実人狼へ補完する。"""
        state = self.state
        if state.surrender_confirmed:
            return
        # 再起動前のViewはコールバックを復元できない。新しいrun照合付きViewを
        # 送る前に、残っている操作を見た目から外す。
        if not self._surrender_control_sent_ids:
            await self._disable_persisted_dm_views(
                state.surrender_dm_message_ids, label="サレンダー",
            )
        remembered = False
        for wolf in state.alive_wolves():
            if wolf.user_id in self._surrender_control_sent_ids:
                continue
            view = WolfSurrenderView(self)
            try:
                sent_message = await self._discord_api_call(
                    wolf.member.send,
                    "🏳️ **サレンダー** — 生存中の人狼全員が同意すると村陣営の勝利で終了します。",
                    view=view,
                )
                self._surrender_control_sent_ids.add(wolf.user_id)
                remembered = self._remember_dm_view_message(
                    state.surrender_dm_message_ids, wolf.user_id, sent_message,
                ) or remembered
            except (discord.Forbidden, discord.HTTPException) as error:
                view.stop()
                log.warning(
                    "サレンダー操作DM送信失敗 (%s): %s",
                    wolf.display_name,
                    error,
                )
        if remembered:
            try:
                await self._persist_room_state()
            except Exception as error:
                # 操作自体はrun_idで保護済み。UI掃除用のID保存失敗だけで
                # 進行を止めず、次の送信/終了で改めて回収する。
                log.warning("サレンダーDM操作IDの保存に失敗 (%s): %s", state.room_name, error)

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
            "プルダウンで襲撃対象を選択（何度でも変更でき、"
            "**最後に選んだ対象**が夜明けに襲撃されます）。\n"
            "**制限時間を過ぎると相談の中継と襲撃先の変更通知が止まります**"
            "（選び直しは可能ですが仲間には伝わりません）。\n"
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

    async def submit_surrender(
        self,
        member: discord.Member,
        *,
        expected_game_run_id: str,
    ) -> str:
        """生存中の実人狼のサレンダー同意を保存し、全員同意なら通常精算する。"""
        state = self.state
        player = state.get_player(member.id)
        if (
            not self.is_current_game_view(expected_game_run_id)
            or player is None
            or not player.alive
            or player.role != Role.WEREWOLF
        ):
            return "⏳ 現在この操作はできません。"
        if state.surrender_confirmed:
            return "🏳️ サレンダーは既に成立しています。"

        living_wolves = {wolf.user_id for wolf in state.alive_wolves()}
        if member.id in state.surrender_ids:
            agreed = len(living_wolves & state.surrender_ids)
            return f"🏳️ あなたは同意済みです（**{agreed} / {len(living_wolves)}人**）。"

        old_ids = set(state.surrender_ids)
        old_confirmed = state.surrender_confirmed
        old_pending_winner = state.pending_winner
        state.surrender_ids.add(member.id)
        agreed = len(living_wolves & state.surrender_ids)
        completed = bool(living_wolves and living_wolves <= state.surrender_ids)
        if completed:
            state.surrender_confirmed = True
            # サレンダー成立と村勝利意図を同じcheckpointへ保存する。
            # finish task起動前に落ちても、復元時は通常進行へ戻さない。
            state.pending_winner = Team.VILLAGE
        try:
            await self._persist_room_state()
        except Exception as error:
            state.surrender_ids = old_ids
            state.surrender_confirmed = old_confirmed
            state.pending_winner = old_pending_winner
            log.exception("サレンダー同意の保存に失敗: %s", error)
            return "❌ サレンダー同意を保存できませんでした。もう一度お試しください。"

        if not completed:
            # 途中人数は人狼のDM内だけへ通知し、公開チャンネルへは出さない。
            await self._relay_to_wolves(
                f"🏳️ サレンダー同意: **{agreed} / {len(living_wolves)}人**",
            )
            return f"🏳️ サレンダーに同意しました（**{agreed} / {len(living_wolves)}人**）。"

        # 成立のdurable checkpoint後にのみ、公開・ゲーム停止・通常精算を始める。
        # 複数狼の同時押下でもfinish taskは1本だけにする。
        self._start_surrender_finish_task()
        return "🏳️ 全人狼が同意しました。村陣営の勝利として終了します。"

    async def _finish_surrender(self) -> None:
        state = self.state
        try:
            if not state.surrender_confirmed or state.phase == Phase.GAME_OVER:
                return
            if state.pending_winner is not Team.VILLAGE:
                old_pending_winner = state.pending_winner
                state.pending_winner = Team.VILLAGE
                try:
                    await self._persist_room_state()
                except Exception:
                    state.pending_winner = old_pending_winner
                    raise
            if not state.surrender_announced:
                await self._safe_village_send(
                    "🏳️ **人狼全員のサレンダーが成立しました。**\n"
                    "村陣営の勝利として、通常どおり戦績とレートへ反映します。"
                )
                state.surrender_announced = True
                try:
                    await self._persist_room_state()
                except Exception as error:
                    # 成立そのものは保存済み。告知済み印の失敗で勝敗を失わない。
                    log.warning("サレンダー成立告知状態の保存に失敗: %s", error)
            await self._finish_game_externally(Team.VILLAGE)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.exception("サレンダー終了処理に失敗: %s", error)
            await self._stop_for_durability_error("サレンダー終了処理", error)

    def _start_surrender_finish_task(self) -> None:
        if self._surrender_finish_task is None or self._surrender_finish_task.done():
            self._surrender_finish_task = self.manager.spawn_bg_task(
                self._finish_surrender()
            )

    async def _confirm_surrender_after_roster_change(self) -> bool:
        """死亡・除外後の生存実人狼だけで全員同意を再判定する。"""
        state = self.state
        if state.surrender_confirmed:
            self._start_surrender_finish_task()
            return True
        living_wolves = {wolf.user_id for wolf in state.alive_wolves()}
        if not living_wolves or not living_wolves <= state.surrender_ids:
            return False
        old_pending_winner = state.pending_winner
        state.surrender_confirmed = True
        state.pending_winner = Team.VILLAGE
        try:
            await self._persist_room_state()
        except Exception as error:
            state.surrender_confirmed = False
            state.pending_winner = old_pending_winner
            await self._stop_for_durability_error(
                "サレンダー成立状態の保存", error
            )
            raise StateDurabilityError(
                "サレンダー成立状態を保存できませんでした"
            ) from error
        self._start_surrender_finish_task()
        return True

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
        # 送信者が生存中の人狼か確認
        sender = state.get_player(message.author.id)
        if not sender or not sender.is_wolf or not sender.alive:
            return

        # 役職DMは役職確認パネルより先に届く。パネル掲示前にすぐ挨拶を
        # 送ると中継窓がまだ開いておらず、従来は黙って消えていた。
        # 中継済みと誤解させないよう、この狭い開始前だけ再送を案内する。
        if not self.wolf_relay_open():
            if (
                self._effective_phase() == Phase.PREPARATION
                and not state.initial_night_completed
            ):
                try:
                    await self._discord_api_call(
                        sender.member.send,
                        "⏳ このメッセージは人狼へ中継されませんでした。"
                        "#昼の役職確認パネルが表示された後、"
                        "初日内通の受付中にもう一度送ってください。",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
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

    async def _initial_wolf_greeting_during_preparation(self) -> None:
        """役職確認と同時に、人狼DM中継を最大30秒だけ開く。"""
        state = self.state
        if state.initial_night_completed:
            return
        await state.pause_event.wait()
        if state.surrender_confirmed or state.ending or state.pending_winner is not None:
            return

        state.wolf_relay_window_open = True
        try:
            # 全員確認またはGM締切なら、30秒を待たず同じEventで挨拶も終了する。
            await self._pausable_countdown(
                None,
                lambda _remaining: "",
                INITIAL_NIGHT_GREETING_TIME,
                state.prep_ready_event,
            )
        finally:
            state.wolf_relay_window_open = False

        await state.pause_event.wait()
        if state.surrender_confirmed or state.ending or state.pending_winner is not None:
            return
        state.initial_night_completed = True
        await self._persist_room_state()

    async def _initial_night_greeting(self) -> None:
        """旧INITIAL_NIGHT snapshot復旧用に、0日目初夜を単独で再開する。"""
        state = self.state
        await state.pause_event.wait()
        state.phase = Phase.INITIAL_NIGHT
        state.phase_before_pause = None
        state.initial_night_skip_event.clear()
        await self._persist_room_state()

        await self._lock_village()
        await self._mute_phase("0日目初夜に入ります。")

        def greeting_content(remaining: float) -> str:
            return (
                "🌙 **0日目 - 初夜（人狼の挨拶）**\n"
                "人狼は役職DMへ送ったメッセージで挨拶できます。"
                "この時間は能力を使用しません。\n"
                + self._timer_line(remaining)
            )

        timer_message = await self._safe_village_send(
            greeting_content(INITIAL_NIGHT_GREETING_TIME)
        )
        # GMメニューの汎用スキップは、現時点ではこの待機だけを終了する。
        await self._repost_gm_panel()

        # 最初の狼がDMを受け取って即返信しても落とさないよう、送信より前に開く。
        state.wolf_relay_window_open = True
        try:
            await asyncio.gather(*(
                self._send_initial_night_notice(wolf)
                for wolf in state.alive_wolves()
            ))
            await self._pausable_countdown(
                timer_message,
                greeting_content,
                INITIAL_NIGHT_GREETING_TIME,
                state.initial_night_skip_event,
            )
        finally:
            state.wolf_relay_window_open = False

        await state.pause_event.wait()
        if state.surrender_confirmed or state.ending or state.pending_winner is not None:
            return
        state.initial_night_completed = True
        await self._persist_room_state()
        await self._safe_village_send("☀️ **0日目の初夜が終了しました。1日目を開始します。**")

    async def _send_initial_night_notice(self, wolf: Player) -> None:
        try:
            await self._discord_api_call(
                wolf.member.send,
                f"🌙 **0日目の初夜（{INITIAL_NIGHT_GREETING_TIME}秒）**\n"
                "このDMのメッセージが他の人狼へ中継されます。",
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning(
                "0日目初夜DM送信失敗 (%s): %s",
                wolf.display_name,
                error,
            )

    async def force_skip_wait(
        self,
        member: discord.Member,
        *,
        expected_vote_slot_token: Optional[int] = None,
    ) -> str:
        """GMが現在の安全な時間待ちだけを終了する。"""
        state = self.state
        if member.id != state.gm_id:
            return "GMのみ操作可能です。"
        if state.paused:
            return "⏸️ 一時停止中です。先に「再開」を押してください。"
        phase = self._effective_phase()
        if (
            phase == Phase.INITIAL_NIGHT
            and not state.initial_night_completed
            and not state.initial_night_skip_event.is_set()
        ):
            state.initial_night_skip_event.set()
            await self._safe_village_send("⏭️ **GMの操作で0日目の初夜をスキップします。**")
            return "⏭️ 0日目の初夜をスキップしました。"
        if phase == Phase.DAY_DISCUSSION:
            if self._day_discussion_skipped():
                return "⏳ 議論の終了は受付済みです。まもなく投票へ進みます。"
            previous_generation = state.day_discussion_skip_generation
            state.day_discussion_skip_generation = state.day_generation
            try:
                await self._persist_room_state()
            except Exception as error:
                state.day_discussion_skip_generation = previous_generation
                log.exception(f"議論スキップの保存に失敗: {error}")
                return "❌ 保存できませんでした。もう一度押してください。"
            state.day_discussion_skip_event.set()
            if self.is_turn_discussion_mode():
                # 発言中なら現在の枠も閉じる。枠外なら次の枠を開かせない。
                state.turn_done_event.set()
                state.turn_signal_event.set()
            await self._safe_village_send(
                "⏭️ **GMの操作で議論を終了します。** まもなく投票フェーズに入ります。"
            )
            return "⏭️ 議論を終了して投票へ進みます。"
        if (
            # 一斉投票には発言枠も待機列もない。逐次投票用の締切分岐は
            # vote_order が空のターン制でも条件が揃ってしまい、締め切った
            # と表示しながら実際は時間切れまで続く。先にここで捌く。
            phase == Phase.DAY_VOTE
            and not self.uses_sequential_vote()
            and not state.vote_complete_event.is_set()
        ):
            state.vote_closed = True
            try:
                await self._persist_room_state()
            except Exception as error:
                state.vote_closed = False
                log.exception(f"一斉投票締切の保存に失敗: {error}")
                return "❌ 保存できませんでした。もう一度押してください。"
            state.vote_complete_event.set()
            await self._safe_village_send(
                "⏭️ **GMの操作で投票を締め切ります。**（未投票は棄権）"
            )
            return "⏭️ 投票を締め切りました。"
        if (
            phase == Phase.DAY_VOTE
            and self.uses_sequential_vote()
            and expected_vote_slot_token is not None
            and expected_vote_slot_token != state.vote_slot_token
        ):
            return "⏳ 対象の投票枠は既に終了しています。状況を更新してください。"
        if (
            phase == Phase.DAY_VOTE
            and self.uses_sequential_vote()
            and state.vote_slot_active
            and not state.vote_speech_finished
        ):
            old_window = state.vote_speech_window_open
            state.vote_speech_finished = True
            state.vote_speech_window_open = False
            try:
                await self._persist_room_state()
            except Exception as error:
                state.vote_speech_finished = False
                state.vote_speech_window_open = old_window
                await self._stop_for_durability_error("GMによる投票発言終了の保存", error)
                return "❌ 保存できないため安全停止しました。"
            state.speech_done_event.set()
            await self._safe_village_send("⏭️ **GMの操作で現在の投票発言を終了します。**")
            return "⏭️ 現在の投票発言を終了しました。投票先の確定は待ち続けます。"
        if (
            phase == Phase.DAY_VOTE
            and self.uses_sequential_vote()
            and state.vote_slot_active
            and state.vote_speech_finished
            and not state.vote_slot_forced_abstain
        ):
            voter = state.get_player(state.current_speaker_id)
            if voter is None or not voter.alive:
                state.vote_choice_event.set()
                return "⏳ 現在の投票者は既に除外されています。"
            if voter.user_id in state.votes:
                return "⏳ すでに投票確定済みです。"
            state.vote_slot_forced_abstain = True
            if not await self._record_vote_abstentions([voter], "本投票", 0):
                state.vote_slot_forced_abstain = False
                return "❌ 棄権を保存できませんでした。もう一度押してください。"
            state.vote_choice_event.set()
            await self._safe_village_send(
                f"⏭️ **GMの操作で {voter.display_name} は棄権となります。**"
            )
            return "⏭️ 現在の投票者を棄権にして次へ進めます。"
        if (
            phase == Phase.DAY_VOTE
            and self.uses_sequential_vote()
            and not state.vote_slot_active
            # 列に未処理が残っている間は締め切らない。1枠の終了処理は
            # 「vote_slot_activeを下ろす → ミュート戻し(API) → cursor更新(DB)」
            # の順で、その数百msはactive=Falseだが列は消化しきっていない。
            # cursorも見ないと、GMが「今の発言を飛ばす」つもりで押しただけで
            # 並んでいた残り全員の投票権ごと締め切ってしまう。
            and state.vote_slot_index >= len(state.vote_order)
            and not state.vote_closed
        ):
            # 通常は締切時間を設けない。GMが明示的に締めた場合だけ、
            # まだ「投票参加」を押していない生存者を棄権として閉じる。
            not_pressed = self._vote_queue_waiting()
            state.vote_closed = True
            if not await self._record_vote_abstentions(not_pressed, "本投票", 0):
                state.vote_closed = False
                return "❌ 保存できませんでした。もう一度押してください。"
            state.vote_queue_event.set()
            await self._safe_village_send(
                "⏭️ **GMの操作で投票を締め切ります。**（未投票は棄権）"
            )
            return "⏭️ 投票を締め切りました。"
        if (
            phase in (Phase.DAY_RUNOFF_SPEECH, Phase.DAY_LAST_WILL)
            and state.current_speaker_id is not None
            and not state.speech_done_event.is_set()
        ):
            state.speech_done_event.set()
            label = "決戦弁明" if phase == Phase.DAY_RUNOFF_SPEECH else "遺言"
            await self._safe_village_send(f"⏭️ **GMの操作で現在の{label}を終了します。**")
            return f"⏭️ 現在の{label}をスキップしました。"
        return "⏳ 現在スキップできる時間待ちはありません。"

    # ============================================================
    # ゲームループ
    # ============================================================

    async def _game_loop(self) -> None:
        state = self.state
        try:
            # 役職確認フェーズ (pause対応)。夜と同じく制限時間は目安で、
            # 参加者全員が「役職を確認した」を押すまで1日目へ進まない。
            # 人狼の初日挨拶はこの30秒と同時に走り、確認完了時にも終了する。
            def prep_content(remaining: float) -> str:
                return (
                    f"⏳ 役職確認タイム（{PREPARATION_TIME}秒）\n"
                    + self._timer_line(remaining, "目安")
                )

            # 復元で「確認済み」のまま戻ってきた場合はパネルもタイマーも出さない
            if not state.prep_confirmed:
                greeting_task: Optional[asyncio.Task] = None
                try:
                    prep_msg = await self._safe_village_send(
                        prep_content(PREPARATION_TIME)
                    )
                    # 宣言パネルは役職DM配布後に出す (DM到着前に全員が押して
                    # 1日目へ進んでしまわないよう、夜の朝パネルと同じ順序にする)
                    await self._post_prep_panel()
                    await self._repost_gm_panel()
                    if self._check_prep_ready():
                        await self._persist_room_state()
                        state.prep_ready_event.set()
                    # 両タイマーは、参加者が操作できるパネルを掲示し終えた時点から
                    # 同時に開始する。ローカル参照を保持してGC・例外を回収する。
                    greeting_task = asyncio.create_task(
                        self._initial_wolf_greeting_during_preparation()
                    )
                    await self._pausable_countdown(
                        prep_msg,
                        prep_content,
                        PREPARATION_TIME,
                        state.prep_ready_event,
                    )
                    # どちらも30秒か同じEventで終わる。先に挨拶taskの保存まで
                    # 確定し、その後は未確認者だけを無期限で待つ。
                    await greeting_task
                    await self._wait_for_prep_ready()
                    # 待機解除と同時にGMが一時停止した場合、パネル終了やSEだけが
                    # 停止中に先行しないよう、進行再開を待ってから副作用を出す。
                    await state.pause_event.wait()
                    await self._close_prep_panel()
                    self._play_se("prep_end")
                finally:
                    if greeting_task is not None and not greeting_task.done():
                        greeting_task.cancel()
                        try:
                            await greeting_task
                        except asyncio.CancelledError:
                            pass
                    elif greeting_task is not None and not greeting_task.cancelled():
                        # prep側が先に例外になった場合もtask例外を未回収にしない。
                        greeting_task.exception()
                    state.wolf_relay_window_open = False

            if state.surrender_confirmed or state.ending or state.pending_winner is not None:
                return
            # v0.51以前のPREPARATION snapshotで「確認済み・初夜未完了」だった
            # 場合と、旧INITIAL_NIGHT復旧だけは従来の単独30秒を維持する。
            if not state.initial_night_completed:
                await self._initial_night_greeting()
            if state.surrender_confirmed or state.ending or state.pending_winner is not None:
                return

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
                await self._process_night()
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
        if self.is_turn_discussion_mode():
            await self._turn_day_discussion()
            return

        state = self.state
        await state.pause_event.wait()
        state.phase = Phase.DAY_DISCUSSION
        state.votes.clear()
        # 再起動をまたいでも、その日ぶんのGMスキップ指示だけを引き継ぐ。
        self._sync_day_discussion_skip_event()
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

        duration = state.get_day_discussion_time(
            self.variant.crosstalk_discussion_seconds
        )
        def discussion_content(remaining: float) -> str:
            return (
                f"☀️ **{state.day_number}日目 - 議論タイム** (生存者: {len(state.alive_players())}人)\n"
                "議論終了後、「投票参加」を押した順に1人ずつ投票発言を行います。\n"
                + self._timer_line(remaining)
            )

        # 投票は議論終了後の30秒発言枠でのみ受け付ける。議論中の仮投票を
        # 残すと、順番が来る前に票だけ確定して発言枠が空になるため作らない。
        timer_msg = await self._safe_village_send(discussion_content(duration))
        await self.post_village_panel()
        await self._repost_gm_panel()

        skipped = await self._pausable_countdown(
            timer_msg,
            discussion_content,
            duration,
            state.day_discussion_skip_event,
        )

        # 議論終了。ミュートが行き渡るまで数秒かかるので、
        # 「終了 = もう喋れない」と誤解させない文言にする
        self._play_se("discussion_end")
        await self._safe_village_send(
            "⏰ **GMの操作で議論を終了しました！** 全員をミュートしています…"
            if skipped
            else "⏰ **議論時間終了！** 全員をミュートしています…"
        )
        await self._lock_village()
        await self._mute_phase("まもなく投票フェーズに入ります。")

    # ============================================================
    # 昼フェーズ: ターン制議論
    # ============================================================

    def _turn_round_durations(self) -> tuple[int, ...]:
        """当日の巡ごとの持ち時間を返す。"""
        values = tuple(self.variant.turn_round_seconds)
        if len(values) != 3:
            raise RuntimeError(
                f"ターン時間設定が不正です ({self.variant.variant_id}: {values!r})"
            )
        return values[:2] if self.state.day_number == 1 else (values[-1],)

    def _build_turn_order(self, anchor_number: int) -> list[int]:
        """起点から番号昇順・末尾で折り返す固定順を作る。

        死亡者も順序には残す。襲撃死の席を起点にした場合は実行時のalive判定で
        その席を飛ばし、次の生存番号から始められるため。
        """
        player_count = self.variant.player_count
        players = list(self.state.players.values())
        players.sort(key=lambda p: ((p.number - anchor_number) % player_count, p.number))
        return [player.user_id for player in players]

    async def _persist_turn_checkpoint(self, context: str) -> None:
        try:
            await self._persist_room_state()
        except Exception as error:
            await self._stop_for_durability_error(context, error)
            raise StateDurabilityError(f"{context}を保存できませんでした") from error

    async def _initialize_turn_day(self) -> bool:
        """新しい昼の起点・順序・cursorを一度だけ確定する。

        Returns:
            True: 新しく初期化した / False: 保存済みcursorを再利用した
        """
        state = self.state
        if state.turn_day_generation == state.day_generation and state.turn_order:
            return False

        alive = state.alive_players()
        if not alive:
            raise RuntimeError("ターン開始時に生存者がいません")

        anchor_number = state.next_turn_anchor_number if state.day_number > 1 else None
        # 襲撃死者が再起動中にサーバーを退出すると、復元時にはplayersから
        # 除外される。それでも「死亡した席の次から」という夜の確定結果は
        # 失わず、存在しない席番号を起点に残存者を時計回りへ並べる。
        if (
            isinstance(anchor_number, bool)
            or not isinstance(anchor_number, int)
            or not 1 <= anchor_number <= self.variant.player_count
        ):
            anchor_number = secrets.choice(alive).number

        state.phase = Phase.DAY_DISCUSSION
        state.votes.clear()
        state.vote_complete_event.clear()
        state.turn_day_generation = state.day_generation
        state.turn_anchor_number = int(anchor_number)
        state.next_turn_anchor_number = None
        state.turn_order = self._build_turn_order(int(anchor_number))
        state.turn_round_index = 0
        state.turn_slot_index = 0
        state.turn_slot_active = False
        state.current_speaker_id = None
        state.turn_original_speaker_id = None
        state.turn_interrupt_active = False
        state.turn_interrupt_pending_id = None
        state.turn_interrupts_used = 0
        state.turn_co_declarations = []
        state.turn_panel_message_id = None
        state.turn_window_open = False
        state.turn_remaining_seconds = 0.0
        state.turn_done_event.clear()
        state.turn_interrupt_event.clear()
        state.turn_signal_event.clear()
        await self._persist_turn_checkpoint("ターン制議論の開始位置")
        return True

    def _turn_panel_reference(self) -> Optional[discord.Message]:
        """保存済みIDから再編集可能な話者パネル参照を作る。"""
        state = self.state
        channel = self._ui_cleanup_channel()
        get_partial_message = getattr(channel, "get_partial_message", None)
        if state.turn_panel_message_id is None or not callable(get_partial_message):
            return None
        return get_partial_message(state.turn_panel_message_id)

    # 旧CO機構の一覧表示・パネル即時更新 (_turn_co_declaration_line /
    # _refresh_turn_co_declaration_panel) は撤去した。CO一覧は #昼 常設パネル
    # (build_village_panel_content) が表示する。

    async def _disable_recovered_turn_panel(
        self, *, clear_on_success: bool = False,
    ) -> bool:
        """再起動前のViewを表示上も外し、再開時に同じ1枚を再利用する。"""
        if not self.is_turn_discussion_mode() and not clear_on_success:
            return True
        panel = self._turn_panel_reference()
        if panel is None:
            return self.state.turn_panel_message_id is None
        try:
            await self._discord_api_call(panel.edit, view=None)
        except discord.NotFound:
            self.state.turn_panel_message_id = None
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning(
                f"復元ターン話者パネルの無効化失敗 ({self.state.room_name}): {error}"
            )
            return False
        else:
            if clear_on_success:
                self.state.turn_panel_message_id = None
        return True

    # ------------------------------------------------------------------
    # #昼 常設村パネル (VillagePanelView)
    #
    # 朝パネル (削除→新規) やターン話者パネル (編集で使い回す) と違い、
    # このパネルは昼夜をまたいで1試合中ずっと生きる。フェーズが切り替わる
    # たびに「削除→新規送信」でGMパネルより上流の位置から末尾へ動かし、
    # フェーズ内の押下 (CO等) は同じメッセージへの edit で反映する。
    # ------------------------------------------------------------------

    # Discordのメッセージ上限は2000字。編集が落ちるとCO受付ごと死ぬので、
    # 余裕を持たせた値で自分から先に切り詰める。
    _VILLAGE_PANEL_MAX_LENGTH: int = 1900

    _VILLAGE_PANEL_PHASE_LABELS: dict[Phase, str] = {
        Phase.DAY_DISCUSSION: "昼の議論",
        Phase.DAY_VOTE: "投票",
        Phase.DAY_RUNOFF_SPEECH: "決選投票の弁明",
        Phase.DAY_RUNOFF_VOTE: "決選投票",
        Phase.DAY_LAST_WILL: "遺言",
        Phase.NIGHT: "夜",
        Phase.MORNING: "朝",
    }

    # CO宣言・撤回を受け付ける昼フェーズ (仕様§2-2)。
    _CO_OPEN_PHASES: frozenset[Phase] = frozenset({
        Phase.DAY_DISCUSSION,
        Phase.DAY_VOTE,
        Phase.DAY_RUNOFF_SPEECH,
        Phase.DAY_RUNOFF_VOTE,
        Phase.DAY_LAST_WILL,
    })

    # CO宣言で選べる役職の6択 (仕様§2-2)。
    CO_CLAIMABLE_ROLES: tuple[str, ...] = (
        "占い師", "霊能者", "狩人", "狂人", "村人", "その他",
    )

    # 実役職とは無関係に自己申告できる公開結果。白黒の真偽もBotは判定しない。
    # 狩人は結果ではなく、その日に公表する護衛先を1人選ぶ。
    CO_RESULT_JUDGEMENTS: dict[str, frozenset[str]] = {
        Role.SEER.value: frozenset({"白", "黒"}),
        Role.MEDIUM.value: frozenset({"白", "黒"}),
        Role.GUARD.value: frozenset({"護衛"}),
    }
    _CO_RESULT_LABELS: dict[str, str] = {
        Role.SEER.value: "占い",
        Role.MEDIUM.value: "霊媒",
        Role.GUARD.value: "護衛",
    }

    def _village_panel_reference(self) -> Optional[discord.Message]:
        """保存済みIDから再編集可能な村パネル参照を作る。"""
        state = self.state
        channel = state.village_channel
        get_partial_message = getattr(channel, "get_partial_message", None)
        if state.village_panel_message_id is None or not callable(get_partial_message):
            return None
        return get_partial_message(state.village_panel_message_id)

    def build_village_panel_content(self) -> str:
        """フェーズ・CO・公開結果を、昼夜を問わず1枚にまとめる。"""
        state = self.state
        phase = self._effective_phase()
        phase_label = self._VILLAGE_PANEL_PHASE_LABELS.get(
            phase, phase.name if phase is not None else "不明"
        )

        # 役職名まで出すのが新CO機構の目的 (旧機構は名前だけだった)。
        # co_claims は user_id をキーにした dict で、値の側に user_id は持たない。
        co_entries = [
            (user_id, claim)
            for user_id, claim in state.co_claims.items()
            if isinstance(claim, dict)
        ]
        co_entries.sort(key=lambda entry: entry[1].get("number") or 0)

        # 「占いCO3人」のように役職ごとにまとめるのが盤面整理の読み方。
        # 番号順に混在させるより、何COが何人出ているかが一目で分かる。
        # 並びは CO_CLAIMABLE_ROLES の順 (占い師→霊能者→狩人→狂人→村人→その他)。
        grouped: dict[str, list[tuple[object, dict]]] = {}
        for user_id, claim in co_entries:
            if not (claim.get("display_name") and claim.get("role")):
                continue
            grouped.setdefault(str(claim.get("role")), []).append((user_id, claim))

        ordered_roles = [role for role in self.CO_CLAIMABLE_ROLES if role in grouped]
        ordered_roles += [role for role in grouped if role not in ordered_roles]

        valid_results: list[dict] = []
        role_order = {
            Role.SEER.value: 0,
            Role.MEDIUM.value: 1,
            Role.GUARD.value: 2,
        }
        for row in state.co_result_claims:
            if not isinstance(row, dict):
                continue
            role = str(row.get("role", ""))
            judgement = str(row.get("judgement", ""))
            if judgement not in self.CO_RESULT_JUDGEMENTS.get(role, frozenset()):
                continue
            try:
                normalized = {
                    **row,
                    "user_id": int(row["user_id"]),
                    "number": int(row["number"]),
                    "target_number": int(row["target_number"]),
                    "day": int(row["day"]),
                    "role": role,
                    "judgement": judgement,
                }
            except (KeyError, TypeError, ValueError):
                continue
            valid_results.append(normalized)
        valid_results.sort(key=lambda row: (
            row["number"], row["day"], role_order.get(row["role"], 99)
        ))
        results_by_actor: dict[int, list[dict]] = {}
        for row in valid_results:
            results_by_actor.setdefault(row["user_id"], []).append(row)

        def result_line(row: dict) -> str:
            label = self._CO_RESULT_LABELS.get(row["role"], row["role"])
            target_name = str(row.get("target_name") or "?")[:48]
            target = f"{row['target_number']}番 {target_name}"
            if row["role"] == Role.GUARD.value:
                return f"　└ {row['day']}日目 {label}: {target}"
            return (
                f"　└ {row['day']}日目 {label}: {target}"
                f" → {row['judgement']}"
            )

        displayed_co_ids = {
            int(user_id)
            for members in grouped.values()
            for user_id, _claim in members
        }

        def render(visible_result_count: int) -> tuple[str, int]:
            shown = 0
            co_lines: list[str] = []
            for role in ordered_roles:
                members = grouped[role]
                co_lines.append(f"【{str(role)[:24]}CO {len(members)}人】")
                for user_id, claim in members:
                    co_lines.append(
                        f"・{claim.get('number')}番 "
                        f"{str(claim.get('display_name'))[:48]}"
                    )
                    for result in results_by_actor.get(int(user_id), []):
                        if shown >= visible_result_count:
                            break
                        co_lines.append(result_line(result))
                        shown += 1

            result_only_lines: list[str] = []
            for actor_id, results in results_by_actor.items():
                if actor_id in displayed_co_ids or shown >= visible_result_count:
                    continue
                visible = min(len(results), visible_result_count - shown)
                if visible <= 0:
                    continue
                first = results[0]
                result_only_lines.append(
                    f"・{first['number']}番 "
                    f"{str(first.get('display_name') or '?')[:48]}"
                )
                for result in results[:visible]:
                    result_only_lines.append(result_line(result))
                    shown += 1
            if result_only_lines:
                co_lines.append("【結果申告のみ】")
                co_lines.extend(result_only_lines)

            omitted = len(valid_results) - shown
            if omitted > 0:
                co_lines.append(f"・結果ほか{omitted}件")
            lines = [
                f"🏘️ **村パネル**（現在フェーズ: {phase_label}）",
                "📣 **CO一覧**\n" + ("\n".join(co_lines) if co_lines else "・なし"),
            ]
            return "\n".join(lines), shown

        # CO行は必ず残し、長くなり得る結果行だけを末尾から省略する。
        # 盤面のeditが2000字制限で失敗すると以後のCO操作も見えなくなるため、
        # 余裕を持った1900字以内に収まる表示件数を選ぶ。
        for visible_count in range(len(valid_results), -1, -1):
            content, _shown = render(visible_count)
            if len(content) <= self._VILLAGE_PANEL_MAX_LENGTH:
                return content
        content, _shown = render(0)
        return content[:self._VILLAGE_PANEL_MAX_LENGTH]

    def build_medium_results_content(self) -> str:
        """霊媒ボタン用の霊能結果一覧 (新しい順) をephemeral向けに整形する。"""
        state = self.state
        if not state.medium_results:
            return "👻 霊能結果はまだありません。"
        lines = ["👻 **霊能結果**（新しい順）"]
        for entry in reversed(state.medium_results):
            if not isinstance(entry, dict):
                continue
            day = entry.get("day", "?")
            name = entry.get("display_name", "?")
            result = entry.get("result", "?")
            lines.append(f"・{day}日目: {name} は {result} でした。")
        return "\n".join(lines)

    def _night_label(self, night_number: object) -> str:
        """夜行動ログの night_number を表示用の日付ラベルにする。"""
        try:
            night = int(night_number)
        except (TypeError, ValueError):
            return "?日目夜"
        return "0日目初夜" if night == 0 else f"{night}日目夜"

    def _record_target_name(self, row: dict) -> str:
        """夜行動ログ1行の対象を表示名にする。

        GM除外などで state から居なくなった相手でも、記録側に残っている
        番号で「#7」と出せるようにする (履歴が歯抜けに見えないため)。
        """
        target_id = row.get("target_id")
        if target_id is not None:
            target = self.state.get_player(int(target_id))
            if target is not None:
                return target.display_name
        number = row.get("target_number")
        return f"#{number}" if number else "?"

    async def _load_night_records(
        self,
        *,
        actor_id: Optional[int] = None,
        actions: Optional[tuple[str, ...]] = None,
    ) -> list[dict]:
        """この試合の夜行動ログを読む。失敗しても空リストで進む。

        履歴表示は「見えないと困るが、見えなくても進行は止めない」ので、
        DB例外はここで吸収する (仕様§0-8と同じ扱い)。
        """
        state = self.state
        try:
            return await database.list_night_actions_for_run(
                state.guild.id, state.room_id, state.game_run_id,
                actor_id=actor_id, actions=actions,
            )
        except Exception as error:
            log.warning(f"夜行動ログの読み出しに失敗 ({state.room_name}): {error}")
            return []

    async def build_seer_history_content(self, actor_id: int) -> str:
        """占い師本人の[占い]ボタン用。初日白から直近までを古い順で返す。

        DBが正 (再起動をまたいでも残る) だが、今夜ぶんの追記に失敗していた
        場合だけ state.seer_target から補って、その夜の結果を落とさない。
        """
        state = self.state
        rows = await self._load_night_records(
            actor_id=actor_id, actions=("初日白", "占い"),
        )
        lines: list[str] = []
        recorded_nights: set[int] = set()
        for row in rows:
            name = self._record_target_name(row)
            result = row.get("result") or "?"
            if row.get("action") == "初日白":
                lines.append(f"・初日白（ランダム）: {name} は **{result}**")
                continue
            try:
                recorded_nights.add(int(row.get("night_number")))
            except (TypeError, ValueError):
                pass
            lines.append(
                f"・{self._night_label(row.get('night_number'))}: {name} は **{result}**"
            )
        if state.seer_target is not None and state.day_number not in recorded_nights:
            target = state.get_player(state.seer_target)
            if target is not None:
                judgement = "人狼" if target.role == Role.WEREWOLF else "村人"
                lines.append(
                    f"・{self._night_label(state.day_number)}: "
                    f"{target.display_name} は **{judgement}**"
                )
        if not lines:
            return "🔮 占いの記録はまだありません。"
        return "🔮 **占いの記録**（古い順）\n" + "\n".join(lines)

    async def build_guard_history_content(self, actor_id: int) -> str:
        """狩人本人の[狩人]ボタン用。護衛先を古い順で返す。

        GJの有無はここに出さない。朝の公開文は護衛成功・噛みなし・襲撃なしを
        すべて「平和な朝」に統一しているので、狩人にだけ「これは自分のGJだ」と
        教えると、村に出ていない情報を本人だけが持つことになる
        (記録自体は `action='襲撃確定'` / `result='GJ'` としてDBに残す)。
        """
        rows = await self._load_night_records(actor_id=actor_id, actions=("護衛",))
        state = self.state
        lines: list[str] = []
        recorded_nights: set[int] = set()
        for row in rows:
            try:
                night = int(row.get("night_number"))
            except (TypeError, ValueError):
                night = -1
            recorded_nights.add(night)
            lines.append(
                f"・{self._night_label(row.get('night_number'))}: "
                f"{self._record_target_name(row)}"
            )
        if state.guard_target is not None and state.day_number not in recorded_nights:
            target = state.get_player(state.guard_target)
            if target is not None:
                lines.append(
                    f"・{self._night_label(state.day_number)}: {target.display_name}"
                )
        if not lines:
            return "🛡️ 護衛の記録はまだありません。"
        return "🛡️ **護衛の記録**（古い順）\n" + "\n".join(lines)

    async def post_village_panel(self) -> None:
        """村パネルを #昼 の末尾へ掲示し直す (旧メッセージは削除)。

        GMパネルが常に最下部に残るよう、呼び出し順は必ず
        `_repost_gm_panel()` の直前にする。
        """
        state = self.state
        ch = state.village_channel
        if ch is None:
            return
        old_message_id = state.village_panel_message_id
        old_view = self._village_panel_view
        new_view = VillagePanelView(self)
        try:
            new_message = await self._discord_api_call(
                ch.send, self.build_village_panel_content(), view=new_view
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"村パネル投稿失敗 ({state.room_name}): {e}")
            new_view.stop()
            if new_view in self._game_views:
                self._game_views.remove(new_view)
            return
        self._village_panel_view = new_view
        state.village_panel_message_id = getattr(new_message, "id", None)
        if old_view is not None:
            try:
                old_view.stop()
            except Exception:
                pass
            if old_view in self._game_views:
                self._game_views.remove(old_view)
        if old_message_id is not None:
            get_partial = getattr(ch, "get_partial_message", None)
            if callable(get_partial):
                try:
                    await self._discord_api_call(get_partial(old_message_id).delete)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"旧村パネル削除失敗 ({state.room_name}): {e}")
        try:
            await self._persist_room_state()
        except Exception as e:
            # 消し損ねたパネルが1枚残るだけで進行は続けられる
            log.warning(f"村パネルIDの保存に失敗 ({state.room_name}): {e}")

    def refresh_village_panel(self) -> None:
        """村パネルの本文だけをeditする (1.5秒デバウンス)。

        CO宣言等は連打され得るため、待機中の再要求は最新1件にまとめて
        edit APIを連打しない。既にタスクが走っていれば、完了後の
        `_village_panel_refresh_pending` を立てるだけで新規タスクは作らない。
        """
        if self._village_panel_refresh_task is not None and not self._village_panel_refresh_task.done():
            self._village_panel_refresh_pending = True
            return
        self._village_panel_refresh_task = self.manager.spawn_bg_task(
            self._debounced_village_panel_refresh()
        )

    async def _debounced_village_panel_refresh(self) -> None:
        try:
            await asyncio.sleep(1.5)
            while True:
                self._village_panel_refresh_pending = False
                await self._apply_village_panel_refresh()
                if not self._village_panel_refresh_pending:
                    break
                await asyncio.sleep(1.5)
        finally:
            self._village_panel_refresh_task = None

    async def _apply_village_panel_refresh(self) -> None:
        panel = self._village_panel_reference()
        if panel is None:
            return
        try:
            await self._discord_api_call(
                panel.edit, content=self.build_village_panel_content()
            )
        except discord.NotFound:
            self.state.village_panel_message_id = None
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning(f"村パネル更新失敗 ({self.state.room_name}): {error}")

    async def close_village_panel(self) -> bool:
        """試合終了時に村パネルを削除する (再起動残骸と同じ削除経路)。"""
        state = self.state
        if self._village_panel_view is not None:
            try:
                self._village_panel_view.stop()
            except Exception:
                pass
            if self._village_panel_view in self._game_views:
                self._game_views.remove(self._village_panel_view)
            self._village_panel_view = None
        message_id = state.village_panel_message_id
        ch = self._ui_cleanup_channel()
        if message_id is None:
            return True
        if ch is None:
            return False
        get_partial = getattr(ch, "get_partial_message", None)
        if not callable(get_partial):
            return False
        try:
            await self._discord_api_call(get_partial(message_id).delete)
        except discord.NotFound:
            state.village_panel_message_id = None
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"村パネル削除失敗 ({state.room_name}): {e}")
            return False
        else:
            state.village_panel_message_id = None
        return True

    async def _disable_recovered_village_panel(self) -> None:
        """再起動前のViewを表示上も外す (ターン話者パネルと同じ方式)。

        GMが「再開」を押すと、以後の `_day_discussion` / `_night_phase` が
        `post_village_panel()` を呼ぶため、削除まではせず無効化だけする。
        """
        panel = self._village_panel_reference()
        if panel is None:
            return
        try:
            await self._discord_api_call(panel.edit, view=None)
        except discord.NotFound:
            self.state.village_panel_message_id = None
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning(
                f"復元村パネルの無効化失敗 ({self.state.room_name}): {error}"
            )

    # ------------------------------------------------------------------
    # CO宣言・撤回 (仕様§2-2)
    #
    # DBへの追記 (record_co_event) は state 更新 +
    # _persist_room_state() が成功した「後」に行う。DB書き込みが失敗しても
    # ログに残すだけで進行は止めない (仕様0-8)。event_seq は
    # state.record_event_seq を単調増加させて払い出す (投票・夜行動ログと共有)。
    # ------------------------------------------------------------------

    def co_actions_open(self) -> bool:
        """CO宣言・撤回を受け付ける昼フェーズか。"""
        return self._effective_phase() in self._CO_OPEN_PHASES

    def _co_action_reject_reason(self, actor_id: int) -> Optional[str]:
        """CO関連の全操作に共通する受付判定。弾く理由があれば文言を返す。"""
        state = self.state
        if not self.is_current_game_view(state.game_run_id):
            return "⏳ この村パネルは終了しています。"
        actor = state.get_player(actor_id)
        if actor is None or not actor.alive:
            return "⏳ 生存中の参加者だけが操作できます。"
        if not self.co_actions_open():
            return "⏳ 現在はCOを受け付けていません。"
        if actor_id in state.disconnected_players:
            return "VCへ復帰してから操作してください。"
        return None

    async def declare_co(self, actor_id: int, role: str) -> Optional[str]:
        """役職1個までのCO宣言。既にCO中なら拒否する (同時に複数役職は不可)。"""
        async with self.action_lock:
            state = self.state
            reason = self._co_action_reject_reason(actor_id)
            if reason is not None:
                return reason
            if actor_id in state.co_claims:
                return "既にCOしています。先に撤回してください。"
            actor = state.get_player(actor_id)
            claim = {
                "number": actor.number,
                "display_name": actor.display_name,
                "role": role,
                "day": state.day_number,
            }
            state.co_claims[actor_id] = claim
            state.record_event_seq += 1
            seq = state.record_event_seq
            try:
                await self._persist_room_state()
            except Exception as error:
                del state.co_claims[actor_id]
                state.record_event_seq -= 1
                log.exception(f"CO宣言の保存に失敗: {error}")
                return "CO宣言を保存できませんでした。もう一度お試しください。"

            try:
                await database.record_co_event(
                    state.guild.id, state.room_id, state.game_run_id,
                    event_seq=seq, day_number=state.day_number,
                    phase=self._effective_phase().name,
                    actor_id=actor_id, actor_number=actor.number,
                    event_type="CO", claimed_role=role,
                )
            except Exception as error:
                log.exception(f"CO宣言のDB記録に失敗 ({state.room_name}): {error}")
            self.refresh_village_panel()
            return None

    async def withdraw_co(self, actor_id: int) -> Optional[str]:
        """CO撤回。撤回後は別役職を改めてCOできる。"""
        async with self.action_lock:
            state = self.state
            reason = self._co_action_reject_reason(actor_id)
            if reason is not None:
                return reason
            claim = state.co_claims.get(actor_id)
            if claim is None:
                return "CO中ではありません。"
            del state.co_claims[actor_id]
            state.record_event_seq += 1
            seq = state.record_event_seq
            try:
                await self._persist_room_state()
            except Exception as error:
                state.co_claims[actor_id] = claim
                state.record_event_seq -= 1
                log.exception(f"CO撤回の保存に失敗: {error}")
                return "CO撤回を保存できませんでした。もう一度お試しください。"

            try:
                await database.record_co_event(
                    state.guild.id, state.room_id, state.game_run_id,
                    event_seq=seq, day_number=state.day_number,
                    phase=self._effective_phase().name,
                    actor_id=actor_id, actor_number=claim["number"],
                    event_type="撤回", claimed_role=claim["role"],
                )
            except Exception as error:
                log.exception(f"CO撤回のDB記録に失敗 ({state.room_name}): {error}")
            self.refresh_village_panel()
            return None

    async def publish_co_result(
        self,
        actor_id: int,
        claimed_role: str,
        target_id: int,
        judgement: str,
        *,
        expected_game_run_id: str,
    ) -> Optional[str]:
        """占い・霊媒・護衛結果を公開する（実役職やCO内容とは照合しない）。

        同じ日・本人・結果種別の再申告は最新1件へ置き換える。snapshotでは
        現在の盤面だけを保存し、記録DBには旧内容の取消と新内容の公開を
        1transactionで追記する。
        """
        async with self.action_lock:
            state = self.state
            if expected_game_run_id != state.game_run_id:
                return "⏳ この結果申告パネルは終了しています。"
            reason = self._co_action_reject_reason(actor_id)
            if reason is not None:
                return reason
            allowed = self.CO_RESULT_JUDGEMENTS.get(claimed_role)
            if allowed is None or judgement not in allowed:
                return "選択された結果を申告できません。"
            actor = state.get_player(actor_id)
            target = state.get_player(target_id)
            if target is None:
                return "対象が参加者ではありません。"
            if target_id == actor_id:
                return "自分自身は結果の対象にできません。"

            key = (actor_id, state.day_number, claimed_role)
            previous_index: Optional[int] = None
            previous: Optional[dict] = None
            for index, row in enumerate(state.co_result_claims):
                if not isinstance(row, dict):
                    continue
                if (
                    row.get("user_id") == key[0]
                    and row.get("day") == key[1]
                    and row.get("role") == key[2]
                ):
                    previous_index = index
                    previous = row
                    break
            if (
                previous is not None
                and previous.get("target_id") == target_id
                and previous.get("judgement") == judgement
            ):
                return None

            result = {
                "user_id": actor_id,
                "number": actor.number,
                "display_name": actor.display_name,
                "role": claimed_role,
                "target_id": target_id,
                "target_number": target.number,
                "target_name": target.display_name,
                "judgement": judgement,
                "day": state.day_number,
            }
            old_results = state.co_result_claims
            new_results = list(old_results)
            if previous_index is None:
                new_results.append(result)
            else:
                new_results[previous_index] = result

            old_seq = state.record_event_seq
            db_events: list[dict] = []
            if previous is not None:
                db_events.append({
                    "event_seq": old_seq + 1,
                    "day_number": state.day_number,
                    "actor_id": actor_id,
                    "actor_number": int(previous.get("number", actor.number)),
                    "claimed_role": claimed_role,
                    "event_type": "取消",
                    "target_id": int(previous["target_id"]),
                    "target_number": int(previous["target_number"]),
                    "judgement": str(previous["judgement"]),
                })
            db_events.append({
                "event_seq": old_seq + len(db_events) + 1,
                "day_number": state.day_number,
                "actor_id": actor_id,
                "actor_number": actor.number,
                "claimed_role": claimed_role,
                "event_type": "公開",
                "target_id": target_id,
                "target_number": target.number,
                "judgement": judgement,
            })
            state.co_result_claims = new_results
            state.record_event_seq = old_seq + len(db_events)
            try:
                await self._persist_room_state()
            except Exception as error:
                state.co_result_claims = old_results
                state.record_event_seq = old_seq
                log.exception(f"結果自己申告の保存に失敗: {error}")
                return "結果を保存できませんでした。もう一度お試しください。"

            try:
                await database.record_co_result_events(
                    state.guild.id,
                    state.room_id,
                    state.game_run_id,
                    events=db_events,
                )
            except Exception as error:
                log.exception(
                    f"結果自己申告のDB記録に失敗 ({state.room_name}): {error}"
                )
            self.refresh_village_panel()
            return None

    def _night_role_reject_reason(
        self, actor_id: int, role: Role, role_label: str
    ) -> Optional[str]:
        """占い・狩人の#昼パネル操作に共通する受付判定。弾く理由があれば文言を返す。"""
        state = self.state
        if not self.is_current_game_view(state.game_run_id):
            return "⏳ この村パネルは終了しています。"
        actor = state.get_player(actor_id)
        if actor is None or not actor.alive or actor.role != role:
            return f"生存する{role_label}だけが使えます。"
        if not self.night_actions_open():
            return "⏳ 今は使えません（夜だけ操作できます）。"
        return None

    def seer_targets(self, actor_id: int) -> list:
        """占い師が選べる対象 (自分以外の生存者)。既存 SeerView と同一の絞り込み。"""
        state = self.state
        return by_number(
            [p for p in state.alive_players() if p.user_id != actor_id]
        )

    def guard_targets(self, actor_id: int) -> list:
        """狩人が選べる対象 (自分と前回の護衛先を除く生存者)。既存 GuardView と同一の絞り込み。"""
        state = self.state
        return by_number([
            p for p in state.alive_players()
            if p.user_id != actor_id and p.user_id != state.guard_previous
        ])

    async def commit_seer_target(self, actor_id: int, target_id: int) -> tuple[str, bool]:
        """#昼パネル[占い]の確定処理。(表示文字列, 確定したか) を返す。結果は即時開示。"""
        async with self.action_lock:
            state = self.state
            reason = self._night_role_reject_reason(actor_id, Role.SEER, "占い師")
            if reason is not None:
                return reason, False
            if state.seer_target is not None:
                return "✅ 今夜の占いは既に確定しています。変更はできません。", False
            target = state.get_player(target_id)
            if target is None or not target.alive:
                return "❌ その対象は占えません（既に死亡しています）。", False

            actor = state.get_player(actor_id)
            old_action_log_len = len(state.action_log)
            state.seer_target = target.user_id
            judgement = "人狼" if target.role == Role.WEREWOLF else "村人"
            self.log_action(
                "占い", actor=actor, target=target, detail=f"結果={judgement}",
            )
            state.record_event_seq += 1
            seq = state.record_event_seq
            try:
                await self._persist_room_state()
            except Exception as error:
                state.seer_target = None
                state.record_event_seq -= 1
                del state.action_log[old_action_log_len:]
                log.exception(f"占い結果の保存に失敗: {error}")
                return "❌ 占い結果を保存できませんでした。もう一度実行してください。", False
            # 未行動警告の対象から外す
            self._check_night_complete()

            try:
                await database.record_night_action(
                    state.guild.id, state.room_id, state.game_run_id,
                    event_seq=seq, night_number=state.day_number,
                    actor_id=actor_id, actor_number=actor.number,
                    actor_role="占い師", action="占い",
                    target_id=target.user_id, target_number=target.number,
                    result=judgement,
                )
            except Exception as error:
                log.exception(f"占い結果のDB記録に失敗 ({state.room_name}): {error}")

            text = f"🔮 占い結果: **{target.display_name}** は **{judgement}** でした。"
            # 通常DMは送らない。確認UIはエフェメラルで消えるが、[占い]は
            # 何度押しても初日からの結果を返すようになったため、手元へ残す
            # ためのDMは役目を終えた (DM 1通ぶんのAPI呼び出しも減る)。
            # 村パネルの本文はCO一覧・公開結果のみで占い有無は表示しないため、
            # ここでは refresh_village_panel() を呼ばない
            # (見た目が変わらない編集APIを増やさない)。
            return text, True

    async def commit_guard_target(self, actor_id: int, target_id: int) -> tuple[str, bool]:
        """#昼パネル[狩人]の確定処理。(表示文字列, 確定したか) を返す。"""
        async with self.action_lock:
            state = self.state
            reason = self._night_role_reject_reason(actor_id, Role.GUARD, "狩人")
            if reason is not None:
                return reason, False
            if state.guard_target is not None:
                return "✅ 今夜の護衛は既に確定しています。変更はできません。", False
            # 表示候補を迂回した護衛先も最終的に拒否する (既存 GuardView と同じ検証)。
            target = state.get_player(target_id)
            if target is None:
                return "❌ その対象は護衛できません。", False
            if target.user_id == actor_id:
                return "⚠️ 自分は護衛できません。", False
            if target.user_id == state.guard_previous:
                return "⚠️ 前回と同じ対象は護衛できません。", False
            if not target.alive:
                return "❌ その対象は護衛できません（既に死亡しています）。", False

            actor = state.get_player(actor_id)
            old_action_log_len = len(state.action_log)
            state.guard_target = target.user_id
            self.log_action("護衛", actor=actor, target=target)
            state.record_event_seq += 1
            seq = state.record_event_seq
            try:
                await self._persist_room_state()
            except Exception as error:
                state.guard_target = None
                state.record_event_seq -= 1
                del state.action_log[old_action_log_len:]
                log.exception(f"護衛先の保存に失敗: {error}")
                return "❌ 護衛先を保存できませんでした。もう一度実行してください。", False
            # 未行動警告の対象から外す
            self._check_night_complete()

            try:
                await database.record_night_action(
                    state.guild.id, state.room_id, state.game_run_id,
                    event_seq=seq, night_number=state.day_number,
                    actor_id=actor_id, actor_number=actor.number,
                    actor_role="狩人", action="護衛",
                    target_id=target.user_id, target_number=target.number,
                    result=None,
                )
            except Exception as error:
                log.exception(f"護衛先のDB記録に失敗 ({state.room_name}): {error}")

            # 村パネルの本文は護衛有無を表示しないため refresh_village_panel() は
            # 呼ばない (占いと同じ理由)。
            return f"🛡️ **{target.display_name}** の護衛を確定しました。", True

    async def _replace_turn_message(
        self,
        message: Optional[discord.Message],
        content: str,
        view: Optional[discord.ui.View],
    ) -> Optional[discord.Message]:
        """1日1枚の話者パネルを編集し、消失時だけ再投稿する。"""
        if message is not None:
            try:
                await self._discord_api_call(message.edit, content=content, view=view)
                return message
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                log.warning(f"ターン話者パネル更新失敗 ({self.state.room_name}): {error}")
        new_message = await self._safe_village_send(content, view=view)
        if new_message is not None:
            message_id = getattr(new_message, "id", None)
            if message_id is not None and message_id != self.state.turn_panel_message_id:
                self.state.turn_panel_message_id = int(message_id)
                await self._persist_turn_checkpoint("ターン話者パネルID")
        return new_message

    def _turn_segment_content(
        self,
        speaker: Player,
        remaining: float,
        *,
        interrupt: bool,
    ) -> str:
        state = self.state
        if interrupt:
            title = f"⚡ **{speaker.display_name}** の割り込み発言"
            detail = "元の話者の時間は停止中です。"
        else:
            durations = self._turn_round_durations()
            title = (
                f"🎤 **{speaker.display_name}** の発言 "
                f"({state.turn_round_index + 1}/{len(durations)}巡目)"
            )
            remaining_interrupts = max(
                0, self.variant.turn_interrupts_per_day - state.turn_interrupts_used
            )
            detail = f"本日の割り込み残数: **{remaining_interrupts}回**"
        return f"{title}\n{detail}\n" + self._timer_line(remaining)

    async def _turn_segment_countdown(
        self,
        message: Optional[discord.Message],
        speaker: Player,
        seconds: float,
        *,
        allow_interrupt: bool,
    ) -> tuple[str, float, Optional[discord.Message]]:
        """発言終了・割り込み・一時停止に対応した疎なカウントダウン。

        Returns:
            ("done" | "interrupt" | "timeout", 残り秒, message)
        """
        state = self.state
        remaining = float(seconds)
        state.turn_remaining_seconds = remaining
        last_display = max(0, int(remaining + 0.999))
        loop = asyncio.get_running_loop()

        async def terminal_outcome(*, timed_out: bool = False) -> Optional[str]:
            """発言終了・割り込み・時間切れを受付ロック上で線形化する。

            割り込みは回数確保後のDB保存中もpendingになる。保存を待たずに
            timeoutを確定すると、受付成功を返した要求が実行されず日次枠だけ
            消費されるため、境界ではaction_lockの解放を待って再判定する。
            """
            signaled = state.turn_done_event.is_set() or (
                allow_interrupt
                and (
                    state.turn_interrupt_pending_id is not None
                    or state.turn_interrupt_event.is_set()
                )
            )
            if not signaled and not timed_out:
                return None
            async with self.action_lock:
                if state.turn_done_event.is_set():
                    outcome = "done"
                elif allow_interrupt and (
                    state.turn_interrupt_pending_id is not None
                    or state.turn_interrupt_event.is_set()
                ):
                    outcome = "interrupt"
                elif timed_out:
                    outcome = "timeout"
                else:
                    return None
                # ここから後の押下はturn_actions_openで拒否する。
                state.turn_window_open = False
                # 音声同期はUI窓とは分離してactive中の現在話者を維持するため、
                # 終端確定と同時にactiveも閉じる。cursor保存前に落ちた場合は
                # 同じ枠を満額再実行するので、durability上も安全側になる。
                state.turn_slot_active = False
                return outcome

        while remaining > 0:
            await state.pause_event.wait()
            outcome = await terminal_outcome()
            if outcome is not None:
                return outcome, remaining, message

            chunk = min(remaining, self._PAUSE_POLL)
            start = loop.time()
            try:
                await asyncio.wait_for(state.turn_signal_event.wait(), timeout=chunk)
            except asyncio.TimeoutError:
                pass
            # 一時停止から即座に起こす用途でも使うため、終了/割り込みの
            # 状態フラグを確認する前に集約Eventだけを次の待機へ戻す。
            state.turn_signal_event.clear()
            elapsed = loop.time() - start
            remaining = max(0.0, remaining - elapsed)
            state.turn_remaining_seconds = remaining

            outcome = await terminal_outcome()
            if outcome is not None:
                return outcome, remaining, message

            display = max(0, int(remaining + 0.999))
            if turn_timer_should_update(display, last_display):
                message = await self._safe_timer_edit(
                    message,
                    self._turn_segment_content(
                        speaker, display, interrupt=not allow_interrupt
                    ),
                )
                last_display = display

        outcome = await terminal_outcome(timed_out=True)
        return outcome or "timeout", 0.0, message

    async def _begin_turn_segment(
        self,
        speaker: Player,
        seconds: float,
        message: Optional[discord.Message],
        *,
        interrupt: bool,
        original_speaker_id: Optional[int] = None,
    ) -> tuple[Optional[discord.Message], Optional[TurnSpeechView]]:
        state = self.state
        await state.pause_event.wait()
        if not speaker.alive:
            return message, None
        state.turn_slot_token += 1
        state.current_speaker_id = speaker.user_id
        state.turn_original_speaker_id = original_speaker_id
        state.turn_interrupt_active = interrupt
        state.turn_interrupt_pending_id = None
        state.turn_slot_active = True
        state.turn_window_open = False
        state.turn_remaining_seconds = float(seconds)
        state.turn_done_event.clear()
        state.turn_interrupt_event.clear()
        state.turn_signal_event.clear()
        if self._day_discussion_skipped():
            # GMのスキップとこのclearが競合しても、新しい発言枠を開かない。
            state.turn_done_event.set()
        await self._persist_turn_checkpoint("ターン話者の開始")

        while True:
            if not speaker.alive or state.turn_done_event.is_set():
                state.turn_window_open = False
                return message, None
            await self._grant_turn_speaker(speaker)
            if not speaker.alive or state.turn_done_event.is_set():
                await self._clear_speaker()
                state.turn_window_open = False
                return message, None
            # grantとGM停止が競合した場合、停止側が全員muteを完了するまで
            # パネル受付・タイマーを開かない。再開後に話者だけを再度grantする。
            if not state.paused:
                break
            await self._clear_speaker()
            await state.pause_event.wait()
        view = TurnSpeechView(
            self,
            speaker.user_id,
            state.turn_slot_token,
            allow_interrupt=(
                not interrupt
                and state.turn_interrupts_used < self.variant.turn_interrupts_per_day
            ),
        )
        message = await self._replace_turn_message(
            message,
            self._turn_segment_content(speaker, seconds, interrupt=interrupt),
            view,
        )
        state.turn_window_open = True
        return message, view

    async def _refund_unusable_interrupt(self, player_id: int) -> None:
        state = self.state
        if state.turn_interrupt_pending_id != player_id:
            return
        state.turn_interrupt_pending_id = None
        state.turn_interrupts_used = max(0, state.turn_interrupts_used - 1)
        await self._persist_turn_checkpoint("実行不能な割り込みの返却")

    async def _run_turn_interrupt(
        self,
        original: Player,
        remaining: float,
        message: Optional[discord.Message],
    ) -> Optional[discord.Message]:
        state = self.state
        interrupter_id = state.turn_interrupt_pending_id
        interrupter = state.get_player(interrupter_id) if interrupter_id else None
        if interrupter is None or not interrupter.alive:
            if interrupter_id is not None:
                await self._refund_unusable_interrupt(interrupter_id)
            return message

        message, view = await self._begin_turn_segment(
            interrupter,
            30,
            message,
            interrupt=True,
            original_speaker_id=original.user_id,
        )
        if view is None:
            # 開始前に割り込み者が死亡した場合は未実行の予約を返却する。
            if state.turn_interrupt_pending_id == interrupter.user_id:
                await self._refund_unusable_interrupt(interrupter.user_id)
                return message
            state.current_speaker_id = None
            state.turn_original_speaker_id = None
            state.turn_interrupt_active = False
            state.turn_interrupt_pending_id = None
            state.turn_slot_active = False
            state.turn_remaining_seconds = remaining
            await self._persist_turn_checkpoint("実行不能な割り込み発言の終了")
            return message
        try:
            _, _, message = await self._turn_segment_countdown(
                message, interrupter, 30, allow_interrupt=False
            )
        finally:
            state.turn_window_open = False
            state.turn_slot_active = False
            view.stop()
            await self._clear_speaker()

        state.current_speaker_id = None
        state.turn_original_speaker_id = None
        state.turn_interrupt_active = False
        state.turn_interrupt_pending_id = None
        # 割り込み終了から元話者の再開までにもcheckpointがある。
        # ここでactiveのまま話者だけ消すと、直後の再起動で
        # 「active枠なのに話者なし」という不正snapshotになる。
        # cursor自体は進めず非activeにし、復旧時は同じ通常枠を満額で再実行する。
        state.turn_slot_active = False
        state.turn_remaining_seconds = remaining
        await self._persist_turn_checkpoint("割り込み発言の終了")
        return message

    async def _run_main_turn(
        self,
        speaker: Player,
        duration: int,
        message: Optional[discord.Message],
    ) -> Optional[discord.Message]:
        remaining = float(duration)
        while remaining > 0 and speaker.alive:
            message, view = await self._begin_turn_segment(
                speaker, remaining, message, interrupt=False
            )
            if view is None:
                break
            outcome = "timeout"
            try:
                outcome, remaining, message = await self._turn_segment_countdown(
                    message, speaker, remaining, allow_interrupt=True
                )
            finally:
                self.state.turn_window_open = False
                self.state.turn_slot_active = False
                view.stop()
                await self._clear_speaker()

            if outcome != "interrupt":
                break
            message = await self._run_turn_interrupt(speaker, remaining, message)

        return message

    async def _advance_turn_cursor(self) -> None:
        state = self.state
        state.turn_slot_active = False
        state.current_speaker_id = None
        state.turn_original_speaker_id = None
        state.turn_interrupt_active = False
        state.turn_interrupt_pending_id = None
        state.turn_window_open = False
        state.turn_remaining_seconds = 0.0
        state.turn_slot_index += 1
        if state.turn_slot_index >= len(state.turn_order):
            state.turn_slot_index = 0
            state.turn_round_index += 1
        await self._persist_turn_checkpoint("ターン発言枠の完了")

    async def _turn_day_discussion(self) -> None:
        state = self.state
        await state.pause_event.wait()
        state.phase = Phase.DAY_DISCUSSION
        # 復元で投票フェーズそのものへ戻る場合はこの関数を通らない。
        # 新しい昼議論に入る時点で前日の票だけを消す。
        if state.vote_day_generation != state.day_generation:
            state.votes.clear()

        # ターン制はVCだけで進行し、#昼のテキスト書き込みは許可しない。
        await self._lock_village()
        # 再起動をまたいでも、その日ぶんのGMスキップ指示だけを引き継ぐ。
        self._sync_day_discussion_skip_event()
        initialized = await self._initialize_turn_day()
        durations = self._turn_round_durations()

        if initialized:
            ordered_names = []
            for player_id in state.turn_order:
                player = state.get_player(player_id)
                if player is not None and player.alive:
                    ordered_names.append(player.display_name)
            await self._safe_village_send(
                f"🔁 **{state.day_number}日目 - ターン制議論**\n"
                f"発言順: {' → '.join(ordered_names)}"
            )
            self._play_se("discussion")
        else:
            await self._safe_village_send(
                f"♻️ **{state.day_number}日目のターン制議論** を保存済みの発言枠から再開します。"
            )
        await self.post_village_panel()
        await self._repost_gm_panel()

        turn_message: Optional[discord.Message] = self._turn_panel_reference()
        skipped = self._day_discussion_skipped()
        while not skipped and state.turn_round_index < len(durations):
            if not state.turn_order:
                raise StateDurabilityError("ターン順序が保存されていません")
            if state.turn_slot_index >= len(state.turn_order):
                state.turn_slot_index = 0
                state.turn_round_index += 1
                await self._persist_turn_checkpoint("ターン巡の繰り上げ")
                continue

            player_id = state.turn_order[state.turn_slot_index]
            player = state.get_player(player_id)
            if player is None or not player.alive:
                await self._advance_turn_cursor()
                continue

            duration = durations[state.turn_round_index]
            turn_message = await self._run_main_turn(player, duration, turn_message)
            await self._advance_turn_cursor()
            # 現在の発言を終えた時点で確認する。発言中に押された場合は
            # turn_done_eventがその枠を先に閉じ、ここで残りの巡を打ち切る。
            skipped = self._day_discussion_skipped()

        state.turn_slot_active = False
        state.current_speaker_id = None
        state.turn_window_open = False
        await self._persist_turn_checkpoint("ターン制議論の完了")
        if turn_message is not None:
            await self._replace_turn_message(
                turn_message,
                (
                    f"⏭️ **{state.day_number}日目の議論をGMの操作で終了しました。**"
                    if skipped
                    else f"✅ **{state.day_number}日目の規定発言がすべて終了しました。**"
                ),
                None,
            )
        self._play_se("discussion_end")
        await self._safe_village_send(
            "⏭️ **GMの操作でターン制議論を終了しました。** 投票フェーズに入ります。"
            if skipped
            else "⏰ **ターン制議論終了！** 投票フェーズに入ります。"
        )

    async def request_turn_pass(
        self, actor_id: int, speaker_id: int, turn_token: int
    ) -> Optional[str]:
        """現在話者本人からの発言終了を競合なく受け付ける。"""
        async with self.action_lock:
            state = self.state
            if not self.turn_actions_open() or state.turn_slot_token != turn_token:
                return "この発言枠は既に終了しています。"
            if state.current_speaker_id != speaker_id or actor_id != speaker_id:
                return "発言終了を押せるのは現在の話者本人だけです。"
            if state.turn_interrupt_pending_id is not None or state.turn_interrupt_event.is_set():
                return "割り込みを受け付け済みのため、現在は終了できません。"
            if state.turn_done_event.is_set():
                return "発言終了を受け付け済みです。"
            state.turn_done_event.set()
            state.turn_signal_event.set()
            return None

    # 旧CO機構 request_turn_co_declaration は撤去した。CO宣言は #昼 常設パネル
    # (VillagePanelView) の [CO] ボタン (declare_co) に一本化している。

    async def request_turn_interrupt(
        self, actor_id: int, turn_token: int
    ) -> tuple[Optional[str], int]:
        """村全体の日次枠から割り込みを1回、先着で確保する。"""
        async with self.action_lock:
            state = self.state
            limit = self.variant.turn_interrupts_per_day
            remaining = max(0, limit - state.turn_interrupts_used)
            if not self.turn_actions_open() or state.turn_slot_token != turn_token:
                return "この発言枠は既に終了しています。", remaining
            if state.turn_interrupt_active:
                return "割り込み発言中へさらに割り込むことはできません。", remaining
            actor = state.get_player(actor_id)
            if actor is None or not actor.alive:
                return "生存中の参加者だけが割り込めます。", remaining
            if actor_id == state.current_speaker_id:
                return "現在の話者は割り込みを使えません。", remaining
            if actor_id in state.disconnected_players:
                return "VCへ復帰してから割り込みを押してください。", remaining
            if state.turn_done_event.is_set():
                return "現在の話者は既に発言終了しています。", remaining
            if state.turn_interrupt_pending_id is not None or state.turn_interrupt_event.is_set():
                return "別の人の割り込みを受け付け済みです。", remaining
            if remaining <= 0:
                return "本日の割り込み回数を使い切っています。", 0

            old_used = state.turn_interrupts_used
            state.turn_interrupts_used += 1
            state.turn_interrupt_pending_id = actor_id
            state.record_event_seq += 1
            seq = state.record_event_seq
            try:
                await self._persist_room_state()
            except Exception as error:
                state.turn_interrupts_used = old_used
                state.turn_interrupt_pending_id = None
                state.record_event_seq -= 1
                log.exception(f"割り込み受付の保存に失敗: {error}")
                return "割り込みを保存できませんでした。もう一度押してください。", remaining

            speaker = state.get_player(state.current_speaker_id)
            try:
                await database.record_turn_event(
                    state.guild.id, state.room_id, state.game_run_id,
                    event_seq=seq, day_number=state.day_number,
                    event_type="割り込み",
                    actor_id=actor_id, actor_number=actor.number,
                    speaker_id=speaker.user_id if speaker is not None else None,
                    speaker_number=speaker.number if speaker is not None else None,
                )
            except Exception as error:
                log.exception(f"割り込みのDB記録に失敗 ({state.room_name}): {error}")

            state.turn_interrupt_event.set()
            state.turn_signal_event.set()
            return None, max(0, limit - state.turn_interrupts_used)

    async def force_next_turn(self, actor_id: int, turn_token: int) -> Optional[str]:
        """GMが現在の通常発言または割り込み発言を終了する。"""
        async with self.action_lock:
            state = self.state
            if actor_id != state.gm_id:
                return "GMのみ操作可能です。"
            if not self.turn_actions_open() or state.turn_slot_token != turn_token:
                return "対象の発言枠は既に終了しています。状況を更新してください。"
            if state.turn_interrupt_pending_id is not None or state.turn_interrupt_event.is_set():
                return "割り込みを受け付け済みのため、現在は次へ進めません。"
            if state.turn_done_event.is_set():
                return "発言終了を受け付け済みです。"
            state.turn_done_event.set()
            state.turn_signal_event.set()
            return None

    # ============================================================
    # 昼フェーズ: 投票
    # ============================================================

    async def _persist_vote_checkpoint(self, context: str) -> None:
        try:
            await self._persist_room_state()
        except Exception as error:
            await self._stop_for_durability_error(context, error)
            raise StateDurabilityError(f"{context}を保存できませんでした") from error

    async def _initialize_sequential_vote(self) -> bool:
        """当日の通常投票を開始する。発言順は空から始め、押した順に伸ばす。"""
        state = self.state
        if state.vote_day_generation == state.day_generation:
            return False
        state.phase = Phase.DAY_VOTE
        state.votes.clear()
        state.vote_order = []
        state.vote_day_generation = state.day_generation
        state.vote_slot_index = 0
        state.vote_slot_token = 0
        state.vote_slot_active = False
        state.vote_speech_finished = False
        state.vote_slot_forced_abstain = False
        state.vote_speech_window_open = False
        state.vote_closed = False
        state.vote_requeue_ids.clear()
        state.vote_panel_message_id = None
        state.vote_remaining_seconds = 0.0
        state.current_speaker_id = None
        state.speech_done_event.clear()
        state.vote_choice_event.clear()
        state.vote_queue_event.clear()
        await self._persist_vote_checkpoint("通常投票の開始")
        return True

    def _vote_queue_waiting(self) -> list[Player]:
        """まだ「投票参加」を押していない生存者 (押すのを待っている人)。"""
        queued = set(self.state.vote_order)
        return [
            player for player in self.state.alive_players()
            if player.user_id not in queued
        ]

    async def join_vote_queue(self, member: discord.Member) -> str:
        """「投票参加」ボタン: 押した順に投票発言の列へ並ぶ。

        発言中でも先に並べる。並んだ順はそのまま公開パネルへ出す。
        """
        async with self.action_lock:
            state = self.state
            if self._effective_phase() != Phase.DAY_VOTE or state.vote_closed:
                return "⏳ 今は投票を受け付けていません。"
            player = state.get_player(member.id)
            if player is None or not player.alive:
                return "投票権がありません。"
            if member.id in state.vote_order:
                if member.id in state.votes:
                    return "投票済みです。"
                # 枠を使い切っている場合は「並んでいる」と誤解させない。
                # 未投票で完了済みならGMが明示的に棄権にした枠だけ。
                if state.vote_order.index(member.id) < state.vote_slot_index:
                    return "この日の投票は終了しています。"
                return (
                    f"すでに{state.vote_order.index(member.id) - state.vote_slot_index + 1}"
                    "番目に並んでいます。"
                )
            state.vote_order.append(member.id)
            try:
                await self._persist_room_state()
            except Exception as error:
                state.vote_order.pop()
                log.exception(f"投票順の保存に失敗: {error}")
                return "❌ 保存できませんでした。もう一度押してください。"
            # 表示は「今から何番目に話すか」。既に終わった枠は数えない。
            position = len(state.vote_order) - state.vote_slot_index
            state.vote_queue_event.set()
            # 発言中はカウントダウンの毎秒更新が列を拾うので、待機中だけ
            # ここで公開パネルを書き換える (同じ内容の二重編集を避ける)。
            if not state.vote_slot_active:
                await self._refresh_vote_waiting_panel()
        return f"✅ {position}番目に並びました。"

    def _vote_panel_reference(self) -> Optional[discord.Message]:
        state = self.state
        channel = self._ui_cleanup_channel()
        get_partial_message = getattr(channel, "get_partial_message", None)
        if state.vote_panel_message_id is None or not callable(get_partial_message):
            return None
        return get_partial_message(state.vote_panel_message_id)

    async def _disable_recovered_vote_panel(self) -> None:
        """通常投票の公開パネルから、終了後に操作ボタンを外す。"""
        panel = self._vote_panel_reference()
        if panel is None:
            return
        try:
            await self._discord_api_call(panel.edit, view=None)
        except discord.NotFound:
            self.state.vote_panel_message_id = None
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning(
                "投票パネルの無効化に失敗 (%s): %s",
                self.state.room_name,
                error,
            )
        else:
            self.state.vote_panel_message_id = None

    def _sequential_vote_detail(self) -> str:
        """公開済みの票を「A → C」で並べる (棄権もその場に残す)。"""
        state = self.state
        lines: list[str] = []
        visible_count = state.vote_slot_index
        if (
            state.vote_slot_active
            and (
                state.current_speaker_id in state.votes
                or state.vote_slot_forced_abstain
            )
            and state.vote_slot_index < len(state.vote_order)
        ):
            visible_count += 1
        for voter_id in state.vote_order[:visible_count]:
            voter = state.get_player(voter_id)
            if voter is None:
                continue
            target = state.get_player(state.votes.get(voter_id))
            lines.append(
                f"{voter.display_name} → "
                + (target.display_name if target is not None else "棄権")
            )
        return "\n".join(lines) if lines else "まだ投票はありません。"

    def _vote_queue_line(self) -> str:
        """発言待ちの列 (現在の発言者より後ろ)。"""
        state = self.state
        start = state.vote_slot_index + (1 if state.vote_slot_active else 0)
        names: list[str] = []
        for voter_id in state.vote_order[start:]:
            voter = state.get_player(voter_id)
            if voter is not None and voter.alive:
                names.append(voter.display_name)
        return " → ".join(names) if names else "なし"

    def _sequential_vote_content(self, speaker: Player, remaining: float) -> str:
        return (
            f"🗳️ **{speaker.display_name}** の投票発言\n"
            + self._timer_line(remaining)
            + "\n投票先は発言終了後に選びます。"
            + f"\n📋 待機列: {self._vote_queue_line()}"
            + f"\n\n**ここまでの投票**\n{self._sequential_vote_detail()}"
        )

    def _sequential_vote_choice_content(self, voter: Player) -> str:
        return (
            f"🗳️ **{voter.display_name}** の発言は終了しました。\n"
            "時間制限はありません。投票先の名前を押して確定してください。"
            + f"\n📋 待機列: {self._vote_queue_line()}"
            + f"\n\n**ここまでの投票**\n{self._sequential_vote_detail()}"
        )

    def _sequential_vote_committed_content(self, voter: Player) -> str:
        return (
            f"✅ **{voter.display_name}** の投票が確定しました。"
            + f"\n📋 待機列: {self._vote_queue_line()}"
            + f"\n\n**ここまでの投票**\n{self._sequential_vote_detail()}"
        )

    def _sequential_vote_abstain_content(self, voter: Player) -> str:
        return (
            f"⏭️ **{voter.display_name}** はGMの操作で棄権となりました。"
            + f"\n📋 待機列: {self._vote_queue_line()}"
            + f"\n\n**ここまでの投票**\n{self._sequential_vote_detail()}"
        )

    def _vote_waiting_content(self, waiting: list[Player]) -> str:
        """列が空のときの公開パネル。押した人から順に発言する。"""
        return (
            f"🗳️ **投票待ち** — 押した順に{VOTE_SPEECH_TIME}秒発言し、その後に投票先を選びます\n"
            f"未投票 **{len(waiting)}人**: {'、'.join(p.display_name for p in waiting)}\n"
            f"\n**ここまでの投票**\n{self._sequential_vote_detail()}"
        )

    async def _replace_sequential_vote_panel(
        self,
        message: Optional[discord.Message],
        content: str,
        view: Optional[discord.ui.View],
    ) -> Optional[discord.Message]:
        if message is not None:
            try:
                await self._discord_api_call(message.edit, content=content, view=view)
                return message
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                log.warning(f"投票パネル更新失敗 ({self.state.room_name}): {error}")
        message = await self._safe_village_send(content, view=view)
        if message is None:
            return None
        message_id = getattr(message, "id", None)
        if message_id is not None and message_id != self.state.vote_panel_message_id:
            self.state.vote_panel_message_id = int(message_id)
            await self._persist_vote_checkpoint("通常投票パネルID")
        return message

    async def _refresh_sequential_vote_panel(self) -> bool:
        """票の保存直後、同じ公開パネルへ投票者と投票先を反映する。"""
        state = self.state
        speaker = state.get_player(state.current_speaker_id)
        panel = self._vote_panel_reference()
        if speaker is None or not state.vote_slot_active:
            return False
        panel = await self._replace_sequential_vote_panel(
            panel,
            self._sequential_vote_committed_content(speaker),
            None,
        )
        if panel is None:
            error = StateDurabilityError(
                "確定票を公開パネルへ反映できませんでした",
                state_committed=True,
            )
            await self._stop_for_durability_error("通常投票の公開", error)
            return False
        return True

    async def _refresh_vote_waiting_panel(self) -> None:
        """待機中に列が伸びたことだけを公開パネルへ反映する。"""
        view = self._vote_queue_view
        panel = self._vote_panel_reference()
        if view is None or panel is None:
            return
        await self._replace_sequential_vote_panel(
            panel, self._vote_waiting_content(self._vote_queue_waiting()), view
        )

    async def _wait_for_vote_queue(
        self, panel: Optional[discord.Message], view: discord.ui.View
    ) -> Optional[discord.Message]:
        """列が空の間、全員ミュートのまま次に押す人を待つ。

        締切時間は設けない。誰も押さずに進行が止まったときだけ、GMが
        「スキップ」で締め切って残りを棄権にできる。
        """
        state = self.state
        state.vote_queue_event.clear()
        if state.vote_slot_index < len(state.vote_order) or state.vote_closed:
            return panel
        panel = await self._replace_sequential_vote_panel(
            panel, self._vote_waiting_content(self._vote_queue_waiting()), view
        )
        if panel is None:
            error = StateDurabilityError("投票待ちパネルを掲示できませんでした")
            await self._stop_for_durability_error("投票待ちパネルの掲示", error)
            raise error
        self._vote_queue_view = view
        try:
            await state.vote_queue_event.wait()
        finally:
            self._vote_queue_view = None
        return panel

    def _requeue_lost_vote_slot(self, voter_id: int) -> bool:
        """票を失った人へ、順番を崩さず最後にもう一度発言枠を積む。

        順序の重複はsnapshot検証と公開表示を壊すので、完了済み位置から
        一度取り除いてcursorを補正してから末尾へ移す。
        """
        state = self.state
        try:
            completed_index = state.vote_order.index(voter_id, 0, state.vote_slot_index)
        except ValueError:
            return False
        del state.vote_order[completed_index]
        if completed_index < state.vote_slot_index:
            state.vote_slot_index -= 1
        state.vote_order.append(voter_id)
        return True

    async def _advance_vote_cursor(self) -> None:
        state = self.state
        state.vote_slot_active = False
        state.vote_speech_finished = False
        state.vote_slot_forced_abstain = False
        state.vote_speech_window_open = False
        state.current_speaker_id = None
        state.vote_remaining_seconds = 0.0
        state.speech_done_event.clear()
        state.vote_choice_event.clear()
        state.vote_slot_index += 1
        # 同じ日の一つ前の候補・発言終了・GMボタンも即座無効にする。
        state.vote_slot_token += 1
        await self._persist_vote_checkpoint("通常投票発言枠の完了")

    async def finish_sequential_vote_speech(
        self, actor_id: int, vote_slot_token: int
    ) -> str:
        """現在者本人の「発言終了」をdurableに受け付ける。"""
        async with self.action_lock:
            state = self.state
            if state.paused:
                return "⏸️ 一時停止中は操作できません。"
            if (
                self._effective_phase() != Phase.DAY_VOTE
                or not self.uses_sequential_vote()
                or not state.vote_slot_active
                or state.current_speaker_id != actor_id
                or state.vote_slot_token != vote_slot_token
                or state.vote_speech_finished
            ):
                return "⏳ この投票発言は終了しています。"

            old_window = state.vote_speech_window_open
            state.vote_speech_finished = True
            state.vote_speech_window_open = False
            try:
                await self._persist_room_state()
            except Exception as error:
                state.vote_speech_finished = False
                state.vote_speech_window_open = old_window
                await self._stop_for_durability_error("投票発言終了の保存", error)
                return "❌ 発言終了を保存できないため安全停止しました。"
            state.speech_done_event.set()
            return "✅ 発言を終了しました。続けて投票先を選んでください。"

    async def _day_vote(self) -> Optional[int]:
        if not self.uses_sequential_vote():
            return await self._day_vote_simultaneous()
        return await self._day_vote_sequential()

    async def _day_vote_simultaneous(self) -> Optional[int]:
        """ターン制の通常投票。規定の発言を終えてから全員が一斉に投票する。"""
        state = self.state
        await state.pause_event.wait()
        state.phase = Phase.DAY_VOTE
        # 新規開始時だけ当日へ進める。復元時は同じ世代なので、保存済みの票を
        # 消さずに再開する。DAY_VOTE snapshotは世代一致を必須にしているため、
        # persistより先に更新しないと投票中の再起動で復元できなくなる。
        if state.vote_day_generation != state.day_generation:
            state.votes.clear()
            state.vote_day_generation = state.day_generation
            state.vote_closed = False
        state.vote_complete_event.clear()
        await self._persist_room_state()

        alive = state.alive_players()
        view = VoteView(self, candidates=by_number(alive), voters=alive)
        alive_voter_ids = {p.user_id for p in alive}
        if state.vote_closed or (
            alive_voter_ids and alive_voter_ids <= state.votes.keys()
        ):
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
            await self._record_vote_abstentions(not_voted, "本投票", 0)

        # 翌日の投票フェーズで古いボタンが反応しないよう停止する
        view.stop()
        return await self._resolve_day_vote()

    async def _day_vote_sequential(self) -> Optional[int]:
        state = self.state
        await state.pause_event.wait()
        initialized = await self._initialize_sequential_vote()
        # 投票開始のSEは鳴らさない。直前の議論終了SEと数秒差で連続し、
        # 合図として区別できないため (議論終了SEに一本化)
        if initialized:
            await self._safe_village_send(
                f"🗳️ **投票**「投票参加」を押した順に、1人{VOTE_SPEECH_TIME}秒発言し、"
                "発言終了後に投票先を確定します。"
            )
        await self._repost_gm_panel()

        panel = self._vote_panel_reference()
        queue_view = VoteQueueView(self)
        # 直前の確定票を公開したあとに投票開示SEを鳴らした場合、その約2秒を
        # 次の話者への切替猶予として使う。次枠で開始SEまで重ねると二重SE・
        # 約4秒待ちになるため、列が連続している1枠だけ開始SEを省略する。
        next_speaker_already_cued = False
        while not state.vote_closed:
            await state.pause_event.wait()
            if state.vote_slot_index >= len(state.vote_order):
                if not self._vote_queue_waiting():
                    break
                # 列が空の待機を挟んだ後は、前の確定SEから時間が空いている。
                # 新しく押した人の開始SEを通常どおり鳴らす。
                next_speaker_already_cued = False
                panel = await self._wait_for_vote_queue(panel, queue_view)
                continue
            voter_id = state.vote_order[state.vote_slot_index]
            voter = state.get_player(voter_id)
            if voter is None or not voter.alive:
                await self._advance_vote_cursor()
                continue

            if voter.user_id in state.votes and not state.vote_slot_active:
                # cursor保存直前の旧snapshot等。確定票を消さず二重発言もしない。
                await self._advance_vote_cursor()
                continue

            resuming_slot = state.vote_slot_active
            if resuming_slot and state.current_speaker_id != voter.user_id:
                error = StateDurabilityError("投票cursorと復元中の現在者が一致しません")
                await self._stop_for_durability_error("通常投票枠の復元", error)
                raise error

            state.vote_slot_token += 1
            if not resuming_slot:
                state.vote_slot_active = True
                state.current_speaker_id = voter.user_id
                state.vote_speech_finished = False
                state.vote_slot_forced_abstain = False
            state.vote_remaining_seconds = float(VOTE_SPEECH_TIME)
            state.vote_speech_window_open = False
            state.speech_done_event.clear()
            state.vote_choice_event.clear()
            await self._persist_vote_checkpoint(
                "通常投票発言枠の再開"
                if resuming_slot else "通常投票発言枠の開始"
            )
            slot_token = state.vote_slot_token

            if not state.vote_speech_finished:
                speech_view = SequentialVoteSpeechView(self, voter.user_id)

                def vote_content(remaining: float, voter=voter) -> str:
                    state.vote_remaining_seconds = remaining
                    return self._sequential_vote_content(voter, remaining)

                panel = await self._replace_sequential_vote_panel(
                    panel, vote_content(VOTE_SPEECH_TIME), speech_view
                )
                if panel is None:
                    error = StateDurabilityError("通常投票パネルを掲示できませんでした")
                    await self._stop_for_durability_error("通常投票パネルの掲示", error)
                    raise error

                if next_speaker_already_cued:
                    next_speaker_already_cued = False
                else:
                    await self._play_transition_se("speech")
                await state.pause_event.wait()
                if (
                    state.vote_slot_active
                    and state.vote_slot_token == slot_token
                    and state.current_speaker_id == voter.user_id
                    and voter.alive
                    and not state.vote_speech_finished
                ):
                    state.vote_speech_window_open = True
                    await self._grant_speaker(voter.member)
                    await self._pausable_countdown(
                        panel, vote_content, VOTE_SPEECH_TIME, state.speech_done_event
                    )

                # 30秒経過でも棄権にしない。発言終了だけを先にdurableにする。
                async with self.action_lock:
                    if (
                        state.vote_slot_active
                        and state.vote_slot_token == slot_token
                        and state.current_speaker_id == voter.user_id
                        and not state.vote_speech_finished
                    ):
                        state.vote_speech_finished = True
                        state.vote_speech_window_open = False
                        await self._persist_vote_checkpoint("通常投票発言の終了")
                    else:
                        state.vote_speech_window_open = False
                speech_view.stop()
                await self._clear_speaker()
                if voter.alive and state.vote_slot_active:
                    await self._play_transition_se("speech_end")
            else:
                # 投票先待ちの復元。再度30秒を開けずミュートへ収束させる。
                await self._clear_speaker()

            if state.paused and state.recovered_from_restart:
                raise StateDurabilityError("通常投票は安全停止中です")

            if voter.alive and not state.vote_slot_forced_abstain and voter.user_id not in state.votes:
                choice_view = VoteView(
                    self,
                    candidates=by_number(
                        player
                        for player in state.alive_players()
                        if player.user_id != voter.user_id
                    ),
                    voters=[voter],
                )
                panel = await self._replace_sequential_vote_panel(
                    panel, self._sequential_vote_choice_content(voter), choice_view
                )
                if panel is None:
                    choice_view.stop()
                    error = StateDurabilityError("通常投票先パネルを掲示できませんでした")
                    await self._stop_for_durability_error("通常投票先パネルの掲示", error)
                    raise error
                while (
                    voter.alive
                    and voter.user_id not in state.votes
                    and not state.vote_slot_forced_abstain
                ):
                    await self._pausable_wait_forever(state.vote_choice_event)
                    if state.paused and state.recovered_from_restart:
                        choice_view.stop()
                        raise StateDurabilityError("通常投票は安全停止中です")
                    if (
                        voter.alive
                        and voter.user_id not in state.votes
                        and not state.vote_slot_forced_abstain
                    ):
                        state.vote_choice_event.clear()
                choice_view.stop()
            elif voter.user_id in state.votes:
                # 確定票保存後の再起動は公開を復旧してからcursorを進める。
                if not await self._refresh_sequential_vote_panel():
                    raise StateDurabilityError(
                        "確定票を公開できず安全停止しました",
                        state_committed=True,
                    )
            elif state.vote_slot_forced_abstain:
                panel = await self._replace_sequential_vote_panel(
                    panel, self._sequential_vote_abstain_content(voter), None
                )
                if panel is None:
                    error = StateDurabilityError("GM棄権を公開できませんでした")
                    await self._stop_for_durability_error("GM棄権の公開", error)
                    raise error

            # 確定票（またはGM明示棄権）を公開した直後にSEを鳴らし、約2秒後に
            # 次の人へ進む。列が続いていれば、このSEを次枠の開始合図と兼用する。
            # 最終票は直後の集計開示SEに任せ、同じ合図を連続させない。
            slot_completed = (
                voter.user_id in state.votes
                or state.vote_slot_forced_abstain
            )
            future_queued = any(
                (candidate := state.get_player(queued_id)) is not None
                and candidate.alive
                and queued_id not in state.votes
                for queued_id in state.vote_order[state.vote_slot_index + 1:]
            )
            unqueued_remain = bool(self._vote_queue_waiting())
            if slot_completed and (future_queued or unqueued_remain):
                await self._play_transition_se("reveal")
                next_speaker_already_cued = future_queued

            await self._advance_vote_cursor()
            # 自分の枠の中で投票先が除外された場合だけ、末尾で投票し直す。
            # 除外側が置いた印をここで回収し、末尾へ枠を積み直す。
            if voter.user_id in state.vote_requeue_ids:
                state.vote_requeue_ids.discard(voter.user_id)
                requeued = (
                    voter.alive
                    and voter.user_id not in state.votes
                    and self._requeue_lost_vote_slot(voter.user_id)
                )
                if requeued:
                    await self._safe_village_send(
                        f"↩️ **{voter.display_name}** の投票先が外れたため、"
                        "最後にもう一度投票発言を行います。"
                    )
                await self._persist_vote_checkpoint("失効した票の再投票枠")

        state.vote_slot_active = False
        state.vote_speech_finished = False
        state.vote_slot_forced_abstain = False
        state.vote_speech_window_open = False
        state.current_speaker_id = None
        queue_view.stop()
        if panel is not None:
            await self._replace_sequential_vote_panel(
                panel,
                "✅ **投票終了**\n\n**投票内訳**\n" + self._sequential_vote_detail(),
                None,
            )

        return await self._resolve_day_vote()

    async def _resolve_day_vote(self) -> Optional[int]:
        """集計〜決戦。投票の集め方 (一斉/投票発言) によらず共通。"""
        state = self.state
        # 投票終了の合図が終わる前に結果を表示しない。0票でも終了合図は鳴らす。
        await self._play_transition_se("reveal")
        if not state.votes:
            await self._safe_village_send("⚠️ 投票が1票もなかったため、処刑なしとなります。")
            return None

        # 結果表示
        embed = build_vote_result_embed(state.votes, state.players)
        await self._safe_village_send(embed=embed)

        # 集計
        tally = self._tally_votes(state.votes)
        max_votes = max(tally.values())
        top = [pid for pid, cnt in tally.items() if cnt == max_votes]

        if len(top) == 1:
            self._record_decisive_execution(top[0])
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

    def _record_decisive_execution(self, target_id: int) -> None:
        """投票で決まった処刑と、その対象へ入れた人を控える (プレイボーナス用)。

        ランダム処刑 (0票・再同票) では呼ばない。処刑を確定させた最終ラウンドの
        票だけを見るので、決戦があった場合は決戦の票だけが残る。
        """
        state = self.state
        voters = sorted(
            voter_id for voter_id, voted_for in state.votes.items()
            if voted_for == target_id
        )
        if not voters:
            return
        state.decisive_executions.append({
            "day": state.day_number,
            "target": int(target_id),
            "voters": voters,
        })

    async def _record_vote_abstentions(
        self, voters: list[Player], vote_kind: str, round_index: int,
    ) -> bool:
        """投票タイムアウトまたはGM強制の棄権を record_vote_event へ1行ずつ追記する
        (仕様§2-4)。target_id=NULL の行として残す。失敗しても進行は止めない。
        """
        if not voters:
            try:
                # vote_closed等、呼び出し側が同じcheckpointに載せた状態を保存する。
                await self._persist_room_state()
            except Exception as error:
                log.exception(f"棄権状態の保存に失敗 ({self.state.room_name}): {error}")
                return False
            return True
        state = self.state
        entries: list[tuple[Player, int]] = []
        for voter in voters:
            state.record_event_seq += 1
            entries.append((voter, state.record_event_seq))
        try:
            await self._persist_room_state()
        except Exception as error:
            state.record_event_seq -= len(entries)
            log.exception(f"棄権ログの保存に失敗 ({state.room_name}): {error}")
            return False
        for voter, seq in entries:
            try:
                await database.record_vote_event(
                    state.guild.id, state.room_id, state.game_run_id,
                    event_seq=seq, day_number=state.day_number,
                    vote_kind=vote_kind, round_index=round_index,
                    voter_id=voter.user_id, voter_number=voter.number,
                    target_id=None, target_number=None,
                )
            except Exception as error:
                log.exception(f"棄権ログのDB記録に失敗 ({state.room_name}): {error}")
        return True

    def _tally_votes(self, votes: dict) -> dict[int, int]:
        tally: dict[int, int] = {}
        for target_id in votes.values():
            tally[target_id] = tally.get(target_id, 0) + 1
        return tally

    async def _remember_speech_panel(self, message) -> None:
        """弁明・遺言パネルのIDを、再起動後の見た目の無効化用に保存する。"""
        message_id = getattr(message, "id", None)
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
        ):
            return
        if self.state.speech_panel_message_id == message_id:
            return
        self.state.speech_panel_message_id = message_id
        try:
            await self._persist_room_state()
        except Exception as error:
            # UIの後始末用IDであり、弁明・遺言そのものの進行checkpointは
            # 既に別途保存済み。ここだけの失敗でゲームを止めない。
            log.warning("弁明/遺言パネルIDの保存に失敗 (%s): %s", self.state.room_name, error)

    async def _disable_recovered_speech_panel(self) -> bool:
        """再起動前の弁明・遺言終了ボタンを表示から外す。"""
        state = self.state
        message_id = state.speech_panel_message_id
        channel = self._ui_cleanup_channel()
        if message_id is None:
            return True
        if channel is None:
            return False
        get_partial = getattr(channel, "get_partial_message", None)
        if not callable(get_partial):
            return False
        try:
            await self._discord_api_call(get_partial(message_id).edit, view=None)
        except discord.NotFound:
            state.speech_panel_message_id = None
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning("復元した弁明/遺言パネルの無効化に失敗 (%s): %s", state.room_name, error)
            return False
        else:
            state.speech_panel_message_id = None
        return True

    async def _require_recovered_speech_panel_closed(self) -> None:
        """古い発言終了ボタンを孤立させず、失敗時は安全停止する。"""
        if await self._disable_recovered_speech_panel():
            return
        error = StateDurabilityError("前回の弁明/遺言パネルを無効化できません")
        await self._stop_for_durability_error("発言終了パネルの復旧", error)
        raise error

    async def _close_speech_panel(self, message, view: SpeechDoneView) -> None:
        """終了パネルを表示上無効化してからViewを停止する。"""
        visually_closed = message is not None and all(
            item.disabled for item in view.children
        )
        if message is not None and not all(item.disabled for item in view.children):
            for item in view.children:
                item.disabled = True
            try:
                await self._discord_api_call(message.edit, view=view)
            except discord.NotFound:
                visually_closed = True
            except (discord.Forbidden, discord.HTTPException):
                # IDを残し、終了処理または再起動時のview=None編集で再試行する。
                visually_closed = False
            else:
                visually_closed = True
        view.stop()
        if visually_closed:
            self.state.speech_panel_message_id = None

    async def _runoff(
        self,
        candidate_ids: list[int],
        *,
        resume_vote: bool = False,
        resume_speech: bool = False,
    ) -> Optional[int]:
        state = self.state
        await state.pause_event.wait()
        # 再起動前に表示されていた弁明終了ボタンは、同じ話者を満額で
        # やり直すときにも旧タイマーへ紐付いたままなので先に外す。
        await self._require_recovered_speech_panel_closed()
        state.runoff_candidates = list(candidate_ids)
        if not resume_vote and not resume_speech:
            state.phase = Phase.DAY_RUNOFF_SPEECH
            # 弁明順も再起動後に同じ順で続けられるよう、抽選した順序を
            # runoff_candidates自体へ保存する。
            shuffled_ids = list(candidate_ids)
            random.shuffle(shuffled_ids)
            state.runoff_candidates = shuffled_ids
            state.runoff_speech_index = 0
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
            candidates = [
                player
                for player_id in state.runoff_candidates
                if (player := state.get_player(player_id)) is not None
            ]

        if not resume_vote and not resume_speech:
            await self._safe_village_send(
                "⚖️ **同票のため決戦投票！** 候補者が順番に弁明します。"
            )

        # 弁明フェーズ
        while not resume_vote and state.runoff_speech_index < len(candidates):
            candidate = candidates[state.runoff_speech_index]
            # 前の弁明中に退出/GM除外で死亡した候補はスキップ
            if not candidate.alive:
                state.runoff_speech_index += 1
                await self._persist_vote_checkpoint("決戦弁明枠の完了")
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
            await self._remember_speech_panel(msg)
            await self._repost_gm_panel()

            # 弁明終了待ち (タイムアウト or ボタン, pause対応)
            # タイマーは _pausable_countdown だけが管理する (View側timeoutは廃止済み)
            await self._pausable_countdown(
                msg, speech_content, RUNOFF_SPEECH_TIME, state.speech_done_event
            )

            await self._close_speech_panel(msg, view)
            state.current_speaker_id = None
            await self._clear_speaker()
            state.runoff_speech_index += 1
            await self._persist_vote_checkpoint("決戦弁明枠の完了")
            # 弁明ごとの「終了」告知は出さない。次の弁明パネルへ切り替われば
            # 交代は分かるので、1人につき1回の送信APIを丸ごと省ける。

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
        runoff_candidate_ids = {
            candidate.user_id for candidate in candidates if candidate.alive
        }
        eligible_voters = [
            player for player in alive
            if player.user_id not in runoff_candidate_ids
        ]
        view = RunoffVoteView(
            self, candidates=by_number(candidates), voters=eligible_voters
        )
        alive_voter_ids = {p.user_id for p in eligible_voters}
        if not alive_voter_ids or alive_voter_ids <= state.votes.keys():
            state.vote_complete_event.set()

        def runoff_vote_content(remaining: float) -> str:
            return (
                "🗳️ **決戦投票** — 候補者以外が一斉に投票します。\n"
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
            await self._record_vote_abstentions(not_voted, "決選投票", 1)

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

        await self._play_transition_se("reveal")
        embed = build_vote_result_embed(state.votes, state.players, title="決戦投票結果")
        await self._safe_village_send(embed=embed)

        tally = self._tally_votes(state.votes)
        max_votes = max(tally.values())
        top = [pid for pid, cnt in tally.items() if cnt == max_votes]

        if len(top) == 1:
            self._record_decisive_execution(top[0])
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
        # 復元時は以前の遺言終了ボタンを使い回せない。新しい満額タイマーを
        # 投稿する前に、保存済みメッセージのコンポーネントを外す。
        await self._require_recovered_speech_panel_closed()
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
        await self._remember_speech_panel(msg)
        await self._repost_gm_panel()

        # 遺言終了待ち (タイムアウト or ボタン, pause対応)
        await self._pausable_countdown(
            msg, will_content, LAST_WILL_TIME, state.speech_done_event
        )

        await self._close_speech_panel(msg, view)
        state.current_speaker_id = None
        await self._clear_speaker()

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
        await self._confirm_surrender_after_roster_change()

    def _can_archive_to_public_log(self) -> bool:
        """総合・ローカル固定卓・GM名前村の終了ログを公開ログへ退避する。"""
        return True

    @staticmethod
    def _public_log_overwrite(
        current: Optional[discord.PermissionOverwrite] = None,
    ) -> discord.PermissionOverwrite:
        """既存の無関係な設定を保ちつつ、公開ログの読み書き境界を固定する。"""
        overwrite = (
            discord.PermissionOverwrite()
            if current is None
            else discord.PermissionOverwrite.from_pair(*current.pair())
        )
        overwrite.update(
            view_channel=True,
            read_message_history=True,
            send_messages=False,
            add_reactions=False,
            manage_channels=False,
            manage_roles=False,
            manage_messages=False,
            manage_threads=False,
            manage_webhooks=False,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
            send_voice_messages=False,
            send_polls=False,
            use_application_commands=False,
            use_external_apps=False,
        )
        return overwrite

    @classmethod
    def _public_log_bot_overwrite(
        cls,
        current: Optional[discord.PermissionOverwrite] = None,
    ) -> discord.PermissionOverwrite:
        """通常メンバーを閉じたまま、Botの整理権限だけを確保する。"""
        overwrite = cls._public_log_overwrite(current)
        overwrite.manage_channels = True
        overwrite.manage_roles = True
        return overwrite

    def _public_log_overwrites(
        self, channel: discord.abc.GuildChannel,
    ) -> dict[object, discord.PermissionOverwrite]:
        """既存対象を落とさず、公開ログ用の上書き一式を組み立てる。"""
        guild = self.state.guild
        if guild is None:
            raise ValueError("guildがありません")
        existing = dict(channel.overwrites)
        existing.setdefault(guild.default_role, discord.PermissionOverwrite())
        bot_member = getattr(guild, "me", None)
        if bot_member is not None:
            existing.setdefault(bot_member, discord.PermissionOverwrite())
        bot_id = getattr(bot_member, "id", None)
        return {
            target: (
                self._public_log_bot_overwrite(current)
                if bot_id is not None and getattr(target, "id", None) == bot_id
                else self._public_log_overwrite(current)
            )
            for target, current in existing.items()
        }

    @staticmethod
    def _is_managed_log_channel_name(category_name: str, channel_name: str) -> bool:
        """同名の手動カテゴリや専用村をログとして誤採用しない。"""
        # 退避先は #昼 だけ。過去に作られた「ログ-霊界」はBotの管理対象外とし、
        # 権限同期もtrimも行わない (運営が手動で消す)。
        expected = {
            LOG_CATEGORY_VILLAGE: CH_VILLAGE,
        }.get(category_name)
        sequence, separator, suffix = channel_name.partition("-")
        return bool(expected and separator and sequence.isdecimal() and suffix == expected)

    async def _sync_log_category_permissions(
        self, category: discord.CategoryChannel,
    ) -> None:
        """既存allow/denyを含め、通常メンバーを「閲覧可・書込不可」へ揃える。"""
        guild = self.state.guild
        if guild is None:
            return
        unexpected = next(
            (
                channel for channel in category.channels
                if not self._is_managed_log_channel_name(category.name, channel.name)
            ),
            None,
        )
        if unexpected is not None:
            raise ValueError(
                f"管理外チャンネルを含む同名カテゴリです: #{unexpected.name}"
            )

        desired = self._public_log_overwrites(category)
        if dict(category.overwrites) != desired:
            await self._paced_discord_api_call(
                category.edit,
                overwrites=desired,
                reason="人狼: 公開ログカテゴリ権限更新",
            )
        # カテゴリと権限非同期だった既存ログも同じ境界へ戻す。上書き一式を
        # 直接PATCHするため、category.edit後のGatewayキャッシュ反映を待たない。
        for channel in category.channels:
            if dict(channel.overwrites) == desired:
                continue
            await self._paced_discord_api_call(
                channel.edit,
                overwrites=desired,
                reason="人狼: 既存公開ログ権限更新",
            )

    async def _ensure_log_category(self, name: str) -> Optional[discord.CategoryChannel]:
        """通常メンバーが読めて書き込めないログカテゴリを用意する。"""
        guild = self.state.guild
        if guild is None:
            return None
        category = discord.utils.get(guild.categories, name=name)
        if category is not None:
            try:
                await self._sync_log_category_permissions(category)
            except (discord.Forbidden, discord.HTTPException, TypeError, ValueError) as e:
                # 誤った権限のカテゴリへ同期して公開するより、従来の削除へ倒す。
                log.warning(f"ログカテゴリ権限更新失敗 ({name}): {e}")
                return None
            return category
        overwrites = {
            guild.default_role: self._public_log_overwrite(),
        }
        bot_member = getattr(guild, "me", None)
        if bot_member is not None:
            overwrites[bot_member] = self._public_log_bot_overwrite()
        try:
            return await self._discord_api_call(
                guild.create_category, name, overwrites=overwrites
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"ログカテゴリ作成失敗 ({name}): {e}")
            return None

    async def _trim_log_category(self, category: discord.CategoryChannel) -> None:
        """上限に達したら古い順にまとめて減らす。

        Discordの上限はカテゴリあたり50チャンネル。埋まってから1つずつ
        消すと毎試合その処理が走るので、まとめて LOG_CATEGORY_TRIM_TO まで
        落とす。作成が古い順 = チャンネルIDの昇順 (Snowflakeは時刻を含む)。
        """
        channels = sorted(category.channels, key=lambda ch: ch.id)
        if len(channels) < LOG_CATEGORY_LIMIT:
            return
        for ch in channels[: len(channels) - LOG_CATEGORY_TRIM_TO]:
            try:
                await self._paced_discord_api_call(ch.delete)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"古いログチャンネルの削除失敗 (#{ch.name}): {e}")

    async def _archive_game_channel(
        self,
        channel: discord.TextChannel,
        category_name: str,
        seq: int,
    ) -> bool:
        """終了した卓チャンネルをログカテゴリへ移す。成功したらTrue。

        名前は「04-昼」のように試合番号を先頭へ置く。Discordはカテゴリ内を
        名前順に並べるため、番号が前にあると自然に試合順で並ぶ。
        権限はカテゴリと同じ読み取り専用上書きを直接指定する。
        """
        category = await self._ensure_log_category(category_name)
        if category is None:
            return False
        await self._trim_log_category(category)
        try:
            await self._paced_discord_api_call(
                channel.edit,
                name=f"{seq:02d}-{channel.name}",
                category=category,
                overwrites=self._public_log_overwrites(category),
                reason="人狼: 終了した卓のログを保管",
            )
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"ログカテゴリへの移動失敗 (#{channel.name}): {e}")
            return False

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
        state.turn_window_open = False
        state.turn_signal_event.set()
        state.pause_event.clear()
        try:
            await self._persist_room_state()
        except Exception:
            log.exception(f"{context}失敗後の停止状態も保存できませんでした")
        log.exception(f"{context}に失敗: {error}")
        await self._safe_village_send(
            f"⚠️ **{context}に失敗したため安全停止しました。**\n"
            "状態を捨てずに停止しています。原因を解消後、GMが再開してください。"
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
        manual_mute_gm = self._uses_manual_gm_mute(player.user_id)
        owned_gm_mute = bool(
            manual_mute_gm
            and (
                player.user_id in state.bot_muted_ids
                or self._has_own_mute_marker(player.member)
            )
        )
        release_owned_gm_mute = bool(
            owned_gm_mute
            and vs is not None
            and vs.channel is not None
        )
        will_mute = (
            not manual_mute_gm
            and vs is not None and vs.channel is not None
            and vc is not None and vs.channel.id == vc.id
            and not vs.mute
        )
        marker: Optional[discord.Role] = None
        if will_mute:
            edit_kwargs["mute"] = True
            if state.guild is not None:
                marker = await self._enable_mute_markers()
        elif release_owned_gm_mute:
            # 旧snapshotから復元したBot所有muteだけは、手動運用へ切り替える
            # このPATCHで解除する。マーカーのない手動muteには触れない。
            if state.guild is not None:
                marker = await self._enable_mute_markers()
            edit_kwargs["mute"] = False
        alive_role = (
            discord.utils.get(state.guild.roles, name=self._alive_role_name())
            if state.guild is not None else None
        )
        current_roles = member_roles_for_edit(player.member)
        desired_roles = [
            role for role in current_roles
            if (alive_role is None or getattr(role, "id", None) != alive_role.id)
            and not (
                release_owned_gm_mute
                and marker is not None
                and getattr(role, "id", None) == marker.id
            )
        ]
        if will_mute and marker is not None and not any(
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
            if edit_kwargs.get("mute") is True:
                state.bot_muted_ids.add(player.user_id)
            elif release_owned_gm_mute:
                state.bot_muted_ids.discard(player.user_id)
                state.bot_mute_intent_ids.discard(player.user_id)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"死亡者ニックネーム更新失敗 ({player.display_name}): {e}")
        else:
            # 復元再適用時、nick+mute同一PATCH成功後のクラッシュで
            # 既にmute=Trueならedit_kwargsにmuteが入らない。期待nickが
            # 反映済みならBot所有と照合する。
            vs_after = getattr(player.member, "voice", None)
            if (
                not manual_mute_gm
                and vs_after is not None
                and vs_after.mute
                and state.mute_marker_enabled
                and self._has_own_mute_marker(player.member)
            ):
                state.bot_muted_ids.add(player.user_id)

        # チャンネル権限: 読み取り専用。死亡者が生存ロールを保持する
        # 取りこぼしがあっても、メンバー個別denyを最後の防壁にする。
        village_blocked = False
        if state.village_channel is not None:
            try:
                village_ow = state.village_channel.overwrites_for(player.member)
                village_ow.read_messages = True
                village_ow.send_messages = False
                await self._discord_api_call(
                    state.village_channel.set_permissions,
                    player.member,
                    overwrite=village_ow,
                    reason="人狼: 死亡者の昼書き込み禁止",
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
        # textは個別denyを必須、通常参加者のVCはserver muteまたは
        # 生存ロール剥奪を必須にする。GMの音声ミュートだけは手動運用なので、
        # その成否を死亡副作用の完了条件には含めない。
        voice_block_satisfied = (
            manual_mute_gm or mute_applied or alive_role_removed
        )
        # GM兼プレイヤーの音声は意図的に手動運用へ残す一方、参加者としての
        # 生存ロールは必ず外す。ここを成功扱いにすると死亡後も生存者用の
        # 接続・閲覧権限を持ち越すため、通常プレイヤー処理とは分けて確認する。
        participant_role_satisfied = (
            not manual_mute_gm or alive_role_removed
        )
        if (
            not village_blocked
            or not voice_block_satisfied
            or not participant_role_satisfied
        ):
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
        # 陣営に関係なく同じ条件で受付を締めるまで霊界を開けない。
        # 陣営で挙動を変えると死亡者の正体が漏れる。提出はDMではなく
        # #昼パネルの[人狼予想]から行う (DM送信ぶんのAPIを増やさず、
        # DM不達で提出機会そのものを失う事故も無くす)。
        held_for_guess = self._should_hold_spirit(method)
        if held_for_guess:
            death_event_id = str(effect.get("event_id") or "")
            self._hold_spirit_for_guess(player.user_id, death_event_id)
        else:
            await self._open_spirit_for(player.member)
            await self._safe_spirit_send(
                f"👻 **{player.display_name}** が霊界へやってきました。"
            )

        if method == "処刑":
            self._play_se("execution")
            await self._safe_village_send(
                f"⚰️ **{player.display_name}** が処刑されました。"
                + (f"\n{WOLF_GUESS_NOTICE}" if held_for_guess else "")
            )

            # 霊能結果を state へ記録する (仕様§2-3)。DMは廃止し、霊媒ボタンで
            # ephemeral表示する経路へ一本化した。霊能者の生存・在籍に関わらず
            # 必ず記録する (統計のため。存在しなくても記録自体は残す)。
            # このメソッドはoutbox経由でat-least-once再適用されるため、
            # 同じ死亡 (day + target_id) の記録が既にあれば追記しない
            # (再適用のたびに一覧が伸び続けるのを防ぐ)。
            judgement = "人狼" if player.is_wolf else "村人"
            already_recorded = any(
                isinstance(entry, dict)
                and entry.get("day") == state.day_number
                and entry.get("target_id") == player.user_id
                for entry in state.medium_results
            )
            if not already_recorded:
                state.medium_results.append({
                    "day": state.day_number,
                    "target_id": player.user_id,
                    "target_number": player.number,
                    "display_name": player.display_name,
                    "result": judgement,
                })
                state.record_event_seq += 1
                seq = state.record_event_seq
                medium = next(
                    (p for p in state.players.values() if p.role == Role.MEDIUM),
                    None,
                )
                try:
                    await database.record_night_action(
                        state.guild.id, state.room_id, state.game_run_id,
                        event_seq=seq, night_number=state.day_number,
                        actor_id=medium.user_id if medium else 0,
                        actor_number=medium.number if medium else 0,
                        actor_role="霊能者", action="霊能",
                        target_id=player.user_id, target_number=player.number,
                        result=judgement,
                    )
                except Exception as error:
                    log.exception(f"霊能結果のDB記録に失敗 ({state.room_name}): {error}")
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

        # 前の夜・異常終了経路で残ったViewを先に表示上も閉じる。
        # stopだけではDMに古いボタンが残り、再起動後はDiscordの汎用エラーに
        # なるため、現在プロセスのDMと保存済みIDの両方をbest effortで編集する。
        await self._disable_live_wolf_vote_dms()
        if resume_existing:
            await self._disable_persisted_dm_views(
                state.wolf_dm_message_ids, label="人狼襲撃",
            )
        self._stop_night_views()
        if not resume_existing:
            state.night_generation += 1
            state.wolf_target = None
            state.wolf_voters.clear()
            state.seer_target = None
            state.guard_target = None
            state.night_complete_event.clear()
            state.morning_ready_open = False
            state.morning_ready_ids.clear()
            state.morning_warned_ids.clear()
            state.morning_confirmed = False
            state.morning_ready_event.clear()
            state.night_resolved = False
        elif self._pending_guard_player() is not None:
            # 朝が確定済みでも、護衛先が無効なら解決せず同じ夜の
            # DM/朝パネルを再掲示する。
            await self._reopen_night_for_required_guard()
        elif state.morning_confirmed:
            state.morning_ready_event.set()
        await self._persist_room_state()

        # #昼チャンネル ロック & ミュート
        await self._lock_village()
        await self._mute_phase("夜フェーズに入ります。")
        # 朝待機まで進んだ夜を復元する場合、夜開始SEを再度鳴らさない。
        # この経路は夜時間をやり直さず、朝パネルだけを復旧する。
        if not (resume_existing and state.morning_ready_open):
            self._play_se("night")

        duration = state.get_night_time()
        state.night_duration = duration

        def night_content(remaining: float) -> str:
            return (
                f"🌙 **{state.day_number}日目 - 夜フェーズ**\n"
                + self._timer_line(remaining, "目安")
            )

        timer_msg = None
        if not state.morning_ready_open:
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
            wolf_view = WolfVoteView(self, non_wolves_alive)
            self.register_game_view(wolf_view, night=True)
            try:
                msg = await self._discord_api_call(
                    wolf.member.send,
                    self.build_wolf_dm_content(duration),
                    view=wolf_view,
                )
                state.wolf_dm_messages[wolf.user_id] = msg
                self._remember_dm_view_message(
                    state.wolf_dm_message_ids, wolf.user_id, msg,
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"人狼DM送信失敗: {wolf.member.display_name} ({e})")

        wolves_alive = state.alive_wolves()
        if wolves_alive:
            await asyncio.gather(*(send_wolf_dm(w) for w in wolves_alive))
            # 再起動後も古い襲撃UIを表示上閉じられるよう、DM送信直後に
            # メッセージIDを控える。保存失敗はUI後始末だけの劣化なので、
            # 夜の進行を止めず次の終了/復元で回収を試みる。
            if state.wolf_dm_message_ids:
                try:
                    await self._persist_room_state()
                except Exception as error:
                    log.warning("人狼襲撃DM操作IDの保存に失敗 (%s): %s", state.room_name, error)

        # 占い師・狩人のDMは廃止した (v0.51)。#昼常設パネルの[占い][狩人]
        # ボタン経由のephemeral UIへ一本化している (仕様§2-3)。夜が明けるまで
        # 何度でも押せ、確定済みならそのまま確定内容を再表示する。

        # 制限時間中のGMパネルでは「朝」を無効にする。安全条件を無視した
        # 早送りにはせず、朝待機が始まってから既存の強制夜明けを使う。
        await self.post_village_panel()
        await self._repost_gm_panel()

        if not state.morning_ready_open:
            # 全員の夜UIが届いてから中継窓と制限時間を同時に開始する。
            # 朝ボタンはこのcountdownが完了するまで作らない。
            state.wolf_relay_window_open = True
            try:
                await self._pausable_countdown(
                    timer_msg, night_content, duration
                )
            finally:
                state.wolf_relay_window_open = False

            # ボタン受付開始を先にdurableにする。投稿直後に落ちても復元時は
            # 夜時間をやり直さず、同じ0/Nパネルを再掲示できる。
            state.morning_ready_open = True
            await self._persist_room_state()

        # 夜時間終了後に初めて、0/生存人数から始まる朝パネルを掲示する。
        await self._post_morning_panel()
        await self._repost_gm_panel()
        if self._check_morning_ready():
            await self._persist_room_state()
            state.morning_ready_event.set()

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
        await self._disable_live_wolf_vote_dms()
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

        guard = self._pending_guard_player()
        if guard is not None:
            pending.append((guard, "護衛先"))

        return pending

    def _guard_target_is_valid(self, guard: Player) -> bool:
        """現在の護衛先が、今夜の有効な護衛先かを判定する。"""
        state = self.state
        target_id = state.guard_target
        if target_id is None or target_id == -1:
            return False
        target = state.get_player(target_id)
        return bool(
            target is not None
            and target.alive
            and target.user_id != guard.user_id
            and target.user_id != state.guard_previous
        )

    def _pending_guard_player(self) -> Optional[Player]:
        """有効な護衛を確定していない生存狩人を返す。

        狩人だけは護衛放棄不可で、朝宣言やGMの強制夜明けでも
        有効な ``guard_target`` が無いまま夜を解決してはならない。
        GM除外後に死亡した対象なども未確定として扱う。
        占い・襲撃の既存の未行動スキップとは意図的に分ける。
        """
        state = self.state
        guard = next(
            (p for p in state.players.values() if p.role == Role.GUARD and p.alive),
            None,
        )
        if guard is None or self._guard_target_is_valid(guard):
            return None
        return guard

    async def _reopen_night_for_required_guard(self) -> Optional[Player]:
        """未護衛の夜明け確定を取り消し、狩人の再選択を可能にする。

        GM除外などで護衛先が無効になった場合にも、夜を解決せず同じ夜のUIを
        再掲示するために使う。ほかの役職の未行動スキップには影響しない。
        """
        state = self.state
        guard = self._pending_guard_player()
        if guard is None:
            return None

        old_guard_target = state.guard_target
        old_ready_ids = set(state.morning_ready_ids)
        old_warned_ids = set(state.morning_warned_ids)
        old_confirmed = state.morning_confirmed
        old_morning_event = state.morning_ready_event.is_set()
        old_night_complete = state.night_complete_event.is_set()

        # 無効な値 (-1 / 自己 / 前夜 / 死亡済み) を残すと#昼パネルの[狩人]が
        # 「既に確定済み」と判断して再選択できない。必ず未選択へ戻す。
        state.guard_target = None
        state.morning_ready_ids.discard(guard.user_id)
        state.morning_warned_ids.discard(guard.user_id)
        state.morning_confirmed = False
        state.morning_ready_event.clear()
        state.night_complete_event.clear()
        try:
            await self._persist_room_state()
        except Exception as e:
            state.guard_target = old_guard_target
            state.morning_ready_ids = old_ready_ids
            state.morning_warned_ids = old_warned_ids
            state.morning_confirmed = old_confirmed
            if old_morning_event:
                state.morning_ready_event.set()
            else:
                state.morning_ready_event.clear()
            if old_night_complete:
                state.night_complete_event.set()
            else:
                state.night_complete_event.clear()
            await self._stop_for_durability_error("未護衛の夜明け確定解除", e)
            raise StateDurabilityError(
                "未護衛の夜明け確定を安全に解除できませんでした"
            ) from e

        log.warning(
            "未確定の護衛があるため夜明けを解除し、夜を再開します (%s)",
            state.room_name,
        )
        return guard

    async def _request_guard_reselection(self, guard: Player) -> None:
        """GM除外で護衛先が無効になった狩人へ、#昼パネルからの再選択を促す。

        以前はDMにGuardViewを直接送っていたが、役職行動は#昼パネルの
        [狩人]ボタンへ一本化したため (仕様§2-3)、再選択の入口もそちらに
        統一する。ここではプレーンテキストの通知のみDMで送り、Viewは
        付けない (人狼DM系のViewには一切触れない方針を守るため)。
        state.guard_target は呼び出し側で既に None へ戻し済みなので、
        [狩人] を押せばそのまま新しい対象候補が出る。
        """
        state = self.state
        if (
            not guard.alive
            or self._pending_guard_player() is not guard
            or not self.night_actions_open()
        ):
            return
        if not self.guard_targets(guard.user_id):
            await self.pause_game(
                "⚠️ 必須の夜行動を再選択できる対象がいないため、安全のため一時停止しました。\n"
                "GMはプレイヤー除外または強制終了を選んでください。"
            )
            return

        try:
            await self._discord_api_call(
                guard.member.send,
                "⚠️ **護衛先が除外されたため未確定です。**"
                " #昼 の [狩人] ボタンからもう一度選んでください。",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.error("狩人への再選択通知DM送信失敗 (%s): %s", guard.display_name, e)
            await self.pause_game(
                "⚠️ 必須の夜行動を再選択するDMを送信できないため、安全のため一時停止しました。\n"
                "GMは権限を確認し、続行不能ならプレイヤー除外または強制終了を選んでください。"
            )

    def _check_night_complete(self) -> None:
        """夜アクションが揃ったら完了イベントを立てる (未行動警告の抑制用)"""
        state = self.state
        if not self.night_actions_open():
            return
        if not self._pending_night_actions():
            state.night_complete_event.set()

    async def _wait_for_morning(self) -> None:
        """夜の制限時間が切れた後、生存者全員の「朝を迎える」宣言を待つ。

        時間切れでは自動的に朝にしない。ここへ入ってから初めてパネルを出し、
        0/生存人数から押下ごとに公開人数を更新する。宣言は取り消せない。
        全員が戻らない場合はGMがGMコントロールパネルの「朝」で進行できる。
        ただし生存狩人の護衛が未確定なら、護衛放棄を許さず朝へ進めない。
        """
        state = self.state
        if state.morning_ready_event.is_set():
            return

        # 通常は_night_phaseが先に保存する。復元/直接再開経路でも、受付開始を
        # パネル投稿より先にdurableにして同じ順序を守る。
        if not state.morning_ready_open:
            state.morning_ready_open = True
            await self._persist_room_state()

        # パネルを掲示できていないと誰も押せない。取りこぼしをここで補完する
        await self._post_morning_panel()

        # パネルを掲示できないなら、誰も夜を明けられない。通常は待ち続けても
        # 永久に明けないため自動で明けるが、未護衛の狩人だけは放棄を
        # 許せないため、安全停止してGMの判断を待つ。
        missing = self._morning_required_ids() - state.morning_ready_ids
        if missing and state.morning_panel_message is None:
            if self._pending_guard_player() is not None:
                log.error(
                    "朝パネルを掲示できず、必須の護衛も未確定です (%s): 安全停止します",
                    state.room_name,
                )
                await self.pause_game(
                    "⚠️ 「朝を迎える」パネルを掲示できないため、安全のため一時停止しました。\n"
                    "GMは権限を確認し、護衛確定後に再開するか、続行不能なら"
                    "プレイヤー除外または強制終了を選んでください。"
                )
                # pause中も夜のDM操作は有効。再開後も朝宣言を待ち直し、
                # 未護衛のまま _night_phase を抜けて解決へ進まない。
                await self._pausable_wait_forever(state.morning_ready_event)
                return
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
                if action == "護衛先":
                    text = (
                        "⚠️ **まだ護衛先を選んでいません。** "
                        "護衛放棄はできず、確定するまで朝を迎えられません。"
                        "#昼 の村パネル[狩人]から選んでください。"
                    )
                else:
                    text = (
                        f"⚠️ **まだ{action}を選んでいません。** "
                        "選ばないまま朝になると今夜の行動はなしになります。"
                    )
                try:
                    await self._discord_api_call(
                        player.member.send,
                        text,
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"未行動の警告DM送信失敗 ({player.display_name}): {e}")

            await asyncio.gather(*(warn(p, action) for p, action in pending))

        # 再掲示/警告送信の間に人数が変わっていても最新表示へ揃える。
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

    def _morning_panel_content(self) -> str:
        """夜時間終了後だけ掲示し、押下ごとに人数を更新する本文。"""
        ready, required = self._morning_ready_count()
        return (
            "🌅 **朝を迎える**（夜の行動を終えた人だけ押してください。取消不可）\n"
            f"現在 **{ready} / {required}人** — 生存者全員が押すと朝になります"
        )

    def _morning_feedback_text(self) -> str:
        """押した本人へ、冪等な宣言結果と現在人数を返す。"""
        ready, required = self._morning_ready_count()
        if required and ready >= required:
            return "🌅 **全員が「朝を迎える」を押しました。夜が明けます。**"
        return (
            "✅ **「朝を迎える」を宣言しました。**\n"
            f"現在 **{ready} / {required}人**。他の生存者を待っています。"
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
        if not state.morning_ready_open:
            return
        if state.morning_panel_message is not None:
            return
        # 再起動をまたいだ前回のパネルを先に消す (下の説明を参照)
        if not await self._delete_stale_morning_panel():
            error = StateDurabilityError("前回の朝パネルを削除できません")
            await self._stop_for_durability_error("朝パネルの復旧", error)
            raise error
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

    async def _delete_stale_morning_panel(self) -> bool:
        """再起動前に掲示した朝パネルを消す。

        Viewはプロセスをまたいで復元されない (ボタンに custom_id が無く
        永続View化もできない) ため、残ったパネルを押すとDiscordの
        「インタラクションに失敗しました」しか出ない。#昼 に新旧2枚が
        並ぶと誤タップの元なので、次の掲示より前に消す。

        get_partial_message ならメッセージを取得せず1回のAPIで消せる。
        """
        state = self.state
        message_id = state.morning_panel_message_id
        ch = self._ui_cleanup_channel()
        if message_id is None:
            return True
        if ch is None:
            return False
        get_partial = getattr(ch, "get_partial_message", None)
        if not callable(get_partial):
            return False
        try:
            await self._discord_api_call(get_partial(message_id).delete)
        except discord.NotFound:
            state.morning_panel_message_id = None
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"前回の朝パネル削除に失敗 ({state.room_name}): {e}")
            return False
        else:
            state.morning_panel_message_id = None
        return True

    async def _reveal_morning_count(self) -> None:
        """押下後、同じ公開パネルの宣言人数を最新値へ更新する。"""
        state = self.state
        msg = state.morning_panel_message
        if msg is None:
            return
        try:
            await self._discord_api_call(
                msg.edit, content=self._morning_panel_content()
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
        if view is None:
            await self._delete_stale_morning_panel()
            return
        # 先に表示を無効化してから stop する。逆順だと、編集が着地するまでの
        # 数百msに押した人へ汎用エラーが出る。
        visually_closed = msg is not None and all(
            item.disabled for item in view.children
        )
        if msg is not None and not all(item.disabled for item in view.children):
            for item in view.children:
                item.disabled = True
            try:
                await self._discord_api_call(msg.edit, view=view)
            except discord.NotFound:
                visually_closed = True
            except (discord.Forbidden, discord.HTTPException):
                visually_closed = False
            else:
                visually_closed = True
        view.stop()
        if visually_closed:
            state.morning_panel_message_id = None

    async def toggle_morning_ready(self, member: discord.Member) -> tuple[str, Optional[str]]:
        """「朝を迎える」を一方向・冪等に宣言する。

        Returns:
            (押した本人へephemeralで返す本文, エラー文字列)
            — 保存後、公開パネルの人数も押下ごとに更新する
        """
        state = self.state
        if state.morning_ready_event.is_set():
            # 既に夜明けが確定している (全員宣言済み / GMの強制)。
            # ここで取り消しを受け付けると表示だけ巻き戻って紛らわしい
            return "", "🌅 まもなく朝になります。"
        if not self.night_actions_open():
            return "", "⏳ 現在この操作はできません。"
        if not state.morning_ready_open:
            return "", "⏳ 夜の制限時間が終わるまでお待ちください。"
        player = state.get_player(member.id)
        if player is None or not player.alive:
            return "", "⏳ 生存中の参加者だけが押せます。"

        if member.id in state.morning_ready_ids:
            return "", "✅ 既に「朝を迎える」を宣言しています。取り消しはできません。"

        if (guard := self._pending_guard_player()) is not None and member.id == guard.user_id:
            return "", (
                "🛡️ **護衛先を確定するまで朝を迎えられません。**\n"
                "狩人は護衛放棄できません。村パネルの[狩人]から選んでください。"
            )

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
        # snapshot保存後に公開表示を更新する。編集失敗でも宣言自体は成立済みで、
        # 次の押下または復元再掲示で最新人数へ追いつく。
        await self._reveal_morning_count()
        return self._morning_feedback_text(), None

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
        if not state.morning_ready_open:
            return "", "⏳ 夜の制限時間が終わるまで朝には進めません。"
        if state.paused:
            # 停止中に朝にすると、再開直後に朝が流れて誰も追えない
            return "", "⏸️ 一時停止中です。先に「再開」を押してください。"
        if self._pending_guard_player() is not None:
            return "", (
                "⚠️ **必須の夜行動が未確定のため、朝を強制できません。**\n"
                "護衛先の確定を待つか、続行不能ならGMメニューで"
                "プレイヤー除外または強制終了を選んでください。"
            )
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
            "⏭️ **GMの操作で役職確認を締め切り、1日目へ進みます。**"
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
        if not state.morning_ready_open:
            return False
        if self._pending_guard_player() is not None:
            return False
        required = self._morning_required_ids()
        if not required or required <= state.morning_ready_ids:
            state.morning_confirmed = True
            return True
        return False

    # ============================================================
    # 役職確認の宣言 (初日はここが揃ってから1日目へ進む)
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
                "✅ **全員が役職を確認しました。1日目へ進みます。**\n"
                f"**{ready} / {len(required)}人**"
            )
        return (
            "📩 **役職を確認した**（DMの役職を見てから押してください。取消不可）\n"
            f"現在 **{ready} / {len(required)}人** — 全員が押すと1日目へ進みます"
        )

    async def _post_prep_panel(self) -> None:
        """役職確認タイムの宣言パネルを #昼 に掲示する。

        再起動後のViewは復元されないため、保存済みの古いパネルを先に削除する。
        """
        state = self.state
        if state.prep_panel_message is not None:
            return
        if not await self._delete_stale_prep_panel():
            error = StateDurabilityError("前回の役職確認パネルを削除できません")
            await self._stop_for_durability_error("役職確認パネルの復旧", error)
            raise error
        self._prep_view = PrepReadyView(self)
        state.prep_panel_message = await self._safe_village_send(
            self._prep_panel_content(), view=self._prep_view
        )
        if state.prep_panel_message is None:
            self._prep_view.stop()
            self._prep_view = None
            return
        state.prep_panel_message_id = getattr(state.prep_panel_message, "id", None)
        try:
            await self._persist_room_state()
        except Exception as error:
            # 役職確認の待機自体は生きている。次の投稿/終了で古いパネルを
            # 回収できるよう、IDはメモリ上に残して進行を継続する。
            log.warning("役職確認パネルIDの保存に失敗 (%s): %s", state.room_name, error)

    async def _delete_stale_prep_panel(self) -> bool:
        """再起動前の役職確認パネルを、再掲前または終了時に削除する。"""
        state = self.state
        message_id = state.prep_panel_message_id
        channel = self._ui_cleanup_channel()
        if message_id is None:
            return True
        if channel is None:
            return False
        get_partial = getattr(channel, "get_partial_message", None)
        if not callable(get_partial):
            return False
        try:
            await self._discord_api_call(get_partial(message_id).delete)
        except discord.NotFound:
            state.prep_panel_message_id = None
        except (discord.Forbidden, discord.HTTPException) as error:
            log.warning("前回の役職確認パネル削除に失敗 (%s): %s", state.room_name, error)
            return False
        else:
            state.prep_panel_message_id = None
        return True

    async def _close_prep_panel(self) -> None:
        """役職確認終了後にパネルのボタンを無効化する。"""
        state = self.state
        view = self._prep_view
        msg = state.prep_panel_message
        self._prep_view = None
        state.prep_panel_message = None
        if view is None:
            # 再起動後はView実体が無い。残った見た目だけを削除する。
            await self._delete_stale_prep_panel()
            return
        # Viewをstopする前に表示を無効化する。逆順では編集がDiscordへ届く
        # 数百msの間に旧ボタンを押すと、汎用のinteraction failedになり得る。
        visually_closed = msg is not None and all(
            item.disabled for item in view.children
        )
        if msg is not None and not all(item.disabled for item in view.children):
            for item in view.children:
                item.disabled = True
            try:
                await self._discord_api_call(msg.edit, view=view)
            except discord.NotFound:
                visually_closed = True
            except (discord.Forbidden, discord.HTTPException):
                # 保存済みIDを残し、終了処理または再起動時に削除を再試行する。
                visually_closed = False
            else:
                visually_closed = True
        view.stop()
        if visually_closed:
            state.prep_panel_message_id = None

    async def toggle_prep_ready(self, member: discord.Member) -> tuple[str, Optional[str]]:
        """「役職を確認した」を宣言する (**一度きり。取り消せない**)。

        トグルにしていた頃は、スマホで反応が遅いと二度タップしてしまい、
        本人には何も返らないまま宣言が取り消されていた。誰が押していないかは
        村へ出さないので、12/13 のまま理由が分からず次へ進まない。
        役職を確認し直すのにボタンを取り消す必要はないため、冪等にする。
        「朝を迎える」も夜時間終了後の一方向宣言で、取り消せない。

        Returns:
            (パネルの新しい本文, エラー文字列) — エラー時は本文を使わない
        """
        state = self.state
        if state.prep_ready_event.is_set():
            return "", "▶️ まもなく1日目へ進みます。"
        if not self.prep_actions_open():
            return "", "⏳ 現在この操作はできません。"
        player = state.get_player(member.id)
        if player is None or not player.alive:
            return "", "⏳ 参加者だけが押せます。"

        if member.id in state.prep_ready_ids:
            return "", "✅ 既に「役職を確認した」を宣言しています。"

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

        時間切れでは自動的に1日目へ進まない (夜の _wait_for_morning と同じ)。
        誰が押していないかは村へ出さず、人数だけ表示する。
        """
        state = self.state
        if state.prep_ready_event.is_set():
            return

        # パネルが出ていないと誰も1日目へ進めない
        if state.prep_panel_message is None:
            await self._post_prep_panel()
        if state.prep_panel_message is None:
            log.error(f"役職確認パネルを掲示できません ({state.room_name}): 自動で1日目へ進みます")
            state.prep_confirmed = True
            try:
                await self._persist_room_state()
            except Exception:
                state.prep_confirmed = False
                raise
            state.prep_ready_event.set()
            await self._safe_village_send(
                "⚠️ 「役職を確認した」パネルを表示できないため、自動で1日目へ進みます。"
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
                        "#昼 のパネルを押すと1日目へ進みます。",
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"役職確認の催促DM送信失敗 ({player.display_name}): {e}")

            await asyncio.gather(*(warn(p) for p in not_ready))

        await self._safe_village_send(
            "⏳ **役職確認タイムの目安時間が終了しました。** "
            "未確認の方はDMの役職を確認してください。"
        )
        await self._pausable_wait_forever(state.prep_ready_event)


    async def _process_night(self) -> Optional[int]:
        """夜の結果処理。襲撃が成功したらplayer_idを返す"""
        state = self.state
        await state.pause_event.wait()
        if state.night_resolved:
            return None

        # 通常のUI経路だけでなく、直接のEvent操作からも未護衛のまま
        # 夜を解決させない。状態をdurableに戻してから同じ夜を
        # 再掲示するので、再起動復元経路でも護衛放棄は発生しない。
        if self._pending_guard_player() is not None:
            await self._reopen_night_for_required_guard()
            await self._night_phase(resume_existing=True)
            return await self._process_night()

        old_night_resolved = state.night_resolved
        old_guard_previous = state.guard_previous
        old_last_guarded = state._last_guarded
        old_last_killed = state._last_killed
        old_next_turn_anchor_number = state.next_turn_anchor_number
        old_action_log_len = len(state.action_log)
        old_record_event_seq = state.record_event_seq

        try:
            # この印は襲撃死の alive=False と同じスナップショットへ保存される。
            # _execute_player 内のpersist後にクラッシュしても同じ夜を再解決しない。
            state.night_resolved = True
            state._last_killed = None
            state._last_guarded = False
            # 2日目以降のターン起点。襲撃成功なら死亡席、GJなら同じ襲撃対象
            # 本人を指す。噛みなし・未行動はNoneとし、昼初期化時にランダム化する。
            attacked = (
                state.get_player(state.wolf_target)
                if state.wolf_target and state.wolf_target != -1
                else None
            )
            state.next_turn_anchor_number = attacked.number if attacked is not None else None

            killed_id = None

            # 連続護衛判定もnight_resolved/襲撃死と同じ最初の
            # スナップショットに入れる。
            if state.guard_target and state.guard_target != -1:
                state.guard_previous = state.guard_target
            else:
                state.guard_previous = None

            # 襲撃投票・襲撃確定のログ用 event_seq をここで払い出し、
            # これから行う persist (killed_id 有無どちらの分岐でも必ず通る)
            # に含める。狼DMのコード (WolfVoteView 等) には一切触れず、
            # 夜が確定するこの受付関数側で wolf_voters / wolf_target を
            # 読むだけにとどめる。
            wolf_vote_log_entries: list[tuple[Player, Optional[int], int]] = []
            for wolf_id, voted_target_id in state.wolf_voters.items():
                wolf = state.get_player(wolf_id)
                if wolf is None:
                    continue
                state.record_event_seq += 1
                wolf_vote_log_entries.append((wolf, voted_target_id, state.record_event_seq))
            state.record_event_seq += 1
            attack_result_seq = state.record_event_seq

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

            # 記録DBへの追記は state 側が durable になった後に行う。
            # 失敗しても進行は止めない (仕様§0-8)。
            try:
                for wolf, voted_target_id, seq in wolf_vote_log_entries:
                    if voted_target_id and voted_target_id != -1:
                        vote_target = state.get_player(voted_target_id)
                        vote_target_id = vote_target.user_id if vote_target else None
                        vote_target_number = vote_target.number if vote_target else None
                        vote_result = None
                    else:
                        vote_target_id = None
                        vote_target_number = None
                        vote_result = "噛みなし" if voted_target_id == -1 else None
                    await database.record_night_action(
                        state.guild.id, state.room_id, state.game_run_id,
                        event_seq=seq, night_number=state.day_number,
                        actor_id=wolf.user_id, actor_number=wolf.number,
                        actor_role="人狼", action="襲撃投票",
                        target_id=vote_target_id, target_number=vote_target_number,
                        result=vote_result,
                    )

                if killed_id is not None:
                    victim = state.get_player(killed_id)
                    attack_target_id = victim.user_id if victim else killed_id
                    attack_target_number = victim.number if victim else None
                    attack_result = "襲撃成功"
                elif state._last_guarded:
                    guarded = state.get_player(state.guard_target)
                    attack_target_id = guarded.user_id if guarded else None
                    attack_target_number = guarded.number if guarded else None
                    attack_result = "GJ"
                elif state.wolf_target == -1:
                    attack_target_id = None
                    attack_target_number = None
                    attack_result = "噛みなし"
                else:
                    attack_target_id = None
                    attack_target_number = None
                    attack_result = "襲撃なし"
                await database.record_night_action(
                    state.guild.id, state.room_id, state.game_run_id,
                    event_seq=attack_result_seq, night_number=state.day_number,
                    actor_id=0, actor_number=0,
                    actor_role="人狼", action="襲撃確定",
                    target_id=attack_target_id, target_number=attack_target_number,
                    result=attack_result,
                )
            except Exception as error:
                log.exception(f"襲撃ログのDB記録に失敗 ({state.room_name}): {error}")

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
            state.next_turn_anchor_number = old_next_turn_anchor_number
            state.record_event_seq = old_record_event_seq
            del state.action_log[old_action_log_len:]
            await self._stop_for_durability_error("夜解決状態の保存", e)
            raise
        except Exception as e:
            state.night_resolved = old_night_resolved
            state.guard_previous = old_guard_previous
            state._last_guarded = old_last_guarded
            state._last_killed = old_last_killed
            state.next_turn_anchor_number = old_next_turn_anchor_number
            state.record_event_seq = old_record_event_seq
            del state.action_log[old_action_log_len:]
            await self._stop_for_durability_error("夜解決状態の保存", e)
            raise StateDurabilityError("夜解決状態を保存できませんでした") from e

    # ============================================================
    # 朝のログ
    # ============================================================

    def build_morning_log_text(self) -> str:
        """朝のログ本文を組み立てる (Discordに触れない部分だけ)。"""
        state = self.state
        lines = []

        if state._last_executed:
            lines.append(f"☀️ 処刑: {state._last_executed.display_name}（役職は非公開）")

        # 護衛成功・噛みなし・未行動はすべて同じ文言に統一する
        # (理由を出し分けると護衛の生存状況などが村へ漏れるため)
        if state._last_killed:
            lines.append(f"🌙 襲撃: {state._last_killed.display_name}（死亡）")
            # 襲撃死は朝まで本人も気づけない。人狼予想の受付が続いている
            # ときだけ、提出先を1行だけ添える (DM廃止ぶんの導線)。
            if state._last_killed.user_id in state.spirit_hold_ids:
                lines.append(WOLF_GUESS_NOTICE)
        else:
            lines.append("🕊️ 平和な朝を迎えました")

        lines.append(f"\n現在の生存者: **{len(state.alive_players())}人**")
        return "\n".join(lines)

    async def _morning_log(self) -> None:
        state = self.state
        await state.pause_event.wait()
        state.phase = Phase.MORNING
        await self._persist_room_state()
        # 夜明けSEは _night_phase の末尾 (夜が終わった瞬間) で鳴らす。
        # ここで二重に鳴らさない

        embed = discord.Embed(
            title=f"🌅 {state.day_number}日目の朝",
            description=self.build_morning_log_text(),
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

    def _build_bonus_facts(self, game_stats: Optional[dict]) -> dict:
        """プレイボーナスの材料を精算キューへ載せる形にまとめる。

        日数と護衛成功数は集計済みの統計から、投票と人狼予想はゲーム中に
        控えたものから取る。Discord APIは呼ばない。
        """
        state = self.state
        stats = game_stats or {}
        return {
            "days": int(stats.get("days") or state.day_number or 0),
            "guard_successes": int(stats.get("guard_successes") or 0),
            "executions": list(state.decisive_executions),
            # JSONのキーは文字列になるので、読み出し側で int へ戻している
            "wolf_guesses": {
                str(player_id): list(targets)
                for player_id, targets in state.wolf_guesses.items()
            },
            "night1_kill_target": state.night1_killed_id,
        }

    def _settlement_variant_kwargs(self) -> dict:
        """精算キューへ、その試合で固定した変種パラメータを渡す。"""
        variant = self.variant
        return {
            "variant_id": variant.variant_id,
            "ladder_id": variant.ladder_id,
            "village_win_pool": variant.village_win_pool,
            "wolf_win_pool": variant.wolf_win_pool,
            "wolf_guess_slots": variant.wolf_guess_slots,
            "final_day_threshold": variant.final_day_threshold,
        }

    def _started_at_str(self) -> Optional[str]:
        """試合開始時刻をDB書式 (UTCの'YYYY-MM-DD HH:MM:SS') へ変換する。

        state.started_at はゲーム開始採番時 (game_run_id 発行と同じ場所) に
        設定される。未設定 (旧snapshotからの復帰直後など) ならNoneを返し、
        stage_game_settlement側の既定Noneに委ねる。
        """
        started_at = self.state.started_at
        if started_at is None:
            return None
        return started_at.strftime("%Y-%m-%d %H:%M:%S")

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
        # 受付を閉じてから材料を集める。解放すると spirit_hold_ids が空になり、
        # submit_wolf_guess も通らなくなるので、提出の締めを兼ねる
        await self._release_all_spirit_holds()

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

        bonus_facts = self._build_bonus_facts(game_stats)

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
                            state.guild.id, self.variant.ladder_id
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
                        bonus_facts=bonus_facts,
                        gm_id=state.gm_id,
                        base_room_id=state.room_id,
                        recruitment_id=state.recruitment_id,
                        started_at=self._started_at_str(),
                        **self._settlement_variant_kwargs(),
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
                    bonus_facts=bonus_facts,
                    gm_id=state.gm_id,
                    base_room_id=state.room_id,
                    recruitment_id=state.recruitment_id,
                    started_at=self._started_at_str(),
                    **self._settlement_variant_kwargs(),
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
        await self._close_game_views_for_shutdown()
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
                                before_rank_map = await database.get_current_rank_map(
                                    state.guild.id, self.variant.ladder_id
                                )
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
                                    bonus_facts=bonus_facts,
                                    gm_id=state.gm_id,
                                    base_room_id=state.room_id,
                                    recruitment_id=state.recruitment_id,
                                    started_at=self._started_at_str(),
                                    **self._settlement_variant_kwargs(),
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
            finished_ladder_id = self.variant.ladder_id
            recommendation_voters = self._postgame_recommendation_voters(state)
            postgame_voters: set[int] = set()
            loser_ids: set[int] = set()
            if winner is not None:
                postgame_voters = self._postgame_vote_voters(state, winner)
                loser_ids = {
                    player.user_id
                    for player in state.players.values()
                    if player.role is not None and ROLE_TEAM[player.role] is not winner
                }
                if not loser_ids:
                    postgame_voters = set()
            ballot_keys = (
                {(voter_id, "recommend") for voter_id in recommendation_voters}
                | {(voter_id, "postgame") for voter_id in postgame_voters}
            )
            if ballot_keys:
                try:
                    # タスク起動前に行を作り、直後のシーズンリセットとの隙間を塞ぐ。
                    if recommendation_voters:
                        await database.create_game_recommendation_ballots(
                            int(game_id),
                            state.guild.id,
                            recommendation_voters,
                            timeout_seconds=POSTGAME_RECOMMENDATION_TIMEOUT,
                            kind="recommend",
                        )
                    if postgame_voters:
                        await database.create_game_recommendation_ballots(
                            int(game_id),
                            state.guild.id,
                            postgame_voters,
                            timeout_seconds=POSTGAME_RECOMMENDATION_TIMEOUT,
                            kind="postgame",
                        )
                except Exception as e:
                    log.exception(f"終了後投票の受付作成に失敗: {e}")
                    await self._safe_village_send(
                        "⚠️ 終了後投票の受付を開始できませんでした。ログを確認してください。"
                    )
                else:
                    # 次ゲーム開始は旧#昼を削除するため、投票が終わるまでは
                    # 全卓で開始だけを止める。GM名前村も公開なのでアクセス
                    # ロールを保持・回収する処理は不要。
                    self._postgame_vote_pending = True
                    self.manager.spawn_bg_task(
                        self._run_postgame_recommendations_task(
                            state,
                            int(game_id),
                            ballot_keys,
                            loser_ids,
                            ladder_id=finished_ladder_id,
                        )
                    )

        cleanup_progress = await self._safe_village_send(
            "⏳ **ゲーム終了処理中**\n"
            "ニックネーム・ミュート・VC権限を順番に復元しています。"
        )

        # ニックネーム復元。Discord一時失敗は空LOBBYへ持ち越し、次の開始前に
        # 再試行するため、成功した対象と混ぜて捨てない。
        nickname_failures = await self._restore_nicknames(state)

        # ゲーム用の一時ロール・VC個別権限を全て撤去
        await self._teardown_game_roles_and_perms()
        cleanup_text = (
            "⚠️ **ゲーム終了処理は完了しました。**\n"
            f"名前を戻せなかったメンバーが{len(nickname_failures)}人います。次の開始前に再試行します。"
            if nickname_failures
            else "✅ **ゲーム終了処理が完了しました。**\nニックネームとVC設定を復元しました。"
        )
        await self._safe_timer_edit(cleanup_progress, cleanup_text)

        # ログカテゴリへ退避するのは #昼 だけ。移せなければ従来どおり削除する。
        # #霊界 は退避せず常に削除する (Noneのカテゴリ名で削除側へ倒す)。
        game_channels = [
            (state.village_channel, LOG_CATEGORY_VILLAGE),
            (state.spirit_channel, None),
        ]
        game_channels = [(ch, cat) for ch, cat in game_channels if ch]
        seq = None
        if game_id is not None and state.guild is not None:
            try:
                seq = await database.get_game_sequence_number(
                    state.guild.id, int(game_id)
                )
            except Exception as e:
                log.warning(f"ログ用の試合番号を取得できません: {e}")

        archive_to_public_log = self._can_archive_to_public_log()
        if seq is None or not archive_to_public_log:
            await self._safe_village_send(
                f"🕐 このチャンネルは {CHANNEL_DELETE_DELAY}秒後 に削除されます。"
            )
        else:
            await self._safe_village_send(
                f"🕐 このチャンネルは {CHANNEL_DELETE_DELAY}秒後 に "
                f"**{LOG_CATEGORY_VILLAGE}** へ移動します (読み返せます)。"
            )

        async def archive_channels():
            await asyncio.sleep(CHANNEL_DELETE_DELAY)
            for ch, category_name in game_channels:
                if (
                    category_name is not None
                    and archive_to_public_log
                    and seq is not None
                    and await self._archive_game_channel(
                        ch,
                        category_name,
                        seq,
                    )
                ):
                    continue
                try:
                    await ch.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"#{ch.name} チャンネル削除失敗: {e}")

        self.manager.spawn_bg_task(archive_channels())

        # 次村用に直前のメンバーを記録
        if state.players:
            self.last_game_roster = list(state.players.keys())
            self.last_game_gm = state.gm_id

        if not await self._transition_to_empty_lobby(
            state,
            nickname_failures=nickname_failures,
            log_label="ゲーム終了後LOBBY初期化",
        ):
            await self._safe_village_send(
                "⚠️ 参加受付の初期化をDBへ保存できませんでした。"
                "状態を保持しているため、GM操作から再試行してください。"
            )

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

    @staticmethod
    def _postgame_vote_voters(state: GameState, winner: Team) -> set[int]:
        """勝利陣営の全員を返す (敗北陣営の1人へ+1を贈る票)。"""
        return {
            player.user_id
            for player in state.players.values()
            if player.role is not None and ROLE_TEAM[player.role] is winner
        }

    async def _run_postgame_recommendations(
        self,
        finished_state: GameState,
        game_id: int,
        ballot_keys: set[tuple[int, str]],
        loser_ids: set[int],
        *,
        ladder_id: str,
    ) -> None:
        """終了後の投票パネルを `#昼` に1枚だけ出し、締切後に匿名で集計する。

        **DMは送らない。** 投票権者は最大13人になるので、DMだと1試合で13通に
        なる。パネルなら送信APIは1回で済む (押下は interaction 専用ルート)。
        """
        guild = finished_state.guild
        if guild is None:
            return
        pending = set(ballot_keys)
        all_done = asyncio.Event()

        def on_confirmed(voter_id: int, kind: str) -> None:
            pending.discard((voter_id, kind))
            if not pending:
                all_done.set()

        ballots: dict[int, list[str]] = {}
        for voter_id, kind in sorted(ballot_keys):
            if voter_id in finished_state.players:
                ballots.setdefault(voter_id, []).append(kind)
            else:
                # 参加者として復元できない票は閉じる (集計を待たせない)
                pending.discard((voter_id, kind))
                await database.cancel_game_recommendation_ballot(
                    game_id, guild.id, voter_id, kind=kind,
                )

        if ballots:
            view = PostgameVotePanelView(
                game_id=game_id,
                guild_id=guild.id,
                ballots=ballots,
                players=list(finished_state.players.values()),
                loser_ids=loser_ids,
                timeout=POSTGAME_RECOMMENDATION_TIMEOUT,
                on_confirmed=on_confirmed,
            )
            channel = finished_state.village_channel
            if channel is not None:
                try:
                    view.message = await channel.send(
                        "🗳️ **終了後の投票**（受付 "
                        f"**{POSTGAME_RECOMMENDATION_TIMEOUT // 60}分**・"
                        f"1票につきレート+{BONUS_POSTGAME_VOTE}）\n"
                        "・**勝利陣営**は、手強かった敗北陣営の1人へ\n"
                        "・**霊媒師 / 初日の処刑者 / 初夜の襲撃死者**は、参加者の1人へ\n"
                        "投票権のある人だけが操作できます。投票者名は公開されません。",
                        view=view,
                    )
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ) as exc:
                    log.warning("終了後投票パネルの投稿失敗: %s", exc)

        if not pending:
            all_done.set()
        try:
            await asyncio.wait_for(
                all_done.wait(), timeout=POSTGAME_RECOMMENDATION_TIMEOUT
            )
        except TimeoutError:
            pass

        async with self.manager.rating_lock:
            before_rank_map = await database.get_current_rank_map(
                guild.id, ladder_id
            )
            results = await database.finalize_game_recommendations(
                game_id, guild.id, close_pending=True
            )
            after_rank_map = await database.get_current_rank_map(
                guild.id, ladder_id
            )
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
                    member,
                    rank_ctx.rank_name,
                    roles_map=roles_map,
                    ladder_id=ladder_id,
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
        embed.set_footer(
            text=f"推薦者名は非公開です。推薦は1票につきレート+{BONUS_POSTGAME_VOTE}。"
        )
        channel = finished_state.village_channel
        if channel is not None:
            try:
                await channel.send(embed=embed)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"終了後推薦結果の投稿失敗: {e}")

    async def _run_postgame_recommendations_task(
        self,
        finished_state: GameState,
        game_id: int,
        ballot_keys: set[tuple[int, str]],
        loser_ids: set[int],
        *,
        ladder_id: str,
    ) -> None:
        """終了後投票を実行し、完了後に次ゲーム開始を解放する。"""
        try:
            await self._run_postgame_recommendations(
                finished_state,
                game_id,
                ballot_keys,
                loser_ids,
                ladder_id=ladder_id,
            )
        finally:
            self._postgame_vote_pending = False
            # 投票中に先に掲示したロビーは開始ボタンをdisabledにしている。
            # 完了後に同じメッセージを静かに再描画し、押して初めて3分待ちと
            # 分かる状態や、期限後もボタンだけ無効のまま残る状態を防ぐ。
            state = self.state
            if state.phase == Phase.LOBBY and not state.ending:
                try:
                    await self._post_lobby_ui(reuse_existing=True)
                except Exception as error:
                    # 推薦集計自体は完了済み。パネル再描画の一時失敗で
                    # タスクを失敗扱いにせず、次のロビー更新で追いつかせる。
                    log.warning(
                        "終了後投票完了後のロビーパネル更新に失敗 (%s): %s",
                        state.room_name,
                        error,
                    )

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

        after_rank_map = await database.get_current_rank_map(
            guild.id, self.variant.ladder_id
        )
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
            play_bonus = r.get("play_bonus", 0)
            recommendation_bonus = r.get("recommendation_bonus", 0)
            sign = "+" if delta >= 0 else ""
            elo_sign = "+" if elo_delta >= 0 else ""
            parts = [f"本体{elo_sign}{elo_delta}"]
            if bonus > 0:
                parts.append(f"勝利+{bonus}")
            if play_bonus > 0:
                parts.append(f"活躍+{play_bonus}")
            if recommendation_bonus > 0:
                parts.append(f"投票+{recommendation_bonus}")
            detail_txt = f" ({' / '.join(parts)})" if len(parts) > 1 else ""
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
                await self.manager._sync_rank_role(
                    member,
                    rank_ctx.rank_name,
                    roles_map=roles_map,
                    ladder_id=self.variant.ladder_id,
                )
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

    def _day_discussion_skipped(self) -> bool:
        """当日の議論をGMが打ち切ったか。世代一致だけを正とする。"""
        state = self.state
        return (
            state.day_discussion_skip_generation is not None
            and state.day_discussion_skip_generation == state.day_generation
        )

    def _sync_day_discussion_skip_event(self) -> None:
        """保存済みのスキップ指示を、その日の進行イベントへ反映する。"""
        if self._day_discussion_skipped():
            self.state.day_discussion_skip_event.set()
        else:
            self.state.day_discussion_skip_event.clear()

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
        # GM再開とVC切断による自動停止が同時に走ると、後から完了した再開側が
        # pause_eventを再び開き、paused=Trueのままタイマーだけ進み得る。
        # resume_gameと同じロックで状態遷移を直列化する。
        async with self.resume_lock:
            return await self._pause_game_locked(reason)

    async def _pause_game_locked(self, reason: Optional[str] = None) -> str:
        state = self.state
        if not self._is_game_in_progress():
            return "進行中のゲームがありません。"
        if state.paused:
            return "既に一時停止中です。"
        state.paused = True
        state.phase_before_pause = state.phase
        state.phase = Phase.PAUSED
        # ターンの0.5秒待機を即座に起こしてからpause gateへ移す。
        state.turn_signal_event.set()
        state.pause_event.clear()
        await self._persist_room_state()

        # タイマーだけでなく会話も停止する。切断者不在のまま議論が続くのを防ぐ。
        changed = await self._sync_server_mutes(set())
        if not await self._await_mute_applied(changed, MUTE_GRACE_TIME):
            await self._safe_village_send(
                "⚠️ **一時停止しましたが、全員のミュート反映を確認できません。**\n"
                "タイマーは停止中です。Botのメンバーミュート権限とロール順位を確認してから、"
                "GMが再開してください。"
            )
            return (
                "⚠️ タイマーは停止しましたが、全員のミュート反映を確認できません。"
                "権限確認後に再開してください。"
            )

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
            await self._send_surrender_controls()

            await self._assign_alive_role()
            await self._prepare_game_vc_permissions(
                "開始フェーズ復元時のVC権限設定"
            )
            await self._mute_all()
            await self._persist_room_state()
            await self._safe_village_send(
                "♻️ **保存済みの役職配布内容で開始処理を復元しました。**"
            )
            await self._game_loop()
        except asyncio.CancelledError:
            log.info("開始復元タスクがキャンセルされました")
        except StateDurabilityError as e:
            # VC権限などの失敗時は呼出元で既に安全停止・保存・告知済み。
            log.error(f"開始フェーズ復元を安全停止: {e}")
        except Exception as e:
            log.exception(f"開始フェーズ復元エラー: {e}")
            await self._stop_for_durability_error("開始フェーズの復元", e)

    async def _resume_recovered_game(self) -> None:
        state = self.state
        try:
            resume_phase = state.recovery_phase or state.phase_before_pause or Phase.DAY_DISCUSSION
            state.recovered_from_restart = False
            state.recovery_phase = None

            # DAY_VOTE / DAY_RUNOFF_* / DAY_LAST_WILL / MORNING から再開する分岐は
            # 「昼開始」を経由せず finish_recovered_vote / _day_vote / _runoff /
            # _last_will / _morning_log を直接呼ぶため、村パネルの貼り直し
            # (post_village_panel) を通る _day_discussion / _turn_day_discussion /
            # _night_phase を一切経由しない。放置すると復元後その日が夜に
            # 遷移するまでCO・CO撤回・占い・狩人・霊媒・人狼予想の全ボタンが
            # 使えないままになる。
            # そのためここで進行中フェーズなら無条件に1回貼り直す。
            # _day_discussion / _night_phase を通る経路では二重貼り直しに
            # なるが、post_village_panel は旧メッセージを削除してから送るため
            # 実害はなく、再起動1回あたりAPI呼び出しが2回増えるだけ。
            # 経路ごとに貼り直しの要否を分岐する複雑さより、単純さと
            # 確実さ (どの経路で再開してもパネルが必ず生きている) を
            # 優先する。
            if state.phase not in (Phase.LOBBY, Phase.GAME_OVER):
                await self.post_village_panel()

            async def finish_night(*, resume_existing: bool) -> bool:
                """夜を解決して朝へ進む。勝敗確定ならTrue。"""
                if not state.night_resolved:
                    # 夜明けが確定済みでも、狩人の護衛が未確定/無効なら
                    # 必ず確定を解除してUIを再掲示する。
                    # これを先に行わないと ``morning_confirmed=True`` の経路が
                    # _night_phase を飛ばして未護衛のまま解決してしまう。
                    if self._pending_guard_player() is not None:
                        await self._reopen_night_for_required_guard()
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

            if resume_phase == Phase.INITIAL_NIGHT:
                await self._safe_village_send(
                    "♻️ **0日目の初夜** を保存状態から再開します。"
                )
                if not state.initial_night_completed:
                    await self._initial_night_greeting()
                if state.surrender_confirmed or state.pending_winner is not None:
                    return
                # 通常_game_loopと同じ1日目開始checkpointへ揃える。
                state.day_number = 1
                state.day_generation = 1
                state.day_execution_resolved = False
                state.day_executed_target = None
                state.pending_execution_target = None
                await self._persist_room_state()
            elif resume_phase == Phase.NIGHT:
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
                # 集計まで終えて処刑対象を保存した後に落ちた場合は、投票を
                # やり直さず保存済み対象から続ける。決戦側と同じ扱いにする。
                # やり直すと同じ票をもう一度集計し、活躍ボーナスの基礎に
                # なる decisive_executions が二重に積まれる。
                if state.pending_execution_target is not None:
                    await self._safe_village_send(
                        "♻️ **保存済みの投票結果** から処刑処理を再開します。"
                    )
                    if await finish_recovered_vote(state.pending_execution_target):
                        return
                else:
                    await self._safe_village_send(
                        f"♻️ **{state.day_number}日目の投票** を保存済みの順番から再開します。"
                    )
                    if await finish_recovered_vote(
                        await self._day_vote()
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
                        await self._runoff(
                            state.runoff_candidates,
                            resume_vote=resume_vote,
                            resume_speech=not resume_vote,
                        )
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
                    if self.is_turn_discussion_mode():
                        await self._safe_village_send(
                            f"♻️ **{state.day_number}日目のターン制議論** を"
                            "保存済みの発言枠から再開します。"
                        )
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

    async def _eliminate_player_mid_game(self, player: Player, reason: str) -> bool:
        """ゲーム中のプレイヤーを死亡扱いで除外する (サーバー退出 / GM除外)。

        役職は公開しない。除外で勝敗が決した場合はゲームループを止めて終了処理を行う。
        """
        state = self.state
        if state.phase in (Phase.LOBBY, Phase.GAME_OVER):
            # 廃村/終了処理と競合した場合は何もしない
            return False
        if not player.alive:
            return False
        old_votes = dict(state.votes)
        old_vote_order = list(state.vote_order)
        old_disconnected = set(state.disconnected_players)
        old_wolf_target = state.wolf_target
        old_wolf_voters = dict(state.wolf_voters)
        old_guard_target = state.guard_target
        old_ready = set(state.morning_ready_ids)
        old_morning_warned = set(state.morning_warned_ids)
        old_morning_confirmed = state.morning_confirmed
        old_morning_event = state.morning_ready_event.is_set()
        old_prep_ready = set(state.prep_ready_ids)
        old_prep_confirmed = state.prep_confirmed
        old_prep_event = state.prep_ready_event.is_set()
        old_night_complete = state.night_complete_event.is_set()
        old_vote_complete = state.vote_complete_event.is_set()
        old_action_log_len = len(state.action_log)
        guard = next(
            (candidate for candidate in state.players.values()
             if candidate.role == Role.GUARD and candidate.alive),
            None,
        )
        guard_target_invalidated = bool(
            self._effective_phase() == Phase.NIGHT
            and not state.night_resolved
            and guard is not None
            and guard.user_id != player.user_id
            and state.guard_target == player.user_id
        )
        # 襲撃先として除外した相手が残ると、夜結果処理が死亡済み対象を
        # 襲撃しようとする。狼自身の除外も投票者一覧から消す。現在の襲撃先が
        # 無効になった場合は、残存する狼に選び直してもらうため朝宣言を戻す。
        wolf_target_invalidated = bool(
            self._effective_phase() == Phase.NIGHT
            and not state.night_resolved
            and state.wolf_target == player.user_id
        )
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
        invalidated_voters = [
            voter_id for voter_id, target_id in state.votes.items()
            if target_id == player.user_id
        ]
        for voter_id in invalidated_voters:
            del state.votes[voter_id]
            # 通常投票で既に公開済みの票を失った人には、現在の順番を崩さず
            # 最後にもう一度30秒の発言枠を与える。同じ人を複数回は積まない。
            if (
                # 一時停止中の除外でも票を返す。PAUSEDを素で比較すると、
                # GMが止めてから除外した場合だけ再投票枠が付かず、
                # 巻き込まれた人が投票権を失ったまま集計に入る。
                self._effective_phase() == Phase.DAY_VOTE
            ):
                voter = state.get_player(voter_id)
                if voter is not None and voter.alive:
                    # 通り過ぎた枠はその場で積み直せる。現在の枠は
                    # _day_vote がcursorを進めてからでないと組み替えられ
                    # ないので、印だけ置いて拾い直してもらう。
                    if not self._requeue_lost_vote_slot(voter_id):
                        if voter_id in state.vote_order:
                            state.vote_requeue_ids.add(voter_id)

        # 弁明/遺言フェーズ中に本人が除外された場合は、タイムアウトを
        # 待たずに即終了する
        release_speech = (
            state.phase in (Phase.DAY_RUNOFF_SPEECH, Phase.DAY_LAST_WILL)
            and state.current_speaker_id == player.user_id
        )
        release_turn = (
            self.is_turn_discussion_mode()
            and self._effective_phase() == Phase.DAY_DISCUSSION
            and state.current_speaker_id == player.user_id
        )

        # 夜の未行動警告と「朝を迎える」宣言は、除外で条件が揃った可能性を再チェック
        # (除外された人の宣言・行動を待ち続けないようにする)
        state.morning_ready_ids.discard(player.user_id)
        state.morning_warned_ids.discard(player.user_id)
        state.wolf_voters = {
            wolf_id: target_id
            for wolf_id, target_id in state.wolf_voters.items()
            if wolf_id != player.user_id and target_id != player.user_id
        }
        removed_wolf_choice_invalidated = bool(
            self._effective_phase() == Phase.NIGHT
            and not state.night_resolved
            and player.is_wolf
            and state.wolf_target is not None
            # wolf_targetは「最後に選択された対象」。除外された狼の選択だけが
            # 正本だった場合、残存狼の票に同じ対象が無ければ採用できない。
            and state.wolf_target not in state.wolf_voters.values()
        )
        wolf_choice_invalidated = (
            wolf_target_invalidated or removed_wolf_choice_invalidated
        )
        if wolf_choice_invalidated:
            # 最後に選ばれた襲撃先が無効なら、部分的に残った投票履歴を
            # 襲撃確定へ使わない。襲撃対象そのものの除外だけでなく、
            # 最後の選択者だった狼の除外でも全生存狼に選び直してもらう。
            state.wolf_target = None
            state.wolf_voters.clear()
            living_wolf_ids = {wolf.user_id for wolf in state.alive_wolves()}
            state.morning_ready_ids.difference_update(living_wolf_ids)
            state.morning_warned_ids.difference_update(living_wolf_ids)
            state.morning_confirmed = False
            state.morning_ready_event.clear()
            state.night_complete_event.clear()
        if guard_target_invalidated and guard is not None:
            # 護衛先が死亡したままでは、狩人が「確定済み」扱いで
            # 再選択できず、未護衛の夜を解決する危険がある。除外と
            # 同じcheckpointで未確定へ戻し、朝確定も取り消す。
            state.guard_target = None
            state.morning_ready_ids.discard(guard.user_id)
            state.morning_warned_ids.discard(guard.user_id)
            state.morning_confirmed = False
            state.morning_ready_event.clear()
            state.night_complete_event.clear()
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
            if state.phase == Phase.DAY_RUNOFF_VOTE:
                alive_ids -= set(state.runoff_candidates)
            release_vote_complete = bool(
                alive_ids and alive_ids.issubset(state.votes.keys())
            )
        release_vote_speech = bool(
            self._effective_phase() == Phase.DAY_VOTE
            and state.vote_slot_active
            and state.current_speaker_id == player.user_id
            and not state.vote_speech_finished
        )
        release_vote_choice = bool(
            self._effective_phase() == Phase.DAY_VOTE
            and state.vote_slot_active
            and state.current_speaker_id == player.user_id
            and state.vote_speech_finished
        )
        # 「投票参加」待ちで止まっている場合は、除外で生存者集合が変わった
        # ことを知らせて条件を見直させる。最後の未押下者が抜けたときに
        # 誰も押さない待機のまま残り、GMのスキップ頼みになるのを防ぐ。
        release_vote_queue = self._effective_phase() == Phase.DAY_VOTE

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
            state.vote_order = old_vote_order
            state.disconnected_players = old_disconnected
            state.wolf_target = old_wolf_target
            state.wolf_voters = old_wolf_voters
            state.guard_target = old_guard_target
            state.morning_ready_ids = old_ready
            state.morning_warned_ids = old_morning_warned
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
            return False

        await self._apply_death_effect(effect)

        if await self._confirm_surrender_after_roster_change():
            return True

        winner = state.check_win()
        if winner and state.phase != Phase.GAME_OVER:
            await self._finish_game_externally(winner)
            return True

        if guard_target_invalidated and guard is not None:
            await self._request_guard_reselection(guard)
        if wolf_choice_invalidated:
            # 既存の狼DMは同じ夜のViewのまま使える。本文だけを空の襲撃先へ
            # 更新し、残存狼が有効な候補を選び直せることを明示する。
            await self.refresh_wolf_dm_displays(state.night_duration)
            await self._reveal_morning_count()

        # 待機側は、死亡本体だけでなくDiscord副作用outboxの
        # 除去保存まで成功し、かつ勝敗確定でない場合にだけ解放する。
        # それより前にsetすると、副作用保存失敗の安全停止中や
        # 外部終了前にゲームループが先へ進み得る。
        if release_speech:
            state.speech_done_event.set()
        if release_vote_speech:
            state.speech_done_event.set()
        if release_vote_choice:
            state.vote_choice_event.set()
        if release_vote_queue:
            state.vote_queue_event.set()
        if release_turn:
            state.turn_done_event.set()
            state.turn_signal_event.set()
        if release_night_complete:
            state.night_complete_event.set()
        if release_morning:
            state.morning_ready_event.set()
        if release_prep:
            state.prep_ready_event.set()
        if release_vote_complete:
            state.vote_complete_event.set()
        return True

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

    async def force_end(
        self,
        reason: str = "廃村",
        *,
        preserve_players: bool = False,
        preserve_gm: bool = True,
    ) -> bool:
        """戦績を残さずゲームを終了する。

        明示的な強制終了は参加者を外してGMだけを残す。ゲームリセットは
        ``preserve_players=True`` で同じ参加者とGMを受付へ戻す。
        GM本人がサーバー退出した経路だけは ``preserve_gm=False`` にする。
        """
        state = self.state

        if preserve_players:
            preserve_gm = True

        # ゲーム進行中以外 (ロビー/終了済み) は無視
        if state.phase in (Phase.LOBBY, Phase.GAME_OVER) or state.ending:
            return False

        if state.gm_id is None:
            # 残すGMが無い卓で参加者だけ持ち越すと保存契約に反する。
            # lobby_return_modeと実際の引き継ぎをここで一致させておく。
            preserve_gm = False
            preserve_players = False

        state.lobby_return_mode = (
            "roster"
            if preserve_players
            else "gm"
            if preserve_gm and state.gm_id is not None
            else "empty"
        )
        state.pending_recruitment_reopen = bool(
            self.is_private_room()
            and state.lobby_return_mode == "gm"
        )

        # 最初のawaitより前に「終了状態の確定」と「ループのキャンセル指示」を
        # 同期的に行う。これで退出処理 (_eliminate_player_mid_game)・
        # start_gameのループ起動・force_endの二度押しが旧stateのGAME_OVERガードで
        # 遮断され、ゲームループがこれ以降 phase を書き換えることもなくなる。
        state.ending = True
        state.phase = Phase.GAME_OVER
        if state.game_task and not state.game_task.done():
            state.game_task.cancel()
        # 古いボタンのIDを空LOBBYへ差し替える前に使い、
        # 通常終了と同じ表示上の後始末を行う。
        await self._close_game_views_for_shutdown()
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
        # (パネルの無効化は _close_game_views_for_shutdown で実施済み)
        state.morning_ready_event.set()

        # 役職確認の宣言待ちも同様に解除する
        state.prep_ready_event.set()

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
        nickname_failures = await self._restore_nicknames(state)

        # ゲーム用の一時ロール・VC個別権限を全て撤去
        await self._teardown_game_roles_and_perms()
        cleanup_text = (
            "⚠️ **終了処理は完了しました。**\n"
            f"名前を戻せなかったメンバーが{len(nickname_failures)}人います。次の開始前に再試行します。"
            if nickname_failures
            else "✅ **終了処理が完了しました。**\nニックネームとVC設定を復元しました。"
        )
        await self._safe_timer_edit(cleanup_progress, cleanup_text)

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

        return await self._transition_to_empty_lobby(
            state,
            nickname_failures=nickname_failures,
            log_label="廃村後LOBBY初期化",
            preserve_players=preserve_players,
            preserve_gm=preserve_gm,
        )

    async def reset_game(self) -> str:
        """やり直し。

        ゲーム中: 廃村して同じ参加者・GMの参加受付へ戻す。
        ロビー中: 参加者とGMを解除して受付を作り直す (無反応にしない)。
        どちらの場合も直前メンバーの記録を残す。
        """
        state = self.state
        if state.phase in (Phase.LOBBY, Phase.GAME_OVER):
            had_entries = bool(state.players) or state.gm_id is not None
            if had_entries:
                self.last_game_roster = list(state.players)
                self.last_game_gm = state.gm_id
            if state.phase == Phase.GAME_OVER:
                nickname_failures = await self._restore_nicknames(state)
            else:
                nickname_failures = dict(state.original_nicknames)
            old_ending = state.ending
            state.ending = True
            transitioned = await self._transition_to_empty_lobby(
                state,
                nickname_failures=nickname_failures,
                log_label="受付リセット",
            )
            if not transitioned:
                state.ending = old_ending
                return (
                    "⚠️ 参加受付をDBへ安全に保存できなかったため、"
                    "リセットしませんでした。時間を置いて再度お試しください。"
                )
            if had_entries:
                return "🔄 参加受付をリセットしました (参加者とGMを解除)。"
            return "🔄 参加受付をリセットしました (登録はありませんでした)。"

        if await self.force_end(
            "ゲームがリセットされました。",
            preserve_players=True,
        ):
            return "🔄 ゲームをリセットし、同じ参加者・GMで参加受付へ戻しました。"
        return (
            "⚠️ 終了処理が既に進行中か、参加受付をDBへ保存できなかったため、"
            "リセット完了を確認できませんでした。"
        )

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
        """現行ゲームで有効化済みのmute所有マーカーを取得・復旧する。"""
        state = self.state
        if not state.mute_marker_enabled:
            raise StateDurabilityError(
                "現行ゲームのsnapshotにmute所有マーカー情報がありません"
            )
        guild = state.guild
        existing_marker = (
            discord.utils.get(guild.roles, name=self._mute_marker_role_name())
            if guild is not None else None
        )
        marker = await self._ensure_mute_marker_role()
        if marker is None:
            raise StateDurabilityError("mute所有マーカーを確保できません")
        if existing_marker is not None or guild is None:
            return marker

        # 現行snapshotのままDiscord側のマーカーロールだけが削除された場合、
        # DBに残るBot所有記録を手動muteと誤認して捨てない。接続中なら実際に
        # muteの人だけ、切断中なら状態を確認できないため所有記録を保つ形で、
        # 再作成したロールを付け直す。
        for member_id in list(state.bot_muted_ids):
            member = guild.get_member(member_id)
            if member is None or self._has_own_mute_marker(member):
                continue
            voice = getattr(member, "voice", None)
            if voice is not None and not voice.mute:
                continue
            edit_kwargs: dict = {
                "roles": self._roles_with_mute_marker(
                    member, marker, present=True,
                ),
            }
            if voice is not None:
                # muteと所有マーカーを同じPATCHで確定し、途中状態を作らない。
                edit_kwargs["mute"] = True
            try:
                await self._paced_discord_api_call(
                    member.edit,
                    **edit_kwargs,
                    reason="人狼: 消失したmute所有マーカーの復旧",
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                await self._stop_for_durability_error("mute所有マーカー復旧", e)
                raise StateDurabilityError(
                    f"mute所有マーカーを復旧できません: {member_id}"
                ) from e
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
        if role is None:
            raise RuntimeError("進行中ロールを作成または取得できません")
        if vc is None:
            raise RuntimeError("ゲームVCを確認できません")
        await self._paced_discord_api_call(
            vc.set_permissions, role,
            view_channel=True, connect=True, speak=True,
            reason="人狼: 生存者のVCアクセス保証",
        )

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
        phase = self._effective_phase()
        return (
            state.wolf_relay_window_open
            and (
                (
                    phase == Phase.PREPARATION
                    and not state.initial_night_completed
                )
                or phase == Phase.INITIAL_NIGHT
                or (phase == Phase.NIGHT and not state.night_resolved)
            )
            and not state.ending
            and state.pending_winner is None
        )

    def _current_speaker_ids(self) -> set[int]:
        """現在のフェーズで発言できるべきプレイヤーIDの集合"""
        state = self.state
        # 一時停止はタイマーだけでなく会話も止める。phase_before_pauseを
        # 透過した結果、VC再入室を契機に話者を再unmuteしてはいけない。
        if state.paused:
            return set()
        phase = self._effective_phase()
        if phase == Phase.DAY_DISCUSSION:
            if self.is_turn_discussion_mode():
                current = (
                    state.get_player(state.current_speaker_id)
                    if state.current_speaker_id is not None
                    else None
                )
                return (
                    {current.user_id}
                    if (
                        state.turn_slot_active
                        and current is not None
                        and current.alive
                    )
                    else set()
                )
            return {p.user_id for p in state.alive_players()}
        if phase == Phase.DAY_VOTE:
            return (
                {state.current_speaker_id}
                if (
                    state.vote_slot_active
                    and state.vote_speech_window_open
                    and not state.vote_speech_finished
                    and state.current_speaker_id
                )
                else set()
            )
        if phase in (Phase.DAY_RUNOFF_SPEECH, Phase.DAY_LAST_WILL):
            return {state.current_speaker_id} if state.current_speaker_id else set()
        return set()

    def _uses_manual_gm_mute(self, user_id: int) -> bool:
        """Botの自動mute/unmuteから外すGM登録者か。

        参加者兼任を含む全GMは、ゲーム説明や周知を続けられるよう
        Discord側のミュートを本人が手動で管理する。
        """
        return self.state.gm_id == user_id

    async def _sync_server_mutes(
        self,
        speakers: set[int],
        *,
        skip_ids: Optional[set[int]] = None,
    ) -> list[tuple[discord.Member, bool]]:
        """VC接続中メンバーのサーバーミュートを目標状態に同期する。

        speakers に含まれるメンバーだけ発言可、他は全てミュート。
        既に目標状態のメンバーはAPIを呼ばずスキップする (メンバー編集
        バケット ≈10回/10秒 の節約)。権限suppressは権限変更のタイミング次第で
        生存者にも立ちうるDiscord側の計算値のため、立っていても発言不可対象なら
        必ずサーバーミュートする (確実さを優先し、余分なAPI呼び出しは許容する)。
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
            log.warning(
                f"VC未解決のためミュート同期をスキップしました ({state.room_name})"
            )
            return []
        skip_ids = skip_ids or set()

        marker: Optional[discord.Role] = None
        if state.guild is not None:
            # ゲーム開始時に有効化済み。mute/unmuteは全て同一PATCHで
            # マーカーを付け外しする。
            marker = await self._enable_mute_markers()

        owned_before = set(state.bot_muted_ids)
        intents_before = set(state.bot_mute_intent_ids)
        if state.gm_id is not None:
            # 新仕様ではGMへ新しいmute意図を適用しない。Discord側に
            # Bot所有マーカーがあれば、下の候補処理で安全に解除する。
            state.bot_mute_intent_ids.discard(state.gm_id)

        failed: list[tuple[discord.Member, bool]] = []

        async def set_mute(
            member: discord.Member, mute: bool, *, retry: bool = True
        ) -> bool:
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
                return True
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(
                    f"サーバーミュート変更失敗 ({member.display_name} → mute={mute}): {e}"
                )
                if retry:
                    failed.append((member, mute))
                return False

        # vc.membersはDiscordのボイス状態キャッシュ由来で、キャッシュ更新の
        # タイミング次第で参加者が静かに漏れることがある (取りこぼすと対象0件
        # になり、誰もミュートされない事故につながる)。state.playersの参加者も
        # guild.get_memberで解決し、実際にこの卓のVCに接続しているメンバーだけ
        # 候補へ追加する (vc.membersと重複しないようuser_idで集合化)。
        # 判定条件そのものは変えず、候補の集め方だけを広げる。
        candidates: dict[int, discord.Member] = {m.id: m for m in vc.members}
        if state.guild is not None:
            for user_id in state.players:
                if user_id in candidates:
                    continue
                member = state.guild.get_member(user_id)
                if member is None:
                    continue
                vs = getattr(member, "voice", None)
                if vs is None or vs.channel is None or vs.channel.id != vc.id:
                    continue
                candidates[user_id] = member

        targets: list[tuple[discord.Member, bool]] = []
        skip_counts = {"bot": 0, "gm": 0, "vc不一致": 0, "既にmute": 0, "suppress": 0}
        for member in candidates.values():
            if member.bot:
                skip_counts["bot"] += 1
                continue
            vs = member.voice
            if vs is None or vs.channel is None or vs.channel.id != vc.id:
                skip_counts["vc不一致"] += 1
                continue
            if self._uses_manual_gm_mute(member.id):
                # 旧版でBotが付けたものだけ解除する。所有記録もマーカーも
                # 無いmuteはGM本人の手動操作なので、そのまま保護する。
                if (
                    member.id in state.bot_muted_ids
                    or self._has_own_mute_marker(member)
                ):
                    targets.append((member, False))
                else:
                    skip_counts["gm"] += 1
                continue
            if member.id in skip_ids:
                skip_counts["既にmute"] += 1
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
                    skip_counts["既にmute"] += 1
            elif not should_speak and not vs.mute:
                if getattr(vs, "suppress", False):
                    # 権限側の計算値は生存者にも立ちうるため取りこぼせない。
                    # スキップはせず、必ずミュート対象に含めた上でログに残す
                    # (どれだけ取りこぼしていたかを後から確認できるように)。
                    skip_counts["suppress"] += 1
                    log.info(
                        "suppress中の相手もミュート対象に含めました: "
                        f"{member.display_name} ({state.room_name})"
                    )
                targets.append((member, True))
            else:
                skip_counts["既にmute"] += 1

        if not targets:
            log.info(
                "ミュート同期: 対象0件 "
                f"(VC接続{len(vc.members)}人, 候補{len(candidates)}人, "
                f"bot={skip_counts['bot']}, gm={skip_counts['gm']}, "
                f"VC不一致={skip_counts['vc不一致']}, "
                f"既にmute={skip_counts['既にmute']}, "
                f"suppressだが対象化={skip_counts['suppress']})"
            )
            if (
                state.bot_muted_ids != owned_before
                or state.bot_mute_intent_ids != intents_before
            ):
                await self._persist_mute_ownership_checkpoint(
                    "GM手動mute切替状態の保存"
                )
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
            final_failed: list[tuple[discord.Member, bool]] = []
            for member, mute in retry_targets:
                if not await set_mute(member, mute, retry=False):
                    final_failed.append((member, mute))
            if final_failed:
                details = ", ".join(
                    f"{member.display_name}(mute={mute})"
                    for member, mute in final_failed
                )
                log.error(
                    "サーバーミュート最終失敗 "
                    f"({state.room_name}, {len(final_failed)}人): {details}"
                )
        # mute操作と所有記録を別々の障害窓にしない。
        # ここで落ちても、再起動後にBot自身がmuteした相手を
        # 手動muteと誤認せず解除できる。
        if (
            state.bot_muted_ids != owned_before
            or state.bot_mute_intent_ids != intents_before
        ):
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
            if self._uses_manual_gm_mute(user_id):
                # markerは直前の所有照合でも拾うが、このメソッド単独でも
                # Bot所有証拠を捨てない。次の同期でGMから安全に解除する。
                if member is not None and self._has_own_mute_marker(member):
                    state.bot_muted_ids.add(user_id)
                state.bot_mute_intent_ids.discard(user_id)
                changed = True
                continue
            if member is None:
                state.bot_mute_intent_ids.discard(user_id)
                changed = True
                continue
            vs = getattr(member, "voice", None)
            if vs is None or not vs.mute:
                # 未実行。意図は残し、次の_sync_server_mutesで実行する。
                continue
            if state.mute_marker_enabled and self._has_own_mute_marker(member):
                # mute+専用ロールを同一PATCHで送っているため、
                # ニックネームより強い一意な成功証拠になる。
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

    async def _grant_turn_speaker(self, speaker: Player) -> None:
        """ターン話者が実際に発言可能になってから持ち時間を開始する。"""
        await self._grant_speaker(speaker.member)
        state = self.state
        voice = getattr(speaker.member, "voice", None)
        in_game_vc = (
            voice is not None
            and voice.channel is not None
            and state.voice_channel is not None
            and voice.channel.id == state.voice_channel.id
        )
        if not in_game_vc:
            await self._pause_for_disconnect(
                speaker, "発言順ですが通話に接続していません"
            )
            return
        if not bool(getattr(voice, "mute", False)):
            return

        ownership = (
            "Bot所有muteの解除が反映されていません"
            if speaker.user_id in state.bot_muted_ids
            else "手動server muteが残っています"
        )
        error = RuntimeError(f"{speaker.display_name}: {ownership}")
        await self._stop_for_durability_error(
            f"ターン話者 {speaker.display_name} の発言可否確認（{ownership}）",
            error,
        )
        raise StateDurabilityError(
            "ターン話者がミュート中のため持ち時間を開始できません"
        )

    async def _clear_speaker(self) -> None:
        """発言枠の終了: 全員を再びミュートへ戻す。

        誰が話していたかに関係なく「生存者全員ミュート」へ収束させるだけなので、
        話者のMemberは受け取らない (渡しても使い道が無く、呼び出し側に
        speaker.member を持たせる制約だけが残るため)。
        """
        changed = await self._sync_server_mutes(set())
        await self._wait_for_mute_sync_or_pause(
            changed, set(), "発言者の再ミュート"
        )

    async def _prepare_game_vc_permissions(self, context: str) -> None:
        """観戦者denyと生存者allowの両方を必須化し、失敗時は安全停止する。"""
        try:
            await self._restrict_vc_for_game()
            await self._grant_alive_vc_access()
        except Exception as error:
            await self._stop_for_durability_error(context, error)
            if isinstance(error, StateDurabilityError):
                raise
            raise StateDurabilityError(f"{context}に失敗しました") from error

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
            raise RuntimeError("ゲームVCまたはサーバー情報を確認できません")

        default_overwrite = vc.overwrites_for(state.guild.default_role)
        gm_member: Optional[discord.Member] = None
        gm_overwrite: Optional[discord.PermissionOverwrite] = None
        captured_manual_value = False

        # ねいとくん村などの手動管理卓では、ゲーム中に一時変更する2項目だけ
        # 開始前の三値を保存する。保存をDiscord PATCHより先に確定させれば、
        # 直後にプロセスが落ちても次回起動で元へ戻せる。
        if (
            self.uses_manual_static_permissions()
            and not state.vc_default_permissions_captured
        ):
            state.vc_default_permissions_captured = True
            state.vc_default_speak_before_game = default_overwrite.speak
            state.vc_default_send_before_game = default_overwrite.send_messages
            captured_manual_value = True

        # GMは参加者兼任でも個別に発言許可する。手動管理卓では
        # 既存overwriteを丸ごと置換せず、speakだけを一時変更して元値を保存する。
        if state.gm_id is not None:
            gm_member = state.guild.get_member(state.gm_id)
            if gm_member is not None:
                gm_overwrite = vc.overwrites_for(gm_member)
                if (
                    self.uses_manual_static_permissions()
                    and not state.vc_gm_speak_captured
                ):
                    state.vc_gm_speak_captured = True
                    state.vc_gm_speak_user_id = gm_member.id
                    state.vc_gm_speak_before_game = gm_overwrite.speak
                    captured_manual_value = True

        if captured_manual_value:
            await self._persist_room_state()

        default_overwrite.speak = False
        default_overwrite.send_messages = False
        await self._paced_discord_api_call(
            vc.set_permissions, state.guild.default_role,
            overwrite=default_overwrite,
            reason="人狼: ゲーム中の観戦者発言禁止",
        )

        if gm_member is not None and gm_overwrite is not None:
            try:
                if self.uses_manual_static_permissions():
                    gm_overwrite.speak = True
                    await self._paced_discord_api_call(
                        vc.set_permissions, gm_member,
                        overwrite=gm_overwrite,
                        reason="人狼: GM発言許可",
                    )
                else:
                    await self._paced_discord_api_call(
                        vc.set_permissions, gm_member,
                        speak=True, reason="人狼: GM発言許可",
                    )
            except (discord.Forbidden, discord.HTTPException) as e:
                raise RuntimeError(
                    f"GMのVC発言許可を設定できません ({state.room_name})"
                ) from e

    async def _release_vc_after_game(self) -> None:
        """ゲーム終了時に@everyoneの発言制限だけを解除する (表示権限は維持)。

        自動管理卓は従来どおり未設定へ戻す。手動管理卓は
        _restrict_vc_for_game 前に保存した三値へ戻し、Discordで設定していた
        speak/send_messagesやGM個別overwriteの他項目を失わせない。
        """
        state = self.state
        vc = state.voice_channel
        if vc is None or state.guild is None:
            return
        snapshot_changed = False
        try:
            ow = vc.overwrites_for(state.guild.default_role)
            if state.vc_default_permissions_captured:
                ow.speak = state.vc_default_speak_before_game
                ow.send_messages = state.vc_default_send_before_game
            else:
                ow.speak = None
                ow.send_messages = None
            await self._paced_discord_api_call(
                vc.set_permissions, state.guild.default_role,
                overwrite=None if ow.is_empty() else ow,
                reason="人狼: ゲーム終了で発言制限解除",
            )
            if state.vc_default_permissions_captured:
                state.vc_default_permissions_captured = False
                state.vc_default_speak_before_game = None
                state.vc_default_send_before_game = None
                snapshot_changed = True
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"VC発言制限解除失敗 ({state.room_name}): {e}")

        if state.vc_gm_speak_captured and state.vc_gm_speak_user_id is not None:
            gm_target = next(
                (
                    target for target in vc.overwrites
                    if getattr(target, "id", None) == state.vc_gm_speak_user_id
                ),
                state.guild.get_member(state.vc_gm_speak_user_id),
            )
            if gm_target is not None:
                try:
                    gm_ow = vc.overwrites_for(gm_target)
                    gm_ow.speak = state.vc_gm_speak_before_game
                    await self._paced_discord_api_call(
                        vc.set_permissions,
                        gm_target,
                        overwrite=None if gm_ow.is_empty() else gm_ow,
                        reason="人狼: ゲーム終了でGM発言許可を復元",
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"GM発言許可復元失敗 ({state.room_name}): {e}")
                else:
                    state.vc_gm_speak_captured = False
                    state.vc_gm_speak_user_id = None
                    state.vc_gm_speak_before_game = None
                    snapshot_changed = True

        if snapshot_changed:
            try:
                await self._persist_room_state()
            except Exception as e:
                # Discord側は既に開始前へ戻っている。次回の復元処理は
                # 同じ値を再適用するだけなので、終了処理全体は止めない。
                log.exception("VC手動権限の復元済み状態を保存できません: %s", e)

    async def _mute_all(
        self, *, skip_ids: Optional[set[int]] = None
    ) -> list[tuple[discord.Member, bool]]:
        """VC接続中の全員をミュート (参加者兼任を含むGMを除く)。"""
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

            # 手動管理卓の個別overwriteは _release_vc_after_game がspeakだけを
            # 元値へ戻す。ここで丸ごと削除すると手動設定まで失うため触らない。
            manual_vc_permissions = (
                self.uses_manual_static_permissions()
                or state.vc_gm_speak_captured
            )
            perm_targets = (
                [] if manual_vc_permissions
                else [p.member for p in state.players.values()]
            )
            # 自動管理卓では、参加者の個別上書きも含めてゲーム用設定を撤去する。
            # GMのspeakはcapture済みなら _release_vc_after_game が元値へ戻す。
            if (
                not manual_vc_permissions
                and state.gm_id is not None
                and state.gm_id not in state.players
                and state.guild
            ):
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

    async def _restore_nicknames(
        self,
        state: Optional[GameState] = None,
        *,
        member_ids: Optional[set[int]] = None,
    ) -> dict[int, Optional[str]]:
        """名前を開始前へ戻し、戻せなかった対象だけを返す。"""
        # force_end が self.state を差し替えた後に旧stateの改名を戻すケースが
        # あるため、明示指定できるようにする (省略時は現在のstate)
        state = state or self.state
        failures: dict[int, Optional[str]] = {}
        marker = (
            discord.utils.get(
                state.guild.roles,
                name=f"{MUTE_MARKER_ROLE_PREFIX}{state.room_id}",
            )
            if state.guild is not None else None
        )
        async def restore_one(member_id: int, nick: Optional[str]) -> None:
            if state.guild is None:
                failures[member_id] = nick
                return
            member = state.guild.get_member(member_id)
            if member is None:
                # キャッシュ不在・一時退出を成功扱いにせず、次のLOBBYへ持ち越す。
                failures[member_id] = nick
                return
            if member.bot:
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
                    kwargs["roles"] = [
                        role for role in member_roles_for_edit(member)
                        if getattr(role, "id", None) != marker.id
                    ]
            if not kwargs:
                return
            nickname_needed = "nick" in kwargs
            try:
                updated = await self._paced_discord_api_call(member.edit, **kwargs)
                player = state.players.get(member_id)
                if player is not None and getattr(updated, "id", None) == member_id:
                    player.member = updated
                if "mute" in kwargs or "roles" in kwargs:
                    state.bot_muted_ids.discard(member_id)
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"ニックネーム復元失敗 (ID:{member_id}, nick:{nick}): {e}")
                if not nickname_needed:
                    # mute/マーカーロールだけの失敗は既存のVC復元台帳で扱う。
                    return
                try:
                    # mute・rolesとの統合PATCHだけが失敗した場合にも、名前だけは
                    # 個別に回収する。二度目も失敗した対象だけを次村へ持ち越す。
                    updated = await self._paced_discord_api_call(
                        member.edit,
                        nick=nick,
                    )
                    player = state.players.get(member_id)
                    if player is not None and getattr(updated, "id", None) == member_id:
                        player.member = updated
                except (discord.Forbidden, discord.HTTPException) as nick_error:
                    failures[member_id] = nick
                    log.warning(
                        "ニックネーム単独復元も失敗 (ID:%s, nick:%s): %s",
                        member_id,
                        nick,
                        nick_error,
                    )

        restore_ids = (
            set(member_ids)
            if member_ids is not None
            else set(state.players) | set(state.original_nicknames)
        )
        for member_id in restore_ids:
            player = state.players.get(member_id)
            nickname = state.original_nicknames.get(
                member_id,
                player.original_nickname if player is not None else None,
            )
            await restore_one(member_id, nickname)
        return failures

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

    # ============================================================
    # 人狼予想 (霊界を開ける前の数分だけDMで受け付ける)
    # ============================================================

    def _should_hold_spirit(self, method: str) -> bool:
        """人狼予想のために #霊界 の解放を待つべき死亡かどうか。

        処刑死・襲撃死は陣営に関係なく同じ挙動にする。除外、復元をまたいだ
        受付、勝敗確定と同時の死亡は待たない。
        """
        state = self.state
        if (
            state.ending
            or state.pending_winner is not None
            or state.recovered_from_restart
        ):
            return False
        if method not in BONUS_WOLF_GUESS_DEATH_CAUSES:
            return False
        # player.alive=Falseを永続化した後に呼ばれるため、ここで判定すれば
        # この死亡による勝敗確定も含められる。
        if state.check_win() is not None:
            return False
        return state.spirit_channel is not None

    def _hold_spirit_for_guess(self, player_id: int, death_event_id: str) -> None:
        """霊界の解放を保留し、時間切れで自動解放するタイマーを仕掛ける"""
        state = self.state
        if (
            player_id in state.spirit_hold_ids
            and state.spirit_hold_events.get(player_id) == death_event_id
        ):
            return
        state.spirit_hold_ids.add(player_id)
        state.spirit_hold_events[player_id] = death_event_id

        async def release_later() -> None:
            try:
                await asyncio.sleep(WOLF_GUESS_TIMEOUT)
                await self._release_spirit_hold(player_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"霊界保留の自動解除に失敗 (ID:{player_id}): {e}")

        task = asyncio.create_task(release_later())
        self._spirit_release_tasks.add(task)
        task.add_done_callback(self._spirit_release_tasks.discard)

    async def submit_wolf_guess(
        self,
        player_id: int,
        targets: list[int],
        *,
        game_run_id: str,
        death_event_id: str,
    ) -> bool:
        """変種ごとの人数で人狼予想を凍結する。受付外ならFalse。"""
        state = self.state
        unique_targets = sorted({int(target) for target in targets})
        async with self.action_lock:
            valid_target_ids = set(state.players) - {player_id}
            if (
                not self.is_current_game_view(game_run_id)
                or state.spirit_hold_events.get(player_id) != death_event_id
                or player_id not in state.spirit_hold_ids
                or player_id in state.wolf_guesses
                or len(unique_targets) != self.variant.wolf_guess_slots
                or not set(unique_targets) <= valid_target_ids
            ):
                return False
            state.wolf_guesses[player_id] = unique_targets
            # 予想の中身 (誰を挙げたか) は state だけだと試合終了で消え、
            # game_players.wolf_guess_hits に正解数しか残らない。1人1行で
            # 追記して後から精度・傾向を追えるようにする。seqは先に払い出し、
            # persist に含めてから書く (仕様§0-8: state が durable になって
            # からDBへ追記する)。
            old_record_event_seq = state.record_event_seq
            state.record_event_seq += len(unique_targets)
            try:
                await self._persist_room_state()
            except Exception as e:
                state.wolf_guesses.pop(player_id, None)
                state.record_event_seq = old_record_event_seq
                log.warning(f"人狼予想の保存に失敗 (ID:{player_id}): {e}")
                return False
            actor = state.get_player(player_id)
            try:
                for offset, target_id in enumerate(unique_targets, start=1):
                    target = state.get_player(target_id)
                    await database.record_night_action(
                        state.guild.id, state.room_id, state.game_run_id,
                        event_seq=old_record_event_seq + offset,
                        night_number=state.day_number,
                        actor_id=player_id,
                        actor_number=actor.number if actor is not None else 0,
                        actor_role=(
                            actor.role.value
                            if actor is not None and actor.role is not None
                            else "不明"
                        ),
                        action="人狼予想",
                        target_id=target_id,
                        target_number=target.number if target is not None else None,
                        result=(
                            ("人狼" if target.is_wolf else "村人")
                            if target is not None else None
                        ),
                    )
            except Exception as error:
                log.exception(f"人狼予想のDB記録に失敗 ({state.room_name}): {error}")
        await self._release_spirit_hold(player_id)
        return True

    async def _release_spirit_hold(self, player_id: int) -> None:
        state = self.state
        if player_id not in state.spirit_hold_ids:
            state.spirit_hold_events.pop(player_id, None)
            return
        state.spirit_hold_ids.discard(player_id)
        state.spirit_hold_events.pop(player_id, None)
        player = state.get_player(player_id)
        if player is not None and player.member is not None:
            await self._open_spirit_for(player.member)
            await self._safe_spirit_send(
                f"👻 **{player.display_name}** が霊界へやってきました。"
            )
        try:
            await self._persist_room_state()
        except Exception as e:
            log.warning(f"霊界保留の解除保存に失敗 (ID:{player_id}): {e}")

    async def _release_all_spirit_holds(self) -> None:
        """保留を全部解放する。ゲーム終了時と復元時に呼ぶ。

        再起動をまたいでまで提出を待たない。待つと、その間に霊界の話を
        聞けてしまい提出の意味がなくなる (未提出のぶんは0点で確定)。

        自動解除タイマーもここで畳む。残しておくと、2分以内に次の村が
        始まった場合に前の村のタイマーが新しい保留を解いてしまう。
        """
        for task in list(self._spirit_release_tasks):
            task.cancel()
        self._spirit_release_tasks.clear()
        for player_id in list(self.state.spirit_hold_ids):
            await self._release_spirit_hold(player_id)
        self.state.spirit_hold_events.clear()

    def _spirit_member_overwrite(
        self,
        member: discord.Member,
        *,
        blocked: bool,
    ) -> Optional[discord.PermissionOverwrite]:
        """霊界の個人denyだけを変更し、他の手動bitを保つ。"""
        ch = self.state.spirit_channel
        if ch is None:
            return None
        if blocked:
            overwrite = ch.overwrites_for(member)
            overwrite.view_channel = False
            overwrite.read_messages = False
            return overwrite
        if not self.uses_manual_static_permissions():
            return None

        # 手動管理卓はカテゴリの現在値を静的な正本とする。ゲーム中に運営が
        # 別bitを調整していても消さず、Botが伏せた閲覧2項目だけ戻す。
        overwrite = ch.overwrites_for(member)
        category = self.state.category
        base = (
            category.overwrites_for(member)
            if category is not None
            else discord.PermissionOverwrite()
        )
        overwrite.view_channel = base.view_channel
        overwrite.read_messages = base.read_messages
        return None if overwrite.is_empty() else overwrite

    async def _open_spirit_for(self, member: discord.Member) -> None:
        """死亡/除外したメンバーの #霊界 閲覧ブロック (メンバー個別上書き) を解除する"""
        ch = self.state.spirit_channel
        if ch is None:
            return
        try:
            await self._discord_api_call(
                ch.set_permissions,
                member,
                overwrite=self._spirit_member_overwrite(member, blocked=False),
                reason="人狼: 死亡により霊界を開放",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"霊界開放失敗 ({member.display_name}): {e}")

    async def _apply_spirit_blocks(self, *, required: bool = False) -> None:
        """#霊界 の生存者ブロックを現在の生死に合わせて再適用する (復元時の冪等処理)"""
        ch = self.state.spirit_channel
        if ch is None:
            if required:
                raise RuntimeError("#霊界を確認できないため閲覧制御を再適用できません")
            return
        for player in self.state.players.values():
            overwrite = self._spirit_member_overwrite(
                player.member,
                blocked=player.alive,
            )
            try:
                await self._paced_discord_api_call(
                    ch.set_permissions, player.member, overwrite=overwrite,
                    reason="人狼: 復元時の霊界権限再適用",
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                if required:
                    raise RuntimeError(
                        f"霊界権限を安全に再適用できません ({player.display_name})"
                    ) from e
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
        表示は30秒ごと → 残り60秒から5秒ごと → 残り30秒から毎秒。

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
            if timer_should_update(display, last_display):
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
                    overwrite=self._spirit_member_overwrite(
                        member, blocked=True,
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
        # ロビー中の退出: GM枠/参加枠を解放してUIを更新する
        # (これがないと、退出した人が GM/参加者のままロックされ
        #  ボット再起動以外で復旧できなくなる)。募集の開催反映と同じlockで
        # 再判定し、確定rosterを並行して欠員状態へ書き換えない。
        async with self.action_lock:
            state = self.state
            if state.phase == Phase.LOBBY:
                old_gm_id = state.gm_id
                old_players = dict(state.players)
                changed = False
                if state.gm_id == member.id:
                    state.gm_id = None
                    changed = True
                if member.id in state.players:
                    del state.players[member.id]
                    changed = True
                if changed:
                    try:
                        await self._persist_room_state()
                    except Exception as e:
                        state.gm_id = old_gm_id
                        state.players = old_players
                        log.warning(f"ロビー退出状態の保存失敗: {e}")
                        return
                    try:
                        await self._post_lobby_ui()
                    except Exception as e:
                        log.warning(f"ロビー退出後のUI更新失敗: {e}")
                        self._schedule_lobby_panel_recovery(
                            state,
                            recruitment_id=state.recruitment_id,
                            log_label="ロビー退出",
                        )
                return

        state = self.state

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
            await self.force_end(
                "GMが退出したためゲームを中断します。",
                preserve_gm=False,
            )
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
