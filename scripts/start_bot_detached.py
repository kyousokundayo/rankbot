"""Botを完全デタッチ起動し、Discord初期化完了まで確認する。"""
from __future__ import annotations

import os
import math
import shlex
import signal
import stat
import subprocess
import tempfile
import time
import fcntl
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv


BOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BOT_DIR / ".env")
BOT_FILE = BOT_DIR / "bot.py"
PYTHON_FILE = BOT_DIR / ".venv" / "bin" / "python"
LAUNCHER = BOT_DIR / "scripts" / "run_bot.sh"
LOG_DIR = BOT_DIR / "logs"
LAUNCHER_LOG = LOG_DIR / "launcher.log"
LAUNCHER_LOG_MAX_BYTES = 1_000_000
MAX_OPERATION_TIMEOUT_SECONDS = 3600.0


def _parse_positive_finite_timeout(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise RuntimeError(f"{name} は秒数を数値で指定してください: {raw!r}") from exc
    if (
        not math.isfinite(value)
        or value <= 0
        or value > MAX_OPERATION_TIMEOUT_SECONDS
    ):
        shown = raw if raw is not None else str(default)
        raise RuntimeError(
            f"{name} は0より大きく{MAX_OPERATION_TIMEOUT_SECONDS:.0f}以下の"
            f"秒数で指定してください: {shown!r}"
        )
    return value


READY_TIMEOUT = _parse_positive_finite_timeout("WEREWOLF_BOT_READY_TIMEOUT", 90.0)
STOP_TIMEOUT = _parse_positive_finite_timeout("WEREWOLF_BOT_STOP_TIMEOUT", 5.0)


class ProcessInspectionError(RuntimeError):
    """OSのプロセス照合自体を安全に実行できなかった。"""


def _runtime_dir() -> Path:
    configured = os.getenv("WEREWOLF_BOT_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser().absolute()
    return Path(tempfile.gettempdir()) / f"werewolf-bot-{os.getuid()}"


def _bot_lock_path(runtime_dir: Path) -> Path:
    configured = os.getenv("WEREWOLF_BOT_LOCK_FILE")
    if configured:
        return Path(configured).expanduser().absolute()
    return runtime_dir / "bot.lock"


def _prepare_runtime_dir(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"実行時ディレクトリがシンボリックリンクです: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"安全な実行時ディレクトリを作成できません: {path}")
    if path.stat().st_uid != os.getuid():
        raise RuntimeError(f"実行時ディレクトリの所有者が異なります: {path}")
    path.chmod(0o700)


def _read_pid(path: Path) -> int | None:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        raw = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    if not raw.isascii() or not raw.isdecimal():
        return None
    pid = int(raw)
    return pid if pid > 1 else None


def _process_matches_bot(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProcessInspectionError(f"psでPID {pid}を確認できません") from exc
    if result.returncode != 0 or not result.stdout.strip():
        return False
    raw_command = result.stdout.strip()
    try:
        argv = shlex.split(raw_command)
    except ValueError:
        argv = []
    # macOSのpsはargvを再引用せず表示するため、配置パスに空白があると
    # shlexで元のargvへ戻せない。通常の厳密解析ができない場合だけ、
    # cwdがこのcheckoutそのもの・commandが正確なbot.pyを含むことを併用する。
    def path_spellings(path: Path) -> set[str]:
        """macOSの/private/varと/var表記を同一パス候補として返す。"""
        values = {str(path.absolute()), str(path.resolve())}
        aliases: set[str] = set()
        for value in values:
            if value.startswith("/private/var/"):
                aliases.add(value[len("/private"):])
            elif value.startswith("/var/"):
                aliases.add("/private" + value)
        return values | aliases

    def matches_by_cwd() -> bool:
        if "python" not in raw_command.lower():
            return False
        cwd = _process_cwd(pid)
        if cwd is None or cwd.resolve() != BOT_DIR.resolve():
            return False
        expected_paths = path_spellings(BOT_FILE)
        return any(
            raw_command.endswith(expected)
            or f" {expected} " in raw_command
            for expected in expected_paths
        )

    if not argv or "python" not in Path(argv[0]).name.lower():
        return matches_by_cwd()

    script_arg: str | None = None
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in ("-c", "-m"):
            return False
        if arg in ("-W", "-X"):
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        script_arg = arg
        break
    if script_arg is None:
        return False

    script_path = Path(script_arg).expanduser()
    if not script_path.is_absolute():
        cwd = _process_cwd(pid)
        if cwd is None:
            return False
        script_path = cwd / script_path
    return script_path.resolve() == BOT_FILE or matches_by_cwd()


def _process_cwd(pid: int) -> Path | None:
    """macOSのlsofから作業ディレクトリを得る。"""
    for executable in ("/usr/sbin/lsof", "/usr/bin/lsof"):
        if not Path(executable).exists():
            continue
        try:
            result = subprocess.run(
                [executable, "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("n") and len(line) > 1:
                    return Path(line[1:])
    return None


def _existing_bot_pid(pid_file: Path) -> int | None:
    pid = _read_pid(pid_file)
    return pid if pid is not None and _process_matches_bot(pid) else None


def _marker_matches(path: Path, pid: int) -> bool:
    return _read_pid(path) == pid


def _candidate_bot_pids(pid_file: Path) -> list[int]:
    """markerと厳密なコマンドライン照合の両方を通ったPIDだけ返す。"""
    candidates: set[int] = set()
    for marker in (pid_file, Path("/tmp/werewolf_bot.pid")):
        pid = _read_pid(marker)
        if pid is not None:
            candidates.add(pid)
    try:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-f", str(BOT_FILE)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode in (0, 1):
            for raw in result.stdout.splitlines():
                if raw.isascii() and raw.isdecimal() and int(raw) > 1:
                    candidates.add(int(raw))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProcessInspectionError("pgrepでBotプロセスを確認できません") from exc
    if result.returncode not in (0, 1):
        raise ProcessInspectionError(
            f"pgrepでBotプロセスを確認できません (exit={result.returncode})"
        )
    return sorted(pid for pid in candidates if _process_matches_bot(pid))


def _stop_failed_launch(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _prepare_private_log_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"ログディレクトリがシンボリックリンクです: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def _open_private_launcher_log(path: Path):
    if path.is_symlink():
        raise RuntimeError(f"ランチャーログがシンボリックリンクです: {path}")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError(f"安全なランチャーログではありません: {path}")
        os.fchmod(file_descriptor, 0o600)
        return os.fdopen(file_descriptor, "ab", buffering=0)
    except Exception:
        os.close(file_descriptor)
        raise


def _rotate_launcher_log() -> None:
    """初期化前クラッシュの証跡を1世代残しつつ、ログの無限増大を防ぐ。"""
    if LAUNCHER_LOG.is_symlink():
        raise RuntimeError(f"ランチャーログがシンボリックリンクです: {LAUNCHER_LOG}")
    try:
        if LAUNCHER_LOG.stat().st_size < LAUNCHER_LOG_MAX_BYTES:
            return
    except FileNotFoundError:
        return
    rotated = LAUNCHER_LOG.with_suffix(f"{LAUNCHER_LOG.suffix}.1")
    if rotated.is_symlink():
        raise RuntimeError(f"ローテーション先がシンボリックリンクです: {rotated}")
    rotated.unlink(missing_ok=True)
    LAUNCHER_LOG.replace(rotated)
    rotated.chmod(0o600)


@contextmanager
def _launcher_operation_lock(runtime_dir: Path):
    """起動と停止のmarker更新を複数ランチャー間で直列化する。"""
    path = runtime_dir / "launcher.lock"
    if path.is_symlink():
        raise RuntimeError(f"ランチャーロックがシンボリックリンクです: {path}")
    handle = path.open("a+", encoding="utf-8")
    try:
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            raise RuntimeError(f"安全なランチャーロックではありません: {path}")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _start_locked(runtime_dir: Path) -> int:
    pid_file = runtime_dir / "bot.pid"
    ready_file = runtime_dir / "bot.ready"
    lock_file = _bot_lock_path(runtime_dir)

    if _candidate_bot_pids(pid_file):
        print("already_running")
        return 0

    # 古いmarkerは本体ロック取得前に除去し、新しいreadyと誤認しない。
    pid_file.unlink(missing_ok=True)
    ready_file.unlink(missing_ok=True)

    _prepare_private_log_directory(LOG_DIR)
    _harden_existing_private_log(LAUNCHER_LOG)
    _harden_existing_private_log(
        LAUNCHER_LOG.with_suffix(f"{LAUNCHER_LOG.suffix}.1")
    )
    _rotate_launcher_log()
    child_env = os.environ.copy()
    child_env.update(
        {
            "WEREWOLF_BOT_PID_FILE": str(pid_file),
            "WEREWOLF_BOT_READY_FILE": str(ready_file),
            "WEREWOLF_BOT_LOCK_FILE": str(lock_file),
        }
    )
    # 起動失敗の履歴を失わないよう追記する。Bot本体のログは別途ローテーションされる。
    with _open_private_launcher_log(LAUNCHER_LOG) as log:
        process = subprocess.Popen(
            [str(LAUNCHER)],
            cwd=str(BOT_DIR),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if _marker_matches(ready_file, process.pid) and _process_matches_bot(process.pid):
            print("started")
            return 0
        return_code = process.poll()
        if return_code is not None:
            # 競合した別ランチャーがロックを取得した場合は正常な既存起動とする。
            if _existing_bot_pid(pid_file) is not None:
                print("already_running")
                return 0
            raise RuntimeError(f"Botが準備完了前に終了しました (exit={return_code})")
        time.sleep(0.2)

    _stop_failed_launch(process)
    raise RuntimeError(f"Botの準備完了を{READY_TIMEOUT:.0f}秒以内に確認できませんでした")


def start() -> int:
    if not PYTHON_FILE.is_file() or not os.access(PYTHON_FILE, os.X_OK):
        raise RuntimeError(".venvがありません。./scripts/setup_venv.sh を先に実行してください")

    runtime_dir = _runtime_dir()
    _prepare_runtime_dir(runtime_dir)
    with _launcher_operation_lock(runtime_dir):
        return _start_locked(runtime_dir)


def status() -> int:
    """Botが厳密なコマンドライン照合を通して稼働中か返す。書き込みは行わない。"""
    pid_file = _runtime_dir() / "bot.pid"
    try:
        if _candidate_bot_pids(pid_file):
            print("running")
            return 0
    except ProcessInspectionError:
        print("unknown")
        return 2
    print("not_running")
    return 1


def _stop_locked(runtime_dir: Path) -> int:
    pid_file = runtime_dir / "bot.pid"
    ready_file = runtime_dir / "bot.ready"
    pids = _candidate_bot_pids(pid_file)
    if not pids:
        # 無関係なPIDは決し停止せず、古いmarkerだけ除去する。
        pid_file.unlink(missing_ok=True)
        ready_file.unlink(missing_ok=True)
        print("not_running")
        return 0

    for pid in pids:
        if _process_matches_bot(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        remaining = [pid for pid in pids if _process_matches_bot(pid)]
        if not remaining:
            pid_file.unlink(missing_ok=True)
            ready_file.unlink(missing_ok=True)
            print("stopped")
            return 0
        time.sleep(0.2)

    forced = False
    for pid in pids:
        if _process_matches_bot(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                forced = True
            except ProcessLookupError:
                pass
    force_deadline = time.monotonic() + 2.0
    while time.monotonic() < force_deadline:
        if not any(_process_matches_bot(pid) for pid in pids):
            break
        time.sleep(0.1)
    if any(_process_matches_bot(pid) for pid in pids):
        print("failed")
        return 1

    pid_file.unlink(missing_ok=True)
    ready_file.unlink(missing_ok=True)
    print("stopped_force" if forced else "stopped")
    return 0


def stop() -> int:
    runtime_dir = _runtime_dir()
    _prepare_runtime_dir(runtime_dir)
    with _launcher_operation_lock(runtime_dir):
        return _stop_locked(runtime_dir)


if __name__ == "__main__":
    try:
        if len(os.sys.argv) == 2 and os.sys.argv[1] == "--stop":
            raise SystemExit(stop())
        if len(os.sys.argv) == 2 and os.sys.argv[1] == "--status":
            raise SystemExit(status())
        if len(os.sys.argv) != 1:
            raise RuntimeError("未定義のオプションです")
        raise SystemExit(start())
    except Exception as exc:
        if "--stop" in os.sys.argv:
            operation = "stop"
        elif "--status" in os.sys.argv:
            operation = "status"
        else:
            operation = "start"
        print(f"{operation}_failed: {exc}", flush=True)
        raise SystemExit(2 if operation == "status" else 1)
