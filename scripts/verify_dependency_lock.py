#!/usr/bin/env python3
"""requirements-lock.txt の全pinが実環境と一致することを確認する。"""
from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOCK_FILE = BOT_DIR / "requirements-lock.txt"
DEFAULT_REQUIREMENTS_FILE = BOT_DIR / "requirements.txt"
_PIN_PATTERN = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[([A-Za-z0-9._,-]+)\])?"
    r"==([^\s;#]+)(?:\s+#.*)?$"
)


class LockFormatError(RuntimeError):
    """完全固定lockとして解釈できない行がある。"""


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_exact_pins(
    path: Path,
    *,
    allow_extras: bool,
) -> dict[str, tuple[str, str]]:
    pins: dict[str, tuple[str, str]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LockFormatError(f"依存lockを読めません: {path}") from exc

    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN_PATTERN.fullmatch(line)
        if match is None:
            raise LockFormatError(
                f"{path}:{line_number}: 完全固定の name==version ではありません"
            )
        raw_name, extras, version = match.groups()
        if extras and not allow_extras:
            raise LockFormatError(
                f"{path}:{line_number}: lock側のパッケージ名にextrasは指定できません"
            )
        display_name = f"{raw_name}[{extras}]" if extras else raw_name
        normalized = _normalize_name(raw_name)
        if normalized in pins:
            raise LockFormatError(
                f"{path}:{line_number}: 依存が重複しています: {display_name}"
            )
        pins[normalized] = (display_name, version)

    if not pins:
        raise LockFormatError(f"依存pinがありません: {path}")
    return pins


def load_locked_versions(path: Path) -> dict[str, tuple[str, str]]:
    """正規化名 -> (lock上の名前, version) を返す。"""
    return _load_exact_pins(path, allow_extras=False)


def load_direct_versions(path: Path) -> dict[str, tuple[str, str]]:
    """requirements.txtのextras付き直接pinを正規化して返す。"""
    return _load_exact_pins(path, allow_extras=True)


def get_installed_versions() -> dict[str, str]:
    installed: dict[str, str] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            installed[_normalize_name(name)] = distribution.version
    return installed


def find_mismatches(
    locked: dict[str, tuple[str, str]],
    installed: dict[str, str],
) -> list[str]:
    mismatches: list[str] = []
    for normalized, (raw_name, expected) in sorted(locked.items()):
        actual = installed.get(normalized)
        if actual is None:
            mismatches.append(f"{raw_name}: 未導入 (lock={expected})")
        elif actual != expected:
            mismatches.append(f"{raw_name}: installed={actual} / lock={expected}")
    return mismatches


def find_direct_lock_mismatches(
    direct: dict[str, tuple[str, str]],
    locked: dict[str, tuple[str, str]],
) -> list[str]:
    mismatches: list[str] = []
    for normalized, (direct_name, expected) in sorted(direct.items()):
        lock_pin = locked.get(normalized)
        if lock_pin is None:
            mismatches.append(f"{direct_name}: requirements={expected} / lock=未登録")
            continue
        _lock_name, actual = lock_pin
        if actual != expected:
            mismatches.append(
                f"{direct_name}: requirements={expected} / lock={actual}"
            )
    return mismatches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="完全固定依存lockとの一致を確認する")
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK_FILE)
    parser.add_argument(
        "--requirements", type=Path, default=DEFAULT_REQUIREMENTS_FILE
    )
    args = parser.parse_args(argv)

    try:
        locked = load_locked_versions(args.lock)
        direct = load_direct_versions(args.requirements)
    except LockFormatError as exc:
        print(f"dependency_lock_error: {exc}", file=sys.stderr)
        return 2

    direct_mismatches = find_direct_lock_mismatches(direct, locked)
    mismatches = find_mismatches(locked, get_installed_versions())
    if direct_mismatches:
        print("dependency_direct_lock_mismatch:", file=sys.stderr)
        for mismatch in direct_mismatches:
            print(f"  {mismatch}", file=sys.stderr)
    if mismatches:
        print("dependency_lock_mismatch:", file=sys.stderr)
        for mismatch in mismatches:
            print(f"  {mismatch}", file=sys.stderr)
    if direct_mismatches or mismatches:
        return 1

    print(
        f"dependency_lock_ok: {len(locked)} packages / "
        f"{len(direct)} direct requirements"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
