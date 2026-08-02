"""数日先の募集を既存の即時ロビーへ安全に移す予約層。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord

import database
import rating as rating_lib
from config import (
    BOT_VERSION,
    CH_OPERATIONS,
    CH_RECRUITMENT,
    MAX_PLAYERS,
    OPERATIONS_CATEGORY_NAME,
    PLAYER_BLOCK_LIMIT,
    PRIVATE_ROOM_CREATOR_ROLE_NAME,
    RECRUITMENT_BACKUP_CAPACITY,
    RECRUITMENT_MAX_DAYS_AHEAD,
    RECRUITMENT_OCCUPANCY_MINUTES,
    RECRUITMENT_RANK_OPTIONS,
    RECRUITMENT_UNRANKED_LABEL,
    ROOM_DEFINITION_MAP,
    ROOM_DEFINITIONS,
    RECRUITMENT_ROOM_IDS,
    Phase,
)
from models import Player

log = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")
WEEKDAY_JA = "月火水木金土日"


def _utc_datetime(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _display_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member is not None else f"ID:{user_id}"


def _plain_identity(guild: discord.Guild, user_id: int) -> str:
    """運営通知用。メンションを絶対に作らない。"""
    return f"{_display_name(guild, user_id)} (ID: {user_id})"


def build_recruitment_help_embed() -> discord.Embed:
    """#募集 の利用者向けクイックヘルプ。"""
    embed = discord.Embed(
        title="募集の使い方",
        description="数日先のゲームを予約し、集まったメンバーを参加受付へ移す機能です。",
        color=discord.Color.dark_gold(),
    )
    embed.add_field(
        name="参加する",
        value=(
            "募集カードの **「参加」** を押します。13人を超えると補欠になり、"
            "空きが出ると自動で繰り上がります。参加と繰り上げの連絡にDMを使います。"
        ),
        inline=False,
    )
    embed.add_field(
        name="募集を作る",
        value=(
            f"**{PRIVATE_ROOM_CREATOR_ROLE_NAME}** ロール保持者が、7日先まで作成できます。"
            "総合卓は参加可能ランクを複数選択でき、未選択なら制限なしです。"
        ),
        inline=False,
    )
    embed.add_field(
        name="開催する",
        value=(
            "参加者13人とGMが揃ったら、主催者が **「卓へ移行」** を押します。"
            "開催15分前には参加者へDMが届きます。"
        ),
        inline=False,
    )
    embed.set_footer(text=f"{BOT_VERSION} / 募集の占有時間は{RECRUITMENT_OCCUPANCY_MINUTES}分")
    return embed


