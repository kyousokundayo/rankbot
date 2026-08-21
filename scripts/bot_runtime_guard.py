"""実Discordや本番DBを使う補助処理とBot本体の同時実行を防ぐ。"""
from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def bot_lock_path() -> Path:
    """Bot本体と同じ排他ロックの絶対パスを返す。"""
    runtime_dir = Path(
        os.getenv(
            "WEREWOLF_BOT_RUNTIME_DIR",
            str(Path(tempfile.gettempdir()) / f"werewolf-bot-{os.getuid()}"),
        )
    ).expanduser().absolute()
    return Path(
        os.getenv("WEREWOLF_BOT_LOCK_FILE", str(runtime_dir / "bot.lock"))
    ).expanduser().absolute()


@contextmanager
def bot_stopped_guard() -> Iterator[None]:
    """Bot本体が停止中であることを確認し、終了まで同じロックを保持する。"""
    lock_path = bot_lock_path()
    parent = lock_path.parent
    if parent.is_symlink() or lock_path.is_symlink():
        raise RuntimeError(f"安全なBotロックではありません: {lock_path}")
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_info = parent.stat()
    except OSError as exc:
        raise RuntimeError(f"Bot停止状態を確認できません: {lock_path}") from exc
    if (
        parent.is_symlink()
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.getuid()
        or parent_info.st_mode & 0o022
    ):
        raise RuntimeError(f"安全なBotロック用ディレクトリではありません: {parent}")

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        file_descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError(f"Bot停止状態を確認できません: {lock_path}") from exc

    acquired = False
    try:
        lock_info = os.fstat(file_descriptor)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid():
            raise RuntimeError(f"安全なBotロックではありません: {lock_path}")
        os.fchmod(file_descriptor, 0o600)
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "人狼Botが稼働中です。Botを停止してから実行してください。"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"Bot停止状態を確認できません: {lock_path}") from exc
        acquired = True
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(file_descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(file_descriptor)
