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
    CH_STATS, CH_GM_INFO,
    STATS_PARENT_CHANNEL_NAME,
    GM_INFO_CATEGORY_NAME, GM_INFO_ADMIN_ONLY,
    LOG_CATEGORY_VILLAGE, LOG_CATEGORY_SPIRIT,
    GM_ROLE_NAME, TEMP_GM_ROLE_NAME,
    PRIVATE_ROOM_CREATOR_ROLE_NAMES, PRIVATE_ROOM_CREATOR_ROLE_LABEL,
    RECRUITMENT_NOTIFICATION_ROLE_NAME,
    BULK_DISCORD_API_INTERVAL,
    DEFAULT_LADDER_ID, LADDER_DEFINITIONS,
    ACTIVE_ROOM_DEFINITIONS,
    ROOM_DEFINITIONS, RoomDefinition,
    USER_VISIBLE_VARIANT_IDS,
)
from permissions import RoomPermissionMixin, RoomVisibilityError
from room_runner import (  # noqa: F401  (互換のため再エクスポート)
    MUTE_MARKER_ROLE_PREFIX,
    ROLE_EMOJI,
    RoomRunner,
    member_roles_for_edit,
)
from views import DangerConfirmView, PrivateRoomInfoView, StatsView
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
        # API待ち時間の内訳をルート別に集計する。間隔を詰めてよいのか、
        # ロック分割が要るのか、Discord側のバケットで待たされているのかは
        # 実測しないと区別できない (どれも「遅い」としか見えない)。
        self._api_call_stats: dict[str, dict[str, float]] = {}
        # シーズンリマインダーの最終投稿時刻 (連投防止)
        self._season_reminder_last: Optional[datetime] = None
        self._settlement_recovered_since_notice = 0
        self._rating_recovery_needs_role_sync = False
        self._pending_settlement_fail_alerted: set[tuple[str, str]] = set()
        # HTTPで作成した直後のDiscordロールはGatewayイベントが届くまで
        # guild.rolesへ反映されないことがあるため、同じ起動中はここを正本にする。
        self._gm_staff_roles: dict[str, discord.Role] = {}
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
            for room in ACTIVE_ROOM_DEFINITIONS
        }
        self.recruitment_manager = RecruitmentManager(bot, self)

    @staticmethod
    def _api_route_label(func) -> str:
        """待ち時間の集計キー。Discordのバケットは対象種別＋操作でほぼ決まる。"""
        try:
            name = getattr(func, "__name__", None) or "unknown"
            owner = getattr(func, "__self__", None)
            if owner is None:
                return name
            return f"{type(owner).__name__}.{name}"
        except Exception:  # 計測が本処理を壊すことは絶対に避ける
            return "unknown"

    def _record_api_call(
        self,
        route: str,
        *,
        queue_wait: float,
        interval_wait: float,
        exec_seconds: float,
    ) -> None:
        stat = self._api_call_stats.get(route)
        if stat is None:
            stat = {
                "count": 0.0,
                "queue_wait": 0.0,
                "interval_wait": 0.0,
                "exec": 0.0,
                "max_exec": 0.0,
            }
            self._api_call_stats[route] = stat
        stat["count"] += 1
        stat["queue_wait"] += queue_wait
        stat["interval_wait"] += interval_wait
        stat["exec"] += exec_seconds
        stat["max_exec"] = max(stat["max_exec"], exec_seconds)

    async def paced_discord_api_call(self, func, *args, **kwargs):
        """高負荷な連続APIを全卓で直列化し、最小間隔を空けて実行する。"""
        loop = asyncio.get_running_loop()
        route = self._api_route_label(func)
        queued_at = loop.time()
        async with self.bulk_api_lock:
            queue_wait = loop.time() - queued_at
            wait = self._bulk_api_next_at - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            interval_wait = wait if wait > 0 else 0.0
            started_at = loop.time()
            try:
                async with self.discord_api_sem:
                    return await func(*args, **kwargs)
            finally:
                finished_at = loop.time()
                self._bulk_api_next_at = finished_at + self.bulk_api_interval
                self._record_api_call(
                    route,
                    queue_wait=queue_wait,
                    interval_wait=interval_wait,
                    exec_seconds=finished_at - started_at,
                )

    def log_api_pacing_summary(self, label: str, *, reset: bool = True) -> None:
        """API待ち時間の内訳をルート別に出す。

        読み方:
        - 間隔 が支配的 → `BULK_DISCORD_API_INTERVAL` を詰めれば速くなる
        - 順番待ち が支配的 → 単一ロックが原因。バケット別ロックへ分ける
        - 実行 が支配的 → discord.pyがDiscord側のバケットで待たされている。
          こちらの間隔をいくら詰めても速くならない
        """
        stats = self._api_call_stats
        if not stats:
            return
        rows = sorted(
            stats.items(),
            key=lambda item: item[1]["queue_wait"]
            + item[1]["interval_wait"]
            + item[1]["exec"],
            reverse=True,
        )
        total_calls = int(sum(stat["count"] for _route, stat in rows))
        total_queue = sum(stat["queue_wait"] for _route, stat in rows)
        total_interval = sum(stat["interval_wait"] for _route, stat in rows)
        total_exec = sum(stat["exec"] for _route, stat in rows)
        log.info(
            "API待ち内訳 (%s): %d回 / 合計%.1f秒 = 順番待ち%.1f + 間隔%.1f + 実行%.1f",
            label,
            total_calls,
            total_queue + total_interval + total_exec,
            total_queue,
            total_interval,
            total_exec,
        )
        for route, stat in rows[:10]:
            count = int(stat["count"]) or 1
            # 1回あたりの実行時間が判断の決め手。0.1秒前後ならDiscordは待たせて
            # おらず間隔を詰める余地がある。1秒を超えるならdiscord.pyがバケットで
            # 先回りして寝ているので、こちらの間隔を詰めても速くならない。
            log.info(
                "  %-26s %4d回 順番待ち%6.1f 間隔%6.1f 実行%6.1f"
                " (1回%.2f秒 / 最大%.2f秒)",
                route,
                int(stat["count"]),
                stat["queue_wait"],
                stat["interval_wait"],
                stat["exec"],
                stat["exec"] / count,
                stat["max_exec"],
            )
        if len(rows) > 10:
            log.info("  ... 他%dルート", len(rows) - 10)
        if reset:
            stats.clear()

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
        if self.api_pacing_report_loop.is_running():
            self.api_pacing_report_loop.cancel()
        # 停止前に、まだ出していないぶんをログへ残す。
        self.log_api_pacing_summary("停止時")
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

    @staticmethod
    def _disabled_fixed_room_snapshot_conflicts(
        snapshots: dict[str, dict],
        quarantined_room_ids: set[str],
    ) -> dict[str, set[str]]:
        """無効固定卓に復旧が必要な状態が残っていないか判定する。

        無効卓はRunnerを作らないため、空のLOBBY/GAME_OVER以外を見落とすと
        復元・ミュート回収・募集移行を失う。隔離snapshotも手掛かりを失わない
        よう常に停止理由にする。
        """
        disabled_room_ids = {
            room.room_id
            for room in ROOM_DEFINITIONS
            if not room.enabled and room.private_owner_id is None
        }
        conflicts: dict[str, set[str]] = {}
        for room_id in disabled_room_ids & set(quarantined_room_ids):
            conflicts.setdefault(room_id, set()).add("隔離snapshot")
        for room_id in disabled_room_ids:
            payload = snapshots.get(room_id)
            if payload is None:
                continue
            phase = payload.get("phase")
            reasons: set[str] = set()
            if phase not in (Phase.LOBBY.name, Phase.GAME_OVER.name):
                reasons.add(f"進行中snapshot ({phase})")
            if payload.get("players"):
                reasons.add("参加者を含むsnapshot")
            if payload.get("gm_id") is not None:
                reasons.add("GMを含むsnapshot")
            if payload.get("recruitment_id") is not None:
                reasons.add("募集紐付きsnapshot")
            # 一度有効化した卓は、ロビー状態でもBot所有のカテゴリ・受付・VCの
            # IDをsnapshotへ残す。Runnerを外すだけではそのDiscord資源を
            # 非公開化できず、空の見える卓が残るため、完全未公開へ切り替える
            # 場合は手動で安全に回収するまで起動を止める。
            channel_ids = payload.get("channel_ids")
            if isinstance(channel_ids, dict) and any(
                channel_ids.get(name) is not None
                for name in ("category", "lobby", "voice", "village", "spirit")
            ):
                reasons.add("Bot所有Discordチャンネルを含むsnapshot")
            if reasons:
                conflicts.setdefault(room_id, set()).update(reasons)
        return conflicts

    async def _assert_disabled_fixed_rooms_safe_to_skip(
        self,
        guild_id: int,
        snapshots: dict[str, dict],
        quarantined_room_ids: set[str],
    ) -> None:
        """Discordへの副作用より先に、無効卓を安全にスキップできるか確認する。"""
        conflicts = self._disabled_fixed_room_snapshot_conflicts(
            snapshots,
            quarantined_room_ids,
        )
        disabled_room_ids = {
            room.room_id
            for room in ROOM_DEFINITIONS
            if not room.enabled and room.private_owner_id is None
        }
        active_recruitments = await database.list_active_recruitments_for_room_ids(
            guild_id,
            disabled_room_ids,
        )
        for recruitment in active_recruitments:
            room_id = str(recruitment["room_id"])
            conflicts.setdefault(room_id, set()).add(
                f"未終了募集 #{recruitment['id']} ({recruitment['status']})"
            )
        if not conflicts:
            return
        detail = " | ".join(
            f"{room_id}: {', '.join(sorted(reasons))}"
            for room_id, reasons in sorted(conflicts.items())
        )
        raise RuntimeError(
            "無効固定卓に復旧が必要な状態が残っているため起動を停止しました。"
            "卓を一時的に有効化して復旧・募集終了を行うか、Bot所有のカテゴリ/受付/VCを"
            "手動で回収し、空のLOBBY/GAME_OVER snapshotだけにしてから再起動してください: "
            f"{detail}"
        )

    async def _retire_removed_fixed_rooms(
        self,
        guild: discord.Guild,
        snapshots: dict[str, dict],
        quarantined_room_ids: set[str],
    ) -> None:
        """明示廃止された旧5卓を、保存IDだけで一回限り回収する。

        名前一致だけでは削除しない。空ロビー、非隔離snapshot、保存カテゴリID、
        Botが記録した子チャンネルだけ、という全条件が揃った卓に限定する。
        手動チャンネルが1つでも混在すれば通常のfail-closed検査へ残す。
        """
        retired_ids = {
            "beginner", "intermediate", "advanced", "open_9_cross", "open_9_turn",
        }
        definitions = {room.room_id: room for room in ROOM_DEFINITIONS}
        active_recruitments = await database.list_active_recruitments_for_room_ids(
            guild.id, retired_ids
        )
        recruitments_by_room: dict[str, list[dict]] = {}
        for row in active_recruitments:
            recruitments_by_room.setdefault(str(row["room_id"]), []).append(row)

        for room_id in sorted(retired_ids):
            payload = snapshots.get(room_id)
            if payload is None or room_id in quarantined_room_ids:
                continue
            phase = str(payload.get("phase") or "")
            if phase not in {Phase.LOBBY.name, Phase.GAME_OVER.name}:
                continue
            if payload.get("players") or payload.get("gm_id") is not None:
                continue
            linked = payload.get("recruitment_id")
            if linked is not None and not any(
                int(item["id"]) == int(linked)
                for item in recruitments_by_room.get(room_id, [])
            ):
                continue

            channel_ids = payload.get("channel_ids") or {}
            category_id = channel_ids.get("category")
            # statsは全卓共通の#統計で、旧卓カテゴリの子ではない。ここへ含めると
            # 「保存済み子がカテゴリ外」の安全判定に必ず当たり、空の旧卓でも
            # 回収できず起動停止になる。
            owned_child_keys = {"lobby", "voice", "village", "spirit"}
            recorded_child_ids = {
                int(channel_id)
                for name, channel_id in channel_ids.items()
                if name in owned_child_keys and isinstance(channel_id, int)
            }
            category = guild.get_channel(category_id) if isinstance(category_id, int) else None
            existing_children = [
                guild.get_channel(channel_id) for channel_id in recorded_child_ids
            ]
            existing_children = [item for item in existing_children if item is not None]
            if category is None:
                if existing_children:
                    continue
            else:
                definition = definitions.get(room_id)
                if not isinstance(category, discord.CategoryChannel):
                    continue
                if definition is None or category.name != definition.name:
                    continue
                actual_child_ids = {
                    int(child.id) for child in getattr(category, "channels", ())
                }
                if not actual_child_ids.issubset(recorded_child_ids):
                    log.warning(
                        "旧固定卓に手動チャンネルがあるため自動削除しません: %s",
                        definition.name,
                    )
                    continue
                if any(
                    getattr(getattr(child, "category", None), "id", None) != category.id
                    for child in existing_children
                ):
                    continue

            try:
                for child in existing_children:
                    await self.paced_discord_api_call(
                        child.delete,
                        reason="常設卓廃止に伴うBot所有チャンネル回収",
                    )
                if category is not None:
                    await self.paced_discord_api_call(
                        category.delete,
                        reason="常設卓廃止に伴うBot所有カテゴリ回収",
                    )
            except (discord.Forbidden, discord.HTTPException) as exc:
                raise RuntimeError(
                    f"旧固定卓 {room_id} のDiscord資産を安全に回収できません: {exc}"
                ) from exc

            for recruitment in recruitments_by_room.get(room_id, []):
                await database.set_recruitment_status(
                    int(recruitment["id"]), database.RECRUITMENT_ARCHIVED
                )
                await database.clear_recruitment_message_id(int(recruitment["id"]))
            await database.delete_room_state(guild.id, room_id)
            snapshots.pop(room_id, None)
            log.info("旧常設卓を安全に削除しました: %s", room_id)

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

    @tasks.loop(minutes=30)
    async def api_pacing_report_loop(self) -> None:
        try:
            self.log_api_pacing_summary("直近30分")
        except Exception as e:
            # tasks.loopは例外が外へ出ると停止する。計測で本体を止めない。
            log.warning("API待ち内訳の出力に失敗: %s", e)

    @api_pacing_report_loop.before_loop
    async def _api_pacing_report_wait_ready(self) -> None:
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

    @staticmethod
    def _roles_named(guild: discord.Guild, name: str) -> list[discord.Role]:
        return [role for role in guild.roles if role.name == name]

    @classmethod
    def _primary_role_named(
        cls, guild: discord.Guild, name: str
    ) -> Optional[discord.Role]:
        """同名ロールが複数あっても最上位を正本として返す。

        認可は `_has_private_room_creator_role` がロール「名」で判定するため、
        重複していても権限は正しく働く。Roleオブジェクトが要るのは並び順と
        閲覧許可だけなので、ここで起動を止める理由はない。
        """
        roles = cls._roles_named(guild, name)
        if len(roles) > 1:
            log.warning(
                "%s ロールが%d個あります。最上位を正本として使うので起動は続けますが、"
                "運用が紛らわしくなるため重複は整理してください。",
                name,
                len(roles),
            )
            roles.sort(key=lambda role: getattr(role, "position", 0), reverse=True)
        return roles[0] if roles else None

    async def _ensure_gm_staff_roles(self, guild: discord.Guild) -> None:
        """GM／仮GMロールを用意し、GMを仮GMより上に保つ。

        ランクロール確認 (`_ensure_rank_roles`) と同じく、失敗しても起動は
        止めない。ここで例外を投げると bot.py の on_ready が全体を
        `bot.close()` するため、GMロールの不調だけで13人村・9人村・レートまで
        巻き添えで停止してしまう。
        """
        roles: dict[str, discord.Role] = {}
        for role_name in (GM_ROLE_NAME, TEMP_GM_ROLE_NAME):
            role = self._gm_staff_roles.get(role_name)
            if role is None:
                role = self._primary_role_named(guild, role_name)
            if role is None:
                try:
                    role = await self.paced_discord_api_call(
                        guild.create_role,
                        name=role_name,
                        reason="GM運営ロール作成",
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.error(
                        "%s ロールを作成できません。Botのロール管理権限と階層を確認してください: %s",
                        role_name,
                        exc,
                    )
                    continue
            roles[role_name] = role
            self._gm_staff_roles[role_name] = role

        gm_role = roles.get(GM_ROLE_NAME)
        temp_gm_role = roles.get(TEMP_GM_ROLE_NAME)
        if gm_role is None or temp_gm_role is None:
            return
        gm_position = getattr(gm_role, "position", None)
        temp_gm_position = getattr(temp_gm_role, "position", None)
        if (
            isinstance(gm_position, int)
            and isinstance(temp_gm_position, int)
            and gm_position <= temp_gm_position
            and callable(getattr(gm_role, "edit", None))
        ):
            try:
                moved = await self.paced_discord_api_call(
                    gm_role.edit,
                    position=temp_gm_position + 1,
                    reason="GMを仮GMより上に配置",
                )
            except (discord.Forbidden, discord.HTTPException, ValueError) as exc:
                # 並び順は表示上の慣習で、認可はロール名で行うため実害はない。
                log.warning(
                    "GMを仮GMより上に配置できません。Botのロール階層を確認してください: %s",
                    exc,
                )
                return
            if moved is not None:
                self._gm_staff_roles[GM_ROLE_NAME] = moved

    async def _ensure_gm_info_channel(self, guild: discord.Guild) -> discord.TextChannel:
        # 認可はロール名で行うので、同名ロールが複数あればその全員へ閲覧を許す。
        # 正本1つだけに許可を出すと、もう一方の保持者だけ見えない状態になる。
        staff_roles: list[discord.Role] = []
        seen_role_ids: set[int] = set()
        for name in (GM_ROLE_NAME, TEMP_GM_ROLE_NAME):
            candidates = self._roles_named(guild, name)
            cached = self._gm_staff_roles.get(name)
            if cached is not None and cached not in candidates:
                candidates.append(cached)
            for role in candidates:
                role_id = getattr(role, "id", None)
                if role_id is not None and role_id in seen_role_ids:
                    continue
                if role_id is not None:
                    seen_role_ids.add(role_id)
                staff_roles.append(role)
        category_overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False, read_messages=False, connect=False
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, read_messages=True, send_messages=True,
                connect=True, manage_channels=True,
            ),
        }
        if not GM_INFO_ADMIN_ONLY:
            for staff_role in staff_roles:
                category_overwrites[staff_role] = discord.PermissionOverwrite(
                    view_channel=True, read_messages=True, send_messages=False, connect=True
                )
        legacy_category_name = "GMロール説明"
        legacy_channel_name = "専用村作成"
        category = None
        stored_category_id = await database.get_meta(guild.id, "gm_hub_category_id")
        if stored_category_id and str(stored_category_id).isdigit():
            candidate = guild.get_channel(int(stored_category_id))
            if isinstance(candidate, discord.CategoryChannel):
                category = candidate
        if category is None:
            category = discord.utils.get(guild.categories, name=GM_INFO_CATEGORY_NAME)
        if category is None:
            legacy_category = discord.utils.get(
                guild.categories, name=legacy_category_name
            )
            legacy_channel = (
                discord.utils.get(
                    guild.text_channels,
                    name=legacy_channel_name,
                    category=legacy_category,
                )
                if legacy_category is not None else None
            )
            if legacy_category is not None and legacy_channel is not None:
                category = await legacy_category.edit(
                    name=GM_INFO_CATEGORY_NAME,
                    reason="募集とGM村作成を統合",
                )
        if category is None:
            category = await guild.create_category(
                GM_INFO_CATEGORY_NAME, overwrites=category_overwrites
            )
        await database.set_meta(guild.id, "gm_hub_category_id", str(category.id))
        channel = None
        stored_channel_id = await database.get_meta(guild.id, "gm_hub_channel_id")
        if stored_channel_id and str(stored_channel_id).isdigit():
            candidate = guild.get_channel(int(stored_channel_id))
            if isinstance(candidate, discord.TextChannel) and candidate.category == category:
                channel = candidate
        if channel is None:
            channel = discord.utils.get(
                guild.text_channels, name=CH_GM_INFO, category=category
            )
        if channel is None:
            legacy_channel = discord.utils.get(
                guild.text_channels, name=legacy_channel_name, category=category
            )
            if legacy_channel is not None:
                channel = await legacy_channel.edit(
                    name=CH_GM_INFO,
                    reason="募集とGM村作成を統合",
                )
        channel_overwrites = dict(category_overwrites)
        channel_overwrites[guild.default_role] = discord.PermissionOverwrite(
            view_channel=False, read_messages=False, send_messages=False
        )
        if channel is None:
            channel = await guild.create_text_channel(
                CH_GM_INFO, category=category, overwrites=channel_overwrites
            )
        await database.set_meta(guild.id, "gm_hub_channel_id", str(channel.id))

        try:
            for target, overwrite in category_overwrites.items():
                await self._set_permission_if_changed(
                    category,
                    target,
                    overwrite,
                    reason="GM説明カテゴリ権限更新",
                )
            for target, overwrite in channel_overwrites.items():
                await self._set_permission_if_changed(
                    channel,
                    target,
                    overwrite,
                    reason="GM説明チャンネル権限更新",
                )
        except (discord.Forbidden, discord.HTTPException) as e:
            raise RoomVisibilityError(f"GM説明チャンネル権限更新失敗: {e}") from e

        if not staff_roles and not GM_INFO_ADMIN_ONLY:
            log.warning(
                f"{PRIVATE_ROOM_CREATOR_ROLE_LABEL}ロールが見つからないため、"
                "GM説明カテゴリの閲覧許可を付与できません"
            )
        if GM_INFO_ADMIN_ONLY:
            # 過去に付けたロール・個人へのallowを残すと非管理者から見えるため、
            # 現在の管理対象以外の閲覧許可はカテゴリとチャンネルの両方から外す。
            await self._remove_stale_visibility_allows(
                guild,
                category,
                set(category_overwrites),
                label=f"カテゴリ {GM_INFO_CATEGORY_NAME}",
            )
            await self._remove_stale_visibility_allows(
                guild,
                channel,
                set(channel_overwrites),
                label=f"チャンネル {GM_INFO_CATEGORY_NAME}/{CH_GM_INFO}",
            )

        await self._purge_bot_messages(channel, "村作成")
        embed = discord.Embed(
            title="GM村と募集の作成",
            description=(
                f"**{PRIVATE_ROOM_CREATOR_ROLE_LABEL}** ロールを持っている人は、"
                "自分の名前村と募集を一度に作成できます。\n"
                "募集カードは作成した村の #参加受付 に置かれ、参加・取消・同村拒否の確認をそこで完結します。"
            ),
            color=discord.Color.dark_gold(),
        )
        embed.add_field(
            name="できること",
            value=(
                "GM村と募集を同時作成\n"
                "村名を変更\n"
                "GM村を削除\n"
                "ゲーム開始前に公開中の3形式へ変更\n"
                "参加者への連絡・募集終了"
            ),
            inline=False,
        )
        embed.add_field(
            name="制限",
            value=(
                "GM村は1人1つまでです。\n"
                "村主本人だけがその村のゲームマスターになれます。\n"
                f"**{PRIVATE_ROOM_CREATOR_ROLE_LABEL}** の両方を外れると、GM村は自動削除されます。"
            ),
            inline=False,
        )
        embed.add_field(
            name="ランク対象",
            value=(
                "正常終了したすべての村で、レート、ランク、ランクロール、"
                "今季戦績、統計、ランキングが更新されます。\n"
                "13人村と9人村は、それぞれ対応するラダーへ反映します。"
            ),
            inline=False,
        )
        embed.add_field(
            name="注意",
            value=(
                "削除するとGM村カテゴリ、募集、参加記録が消えます。\n"
                "ゲーム中のGM村は、先にゲームを終了してから削除してください。"
            ),
            inline=False,
        )
        for attempt in range(3):
            try:
                await channel.send(embed=embed, view=PrivateRoomInfoView(self))
                break
            except discord.HTTPException as e:
                if attempt >= 2:
                    log.warning(f"GM説明メッセージ投稿失敗: {e}")
                    return channel
                await asyncio.sleep(2 * (attempt + 1))
        return channel

    def _private_room_definition_from_row(self, row: dict) -> RoomDefinition:
        return RoomDefinition(
            room_id=row["room_id"],
            name=row["room_name"],
            allowed_gm_user_ids=frozenset({row["owner_id"]}),
            private_owner_id=row["owner_id"],
            # GM名前村は公開観戦型。動的村の判定はowner/room_idが正本で、
            # role_nameは旧閲覧ロールの掃除中だけDBに残る互換値。
            private_role_name=None,
            variant_id=str(row.get("variant_id") or "v13_cross"),
        )

    @staticmethod
    def _is_protected_from_legacy_access_role_cleanup(
        guild: discord.Guild,
        role: discord.Role,
    ) -> bool:
        """旧閲覧ロールの保存IDが壊れても運営ロールを削除しない。"""
        role_name = str(getattr(role, "name", ""))
        permissions = getattr(role, "permissions", None)
        return (
            role_name in {GM_ROLE_NAME, TEMP_GM_ROLE_NAME}
            or role_name.startswith(MUTE_MARKER_ROLE_PREFIX)
            or role_name.startswith("人狼進行中:")
            or bool(getattr(role, "managed", False))
            # 旧閲覧ロールはサーバー権限を持たない。運用中に別用途へ
            # 転用されたロールは、保存IDと名前が一致しても削除しない。
            or int(getattr(permissions, "value", 0)) != 0
            or role == getattr(guild, "default_role", None)
        )

    @staticmethod
    def _legacy_access_role_name_matches(row: dict, role: discord.Role) -> bool:
        """stable IDに加え、保存時の名前も一致する旧ロールだけを認める。"""
        stored_role_name = row.get("role_name")
        return (
            isinstance(stored_role_name, str)
            and bool(stored_role_name)
            and getattr(role, "name", None) == stored_role_name
        )

    def _resolve_private_room_assets(
        self,
        guild: discord.Guild,
        row: dict,
        *,
        legacy_category_id: Optional[int] = None,
        require_existing: bool = False,
    ) -> tuple[Optional[discord.CategoryChannel], Optional[discord.Role], list[str]]:
        """削除・改名対象をstable IDだけで解決する。

        名前一致は所有証明にならない。保存IDが無い旧資産には触れず、
        snapshot/runnerが持つcategory IDだけを互換用に使う。
        """
        errors: list[str] = []
        stored_category_id = row.get("category_id")
        stored_role_id = row.get("role_id")

        trusted_category_id = (
            stored_category_id
            if stored_category_id is not None
            else legacy_category_id
        )
        category = next(
            (
                item for item in getattr(guild, "categories", [])
                if getattr(item, "id", None) == trusted_category_id
            ),
            None,
        )
        role = next(
            (
                item for item in getattr(guild, "roles", [])
                if getattr(item, "id", None) == stored_role_id
            ),
            None,
        )

        # 保存IDの不在は削除時には「既に削除済み」。改名はカテゴリが必要。
        if require_existing and category is None and trusted_category_id is not None:
            errors.append(f"保存カテゴリID {trusted_category_id} が見つかりません")

        if require_existing and category is None and not any(
            "カテゴリ" in error for error in errors
        ):
            errors.append("カテゴリが見つかりません")
        return category, role, errors

    async def _cleanup_legacy_private_room_access_roles(
        self,
        guild: discord.Guild,
        room_id: str,
    ) -> None:
        """公開overwrite適用後に、指定GM村の旧閲覧ロールを回収する。"""
        rows = [
            row for row in await database.load_private_rooms(guild.id)
            if row["room_id"] == room_id
        ]
        for row in rows:
            stored_role_id = row.get("role_id")
            if stored_role_id is None:
                if row.get("role_name") is None:
                    continue
                # stable IDの無い旧ロールは名前だけでは特定・削除しない。
                # 今後使わないDB上の名前と再付与journalだけを捨てる。
                await database.clear_private_room_access_role(
                    guild.id,
                    row["room_id"],
                    expected_role_id=None,
                )
                continue
            if not isinstance(stored_role_id, int) or isinstance(stored_role_id, bool):
                log.warning(
                    "GM名前村の旧閲覧ロールIDが不正なため触れません: "
                    "room=%s role_id=%r",
                    row["room_id"],
                    stored_role_id,
                )
                continue
            get_role = getattr(guild, "get_role", None)
            role = get_role(stored_role_id) if callable(get_role) else None
            if role is not None and self._is_protected_from_legacy_access_role_cleanup(
                guild, role,
            ):
                log.error(
                    "GM名前村の旧閲覧ロールIDが保護対象を指すため"
                    "Discord側には触れず参照だけ解除します: room=%s role=%s(%s)",
                    row["room_id"],
                    getattr(role, "name", "?"),
                    stored_role_id,
                )
                cleared = await database.clear_private_room_access_role(
                    guild.id,
                    row["room_id"],
                    expected_role_id=stored_role_id,
                )
                if not cleared:
                    log.warning(
                        "GM名前村の旧閲覧ロール参照が同時更新されました: %s",
                        row["room_id"],
                    )
                continue
            if role is not None and not self._legacy_access_role_name_matches(row, role):
                log.warning(
                    "GM名前村の旧閲覧ロールIDは存在しますが保存名と"
                    "一致しないため、Discord側には触れず参照だけ解除します: "
                    "room=%s stored_name=%r actual_name=%r role=%s",
                    row["room_id"],
                    row.get("role_name"),
                    getattr(role, "name", None),
                    stored_role_id,
                )
                await database.clear_private_room_access_role(
                    guild.id,
                    row["room_id"],
                    expected_role_id=stored_role_id,
                )
                continue
            if role is not None:
                try:
                    await self.paced_discord_api_call(
                        role.delete,
                        reason="GM名前村を公開観戦型へ移行",
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    # DB参照を残すことで、次回起動時に同じstable IDを再試行する。
                    log.warning(
                        "GM名前村の旧閲覧ロール削除失敗"
                        " (room=%s role=%s): %s",
                        row["room_id"],
                        stored_role_id,
                        exc,
                    )
                    continue
            cleared = await database.clear_private_room_access_role(
                guild.id,
                row["room_id"],
                expected_role_id=stored_role_id,
            )
            if not cleared:
                log.warning(
                    "GM名前村の旧閲覧ロール参照が同時更新されました: %s",
                    row["room_id"],
                )

    async def _load_private_room_runners(
        self,
        guild: discord.Guild,
        snapshots: Optional[dict[str, dict]] = None,
    ) -> None:
        snapshots = snapshots or {}
        rows = await database.load_private_rooms(guild.id)
        for row in [item for item in rows if item.get("status") == "deleting"]:
            await self._delete_private_room_by_row(
                guild,
                row,
                reason="中断された専用村削除を再試行",
                legacy_category_id=(
                    snapshots.get(row["room_id"], {}).get("channel_ids", {}).get("category")
                ),
            )
        rows = await database.load_private_rooms(guild.id)
        for row in [item for item in rows if item.get("status") == "renaming"]:
            await self._reconcile_private_room_rename(
                guild,
                row,
                legacy_category_id=(
                    snapshots.get(row["room_id"], {}).get("channel_ids", {}).get("category")
                ),
            )
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
            variant_id = str(row.get("variant_id") or "")
            if variant_id not in USER_VISIBLE_VARIANT_IDS:
                error = (
                    f"非公開のゲーム形式 {variant_id or '不明'} が保存されているため"
                    "GM村を隔離しました。"
                )
                await database.mark_private_room_status(
                    guild.id,
                    row["room_id"],
                    "error",
                    error=error,
                )
                log.error("%s room=%s", error, row["room_id"])
                continue
            room_def = self._private_room_definition_from_row(row)
            self.rooms[room_def.room_id] = RoomRunner(self.bot, self, room_def)

    async def _reconcile_private_room_rename(
        self,
        guild: discord.Guild,
        row: dict,
        *,
        legacy_category_id: Optional[int] = None,
    ) -> bool:
        """DBへ記録済みの改名intentをDiscordへ反映する。"""
        category, role, errors = self._resolve_private_room_assets(
            guild,
            row,
            legacy_category_id=legacy_category_id,
            require_existing=True,
        )
        if not errors:
            checkpointed = await database.checkpoint_private_room_asset_ids(
                guild.id,
                row["room_id"],
                category_id=category.id if category is not None else None,
                role_id=row.get("role_id"),
            )
            if not checkpointed:
                errors.append("Discord資産IDの保存状態が競合しました")

        if not errors and category is not None and category.name != row["room_name"]:
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
            role_id=row.get("role_id"),
        )
        return True

    async def _delete_private_room_by_row(
        self,
        guild: discord.Guild,
        row: dict,
        *,
        reason: str,
        legacy_category_id: Optional[int] = None,
    ) -> bool:
        active_recruitments = await database.list_active_recruitments_for_room_ids(
            guild.id, {row["room_id"]},
        )
        for recruitment in active_recruitments:
            await database.set_recruitment_status(
                recruitment["id"],
                database.RECRUITMENT_ARCHIVED,
                expected_status=recruitment["status"],
            )
            await database.clear_recruitment_message_id(recruitment["id"])
        await database.mark_private_room_status(
            guild.id, row["room_id"], "deleting", error=None
        )
        errors: list[str] = []
        # 最初にdispatch対象から外す。Discord削除の一部が失敗しても、残存する
        # ボタンやチャンネルから削除中のrunnerを操作できないようにする。
        room = self.rooms.pop(row["room_id"], None)
        runtime_category = getattr(getattr(room, "state", None), "category", None)
        runtime_category_id = getattr(runtime_category, "id", None)
        category, role, asset_errors = self._resolve_private_room_assets(
            guild,
            row,
            legacy_category_id=legacy_category_id or runtime_category_id,
            require_existing=False,
        )
        if (
            category is None
            and isinstance(runtime_category, discord.CategoryChannel)
            and row.get("category_id") is not None
            and runtime_category_id == row.get("category_id")
        ):
            # REST作成直後などguild.categoriesへの反映が遅れていても、保存IDが
            # 完全一致するRunner上のCategoryは同じBot資産として使える。
            category = runtime_category
        if not asset_errors:
            checkpointed = await database.checkpoint_private_room_asset_ids(
                guild.id,
                row["room_id"],
                category_id=(
                    category.id if category is not None else row.get("category_id")
                ),
                role_id=role.id if role is not None else row.get("role_id"),
            )
            if not checkpointed:
                asset_errors.append("Discord資産IDの保存状態が競合しました")
        errors.extend(asset_errors)
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

        if not asset_errors and category is not None:
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

        marker_role = discord.utils.get(
            guild.roles,
            name=f"{MUTE_MARKER_ROLE_PREFIX}{row['room_id']}",
        )
        if role is not None and self._is_protected_from_legacy_access_role_cleanup(
            guild, role,
        ):
            # 壊れたDB参照でGM/仮GM/ゲーム中の生存者ロールを
            # 専用村アクセスロールと誤認しても、Discord側には触れない。
            log.error(
                "専用村削除の保存ロールIDが保護対象のため削除から除外: "
                "room=%s role=%s(%s)",
                row["room_id"],
                getattr(role, "name", "?"),
                getattr(role, "id", "?"),
            )
            role = None
        elif role is not None and not self._legacy_access_role_name_matches(row, role):
            log.warning(
                "専用村削除の保存ロールIDは保存名と一致しないため"
                "Discord側の削除から除外: room=%s role=%s",
                row["room_id"],
                getattr(role, "id", "?"),
            )
            role = None
        roles_to_delete = [
            target for target in (role, marker_role)
            if not asset_errors and target is not None
        ]
        for target_role in roles_to_delete:
            try:
                await self.paced_discord_api_call(target_role.delete, reason=reason)
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"GM村関連ロール削除失敗 ({target_role.name}): {e}")
                errors.append(f"ロール {target_role.id}: {e}")

        if errors:
            await database.mark_private_room_status(
                guild.id, row["room_id"], "deleting", error=" | ".join(errors)[:2000]
            )
            return False

        await database.delete_private_room(guild.id, row["room_id"])
        return True

    async def _delete_private_room_for_owner_safely(
        self,
        guild: discord.Guild,
        owner_id: int,
        *,
        reason: str,
    ) -> bool:
        """ライブ中の自動削除を manager→action→private で直列化する。"""
        async with self.recruitment_manager.lock:
            row = await database.get_private_room_by_owner(guild.id, owner_id)
            if row is None:
                return False
            room = self.rooms.get(row["room_id"])
            if room is None:
                async with self.private_room_lock:
                    latest = await database.get_private_room_by_owner(guild.id, owner_id)
                    if latest is None:
                        return False
                    return await self._delete_private_room_by_row(
                        guild, latest, reason=reason,
                    )
            async with room.action_lock:
                async with self.private_room_lock:
                    latest = await database.get_private_room_by_owner(guild.id, owner_id)
                    if latest is None or latest["room_id"] != room.state.room_id:
                        return False
                    return await self._delete_private_room_by_row(
                        guild, latest, reason=reason,
                    )

    async def _cleanup_private_rooms_without_creator_role(
        self,
        guild: discord.Guild,
        *,
        defer_room_ids: frozenset[str] = frozenset(),
    ) -> set[str]:
        """作成者ロールを失った専用村を削除し、後回しにしたroom_idを返す。

        `defer_room_ids` は進行中ゲームのsnapshotを持つ卓。RoomRunnerの復元前に
        消すと `_delete_private_room_by_row` が `self.rooms` から卓を引けず、
        `force_end` を通らないままチャンネルだけ消えて参加者が放置される。
        復元後に呼び直してもらうため、ここでは削除しない。
        """
        deferred: set[str] = set()
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
            if row["room_id"] in defer_room_ids:
                deferred.add(row["room_id"])
                log.info(
                    "進行中ゲームのある専用村の削除を復元後へ回します: %s",
                    row["room_name"],
                )
                continue
            await self._delete_private_room_by_row(
                guild,
                row,
                reason=f"{PRIVATE_ROOM_CREATOR_ROLE_LABEL}ロール未保持のため専用村削除",
            )
        return deferred

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
        await self.stats_channel.send(embed=embed, view=view)

    async def _sync_all_rank_roles(
        self, guild: discord.Guild, *, paced: bool = False
    ) -> tuple[int, int, int]:
        """全プレイヤーのランクロールを現在ランクへ同期する。

        paced=True は低速モード (1人ごとに小休止)。シーズンリセット直後など
        大量のロール変更が出る場面で、メンバー編集バケットを占有して
        進行中ゲームのニックネーム変更等を遅らせないために使う。
        """
        roles_map = await self._ensure_rank_roles(guild)
        # 同じplayer_idが複数ラダーに存在するため、単一dictへ混ぜない。各ラダーの
        # 母集団で独立にランクを確定してから、プレイヤー単位へ合流する。
        rows_by_ladder = {
            ladder_id: await database.get_all_player_ratings(
                guild.id, ladder_id=ladder_id,
            )
            for ladder_id in LADDER_DEFINITIONS
        }
        rank_maps = {
            ladder_id: rating_lib.build_rank_context_map(
                rows,
                grandmaster_slots=definition.grandmaster_slots,
            )
            for ladder_id, definition in LADDER_DEFINITIONS.items()
            for rows in (rows_by_ladder[ladder_id],)
        }
        player_ids = {
            int(row["player_id"])
            for rows in rows_by_ladder.values()
            for row in rows
        }
        synced = 0
        skipped = 0
        failed = 0

        for player_id in sorted(player_ids):
            member = guild.get_member(player_id)
            rank_names = {
                ladder_id: (
                    context.rank_name
                    if (context := rank_map.get(player_id)) is not None
                    else None
                )
                for ladder_id, rank_map in rank_maps.items()
            }
            if member is None:
                skipped += 1
                continue
            try:
                outcome = await self._sync_rank_role(
                    member,
                    roles_map=roles_map,
                    rank_names_by_ladder=rank_names,
                )
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

        # 無効固定卓にはRunnerを作らない。Discordへチャンネル作成・ロール更新・
        # DM送信等を行う前に、復旧すべき状態を取り残さないことをDBだけで確認する。
        # load_room_states は壊れた行を隔離する場合があるが、Discord副作用は持たない。
        snapshots = await database.load_room_states(guild.id)
        quarantined_room_ids = await database.load_unresolved_room_state_quarantine_ids(
            guild.id
        )
        await self._retire_removed_fixed_rooms(
            guild, snapshots, quarantined_room_ids
        )
        await self._assert_disabled_fixed_rooms_safe_to_skip(
            guild.id,
            snapshots,
            quarantined_room_ids,
        )

        log.info("チャンネルセットアップ開始")
        await self._recover_pending_settlements(guild)
        await self.load_pending_unmutes(guild)
        await self._ensure_gm_staff_roles(guild)
        log.info("GM／仮GMロール確認完了")
        in_progress_room_ids = frozenset(
            room_id
            for room_id, payload in snapshots.items()
            if payload.get("phase") not in (Phase.LOBBY.name, Phase.GAME_OVER.name)
        )
        deferred_private_room_ids = await self._cleanup_private_rooms_without_creator_role(
            guild, defer_room_ids=in_progress_room_ids
        )
        log.info("GM／仮GMロール未保持の専用村クリーンアップ完了")
        await self._load_private_room_runners(guild, snapshots)
        log.info("専用村読み込み完了")
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
        await self._ensure_gm_info_channel(guild)
        log.info(f"GM説明チャンネル確認完了: {GM_INFO_CATEGORY_NAME}/#{CH_GM_INFO}")
        fixed_room_errors: list[str] = []
        completed_rooms = 0
        try:
            for room in self.rooms.values():
                # 卓ごとの開始/完了は起動のたびに卓数ぶん出るのでDEBUGへ。
                # 失敗したときは下の log.exception が卓名を出す。
                log.debug(f"卓セットアップ開始: {room.state.room_name}")
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
                        snapshot_was_active = (
                            snapshot is not None
                            and snapshot.get("phase")
                            not in (Phase.LOBBY.name, Phase.GAME_OVER.name)
                        )
                        if snapshot_was_active:
                            # 復元で霊界の生存者denyとVC発言禁止を確定した後、
                            # 子チャンネルから最後にカテゴリを公開する。
                            await room._sync_active_gm_named_game_channel_visibility(
                                guild,
                                snapshot.get("channel_ids", {}),
                            )
                        # setup/restoreでカテゴリ・VC・#昼・#霊界の公開権限を
                        # 適用できた後に旧閲覧ロールを消す。先に消すと、
                        # 進行中snapshotが旧@everyone denyのまま閉じる。
                        private_row = await database.get_private_room_by_owner(
                            guild.id,
                            int(room.room_def.private_owner_id),
                        )
                        await database.mark_private_room_active(
                            guild.id,
                            room.state.room_id,
                            category_id=room.state.category.id if room.state.category else None,
                            # 旧ロール削除のDiscord APIが失敗した場合に、次回起動の
                            # stable-ID再試行を失わない。成功時だけcleanup側がNULL化する。
                            role_id=(private_row or {}).get("role_id"),
                        )
                        await self._cleanup_legacy_private_room_access_roles(
                            guild,
                            room.state.room_id,
                        )
                    log.debug(f"卓セットアップ完了: {room.state.room_name}")
                    completed_rooms += 1
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
        log.info(f"卓セットアップ完了: {completed_rooms} / {len(self.rooms)}卓")
        for room_id, room in list(self.rooms.items()):
            if room.is_private_room():
                row = await database.get_private_room_by_name(guild.id, room.state.room_name)
                if row is not None and row.get("status") == "error":
                    self.rooms.pop(room_id, None)
        if deferred_private_room_ids:
            # 復元が済んだのでRoomRunnerが self.rooms に載っている。ここで消せば
            # _delete_private_room_by_row が force_end を通し、参加者へ終了を伝えて
            # レートの精算も済ませたうえでチャンネルを片付けられる。
            await self._cleanup_private_rooms_without_creator_role(guild)
            log.info(
                "進行中だった専用村のクリーンアップ完了: %d卓",
                len(deferred_private_room_ids),
            )
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
        # 起動ぶんはここで確定させる。90秒のREADY_TIMEOUTに対して
        # どのAPIがどれだけ食ったかが、そのままログに残る。
        self.log_api_pacing_summary("起動時")
        if (
            hasattr(self.bot, "wait_until_ready")
            and not self.api_pacing_report_loop.is_running()
        ):
            self.api_pacing_report_loop.start()
            log.info("API待ち内訳の記録開始 (30分ごと)")

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
        # v0.38までの共有9人ラダー用GMロールが既にあれば、位置・権限を
        # 保ったままクロストーク側へ改名して再利用する。メンバー構成は、
        # 直後のラダー別ランクロール同期で新しい2ラダーの実績へ揃える。
        legacy_name = rating_lib.LEGACY_NINE_GRANDMASTER_ROLE_NAME
        cross_role_name = rating_lib.special_grandmaster_role_name("l9_cross")
        legacy_role = existing.get(legacy_name)
        if cross_role_name not in existing and legacy_role is not None:
            edit_role = getattr(legacy_role, "edit", None)
            if callable(edit_role):
                try:
                    migrated = await self.paced_discord_api_call(
                        edit_role,
                        name=cross_role_name,
                        hoist=True,
                        reason="9人GMロールを進行別ラダーへ移行",
                    )
                    existing.pop(legacy_name, None)
                    existing[cross_role_name] = migrated or legacy_role
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("旧9人GMロールの改名失敗 (%s): %s", legacy_name, e)
        gm_role_names = {
            rating_lib.special_grandmaster_role_name(ladder_id)
            for ladder_id in LADDER_DEFINITIONS
        }
        for role_name, color_int in rating_lib.all_rank_role_specs():
            role = existing.get(role_name)
            hoist = role_name in gm_role_names
            if role is None:
                try:
                    role = await self.paced_discord_api_call(
                        guild.create_role,
                        name=role_name,
                        color=discord.Color(color_int),
                        hoist=hoist,
                        reason="人狼ランク自動作成",
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"ロール作成失敗 ({role_name}): {e}")
                    continue
            # 既存の通常ランクロールを管理者がhoistしていても戻さない。
            # 今回Botが保証するのは、3つのグランドマスターロールを
            # hoistすることだけで、ほかの手動レイアウトには触れない。
            elif hoist and not bool(getattr(role, "hoist", False)):
                try:
                    role = await self.paced_discord_api_call(
                        role.edit,
                        hoist=hoist,
                        reason="人狼GMロール表示設定",
                    )
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning(f"ロール表示設定失敗 ({role_name}): {e}")
            result[role_name] = role

        # 複数保持者はDiscordの仕様上1欄だけに出る。13人村GMを9人の両GMより
        # 上位に置き、プロフィールでは保持中の全ロールを確認できるようにする。
        l13_role = result.get(rating_lib.special_grandmaster_role_name("l13"))
        nine_roles = [
            result.get(rating_lib.special_grandmaster_role_name(ladder_id))
            for ladder_id in ("l9_cross", "l9_turn")
        ]
        lower_positions = [
            role.position
            for role in nine_roles
            if role is not None
            and isinstance(getattr(role, "position", None), int)
        ]
        if (
            l13_role is not None
            and isinstance(getattr(l13_role, "position", None), int)
            and lower_positions
            and l13_role.position <= max(lower_positions)
            and callable(getattr(l13_role, "edit", None))
        ):
            try:
                moved = await self.paced_discord_api_call(
                    l13_role.edit,
                    position=max(lower_positions),
                    reason="13人村GMを9人の両GMより上位に配置",
                )
                if moved is not None:
                    result[l13_role.name] = moved
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(f"GMロール順序更新失敗: {e}")
        return result

    async def _sync_rank_role(
        self,
        member: discord.Member,
        rank_name: Optional[str] = None,
        roles_map: Optional[dict[str, discord.Role]] = None,
        *,
        ladder_id: str = DEFAULT_LADDER_ID,
        rank_names_by_ladder: Optional[dict[str, Optional[str]]] = None,
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
        if ladder_id not in LADDER_DEFINITIONS:
            raise ValueError(f"unknown ladder_id: {ladder_id}")

        if rank_names_by_ladder is None:
            rank_names: dict[str, Optional[str]] = {}
            if rank_name is not None:
                rank_names[ladder_id] = rank_name
            # 1ラダーの試合終了でも、他ラダーのGMロールを消さず1PATCHへ合流する。
            # 取得不能時に既存ロールを推測で剥がすのは危険なので更新自体を止める。
            for other_ladder_id in LADDER_DEFINITIONS:
                if other_ladder_id in rank_names:
                    continue
                try:
                    info = await database.get_player_current_rank_info(
                        member.id,
                        guild.id,
                        ladder_id=other_ladder_id,
                    )
                except Exception as e:
                    log.warning(
                        "別ラダー取得失敗のためロール同期を保留 (%s/%s): %s",
                        member.display_name,
                        other_ladder_id,
                        e,
                    )
                    return "failed"
                rank_names[other_ladder_id] = (
                    info["rank_name"] if info is not None else None
                )
        else:
            rank_names = {
                ladder: rank_names_by_ladder.get(ladder)
                for ladder in LADDER_DEFINITIONS
            }

        desired_role_names: set[str] = set()
        l13_rank = rank_names.get("l13")
        if l13_rank is not None:
            desired_role_names.add(rating_lib.get_rank_role_name(l13_rank))
        for nine_ladder_id in ("l9_cross", "l9_turn"):
            if rank_names.get(nine_ladder_id) == "グランドマスター":
                desired_role_names.add(
                    rating_lib.special_grandmaster_role_name(nine_ladder_id)
                )

        if roles_map is None:
            roles_map = await self._ensure_rank_roles(guild)
        desired_rank_roles = [
            roles_map[name]
            for name in sorted(desired_role_names)
            if name in roles_map
        ]
        if len(desired_rank_roles) != len(desired_role_names):
            return "failed"

        current_rank_roles = [r for r in member.roles if r.name in all_role_names]
        current_rank_ids = {r.id for r in current_rank_roles}
        desired_rank_ids = {role.id for role in desired_rank_roles}
        if current_rank_ids == desired_rank_ids:
            return "updated"

        # 目標ランクの付与と旧ランクの剥奪を1回のPATCHへ統合する。
        # PATCH自体が失敗すれば旧ロール構成が残るため、「追加成功後に削除失敗」
        # という中間状態も作らない。ゲーム用・専用村等の他ロールは全て維持する。
        desired_roles = [
            role for role in member_roles_for_edit(member)
            if role.name not in all_role_names
        ]
        desired_roles.extend(desired_rank_roles)
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
            async with self.recruitment_manager.lock:
                archived_ids = await database.archive_host_recruitments(
                    member.guild.id, member.id
                )
            for recruitment_id in archived_ids:
                await self.recruitment_manager.refresh_message(recruitment_id)
        except Exception as exc:
            log.exception("退出主催者の募集アーカイブ失敗: %s", exc)
        await self._delete_private_room_for_owner_safely(
            member.guild,
            member.id,
            reason=f"{PRIVATE_ROOM_CREATOR_ROLE_LABEL}がサーバーから退出したため専用村削除",
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if not self._is_managed_guild(after.guild):
            return
        had_creator_role = self._has_private_room_creator_role(before)
        has_creator_role = self._has_private_room_creator_role(after)
        if not had_creator_role or has_creator_role:
            return
        await self._delete_private_room_for_owner_safely(
            after.guild,
            after.id,
            reason=f"{PRIVATE_ROOM_CREATOR_ROLE_LABEL}ロールが外れたため専用村削除",
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not self._is_managed_guild(member.guild):
            return
        # ゲーム中の参加者の復帰だけを扱う。GM名前村の閲覧ロールは
        # 廃止済みで、募集への参加状態はrecruitment_entriesが正本。
        for room in list(self.rooms.values()):
            await room.on_member_join(member)

    def _has_private_room_creator_role(self, member: discord.Member) -> bool:
        return any(
            role.name in PRIVATE_ROOM_CREATOR_ROLE_NAMES
            for role in getattr(member, "roles", ())
        )

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
        """専用村名の衝突チェック。使えない名前ならエラーメッセージを返す。"""
        reserved = {room.name for room in ROOM_DEFINITIONS}
        reserved.update(rating_lib.all_rank_role_names())
        reserved.update(PRIVATE_ROOM_CREATOR_ROLE_NAMES)
        reserved.add(RECRUITMENT_NOTIFICATION_ROLE_NAME)
        reserved.add(STATS_PARENT_CHANNEL_NAME)
        reserved.add(GM_INFO_CATEGORY_NAME)
        reserved.update({LOG_CATEGORY_VILLAGE, LOG_CATEGORY_SPIRIT})
        if name in reserved:
            return "その村名はシステムで使用される名前のため使えません。別の村名にしてください。"

        # 自分の専用村の現在名は改名時に許可する (同名リネーム等)
        own_names = {own_room["room_name"]} if own_room is not None else set()
        if name not in own_names:
            if discord.utils.get(guild.categories, name=name) is not None:
                return "その名前のカテゴリが既に存在するため使えません。別の村名にしてください。"
        return None

    async def _private_reply(self, interaction: discord.Interaction, message: str) -> None:
        """interactionのack済み/未ackを問わずephemeralで応答する。"""
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def ensure_gm_village_for_recruitment(
        self,
        guild: discord.Guild,
        owner: discord.Member,
        *,
        room_name: Optional[str],
        variant_id: str,
    ) -> tuple[RoomRunner, bool]:
        """名前村を用意し、募集作成と同じゲーム形式へ揃える。

        Discordのカテゴリ作成と募集DB作成は同じtransactionにはできないため、
        このメソッドは「村が今回新規か」も返す。後段の募集掲示に失敗した場合、
        呼出側が ``rollback_gm_village_creation`` で今回分だけ回収できる。
        """
        if not self._is_managed_guild(guild) or owner.guild.id != guild.id:
            raise RuntimeError("このBotの管理対象外サーバーでは作成できません。")
        if not self._has_private_room_creator_role(owner):
            raise RuntimeError(
                f"村を作成できるのは {PRIVATE_ROOM_CREATOR_ROLE_LABEL} ロール保持者だけです。"
            )
        if variant_id not in USER_VISIBLE_VARIANT_IDS:
            raise RuntimeError("公開されていないゲーム形式は選択できません。")

        async with self.private_room_lock:
            existing = await database.get_private_room_by_owner(guild.id, owner.id)
            if existing is not None:
                if existing.get("status") not in {"active", "creating"}:
                    raise RuntimeError(
                        "既存のGM村を復旧中のため、新しい募集を作成できません。"
                    )
                room = self.rooms.get(existing["room_id"])
                if room is None:
                    room_def = self._private_room_definition_from_row(existing)
                    room = await self._setup_private_room_from_definition(guild, room_def)
                if room.state.phase not in (Phase.LOBBY, Phase.GAME_OVER):
                    raise RuntimeError("ゲーム中は次の募集を作成できません。")
                if room.state.players:
                    raise RuntimeError(
                        "参加受付に参加者が残っているため募集を作成できません。"
                        "先に受付をリセットしてください。"
                    )
                # 既存村は、後段の募集INSERTが成功するまでは変更しない。
                # ここで先に形式やGMを書き換えると、日時重複などで募集作成だけ
                # 失敗した際に、受付のない設定変更が残ってしまう。
                return room, False

            normalized_name = self._normalize_private_room_name(room_name, owner)
            name_owner = await database.get_private_room_by_name(guild.id, normalized_name)
            if name_owner is not None:
                raise RuntimeError("その村名は既に使われています。別の村名にしてください。")
            name_error = self._private_room_name_error(guild, normalized_name)
            if name_error is not None:
                raise RuntimeError(name_error)

            room_id = self._private_room_id_for(owner.id)
            room_def = RoomDefinition(
                room_id=room_id,
                name=normalized_name,
                allowed_gm_user_ids=frozenset({owner.id}),
                private_owner_id=owner.id,
                private_role_name=None,
                variant_id=variant_id,
            )
            try:
                await database.save_private_room(
                    guild_id=guild.id,
                    room_id=room_id,
                    owner_id=owner.id,
                    room_name=normalized_name,
                    role_name=None,
                    variant_id=variant_id,
                )
                room = await self._setup_private_room_from_definition(guild, room_def)
                room.state.gm_id = owner.id
                await room._persist_room_state()
                return room, True
            except Exception:
                row = await database.get_private_room_by_owner(guild.id, owner.id)
                if row is not None:
                    await self._delete_private_room_by_row(
                        guild, row, reason="村・募集の一体作成失敗を回収"
                    )
                raise

    async def rollback_gm_village_creation(
        self, guild: discord.Guild, room_id: str,
    ) -> None:
        """今回新規作成した村を、募集作成失敗時だけ回収する。"""
        async with self.private_room_lock:
            row = await database.get_private_room_by_owner(
                guild.id,
                int(room_id.removeprefix("private_"))
                if room_id.startswith("private_") and room_id[8:].isdigit()
                else 0,
            )
            if row is None or row["room_id"] != room_id:
                return
            # 同じGMが作成フォームを並行送信した場合、別送信が先に募集を
            # 成功させている可能性がある。その募集ごと村を消さない。
            if await database.get_open_recruitment_for_room(guild.id, room_id) is not None:
                log.info(
                    "別の受付中募集があるため作成失敗時のGM村回収を省略: %s",
                    room_id,
                )
                return
            await self._delete_private_room_by_row(
                guild, row, reason="募集作成失敗に伴う新規GM村の回収"
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
                f"GM村名を変更できるのは **{PRIVATE_ROOM_CREATOR_ROLE_LABEL}** ロール保持者だけです。",
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
                f"GM村名を変更できるのは **{PRIVATE_ROOM_CREATOR_ROLE_LABEL}** ロール保持者だけです。",
            )
            return

        guild = interaction.guild
        row = await database.get_private_room_by_owner(guild.id, interaction.user.id)
        if row is None:
            await self._private_reply(interaction, "変更できるGM村がありません。")
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

        runtime_category_id = getattr(
            getattr(getattr(room, "state", None), "category", None),
            "id",
            None,
        )
        category, _legacy_role, asset_errors = self._resolve_private_room_assets(
            guild,
            row,
            legacy_category_id=runtime_category_id,
            require_existing=True,
        )
        if asset_errors or category is None:
            detail = " / ".join(asset_errors) or "カテゴリが見つかりません"
            await interaction.followup.send(
                "村名変更の対象を保存IDで確認できないため変更しませんでした。"
                f"運営者へ連絡してください。 ({detail})",
                ephemeral=True,
            )
            return
        checkpointed = await database.checkpoint_private_room_asset_ids(
            guild.id,
            row["room_id"],
            category_id=category.id,
            role_id=row.get("role_id"),
        )
        if not checkpointed:
            await interaction.followup.send(
                "村名変更の対象が同時に変更されたため、何も変更しませんでした。"
                "もう一度お試しください。",
                ephemeral=True,
            )
            return
        row = {
            **row,
            "category_id": category.id,
        }

        try:
            # Discord操作より先にdesired nameをjournalする。ここでクラッシュしても
            # status=renamingと安定IDから起動時に再開できる。
            await database.update_private_room_names(
                guild.id,
                row["room_id"],
                normalized_name,
                # 旧ロール削除がDiscord API失敗で再試行待ちなら、
                # role_idと照合する旧名を失わない。削除済み行は既にNULL。
                row.get("role_name"),
            )
        except sqlite3.IntegrityError:
            await interaction.followup.send(
                "その村名は同時に別のGM村で使われました。別の名前を指定してください。",
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
            "role_name": row.get("role_name"),
            "status": "renaming",
            "category_id": category.id if category is not None else row.get("category_id"),
            "role_id": row.get("role_id"),
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
                "GM村の操作を停止しており、次回起動時に安全に再試行します。",
                ephemeral=True,
            )
            return

        room_def = RoomDefinition(
            room_id=row["room_id"],
            name=normalized_name,
            allowed_gm_user_ids=frozenset({interaction.user.id}),
            private_owner_id=interaction.user.id,
            private_role_name=None,
            variant_id=str(row.get("variant_id") or "v13_cross"),
        )
        if room is None:
            room = RoomRunner(self.bot, self, room_def)
            self.rooms[row["room_id"]] = room
        else:
            room.room_def = room_def
            room.state.room_name = normalized_name
            if category is not None:
                room.state.category = category
        if room.state.category is not None:
            await self._apply_room_visibility(guild, room.state.category, room_def)
        if room.state.lobby_channel is not None:
            await room._post_lobby_ui()
        await interaction.followup.send(f"GM村名を **{normalized_name}** に変更しました。", ephemeral=True)

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
                "削除できるGM村がありません。", ephemeral=True
            )
            return
        if (
            not self._has_private_room_creator_role(interaction.user)
            and not interaction.user.guild_permissions.manage_guild
        ):
            await interaction.followup.send(
                f"GM村を削除できるのは村主本人の **{PRIVATE_ROOM_CREATOR_ROLE_LABEL}** "
                "ロール保持者、またはサーバー管理者だけです。",
                ephemeral=True,
            )
            return
        room = self.rooms.get(row["room_id"])
        if room is not None and room.state.phase not in (Phase.LOBBY, Phase.GAME_OVER):
            await interaction.followup.send(
                "ゲーム中のGM村は削除できません。先にゲームを終了してください。",
                ephemeral=True,
            )
            return

        async def execute(confirm_interaction: discord.Interaction) -> None:
            await self._delete_private_room_confirmed(confirm_interaction)

        await interaction.followup.send(
            f"⚠️ GM村 **{row['room_name']}** のカテゴリ・チャンネルを削除します。実行しますか？",
            view=DangerConfirmView(
                interaction.user.id,
                execute,
                confirm_label="GM村を削除",
            ),
            ephemeral=True,
        )

    async def _delete_private_room_confirmed(
        self, interaction: discord.Interaction
    ) -> None:
        """確認操作の直後に状態を再検査し、GM村を削除する。"""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self._private_reply(interaction, "この操作はサーバー内でのみ使用できます。")
            return
        # 参加・形式変更・開催と同じ manager→action→private の順で固定する。
        async with self.recruitment_manager.lock:
            row = await database.get_private_room_by_owner(
                interaction.guild.id, interaction.user.id,
            )
            room = self.rooms.get(row["room_id"]) if row is not None else None
            if room is None:
                async with self.private_room_lock:
                    await self._delete_private_room_locked(interaction)
                return
            # 確認画面を開いた後にtransfer/startが始まっても、LOBBY再検査と
            # runner除外を一体にする。
            async with room.action_lock, self.private_room_lock:
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
            await self._private_reply(interaction, "削除できるGM村がありません。")
            return
        if not self._has_private_room_creator_role(interaction.user) and not interaction.user.guild_permissions.manage_guild:
            await self._private_reply(
                interaction,
                f"GM村を削除できるのは村主本人の **{PRIVATE_ROOM_CREATOR_ROLE_LABEL}** ロール保持者、またはサーバー管理者だけです。",
            )
            return

        room = self.rooms.get(row["room_id"])
        if room is not None and room.state.phase not in (Phase.LOBBY, Phase.GAME_OVER):
            await self._private_reply(
                interaction,
                "ゲーム中のGM村は削除できません。先にゲームを終了してください。",
            )
            return
        deleted = await self._delete_private_room_by_row(guild, row, reason="専用村削除")
        if deleted:
            await interaction.followup.send(f"GM村 **{row['room_name']}** を削除しました。", ephemeral=True)
        else:
            await interaction.followup.send(
                "GM村の一部を削除できませんでした。記録を保持しているため、起動時に再試行します。",
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
        private_row = await database.get_private_room_by_owner(
            guild.id,
            int(room_def.private_owner_id),
        )
        await database.mark_private_room_active(
            guild.id,
            room_def.room_id,
            category_id=room.state.category.id if room.state.category else None,
            role_id=(private_row or {}).get("role_id"),
        )
        await self._cleanup_legacy_private_room_access_roles(
            guild,
            room_def.room_id,
        )
        return room

    @app_commands.command(
        name="private_room_create",
        description="自分のGM村と募集を一度に作成します（GM／仮GM専用）",
    )
    async def private_room_create(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await self.recruitment_manager.start_village_creation(interaction)

    @app_commands.command(
        name="private_room_delete",
        description="自分のGM村と受付中の募集を削除します（GM／仮GM専用）",
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
