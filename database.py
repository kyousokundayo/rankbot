"""SQLite統計データベース"""
from __future__ import annotations

import json
import logging
import aiosqlite
from collections.abc import Collection
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from config import (
    BONUS_WOLF_GUESS_SLOTS,
    FEEDBACK_MAX_PER_DAY,
    INITIAL_RATING,
    LEADERBOARD_LIMIT,
    LEADERBOARD_MIN_SAMPLES,
    PLAYER_BLOCK_LIMIT,
    RECRUITMENT_BACKUP_CAPACITY,
    RECRUITMENT_ARCHIVE_RETENTION_DAYS,
    RECRUITMENT_CAPACITY,
    RECRUITMENT_MAX_PER_HOST,
    RECRUITMENT_NOTIFICATION_WINDOW_MINUTES,
    RECRUITMENT_OCCUPANCY_MINUTES,
    RECRUITMENT_RANK_OPTIONS,
    RECRUITMENT_UNRANKED_LABEL,
    ROOM_DEFINITION_MAP,
    Phase,
    Role,
    Team,
)
import rating as rating_lib

log = logging.getLogger(__name__)

# 起動ディレクトリに依存しない絶対パス (bot/data/ 配下)
# テスト/シミュレーションからは DB_PATH を差し替えて使う
DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = str(DATA_DIR / "werewolf_stats.db")
DB_TIMEOUT_SECONDS = 10
DB_BUSY_TIMEOUT_MS = 10_000
ROOM_STATE_SCHEMA_VERSION = 1
RATING_SCALE_MIGRATION_KEY = "rating_scale_1500_v1"
RATING_SCALE_MIGRATION_OFFSET = 300

BACKUP_DIR = DATA_DIR / "backups"
BACKUP_KEEP_PER_LABEL = 10


class SeasonResetConflict(RuntimeError):
    """同一シーズンの二重リセットまたは古い要求を拒否した。"""


class SettlementNotFound(RuntimeError):
    """指定された未精算ゲームが存在しない。"""


class RecruitmentConflict(RuntimeError):
    """募集の予約・定員・重複参加などの競合。"""


class PlayerBlockLimitReached(RuntimeError):
    """同村拒否の登録上限に達した。"""


class FeedbackRateLimited(RuntimeError):
    """不具合・改善報告の投稿上限に達した。"""


_EXPECTED_GM_UNSET = object()


def _is_snapshot_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_wolf_target(value) -> bool:
    return value == -1 or _is_snapshot_id(value)


def _validate_room_snapshot(phase: str, payload: dict) -> None:
    """復元コードが前提にする構造をDB読込境界で検証する。"""
    phase_names = {item.name for item in Phase}
    if phase not in phase_names:
        raise ValueError(f"unknown phase: {phase}")
    recovery_phase = payload.get("recovery_phase")
    if recovery_phase is not None and recovery_phase not in phase_names:
        raise ValueError(f"unknown recovery_phase: {recovery_phase}")
    pending_winner = payload.get("pending_winner")
    if pending_winner is not None and pending_winner not in {item.name for item in Team}:
        raise ValueError(f"unknown pending_winner: {pending_winner}")

    for key in ("day_number", "day_generation", "night_generation"):
        if key in payload and (
            not isinstance(payload[key], int)
            or isinstance(payload[key], bool)
            or payload[key] < 0
        ):
            raise ValueError(f"{key} is not a non-negative integer")
    for key in (
        "gm_id", "day_executed_target", "pending_execution_target",
        "initial_seer_target", "guard_previous",
        "seer_target", "guard_target", "last_game_gm",
        "last_executed", "last_killed", "day1_executed_id", "night1_killed_id",
        "recruitment_id", "morning_panel_message_id",
    ):
        value = payload.get(key)
        if value is not None and not _is_snapshot_id(value):
            raise ValueError(f"{key} is not an integer ID")
    wolf_target = payload.get("wolf_target")
    if wolf_target is not None and not _is_wolf_target(wolf_target):
        raise ValueError("wolf_target is not a player ID or no-attack sentinel")
    game_run_id = payload.get("game_run_id")
    if game_run_id is not None and not isinstance(game_run_id, str):
        raise ValueError("game_run_id is not a string")
    for key in (
        "day_execution_resolved", "night_resolved", "initial_seer_result_sent",
        "morning_confirmed", "prep_confirmed", "mute_marker_enabled",
    ):
        if key in payload and not isinstance(payload[key], bool):
            raise ValueError(f"{key} is not boolean")

    players = payload.get("players", [])
    if not isinstance(players, list):
        raise ValueError("players is not a list")
    seen_player_ids: set[int] = set()
    role_names = {item.name for item in Role}
    for index, row in enumerate(players):
        if not isinstance(row, dict):
            raise ValueError(f"players[{index}] is not an object")
        user_id = row.get("user_id")
        if not _is_snapshot_id(user_id):
            raise ValueError(f"players[{index}].user_id is invalid")
        if user_id in seen_player_ids:
            raise ValueError(f"duplicate player user_id: {user_id}")
        seen_player_ids.add(user_id)
        role_name = row.get("role")
        if role_name is not None and role_name not in role_names:
            raise ValueError(f"players[{index}].role is invalid: {role_name}")
        if "alive" in row and not isinstance(row["alive"], bool):
            raise ValueError(f"players[{index}].alive is not boolean")
        if "number" in row and (
            not isinstance(row["number"], int) or isinstance(row["number"], bool)
        ):
            raise ValueError(f"players[{index}].number is not an integer")
        if phase not in {Phase.LOBBY.name, Phase.GAME_OVER.name}:
            if role_name is None:
                raise ValueError(f"players[{index}].role is required in active phase")
            if not isinstance(row.get("number"), int) or row.get("number", 0) <= 0:
                raise ValueError(f"players[{index}].number must be positive in active phase")

    for key in (
        "preparation_dm_sent_ids", "morning_ready_ids", "morning_warned_ids",
        "prep_ready_ids",
        "disconnected_players", "bot_muted_ids", "bot_mute_intent_ids",
        "last_game_roster",
        "runoff_candidates",
    ):
        values = payload.get(key, [])
        if not isinstance(values, list) or any(not _is_snapshot_id(value) for value in values):
            raise ValueError(f"{key} is not an ID list")

    for key in ("original_nicknames", "votes", "wolf_voters", "pending_death_effects"):
        if not isinstance(payload.get(key, []), list):
            raise ValueError(f"{key} is not a list")
    for index, row in enumerate(payload.get("original_nicknames", [])):
        if not isinstance(row, dict) or not _is_snapshot_id(row.get("user_id")):
            raise ValueError(f"original_nicknames[{index}] is invalid")
    for index, row in enumerate(payload.get("votes", [])):
        if (
            not isinstance(row, dict)
            or not _is_snapshot_id(row.get("voter_id"))
            or not _is_snapshot_id(row.get("target_id"))
        ):
            raise ValueError(f"votes[{index}] is invalid")
    for index, row in enumerate(payload.get("wolf_voters", [])):
        if (
            not isinstance(row, dict)
            or not _is_snapshot_id(row.get("user_id"))
            or not _is_wolf_target(row.get("target_id"))
        ):
            raise ValueError(f"wolf_voters[{index}] is invalid")
    for index, row in enumerate(payload.get("pending_death_effects", [])):
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("event_id"), str)
            or not row["event_id"]
            or not _is_snapshot_id(row.get("player_id"))
            or row.get("method") not in {"処刑", "襲撃", "除外"}
            or (row.get("reason") is not None and not isinstance(row["reason"], str))
        ):
            raise ValueError(f"pending_death_effects[{index}] is invalid")

    channel_ids = payload.get("channel_ids", {})
    if not isinstance(channel_ids, dict):
        raise ValueError("channel_ids is not an object")
    for key, value in channel_ids.items():
        if key not in {"category", "lobby", "stats", "voice", "village", "spirit"}:
            continue
        if value is not None and not _is_snapshot_id(value):
            raise ValueError(f"channel_ids.{key} is not an integer ID")


@asynccontextmanager
async def connect_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT_SECONDS) as db:
        await db.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        await db.execute("PRAGMA foreign_keys = ON")
        # synchronousは接続ごとの設定 (WALと違いDBファイルに永続しない)
        await db.execute("PRAGMA synchronous = NORMAL")
        yield db


async def backup_db(*, label: str = "auto", keep: int = BACKUP_KEEP_PER_LABEL) -> Optional[str]:
    """DBを data/backups/ へバックアップし、作成したファイルパスを返す。

    SQLiteのオンラインバックアップAPIを使うため、Bot稼働中 (WAL) でも安全。
    ラベルごとに直近 keep 件だけ残して古いものは削除する
    (起動時バックアップの世代がリセット前バックアップを押し流さないように)。
    DBファイルがまだ無ければ何もしない。
    """
    src_path = Path(DB_PATH)
    if not src_path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    dest = BACKUP_DIR / f"{src_path.stem}_{timestamp}_{label}.db"
    async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT_SECONDS) as src:
        async with aiosqlite.connect(str(dest)) as dst:
            await src.backup(dst)
            # 元DBがWALなのでバックアップ先もWALになる。閉じる前に
            # 本体へ書き戻し、-wal / -shm 無しで復元できる状態にする。
            await dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # チェックポイント済みなので、残っていても復元には要らない
    for suffix in ("-wal", "-shm"):
        Path(f"{dest}{suffix}").unlink(missing_ok=True)

    if keep > 0:
        backups = sorted(BACKUP_DIR.glob(f"{src_path.stem}_*_{label}.db"))
        for old in backups[:-keep]:
            _remove_backup_files(old)
        _remove_orphan_backup_sidecars(src_path.stem)
    return str(dest)


def _remove_backup_files(backup: Path) -> None:
    """バックアップ本体と、SQLiteが同名で作る -wal / -shm をまとめて消す。

    本体だけ消すと、バックアップ先がWALモードのときに残る
    `xxx.db-wal` / `xxx.db-shm` が回収されず、世代を捨てても
    ディスクを食い続ける。
    """
    for suffix in ("", "-wal", "-shm"):
        Path(f"{backup}{suffix}").unlink(missing_ok=True)


def _remove_orphan_backup_sidecars(stem: str) -> None:
    """本体が無い -wal / -shm を回収する。

    本体だけを消していた頃に取り残されたぶんを、次のバックアップで
    まとめて片付ける。単体では復元に使えないので消して問題ない。
    """
    for sidecar in BACKUP_DIR.glob(f"{stem}_*.db-*"):
        name = sidecar.name
        for suffix in ("-wal", "-shm"):
            if not name.endswith(suffix):
                continue
            if not (BACKUP_DIR / name[: -len(suffix)]).exists():
                sidecar.unlink(missing_ok=True)
            break


