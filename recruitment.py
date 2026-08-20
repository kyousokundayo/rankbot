"""GM名前村の募集・予約・開催反映を安全に管理する。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord

import database
import rating as rating_lib
from config import (
    CH_LOBBY,
    CH_OPERATIONS,
    CH_OPERATIONS_LOG,
    OPERATIONS_CATEGORY_NAME,
    OPERATIONS_LOG_ROLE_NAMES,
    OPERATIONS_STAFF_ROLE_NAMES,
    OPERATIONS_STAFF_USER_IDS,
    PLAYER_BLOCK_LIMIT,
    PRIVATE_ROOM_CREATOR_ROLE_LABEL,
    PRIVATE_ROOM_CREATOR_ROLE_NAMES,
    RECRUITMENT_CALL_DM_DAILY_LIMIT,
    RECRUITMENT_CALL_DM_INTERVAL_SECONDS,
    RECRUITMENT_CONTACT_COOLDOWN_SECONDS,
    RECRUITMENT_IMMEDIATE_LEAD_MINUTES,
    RECRUITMENT_MAX_DAYS_AHEAD,
    RECRUITMENT_NOTIFICATION_WINDOW_MINUTES,
    RECRUITMENT_RANK_OPTIONS,
    RECRUITMENT_NOTIFICATION_ROLE_NAME,
    RECRUITMENT_UNRANKED_LABEL,
    ROOM_DEFINITION_MAP,
    VARIANT_DEFINITIONS,
    USER_VISIBLE_VARIANT_IDS,
    Phase,
    private_room_limit_for_roles,
)
from models import Player, parse_select_id

log = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")
WEEKDAY_JA = "月火水木金土日"
# 開催日セレクトで「今すぐ」を表すマーカー。日付ISO文字列とは衝突しない値にする
IMMEDIATE_DATE_VALUE = "now"
_NOTIFICATION_ROLE_META_KEY = "recruitment_notification_role_id"

# 旧固定卓の区分は常設カテゴリとして復活させず、名前村の募集条件として
# 再利用する。未戦績者は旧初心者卓で暫定ブロンズ扱いだったため、初心者
# プリセットにだけ「ランク未設定」を明示的に含める。
_RANK_PRESET_RANKS: dict[str, frozenset[str]] = {
    "all": frozenset(),
    "beginner": frozenset(
        ROOM_DEFINITION_MAP["beginner"].allowed_ranks or ()
    ) | {RECRUITMENT_UNRANKED_LABEL},
    "intermediate": frozenset(
        ROOM_DEFINITION_MAP["intermediate"].allowed_ranks or ()
    ),
    "advanced": frozenset(
        ROOM_DEFINITION_MAP["advanced"].allowed_ranks or ()
    ),
}
_RANK_PRESET_LABELS = {
    "all": "制限なし",
    "beginner": "初心者（未設定・アイアン〜シルバー）",
    "intermediate": "中級者（ゴールド〜エメラルド）",
    "advanced": "上級者（ダイヤ〜グランドマスター）",
    "custom": "個別に指定",
}


def _rank_preset_for_allowed_ranks(allowed_ranks: object) -> str:
    selected = frozenset(allowed_ranks or ())
    for preset, ranks in _RANK_PRESET_RANKS.items():
        if selected == ranks:
            return preset
    return "custom"


def _rank_condition_text(allowed_ranks: Optional[frozenset[str]]) -> str:
    if allowed_ranks is None:
        return _RANK_PRESET_LABELS["all"]
    if not allowed_ranks:
        return "設定エラー（参加不可）"
    preset = _rank_preset_for_allowed_ranks(allowed_ranks)
    if preset != "custom":
        return _RANK_PRESET_LABELS[preset]
    labels = []
    for rank_name in RECRUITMENT_RANK_OPTIONS:
        if rank_name not in allowed_ranks:
            continue
        emoji = (
            "❔" if rank_name == RECRUITMENT_UNRANKED_LABEL
            else rating_lib.get_rank_emoji_by_name(rank_name)
        )
        labels.append(f"{emoji} {rank_name}")
    return " / ".join(labels)

def _has_private_room_creator_role(member: object) -> bool:
    """GMまたは仮GMのGM村・募集管理権限を確認する。"""
    role_names = {
        getattr(role, "name", None) for role in getattr(member, "roles", ())
    }
    return bool(PRIVATE_ROOM_CREATOR_ROLE_NAMES & role_names)


def _strict_access_role_names(room) -> frozenset[str]:
    """管理権限で迂回しない、卓の限定ロール名を返す。"""
    return frozenset(getattr(room, "strict_access_role_names", None) or ())


def _strict_room_access_error(
    guild: discord.Guild,
    room,
    member: discord.Member,
    *,
    action: str,
) -> Optional[str]:
    """厳格ロール限定卓での操作資格を確認する。

    権限上書きの作成時と同じく、同名ロールの重複・欠損は安全側に倒す。
    ``manage_guild`` は通常のローカル卓だけのバイパスであり、ここでは
    一切参照しない。
    """
    required = _strict_access_role_names(room)
    if not required:
        return None

    matches_by_name: dict[str, list] = {name: [] for name in required}
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


class RecruitmentSnapshotMismatch(RuntimeError):
    """保存済み募集ルールと現在設定が一致せず、安全に続行できない。"""


def _recruitment_is_disabled(row: dict) -> bool:
    """無効固定卓または非公開変種の募集をfail-closedにする。"""
    room_id = str(row.get("room_id") or "")
    static_room = ROOM_DEFINITION_MAP.get(room_id)
    return (
        (static_room is not None and not getattr(static_room, "enabled", True))
        or str(row.get("variant_id") or "") not in USER_VISIBLE_VARIANT_IDS
    )


def _recruitment_snapshot(row: dict, game_cog=None):
    """予約時snapshotを正本として返し、現在設定との不一致は拒否する。"""
    room_id = str(row.get("room_id") or "")
    room = ROOM_DEFINITION_MAP.get(room_id)
    if room is None and game_cog is not None:
        runner = getattr(game_cog, "rooms", {}).get(room_id)
        room = getattr(runner, "room_def", None)
    if room is None:
        raise RecruitmentSnapshotMismatch("対象卓の現在設定が見つかりません。")
    variant_id = str(row.get("variant_id") or "")
    variant = VARIANT_DEFINITIONS.get(variant_id)
    if variant is None:
        raise RecruitmentSnapshotMismatch("募集作成時の変種設定を確認できません。")
    try:
        capacity = int(row["capacity"])
        backup_capacity = int(row["backup_capacity"])
        occupancy_minutes = int(row["occupancy_minutes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RecruitmentSnapshotMismatch(
            "募集作成時の定員・占有時間を確認できません。"
        ) from exc
    if min(capacity, backup_capacity, occupancy_minutes) <= 0:
        raise RecruitmentSnapshotMismatch("募集作成時の定員・占有時間が不正です。")
    if (
        room.variant_id != variant_id
        or variant.player_count != capacity
        or variant.recruitment_occupancy_minutes != occupancy_minutes
    ):
        raise RecruitmentSnapshotMismatch(
            "募集作成時のルールと現在の卓設定が一致しません。"
            "安全のため、この募集は操作できません。"
        )
    return room, variant, capacity, backup_capacity, occupancy_minutes


def _utc_datetime(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _schedule_out_of_range(local_start: datetime, now: datetime) -> bool:
    """開催日時が「今より後、RECRUITMENT_MAX_DAYS_AHEAD 日以内」から外れるか。

    上限は**日付単位**で判定する。日時で比較すると、日付セレクトが提示した
    最終日 (now + MAX日) のうち「今の時刻より後」だけが弾かれ、10:00に開いた
    人には最終日の11:00以降が選べないという食い違いが起きる。
    """
    if local_start <= now:
        return True
    last_day = (now + timedelta(days=RECRUITMENT_MAX_DAYS_AHEAD)).date()
    return local_start.date() > last_day


def _display_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member is not None else f"ID:{user_id}"


def _plain_identity(guild: discord.Guild, user_id: int) -> str:
    """運営通知用。メンションを絶対に作らない。"""
    return f"{_display_name(guild, user_id)} (ID: {user_id})"


def _as_quoted_block(text: object, limit: int) -> str:
    """利用者が書いた文章をコードブロックへ収める。

    ``` をそのまま通すとブロックを閉じて外へ抜けられる。抜けられると
    「報告者: 〇〇」のような**通知本体に見える文面を報告者が自作できる**ため、
    運営が読み違えないよう潰しておく。メンションの心配ではない
    (到達範囲はチャンネルの可視性で閉じている)。
    """
    body = str(text or "").strip()
    if not body:
        return ""
    if len(body) > limit:
        body = body[:limit] + "…"
    return "```\n" + body.replace("`", "'") + "\n```"


