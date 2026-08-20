"""GMが昼の議論を打ち切って投票へ進める操作の回帰契約。"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from config import Phase, Role, RoomDefinition
from models import Player
from room_runner import RoomRunner, StateDurabilityError
from views import GMControlView


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
        RoomDefinition("skip-test", "スキップ卓", variant_id=variant_id),
    )
    runner.state.game_run_id = "run-skip"
    runner.state.phase = Phase.DAY_DISCUSSION
    runner.state.day_number = 1
    runner.state.day_generation = 1
    runner.state.pause_event.set()
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
    runner.state.gm_id = players[0].user_id
    return players


class DayDiscussionSkipContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_crosstalk_skip_records_generation_and_wakes_countdown(self) -> None:
        runner = make_runner("v9_cross")
        players = add_players(runner, 9)

        result = await runner.force_skip_wait(players[0].member)

        self.assertIn("投票へ進みます", result)
        self.assertTrue(runner._day_discussion_skipped())
        self.assertTrue(runner.state.day_discussion_skip_event.is_set())
        self.assertEqual(runner.state.day_discussion_skip_generation, 1)
        # クロストークにはターン枠が無いので発言終了は立てない。
        self.assertFalse(runner.state.turn_done_event.is_set())
        runner._persist_room_state.assert_awaited()

    async def test_turn_skip_also_closes_the_current_speech_slot(self) -> None:
        runner = make_runner("v9_turn")
        players = add_players(runner, 9)

        result = await runner.force_skip_wait(players[0].member)

        self.assertIn("投票へ進みます", result)
        self.assertTrue(runner.state.day_discussion_skip_event.is_set())
        self.assertTrue(runner.state.turn_done_event.is_set())
        self.assertTrue(runner.state.turn_signal_event.is_set())

    async def test_second_press_is_rejected_without_touching_state(self) -> None:
        runner = make_runner("v13_cross")
        players = add_players(runner, 13)
        await runner.force_skip_wait(players[0].member)
        runner._persist_room_state.reset_mock()

        result = await runner.force_skip_wait(players[0].member)

        self.assertIn("受付済み", result)
        runner._persist_room_state.assert_not_awaited()

    async def test_save_failure_rolls_back_and_keeps_discussion_running(self) -> None:
        runner = make_runner("v9_cross")
        players = add_players(runner, 9)
        runner._persist_room_state = AsyncMock(side_effect=RuntimeError("db down"))

        result = await runner.force_skip_wait(players[0].member)

        self.assertIn("保存できませんでした", result)
        self.assertIsNone(runner.state.day_discussion_skip_generation)
        self.assertFalse(runner.state.day_discussion_skip_event.is_set())

    async def test_paused_game_is_not_skippable(self) -> None:
        runner = make_runner("v9_cross")
        players = add_players(runner, 9)
        runner.state.paused = True

        result = await runner.force_skip_wait(players[0].member)

        self.assertIn("一時停止中", result)
        self.assertIsNone(runner.state.day_discussion_skip_generation)

    async def test_skip_only_applies_to_the_day_it_was_pressed(self) -> None:
        runner = make_runner("v9_cross")
        players = add_players(runner, 9)
        await runner.force_skip_wait(players[0].member)

        runner.state.day_number = 2
        runner.state.day_generation = 2
        runner._sync_day_discussion_skip_event()

        self.assertFalse(runner._day_discussion_skipped())
        self.assertFalse(runner.state.day_discussion_skip_event.is_set())


class DayDiscussionSkipDurabilityTest(unittest.IsolatedAsyncioTestCase):
    def _restorable(self, payload: dict) -> RoomRunner:
        """GAME_OVER回収経路だけを動かして、scalar復元の契約を確かめる。"""
        restored = make_runner("v9_turn")
        restored.state.guild = SimpleNamespace(
            id=1,
            roles=[],
            get_member=lambda _user_id: None,
            get_channel=lambda _channel_id: None,
        )
        restored._close_game_views_for_shutdown = AsyncMock()
        restored._restore_nicknames = AsyncMock(return_value={})
        restored._teardown_game_roles_and_perms = AsyncMock()
        restored._transition_to_empty_lobby = AsyncMock(return_value=True)
        payload["phase"] = Phase.GAME_OVER.name
        return restored

    async def test_snapshot_round_trip_keeps_the_skipped_day(self) -> None:
        runner = make_runner("v9_turn")
        players = add_players(runner, 9)
        await runner.force_skip_wait(players[0].member)
        payload = runner._build_room_snapshot()
        self.assertEqual(payload["day_discussion_skip_generation"], 1)

        restored = self._restorable(payload)
        await restored.restore_from_snapshot(payload)

        self.assertEqual(restored.state.day_discussion_skip_generation, 1)

    async def test_broken_skip_generation_is_rejected(self) -> None:
        runner = make_runner("v9_turn")
        payload = runner._build_room_snapshot()
        restored = self._restorable(payload)
        payload["day_discussion_skip_generation"] = -1

        with self.assertRaises(StateDurabilityError):
            await restored.restore_from_snapshot(payload)


class DayDiscussionSkipProgressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_turn_discussion_stops_issuing_speakers_after_skip(self) -> None:
        runner = make_runner("v9_turn")
        add_players(runner, 9)
        state = runner.state
        state.day_discussion_skip_generation = state.day_generation
        runner._lock_village = AsyncMock()
        runner._repost_gm_panel = AsyncMock(return_value=True)
        runner.post_village_panel = AsyncMock()
        runner._play_se = Mock()
        runner._run_main_turn = AsyncMock(return_value=None)

        await runner._turn_day_discussion()

        runner._run_main_turn.assert_not_awaited()
        announced = [
            call.args[0] for call in runner._safe_village_send.await_args_list
        ]
        self.assertTrue(any("GMの操作でターン制議論を終了" in text for text in announced))

    async def test_turn_discussion_runs_every_slot_without_skip(self) -> None:
        runner = make_runner("v9_turn")
        add_players(runner, 9)
        runner._lock_village = AsyncMock()
        runner._repost_gm_panel = AsyncMock(return_value=True)
        runner.post_village_panel = AsyncMock()
        runner._play_se = Mock()
        runner._run_main_turn = AsyncMock(return_value=None)

        await runner._turn_day_discussion()

        # 9人 × 初日2巡。スキップ分岐が通常進行を削っていないことを固定する。
        self.assertEqual(runner._run_main_turn.await_count, 18)

    async def test_crosstalk_countdown_receives_the_skip_event(self) -> None:
        runner = make_runner("v9_cross")
        add_players(runner, 9)
        runner._unmute_alive = AsyncMock(return_value=[])
        runner._wait_for_mute_sync_or_pause = AsyncMock()
        runner._pausable_sleep = AsyncMock()
        runner._play_se = Mock()
        runner.post_village_panel = AsyncMock()
        runner._repost_gm_panel = AsyncMock()
        runner._lock_village = AsyncMock()
        runner._mute_phase = AsyncMock()
        runner._pausable_countdown = AsyncMock(return_value=True)

        await runner._day_discussion()

        self.assertIs(
            runner._pausable_countdown.await_args.args[3],
            runner.state.day_discussion_skip_event,
        )
        announced = [
            call.args[0] for call in runner._safe_village_send.await_args_list
        ]
        self.assertTrue(any("GMの操作で議論を終了" in text for text in announced))


class DayDiscussionSkipButtonTest(unittest.IsolatedAsyncioTestCase):
    def _cog(self, *, skipped: bool) -> SimpleNamespace:
        state = SimpleNamespace(
            game_run_id="run-skip",
            phase=Phase.DAY_DISCUSSION,
            gm_id=1,
            paused=False,
            ending=False,
            pending_winner=None,
            day_generation=1,
            day_discussion_skip_generation=1 if skipped else None,
            current_speaker_id=None,
            vote_closed=False,
            vote_slot_active=False,
            vote_speech_finished=False,
            initial_night_completed=False,
        )
        return SimpleNamespace(
            state=state,
            register_game_view=lambda _view: None,
            is_private_room=lambda: False,
            uses_sequential_vote=lambda: True,
            _effective_phase=lambda: Phase.DAY_DISCUSSION,
        )

    def test_skip_button_is_labelled_for_the_discussion_phase(self) -> None:
        view = GMControlView(self._cog(skipped=False))
        self.assertEqual(view.skip_wait_btn.label, "議論終了")
        self.assertFalse(view.skip_wait_btn.disabled)

    def test_skip_button_is_disabled_once_the_day_is_skipped(self) -> None:
        view = GMControlView(self._cog(skipped=True))
        self.assertEqual(view.skip_wait_btn.label, "議論終了")
        self.assertTrue(view.skip_wait_btn.disabled)


if __name__ == "__main__":
    unittest.main()