async def _ensure_column(
    db: aiosqlite.Connection,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    rows = await db.execute_fetchall(f"PRAGMA table_info({table_name})")
    existing = {row[1] for row in rows}
    if column_name not in existing:
        await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {definition}")


async def init_db() -> None:
    async with connect_db() as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS games (
                game_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                room_id TEXT NOT NULL DEFAULT '',
                room_name TEXT NOT NULL DEFAULT '',
                game_run_id TEXT,
                gm_id INTEGER,
                base_room_id TEXT,
                recruitment_id INTEGER,
                winner_team TEXT NOT NULL,
                played_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS game_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                team TEXT NOT NULL,
                won INTEGER NOT NULL,
                died_on_day INTEGER,
                death_cause TEXT,
                rank_at_game TEXT,
                rank_provisional INTEGER,
                FOREIGN KEY (game_id) REFERENCES games(game_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS game_stats (
                game_id INTEGER PRIMARY KEY,
                days INTEGER NOT NULL,
                peaceful_mornings INTEGER NOT NULL,
                guard_successes INTEGER NOT NULL,
                guard_checks INTEGER NOT NULL,
                seer_checks INTEGER NOT NULL,
                seer_wolf_hits INTEGER NOT NULL,
                day1_execution_was_wolf INTEGER,
                executions_total INTEGER NOT NULL,
                executions_wolf INTEGER NOT NULL,
                night1_kill_had_role INTEGER,
                wolf_alive_by_day TEXT NOT NULL,
                rank_bucket TEXT,
                FOREIGN KEY (game_id) REFERENCES games(game_id)
            )
        """)
        # ============================================================
        # レーティング: 現在値
        # ============================================================
        await db.execute("""
            CREATE TABLE IF NOT EXISTS player_ratings (
                player_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                rating INTEGER NOT NULL,
                peak_rating INTEGER NOT NULL,
                games INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                season_games INTEGER NOT NULL DEFAULT 0,
                season_wins INTEGER NOT NULL DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (player_id, guild_id)
            )
        """)
        # ============================================================
        # レーティング: 全変動履歴 (永続)
        # ============================================================
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rating_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                game_id INTEGER NOT NULL,
                rating_before INTEGER NOT NULL,
                rating_after INTEGER NOT NULL,
                elo_delta INTEGER NOT NULL,
                bonus INTEGER NOT NULL DEFAULT 0,
                play_bonus INTEGER NOT NULL DEFAULT 0,
                recommendation_bonus INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(game_id)
            )
        """)
        # ============================================================
        # シーズンリセット履歴
        # ============================================================
        await db.execute("""
            CREATE TABLE IF NOT EXISTS season_resets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                executed_by INTEGER NOT NULL,
                reset_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                affected_players INTEGER NOT NULL DEFAULT 0,
                note TEXT
            )
        """)
        # ============================================================
        # シーズンリセット時のスナップショット (各プレイヤーの前後)
        # ============================================================
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rating_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_reset_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                rating_before INTEGER NOT NULL,
                rating_after INTEGER NOT NULL,
                season_rank TEXT,
                rank_position INTEGER,
                top_percent REAL,
                season_games INTEGER NOT NULL DEFAULT 0,
                season_wins INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (season_reset_id) REFERENCES season_resets(id)
            )
        """)
        # ============================================================
        # Bot運用メタ情報 (key-value。再起動をまたいで保持したい小さな状態)
        # ============================================================
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_meta (
                guild_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, key)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_unmutes (
                guild_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, member_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS room_states (
                guild_id INTEGER NOT NULL,
                room_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, room_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS room_state_quarantine (
                guild_id INTEGER NOT NULL,
                room_id TEXT NOT NULL,
                phase TEXT,
                payload TEXT,
                error TEXT NOT NULL,
                quarantined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, room_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS game_settlements (
                guild_id INTEGER NOT NULL,
                room_id TEXT NOT NULL,
                game_run_id TEXT NOT NULL,
                room_name TEXT NOT NULL DEFAULT '',
                rated INTEGER NOT NULL,
                winner_team TEXT NOT NULL,
                player_records TEXT NOT NULL,
                stats_payload TEXT,
                bonus_payload TEXT,
                gm_id INTEGER,
                base_room_id TEXT,
                recruitment_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                game_id INTEGER,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, room_id, game_run_id),
                FOREIGN KEY (game_id) REFERENCES games(game_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS game_recommendations (
                game_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                voter_id INTEGER NOT NULL,
                kind TEXT NOT NULL DEFAULT 'recommend',
                recipient_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                expires_at TEXT NOT NULL,
                confirmed_at TIMESTAMP,
                awarded_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (game_id, voter_id, kind),
                FOREIGN KEY (game_id) REFERENCES games(game_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS private_rooms (
                guild_id INTEGER NOT NULL,
                room_id TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                room_name TEXT NOT NULL,
                role_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                category_id INTEGER,
                role_id INTEGER,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, room_id),
                UNIQUE (guild_id, owner_id),
                UNIQUE (guild_id, room_name),
                UNIQUE (guild_id, role_name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS private_room_members (
                guild_id INTEGER NOT NULL,
                room_id TEXT NOT NULL,
                member_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                last_error TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, room_id, member_id),
                FOREIGN KEY (guild_id, room_id) REFERENCES private_rooms(guild_id, room_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS recruitments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                host_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                room_id TEXT NOT NULL,
                gm_id INTEGER,
                streaming INTEGER NOT NULL DEFAULT 0,
                allowed_ranks TEXT,
                note TEXT,
                status TEXT NOT NULL DEFAULT '募集中',
                notified_at TEXT,
                ready_notified_at TEXT,
                closed_at TEXT,
                message_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS recruitment_entries (
                recruitment_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('参加', '補欠')),
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (recruitment_id, user_id),
                FOREIGN KEY (recruitment_id) REFERENCES recruitments(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS player_blocks (
                guild_id INTEGER NOT NULL,
                blocker_id INTEGER NOT NULL,
                blocked_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, blocker_id, blocked_id),
                CHECK(blocker_id <> blocked_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS feedback_reports (
                report_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                summary TEXT NOT NULL,
                details TEXT,
                bot_version TEXT NOT NULL,
                room_id TEXT,
                room_name TEXT,
                phase TEXT,
                source_channel_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await _ensure_column(db, "games", "room_id", "room_id TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "games", "room_name", "room_name TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "games", "game_run_id", "game_run_id TEXT")
        await _ensure_column(db, "games", "gm_id", "gm_id INTEGER")
        await _ensure_column(db, "games", "base_room_id", "base_room_id TEXT")
        await _ensure_column(db, "games", "recruitment_id", "recruitment_id INTEGER")
        await _ensure_column(db, "game_players", "died_on_day", "died_on_day INTEGER")
        await _ensure_column(db, "game_players", "death_cause", "death_cause TEXT")
        await _ensure_column(db, "game_players", "rank_at_game", "rank_at_game TEXT")
        # 3狼提出の的中数。NULLは「提出していない/対象外」で、統計の分母にも入らない
        await _ensure_column(
            db, "game_players", "wolf_guess_hits", "wolf_guess_hits INTEGER",
        )
        await _ensure_column(
            db, "game_players", "rank_provisional", "rank_provisional INTEGER",
        )
        await _ensure_column(
            db, "game_stats", "guard_checks",
            "guard_checks INTEGER NOT NULL DEFAULT 0",
        )
        await _ensure_column(db, "player_ratings", "season_games", "season_games INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "player_ratings", "season_wins", "season_wins INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "rating_snapshots", "season_rank", "season_rank TEXT")
        await _ensure_column(db, "rating_snapshots", "rank_position", "rank_position INTEGER")
        await _ensure_column(db, "rating_snapshots", "top_percent", "top_percent REAL")
        await _ensure_column(db, "rating_snapshots", "season_games", "season_games INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "rating_snapshots", "season_wins", "season_wins INTEGER NOT NULL DEFAULT 0")
        await _ensure_column(db, "private_rooms", "status", "status TEXT NOT NULL DEFAULT 'active'")
        await _ensure_column(db, "private_rooms", "category_id", "category_id INTEGER")
        await _ensure_column(db, "private_rooms", "role_id", "role_id INTEGER")
        await _ensure_column(db, "private_rooms", "last_error", "last_error TEXT")
        await _ensure_column(db, "private_room_members", "status", "status TEXT NOT NULL DEFAULT 'active'")
        await _ensure_column(db, "private_room_members", "last_error", "last_error TEXT")
        await _ensure_column(db, "game_settlements", "room_name", "room_name TEXT NOT NULL DEFAULT ''")
        await _ensure_column(db, "game_settlements", "stats_payload", "stats_payload TEXT")
        await _ensure_column(db, "game_settlements", "bonus_payload", "bonus_payload TEXT")
        # 終了後の票を「推薦」と「勝利陣営→敗北陣営」の2種類へ広げる。
        # 勝利陣営の霊媒師などは1人で両方を持つので、主キーに kind が要る。
        # 列追加では主キーを変えられないため、ここだけ作り直しになる。
        recommendation_columns = {
            row[1] for row in await db.execute_fetchall(
                "PRAGMA table_info(game_recommendations)"
            )
        }
        if recommendation_columns and "kind" not in recommendation_columns:
            await db.execute("""
                CREATE TABLE game_recommendations_migrated (
                    game_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    voter_id INTEGER NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'recommend',
                    recipient_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    expires_at TEXT NOT NULL,
                    confirmed_at TIMESTAMP,
                    awarded_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (game_id, voter_id, kind),
                    FOREIGN KEY (game_id) REFERENCES games(game_id)
                )
            """)
            await db.execute(
                "INSERT INTO game_recommendations_migrated "
                "(game_id, guild_id, voter_id, kind, recipient_id, status, "
                "expires_at, confirmed_at, awarded_at, created_at) "
                "SELECT game_id, guild_id, voter_id, 'recommend', recipient_id, status, "
                "expires_at, confirmed_at, awarded_at, created_at "
                "FROM game_recommendations"
            )
            await db.execute("DROP TABLE game_recommendations")
            await db.execute(
                "ALTER TABLE game_recommendations_migrated "
                "RENAME TO game_recommendations"
            )
        await _ensure_column(
            db, "rating_history", "play_bonus",
            "play_bonus INTEGER NOT NULL DEFAULT 0",
        )
        await _ensure_column(db, "game_settlements", "gm_id", "gm_id INTEGER")
        await _ensure_column(
            db, "game_settlements", "base_room_id", "base_room_id TEXT",
        )
        await _ensure_column(
            db, "game_settlements", "recruitment_id", "recruitment_id INTEGER",
        )
        await _ensure_column(
            db, "recruitments", "ready_notified_at", "ready_notified_at TEXT",
        )
        await _ensure_column(db, "recruitments", "closed_at", "closed_at TEXT")
        await _ensure_column(db, "recruitments", "allowed_ranks", "allowed_ranks TEXT")
        recruitment_columns = {
            row[1] for row in await db.execute_fetchall("PRAGMA table_info(recruitments)")
        }
        if "allow_beginner" in recruitment_columns:
            # 旧「初心者参加不可」は、意味を変えずゴールド以上の選択へ移す。
            # 旧「参加可」およびNULLは、新方式の「制限なし」(NULL) のまま扱う。
            beginner_ranks = ROOM_DEFINITION_MAP["beginner"].allowed_ranks or frozenset()
            legacy_allowed = [
                rank_name for rank_name in RECRUITMENT_RANK_OPTIONS
                if rank_name != RECRUITMENT_UNRANKED_LABEL
                and rank_name not in beginner_ranks
            ]
            await db.execute(
                "UPDATE recruitments SET allowed_ranks = ? "
                "WHERE room_id = 'open' AND allow_beginner = 0 AND allowed_ranks IS NULL",
                (json.dumps(legacy_allowed, ensure_ascii=False),),
            )
        await _ensure_column(
            db, "rating_history", "recommendation_bonus",
            "recommendation_bonus INTEGER NOT NULL DEFAULT 0",
        )
        # ============================================================
        # インデックス: 統計クエリ高速化
        # ============================================================
        # get_player_stats: WHERE player_id = ?
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_game_players_player_id "
            "ON game_players(player_id)"
        )
        # get_all_player_ratings / ランキング集計: WHERE guild_id = ? の絞り込み用
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_player_ratings_guild_rating "
            "ON player_ratings(guild_id, rating DESC)"
        )
        # rating_history 検索 (将来の履歴表示用)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_rating_history_player_guild "
            "ON rating_history(player_id, guild_id)"
        )
        # 旧DBに重複履歴があっても起動不能にしない。ゲームrun自体の冪等性は
        # idx_games_run_uniqueで保証し、履歴側は検索用の非unique indexに留める。
        await db.execute("DROP INDEX IF EXISTS idx_game_players_game_player")
        await db.execute("DROP INDEX IF EXISTS idx_rating_history_game_player")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_game_players_game_player "
            "ON game_players(game_id, player_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_rating_history_game_player "
            "ON rating_history(game_id, player_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_games_guild_played_at "
            "ON games(guild_id, played_at DESC)"
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_games_run_unique "
            "ON games(guild_id, room_id, game_run_id) "
            "WHERE game_run_id IS NOT NULL AND game_run_id <> ''"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_reports_guild_created "
            "ON feedback_reports(guild_id, report_id DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_game_recommendations_guild_status "
            "ON game_recommendations(guild_id, status, expires_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_recruitments_guild_scheduled "
            "ON recruitments(guild_id, scheduled_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_recruitment_entries_recruitment "
            "ON recruitment_entries(recruitment_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_player_blocks_guild_blocked "
            "ON player_blocks(guild_id, blocked_id)"
        )
        # rating_snapshots はシーズンリセットのたびに全プレイヤー分が積み増され、
        # 削除されない。統計表示の主要導線 (自分の統計 / ユーザー選択) が毎回
        # 引くので、行数が増える前に索引を張っておく。
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_rating_snapshots_player_guild "
            "ON rating_snapshots(player_id, guild_id, season_reset_id DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_rating_snapshots_reset_guild "
            "ON rating_snapshots(season_reset_id, guild_id)"
        )
        await db.commit()


async def rating_scale_migration_needed() -> bool:
    """旧1200基準から1500基準への一度限りの移行が未実行か返す。"""
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT 1 FROM bot_meta WHERE guild_id = 0 AND key = ?",
            (RATING_SCALE_MIGRATION_KEY,),
        )
    return not rows


async def migrate_rating_scale_to_1500() -> int:
    """既存の全レート値を+300し、新しい1500基準へ原子的に移行する。

    現在値だけでなく履歴とシーズンスナップショットも同じ幅だけ動かすため、
    過去の増減量と順位関係は変わらない。二度目以降は何もしない。
    戻り値は移行した現在レート行数。
    """
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        rows = await db.execute_fetchall(
            "SELECT 1 FROM bot_meta WHERE guild_id = 0 AND key = ?",
            (RATING_SCALE_MIGRATION_KEY,),
        )
        if rows:
            await db.rollback()
            return 0

        count_rows = await db.execute_fetchall("SELECT COUNT(*) FROM player_ratings")
        affected = int(count_rows[0][0]) if count_rows else 0
        offset = RATING_SCALE_MIGRATION_OFFSET
        await db.execute(
            "UPDATE player_ratings SET rating = rating + ?, "
            "peak_rating = peak_rating + ?",
            (offset, offset),
        )
        await db.execute(
            "UPDATE rating_history SET rating_before = rating_before + ?, "
            "rating_after = rating_after + ?",
            (offset, offset),
        )
        await db.execute(
            "UPDATE rating_snapshots SET rating_before = rating_before + ?, "
            "rating_after = rating_after + ?",
            (offset, offset),
        )
        await db.execute(
            "INSERT INTO bot_meta (guild_id, key, value) VALUES (0, ?, ?)",
            (RATING_SCALE_MIGRATION_KEY, f"+{offset}"),
        )
        await db.commit()
    return affected


async def save_feedback_report(
    *,
    guild_id: int,
    user_id: int,
    category: str,
    summary: str,
    details: Optional[str],
    bot_version: str,
    room_id: Optional[str] = None,
    room_name: Optional[str] = None,
    phase: Optional[str] = None,
    source_channel_id: Optional[int] = None,
) -> int:
    """プレイヤーから届いた不具合・改善報告をローカルDBへ保存する。

    誰でも押せるフォームなので、1人あたり直近24時間の件数で上限をかける。
    上限判定とINSERTは BEGIN IMMEDIATE の同一トランザクションで行い、
    連打で同時に走っても上限を越えられないようにする。
    """
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        recent_rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM feedback_reports "
            "WHERE guild_id=? AND user_id=? "
            "AND created_at >= datetime('now', '-1 day')",
            (guild_id, user_id),
        )
        if int(recent_rows[0][0]) >= FEEDBACK_MAX_PER_DAY:
            await db.rollback()
            raise FeedbackRateLimited(
                f"報告は1日{FEEDBACK_MAX_PER_DAY}件までです。"
                "時間をおいてからお試しください。"
            )
        cursor = await db.execute(
            "INSERT INTO feedback_reports "
            "(guild_id, user_id, category, summary, details, bot_version, "
            "room_id, room_name, phase, source_channel_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id,
                user_id,
                category[:32],
                summary[:1000],
                details[:1000] if details else None,
                bot_version[:32],
                room_id,
                room_name,
                phase,
                source_channel_id,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def load_recent_feedback_reports(guild_id: int, limit: int = 50) -> list[dict]:
    """管理・調査用に新しい報告から取得する。"""
    safe_limit = max(1, min(int(limit), 200))
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT report_id, user_id, category, summary, details, bot_version, "
            "room_id, room_name, phase, source_channel_id, created_at "
            "FROM feedback_reports WHERE guild_id = ? "
            "ORDER BY report_id DESC LIMIT ?",
            (guild_id, safe_limit),
        )
    return [
        {
            "report_id": int(row[0]),
            "user_id": int(row[1]),
            "category": row[2],
            "summary": row[3],
            "details": row[4],
            "bot_version": row[5],
            "room_id": row[6],
            "room_name": row[7],
            "phase": row[8],
            "source_channel_id": row[9],
            "created_at": row[10],
        }
        for row in rows
    ]


async def stage_game_settlement(
    guild_id: int,
    room_id: str,
    game_run_id: str,
    *,
    room_name: str,
    rated: bool,
    winner_team: str,
    player_records: list[dict],
    game_stats: Optional[dict] = None,
    bonus_facts: Optional[dict] = None,
    gm_id: Optional[int] = None,
    base_room_id: Optional[str] = None,
    recruitment_id: Optional[int] = None,
) -> None:
    """結果を先に永続化し、クラッシュ後も同じrun IDで精算できるようにする。"""
    if not game_run_id:
        raise ValueError("game_run_id is required")
    player_ids = [int(rec["player_id"]) for rec in player_records]
    if not player_ids or len(player_ids) != len(set(player_ids)):
        raise ValueError("player_records must contain unique players")
    payload = json.dumps(player_records, ensure_ascii=False)
    stats_payload: Optional[str] = None
    if game_stats is not None:
        try:
            stats_payload = json.dumps(game_stats, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            # 統計は付加情報。壊れた統計値で勝敗のdurable stageを止めない。
            log.exception("ゲーム統計のstage用JSON化に失敗しました: %s", exc)
    bonus_payload: Optional[str] = None
    if bonus_facts is not None:
        try:
            bonus_payload = json.dumps(bonus_facts, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            # ボーナスも付加要素。壊れた値で勝敗のdurable stageを止めない。
            log.exception("プレイボーナスのstage用JSON化に失敗しました: %s", exc)
    async with connect_db() as db:
        await db.execute(
            "INSERT INTO game_settlements "
            "(guild_id, room_id, game_run_id, room_name, rated, winner_team, "
            "player_records, stats_payload, bonus_payload, gm_id, base_room_id, "
            "recruitment_id, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP) "
            "ON CONFLICT(guild_id, room_id, game_run_id) DO UPDATE SET "
            "room_name = excluded.room_name, rated = excluded.rated, winner_team = excluded.winner_team, "
            "player_records = excluded.player_records, stats_payload = excluded.stats_payload, "
            "bonus_payload = excluded.bonus_payload, "
            "gm_id = excluded.gm_id, base_room_id = excluded.base_room_id, "
            "recruitment_id = excluded.recruitment_id, updated_at = CURRENT_TIMESTAMP "
            "WHERE game_settlements.status = 'pending'",
            (
                guild_id, room_id, game_run_id, room_name, int(rated), winner_team,
                payload, stats_payload, bonus_payload, gm_id,
                base_room_id or room_id, recruitment_id,
            ),
        )
        await db.commit()


async def _load_rating_results_for_game(
    db: aiosqlite.Connection,
    game_id: int,
) -> list[dict]:
    rows = await db.execute_fetchall(
        "SELECT player_id, rating_before, rating_after, elo_delta, bonus, "
        "play_bonus, recommendation_bonus "
        "FROM rating_history WHERE game_id = ? ORDER BY id",
        (game_id,),
    )
    return [
        {
            "player_id": row[0],
            "rating_before": row[1],
            "rating_after": row[2],
            "delta": row[2] - row[1],
            "elo_delta": row[3],
            "bonus": row[4],
            "play_bonus": row[5],
            "recommendation_bonus": row[6],
        }
        for row in rows
    ]


async def _write_game_stats(
    db: aiosqlite.Connection,
    game_id: int,
    stats: dict,
    *,
    rank_bucket: Optional[str],
) -> None:
    """同じ精算トランザクションへ、再実行可能な形で付加統計を書く。"""
    wolf_alive = stats.get("wolf_alive_by_day", [])
    if not isinstance(wolf_alive, list):
        raise ValueError("wolf_alive_by_day must be a list")
    await db.execute(
        "INSERT INTO game_stats "
        "(game_id, days, peaceful_mornings, guard_successes, guard_checks, seer_checks, "
        "seer_wolf_hits, day1_execution_was_wolf, executions_total, "
        "executions_wolf, night1_kill_had_role, wolf_alive_by_day, rank_bucket) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(game_id) DO UPDATE SET "
        "days = excluded.days, peaceful_mornings = excluded.peaceful_mornings, "
        "guard_successes = excluded.guard_successes, guard_checks = excluded.guard_checks, "
        "seer_checks = excluded.seer_checks, "
        "seer_wolf_hits = excluded.seer_wolf_hits, "
        "day1_execution_was_wolf = excluded.day1_execution_was_wolf, "
        "executions_total = excluded.executions_total, executions_wolf = excluded.executions_wolf, "
        "night1_kill_had_role = excluded.night1_kill_had_role, "
        "wolf_alive_by_day = excluded.wolf_alive_by_day, rank_bucket = excluded.rank_bucket",
        (
            game_id,
            int(stats["days"]),
            int(stats["peaceful_mornings"]),
            int(stats["guard_successes"]),
            int(stats.get("guard_checks", 0)),
            int(stats["seer_checks"]),
            int(stats["seer_wolf_hits"]),
            stats.get("day1_execution_was_wolf"),
            int(stats["executions_total"]),
            int(stats["executions_wolf"]),
            stats.get("night1_kill_had_role"),
            json.dumps(wolf_alive, ensure_ascii=False, separators=(",", ":")),
            rank_bucket,
        ),
    )


async def settle_game_settlement(
    guild_id: int,
    room_id: str,
    game_run_id: str,
    *,
    rank_records: Optional[dict[int, dict]] = None,
    rank_bucket: Optional[str] = None,
) -> tuple[int, Optional[list[dict]], bool]:
    """保留結果を1トランザクションで冪等精算する。

    Returns: (game_id, rating_results or None, newly_created)
    """
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        rows = await db.execute_fetchall(
            "SELECT room_name, rated, winner_team, player_records, stats_payload, "
            "bonus_payload, gm_id, "
            "base_room_id, recruitment_id, "
            "status, game_id "
            "FROM game_settlements WHERE guild_id = ? AND room_id = ? AND game_run_id = ?",
            (guild_id, room_id, game_run_id),
        )
        if not rows:
            await db.rollback()
            raise SettlementNotFound(f"settlement not found: {guild_id}/{room_id}/{game_run_id}")
        (
            room_name, rated_int, winner_team, records_text, stats_payload,
            bonus_payload, gm_id, base_room_id, recruitment_id, status, stored_game_id,
        ) = rows[0]
        if status == "settled" and stored_game_id is not None:
            results = await _load_rating_results_for_game(db, int(stored_game_id)) if rated_int else None
            await db.rollback()
            return int(stored_game_id), results, False

        existing = await db.execute_fetchall(
            "SELECT game_id FROM games WHERE guild_id = ? AND room_id = ? AND game_run_id = ?",
            (guild_id, room_id, game_run_id),
        )
        if existing:
            game_id = int(existing[0][0])
            await db.execute(
                "UPDATE game_settlements SET status = 'settled', game_id = ?, last_error = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE guild_id = ? AND room_id = ? AND game_run_id = ?",
                (game_id, guild_id, room_id, game_run_id),
            )
            await db.commit()
            results = await _load_rating_results_for_game(db, game_id) if rated_int else None
            return game_id, results, False

        player_records = json.loads(records_text)
        cursor = await db.execute(
            "INSERT INTO games (guild_id, room_id, room_name, game_run_id, gm_id, "
            "base_room_id, recruitment_id, winner_team) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id, room_id, room_name, game_run_id, gm_id,
                base_room_id or room_id, recruitment_id, winner_team,
            ),
        )
        game_id = int(cursor.lastrowid)

        # プレイボーナスの材料。的中数は game_players へも残して統計に使う
        bonus_facts: Optional[dict] = None
        if bonus_payload is not None:
            try:
                bonus_facts = json.loads(bonus_payload)
            except (TypeError, ValueError) as exc:
                log.exception(
                    "プレイボーナスのpayloadを読めませんでした (game_id=%s): %s",
                    game_id, exc,
                )
        try:
            wolf_guess_hits = rating_lib.count_wolf_guess_hits(
                player_records, bonus_facts,
            )
        except Exception as exc:
            log.exception(
                "3狼提出の的中集計に失敗しました (game_id=%s): %s", game_id, exc,
            )
            wolf_guess_hits = {}

        # 試合時表示ランクはレート計算の卓帯補正でも使うので控えておく
        rank_at_game_map: dict[int, Optional[str]] = {}
        for rec in player_records:
            player_id = int(rec["player_id"])
            rank = (rank_records or {}).get(player_id)
            rank_at_game = (
                rank.get("rank_at_game") if rank is not None
                else rec.get("rank_at_game")
            )
            rank_at_game_map[player_id] = rank_at_game
            rank_provisional = (
                rank.get("rank_provisional") if rank is not None
                else rec.get("rank_provisional")
            )
            if rank_provisional is not None:
                rank_provisional = int(bool(rank_provisional))
            await db.execute(
                "INSERT INTO game_players "
                "(game_id, player_id, role, team, won, died_on_day, death_cause, "
                "rank_at_game, rank_provisional, wolf_guess_hits) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    game_id, player_id, rec["role"], rec["team"], int(bool(rec["won"])),
                    rec.get("died_on_day"), rec.get("death_cause"),
                    rank_at_game, rank_provisional,
                    wolf_guess_hits.get(player_id),
                ),
            )

        if stats_payload is not None:
            try:
                stats = json.loads(stats_payload)
                await _write_game_stats(
                    db, game_id, stats,
                    rank_bucket=rank_bucket if rank_bucket is not None else stats.get("rank_bucket"),
                )
            except Exception as exc:
                # 付加統計の欠損は許容し、勝敗・参加履歴・レートの精算を優先する。
                log.exception("game_statsの保存に失敗しました (game_id=%s): %s", game_id, exc)

        rating_results: Optional[list[dict]] = None
        if rated_int:
            player_ids = [int(rec["player_id"]) for rec in player_records]
            placeholders = ",".join("?" * len(player_ids))
            rating_rows = await db.execute_fetchall(
                f"SELECT player_id, rating FROM player_ratings WHERE guild_id = ? "
                f"AND player_id IN ({placeholders})",
                (guild_id, *player_ids),
            )
            ratings_before = {int(row[0]): int(row[1]) for row in rating_rows}
            calc_input = [
                {
                    "player_id": int(rec["player_id"]),
                    "rating": ratings_before.get(int(rec["player_id"]), INITIAL_RATING),
                    "won": bool(rec["won"]),
                    "rank_name": rank_at_game_map.get(int(rec["player_id"])),
                }
                for rec in player_records
            ]
            rating_results = rating_lib.calculate_game_results(
                calc_input, winner_team=winner_team
            )

            # プレイボーナスは非ゼロサムの別枠。壊れた payload でレート精算を
            # 止めないよう、読めなければ加点なしで続行する (bonus_facts は
            # game_players を書く前に読んである)
            try:
                play_bonuses = rating_lib.calculate_play_bonuses(
                    player_records, bonus_facts,
                )
            except Exception as exc:
                log.exception(
                    "プレイボーナスの計算に失敗しました (game_id=%s): %s", game_id, exc,
                )
                play_bonuses = {}

            won_map = {int(rec["player_id"]): bool(rec["won"]) for rec in player_records}
            for result in rating_results:
                result["recommendation_bonus"] = 0
                pid = result["player_id"]
                # ボーナスは常に0以上なので、フロアの下限判定より後に足してよい
                play_bonus = int(play_bonuses.get(pid, 0))
                result["play_bonus"] = play_bonus
                if play_bonus:
                    result["rating_after"] += play_bonus
                    result["delta"] += play_bonus
                won_int = int(won_map.get(pid, False))
                new_rating = result["rating_after"]
                await db.execute(
                    """
                    INSERT INTO player_ratings
                        (player_id, guild_id, rating, peak_rating, games, wins, season_games, season_wins, last_updated)
                    VALUES (?, ?, ?, ?, 1, ?, 1, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(player_id, guild_id) DO UPDATE SET
                        rating = excluded.rating,
                        peak_rating = MAX(peak_rating, excluded.peak_rating),
                        games = games + 1,
                        wins = wins + excluded.wins,
                        season_games = season_games + 1,
                        season_wins = season_wins + excluded.season_wins,
                        last_updated = CURRENT_TIMESTAMP
                    """,
                    (pid, guild_id, new_rating, new_rating, won_int, won_int),
                )
                await db.execute(
                    "INSERT INTO rating_history "
                    "(player_id, guild_id, game_id, rating_before, rating_after, "
                    "elo_delta, bonus, play_bonus) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        pid, guild_id, game_id, result["rating_before"], result["rating_after"],
                        result["elo_delta"], result["bonus"], play_bonus,
                    ),
                )

        await db.execute(
            "UPDATE game_settlements SET status = 'settled', game_id = ?, last_error = NULL, "
            "updated_at = CURRENT_TIMESTAMP WHERE guild_id = ? AND room_id = ? AND game_run_id = ?",
            (game_id, guild_id, room_id, game_run_id),
        )
        await db.commit()
        return game_id, rating_results, True


async def load_pending_game_settlements(guild_id: int) -> list[dict]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT room_id, game_run_id, room_name, rated, winner_team, player_records, created_at "
            "FROM game_settlements WHERE guild_id = ? AND status = 'pending' ORDER BY created_at",
            (guild_id,),
        )
    return [
        {
            "room_id": row[0],
            "game_run_id": row[1],
            "room_name": row[2],
            "rated": bool(row[3]),
            "winner_team": row[4],
            "player_records": json.loads(row[5]),
            "created_at": row[6],
        }
        for row in rows
    ]


# ============================================================
# 終了後推薦
# ============================================================

async def create_game_recommendation_ballots(
    game_id: int,
    guild_id: int,
    voter_ids: set[int],
    *,
    timeout_seconds: int,
    kind: str = "recommend",
) -> str:
    """終了後の票をDBへ作る。再実行しても同じ投票者の行は増えない。

    kind は "recommend" (霊媒師・初日処刑者・初夜襲撃死者の推薦) と
    "postgame" (勝利陣営から敗北陣営への1票) の2種類。1人が両方を
    持つことがあるので、種別ごとに別の行になる。
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    voters = {int(voter_id) for voter_id in voter_ids}
    if not voters:
        raise ValueError("voter_ids must not be empty")
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    ).isoformat()
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        game_rows = await db.execute_fetchall(
            "SELECT guild_id FROM games WHERE game_id = ?",
            (game_id,),
        )
        if not game_rows or int(game_rows[0][0]) != guild_id:
            await db.rollback()
            raise ValueError("game does not belong to guild")
        participant_rows = await db.execute_fetchall(
            "SELECT player_id FROM game_players WHERE game_id = ?",
            (game_id,),
        )
        participants = {int(row[0]) for row in participant_rows}
        if not voters <= participants:
            await db.rollback()
            raise ValueError("every recommendation voter must be a game participant")
        for voter_id in voters:
            await db.execute(
                "INSERT INTO game_recommendations "
                "(game_id, guild_id, voter_id, kind, expires_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(game_id, voter_id, kind) DO NOTHING",
                (game_id, guild_id, voter_id, kind, expires_at),
            )
        await db.commit()
    return expires_at


async def cancel_game_recommendation_ballot(
    game_id: int,
    guild_id: int,
    voter_id: int,
    *,
    kind: str = "recommend",
) -> None:
    """投票できない票を閉じる (対象者が居ないなど)。"""
    async with connect_db() as db:
        await db.execute(
            "UPDATE game_recommendations SET status = 'unavailable' "
            "WHERE game_id = ? AND guild_id = ? AND voter_id = ? AND kind = ? "
            "AND status = 'pending'",
            (game_id, guild_id, voter_id, kind),
        )
        await db.commit()


async def confirm_game_recommendation(
    game_id: int,
    guild_id: int,
    voter_id: int,
    recipient_id: int,
    *,
    kind: str = "recommend",
) -> str:
    """投票先を確定する。戻り値は confirmed/already/expired/invalid。"""
    voter_id = int(voter_id)
    recipient_id = int(recipient_id)
    if voter_id == recipient_id:
        return "self"
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        rows = await db.execute_fetchall(
            "SELECT status, recipient_id, expires_at FROM game_recommendations "
            "WHERE game_id = ? AND guild_id = ? AND voter_id = ? AND kind = ?",
            (game_id, guild_id, voter_id, kind),
        )
        if not rows:
            await db.rollback()
            return "invalid"
        status, stored_recipient, expires_at = rows[0]
        if status in {"confirmed", "awarded"}:
            await db.rollback()
            return "already" if stored_recipient == recipient_id else "already_other"
        if status != "pending":
            await db.rollback()
            return "expired"
        try:
            deadline = datetime.fromisoformat(expires_at)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            deadline = datetime.min.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= deadline:
            await db.execute(
                "UPDATE game_recommendations SET status = 'expired' "
                "WHERE game_id = ? AND voter_id = ? AND kind = ? AND status = 'pending'",
                (game_id, voter_id, kind),
            )
            await db.commit()
            return "expired"
        participant = await db.execute_fetchall(
            "SELECT 1 FROM game_players WHERE game_id = ? AND player_id = ? LIMIT 1",
            (game_id, recipient_id),
        )
        if not participant:
            await db.rollback()
            return "invalid"
        cursor = await db.execute(
            "UPDATE game_recommendations "
            "SET recipient_id = ?, status = 'confirmed', confirmed_at = CURRENT_TIMESTAMP "
            "WHERE game_id = ? AND guild_id = ? AND voter_id = ? AND kind = ? "
            "AND status = 'pending'",
            (recipient_id, game_id, guild_id, voter_id, kind),
        )
        await db.commit()
        return "confirmed" if cursor.rowcount == 1 else "already"


async def finalize_game_recommendations(
    game_id: int,
    guild_id: int,
    *,
    close_pending: bool = True,
) -> list[dict]:
    """確定済み推薦をまとめて+1し、履歴にも内訳を残す。

    1トランザクションで status を awarded にするため、再試行しても二重加算しない。
    """
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        rows = await db.execute_fetchall(
            "SELECT voter_id, recipient_id FROM game_recommendations "
            "WHERE game_id = ? AND guild_id = ? AND status = 'confirmed' "
            "ORDER BY voter_id",
            (game_id, guild_id),
        )
        bonuses: dict[int, int] = {}
        for _voter_id, recipient_id in rows:
            if recipient_id is not None:
                pid = int(recipient_id)
                bonuses[pid] = bonuses.get(pid, 0) + 1

        results: list[dict] = []
        for player_id, bonus in bonuses.items():
            rating_rows = await db.execute_fetchall(
                "SELECT rating FROM player_ratings "
                "WHERE player_id = ? AND guild_id = ?",
                (player_id, guild_id),
            )
            history_rows = await db.execute_fetchall(
                "SELECT id FROM rating_history "
                "WHERE game_id = ? AND player_id = ? AND guild_id = ? "
                "ORDER BY id LIMIT 1",
                (game_id, player_id, guild_id),
            )
            # ランク対象外卓や壊れた参照からレートだけを増やさない。
            if not rating_rows or not history_rows:
                continue
            before = int(rating_rows[0][0])
            after = before + bonus
            await db.execute(
                "UPDATE player_ratings SET rating = ?, peak_rating = MAX(peak_rating, ?), "
                "last_updated = CURRENT_TIMESTAMP WHERE player_id = ? AND guild_id = ?",
                (after, after, player_id, guild_id),
            )
            await db.execute(
                "UPDATE rating_history SET rating_after = rating_after + ?, "
                "recommendation_bonus = recommendation_bonus + ? WHERE id = ?",
                (bonus, bonus, int(history_rows[0][0])),
            )
            results.append({
                "player_id": player_id,
                "bonus": bonus,
                "rating_before": before,
                "rating_after": after,
            })

        if rows:
            await db.execute(
                "UPDATE game_recommendations SET status = 'awarded', awarded_at = CURRENT_TIMESTAMP "
                "WHERE game_id = ? AND guild_id = ? AND status = 'confirmed'",
                (game_id, guild_id),
            )
        if close_pending:
            await db.execute(
                "UPDATE game_recommendations SET status = 'expired' "
                "WHERE game_id = ? AND guild_id = ? AND status = 'pending'",
                (game_id, guild_id),
            )
        await db.commit()
        return sorted(results, key=lambda item: (-item["bonus"], item["player_id"]))


async def has_open_game_recommendations(guild_id: int) -> bool:
    """未集計の推薦があればシーズンリセットを止めるための判定。"""
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT status, expires_at FROM game_recommendations "
            "WHERE guild_id = ? AND status IN ('pending', 'confirmed')",
            (guild_id,),
        )
    now = datetime.now(timezone.utc)
    for status, expires_at in rows:
        if status == "confirmed":
            return True
        try:
            deadline = datetime.fromisoformat(expires_at)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if deadline > now:
            return True
    return False