class RecruitmentManager:
    def __init__(self, bot, game_cog) -> None:
        self.bot = bot
        self.game_cog = game_cog
        self.channel: Optional[discord.TextChannel] = None
        self.operations_channel: Optional[discord.TextChannel] = None
        self.lock = asyncio.Lock()

    async def _ensure_public_channel(self, guild: discord.Guild) -> discord.TextChannel:
        channel = None
        stored = await database.get_meta(guild.id, "recruitment_channel_id")
        if stored and str(stored).isdigit():
            candidate = guild.get_channel(int(stored))
            if candidate in guild.text_channels:
                channel = candidate
        if channel is None:
            channel = discord.utils.get(guild.text_channels, name=CH_RECRUITMENT)
        if channel is None:
            channel = await guild.create_text_channel(CH_RECRUITMENT)
        await database.set_meta(guild.id, "recruitment_channel_id", str(channel.id))
        try:
            await self.game_cog._set_permission_if_changed(
                channel, guild.default_role,
                discord.PermissionOverwrite(
                    view_channel=True, read_messages=True, send_messages=False,
                ),
                reason="募集チャンネル権限更新",
            )
            await self.game_cog._set_permission_if_changed(
                channel, guild.me,
                discord.PermissionOverwrite(
                    view_channel=True, read_messages=True, send_messages=True,
                    manage_channels=True,
                ),
                reason="募集チャンネル権限更新",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("募集チャンネル権限更新失敗: %s", exc)
        return channel

    async def _ensure_operations_channel(
        self, guild: discord.Guild,
    ) -> Optional[discord.TextChannel]:
        category = discord.utils.get(guild.categories, name=OPERATIONS_CATEGORY_NAME)
        if category is None:
            log.warning("開発カテゴリが無いため#運営を作成しません")
            return None
        channel = discord.utils.get(guild.text_channels, name=CH_OPERATIONS, category=category)
        if channel is None:
            channel = await guild.create_text_channel(CH_OPERATIONS, category=category)
        try:
            await self.game_cog._set_permission_if_changed(
                channel, guild.default_role,
                discord.PermissionOverwrite(
                    view_channel=False, read_messages=False, send_messages=False,
                ),
                reason="運営チャンネル権限更新",
            )
            await self.game_cog._set_permission_if_changed(
                channel, guild.me,
                discord.PermissionOverwrite(
                    view_channel=True, read_messages=True, send_messages=True,
                    manage_channels=True,
                ),
                reason="運営チャンネル権限更新",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("運営チャンネル権限更新失敗: %s", exc)
        return channel

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
        """チャンネル、期限切れ、永続Viewを復元する。失敗はゲーム卓へ伝播させない。"""
        self.channel = await self._ensure_public_channel(guild)
        self.operations_channel = await self._ensure_operations_channel(guild)
        await self._upsert_panel(
            self.channel, "recruitment_home_message_id",
            content=(
                "📅 **人狼ゲーム募集**\n"
                "村長ロールを持つ人が、7日先までの募集を作成できます。"
            ),
            view=RecruitmentHomeView(self),
        )
        if self.operations_channel is not None:
            await self._upsert_panel(
                self.operations_channel, "operations_home_message_id",
                content="🛠️ **運営メニュー**（管理者のみ）",
                view=OperationsView(self),
            )
        expired = await database.archive_expired_recruitments(
            guild.id, datetime.now(timezone.utc)
        )
        for recruitment_id in expired:
            await self.refresh_message(recruitment_id)
        for row in await database.list_open_recruitments(guild.id):
            await self.ensure_recruitment_message(guild, row)
        await self.cleanup_old_messages(guild)

    async def cleanup_old_messages(self, guild: discord.Guild) -> None:
        if self.channel is None:
            return
        fetch = getattr(self.channel, "fetch_message", None)
        if not callable(fetch):
            return
        for row in await database.list_recruitment_messages_for_cleanup(
            guild.id, datetime.now(timezone.utc)
        ):
            try:
                message = await fetch(row["message_id"])
                await message.delete()
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("終了募集メッセージ削除失敗 (%s): %s", row["id"], exc)
                continue
            await database.clear_recruitment_message_id(row["id"])

    async def ensure_recruitment_message(self, guild: discord.Guild, row: dict) -> None:
        if self.channel is None:
            return
        view = RecruitmentCardView(self, row["id"], active=row["status"] == database.RECRUITMENT_OPEN)
        message = None
        fetch = getattr(self.channel, "fetch_message", None)
        if row.get("message_id") and callable(fetch):
            try:
                message = await fetch(row["message_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        embed = await self.build_embed(guild, row["id"])
        if message is None:
            message = await self.channel.send(embed=embed, view=view)
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
        add_view = getattr(self.bot, "add_view", None)
        if callable(add_view) and row["status"] == database.RECRUITMENT_OPEN:
            add_view(view, message_id=message.id)

    async def publish_new_recruitment(
        self, guild: discord.Guild, recruitment_id: int
    ) -> None:
        """新規募集を掲示し、掲示不能なら予約だけ残さず取り消す。"""
        row = await database.get_recruitment(recruitment_id)
        if row is None:
            raise RuntimeError("作成した募集を読み直せませんでした。")
        if self.channel is None:
            published_error: Exception = RuntimeError("募集チャンネルがありません。")
        else:
            try:
                await self.ensure_recruitment_message(guild, row)
                return
            except Exception as exc:
                published_error = exc
                log.exception("新規募集カードの掲示に失敗: %s", recruitment_id)
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
        if row is None or self.channel is None:
            return
        await self.ensure_recruitment_message(self.channel.guild, row)

    async def build_embed(self, guild: discord.Guild, recruitment_id: int) -> discord.Embed:
        row = await database.get_recruitment(recruitment_id)
        if row is None:
            return discord.Embed(title="募集が見つかりません", color=discord.Color.red())
        entries = await database.list_recruitment_entries(recruitment_id)
        participants = [e["user_id"] for e in entries if e["kind"] == "参加"]
        backups = [e["user_id"] for e in entries if e["kind"] == "補欠"]
        room = ROOM_DEFINITION_MAP.get(row["room_id"])
        start = _utc_datetime(row["scheduled_at"])
        color = discord.Color.dark_gold() if row["status"] == database.RECRUITMENT_OPEN else discord.Color.dark_grey()
        embed = discord.Embed(
            title=row["title"],
            description=(
                f"主催者: <@{row['host_id']}>\n"
                f"開催: {discord.utils.format_dt(start, style='F')} "
                f"({discord.utils.format_dt(start, style='R')})\n"
                f"卓: **{room.name if room else row['room_id']}** / "
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
            name=f"参加者 ({len(participants)}/{MAX_PLAYERS})",
            value="\n".join(participant_lines) or "なし", inline=False,
        )
        embed.add_field(
            name=f"補欠 ({len(backups)}/{RECRUITMENT_BACKUP_CAPACITY})",
            value="\n".join(_display_name(guild, uid) for uid in backups) or "なし",
            inline=False,
        )
        gm_text = _display_name(guild, row["gm_id"]) if row["gm_id"] else "未登録（移行時は主催者がGM）"
        embed.add_field(name="GM", value=gm_text, inline=False)
        if row["room_id"] == "open":
            allowed_ranks = row["allowed_ranks"]
            if allowed_ranks is None:
                rank_condition = "制限なし"
            elif not allowed_ranks:
                rank_condition = "設定エラー（参加不可）"
            else:
                labels = []
                for rank_name in RECRUITMENT_RANK_OPTIONS:
                    if rank_name not in allowed_ranks:
                        continue
                    emoji = (
                        "❔" if rank_name == RECRUITMENT_UNRANKED_LABEL
                        else rating_lib.get_rank_emoji_by_name(rank_name)
                    )
                    labels.append(f"{emoji} {rank_name}")
                rank_condition = " / ".join(labels)
            embed.add_field(
                name="総合卓の条件",
                value=f"参加可能ランク: {rank_condition}",
                inline=False,
            )
        try:
            rank_map = await database.get_current_rank_map(guild.id)
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
        embed.set_footer(text=f"募集ID: {recruitment_id} / 占有時間: {RECRUITMENT_OCCUPANCY_MINUTES}分")
        return embed

    async def validate_candidate(self, guild: discord.Guild, row: dict, member: discord.Member) -> Optional[str]:
        if member.id == guild.owner_id or member.guild_permissions.administrator:
            return "サーバーオーナーと管理者権限保持者はプレイヤー参加できません。"
        room = ROOM_DEFINITION_MAP.get(row["room_id"])
        if room is None:
            return "対象卓が見つかりません。"
        if room.access_role_names:
            roles = {role.name for role in member.roles}
            if not member.guild_permissions.manage_guild and not roles.intersection(room.access_role_names):
                return "この卓へ参加するための指定ロールがありません。"
        info = await database.get_player_current_rank_info(member.id, guild.id)
        current_rank = info["rank_name"] if info else RECRUITMENT_UNRANKED_LABEL
        room_rank = info["rank_name"] if info else "ブロンズ"
        if room.allowed_ranks is not None and room_rank not in room.allowed_ranks:
            return f"現在ランク **{room_rank}** はこの卓の参加条件外です。"
        if row["room_id"] == "open":
            allowed_ranks = row["allowed_ranks"]
            if allowed_ranks is not None and current_rank not in allowed_ranks:
                return f"現在ランク **{current_rank}** はこの募集の参加条件外です。"
        return None

    async def notify_ready_if_needed(self, row: dict) -> None:
        if not await database.recruitment_ready_notification_needed(row["id"]):
            return
        guild = self.channel.guild if self.channel else None
        host = guild.get_member(row["host_id"]) if guild else None
        if host is None:
            return
        try:
            await host.send(f"✅ 募集「{row['title']}」は参加者13人とGMが揃いました。")
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
            return "卓へ移行できるのは主催者だけです。"
        room = self.game_cog.rooms.get(row["room_id"])
        if room is None:
            return "対象卓が見つかりません。"
        if room.state.phase != Phase.LOBBY or room._is_game_in_progress():
            return "対象卓はゲーム進行中のため移行できません。"
        entries = await database.list_recruitment_entries(recruitment_id)
        participant_ids = [e["user_id"] for e in entries if e["kind"] == "参加"]
        if len(participant_ids) != MAX_PLAYERS:
            return f"参加者が揃っていません ({len(participant_ids)}/{MAX_PLAYERS})。"
        guild = interaction.guild
        if guild is None:
            return "サーバー内でのみ操作できます。"
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
            gm_error = await room.validate_gm_claim(gm)
            if gm_error:
                invalid.append(f"GM {gm.display_name}: {gm_error}")
        if invalid:
            return "開催時の条件確認で移行を中止しました。\n" + "\n".join(f"・{x}" for x in invalid)
        async with room.action_lock:
            state = room.state
            if state.phase != Phase.LOBBY or room._is_game_in_progress():
                return "対象卓の状態が変わったため移行を中止しました。"
            desired_ids = {member.id for member in members}
            already_transferred = (
                state.recruitment_id == recruitment_id
                and set(state.players) == desired_ids
                and state.gm_id == gm_id
            )
            if not already_transferred:
                # 空判定は長い候補検証の後、通常ロビー操作と同じlock内で
                # 直前に行う。飛び込み参加者を無言clearしない。
                if (state.players or state.gm_id is not None) and not reset_lobby:
                    return "LOBBY_NOT_EMPTY"
                # 募集を先に開催済みへCASし、参加/取消/別移行を止める。
                # その後のロビー保存が失敗しても、開催済みの同じ募集だけは
                # transferを再試行できるため、DBとDiscordの分断を回収できる。
                if row["status"] == database.RECRUITMENT_OPEN:
                    changed = await database.set_recruitment_status(
                        recruitment_id, database.RECRUITMENT_HELD
                    )
                    if not changed:
                        latest = await database.get_recruitment(recruitment_id)
                        if latest is None or latest["status"] != database.RECRUITMENT_HELD:
                            raise database.RecruitmentConflict(
                                "募集状態が変わったため、卓への移行を中止しました。"
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
            if reset_lobby and state.lobby_channel is not None:
                try:
                    await state.lobby_channel.send("募集の開催のため受付をリセットしました。")
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("募集移行の受付リセット告知失敗: %s", exc)
        await self.refresh_message(recruitment_id)
        return f"✅ **{room.state.room_name}** の参加受付へ13人とGMを登録しました。"

    async def process_notifications(self, guild: discord.Guild) -> None:
        now = datetime.now(timezone.utc)
        for row in await database.list_due_recruitment_notifications(guild.id, now):
            entries = await database.list_recruitment_entries(row["id"])
            participant_ids = [e["user_id"] for e in entries if e["kind"] == "参加"]
            for user_id in participant_ids:
                member = guild.get_member(user_id)
                if member is None:
                    continue
                try:
                    await member.send(
                        f"⏰ 募集「{row['title']}」は15分以内に開催予定です。\n"
                        f"{discord.utils.format_dt(_utc_datetime(row['scheduled_at']), style='F')}"
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("募集直前DM失敗 (%s/%s): %s", row["id"], user_id, exc)
            if row["gm_id"] is None:
                host = guild.get_member(row["host_id"])
                if host is not None:
                    try:
                        await host.send(
                            f"⚠️ 募集「{row['title']}」はGM未登録です。"
                            "このまま卓へ移行すると、あなたがGMになります。"
                        )
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        log.warning("募集GM警告DM失敗 (%s): %s", row["id"], exc)
            await database.mark_recruitment_notified(row["id"], now)
        for recruitment_id in await database.archive_expired_recruitments(guild.id, now):
            await self.refresh_message(recruitment_id)
        await self.cleanup_old_messages(guild)

    async def notify_block_added(
        self, guild: discord.Guild, blocker_id: int, blocked_id: int, count: int,
    ) -> None:
        channel = self.operations_channel
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


class RecruitmentHomeView(discord.ui.View):
    def __init__(self, manager: RecruitmentManager) -> None:
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(
        label="募集を作成", style=discord.ButtonStyle.success,
        custom_id="recruitment:create",
    )
    async def create_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("サーバー内でのみ使えます。", ephemeral=True)
        if PRIVATE_ROOM_CREATOR_ROLE_NAME not in {role.name for role in interaction.user.roles}:
            return await interaction.response.send_message(
                f"募集を作成できるのは **{PRIVATE_ROOM_CREATOR_ROLE_NAME}** ロール保持者だけです。",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.user.send("募集作成のDM受信確認です。警告や成立通知をこのDMへ送ります。")
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.followup.send(
                "DMを受け取れないため募集を作成できません。DMを開放してください。",
                ephemeral=True,
            )
        await interaction.followup.send(
            "開催日、時刻、卓、配信の有無を順に選択してください。",
            view=RecruitmentScheduleView(self.manager, interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="募集ヘルプ", style=discord.ButtonStyle.secondary,
        custom_id="recruitment:help",
    )
    async def help_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=build_recruitment_help_embed(), ephemeral=True,
        )


class _DraftSelect(discord.ui.Select):
    def __init__(self, parent: "RecruitmentScheduleView", key: str, **kwargs) -> None:
        self.parent_view = parent
        self.key = key
        super().__init__(**kwargs)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.values[self.key] = self.values[0]
        if len(self.parent_view.values) == 5:
            is_open = self.parent_view.values["room"] == "open"
            await interaction.response.edit_message(
                content=(
                    "必要なら参加可能ランクを複数選び、タイトル・備考を入力してください。"
                    "ランク未選択は制限なしです。"
                    if is_open else "タイトル・備考を入力してください。"
                ),
                view=RecruitmentOptionsView(
                    self.parent_view.manager,
                    self.parent_view.host_id,
                    self.parent_view.values,
                ),
            )
        else:
            await interaction.response.edit_message(view=self.parent_view)


class RecruitmentScheduleView(discord.ui.View):
    def __init__(self, manager: RecruitmentManager, host_id: int) -> None:
        super().__init__(timeout=300)
        self.manager = manager
        self.host_id = host_id
        self.values: dict[str, str] = {}
        now = datetime.now(JST)
        date_options = [
            discord.SelectOption(
                label=(
                    (now + timedelta(days=offset)).strftime("%m月%d日")
                    + f" ({WEEKDAY_JA[(now + timedelta(days=offset)).weekday()]})"
                ),
                value=(now + timedelta(days=offset)).date().isoformat(),
            )
            for offset in range(RECRUITMENT_MAX_DAYS_AHEAD + 1)
        ]
        self.add_item(_DraftSelect(self, "date", placeholder="開催日", options=date_options, row=0))
        self.add_item(_DraftSelect(
            self, "hour", placeholder="開始時（0〜23時）",
            options=[discord.SelectOption(label=f"{hour:02d}時", value=str(hour)) for hour in range(24)],
            row=1,
        ))
        self.add_item(_DraftSelect(
            self, "minute", placeholder="開始分",
            options=[discord.SelectOption(label="00分", value="0"), discord.SelectOption(label="30分", value="30")],
            row=2,
        ))
        self.add_item(_DraftSelect(
            self, "room", placeholder="卓種別",
            options=[
                discord.SelectOption(label=room.name, value=room.room_id)
                for room in ROOM_DEFINITIONS
                if room.room_id in RECRUITMENT_ROOM_IDS
            ],
            row=3,
        ))
        self.add_item(_DraftSelect(
            self, "streaming", placeholder="配信",
            options=[discord.SelectOption(label="配信あり", value="1"), discord.SelectOption(label="配信なし", value="0")],
            row=4,
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
            placeholder="総合卓: 参加可能ランク（未選択＝制限なし）",
            min_values=0,
            max_values=len(RECRUITMENT_RANK_OPTIONS),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.values["allowed_ranks"] = list(self.values)
        await interaction.response.edit_message(view=RecruitmentOptionsView(
            self.parent_view.manager,
            self.parent_view.host_id,
            self.parent_view.values,
        ))


class RecruitmentOptionsView(discord.ui.View):
    def __init__(self, manager: RecruitmentManager, host_id: int, values: dict[str, object]) -> None:
        super().__init__(timeout=300)
        self.manager, self.host_id, self.values = manager, host_id, dict(values)
        self.values.setdefault("allowed_ranks", [])
        if self.values["room"] == "open":
            self.add_item(_RankOptionSelect(self))

    @discord.ui.button(label="タイトル・備考を入力", style=discord.ButtonStyle.primary, row=2)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.host_id:
            return await interaction.response.send_message("作成者だけ操作できます。", ephemeral=True)
        await interaction.response.send_modal(RecruitmentCreateModal(self.manager, self.host_id, self.values))


class RecruitmentCreateModal(discord.ui.Modal, title="募集内容"):
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
        if not isinstance(interaction.user, discord.Member) or PRIVATE_ROOM_CREATOR_ROLE_NAME not in {
            role.name for role in interaction.user.roles
        }:
            return await interaction.response.send_message(
                f"**{PRIVATE_ROOM_CREATOR_ROLE_NAME}** ロールが無いため作成できません。",
                ephemeral=True,
            )
        local_start = datetime.fromisoformat(self.values["date"]).replace(
            hour=int(self.values["hour"]), minute=int(self.values["minute"]), tzinfo=JST,
        )
        now = datetime.now(JST)
        if local_start <= now or local_start > now + timedelta(days=RECRUITMENT_MAX_DAYS_AHEAD):
            return await interaction.response.send_message(
                "開催日時は現在より後、7日以内を選んでください。", ephemeral=True,
            )
        room_id = self.values["room"]
        is_open = room_id == "open"
        selected_ranks = self.values.get("allowed_ranks", [])
        allowed_ranks = (
            list(selected_ranks)
            if is_open and isinstance(selected_ranks, (list, tuple, set, frozenset))
            else None
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            recruitment_id = await database.create_recruitment(
                interaction.guild.id, self.host_id,
                title=str(self.recruitment_title), scheduled_at=local_start,
                room_id=room_id, streaming=self.values["streaming"] == "1",
                allowed_ranks=allowed_ranks,
                note=str(self.note),
            )
        except database.RecruitmentConflict as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        try:
            await self.manager.publish_new_recruitment(
                interaction.guild, recruitment_id
            )
        except RuntimeError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        await interaction.followup.send(
            f"✅ 募集を作成しました（募集ID: {recruitment_id}）。", ephemeral=True,
        )


class RecruitmentCardView(discord.ui.View):
    def __init__(self, manager: RecruitmentManager, recruitment_id: int, *, active: bool = True) -> None:
        super().__init__(timeout=None)
        self.manager, self.recruitment_id = manager, recruitment_id
        buttons = [
            ("参加", discord.ButtonStyle.success, "join", self.join),
            ("参加取消", discord.ButtonStyle.danger, "leave", self.leave),
            ("GM登録/解除", discord.ButtonStyle.primary, "gm", self.gm),
            ("卓へ移行", discord.ButtonStyle.primary, "transfer", self.transfer),
            ("主催者メニュー", discord.ButtonStyle.secondary, "host", self.host_menu),
        ]
        for label, style, suffix, callback in buttons:
            button = discord.ui.Button(
                label=label, style=style,
                custom_id=f"recruitment:{recruitment_id}:{suffix}", disabled=not active,
            )
            button.callback = callback
            self.add_item(button)

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
            kind = await database.add_recruitment_entry(self.recruitment_id, interaction.user.id)
        except database.RecruitmentConflict as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        embed = await self.manager.build_embed(interaction.guild, self.recruitment_id)
        await interaction.message.edit(embed=embed, view=self)
        await interaction.followup.send(f"{kind}として登録しました。", ephemeral=True)
        await self.manager.notify_ready_if_needed(await database.get_recruitment(self.recruitment_id))

    async def leave(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _kind, promoted = await database.remove_recruitment_entry(
                self.recruitment_id, interaction.user.id,
            )
        except database.RecruitmentConflict as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        embed = await self.manager.build_embed(interaction.guild, self.recruitment_id)
        await interaction.message.edit(embed=embed, view=self)
        await interaction.followup.send("参加を取り消しました。", ephemeral=True)
        if promoted and interaction.guild:
            member = interaction.guild.get_member(promoted)
            if member:
                try:
                    await member.send("補欠から参加者へ繰り上がりました。")
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("補欠繰り上げDM失敗 (%s): %s", promoted, exc)

    async def gm(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message(
                "募集が見つかりません。", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await database.get_recruitment(self.recruitment_id)
        if row is None:
            return await interaction.followup.send("募集が見つかりません。", ephemeral=True)
        if row["gm_id"] == interaction.user.id:
            try:
                await database.set_recruitment_gm(
                    self.recruitment_id, None,
                    expected_gm_id=interaction.user.id,
                )
            except database.RecruitmentConflict as exc:
                return await interaction.followup.send(str(exc), ephemeral=True)
            message = "GM登録を解除しました。"
        elif row["gm_id"] is not None:
            return await interaction.followup.send("GMは既に登録されています。", ephemeral=True)
        else:
            room = self.manager.game_cog.rooms.get(row["room_id"])
            if room is None:
                return await interaction.followup.send("対象卓が見つかりません。", ephemeral=True)
            # 予約時点では他卓の現在ロビー重複ではなく、卓固有のGM資格だけを確認する。
            room_def = room.room_def
            info = await database.get_player_current_rank_info(interaction.user.id, interaction.guild.id)
            rank_name = info["rank_name"] if info else "ブロンズ"
            if room_def.allowed_gm_user_ids and interaction.user.id not in room_def.allowed_gm_user_ids:
                return await interaction.followup.send("この卓のGMは指定ユーザー専用です。", ephemeral=True)
            if room_def.allowed_ranks is not None and rank_name not in room_def.allowed_ranks:
                return await interaction.followup.send(
                    f"現在ランク **{rank_name}** はこの卓のGM条件外です。", ephemeral=True,
                )
            try:
                await database.set_recruitment_gm(
                    self.recruitment_id, interaction.user.id,
                    expected_gm_id=None,
                )
            except database.RecruitmentConflict as exc:
                return await interaction.followup.send(str(exc), ephemeral=True)
            message = "GMとして登録しました。"
        embed = await self.manager.build_embed(interaction.guild, self.recruitment_id)
        await interaction.message.edit(embed=embed, view=self)
        await interaction.followup.send(message, ephemeral=True)
        await self.manager.notify_ready_if_needed(await database.get_recruitment(self.recruitment_id))

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
        await interaction.followup.send(result, ephemeral=True)

    async def host_menu(self, interaction: discord.Interaction) -> None:
        row = await database.get_recruitment(self.recruitment_id)
        if row is None or interaction.user.id != row["host_id"]:
            return await interaction.response.send_message("主催者だけ操作できます。", ephemeral=True)
        await interaction.response.send_message(
            "主催者メニューです。日時・卓種別などは変更できません。",
            view=RecruitmentHostView(self.manager, self.recruitment_id, interaction.user.id),
            ephemeral=True,
        )


class RecruitmentLobbyResetConfirmView(discord.ui.View):
    def __init__(self, manager: RecruitmentManager, recruitment_id: int, host_id: int) -> None:
        super().__init__(timeout=120)
        self.manager, self.recruitment_id, self.host_id = manager, recruitment_id, host_id

    @discord.ui.button(label="受付をリセットして移行", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.host_id:
            return await interaction.response.send_message("主催者だけ操作できます。", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.manager.lock:
            result = await self.manager.transfer(interaction, self.recruitment_id, reset_lobby=True)
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="中止", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="移行を中止しました。", view=None)


class RecruitmentHostView(discord.ui.View):
    def __init__(self, manager: RecruitmentManager, recruitment_id: int, host_id: int) -> None:
        super().__init__(timeout=180)
        self.manager, self.recruitment_id, self.host_id = manager, recruitment_id, host_id

    def _allowed(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.host_id

    @discord.ui.button(label="備考を変更", style=discord.ButtonStyle.secondary)
    async def note(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._allowed(interaction):
            return await interaction.response.send_message("主催者だけ操作できます。", ephemeral=True)
        row = await database.get_recruitment(self.recruitment_id)
        await interaction.response.send_modal(RecruitmentNoteModal(self.manager, self.recruitment_id, self.host_id, row["note"]))

    @discord.ui.button(label="複製", style=discord.ButtonStyle.primary)
    async def duplicate(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._allowed(interaction):
            return await interaction.response.send_message("主催者だけ操作できます。", ephemeral=True)
        await interaction.response.send_modal(RecruitmentDuplicateModal(self.manager, self.recruitment_id, self.host_id))

    @discord.ui.button(label="一括連絡", style=discord.ButtonStyle.primary)
    async def contact(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._allowed(interaction):
            return await interaction.response.send_message("主催者だけ操作できます。", ephemeral=True)
        await interaction.response.send_modal(RecruitmentContactModal(self.manager, self.recruitment_id, self.host_id))

    @discord.ui.button(label="募集を廃止", style=discord.ButtonStyle.danger)
    async def archive(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._allowed(interaction):
            return await interaction.response.send_message("主催者だけ操作できます。", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        changed = await database.set_recruitment_status(self.recruitment_id, database.RECRUITMENT_ARCHIVED)
        await self.manager.refresh_message(self.recruitment_id)
        await interaction.followup.send(
            "募集をアーカイブしました。" if changed else "募集は既に終了しています。",
            ephemeral=True,
        )


class RecruitmentNoteModal(discord.ui.Modal, title="備考を変更"):
    note = discord.ui.TextInput(label="備考", required=False, max_length=1000, style=discord.TextStyle.paragraph)

    def __init__(self, manager: RecruitmentManager, recruitment_id: int, host_id: int, current: str) -> None:
        super().__init__()
        self.manager, self.recruitment_id, self.host_id = manager, recruitment_id, host_id
        self.note.default = current

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await database.update_recruitment_note(self.recruitment_id, self.host_id, str(self.note))
        except database.RecruitmentConflict as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        await self.manager.refresh_message(self.recruitment_id)
        await interaction.followup.send("備考を変更しました。", ephemeral=True)


class RecruitmentDuplicateModal(discord.ui.Modal, title="募集を複製"):
    scheduled_at = discord.ui.TextInput(label="新しい開催日時（例 2026-08-05 20:30）", max_length=16)

    def __init__(self, manager: RecruitmentManager, recruitment_id: int, host_id: int) -> None:
        super().__init__()
        self.manager, self.recruitment_id, self.host_id = manager, recruitment_id, host_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        row = await database.get_recruitment(self.recruitment_id)
        if row is None or interaction.user.id != self.host_id or interaction.guild is None:
            return await interaction.response.send_message("主催中の募集だけ複製できます。", ephemeral=True)
        if not isinstance(interaction.user, discord.Member) or PRIVATE_ROOM_CREATOR_ROLE_NAME not in {
            role.name for role in interaction.user.roles
        }:
            return await interaction.response.send_message(
                f"**{PRIVATE_ROOM_CREATOR_ROLE_NAME}** ロールが無いため複製できません。",
                ephemeral=True,
            )
        try:
            local_start = datetime.strptime(str(self.scheduled_at), "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        except ValueError:
            return await interaction.response.send_message("日時は YYYY-MM-DD HH:MM 形式で入力してください。", ephemeral=True)
        now = datetime.now(JST)
        if local_start <= now or local_start > now + timedelta(days=RECRUITMENT_MAX_DAYS_AHEAD):
            return await interaction.response.send_message("開催日時は現在より後、7日以内にしてください。", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            new_id = await database.create_recruitment(
                interaction.guild.id, self.host_id, title=row["title"], scheduled_at=local_start,
                room_id=row["room_id"], streaming=row["streaming"],
                allowed_ranks=row["allowed_ranks"], note=row["note"],
            )
        except database.RecruitmentConflict as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        try:
            await self.manager.publish_new_recruitment(interaction.guild, new_id)
        except RuntimeError as exc:
            return await interaction.followup.send(str(exc), ephemeral=True)
        await interaction.followup.send(f"募集を複製しました（募集ID: {new_id}）。", ephemeral=True)


class RecruitmentContactModal(discord.ui.Modal, title="参加者へ一括連絡"):
    message = discord.ui.TextInput(label="連絡内容", max_length=1500, style=discord.TextStyle.paragraph)

    def __init__(self, manager: RecruitmentManager, recruitment_id: int, host_id: int) -> None:
        super().__init__()
        self.manager, self.recruitment_id, self.host_id = manager, recruitment_id, host_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        row = await database.get_recruitment(self.recruitment_id)
        if row is None or interaction.user.id != self.host_id or interaction.guild is None:
            return await interaction.response.send_message("主催者だけ操作できます。", ephemeral=True)
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
    def __init__(self, manager: RecruitmentManager, guild_id: int, owner_id: int, blocked_ids: list[int]) -> None:
        super().__init__(timeout=180)
        self.manager, self.guild_id, self.owner_id = manager, guild_id, owner_id
        user_select = discord.ui.UserSelect(
            placeholder="拒否するユーザーを追加", min_values=1, max_values=1,
            custom_id="player_blocks:add",
        )
        user_select.callback = self.add
        self.add_item(user_select)
        if blocked_ids:
            guild = manager.channel.guild if manager.channel else None
            options = [
                discord.SelectOption(
                    label=_display_name(guild, uid)[:100] if guild else f"ID:{uid}", value=str(uid),
                ) for uid in blocked_ids[:25]
            ]
            remove_select = discord.ui.Select(
                placeholder="拒否リストから解除", options=options,
                custom_id="player_blocks:remove", row=1,
            )
            remove_select.callback = self.remove
            self.add_item(remove_select)

    async def add(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id or interaction.guild is None:
            return await interaction.response.send_message("本人だけ操作できます。", ephemeral=True)
        selected = self.children[0].values[0]
        member = selected if isinstance(selected, discord.Member) else interaction.guild.get_member(selected.id)
        if member is None:
            return await interaction.response.send_message("対象が見つかりません。", ephemeral=True)
        if member.id == self.owner_id:
            return await interaction.response.send_message("自分自身は登録できません。", ephemeral=True)
        if member.bot:
            return await interaction.response.send_message("Botは登録できません。", ephemeral=True)
        try:
            count = await database.add_player_block(self.guild_id, self.owner_id, member.id)
        except (database.RecruitmentConflict, database.PlayerBlockLimitReached, ValueError) as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        blocked_ids = await database.list_player_blocks(self.guild_id, self.owner_id)
        await interaction.response.edit_message(
            content=f"同村拒否リスト: {len(blocked_ids)}/{PLAYER_BLOCK_LIMIT}",
            view=PlayerBlockSettingsView(self.manager, self.guild_id, self.owner_id, blocked_ids),
        )
        await self.manager.notify_block_added(interaction.guild, self.owner_id, member.id, count)

    async def remove(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("本人だけ操作できます。", ephemeral=True)
        selected_id = int(self.children[1].values[0])
        await database.remove_player_block(self.guild_id, self.owner_id, selected_id)
        blocked_ids = await database.list_player_blocks(self.guild_id, self.owner_id)
        await interaction.response.edit_message(
            content=f"同村拒否リスト: {len(blocked_ids)}/{PLAYER_BLOCK_LIMIT}",
            view=PlayerBlockSettingsView(self.manager, self.guild_id, self.owner_id, blocked_ids),
        )


class OperationsView(discord.ui.View):
    def __init__(self, manager: RecruitmentManager) -> None:
        super().__init__(timeout=None)
        self.manager = manager

    @staticmethod
    def _is_admin(interaction: discord.Interaction) -> bool:
        return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator

    @discord.ui.button(label="被拒否数の一覧", style=discord.ButtonStyle.secondary, custom_id="operations:block_counts")
    async def counts(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_admin(interaction) or interaction.guild is None:
            return await interaction.response.send_message("管理者のみ操作できます。", ephemeral=True)
        rows = await database.get_blocked_counts(interaction.guild.id)
        lines = [f"{_plain_identity(interaction.guild, row['blocked_id'])}: {row['count']}人" for row in rows]
        await interaction.response.send_message("\n".join(lines) or "登録はありません。", ephemeral=True)

    @discord.ui.button(label="GM解除", style=discord.ButtonStyle.danger, custom_id="operations:release_gm")
    async def release_gm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_admin(interaction):
            return await interaction.response.send_message("管理者のみ操作できます。", ephemeral=True)
        lobby_rooms = [room for room in self.manager.game_cog.rooms.values() if room.state.phase == Phase.LOBBY and room.state.gm_id]
        active = [room.state.room_name for room in self.manager.game_cog.rooms.values() if room.state.phase != Phase.LOBBY]
        if not lobby_rooms:
            suffix = "\n進行中のため対象外: " + ", ".join(active) if active else ""
            return await interaction.response.send_message("解除できるGMはいません。" + suffix, ephemeral=True)
        await interaction.response.send_message(
            "受付中の卓だけ選べます。" + ("\n進行中のため対象外: " + ", ".join(active) if active else ""),
            view=OperationsGMReleaseView(self.manager, lobby_rooms), ephemeral=True,
        )

    @discord.ui.button(label="募集の強制削除", style=discord.ButtonStyle.danger, custom_id="operations:archive_recruitment")
    async def archive_recruitment(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_admin(interaction) or interaction.guild is None:
            return await interaction.response.send_message("管理者のみ操作できます。", ephemeral=True)
        rows = await database.list_open_recruitments(interaction.guild.id)
        if not rows:
            return await interaction.response.send_message("募集中の募集はありません。", ephemeral=True)
        await interaction.response.send_message(
            "強制アーカイブする募集を選んでください。",
            view=OperationsRecruitmentArchiveView(self.manager, rows[:25]), ephemeral=True,
        )


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
            return await interaction.response.send_message("管理者のみ操作できます。", ephemeral=True)
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
            return await interaction.response.send_message("管理者のみ操作できます。", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        recruitment_id = int(self.children[0].values[0])
        await database.set_recruitment_status(recruitment_id, database.RECRUITMENT_ARCHIVED)
        await self.manager.refresh_message(recruitment_id)
        await interaction.followup.send("募集を強制アーカイブしました。", ephemeral=True)
