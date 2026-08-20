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
        for host_id in range(1, 4):
            room_id = f"private_{host_id}"
            await database.save_private_room(
                1, room_id, host_id, f"GM村{host_id}",
            )
            await database.mark_private_room_active(
                1, room_id, category_id=100 + host_id,
            )

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._old_path
        self._tmp.cleanup()

    async def _create(
        self,
        *,
        host: int,
        room: str | None = None,
        offset: int = 0,
        variant_id: str = "v13_cross",
    ) -> int:
        return await database.create_recruitment(
            1, host, title=f"募集{host}",
            scheduled_at=self.base + timedelta(minutes=offset),
            room_id=room or f"private_{host}",
            variant_id=variant_id,
            streaming=False, allowed_ranks=None,
        )

    async def test_allowed_ranks_round_trip_for_gm_rooms(self) -> None:
        recruitment_id = await database.create_recruitment(
            1, 1, title="ランク指定", scheduled_at=self.base,
            room_id="private_1", variant_id="v13_cross", streaming=False,
            allowed_ranks={"ダイヤ", "ランク未設定", "ゴールド"},
        )
        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(
            row["allowed_ranks"],
            frozenset({"ゴールド", "ダイヤ", "ランク未設定"}),
        )

        gm_recruitment_id = await database.create_recruitment(
            1, 2, title="GM村ランク指定",
            scheduled_at=self.base + timedelta(hours=2),
            room_id="private_2", variant_id="v9_turn", streaming=False,
            allowed_ranks={"アイアン", "ランク未設定"},
        )
        gm_row = await database.get_recruitment(gm_recruitment_id)
        self.assertEqual(
            gm_row["allowed_ranks"],
            frozenset({"アイアン", "ランク未設定"}),
        )

    async def test_gm_village_allows_one_open_recruitment(self) -> None:
        await self._create(host=1)
        with self.assertRaisesRegex(database.RecruitmentConflict, "既にあります"):
            await self._create(host=1, offset=90)
        other_room = await self._create(
            host=3, room="private_3", offset=30, variant_id="v13_cross",
        )
        self.assertGreater(other_room, 0)

    async def test_enabled_variant_capacity_and_occupancy_are_snapshotted(self) -> None:
        nine = await self._create(
            host=1, room="private_1", variant_id="v9_cross",
        )
        row = await database.get_recruitment(nine)
        self.assertEqual(row["variant_id"], "v9_cross")
        self.assertEqual(row["capacity"], 9)
        self.assertEqual(row["occupancy_minutes"], 90)
        for user_id in range(100, 109):
            self.assertEqual(
                await database.add_recruitment_entry(nine, user_id),
                "参加",
            )
        self.assertEqual(await database.add_recruitment_entry(nine, 200), "補欠")

        with self.assertRaisesRegex(database.RecruitmentConflict, "GM村"):
            await self._create(host=2, room="open_13_turn", offset=120)

    async def test_all_standard_open_variants_allow_rank_filters(self) -> None:
        recruitment_id = await database.create_recruitment(
            1,
            1,
            title="9人ランク指定",
            scheduled_at=self.base,
            room_id="private_1",
            variant_id="v9_turn",
            streaming=False,
            allowed_ranks={"ゴールド"},
        )
        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(row["allowed_ranks"], frozenset({"ゴールド"}))

    async def test_fixed_room_is_rejected_before_booking(self) -> None:
        with self.assertRaisesRegex(database.RecruitmentConflict, "GM村"):
            await self._create(host=1, room="open")

    async def test_init_db_rejects_reintroduced_active_fixed_recruitment_unchanged(self) -> None:
        async with database.connect_db() as db:
            await db.execute(
                "INSERT INTO recruitments "
                "(guild_id, host_id, title, scheduled_at, room_id, streaming, "
                "status, variant_id, capacity, backup_capacity, occupancy_minutes) "
                "VALUES (1, 1, '不正固定卓募集', ?, 'open', 0, ?, "
                "'v13_cross', 13, 3, 90)",
                (self.base.isoformat(), database.RECRUITMENT_OPEN),
            )
            await db.commit()

        with self.assertRaisesRegex(RuntimeError, "GM名前村と一致しません"):
            await database.init_db()

        async with database.connect_db() as db:
            rows = await db.execute_fetchall(
                "SELECT room_id, status FROM recruitments WHERE title='不正固定卓募集'"
            )
        self.assertEqual(rows, [("open", database.RECRUITMENT_OPEN)])

    async def test_other_owner_cannot_create_recruitment_for_gm_village(self) -> None:
        with self.assertRaisesRegex(database.RecruitmentConflict, "村主だけ"):
            await self._create(host=2, room="private_1")

    async def test_archive_uses_each_recruitments_occupancy(self) -> None:
        first = await self._create(host=1)
        cross = await self._create(
            host=2, room="private_2", variant_id="v9_cross",
        )
        archived = await database.archive_expired_recruitments(
            1, self.base + timedelta(minutes=91),
        )
        self.assertCountEqual(archived, [first, cross])

    async def test_held_recruitment_occupies_room_but_not_other_waiting_registration(self) -> None:
        held = await self._create(host=1)
        await database.add_recruitment_entry(held, 999)
        changed = await database.set_recruitment_status(
            held, database.RECRUITMENT_HELD
        )
        self.assertTrue(changed)

        with self.assertRaises(database.RecruitmentConflict):
            await self._create(host=1, offset=30)

        second = await self._create(
            host=2, room="private_2", offset=30, variant_id="v13_cross",
        )
        self.assertEqual(
            await database.add_recruitment_entry(second, 999), "参加",
        )

    async def test_participant_and_backup_can_register_for_another_waiting_village(self) -> None:
        first = await self._create(host=1)
        second = await self._create(
            host=2, room="private_2", offset=30, variant_id="v13_cross",
        )
        for user_id in range(100, 113):
            self.assertEqual(await database.add_recruitment_entry(first, user_id), "参加")
        self.assertEqual(await database.add_recruitment_entry(first, 999), "補欠")
        self.assertEqual(
            await database.add_recruitment_entry(second, 999), "参加",
        )

    async def test_previous_settings_create_a_new_empty_recruitment(self) -> None:
        source = await database.create_recruitment(
            1,
            1,
            title="前回設定",
            scheduled_at=self.base,
            room_id="private_1",
            variant_id="v9_turn",
            streaming=True,
            allowed_ranks={"ゴールド", "ランク未設定"},
            note="配信卓",
        )
        await database.add_recruitment_entry(source, 999)
        await database.set_recruitment_gm(source, 1)
        await database.set_recruitment_message_id(source, 444)
        self.assertTrue(await database.set_recruitment_status(
            source, database.RECRUITMENT_ARCHIVED,
        ))

        reopened_at = self.base + timedelta(hours=3)
        new_id, source_id = await database.create_recruitment_from_previous_settings(
            1, "private_1", 1, scheduled_at=reopened_at,
        )

        self.assertEqual(source_id, source)
        self.assertNotEqual(new_id, source)
        old_row = await database.get_recruitment(source)
        new_row = await database.get_recruitment(new_id)
        self.assertEqual(old_row["status"], database.RECRUITMENT_ARCHIVED)
        old_entries = await database.list_recruitment_entries(source)
        self.assertEqual(
            [(entry["user_id"], entry["kind"]) for entry in old_entries],
            [(999, "参加")],
        )
        self.assertEqual(await database.list_recruitment_entries(new_id), [])
        self.assertEqual(new_row["status"], database.RECRUITMENT_OPEN)
        self.assertEqual(new_row["gm_id"], 1)
        self.assertEqual(new_row["title"], "前回設定")
        self.assertEqual(new_row["variant_id"], "v9_turn")
        self.assertTrue(new_row["streaming"])
        self.assertEqual(
            new_row["allowed_ranks"],
            frozenset({"ゴールド", "ランク未設定"}),
        )
        self.assertEqual(new_row["note"], "配信卓")
        self.assertEqual(new_row["scheduled_at"], reopened_at.isoformat())
        self.assertIsNone(new_row["notified_at"])
        self.assertIsNone(new_row["message_id"])

    async def test_archive_and_lobby_snapshot_are_committed_together(self) -> None:
        recruitment_id = await self._create(host=1)
        self.assertTrue(await database.set_recruitment_status(
            recruitment_id, database.RECRUITMENT_HELD,
        ))
        payload = {
            "public_log_archive_allowed": False,
            "vc_default_permissions_captured": False,
            "vc_gm_speak_captured": False,
            "morning_confirmed": False,
            "prep_confirmed": False,
            "mute_marker_enabled": False,
            "players": [],
            "recruitment_id": None,
        }

        with self.assertRaises(ValueError):
            await database.archive_linked_recruitment_and_save_lobby_state(
                1,
                "private_1",
                recruitment_id,
                {**payload, "players": [{"user_id": 999}]},
            )
        self.assertEqual(
            (await database.get_recruitment(recruitment_id))["status"],
            database.RECRUITMENT_HELD,
        )

        with self.assertRaises(database.RecruitmentConflict):
            await database.archive_linked_recruitment_and_save_lobby_state(
                1, "private_2", recruitment_id, payload,
            )
        self.assertEqual(
            (await database.get_recruitment(recruitment_id))["status"],
            database.RECRUITMENT_HELD,
        )
        self.assertNotIn("private_2", await database.load_room_states(1))

        await database.archive_linked_recruitment_and_save_lobby_state(
            1, "private_1", recruitment_id, payload,
        )
        self.assertEqual(
            (await database.get_recruitment(recruitment_id))["status"],
            database.RECRUITMENT_ARCHIVED,
        )
        saved = (await database.load_room_states(1))["private_1"]
        self.assertEqual(saved["phase"], "LOBBY")
        self.assertIsNone(saved["recruitment_id"])

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

    async def test_notification_delivery_is_tracked_per_participant(self) -> None:
        recruitment_id = await self._create(host=1)
        await database.add_recruitment_entry(recruitment_id, 100)
        self.assertEqual(
            await database.list_pending_recruitment_notification_user_ids(
                recruitment_id
            ),
            [100],
        )

        self.assertTrue(
            await database.mark_recruitment_participant_notified(
                recruitment_id, 100, self.base, status="sent"
            )
        )
        await database.add_recruitment_entry(recruitment_id, 101)
        self.assertEqual(
            await database.list_pending_recruitment_notification_user_ids(
                recruitment_id
            ),
            [101],
        )

        # 通知済みの人が退出・再参加しても同じ募集では再送しない。
        await database.remove_recruitment_entry(recruitment_id, 100)
        await database.add_recruitment_entry(recruitment_id, 100)
        self.assertEqual(
            await database.list_pending_recruitment_notification_user_ids(
                recruitment_id
            ),
            [101],
        )

    async def test_block_direction_only_existing_players_block_newcomer(self) -> None:
        recruitment_id = await self._create(host=1)
        # Bが先、Aが後。A自身の「B拒否」は参加判定に使わない。
        await database.add_recruitment_entry(recruitment_id, 20)
        await database.add_player_block(1, 10, 20)
        self.assertEqual(await database.add_recruitment_entry(recruitment_id, 10), "参加")

        second = await self._create(
            host=2, room="private_2", offset=120, variant_id="v13_cross",
        )
        # Aが先、Bが後。既存Aの「B拒否」が効く。
        await database.add_recruitment_entry(second, 10)
        with self.assertRaises(database.RecruitmentConflict):
            await database.add_recruitment_entry(second, 20)

    async def test_player_block_limit(self) -> None:
        for blocked_id in range(100, 150):
            count = await database.add_player_block(1, 50, blocked_id)
        self.assertEqual(count, 50)
        with self.assertRaises(database.PlayerBlockLimitReached):
            await database.add_player_block(1, 50, 150)
        self.assertTrue(await database.remove_player_block(1, 50, 100))
        self.assertEqual(await database.add_player_block(1, 50, 150), 50)

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


