"""人狼Bot エントリーポイント"""
import os
import sys
import logging
import socket
import ssl
import asyncio
import atexit
import fcntl
import stat
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
from command_sync import sync_application_commands

sys.stdout.reconfigure(line_buffering=True)

# テスト実行時は tests/test_00_db_path_guard.py が WEREWOLF_LOG_DIR を
# 一時ディレクトリへ差し替える。bot.py の import だけでログハンドラが
# 張られてしまうため、本番ログ (logs/bot.log) を汚さないための退避先。
LOG_DIR = Path(os.environ["WEREWOLF_LOG_DIR"]) if os.environ.get("WEREWOLF_LOG_DIR") else BASE_DIR / "logs"


def _prepare_private_log_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"ログディレクトリがシンボリックリンクです: {path}")
    path.mkdir(mode=0o700, exist_ok=True, parents=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"安全なログディレクトリではありません: {path}")
    path.chmod(0o700)


def _harden_existing_private_log(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise RuntimeError(f"安全な既存ログファイルではありません: {path}")
    path.chmod(0o600)


def _harden_existing_bot_logs() -> None:
    base = LOG_DIR / "bot.log"
    for path in (base, *(Path(f"{base}.{index}") for index in range(1, 4))):
        _harden_existing_private_log(path)


def _open_private_log_stream(
    path: Path,
    mode: str,
    encoding: str | None,
    errors: str | None,
):
    if path.is_symlink():
        raise RuntimeError(f"ログファイルがシンボリックリンクです: {path}")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError(f"安全なログファイルではありません: {path}")
        os.fchmod(file_descriptor, 0o600)
        return os.fdopen(
            file_descriptor,
            mode,
            encoding=encoding,
            errors=errors,
        )
    except Exception:
        os.close(file_descriptor)
        raise


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """新規作成時とローテーション後も0600を維持する。"""

    def _open(self):
        return _open_private_log_stream(
            Path(self.baseFilename), self.mode, self.encoding, self.errors
        )


_prepare_private_log_directory(LOG_DIR)
_harden_existing_bot_logs()


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
    _PrivateRotatingFileHandler(
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


async def _notify_startup_failure(
    guild: discord.Guild,
    error: BaseException,
    *,
    verified_operations_channel: discord.TextChannel | None = None,
) -> None:
    """起動失敗を #運営 へ残す。

    失敗するとBotはオフラインになるだけで、原因はローカルのログにしか残らない。
    Gatewayへは繋がっている段階なので、閉じる前に運営が読める場所へ書いておく。
    通知自体の失敗で元の例外を隠さないよう、ここでは絶対に投げない。
    """
    try:
        from config import CH_OPERATIONS, OPERATIONS_CATEGORY_NAME

        category = discord.utils.get(guild.categories, name=OPERATIONS_CATEGORY_NAME)
        channel = verified_operations_channel
        if (
            channel is None
            or channel not in guild.text_channels
            or category is None
            or channel.category != category
            or channel.name != CH_OPERATIONS
        ):
            # 名前だけで既存チャンネルを拾うと、募集層が既存チャンネルと
            # Bot必須権限を確認する前に例外本文を送る恐れがある。
            log.warning(
                "確認済みの既存#%sが無いため起動失敗を通知できません",
                CH_OPERATIONS,
            )
            return
        embed = discord.Embed(
            title="🚨 Botの起動に失敗しました",
            description=(
                "セットアップ途中で停止したため、Botはオフラインになります。\n"
                "サーバー側でBotを再起動してください。直らない場合は"
                "`logs/bot.log` の詳細を確認してください。"
            ),
            color=discord.Color.red(),
        )
        embed.add_field(
            name="エラー",
            value=f"```{type(error).__name__}: {error}```"[:1024],
            inline=False,
        )
        await channel.send(embed=embed)
        log.info("起動失敗を #%s へ通知しました", CH_OPERATIONS)
    except Exception as notify_error:
        log.warning("起動失敗の通知に失敗しました: %s", notify_error)


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
                get_meta,
                init_db,
                set_meta,
            )

            await init_db()
            log.info("データベース初期化完了")

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

            command_sync = await sync_application_commands(
                bot.tree, guild, get_meta=get_meta, set_meta=set_meta,
            )
            log.info(
                "スラッシュコマンド確認完了 (%s): %d件 / guild=%s / global=%s",
                guild.name,
                command_sync.command_count,
                command_sync.guild_action,
                command_sync.global_action,
            )
        except Exception as e:
            log.exception(f"Bot初期化失敗: {e}")
            failed_cog = bot.get_cog("GameCog")
            recruitment_manager = getattr(failed_cog, "recruitment_manager", None)
            verified_operations_channel = getattr(
                recruitment_manager, "operations_channel", None
            )
            await _notify_startup_failure(
                guild,
                e,
                verified_operations_channel=verified_operations_channel,
            )
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