async def load_expired_recommendation_game_ids(guild_id: int) -> list[int]:
    """再起動で集計タスクを失った推薦試合を回収する。"""
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT DISTINCT game_id, expires_at FROM game_recommendations "
            "WHERE guild_id = ? AND status IN ('pending', 'confirmed')",
            (guild_id,),
        )
    now = datetime.now(timezone.utc)
    game_ids: list[int] = []
    for game_id, expires_at in rows:
        try:
            deadline = datetime.fromisoformat(expires_at)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            deadline = datetime.min.replace(tzinfo=timezone.utc)
        if deadline <= now:
            game_ids.append(int(game_id))
    return sorted(set(game_ids))


# ============================================================
# レーティング関連
# ============================================================

async def get_all_player_ratings(guild_id: int) -> list[dict]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT player_id, rating, peak_rating, games, wins, season_games, season_wins, last_updated "
            "FROM player_ratings WHERE guild_id = ?",
            (guild_id,),
        )
    return [
        {
            "player_id": row[0],
            "rating": row[1],
            "peak_rating": row[2],
            "games": row[3],
            "wins": row[4],
            "season_games": row[5],
            "season_wins": row[6],
            "last_updated": row[7],
        }
        for row in rows
    ]


async def get_current_rank_map(guild_id: int) -> dict[int, rating_lib.RankContext]:
    rows = await get_all_player_ratings(guild_id)
    return rating_lib.build_rank_context_map(rows)


