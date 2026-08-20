"""ターン制「割り込み」の統計記録 (game_turn_events) の回帰テスト。

対象は割り込みの受理のみ。パス・発言時間・遺言・途中抜けは本人判断で
「ゲームへの関係が薄い」として対象外 (今回の仕事の依頼書きどおり)。
DBの記録経路は tests/test_vote_night_action_records.py と同じ作法
(database.record_* を patch して呼び出し引数を検証する) に倣う。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
from config import Phase, Role, RoomDefinition, Team
from models import Player
from room_runner import RoomRunner


class FakeManager:
    def __init__(self) -> None:
        self.discord_api_sem = asyncio.Semaphore(20)
        self.start_lock = asyncio.Lock()
        self.rating_lock = asyncio.Lock()
        self.join_lock = asyncio.Lock()

    async def paced_discord_api_call(self, func, *args, **kwargs):
        async with self.discord_api_sem:
            return await func(*args, **kwargs)


class FakeMember:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.display_name = f"user-{user_id}"
        self.nick = None
        self.roles = []
        self.voice = None


def make_runner(variant_id: str) -> RoomRunner:
    runner = RoomRunner(
        None,
        FakeManager(),
        RoomDefinition("turn-record", "割り込み記録村", variant_id=variant_id),
    )
    state = runner.state
    state.game_run_id = "run-interrupt"
    state.guild = SimpleNamespace(id=777)
    state.room_id = "turn-record"
    state.phase = Phase.DAY_DISCUSSION
    state.day_number = 1
    state.day_generation = 1
    state.pause_event.set()
    runner._persist_room_state = AsyncMock()
    runner._safe_village_send = AsyncMock(return_value=None)
    return runner


def add_players(runner: RoomRunner, count: int) -> list[Player]:
    players = []
    for number in range(1, count + 1):
        member = FakeMember(number)
        player = Player(
            user_id=number,
            member=member,
            role=Role.VILLAGER,
            alive=True,
            number=number,
            base_name=member.display_name,
        )
        runner.state.players[number] = player
        players.append(player)
    return players


class TurnInterruptRecordingTest(unittest.IsolatedAsyncioTestCase):
    """ターン制variant (v9_turn) で割り込みが受理APIから1行追記されること。"""

    def setUp(self) -> None:
        self.runner = make_runner("v9_turn")
        add_players(self.runner, 9)
        state = self.runner.state
        state.turn_day_generation = state.day_generation
        state.turn_order = list(range(1, 10))
        state.turn_slot_active = True
        state.turn_window_open = True
        state.current_speaker_id = 1
        state.turn_slot_token = 8

    async def test_accepted_interrupt_records_actor_and_speaker(self) -> None:
        with patch(
            "room_runner.database.record_turn_event", new=AsyncMock()
        ) as record:
            error, remaining = await self.runner.request_turn_interrupt(2, 8)

        self.assertIsNone(error)
        record.assert_awaited_once()
        args, kwargs = record.await_args
        self.assertEqual(args[:3], (777, "turn-record", "run-interrupt"))
        self.assertEqual(kwargs["event_type"], "割り込み")
        self.assertEqual(kwargs["actor_id"], 2)
        self.assertEqual(kwargs["actor_number"], 2)
        self.assertEqual(kwargs["speaker_id"], 1)
        self.assertEqual(kwargs["speaker_number"], 1)
        self.assertEqual(kwargs["day_number"], 1)
        self.assertGreaterEqual(kwargs["event_seq"], 1)

    async def test_event_seq_shared_and_monotonic_with_co_log(self) -> None:
        """event_seq は CO・投票・夜行動ログと共有のカウンタを使う。"""
        state = self.runner.state
        state.record_event_seq = 5

        with patch(
            "room_runner.database.record_turn_event", new=AsyncMock()
        ) as record:
            await self.runner.request_turn_interrupt(2, 8)

        self.assertEqual(record.await_args.kwargs["event_seq"], 6)
        self.assertEqual(state.record_event_seq, 6)

    async def test_rejected_interrupt_does_not_record(self) -> None:
        """割り込み上限を使い切っている等で拒否された場合は記録しない。"""
        self.runner.state.turn_interrupt_active = True
        with patch(
            "room_runner.database.record_turn_event", new=AsyncMock()
        ) as record:
            error, _remaining = await self.runner.request_turn_interrupt(2, 8)

        self.assertIsNotNone(error)
        record.assert_not_awaited()

    async def test_db_failure_during_interrupt_does_not_block_acceptance(self) -> None:
        """DB書き込みが失敗しても割り込み自体は成立すること。"""
        with patch(
            "room_runner.database.record_turn_event",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            error, remaining = await self.runner.request_turn_interrupt(2, 8)

        self.assertIsNone(error)
        self.assertEqual(self.runner.state.turn_interrupts_used, 1)
        self.assertEqual(self.runner.state.turn_interrupt_pending_id, 2)
        self.assertEqual(remaining, 0)

    async def test_persist_failure_rolls_back_record_event_seq(self) -> None:
        """state保存自体が失敗した場合はseqも巻き戻り、記録APIは呼ばれない。"""
        state = self.runner.state
        before_seq = state.record_event_seq
        self.runner._persist_room_state = AsyncMock(side_effect=RuntimeError("save down"))

        with patch(
            "room_runner.database.record_turn_event", new=AsyncMock()
        ) as record:
            error, _remaining = await self.runner.request_turn_interrupt(2, 8)

        self.assertIsNotNone(error)
        self.assertEqual(state.record_event_seq, before_seq)
        record.assert_not_awaited()

class CrosstalkVillageHasNoInterruptTest(unittest.IsolatedAsyncioTestCase):
    """クロストーク村 (v13_cross) には割り込み機能自体が無く、記録もされない。"""

    async def test_crosstalk_variant_rejects_interrupt_and_never_records(self) -> None:
        runner = make_runner("v13_cross")
        add_players(runner, 5)
        runner.state.current_speaker_id = 1

        with patch(
            "room_runner.database.record_turn_event", new=AsyncMock()
        ) as record:
            error, remaining = await runner.request_turn_interrupt(2, 8)

        self.assertIsNotNone(error)
        self.assertEqual(remaining, 0)
        record.assert_not_awaited()


class RecordTurnEventDatabaseTest(unittest.IsolatedAsyncioTestCase):
    """database.record_turn_event / get_player_interrupt_stats の単体テスト。

    tests/test_record_tables.py と同じ雛形 (一時DB) を使う。
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-turn-events-test-")
        self._old_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "turn_events.db")
        await database.init_db()

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._old_path
        self._tmp.cleanup()

    async def test_migration_creates_game_turn_events_and_passes_validation(self) -> None:
        async with database.connect_db() as db:
            tables = {
                row[0] for row in await db.execute_fetchall(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("game_turn_events", tables)
        # 検証を通ったうえで再実行しても壊れない (冪等)。
        await database.init_db()

    async def test_record_turn_event_appends_row_with_speaker(self) -> None:
        await database.record_turn_event(
            1, "open", "run-turn", event_seq=1, day_number=1, event_type="割り込み",
            actor_id=100, actor_number=1, speaker_id=200, speaker_number=2,
        )
        async with database.connect_db() as db:
            rows = await db.execute_fetchall(
                "SELECT event_type, actor_id, actor_number, speaker_id, speaker_number "
                "FROM game_turn_events WHERE guild_id=? AND room_id=? AND game_run_id=?",
                (1, "open", "run-turn"),
            )
        self.assertEqual(rows, [("割り込み", 100, 1, 200, 2)])

    def _player_records(self, *, wolf_ids: set[int], player_ids: list[int]) -> list[dict]:
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

    async def test_settlement_backfills_game_id_for_turn_events(self) -> None:
        guild_id = 1
        room_id = "open"
        run_id = "run-turn-backfill"
        player_ids = [1000 + i for i in range(5)]

        await database.record_turn_event(
            guild_id, room_id, run_id, event_seq=1, day_number=1, event_type="割り込み",
            actor_id=player_ids[1], actor_number=2,
            speaker_id=player_ids[0], speaker_number=1,
        )
        await database.stage_game_settlement(
            guild_id, room_id, run_id, room_name="総合", rated=False,
            winner_team=Team.VILLAGE.value,
            player_records=self._player_records(
                wolf_ids={player_ids[0]}, player_ids=player_ids,
            ),
        )
        game_id, _results, created = await database.settle_game_settlement(
            guild_id, room_id, run_id,
        )
        self.assertTrue(created)

        async with database.connect_db() as db:
            row = (await db.execute_fetchall(
                "SELECT game_id FROM game_turn_events WHERE guild_id=? AND room_id=? "
                "AND game_run_id=?",
                (guild_id, room_id, run_id),
            ))[0]
        self.assertEqual(row[0], game_id)

    async def test_get_player_interrupt_stats_empty_does_not_raise(self) -> None:
        stats = await database.get_player_interrupt_stats(999, 1, "v9_turn")
        self.assertEqual(stats, {
            "total_games": 0,
            "interrupt_count": 0,
            "interrupts_per_game": None,
            "interrupted_count": 0,
        })

    async def test_get_player_interrupt_stats_counts_interrupts_and_interrupted(self) -> None:
        guild_id = 1
        room_id = "open"
        run_id = "run-turn-stats"
        player_ids = [4000 + i for i in range(5)]

        # player_ids[1] が player_ids[0] の発言枠へ2回割り込む。
        await database.record_turn_event(
            guild_id, room_id, run_id, event_seq=1, day_number=1, event_type="割り込み",
            actor_id=player_ids[1], actor_number=2,
            speaker_id=player_ids[0], speaker_number=1,
        )
        await database.record_turn_event(
            guild_id, room_id, run_id, event_seq=2, day_number=1, event_type="割り込み",
            actor_id=player_ids[1], actor_number=2,
            speaker_id=player_ids[0], speaker_number=1,
        )
        await database.stage_game_settlement(
            guild_id, room_id, run_id, room_name="総合", rated=False,
            winner_team=Team.VILLAGE.value,
            player_records=self._player_records(
                wolf_ids={player_ids[0]}, player_ids=player_ids,
            ),
            variant_id="v9_turn",
        )
        await database.settle_game_settlement(guild_id, room_id, run_id)

        interrupter_stats = await database.get_player_interrupt_stats(
            player_ids[1], guild_id, "v9_turn",
        )
        self.assertEqual(interrupter_stats["total_games"], 1)
        self.assertEqual(interrupter_stats["interrupt_count"], 2)
        self.assertEqual(interrupter_stats["interrupts_per_game"], 2.0)
        self.assertEqual(interrupter_stats["interrupted_count"], 0)

        target_stats = await database.get_player_interrupt_stats(
            player_ids[0], guild_id, "v9_turn",
        )
        self.assertEqual(target_stats["interrupt_count"], 0)
        self.assertEqual(target_stats["interrupted_count"], 2)


if __name__ == "__main__":
    unittest.main()
