"""人狼ゲーム GameCog: 全卓の管理・Discordイベントのdispatch・スラッシュコマンド"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import (
    Phase, INITIAL_RATING, SEASON_LENGTH_DAYS,
    CH_STATS, CH_LOBBY, CH_MAYOR_INFO,
    STATS_PARENT_CHANNEL_NAME,
    MAYOR_INFO_CATEGORY_NAME, MAYOR_INFO_ADMIN_ONLY,
    PRIVATE_ROOM_CREATOR_ROLE_NAME,
    BULK_DISCORD_API_INTERVAL,
    ROOM_DEFINITIONS, RATED_ROOM_NAMES, RoomDefinition,
)
from permissions import RoomPermissionMixin, RoomVisibilityError
from room_runner import (  # noqa: F401  (互換のため再エクスポート)
    MUTE_MARKER_ROLE_PREFIX,
    ROLE_EMOJI,
    RoomRunner,
    member_roles_for_edit,
)
from views import DangerConfirmView, MayorInfoView, StatsView
from recruitment import RecruitmentManager
import database
import rating as rating_lib
import sounds

log = logging.getLogger(__name__)

class GameCog(RoomPermissionMixin, commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.managed_guild_id: Optional[int] = getattr(bot, "managed_guild_id", None)
        self.stats_channel: Optional[discord.TextChannel] = None
        self.stats_message: Optional[discord.Message] = None
        self.discord_api_sem = asyncio.Semaphore(5)
        self.rating_lock = asyncio.Lock()
        self.season_reset_lock = asyncio.Lock()
        self.private_room_lock = asyncio.Lock()
        # 「どの卓に所属するか」を変える操作 (参加・GM取得) を全卓で直列化する。
        # 各卓の action_lock は卓ローカルなので、卓Aが二重参加チェックを
        # 通過してからDM送信テスト (Discordへの往復) を待つ間に、卓Bが
        # 「まだどこにも参加していない」と判定して同じ人を登録できてしまう。
        # 判定から state.players / gm_id への書き込みまでをこのロックで囲む。
        self.join_lock = asyncio.Lock()
        # ゲーム開始処理 (ニックネーム変更/ロール付与/DM一斉送信) は
        # ギルド共有のメンバー編集バケットを長く占有するため、全卓で1件ずつ直列化する
        self.start_lock = asyncio.Lock()
        # start_lockは開始処理同士だけを直列化する。別卓の終了cleanupや
        # 復元・ランク/専用村同期とも高負荷APIが衝突しないよう共有する。
        # 実際のHTTP呼び出しは従来どおりdiscord_api_semの内側で行う。
        self.bulk_api_lock = asyncio.Lock()
        self.bulk_api_interval = BULK_DISCORD_API_INTERVAL
        self._bulk_api_next_at = 0.0
        # シーズンリマインダーの最終投稿時刻 (連投防止)
        self._season_reminder_last: Optional[datetime] = None
        self._settlement_recovered_since_notice = 0
        self._rating_recovery_needs_role_sync = False
        self._pending_settlement_fail_alerted: set[tuple[str, str]] = set()
        # 各卓のrestoreが終わる前でも、raw snapshotから
        # 「別卓の進行中VC」を判定する。起動時cleanupが
        # 先に処理した卓の残留muteを剥がすレースを防ぐ。
        self._startup_active_vc_rooms: dict[int, str] = {}
        # バックグラウンドタスクの参照保持 (asyncioはタスクを弱参照でしか
        # 持たないため、参照なしのcreate_taskは実行途中にGCで消えうる)
        self._bg_tasks: set[asyncio.Task] = set()
        # ゲーム終了時にVC未接続でサーバーミュートを解除できなかったメンバー。
        # どこかのVCへ入った時点で解除する (bot_metaに永続化 / guild_id -> ids)
        self.pending_unmutes: dict[int, set[int]] = {}
        # シーン切替SE (ギルド単位で再生を直列化)
        self.sound_player = sounds.SoundPlayer()
        self.rooms: dict[str, RoomRunner] = {
            room.room_id: RoomRunner(bot, self, room)
            for room in ROOM_DEFINITIONS
        }
        self.recruitment_manager = RecruitmentManager(bot, self)

    async def paced_discord_api_call(self, func, *args, **kwargs):
        """高負荷な連続APIを全卓で直列化し、最小間隔を空けて実行する。"""
        async with self.bulk_api_lock:
            loop = asyncio.get_running_loop()
            wait = self._bulk_api_next_at - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                async with self.discord_api_sem:
                    return await func(*args, **kwargs)
            finally:
                self._bulk_api_next_at = loop.time() + self.bulk_api_interval

    def spawn_bg_task(self, coro) -> asyncio.Task:
        """参照を保持したままバックグラウンドタスクを起動する"""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._on_bg_task_done)
        return task

    def _on_bg_task_done(self, task: asyncio.Task) -> None:
        self._bg_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            log.error(
                "バックグラウンドタスクが失敗しました: %r",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def cog_unload(self) -> None:
        if self.season_reminder_loop.is_running():
            self.season_reminder_loop.cancel()
        if self.daily_backup_loop.is_running():
            self.daily_backup_loop.cancel()
        if self.pending_settlement_retry_loop.is_running():
            self.pending_settlement_retry_loop.cancel()
        if self.recruitment_notification_loop.is_running():
            self.recruitment_notification_loop.cancel()
        tasks = set(self._bg_tasks)
        for room in self.rooms.values():
            game_task = room.state.game_task
            if game_task is not None and not game_task.done():
                tasks.add(game_task)
            # 朝パネルのViewも _game_views 経由でここで止まる
            room._stop_all_game_views()
            gm_view = getattr(room.state, "gm_panel_view", None)
            if gm_view is not None:
                gm_view.stop()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bg_tasks.clear()

    def _is_managed_guild(self, guild: Optional[discord.Guild]) -> bool:
        return (
            guild is not None
            and self.managed_guild_id is not None
            and guild.id == self.managed_guild_id
        )

    # ============================================================
    # 定期DBバックアップ (起動時バックアップとは別に24時間ごと)
    # ============================================================

    @tasks.loop(hours=24)
    async def daily_backup_loop(self) -> None:
        try:
            backup_path = await database.backup_db(label="daily")
            if backup_path:
                log.info(f"定期DBバックアップ作成: {backup_path}")
        except Exception as e:
            log.warning(f"定期DBバックアップ失敗: {e}")

    @daily_backup_loop.before_loop
    async def _daily_backup_wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def pending_settlement_retry_loop(self) -> None:
        get_guild = getattr(self.bot, "get_guild", None)
        if self.managed_guild_id is None or not callable(get_guild):
            return
        guild = get_guild(self.managed_guild_id)
        if guild is None:
            return
        try:
            await self._recover_pending_settlements(guild)
        except Exception as e:
            # tasks.loopは例外が外へ出ると停止するため、次回再試行を維持する。
            log.exception("未精算ゲーム定期再試行の準備に失敗: %s", e)

    @pending_settlement_retry_loop.before_loop
    async def _pending_settlement_retry_wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=10)
    async def recruitment_notification_loop(self) -> None:
        get_guild = getattr(self.bot, "get_guild", None)
        if self.managed_guild_id is None or not callable(get_guild):
            return
        guild = get_guild(self.managed_guild_id)
        if guild is None:
            return
        try:
            await self.recruitment_manager.process_notifications(guild)
        except Exception as exc:
            # 募集は付加機能。loopを止めず、ゲーム進行へも伝播させない。
            log.exception("募集通知・期限切れ処理に失敗: %s", exc)

    @recruitment_notification_loop.before_loop
    async def _recruitment_notification_wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    # ============================================================
    # シーズンリマインダー (3ヶ月経過で #統計 に通知。リセット自体は手動)
    # ============================================================

    SEASON_REMINDER_COOLDOWN_HOURS = 72
    SEASON_REMINDER_META_KEY = "season_reminder_last"
    _META_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

    @tasks.loop(hours=12)
    async def season_reminder_loop(self) -> None:
        ch = self.stats_channel
        if ch is None:
            return
        try:
            # 再起動後もクールダウンが効くよう、最終投稿時刻はDBから復元する
            if self._season_reminder_last is None:
                stored = await database.get_meta(ch.guild.id, self.SEASON_REMINDER_META_KEY)
                if stored:
                    try:
                        self._season_reminder_last = datetime.strptime(
                            stored, self._META_TIME_FORMAT
                        ).replace(tzinfo=timezone.utc)
                    except ValueError:
                        log.warning(f"シーズンリマインダー時刻を解釈できません: {stored}")

            start_text = await database.get_season_start(ch.guild.id)
            if not start_text:
                return
            try:
                start = datetime.strptime(
                    str(start_text), "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                log.warning(f"シーズン開始時刻を解釈できません: {start_text}")
                return

            now = datetime.now(timezone.utc)
            elapsed_days = (now - start).days
            if elapsed_days < SEASON_LENGTH_DAYS:
                return
            if (
                self._season_reminder_last is not None
                and (now - self._season_reminder_last).total_seconds()
                < self.SEASON_REMINDER_COOLDOWN_HOURS * 3600
            ):
                return

            embed = discord.Embed(
                title="📅 シーズン更新のお知らせ",
                description=(
                    f"現行シーズンの開始から **{elapsed_days}日** が経過しました"
                    f" (シーズン長の目安: {SEASON_LENGTH_DAYS}日)。\n"
                    "サーバー管理者は `/season_reset` の実行を検討してください。\n"
                    f"(全員のレートが `{INITIAL_RATING} + (現在 - {INITIAL_RATING}) ÷ 2` に圧縮され、"
                    "今季戦績がリセットされて新シーズンが始まります)"
                ),
                color=discord.Color.orange(),
            )
            await ch.send(embed=embed)
            self._season_reminder_last = now
            await database.set_meta(
                ch.guild.id,
                self.SEASON_REMINDER_META_KEY,
                now.strftime(self._META_TIME_FORMAT),
            )
        except Exception as e:
            log.warning(f"シーズンリマインダー処理失敗: {e}")

    @season_reminder_loop.before_loop
    async def _season_reminder_wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    def has_active_rated_games(self) -> bool:
        return any(
            room.is_rated_room()
            and room.state.phase not in (Phase.LOBBY, Phase.GAME_OVER)
            for room in self.rooms.values()
        )

    async def _ensure_stats_channel(self, guild: discord.Guild) -> discord.TextChannel:
        # 一度確定したチャンネルはIDで追跡し、ユーザーが調整した位置や
        # カテゴリを起動時に変更しない。
        stats = None
        stored_id = await database.get_meta(guild.id, "stats_channel_id")
        if stored_id is not None:
            try:
                channel_id = int(stored_id)
            except (TypeError, ValueError):
                channel_id = 0
            get_channel = getattr(guild, "get_channel", None)
            candidate = get_channel(channel_id) if callable(get_channel) else None
            if candidate in guild.text_channels:
                stats = candidate

        if stats is None:
            candidates = sorted(
                (channel for channel in guild.text_channels if channel.name == CH_STATS),
                key=lambda channel: channel.id,
            )
            stats = candidates[0] if candidates else None
        if stats is None:
            general = next(
                (
                    channel for channel in guild.text_channels
                    if channel.name == STATS_PARENT_CHANNEL_NAME
                    and channel.category is None
                ),
                None,
            )
            create_options = {"category": None}
            if general is not None:
                create_options["position"] = general.position + 1
            stats = await guild.create_text_channel(CH_STATS, **create_options)
        await database.set_meta(guild.id, "stats_channel_id", str(stats.id))

        try:
            await self._set_permission_if_changed(
                stats,
                guild.default_role,
                discord.PermissionOverwrite(
                    view_channel=True,
                    read_messages=True,
                    send_messages=False,
                ),
                reason="統計チャンネル権限更新",
            )
            await self._set_permission_if_changed(
                stats,
                guild.me,
                discord.PermissionOverwrite(
                    view_channel=True,
                    read_messages=True,
                    send_messages=True,
                    manage_channels=True,
                ),
                reason="統計チャンネル権限更新",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"統計チャンネル権限更新失敗: {e}")
        for role in guild.roles:
            if role.name not in rating_lib.all_rank_role_names():
                continue
            try:
                await self._set_permission_if_changed(
                    stats, role, None, reason="統計チャンネル権限更新"
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"統計チャンネルランク個別権限解除失敗 ({role.name}): {e}")
        return stats

    async def _ensure_mayor_info_channel(self, guild: discord.Guild) -> discord.TextChannel:
        mayor_role = discord.utils.get(guild.roles, name=PRIVATE_ROOM_CREATOR_ROLE_NAME)
        category_overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False, read_messages=False, connect=False
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, read_messages=True, send_messages=True,
                connect=True, manage_channels=True,
            ),
        }
        if mayor_role is not None and not MAYOR_INFO_ADMIN_ONLY:
            category_overwrites[mayor_role] = discord.PermissionOverwrite(
                view_channel=True, read_messages=True, send_messages=False, connect=True
            )
        category = discord.utils.get(guild.categories, name=MAYOR_INFO_CATEGORY_NAME)
        if category is None:
            category = await guild.create_category(
                MAYOR_INFO_CATEGORY_NAME, overwrites=category_overwrites
            )
        channel = discord.utils.get(guild.text_channels, name=CH_MAYOR_INFO, category=category)
        channel_overwrites = dict(category_overwrites)
        channel_overwrites[guild.default_role] = discord.PermissionOverwrite(
            view_channel=False, read_messages=False, send_messages=False
        )
        if channel is None:
            channel = await guild.create_text_channel(
                CH_MAYOR_INFO, category=category, overwrites=channel_overwrites
            )

        try:
            for target, overwrite in category_overwrites.items():
                await self._set_permission_if_changed(
                    category,
                    target,
                    overwrite,
                    reason="村長説明カテゴリ権限更新",
                )
            for target, overwrite in channel_overwrites.items():
                await self._set_permission_if_changed(
                    channel,
                    target,
                    overwrite,
                    reason="村長説明チャンネル権限更新",
                )
        except (discord.Forbidden, discord.HTTPException) as e:
            raise RoomVisibilityError(f"村長説明チャンネル権限更新失敗: {e}") from e

        if mayor_role is None and not MAYOR_INFO_ADMIN_ONLY:
            log.warning(
                f"{PRIVATE_ROOM_CREATOR_ROLE_NAME}ロールが見つからないため、"
                "村長説明カテゴリの閲覧許可を付与できません"
            )
        if MAYOR_INFO_ADMIN_ONLY:
            # 以前の村長ロール・個人・役職へのallowを残すと非管理者から見えるため、
            # 現在の管理対象以外の閲覧許可はカテゴリとチャンネルの両方から外す。
            await self._remove_stale_visibility_allows(
                guild,
                category,
                set(category_overwrites),
                label=f"カテゴリ {MAYOR_INFO_CATEGORY_NAME}",
            )
            await self._remove_stale_visibility_allows(
                guild,
                channel,
                set(channel_overwrites),
                label=f"チャンネル {MAYOR_INFO_CATEGORY_NAME}/{CH_MAYOR_INFO}",
            )

        await self._purge_bot_messages(channel, "村長説明")
        embed = discord.Embed(
            title="村長ロール説明",
            description=(
                f"**{PRIVATE_ROOM_CREATOR_ROLE_NAME}** ロールを持っている人は、自分専用の人狼村を1つ作れます。\n"
                "作成後は専用村の参加受付にある「専用村管理」ボタンから、参加者の招待と削除を操作できます。"
            ),
            color=discord.Color.dark_gold(),
        )
        embed.add_field(
            name="できること",
            value=(
                "専用村を作成\n"
                "村名を変更\n"
                "専用村を削除\n"
                "自分の専用村に参加できる人を招待・削除"
            ),
            inline=False,
        )
        embed.add_field(
            name="制限",
            value=(
                "専用村は1人1つまでです。\n"
                "村長本人だけが専用村のGMになれます。\n"
                f"**{PRIVATE_ROOM_CREATOR_ROLE_NAME}** ロールが外れると、専用村は自動削除されます。"
            ),
            inline=False,
        )
        embed.add_field(
            name="ランク対象",
            value=(
                f"レートとランクが変動するのは {' / '.join(RATED_ROOM_NAMES)} "
                f"の{len(RATED_ROOM_NAMES)}卓です。\n"
                "村長専用村では、レート、ランク、ランクロール、今季戦績は変動しません。"
            ),
            inline=False,
        )
        embed.add_field(
            name="注意",
            value=(
                "削除すると専用村カテゴリ、専用村ロール、招待リストが消えます。\n"
                "ゲーム中の専用村は、先にゲームを終了してから削除してください。"
            ),
            inline=False,
        )
        for attempt in range(3):
            try:
                await channel.send(embed=embed, view=MayorInfoView(self))
                break
            except discord.HTTPException as e:
                if attempt >= 2:
                    log.warning(f"村長説明メッセージ投稿失敗: {e}")
                    return channel
                await asyncio.sleep(2 * (attempt + 1))
        return channel

    def _private_room_definition_from_row(self, row: dict) -> RoomDefinition:
        return RoomDefinition(
            room_id=row["room_id"],
            name=row["room_name"],
            allowed_gm_user_ids=frozenset({row["owner_id"]}),
            private_owner_id=row["owner_id"],
            private_role_name=row["role_name"],
        )

    async def _load_private_room_runners(self, guild: discord.Guild) -> None:
        rows = await database.load_private_rooms(guild.id)
        for row in [item for item in rows if item.get("status") == "deleting"]:
            await self._delete_private_room_by_row(
                guild, row, reason="中断された専用村削除を再試行"
            )
        rows = await database.load_private_rooms(guild.id)
        for row in [item for item in rows if item.get("status") == "renaming"]:
            await self._reconcile_private_room_rename(guild, row)
        rows = await database.load_private_rooms(guild.id)
        private_room_ids = {row["room_id"] for row in rows}
        for room_id in [
            room_id for room_id, room in self.rooms.items()
            if room.room_def.private_owner_id is not None and room_id not in private_room_ids
        ]:
            del self.rooms[room_id]

        for row in rows:
            if row.get("status") in {"deleting", "renaming", "error"}:
                # 外部操作が未完了・隔離済みの卓は、UI/event dispatchへ戻さない。
                continue
            room_def = self._private_room_definition_from_row(row)
            self.rooms[room_def.room_id] = RoomRunner(self.bot, self, room_def)

    async def _reconcile_private_room_rename(
        self, guild: discord.Guild, row: dict
    ) -> bool:
        """DBへ記録済みの改名intentをDiscordへ反映する。"""
        errors: list[str] = []
        get_category = getattr(guild, "get_channel", None)
        category = (
            get_category(row.get("category_id"))
            if row.get("category_id") and callable(get_category) else None
        )
        if not isinstance(category, discord.CategoryChannel):
            category = discord.utils.get(guild.categories, name=row["room_name"])

        get_role = getattr(guild, "get_role", None)
        role = (
            get_role(row.get("role_id"))
            if row.get("role_id") and callable(get_role) else None
        )
        if role is None:
            role = discord.utils.get(guild.roles, name=row["role_name"])

        if category is None:
            errors.append("カテゴリが見つかりません")
        if role is None:
            errors.append("ロールが見つかりません")

        if role is not None and role.name != row["role_name"]:
            try:
                await self.paced_discord_api_call(
                    role.edit,
                    name=row["role_name"],
                    reason="中断された専用村名変更を再開",
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                errors.append(f"ロール {role.id}: {e}")
        if category is not None and category.name != row["room_name"]:
            try:
                await self.paced_discord_api_call(
                    category.edit,
                    name=row["room_name"],
                    reason="中断された専用村名変更を再開",
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                errors.append(f"カテゴリ {category.id}: {e}")

        if errors:
            await database.mark_private_room_status(
                guild.id, row["room_id"], "renaming", error=" | ".join(errors)[:2000]
            )
            log.error("専用村名変更の復旧失敗 (%s): %s", row["room_id"], errors)
            return False
        await database.mark_private_room_active(
            guild.id,
            row["room_id"],
            category_id=category.id if category is not None else row.get("category_id"),
            role_id=role.id if role is not None else row.get("role_id"),
        )
        return True

    async def _ensure_private_room_role(
        self,
        guild: discord.Guild,
        room_def: RoomDefinition,
    ) -> Optional[discord.Role]:
        if room_def.private_role_name is None:
            return None
        role = discord.utils.get(guild.roles, name=room_def.private_role_name)
        if role is None:
            try:
                role = await self.paced_discord_api_call(
                    guild.create_role,
                    name=room_def.private_role_name,
                    reason="専用村ロール自動作成",
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"専用村ロール作成失敗 ({room_def.private_role_name}): {e}")
                return None

        if room_def.private_owner_id is not None:
            owner = guild.get_member(room_def.private_owner_id)
            if owner is not None and role not in owner.roles:
                try:
                    await self.paced_discord_api_call(
                        owner.add_roles,
                        role,
                        reason="専用村オーナーロール付与",
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"専用村オーナーロール付与失敗 ({owner.display_name}): {e}")
        return role

    async def _sync_private_room_member_roles(
        self,
        guild: discord.Guild,
        room_def: RoomDefinition,
    ) -> None:
        """DBの招待intentをDiscordロールへ収束させ、無記録アクセスを剥がす。"""
        role = await self._ensure_private_room_role(guild, room_def)
        if role is None:
            return
        records = await database.load_private_room_members(guild.id, room_def.room_id)
        desired_ids = {
            item["member_id"] for item in records
            if item["status"] in {"active", "adding"}
        }
        for item in records:
            member_id = item["member_id"]
            member = guild.get_member(member_id)
            if item["status"] == "removing":
                if member is not None and role in member.roles:
                    try:
                        await self.paced_discord_api_call(
                            member.remove_roles,
                            role,
                            reason="専用村招待削除の再開",
                        )
                    except (discord.Forbidden, discord.HTTPException) as e:
                        await database.mark_private_room_member_error(
                            guild.id, room_def.room_id, member_id, str(e)
                        )
                        log.warning("専用村招待削除の復旧失敗 (%s): %s", member_id, e)
                        continue
                await database.remove_private_room_member(guild.id, room_def.room_id, member_id)
                continue

            if member is None:
                # サーバーへ戻った時にon_member_joinで再付与するためintentは保持する。
                continue
            if role not in member.roles:
                try:
                    await self.paced_discord_api_call(
                        member.add_roles,
                        role,
                        reason="専用村招待ロール同期",
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    await database.mark_private_room_member_error(
                        guild.id, room_def.room_id, member_id, str(e)
                    )
                    log.warning(f"専用村招待ロール同期失敗 ({member.display_name}): {e}")
                    continue
            await database.add_private_room_member(guild.id, room_def.room_id, member_id)

        # ロールだけ付いてDBに招待記録が無いメンバーは、クラッシュで残った
        # 無記録アクセスとみなしfail-closedで剥がす。
        for member in list(getattr(guild, "members", [])):
            if member.id in desired_ids or role not in member.roles:
                continue
            try:
                await self.paced_discord_api_call(
                    member.remove_roles,
                    role,
                    reason="無記録の専用村アクセスを解除",
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.error("無記録の専用村ロール解除失敗 (%s/%s): %s", room_def.room_id, member.id, e)

    async def _delete_private_room_by_row(
        self,
        guild: discord.Guild,
        row: dict,
        *,
        reason: str,
    ) -> bool:
        await database.mark_private_room_status(
            guild.id, row["room_id"], "deleting", error=None
        )
        errors: list[str] = []
        # 最初にdispatch対象から外す。Discord削除の一部が失敗しても、残存する
        # ボタンやチャンネルから削除中のrunnerを操作できないようにする。
        room = self.rooms.pop(row["room_id"], None)
        if room is not None and room.state.phase not in (Phase.LOBBY, Phase.GAME_OVER):
            try:
                await room.force_end(reason)
            except Exception as e:
                log.warning(f"専用村削除前の強制終了失敗 ({row['room_name']}): {e}")
                errors.append(f"ゲーム終了: {e}")
        if room is not None:
            room.state.ending = True
            room.state.phase = Phase.GAME_OVER
            # 朝パネルのViewも _game_views 経由でここで止まる
            room._stop_all_game_views()
            room._morning_view = None
            room.state.morning_panel_message = None
            room.state.morning_panel_message_id = None
            gm_view = room.state.gm_panel_view
            if gm_view is not None:
                gm_view.stop()
            # LobbyViewは参照を保持していないため、既知メッセージからViewを剥がす。
            for message in (room.state.lobby_message, room.state.gm_panel_message):
                if message is None:
                    continue
                try:
                    await self.paced_discord_api_call(message.edit, view=None)
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("専用村削除中のUI停止失敗 (%s): %s", row["room_id"], e)

        get_channel = getattr(guild, "get_channel", None)
        category = (
            get_channel(row.get("category_id"))
            if row.get("category_id") and callable(get_channel) else None
        )
        if not isinstance(category, discord.CategoryChannel):
            category = discord.utils.get(guild.categories, name=row["room_name"])
        if category is not None:
            child_delete_failed = False
            for ch in list(getattr(category, "channels", [])):
                try:
                    await self.paced_discord_api_call(ch.delete, reason=reason)
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"専用村チャンネル削除失敗 ({row['room_name']}/{ch.name}): {e}")
                    errors.append(f"チャンネル {ch.id}: {e}")
                    child_delete_failed = True
            # 子削除に失敗した状態でカテゴリだけ消すと、子がuncategorizedの
            # 孤立チャンネルになりstable IDから再試行できなくなる。
            if not child_delete_failed:
                try:
                    await self.paced_discord_api_call(category.delete, reason=reason)
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"専用村カテゴリ削除失敗 ({row['room_name']}): {e}")
                    errors.append(f"カテゴリ {category.id}: {e}")

        get_role = getattr(guild, "get_role", None)
        role = (
            get_role(row.get("role_id"))
            if row.get("role_id") and callable(get_role) else None
        )
        if role is None:
            role = discord.utils.get(guild.roles, name=row["role_name"])
        marker_role = discord.utils.get(
            guild.roles,
            name=f"{MUTE_MARKER_ROLE_PREFIX}{row['room_id']}",
        )
        roles_to_delete = [
            target for target in (role, marker_role) if target is not None
        ]
        for target_role in roles_to_delete:
            try:
                await self.paced_discord_api_call(target_role.delete, reason=reason)
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"専用村ロール削除失敗 ({target_role.name}): {e}")
                errors.append(f"ロール {target_role.id}: {e}")

        if errors:
            await database.mark_private_room_status(
                guild.id, row["room_id"], "deleting", error=" | ".join(errors)[:2000]
            )
            return False

        await database.delete_private_room(guild.id, row["room_id"])
        return True

    async def _cleanup_private_rooms_without_creator_role(self, guild: discord.Guild) -> None:
        rows = await database.load_private_rooms(guild.id)
        for row in rows:
            owner = guild.get_member(row["owner_id"])
            if owner is None:
                try:
                    owner = await guild.fetch_member(row["owner_id"])
                except discord.NotFound:
                    owner = None
                except discord.HTTPException as e:
                    log.warning(f"専用村オーナー確認失敗 ({row['room_name']} / {row['owner_id']}): {e}")
                    continue
            if owner is not None and self._has_private_room_creator_role(owner):
                continue
            await self._delete_private_room_by_row(
                guild,
                row,
                reason=f"{PRIVATE_ROOM_CREATOR_ROLE_NAME}ロール未保持のため専用村削除",
            )

    async def add_private_room_member(self, room: RoomRunner, member: discord.Member) -> str:
        async with self.private_room_lock:
            return await self._add_private_room_member_locked(room, member)

    async def _add_private_room_member_locked(
        self, room: RoomRunner, member: discord.Member
    ) -> str:
        guild = member.guild
        role = await self._ensure_private_room_role(guild, room.room_def)
        if role is None:
            return "専用村ロールを作成または取得できませんでした。Botのロール管理権限を確認してください。"
        if member.bot:
            return "Botは招待できません。"
        try:
            await database.stage_private_room_member_add(
                guild.id, room.state.room_id, member.id
            )
        except Exception as e:
            log.exception("専用村招待intent保存失敗 (%s): %s", member.id, e)
            return "招待内容を安全に保存できなかったため、ロールは変更しませんでした。"
        if role in member.roles:
            await database.add_private_room_member(guild.id, room.state.room_id, member.id)
            return f"{member.display_name} は既に招待済みです。"
        try:
            async with self.discord_api_sem:
                await member.add_roles(role, reason=f"{room.state.room_name} へ招待")
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"専用村招待ロール付与失敗 ({member.display_name}): {e}")
            # 外部操作が失敗したため、先行journalをロールバックする。
            try:
                await database.remove_private_room_member(
                    guild.id, room.state.room_id, member.id
                )
            except Exception as rollback_error:
                log.exception("専用村招待intent取消失敗 (%s): %s", member.id, rollback_error)
                try:
                    await database.stage_private_room_member_remove(
                        guild.id, room.state.room_id, member.id
                    )
                except Exception:
                    log.exception("専用村招待intentをremovingへ変更できませんでした")
            return "ロール付与に失敗しました。Botのロール位置とロール管理権限を確認してください。"
        try:
            await database.add_private_room_member(guild.id, room.state.room_id, member.id)
        except Exception as e:
            # adding intentは残っており、起動時reconcileでactiveへ確定できる。
            log.exception("専用村招待確定失敗 (%s): %s", member.id, e)
            return f"{member.display_name} を招待しました。記録の確定は自動再試行されます。"
        return f"{member.display_name} を {room.state.room_name} に招待しました。"

    async def remove_private_room_member(self, room: RoomRunner, member: discord.Member) -> str:
        async with self.private_room_lock:
            return await self._remove_private_room_member_locked(room, member)

    async def _remove_private_room_member_locked(
        self, room: RoomRunner, member: discord.Member
    ) -> str:
        guild = member.guild
        if member.id == room.room_def.private_owner_id:
            return "村主は削除できません。専用村自体を削除してください。"
        role = discord.utils.get(guild.roles, name=room.room_def.private_role_name)
        try:
            await database.stage_private_room_member_remove(
                guild.id, room.state.room_id, member.id
            )
        except Exception as e:
            log.exception("専用村招待削除intent保存失敗 (%s): %s", member.id, e)
            return "削除内容を安全に保存できなかったため、ロールは変更しませんでした。"
        if role is not None and role in member.roles:
            try:
                async with self.discord_api_sem:
                    await member.remove_roles(role, reason=f"{room.state.room_name} から削除")
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"専用村招待ロール削除失敗 ({member.display_name}): {e}")
                await database.mark_private_room_member_error(
                    guild.id, room.state.room_id, member.id, str(e)
                )
                return "ロール削除に失敗しました。削除intentを保持し、起動時に再試行します。"
        await database.remove_private_room_member(guild.id, room.state.room_id, member.id)
        return f"{member.display_name} を {room.state.room_name} から削除しました。"

    async def _purge_bot_messages(self, ch: discord.TextChannel, label: str) -> None:
        try:
            await ch.purge(limit=50, check=lambda m: m.author == self.bot.user)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"{label}メッセージ削除失敗: {e}")

    async def _post_stats_ui(self) -> None:
        if self.stats_channel is None:
            return

        await self._purge_bot_messages(self.stats_channel, "統計")
        view = StatsView(self)
        embed = discord.Embed(
            title="人狼ゲーム 統計",
            description="現在シーズンの統計、ランキング、前シーズン結果、最近の試合をここで確認できます。",
            color=discord.Color.blue(),
        )
        self.stats_message = await self.stats_channel.send(embed=embed, view=view)

    async def _sync_all_rank_roles(
        self, guild: discord.Guild, *, paced: bool = False
    ) -> tuple[int, int, int]:
        """全プレイヤーのランクロールを現在ランクへ同期する。

        paced=True は低速モード (1人ごとに小休止)。シーズンリセット直後など
        大量のロール変更が出る場面で、メンバー編集バケットを占有して
        進行中ゲームのニックネーム変更等を遅らせないために使う。
        """
        roles_map = await self._ensure_rank_roles(guild)
        all_rows = await database.get_all_player_ratings(guild.id)
        # get_current_rank_map は内部で get_all_player_ratings を呼び直すため、
        # 取得済みの all_rows から直接ランクを組み立てて全件読み出しの重複を避ける
        rank_map = rating_lib.build_rank_context_map(all_rows)
        synced = 0
        skipped = 0
        failed = 0

        for row in all_rows:
            member = guild.get_member(row["player_id"])
            rank_ctx = rank_map.get(row["player_id"])
            if member is None or rank_ctx is None:
                skipped += 1
                continue
            try:
                outcome = await self._sync_rank_role(member, rank_ctx.rank_name, roles_map=roles_map)
                if outcome == "updated":
                    synced += 1
                elif outcome == "skipped":
                    skipped += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                log.warning(f"統計ランクロール同期失敗 ({member.display_name}): {e}")
            if paced:
                await asyncio.sleep(0.3)

        return synced, skipped, failed

    async def _resync_roles_after_reset(self, guild: discord.Guild) -> None:
        """シーズンリセット後のランクロール再同期 (バックグラウンド・低速)"""
        try:
            synced, skipped, failed = await self._sync_all_rank_roles(guild, paced=True)
            log.info(
                f"リセット後ロール再同期完了: 同期{synced}人 / 未在籍{skipped}人 / 失敗{failed}人"
            )
            if self.stats_channel is not None:
                await self.stats_channel.send(
                    f"🏷️ シーズンリセット後のランクロール再同期が完了しました: "
                    f"同期{synced}人 / 未在籍{skipped}人 / 失敗{failed}人"
                )
        except Exception as e:
            log.exception(f"リセット後ロール再同期失敗: {e}")

    async def _recover_pending_settlements(self, guild: discord.Guild) -> None:
        pending = await database.load_pending_game_settlements(guild.id)
        recovered = 0
        failed_keys: list[tuple[str, str]] = []
        for item in pending:
            key = (item["room_id"], item["game_run_id"])
            try:
                async with self.rating_lock:
                    game_id, _, created = await database.settle_game_settlement(
                        guild.id, item["room_id"], item["game_run_id"]
                    )
                log.warning(
                    "未精算ゲームを自動精算しました: %s/%s game_id=%s created=%s",
                    item["room_id"], item["game_run_id"], game_id, created,
                )
                recovered += 1
                self._pending_settlement_fail_alerted.discard(key)
            except Exception as e:
                # pending行を残す。次回起動または管理者の再試行で回収できる
                log.exception(
                    "未精算ゲームの自動精算に失敗: %s/%s: %s",
                    item["room_id"], item["game_run_id"], e,
                )
                failed_keys.append(key)

        # 推薦受付中の再起動でバックグラウンド集計タスクが失われても、
        # 期限後に確定済み票を一度だけ反映する。
        recommendation_game_ids = await database.load_expired_recommendation_game_ids(
            guild.id
        )
        recovered_recommendations = 0
        for game_id in recommendation_game_ids:
            try:
                async with self.rating_lock:
                    results = await database.finalize_game_recommendations(
                        game_id, guild.id, close_pending=True
                    )
                recovered_recommendations += sum(item["bonus"] for item in results)
            except Exception as e:
                log.exception(
                    "終了後推薦の自動復旧に失敗: game_id=%s: %s", game_id, e
                )
        if recovered_recommendations:
            log.warning(
                "終了後推薦を自動復旧しました: %s票", recovered_recommendations
            )
            self._rating_recovery_needs_role_sync = True

        if recovered:
            self._settlement_recovered_since_notice += recovered
            self._rating_recovery_needs_role_sync = True
        if self._rating_recovery_needs_role_sync:
            # rating結果の復旧では終了時通知が失われるため、少なくとも
            # 現在ランクロールを全員分収束させる。
            if self.stats_channel is not None:
                try:
                    await self._sync_all_rank_roles(guild)
                except Exception as e:
                    log.exception("未精算ゲーム復旧後のランクロール同期失敗: %s", e)
                else:
                    self._rating_recovery_needs_role_sync = False
        if self.stats_channel is not None and self._settlement_recovered_since_notice:
            count = self._settlement_recovered_since_notice
            try:
                await self.stats_channel.send(
                    f"✅ 未精算だったゲーム **{count}件** の戦績・レートを自動復旧しました。"
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning("未精算ゲーム復旧通知に失敗: %s", e)
            else:
                self._settlement_recovered_since_notice = 0
        if self.stats_channel is not None:
            new_failures = [
                key for key in failed_keys
                if key not in self._pending_settlement_fail_alerted
            ]
            if new_failures:
                try:
                    await self.stats_channel.send(
                        f"⚠️ 未精算ゲーム **{len(new_failures)}件** の自動復旧に失敗しました。"
                        "5分ごとに再試行します。"
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("未精算ゲーム失敗通知に失敗: %s", e)
                else:
                    self._pending_settlement_fail_alerted.update(new_failures)

    async def setup_channels(self, guild: discord.Guild) -> None:
        if self.managed_guild_id is None:
            # シミュレータ等、bot.pyを経由しない単一guild実行
            self.managed_guild_id = guild.id
        if not self._is_managed_guild(guild):
            raise RuntimeError(f"管理対象外サーバーのセットアップを拒否しました: {guild.id}")
        log.info("チャンネルセットアップ開始")
        await self._recover_pending_settlements(guild)
        await self.load_pending_unmutes(guild)
        await self._cleanup_private_rooms_without_creator_role(guild)
        log.info("村長ロール未保持の専用村クリーンアップ完了")
        await self._load_private_room_runners(guild)
        log.info("専用村読み込み完了")
        snapshots = await database.load_room_states(guild.id)
        quarantined_room_ids = await database.load_unresolved_room_state_quarantine_ids(
            guild.id
        )
        self._startup_active_vc_rooms = {
            int(payload.get("channel_ids", {}).get("voice")): room_id
            for room_id, payload in snapshots.items()
            if payload.get("phase") not in (Phase.LOBBY.name, Phase.GAME_OVER.name)
            and payload.get("channel_ids", {}).get("voice") is not None
        }
        log.info("ルーム状態読み込み完了")
        await self._ensure_rank_roles(guild)
        log.info("ランクロール確認完了")
        self.stats_channel = await self._ensure_stats_channel(guild)
        log.info(f"統計チャンネル確認完了: #{CH_STATS} (ID: {self.stats_channel.id})")
        if self._rating_recovery_needs_role_sync:
            try:
                await self._sync_all_rank_roles(guild)
            except Exception as e:
                log.exception("起動時レート復旧後のランクロール同期失敗: %s", e)
            else:
                self._rating_recovery_needs_role_sync = False
        if self._settlement_recovered_since_notice:
            try:
                await self.stats_channel.send(
                    f"✅ 起動時に未精算ゲーム **{self._settlement_recovered_since_notice}件** "
                    "の戦績・レートを自動復旧しました。"
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning("起動時未精算ゲーム復旧通知に失敗: %s", e)
            else:
                self._settlement_recovered_since_notice = 0
        await self._ensure_mayor_info_channel(guild)
        log.info(f"村長説明チャンネル確認完了: {MAYOR_INFO_CATEGORY_NAME}/#{CH_MAYOR_INFO}")
        fixed_room_errors: list[str] = []
        try:
            for room in self.rooms.values():
                log.info(f"卓セットアップ開始: {room.state.room_name}")
                snapshot = snapshots.get(room.state.room_id)
                try:
                    if room.state.room_id in quarantined_room_ids:
                        raise RuntimeError(
                            "卓の復元snapshotが隔離されています。"
                            "#昼/#霊界を保持したまま起動を停止しました。DBとログを確認してください"
                        )
                    await room.setup_channels(
                        guild, snapshot=snapshot, stats_channel=self.stats_channel
                    )
                    await room.restore_from_snapshot(snapshot)
                    if room.is_private_room():
                        private_role = discord.utils.get(
                            guild.roles, name=room.room_def.private_role_name
                        )
                        await database.mark_private_room_active(
                            guild.id,
                            room.state.room_id,
                            category_id=room.state.category.id if room.state.category else None,
                            role_id=private_role.id if private_role else None,
                        )
                    log.info(f"卓セットアップ完了: {room.state.room_name}")
                except Exception as e:
                    log.exception(f"卓セットアップ失敗: {room.state.room_name}: {e}")
                    if room.is_private_room():
                        await database.mark_private_room_status(
                            guild.id, room.state.room_id, "error", error=str(e)[:2000]
                        )
                        room.state.ending = True
                        room._stop_all_game_views()
                    else:
                        fixed_room_errors.append(f"{room.state.room_name}: {e}")
        finally:
            # raw snapshotの判定は卓を順番にrestoreしている間だけ必要。
            # 復元失敗卓まで永久にactiveと見なすとpending muteが
            # 解除されないため、以後は動的なroom状態だけを正とする。
            self._startup_active_vc_rooms.clear()
        for room_id, room in list(self.rooms.items()):
            if room.is_private_room():
                row = await database.get_private_room_by_name(guild.id, room.state.room_name)
                if row is not None and row.get("status") == "error":
                    self.rooms.pop(room_id, None)
        if fixed_room_errors:
            raise RuntimeError(
                "固定卓のセットアップに失敗しました: " + " | ".join(fixed_room_errors)
            )
        try:
            await self._post_stats_ui()
            log.info("統計UI投稿完了")
        except Exception as e:
            log.exception(f"統計UI投稿失敗: {e}")
        try:
            await self.recruitment_manager.setup(guild)
            log.info("募集・運営UIセットアップ完了")
        except Exception as e:
            # 予約層の不調で固定卓の起動を失敗させない。
            log.exception(f"募集・運営UIセットアップ失敗 (ゲーム卓は継続): {e}")
        synced, skipped, failed = await self._sync_all_rank_roles(guild)
        log.info(
            f"統計ランクロール自動同期完了: 同期{synced}人 / 未在籍{skipped}人 / 失敗{failed}人"
        )
        # シーズンリマインダー開始 (シミュレータのFakeBotでは起動しない)
        if hasattr(self.bot, "wait_until_ready") and not self.season_reminder_loop.is_running():
            self.season_reminder_loop.start()
            log.info(f"シーズンリマインダー開始 (シーズン長 {SEASON_LENGTH_DAYS}日 / 12時間ごとに確認)")
        if hasattr(self.bot, "wait_until_ready") and not self.daily_backup_loop.is_running():
            self.daily_backup_loop.start()
            log.info("定期DBバックアップ開始 (24時間ごと)")
        if (
            hasattr(self.bot, "wait_until_ready")
            and not self.pending_settlement_retry_loop.is_running()
        ):
            self.pending_settlement_retry_loop.start()
            log.info("未精算ゲームの自動再試行開始 (5分ごと)")
        if (
            hasattr(self.bot, "wait_until_ready")
            and not self.recruitment_notification_loop.is_running()
        ):
            self.recruitment_notification_loop.start()
            log.info("募集通知・期限切れ確認開始 (10分ごと)")

    def is_other_active_game_vc(
        self, channel_id: Optional[int], exclude_room_id: Optional[str] = None
    ) -> bool:
        """指定VCが「別卓の進行中ゲーム」のVCかどうか。

        終了処理や持ち越しミュート解除が、進行中の別卓のミュートを
        剥がしてしまうのを防ぐために使う。
        """
        if channel_id is None:
            return False
        startup_room_id = self._startup_active_vc_rooms.get(channel_id)
        if startup_room_id is not None and startup_room_id != exclude_room_id:
            return True
        for room in self.rooms.values():
            if exclude_room_id is not None and room.state.room_id == exclude_room_id:
                continue
            vc = room.state.voice_channel
            if vc is not None and vc.id == channel_id and room._is_game_in_progress():
                return True
        return False

    def find_user_room(self, user_id: int, exclude_room_id: Optional[str] = None) -> Optional[RoomRunner]:
        for room in self.rooms.values():
            if exclude_room_id is not None and room.state.room_id == exclude_room_id:
                continue
            if room.state.gm_id == user_id or user_id in room.state.players:
                return room
        return None

    async def _ensure_rank_roles(self, guild: discord.Guild) -> dict[str, discord.Role]:
        existing = {r.name: r for r in guild.roles}
        result: dict[str, discord.Role] = {}
        for role_name, color_int in rating_lib.all_rank_role_specs():
            role = existing.get(role_name)
            if role is None:
                try:
                    role = await self.paced_discord_api_call(
                        guild.create_role,
                        name=role_name,
                        color=discord.Color(color_int),
                        reason="人狼ランク自動作成",
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"ロール作成失敗 ({role_name}): {e}")
                    continue
            result[role_name] = role
        return result

    async def _sync_rank_role(
        self,
        member: discord.Member,
        rank_name: str,
        roles_map: Optional[dict[str, discord.Role]] = None,
    ) -> str:
        # ゲーム進行中のメンバーはロールを触らない。
        # 制限卓の表示権限はランクロールで制御されているため、別卓の終了に
        # 伴う昇降格でプレイ中にカテゴリやVCが見えなくなるのを防ぐ。
        # 本人のゲームが終わればphaseがGAME_OVERになってから同期が走るし、
        # 起動時の全体同期・統計ボタンでも追い付くため取りこぼしはない
        active_room = self.find_user_room(member.id)
        if active_room is not None and active_room.state.phase not in (Phase.LOBBY, Phase.GAME_OVER):
            log.info(
                f"ゲーム中のためランクロール同期を保留 ({member.display_name} / {active_room.state.room_name})"
            )
            return "skipped"

        guild = member.guild
        all_role_names = set(rating_lib.all_rank_role_names())
        target_role_name = rating_lib.get_rank_role_name(rank_name)

        if roles_map is None:
            roles_map = await self._ensure_rank_roles(guild)
        target_role = roles_map.get(target_role_name)
        if target_role is None:
            return "failed"

        current_rank_roles = [r for r in member.roles if r.name in all_role_names]
        current_rank_ids = {r.id for r in current_rank_roles}
        if current_rank_ids == {target_role.id}:
            return "updated"

        # 目標ランクの付与と旧ランクの剥奪を1回のPATCHへ統合する。
        # PATCH自体が失敗すれば旧ロール構成が残るため、「追加成功後に削除失敗」
        # という中間状態も作らない。ゲーム用・専用村等の他ロールは全て維持する。
        desired_roles = [
            role for role in member_roles_for_edit(member)
            if role.name not in all_role_names
        ]
        desired_roles.append(target_role)
        try:
            await self.paced_discord_api_call(
                member.edit, roles=desired_roles, reason="ランク更新"
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"ランクロール同期失敗 ({member.display_name}): {e}")
            return "failed"
        return "updated"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is not None and not self._is_managed_guild(message.guild):
            return
        # awaitを挟むため、並行する専用村の作成/削除による辞書変更に備えてコピーを走査
        for room in list(self.rooms.values()):
            await room.on_message(message)

    # ============================================================
    # サーバーミュート解除待ち (ゲーム終了時にVC未接続だったメンバー)
    # ============================================================

    async def register_pending_unmutes(
        self, guild: Optional[discord.Guild], member_ids: set[int]
    ) -> None:
        """終了時に解除できなかったサーバーミュートを記録する (VC入室時に解除)"""
        if guild is None or not member_ids:
            return
        pending = self.pending_unmutes.setdefault(guild.id, set())
        pending.update(member_ids)
        try:
            await database.add_pending_unmutes(guild.id, member_ids)
        except Exception as e:
            log.warning(f"ミュート解除待ちの保存失敗: {e}")
        log.info(f"ミュート解除待ちに登録: {sorted(member_ids)}")

    async def load_pending_unmutes(self, guild: discord.Guild) -> None:
        try:
            normalized = await database.load_pending_unmute_ids(guild.id)
            # 旧CSV形式から一度だけ移行する
            raw = await database.get_meta(guild.id, "pending_unmutes")
        except Exception as e:
            log.warning(f"ミュート解除待ちの読込失敗: {e}")
            return
        legacy = {
            int(x) for x in raw.split(",") if x.strip().isdigit()
        } if raw else set()
        merged = normalized | legacy
        self.pending_unmutes[guild.id] = merged
        if legacy:
            await database.add_pending_unmutes(guild.id, legacy)
            await database.set_meta(guild.id, "pending_unmutes", "")

    async def _resolve_pending_unmute(self, member: discord.Member) -> None:
        pending = self.pending_unmutes.get(member.guild.id)
        if not pending or member.id not in pending:
            return
        # 進行中の別卓のVCに入った場合は解除しない (その卓の発言制御を壊さない)。
        # 待ちリストには残し、次に別の場所へ入ったときに解除する
        vs = member.voice
        channel_id = vs.channel.id if vs is not None and vs.channel is not None else None
        if self.is_other_active_game_vc(channel_id):
            return
        try:
            edit_kwargs: dict = {"mute": False}
            marker_roles = [
                role for role in getattr(member, "roles", [])
                if getattr(role, "name", "").startswith(MUTE_MARKER_ROLE_PREFIX)
            ]
            if marker_roles:
                edit_kwargs["roles"] = [
                    role for role in member_roles_for_edit(member)
                    if not getattr(role, "name", "").startswith(MUTE_MARKER_ROLE_PREFIX)
                ]
            async with self.discord_api_sem:
                await member.edit(
                    **edit_kwargs, reason="人狼: 持ち越しミュート解除"
                )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"持ち越しミュート解除失敗 ({member.display_name}): {e}")
            return
        pending.discard(member.id)
        try:
            await database.remove_pending_unmute(member.guild.id, member.id)
        except Exception as e:
            log.warning(f"ミュート解除待ちの保存失敗: {e}")
        log.info(f"持ち越しサーバーミュートを解除しました ({member.display_name})")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if not self._is_managed_guild(member.guild):
            return
        # ゲーム終了時に解除できなかったサーバーミュートを、VC入室を機に解除する
        if after.channel is not None and not member.bot:
            await self._resolve_pending_unmute(member)

        # 各卓が自分のVCへの入室かを判定する
        for room in list(self.rooms.values()):
            await room.on_voice_state_update(member, before, after)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if not self._is_managed_guild(member.guild):
            return
        for room in list(self.rooms.values()):
            await room.on_member_remove(member)
        try:
            archived_ids = await database.archive_host_recruitments(member.guild.id, member.id)
            for recruitment_id in archived_ids:
                await self.recruitment_manager.refresh_message(recruitment_id)
        except Exception as exc:
            log.exception("退出主催者の募集アーカイブ失敗: %s", exc)
        async with self.private_room_lock:
            row = await database.get_private_room_by_owner(member.guild.id, member.id)
            if row is not None:
                await self._delete_private_room_by_row(
                    member.guild,
                    row,
                    reason=f"{PRIVATE_ROOM_CREATOR_ROLE_NAME}がサーバーから退出したため専用村削除",
                )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if not self._is_managed_guild(after.guild):
            return
        had_creator_role = any(role.name == PRIVATE_ROOM_CREATOR_ROLE_NAME for role in before.roles)
        has_creator_role = any(role.name == PRIVATE_ROOM_CREATOR_ROLE_NAME for role in after.roles)
        if not had_creator_role or has_creator_role:
            return
        async with self.private_room_lock:
            row = await database.get_private_room_by_owner(after.guild.id, after.id)
            if row is None:
                return
            await self._delete_private_room_by_row(
                after.guild,
                row,
                reason=f"{PRIVATE_ROOM_CREATOR_ROLE_NAME}ロールが外れたため専用村削除",
            )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not self._is_managed_guild(member.guild):
            return
        # ゲーム中の参加者の復帰 (復帰待ち解除・ロール/権限の再適用)
        for room in list(self.rooms.values()):
            await room.on_member_join(member)
        async with self.private_room_lock:
            private_rooms = await database.load_private_rooms(member.guild.id)
            for row in private_rooms:
                if row.get("status") in {"deleting", "error"}:
                    continue
                member_ids = await database.get_private_room_member_ids(
                    member.guild.id, row["room_id"]
                )
                if member.id not in member_ids:
                    continue
                get_role = getattr(member.guild, "get_role", None)
                role = (
                    get_role(row.get("role_id"))
                    if row.get("role_id") and callable(get_role) else None
                )
                if role is None:
                    role = discord.utils.get(member.guild.roles, name=row["role_name"])
                if role is None:
                    continue
                try:
                    await self.paced_discord_api_call(
                        member.add_roles,
                        role,
                        reason="専用村招待ロール復元",
                    )
                    await database.add_private_room_member(
                        member.guild.id, row["room_id"], member.id
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"専用村招待ロール復元失敗 ({member.display_name}): {e}")

    def _has_private_room_creator_role(self, member: discord.Member) -> bool:
        return any(role.name == PRIVATE_ROOM_CREATOR_ROLE_NAME for role in member.roles)

    def _private_room_id_for(self, owner_id: int) -> str:
        return f"private_{owner_id}"

    def _normalize_private_room_name(self, raw_name: Optional[str], owner: discord.Member) -> str:
        base = (raw_name or f"{owner.display_name}村").strip()
        if not base:
            base = f"{owner.display_name}村"
        if len(base) > 90:
            base = base[:90].rstrip()
        return base

    def _private_room_name_error(
        self,
        guild: discord.Guild,
        name: str,
        *,
        own_room: Optional[dict] = None,
    ) -> Optional[str]:
        """専用村名の衝突チェック。使えない名前ならエラーメッセージを返す。

        専用村のカテゴリ・ロールは「名前」で検索/削除されるため、
        固定卓名やランクロール名等と同名を許すと既存カテゴリの乗っ取りや
        ランクロールの誤付与・誤削除が起きる。
        """
        reserved = {room.name for room in ROOM_DEFINITIONS}
        reserved.update(rating_lib.all_rank_role_names())
        reserved.add(PRIVATE_ROOM_CREATOR_ROLE_NAME)
        reserved.add(STATS_PARENT_CHANNEL_NAME)
        reserved.add(MAYOR_INFO_CATEGORY_NAME)
        if name in reserved:
            return "その村名はシステムで使用される名前のため使えません。別の村名にしてください。"

        # 自分の専用村の現在名は改名時に許可する (同名リネーム等)
        own_names = set()
        if own_room is not None:
            own_names = {own_room["room_name"], own_room["role_name"]}
        if name not in own_names:
            if discord.utils.get(guild.categories, name=name) is not None:
                return "その名前のカテゴリが既に存在するため使えません。別の村名にしてください。"
            if discord.utils.get(guild.roles, name=name) is not None:
                return "その名前のロールが既に存在するため使えません。別の村名にしてください。"
        return None

    async def _private_reply(self, interaction: discord.Interaction, message: str) -> None:
        """interactionのack済み/未ackを問わずephemeralで応答する。"""
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def create_private_room_for_member(
        self,
        interaction: discord.Interaction,
        room_name: Optional[str] = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self._private_reply(interaction, "この操作はサーバー内でのみ使用できます。")
            return
        if not self._is_managed_guild(interaction.guild):
            await self._private_reply(interaction, "このBotの管理対象外サーバーでは操作できません。")
            return
        if not self._has_private_room_creator_role(interaction.user):
            await self._private_reply(
                interaction,
                f"専用村を作成できるのは **{PRIVATE_ROOM_CREATOR_ROLE_NAME}** ロール保持者だけです。",
            )
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.private_room_lock:
            await self._create_private_room_locked(interaction, room_name)

    async def _create_private_room_locked(
        self,
        interaction: discord.Interaction,
        room_name: Optional[str] = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self._private_reply(interaction, "この操作はサーバー内でのみ使用できます。")
            return
        if not self._is_managed_guild(interaction.guild):
            await self._private_reply(interaction, "このBotの管理対象外サーバーでは操作できません。")
            return
        if not self._has_private_room_creator_role(interaction.user):
            await self._private_reply(
                interaction,
                f"専用村を作成できるのは **{PRIVATE_ROOM_CREATOR_ROLE_NAME}** ロール保持者だけです。",
            )
            return

        guild = interaction.guild
        existing = await database.get_private_room_by_owner(guild.id, interaction.user.id)
        if existing is not None:
            await self._private_reply(
                interaction,
                f"既に専用村 **{existing['room_name']}** があります。複数の専用村は作成できません。",
            )
            return

        normalized_name = self._normalize_private_room_name(room_name, interaction.user)
        name_owner = await database.get_private_room_by_name(guild.id, normalized_name)
        if name_owner is not None:
            await self._private_reply(
                interaction,
                "その村名は既に使われています。別の村名にしてください。",
            )
            return
        name_error = self._private_room_name_error(guild, normalized_name)
        if name_error is not None:
            await self._private_reply(interaction, name_error)
            return

        room_id = self._private_room_id_for(interaction.user.id)
        role_name = normalized_name
        room_def = RoomDefinition(
            room_id=room_id,
            name=normalized_name,
            allowed_gm_user_ids=frozenset({interaction.user.id}),
            private_owner_id=interaction.user.id,
            private_role_name=role_name,
        )

        try:
            await database.save_private_room(
                guild_id=guild.id,
                room_id=room_id,
                owner_id=interaction.user.id,
                room_name=normalized_name,
                role_name=role_name,
            )
            await self._setup_private_room_from_definition(guild, room_def)
        except discord.Forbidden:
            row = await database.get_private_room_by_owner(guild.id, interaction.user.id)
            if row is not None:
                await self._delete_private_room_by_row(guild, row, reason="専用村作成失敗の復旧")
            await interaction.followup.send(
                "専用村の作成に失敗しました。Botのチャンネル管理・ロール管理権限を確認してください。",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            row = await database.get_private_room_by_owner(guild.id, interaction.user.id)
            if row is not None:
                await self._delete_private_room_by_row(guild, row, reason="専用村作成失敗の復旧")
            log.warning(f"専用村作成失敗 ({normalized_name}): {e}")
            await interaction.followup.send(
                "専用村の作成中にDiscord APIエラーが発生しました。",
                ephemeral=True,
            )
            return
        except RuntimeError as e:
            row = await database.get_private_room_by_owner(guild.id, interaction.user.id)
            if row is not None:
                await self._delete_private_room_by_row(guild, row, reason="専用村作成失敗の復旧")
            log.warning(f"専用村作成失敗 ({normalized_name}): {e}")
            await interaction.followup.send(
                "専用村の作成に失敗しました。専用村ロールを作成または取得できませんでした。",
                ephemeral=True,
            )
            return
        except sqlite3.IntegrityError:
            await interaction.followup.send(
                "同じ所有者または村名の専用村が同時に作成されました。既存の村を確認してください。",
                ephemeral=True,
            )
            return
        except Exception as e:
            log.exception("専用村作成の永続化に失敗 (%s): %s", room_id, e)
            row = await database.get_private_room_by_owner(guild.id, interaction.user.id)
            if row is not None:
                await self._delete_private_room_by_row(
                    guild, row, reason="専用村作成失敗の復旧"
                )
            await interaction.followup.send(
                "専用村を安全に保存できなかったため、作成を中止しました。",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"専用村 **{normalized_name}** を作成しました。`#{CH_LOBBY}` の「専用村管理」ボタンから招待と削除ができます。",
            ephemeral=True,
        )

    async def rename_private_room_for_member(
        self,
        interaction: discord.Interaction,
        new_name: str,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self._private_reply(interaction, "この操作はサーバー内でのみ使用できます。")
            return
        if not self._is_managed_guild(interaction.guild):
            await self._private_reply(interaction, "このBotの管理対象外サーバーでは操作できません。")
            return
        if not self._has_private_room_creator_role(interaction.user):
            await self._private_reply(
                interaction,
                f"専用村名を変更できるのは **{PRIVATE_ROOM_CREATOR_ROLE_NAME}** ロール保持者だけです。",
            )
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.private_room_lock:
            await self._rename_private_room_locked(interaction, new_name)

    async def _rename_private_room_locked(
        self,
        interaction: discord.Interaction,
        new_name: str,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self._private_reply(interaction, "この操作はサーバー内でのみ使用できます。")
            return
        if not self._is_managed_guild(interaction.guild):
            await self._private_reply(interaction, "このBotの管理対象外サーバーでは操作できません。")
            return
        if not self._has_private_room_creator_role(interaction.user):
            await self._private_reply(
                interaction,
                f"専用村名を変更できるのは **{PRIVATE_ROOM_CREATOR_ROLE_NAME}** ロール保持者だけです。",
            )
            return

        guild = interaction.guild
        row = await database.get_private_room_by_owner(guild.id, interaction.user.id)
        if row is None:
            await self._private_reply(interaction, "変更できる専用村がありません。")
            return

        normalized_name = self._normalize_private_room_name(new_name, interaction.user)
        existing = await database.get_private_room_by_name(guild.id, normalized_name)
        if existing is not None and existing["room_id"] != row["room_id"]:
            await self._private_reply(
                interaction,
                "その村名は既に使われています。別の村名にしてください。",
            )
            return
        name_error = self._private_room_name_error(guild, normalized_name, own_room=row)
        if name_error is not None:
            await self._private_reply(interaction, name_error)
            return

        room = self.rooms.get(row["room_id"])
        if room is not None and room.state.phase not in (Phase.LOBBY, Phase.GAME_OVER):
            await self._private_reply(
                interaction,
                "ゲーム中は村名を変更できません。先にゲームを終了してください。",
            )
            return

        get_channel = getattr(guild, "get_channel", None)
        category = (
            get_channel(row.get("category_id"))
            if row.get("category_id") and callable(get_channel) else None
        )
        if not isinstance(category, discord.CategoryChannel):
            category = discord.utils.get(guild.categories, name=row["room_name"])
        get_role = getattr(guild, "get_role", None)
        role = (
            get_role(row.get("role_id"))
            if row.get("role_id") and callable(get_role) else None
        )
        if role is None:
            role = discord.utils.get(guild.roles, name=row["role_name"])
        if role is None:
            role = await self._ensure_private_room_role(
                guild,
                self._private_room_definition_from_row(row),
            )
            if role is None:
                await interaction.followup.send(
                    "村名変更に失敗しました。専用村ロールを作成または取得できませんでした。",
                    ephemeral=True,
                )
                return

        try:
            # Discord操作より先にdesired nameをjournalする。ここでクラッシュしても
            # status=renamingと安定IDから起動時に再開できる。
            await database.update_private_room_names(
                guild.id,
                row["room_id"],
                normalized_name,
                normalized_name,
            )
        except sqlite3.IntegrityError:
            await interaction.followup.send(
                "その村名は同時に別の専用村で使われました。別の名前を指定してください。",
                ephemeral=True,
            )
            return
        except Exception as e:
            log.exception("専用村名変更intent保存失敗 (%s): %s", row["room_id"], e)
            await interaction.followup.send(
                "村名変更を安全に保存できなかったため、Discord側は変更しませんでした。",
                ephemeral=True,
            )
            return

        desired_row = {
            **row,
            "room_name": normalized_name,
            "role_name": normalized_name,
            "status": "renaming",
            "category_id": category.id if category is not None else row.get("category_id"),
            "role_id": role.id if role is not None else row.get("role_id"),
        }
        renamed = await self._reconcile_private_room_rename(guild, desired_row)
        if not renamed:
            disabled_room = self.rooms.pop(row["room_id"], None)
            if disabled_room is not None:
                disabled_room.state.ending = True
                disabled_room.state.phase = Phase.GAME_OVER
                disabled_room._stop_all_game_views()
                gm_view = disabled_room.state.gm_panel_view
                if gm_view is not None:
                    gm_view.stop()
            await interaction.followup.send(
                "村名変更を記録しましたがDiscordへの反映が完了していません。"
                "専用村の操作を停止しており、次回起動時に安全に再試行します。",
                ephemeral=True,
            )
            return

        room_def = RoomDefinition(
            room_id=row["room_id"],
            name=normalized_name,
            allowed_gm_user_ids=frozenset({interaction.user.id}),
            private_owner_id=interaction.user.id,
            private_role_name=normalized_name,
        )
        if room is None:
            room = RoomRunner(self.bot, self, room_def)
            self.rooms[row["room_id"]] = room
        else:
            room.room_def = room_def
            room.state.room_name = normalized_name
            if category is not None:
                room.state.category = category
        await self._sync_private_room_member_roles(guild, room_def)
        if room.state.category is not None:
            await self._apply_room_visibility(guild, room.state.category, room_def)
        if room.state.lobby_channel is not None:
            await room._post_lobby_ui()
        await interaction.followup.send(f"専用村名を **{normalized_name}** に変更しました。", ephemeral=True)

    async def delete_private_room_for_member(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self._private_reply(interaction, "この操作はサーバー内でのみ使用できます。")
            return
        if not self._is_managed_guild(interaction.guild):
            await self._private_reply(interaction, "このBotの管理対象外サーバーでは操作できません。")
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        row = await database.get_private_room_by_owner(
            interaction.guild.id, interaction.user.id
        )
        if row is None:
            await interaction.followup.send(
                "削除できる専用村がありません。", ephemeral=True
            )
            return
        if (
            not self._has_private_room_creator_role(interaction.user)
            and not interaction.user.guild_permissions.manage_guild
        ):
            await interaction.followup.send(
                f"専用村を削除できるのは村主本人の **{PRIVATE_ROOM_CREATOR_ROLE_NAME}** "
                "ロール保持者、またはサーバー管理者だけです。",
                ephemeral=True,
            )
            return
        room = self.rooms.get(row["room_id"])
        if room is not None and room.state.phase not in (Phase.LOBBY, Phase.GAME_OVER):
            await interaction.followup.send(
                "ゲーム中の専用村は削除できません。先にゲームを終了してください。",
                ephemeral=True,
            )
            return

        async def execute(confirm_interaction: discord.Interaction) -> None:
            await self._delete_private_room_confirmed(confirm_interaction)

        await interaction.followup.send(
            f"⚠️ 専用村 **{row['room_name']}** のカテゴリ・チャンネル・専用ロールを削除します。実行しますか？",
            view=DangerConfirmView(
                interaction.user.id,
                execute,
                confirm_label="専用村を削除",
            ),
            ephemeral=True,
        )

    async def _delete_private_room_confirmed(
        self, interaction: discord.Interaction
    ) -> None:
        """確認操作の直後に状態を再検査し、専用村を削除する。"""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self._private_reply(interaction, "この操作はサーバー内でのみ使用できます。")
            return
        async with self.private_room_lock:
            await self._delete_private_room_locked(interaction)

    async def _delete_private_room_locked(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self._private_reply(interaction, "この操作はサーバー内でのみ使用できます。")
            return
        if not self._is_managed_guild(interaction.guild):
            await self._private_reply(interaction, "このBotの管理対象外サーバーでは操作できません。")
            return

        guild = interaction.guild
        row = await database.get_private_room_by_owner(guild.id, interaction.user.id)
        if row is None:
            await self._private_reply(interaction, "削除できる専用村がありません。")
            return
        if not self._has_private_room_creator_role(interaction.user) and not interaction.user.guild_permissions.manage_guild:
            await self._private_reply(
                interaction,
                f"専用村を削除できるのは村主本人の **{PRIVATE_ROOM_CREATOR_ROLE_NAME}** ロール保持者、またはサーバー管理者だけです。",
            )
            return

        room = self.rooms.get(row["room_id"])
        if room is not None and room.state.phase not in (Phase.LOBBY, Phase.GAME_OVER):
            await self._private_reply(
                interaction,
                "ゲーム中の専用村は削除できません。先にゲームを終了してください。",
            )
            return
        deleted = await self._delete_private_room_by_row(guild, row, reason="専用村削除")
        if deleted:
            await interaction.followup.send(f"専用村 **{row['room_name']}** を削除しました。", ephemeral=True)
        else:
            await interaction.followup.send(
                "専用村の一部を削除できませんでした。記録を保持しているため、起動時に再試行します。",
                ephemeral=True,
            )

    async def _setup_private_room_from_definition(
        self,
        guild: discord.Guild,
        room_def: RoomDefinition,
    ) -> RoomRunner:
        room = RoomRunner(self.bot, self, room_def)
        self.rooms[room_def.room_id] = room
        await room.setup_channels(guild, stats_channel=self.stats_channel)
        await room.restore_from_snapshot(None)
        private_role = discord.utils.get(guild.roles, name=room_def.private_role_name)
        await database.mark_private_room_active(
            guild.id,
            room_def.room_id,
            category_id=room.state.category.id if room.state.category else None,
            role_id=private_role.id if private_role else None,
        )
        return room

    @app_commands.command(
        name="private_room_create",
        description="自分専用の人狼村を作成します（村長専用）",
    )
    @app_commands.describe(room_name="村名。未入力なら自分の表示名に「村」を付けます")
    async def private_room_create(
        self,
        interaction: discord.Interaction,
        room_name: Optional[str] = None,
    ) -> None:
        await self.create_private_room_for_member(interaction, room_name)

    @app_commands.command(
        name="private_room_delete",
        description="自分の専用村を削除します（村長専用）",
    )
    async def private_room_delete(self, interaction: discord.Interaction) -> None:
        await self.delete_private_room_for_member(interaction)

    @app_commands.command(
        name="season_reset",
        description="全プレイヤーのレートをハーフリセット（管理者専用）",
    )
    @app_commands.describe(note="リセット理由 (任意)")
    @app_commands.default_permissions(manage_guild=True)
    async def season_reset(
        self,
        interaction: discord.Interaction,
        note: Optional[str] = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ このコマンドはサーバー内でのみ使用できます。",
                ephemeral=True,
            )
            return

        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ このコマンドは「サーバー管理」権限が必要です。",
                ephemeral=True,
            )
            return

        if not self._is_managed_guild(interaction.guild):
            await interaction.response.send_message(
                "このBotの管理対象外サーバーでは操作できません。", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=False, thinking=True)
        # SQLite busy waitより先にinteractionをackする。同時要求はCASと
        # season_games=0検査の双方で二重実行を拒否する。
        try:
            expected_start = await database.get_season_start(interaction.guild.id)
        except Exception as e:
            log.exception("シーズン開始情報の取得に失敗: %s", e)
            await interaction.followup.send(
                "❌ データベースからシーズン情報を取得できないため、中止しました。"
            )
            return
        async with self.season_reset_lock:
            # start_gameと同じlockを保持してからactiveを再確認する。
            # これにより確認後〜reset完了まで新規ゲームが割り込まない
            async with self.start_lock:
                await self._execute_season_reset(
                    interaction, note=note, expected_start=expected_start
                )

    async def _execute_season_reset(
        self,
        interaction: discord.Interaction,
        *,
        note: Optional[str],
        expected_start: Optional[str],
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        if self.has_active_rated_games():
            await interaction.followup.send(
                "ランク対象卓が進行中のため、シーズンリセットは実行できません。全卓終了後に実行してください。"
            )
            return

        backup_path = None
        abort_message = None
        try:
            async with self.rating_lock:
                pending = await database.load_pending_game_settlements(guild.id)
                if any(item["rated"] for item in pending):
                    abort_message = (
                        "⚠️ レート未精算の終了ゲームがあるため、シーズンリセットを中止しました。"
                        "精算の自動再試行完了後にもう一度実行してください。"
                    )
                elif await database.has_open_game_recommendations(guild.id):
                    abort_message = (
                        "⚠️ 終了後推薦の受付・集計中のため、シーズンリセットを中止しました。"
                        "推薦結果の反映後にもう一度実行してください。"
                    )
                else:
                    # rating_lock中にbackupとresetを連続実行し、backup取得後に
                    # 別卓の精算が入って復旧点からゲームだけ欠落する窓を塞ぐ。
                    try:
                        backup_path = await database.backup_db(label="season_reset")
                    except Exception as e:
                        log.exception(f"リセット前DBバックアップ失敗: {e}")
                        abort_message = (
                            "❌ リセット前のデータベースバックアップに失敗したため、"
                            "シーズンリセットを中止しました。ログを確認してください。"
                        )
                    else:
                        reset_id, affected = await database.season_half_reset(
                            guild_id=guild.id,
                            executed_by=interaction.user.id,
                            note=note,
                            expected_season_start=expected_start,
                        )
        except database.SeasonResetConflict as e:
            await interaction.followup.send(f"⚠️ シーズンリセットを中止しました: {e}")
            return
        except Exception as e:
            log.exception("シーズンリセットDB処理失敗: %s", e)
            await interaction.followup.send(
                "❌ データベース処理に失敗したため、シーズンリセットを中止しました。"
            )
            return

        if abort_message is not None:
            await interaction.followup.send(abort_message)
            return

        if affected == 0:
            await interaction.followup.send("⚠️ リセット対象のプレイヤーが存在しません。")
            return

        self.spawn_bg_task(self._resync_roles_after_reset(guild))
        embed = discord.Embed(
            title="🔄 シーズンハーフリセット完了",
            description=(
                f"全プレイヤーのレートを\n"
                f"`新レート = {INITIAL_RATING} + (現レート - {INITIAL_RATING}) ÷ 2`\n"
                f"で再計算し、今シーズン試合数/勝利数をリセットしました。"
            ),
            color=discord.Color.purple(),
        )
        embed.add_field(name="対象プレイヤー", value=f"{affected}人", inline=True)
        embed.add_field(name="ロール再同期", value="バックグラウンドで進行中 (完了時に通知)", inline=True)
        embed.add_field(name="実行者", value=interaction.user.mention, inline=True)
        if backup_path:
            embed.add_field(name="バックアップ", value=f"`{Path(backup_path).name}`", inline=False)
        if note:
            embed.add_field(name="メモ", value=note[:1024], inline=False)
        embed.set_footer(text=f"リセットID: {reset_id}")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GameCog(bot))