async def get_current_season_leaderboard(guild_id: int, limit: int = 20) -> list[dict]:
    rows = await get_all_player_ratings(guild_id)
    rank_map = rating_lib.build_rank_context_map(rows)
    ordered = sorted(
        rows,
        key=lambda row: (
            rank_map[row["player_id"]].provisional,
            rank_map[row["player_id"]].position if rank_map[row["player_id"]].position is not None else 10**9,
            -row["rating"],
            row["player_id"],
        ),
    )

    result = []
    for row in ordered[:limit]:
        ctx = rank_map[row["player_id"]]
        result.append({
            **row,
            "rank_name": ctx.rank_name,
            "emoji": ctx.emoji,
            "color": ctx.color,
            "top_percent": ctx.percentile,
            "position": ctx.position,
            "active_count": ctx.active_count,
            "provisional": ctx.provisional,
            "season_winrate": round(row["season_wins"] / row["season_games"] * 100, 1)
            if row["season_games"] > 0 else 0,
        })
    return result


async def get_player_current_rank_info(player_id: int, guild_id: int) -> Optional[dict]:
    rows = await get_all_player_ratings(guild_id)
    row_map = {row["player_id"]: row for row in rows}
    row = row_map.get(player_id)
    if row is None:
        return None
    ctx = rating_lib.build_rank_context_map(rows)[player_id]
    return {
        **row,
        "rank_name": ctx.rank_name,
        "emoji": ctx.emoji,
        "color": ctx.color,
        "top_percent": ctx.percentile,
        "position": ctx.position,
        "active_count": ctx.active_count,
        "provisional": ctx.provisional,
        "season_winrate": round(row["season_wins"] / row["season_games"] * 100, 1)
        if row["season_games"] > 0 else 0,
    }


async def get_latest_season_results(guild_id: int, limit: int = 20) -> tuple[int, list[dict]]:
    async with connect_db() as db:
        reset_row = await db.execute_fetchall(
            "SELECT id FROM season_resets WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
            (guild_id,),
        )
        if not reset_row:
            return (0, [])
        reset_id = reset_row[0][0]
        rows = await db.execute_fetchall(
            "SELECT player_id, rating_before, rating_after, season_rank, rank_position, top_percent, season_games, season_wins "
            "FROM rating_snapshots WHERE season_reset_id = ? AND guild_id = ? "
            "ORDER BY CASE WHEN rank_position IS NULL THEN 1 ELSE 0 END, rank_position ASC, rating_before DESC LIMIT ?",
            (reset_id, guild_id, limit),
        )

    result = []
    for row in rows:
        rank_name = row[3] or "ブロンズ"
        result.append({
            "player_id": row[0],
            "final_rating": row[1],
            "reset_rating": row[2],
            "rank_name": rank_name,
            "emoji": rating_lib.get_rank_emoji_by_name(rank_name),
            "top_percent": row[5],
            "position": row[4],
            "season_games": row[6],
            "season_wins": row[7],
            "season_winrate": round(row[7] / row[6] * 100, 1) if row[6] > 0 else 0,
        })
    return (reset_id, result)


async def get_player_latest_season_result(player_id: int, guild_id: int) -> Optional[dict]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT season_reset_id, rating_before, rating_after, season_rank, rank_position, top_percent, season_games, season_wins "
            "FROM rating_snapshots WHERE player_id = ? AND guild_id = ? "
            "ORDER BY season_reset_id DESC LIMIT 1",
            (player_id, guild_id),
        )
    if not rows:
        return None
    row = rows[0]
    rank_name = row[3] or "ブロンズ"
    return {
        "season_reset_id": row[0],
        "final_rating": row[1],
        "reset_rating": row[2],
        "rank_name": rank_name,
        "emoji": rating_lib.get_rank_emoji_by_name(rank_name),
        "position": row[4],
        "top_percent": row[5],
        "season_games": row[6],
        "season_wins": row[7],
        "season_winrate": round(row[7] / row[6] * 100, 1) if row[6] > 0 else 0,
    }