class RecruitmentManager:
    def __init__(self, bot, game_cog) -> None:
        self.bot = bot
        self.game_cog = game_cog
        self.channel: Optional[discord.TextChannel] = None
        self.operations_channel: Optional[discord.TextChannel] = None
        self.operations_log_channel: Optional[discord.TextChannel] = None
        self.lock = asyncio.Lock()
        # 定期巡回と、通知後に参加・補欠繰上げとなった人への即時補完が
        # 同じ利用者へ並行送信しないよう直列化する。
        self.notification_lock = asyncio.Lock()
        # 通知ロール作成と自己付与/解除を直列化する。作成直後はGatewayキャッシュへ
        # 反映されるまで guild.roles に見えないことがあるため、Role自体も保持する。
        self.notification_role_lock = asyncio.Lock()
        self._notification_role: Optional[discord.Role] = None
        # add_roles/remove_roles成功直後はGatewayのmember.roles反映が遅れるため、
        # 短時間だけ直前の確定状態を正として二度押しを正しいトグルにする。
        self._notification_membership_intent: dict[int, tuple[bool, float]] = {}
        # publish_new_recruitment が同一プロセスで再試行されても二重通知しない。
        # 再起動時は既存募集をpublishし直さないため、DB列を増やす必要はない。
        self._role_pinged_recruitment_ids: set[int] = set()
        # 募集ごとの「参加者へ一括連絡」の最終送信時刻 (monotonic秒)。
        # 1回で最大17人へDMが飛ぶため、連打・誤操作で参加者のDMが
        # 埋まらないよう間隔を空ける。主催者だけが使う機能なので、
        # 再起動をまたぐ厳密さは要らずメモリ保持で足りる。
        self._contact_sent_at: dict[int, float] = {}
        # 「募集」ボタンのDM送信専用ペーサー (実装仕様 v0.51 §3-2)。
        # 全卓共有の bulk_api_lock (paced_discord_api_call) は使わない。
        # ゲーム開始時のロール付与・ミュートを数十秒待たせてしまうため、
        # ここだけ独立した直列化と間隔を持たせる。
        self._call_dm_semaphore = asyncio.Semaphore(1)
        self._call_dm_last_sent: float = 0.0
        # 「募集」DM配信タスクへの強参照 (room_runner._spirit_release_tasks と
        # 同じ手法)。asyncio.create_task の戻り値を握らないと、300人規模で
        # 210秒超かかる配信の途中でGCに回収されうる (指摘1)。
        self._call_delivery_tasks: set[asyncio.Task] = set()
        # 現在配信中の call_id 集合 (指摘2)。定期ループの再開処理はこれを見て、
        # まだ配信タスクが走っている call_id を二重に spawn しない。
        # (行単位の重複は claim_recruitment_call_delivery のDB側ガードで防ぐが、
        #  そもそも同じ call_id へ2つ目の配信タスクを立てないことを二重の安全策とする)
        self._active_call_ids: set[int] = set()

    async def start_village_creation(self, interaction: discord.Interaction) -> None:
        """GM村と、その#参加受付に置く募集カードの一体作成を開始する。"""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "サーバー内でのみ使えます。", ephemeral=True
            )
        if not _has_private_room_creator_role(interaction.user):
            return await interaction.response.send_message(
                f"村と募集を作成できるのは **{PRIVATE_ROOM_CREATOR_ROLE_LABEL}** "
                "ロール保持者だけです。",
                ephemeral=True,
            )
        own_rooms = await database.list_private_rooms_by_owner(
            interaction.guild.id, interaction.user.id
        )
        if own_rooms:
            # 受付中の募集を持つ村は次の募集を出せない。全部が受付中で、かつ
            # 村の上限にも達しているときだけ、フォームを開く前に止める
            # (残りの判定は ensure_gm_village_for_recruitment が行う)。
            busy_room_ids = {
                str(row["room_id"])
                for row in await database.list_active_recruitments_for_room_ids(
                    interaction.guild.id, [row["room_id"] for row in own_rooms],
                )
            }
            free_rooms = [
                row for row in own_rooms
                if str(row["room_id"]) not in busy_room_ids
            ]
            limit = private_room_limit_for_roles(
                role.name for role in getattr(interaction.user, "roles", ())
            )
            if not free_rooms and len(own_rooms) >= limit:
                names = "、".join(row["room_name"] for row in own_rooms)
                return await interaction.response.send_message(
                    f"作成できるGM村{limit}個すべてで募集を受け付けています（{names}）。"
                    "新しく作るには、どれかの村を削除するか、"
                    "その村の #参加受付 から募集を締め切ってください。",
                    ephemeral=True,
                )
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.user.send(
                "GM村の募集作成DM確認です。開催案内や成立通知をこのDMへ送ります。"
            )
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.followup.send(
                "DMを受け取れないため作成できません。DMを開放してください。",
                ephemeral=True,
            )
        await interaction.followup.send(
            "開催日、時刻、ゲーム形式、配信の有無を順に選択してください。",
            view=RecruitmentScheduleView(self, interaction.user.id),
            ephemeral=True,
        )

    def _room_for_row(self, row: dict):
        return self.game_cog.rooms.get(str(row.get("room_id") or ""))

    def _lobby_for_row(self, row: dict) -> Optional[discord.TextChannel]:
        room = self._room_for_row(row)
        channel = getattr(getattr(room, "state", None), "lobby_channel", None)
        return channel if isinstance(channel, discord.TextChannel) else channel

    async def _ensure_notification_role(
        self, guild: discord.Guild,
    ) -> Optional[discord.Role]:
        """権限を持たない安全な @通知 ロールをIDで再利用する。"""
        async with self.notification_role_lock:
            cached = self._notification_role
            if cached is not None and getattr(cached, "guild", guild) is guild:
                if self._notification_role_safety_error(guild, cached) is None:
                    return cached
                self._notification_role = None

            stored_id = await database.get_meta(guild.id, _NOTIFICATION_ROLE_META_KEY)
            if stored_id and str(stored_id).isdigit():
                get_role = getattr(guild, "get_role", None)
                stored_role = (
                    get_role(int(stored_id)) if callable(get_role) else None
                )
                if stored_role is not None:
                    error = self._notification_role_safety_error(guild, stored_role)
                    if error is not None:
                        log.error("保存済み@%sロールを採用しません: %s", RECRUITMENT_NOTIFICATION_ROLE_NAME, error)
                        return None
                    self._notification_role = stored_role
                    return stored_role
                await database.set_meta(guild.id, _NOTIFICATION_ROLE_META_KEY, "")

            matches = [
                role for role in guild.roles
                if role.name == RECRUITMENT_NOTIFICATION_ROLE_NAME
            ]
            if matches:
                if len(matches) > 1:
                    log.error(
                        "%s ロールが%d個あるため、安全に1つへ特定できません。",
                        RECRUITMENT_NOTIFICATION_ROLE_NAME,
                        len(matches),
                    )
                    return None
                role = matches[0]
                error = self._notification_role_safety_error(guild, role)
                if error is not None:
                    log.error("既存@%sロールを自己付与用に採用しません: %s", RECRUITMENT_NOTIFICATION_ROLE_NAME, error)
                    return None
                self._notification_role = role
                await database.set_meta(
                    guild.id, _NOTIFICATION_ROLE_META_KEY, str(role.id),
                )
                return role

            try:
                role = await self.game_cog.paced_discord_api_call(
                    guild.create_role,
                    name=RECRUITMENT_NOTIFICATION_ROLE_NAME,
                    permissions=discord.Permissions.none(),
                    hoist=False,
                    mentionable=False,
                    reason="募集通知の自己選択ロール作成",
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.error("@%s ロールを作成できません: %s", RECRUITMENT_NOTIFICATION_ROLE_NAME, exc)
                return None
            error = self._notification_role_safety_error(guild, role)
            if error is not None:
                log.error("作成した@%sロールを安全確認できません: %s", RECRUITMENT_NOTIFICATION_ROLE_NAME, error)
                return None
            self._notification_role = role
            await database.set_meta(
                guild.id, _NOTIFICATION_ROLE_META_KEY, str(role.id),
            )
            return role

    @staticmethod
    def _notification_role_safety_error(
        guild: discord.Guild, role: discord.Role,
    ) -> Optional[str]:
        """自己付与しても権限昇格しないロールだけを許可する。"""
        if role.name != RECRUITMENT_NOTIFICATION_ROLE_NAME:
            return "保存IDのロール名が通知ではありません"
        if getattr(role, "managed", True):
            return "外部連携が管理するロールです"
        default_role = getattr(guild, "default_role", None)
        if getattr(default_role, "id", None) == getattr(role, "id", None):
            return "@everyoneは通知ロールにできません"
        permissions = getattr(role, "permissions", None)
        if permissions is None or int(getattr(permissions, "value", -1)) != 0:
            return "サーバー権限を持っています"
        is_assignable = getattr(role, "is_assignable", None)
        if callable(is_assignable) and not is_assignable():
            return "Botより上位などの理由で付与できません"
        for channel in getattr(guild, "channels", ()):
            overwrites = getattr(channel, "overwrites", None)
            if not isinstance(overwrites, Mapping):
                continue
            for target, overwrite in overwrites.items():
                if getattr(target, "id", None) != role.id:
                    continue
                if not isinstance(overwrite, discord.PermissionOverwrite):
                    return f"#{getattr(channel, 'name', '?')}の権限を確認できません"
                allow, _deny = overwrite.pair()
                if allow.value:
                    return f"#{getattr(channel, 'name', '?')}で個別権限を許可されています"
        return None

    def _cached_notification_role_membership(
        self, role: discord.Role, member: discord.Member,
    ) -> bool:
        """直後の連打でも安定するよう、15秒以内の自己変更意図を優先して返す。"""
        cached_intent = self._notification_membership_intent.get(member.id)
        if cached_intent is not None and time.monotonic() - cached_intent[1] < 15:
            return cached_intent[0]
        return any(
            getattr(member_role, "id", None) == role.id
            for member_role in member.roles
        )

    async def _apply_notification_role_membership(
        self,
        guild: discord.Guild,
        member: discord.Member,
        *,
        desired: Optional[bool] = None,
    ) -> Optional[bool]:
        """@通知 ロールの所持状態を desired に合わせる (Noneなら現状を反転)。

        実際に確定した状態 (bool) を返す。安全なロールを用意できない、
        またはDiscord API呼び出しに失敗した場合は None を返す。
        現状とdesiredが一致していればAPIは呼ばずそのまま返す。
        """
        role = await self._ensure_notification_role(guild)
        if role is None:
            return None
        async with self.notification_role_lock:
            has_role = self._cached_notification_role_membership(role, member)
            target = (not has_role) if desired is None else desired
            if has_role != target:
                try:
                    if target:
                        await self.game_cog.paced_discord_api_call(
                            member.add_roles,
                            role,
                            reason="本人が募集通知をON",
                        )
                    else:
                        await self.game_cog.paced_discord_api_call(
                            member.remove_roles,
                            role,
                            reason="本人が募集通知をOFF",
                        )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("@%s ロールの自己変更に失敗: %s", RECRUITMENT_NOTIFICATION_ROLE_NAME, exc)
                    return None
            self._notification_membership_intent[member.id] = (target, time.monotonic())
            return target

    async def toggle_notification_role(
        self, interaction: discord.Interaction,
    ) -> None:
        """ボタンを押した本人の @通知 ロールを付与または解除する。"""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "サーバー内でのみ使えます。", ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        role = await self._ensure_notification_role(interaction.guild)
        if role is None:
            return await interaction.followup.send(
                "安全な通知ロールを用意できませんでした。Botのロール管理権限と並び順を確認してください。",
                ephemeral=True,
            )
        # ここは設定パネルの「村ができた時に通知」と同じ@通知ロールを操作する
        # もう一つの入口。マスターOFFのまま押すと同じ穴 (指摘4) が開くため、
        # ロールを新たに付ける方向のときだけ先にマスターの許可を求める。
        # OFFへ向かう操作 (既にロール持ち→外す) は常に許可する。
        prefs = await database.get_user_notification_prefs(
            interaction.guild.id, interaction.user.id,
        )
        has_role = self._cached_notification_role_membership(role, interaction.user)
        if not has_role and not prefs["allow_notifications"]:
            return await interaction.followup.send(
                "先に設定パネルの「通知の許可」をONにしてください。", ephemeral=True,
            )
        new_state = await self._apply_notification_role_membership(
            interaction.guild, interaction.user, desired=None,
        )
        if new_state is None:
            return await interaction.followup.send(
                "通知設定を変更できませんでした。Botのロール管理権限と並び順を確認してください。",
                ephemeral=True,
            )
        state = "ON" if new_state else "OFF"
        await interaction.followup.send(
            f"🔔 募集通知を **{state}** にしました。",
            ephemeral=True,
        )

    async def send_notification_settings_panel(
        self, interaction: discord.Interaction,
    ) -> None:
        """募集カードの「通知」ボタンから、3チェックの設定パネルを表示する。

        ロール状態と prefs.notify_on_create が食い違っていたら、ロールを
        正としてこの時点で prefs 側を合わせる (実装仕様 v0.51 §3-1)。
        """
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "サーバー内でのみ使えます。", ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, member = interaction.guild, interaction.user
        prefs = await database.get_user_notification_prefs(guild.id, member.id)
        role = await self._ensure_notification_role(guild)
        if role is not None:
            has_role = self._cached_notification_role_membership(role, member)
            if has_role != prefs["notify_on_create"]:
                prefs = await database.set_user_notification_prefs(
                    guild.id, member.id, notify_on_create=has_role,
                )
        view = RecruitmentNotificationSettingsView(self, guild.id, member.id, prefs)
        await interaction.followup.send(
            content=view.render_content(), view=view, ephemeral=True,
        )

    async def send_recruitment_call(
        self, interaction: discord.Interaction, recruitment_id: int,
    ) -> None:
        """募集カードの「募集」ボタン: DM購読者へこの村を知らせる。

        1村1日1回は open_recruitment_call の UNIQUE 制約 (recruitment_id,
        called_on) で担保する。宛先の絞り込みとエラーEmbed判定を終えたら、
        実際の送信は専用ペーサーでバックグラウンド実行し、押した本人には
        即座に人数だけ返す (実装仕様 v0.51 §3-2)。
        """
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "サーバー内でのみ使えます。", ephemeral=True,
            )
        guild, member = interaction.guild, interaction.user
        row = await database.get_recruitment(recruitment_id)
        if row is None or member.id != row["host_id"]:
            return await interaction.response.send_message(
                "主催者だけ操作できます。", ephemeral=True,
            )
        if row["status"] != database.RECRUITMENT_OPEN:
            return await interaction.response.send_message(
                "受付中の募集だけ通知を呼べます。", ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        called_on = datetime.now(JST).date().isoformat()
        call_id = await database.open_recruitment_call(
            recruitment_id, guild.id, member.id, called_on,
        )
        if call_id is None:
            return await interaction.followup.send(
                "この村の募集通知は今日すでに送りました。", ephemeral=True,
            )
        embed = await self.build_embed(guild, recruitment_id)
        if embed.color == discord.Color.red():
            # 募集情報の取得自体が壊れているため、1通も送らずに中止する。
            # ここで枠を確保したままだと同日中は二度と押せなくなるので解放する
            # (親レビュー指摘1)。
            log.error("募集通知DMをエラーEmbedのため中止 (%s)", recruitment_id)
            await database.release_recruitment_call(call_id)
            return await interaction.followup.send(
                "募集情報を取得できなかったため、通知を送信しませんでした。"
                "枠は消費していません。あとで押し直せます。",
                ephemeral=True,
            )
        recipients = await self._resolve_call_recipients(
            guild, recruitment_id, member.id, call_id,
        )
        if not recipients:
            # 宛先0人 (購読者不在・全員除外など) のまま枠だけ消費されるのを防ぐ
            # (親レビュー指摘1)。まだ誰にも送っていないので安全に解放できる。
            await database.release_recruitment_call(call_id)
            return await interaction.followup.send(
                "通知できる宛先が0人でした。枠は消費していません。あとで押し直せます。",
                ephemeral=True,
            )
        await interaction.followup.send(
            f"{len(recipients)}人へ順次送信します。", ephemeral=True,
        )
        self._spawn_call_delivery(guild, call_id, called_on, embed, recipients)

    async def _resolve_call_recipients(
        self, guild: discord.Guild, recruitment_id: int, host_id: int, call_id: int,
    ) -> list[discord.Member]:
        """「募集」DMの宛先を絞り込む (押下時・再開時の両方から使う)。

        再開時 (指摘2) もこの絞り込みをそのまま再計算する。送達台帳
        (recruitment_call_deliveries) に記録済みの人は
        list_pending_call_recipient_ids で自然に除外されるため、
        二重送信の心配なく毎回全候補から計算し直してよい。
        """
        entries = await database.list_recruitment_entries(recruitment_id)
        excluded = {e["user_id"] for e in entries}
        excluded.add(host_id)
        subscriber_ids = await database.list_call_dm_subscriber_ids(guild.id)
        candidate_ids = [uid for uid in subscriber_ids if uid not in excluded]
        # 主催者を同村拒否している人 (blocker=候補, blocked=主催者) を除く。
        block_pairs = await database.list_player_blocks_between(
            guild.id, [host_id, *candidate_ids],
        )
        blocked_host = {
            blocker for blocker, blocked in block_pairs if blocked == host_id
        }
        candidate_ids = [uid for uid in candidate_ids if uid not in blocked_host]
        # 送達台帳に既に記録済みの人を除く (この call_id への再送を弾く)。
        candidate_ids = await database.list_pending_call_recipient_ids(
            call_id, candidate_ids,
        )
        return [
            target for target in (guild.get_member(uid) for uid in candidate_ids)
            if target is not None
        ]

    def _spawn_call_delivery(
        self,
        guild: discord.Guild,
        call_id: int,
        called_on: str,
        embed: discord.Embed,
        recipients: list[discord.Member],
    ) -> None:
        """募集通知DM配信タスクを起動し、GCで消えないよう強参照を保持する (指摘1)。"""
        self._active_call_ids.add(call_id)
        task = asyncio.create_task(
            self._deliver_recruitment_call(guild, call_id, called_on, embed, recipients)
        )
        self._call_delivery_tasks.add(task)

        def _done(_task: asyncio.Task, call_id: int = call_id) -> None:
            self._call_delivery_tasks.discard(_task)
            self._active_call_ids.discard(call_id)

        task.add_done_callback(_done)

    async def _deliver_recruitment_call(
        self,
        guild: discord.Guild,
        call_id: int,
        called_on: str,
        embed: discord.Embed,
        recipients: list[discord.Member],
    ) -> None:
        """募集通知DMを専用ペーサーで順次送る (バックグラウンドタスク)。

        失敗しても募集全体の進行は止めない (実装仕様の既定方針)。
        Forbiddenは恒久抑止、HTTPExceptionは次回再試行可として記録を分ける。
        """
        sent = 0
        try:
            for target in recipients:
                notified_at = datetime.now(timezone.utc).isoformat()
                try:
                    # 実際に送る「前」に送達台帳へ席を確保する (指摘2)。
                    # 同じ call_id への配信が (再開処理などで) 並行して走っていても、
                    # この INSERT OR IGNORE で勝てるのはどちらか一方だけなので
                    # 二重送信にならない。取れなかった場合は既に他方が担当中/送達
                    # 済みなので、ここでは何もせず次の受信者へ進む。
                    claimed = await database.claim_recruitment_call_delivery(
                        call_id, target.id, notified_at,
                    )
                except Exception:
                    log.exception("募集通知DM送達席の確保に失敗 (call_id=%s, user=%s)", call_id, target.id)
                    continue
                if not claimed:
                    continue
                try:
                    sent_today = await database.count_call_dms_sent_today(
                        guild.id, target.id, called_on,
                    )
                except Exception:
                    log.exception("募集通知DM上限確認に失敗 (call_id=%s, user=%s)", call_id, target.id)
                    continue
                if sent_today >= RECRUITMENT_CALL_DM_DAILY_LIMIT:
                    try:
                        await database.mark_recruitment_call_delivery(
                            call_id, target.id, notified_at, "skipped_cap",
                        )
                    except Exception:
                        log.exception("募集通知DM上限記録に失敗 (call_id=%s, user=%s)", call_id, target.id)
                    continue
                status: Optional[str] = None
                try:
                    await self._send_call_dm_paced(target, embed)
                except asyncio.CancelledError:
                    # キャンセルは握り潰さず、配信ループ自体を止める。
                    raise
                except discord.Forbidden:
                    status = "forbidden"
                except discord.HTTPException as exc:
                    log.warning("募集通知DM送信失敗 (call_id=%s, user=%s): %s", call_id, target.id, exc)
                    status = "failed"
                except Exception:
                    # discord.py起因以外 (ネットワーク断など) でforループを
                    # 抜けて残り全員へ届かなくなるのを防ぐ (指摘3)。1人失敗
                    # しても記録だけ残し、次の受信者へ進む。
                    log.exception("募集通知DM送信で想定外の例外 (call_id=%s, user=%s)", call_id, target.id)
                    status = "failed"
                else:
                    status = "sent"
                    sent += 1
                try:
                    await database.mark_recruitment_call_delivery(
                        call_id, target.id, notified_at, status,
                    )
                except Exception:
                    log.exception("募集通知DM送達記録に失敗 (call_id=%s, user=%s)", call_id, target.id)
        finally:
            try:
                await database.set_recruitment_call_recipients(call_id, sent)
            except Exception:
                log.exception("募集通知DM送信数の記録に失敗 (call_id=%s)", call_id)

    async def _send_call_dm_paced(
        self, member: discord.Member, embed: discord.Embed,
    ) -> None:
        """募集通知DM専用のペーサーで1通送る。

        既存 paced_discord_api_call の bulk_api_lock (全卓共有) は使わない。
        ここは Semaphore(1) と間隔だけの、募集通知DM専用の直列化にする。
        """
        async with self._call_dm_semaphore:
            wait = RECRUITMENT_CALL_DM_INTERVAL_SECONDS - (
                time.monotonic() - self._call_dm_last_sent
            )
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await member.send(embed=embed)
            finally:
                self._call_dm_last_sent = time.monotonic()

    async def _notify_new_recruitment(
        self, guild: discord.Guild, row: dict,
    ) -> None:
        """新しく公開できた募集だけを、安全なAllowedMentionsで1回通知する。"""
        recruitment_id = int(row["id"])
        if recruitment_id in self._role_pinged_recruitment_ids:
            return
        role = await self._ensure_notification_role(guild)
        channel = self._lobby_for_row(row)
        if role is None or channel is None:
            return
        members = getattr(role, "members", None)
        recent_subscription_intent = any(
            desired and time.monotonic() - changed_at < 15
            for desired, changed_at in self._notification_membership_intent.values()
        )
        if members is not None and not members and not recent_subscription_intent:
            # 購読者0人なら通知APIを使わない。後からONにした人へ過去募集を
            # 遡って通知する仕様ではないため、この募集は処理済みにする。
            self._role_pinged_recruitment_ids.add(recruitment_id)
            return
        permissions_for = getattr(channel, "permissions_for", None)
        bot_member = getattr(guild, "me", None)
        if (
            not getattr(role, "mentionable", False)
            and callable(permissions_for)
            and bot_member is not None
            and not permissions_for(bot_member).mention_everyone
        ):
            log.error(
                "@%sを通知できません。Botに『@everyone、@here、すべてのロールにメンション』権限が必要です。",
                RECRUITMENT_NOTIFICATION_ROLE_NAME,
            )
            return
        room = self._room_for_row(row)
        card_message = getattr(getattr(room, "state", None), "lobby_message", None)
        jump_url = getattr(card_message, "jump_url", None)
        link = f"\n{jump_url}" if jump_url else ""
        try:
            await channel.send(
                f"{role.mention} 新しい人狼ゲーム募集が作成されました。"
                f"募集カードから参加できます。{link}",
                allowed_mentions=discord.AllowedMentions(
                    users=False,
                    roles=[role],
                    replied_user=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            # 募集カード自体は公開済みなので、通知だけの失敗で募集を取消さない。
            log.warning("新規募集の@%s通知に失敗 (%s): %s", RECRUITMENT_NOTIFICATION_ROLE_NAME, recruitment_id, exc)
            return
        self._role_pinged_recruitment_ids.add(recruitment_id)

    async def _adopt_operations_channel(
        self,
        channel: discord.TextChannel,
        bot_member: discord.Member,
        *,
        label: str,
    ) -> Optional[discord.TextChannel]:
        """既存チャンネルの手動設定を保ったままBot必須権限だけを補う。

        #運営 と #運営記録 のどちらも「Discordで手動設定した閲覧範囲が正本」で
        扱いが同じなので、判定と1回のPATCHをここへ集約する。
        """
        mapping = getattr(channel, "overwrites", None)
        if not isinstance(mapping, Mapping):
            log.error("#%sの権限上書きを確認できないため採用しません", label)
            return None

        # @everyoneを含む既存のロール・メンバー個別overwriteは、Discord上で
        # 運営者が手動で決めた閲覧範囲としてそのまま保持する。Botが変更する
        # のはBot自身の必要権限だけ。
        desired_overwrites: dict[object, discord.PermissionOverwrite] = dict(mapping)
        for target, overwrite in list(mapping.items()):
            target_id = getattr(target, "id", None)
            if target_id is None or not isinstance(overwrite, discord.PermissionOverwrite):
                log.error(
                    "#%sの権限上書きを解釈できないため採用しません: %s",
                    label,
                    target_id,
                )
                return None

        existing_bot = mapping.get(bot_member)
        if isinstance(existing_bot, discord.PermissionOverwrite):
            bot_overwrite = discord.PermissionOverwrite.from_pair(*existing_bot.pair())
        else:
            bot_overwrite = discord.PermissionOverwrite()
        bot_overwrite.view_channel = True
        bot_overwrite.read_messages = True
        bot_overwrite.read_message_history = True
        bot_overwrite.send_messages = True
        desired_overwrites[bot_member] = bot_overwrite

        # 変更が無ければDiscord APIを呼ばない。変更時は全overwriteを1回の
        # PATCHで置換し、逐次更新の途中状態を運営通知先として採用しない。
        if dict(mapping) == desired_overwrites:
            return channel
        try:
            updated_channel = await channel.edit(
                overwrites=desired_overwrites,
                reason="運営チャンネルの必須権限だけを補完",
            )
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError) as exc:
            log.error("既存#%sのBot必須権限を補完できません: %s", label, exc)
            return None
        return updated_channel or channel

    async def _ensure_operations_channel(
        self, guild: discord.Guild,
    ) -> Optional[discord.TextChannel]:
        category = discord.utils.get(guild.categories, name=OPERATIONS_CATEGORY_NAME)
        if category is None:
            log.warning(
                "既存の開発カテゴリが見つからないため、運営UIと通知を無効化します"
            )
            return None
        bot_member = guild.me
        if bot_member is None:
            log.error("Botメンバーを確認できないため#運営を採用しません")
            return None
        channel = discord.utils.get(guild.text_channels, name=CH_OPERATIONS, category=category)
        if channel is None:
            log.warning(
                "既存の開発/#運営が見つからないため、運営UIと通知を無効化します"
            )
            return None
        return await self._adopt_operations_channel(
            channel, bot_member, label=CH_OPERATIONS,
        )

    def _resolve_operations_log_roles(
        self, guild: discord.Guild,
    ) -> Optional[list[discord.Role]]:
        """#運営記録を新規作成するときの閲覧ロールを一意に解決する。

        同名ロールがあると誰へ許可したのか設定者が判別できないため、
        欠損・重複はどちらも「作らない」で止める (記録は#運営へ残る)。
        """
        if not OPERATIONS_LOG_ROLE_NAMES:
            return None
        matches: dict[str, list[discord.Role]] = {
            name: [] for name in OPERATIONS_LOG_ROLE_NAMES
        }
        for role in getattr(guild, "roles", ()):
            if getattr(role, "name", None) in matches:
                matches[role.name].append(role)
        missing = sorted(name for name, found in matches.items() if not found)
        duplicated = sorted(name for name, found in matches.items() if len(found) > 1)
        if missing or duplicated:
            log.warning(
                "#%sの閲覧ロールを確認できません (見つかりません: %s / 同名複数: %s)",
                CH_OPERATIONS_LOG,
                " ".join(missing) or "なし",
                " ".join(duplicated) or "なし",
            )
            return None
        return [found[0] for found in matches.values()]

    async def _ensure_operations_log_channel(
        self, guild: discord.Guild,
    ) -> Optional[discord.TextChannel]:
        """同村拒否・報告を流す #運営記録 を用意する。

        既にあればDiscord側の手動設定を正本として扱い、無ければ
        OPERATIONS_LOG_ROLE_NAMES のロールだけが読める形で開発カテゴリへ作る。
        用意できない場合はNoneを返し、記録は従来どおり#運営へ流す
        (設定漏れで同村拒否の記録そのものが消えるのを避けるため)。
        """
        category = discord.utils.get(guild.categories, name=OPERATIONS_CATEGORY_NAME)
        if category is None:
            return None
        bot_member = guild.me
        if bot_member is None:
            return None

        channel = None
        stored_channel_id = await database.get_meta(guild.id, "operations_log_channel_id")
        if stored_channel_id and str(stored_channel_id).isdigit():
            candidate = guild.get_channel(int(stored_channel_id))
            if (
                isinstance(candidate, discord.TextChannel)
                and getattr(candidate.category, "id", None) == category.id
            ):
                # 手動で改名されていてもIDで拾い、同じ役目の空チャンネルを
                # 増やさない。
                channel = candidate
        if channel is None:
            channel = discord.utils.get(
                guild.text_channels, name=CH_OPERATIONS_LOG, category=category,
            )
        if channel is not None:
            adopted = await self._adopt_operations_channel(
                channel, bot_member, label=CH_OPERATIONS_LOG,
            )
            if adopted is not None:
                await database.set_meta(
                    guild.id, "operations_log_channel_id", str(adopted.id),
                )
            return adopted

        roles = self._resolve_operations_log_roles(guild)
        if not roles:
            log.warning(
                "#%sを作成できないため、同村拒否・報告は#%sへ流します",
                CH_OPERATIONS_LOG,
                CH_OPERATIONS,
            )
            return None
        overwrites: dict[object, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False, read_messages=False, send_messages=False,
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                read_messages=True,
                read_message_history=True,
                send_messages=True,
            ),
        }
        for role in roles:
            # 読むための場所なので、閲覧ロールにも書き込みは許可しない
            # (記録の間に雑談が挟まると後から追いにくい)。
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                read_messages=True,
                read_message_history=True,
                send_messages=False,
            )
        try:
            created = await guild.create_text_channel(
                CH_OPERATIONS_LOG,
                category=category,
                overwrites=overwrites,
                topic="同村拒否と報告の記録 (書き込みはBotのみ)",
                reason="運営記録チャンネルの作成",
            )
        except (discord.Forbidden, discord.HTTPException, TypeError, ValueError) as exc:
            log.error(
                "#%sを作成できません (記録は#%sへ流します): %s",
                CH_OPERATIONS_LOG,
                CH_OPERATIONS,
                exc,
            )
            return None
        await database.set_meta(
            guild.id, "operations_log_channel_id", str(created.id),
        )
        log.info("#%sを作成しました", CH_OPERATIONS_LOG)
        return created

    def _operations_record_channel(self) -> Optional[discord.TextChannel]:
        """同村拒否・報告の記録先。#運営記録が無ければ#運営へ戻す。"""
        return self.operations_log_channel or self.operations_channel

    async def _upsert_panel(
        self, channel: discord.TextChannel, meta_key: str, *, content: str, view,
    ) -> None:
        message = None
        stored = await database.get_meta(channel.guild.id, meta_key)
        fetch = getattr(channel, "fetch_message", None)
        if stored and str(stored).isdigit() and callable(fetch):
            try:
                message = await fetch(int(stored))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            message = await channel.send(content, view=view)
            await database.set_meta(channel.guild.id, meta_key, str(message.id))
        else:
            await message.edit(content=content, view=view)
        add_view = getattr(self.bot, "add_view", None)
        if callable(add_view):
            add_view(view, message_id=message.id)

    async def setup(self, guild: discord.Guild) -> None:
        """GM村の参加受付へ募集カードを復元する。"""
        self.channel = None
        self.operations_channel = await self._ensure_operations_channel(guild)
        self.operations_log_channel = await self._ensure_operations_log_channel(guild)
        if self.operations_channel is not None:
            await self._upsert_panel(
                self.operations_channel, "operations_home_message_id",
                content="🛠️ **運営メニュー**（運営のみ）",
                view=OperationsView(self),
            )
        expired = await database.archive_expired_recruitments(
            guild.id, datetime.now(timezone.utc)
        )
        for recruitment_id in expired:
            await self.cleanup_archived_recruitment(guild, recruitment_id)

        # HELDへのCAS後、ロビーsnapshot保存前に停止した場合はカードIDが
        # 残る。その狭い窓だけ先に復旧してから、通常のOPENカードを戻す。
        message_rows = await database.list_recruitments_with_messages(guild.id)
        for row in message_rows:
            if _recruitment_is_disabled(row):
                await self._remove_hidden_recruitment_message(row)
                continue
            if row["status"] == database.RECRUITMENT_HELD:
                await self._recover_held_recruitment(guild, row)
            elif row["status"] == database.RECRUITMENT_ARCHIVED:
                await self.cleanup_archived_recruitment(guild, int(row["id"]))

        for row in await database.list_open_recruitments(guild.id):
            room = self._room_for_row(row)
            if _recruitment_is_disabled(row):
                await self._remove_hidden_recruitment_message(row)
                continue
            if room is None or not room.is_private_room():
                raise RuntimeError(
                    "未終了募集のGM名前村を復元できません: "
                    f"募集#{row['id']}/{row['room_id']}"
                )
            await self.ensure_recruitment_message(guild, row)

    async def _recover_held_recruitment(
        self, guild: discord.Guild, row: dict,
    ) -> None:
        """開催確定CASとロビー保存の間で停止した募集を再開する。"""
        if _recruitment_is_disabled(row):
            await self._remove_hidden_recruitment_message(row)
            return
        room = self._room_for_row(row)
        if room is None or not room.is_private_room():
            await self._remove_hidden_recruitment_message(row)
            return
        state = room.state
        recruitment_id = int(row["id"])
        if state.recruitment_id != recruitment_id:
            # 既に別ゲームへ進んだ古いカードを現在ロビーへ戻さない。
            await self._remove_hidden_recruitment_message(row)
            return
        if state.phase != Phase.LOBBY or room._is_game_in_progress():
            await self._remove_hidden_recruitment_message(row)
            return
        try:
            _room_def, _variant, capacity, _backup, _occupancy = (
                _recruitment_snapshot(row, self.game_cog)
            )
        except RecruitmentSnapshotMismatch:
            log.exception("開催確定済み募集の復旧設定が不正: %s", recruitment_id)
            return
        entries = await database.list_recruitment_entries(recruitment_id)
        participant_ids = [
            int(entry["user_id"])
            for entry in entries
            if entry["kind"] == "参加"
        ]
        gm_id = int(row["gm_id"] or row["host_id"])
        desired_ids = set(participant_ids)
        if set(state.players) == desired_ids and state.gm_id == gm_id:
            await room._post_lobby_ui()
            await database.clear_recruitment_message_id(recruitment_id)
            return

        members = [guild.get_member(user_id) for user_id in participant_ids]
        gm = guild.get_member(gm_id)
        recoverable = (
            not state.players
            and state.gm_id in (None, gm_id)
            and len(participant_ids) == capacity
            and all(member is not None for member in members)
            and gm is not None
        )
        if not recoverable:
            reopened = await database.set_recruitment_status(
                recruitment_id,
                database.RECRUITMENT_OPEN,
                expected_status=database.RECRUITMENT_HELD,
            )
            if reopened:
                latest = await database.get_recruitment(recruitment_id)
                if latest is not None:
                    await self.ensure_recruitment_message(guild, latest)
                log.warning(
                    "開催確定後のロビーを復旧できないため募集受付へ戻しました: %s",
                    recruitment_id,
                )
            return

        async with room.action_lock:
            if (
                state.phase != Phase.LOBBY
                or room._is_game_in_progress()
                or state.recruitment_id != recruitment_id
                or state.players
            ):
                return
            state.gm_id = gm_id
            for member in members:
                assert member is not None
                state.players[member.id] = Player(
                    user_id=member.id,
                    member=member,
                    original_nickname=member.nick,
                )
            await room._persist_room_state()
            await room._post_lobby_ui()
            await database.clear_recruitment_message_id(recruitment_id)
        log.info("開催確定済み募集のロビーを復旧しました: %s", recruitment_id)

    async def ensure_recruitment_message(self, guild: discord.Guild, row: dict) -> None:
        if _recruitment_is_disabled(row):
            await self._remove_hidden_recruitment_message(row)
            return
        room = self._room_for_row(row)
        channel = self._lobby_for_row(row)
        if room is None or channel is None:
            raise RuntimeError("募集先のGM村または#参加受付が見つかりません。")
        if not room.is_private_room():
            raise RuntimeError("募集カードはGM村の#参加受付にだけ掲示できます。")
        if row["status"] != database.RECRUITMENT_OPEN:
            if room.state.phase == Phase.LOBBY and not room._is_game_in_progress():
                if room.state.recruitment_id == int(row["id"]):
                    room.state.recruitment_id = None
                    room.state.gm_id = None
                    await room._persist_room_state()
                    await room._post_lobby_ui()
                elif room.state.recruitment_id is None:
                    await room._post_lobby_ui()
            await database.clear_recruitment_message_id(row["id"])
            return
        view = RecruitmentCardView(self, row["id"], active=row["status"] == database.RECRUITMENT_OPEN)
        message = None
        fetch = getattr(channel, "fetch_message", None)
        if row.get("message_id") and callable(fetch):
            try:
                message = await fetch(row["message_id"])
            except discord.NotFound:
                message = None
        embed = await self.build_embed(guild, row["id"])
        if message is None:
            # RoomRunnerが起動時に用意したロビーメッセージをそのまま募集カードへ
            # 置換する。削除→再投稿よりAPIが少なく、参加受付に操作面が2枚残らない。
            candidate = getattr(room.state, "lobby_message", None)
            if candidate is not None:
                try:
                    await candidate.edit(content=None, embed=embed, view=view)
                    message = candidate
                except discord.NotFound:
                    message = None
            if message is None:
                message = await channel.send(embed=embed, view=view)
            try:
                await database.set_recruitment_message_id(row["id"], message.id)
            except Exception:
                # カードだけ公開されDBが追跡できない状態を残さない。
                try:
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    log.exception("DB保存失敗後の募集カード削除にも失敗: %s", row["id"])
                raise
        else:
            await message.edit(embed=embed, view=view)
        room.state.lobby_message = message
        # 起動時にこのカードへ編集で戻れるよう、#参加受付の常設メッセージIDを
        # 更新しておく (再起動のたびに新着を出さないため)。
        remember = getattr(room, "_remember_lobby_message", None)
        if callable(remember):
            await remember(message)
        room.state.recruitment_id = int(row["id"])
        await room._persist_room_state()
        add_view = getattr(self.bot, "add_view", None)
        if callable(add_view) and row["status"] == database.RECRUITMENT_OPEN:
            add_view(view, message_id=message.id)

    async def _remove_hidden_recruitment_message(self, row: dict) -> None:
        """段階導入中卓のカードが既にあれば公開チャンネルから回収する。"""
        channel = self._lobby_for_row(row)
        if channel is None or not row.get("message_id"):
            return
        fetch = getattr(channel, "fetch_message", None)
        if not callable(fetch):
            log.error(
                "段階導入中卓の募集カードを回収できません: %s", row["id"]
            )
            return
        try:
            message = await fetch(row["message_id"])
            await message.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.error(
                "段階導入中卓の募集カード削除失敗 (%s): %s", row["id"], exc,
            )
            return
        await database.clear_recruitment_message_id(row["id"])

    async def publish_new_recruitment(
        self, guild: discord.Guild, recruitment_id: int
    ) -> None:
        """新規募集を掲示し、掲示不能なら予約だけ残さず取り消す。"""
        row = await database.get_recruitment(recruitment_id)
        if row is None:
            raise RuntimeError("作成した募集を読み直せませんでした。")
        room = self._room_for_row(row)
        if _recruitment_is_disabled(row):
            published_error = RuntimeError(
                "非公開のゲーム形式は募集カードを公開できません。"
            )
        elif room is None or not room.is_private_room():
            published_error: Exception = RuntimeError(
                "募集先のGM村が見つからないため公開できません。"
            )
        else:
            try:
                await self.ensure_recruitment_message(guild, row)
            except Exception as exc:
                published_error = exc
                log.exception("新規募集カードの掲示に失敗: %s", recruitment_id)
            else:
                try:
                    await self._notify_new_recruitment(guild, row)
                except Exception:
                    # 通知ロールの不調で、公開済みの募集カードまで取り消さない。
                    log.exception("新規募集のロール通知に失敗: %s", recruitment_id)
                return
        try:
            archived = await database.set_recruitment_status(
                recruitment_id, database.RECRUITMENT_ARCHIVED
            )
        except Exception as exc:
            log.critical("掲示不能な募集の取消にも失敗: %s", recruitment_id)
            raise RuntimeError(
                f"募集カードを掲示できず、募集ID {recruitment_id} の取消確認もできません。"
                "運営者へ連絡してください。"
            ) from exc
        if not archived:
            raise RuntimeError(
                f"募集カードを掲示できず、募集ID {recruitment_id} の状態が変わっています。"
                "運営者へ連絡してください。"
            ) from published_error
        raise RuntimeError(
            "募集カードを掲示できなかったため、予約を残さず作成を取り消しました。"
            "時間を置いてもう一度お試しください。"
        ) from published_error

    async def refresh_message(self, recruitment_id: int) -> None:
        row = await database.get_recruitment(recruitment_id)
        if row is None:
            return
        room = self._room_for_row(row)
        guild = getattr(getattr(room, "state", None), "guild", None)
        if guild is None:
            return
        if row["status"] == database.RECRUITMENT_ARCHIVED:
            await self.cleanup_archived_recruitment(guild, recruitment_id)
            return
        await self.ensure_recruitment_message(guild, row)

    async def _cleanup_archived_recruitment_locked(
        self, guild: discord.Guild, row: dict,
    ) -> None:
        """manager lock保持中に、終了済み募集だけを安全に回収する。"""
        room = self._room_for_row(row)
        if room is None or not room.is_private_room():
            await database.clear_recruitment_message_id(int(row["id"]))
            return
        async with room.action_lock:
            await self.ensure_recruitment_message(guild, row)

    async def cleanup_archived_recruitment(
        self, guild: discord.Guild, recruitment_id: int,
    ) -> None:
        """新募集の作成・参加と直列化して、終了募集の表示と権限を回収する。"""
        async with self.lock:
            row = await database.get_recruitment(recruitment_id)
            if row is None or row["status"] != database.RECRUITMENT_ARCHIVED:
                return
            await self._cleanup_archived_recruitment_locked(guild, row)

    async def archive_recruitment(
        self,
        guild: discord.Guild,
        recruitment_id: int,
        *,
        expected_status: str = database.RECRUITMENT_OPEN,
    ) -> bool:
        """募集のarchiveとカード・参加権限の回収を同じ排他内で完了する。"""
        async with self.lock:
            changed = await database.set_recruitment_status(
                recruitment_id,
                database.RECRUITMENT_ARCHIVED,
                expected_status=expected_status,
            )
            row = await database.get_recruitment(recruitment_id)
            if row is not None and row["status"] == database.RECRUITMENT_ARCHIVED:
                await self._cleanup_archived_recruitment_locked(guild, row)
            return changed

    async def build_embed(self, guild: discord.Guild, recruitment_id: int) -> discord.Embed:
        row = await database.get_recruitment(recruitment_id)
        if row is None:
            return discord.Embed(title="募集が見つかりません", color=discord.Color.red())
        try:
            (
                room, variant, capacity, backup_capacity, occupancy_minutes,
            ) = _recruitment_snapshot(row, self.game_cog)
        except RecruitmentSnapshotMismatch as exc:
            log.error("募集snapshot不整合 (%s): %s", recruitment_id, exc)
            return discord.Embed(
                title="募集設定エラー",
                description=str(exc),
                color=discord.Color.red(),
            )
        entries = await database.list_recruitment_entries(recruitment_id)
        participants = [e["user_id"] for e in entries if e["kind"] == "参加"]
        backups = [e["user_id"] for e in entries if e["kind"] == "補欠"]
        start = _utc_datetime(row["scheduled_at"])
        color = discord.Color.dark_gold() if row["status"] == database.RECRUITMENT_OPEN else discord.Color.dark_grey()
        embed = discord.Embed(
            title=row["title"],
            description=(
                f"主催者: <@{row['host_id']}>\n"
                f"開催: {discord.utils.format_dt(start, style='F')} "
                f"({discord.utils.format_dt(start, style='R')})\n"
                f"卓: **{room.name}** / "
                f"変種: **{variant.label}** / 定員: **{capacity}人**\n"
                f"配信: **{'あり' if row['streaming'] else 'なし'}**\n"
                f"状態: **{row['status']}**"
            ),
            color=color,
        )
        participant_lines = [
            f"`{index:>2}.` {_display_name(guild, uid)}"
            for index, uid in enumerate(participants, 1)
        ]
        embed.add_field(
            name=f"参加者 ({len(participants)}/{capacity})",
            value="\n".join(participant_lines) or "なし", inline=False,
        )
        embed.add_field(
            name=f"補欠 ({len(backups)}/{backup_capacity})",
            value="\n".join(_display_name(guild, uid) for uid in backups) or "なし",
            inline=False,
        )
        gm_text = _display_name(guild, row["gm_id"]) if row["gm_id"] else "未登録（開催時は主催者がGM）"
        embed.add_field(name="GM", value=gm_text, inline=False)
        embed.add_field(
            name="参加条件",
            value=f"参加可能ランク: {_rank_condition_text(row['allowed_ranks'])}",
            inline=False,
        )
        try:
            rank_map = await database.get_current_rank_map(
                guild.id,
                ladder_id=variant.ladder_id,
            )
            rank_names = [
                rank_map[uid].rank_name if uid in rank_map else "ランク未設定"
                for uid in participants
            ]
            if rank_names:
                ordered = sorted(
                    rank_names,
                    key=lambda name: -1 if name == "ランク未設定" else rating_lib.rank_order_value(name),
                )
                embed.add_field(
                    name="参加者のランク範囲",
                    value=f"最低 **{ordered[0]}** / 最高 **{ordered[-1]}**",
                    inline=False,
                )
        except Exception as exc:
            log.warning("募集ランク範囲の表示失敗 (%s): %s", recruitment_id, exc)
        if row["note"]:
            embed.add_field(name="備考", value=row["note"][:1024], inline=False)
        embed.set_footer(text=f"募集ID: {recruitment_id} / 占有時間: {occupancy_minutes}分")
        return embed

    async def validate_candidate(self, guild: discord.Guild, row: dict, member: discord.Member) -> Optional[str]:
        if member.id == guild.owner_id or member.guild_permissions.administrator:
            return "サーバーオーナーと管理者権限保持者はプレイヤー参加できません。"
        access_error = self.validate_existing_card_action(
            guild, row, member, action="参加"
        )
        if access_error:
            return access_error
        try:
            room, variant, _capacity, _backup, _occupancy = _recruitment_snapshot(
                row, self.game_cog
            )
        except RecruitmentSnapshotMismatch as exc:
            log.error("募集snapshot不整合 (%s): %s", row.get("id"), exc)
            return str(exc)
        if room.access_role_names and not _strict_access_role_names(room):
            roles = {role.name for role in member.roles}
            if not member.guild_permissions.manage_guild and not roles.intersection(room.access_role_names):
                return "この卓へ参加するための指定ロールがありません。"
        info = await database.get_player_current_rank_info(
            member.id, guild.id, ladder_id=variant.ladder_id,
        )
        current_rank = info["rank_name"] if info else RECRUITMENT_UNRANKED_LABEL
        room_rank = info["rank_name"] if info else "ブロンズ"
        if room.allowed_ranks is not None and room_rank not in room.allowed_ranks:
            return f"現在ランク **{room_rank}** はこの卓の参加条件外です。"
        allowed_ranks = row["allowed_ranks"]
        if allowed_ranks is not None and current_rank not in allowed_ranks:
            return f"現在ランク **{current_rank}** はこの募集の参加条件外です。"
        return None

    def validate_existing_card_action(
        self,
        guild: discord.Guild,
        row: dict,
        member: discord.Member,
        *,
        action: str,
    ) -> Optional[str]:
        """残った募集カードを操作する資格を再検証する。

        段階導入中へ戻した直後は、Discord APIの一時失敗で古いカード/Viewが
        数分残ることがある。表示を隠すだけでは古いボタンを押せるため、参加・
        GM・主催者操作・開催反映のすべてで同じ境界を通す。
        """
        if _recruitment_is_disabled(row):
            return f"この卓は段階導入中のため{action}できません。"
        try:
            room, _variant, _capacity, _backup, _occupancy = _recruitment_snapshot(
                row, self.game_cog
            )
        except RecruitmentSnapshotMismatch as exc:
            log.error("募集操作をsnapshot不整合で抑止 (%s): %s", row.get("id"), exc)
            return str(exc)
        return _strict_room_access_error(guild, room, member, action=action)

    async def notify_ready_if_needed(self, row: dict) -> None:
        if _recruitment_is_disabled(row):
            log.warning("段階導入中卓の募集成立DMを抑止: %s", row["id"])
            return
        try:
            _room, _variant, capacity, _backup, _occupancy = _recruitment_snapshot(
                row, self.game_cog
            )
        except RecruitmentSnapshotMismatch as exc:
            log.error("募集成立DMをsnapshot不整合で抑止 (%s): %s", row["id"], exc)
            return
        if not await database.recruitment_ready_notification_needed(row["id"]):
            return
        room = self._room_for_row(row)
        guild = getattr(getattr(room, "state", None), "guild", None)
        host = guild.get_member(row["host_id"]) if guild else None
        if host is None:
            return
        try:
            await host.send(
                f"✅ 募集「{row['title']}」は参加者{capacity}人とGMが揃いました。"
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("募集成立DM失敗 (%s): %s", row["id"], exc)
            return
        await database.mark_recruitment_ready_notified(row["id"], datetime.now(timezone.utc))

    async def transfer(self, interaction: discord.Interaction, recruitment_id: int, *, reset_lobby: bool = False) -> str:
        row = await database.get_recruitment(recruitment_id)
        if row is None or row["status"] not in {
            database.RECRUITMENT_OPEN,
            database.RECRUITMENT_HELD,
        }:
            return "この募集は終了しています。"
        if interaction.user.id != row["host_id"]:
            return "ゲームを開始できるのは村主だけです。"
        guild = interaction.guild
        if guild is None:
            return "サーバー内でのみ操作できます。"
        access_error = self.validate_existing_card_action(
            guild, row, interaction.user, action="ゲーム開始"
        )
        if access_error:
            return access_error
        try:
            _room_def, _variant, capacity, _backup, _occupancy = (
                _recruitment_snapshot(row, self.game_cog)
            )
        except RecruitmentSnapshotMismatch as exc:
            log.error("募集の開催反映をsnapshot不整合で抑止 (%s): %s", row["id"], exc)
            return str(exc)
        room = self.game_cog.rooms.get(row["room_id"])
        if room is None:
            return "対象卓が見つかりません。"
        if getattr(room, "_postgame_vote_pending", False):
            return "終了後投票を集計中です。受付終了後にもう一度開始してください。"
        if room.state.phase != Phase.LOBBY or room._is_game_in_progress():
            return "対象卓はゲーム進行中のため開催できません。"
        entries = await database.list_recruitment_entries(recruitment_id)
        participant_ids = [e["user_id"] for e in entries if e["kind"] == "参加"]
        if len(participant_ids) != capacity:
            return f"参加者が揃っていません ({len(participant_ids)}/{capacity})。"
        members: list[discord.Member] = []
        invalid: list[str] = []
        for user_id in participant_ids:
            member = guild.get_member(user_id)
            if member is None:
                invalid.append(f"ID:{user_id}（サーバー不在）")
                continue
            error = await self.validate_candidate(guild, row, member)
            if error:
                invalid.append(f"{member.display_name}: {error}")
            else:
                members.append(member)
        gm_id = row["gm_id"] or row["host_id"]
        gm = guild.get_member(gm_id)
        if gm is None:
            invalid.append(f"GM ID:{gm_id}（サーバー不在）")
        else:
            strict_access_error = _strict_room_access_error(
                guild, _room_def, gm, action="GM登録"
            )
            if strict_access_error:
                invalid.append(f"GM {gm.display_name}: {strict_access_error}")
            else:
                gm_error = await room.validate_gm_claim(gm)
                if gm_error:
                    invalid.append(f"GM {gm.display_name}: {gm_error}")
        if invalid:
            return "開催時の条件確認で開始を中止しました。\n" + "\n".join(f"・{x}" for x in invalid)
        async with room.action_lock:
            state = room.state
            if getattr(room, "_postgame_vote_pending", False):
                return "終了後投票を集計中です。受付終了後にもう一度開始してください。"
            if state.phase != Phase.LOBBY or room._is_game_in_progress():
                return "対象卓の状態が変わったため開始を中止しました。"
            desired_ids = {member.id for member in members}
            already_applied = (
                state.recruitment_id == recruitment_id
                and set(state.players) == desired_ids
                and state.gm_id == gm_id
            )
            if not already_applied:
                # 空判定は長い候補検証の後、通常ロビー操作と同じlock内で
                # 直前に行う。飛び込み参加者を無言clearしない。
                linked_empty_lobby = (
                    not state.players
                    and state.gm_id in (None, gm_id)
                    and state.recruitment_id in (None, recruitment_id)
                )
                if not linked_empty_lobby and (
                    state.players or state.gm_id is not None
                ) and not reset_lobby:
                    return "LOBBY_NOT_EMPTY"
                # 募集を先に開催済みへCASし、参加/取消/別の開催反映を止める。
                # その後のロビー保存が失敗しても、開催済みの同じ募集だけは
                # 開催反映を再試行できるため、DBとDiscordの分断を回収できる。
                if row["status"] == database.RECRUITMENT_OPEN:
                    changed = await database.set_recruitment_status(
                        recruitment_id, database.RECRUITMENT_HELD
                    )
                    if not changed:
                        latest = await database.get_recruitment(recruitment_id)
                        if latest is None or latest["status"] != database.RECRUITMENT_HELD:
                            raise database.RecruitmentConflict(
                                "募集状態が変わったため、卓への開催反映を中止しました。"
                            )
                old_players = dict(state.players)
                old_gm_id = state.gm_id
                old_recruitment_id = state.recruitment_id
                try:
                    state.players.clear()
                    state.gm_id = gm_id
                    state.recruitment_id = recruitment_id
                    for member in members:
                        state.players[member.id] = Player(
                            user_id=member.id, member=member, original_nickname=member.nick,
                        )
                    await room._persist_room_state()
                except Exception:
                    state.players.clear()
                    state.players.update(old_players)
                    state.gm_id = old_gm_id
                    state.recruitment_id = old_recruitment_id
                    raise
            await room._post_lobby_ui()
            await database.clear_recruitment_message_id(recruitment_id)
            if reset_lobby and state.lobby_channel is not None:
                try:
                    await state.lobby_channel.send("募集の開催のため受付をリセットしました。")
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("募集開催時の受付リセット告知失敗: %s", exc)
        return f"✅ **{room.state.room_name}** の参加受付へ{capacity}人とGMを登録しました。"

    @staticmethod
    def _notification_time_eligible(row: dict, now: datetime) -> bool:
        scheduled_at = _utc_datetime(row["scheduled_at"])
        occupancy_minutes = int(row["occupancy_minutes"])
        return (
            scheduled_at <= now + timedelta(minutes=RECRUITMENT_NOTIFICATION_WINDOW_MINUTES)
            and now <= scheduled_at + timedelta(minutes=occupancy_minutes)
        )

    @staticmethod
    async def _send_recruitment_reminder(
        member: discord.Member,
        row: dict,
        now: datetime,
    ) -> None:
        scheduled_at = _utc_datetime(row["scheduled_at"])
        timing_text = (
            "15分以内に開催予定です。"
            if scheduled_at > now
            else "開催予定時刻を迎えています。"
        )
        await member.send(
            f"⏰ 募集「{row['title']}」は{timing_text}\n"
            f"{discord.utils.format_dt(scheduled_at, style='F')}"
        )

    async def remove_entry_with_promotion(
        self,
        guild: discord.Guild,
        recruitment_id: int,
        user_id: int,
    ) -> str:
        """登録を1件外し、補欠繰り上げと開催前DMまで面倒を見る。

        本人の「参加取消」と主催者の「参加者を除外」で共通に使う。補欠繰り上げと
        開催前通知は、卓への開催反映と同じlock内で完了させる——次回巡回を待つと、
        繰り上げ直後に開催済みとなった人だけ通知が漏れる。
        """
        async with self.lock:
            entries = await database.list_recruitment_entries(recruitment_id)
            current_entry = next(
                (entry for entry in entries if entry["user_id"] == user_id),
                None,
            )
            if current_entry is None:
                raise database.RecruitmentConflict("この募集には登録されていません。")
            kind, promoted = await database.remove_recruitment_entry(
                recruitment_id, user_id,
            )
            if promoted is not None:
                member = guild.get_member(promoted)
                if member is not None:
                    try:
                        await member.send("補欠から参加者へ繰り上がりました。")
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        log.warning("補欠繰り上げDM失敗 (%s): %s", promoted, exc)
                    try:
                        await self.notify_participant_if_due(
                            guild, recruitment_id, promoted,
                        )
                    except Exception:
                        # 取消・繰上げはDBへcommit済み。通知台帳の障害で取消
                        # 自体を失敗表示にせず、定期巡回の再試行へ残す。
                        log.exception(
                            "補欠繰上げ直後の開催前DM補完に失敗 (%s/%s)",
                            recruitment_id,
                            promoted,
                        )
        return str(kind)

    async def notify_participant_if_due(
        self,
        guild: discord.Guild,
        recruitment_id: int,
        user_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        """通知開始後に参加・繰上げとなった1人を、その場で台帳へ反映する。"""
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        current_time = current_time.astimezone(timezone.utc)
        async with self.notification_lock:
            try:
                row = await database.get_recruitment(recruitment_id)
                if (
                    row is None
                    or row["status"] not in {
                        database.RECRUITMENT_OPEN,
                        database.RECRUITMENT_HELD,
                    }
                    or _recruitment_is_disabled(row)
                    or not self._notification_time_eligible(row, current_time)
                ):
                    return False
                _recruitment_snapshot(row, self.game_cog)
                pending_ids = (
                    await database.list_pending_recruitment_notification_user_ids(
                        recruitment_id
                    )
                )
                if user_id not in pending_ids:
                    return False
                member = guild.get_member(user_id)
                if member is None:
                    return False
                try:
                    await self._send_recruitment_reminder(member, row, current_time)
                except discord.Forbidden as exc:
                    # DM拒否は同じ設定のまま再試行しても成功しないため、処理済み
                    # として保持し、10分ごとの無駄なAPI呼び出しを止める。
                    await database.mark_recruitment_participant_notified(
                        recruitment_id, user_id, current_time, status="forbidden",
                    )
                    log.warning(
                        "募集直前DM拒否・再試行抑止 (%s/%s): %s",
                        recruitment_id,
                        user_id,
                        exc,
                    )
                    return False
                except discord.HTTPException as exc:
                    log.warning(
                        "募集直前DM失敗 (%s/%s): %s",
                        recruitment_id,
                        user_id,
                        exc,
                    )
                    return False
                await database.mark_recruitment_participant_notified(
                    recruitment_id, user_id, current_time, status="sent",
                )
                return True
            except RecruitmentSnapshotMismatch as exc:
                log.error(
                    "参加直後DMをsnapshot不整合で抑止 (%s): %s",
                    recruitment_id,
                    exc,
                )
                return False
            except Exception:
                # 参加/繰り上げのDB commit後に通知補完だけが失敗しても、
                # interaction全体を失敗表示にしない。定期巡回が再試行する。
                log.exception("参加直後DMの補完処理に失敗: %s/%s", recruitment_id, user_id)
                return False

    async def process_notifications(
        self,
        guild: discord.Guild,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(timezone.utc)
        # setup時の削除がDiscordの一時エラーで失敗しても、公開カードを
        # 30日の通常保存期限まで残さない。定期ループごとに状態を問わず
        # message_id付きの段階導入中卓を再回収する。
        for row in await database.list_recruitments_with_messages(guild.id):
            if _recruitment_is_disabled(row):
                await self._remove_hidden_recruitment_message(row)
            elif row["status"] == database.RECRUITMENT_ARCHIVED:
                await self.cleanup_archived_recruitment(
                    guild, int(row["id"]),
                )
        for row in await database.list_due_recruitment_notifications(guild.id, now):
            if _recruitment_is_disabled(row):
                log.warning("段階導入中卓の開催前DMを抑止: %s", row["id"])
                await self.archive_recruitment(
                    guild,
                    int(row["id"]),
                    expected_status=row["status"],
                )
                continue
            try:
                _recruitment_snapshot(row, self.game_cog)
            except RecruitmentSnapshotMismatch as exc:
                log.error("開催前DMをsnapshot不整合で抑止 (%s): %s", row["id"], exc)
                continue
            participant_ids = (
                await database.list_pending_recruitment_notification_user_ids(row["id"])
            )
            for user_id in participant_ids:
                # 取消/繰上げ/開催反映と同じmanager→notification順で1人ずつ処理。
                # 取消済みへの誤送信を防ぎつつ、全募集のDM中ずっと操作を
                # 止めるlock convoyは避ける。
                async with self.lock:
                    await self.notify_participant_if_due(
                        guild, row["id"], user_id, now=now,
                    )

            # GM警告と募集単位の初回時刻も最新状態をmanager lock内で確認する。
            async with self.lock:
                latest = await database.get_recruitment(row["id"])
                if latest is None or latest["status"] not in {
                    database.RECRUITMENT_OPEN,
                    database.RECRUITMENT_HELD,
                }:
                    continue
                initial_notification = latest["notified_at"] is None
                if (
                    initial_notification
                    and latest["status"] == database.RECRUITMENT_OPEN
                    and latest["gm_id"] is None
                ):
                    host = guild.get_member(latest["host_id"])
                    if host is not None:
                        try:
                            await host.send(
                                f"⚠️ 募集「{latest['title']}」はGM未登録です。"
                                "村主がゲーム開始を押す前にGM状態を確認してください。"
                            )
                        except (discord.Forbidden, discord.HTTPException) as exc:
                            log.warning(
                                "募集GM警告DM失敗 (%s): %s", latest["id"], exc
                            )
                if initial_notification:
                    await database.mark_recruitment_notified(latest["id"], now)
        async with self.lock:
            expired_ids = await database.archive_expired_recruitments(guild.id, now)
        for recruitment_id in expired_ids:
            await self.cleanup_archived_recruitment(guild, recruitment_id)
        await self._resume_stalled_recruitment_calls(guild, now)

    async def _resume_stalled_recruitment_calls(
        self, guild: discord.Guild, now: datetime,
    ) -> None:
        """再起動でGCされた「募集」DM配信を10分周期のこのループから再開する (指摘2)。

        再開してよいと判定する条件 (list_resumable_recruitment_calls に渡す):
          - called_on が当日 (JST)
          - called_at が直近60分以内
          - 募集がまだ OPEN
        スキーマは変更していない。60分を過ぎたものは対象外 = 主催者が
        再度「募集」を押しても当日中は「今日はもう送りました」で弾かれる
        (open_recruitment_callのUNIQUE制約) が、それは指摘2の対応範囲外の
        既知の制約として許容する。宛先は _resolve_call_recipients で
        毎回再計算するが、送達台帳に記録済みの人は自然に除外されるので
        二重送信にはならない。

        なお、この call_id への配信タスクが既にこのプロセスで動いている場合
        (_active_call_ids) は spawn しない。前回tickの配信がまだ終わっていない
        状態でここへ来ても、同じ call_id へ2つ目の配信タスクを立てないための
        ガード。行単位の重複防止は claim_recruitment_call_delivery 側のDB制約が
        担うが、そもそも同じプロセス内で無駄な二重タスクを立てないための対策。
        """
        called_on = now.astimezone(JST).date().isoformat()
        since = now - timedelta(minutes=60)
        try:
            calls = await database.list_resumable_recruitment_calls(
                guild.id, called_on, since,
            )
        except Exception:
            log.exception("募集通知DM再開対象の取得に失敗")
            return
        for call in calls:
            recruitment_id = call["recruitment_id"]
            call_id = call["id"]
            if call_id in self._active_call_ids:
                # 前回tickの配信タスクがまだ走っている。同じ call_id へ2つ目の
                # 配信タスクを立てない (指摘2)。
                continue
            embed = await self.build_embed(guild, recruitment_id)
            if embed.color == discord.Color.red():
                log.warning("募集通知DM再開をエラーEmbedのため抑止 (call_id=%s)", call_id)
                continue
            recipients = await self._resolve_call_recipients(
                guild, recruitment_id, call["host_id"], call_id,
            )
            if not recipients:
                continue
            log.info(
                "募集通知DMを再開します (call_id=%s, recruitment_id=%s, 残り%s人)",
                call_id, recruitment_id, len(recipients),
            )
            self._spawn_call_delivery(guild, call_id, call["called_on"], embed, recipients)

    async def notify_feedback_report(
        self, guild: discord.Guild, report: dict,
    ) -> None:
        """不具合・改善の報告を #運営記録 (無ければ #運営) へ流す。

        本文は一般公開しないが、記録先はDiscordで許可された閲覧者に限られる
        ため中身まで載せる。設定運営ロール・運営ユーザーはメニュー操作の認可
        だけで、#運営の閲覧権限はBotから追加しない。
        (毎回「報告の一覧」を開かないと読めない形だと見落とされるため)。

        **メンションは抑制しない。** 到達範囲はチャンネルの可視性で閉じるので、
        運営限定のここに書かれた @everyone がサーバー全体へ飛ぶことはない。
        報告者の表示にメンションを使わないのは、通知を出さないためではなく
        退出済みでもIDから追えるようにするため (`_plain_identity`)。
        """
        channel = self._operations_record_channel()
        if channel is None:
            return
        lines = [
            f"📮 **報告が届きました**（ID: `{report['report_id']}` / {report['category']}）",
            f"報告者: {_plain_identity(guild, report['user_id'])}",
        ]
        context = " / ".join(
            str(part) for part in (
                report.get("room_name"), report.get("phase"), report.get("bot_version"),
            ) if part
        )
        if context:
            lines.append(f"状況: {context}")
        lines.append(_as_quoted_block(report.get("summary"), 900))
        details = _as_quoted_block(report.get("details"), 500)
        if details:
            lines.append("補足:")
            lines.append(details)
        try:
            await channel.send("\n".join(part for part in lines if part))
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("報告の運営通知失敗: %s", exc)

    async def notify_block_added(
        self, guild: discord.Guild, blocker_id: int, blocked_id: int, count: int,
    ) -> None:
        channel = self._operations_record_channel()
        if channel is None:
            return
        try:
            await channel.send(
                "🚫 **同村拒否が登録されました**\n"
                f"拒否した人: {_plain_identity(guild, blocker_id)}\n"
                f"拒否された人: {_plain_identity(guild, blocked_id)}\n"
                f"登録数: {count} / {PLAYER_BLOCK_LIMIT}"
            )
            if count == PLAYER_BLOCK_LIMIT:
                blocked_ids = await database.list_player_blocks(guild.id, blocker_id)
                lines = [
                    f"{index}. {_plain_identity(guild, user_id)}"
                    for index, user_id in enumerate(blocked_ids, 1)
                ]
                await channel.send(
                    "⚠️ **同村拒否が上限に達しました**\n"
                    f"対象: {_plain_identity(guild, blocker_id)}  {count} / {PLAYER_BLOCK_LIMIT}\n"
                    "拒否リスト:\n" + "\n".join(lines)
                )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("同村拒否の運営通知失敗: %s", exc)


class _DraftSelect(discord.ui.Select):
    def __init__(self, parent: "RecruitmentScheduleView", key: str, **kwargs) -> None:
        self.parent_view = parent
        self.key = key
        super().__init__(**kwargs)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.values[self.key] = self.values[0]
        if self.key == "date":
            # 「今すぐ」なら時刻を選ばせない。選択肢を残すと、選んだ時刻が
            # 無視されるのか反映されるのか分からなくなる
            if self.values[0] == IMMEDIATE_DATE_VALUE:
                self.parent_view.values.pop("hour", None)
                self.parent_view.values.pop("minute", None)
            self.parent_view.rebuild()
        if self.parent_view.is_complete():
            await interaction.response.edit_message(
                content="参加ランク条件を選び、村名・募集タイトル・備考を入力してください。",
                view=RecruitmentOptionsView(
                    self.parent_view.manager,
                    self.parent_view.host_id,
                    self.parent_view.values,
                ),
            )
        else:
            await interaction.response.edit_message(view=self.parent_view)


class RecruitmentScheduleView(discord.ui.View):
    def __init__(
        self,
        manager: RecruitmentManager,
        host_id: int,
    ) -> None:
        super().__init__(timeout=300)
        self.manager = manager
        self.host_id = host_id
        self.values: dict[str, str] = {}
        self.rebuild()

    @property
    def immediate(self) -> bool:
        return self.values.get("date") == IMMEDIATE_DATE_VALUE

    def is_complete(self) -> bool:
        """「今すぐ」は時刻を選ばないので、必須項目が3つになる。"""
        required = {"date", "variant", "streaming"}
        if not self.immediate:
            required |= {"hour", "minute"}
        return required <= self.values.keys()

    def rebuild(self) -> None:
        """「今すぐ」を選んだかどうかで時刻セレクトを出し分ける。"""
        self.clear_items()
        now = datetime.now(JST)
        date_options = [
            discord.SelectOption(
                label=f"今すぐ（約{RECRUITMENT_IMMEDIATE_LEAD_MINUTES}分後に開始）",
                value=IMMEDIATE_DATE_VALUE,
                emoji="⚡",
                default=self.immediate,
            )
        ] + [
            discord.SelectOption(
                label=(
                    (now + timedelta(days=offset)).strftime("%m月%d日")
                    + f" ({WEEKDAY_JA[(now + timedelta(days=offset)).weekday()]})"
                ),
                value=(now + timedelta(days=offset)).date().isoformat(),
                default=(
                    self.values.get("date")
                    == (now + timedelta(days=offset)).date().isoformat()
                ),
            )
            for offset in range(RECRUITMENT_MAX_DAYS_AHEAD + 1)
        ]
        row = 0
        self.add_item(_DraftSelect(self, "date", placeholder="開催日", options=date_options, row=row))
        row += 1
        if not self.immediate:
            self.add_item(_DraftSelect(
                self, "hour", placeholder="開始時（0〜23時）",
                options=[
                    discord.SelectOption(
                        label=f"{hour:02d}時", value=str(hour),
                        default=self.values.get("hour") == str(hour),
                    )
                    for hour in range(24)
                ],
                row=row,
            ))
            row += 1
            self.add_item(_DraftSelect(
                self, "minute", placeholder="開始分",
                options=[
                    discord.SelectOption(
                        label=f"{minute:02d}分", value=str(minute),
                        default=self.values.get("minute") == str(minute),
                    )
                    for minute in (0, 30)
                ],
                row=row,
            ))
            row += 1
        self.add_item(_DraftSelect(
            self, "variant", placeholder="ゲーム形式",
            options=[
                discord.SelectOption(
                    label=VARIANT_DEFINITIONS[variant_id].label,
                    value=variant_id,
                    default=self.values.get("variant") == variant_id,
                )
                for variant_id in USER_VISIBLE_VARIANT_IDS
            ],
            row=row,
        ))
        row += 1
        self.add_item(_DraftSelect(
            self, "streaming", placeholder="配信",
            options=[
                discord.SelectOption(
                    label=label, value=value,
                    default=self.values.get("streaming") == value,
                )
                for label, value in (("配信あり", "1"), ("配信なし", "0"))
            ],
            row=row,
        ))


class _RankOptionSelect(discord.ui.Select):
    def __init__(self, parent: "RecruitmentOptionsView") -> None:
        self.parent_view = parent
        selected = set(parent.values.get("allowed_ranks", []))
        options = []
        for rank_name in RECRUITMENT_RANK_OPTIONS:
            emoji = (
                "❔" if rank_name == RECRUITMENT_UNRANKED_LABEL
                else rating_lib.get_rank_emoji_by_name(rank_name)
            )
            options.append(discord.SelectOption(
                label=rank_name, value=rank_name, emoji=emoji,
                default=rank_name in selected,
            ))
        super().__init__(
            placeholder="個別に参加可能ランクを選択",
            min_values=1,
            max_values=len(RECRUITMENT_RANK_OPTIONS),
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.values["rank_preset"] = "custom"
        self.parent_view.values["allowed_ranks"] = list(self.values)
        await interaction.response.edit_message(view=RecruitmentOptionsView(
            self.parent_view.manager,
            self.parent_view.host_id,
            self.parent_view.values,
        ))


class _RankPresetSelect(discord.ui.Select):
    def __init__(self, parent: "RecruitmentOptionsView") -> None:
        self.parent_view = parent
        current = str(parent.values.get("rank_preset") or "all")
        super().__init__(
            placeholder="参加ランク区分",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=_RANK_PRESET_LABELS[preset],
                    value=preset,
                    default=current == preset,
                )
                for preset in ("all", "beginner", "intermediate", "advanced", "custom")
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        preset = self.values[0]
        self.parent_view.values["rank_preset"] = preset
        if preset in _RANK_PRESET_RANKS:
            self.parent_view.values["allowed_ranks"] = list(
                _RANK_PRESET_RANKS[preset]
            )
        elif not self.parent_view.values.get("allowed_ranks"):
            self.parent_view.values["allowed_ranks"] = []
        await interaction.response.edit_message(
            content="参加ランク条件を確認し、村名・募集内容を入力してください。",
            view=RecruitmentOptionsView(
                self.parent_view.manager,
                self.parent_view.host_id,
                self.parent_view.values,
            ),
        )


class RecruitmentOptionsView(discord.ui.View):
    def __init__(self, manager: RecruitmentManager, host_id: int, values: dict[str, object]) -> None:
        super().__init__(timeout=300)
        self.manager, self.host_id, self.values = manager, host_id, dict(values)
        self.values.setdefault("allowed_ranks", [])
        self.values.setdefault(
            "rank_preset",
            _rank_preset_for_allowed_ranks(self.values["allowed_ranks"]),
        )
        self.add_item(_RankPresetSelect(self))
        if self.values["rank_preset"] == "custom":
            self.add_item(_RankOptionSelect(self))
            if not self.values["allowed_ranks"]:
                for item in self.children:
                    if isinstance(item, discord.ui.Button):
                        item.disabled = True

    @discord.ui.button(label="タイトル・備考を入力", style=discord.ButtonStyle.primary, row=2)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.host_id:
            return await interaction.response.send_message("作成者だけ操作できます。", ephemeral=True)
        if self.values.get("rank_preset") == "custom" and not self.values.get("allowed_ranks"):
            return await interaction.response.send_message(
                "個別指定するランクを1つ以上選んでください。", ephemeral=True,
            )
        await interaction.response.send_modal(RecruitmentCreateModal(self.manager, self.host_id, self.values))


class RecruitmentCreateModal(discord.ui.Modal, title="募集内容"):
    village_name = discord.ui.TextInput(
        label="村名（新しい村を作るときは新しい名前）",
        placeholder="既存の村で募集するならその村名／村が1つなら空欄",
        required=False,
        max_length=90,
    )
    recruitment_title = discord.ui.TextInput(label="募集タイトル", max_length=100)
    note = discord.ui.TextInput(
        label="備考（作成後も変更できます）", required=False,
        max_length=1000, style=discord.TextStyle.paragraph,
    )

    def __init__(self, manager: RecruitmentManager, host_id: int, values: dict[str, object]) -> None:
        super().__init__()
        self.manager, self.host_id, self.values = manager, host_id, dict(values)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.host_id or interaction.guild is None:
            return await interaction.response.send_message("作成者だけ操作できます。", ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or not _has_private_room_creator_role(
            interaction.user
        ):
            return await interaction.response.send_message(
                f"**{PRIVATE_ROOM_CREATOR_ROLE_LABEL}** ロールが無いため作成できません。",
                ephemeral=True,
            )
        now = datetime.now(JST)
        if self.values["date"] == IMMEDIATE_DATE_VALUE:
            # 「今すぐ」。_schedule_out_of_range が「現在より後」を要求するので
            # 0分後にはできない。秒は落として占有区間の端を安定させる
            local_start = (
                now + timedelta(minutes=RECRUITMENT_IMMEDIATE_LEAD_MINUTES)
            ).replace(second=0, microsecond=0)
        else:
            local_start = datetime.fromisoformat(self.values["date"]).replace(
                hour=int(self.values["hour"]), minute=int(self.values["minute"]),
                tzinfo=JST,
            )
        if _schedule_out_of_range(local_start, now):
            return await interaction.response.send_message(
                f"開催日時は現在より後、{RECRUITMENT_MAX_DAYS_AHEAD}日以内を選んでください。",
                ephemeral=True,
            )
        variant_id = str(self.values.get("variant") or "")
        if variant_id not in USER_VISIBLE_VARIANT_IDS:
            return await interaction.response.send_message(
                "ゲーム形式が見つからないため作成できません。", ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)

        # 村作成・募集保存・カード公開を1操作として直列化する。同じGMが
        # フォームを並行送信しても、片方の失敗処理がもう片方の村や募集を
        # 巻き戻さないようにする。
        async with self.manager.lock:
            await self._create_locked(
                interaction,
                interaction.guild,
                interaction.user,
                local_start,
                variant_id,
            )

    async def _create_locked(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        member: discord.Member,
        local_start: datetime,
        variant_id: str,
    ) -> None:
        room = None
        created_room = False
        recruitment_id: Optional[int] = None
        previous_variant_id: Optional[str] = None
        previous_gm_id: Optional[int] = None
        room_state_changed = False

        async def rollback_failed_creation() -> None:
            """今回の作成操作だけを戻し、既存GM村の状態は保持する。"""
            if recruitment_id is not None:
                try:
                    await database.set_recruitment_status(
                        recruitment_id, database.RECRUITMENT_ARCHIVED,
                    )
                    await database.clear_recruitment_message_id(recruitment_id)
                except Exception:
                    log.exception(
                        "失敗した募集のアーカイブにも失敗: %s", recruitment_id
                    )
            if created_room and room is not None:
                try:
                    await self.manager.game_cog.rollback_gm_village_creation(
                        guild, room.state.room_id,
                    )
                except Exception:
                    log.exception(
                        "作成失敗後のGM村回収にも失敗: %s", room.state.room_id
                    )
                return
            if not room_state_changed or room is None or previous_variant_id is None:
                return
            try:
                await database.update_private_room_variant(
                    guild.id, room.state.room_id, previous_variant_id,
                )
                room.room_def = replace(
                    room.room_def, variant_id=previous_variant_id,
                )
                room.state.gm_id = previous_gm_id
                room.state.recruitment_id = None
                await room._persist_room_state()
                await room._post_lobby_ui()
            except Exception:
                log.exception(
                    "作成失敗後の既存GM村復元にも失敗: %s", room.state.room_id
                )

        try:
            room, created_room = await self.manager.game_cog.ensure_gm_village_for_recruitment(
                guild,
                member,
                room_name=str(self.village_name),
                variant_id=variant_id,
            )
            previous_variant_id = room.room_def.variant_id
            previous_gm_id = room.state.gm_id
            recruitment_id = await database.create_recruitment(
                guild.id, self.host_id,
                title=str(self.recruitment_title), scheduled_at=local_start,
                room_id=room.state.room_id,
                variant_id=variant_id,
                streaming=self.values["streaming"] == "1",
                allowed_ranks=(
                    set(self.values.get("allowed_ranks", ())) or None
                ),
                note=str(self.note),
            )
            await database.set_recruitment_gm(
                recruitment_id, self.host_id, expected_gm_id=None,
            )
            await database.update_private_room_and_open_recruitment_variant(
                guild.id,
                room.state.room_id,
                self.host_id,
                variant_id,
            )
            room.room_def = replace(room.room_def, variant_id=variant_id)
            room.state.gm_id = self.host_id
            room.state.recruitment_id = recruitment_id
            room_state_changed = True
            await room._persist_room_state()
        except (database.RecruitmentConflict, RuntimeError, ValueError) as exc:
            await rollback_failed_creation()
            return await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            log.exception("GM村と募集の一体作成に失敗")
            await rollback_failed_creation()
            return await interaction.followup.send(
                "GM村と募集を安全に保存できませんでした。時間を置いて再度お試しください。",
                ephemeral=True,
            )
        try:
            await self.manager.publish_new_recruitment(
                guild, recruitment_id
            )
        except RuntimeError as exc:
            await rollback_failed_creation()
            return await interaction.followup.send(str(exc), ephemeral=True)
        await interaction.followup.send(
            f"✅ GM村 **{room.state.room_name}** と募集を用意しました。"
            f" #{CH_LOBBY} のカードで参加を受け付けます。",
            ephemeral=True,
        )


class RecruitmentNotificationSettingsView(discord.ui.View):
    """通知3チェックの設定パネル (ephemeral)。

    - 通知の許可: マスタースイッチ。OFFにすると @通知 ロールも外す
      (DM購読のnotify_on_callは値を保ったまま、許可OFFで実質無効になる。
      user_notification_prefsのWHERE句がallow_notifications=1を要求するため)。
    - 村ができた時に通知: @通知 ロールの付与/解除
      (`RecruitmentManager._apply_notification_role_membership` を流用)。
    - この村を通知を受け取る: 「募集」ボタンDMの購読フラグ。
    押すたびに1項目だけトグルし、同じephemeralをeditして現在状態を表示する。
    """

    def __init__(
        self,
        manager: RecruitmentManager,
        guild_id: int,
        user_id: int,
        prefs: dict,
    ) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.guild_id = guild_id
        self.user_id = user_id
        self.prefs = dict(prefs)
        self._role_unavailable = False
        self._build_buttons()

    @staticmethod
    def _label(text: str, on: bool) -> str:
        return f"{'✅' if on else '⬜'} {text}"

    def _build_buttons(self) -> None:
        self.clear_items()
        specs = [
            ("通知の許可", "allow_notifications", self.toggle_allow, 0),
            ("村ができた時に通知", "notify_on_create", self.toggle_create, 1),
            ("この村を通知を受け取る", "notify_on_call", self.toggle_call, 2),
        ]
        for text, key, callback, row in specs:
            on = bool(self.prefs.get(key))
            button = discord.ui.Button(
                label=self._label(text, on),
                style=discord.ButtonStyle.primary if on else discord.ButtonStyle.secondary,
                row=row,
            )
            button.callback = callback
            self.add_item(button)

    def render_content(self) -> str:
        base = (
            "🔔 通知設定です。押すたびにON/OFFが切り替わります。\n"
            "「通知の許可」をOFFにすると他の通知もまとめて無効になります。"
        )
        if self._role_unavailable:
            base += (
                "\n⚠️ 安全な@通知ロールを用意できないため、"
                "「村ができた時に通知」は今は変更できません。"
            )
        return base

    async def _authorized(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "この設定パネルは開いた本人だけが操作できます。", ephemeral=True,
            )
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._build_buttons()
        await interaction.edit_original_response(content=self.render_content(), view=self)

    async def toggle_allow(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction):
            return
        await interaction.response.defer()
        desired = not self.prefs["allow_notifications"]
        self.prefs = await database.set_user_notification_prefs(
            self.guild_id, self.user_id, allow_notifications=desired,
        )
        if not desired and isinstance(interaction.guild, discord.Guild):
            new_role_state = await self.manager._apply_notification_role_membership(
                interaction.guild, interaction.user, desired=False,
            )
            if new_role_state is not None:
                self.prefs = await database.set_user_notification_prefs(
                    self.guild_id, self.user_id, notify_on_create=False,
                )
        await self._refresh(interaction)

    async def toggle_create(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction):
            return
        await interaction.response.defer()
        desired = not self.prefs["notify_on_create"]
        if desired and not self.prefs["allow_notifications"]:
            # マスターOFFのままロールを付けると、_notify_new_recruitmentは
            # DB設定を見ずに@通知ロールをメンションするため、UI上OFF表示のまま
            # 実際にはピンを受け取ってしまう (指摘4)。ONにする前にマスターの
            # 許可を求める。トグル自体はせず現在状態のまま返す。
            await interaction.followup.send(
                "先に「通知の許可」をONにしてください。", ephemeral=True,
            )
            return
        new_role_state = None
        if isinstance(interaction.guild, discord.Guild):
            new_role_state = await self.manager._apply_notification_role_membership(
                interaction.guild, interaction.user, desired=desired,
            )
        if new_role_state is None:
            self._role_unavailable = True
        else:
            self.prefs = await database.set_user_notification_prefs(
                self.guild_id, self.user_id, notify_on_create=new_role_state,
            )
        await self._refresh(interaction)

    async def toggle_call(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction):
            return
        await interaction.response.defer()
        desired = not self.prefs["notify_on_call"]
        self.prefs = await database.set_user_notification_prefs(
            self.guild_id, self.user_id, notify_on_call=desired,
        )
        await self._refresh(interaction)


class RecruitmentCardView(discord.ui.View):
    def __init__(self, manager: RecruitmentManager, recruitment_id: int, *, active: bool = True) -> None:
        super().__init__(timeout=None)
        self.manager, self.recruitment_id = manager, recruitment_id
        buttons = [
            ("参加", discord.ButtonStyle.success, "join", self.join, 0),
            ("参加取消", discord.ButtonStyle.danger, "leave", self.leave, 0),
            ("ゲーム開始", discord.ButtonStyle.success, "transfer", self.transfer, 0),
            ("通知", discord.ButtonStyle.primary, "notify", self.notification, 0),
            # 主催者メニューは2段目の先頭に置く。1段目は参加者が使うボタンだけに
            # 揃えたほうが、押す人と押さない人の境目が見た目で分かる。
            ("主催者メニュー", discord.ButtonStyle.secondary, "host", self.host_menu, 1),
            # 受付中のGM村で唯一出ている常設パネルがこのカード。ルール・ヘルプを
            # 載せないと、参加前に規定を読む導線が村の中に無くなる (旧・総合卓は
            # 参加受付のLobbyViewに常時出ていた)。読むだけの操作なので、受付が
            # 閉じたカードでも押せるままにする。
            ("ルール", discord.ButtonStyle.secondary, "rule", self.rule, 1),
            ("ヘルプ", discord.ButtonStyle.secondary, "help", self.help, 1),
            # 主催者だけがDM一斉通知を送るためのボタン。押下可否と1村1日1回の
            # 判定はコールバック側 (send_recruitment_call) で行う。
            ("募集", discord.ButtonStyle.primary, "call", self.call, 1),
        ]
        for label, style, suffix, callback, row in buttons:
            button = discord.ui.Button(
                label=label, style=style, row=row,
                emoji="🔔" if suffix == "notify" else None,
                custom_id=f"recruitment:{recruitment_id}:{suffix}",
                disabled=not active and suffix not in ("rule", "help"),
            )
            button.callback = callback
            self.add_item(button)

    async def _variant_for_card(self) -> Optional[object]:
        """カードの現在の形式を返す。取得できなければ既定表示へ委ねる。"""
        try:
            row = await database.get_recruitment(self.recruitment_id)
        except Exception:
            log.exception("募集カードの形式取得に失敗 (%s)", self.recruitment_id)
            return None
        if row is None:
            return None
        return VARIANT_DEFINITIONS.get(str(row["variant_id"]))

    async def rule(self, interaction: discord.Interaction) -> None:
        from views import build_rule_embeds

        variant = await self._variant_for_card()
        await interaction.response.send_message(
            embeds=build_rule_embeds(variant), ephemeral=True,
        )

    async def help(self, interaction: discord.Interaction) -> None:
        from views import build_help_embeds

        variant = await self._variant_for_card()
        await interaction.response.send_message(
            embeds=build_help_embeds(variant), ephemeral=True,
        )

    async def notification(self, interaction: discord.Interaction) -> None:
        await self.manager.send_notification_settings_panel(interaction)

    async def call(self, interaction: discord.Interaction) -> None:
        await self.manager.send_recruitment_call(interaction, self.recruitment_id)

    async def join(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("サーバー内でのみ使えます。", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await database.get_recruitment(self.recruitment_id)
        if row is None:
            return await interaction.followup.send("募集が見つかりません。", ephemeral=True)
        error = await self.manager.validate_candidate(interaction.guild, row, interaction.user)
        if error:
            return await interaction.followup.send(error, ephemeral=True)
        try:
            await interaction.user.send(f"募集「{row['title']}」への参加DM確認です。")
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.followup.send(
                "DMを受け取れないため参加できません。DMを開放してください。", ephemeral=True,
            )
        try:
            # 卓への開催反映も同じlockを使う。登録から開催前通知までの間に
            # 開催済みへ変わり、後参加者だけ通知されない隙間を作らない。
            async with self.manager.lock:
                # DM確認中に形式・ランク条件が変更されていることがあるため、
                # lock内で最新状態に対してもう一度参加条件を確認する。
                row = await database.get_recruitment(self.recruitment_id)
                if row is None:
                    raise database.RecruitmentConflict("募集が見つかりません。")
                error = await self.manager.validate_candidate(
                    interaction.guild, row, interaction.user,
                )
                if error:
                    raise database.RecruitmentConflict(error)
                kind = await database.add_recruitment_entry(
                    self.recruitment_id, interaction.user.id,
                )
                if kind == "参加":
                    room = self.manager._room_for_row(row)
                    if room is None:
                        await database.remove_recruitment_entry(
                            self.recruitment_id, interaction.user.id
                        )
                        raise database.RecruitmentConflict(
                            "参加先のGM村が見つかりません。"
                        )
                    try:
                        await self.manager.notify_participant_if_due(
                            interaction.guild,
                            self.recruitment_id,
                            interaction.user.id,
                        )
                    except Exception:
                        # 登録はDBへcommit済み。通知台帳の障害で参加自体を
                        # 失敗表示にせず、定期巡回の再試行へ残す。
                        log.exception(
                            "参加直後の開催前DM補完に失敗 (%s/%s)",
                            self.recruitment_id,
                            interaction.user.id,
                        )
        except database.RecruitmentConflict as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        embed = await self.manager.build_embed(interaction.guild, self.recruitment_id)
        await interaction.message.edit(embed=embed, view=self)
        await interaction.followup.send(f"{kind}として登録しました。", ephemeral=True)
        await self.manager.notify_ready_if_needed(await database.get_recruitment(self.recruitment_id))

    async def leave(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "サーバー内でのみ使えます。", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await database.get_recruitment(self.recruitment_id)
        if row is None:
            return await interaction.followup.send("募集が見つかりません。", ephemeral=True)
        access_error = self.manager.validate_existing_card_action(
            interaction.guild, row, interaction.user, action="参加取消"
        )
        if access_error:
            return await interaction.followup.send(access_error, ephemeral=True)
        try:
            await self.manager.remove_entry_with_promotion(
                interaction.guild, self.recruitment_id, interaction.user.id,
            )
        except database.RecruitmentConflict as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        embed = await self.manager.build_embed(interaction.guild, self.recruitment_id)
        await interaction.message.edit(embed=embed, view=self)
        await interaction.followup.send("参加を取り消しました。", ephemeral=True)

    async def transfer(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.manager.lock:
            result = await self.manager.transfer(interaction, self.recruitment_id)
        if result == "LOBBY_NOT_EMPTY":
            return await interaction.followup.send(
                "参加受付に既存の参加者またはGMがいます。無言で解除はしません。",
                view=RecruitmentLobbyResetConfirmView(self.manager, self.recruitment_id, interaction.user.id),
                ephemeral=True,
            )
        if not result.startswith("✅"):
            return await interaction.followup.send(result, ephemeral=True)
        row = await database.get_recruitment(self.recruitment_id)
        room = self.manager._room_for_row(row or {})
        if room is None:
            return await interaction.followup.send(
                "参加者は登録しましたが、GM村を確認できないため開始できません。",
                ephemeral=True,
            )
        await interaction.followup.send("✅ 参加者を確定し、ゲームを開始します。", ephemeral=True)
        # 削除・形式変更と同じroom lockで開始条件をもう一度固定する。
        # manager lockは開始中ずっと保持せず、他村の参加操作を止めない。
        async with room.action_lock:
            await room.start_game(interaction)

    async def host_menu(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "サーバー内でのみ使えます。", ephemeral=True
            )
        row = await database.get_recruitment(self.recruitment_id)
        if row is None or interaction.user.id != row["host_id"]:
            return await interaction.response.send_message("主催者だけ操作できます。", ephemeral=True)
        access_error = self.manager.validate_existing_card_action(
            interaction.guild, row, interaction.user, action="主催者メニューの利用"
        )
        if access_error:
            return await interaction.response.send_message(access_error, ephemeral=True)
        await interaction.response.send_message(
            "主催者メニューです。受付中はここからゲーム形式も変更できます。",
            view=RecruitmentHostView(self.manager, self.recruitment_id, interaction.user.id),
            ephemeral=True,
        )


class RecruitmentLobbyResetConfirmView(discord.ui.View):
    def __init__(self, manager: RecruitmentManager, recruitment_id: int, host_id: int) -> None:
        super().__init__(timeout=120)
        self.manager, self.recruitment_id, self.host_id = manager, recruitment_id, host_id

    @discord.ui.button(label="受付をリセットして開催", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.host_id:
            return await interaction.response.send_message("主催者だけ操作できます。", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.manager.lock:
            result = await self.manager.transfer(interaction, self.recruitment_id, reset_lobby=True)
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="中止", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="開催を中止しました。", view=None)


class RecruitmentHostView(discord.ui.View):
    def __init__(self, manager: RecruitmentManager, recruitment_id: int, host_id: int) -> None:
        super().__init__(timeout=180)
        self.manager, self.recruitment_id, self.host_id = manager, recruitment_id, host_id

    def _allowed(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.host_id

    async def _authorized_row(
        self,
        interaction: discord.Interaction,
        *,
        action: str,
    ) -> tuple[Optional[dict], Optional[str]]:
        """古い主催者Viewでも現在の公開境界を再検証する。"""
        if not self._allowed(interaction):
            return None, "主催者だけ操作できます。"
        if interaction.guild is None:
            return None, "サーバー内でのみ使えます。"
        row = await database.get_recruitment(self.recruitment_id)
        if row is None or row["host_id"] != self.host_id:
            return None, "募集が見つかりません。"
        access_error = self.manager.validate_existing_card_action(
            interaction.guild, row, interaction.user, action=action
        )
        return row, access_error

    @discord.ui.button(label="備考を変更", style=discord.ButtonStyle.secondary)
    async def note(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        row, error = await self._authorized_row(interaction, action="備考変更")
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        assert row is not None
        await interaction.response.send_modal(RecruitmentNoteModal(self.manager, self.recruitment_id, self.host_id, row["note"]))

    @discord.ui.button(label="ゲーム形式を変更", style=discord.ButtonStyle.primary)
    async def change_variant(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        row, error = await self._authorized_row(interaction, action="ゲーム形式の変更")
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        assert row is not None
        await interaction.response.send_message(
            "変更先のゲーム形式を選んでください。",
            view=RecruitmentVariantSelectView(
                self.manager,
                self.recruitment_id,
                self.host_id,
                current_variant_id=str(row["variant_id"]),
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="一括連絡", style=discord.ButtonStyle.primary)
    async def contact(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _row, error = await self._authorized_row(interaction, action="一括連絡")
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        await interaction.response.send_modal(RecruitmentContactModal(self.manager, self.recruitment_id, self.host_id))

    @discord.ui.button(label="参加者を除外", style=discord.ButtonStyle.secondary)
    async def remove_entry(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        row, error = await self._authorized_row(interaction, action="参加者の除外")
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        entries = await database.list_recruitment_entries(self.recruitment_id)
        # 主催者本人は「募集を廃止」で畳む。ここで自分を外せると、GM不在の
        # 募集だけが残って誰も開始できなくなる。
        options = [
            discord.SelectOption(
                label=_display_name(interaction.guild, int(entry["user_id"])),
                description=str(entry["kind"]),
                value=str(entry["user_id"]),
            )
            for entry in entries
            if int(entry["user_id"]) != self.host_id
        ][:25]
        if not options:
            return await interaction.response.send_message(
                "除外できる参加者がいません。", ephemeral=True
            )
        await interaction.response.send_message(
            "除外する参加者を選んでください。選択後に確認が出ます。",
            view=RecruitmentRemoveEntryView(
                self.manager, self.recruitment_id, self.host_id, options,
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="募集を廃止", style=discord.ButtonStyle.danger)
    async def archive(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _row, error = await self._authorized_row(interaction, action="募集の廃止")
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        changed = await self.manager.archive_recruitment(
            interaction.guild, self.recruitment_id,
        )
        await interaction.followup.send(
            "募集をアーカイブしました。" if changed else "募集は既に終了しています。",
            ephemeral=True,
        )


class RecruitmentRemoveEntryView(discord.ui.View):
    """主催者が受付中の登録を1件外す。ロビーの「参加者を除外」に相当する。"""

    def __init__(
        self,
        manager: RecruitmentManager,
        recruitment_id: int,
        host_id: int,
        options: list[discord.SelectOption],
    ) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.recruitment_id = recruitment_id
        self.host_id = host_id
        select = discord.ui.Select(
            placeholder="除外する参加者を選択",
            min_values=1,
            max_values=1,
            options=options,
        )
        select.callback = self._selected
        self.add_item(select)

    async def _selected(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.host_id or interaction.guild is None:
            return await interaction.response.send_message(
                "主催者だけ操作できます。", ephemeral=True
            )
        user_id = parse_select_id(interaction.data["values"][0])
        if user_id is None:
            return await interaction.response.send_message(
                "❌ 不正な選択です。", ephemeral=True
            )
        if user_id == self.host_id:
            return await interaction.response.send_message(
                "主催者自身は除外できません。募集ごと畳む場合は「募集を廃止」を使ってください。",
                ephemeral=True,
            )
        row = await database.get_recruitment(self.recruitment_id)
        if row is None or row["host_id"] != self.host_id:
            return await interaction.response.send_message(
                "募集が見つかりません。", ephemeral=True
            )
        access_error = self.manager.validate_existing_card_action(
            interaction.guild, row, interaction.user, action="参加者の除外",
        )
        if access_error:
            return await interaction.response.send_message(
                access_error, ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            kind = await self.manager.remove_entry_with_promotion(
                interaction.guild, self.recruitment_id, user_id,
            )
        except database.RecruitmentConflict as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        await self.manager.refresh_message(self.recruitment_id)
        # 除外しても再登録は塞がない。繰り返す相手は同村拒否で扱う方が、
        # 主催者ごとの一時的な判断を恒久ルールにせずに済む。
        await interaction.followup.send(
            f"{_display_name(interaction.guild, user_id)} を除外しました ({kind})。\n"
            "本人はもう一度「参加」を押せます。繰り返す場合は同村拒否をご利用ください。",
            ephemeral=True,
        )


class RecruitmentVariantSelectView(discord.ui.View):
    def __init__(
        self,
        manager: RecruitmentManager,
        recruitment_id: int,
        host_id: int,
        *,
        current_variant_id: str,
    ) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.recruitment_id = recruitment_id
        self.host_id = host_id
        select = discord.ui.Select(
            placeholder="ゲーム形式を選択",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=VARIANT_DEFINITIONS[variant_id].label,
                    value=variant_id,
                    default=variant_id == current_variant_id,
                )
                for variant_id in USER_VISIBLE_VARIANT_IDS
            ],
        )
        select.callback = self._selected
        self.add_item(select)

    async def _selected(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.host_id or interaction.guild is None:
            return await interaction.response.send_message(
                "主催者だけ操作できます。", ephemeral=True
            )
        row = await database.get_recruitment(self.recruitment_id)
        if row is None or row["host_id"] != self.host_id:
            return await interaction.response.send_message(
                "募集が見つかりません。", ephemeral=True
            )
        raw = self.children[0].values[0]
        room = self.manager._room_for_row(row)
        if room is None:
            return await interaction.response.send_message(
                "GM村が見つかりません。", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await room.change_lobby_variant(self.host_id, raw)
        await interaction.followup.send(result, ephemeral=True)


class RecruitmentNoteModal(discord.ui.Modal, title="備考を変更"):
    note = discord.ui.TextInput(label="備考", required=False, max_length=1000, style=discord.TextStyle.paragraph)

    def __init__(self, manager: RecruitmentManager, recruitment_id: int, host_id: int, current: str) -> None:
        super().__init__()
        self.manager, self.recruitment_id, self.host_id = manager, recruitment_id, host_id
        self.note.default = current

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "サーバー内でのみ使えます。", ephemeral=True
            )
        row = await database.get_recruitment(self.recruitment_id)
        if row is None or interaction.user.id != self.host_id or row["host_id"] != self.host_id:
            return await interaction.response.send_message("主催者だけ操作できます。", ephemeral=True)
        access_error = self.manager.validate_existing_card_action(
            interaction.guild, row, interaction.user, action="備考変更"
        )
        if access_error:
            return await interaction.response.send_message(access_error, ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await database.update_recruitment_note(self.recruitment_id, self.host_id, str(self.note))
        except database.RecruitmentConflict as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        await self.manager.refresh_message(self.recruitment_id)
        await interaction.followup.send("備考を変更しました。", ephemeral=True)


class RecruitmentContactModal(discord.ui.Modal, title="参加者へ一括連絡"):
    message = discord.ui.TextInput(label="連絡内容", max_length=1500, style=discord.TextStyle.paragraph)

    def __init__(self, manager: RecruitmentManager, recruitment_id: int, host_id: int) -> None:
        super().__init__()
        self.manager, self.recruitment_id, self.host_id = manager, recruitment_id, host_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        row = await database.get_recruitment(self.recruitment_id)
        if row is None or interaction.user.id != self.host_id or interaction.guild is None:
            return await interaction.response.send_message("主催者だけ操作できます。", ephemeral=True)
        access_error = self.manager.validate_existing_card_action(
            interaction.guild, row, interaction.user, action="一括連絡"
        )
        if access_error:
            return await interaction.response.send_message(access_error, ephemeral=True)

        # 連打・誤操作で参加者のDMが埋まらないよう間隔を空ける
        now = time.monotonic()
        last_sent = self.manager._contact_sent_at.get(self.recruitment_id)
        if last_sent is not None:
            elapsed = now - last_sent
            if elapsed < RECRUITMENT_CONTACT_COOLDOWN_SECONDS:
                wait = int(RECRUITMENT_CONTACT_COOLDOWN_SECONDS - elapsed) + 1
                return await interaction.response.send_message(
                    f"⏳ 一括連絡は{RECRUITMENT_CONTACT_COOLDOWN_SECONDS // 60}分に1回までです。"
                    f"あと約{wait}秒お待ちください。",
                    ephemeral=True,
                )
        # 送信前に記録する。DM送信中の再送信も弾く
        self.manager._contact_sent_at[self.recruitment_id] = now

        await interaction.response.defer(ephemeral=True, thinking=True)
        entries = await database.list_recruitment_entries(self.recruitment_id)
        recipients = {e["user_id"] for e in entries}
        if row["gm_id"]:
            recipients.add(row["gm_id"])
        recipients.discard(self.host_id)
        failed = []
        for user_id in recipients:
            member = interaction.guild.get_member(user_id)
            if member is None:
                failed.append(str(user_id))
                continue
            try:
                await member.send(f"📣 募集「{row['title']}」主催者からの連絡\n{self.message}")
            except (discord.Forbidden, discord.HTTPException):
                failed.append(member.display_name)
        text = f"{len(recipients) - len(failed)}人へ送信しました。"
        if failed:
            text += "\n送信できなかった人: " + ", ".join(failed)
        await interaction.followup.send(text, ephemeral=True)


class PlayerBlockSettingsView(discord.ui.View):
    PAGE_SIZE = 25

    def __init__(
        self,
        manager: RecruitmentManager,
        guild_id: int,
        owner_id: int,
        blocked_ids: list[int],
        *,
        add_page: int = 0,
        remove_page: int = 0,
    ) -> None:
        super().__init__(timeout=180)
        self.manager, self.guild_id, self.owner_id = manager, guild_id, owner_id
        self.blocked_ids = list(dict.fromkeys(int(uid) for uid in blocked_ids))
        get_guild = getattr(manager.bot, "get_guild", None)
        self.guild = get_guild(guild_id) if callable(get_guild) else None
        at_limit = len(self.blocked_ids) >= PLAYER_BLOCK_LIMIT

        # DiscordのUserSelectは初期候補を全員分並べないが、文字入力による
        # サーバーメンバー検索ができる。候補一覧も下段で25人ずつ表示する。
        user_select = discord.ui.UserSelect(
            placeholder="名前を入力して拒否ユーザーを検索", min_values=1, max_values=1,
            custom_id="player_blocks:add_search", row=0, disabled=at_limit,
        )
        user_select.callback = self.add_from_search
        self.add_item(user_select)

        candidates = self._candidate_members()
        self.add_page, self.add_page_count, candidate_page = self._page(
            candidates, add_page,
        )
        if candidate_page:
            candidate_select = discord.ui.Select(
                placeholder=(
                    f"候補一覧から追加 ({self.add_page + 1}/{self.add_page_count})"
                ),
                options=[
                    discord.SelectOption(
                        label=str(getattr(member, "display_name", member.id))[:100],
                        description=(
                            f"@{getattr(member, 'name', member.id)} / ID:{member.id}"
                        )[:100],
                        value=str(member.id),
                    )
                    for member in candidate_page
                ],
                custom_id="player_blocks:add_page",
                row=1,
                disabled=at_limit,
            )
            candidate_select.callback = self.add_from_page
            self.add_item(candidate_select)
        else:
            self.add_item(discord.ui.Button(
                label=(
                    "拒否リストが上限です"
                    if at_limit else "追加できる候補がありません"
                ),
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=1,
            ))
        self._add_page_buttons(
            row=2,
            prefix="player_blocks:add",
            page=self.add_page,
            page_count=self.add_page_count,
            previous=self.add_previous,
            following=self.add_next,
        )

        self.remove_page, self.remove_page_count, blocked_page = self._page(
            self.blocked_ids, remove_page,
        )
        if blocked_page:
            options = [
                discord.SelectOption(
                    label=(
                        _display_name(self.guild, uid)[:100]
                        if self.guild else f"ID:{uid}"
                    ),
                    description=f"ID:{uid}",
                    value=str(uid),
                )
                for uid in blocked_page
            ]
            remove_select = discord.ui.Select(
                placeholder=(
                    f"拒否リストから解除 ({self.remove_page + 1}/{self.remove_page_count})"
                ),
                options=options,
                custom_id="player_blocks:remove",
                row=3,
            )
            remove_select.callback = self.remove
            self.add_item(remove_select)
            self._add_page_buttons(
                row=4,
                prefix="player_blocks:remove",
                page=self.remove_page,
                page_count=self.remove_page_count,
                previous=self.remove_previous,
                following=self.remove_next,
            )

    @staticmethod
    def _page(values: list, requested_page: int) -> tuple[int, int, list]:
        page_count = max(
            1,
            (len(values) + PlayerBlockSettingsView.PAGE_SIZE - 1)
            // PlayerBlockSettingsView.PAGE_SIZE,
        )
        page = min(max(0, int(requested_page)), page_count - 1)
        start = page * PlayerBlockSettingsView.PAGE_SIZE
        return page, page_count, values[start:start + PlayerBlockSettingsView.PAGE_SIZE]

    def _candidate_members(self) -> list:
        if self.guild is None:
            return []
        blocked = set(self.blocked_ids)
        members = [
            member for member in getattr(self.guild, "members", ())
            if getattr(member, "id", None) != self.owner_id
            and getattr(member, "id", None) not in blocked
            and not bool(getattr(member, "bot", False))
        ]
        return sorted(
            members,
            key=lambda member: (
                str(getattr(member, "display_name", "")).casefold(),
                int(member.id),
            ),
        )

    def _add_page_buttons(
        self,
        *,
        row: int,
        prefix: str,
        page: int,
        page_count: int,
        previous,
        following,
    ) -> None:
        previous_button = discord.ui.Button(
            label="◀",
            style=discord.ButtonStyle.secondary,
            custom_id=f"{prefix}_previous",
            disabled=page <= 0,
            row=row,
        )
        previous_button.callback = previous
        self.add_item(previous_button)
        self.add_item(discord.ui.Button(
            label=f"{page + 1}/{page_count}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
            row=row,
        ))
        next_button = discord.ui.Button(
            label="▶",
            style=discord.ButtonStyle.secondary,
            custom_id=f"{prefix}_next",
            disabled=page >= page_count - 1,
            row=row,
        )
        next_button.callback = following
        self.add_item(next_button)

    @property
    def summary_content(self) -> str:
        text = (
            f"同村拒否リスト: {len(self.blocked_ids)}/{PLAYER_BLOCK_LIMIT}\n"
            "名前検索、または25人ずつの候補一覧から追加できます。"
        )
        if self.blocked_ids:
            text += (
                f"\n候補 {self.add_page + 1}/{self.add_page_count}ページ / "
                f"解除 {self.remove_page + 1}/{self.remove_page_count}ページ"
            )
        else:
            text += f"\n候補 {self.add_page + 1}/{self.add_page_count}ページ"
        return text

    def _authorized_guild(
        self, interaction: discord.Interaction,
    ) -> Optional[discord.Guild]:
        guild = interaction.guild
        if (
            interaction.user.id != self.owner_id
            or guild is None
            or guild.id != self.guild_id
        ):
            return None
        return guild

    async def _refresh(
        self,
        interaction: discord.Interaction,
        *,
        add_page: Optional[int] = None,
        remove_page: Optional[int] = None,
    ) -> None:
        blocked_ids = await database.list_player_blocks(self.guild_id, self.owner_id)
        view = PlayerBlockSettingsView(
            self.manager,
            self.guild_id,
            self.owner_id,
            blocked_ids,
            add_page=self.add_page if add_page is None else add_page,
            remove_page=self.remove_page if remove_page is None else remove_page,
        )
        await interaction.response.edit_message(
            content=view.summary_content,
            view=view,
        )

    async def add_from_search(self, interaction: discord.Interaction) -> None:
        select = discord.utils.get(
            self.children, custom_id="player_blocks:add_search",
        )
        selected = select.values[0] if select is not None and select.values else None
        await self._add_member(interaction, selected)

    async def add_from_page(self, interaction: discord.Interaction) -> None:
        select = discord.utils.get(
            self.children, custom_id="player_blocks:add_page",
        )
        selected_id = parse_select_id(
            select.values[0] if select is not None and select.values else None
        )
        guild = self._authorized_guild(interaction)
        member = guild.get_member(selected_id) if guild is not None and selected_id is not None else None
        await self._add_member(interaction, member)

    async def _add_member(
        self, interaction: discord.Interaction, member: object,
    ) -> None:
        guild = self._authorized_guild(interaction)
        if guild is None:
            return await interaction.response.send_message(
                "本人だけ操作できます。", ephemeral=True
            )
        member_id = getattr(member, "id", None)
        cached_member = (
            guild.get_member(member_id) if isinstance(member_id, int) else None
        )
        if (
            cached_member is None
            and getattr(getattr(member, "guild", None), "id", None) == guild.id
        ):
            # UserSelectのresolved memberがGatewayキャッシュ反映より先に届いた
            # 場合も、同じguild由来と確認できれば追加できる。
            cached_member = member
        if not isinstance(member_id, int) or cached_member is None:
            return await interaction.response.send_message(
                "対象がサーバー内に見つかりません。", ephemeral=True
            )
        member = cached_member
        if member_id == self.owner_id:
            return await interaction.response.send_message("自分自身は登録できません。", ephemeral=True)
        if bool(getattr(member, "bot", False)):
            return await interaction.response.send_message("Botは登録できません。", ephemeral=True)
        try:
            count = await database.add_player_block(self.guild_id, self.owner_id, member_id)
        except (database.RecruitmentConflict, database.PlayerBlockLimitReached, ValueError) as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        await self._refresh(interaction)
        await self.manager.notify_block_added(guild, self.owner_id, member_id, count)

    async def remove(self, interaction: discord.Interaction) -> None:
        if self._authorized_guild(interaction) is None:
            return await interaction.response.send_message("本人だけ操作できます。", ephemeral=True)
        select = discord.utils.get(
            self.children, custom_id="player_blocks:remove",
        )
        selected_id = parse_select_id(
            select.values[0] if select is not None and select.values else None
        )
        if selected_id is None:
            return await interaction.response.send_message(
                "❌ 不正な選択です。", ephemeral=True
            )
        await database.remove_player_block(self.guild_id, self.owner_id, selected_id)
        await self._refresh(interaction)

    async def add_previous(self, interaction: discord.Interaction) -> None:
        if self._authorized_guild(interaction) is None:
            return await interaction.response.send_message("本人だけ操作できます。", ephemeral=True)
        await self._refresh(interaction, add_page=self.add_page - 1)

    async def add_next(self, interaction: discord.Interaction) -> None:
        if self._authorized_guild(interaction) is None:
            return await interaction.response.send_message("本人だけ操作できます。", ephemeral=True)
        await self._refresh(interaction, add_page=self.add_page + 1)

    async def remove_previous(self, interaction: discord.Interaction) -> None:
        if self._authorized_guild(interaction) is None:
            return await interaction.response.send_message("本人だけ操作できます。", ephemeral=True)
        await self._refresh(interaction, remove_page=self.remove_page - 1)

    async def remove_next(self, interaction: discord.Interaction) -> None:
        if self._authorized_guild(interaction) is None:
            return await interaction.response.send_message("本人だけ操作できます。", ephemeral=True)
        await self._refresh(interaction, remove_page=self.remove_page + 1)


# ============================================================
# 運営ダッシュボード (v0.51)
#
# #運営 はボタンだけの操作盤に保つ方針なので、指標は1枚のephemeralへ
# まとめ、セレクトで面を切り替える。押した面のSQLだけを都度投げる
# (6面ぶんを毎回集計しない)。
# ============================================================

_OPS_DASHBOARD_PANELS: tuple[tuple[str, str, str], ...] = (
    ("activity", "稼働", "DAU/WAU/MAU と直近14日の日別"),
    ("retention", "定着", "新規流入・2戦目到達・週次継続率"),
    ("churn", "離脱", "休眠人数・最終プレイからの経過・プレイ間隔"),
    ("throughput", "回転", "募集の成立率・試合数・所要時間"),
    ("delivery", "通知", "DM送達失敗率・通知オプトアウト"),
    ("rating", "レート", "分布・仮ランク・月別の増減"),
)


def _ops_int(value: object) -> str:
    return "—" if value is None else f"{int(value)}"


def _ops_percent(value: object, *, digits: int = 1) -> str:
    return "—" if value is None else f"{float(value) * 100:.{digits}f}%"


def _ops_number(value: object, *, digits: int = 1, suffix: str = "") -> str:
    return "—" if value is None else f"{float(value):.{digits}f}{suffix}"


def _build_ops_activity_embed(stats: dict) -> discord.Embed:
    embed = discord.Embed(
        title="📈 稼働",
        description=(
            f"JST {stats.get('today')} 時点 / 集計窓 {stats.get('window_days')}日\n"
            "練習卓を含む「実際に遊ばれた回数」で数えます。"
        ),
        color=discord.Color.green(),
    )
    embed.add_field(
        name="アクティブ人数",
        value=(
            f"DAU **{_ops_int(stats.get('dau'))}** / "
            f"WAU **{_ops_int(stats.get('wau'))}** / "
            f"MAU **{_ops_int(stats.get('mau'))}**\n"
            f"DAU÷MAU {_ops_percent(stats.get('stickiness'))} ・ "
            f"WAU÷MAU {_ops_percent(stats.get('wau_mau'))}\n"
            f"累計プレイヤー {_ops_int(stats.get('total_players'))}人"
        ),
        inline=False,
    )
    embed.add_field(
        name="試合数",
        value=(
            f"本日 {_ops_int(stats.get('games_today'))} / "
            f"7日 {_ops_int(stats.get('games_7d'))} / "
            f"30日 {_ops_int(stats.get('games_30d'))}"
        ),
        inline=False,
    )
    daily = stats.get("daily") or []
    lines = [
        f"`{row['date'][5:]}` 試合{row['games']:>3} / 参加{row['players']:>3}人"
        + (f" / 新規{row['new_players']}" if row["new_players"] else "")
        for row in daily
    ]
    embed.add_field(
        name="直近の日別", value="\n".join(lines) or "記録なし", inline=False,
    )
    return embed


def _build_ops_retention_embed(stats: dict) -> discord.Embed:
    embed = discord.Embed(
        title="🌱 定着",
        description="新しく入った人が残っているかを見る面です。",
        color=discord.Color.teal(),
    )
    embed.add_field(
        name="新規プレイヤー",
        value=(
            f"本日 {_ops_int(stats.get('new_today'))}人 / "
            f"7日 {_ops_int(stats.get('new_7d'))}人 / "
            f"30日 {_ops_int(stats.get('new_30d'))}人"
        ),
        inline=False,
    )
    embed.add_field(
        name="2戦目到達率",
        value=(
            f"{_ops_percent(stats.get('second_game_rate'))}"
            f"（初参加から30日以上経った {_ops_int(stats.get('second_game_sample'))}人が母数）"
        ),
        inline=False,
    )
    cohorts = stats.get("cohorts") or []
    if cohorts:
        lines = [
            f"`{row['week'][5:]}週` {row['size']:>2}人 → "
            f"W1 {_ops_percent(row['w1_rate'], digits=0)} / "
            f"W4 {_ops_percent(row['w4_rate'], digits=0)}"
            for row in cohorts[-8:]
        ]
        value = "\n".join(lines)
    else:
        value = "28日以上経過したコホートがまだありません。"
    embed.add_field(name="週次コホート（初参加週別）", value=value, inline=False)
    embed.set_footer(
        text="W1=初参加から1〜7日目に再プレイ / W4=22〜28日目に再プレイ"
    )
    return embed


def _build_ops_churn_embed(stats: dict) -> discord.Embed:
    embed = discord.Embed(
        title="🚪 離脱",
        description="どれだけ間が空いているかを見る面です。",
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="休眠人数",
        value=(
            f"14日以上 {_ops_int(stats.get('dormant_14'))}人 / "
            f"30日以上 {_ops_int(stats.get('dormant_30'))}人 / "
            f"60日以上 {_ops_int(stats.get('dormant_60'))}人\n"
            f"直近7日の復帰（14日以上空けて再開） **{_ops_int(stats.get('returning_7d'))}人**"
        ),
        inline=False,
    )
    buckets = stats.get("last_play_buckets") or {}
    embed.add_field(
        name="最終プレイからの経過",
        value="\n".join(
            f"{label}: {count}人" for label, count in buckets.items()
        ) or "記録なし",
        inline=False,
    )
    streaks = stats.get("longest_streaks") or []
    embed.add_field(
        name="プレイ間隔 / 連続日数",
        value=(
            f"間隔の中央値 {_ops_number(stats.get('gap_median'), suffix='日')} ・ "
            f"90%点 {_ops_number(stats.get('gap_p90'), suffix='日')}\n"
            + (
                "最長連続: "
                + " / ".join(f"{row['days']}日" for row in streaks)
                if streaks else "連続プレイ（2日以上）はまだありません。"
            )
        ),
        inline=False,
    )
    return embed


def _ops_identity(guild: Optional[discord.Guild], user_id: int) -> str:
    """運営表示用の名前。guildが取れない経路でも落とさない。"""
    if guild is None:
        return f"ID: {user_id}"
    return _plain_identity(guild, user_id)


def _build_ops_throughput_embed(
    stats: dict, guild: Optional[discord.Guild],
) -> discord.Embed:
    embed = discord.Embed(
        title="🔁 回転",
        description=f"直近{stats.get('days')}日の募集と試合。",
        color=discord.Color.blue(),
    )
    status = stats.get("recruitment_status") or {}
    embed.add_field(
        name="募集",
        value=(
            f"作成 {_ops_int(stats.get('recruitments'))}件 / "
            f"成立率 {_ops_percent(stats.get('held_rate'))}\n"
            + ("内訳: " + " / ".join(f"{k} {v}" for k, v in status.items()) if status else "内訳なし")
            + f"\n定員充足の中央値 {_ops_percent(stats.get('fill_rate_median'), digits=0)}\n"
            f"満席までの時間 中央値 {_ops_number(stats.get('ready_wait_median_min'), suffix='分')} ・ "
            f"90%点 {_ops_number(stats.get('ready_wait_p90_min'), suffix='分')}"
        ),
        inline=False,
    )
    embed.add_field(
        name="試合",
        value=(
            f"{_ops_int(stats.get('games'))}戦 / "
            f"稼働日あたり {_ops_number(stats.get('games_per_active_day'))}戦\n"
            f"所要時間 中央値 {_ops_number(stats.get('duration_median_min'), suffix='分')} ・ "
            f"90%点 {_ops_number(stats.get('duration_p90_min'), suffix='分')}"
            f"（n={_ops_int(stats.get('duration_sample'))}）\n"
            f"終了日数の中央値 {_ops_number(stats.get('game_days_median'), suffix='日')}\n"
            f"途中離脱 {_ops_int(stats.get('dropouts'))} / 参加席 {_ops_int(stats.get('seats'))}"
            f"（{_ops_percent(stats.get('dropout_rate'), digits=2)}）"
        ),
        inline=False,
    )
    hours = stats.get("hour_buckets") or {}
    embed.add_field(
        name="時間帯（JST）",
        value=" / ".join(f"{label} {count}" for label, count in hours.items()) or "記録なし",
        inline=False,
    )
    gm_top = stats.get("gm_top") or []
    if gm_top:
        embed.add_field(
            name="GM実施回数",
            value="\n".join(
                f"{_ops_identity(guild, row['player_id'])}: {row['games']}回"
                for row in gm_top
            ),
            inline=False,
        )
    return embed


def _build_ops_delivery_embed(stats: dict) -> discord.Embed:
    embed = discord.Embed(
        title="📮 通知",
        description=(
            f"直近{stats.get('days')}日のDM送達。失敗率が上がっていると、"
            "本人にもホストにも見えないまま募集が回らなくなります。"
        ),
        color=discord.Color.purple(),
    )
    for key, label in (("call_dm", "「募集」ボタンのDM"), ("recruitment_dm", "開始前の通知DM")):
        block = stats.get(key) or {}
        by_status = block.get("by_status") or {}
        embed.add_field(
            name=label,
            value=(
                f"送信 {_ops_int(block.get('total'))}件 / "
                f"失敗率 **{_ops_percent(block.get('failure_rate'), digits=2)}**\n"
                + ("内訳: " + " / ".join(f"{k} {v}" for k, v in by_status.items())
                   if by_status else "内訳なし")
            ),
            inline=False,
        )
    embed.add_field(
        name="通知設定",
        value=(
            f"設定済み {_ops_int(stats.get('prefs_configured'))}人 / "
            f"オプトアウト {_ops_int(stats.get('prefs_opted_out'))}人"
            f"（{_ops_percent(stats.get('opt_out_rate'))}）\n"
            f"作成時に通知 {_ops_int(stats.get('prefs_notify_on_create'))}人 / "
            f"「募集」ボタン {_ops_int(stats.get('prefs_notify_on_call'))}人"
        ),
        inline=False,
    )
    return embed


def _build_ops_rating_embed(stats: dict) -> discord.Embed:
    embed = discord.Embed(
        title="🎯 レート健全性",
        description=(
            f"{stats.get('variant_id')} / ラダー {stats.get('ladder_id')}。"
            "平均が一方向へ動き続けていたら配分の見直し時です。"
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="分布",
        value=(
            f"対象 {_ops_int(stats.get('players'))}人 / "
            f"平均 {_ops_number(stats.get('mean'), digits=0)} / "
            f"中央値 {_ops_number(stats.get('median'), digits=0)}\n"
            f"最小 {_ops_int(stats.get('min'))} 〜 最大 {_ops_int(stats.get('max'))} ・ "
            f"仮ランク {_ops_int(stats.get('provisional'))}人"
        ),
        inline=False,
    )
    histogram = stats.get("histogram") or []
    if histogram:
        peak = max(row["count"] for row in histogram) or 1
        lines = [
            f"`{row['floor']:>5}` {'█' * max(1, round(row['count'] / peak * 18))} {row['count']}"
            for row in histogram
        ]
        embed.add_field(name="レート帯（100刻み）", value="\n".join(lines[-14:]), inline=False)
    monthly = stats.get("monthly") or []
    if monthly:
        embed.add_field(
            name="月別（精算ベース）",
            value="\n".join(
                f"`{row['month']}` 精算{row['settlements']:>4}件 / "
                f"平均レート {_ops_number(row['avg_rating_after'], digits=0)} / "
                f"平均増減 {_ops_number(row['avg_total_delta'], digits=2)}"
                for row in monthly
            ),
            inline=False,
        )
    return embed


class OperationsDashboardSelect(discord.ui.Select):
    def __init__(self, parent: "OperationsDashboardView") -> None:
        super().__init__(
            placeholder="見る面を選ぶ",
            options=[
                discord.SelectOption(
                    label=label, value=key, description=description,
                    default=(key == parent.panel),
                )
                for key, label, description in _OPS_DASHBOARD_PANELS
            ],
            row=0,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        # ephemeralなので他人はそもそも触れないが、View timeout(300秒)の間に
        # 運営ロールを外れた相手が面を切り替えられてしまう穴があった
        # (親レビュー指摘2)。入口の「運営データ」ボタンと同じ OperationsView.
        # _is_admin を毎回再検証し、コピペで判定ロジックを二重管理しない。
        if interaction.guild is None or not OperationsView._is_admin(interaction):
            return await interaction.response.send_message(
                "❌ 運営のみ操作できます。", ephemeral=True,
            )
        self.parent_view.panel = self.values[0]
        await interaction.response.defer()
        self.parent_view.rebuild()
        embed = await self.parent_view.load_embed(interaction.guild)
        await interaction.edit_original_response(embed=embed, view=self.parent_view)


class OperationsDashboardView(discord.ui.View):
    """運営専用の指標をまとめた1枚。面ごとに必要なSQLだけを投げる。"""

    def __init__(self, guild_id: int, *, panel: str = "activity") -> None:
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.panel = panel
        self.rebuild()

    def rebuild(self) -> None:
        self.clear_items()
        self.add_item(OperationsDashboardSelect(self))

    async def load_embed(self, guild: Optional[discord.Guild]) -> discord.Embed:
        if self.panel in ("activity", "retention", "churn"):
            stats = await database.get_ops_activity_stats(self.guild_id)
            if self.panel == "activity":
                return _build_ops_activity_embed(stats)
            if self.panel == "retention":
                return _build_ops_retention_embed(stats)
            return _build_ops_churn_embed(stats)
        if self.panel == "throughput":
            return _build_ops_throughput_embed(
                await database.get_ops_throughput_stats(self.guild_id), guild,
            )
        if self.panel == "delivery":
            return _build_ops_delivery_embed(
                await database.get_ops_delivery_stats(self.guild_id),
            )
        return _build_ops_rating_embed(
            await database.get_ops_rating_health(self.guild_id),
        )


class OperationsView(discord.ui.View):
    def __init__(self, manager: RecruitmentManager) -> None:
        super().__init__(timeout=None)
        self.manager = manager

    @staticmethod
    def _is_admin(interaction: discord.Interaction) -> bool:
        """運営メニューを操作してよい相手か。

        サーバー所有者はguild_permissionsが常にallになるためAdministrator判定に
        含まれるが、所有者を弾く実装へ後から変えられないよう明示的に許可する。
        OPERATIONS_STAFF_ROLE_NAMESはAdministratorを付けずに運営を任せるための
        設定で、#運営の閲覧と同じ範囲を操作にも認める。
        OPERATIONS_STAFF_USER_IDSはロールを使わず名指しで足すための枠。
        """
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        guild = getattr(interaction, "guild", None)
        owner_id = getattr(guild, "owner_id", None)
        if owner_id is not None and owner_id == member.id:
            return True
        if getattr(member.guild_permissions, "administrator", False):
            return True
        if member.id in OPERATIONS_STAFF_USER_IDS:
            return True
        return any(
            role.name in OPERATIONS_STAFF_ROLE_NAMES
            for role in getattr(member, "roles", ())
        )

    @discord.ui.button(label="被拒否数の一覧", style=discord.ButtonStyle.secondary, custom_id="operations:block_counts", row=0)
    async def counts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_admin(interaction) or interaction.guild is None:
            return await interaction.response.send_message("運営のみ操作できます。", ephemeral=True)
        rows = await database.get_blocked_counts(interaction.guild.id)
        lines = [f"{_plain_identity(interaction.guild, row['blocked_id'])}: {row['count']}人" for row in rows]
        await interaction.response.send_message("\n".join(lines) or "登録はありません。", ephemeral=True)

    @discord.ui.button(label="報告の一覧", style=discord.ButtonStyle.secondary, custom_id="operations:feedback", row=0)
    async def feedback_list(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_admin(interaction) or interaction.guild is None:
            return await interaction.response.send_message("運営のみ操作できます。", ephemeral=True)
        rows = await database.load_recent_feedback_reports(interaction.guild.id, limit=10)
        if not rows:
            return await interaction.response.send_message(
                "不具合・改善の報告はまだありません。", ephemeral=True
            )
        embed = discord.Embed(
            title="📮 直近の報告",
            description="全文はこのチャンネルの通知に残っています。",
            color=discord.Color.blurple(),
        )
        for row in rows:
            context = " / ".join(
                str(part) for part in (
                    row.get("room_name"), row.get("phase"), row.get("bot_version"),
                ) if part
            )
            summary = str(row.get("summary") or "").strip().replace("`", "'")
            if len(summary) > 300:
                summary = summary[:300] + "…"
            embed.add_field(
                name=f"#{row['report_id']} [{row['category']}] {str(row['created_at'])[:16]}",
                value=(
                    f"{_plain_identity(interaction.guild, row['user_id'])}"
                    + (f"\n{context}" if context else "")
                    + f"\n{summary or '(本文なし)'}"
                )[:1024],
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="途中離脱の一覧", style=discord.ButtonStyle.secondary, custom_id="operations:dropouts", row=0)
    async def dropouts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_admin(interaction) or interaction.guild is None:
            return await interaction.response.send_message("運営のみ操作できます。", ephemeral=True)
        rows = await database.get_dropout_counts(interaction.guild.id)
        if not rows:
            return await interaction.response.send_message(
                "途中離脱の記録はありません。", ephemeral=True
            )
        lines = [
            f"{_plain_identity(interaction.guild, row['player_id'])}: "
            f"{row['dropouts']}回 / {row['games']}戦 ({row['rate'] * 100:.0f}%)"
            for row in rows
        ]
        await interaction.response.send_message(
            "🚪 **途中離脱 (GM除外) の回数**\n"
            "離脱の多い順。回数が同じなら試合数の少ない人が上です。\n"
            + "\n".join(lines),
            ephemeral=True,
        )

    @discord.ui.button(label="9人均衡監視", style=discord.ButtonStyle.secondary, custom_id="operations:variant_balance", row=0)
    async def variant_balance(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        if not self._is_admin(interaction) or interaction.guild is None:
            return await interaction.response.send_message(
                "運営のみ操作できます。", ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await database.get_variant_balance_stats(interaction.guild.id)
        # views は起動時に recruitment を参照するため、循環importを避けて
        # ボタン押下時だけ参照する。
        from views import build_variant_balance_embed
        await interaction.followup.send(
            embed=build_variant_balance_embed(rows), ephemeral=True,
        )

    @discord.ui.button(label="運営データ", style=discord.ButtonStyle.primary, custom_id="operations:dashboard", row=0)
    async def dashboard(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        if not self._is_admin(interaction) or interaction.guild is None:
            return await interaction.response.send_message(
                "運営のみ操作できます。", ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        view = OperationsDashboardView(interaction.guild.id)
        embed = await view.load_embed(interaction.guild)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="GM解除", style=discord.ButtonStyle.danger, custom_id="operations:release_gm", row=1)
    async def release_gm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_admin(interaction):
            return await interaction.response.send_message("運営のみ操作できます。", ephemeral=True)
        lobby_rooms = [room for room in self.manager.game_cog.rooms.values() if room.state.phase == Phase.LOBBY and room.state.gm_id]
        active = [room.state.room_name for room in self.manager.game_cog.rooms.values() if room.state.phase != Phase.LOBBY]
        if not lobby_rooms:
            suffix = "\n進行中のため対象外: " + ", ".join(active) if active else ""
            return await interaction.response.send_message("解除できるGMはいません。" + suffix, ephemeral=True)
        await interaction.response.send_message(
            "受付中の卓だけ選べます。" + ("\n進行中のため対象外: " + ", ".join(active) if active else ""),
            view=OperationsGMReleaseView(self.manager, lobby_rooms), ephemeral=True,
        )

    @discord.ui.button(label="募集の強制削除", style=discord.ButtonStyle.danger, custom_id="operations:archive_recruitment", row=1)
    async def archive_recruitment(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_admin(interaction) or interaction.guild is None:
            return await interaction.response.send_message("運営のみ操作できます。", ephemeral=True)
        rows = await database.list_open_recruitments(interaction.guild.id)
        if not rows:
            return await interaction.response.send_message("募集中の募集はありません。", ephemeral=True)
        await interaction.response.send_message(
            "強制アーカイブする募集を選んでください。",
            view=OperationsRecruitmentArchiveView(self.manager, rows[:25]), ephemeral=True,
        )

    @discord.ui.button(label="村の強制削除", style=discord.ButtonStyle.danger, custom_id="operations:delete_private_room", row=1)
    async def delete_private_room(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # 村主が対応できない村の救済。選択と最終確認は本人削除と同じ入口に任せる。
        if not self._is_admin(interaction) or interaction.guild is None:
            return await interaction.response.send_message("運営のみ操作できます。", ephemeral=True)
        await self.manager.game_cog.prompt_private_room_force_delete(interaction)

    @discord.ui.button(label="シーズンリセット", style=discord.ButtonStyle.danger, custom_id="operations:season_reset", row=2)
    async def season_reset(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # メニュー表示の運営権限だけでは足りない。全員のレートを書き換える
        # 操作なので、`/season_reset` と同じ「サーバー管理」権限と最終確認を
        # 共通入口へ委ね、権限判定を二重に定義しない。
        if not self._is_admin(interaction) or interaction.guild is None:
            return await interaction.response.send_message("運営のみ操作できます。", ephemeral=True)
        await self.manager.game_cog.prompt_season_reset(interaction)


class OperationsGMReleaseView(discord.ui.View):
    def __init__(self, manager: RecruitmentManager, rooms: list) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        select = discord.ui.Select(
            placeholder="GMを解除する卓",
            options=[discord.SelectOption(label=r.state.room_name, value=r.state.room_id) for r in rooms[:25]],
        )
        select.callback = self.selected
        self.add_item(select)

    async def selected(self, interaction: discord.Interaction) -> None:
        if not OperationsView._is_admin(interaction):
            return await interaction.response.send_message("運営のみ操作できます。", ephemeral=True)
        room = self.manager.game_cog.rooms.get(self.children[0].values[0])
        if room is None or room.state.phase != Phase.LOBBY:
            return await interaction.response.send_message("進行中または対象外のため解除できません。", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with room.action_lock:
            room.state.gm_id = None
            if not room.state.players:
                room.state.recruitment_id = None
            await room._persist_room_state()
            await room._post_lobby_ui()
        await interaction.followup.send("GMを解除しました。", ephemeral=True)


class OperationsRecruitmentArchiveView(discord.ui.View):
    def __init__(self, manager: RecruitmentManager, rows: list[dict]) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        select = discord.ui.Select(
            placeholder="強制アーカイブする募集",
            options=[discord.SelectOption(label=row["title"][:100], value=str(row["id"])) for row in rows],
        )
        select.callback = self.selected
        self.add_item(select)

    async def selected(self, interaction: discord.Interaction) -> None:
        if not OperationsView._is_admin(interaction):
            return await interaction.response.send_message("運営のみ操作できます。", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        recruitment_id = parse_select_id(self.children[0].values[0])
        if recruitment_id is None:
            return await interaction.followup.send(
                "❌ 不正な選択です。", ephemeral=True
            )
        await self.manager.archive_recruitment(
            interaction.guild, recruitment_id,
        )
        await interaction.followup.send("募集を強制アーカイブしました。", ephemeral=True)
