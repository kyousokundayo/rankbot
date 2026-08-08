"""ターン制議論の順序・競合・復旧cursorの回帰テスト。"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from config import Phase, Role, RoomDefinition, Team, get_variant_definition
from models import GameState, Player
from room_runner import RoomRunner, StateDurabilityError, turn_timer_should_update
from views import TurnSpeechView, build_help_embeds, build_rule_embeds


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


def make_runner(variant_id: str = "v13_turn") -> RoomRunner:
    runner = RoomRunner(
        None,
        FakeManager(),
        RoomDefinition("turn-test", "ターンテスト", variant_id=variant_id),
    )
    runner.state.game_run_id = "run-turn"
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
    return players


class TurnOrderTest(unittest.IsolatedAsyncioTestCase):
    async def test_day_one_has_two_rounds_and_later_days_have_one(self) -> None:
        runner = make_runner()
        self.assertEqual(runner._turn_round_durations(), (50, 80))
        runner.state.day_number = 2
        self.assertEqual(runner._turn_round_durations(), (90,))

    async def test_clockwise_order_wraps_and_dead_anchor_is_skipped_at_runtime(self) -> None:
        runner = make_runner()
        players = add_players(runner, 13)
        order = runner._build_turn_order(12)
        self.assertEqual(order[:4], [12, 13, 1, 2])

        players[11].alive = False
        living_order = [
            player_id
            for player_id in runner._build_turn_order(12)
            if runner.state.get_player(player_id).alive
        ]
        self.assertEqual(living_order[:3], [13, 1, 2])

    async def test_saved_night_anchor_is_used_and_no_action_chooses_random(self) -> None:
        runner = make_runner()
        players = add_players(runner, 13)
        runner.state.day_number = 2
        runner.state.day_generation = 2
        runner.state.next_turn_anchor_number = 4

        initialized = await runner._initialize_turn_day()

        self.assertTrue(initialized)
        self.assertEqual(runner.state.turn_anchor_number, 4)
        self.assertEqual(runner.state.turn_order[0], 4)

        runner.state.day_generation = 3
        runner.state.day_number = 3
        runner.state.next_turn_anchor_number = None
        with patch("room_runner.secrets.choice", return_value=players[6]):
            await runner._initialize_turn_day()
        self.assertEqual(runner.state.turn_anchor_number, 7)

    async def test_saved_dead_seat_anchor_survives_member_leaving_during_restart(self) -> None:
        runner = make_runner("v9_turn")
        add_players(runner, 9)
        runner.state.day_number = 2
        runner.state.day_generation = 2
        runner.state.next_turn_anchor_number = 4
        # 襲撃死後、Bot再起動中に本人がguildを退出し、復元対象から外れた状態。
        runner.state.players.pop(4)

        with patch("room_runner.secrets.choice") as choose:
            initialized = await runner._initialize_turn_day()

        self.assertTrue(initialized)
        choose.assert_not_called()
        self.assertEqual(runner.state.turn_anchor_number, 4)
        self.assertEqual(runner.state.turn_order[:3], [5, 6, 7])

    async def test_night_result_records_kill_gj_or_random_anchor_source(self) -> None:
        for wolf_target, guard_target, expected_anchor in (
            (4, None, 4),
            (4, 4, 4),
            (None, None, None),
            (-1, None, None),
        ):
            with self.subTest(wolf_target=wolf_target, guard_target=guard_target):
                runner = make_runner()
                add_players(runner, 13)
                runner.state.phase = Phase.NIGHT
                runner.state.wolf_target = wolf_target
                runner.state.guard_target = guard_target
                runner._execute_player = AsyncMock()

                await runner._process_night()

                self.assertEqual(
                    runner.state.next_turn_anchor_number, expected_anchor
                )

    async def test_completed_slots_are_not_replayed_but_active_slot_uses_full_duration(self) -> None:
        runner = make_runner("v9_turn")
        add_players(runner, 9)
        state = runner.state
        state.turn_day_generation = state.day_generation
        state.turn_order = list(range(1, 10))
        state.turn_round_index = 0
        # 01〜03は完了済み、04の途中で再起動した状態。
        state.turn_slot_index = 3
        state.turn_slot_active = True
        state.turn_remaining_seconds = 12
        runner._lock_village = AsyncMock()
        runner._repost_gm_panel = AsyncMock(return_value=True)
        runner._play_se = Mock()
        runner._run_main_turn = AsyncMock(return_value=None)

        await runner._turn_day_discussion()

        calls = runner._run_main_turn.await_args_list
        self.assertEqual(calls[0].args[0].user_id, 4)
        self.assertEqual(calls[0].args[1], 50)
        self.assertEqual([call.args[1] for call in calls], [50] * 6 + [80] * 9)
        self.assertEqual(state.turn_round_index, 2)
        self.assertEqual(state.turn_slot_index, 0)


class TurnActionRaceTest(unittest.IsolatedAsyncioTestCase):
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

    async def test_only_current_speaker_can_pass_and_old_token_is_rejected(self) -> None:
        error = await self.runner.request_turn_pass(2, 1, 8)
        self.assertIn("現在の話者本人", error)
        error = await self.runner.request_turn_pass(1, 1, 7)
        self.assertIn("既に終了", error)

        self.assertIsNone(await self.runner.request_turn_pass(1, 1, 8))
        self.assertTrue(self.runner.state.turn_done_event.is_set())
        self.assertTrue(self.runner.state.turn_signal_event.is_set())

    async def test_simultaneous_interrupts_accept_exactly_one(self) -> None:
        results = await asyncio.gather(
            self.runner.request_turn_interrupt(2, 8),
            self.runner.request_turn_interrupt(3, 8),
        )

        accepted = [result for result in results if result[0] is None]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(self.runner.state.turn_interrupts_used, 1)
        self.assertIn(self.runner.state.turn_interrupt_pending_id, {2, 3})
        self.runner._persist_room_state.assert_awaited_once()

    async def test_interrupt_persisting_at_deadline_wins_before_timeout(self) -> None:
        """受付保存中に0秒になっても、成功した割り込みを取りこぼさない。"""
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_persist() -> None:
            entered.set()
            await release.wait()

        self.runner._persist_room_state = blocked_persist
        request = asyncio.create_task(self.runner.request_turn_interrupt(2, 8))
        await entered.wait()
        countdown = asyncio.create_task(
            self.runner._turn_segment_countdown(
                None,
                self.runner.state.get_player(1),
                0.01,
                allow_interrupt=True,
            )
        )
        # countdownは時間切れになっても、受付のDB確定を待っている。
        await asyncio.sleep(0.03)
        self.assertFalse(countdown.done())

        release.set()
        accepted = await request
        outcome, _, _ = await countdown

        self.assertEqual(accepted, (None, 0))
        self.assertEqual(outcome, "interrupt")
        self.assertEqual(self.runner.state.turn_interrupts_used, 1)
        self.assertFalse(self.runner.state.turn_window_open)

    async def test_late_old_snapshot_cannot_overwrite_accepted_interrupt(self) -> None:
        """VC復帰の古い保存より、受付済み割り込みを必ず後に保存する。"""
        runner = self.runner
        state = runner.state
        state.guild = SimpleNamespace(id=123)
        runner._persist_room_state = RoomRunner._persist_room_state.__get__(
            runner, RoomRunner
        )
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        snapshots: list[tuple[object, int]] = []
        stored: tuple[object, int] | None = None

        async def fake_save(_guild_id, _room_id, _phase, payload):
            nonlocal stored
            snapshot = (
                payload["turn_interrupt_pending_id"],
                payload["turn_interrupts_used"],
            )
            snapshots.append(snapshot)
            if len(snapshots) == 1:
                first_entered.set()
                await release_first.wait()
            stored = snapshot

        with patch("room_runner.database.save_room_state", side_effect=fake_save):
            old_save = asyncio.create_task(runner._persist_room_state())
            await first_entered.wait()
            accepted = asyncio.create_task(runner.request_turn_interrupt(2, 8))
            await asyncio.sleep(0)
            self.assertFalse(accepted.done())

            release_first.set()
            await old_save
            result = await accepted

        self.assertEqual(result, (None, 0))
        self.assertEqual(snapshots, [(None, 0), (2, 1)])
        self.assertEqual(stored, (2, 1))

    async def test_waiting_save_uses_one_consistent_replacement_state(self) -> None:
        """reset待機中もDBキーとpayloadを別世代から混ぜない。"""
        runner = self.runner
        runner.state.guild = SimpleNamespace(id=123)
        runner._persist_room_state = RoomRunner._persist_room_state.__get__(
            runner, RoomRunner
        )
        captured = None

        async def fake_save(guild_id, room_id, phase, payload):
            nonlocal captured
            captured = (
                guild_id,
                room_id,
                phase,
                payload["room_name"],
                payload["day_number"],
            )

        await runner.state_persist_lock.acquire()
        try:
            with patch("room_runner.database.save_room_state", side_effect=fake_save):
                waiting = asyncio.create_task(runner._persist_room_state())
                await asyncio.sleep(0)

                replacement = GameState()
                replacement.guild = SimpleNamespace(id=456)
                replacement.room_id = "replacement"
                replacement.room_name = "差替後"
                replacement.phase = Phase.DAY_VOTE
                replacement.day_number = 4
                runner.state = replacement

                runner.state_persist_lock.release()
                await waiting
        finally:
            if runner.state_persist_lock.locked():
                runner.state_persist_lock.release()

        self.assertEqual(
            captured,
            (456, "replacement", "DAY_VOTE", "差替後", 4),
        )

    async def test_terminal_outcomes_hide_finished_speaker_from_voice_join_sync(self) -> None:
        """window close後のVC入室同期が終了話者を再unmuteしない。"""
        for expected in ("done", "interrupt", "timeout"):
            with self.subTest(expected=expected):
                runner = make_runner("v9_turn")
                add_players(runner, 2)
                state = runner.state
                state.turn_slot_active = True
                state.turn_window_open = True
                state.current_speaker_id = 1
                if expected == "done":
                    state.turn_done_event.set()
                    seconds = 10
                elif expected == "interrupt":
                    state.turn_interrupt_pending_id = 2
                    state.turn_interrupt_event.set()
                    seconds = 10
                else:
                    seconds = 0

                outcome, _, _ = await runner._turn_segment_countdown(
                    None,
                    state.get_player(1),
                    seconds,
                    allow_interrupt=True,
                )

                self.assertEqual(outcome, expected)
                self.assertFalse(state.turn_window_open)
                self.assertFalse(state.turn_slot_active)
                # on_voice_state_updateはこの集合で同期するため、終了話者は復活しない。
                self.assertEqual(runner._current_speaker_ids(), set())

    async def test_external_win_can_cancel_countdown_waiting_for_action_lock(self) -> None:
        """除外側がaction_lock保持中でも、ゲームタスクcancelを待ってdeadlockしない。"""
        runner = make_runner("v9_turn")
        speaker = add_players(runner, 1)[0]
        state = runner.state
        state.turn_slot_active = True
        state.turn_window_open = True
        state.current_speaker_id = speaker.user_id
        runner._end_game = AsyncMock()

        async with runner.action_lock:
            state.game_task = asyncio.create_task(
                runner._turn_segment_countdown(
                    None, speaker, 0, allow_interrupt=True
                )
            )
            # countdownが時間切れ確定のaction_lock待ちへ入る。
            await asyncio.sleep(0)
            self.assertFalse(state.game_task.done())
            await asyncio.wait_for(
                runner._finish_game_externally(Team.VILLAGE), timeout=0.2
            )

        self.assertTrue(state.game_task.cancelled())
        runner._end_game.assert_awaited_once_with(Team.VILLAGE)

    async def test_nested_interrupt_is_rejected(self) -> None:
        self.runner.state.turn_interrupt_active = True
        error, remaining = await self.runner.request_turn_interrupt(2, 8)
        self.assertIn("さらに割り込む", error)
        self.assertEqual(remaining, 1)

    async def test_gm_force_next_does_not_consume_an_accepted_interrupt(self) -> None:
        self.runner.state.gm_id = 9
        self.runner.state.turn_interrupt_pending_id = 2
        self.runner.state.turn_interrupt_event.set()

        error = await self.runner.force_next_turn(9, 8)

        self.assertIn("割り込みを受け付け済み", error)
        self.assertFalse(self.runner.state.turn_done_event.is_set())

    async def test_original_timer_resumes_with_remaining_seconds(self) -> None:
        speaker = self.runner.state.get_player(1)
        message = object()
        view = Mock()
        self.runner._begin_turn_segment = AsyncMock(
            side_effect=[(message, view), (message, view)]
        )
        self.runner._turn_segment_countdown = AsyncMock(
            side_effect=[
                ("interrupt", 17.0, message),
                ("done", 9.0, message),
            ]
        )
        self.runner._run_turn_interrupt = AsyncMock(return_value=message)
        self.runner._clear_speaker = AsyncMock()

        await self.runner._run_main_turn(speaker, 50, message)

        first, second = self.runner._begin_turn_segment.await_args_list
        self.assertEqual(first.args[1], 50.0)
        self.assertEqual(second.args[1], 17.0)
        self.runner._run_turn_interrupt.assert_awaited_once_with(
            speaker, 17.0, message
        )


class TurnCoDeclarationTest(unittest.IsolatedAsyncioTestCase):
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

    async def test_co_window_is_second_round_on_day_one_and_only_round_later(self) -> None:
        state = self.runner.state
        self.assertFalse(self.runner.turn_co_declaration_open())

        state.turn_round_index = 1
        self.assertTrue(self.runner.turn_co_declaration_open())

        state.turn_interrupt_active = True
        self.assertFalse(self.runner.turn_co_declaration_open())
        state.turn_interrupt_active = False

        state.day_number = 2
        state.day_generation = 2
        state.turn_round_index = 0
        self.assertTrue(self.runner.turn_co_declaration_open())
        state.turn_round_index = 1
        self.assertFalse(self.runner.turn_co_declaration_open())

    async def test_co_is_persisted_once_per_day_and_requires_vc_return(self) -> None:
        state = self.runner.state
        state.turn_round_index = 1

        first, duplicate = await asyncio.gather(
            self.runner.request_turn_co_declaration(2, 8),
            self.runner.request_turn_co_declaration(2, 8),
        )

        self.assertEqual([first, duplicate].count(None), 1)
        self.assertTrue(any("既に宣言済み" in result for result in (first, duplicate) if result))
        self.assertEqual(
            state.turn_co_declarations,
            [{"user_id": 2, "number": 2, "display_name": "02.user-2"}],
        )
        self.runner._persist_room_state.assert_awaited_once()

        state.disconnected_players.add(3)
        error = await self.runner.request_turn_co_declaration(3, 8)
        self.assertIn("VCへ復帰", error)
        self.assertEqual(len(state.turn_co_declarations), 1)

        error = await self.runner.request_turn_co_declaration(4, 7)
        self.assertIn("この発言枠", error)

    async def test_co_save_failure_rolls_back(self) -> None:
        state = self.runner.state
        state.turn_round_index = 1
        self.runner._persist_room_state = AsyncMock(side_effect=RuntimeError("db down"))

        error = await self.runner.request_turn_co_declaration(2, 8)

        self.assertIn("保存できません", error)
        self.assertEqual(state.turn_co_declarations, [])


class TurnDurabilityAndSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_end_checkpoint_is_valid_for_restart(self) -> None:
        runner = make_runner("v9_turn")
        players = add_players(runner, 9)
        state = runner.state
        state.turn_day_generation = state.day_generation
        state.turn_anchor_number = 1
        state.turn_order = list(range(1, 10))
        state.turn_round_index = 0
        state.turn_slot_index = 0
        state.turn_slot_active = True
        state.current_speaker_id = 1
        state.turn_interrupt_pending_id = 2
        state.turn_interrupts_used = 1
        message = object()
        runner._grant_turn_speaker = AsyncMock()
        runner._replace_turn_message = AsyncMock(return_value=message)
        runner._turn_segment_countdown = AsyncMock(
            return_value=("done", 0.0, message)
        )
        runner._clear_speaker = AsyncMock()

        await runner._run_turn_interrupt(players[0], 17.0, message)

        self.assertFalse(state.turn_slot_active)
        self.assertEqual(state.turn_slot_index, 0)
        payload = runner._build_room_snapshot()
        runner._validate_turn_snapshot(
            payload, Phase.DAY_DISCUSSION, list(payload["turn_order"])
        )

    async def test_snapshot_contains_cursor_token_and_panel_id(self) -> None:
        runner = make_runner()
        add_players(runner, 13)
        state = runner.state
        state.turn_order = list(range(1, 14))
        state.turn_round_index = 1
        state.turn_slot_index = 5
        state.turn_slot_token = 12
        state.turn_slot_active = True
        state.turn_co_declarations = [
            {"user_id": 2, "number": 2, "display_name": "02.user-2"},
        ]
        state.turn_panel_message_id = 9876

        snapshot = runner._build_room_snapshot()

        self.assertEqual(snapshot["variant_id"], "v13_turn")
        self.assertEqual(snapshot["turn_round_index"], 1)
        self.assertEqual(snapshot["turn_slot_index"], 5)
        self.assertEqual(snapshot["turn_slot_token"], 12)
        self.assertEqual(
            snapshot["turn_co_declarations"],
            [{"user_id": 2, "number": 2, "display_name": "02.user-2"}],
        )
        self.assertEqual(snapshot["turn_panel_message_id"], 9876)

    async def test_corrupt_turn_order_or_cursor_is_rejected(self) -> None:
        runner = make_runner()
        add_players(runner, 13)
        state = runner.state
        state.turn_day_generation = state.day_generation
        state.turn_anchor_number = 1
        state.turn_order = list(range(1, 14))
        payload = runner._build_room_snapshot()

        runner._validate_turn_snapshot(
            payload, Phase.DAY_DISCUSSION, list(payload["turn_order"])
        )

        duplicate = dict(payload)
        duplicate["turn_order"] = [1] * 13
        with self.assertRaises(StateDurabilityError):
            runner._validate_turn_snapshot(
                duplicate, Phase.DAY_DISCUSSION, duplicate["turn_order"]
            )

        out_of_range = dict(payload)
        out_of_range["turn_slot_index"] = 13
        with self.assertRaises(StateDurabilityError):
            runner._validate_turn_snapshot(
                out_of_range, Phase.DAY_DISCUSSION, out_of_range["turn_order"]
            )

        invalid_co = dict(payload)
        invalid_co["turn_co_declarations"] = [
            {"user_id": 2, "number": 2, "display_name": "02.user-2"},
        ]
        with self.assertRaises(StateDurabilityError):
            runner._validate_turn_snapshot(
                invalid_co, Phase.DAY_DISCUSSION, invalid_co["turn_order"]
            )

        # 2日目の朝には、完了済みの初日2巡cursorが残っていてよい。
        previous_day = dict(payload)
        previous_day["day_number"] = 2
        previous_day["day_generation"] = 2
        previous_day["turn_day_generation"] = 1
        previous_day["turn_round_index"] = 2
        previous_day["turn_slot_index"] = 0
        runner.state.day_number = 2
        runner.state.day_generation = 2
        runner._validate_turn_snapshot(
            previous_day, Phase.MORNING, previous_day["turn_order"]
        )

    async def test_restore_keeps_public_co_list(self) -> None:
        source = make_runner("v9_turn")
        add_players(source, 9)
        source.state.day_number = 2
        source.state.day_generation = 2
        source.state.turn_day_generation = 2
        source.state.gm_id = 1
        source.state.turn_order = list(range(1, 10))
        source.state.turn_co_declarations = [
            {"user_id": 2, "number": 2, "display_name": "02.user-2"},
        ]
        payload = source._build_room_snapshot()
        payload["phase"] = Phase.DAY_DISCUSSION.name

        restored = make_runner("v9_turn")
        members = {player.user_id: player.member for player in source.state.players.values()}
        restored.state.guild = SimpleNamespace(
            get_member=lambda user_id: members.get(user_id),
            get_channel=lambda _channel_id: None,
        )
        restored._enable_mute_markers = AsyncMock()
        restored._reconcile_mute_marker_ownership = AsyncMock()
        restored._reconcile_mute_intents = AsyncMock()
        restored._create_game_channels = AsyncMock()
        restored._reconcile_pending_death_effects = AsyncMock()
        restored._assign_alive_role = AsyncMock()
        restored._restrict_vc_for_game = AsyncMock()
        restored._grant_alive_vc_access = AsyncMock()
        restored._apply_spirit_blocks = AsyncMock()
        restored._post_lobby_ui = AsyncMock()
        restored._safe_village_send = AsyncMock(return_value=None)
        restored._repost_gm_panel = AsyncMock(return_value=True)
        restored._persist_room_state = AsyncMock()

        await restored.restore_from_snapshot(payload)

        self.assertEqual(
            restored.state.turn_co_declarations,
            [{"user_id": 2, "number": 2, "display_name": "02.user-2"}],
        )
        self.assertEqual(restored.state.phase, Phase.PAUSED)

    async def test_snapshot_variant_mismatch_refuses_restore(self) -> None:
        runner = make_runner("v9_turn")
        with self.assertRaises(StateDurabilityError):
            await runner.restore_from_snapshot({"variant_id": "v13_turn"})

    async def test_paused_slot_is_muted_but_active_slot_keeps_speaker_before_panel(self) -> None:
        runner = make_runner()
        add_players(runner, 13)
        state = runner.state
        state.turn_slot_active = True
        state.current_speaker_id = 1
        state.turn_window_open = False
        # UIの受付開始前でも、VC入室同期が現在話者を再muteしてはいけない。
        self.assertEqual(runner._current_speaker_ids(), {1})

        state.paused = True
        state.phase_before_pause = Phase.DAY_DISCUSSION
        state.phase = Phase.PAUSED
        self.assertEqual(runner._current_speaker_ids(), set())

        # 同じ停止条件は既存13人クロストークでも全員muteを維持する。
        cross = make_runner("v13_cross")
        add_players(cross, 13)
        cross.state.paused = True
        cross.state.phase_before_pause = Phase.DAY_DISCUSSION
        cross.state.phase = Phase.PAUSED
        self.assertEqual(cross._current_speaker_ids(), set())

    async def test_voice_join_during_panel_io_does_not_remute_verified_speaker(self) -> None:
        runner = make_runner("v9_turn")
        speaker, joiner = add_players(runner, 2)
        state = runner.state
        voice_channel = SimpleNamespace(id=50, members=[speaker.member, joiner.member])
        state.voice_channel = voice_channel

        for player in (speaker, joiner):
            player.member.bot = False
            player.member.voice = SimpleNamespace(
                channel=voice_channel, mute=False, suppress=False
            )

            async def edit_member(*, mute=None, _member=player.member, **_kwargs):
                if mute is not None:
                    _member.voice.mute = mute
                return _member

            player.member.edit = edit_member

        runner._grant_turn_speaker = AsyncMock()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_replace(message, content, view):
            entered.set()
            await release.wait()
            return message

        runner._replace_turn_message = blocked_replace
        begin = asyncio.create_task(
            runner._begin_turn_segment(speaker, 50, None, interrupt=False)
        )
        await entered.wait()

        # on_voice_state_updateがこの集合でmute同期する状況を再現する。
        self.assertEqual(runner._current_speaker_ids(), {speaker.user_id})
        await runner._sync_server_mutes(runner._current_speaker_ids())
        release.set()
        _, view = await begin

        self.assertIsNotNone(view)
        self.assertFalse(speaker.member.voice.mute)
        self.assertTrue(joiner.member.voice.mute)
        view.stop()

    async def test_removed_absent_speaker_does_not_pause_again_after_resume(self) -> None:
        runner = make_runner("v9_turn")
        speaker = add_players(runner, 1)[0]
        state = runner.state
        state.voice_channel = SimpleNamespace(id=50)
        speaker.member.voice = None
        runner._grant_speaker = AsyncMock()
        runner._clear_speaker = AsyncMock()
        first_pause = asyncio.Event()
        pause_calls = 0

        async def fake_pause(_player, _reason):
            nonlocal pause_calls
            pause_calls += 1
            state.paused = True
            state.phase_before_pause = state.phase
            state.phase = Phase.PAUSED
            state.pause_event.clear()
            first_pause.set()

        runner._pause_for_disconnect = fake_pause
        begin = asyncio.create_task(
            runner._begin_turn_segment(speaker, 50, None, interrupt=False)
        )
        await first_pause.wait()

        # GM除外がdurableになった後のrelease_turn signalを再現する。
        speaker.alive = False
        state.turn_done_event.set()
        state.turn_signal_event.set()
        state.paused = False
        state.phase = state.phase_before_pause
        state.pause_event.set()
        _, view = await asyncio.wait_for(begin, timeout=0.2)

        self.assertIsNone(view)
        self.assertEqual(pause_calls, 1)

    async def test_resume_does_not_unmute_removed_current_speaker(self) -> None:
        """停止中に除外された元話者を再開同期で一度も発言可にしない。"""
        runner = make_runner("v9_turn")
        speaker = add_players(runner, 1)[0]
        state = runner.state
        state.phase = Phase.PAUSED
        state.phase_before_pause = Phase.DAY_DISCUSSION
        state.paused = True
        state.turn_slot_active = True
        state.turn_window_open = True
        state.current_speaker_id = speaker.user_id
        state.game_task = asyncio.create_task(asyncio.sleep(60))
        speaker.alive = False
        runner._sync_server_mutes = AsyncMock(return_value=[])
        runner._await_mute_applied = AsyncMock(return_value=True)

        result = await runner.resume_game()

        self.assertEqual(result, "▶️ 再開しました。")
        runner._sync_server_mutes.assert_awaited_once_with(set())
        self.assertEqual(runner._current_speaker_ids(), set())
        state.game_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await state.game_task

    async def test_disconnect_during_resume_finishes_in_consistent_pause(self) -> None:
        runner = make_runner("v9_turn")
        players = add_players(runner, 2)
        state = runner.state
        state.phase = Phase.PAUSED
        state.phase_before_pause = Phase.DAY_DISCUSSION
        state.paused = True
        state.turn_slot_active = True
        state.turn_window_open = True
        state.current_speaker_id = players[0].user_id
        state.game_task = asyncio.create_task(asyncio.sleep(60))
        resume_sync_entered = asyncio.Event()
        release_resume_sync = asyncio.Event()
        sync_calls = 0

        async def sync_mutes(_speakers):
            nonlocal sync_calls
            sync_calls += 1
            if sync_calls == 1:
                resume_sync_entered.set()
                await release_resume_sync.wait()
            return []

        runner._sync_server_mutes = sync_mutes
        runner._await_mute_applied = AsyncMock(return_value=True)
        resume = asyncio.create_task(runner.resume_game())
        await resume_sync_entered.wait()
        disconnect = asyncio.create_task(
            runner._pause_for_disconnect(players[1], "再開中に切断")
        )
        await asyncio.sleep(0)
        self.assertFalse(disconnect.done())

        release_resume_sync.set()
        await resume
        await disconnect

        self.assertTrue(state.paused)
        self.assertEqual(state.phase, Phase.PAUSED)
        self.assertFalse(state.pause_event.is_set())
        self.assertEqual(sync_calls, 2)
        state.game_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await state.game_task

    async def test_pause_mute_timeout_is_not_reported_as_success(self) -> None:
        runner = make_runner("v9_turn")
        speaker = add_players(runner, 1)[0]
        state = runner.state
        state.game_task = asyncio.create_task(asyncio.sleep(60))
        speaker.member.voice = SimpleNamespace(mute=False)
        runner._sync_server_mutes = AsyncMock(
            return_value=[(speaker.member, True)]
        )
        runner._await_mute_applied = AsyncMock(return_value=False)

        result = await runner.pause_game()

        self.assertIn("確認できません", result)
        self.assertNotEqual(result, "⏸️ 一時停止しました。")
        self.assertTrue(state.paused)
        self.assertFalse(state.pause_event.is_set())
        self.assertFalse(speaker.member.voice.mute)
        state.game_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await state.game_task

    async def test_manual_server_mute_stops_before_turn_timer(self) -> None:
        runner = make_runner()
        speaker = add_players(runner, 1)[0]
        voice_channel = SimpleNamespace(id=50)
        runner.state.voice_channel = voice_channel
        speaker.member.voice = SimpleNamespace(
            channel=voice_channel, mute=True
        )
        runner._grant_speaker = AsyncMock()
        runner._stop_for_durability_error = AsyncMock()

        with self.assertRaises(StateDurabilityError):
            await runner._grant_turn_speaker(speaker)
        runner._stop_for_durability_error.assert_awaited_once()

    async def test_absent_speaker_pauses_instead_of_consuming_the_slot(self) -> None:
        runner = make_runner()
        speaker = add_players(runner, 1)[0]
        runner.state.voice_channel = SimpleNamespace(id=50)
        speaker.member.voice = None
        runner._grant_speaker = AsyncMock()
        runner._pause_for_disconnect = AsyncMock()

        await runner._grant_turn_speaker(speaker)

        runner._pause_for_disconnect.assert_awaited_once_with(
            speaker, "発言順ですが通話に接続していません"
        )

    async def test_turn_timer_edits_only_sparse_checkpoints(self) -> None:
        self.assertTrue(turn_timer_should_update(60, 61))
        self.assertTrue(turn_timer_should_update(30, 31))
        self.assertTrue(turn_timer_should_update(5, 6))
        self.assertFalse(turn_timer_should_update(4, 5))
        self.assertFalse(turn_timer_should_update(29, 30))
        self.assertFalse(turn_timer_should_update(30, 30))

    async def test_turn_view_and_nine_player_help_are_dynamic(self) -> None:
        runner = make_runner("v9_turn")
        add_players(runner, 9)
        view = TurnSpeechView(
            runner,
            speaker_id=1,
            turn_token=3,
            allow_interrupt=True,
            allow_co_declaration=False,
        )
        self.assertEqual(
            {item.label for item in view.children},
            {"発言終了（パス）", "30秒割り込み"},
        )
        co_view = TurnSpeechView(
            runner,
            speaker_id=1,
            turn_token=3,
            allow_interrupt=True,
            allow_co_declaration=True,
        )
        self.assertEqual(
            {item.label for item in co_view.children},
            {"発言終了（パス）", "30秒割り込み", "COを宣言"},
        )
        view.stop()
        co_view.stop()

        variant = get_variant_definition("v9_turn")
        embeds = [*build_rule_embeds(variant), *build_help_embeds(variant)]
        parts: list[str] = []
        for embed in embeds:
            parts.append(embed.description or "")
            parts.extend(field.value for field in embed.fields)
        text = "\n".join(parts)
        self.assertIn("9人固定", text)
        self.assertIn("2狼予想", text)
        self.assertIn("50秒", text)
        self.assertIn("1日 **1回**", text)
        self.assertIn("仮投票はありません", text)

    async def test_settlement_kwargs_freeze_variant_parameters(self) -> None:
        runner = make_runner("v9_turn")
        self.assertEqual(
            runner._settlement_variant_kwargs(),
            {
                "variant_id": "v9_turn",
                "ladder_id": "l9",
                "village_win_pool": 90,
                "wolf_win_pool": 110,
                "wolf_guess_slots": 2,
                "final_day_threshold": 4,
            },
        )


if __name__ == "__main__":
    unittest.main()