async def season_half_reset(guild_id: int, executed_by: int,
                            note: Optional[str] = None,
                            *, expected_season_start: Optional[str] = None) -> tuple[int, int]:
    """
    シーズンハーフリセット:
        新レート = INITIAL_RATING + (現レート - INITIAL_RATING) // 2

    Returns:
        (season_reset_id, affected_players)
    """
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        reset_rows = await db.execute_fetchall(
            "SELECT MAX(reset_at) FROM season_resets WHERE guild_id = ?",
            (guild_id,),
        )
        current_start = reset_rows[0][0] if reset_rows and reset_rows[0][0] else None
        if current_start is None:
            game_rows = await db.execute_fetchall(
                "SELECT MIN(played_at) FROM games WHERE guild_id = ?",
                (guild_id,),
            )
            current_start = game_rows[0][0] if game_rows and game_rows[0][0] else None
        if expected_season_start != current_start:
            await db.rollback()
            raise SeasonResetConflict("シーズンが別の操作によって更新されています。")

        # 現在の全プレイヤーレート取得
        # games (通算) はランクの母集団判定に要る。落とすと前シーズンの
        # 最終ランクを別の母集団で計算してしまう。
        rows = await db.execute_fetchall(
            "SELECT player_id, rating, season_games, season_wins, games "
            "FROM player_ratings WHERE guild_id = ?",
            (guild_id,)
        )
        if not rows:
            await db.rollback()
            return (0, 0)
        if not any(int(row[2]) > 0 for row in rows):
            await db.rollback()
            raise SeasonResetConflict("現シーズンの対戦記録がないため、連続リセットはできません。")

        current_rows = [
            {
                "player_id": row[0],
                "rating": row[1],
                "season_games": row[2],
                "season_wins": row[3],
                "games": row[4],
            }
            for row in rows
        ]
        rank_map = rating_lib.build_rank_context_map(current_rows)

        # リセットイベント記録
        cursor = await db.execute(
            "INSERT INTO season_resets (guild_id, executed_by, affected_players, note) "
            "VALUES (?, ?, ?, ?)",
            (guild_id, executed_by, len(rows), note)
        )
        reset_id = cursor.lastrowid

        # 各プレイヤーをハーフリセット
        for player_id, old_rating, season_games, season_wins, _games in rows:
            new_rating = INITIAL_RATING + (old_rating - INITIAL_RATING) // 2
            rank_ctx = rank_map[player_id]

            # スナップショット保存
            await db.execute(
                "INSERT INTO rating_snapshots "
                "(season_reset_id, player_id, guild_id, rating_before, rating_after, "
                " season_rank, rank_position, top_percent, season_games, season_wins) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reset_id, player_id, guild_id, old_rating, new_rating,
                    rank_ctx.rank_name, rank_ctx.position, rank_ctx.percentile,
                    season_games, season_wins,
                )
            )
            # 適用 (peakは更新しない=過去の最高値は残す)
            await db.execute(
                "UPDATE player_ratings "
                "SET rating = ?, season_games = 0, season_wins = 0, last_updated = CURRENT_TIMESTAMP "
                "WHERE player_id = ? AND guild_id = ?",
                (new_rating, player_id, guild_id)
            )

        await db.commit()
        return (reset_id, len(rows))


async def get_season_start(guild_id: int) -> Optional[str]:
    """現行シーズンの開始時刻 (UTC文字列)。

    最後のシーズンリセット時刻、まだ一度もリセットしていなければ
    最初のゲームの実施時刻。ゲーム履歴も無ければ None。
    """
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT MAX(reset_at) FROM season_resets WHERE guild_id = ?",
            (guild_id,),
        )
        if rows and rows[0][0]:
            return rows[0][0]
        rows = await db.execute_fetchall(
            "SELECT MIN(played_at) FROM games WHERE guild_id = ?",
            (guild_id,),
        )
        return rows[0][0] if rows and rows[0][0] else None


async def get_meta(guild_id: int, key: str) -> Optional[str]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT value FROM bot_meta WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        )
    return rows[0][0] if rows else None


async def set_meta(guild_id: int, key: str, value: str) -> None:
    async with connect_db() as db:
        await db.execute(
            "INSERT INTO bot_meta (guild_id, key, value, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET "
            "value = excluded.value, updated_at = CURRENT_TIMESTAMP",
            (guild_id, key, value),
        )
        await db.commit()


async def add_pending_unmutes(guild_id: int, member_ids: set[int]) -> None:
    if not member_ids:
        return
    async with connect_db() as db:
        await db.executemany(
            "INSERT OR IGNORE INTO pending_unmutes (guild_id, member_id) VALUES (?, ?)",
            [(guild_id, int(member_id)) for member_id in member_ids],
        )
        await db.commit()


async def remove_pending_unmute(guild_id: int, member_id: int) -> None:
    async with connect_db() as db:
        await db.execute(
            "DELETE FROM pending_unmutes WHERE guild_id = ? AND member_id = ?",
            (guild_id, member_id),
        )
        await db.commit()


async def load_pending_unmute_ids(guild_id: int) -> set[int]:
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        legacy_rows = await db.execute_fetchall(
            "SELECT value FROM bot_meta WHERE guild_id = ? AND key = 'pending_unmutes'",
            (guild_id,),
        )
        if legacy_rows and legacy_rows[0][0]:
            legacy_ids = {
                int(value)
                for value in str(legacy_rows[0][0]).split(",")
                if value.strip().isdigit()
            }
            if legacy_ids:
                await db.executemany(
                    "INSERT OR IGNORE INTO pending_unmutes (guild_id, member_id) VALUES (?, ?)",
                    [(guild_id, member_id) for member_id in legacy_ids],
                )
            # 正規化insertと同一transactionでのみ旧値を空にする。
            await db.execute(
                "UPDATE bot_meta SET value = '', updated_at = CURRENT_TIMESTAMP "
                "WHERE guild_id = ? AND key = 'pending_unmutes'",
                (guild_id,),
            )
        rows = await db.execute_fetchall(
            "SELECT member_id FROM pending_unmutes WHERE guild_id = ?",
            (guild_id,),
        )
        await db.commit()
    return {int(row[0]) for row in rows}


async def save_room_state(guild_id: int, room_id: str, phase: str, payload: dict) -> None:
    stored_payload = dict(payload)
    stored_payload["_schema_version"] = ROOM_STATE_SCHEMA_VERSION
    async with connect_db() as db:
        await db.execute(
            "INSERT INTO room_states (guild_id, room_id, phase, payload, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(guild_id, room_id) DO UPDATE SET "
            "phase = excluded.phase, payload = excluded.payload, updated_at = CURRENT_TIMESTAMP",
            (guild_id, room_id, phase, json.dumps(stored_payload, ensure_ascii=False)),
        )
        await db.commit()


async def load_room_states(guild_id: int) -> dict[str, dict]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT room_id, phase, payload FROM room_states WHERE guild_id = ?",
            (guild_id,),
        )
        result: dict[str, dict] = {}
        quarantined = False
        for room_id, phase, payload_text in rows:
            try:
                payload = json.loads(payload_text)
                if not isinstance(payload, dict):
                    raise ValueError("payload is not an object")
                version = payload.get("_schema_version", 0)
                if version not in (0, ROOM_STATE_SCHEMA_VERSION):
                    raise ValueError(f"unsupported schema version: {version}")
                _validate_room_snapshot(phase, payload)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                await db.execute(
                    "INSERT INTO room_state_quarantine "
                    "(guild_id, room_id, phase, payload, error, quarantined_at) "
                    "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(guild_id, room_id) DO UPDATE SET "
                    "phase = excluded.phase, payload = excluded.payload, error = excluded.error, "
                    "quarantined_at = CURRENT_TIMESTAMP",
                    (guild_id, room_id, phase, payload_text, str(e)),
                )
                await db.execute(
                    "DELETE FROM room_states WHERE guild_id = ? AND room_id = ?",
                    (guild_id, room_id),
                )
                quarantined = True
                log.error("卓スナップショットを隔離しました (%s/%s): %s", guild_id, room_id, e)
                continue
            payload["phase"] = phase
            result[room_id] = payload
        if quarantined:
            await db.commit()
    return result


async def load_unresolved_room_state_quarantine_ids(guild_id: int) -> set[str]:
    """有効snapshotが無いまま隔離中の卓IDを返す。

    load_room_statesが破損行を隔離・削除した後も、呼び出し側が単なる
    「snapshot無し」と誤認してゲーム用チャンネルを掃除しないために使う。
    同じ卓へ後から有効snapshotが復旧された場合は未解決扱いにしない。
    """
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT q.room_id FROM room_state_quarantine q "
            "LEFT JOIN room_states s ON s.guild_id = q.guild_id AND s.room_id = q.room_id "
            "WHERE q.guild_id = ? AND s.room_id IS NULL",
            (guild_id,),
        )
    return {str(row[0]) for row in rows}


async def save_private_room(
    guild_id: int,
    room_id: str,
    owner_id: int,
    room_name: str,
    role_name: str,
) -> None:
    async with connect_db() as db:
        await db.execute(
            "INSERT INTO private_rooms "
            "(guild_id, room_id, owner_id, room_name, role_name, status) "
            "VALUES (?, ?, ?, ?, ?, 'creating')",
            (guild_id, room_id, owner_id, room_name, role_name),
        )
        await db.execute(
            "INSERT OR IGNORE INTO private_room_members (guild_id, room_id, member_id) VALUES (?, ?, ?)",
            (guild_id, room_id, owner_id),
        )
        await db.commit()


async def load_private_rooms(guild_id: int) -> list[dict]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT room_id, owner_id, room_name, role_name, status, category_id, role_id, last_error "
            "FROM private_rooms WHERE guild_id = ?",
            (guild_id,),
        )
    return [
        {
            "room_id": row[0],
            "owner_id": row[1],
            "room_name": row[2],
            "role_name": row[3],
            "status": row[4],
            "category_id": row[5],
            "role_id": row[6],
            "last_error": row[7],
        }
        for row in rows
    ]


async def get_private_room_by_owner(guild_id: int, owner_id: int) -> Optional[dict]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT room_id, owner_id, room_name, role_name, status, category_id, role_id, last_error "
            "FROM private_rooms WHERE guild_id = ? AND owner_id = ?",
            (guild_id, owner_id),
        )
    if not rows:
        return None
    row = rows[0]
    return {
        "room_id": row[0],
        "owner_id": row[1],
        "room_name": row[2],
        "role_name": row[3],
        "status": row[4],
        "category_id": row[5],
        "role_id": row[6],
        "last_error": row[7],
    }


async def get_private_room_by_name(guild_id: int, room_name: str) -> Optional[dict]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT room_id, owner_id, room_name, role_name, status, category_id, role_id, last_error "
            "FROM private_rooms WHERE guild_id = ? AND room_name = ?",
            (guild_id, room_name),
        )
    if not rows:
        return None
    row = rows[0]
    return {
        "room_id": row[0],
        "owner_id": row[1],
        "room_name": row[2],
        "role_name": row[3],
        "status": row[4],
        "category_id": row[5],
        "role_id": row[6],
        "last_error": row[7],
    }


async def update_private_room_names(
    guild_id: int,
    room_id: str,
    room_name: str,
    role_name: str,
) -> None:
    """改名intentをDBへ先行記録する。

    Discord側の改名完了後に ``mark_private_room_active`` で確定する。
    中断した場合は ``renaming`` 行を起動時に再実行できる。
    """
    async with connect_db() as db:
        await db.execute(
            "UPDATE private_rooms SET room_name = ?, role_name = ?, status = 'renaming', "
            "last_error = NULL "
            "WHERE guild_id = ? AND room_id = ?",
            (room_name, role_name, guild_id, room_id),
        )
        await db.commit()


async def mark_private_room_active(
    guild_id: int,
    room_id: str,
    *,
    category_id: Optional[int],
    role_id: Optional[int],
) -> None:
    async with connect_db() as db:
        await db.execute(
            "UPDATE private_rooms SET status = 'active', category_id = ?, role_id = ?, last_error = NULL "
            "WHERE guild_id = ? AND room_id = ?",
            (category_id, role_id, guild_id, room_id),
        )
        await db.commit()


async def mark_private_room_status(
    guild_id: int,
    room_id: str,
    status: str,
    *,
    error: Optional[str] = None,
) -> None:
    if status not in {"creating", "renaming", "active", "deleting", "error"}:
        raise ValueError(f"invalid private room status: {status}")
    async with connect_db() as db:
        await db.execute(
            "UPDATE private_rooms SET status = ?, last_error = ? WHERE guild_id = ? AND room_id = ?",
            (status, error, guild_id, room_id),
        )
        await db.commit()


async def delete_private_room(guild_id: int, room_id: str) -> None:
    async with connect_db() as db:
        await db.execute(
            "DELETE FROM private_room_members WHERE guild_id = ? AND room_id = ?",
            (guild_id, room_id),
        )
        await db.execute(
            "DELETE FROM private_rooms WHERE guild_id = ? AND room_id = ?",
            (guild_id, room_id),
        )
        await db.execute(
            "DELETE FROM room_states WHERE guild_id = ? AND room_id = ?",
            (guild_id, room_id),
        )
        await db.commit()


async def add_private_room_member(guild_id: int, room_id: str, member_id: int) -> None:
    """招待済みメンバーを確定状態で保存する（互換API）。"""
    async with connect_db() as db:
        await db.execute(
            "INSERT INTO private_room_members (guild_id, room_id, member_id, status, last_error) "
            "VALUES (?, ?, ?, 'active', NULL) "
            "ON CONFLICT(guild_id, room_id, member_id) DO UPDATE SET "
            "status = 'active', last_error = NULL",
            (guild_id, room_id, member_id),
        )
        await db.commit()


async def stage_private_room_member_add(
    guild_id: int, room_id: str, member_id: int
) -> None:
    """Discordロール付与より先に招待intentを永続化する。"""
    async with connect_db() as db:
        await db.execute(
            "INSERT INTO private_room_members (guild_id, room_id, member_id, status, last_error) "
            "VALUES (?, ?, ?, 'adding', NULL) "
            "ON CONFLICT(guild_id, room_id, member_id) DO UPDATE SET "
            "status = 'adding', last_error = NULL",
            (guild_id, room_id, member_id),
        )
        await db.commit()


async def stage_private_room_member_remove(
    guild_id: int, room_id: str, member_id: int
) -> None:
    """Discordロール削除より先に削除intentを永続化する。"""
    async with connect_db() as db:
        cursor = await db.execute(
            "UPDATE private_room_members SET status = 'removing', last_error = NULL "
            "WHERE guild_id = ? AND room_id = ? AND member_id = ?",
            (guild_id, room_id, member_id),
        )
        if cursor.rowcount == 0:
            # DBに無くても外部ロールだけ残っているケースを削除処理で扱えるよう記録する
            await db.execute(
                "INSERT INTO private_room_members "
                "(guild_id, room_id, member_id, status, last_error) "
                "VALUES (?, ?, ?, 'removing', NULL)",
                (guild_id, room_id, member_id),
            )
        await db.commit()


