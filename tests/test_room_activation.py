"""無効固定卓をDiscord副作用なしで安全にスキップする回帰テスト。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import database
from config import Phase
from game import GameCog


class DisabledRoomStartupGuardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-room-activation-")
        self._old_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "activation.db")
        await database.init_db()

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    @staticmethod
    def _manager() -> GameCog:
        manager = GameCog(SimpleNamespace(managed_guild_id=1))
        # 以下はDB検査の後に初めて呼ばれる処理。未awaitをもって、
        # 固定卓の失敗でDiscord副作用へ進んでいないことを確認する。
        manager._recover_pending_settlements = AsyncMock()
        manager.load_pending_unmutes = AsyncMock()
        manager._cleanup_private_rooms_without_creator_role = AsyncMock()
        return manager

    def test_empty_lobby_or_game_over_snapshot_is_permitted(self) -> None:
        for phase in (Phase.LOBBY.name, Phase.GAME_OVER.name):
            with self.subTest(phase=phase):
                conflicts = GameCog._disabled_fixed_room_snapshot_conflicts(
                    {
                        "open_13_turn": {
                            "phase": phase,
                            "players": [],
                            "gm_id": None,
                            "recruitment_id": None,
                        }
                    },
                    set(),
                )
                self.assertEqual(conflicts, {})

    def test_participant_gm_recruitment_and_quarantine_each_block_skip(self) -> None:
        base = {
            "phase": Phase.LOBBY.name,
            "players": [],
            "gm_id": None,
            "recruitment_id": None,
        }
        cases = {
            "参加者を含むsnapshot": {**base, "players": [{"user_id": 10}]},
            "GMを含むsnapshot": {**base, "gm_id": 10},
            "募集紐付きsnapshot": {**base, "recruitment_id": 10},
            "進行中snapshot": {**base, "phase": Phase.NIGHT.name},
            "Bot所有Discordチャンネルを含むsnapshot": {
                **base,
                "channel_ids": {"category": 101, "lobby": 102, "voice": 103},
            },
        }
        for expected_reason, payload in cases.items():
            with self.subTest(reason=expected_reason):
                conflicts = GameCog._disabled_fixed_room_snapshot_conflicts(
                    {"open_13_turn": payload},
                    set(),
                )
                self.assertTrue(
                    any(
                        reason.startswith(expected_reason)
                        for reason in conflicts["open_13_turn"]
                    )
                )

        conflicts = GameCog._disabled_fixed_room_snapshot_conflicts(
            {}, {"open_13_turn"}
        )
        self.assertIn("隔離snapshot", conflicts["open_13_turn"])

    async def test_active_snapshot_blocks_before_discord_work(self) -> None:
        await database.save_room_state(
            1,
            "open_13_turn",
            Phase.NIGHT.name,
            {
                "players": [
                    {
                        "user_id": 10,
                        "role": "VILLAGER",
                        "number": 1,
                        "alive": True,
                    }
                ],
            },
        )
        manager = self._manager()

        with self.assertRaisesRegex(RuntimeError, "無効固定卓"):
            await manager.setup_channels(SimpleNamespace(id=1))

        manager._recover_pending_settlements.assert_not_awaited()
        manager.load_pending_unmutes.assert_not_awaited()
        manager._cleanup_private_rooms_without_creator_role.assert_not_awaited()

    async def test_unarchived_recruitment_blocks_before_discord_work(self) -> None:
        # enabled=False に変更する前の募集が残った移行状態をDBへ再現する。
        async with database.connect_db() as db:
            await db.execute(
                "INSERT INTO recruitments "
                "(guild_id, host_id, title, scheduled_at, room_id, status, "
                "variant_id, capacity, backup_capacity, occupancy_minutes) "
                "VALUES (1, 10, '旧13人ターン募集', '2026-08-09 12:00:00', "
                "'open_13_turn', ?, 'v13_turn', 13, 3, 150)",
                (database.RECRUITMENT_OPEN,),
            )
            await db.commit()
        manager = self._manager()

        with self.assertRaisesRegex(RuntimeError, "未終了募集"):
            await manager.setup_channels(SimpleNamespace(id=1))

        manager._recover_pending_settlements.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
