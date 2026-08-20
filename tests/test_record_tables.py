"""v0.51 Phase1: 記録テーブル (CO/結果申告/投票/夜行動ログ、通知設定、
「募集」ボタンの送達台帳) のスキーマ移行・記録API・集計APIを検証する。

DBテストの雛形は tests/test_recruitment_database.py:15 と
tests/test_game_stats_database.py:22 に倣う。
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

import database
from config import Role, Team


def _player_records(*, wolf_ids: set[int], player_ids: list[int]) -> list[dict]:
    """精算に渡す最小限の player_records を組み立てる。"""
    records = []
    for player_id in player_ids:
        is_wolf = player_id in wolf_ids
        records.append({
            "player_id": player_id,
            "role": Role.WEREWOLF.value if is_wolf else Role.VILLAGER.value,
            "team": Team.WOLF.value if is_wolf else Team.VILLAGE.value,
            "won": int(not is_wolf),
            "died_on_day": None,
            "death_cause": None,
            "rank_at_game": None,
            "rank_provisional": None,
        })
    return records


class RecordTablesDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-record-tables-test-")
        self._old_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "records.db")
        await database.init_db()

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._old_path
        self._tmp.cleanup()

    # ------------------------------------------------------------
    # 移行
    # ------------------------------------------------------------
    async def test_init_db_migrates_pre_v049_schema_and_passes_validation(self) -> None:
        """記録テーブル・started_at列が無い旧DBでも init_db が移行を通す。"""
        async with aiosqlite.connect(database.DB_PATH) as db:
            for table in (
                "game_co_events", "game_co_results", "game_vote_events",
                "game_night_actions", "user_notification_prefs",
                "recruitment_calls", "recruitment_call_deliveries",
            ):
                await db.execute(f"DROP TABLE {table}")
            await db.execute("ALTER TABLE games DROP COLUMN started_at")
            await db.execute("ALTER TABLE game_settlements DROP COLUMN started_at")
            await db.commit()

        # 移行前は想定どおり旧スキーマになっていること (壊れたテストにならないよう確認)
        async with aiosqlite.connect(database.DB_PATH) as db:
            tables_before = {
                row[0] for row in await db.execute_fetchall(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertNotIn("game_co_events", tables_before)

        await database.init_db()  # 再実行 = 既存DB分岐を通る

        async with database.connect_db() as db:
            tables_after = {
                row[0] for row in await db.execute_fetchall(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            games_columns = {
                row[1] for row in await db.execute_fetchall("PRAGMA table_info(games)")
            }
            settlements_columns = {
                row[1] for row in await db.execute_fetchall(
                    "PRAGMA table_info(game_settlements)"
                )
            }
        for table in (
            "game_co_events", "game_co_results", "game_vote_events",
            "game_night_actions", "user_notification_prefs",
            "recruitment_calls", "recruitment_call_deliveries",
        ):
            self.assertIn(table, tables_after)
        self.assertIn("started_at", games_columns)
        self.assertIn("started_at", settlements_columns)

        # 移行は再実行しても壊れない (IF NOT EXISTS / 列存在チェックで冪等)
        await database.init_db()

    async def test_migration_does_not_touch_truly_unmigrated_legacy_db(self) -> None:
        """v0.40より前の未知スキーマは、記録テーブル移行を挟んでも従来どおり拒否される。"""
        legacy_path = str(Path(self._tmp.name) / "legacy.db")
        async with aiosqlite.connect(legacy_path) as db:
            await db.execute(
                "CREATE TABLE games (game_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, "
                "winner_team TEXT NOT NULL, played_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            await db.commit()

        database.DB_PATH = legacy_path
        with self.assertRaisesRegex(RuntimeError, "未移行のDBスキーマ"):
            await database.init_db()

        async with aiosqlite.connect(legacy_path) as db:
            tables = {
                row[0] for row in await db.execute_fetchall(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        # ロールバックされ、新テーブルが中途半端に作られていないこと
        self.assertEqual(tables, {"games"})

    # ------------------------------------------------------------
    # 記録API
    # ------------------------------------------------------------
    async def test_co_event_append_and_list_order(self) -> None:
        await database.record_co_event(
            1, "open", "run-co", event_seq=1, day_number=1, phase="DAY_DISCUSSION",
            actor_id=100, actor_number=1, event_type="CO", claimed_role="占い師",
        )
        await database.record_co_event(
            1, "open", "run-co", event_seq=2, day_number=1, phase="DAY_DISCUSSION",
            actor_id=100, actor_number=1, event_type="撤回", claimed_role="占い師",
        )
        await database.record_co_event(
            1, "open", "run-co", event_seq=3, day_number=1, phase="DAY_DISCUSSION",
            actor_id=100, actor_number=1, event_type="CO", claimed_role="霊媒師",
        )
        events = await database.list_co_events_for_run(1, "open", "run-co")
        self.assertEqual([e["event_type"] for e in events], ["CO", "撤回", "CO"])
        self.assertEqual(events[-1]["claimed_role"], "霊媒師")

    async def test_co_results_recorded_before_v050_are_still_readable(self) -> None:
        """[結果を公開] の書き込み経路は持たないが、既存の行は読める。"""
        async with aiosqlite.connect(database.DB_PATH) as db:
            for seq, event_type in ((1, "公開"), (2, "取消")):
                await db.execute(
                    "INSERT INTO game_co_results "
                    "(guild_id, room_id, game_run_id, event_seq, day_number, "
                    "actor_id, actor_number, claimed_role, event_type, target_id, "
                    "target_number, judgement) "
                    "VALUES (1, 'open', 'run-res', ?, 1, 100, 1, '占い師', ?, 200, 2, '白')",
                    (seq, event_type),
                )
            await db.commit()

        results = await database.list_co_results_for_run(1, "open", "run-res")
        self.assertEqual(len(results), 2)
        self.assertEqual([r["event_type"] for r in results], ["公開", "取消"])
        self.assertFalse(hasattr(database, "record_co_result"))

    async def test_vote_and_night_action_events_accept_null_target(self) -> None:
        await database.record_vote_event(
            1, "open", "run-vote", event_seq=1, day_number=1, vote_kind="本投票",
            round_index=0, voter_id=100, voter_number=1, target_id=None, target_number=None,
        )
        await database.record_night_action(
            1, "open", "run-vote", event_seq=2, night_number=1, actor_id=100,
            actor_number=1, actor_role="占い師", action="占い",
            target_id=None, target_number=None, result=None,
        )
        # 例外が出ないことと、後段のgame_id後埋めで拾われることを別テストで検証する。

    # ------------------------------------------------------------
    # 精算時の game_id 後埋め・二重精算の冪等性
    # ------------------------------------------------------------
    async def test_settlement_backfills_game_id_and_is_idempotent_on_double_settle(self) -> None:
        guild_id = 1
        room_id = "open"
        run_id = "run-backfill"
        player_ids = [1000 + i for i in range(5)]
        wolf_ids = {player_ids[0]}

        await database.record_co_event(
            guild_id, room_id, run_id, event_seq=1, day_number=1, phase="DAY_DISCUSSION",
            actor_id=player_ids[1], actor_number=2, event_type="CO", claimed_role="占い師",
        )
        await database.record_vote_event(
            guild_id, room_id, run_id, event_seq=2, day_number=1, vote_kind="本投票",
            round_index=0, voter_id=player_ids[1], voter_number=2,
            target_id=player_ids[0], target_number=1,
        )

        await database.stage_game_settlement(
            guild_id, room_id, run_id, room_name="総合", rated=False,
            winner_team=Team.VILLAGE.value,
            player_records=_player_records(wolf_ids=wolf_ids, player_ids=player_ids),
        )
        game_id, _results, created = await database.settle_game_settlement(
            guild_id, room_id, run_id,
        )
        self.assertTrue(created)

        events = await database.list_co_events_for_run(guild_id, room_id, run_id)
        self.assertEqual(len(events), 1)
        async with database.connect_db() as db:
            co_game_id = (await db.execute_fetchall(
                "SELECT game_id FROM game_co_events WHERE guild_id=? AND room_id=? AND game_run_id=?",
                (guild_id, room_id, run_id),
            ))[0][0]
            vote_game_id = (await db.execute_fetchall(
                "SELECT game_id FROM game_vote_events WHERE guild_id=? AND room_id=? AND game_run_id=?",
                (guild_id, room_id, run_id),
            ))[0][0]
        self.assertEqual(co_game_id, game_id)
        self.assertEqual(vote_game_id, game_id)

        # 二重精算 (起動時の未精算回収など) しても壊れない・game_idがぶれない
        game_id_2, _results_2, created_2 = await database.settle_game_settlement(
            guild_id, room_id, run_id,
        )
        self.assertFalse(created_2)
        self.assertEqual(game_id_2, game_id)
        async with database.connect_db() as db:
            row_count = (await db.execute_fetchall(
                "SELECT COUNT(*) FROM game_co_events WHERE guild_id=? AND room_id=? AND game_run_id=?",
                (guild_id, room_id, run_id),
            ))[0][0]
        self.assertEqual(row_count, 1)  # 後埋めで行が増えたり壊れたりしない

    async def test_started_at_flows_from_settlement_stage_to_games_table(self) -> None:
        guild_id = 1
        room_id = "open"
        run_id = "run-started-at"
        player_ids = [2000 + i for i in range(5)]
        started_at = "2026-08-19 10:00:00"

        await database.stage_game_settlement(
            guild_id, room_id, run_id, room_name="総合", rated=False,
            winner_team=Team.VILLAGE.value,
            player_records=_player_records(wolf_ids={player_ids[0]}, player_ids=player_ids),
            started_at=started_at,
        )
        game_id, _results, _created = await database.settle_game_settlement(
            guild_id, room_id, run_id,
        )
        async with database.connect_db() as db:
            row = (await db.execute_fetchall(
                "SELECT started_at FROM games WHERE game_id = ?", (game_id,),
            ))[0]
        self.assertEqual(row[0], started_at)

    async def test_stage_game_settlement_still_works_without_started_at(self) -> None:
        """呼び出し側 (room_runner.py) がまだ started_at を渡さない移行期間の互換。"""
        player_ids = [3000 + i for i in range(5)]
        await database.stage_game_settlement(
            1, "open", "run-no-started-at", room_name="総合", rated=False,
            winner_team=Team.VILLAGE.value,
            player_records=_player_records(wolf_ids={player_ids[0]}, player_ids=player_ids),
        )
        game_id, _results, _created = await database.settle_game_settlement(
            1, "open", "run-no-started-at",
        )
        async with database.connect_db() as db:
            row = (await db.execute_fetchall(
                "SELECT started_at FROM games WHERE game_id = ?", (game_id,),
            ))[0]
        self.assertIsNone(row[0])

    # ------------------------------------------------------------
    # 通知設定
    # ------------------------------------------------------------
    async def test_notification_prefs_default_and_toggle(self) -> None:
        default = await database.get_user_notification_prefs(1, 100)
        self.assertEqual(default, {
            "allow_notifications": True,
            "notify_on_create": False,
            "notify_on_call": False,
        })

        updated = await database.set_user_notification_prefs(
            1, 100, notify_on_call=True,
        )
        self.assertEqual(updated, {
            "allow_notifications": True,
            "notify_on_create": False,
            "notify_on_call": True,
        })
        # 部分更新: 他のキーは維持される
        again = await database.set_user_notification_prefs(1, 100, allow_notifications=False)
        self.assertEqual(again, {
            "allow_notifications": False,
            "notify_on_create": False,
            "notify_on_call": True,
        })

    async def test_call_dm_subscriber_ids_require_both_allow_and_notify_on_call(self) -> None:
        await database.set_user_notification_prefs(1, 100, notify_on_call=True)
        await database.set_user_notification_prefs(
            1, 101, notify_on_call=True, allow_notifications=False,
        )
        await database.set_user_notification_prefs(1, 102, notify_on_create=True)
        subscribers = await database.list_call_dm_subscriber_ids(1)
        self.assertEqual(subscribers, [100])

    async def _create_recruitment(self, *, host_id: int = 999) -> int:
        room_id = f"private_{host_id}"
        await database.save_private_room(1, room_id, host_id, f"GM村{host_id}")
        await database.mark_private_room_active(1, room_id, category_id=100)
        return await database.create_recruitment(
            1, host_id, title="テスト募集",
            scheduled_at=datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc),
            room_id=room_id, variant_id="v13_cross",
            streaming=False, allowed_ranks=None,
        )

    # ------------------------------------------------------------
    # 「募集」ボタンの1日1回制約と送達台帳
    # ------------------------------------------------------------
    async def test_recruitment_call_is_limited_to_once_per_day(self) -> None:
        recruitment_id = await self._create_recruitment()
        call_id = await database.open_recruitment_call(recruitment_id, 1, 999, "2026-08-19")
        self.assertIsNotNone(call_id)
        again = await database.open_recruitment_call(recruitment_id, 1, 999, "2026-08-19")
        self.assertIsNone(again)
        # 翌日は別枠として確保できる
        tomorrow = await database.open_recruitment_call(recruitment_id, 1, 999, "2026-08-20")
        self.assertIsNotNone(tomorrow)

    async def test_open_recruitment_call_raises_on_unknown_recruitment_id(self) -> None:
        """UNIQUE違反ではないFK違反 (存在しない募集) は「呼んだことにする」で握り潰さない。"""
        with self.assertRaises(Exception):
            await database.open_recruitment_call(999999, 1, 999, "2026-08-19")

    async def test_recruitment_call_delivery_tracking(self) -> None:
        recruitment_id = await self._create_recruitment()
        call_id = await database.open_recruitment_call(recruitment_id, 1, 999, "2026-08-19")
        await database.mark_recruitment_call_delivery(call_id, 100, "2026-08-19 10:00:00", "sent")
        await database.mark_recruitment_call_delivery(call_id, 101, "2026-08-19 10:00:01", "forbidden")

        pending = await database.list_pending_call_recipient_ids(call_id, [100, 101, 102])
        self.assertEqual(pending, [102])

        sent_count = await database.count_call_dms_sent_today(1, 100, "2026-08-19")
        self.assertEqual(sent_count, 1)
        forbidden_count = await database.count_call_dms_sent_today(1, 101, "2026-08-19")
        self.assertEqual(forbidden_count, 0)

        await database.set_recruitment_call_recipients(call_id, 1)
        async with database.connect_db() as db:
            row = (await db.execute_fetchall(
                "SELECT recipients FROM recruitment_calls WHERE id = ?", (call_id,),
            ))[0]
        self.assertEqual(row[0], 1)

    # ------------------------------------------------------------
    # 集計API: 0件で例外を出さないこと
    # ------------------------------------------------------------
    async def test_aggregate_apis_return_empty_without_raising_when_no_data(self) -> None:
        vote_stats = await database.get_player_vote_stats(999999, 1)
        self.assertEqual(vote_stats["total_opportunities"], 0)
        self.assertIsNone(vote_stats["participation_rate"])

        co_stats = await database.get_player_co_stats(999999, 1)
        self.assertEqual(co_stats["total_games"], 0)
        self.assertIsNone(co_stats["co_rate"])

        distribution = await database.get_co_distribution_stats(1)
        self.assertEqual(distribution, {"buckets": {}})

        duration = await database.get_game_duration_stats(1)
        self.assertEqual(duration, {"count": 0, "average_seconds": None, "median_seconds": None})

    # ------------------------------------------------------------
    # 集計API: 実データでの妥当性
    # ------------------------------------------------------------
    async def test_vote_and_co_and_duration_stats_reflect_recorded_games(self) -> None:
        guild_id = 1
        room_id = "open"
        seer_id = 4000
        wolf_id = 4001
        others = [4002, 4003, 4004]
        player_ids = [seer_id, wolf_id, *others]

        run_id = "run-agg-1"
        # 占い師が初日にCO
        await database.record_co_event(
            guild_id, room_id, run_id, event_seq=1, day_number=1, phase="DAY_DISCUSSION",
            actor_id=seer_id, actor_number=1, event_type="CO", claimed_role="占い師",
        )
        # 結果申告の行 ([結果を公開] の書き込み経路は持たないので、通常は
        # 増えない)。精算時の game_id 後埋めと、既存行が co統計へ乗り続ける
        # ことを確認するために直接INSERTしておく。
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                "INSERT INTO game_co_results "
                "(guild_id, room_id, game_run_id, event_seq, day_number, actor_id, "
                "actor_number, claimed_role, event_type, target_id, target_number, "
                "judgement) VALUES (?, ?, ?, 2, 1, ?, 1, '占い師', '公開', ?, 2, '黒')",
                (guild_id, room_id, run_id, seer_id, wolf_id),
            )
            await db.commit()
        # 占い師が本投票で狼へ投票 (=処刑先と一致・狼へ投票できた)
        await database.record_vote_event(
            guild_id, room_id, run_id, event_seq=3, day_number=1, vote_kind="本投票",
            round_index=0, voter_id=seer_id, voter_number=1,
            target_id=wolf_id, target_number=2,
        )

        records = _player_records(wolf_ids={wolf_id}, player_ids=player_ids)
        for rec in records:
            if rec["player_id"] == wolf_id:
                rec["died_on_day"] = 1
                rec["death_cause"] = "処刑"
            elif rec["player_id"] == seer_id:
                rec["role"] = Role.SEER.value
        await database.stage_game_settlement(
            guild_id, room_id, run_id, room_name="総合", rated=False,
            winner_team=Team.VILLAGE.value, player_records=records,
            started_at="2026-08-19 10:00:00",
        )
        await database.settle_game_settlement(guild_id, room_id, run_id)

        # played_at はstage/settle実行時刻 (CURRENT_TIMESTAMP) なので、
        # started_atより後になり duration>=0 になるはず。
        vote_stats = await database.get_player_vote_stats(seer_id, guild_id)
        self.assertEqual(vote_stats["total_opportunities"], 1)
        self.assertEqual(vote_stats["participation_rate"], 1.0)
        self.assertEqual(vote_stats["execution_match_rate"], 1.0)
        self.assertEqual(vote_stats["wolf_target_rate"], 1.0)

        co_stats = await database.get_player_co_stats(seer_id, guild_id)
        self.assertEqual(co_stats["total_games"], 1)
        self.assertEqual(co_stats["co_rate"], 1.0)
        self.assertEqual(co_stats["day1_co_rate"], 1.0)
        self.assertEqual(co_stats["true_seer_co_count"], 1)
        self.assertEqual(co_stats["fake_co_count"], 0)
        self.assertEqual(co_stats["result_claim_count"], 1)

        distribution = await database.get_co_distribution_stats(guild_id)
        self.assertIn("1", distribution["buckets"])
        self.assertEqual(distribution["buckets"]["1"]["games"], 1)
        self.assertEqual(distribution["buckets"]["1"]["village_win_rate"], 1.0)

        duration = await database.get_game_duration_stats(guild_id)
        self.assertEqual(duration["count"], 1)
        self.assertIsNotNone(duration["average_seconds"])
        self.assertGreaterEqual(duration["average_seconds"], 0)

    async def test_fake_co_is_detected_when_claimed_role_differs_from_actual_role(self) -> None:
        guild_id = 1
        room_id = "open"
        madman_id = 5000
        player_ids = [madman_id, 5001, 5002, 5003, 5004]
        run_id = "run-fake-co"
        await database.record_co_event(
            guild_id, room_id, run_id, event_seq=1, day_number=1, phase="DAY_DISCUSSION",
            actor_id=madman_id, actor_number=1, event_type="CO", claimed_role="占い師",
        )
        records = _player_records(wolf_ids=set(), player_ids=player_ids)
        for rec in records:
            if rec["player_id"] == madman_id:
                rec["role"] = "狂人"
                rec["team"] = Team.WOLF.value
                rec["won"] = 0
        await database.stage_game_settlement(
            guild_id, room_id, run_id, room_name="総合", rated=False,
            winner_team=Team.VILLAGE.value, player_records=records,
        )
        await database.settle_game_settlement(guild_id, room_id, run_id)

        co_stats = await database.get_player_co_stats(madman_id, guild_id)
        self.assertEqual(co_stats["fake_co_count"], 1)
        self.assertEqual(co_stats["fake_co_win_rate"], 0.0)
        self.assertEqual(co_stats["true_seer_co_count"], 0)


if __name__ == "__main__":
    unittest.main()
