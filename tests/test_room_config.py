"""公開コード向けローカル卓設定の厳格な検証。"""
from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from room_config import (
    LocalRoomConfigError,
    load_local_room_json,
    parse_local_room_config,
)


ROOT_DIR = Path(__file__).resolve().parent.parent


class LocalRoomConfigTest(unittest.TestCase):
    def _json(self, **overrides) -> str:
        room = {
            "room_id": "community",
            "name": "コミュニティ村",
            "allowed_gm_user_ids": [123456789012345678],
            "access_role_names": ["コミュニティ参加者"],
        }
        room.update(overrides)
        return json.dumps([room], ensure_ascii=False)

    def test_empty_setting_means_no_local_rooms(self) -> None:
        self.assertEqual(parse_local_room_config(None), ())
        self.assertEqual(parse_local_room_config("  "), ())

    def test_valid_room_preserves_ids_roles_and_safe_defaults(self) -> None:
        registrations = parse_local_room_config(self._json())

        self.assertEqual(len(registrations), 1)
        registration = registrations[0]
        self.assertEqual(registration.room.room_id, "community")
        self.assertEqual(registration.room.name, "コミュニティ村")
        self.assertEqual(
            registration.room.allowed_gm_user_ids,
            frozenset({123456789012345678}),
        )
        self.assertEqual(
            registration.room.access_role_names,
            frozenset({"コミュニティ参加者"}),
        )
        self.assertTrue(registration.room.sync_permissions)

        manual = parse_local_room_config(
            self._json(), manual_static_room_names={"コミュニティ村"}
        )[0]
        self.assertFalse(manual.room.sync_permissions)

    def test_legacy_rating_and_recruitment_keys_are_accepted(self) -> None:
        registration = parse_local_room_config(
            self._json(
                rated=False,
                recruitment_enabled=False,
                sync_permissions=True,
            )
        )[0]
        self.assertTrue(registration.room.sync_permissions)

    def test_reserved_or_duplicate_id_is_rejected(self) -> None:
        with self.assertRaises(LocalRoomConfigError):
            parse_local_room_config(
                self._json(room_id="open"), reserved_room_ids={"open"}
            )
        duplicated = json.loads(self._json()) * 2
        with self.assertRaises(LocalRoomConfigError):
            parse_local_room_config(json.dumps(duplicated, ensure_ascii=False))

    def test_reserved_or_duplicate_name_is_rejected(self) -> None:
        with self.assertRaises(LocalRoomConfigError):
            parse_local_room_config(
                self._json(name="総合"), reserved_room_names={"総合"}
            )
        duplicated = json.loads(self._json()) * 2
        duplicated[1]["room_id"] = "community-2"
        with self.assertRaises(LocalRoomConfigError):
            parse_local_room_config(json.dumps(duplicated, ensure_ascii=False))

    def test_missing_security_boundary_is_rejected(self) -> None:
        room = json.loads(self._json())[0]
        del room["access_role_names"]
        with self.assertRaises(LocalRoomConfigError):
            parse_local_room_config(json.dumps([room], ensure_ascii=False))

    def test_unknown_key_and_wrong_types_are_rejected(self) -> None:
        with self.assertRaises(LocalRoomConfigError):
            parse_local_room_config(self._json(typo=True))
        with self.assertRaises(LocalRoomConfigError):
            parse_local_room_config(self._json(rated="true"))
        with self.assertRaises(LocalRoomConfigError):
            parse_local_room_config(self._json(sync_permissions="false"))
        with self.assertRaises(LocalRoomConfigError):
            parse_local_room_config(self._json(allowed_gm_user_ids=[True]))

    def test_invalid_json_and_reserved_private_prefix_are_rejected(self) -> None:
        with self.assertRaises(LocalRoomConfigError):
            parse_local_room_config("not-json")
        with self.assertRaises(LocalRoomConfigError):
            parse_local_room_config(self._json(room_id="private_owner"))

    def test_required_room_setting_cannot_disappear_silently(self) -> None:
        with tempfile.TemporaryDirectory(prefix="werewolf-room-config-") as tmp:
            dotenv_path = Path(tmp) / ".env"
            dotenv_path.write_text(
                "WEREWOLF_LOCAL_ROOMS_REQUIRED=1\n",
                encoding="utf-8",
            )
            with self.assertRaises(LocalRoomConfigError):
                load_local_room_json(dotenv_path, {})

    def test_malformed_declared_dotenv_value_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="werewolf-room-config-") as tmp:
            dotenv_path = Path(tmp) / ".env"
            dotenv_path.write_text(
                "WEREWOLF_LOCAL_ROOMS_JSON='unterminated\n"
                "WEREWOLF_LOCAL_ROOMS_REQUIRED=1\n",
                encoding="utf-8",
            )
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(LocalRoomConfigError):
                    load_local_room_json(dotenv_path, {})

    def test_process_environment_overrides_dotenv_file(self) -> None:
        raw = self._json(
            rated=False,
            recruitment_enabled=False,
        )
        script = (
            "import config; "
            "room=config.ROOM_DEFINITION_MAP['community']; "
            "print(room.name, 'community' in config.RATED_ROOM_IDS, "
            "set(config.ACTIVE_ROOM_IDS) <= set(config.RATED_ROOM_IDS))"
        )
        env = os.environ.copy()
        env["WEREWOLF_LOCAL_ROOMS_JSON"] = raw
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT_DIR,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # `rated` は旧設定互換として読み込むが、稼働中の全村をレート対象にする。
        self.assertEqual(completed.stdout.strip(), "コミュニティ村 True True")

    def test_invalid_environment_fails_before_runtime_initialization(self) -> None:
        env = os.environ.copy()
        env["WEREWOLF_LOCAL_ROOMS_JSON"] = "not-json"
        completed = subprocess.run(
            [sys.executable, "-c", "import config"],
            cwd=ROOT_DIR,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("LocalRoomConfigError", completed.stderr)


if __name__ == "__main__":
    unittest.main()