async def mark_private_room_member_error(
    guild_id: int,
    room_id: str,
    member_id: int,
    error: str,
) -> None:
    async with connect_db() as db:
        await db.execute(
            "UPDATE private_room_members SET last_error = ? "
            "WHERE guild_id = ? AND room_id = ? AND member_id = ?",
            (error[:2000], guild_id, room_id, member_id),
        )
        await db.commit()


async def remove_private_room_member(guild_id: int, room_id: str, member_id: int) -> None:
    async with connect_db() as db:
        await db.execute(
            "DELETE FROM private_room_members WHERE guild_id = ? AND room_id = ? AND member_id = ?",
            (guild_id, room_id, member_id),
        )
        await db.commit()


async def get_private_room_member_ids(guild_id: int, room_id: str) -> list[int]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT member_id FROM private_room_members "
            "WHERE guild_id = ? AND room_id = ? AND status IN ('active', 'adding')",
            (guild_id, room_id),
        )
    return [row[0] for row in rows]


async def load_private_room_members(guild_id: int, room_id: str) -> list[dict]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT member_id, status, last_error FROM private_room_members "
            "WHERE guild_id = ? AND room_id = ? ORDER BY member_id",
            (guild_id, room_id),
        )
    return [
        {"member_id": int(row[0]), "status": row[1], "last_error": row[2]}
        for row in rows
    ]


# サーバー内での通し番号 (1から連番)。game_id はAUTOINCREMENTで欠番が出るため、
# 表示にはこちらを使う。
#   - 開発中の検証で消費した採番 (本番DBへの書き込みガードは後から入れた)
#   - 精算に失敗して記録されなかった試合
# のぶんが game_id には穴として残る。廃村 (force_end) は精算しないので
# そもそも games に入らず、番号も消費しない。
_GAME_SEQ_CTE = (
    "WITH numbered AS ("
    " SELECT game_id, ROW_NUMBER() OVER (ORDER BY game_id) AS seq"
    " FROM games WHERE guild_id = ?"
    ") "
)


async def get_game_sequence_number(guild_id: int, game_id: int) -> Optional[int]:
    """その試合がサーバー内で何試合目かを返す (統計の表示番号と同じ)。

    ログチャンネルの名前に使う。game_id はAUTOINCREMENTで欠番が出るので、
    画面に出る番号と揃えるには数え直す必要がある。
    """
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM games WHERE guild_id = ? AND game_id <= ?",
            (guild_id, game_id),
        )
    return int(rows[0][0]) if rows and rows[0][0] else None


async def get_recent_games(guild_id: int, limit: int = 10) -> list[dict]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            _GAME_SEQ_CTE
            + "SELECT g.game_id, n.seq, g.room_name, g.winner_team, g.played_at "
            "FROM games g JOIN numbered n ON n.game_id = g.game_id "
            "WHERE g.guild_id = ? "
            "ORDER BY g.game_id DESC LIMIT ?",
            (guild_id, guild_id, limit),
        )
    return [
        {
            "game_id": row[0],
            "seq": row[1],
            "room_name": row[2] or "不明卓",
            "winner_team": row[3],
            "played_at": row[4],
        }
        for row in rows
    ]


async def get_player_recent_games(player_id: int, guild_id: int, limit: int = 10) -> list[dict]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            _GAME_SEQ_CTE
            + "SELECT g.game_id, n.seq, g.room_name, g.winner_team, g.played_at, "
            "gp.role, gp.team, gp.won, "
            "rh.rating_before, rh.rating_after, rh.elo_delta, rh.bonus, rh.play_bonus, "
            "rh.recommendation_bonus "
            "FROM game_players gp "
            "JOIN games g ON gp.game_id = g.game_id "
            "JOIN numbered n ON n.game_id = g.game_id "
            "LEFT JOIN rating_history rh "
            "ON rh.game_id = g.game_id AND rh.player_id = gp.player_id AND rh.guild_id = g.guild_id "
            "WHERE gp.player_id = ? AND g.guild_id = ? "
            "ORDER BY g.game_id DESC LIMIT ?",
            (guild_id, player_id, guild_id, limit),
        )
    return [
        {
            "game_id": row[0],
            "seq": row[1],
            "room_name": row[2] or "不明卓",
            "winner_team": row[3],
            "played_at": row[4],
            "role": row[5],
            "team": row[6],
            "won": bool(row[7]),
            "rating_before": row[8],
            "rating_after": row[9],
            "elo_delta": row[10],
            "bonus": row[11],
            "play_bonus": row[12],
            "recommendation_bonus": row[13],
        }
        for row in rows
    ]


def _played_at_jst_hour(value: object) -> Optional[int]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone(timedelta(hours=9))).hour


async def get_overall_game_stats(
    guild_id: int,
    *,
    room_id: Optional[str] = None,
) -> dict:
    """全試合と導入後の詳細統計を、指定卓だけに絞って返す。"""
    where = "g.guild_id = ?"
    params: list[object] = [guild_id]
    if room_id is not None:
        where += " AND g.room_id = ?"
        params.append(room_id)

    async with connect_db() as db:
        games = await db.execute_fetchall(
            "SELECT g.game_id, g.winner_team, g.played_at, g.gm_id "
            f"FROM games g WHERE {where} ORDER BY g.game_id",
            tuple(params),
        )
        detailed = await db.execute_fetchall(
            "SELECT gs.days, gs.peaceful_mornings, gs.day1_execution_was_wolf, "
            "gs.executions_total, gs.executions_wolf, gs.night1_kill_had_role, "
            "gs.wolf_alive_by_day "
            "FROM games g JOIN game_stats gs ON gs.game_id = g.game_id "
            f"WHERE {where} ORDER BY g.game_id",
            tuple(params),
        )

    wins = {Team.VILLAGE.value: 0, Team.WOLF.value: 0}
    time_counts = {"深夜 0–5時": 0, "朝 6–11時": 0, "昼 12–17時": 0, "夜 18–23時": 0}
    gm_counts: dict[int, int] = {}
    for _game_id, winner_team, played_at, gm_id in games:
        wins[str(winner_team)] = wins.get(str(winner_team), 0) + 1
        hour = _played_at_jst_hour(played_at)
        if hour is not None:
            if hour < 6:
                time_counts["深夜 0–5時"] += 1
            elif hour < 12:
                time_counts["朝 6–11時"] += 1
            elif hour < 18:
                time_counts["昼 12–17時"] += 1
            else:
                time_counts["夜 18–23時"] += 1
        if gm_id is not None:
            gid = int(gm_id)
            gm_counts[gid] = gm_counts.get(gid, 0) + 1

    days_values: list[int] = []
    peaceful_total = 0
    day1_values: list[int] = []
    executions_total = 0
    executions_wolf = 0
    night1_values: list[int] = []
    wolf_totals: list[int] = []
    wolf_counts: list[int] = []
    for (
        days, peaceful, day1_wolf, executions, wolf_executions,
        night1_role, wolf_json,
    ) in detailed:
        days_values.append(int(days))
        peaceful_total += int(peaceful)
        executions_total += int(executions)
        executions_wolf += int(wolf_executions)
        if day1_wolf is not None:
            day1_values.append(int(day1_wolf))
        if night1_role is not None:
            night1_values.append(int(night1_role))
        try:
            wolf_alive = json.loads(wolf_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            wolf_alive = []
        if not isinstance(wolf_alive, list):
            wolf_alive = []
        while len(wolf_totals) < len(wolf_alive):
            wolf_totals.append(0)
            wolf_counts.append(0)
        for index, value in enumerate(wolf_alive):
            if isinstance(value, int) and not isinstance(value, bool):
                wolf_totals[index] += value
                wolf_counts[index] += 1

    return {
        "games": len(games),
        "detailed_games": len(detailed),
        "wins": wins,
        "days": {
            "count": len(days_values),
            "total": sum(days_values),
            "average": sum(days_values) / len(days_values) if days_values else None,
        },
        "peaceful": {
            "numerator": peaceful_total,
            "denominator": sum(days_values),
            "sample_games": len(days_values),
        },
        "day1_execution": {
            "numerator": sum(day1_values),
            "denominator": len(day1_values),
        },
        "executions": {
            "numerator": executions_wolf,
            "denominator": executions_total,
            "sample_games": len(days_values),
        },
        "night1_role_kill": {
            "numerator": sum(night1_values),
            "denominator": len(night1_values),
        },
        "wolf_alive_by_day": [
            {
                "day": index + 1,
                "total": total,
                "count": wolf_counts[index],
                "average": total / wolf_counts[index] if wolf_counts[index] else None,
            }
            for index, total in enumerate(wolf_totals)
        ],
        "time_counts": time_counts,
        "gm_counts": sorted(gm_counts.items(), key=lambda item: (-item[1], item[0])),
    }


async def get_rank_player_stats(
    guild_id: int,
    *,
    rank_name: Optional[str] = None,
) -> dict:
    """試合時点の確定表示ランクで、プレイヤー単位の指標を集計する。"""
    where = (
        "g.guild_id = ? AND gp.rank_at_game IS NOT NULL "
        "AND COALESCE(gp.rank_provisional, 1) = 0"
    )
    params: list[object] = [guild_id]
    if rank_name is not None:
        where += " AND gp.rank_at_game = ?"
        params.append(rank_name)
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT gp.role, gp.won, gp.died_on_day, gs.days, "
            "gs.seer_checks, gs.seer_wolf_hits, gs.guard_successes, gs.guard_checks "
            "FROM game_players gp JOIN games g ON g.game_id = gp.game_id "
            "LEFT JOIN game_stats gs ON gs.game_id = g.game_id "
            f"WHERE {where} ORDER BY g.game_id, gp.id",
            tuple(params),
        )
        provisional_rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM game_players gp JOIN games g ON g.game_id = gp.game_id "
            "WHERE g.guild_id = ? AND gp.rank_at_game IS NOT NULL "
            "AND gp.rank_provisional = 1" + (" AND gp.rank_at_game = ?" if rank_name else ""),
            (guild_id, rank_name) if rank_name else (guild_id,),
        )

    roles: dict[str, dict[str, int]] = {}
    seer_checks = 0
    seer_hits = 0
    seer_survival: list[int] = []
    wolf_survival: list[int] = []
    guard_checks = 0
    guard_successes = 0
    for role, won, died_on_day, days, checks, hits, successes, guard_attempts in rows:
        role_stats = roles.setdefault(str(role), {"count": 0, "wins": 0})
        role_stats["count"] += 1
        role_stats["wins"] += int(bool(won))
        survived_days = (
            int(died_on_day)
            if died_on_day is not None
            else int(days) if days is not None else None
        )
        if role == Role.SEER.value:
            seer_checks += int(checks or 0)
            seer_hits += int(hits or 0)
            if survived_days is not None:
                seer_survival.append(survived_days)
        elif role == Role.WEREWOLF.value:
            if survived_days is not None:
                wolf_survival.append(survived_days)
        elif role == Role.GUARD.value:
            guard_successes += int(successes or 0)
            guard_checks += int(guard_attempts or 0)

    return {
        "players": len(rows),
        "provisional_excluded": int(provisional_rows[0][0]) if provisional_rows else 0,
        "roles": roles,
        "seer": {
            "checks": seer_checks,
            "wolf_hits": seer_hits,
            "survival_count": len(seer_survival),
            "survival_average": (
                sum(seer_survival) / len(seer_survival) if seer_survival else None
            ),
        },
        "guard": {
            "checks": guard_checks,
            "successes": guard_successes,
        },
        "wolf": {
            "survival_count": len(wolf_survival),
            "survival_average": (
                sum(wolf_survival) / len(wolf_survival) if wolf_survival else None
            ),
        },
    }


# ============================================================
# 項目別ランキング
# ============================================================
#
# 各SQLは (player_id, 分子, 分母, サンプル数) を返す。
# サンプル数は指標ごとに母数が違う (村での試合数 / 狼勝利数 / 提出回数…) ため、
# 全体の試合数ではなくこの値へ LEADERBOARD_MIN_SAMPLES を掛ける。
_LEADERBOARD_BASE = (
    "FROM game_players gp JOIN games g ON gp.game_id = g.game_id "
    "WHERE g.guild_id = ? "
)

LEADERBOARD_METRICS: dict[str, dict] = {
    "village_day1_executed": {
        "label": "村で初日に吊られた率",
        "unit": "percent",
        "note": "村陣営で参加した試合のうち、初日の処刑で死亡した割合",
        "sql": (
            "SELECT gp.player_id, "
            "SUM(CASE WHEN gp.died_on_day = 1 AND gp.death_cause = '処刑' "
            "THEN 1 ELSE 0 END), COUNT(*), COUNT(*) "
            + _LEADERBOARD_BASE
            + "AND gp.team = '村陣営' GROUP BY gp.player_id"
        ),
    },
    "wolf_survive_on_win": {
        "label": "人狼で勝った試合の生存率",
        "unit": "percent",
        "note": "人狼を引いて狼陣営が勝った試合のうち、最後まで生き残った割合",
        "sql": (
            "SELECT gp.player_id, "
            "SUM(CASE WHEN gp.died_on_day IS NULL THEN 1 ELSE 0 END), "
            "COUNT(*), COUNT(*) "
            + _LEADERBOARD_BASE
            + "AND gp.role = '人狼' AND gp.won = 1 GROUP BY gp.player_id"
        ),
    },
    "role_winrate": {
        "label": "役職別の勝率",
        "unit": "percent",
        "note": "選んだ役職を引いた試合の勝率",
        "needs_role": True,
        "sql": (
            "SELECT gp.player_id, SUM(gp.won), COUNT(*), COUNT(*) "
            + _LEADERBOARD_BASE
            + "AND gp.role = ? GROUP BY gp.player_id"
        ),
    },
    "votes_received": {
        "label": "終了後投票の獲得票 (1試合あたり)",
        "unit": "per_game",
        "note": "終了後投票で受け取った票の合計 ÷ 参加した試合数",
        "sql": (
            "SELECT gp.player_id, COALESCE(SUM(rh.recommendation_bonus), 0), "
            "COUNT(*), COUNT(*) "
            "FROM game_players gp JOIN games g ON gp.game_id = g.game_id "
            "LEFT JOIN rating_history rh ON rh.game_id = g.game_id "
            "AND rh.player_id = gp.player_id AND rh.guild_id = g.guild_id "
            "WHERE g.guild_id = ? GROUP BY gp.player_id"
        ),
    },
    "wolf_guess_accuracy": {
        "label": "3狼予想の的中率",
        "unit": "percent",
        "note": "提出した人 × 3枠のうち、実際に人狼だった割合",
        "sql": (
            "SELECT gp.player_id, COALESCE(SUM(gp.wolf_guess_hits), 0), "
            f"COUNT(*) * {BONUS_WOLF_GUESS_SLOTS}, COUNT(*) "
            + _LEADERBOARD_BASE
            + "AND gp.wolf_guess_hits IS NOT NULL GROUP BY gp.player_id"
        ),
    },
}


