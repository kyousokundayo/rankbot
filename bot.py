"""人狼Bot エントリーポイント"""
import os
import sys
import logging
import socket
import ssl
import asyncio
import atexit
import fcntl
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

import aiohttp
import certifi
import discord
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from room_config import LocalRoomConfigError

try:
    from config import SE_ENABLED
except LocalRoomConfigError as exc:
    print(f"❌ ローカル卓設定が不正なため起動を中止します: {exc}", file=sys.stderr)
    sys.exit(1)
import sounds

sys.stdout.reconfigure(line_buffering=True)

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)


def _configure_ssl_cert_file() -> None:
    candidates = [
        os.environ.get("SSL_CERT_FILE"),
        "/opt/homebrew/etc/openssl@3/cert.pem",
        certifi.where(),
        # Intel Homebrewの残骸はApple Silicon版とcertifiの後だけ参照する。
        "/usr/local/etc/openssl@3/cert.pem",
        "/usr/local/etc/openssl@1.1/cert.pem",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            os.environ["SSL_CERT_FILE"] = path
            return


_configure_ssl_cert_file()


def _build_http_connector() -> aiohttp.TCPConnector:
    cafile = os.environ.get("SSL_CERT_FILE")
    ssl_context = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    return aiohttp.TCPConnector(limit=0, family=socket.AF_INET, ssl=ssl_context)

# ローテーション付きファイルログ (5MB x 3世代)。
# 端末から起動したときだけコンソールにも出す。
# (.app経由ではstdoutがlogs/launcher.logへ向くため、二重書き込みを避ける)
_log_handlers: list[logging.Handler] = [
    RotatingFileHandler(
        LOG_DIR / "bot.log",
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    ),
]
if sys.stdout.isatty():
    _log_handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_log_handlers,
)
# discordライブラリの冗長ログを抑制。
# ただしレート制限 (429) の警告は運用判断に必要なので discord.http だけ WARNING で拾う
logging.getLogger("discord").setLevel(logging.ERROR)
logging.getLogger("discord.http").setLevel(logging.WARNING)
log = logging.getLogger("werewolf")

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print(
        "❌ DISCORD_TOKEN が設定されていません。\n"
        "   .env ファイルに DISCORD_TOKEN=xxxx を記載してください。",
        file=sys.stderr,
    )
    sys.exit(1)


RUNTIME_DIR = Path(
    os.getenv(
        "WEREWOLF_BOT_RUNTIME_DIR",
        str(Path(tempfile.gettempdir()) / f"werewolf-bot-{os.getuid()}"),
    )
).expanduser().absolute()
LOCK_FILE = Path(os.getenv("WEREWOLF_BOT_LOCK_FILE", str(RUNTIME_DIR / "bot.lock")))
PID_FILE = Path(os.getenv("WEREWOLF_BOT_PID_FILE", str(RUNTIME_DIR / "bot.pid")))
READY_FILE = Path(os.getenv("WEREWOLF_BOT_READY_FILE", str(RUNTIME_DIR / "bot.ready")))
try:
    TARGET_GUILD_ID = int(os.environ["DISCORD_GUILD_ID"]) if os.getenv("DISCORD_GUILD_ID") else None
except ValueError:
    print("❌ DISCORD_GUILD_ID は整数で指定してください。", file=sys.stderr)
    sys.exit(1)
_instance_lock_file = None


