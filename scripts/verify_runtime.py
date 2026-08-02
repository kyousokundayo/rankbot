"""Python実行環境がホストのネイティブCPUで動作しているか検証する。"""
from __future__ import annotations

import platform
import subprocess
import sys
import ctypes


def _sysctl_int(name: bytes) -> int | None:
    """macOSの整数sysctlを取得し、未提供・取得不能ならNoneを返す。"""
    try:
        libc = ctypes.CDLL(None)
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        result = libc.sysctlbyname(
            name, ctypes.byref(value), ctypes.byref(size), None, 0
        )
    except (AttributeError, OSError):
        return None
    return value.value if result == 0 else None


def is_apple_silicon() -> bool:
    """Rosetta配下でもApple Silicon実機を判定する。"""
    if sys.platform != "darwin":
        return False
    if platform.machine() == "arm64":
        return True
    if _sysctl_int(b"sysctl.proc_translated") == 1:
        return True
    try:
        result = subprocess.run(
            ["/usr/bin/arch", "-arm64", "/usr/bin/true"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def verify_runtime() -> None:
    """Apple Siliconではarm64 Python以外を拒否する。"""
    machine = platform.machine()
    apple_silicon = is_apple_silicon()
    if apple_silicon and machine != "arm64":
        raise RuntimeError(
            "Apple Siliconではarm64 Pythonが必要です "
            f"(現在: {machine} / executable: {sys.executable})"
        )
    print(
        f"runtime_arch: {machine} / "
        f"apple_silicon: {'yes' if apple_silicon else 'no'} / "
        f"base: {sys.base_prefix}"
    )


if __name__ == "__main__":
    try:
        verify_runtime()
    except RuntimeError as exc:
        print(f"runtime_arch_error: {exc}", file=sys.stderr)
        raise SystemExit(1)