async def get_metric_leaderboard(
    guild_id: int,
    metric: str,
    *,
    role: Optional[str] = None,
    viewer_id: Optional[int] = None,
    limit: int = LEADERBOARD_LIMIT,
    min_samples: int = LEADERBOARD_MIN_SAMPLES,
) -> dict:
    """指標ごとの順位表と、閲覧者自身の値・順位を返す。

    閲覧者がサンプル数不足で圏外でも、本人の生データだけは返す
    (「あと何戦でランキングに載るか」が分かるようにするため)。
    """
    spec = LEADERBOARD_METRICS.get(metric)
    if spec is None:
        raise ValueError(f"unknown metric: {metric}")
    params: list = [guild_id]
    if spec.get("needs_role"):
        if not role:
            raise ValueError("role is required for this metric")
        params.append(role)

    async with connect_db() as db:
        rows = await db.execute_fetchall(spec["sql"], tuple(params))

    entries = []
    viewer_entry: Optional[dict] = None
    for player_id, numerator, denominator, samples in rows:
        numerator = int(numerator or 0)
        denominator = int(denominator or 0)
        samples = int(samples or 0)
        if denominator <= 0:
            continue
        entry = {
            "player_id": int(player_id),
            "numerator": numerator,
            "denominator": denominator,
            "samples": samples,
            "value": numerator / denominator,
        }
        if viewer_id is not None and entry["player_id"] == int(viewer_id):
            viewer_entry = entry
        if samples >= min_samples:
            entries.append(entry)

    # 値が同じなら母数の多い順 (試行回数が多いほうが確からしい) → ID順で安定させる
    entries.sort(key=lambda e: (-e["value"], -e["samples"], e["player_id"]))
    for position, entry in enumerate(entries, 1):
        entry["position"] = position

    viewer_position = next(
        (e["position"] for e in entries
         if viewer_id is not None and e["player_id"] == int(viewer_id)),
        None,
    )
    return {
        "metric": metric,
        "label": spec["label"],
        "unit": spec["unit"],
        "note": spec["note"],
        "role": role,
        "top": entries[:limit],
        "ranked_count": len(entries),
        "min_samples": min_samples,
        "viewer": viewer_entry,
        "viewer_position": viewer_position,
    }


async def get_player_stats(player_id: int, guild_id: int) -> Optional[dict]:
    async with connect_db() as db:
        row = await db.execute_fetchall(
            "SELECT COUNT(*), SUM(gp.won) "
            "FROM game_players gp "
            "JOIN games g ON gp.game_id = g.game_id "
            "WHERE gp.player_id = ? AND g.guild_id = ?",
            (player_id, guild_id)
        )
        total = row[0][0] or 0
        wins = row[0][1] or 0
        if total == 0:
            return None

        # 役職別統計
        roles = await db.execute_fetchall(
            "SELECT gp.role, COUNT(*) as cnt, SUM(gp.won) as w "
            "FROM game_players gp "
            "JOIN games g ON gp.game_id = g.game_id "
            "WHERE gp.player_id = ? AND g.guild_id = ? "
            "GROUP BY gp.role",
            (player_id, guild_id)
        )
        # 陣営別統計
        teams = await db.execute_fetchall(
            "SELECT gp.team, COUNT(*) as cnt, SUM(gp.won) as w "
            "FROM game_players gp "
            "JOIN games g ON gp.game_id = g.game_id "
            "WHERE gp.player_id = ? AND g.guild_id = ? "
            "GROUP BY gp.team",
            (player_id, guild_id)
        )
        # 最終更新
        last = await db.execute_fetchall(
            "SELECT MAX(g.played_at) FROM game_players gp "
            "JOIN games g ON gp.game_id = g.game_id "
            "WHERE gp.player_id = ? AND g.guild_id = ?",
            (player_id, guild_id)
        )

        history = await db.execute_fetchall(
            "SELECT g.game_id, g.played_at, gp.role, gp.won, gp.died_on_day, gp.death_cause, "
            "gs.days, gs.seer_checks, gs.seer_wolf_hits, gs.guard_successes "
            "FROM game_players gp JOIN games g ON gp.game_id = g.game_id "
            "LEFT JOIN game_stats gs ON gs.game_id = g.game_id "
            "WHERE gp.player_id = ? AND g.guild_id = ? "
            "ORDER BY g.played_at, g.game_id",
            (player_id, guild_id),
        )
        reset_rows = await db.execute_fetchall(
            "SELECT reset_at FROM season_resets WHERE guild_id = ? ORDER BY reset_at, id",
            (guild_id,),
        )
        recommendation_rows = await db.execute_fetchall(
            "SELECT COALESCE(SUM(recommendation_bonus), 0) FROM rating_history "
            "WHERE player_id = ? AND guild_id = ?",
            (player_id, guild_id),
        )

        max_win_streak = 0
        max_loss_streak = 0
        max_villager_streak = 0
        win_streak = 0
        loss_streak = 0
        villager_streak = 0
        seer_checks = 0
        seer_hits = 0
        guard_successes = 0
        first_night_kills = 0
        detailed_games = 0
        season_roles: dict[int, dict[str, dict[str, int]]] = {}
        reset_times = [str(row[0]) for row in reset_rows]
        for (
            _game_id, played_at, role, won, died_on_day, death_cause,
            detail_days, checks, hits, successes,
        ) in history:
            if won:
                win_streak += 1
                loss_streak = 0
            else:
                loss_streak += 1
                win_streak = 0
            max_win_streak = max(max_win_streak, win_streak)
            max_loss_streak = max(max_loss_streak, loss_streak)
            if role == Role.VILLAGER.value:
                villager_streak += 1
            else:
                villager_streak = 0
            max_villager_streak = max(max_villager_streak, villager_streak)

            season_index = sum(str(played_at) >= reset_at for reset_at in reset_times)
            season_role = season_roles.setdefault(season_index, {}).setdefault(
                str(role), {"count": 0, "wins": 0},
            )
            season_role["count"] += 1
            season_role["wins"] += int(bool(won))

            if detail_days is not None:
                detailed_games += 1
                if role == Role.SEER.value:
                    seer_checks += int(checks)
                    seer_hits += int(hits)
                elif role == Role.GUARD.value:
                    guard_successes += int(successes)
                if died_on_day == 1 and death_cause == "襲撃":
                    first_night_kills += 1

        current_season_index = len(reset_times)
        season_role_rows = [
            {
                "offset": current_season_index - season_index,
                "roles": role_data,
            }
            for season_index, role_data in sorted(season_roles.items(), reverse=True)
        ]

        return {
            "total": total,
            "wins": wins,
            "winrate": round(wins / total * 100, 1) if total > 0 else 0,
            "roles": {r[0]: {"count": r[1], "wins": r[2] or 0} for r in roles},
            "teams": {t[0]: {"count": t[1], "wins": t[2] or 0} for t in teams},
            "last_played": last[0][0] if last and last[0][0] else "N/A",
            "season_roles": season_role_rows,
            "seer_checks": seer_checks,
            "seer_wolf_hits": seer_hits,
            "guard_successes": guard_successes,
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
            "max_villager_streak": max_villager_streak,
            "first_night_kills": first_night_kills,
            "recommendations_received": int(recommendation_rows[0][0]) if recommendation_rows else 0,
            "detailed_games": detailed_games,
        }


# ============================================================
# 募集・予約・同村拒否
# ============================================================

RECRUITMENT_OPEN = "募集中"
RECRUITMENT_HELD = "開催済み"
RECRUITMENT_ARCHIVED = "アーカイブ"


def _normalize_recruitment_time(value: datetime | str) -> str:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        raise ValueError("scheduled_at must include timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _recruitment_end_text(start_text: str) -> str:
    start = datetime.fromisoformat(start_text)
    return (start + timedelta(minutes=RECRUITMENT_OCCUPANCY_MINUTES)).isoformat()


def _normalize_recruitment_allowed_ranks(
    allowed_ranks: Optional[Collection[str]],
) -> Optional[tuple[str, ...]]:
    """募集条件を設定順に正規化する。空集合は「制限なし」としてNULLにする。"""
    if allowed_ranks is None:
        return None
    if isinstance(allowed_ranks, str):
        raise ValueError("allowed_ranks must be a collection of rank names")
    selected = set(allowed_ranks)
    if not selected:
        return None
    unknown = selected.difference(RECRUITMENT_RANK_OPTIONS)
    if unknown:
        raise ValueError(f"unknown recruitment ranks: {sorted(unknown)}")
    return tuple(rank_name for rank_name in RECRUITMENT_RANK_OPTIONS if rank_name in selected)


def _decode_recruitment_allowed_ranks(value: Optional[str]) -> Optional[frozenset[str]]:
    if value is None:
        return None
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
            raise ValueError("allowed_ranks is not a string list")
        normalized = _normalize_recruitment_allowed_ranks(decoded)
        if normalized is None:
            raise ValueError("allowed_ranks must be NULL for no restriction")
        return frozenset(normalized)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        # 壊れた条件を「制限なし」に倒すと意図しない参加を許すため、空集合で閉じる。
        log.error("募集の参加ランク条件を読み込めません: %s", exc)
        return frozenset()


def _recruitment_row(row) -> dict:
    return {
        "id": int(row[0]), "guild_id": int(row[1]), "host_id": int(row[2]),
        "title": row[3], "scheduled_at": row[4], "room_id": row[5],
        "gm_id": int(row[6]) if row[6] is not None else None,
        "streaming": bool(row[7]),
        "allowed_ranks": _decode_recruitment_allowed_ranks(row[8]),
        "note": row[9] or "", "status": row[10], "notified_at": row[11],
        "message_id": int(row[12]) if row[12] is not None else None,
        "created_at": row[13],
        "participant_count": int(row[14] or 0) if len(row) > 14 else 0,
        "backup_count": int(row[15] or 0) if len(row) > 15 else 0,
    }


_RECRUITMENT_SELECT = (
    "SELECT r.id, r.guild_id, r.host_id, r.title, r.scheduled_at, r.room_id, "
    "r.gm_id, r.streaming, r.allowed_ranks, r.note, r.status, "
    "r.notified_at, r.message_id, r.created_at, "
    "SUM(CASE WHEN e.kind = '参加' THEN 1 ELSE 0 END), "
    "SUM(CASE WHEN e.kind = '補欠' THEN 1 ELSE 0 END) "
    "FROM recruitments r LEFT JOIN recruitment_entries e ON e.recruitment_id = r.id "
)


async def create_recruitment(
    guild_id: int, host_id: int, *, title: str,
    scheduled_at: datetime | str, room_id: str, streaming: bool,
    allowed_ranks: Optional[Collection[str]], note: str = "",
) -> int:
    """卓予約と主催者上限を同じBEGIN IMMEDIATE内で確定して作成する。"""
    start_text = _normalize_recruitment_time(scheduled_at)
    end_text = _recruitment_end_text(start_text)
    normalized_ranks = _normalize_recruitment_allowed_ranks(allowed_ranks)
    if room_id != "open" and normalized_ranks is not None:
        raise ValueError("参加ランク条件を設定できるのは総合卓だけです")
    allowed_ranks_json = (
        json.dumps(normalized_ranks, ensure_ascii=False)
        if normalized_ranks is not None else None
    )
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        host_rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM recruitments WHERE guild_id = ? AND host_id = ? AND status = ?",
            (guild_id, host_id, RECRUITMENT_OPEN),
        )
        if int(host_rows[0][0]) >= RECRUITMENT_MAX_PER_HOST:
            await db.rollback()
            raise RecruitmentConflict(f"開催前の募集は1人{RECRUITMENT_MAX_PER_HOST}件までです。")
        overlaps = await db.execute_fetchall(
            "SELECT id FROM recruitments WHERE guild_id = ? AND room_id = ? "
            "AND status IN (?, ?) "
            "AND datetime(scheduled_at) < datetime(?) "
            "AND datetime(scheduled_at, '+' || ? || ' minutes') > datetime(?) LIMIT 1",
            (guild_id, room_id, RECRUITMENT_OPEN, RECRUITMENT_HELD, end_text,
             RECRUITMENT_OCCUPANCY_MINUTES, start_text),
        )
        if overlaps:
            await db.rollback()
            raise RecruitmentConflict("同じ卓の占有時間と重なる募集があります。")
        cursor = await db.execute(
            "INSERT INTO recruitments "
            "(guild_id, host_id, title, scheduled_at, room_id, streaming, allowed_ranks, "
            "note, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, host_id, title.strip(), start_text, room_id, int(streaming),
             allowed_ranks_json, note.strip(), RECRUITMENT_OPEN),
        )
        result = int(cursor.lastrowid)
        await db.commit()
    return result


async def get_recruitment(recruitment_id: int) -> Optional[dict]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            _RECRUITMENT_SELECT + "WHERE r.id = ? GROUP BY r.id", (recruitment_id,)
        )
    return _recruitment_row(rows[0]) if rows else None


async def list_open_recruitments(guild_id: int) -> list[dict]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            _RECRUITMENT_SELECT
            + "WHERE r.guild_id = ? AND r.status = ? GROUP BY r.id ORDER BY r.scheduled_at, r.id",
            (guild_id, RECRUITMENT_OPEN),
        )
    return [_recruitment_row(row) for row in rows]