class RecruitmentScheduleViewTest(unittest.TestCase):
    """開催日セレクトの「今すぐ」で時刻選択が消えることを検証する。

    View は Discord に依存しないので、DBを立てずに状態遷移だけを見る。
    """

    def setUp(self) -> None:
        import recruitment
        self.recruitment = recruitment
        self.view = recruitment.RecruitmentScheduleView(object(), host_id=1)

    def _keys(self) -> list[str]:
        return [item.key for item in self.view.children]

    def test_default_flow_needs_date_hour_minute_variant_streaming(self):
        self.assertEqual(
            self._keys(), ["date", "hour", "minute", "variant", "streaming"],
        )
        self.assertFalse(self.view.is_complete())
        self.view.values.update({
            "date": "2026-08-10", "hour": "20", "minute": "30",
            "variant": "v9_cross", "streaming": "0",
        })
        self.assertTrue(self.view.is_complete())

    def test_immediate_hides_time_selects(self):
        self.view.values["date"] = self.recruitment.IMMEDIATE_DATE_VALUE
        self.view.rebuild()
        self.assertEqual(self._keys(), ["date", "variant", "streaming"])
        self.assertTrue(self.view.immediate)
        self.assertFalse(self.view.is_complete())
        self.view.values.update({"variant": "v9_turn", "streaming": "0"})
        self.assertTrue(self.view.is_complete())

    def test_switching_back_to_a_date_restores_time_selects(self):
        self.view.values["date"] = self.recruitment.IMMEDIATE_DATE_VALUE
        self.view.rebuild()
        self.view.values["date"] = "2026-08-10"
        self.view.rebuild()
        self.assertEqual(
            self._keys(), ["date", "hour", "minute", "variant", "streaming"],
        )
        self.assertFalse(self.view.immediate)

    def test_immediate_start_passes_the_range_check(self):
        """「今すぐ」の開始時刻が「現在より後」を満たすこと。

        _schedule_out_of_range は local_start <= now を弾くので、
        猶予を0にすると作成できなくなる。
        """
        from config import (
            RECRUITMENT_IMMEDIATE_LEAD_MINUTES,
            RECRUITMENT_NOTIFICATION_WINDOW_MINUTES,
        )
        now = datetime(2026, 8, 8, 20, 55, 33, tzinfo=self.recruitment.JST)
        start = (
            now + timedelta(minutes=RECRUITMENT_IMMEDIATE_LEAD_MINUTES)
        ).replace(second=0, microsecond=0)
        self.assertGreater(start, now)
        self.assertFalse(self.recruitment._schedule_out_of_range(start, now))
        # 通知ウィンドウより内側なら、作成時点から開催前DMの対象になる
        self.assertLess(
            RECRUITMENT_IMMEDIATE_LEAD_MINUTES,
            RECRUITMENT_NOTIFICATION_WINDOW_MINUTES,
        )