def _write_marker_atomic(path: Path, value: str) -> None:
    if path.is_symlink():
        raise RuntimeError(f"markerがシンボリックリンクです: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(value, encoding="utf-8")
    os.replace(tmp_path, path)


def _remove_own_marker(path: Path) -> None:
    try:
        if path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink(missing_ok=True)
    except (FileNotFoundError, OSError):
        pass


def _acquire_instance_lock() -> None:
    """Bot本体で単一プロセスを保証し、ランチャー用markerを作る。"""
    global _instance_lock_file
    if RUNTIME_DIR.is_symlink():
        raise RuntimeError(f"実行時ディレクトリがシンボリックリンクです: {RUNTIME_DIR}")
    RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if RUNTIME_DIR.is_symlink() or not RUNTIME_DIR.is_dir():
        raise RuntimeError(f"安全な実行時ディレクトリを作成できません: {RUNTIME_DIR}")
    if RUNTIME_DIR.stat().st_uid != os.getuid():
        raise RuntimeError(f"実行時ディレクトリの所有者が異なります: {RUNTIME_DIR}")
    os.chmod(RUNTIME_DIR, 0o700)
    for marker in (LOCK_FILE, PID_FILE, READY_FILE):
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if marker.is_symlink():
            raise RuntimeError(f"runtime markerがシンボリックリンクです: {marker}")
    lock_file = LOCK_FILE.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        raise RuntimeError(f"別の人狼Botプロセスが実行中です: {LOCK_FILE}")
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    _instance_lock_file = lock_file
    READY_FILE.unlink(missing_ok=True)
    _write_marker_atomic(PID_FILE, str(os.getpid()))


def _cleanup_process_markers() -> None:
    global _instance_lock_file
    _remove_own_marker(PID_FILE)
    _remove_own_marker(READY_FILE)
    if _instance_lock_file is not None:
        try:
            fcntl.flock(_instance_lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        _instance_lock_file.close()
        _instance_lock_file = None


atexit.register(_cleanup_process_markers)

class WerewolfBot(commands.Bot):
    async def _async_setup_hook(self) -> None:
        await super()._async_setup_hook()
        self.http.connector = _build_http_connector()


intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = WerewolfBot(command_prefix="!", intents=intents)
_ready_done = False
_ready_lock = asyncio.Lock()


@bot.event
async def on_ready():
    global _ready_done
    async with _ready_lock:
        if _ready_done:
            log.info("再接続検出 - セットアップスキップ")
            return

        if TARGET_GUILD_ID is None:
            if len(bot.guilds) != 1:
                log.critical(
                    "単一サーバー専用Botですが参加サーバーが%d件です。"
                    "DISCORD_GUILD_IDを設定してください。",
                    len(bot.guilds),
                )
                await bot.close()
                return
            guild = bot.guilds[0]
        else:
            guild = bot.get_guild(TARGET_GUILD_ID)
            if guild is None:
                log.critical("管理対象サーバーが見つかりません: %s", TARGET_GUILD_ID)
                await bot.close()
                return

        # GameCog側の全イベントでも同じguild境界を検査する
        bot.managed_guild_id = guild.id
        log.info(f"Bot起動完了: {bot.user} (ID: {bot.user.id}) / 管理対象: {guild.name}")

        try:
            foreign_guilds = [item for item in bot.guilds if item.id != guild.id]
            for foreign in foreign_guilds:
                log.warning(
                    "起動時に管理対象外サーバーを検出したため退出します: %s (%s)",
                    foreign.name,
                    foreign.id,
                )
                await foreign.leave()

            # DB初期化
            from database import (
                backup_db,
                init_db,
                migrate_rating_scale_to_1500,
                rating_scale_migration_needed,
            )

            # init_db自身がスキーマmigrationを含む。既存DBは変更前の復旧点を
            # 必ず作り、バックアップ不能ならmigrationへ入らない。
            try:
                pre_migration_backup = await backup_db(label="pre_migration")
                if pre_migration_backup:
                    log.info(f"DB変更前バックアップ作成: {pre_migration_backup}")
            except Exception as e:
                raise RuntimeError(
                    "DB変更前のバックアップを作成できないため起動を中止します"
                ) from e
            await init_db()
            log.info("データベース初期化完了")

            # 起動時バックアップ。レート基準移行前の復旧点も兼ねる。
            backup_path = None
            try:
                backup_path = await backup_db(label="startup")
                if backup_path:
                    log.info(f"DBバックアップ作成: {backup_path}")
            except Exception as e:
                log.warning(f"起動時DBバックアップ失敗: {e}")

            if await rating_scale_migration_needed():
                if backup_path is None:
                    raise RuntimeError(
                        "レート基準移行前のDBバックアップを作成できないため起動を中止します"
                    )
                migrated = await migrate_rating_scale_to_1500()
                log.info(
                    "レート基準を1200から1500へ移行しました: %d人 (+300)",
                    migrated,
                )

            try:
                await bot.load_extension("game")
                log.info("GameCog読み込み完了")
            except commands.ExtensionAlreadyLoaded:
                pass

            cog = bot.get_cog("GameCog")
            if cog is None:
                raise RuntimeError("GameCogを取得できません")
            await cog.setup_channels(guild)
            log.info(f"チャンネルセットアップ完了: {guild.name}")

            # 対象guildへコピー後、グローバル公開を消して単一guildだけへ同期する
            bot.tree.copy_global_to(guild=guild)
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            synced = await bot.tree.sync(guild=guild)
            log.info(f"スラッシュコマンド同期完了 ({guild.name}): {len(synced)}件")
        except Exception as e:
            log.exception(f"Bot初期化失敗: {e}")
            await bot.close()
            return

        try:
            _write_marker_atomic(READY_FILE, str(os.getpid()))
        except Exception as e:
            log.exception("準備完了markerを書き込めません: %s", e)
            await bot.close()
            return
        _ready_done = True
        log.info("人狼Bot 準備完了！")


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    """稼働中に管理対象外guildへ追加されても状態を作らず即退出する。"""
    managed_id = getattr(bot, "managed_guild_id", TARGET_GUILD_ID)
    if managed_id is not None and guild.id == managed_id:
        return
    log.critical("管理対象外サーバーへ招待されたため退出します: %s (%s)", guild.name, guild.id)
    try:
        await guild.leave()
    except discord.HTTPException as e:
        log.exception("管理対象外サーバーから退出できません: %s", e)


@bot.event
async def on_guild_remove(guild: discord.Guild) -> None:
    """管理対象guildを失ったプロセスをreadyのまま残さない。"""
    if guild.id != getattr(bot, "managed_guild_id", TARGET_GUILD_ID):
        return
    log.critical("管理対象サーバーからBotが削除されたため停止します: %s", guild.id)
    _remove_own_marker(READY_FILE)
    await bot.close()


if __name__ == "__main__":
    try:
        _acquire_instance_lock()
    except Exception as e:
        log.error(str(e))
        sys.exit(2)
    try:
        if SE_ENABLED:
            # discord.py 2.7系はdaveyをimport時に固定判定する。実接続まで
            # 欠落を持ち越さないよう、Discordへ接続する前にネイティブ依存も検査する。
            sounds.require_voice_ready()
        else:
            log.info("SEはconfig.pyで無効化されています")
        bot.run(TOKEN, log_handler=None)
    except sounds.VoiceDependencyError as e:
        log.critical("SE依存の起動前検証に失敗: %s", e)
        sys.exit(3)
    finally:
        _cleanup_process_markers()