async def set_recruitment_message_id(recruitment_id: int, message_id: int) -> None:
    async with connect_db() as db:
        await db.execute("UPDATE recruitments SET message_id = ? WHERE id = ?", (message_id, recruitment_id))
        await db.commit()


async def list_recruitment_entries(recruitment_id: int) -> list[dict]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT user_id, kind, joined_at FROM recruitment_entries WHERE recruitment_id = ? "
            "ORDER BY CASE kind WHEN '参加' THEN 0 ELSE 1 END, joined_at, user_id",
            (recruitment_id,),
        )
    return [{"user_id": int(r[0]), "kind": r[1], "joined_at": r[2]} for r in rows]


async def add_recruitment_entry(recruitment_id: int, user_id: int) -> str:
    """時間重複・既存参加者の拒否・定員を原子的に判定して登録する。"""
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        rows = await db.execute_fetchall(
            "SELECT guild_id, scheduled_at, status FROM recruitments WHERE id = ?",
            (recruitment_id,),
        )
        if not rows or rows[0][2] != RECRUITMENT_OPEN:
            await db.rollback()
            raise RecruitmentConflict("この募集は終了しています。")
        guild_id, start_text, _ = rows[0]
        duplicate = await db.execute_fetchall(
            "SELECT 1 FROM recruitment_entries WHERE recruitment_id = ? AND user_id = ?",
            (recruitment_id, user_id),
        )
        if duplicate:
            await db.rollback()
            raise RecruitmentConflict("既にこの募集へ登録しています。")
        end_text = _recruitment_end_text(start_text)
        overlap = await db.execute_fetchall(
            "SELECT r.id FROM recruitments r JOIN recruitment_entries e ON e.recruitment_id = r.id "
            "WHERE r.guild_id = ? AND r.status IN (?, ?) "
            "AND e.user_id = ? AND r.id <> ? "
            "AND datetime(r.scheduled_at) < datetime(?) "
            "AND datetime(r.scheduled_at, '+' || ? || ' minutes') > datetime(?) LIMIT 1",
            (guild_id, RECRUITMENT_OPEN, RECRUITMENT_HELD,
             user_id, recruitment_id, end_text,
             RECRUITMENT_OCCUPANCY_MINUTES, start_text),
        )
        if overlap:
            await db.rollback()
            raise RecruitmentConflict("同じ時間帯の別の募集へ既に登録しています。")
        # 新規参加者自身ではなく、先に入っている人の拒否だけを見る。
        blocked = await db.execute_fetchall(
            "SELECT 1 FROM recruitment_entries e JOIN player_blocks b "
            "ON b.guild_id = ? AND b.blocker_id = e.user_id AND b.blocked_id = ? "
            "WHERE e.recruitment_id = ? LIMIT 1",
            (guild_id, user_id, recruitment_id),
        )
        if blocked:
            await db.rollback()
            raise RecruitmentConflict("この募集には参加できません。")
        counts = await db.execute_fetchall(
            "SELECT SUM(CASE WHEN kind='参加' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN kind='補欠' THEN 1 ELSE 0 END) "
            "FROM recruitment_entries WHERE recruitment_id = ?", (recruitment_id,)
        )
        participants, backups = int(counts[0][0] or 0), int(counts[0][1] or 0)
        if participants < RECRUITMENT_CAPACITY:
            kind = "参加"
        elif backups < RECRUITMENT_BACKUP_CAPACITY:
            kind = "補欠"
        else:
            await db.rollback()
            raise RecruitmentConflict("参加者と補欠が上限に達しています。")
        await db.execute(
            "INSERT INTO recruitment_entries (recruitment_id, user_id, kind) VALUES (?, ?, ?)",
            (recruitment_id, user_id, kind),
        )
        await db.commit()
    return kind


async def remove_recruitment_entry(recruitment_id: int, user_id: int) -> tuple[str, Optional[int]]:
    """登録を取り消し、参加枠なら最古の補欠を再チェックなしで繰り上げる。"""
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        rows = await db.execute_fetchall(
            "SELECT e.kind, r.status FROM recruitment_entries e "
            "JOIN recruitments r ON r.id=e.recruitment_id "
            "WHERE e.recruitment_id=? AND e.user_id=?", (recruitment_id, user_id)
        )
        if not rows:
            await db.rollback()
            raise RecruitmentConflict("この募集には登録されていません。")
        if rows[0][1] != RECRUITMENT_OPEN:
            await db.rollback()
            raise RecruitmentConflict("この募集は終了しています。")
        removed_kind = rows[0][0]
        await db.execute(
            "DELETE FROM recruitment_entries WHERE recruitment_id=? AND user_id=?",
            (recruitment_id, user_id),
        )
        promoted: Optional[int] = None
        if removed_kind == "参加":
            backup = await db.execute_fetchall(
                "SELECT user_id FROM recruitment_entries WHERE recruitment_id=? AND kind='補欠' "
                "ORDER BY joined_at, user_id LIMIT 1", (recruitment_id,)
            )
            if backup:
                promoted = int(backup[0][0])
                await db.execute(
                    "UPDATE recruitment_entries SET kind='参加' WHERE recruitment_id=? AND user_id=?",
                    (recruitment_id, promoted),
                )
        await db.commit()
    return removed_kind, promoted


async def set_recruitment_gm(
    recruitment_id: int,
    gm_id: Optional[int],
    *,
    expected_gm_id=_EXPECTED_GM_UNSET,
) -> None:
    async with connect_db() as db:
        if expected_gm_id is _EXPECTED_GM_UNSET:
            cursor = await db.execute(
                "UPDATE recruitments SET gm_id=? WHERE id=? AND status=?",
                (gm_id, recruitment_id, RECRUITMENT_OPEN),
            )
        elif expected_gm_id is None:
            cursor = await db.execute(
                "UPDATE recruitments SET gm_id=? "
                "WHERE id=? AND status=? AND gm_id IS NULL",
                (gm_id, recruitment_id, RECRUITMENT_OPEN),
            )
        else:
            cursor = await db.execute(
                "UPDATE recruitments SET gm_id=? "
                "WHERE id=? AND status=? AND gm_id=?",
                (gm_id, recruitment_id, RECRUITMENT_OPEN, int(expected_gm_id)),
            )
        if cursor.rowcount != 1:
            await db.rollback()
            raise RecruitmentConflict("GM状態が先に変更されました。募集カードを更新して確認してください。")
        await db.commit()


async def update_recruitment_note(recruitment_id: int, host_id: int, note: str) -> None:
    async with connect_db() as db:
        cursor = await db.execute(
            "UPDATE recruitments SET note=? WHERE id=? AND host_id=? AND status=?",
            (note.strip(), recruitment_id, host_id, RECRUITMENT_OPEN),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            raise RecruitmentConflict("主催中の募集だけ備考を変更できます。")
        await db.commit()


async def set_recruitment_status(
    recruitment_id: int, status: str, *, expected_status: str = RECRUITMENT_OPEN,
) -> bool:
    if status not in {RECRUITMENT_OPEN, RECRUITMENT_HELD, RECRUITMENT_ARCHIVED}:
        raise ValueError("unknown recruitment status")
    async with connect_db() as db:
        cursor = await db.execute(
            "UPDATE recruitments SET status=?, "
            "closed_at=CASE WHEN ?=? THEN NULL ELSE COALESCE(closed_at, CURRENT_TIMESTAMP) END "
            "WHERE id=? AND status=?",
            (status, status, RECRUITMENT_OPEN, recruitment_id, expected_status),
        )
        await db.commit()
    return cursor.rowcount == 1


async def archive_host_recruitments(guild_id: int, host_id: int) -> list[int]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT id FROM recruitments WHERE guild_id=? AND host_id=? AND status=?",
            (guild_id, host_id, RECRUITMENT_OPEN),
        )
        ids = [int(r[0]) for r in rows]
        await db.execute(
            "UPDATE recruitments SET status=?, closed_at=COALESCE(closed_at, CURRENT_TIMESTAMP) "
            "WHERE guild_id=? AND host_id=? AND status=?",
            (RECRUITMENT_ARCHIVED, guild_id, host_id, RECRUITMENT_OPEN),
        )
        await db.commit()
    return ids


async def list_due_recruitment_notifications(guild_id: int, now: datetime) -> list[dict]:
    now_text = _normalize_recruitment_time(now)
    window_text = _normalize_recruitment_time(
        now + timedelta(minutes=RECRUITMENT_NOTIFICATION_WINDOW_MINUTES)
    )
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            _RECRUITMENT_SELECT
            + "WHERE r.guild_id=? AND r.status=? AND r.notified_at IS NULL "
            "AND datetime(r.scheduled_at)>=datetime(?) AND datetime(r.scheduled_at)<=datetime(?) "
            "GROUP BY r.id ORDER BY r.scheduled_at, r.id",
            (guild_id, RECRUITMENT_OPEN, now_text, window_text),
        )
    return [_recruitment_row(r) for r in rows]


async def mark_recruitment_notified(recruitment_id: int, now: datetime) -> None:
    async with connect_db() as db:
        await db.execute(
            "UPDATE recruitments SET notified_at=COALESCE(notified_at, ?) WHERE id=?",
            (_normalize_recruitment_time(now), recruitment_id),
        )
        await db.commit()


async def recruitment_ready_notification_needed(recruitment_id: int) -> bool:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT r.gm_id, r.ready_notified_at, "
            "SUM(CASE WHEN e.kind='参加' THEN 1 ELSE 0 END) "
            "FROM recruitments r LEFT JOIN recruitment_entries e ON e.recruitment_id=r.id "
            "WHERE r.id=? AND r.status=? GROUP BY r.id",
            (recruitment_id, RECRUITMENT_OPEN),
        )
    return bool(
        rows and rows[0][0] is not None and rows[0][1] is None
        and int(rows[0][2] or 0) >= RECRUITMENT_CAPACITY
    )


async def mark_recruitment_ready_notified(recruitment_id: int, now: datetime) -> None:
    async with connect_db() as db:
        await db.execute(
            "UPDATE recruitments SET ready_notified_at=COALESCE(ready_notified_at, ?) WHERE id=?",
            (_normalize_recruitment_time(now), recruitment_id),
        )
        await db.commit()


async def archive_expired_recruitments(guild_id: int, now: datetime) -> list[int]:
    cutoff = _normalize_recruitment_time(now - timedelta(minutes=RECRUITMENT_OCCUPANCY_MINUTES))
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        rows = await db.execute_fetchall(
            "SELECT id FROM recruitments WHERE guild_id=? AND status=? "
            "AND datetime(scheduled_at)<datetime(?)", (guild_id, RECRUITMENT_OPEN, cutoff)
        )
        ids = [int(r[0]) for r in rows]
        await db.execute(
            "UPDATE recruitments SET status=?, closed_at=COALESCE(closed_at, ?) "
            "WHERE guild_id=? AND status=? "
            "AND datetime(scheduled_at)<datetime(?)",
            (RECRUITMENT_ARCHIVED, _normalize_recruitment_time(now), guild_id, RECRUITMENT_OPEN, cutoff),
        )
        await db.commit()
    return ids


async def list_recruitment_messages_for_cleanup(guild_id: int, now: datetime) -> list[dict]:
    cutoff = _normalize_recruitment_time(
        now - timedelta(days=RECRUITMENT_ARCHIVE_RETENTION_DAYS)
    )
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT id, message_id FROM recruitments WHERE guild_id=? AND status<>? "
            "AND message_id IS NOT NULL AND closed_at IS NOT NULL "
            "AND datetime(closed_at)<=datetime(?)",
            (guild_id, RECRUITMENT_OPEN, cutoff),
        )
    return [{"id": int(row[0]), "message_id": int(row[1])} for row in rows]


async def clear_recruitment_message_id(recruitment_id: int) -> None:
    async with connect_db() as db:
        await db.execute("UPDATE recruitments SET message_id=NULL WHERE id=?", (recruitment_id,))
        await db.commit()


async def add_player_block(guild_id: int, blocker_id: int, blocked_id: int) -> int:
    if blocker_id == blocked_id:
        raise ValueError("自分自身は拒否できません。")
    async with connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        existing = await db.execute_fetchall(
            "SELECT 1 FROM player_blocks WHERE guild_id=? AND blocker_id=? AND blocked_id=?",
            (guild_id, blocker_id, blocked_id),
        )
        if existing:
            await db.rollback()
            raise RecruitmentConflict("既に拒否リストへ登録されています。")
        count_rows = await db.execute_fetchall(
            "SELECT COUNT(*) FROM player_blocks WHERE guild_id=? AND blocker_id=?",
            (guild_id, blocker_id),
        )
        count = int(count_rows[0][0])
        if count >= PLAYER_BLOCK_LIMIT:
            await db.rollback()
            raise PlayerBlockLimitReached(f"拒否リストは{PLAYER_BLOCK_LIMIT}人までです。")
        await db.execute(
            "INSERT INTO player_blocks (guild_id, blocker_id, blocked_id) VALUES (?, ?, ?)",
            (guild_id, blocker_id, blocked_id),
        )
        await db.commit()
    return count + 1


async def remove_player_block(guild_id: int, blocker_id: int, blocked_id: int) -> bool:
    async with connect_db() as db:
        cursor = await db.execute(
            "DELETE FROM player_blocks WHERE guild_id=? AND blocker_id=? AND blocked_id=?",
            (guild_id, blocker_id, blocked_id),
        )
        await db.commit()
    return cursor.rowcount == 1


async def list_player_blocks(guild_id: int, blocker_id: int) -> list[int]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT blocked_id FROM player_blocks WHERE guild_id=? AND blocker_id=? "
            "ORDER BY created_at, blocked_id", (guild_id, blocker_id)
        )
    return [int(r[0]) for r in rows]


async def get_blocked_counts(guild_id: int) -> list[dict]:
    async with connect_db() as db:
        rows = await db.execute_fetchall(
            "SELECT blocked_id, COUNT(*) FROM player_blocks WHERE guild_id=? "
            "GROUP BY blocked_id ORDER BY COUNT(*) DESC, blocked_id", (guild_id,)
        )
    return [{"blocked_id": int(r[0]), "count": int(r[1])} for r in rows]
