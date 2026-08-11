"""Discordアプリコマンドを、差分がある時だけ同期する。"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

import discord


log = logging.getLogger(__name__)

SCHEMA_FORMAT = 1
GUILD_SYNC_META_KEY = "app_command_guild_sync_v1"
GLOBAL_AUDIT_META_KEY = "app_command_global_audit_v1"
GLOBAL_AUDIT_INTERVAL = timedelta(days=7)

GetMeta = Callable[[int, str], Awaitable[Optional[str]]]
SetMeta = Callable[[int, str, str], Awaitable[None]]


@dataclass(frozen=True)
class CommandSyncResult:
    command_count: int
    guild_action: str
    global_action: str


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, discord.Permissions):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _hash_payload(payload: list[dict[str, Any]], *, kind: str) -> str:
    body = {
        "format": SCHEMA_FORMAT,
        "kind": kind,
        "discord_py": discord.__version__,
        "commands": sorted(
            (_json_value(command) for command in payload),
            key=lambda command: (int(command.get("type", 0)), str(command.get("name", ""))),
        ),
    }
    encoded = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _desired_schema(tree, guild) -> tuple[list[Any], str]:
    commands = list(tree.get_commands(guild=guild))
    translator = getattr(tree, "translator", None)
    if translator is None:
        payload = [command.to_dict(tree) for command in commands]
    else:
        payload = [
            await command.get_translated_payload(tree, translator)
            for command in commands
        ]
    return commands, _hash_payload(payload, kind="desired")


def _remote_schema(commands: list[Any]) -> str:
    payload: list[dict[str, Any]] = []
    for command in commands:
        item = dict(command.to_dict())
        for key in ("id", "application_id", "guild_id", "version"):
            item.pop(key, None)
        permissions = getattr(command, "default_member_permissions", None)
        item["default_member_permissions"] = (
            None if permissions is None else permissions.value
        )
        item["dm_permission"] = getattr(command, "dm_permission", None)
        item["nsfw"] = bool(getattr(command, "nsfw", False))
        payload.append(item)
    return _hash_payload(payload, kind="remote")


def _load_state(raw: Optional[str], *required: str) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("format") != SCHEMA_FORMAT:
        return None
    if any(not isinstance(state.get(key), str) or not state[key] for key in required):
        return None
    return state


def _global_audit_due(state: Optional[dict[str, Any]], now: datetime) -> bool:
    if state is None:
        return True
    try:
        verified = datetime.fromisoformat(state["verified_at"])
    except (KeyError, TypeError, ValueError):
        return True
    if verified.tzinfo is None:
        return True
    age = now - verified.astimezone(timezone.utc)
    return age < timedelta(0) or age >= GLOBAL_AUDIT_INTERVAL


async def _read_meta(get_meta: GetMeta, guild_id: int, key: str) -> Optional[str]:
    try:
        return await get_meta(guild_id, key)
    except Exception as exc:
        # キャッシュ不調で起動を止めず、従来どおり同期する安全側へ倒す。
        log.warning("コマンド同期キャッシュ読込失敗 (%s): %s", key, exc)
        return None


async def _write_meta(
    set_meta: SetMeta, guild_id: int, key: str, value: dict[str, Any],
) -> None:
    try:
        await set_meta(
            guild_id,
            key,
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    except Exception as exc:
        # 同期自体は成功済み。次回に再同期されるだけなのでBotは継続する。
        log.warning("コマンド同期キャッシュ保存失敗 (%s): %s", key, exc)


async def sync_application_commands(
    tree,
    guild,
    *,
    get_meta: GetMeta,
    set_meta: SetMeta,
    now: Optional[datetime] = None,
) -> CommandSyncResult:
    """単一guild用コマンドを同期し、不要なglobalコマンドを低頻度で監査する。"""
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current_time = current_time.astimezone(timezone.utc)

    # どちらもローカル操作。guildへ複製した後、globalの望ましい状態を空にする。
    tree.copy_global_to(guild=guild)
    commands, desired_hash = await _desired_schema(tree, guild)
    tree.clear_commands(guild=None)
    application_id = getattr(getattr(tree, "client", None), "application_id", None)
    if application_id is None:
        raise ValueError("Discord application IDを取得できません")
    application_id_text = str(application_id)

    guild_raw = await _read_meta(get_meta, guild.id, GUILD_SYNC_META_KEY)
    guild_state = _load_state(
        guild_raw, "application_id", "desired_sha256", "remote_sha256",
    )
    if guild_state is not None and guild_state["application_id"] != application_id_text:
        guild_state = None
    global_raw = await _read_meta(get_meta, 0, GLOBAL_AUDIT_META_KEY)
    global_state = _load_state(global_raw, "application_id", "verified_at")
    if global_state is not None and global_state["application_id"] != application_id_text:
        global_state = None

    global_action = "skipped"
    if global_state is None:
        # 初回・破損時はGETを足さず、従来と同じ空syncで確実に掃除する。
        await tree.sync()
        global_action = "cleared"
        await _write_meta(
            set_meta, 0, GLOBAL_AUDIT_META_KEY,
            {
                "format": SCHEMA_FORMAT,
                "application_id": application_id_text,
                "verified_at": current_time.isoformat(),
            },
        )
    elif _global_audit_due(global_state, current_time):
        remote_global = await tree.fetch_commands()
        if remote_global:
            await tree.sync()
            global_action = "cleared"
        else:
            global_action = "verified"
        await _write_meta(
            set_meta, 0, GLOBAL_AUDIT_META_KEY,
            {
                "format": SCHEMA_FORMAT,
                "application_id": application_id_text,
                "verified_at": current_time.isoformat(),
            },
        )

    guild_action = "synced"
    if guild_state is not None and guild_state["desired_sha256"] == desired_hash:
        remote = await tree.fetch_commands(guild=guild)
        if _remote_schema(remote) == guild_state["remote_sha256"]:
            guild_action = "verified"
            command_count = len(remote)
        else:
            synced = await tree.sync(guild=guild)
            command_count = len(synced)
            remote_hash = _remote_schema(synced)
            await _write_meta(
                set_meta, guild.id, GUILD_SYNC_META_KEY,
                {
                    "format": SCHEMA_FORMAT,
                    "application_id": application_id_text,
                    "desired_sha256": desired_hash,
                    "remote_sha256": remote_hash,
                },
            )
    else:
        synced = await tree.sync(guild=guild)
        command_count = len(synced)
        remote_hash = _remote_schema(synced)
        await _write_meta(
            set_meta, guild.id, GUILD_SYNC_META_KEY,
            {
                "format": SCHEMA_FORMAT,
                "application_id": application_id_text,
                "desired_sha256": desired_hash,
                "remote_sha256": remote_hash,
            },
        )

    return CommandSyncResult(
        command_count=command_count,
        guild_action=guild_action,
        global_action=global_action,
    )
