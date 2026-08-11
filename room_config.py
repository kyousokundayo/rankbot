"""固定のローカル卓を安全に環境設定から読み込む。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Mapping

from dotenv import dotenv_values


_ROOM_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_MAX_LOCAL_ROOMS = 20
_MAX_DISCORD_ID = (1 << 63) - 1
_LOCAL_ROOM_KEYS = frozenset(
    {
        "room_id",
        "name",
        "allowed_gm_user_ids",
        "access_role_names",
        "rated",
        "recruitment_enabled",
        "sync_permissions",
    }
)
_LOCAL_ROOMS_ENV_KEY = "WEREWOLF_LOCAL_ROOMS_JSON"
_LOCAL_ROOMS_REQUIRED_KEY = "WEREWOLF_LOCAL_ROOMS_REQUIRED"


class LocalRoomConfigError(ValueError):
    """ローカル卓設定が不正で、安全に適用できない。"""


@dataclass(frozen=True)
class RoomDefinition:
    room_id: str
    name: str
    allowed_ranks: frozenset[str] | None = None
    owner_only_gm: bool = False
    # GM取得を許可するユーザーID (Noneなら制限なし)
    allowed_gm_user_ids: frozenset[int] | None = None
    private_owner_id: int | None = None
    # v0.39以前のGM名前村閲覧ロールとのDB互換用。v0.40以降は常にNone。
    private_role_name: str | None = None
    # 指定ロール・サーバー管理者だけにカテゴリ全体を表示する固定卓。
    access_role_names: frozenset[str] | None = None
    # 既存の位置引数を壊さないよう末尾へ追加。省略時は従来どおり13人
    # クロストークとし、ローカル卓・専用村も暗黙に別ルールへ変えない。
    variant_id: str = "v13_cross"
    # False の固定卓はRunnerを作らず、カテゴリ・VC・募集導線も起動しない。
    # 定義自体は統計・履歴・シミュレーションのため残す。
    enabled: bool = True
    # 通常の閲覧許可とは別に、指定ロールだけへ厳格に公開する固定卓用。
    # 実際の権限適用は permissions.py が担う。
    strict_access_role_names: frozenset[str] | None = None
    # Falseならカテゴリ・参加受付・VCの平常時overwriteをDiscordの手動設定へ
    # 委ねる。#昼/#霊界とゲーム中VCの一時制御は秘密保持のため引き続きBot管理。
    sync_permissions: bool = True


@dataclass(frozen=True)
class LocalRoomRegistration:
    room: RoomDefinition


def load_local_room_json(
    dotenv_path: Path,
    environ: Mapping[str, str],
) -> str | None:
    """プロセス環境を優先し、プロジェクト直下の.envから設定を解決する。

    ローカル卓を必須化した運用環境では、JSON行の欠落・dotenv構文エラーを
    「卓なし」と誤認せず起動前に止める。
    """
    try:
        text = dotenv_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeError) as exc:
        raise LocalRoomConfigError(f".envを安全に読み取れません: {exc}") from exc

    file_values = dotenv_values(dotenv_path) if text else {}
    for key in (_LOCAL_ROOMS_ENV_KEY, _LOCAL_ROOMS_REQUIRED_KEY):
        declared = re.search(rf"(?m)^\s*{re.escape(key)}\s*=", text) is not None
        if declared and file_values.get(key) is None:
            raise LocalRoomConfigError(f".envの{key}を解析できません")

    raw = environ.get(_LOCAL_ROOMS_ENV_KEY, file_values.get(_LOCAL_ROOMS_ENV_KEY))
    required_raw = environ.get(
        _LOCAL_ROOMS_REQUIRED_KEY,
        file_values.get(_LOCAL_ROOMS_REQUIRED_KEY),
    )
    normalized_required = str(required_raw or "").strip().lower()
    if normalized_required in ("", "0", "false", "no", "off"):
        required = False
    elif normalized_required in ("1", "true", "yes", "on"):
        required = True
    else:
        raise LocalRoomConfigError(
            f"{_LOCAL_ROOMS_REQUIRED_KEY}は1/0またはtrue/falseで指定してください"
        )
    if required and (raw is None or not str(raw).strip()):
        raise LocalRoomConfigError(
            f"{_LOCAL_ROOMS_REQUIRED_KEY}=1ですが{_LOCAL_ROOMS_ENV_KEY}がありません"
        )
    return None if raw is None else str(raw)


def _nonempty_text(value: object, *, field: str, index: int) -> str:
    if not isinstance(value, str):
        raise LocalRoomConfigError(f"ローカル卓{index + 1}の{field}は文字列で指定してください")
    text = value.strip()
    if not text:
        raise LocalRoomConfigError(f"ローカル卓{index + 1}の{field}が空です")
    if len(text) > 100 or any(char in text for char in ("\r", "\n", "\0")):
        raise LocalRoomConfigError(f"ローカル卓{index + 1}の{field}が長すぎるか使用できない文字を含みます")
    return text


def _discord_ids(value: object, *, index: int) -> frozenset[int]:
    if not isinstance(value, list) or not value:
        raise LocalRoomConfigError(
            f"ローカル卓{index + 1}のallowed_gm_user_idsは1件以上の配列で指定してください"
        )
    result: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise LocalRoomConfigError(
                f"ローカル卓{index + 1}のallowed_gm_user_idsには整数だけを指定してください"
            )
        if item <= 0 or item > _MAX_DISCORD_ID:
            raise LocalRoomConfigError(
                f"ローカル卓{index + 1}のallowed_gm_user_idsに範囲外の値があります"
            )
        result.add(item)
    return frozenset(result)


def _role_names(value: object, *, index: int) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise LocalRoomConfigError(
            f"ローカル卓{index + 1}のaccess_role_namesは1件以上の配列で指定してください"
        )
    result = {
        _nonempty_text(item, field="access_role_names", index=index)
        for item in value
    }
    return frozenset(result)


def _boolean(item: dict, key: str, *, index: int, default: bool) -> bool:
    value = item.get(key, default)
    if not isinstance(value, bool):
        raise LocalRoomConfigError(f"ローカル卓{index + 1}の{key}はtrue/falseで指定してください")
    return value


def parse_local_room_config(
    raw: str | None,
    *,
    reserved_room_ids: Collection[str] = (),
    reserved_room_names: Collection[str] = (),
    manual_static_room_names: Collection[str] = (),
) -> tuple[LocalRoomRegistration, ...]:
    """JSON設定を厳格に検証し、固定卓定義へ変換する。

    未設定または空文字は正常な「ローカル卓なし」。値が存在するのに壊れている場合は
    一部だけ適用せず、起動前に必ず例外にする。
    """
    if raw is None or not raw.strip():
        return ()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LocalRoomConfigError(
            f"WEREWOLF_LOCAL_ROOMS_JSONがJSONとして不正です: {exc.msg}"
        ) from exc
    if not isinstance(decoded, list):
        raise LocalRoomConfigError("WEREWOLF_LOCAL_ROOMS_JSONはJSON配列で指定してください")
    if len(decoded) > _MAX_LOCAL_ROOMS:
        raise LocalRoomConfigError(f"ローカル卓は最大{_MAX_LOCAL_ROOMS}件です")

    used_ids = set(reserved_room_ids)
    used_names = set(reserved_room_names)
    manual_static_names = set(manual_static_room_names)
    registrations: list[LocalRoomRegistration] = []
    for index, item in enumerate(decoded):
        if not isinstance(item, dict):
            raise LocalRoomConfigError(f"ローカル卓{index + 1}はJSONオブジェクトで指定してください")
        unknown = set(item) - _LOCAL_ROOM_KEYS
        if unknown:
            raise LocalRoomConfigError(
                f"ローカル卓{index + 1}に未対応の項目があります: {', '.join(sorted(unknown))}"
            )
        missing = {
            "room_id", "name", "allowed_gm_user_ids", "access_role_names"
        } - set(item)
        if missing:
            raise LocalRoomConfigError(
                f"ローカル卓{index + 1}に必須項目がありません: {', '.join(sorted(missing))}"
            )

        room_id = _nonempty_text(item["room_id"], field="room_id", index=index)
        if not _ROOM_ID_PATTERN.fullmatch(room_id) or room_id.startswith("private_"):
            raise LocalRoomConfigError(
                f"ローカル卓{index + 1}のroom_idは英小文字・数字・_・-で指定し、private_で始めないでください"
            )
        if room_id in used_ids:
            raise LocalRoomConfigError(f"room_idが重複しています: {room_id}")

        name = _nonempty_text(item["name"], field="name", index=index)
        if name in used_names:
            raise LocalRoomConfigError(f"卓の表示名が重複しています: {name}")

        # v0.40では全村がレート対象で、募集はGM名前村の参加受付へ統合した。
        # 既存.envを壊さないため旧キーは受理・型検証のみ行う。
        if "rated" in item:
            _boolean(item, "rated", index=index, default=True)
        if "recruitment_enabled" in item:
            _boolean(item, "recruitment_enabled", index=index, default=True)

        used_ids.add(room_id)
        used_names.add(name)
        registrations.append(
            LocalRoomRegistration(
                room=RoomDefinition(
                    room_id=room_id,
                    name=name,
                    allowed_gm_user_ids=_discord_ids(
                        item["allowed_gm_user_ids"], index=index
                    ),
                    access_role_names=_role_names(
                        item["access_role_names"], index=index
                    ),
                    # 指定されたサーバー固有卓だけ、Discord側の手動overwriteを
                    # 既定の正本にする。他のローカル卓は従来どおり自動同期。
                    sync_permissions=_boolean(
                        item,
                        "sync_permissions",
                        index=index,
                        default=name not in manual_static_names,
                    ),
                ),
            )
        )
    return tuple(registrations)
