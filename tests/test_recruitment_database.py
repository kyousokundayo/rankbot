"""募集の予約・参加・同村拒否を本番DBから隔離して検証する。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import database


class RecruitmentDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-recruitment-test-")
        self._old_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "recruitment.db")
        await database.init_db()
        self.base = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._old_path
        self._tmp.cleanup()

    async def _create(self, *, host: int, room: str = "open", offset: int = 0) -> int:
        return await database.create_recruitment(
            1, host, title=f"募集{host}",
            scheduled_at=self.base + timedelta(minutes=offset), room_id=room,
            streaming=False, allowed_ranks=None,
        )

    async def test_allowed_ranks_round_trip_and_open_room_only(self) -> None:
        recruitment_id = await database.create_recruitment(
            1, 1, title="ランク指定", scheduled_at=self.base,
            room_id="open", streaming=False,
            allowed_ranks={"ダイヤ", "ランク未設定", "ゴールド"},
        )
        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(
            row["allowed_ranks"],
            frozenset({"ゴールド", "ダイヤ", "ランク未設定"}),
        )

        with self.assertRaisesRegex(ValueError, "総合卓だけ"):
            await database.create_recruitment(
                1, 2, title="不正な指定", scheduled_at=self.base + timedelta(hours=2),
                room_id="beginner", streaming=False, allowed_ranks={"アイアン"},
            )

    async def test_legacy_beginner_block_migrates_to_gold_and_above(self) -> None:
        recruitment_id = await self._create(host=1)
        async with database.connect_db() as db:
            await db.execute("ALTER TABLE recruitments ADD COLUMN allow_beginner INTEGER")
            await db.execute(
                "UPDATE recruitments SET allow_beginner = 0 WHERE id = ?",
                (recruitment_id,),
            )
            await db.commit()

        await database.init_db()
        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(
            row["allowed_ranks"],
            frozenset({
                "ゴールド", "プラチナ", "エメラルド",
                "ダイヤ", "マスター", "グランドマスター",
            }),
        )

    async def test_room_reservation_overlap_and_touching_boundary(self) -> None:
        await self._create(host=1)
        with self.assertRaises(database.RecruitmentConflict):
            await self._create(host=2, offset=89)
        touching = await self._create(host=2, offset=90)
        self.assertGreater(touching, 0)
        other_room = await self._create(host=3, room="beginner", offset=30)
        self.assertGreater(other_room, 0)

    async def test_held_recruitment_still_occupies_room_and_participant_time(self) -> None:
        held = await self._create(host=1, room="open")
        await database.add_recruitment_entry(held, 999)
        changed = await database.set_recruitment_status(
            held, database.RECRUITMENT_HELD
        )
        self.assertTrue(changed)

        with self.assertRaises(database.RecruitmentConflict):
            await self._create(host=2, room="open", offset=30)

        second = await self._create(host=2, room="beginner", offset=30)
        with self.assertRaises(database.RecruitmentConflict):
            await database.add_recruitment_entry(second, 999)

    async def test_participant_overlap_includes_backup(self) -> None:
        first = await self._create(host=1, room="open")
        second = await self._create(host=2, room="beginner", offset=30)
        for user_id in range(100, 113):
            self.assertEqual(await database.add_recruitment_entry(first, user_id), "参加")
        self.assertEqual(await database.add_recruitment_entry(first, 999), "補欠")
        with self.assertRaises(database.RecruitmentConflict):
            await database.add_recruitment_entry(second, 999)

    async def test_cancel_promotes_oldest_backup_without_recheck(self) -> None:
        recruitment_id = await self._create(host=1)
        for user_id in range(100, 113):
            await database.add_recruitment_entry(recruitment_id, user_id)
        await database.add_recruitment_entry(recruitment_id, 200)
        await database.add_recruitment_entry(recruitment_id, 201)
        removed, promoted = await database.remove_recruitment_entry(recruitment_id, 105)
        self.assertEqual((removed, promoted), ("参加", 200))
        entries = await database.list_recruitment_entries(recruitment_id)
        kinds = {row["user_id"]: row["kind"] for row in entries}
        self.assertEqual(kinds[200], "参加")
        self.assertEqual(kinds[201], "補欠")

    async def test_block_direction_only_existing_players_block_newcomer(self) -> None:
        recruitment_id = await self._create(host=1)
        # Bが先、Aが後。A自身の「B拒否」は参加判定に使わない。
        await database.add_recruitment_entry(recruitment_id, 20)
        await database.add_player_block(1, 10, 20)
        self.assertEqual(await database.add_recruitment_entry(recruitment_id, 10), "参加")

        second = await self._create(host=2, room="beginner", offset=120)
        # Aが先、Bが後。既存Aの「B拒否」が効く。
        await database.add_recruitment_entry(second, 10)
        with self.assertRaises(database.RecruitmentConflict):
            await database.add_recruitment_entry(second, 20)

    async def test_host_limit_and_player_block_limit(self) -> None:
        await self._create(host=1, room="open", offset=0)
        await self._create(host=1, room="beginner", offset=120)
        await self._create(host=1, room="intermediate", offset=240)
        with self.assertRaises(database.RecruitmentConflict):
            await self._create(host=1, room="advanced", offset=360)

        for blocked_id in range(100, 110):
            count = await database.add_player_block(1, 50, blocked_id)
        self.assertEqual(count, 10)
        with self.assertRaises(database.PlayerBlockLimitReached):
            await database.add_player_block(1, 50, 111)
        self.assertTrue(await database.remove_player_block(1, 50, 100))
        self.assertEqual(await database.add_player_block(1, 50, 111), 10)

    async def test_game_records_base_room_and_recruitment(self) -> None:
        records = [{
            "player_id": 1, "role": "村人", "team": "村陣営", "won": 1,
            "died_on_day": None, "death_cause": None,
            "rank_at_game": None, "rank_provisional": None,
        }]
        await database.stage_game_settlement(
            1, "open", "recruitment-game", room_name="総合", rated=False,
            winner_team="村陣営", player_records=records, gm_id=9,
            base_room_id="open", recruitment_id=123,
        )
        game_id, _, _ = await database.settle_game_settlement(
            1, "open", "recruitment-game"
        )
        async with database.connect_db() as db:
            row = (await db.execute_fetchall(
                "SELECT base_room_id, recruitment_id FROM games WHERE game_id=?",
                (game_id,),
            ))[0]
        self.assertEqual(row, ("open", 123))

    async def test_recruitment_gm_claim_uses_compare_and_swap(self) -> None:
        recruitment_id = await self._create(host=1)

        results = await asyncio.gather(
            database.set_recruitment_gm(
                recruitment_id, 10, expected_gm_id=None
            ),
            database.set_recruitment_gm(
                recruitment_id, 20, expected_gm_id=None
            ),
            return_exceptions=True,
        )

        self.assertEqual(sum(result is None for result in results), 1)
        self.assertEqual(
            sum(isinstance(result, database.RecruitmentConflict) for result in results),
            1,
        )
        row = await database.get_recruitment(recruitment_id)
        self.assertIn(row["gm_id"], {10, 20})


if __name__ == "__main__":
    unittest.main()
