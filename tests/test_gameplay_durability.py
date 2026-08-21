"""夜ゲート・復元チェックポイント・Discord競合の回帰テスト。"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import discord

import database
import room_runner
from config import (
    LOG_CATEGORY_LIMIT,
    LOG_CATEGORY_SPIRIT,
    LOG_CATEGORY_TRIM_TO,
    MUTE_RETRY_DELAY,
    Phase,
    Role,
    RoomDefinition,
    Team,
)
from game import GameCog
from models import GameState, Player, by_number
from room_runner import (
    RoomRunner,
    StateDurabilityError,
    death_nick,
    timer_should_update,
)
from views import (
    MorningReadyView,
    SequentialVoteSpeechView,
    VoteQueueView,
    VoteView,
    build_gm_status_embed,
    WolfSurrenderView,
    WolfVoteView,
)


class FakeManager:
    def __init__(self) -> None:
        self.discord_api_sem = asyncio.Semaphore(20)
        self.start_lock = asyncio.Lock()
        self.rating_lock = asyncio.Lock()
        # 「どの卓に所属するか」を変える操作を全卓で直列化する共有ロック
        self.join_lock = asyncio.Lock()
        self.rooms: dict = {}

    async def paced_discord_api_call(self, func, *args, **kwargs):
        # RoomRunner単体テストでは待機なし。Semaphore経由であることは維持する。
        async with self.discord_api_sem:
            return await func(*args, **kwargs)

    def spawn_bg_task(self, coro):
        # テストで長時間のチャンネル削除予約を残さない。
        coro.close()

    def is_other_active_game_vc(self, channel_id, exclude_room_id=None) -> bool:
        return False

    def find_active_user_room(self, user_id, exclude_room_id=None, **kwargs):
        return None


class FakeVoiceState:
    def __init__(self, *, mute: bool = False, channel=None, suppress: bool = False) -> None:
        self.mute = mute
        self.channel = channel
        self.suppress = suppress


class FakeMember:
    def __init__(self, user_id: int, name: str | None = None) -> None:
        self.id = user_id
        self.display_name = name or f"user-{user_id}"
        self.nick = None
        self.bot = False
        self.roles = []
        self.voice = None
        self.dm_channel = None
        self.send = AsyncMock()
        self.edit = AsyncMock()


class FakeRole:
    def __init__(self, role_id: int, name: str, *, default: bool = False) -> None:
        self.id = role_id
        self.name = name
        self._default = default

    def is_default(self) -> bool:
        return self._default


class FakeLogCategory:
    def __init__(self, name: str, *, channels=None, overwrites=None) -> None:
        self.name = name
        self.channels = list(channels or [])
        self.overwrites = dict(overwrites or {})
        self.edit = AsyncMock(side_effect=self._edit)

    async def _edit(self, *, overwrites, reason=None):
        self.overwrites = dict(overwrites)
        return self


class FakeLogChannel:
    def __init__(self, name: str, channel_id: int, *, overwrites=None) -> None:
        self.name = name
        self.id = channel_id
        self.overwrites = dict(overwrites or {})
        self.edit = AsyncMock(side_effect=self._edit)
        self.delete = AsyncMock()

    async def _edit(self, *, overwrites, reason=None):
        self.overwrites = dict(overwrites)
        return self


class FakeGuild:
    def __init__(self, members: list[FakeMember], roles: list[FakeRole]) -> None:
        self.id = 123
        self.members = members
        self.roles = roles

    def get_member(self, user_id: int):
        return next((member for member in self.members if member.id == user_id), None)


class FakeVoiceChannel:
    def __init__(self, overwrites=None) -> None:
        self.id = 500
        self.overwrites = dict(overwrites or {})

    def overwrites_for(self, target):
        overwrite = self.overwrites.get(target, discord.PermissionOverwrite())
        return discord.PermissionOverwrite.from_pair(*overwrite.pair())

    async def set_permissions(
        self, target, *, overwrite=None, reason=None, **permissions
    ) -> None:
        if permissions:
            overwrite = discord.PermissionOverwrite(**permissions)
        if overwrite is None:
            self.overwrites.pop(target, None)
        else:
            self.overwrites[target] = discord.PermissionOverwrite.from_pair(
                *overwrite.pair()
            )


def permission_text_channel(
    *, set_permissions: AsyncMock | None = None,
) -> SimpleNamespace:
    """discord.TextChannelの個別overwrite APIを再現する最小テストダブル。"""
    return SimpleNamespace(
        set_permissions=set_permissions or AsyncMock(),
        overwrites_for=lambda _target: discord.PermissionOverwrite(),
    )


def make_runner(variant_id: str = "v13_cross") -> RoomRunner:
    # 進行そのものを見る共通ランナーはレート対象外にする。レート精算の
    # 経路を通したいテストは名前村 (private_owner_id) を自前で作る。
    runner = RoomRunner(
        None,
        FakeManager(),
        RoomDefinition("test", "テスト村", variant_id=variant_id, rated=False),
    )
    runner.state.game_run_id = "run-1"
    runner.state.phase = Phase.NIGHT
    runner.state.night_generation = 3
    runner.state.pause_event.set()
    runner._persist_room_state = AsyncMock()
    runner._safe_village_send = AsyncMock()
    runner._safe_spirit_send = AsyncMock()
    return runner


def add_player(runner: RoomRunner, user_id: int, role: Role = Role.VILLAGER) -> Player:
    member = FakeMember(user_id)
    player = Player(
        user_id=user_id,
        member=member,
        role=role,
        alive=True,
        number=user_id,
        base_name=member.display_name,
    )
    runner.state.players[user_id] = player
    return player


class GameplayDurabilityTest(unittest.IsolatedAsyncioTestCase):
    async def test_wolf_guess_hold_uses_same_rule_for_every_alignment(self) -> None:
        for role in (Role.VILLAGER, Role.MADMAN, Role.WEREWOLF):
            with self.subTest(role=role):
                runner = make_runner()
                runner.state.spirit_channel = object()
                victim = add_player(runner, 1, role)
                victim.alive = False
                add_player(runner, 2, Role.WEREWOLF)
                add_player(runner, 3, Role.VILLAGER)
                add_player(runner, 4, Role.VILLAGER)
                add_player(runner, 5, Role.VILLAGER)

                self.assertTrue(runner._should_hold_spirit("処刑"))

    async def test_wolf_guess_skips_death_that_decides_winner(self) -> None:
        runner = make_runner()
        runner.state.spirit_channel = object()
        last_wolf = add_player(runner, 1, Role.WEREWOLF)
        last_wolf.alive = False
        add_player(runner, 2, Role.VILLAGER)
        add_player(runner, 3, Role.VILLAGER)

        self.assertEqual(runner.state.check_win(), Team.VILLAGE)
        self.assertFalse(runner._should_hold_spirit("処刑"))

    async def test_wolf_guess_dm_is_bound_to_run_player_and_death_event(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.DAY_DISCUSSION
        victim = add_player(runner, 1, Role.WEREWOLF)
        victim.alive = False
        for user_id in range(2, 6):
            add_player(runner, user_id)
        event_id = "run-1:処刑:4:1"
        runner.state.spirit_hold_ids.add(victim.user_id)
        runner.state.spirit_hold_events[victim.user_id] = event_id
        runner._release_spirit_hold = AsyncMock()

        stale = await runner.submit_wolf_guess(
            victim.user_id,
            [2, 3, 4],
            game_run_id="old-run",
            death_event_id=event_id,
        )
        wrong_death = await runner.submit_wolf_guess(
            victim.user_id,
            [2, 3, 4],
            game_run_id="run-1",
            death_event_id="run-1:処刑:3:1",
        )
        accepted = await runner.submit_wolf_guess(
            victim.user_id,
            [2, 3, 4],
            game_run_id="run-1",
            death_event_id=event_id,
        )

        self.assertFalse(stale)
        self.assertFalse(wrong_death)
        self.assertTrue(accepted)
        self.assertEqual(runner.state.wolf_guesses[victim.user_id], [2, 3, 4])
        runner._release_spirit_hold.assert_awaited_once_with(victim.user_id)

    async def test_wolf_guess_dm_is_gone_and_panel_notice_rides_on_death_notice(self) -> None:
        """v0.51: 人狼予想DMは廃止。案内は既存の死亡告知へ1行足すだけにする。"""
        self.assertFalse(hasattr(RoomRunner, "_send_wolf_guess_dm"))
        self.assertFalse(hasattr(RoomRunner, "_notify_gm_wolf_guess_dm_failure"))
        self.assertIn("人狼予想", room_runner.WOLF_GUESS_NOTICE)

    async def test_morning_log_adds_the_notice_only_while_holding(self) -> None:
        runner = make_runner()
        victim = add_player(runner, 1)
        victim.alive = False
        runner.state._last_killed = victim

        without_hold = runner.build_morning_log_text()
        runner.state.spirit_hold_ids.add(victim.user_id)
        with_hold = runner.build_morning_log_text()

        self.assertNotIn(room_runner.WOLF_GUESS_NOTICE, without_hold)
        self.assertIn(room_runner.WOLF_GUESS_NOTICE, with_hold)

    async def test_releasing_wolf_guess_hold_opens_spirit_and_invalidates_dm(self) -> None:
        runner = make_runner()
        victim = add_player(runner, 1)
        victim.alive = False
        runner.state.spirit_hold_ids = {victim.user_id}
        runner.state.spirit_hold_events = {
            victim.user_id: "run-1:処刑:4:1"
        }
        runner._open_spirit_for = AsyncMock()

        await runner._release_spirit_hold(victim.user_id)

        self.assertNotIn(victim.user_id, runner.state.spirit_hold_ids)
        self.assertNotIn(victim.user_id, runner.state.spirit_hold_events)
        runner._open_spirit_for.assert_awaited_once_with(victim.member)
        runner._safe_spirit_send.assert_awaited_once()
        runner._persist_room_state.assert_awaited_once()

    async def test_wolf_guess_death_event_is_snapshotted(self) -> None:
        runner = make_runner()
        runner.state.spirit_hold_ids = {1}
        runner.state.spirit_hold_events = {1: "run-1:処刑:4:1"}

        payload = runner._build_room_snapshot()

        self.assertEqual(payload["spirit_hold_ids"], [1])
        self.assertEqual(
            payload["spirit_hold_events"],
            [{"player_id": 1, "event_id": "run-1:処刑:4:1"}],
        )

    async def test_initial_night_opens_only_wolf_relay_and_can_be_skipped(self) -> None:
        runner = make_runner()
        gm = add_player(runner, 10)
        wolf = add_player(runner, 1, Role.WEREWOLF)
        other_wolf = add_player(runner, 2, Role.WEREWOLF)
        add_player(runner, 3, Role.SEER)
        runner.state.gm_id = gm.user_id
        runner.state.phase = Phase.INITIAL_NIGHT
        runner.state.wolf_relay_window_open = True
        # 通常夜の解決済みフラグに影響されず、0日目専用窓で中継する。
        runner.state.night_resolved = True

        self.assertTrue(runner.wolf_relay_open())
        self.assertFalse(runner.night_actions_open())
        message = SimpleNamespace(
            author=SimpleNamespace(id=wolf.user_id, bot=False),
            channel=Mock(spec=discord.DMChannel),
            content="よろしくお願いします",
        )
        await runner.on_message(message)
        other_wolf.member.send.assert_awaited_once()

        result = await runner.force_skip_wait(gm.member)
        self.assertIn("初夜をスキップ", result)
        self.assertTrue(runner.state.initial_night_skip_event.is_set())

    async def test_wolf_dm_before_preparation_relay_window_gets_retry_notice(self) -> None:
        """役職DM直後の挨拶を無言破棄せず、開始後の再送を本人へ案内する。"""
        runner = make_runner()
        wolf = add_player(runner, 1, Role.WEREWOLF)
        other_wolf = add_player(runner, 2, Role.WEREWOLF)
        runner.state.phase = Phase.PREPARATION
        runner.state.initial_night_completed = False
        runner.state.wolf_relay_window_open = False
        message = SimpleNamespace(
            author=SimpleNamespace(id=wolf.user_id, bot=False),
            channel=Mock(spec=discord.DMChannel),
            content="よろしくお願いします",
        )

        await runner.on_message(message)

        wolf.member.send.assert_awaited_once()
        self.assertIn("中継されませんでした", wolf.member.send.await_args.args[0])
        self.assertIn("もう一度", wolf.member.send.await_args.args[0])
        other_wolf.member.send.assert_not_awaited()

    async def test_initial_wolf_greeting_runs_inside_preparation_only(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.PREPARATION
        add_player(runner, 1, Role.WEREWOLF)
        add_player(runner, 2, Role.WEREWOLF)
        add_player(runner, 3, Role.GUARD)

        async def inspect_countdown(_message, _content, seconds, event) -> bool:
            self.assertEqual(seconds, 30)
            self.assertIs(event, runner.state.prep_ready_event)
            self.assertTrue(runner.state.wolf_relay_window_open)
            self.assertTrue(runner.wolf_relay_open())
            self.assertFalse(runner.night_actions_open())
            return False

        runner._pausable_countdown = AsyncMock(side_effect=inspect_countdown)
        await runner._initial_wolf_greeting_during_preparation()

        self.assertTrue(runner.state.initial_night_completed)
        self.assertFalse(runner.state.wolf_relay_window_open)
        runner._persist_room_state.assert_awaited_once()

    async def test_game_loop_skips_separate_initial_night_after_shared_gate(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.PREPARATION
        player = add_player(runner, 1)
        runner.state.prep_ready_ids = {player.user_id}
        runner._post_prep_panel = AsyncMock()
        runner.post_village_panel = AsyncMock(return_value=True)
        runner._repost_gm_panel = AsyncMock(return_value=True)
        runner._close_prep_panel = AsyncMock()
        runner._play_se = Mock()
        runner._pausable_countdown = AsyncMock(return_value=True)
        runner._initial_night_greeting = AsyncMock()
        runner._day_discussion = AsyncMock(side_effect=asyncio.CancelledError())

        await runner._game_loop()

        runner.post_village_panel.assert_awaited_once()

        self.assertTrue(runner.state.prep_confirmed)
        self.assertTrue(runner.state.initial_night_completed)
        runner._initial_night_greeting.assert_not_awaited()
        runner._day_discussion.assert_awaited_once()

    async def test_preparation_village_panel_failure_stops_before_day_one(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.PREPARATION
        player = add_player(runner, 1)
        runner.state.prep_ready_ids = {player.user_id}
        runner._post_prep_panel = AsyncMock()
        runner.post_village_panel = AsyncMock(return_value=False)
        runner._repost_gm_panel = AsyncMock(return_value=True)
        runner._day_discussion = AsyncMock()

        await runner._game_loop()

        self.assertEqual(runner.state.phase, Phase.PAUSED)
        self.assertEqual(runner.state.phase_before_pause, Phase.PREPARATION)
        self.assertEqual(runner.state.recovery_phase, Phase.PREPARATION)
        self.assertTrue(runner.state.recovered_from_restart)
        self.assertFalse(runner.state.pause_event.is_set())
        runner._repost_gm_panel.assert_not_awaited()
        runner._day_discussion.assert_not_awaited()

    async def test_initial_night_opens_relay_before_wolf_notice_and_has_no_actions(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.PREPARATION
        wolf = add_player(runner, 1, Role.WEREWOLF)
        add_player(runner, 2, Role.WEREWOLF)
        add_player(runner, 3, Role.GUARD)
        runner._lock_village = AsyncMock()
        runner._mute_phase = AsyncMock()
        runner._repost_gm_panel = AsyncMock(return_value=True)

        async def inspect_notice(_wolf: Player) -> None:
            self.assertTrue(runner.state.wolf_relay_window_open)
            self.assertTrue(runner.wolf_relay_open())
            self.assertFalse(runner.night_actions_open())
            self.assertFalse(runner.state.morning_ready_open)

        async def finish_countdown(_message, _content, seconds, event) -> bool:
            self.assertEqual(seconds, 30)
            self.assertIs(event, runner.state.initial_night_skip_event)
            self.assertTrue(runner.state.wolf_relay_window_open)
            return False

        runner._send_initial_night_notice = AsyncMock(side_effect=inspect_notice)
        runner._pausable_countdown = AsyncMock(side_effect=finish_countdown)

        await runner._initial_night_greeting()

        self.assertTrue(runner.state.initial_night_completed)
        self.assertFalse(runner.state.wolf_relay_window_open)
        self.assertEqual(runner._send_initial_night_notice.await_count, 2)
        self.assertIn(wolf.user_id, runner.state.players)

    async def test_initial_night_restore_reruns_greeting_then_starts_day_one(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.PAUSED
        runner.state.recovery_phase = Phase.INITIAL_NIGHT
        runner.state.initial_night_completed = False

        async def complete_initial_night() -> None:
            runner.state.initial_night_completed = True

        runner._initial_night_greeting = AsyncMock(
            side_effect=complete_initial_night
        )
        # 1日目の議論へ到達したところでテストだけ終了させる。
        runner._day_discussion = AsyncMock(side_effect=asyncio.CancelledError())

        await runner._resume_recovered_game()

        runner._initial_night_greeting.assert_awaited_once()
        runner._day_discussion.assert_awaited_once()
        self.assertEqual(runner.state.day_number, 1)
        self.assertEqual(runner.state.day_generation, 1)

    async def test_surrender_requires_all_living_real_wolves_and_stays_private_until_complete(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.DAY_DISCUSSION
        first = add_player(runner, 1, Role.WEREWOLF)
        second = add_player(runner, 2, Role.WEREWOLF)
        madman = add_player(runner, 3, Role.MADMAN)
        runner._relay_to_wolves = AsyncMock()

        rejected = await runner.submit_surrender(
            madman.member, expected_game_run_id="run-1"
        )
        first_result = await runner.submit_surrender(
            first.member, expected_game_run_id="run-1"
        )

        self.assertIn("現在この操作はできません", rejected)
        self.assertIn("1 / 2人", first_result)
        self.assertEqual(runner.state.surrender_ids, {first.user_id})
        self.assertFalse(runner.state.surrender_confirmed)
        self.assertIsNone(runner.state.pending_winner)
        runner._relay_to_wolves.assert_awaited_once()
        runner._safe_village_send.assert_not_awaited()

        completed = await runner.submit_surrender(
            second.member, expected_game_run_id="run-1"
        )

        self.assertIn("全人狼が同意", completed)
        self.assertTrue(runner.state.surrender_confirmed)
        self.assertEqual(runner.state.pending_winner, Team.VILLAGE)
        self.assertEqual(runner.state.surrender_ids, {first.user_id, second.user_id})
        # FakeManagerは終了coroutineを閉じるため、公開はfinish task内だけで行われる。
        runner._safe_village_send.assert_not_awaited()

    async def test_stale_surrender_views_cannot_affect_a_new_game(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.DAY_DISCUSSION
        wolf = add_player(runner, 1, Role.WEREWOLF)
        target = add_player(runner, 2)
        permanent = WolfSurrenderView(runner)
        night = WolfVoteView(runner, [target])
        runner.state.game_run_id = "run-2"

        for view, button in (
            (permanent, permanent.surrender_btn),
            (night, night.surrender_btn),
        ):
            with self.subTest(view=type(view).__name__):
                interaction = SimpleNamespace(
                    user=wolf.member,
                    response=SimpleNamespace(send_message=AsyncMock()),
                )
                await button.callback(interaction)
                interaction.response.send_message.assert_awaited_once()
                self.assertIn(
                    "終了しています" if view is permanent else "現在この操作はできません",
                    interaction.response.send_message.await_args.args[0],
                )
                self.assertEqual(runner.state.surrender_ids, set())
            view.stop()

    async def test_surrender_is_rechecked_when_an_unagreed_wolf_dies(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.DAY_DISCUSSION
        agreed = add_player(runner, 1, Role.WEREWOLF)
        unagreed = add_player(runner, 2, Role.WEREWOLF)
        add_player(runner, 3)
        runner.state.surrender_ids = {agreed.user_id}
        unagreed.alive = False

        completed = await runner._confirm_surrender_after_roster_change()

        self.assertTrue(completed)
        self.assertTrue(runner.state.surrender_confirmed)
        self.assertEqual(runner.state.pending_winner, Team.VILLAGE)
        runner._persist_room_state.assert_awaited_once()

    async def test_surrender_controls_are_only_sent_to_real_wolves_and_are_run_bound(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.DAY_DISCUSSION
        wolf = add_player(runner, 1, Role.WEREWOLF)
        madman = add_player(runner, 2, Role.MADMAN)

        await runner._send_surrender_controls()
        await runner._send_surrender_controls()

        wolf.member.send.assert_awaited_once()
        sent_view = wolf.member.send.await_args.kwargs["view"]
        self.assertIsInstance(sent_view, WolfSurrenderView)
        self.assertEqual(sent_view.game_run_id, "run-1")
        madman.member.send.assert_not_awaited()
        sent_view.stop()

    async def test_night_surrender_snapshot_validation_fails_closed(self) -> None:
        runner = make_runner()
        add_player(runner, 1, Role.WEREWOLF)
        add_player(runner, 2, Role.WEREWOLF)
        add_player(runner, 3, Role.VILLAGER)
        payload = runner._build_room_snapshot()
        runner._validate_night_surrender_snapshot(payload)

        invalid_payloads = []
        invalid_payloads.append({**payload, "initial_night_completed": 1})
        invalid_payloads.append({**payload, "surrender_ids": [3]})
        invalid_payloads.append({
            **payload,
            "surrender_ids": [1],
            "surrender_confirmed": True,
            "pending_winner": Team.VILLAGE.name,
        })
        invalid_payloads.append({
            **payload,
            "surrender_ids": [1, 2],
            "surrender_confirmed": True,
            "pending_winner": None,
        })
        invalid_payloads.append({
            **payload,
            "morning_ready_open": False,
            "morning_ready_ids": [1],
        })

        for invalid in invalid_payloads:
            with self.subTest(invalid=invalid):
                with self.assertRaises(StateDurabilityError):
                    runner._validate_night_surrender_snapshot(invalid)

    async def test_restore_recovers_confirmed_surrender_before_normal_progress(self) -> None:
        source = make_runner()
        source.state.phase = Phase.NIGHT
        wolf = add_player(source, 1, Role.WEREWOLF)
        villager = add_player(source, 2, Role.VILLAGER)
        source.state.surrender_ids = {wolf.user_id}
        source.state.surrender_confirmed = True
        source.state.pending_winner = Team.VILLAGE
        snapshot = source._build_room_snapshot()
        snapshot["phase"] = Phase.NIGHT.name

        restored = make_runner()
        restored.state.guild = FakeGuild([wolf.member, villager.member], [])
        restored.state.village_channel = object()
        restored.state.spirit_channel = object()
        restored._enable_mute_markers = AsyncMock()
        restored._reconcile_mute_marker_ownership = AsyncMock()
        restored._reconcile_mute_intents = AsyncMock()
        restored._disable_recovered_turn_panel = AsyncMock()
        restored._reconcile_pending_death_effects = AsyncMock()
        restored._finish_surrender = AsyncMock()

        await restored.restore_from_snapshot(snapshot)

        restored._finish_surrender.assert_awaited_once()
        self.assertTrue(restored.state.surrender_confirmed)
        self.assertEqual(restored.state.pending_winner, Team.VILLAGE)

    async def test_restore_rejects_confirmed_surrender_if_no_living_wolf_remains(self) -> None:
        source = make_runner()
        source.state.phase = Phase.NIGHT
        wolf = add_player(source, 1, Role.WEREWOLF)
        villager = add_player(source, 2, Role.VILLAGER)
        source.state.surrender_ids = {wolf.user_id}
        source.state.surrender_confirmed = True
        source.state.pending_winner = Team.VILLAGE
        snapshot = source._build_room_snapshot()
        snapshot["phase"] = Phase.NIGHT.name

        # snapshot作成後に人狼だけがサーバーを退出した復元境界。
        restored = make_runner()
        restored.state.guild = FakeGuild([villager.member], [])
        restored.state.village_channel = object()
        restored.state.spirit_channel = object()
        restored._enable_mute_markers = AsyncMock()
        restored._reconcile_mute_marker_ownership = AsyncMock()
        restored._reconcile_mute_intents = AsyncMock()
        restored._disable_recovered_turn_panel = AsyncMock()
        restored._reconcile_pending_death_effects = AsyncMock()

        with self.assertRaisesRegex(StateDurabilityError, "生存実人狼"):
            await restored.restore_from_snapshot(snapshot)

    async def test_private_restore_requires_spirit_channel_for_access_control(self) -> None:
        runner = RoomRunner(
            None,
            FakeManager(),
            RoomDefinition("private_1", "GM名前村", private_owner_id=1),
        )
        runner.state.spirit_channel = None

        with self.assertRaisesRegex(RuntimeError, "#霊界を確認できない"):
            await runner._apply_spirit_blocks(required=True)

    async def test_vc_spectator_restriction_failure_stops_game_safely(self) -> None:
        runner = make_runner()
        default = FakeRole(1, "@everyone", default=True)
        guild = FakeGuild([], [default])
        guild.default_role = default
        guild.me = FakeMember(99, "bot")
        denied = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden", headers={}),
            "denied",
        )
        runner.state.guild = guild
        runner.state.voice_channel = SimpleNamespace(
            overwrites_for=lambda _target: discord.PermissionOverwrite(),
            set_permissions=AsyncMock(side_effect=denied),
        )
        runner._stop_for_durability_error = AsyncMock()

        with self.assertRaises(StateDurabilityError):
            await runner._prepare_game_vc_permissions("ゲーム開始時のVC権限設定")

        runner._stop_for_durability_error.assert_awaited_once()

    async def test_manual_room_restores_static_channel_and_vc_permissions(self) -> None:
        manager = FakeManager()
        runner = RoomRunner(
            None,
            manager,
            RoomDefinition(
                "nate",
                "ねいとくん村",
                access_role_names=frozenset({"ねいと"}),
                sync_permissions=False,
            ),
        )
        default = FakeRole(1, "@everyone", default=True)
        bot_member = FakeMember(99, "bot")
        gm = FakeMember(2, "GM")
        manual_role = FakeRole(3, "手動閲覧ロール")
        guild = FakeGuild([gm, bot_member], [default, manual_role])
        guild.default_role = default
        guild.me = bot_member
        category_overwrites = {
            default: discord.PermissionOverwrite(view_channel=False),
            manual_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
            ),
        }
        category = SimpleNamespace(overwrites=category_overwrites)
        category.overwrites_for = lambda target: discord.PermissionOverwrite.from_pair(
            *category_overwrites.get(target, discord.PermissionOverwrite()).pair()
        )
        runner.state.category = category
        runner.state.guild = guild
        runner.state.gm_id = gm.id
        # GM兼プレイヤーでも、ゲーム中の説明のため個別speak許可を得る。
        runner.state.players[gm.id] = Player(
            user_id=gm.id,
            member=gm,
            role=Role.VILLAGER,
            alive=True,
            number=1,
            base_name=gm.display_name,
        )
        runner._persist_room_state = AsyncMock()

        village_overwrites = runner._build_game_channel_overwrites(
            guild, village=True,
        )
        self.assertFalse(village_overwrites[default].view_channel)
        self.assertTrue(village_overwrites[manual_role].view_channel)
        self.assertFalse(village_overwrites[manual_role].send_messages)
        self.assertTrue(category_overwrites[manual_role].send_messages)

        vc = FakeVoiceChannel(
            {
                default: discord.PermissionOverwrite(
                    view_channel=False, speak=True,
                ),
                gm: discord.PermissionOverwrite(connect=True, speak=False),
            }
        )
        runner.state.voice_channel = vc

        await runner._restrict_vc_for_game()
        self.assertFalse(vc.overwrites_for(default).speak)
        self.assertFalse(vc.overwrites_for(default).send_messages)
        self.assertFalse(vc.overwrites_for(default).view_channel)
        self.assertTrue(vc.overwrites_for(gm).speak)
        self.assertTrue(vc.overwrites_for(gm).connect)
        self.assertTrue(runner.state.vc_gm_speak_captured)
        self.assertEqual(runner.state.vc_gm_speak_user_id, gm.id)

        await runner._release_vc_after_game()
        self.assertTrue(vc.overwrites_for(default).speak)
        self.assertIsNone(vc.overwrites_for(default).send_messages)
        self.assertFalse(vc.overwrites_for(default).view_channel)
        self.assertFalse(vc.overwrites_for(gm).speak)
        self.assertTrue(vc.overwrites_for(gm).connect)
        self.assertFalse(runner.state.vc_default_permissions_captured)
        self.assertFalse(runner.state.vc_gm_speak_captured)
        self.assertEqual(runner._persist_room_state.await_count, 2)

    async def test_rematch_skips_members_blocked_by_the_current_lobby(self) -> None:
        """次村は募集カードを通らないので、同村拒否を自前で見る。"""
        runner = make_runner()
        runner.state.phase = Phase.LOBBY
        gm = FakeMember(1, "GM")
        wanted = FakeMember(2, "参加OK")
        blocked = FakeMember(3, "拒否された人")
        members = {member.id: member for member in (gm, wanted, blocked)}
        runner.state.guild = SimpleNamespace(
            id=1, get_member=members.get, owner_id=999,
        )
        runner.state.lobby_channel = SimpleNamespace(send=AsyncMock())
        runner.last_game_gm = gm.id
        runner.last_game_roster = [gm.id, wanted.id, blocked.id]
        runner.validate_gm_claim = AsyncMock(return_value=None)
        runner.validate_join = AsyncMock(return_value=None)

        with patch.object(
            database,
            "list_player_blocks_between",
            AsyncMock(return_value=[(wanted.id, blocked.id)]),
        ):
            result = await runner.rematch(gm)

        self.assertEqual(set(runner.state.players), {gm.id, wanted.id})
        # 誰が誰を拒否したかは出さず、スキップした事実だけを返す
        self.assertIn(blocked.display_name, result)
        blocked.send.assert_not_awaited()

    async def test_rematch_waits_for_shared_join_lock(self) -> None:
        """次村も通常参加と同じ全卓共通ロックの内側で登録する。"""
        runner = make_runner()
        member = FakeMember(1, "GM")
        runner._rematch_locked = AsyncMock(return_value="ok")

        async with runner.manager.join_lock:
            task = asyncio.create_task(runner.rematch(member))
            await asyncio.sleep(0)
            runner._rematch_locked.assert_not_awaited()

        self.assertEqual(await task, "ok")
        runner._rematch_locked.assert_awaited_once_with(member)

    async def test_rematch_reconsiders_blocked_member_after_dm_failure(self) -> None:
        """先行候補がDM不可なら、その人との拒否だけで後続を除外しない。"""
        runner = make_runner()
        runner.state.phase = Phase.LOBBY
        gm = FakeMember(1, "GM")
        dm_failed = FakeMember(2, "DM不可")
        recovered = FakeMember(3, "参加可能")
        dm_failed.send.side_effect = discord.Forbidden(Mock(status=403), "denied")
        members = {member.id: member for member in (gm, dm_failed, recovered)}
        runner.state.guild = SimpleNamespace(
            id=1, get_member=members.get, owner_id=999,
        )
        runner.state.lobby_channel = SimpleNamespace(send=AsyncMock())
        runner.last_game_gm = gm.id
        runner.last_game_roster = [gm.id, dm_failed.id, recovered.id]
        runner.validate_gm_claim = AsyncMock(return_value=None)
        runner.validate_join = AsyncMock(return_value=None)

        with patch.object(
            database,
            "list_player_blocks_between",
            AsyncMock(return_value=[(dm_failed.id, recovered.id)]),
        ):
            result = await runner.rematch(gm)

        self.assertEqual(set(runner.state.players), {gm.id, recovered.id})
        self.assertIn("DM不可", result)
        recovered.send.assert_awaited_once()

    async def test_gm_rooms_are_rated_and_wait_for_postgame(self) -> None:
        gm_room = RoomRunner(
            None,
            FakeManager(),
            RoomDefinition(
                "private_1",
                "テストGM村",
                private_owner_id=1,
                variant_id="v9_turn",
            ),
        )
        # 常設卓の廃止後、レート対象は名前村と rated なローカル固定卓だけ。
        # 「総合」は履歴用に定義だけ残った無効卓なので対象外になる。
        retired_open = RoomRunner(
            None, FakeManager(), RoomDefinition("open", "総合", enabled=False)
        )
        unrated_local = RoomRunner(
            None, FakeManager(), RoomDefinition("nate", "ローカル卓", rated=False)
        )

        self.assertTrue(gm_room.is_rated_room())
        self.assertFalse(retired_open.is_rated_room())
        self.assertFalse(unrated_local.is_rated_room())

        gm_room._postgame_vote_pending = True
        interaction = SimpleNamespace(
            guild=SimpleNamespace(),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await gm_room._start_game_locked(interaction)
        self.assertIn(
            "終了後投票を集計中",
            interaction.followup.send.await_args.args[0],
        )

    async def test_bulk_api_pacer_serializes_calls_and_respects_semaphore(self) -> None:
        manager = GameCog(SimpleNamespace(managed_guild_id=1))
        manager.bulk_api_interval = 0.02
        call_times: list[float] = []

        async def record_call() -> None:
            call_times.append(asyncio.get_running_loop().time())

        # ペーサー内の実呼び出しが共有Semaphoreを通ることを確認する。
        manager.discord_api_sem = asyncio.Semaphore(1)
        await manager.discord_api_sem.acquire()
        blocked = asyncio.create_task(manager.paced_discord_api_call(record_call))
        await asyncio.sleep(0.01)
        self.assertEqual(call_times, [])
        manager.discord_api_sem.release()
        await blocked

        await asyncio.gather(
            manager.paced_discord_api_call(record_call),
            manager.paced_discord_api_call(record_call),
        )
        intervals = [
            later - earlier for earlier, later in zip(call_times, call_times[1:])
        ]
        self.assertTrue(all(interval >= 0.018 for interval in intervals), intervals)

    async def test_panel_double_failure_confirms_and_closes_night_actions(self) -> None:
        runner = make_runner()
        add_player(runner, 1)
        runner.state.morning_panel_message = None
        runner._post_morning_panel = AsyncMock()  # 再掲示しても出せない

        await runner._wait_for_morning()

        self.assertTrue(runner.state.morning_confirmed)
        self.assertTrue(runner.state.morning_ready_event.is_set())
        self.assertFalse(runner.night_actions_open())
        runner._persist_room_state.assert_awaited()

    async def test_v9_initial_seer_white_candidates_exclude_two_wolves_and_seer(self) -> None:
        """9人配役の初日白は、狼2人と占い師本人を除く6人から選ぶ。"""
        for variant_id in ("v9_cross", "v9_turn"):
            with self.subTest(variant_id=variant_id):
                runner = make_runner(variant_id)
                seer = add_player(runner, 1, Role.SEER)
                wolves = [
                    add_player(runner, 2, Role.WEREWOLF),
                    add_player(runner, 3, Role.WEREWOLF),
                ]
                expected = {
                    add_player(runner, 4, Role.MADMAN).user_id,
                    add_player(runner, 5, Role.MEDIUM).user_id,
                    add_player(runner, 6, Role.GUARD).user_id,
                    add_player(runner, 7, Role.VILLAGER).user_id,
                    add_player(runner, 8, Role.VILLAGER).user_id,
                    add_player(runner, 9, Role.VILLAGER).user_id,
                }

                candidates = runner._initial_seer_white_candidates(seer)

                self.assertEqual({player.user_id for player in candidates}, expected)
                self.assertEqual(len(candidates), 6)
                self.assertNotIn(seer.user_id, {player.user_id for player in candidates})
                self.assertTrue(
                    all(wolf.user_id not in {player.user_id for player in candidates}
                        for wolf in wolves)
                )

    async def test_v9_guard_cannot_declare_morning_without_guarding_even_twice(self) -> None:
        for variant_id in ("v9_cross", "v9_turn"):
            with self.subTest(variant_id=variant_id):
                runner = make_runner(variant_id)
                guard = add_player(runner, 1, Role.GUARD)
                target = add_player(runner, 2, Role.VILLAGER)
                runner.state.morning_ready_open = True

                for _ in range(2):
                    _, error = await runner.toggle_morning_ready(guard.member)
                    self.assertIsNotNone(error)
                    self.assertIn("護衛先を確定", error)
                    self.assertNotIn(guard.user_id, runner.state.morning_ready_ids)
                    self.assertNotIn(guard.user_id, runner.state.morning_warned_ids)
                    self.assertFalse(runner.state.morning_confirmed)
                    self.assertFalse(runner.state.morning_ready_event.is_set())

                runner.state.guard_target = target.user_id
                _, error = await runner.toggle_morning_ready(guard.member)
                self.assertIsNone(error)
                self.assertIn(guard.user_id, runner.state.morning_ready_ids)

    async def test_v9_force_morning_rejects_unprotected_guard(self) -> None:
        for variant_id in ("v9_cross", "v9_turn"):
            with self.subTest(variant_id=variant_id):
                runner = make_runner(variant_id)
                gm = add_player(runner, 1, Role.VILLAGER)
                guard = add_player(runner, 2, Role.GUARD)
                target = add_player(runner, 3, Role.VILLAGER)
                runner.state.gm_id = gm.user_id
                runner.state.morning_ready_open = True

                _, error = await runner.force_morning(gm.member)

                self.assertIsNotNone(error)
                self.assertIn("必須の夜行動", error)
                self.assertFalse(runner.state.morning_confirmed)
                self.assertFalse(runner.state.morning_ready_event.is_set())
                runner._persist_room_state.assert_not_awaited()

                runner.state.guard_target = target.user_id
                _, error = await runner.force_morning(gm.member)
                self.assertIsNone(error)
                self.assertTrue(runner.state.morning_confirmed)
                self.assertTrue(runner.state.morning_ready_event.is_set())
                self.assertTrue(guard.alive)

    async def test_v9_invalid_guard_targets_remain_pending(self) -> None:
        for variant_id in ("v9_cross", "v9_turn"):
            with self.subTest(variant_id=variant_id):
                runner = make_runner(variant_id)
                guard = add_player(runner, 1, Role.GUARD)
                previous = add_player(runner, 2, Role.VILLAGER)
                dead = add_player(runner, 3, Role.VILLAGER)
                dead.alive = False
                runner.state.guard_previous = previous.user_id

                for target_id in (-1, guard.user_id, previous.user_id, dead.user_id, 999):
                    with self.subTest(target_id=target_id):
                        runner.state.guard_target = target_id
                        self.assertIs(runner._pending_guard_player(), guard)

                runner.state.guard_target = None
                self.assertIs(runner._pending_guard_player(), guard)

    async def test_v9_guard_commit_rejects_self_previous_and_offered_out_targets(self) -> None:
        """#昼パネル[狩人]の確定処理も、表示候補を迂回した対象を最終的に拒否する。"""
        for variant_id in ("v9_cross", "v9_turn"):
            with self.subTest(variant_id=variant_id):
                runner = make_runner(variant_id)
                guard = add_player(runner, 1, Role.GUARD)
                allowed = add_player(runner, 2, Role.VILLAGER)
                previous = add_player(runner, 3, Role.VILLAGER)
                outside = add_player(runner, 4, Role.VILLAGER)
                outside.alive = False

                text, committed = await runner.commit_guard_target(
                    guard.user_id, guard.user_id
                )
                self.assertFalse(committed)
                self.assertIn("自分", text)

                runner.state.guard_previous = previous.user_id
                text, committed = await runner.commit_guard_target(
                    guard.user_id, previous.user_id
                )
                self.assertFalse(committed)
                self.assertIn("前回", text)

                runner.state.guard_previous = None
                text, committed = await runner.commit_guard_target(
                    guard.user_id, outside.user_id
                )
                self.assertFalse(committed)
                self.assertIn("対象は護衛できません", text)
                self.assertIsNone(runner.state.guard_target)

                text, committed = await runner.commit_guard_target(
                    guard.user_id, allowed.user_id
                )
                self.assertTrue(committed, text)
                self.assertEqual(runner.state.guard_target, allowed.user_id)

    async def test_v9_panel_failure_pauses_instead_of_skipping_unprotected_guard(self) -> None:
        for variant_id in ("v9_cross", "v9_turn"):
            with self.subTest(variant_id=variant_id):
                runner = make_runner(variant_id)
                add_player(runner, 1, Role.GUARD)
                add_player(runner, 2, Role.VILLAGER)
                runner.state.morning_panel_message = None
                runner._post_morning_panel = AsyncMock()  # 再掲示しても出せない
                runner.pause_game = AsyncMock()
                runner._pausable_wait_forever = AsyncMock()

                await runner._wait_for_morning()

                runner.pause_game.assert_awaited_once()
                runner._pausable_wait_forever.assert_awaited_once_with(
                    runner.state.morning_ready_event
                )
                self.assertFalse(runner.state.morning_confirmed)
                self.assertFalse(runner.state.morning_ready_event.is_set())

    async def test_v9_confirmed_morning_reopens_if_required_guard_is_missing(self) -> None:
        """不整合なmorning_confirmedでも、未護衛のまま解決しない。"""
        for variant_id in ("v9_cross", "v9_turn"):
            with self.subTest(variant_id=variant_id):
                runner = make_runner(variant_id)
                guard = add_player(runner, 1, Role.GUARD)
                target = add_player(runner, 2, Role.VILLAGER)
                runner.state.wolf_target = -1
                runner.state.morning_ready_ids = {guard.user_id, target.user_id}
                runner.state.morning_warned_ids = {guard.user_id}
                runner.state.morning_confirmed = True
                runner.state.morning_ready_event.set()
                runner.state.night_complete_event.set()

                async def reopen(*, resume_existing: bool) -> None:
                    self.assertTrue(resume_existing)
                    self.assertIsNone(runner.state.guard_target)
                    self.assertFalse(runner.state.morning_confirmed)
                    self.assertFalse(runner.state.morning_ready_event.is_set())
                    self.assertFalse(runner.state.night_complete_event.is_set())
                    self.assertNotIn(guard.user_id, runner.state.morning_ready_ids)
                    self.assertNotIn(guard.user_id, runner.state.morning_warned_ids)
                    runner.state.guard_target = target.user_id

                runner._night_phase = AsyncMock(side_effect=reopen)

                await runner._process_night()

                runner._night_phase.assert_awaited_once_with(resume_existing=True)
                self.assertTrue(runner.state.night_resolved)
                self.assertEqual(runner.state.guard_previous, target.user_id)

    async def test_v9_guard_reselects_when_gm_excludes_its_guard_target(self) -> None:
        for variant_id in ("v9_cross", "v9_turn"):
            with self.subTest(variant_id=variant_id):
                runner = make_runner(variant_id)
                guard = add_player(runner, 1, Role.GUARD)
                target = add_player(runner, 2, Role.VILLAGER)
                runner.state.guard_target = target.user_id
                runner.state.morning_ready_ids = {guard.user_id, target.user_id}
                runner.state.morning_warned_ids = {guard.user_id}
                runner.state.morning_confirmed = True
                runner.state.morning_ready_event.set()
                runner.state.night_complete_event.set()
                runner._apply_death_effect = AsyncMock()
                runner._request_guard_reselection = AsyncMock()
                runner.state.check_win = lambda: None

                await runner._eliminate_player_mid_game(target, "GM除外")

                self.assertFalse(target.alive)
                self.assertIsNone(runner.state.guard_target)
                self.assertNotIn(guard.user_id, runner.state.morning_ready_ids)
                self.assertNotIn(guard.user_id, runner.state.morning_warned_ids)
                self.assertFalse(runner.state.morning_confirmed)
                self.assertFalse(runner.state.morning_ready_event.is_set())
                self.assertFalse(runner.state.night_complete_event.is_set())
                runner._request_guard_reselection.assert_awaited_once_with(guard)

    async def test_wolves_reselect_when_gm_excludes_the_current_attack_target(self) -> None:
        """除外した襲撃先を保存せず、残存狼の朝宣言も再選択用に戻す。"""
        runner = make_runner()
        first_wolf = add_player(runner, 1, Role.WEREWOLF)
        second_wolf = add_player(runner, 2, Role.WEREWOLF)
        target = add_player(runner, 3)
        remaining = [add_player(runner, user_id) for user_id in (4, 5, 6)]
        runner.state.wolf_target = target.user_id
        runner.state.wolf_voters = {
            first_wolf.user_id: target.user_id,
            second_wolf.user_id: target.user_id,
        }
        runner.state.morning_ready_open = True
        runner.state.morning_ready_ids = {
            first_wolf.user_id, second_wolf.user_id, target.user_id,
            *(player.user_id for player in remaining),
        }
        runner.state.morning_warned_ids = {first_wolf.user_id, second_wolf.user_id}
        runner.state.morning_confirmed = True
        runner.state.morning_ready_event.set()
        runner.state.night_complete_event.set()
        runner._apply_death_effect = AsyncMock()
        runner.refresh_wolf_dm_displays = AsyncMock()
        runner._reveal_morning_count = AsyncMock()
        runner.state.check_win = lambda: None

        completed = await runner._eliminate_player_mid_game(target, "GM除外")

        self.assertTrue(completed)
        self.assertFalse(target.alive)
        self.assertIsNone(runner.state.wolf_target)
        self.assertEqual(runner.state.wolf_voters, {})
        self.assertNotIn(target.user_id, runner.state.morning_ready_ids)
        self.assertNotIn(first_wolf.user_id, runner.state.morning_ready_ids)
        self.assertNotIn(second_wolf.user_id, runner.state.morning_ready_ids)
        self.assertFalse(runner.state.morning_confirmed)
        self.assertFalse(runner.state.morning_ready_event.is_set())
        self.assertFalse(runner.state.night_complete_event.is_set())
        self.assertTrue(runner.night_actions_open())
        runner.refresh_wolf_dm_displays.assert_awaited_once_with(
            runner.state.night_duration
        )
        runner._reveal_morning_count.assert_awaited_once()

    async def test_wolves_reselect_when_last_attack_selector_is_excluded(self) -> None:
        """除外された狼だけが最後の襲撃先を選んでいた場合も持ち越さない。"""
        runner = make_runner()
        removed_wolf = add_player(runner, 1, Role.WEREWOLF)
        remaining_wolf = add_player(runner, 2, Role.WEREWOLF)
        selected_by_removed = add_player(runner, 3)
        selected_by_remaining = add_player(runner, 4)
        add_player(runner, 5)
        add_player(runner, 6)
        runner.state.wolf_target = selected_by_removed.user_id
        runner.state.wolf_voters = {
            removed_wolf.user_id: selected_by_removed.user_id,
            remaining_wolf.user_id: selected_by_remaining.user_id,
        }
        runner.state.morning_ready_open = True
        runner.state.morning_ready_ids = {
            player.user_id for player in runner.state.alive_players()
        }
        runner.state.morning_warned_ids = {
            removed_wolf.user_id, remaining_wolf.user_id,
        }
        runner.state.morning_confirmed = True
        runner.state.morning_ready_event.set()
        runner.state.night_complete_event.set()
        runner._apply_death_effect = AsyncMock()
        runner.refresh_wolf_dm_displays = AsyncMock()
        runner._reveal_morning_count = AsyncMock()
        runner.state.check_win = lambda: None

        completed = await runner._eliminate_player_mid_game(
            removed_wolf, "GM除外",
        )

        self.assertTrue(completed)
        self.assertIsNone(runner.state.wolf_target)
        self.assertEqual(runner.state.wolf_voters, {})
        self.assertNotIn(remaining_wolf.user_id, runner.state.morning_ready_ids)
        self.assertFalse(runner.state.morning_confirmed)
        self.assertFalse(runner.state.morning_ready_event.is_set())
        self.assertFalse(runner.state.night_complete_event.is_set())
        runner.refresh_wolf_dm_displays.assert_awaited_once()

    async def test_restore_falls_back_to_fetch_when_cache_is_cold(self) -> None:
        """起動直後のキャッシュ未反映を「サーバー退出」と誤判定しない。

        discord.py は chunking が max(5秒, 人数/10000) で終わらなくても
        警告だけ出して ready を発火する。get_member だけを信じると、
        在籍している進行中ゲームの参加者を投票権ごと消してしまう。
        """
        runner = make_runner()
        cached = FakeMember(1)
        not_cached = FakeMember(2)
        left_server = FakeMember(3)

        class ColdCacheGuild:
            id = 123

            def get_member(self, user_id: int):
                # chunking未完了で、1人しかキャッシュに載っていない状態
                return cached if user_id == cached.id else None

            async def fetch_member(self, user_id: int):
                if user_id == not_cached.id:
                    return not_cached
                raise discord.NotFound(
                    SimpleNamespace(status=404, reason="Not Found"),
                    {"message": "Unknown Member", "code": 10007},
                )

        runner.state.guild = ColdCacheGuild()

        # キャッシュ済み・未反映・退出済みの3通り
        self.assertIs(
            await runner._fetch_member_for_restore(not_cached.id), not_cached
        )
        self.assertIsNone(await runner._fetch_member_for_restore(left_server.id))

    async def test_restore_fetch_treats_api_failure_as_unknown(self) -> None:
        """API障害は退出と区別できないので不在扱いにするが、握り潰さない。"""
        runner = make_runner()

        class FlakyGuild:
            id = 123

            def get_member(self, user_id: int):
                return None

            async def fetch_member(self, user_id: int):
                raise discord.HTTPException(
                    SimpleNamespace(status=503, reason="Service Unavailable"),
                    {"message": "Service Unavailable", "code": 0},
                )

        runner.state.guild = FlakyGuild()
        with self.assertLogs("room_runner", level="WARNING") as logs:
            self.assertIsNone(await runner._fetch_member_for_restore(1))
        self.assertTrue(any("メンバー照会に失敗" in line for line in logs.output))

    async def test_restore_fetch_is_safe_without_fetch_member(self) -> None:
        """fetch_member を持たない相手 (古いfake等) でも例外にしない。"""
        runner = make_runner()
        runner.state.guild = SimpleNamespace(id=123, get_member=lambda _uid: None)
        self.assertIsNone(await runner._fetch_member_for_restore(1))

    async def test_join_lock_serializes_across_rooms(self) -> None:
        """参加の判定〜登録は全卓で直列化する。

        卓ごとの action_lock だけだと、卓Aが二重参加チェックを通過してから
        DM送信テスト (Discordへの往復) を待つ間に、卓Bが「まだどこにも
        参加していない」と判定して同じ人を登録できてしまう。
        """
        manager = FakeManager()
        rooms = {}
        for room_id in ("a", "b"):
            runner = RoomRunner(None, manager, RoomDefinition(room_id, f"卓{room_id}"))
            runner.state.room_id = room_id
            runner.state.phase = Phase.LOBBY
            runner._persist_room_state = AsyncMock()
            rooms[room_id] = runner
        manager.rooms = rooms

        member = FakeMember(1)
        joined_rooms: list[str] = []

        async def join(runner: RoomRunner) -> None:
            """join_button と同じ順序・同じ窓を再現する"""
            async with manager.join_lock, runner.action_lock:
                # 他卓に登録済みなら参加させない (validate_join 相当)
                for other in rooms.values():
                    if other is not runner and member.id in other.state.players:
                        return
                # DM送信テストのawait。ここが割り込みの窓になる
                await asyncio.sleep(0)
                runner.state.players[member.id] = Player(
                    user_id=member.id, member=member, base_name=member.display_name
                )
                joined_rooms.append(runner.state.room_id)

        await asyncio.gather(join(rooms["a"]), join(rooms["b"]))

        # 二重登録されない
        self.assertEqual(len(joined_rooms), 1)
        registered = [r for r in rooms.values() if member.id in r.state.players]
        self.assertEqual(len(registered), 1)

    async def test_prep_gate_opens_only_after_everyone_declares(self) -> None:
        """役職確認は参加者全員が押すまで開かず、**二度押しでは取り消せない**。

        トグルだった頃はスマホの二度タップで無言のまま宣言が消え、
        12/13 のまま理由が分からず議論が始まらなかった。
        """
        runner = make_runner()
        runner.state.phase = Phase.PREPARATION
        players = [add_player(runner, uid) for uid in (1, 2, 3)]

        for player in players[:2]:
            _, error = await runner.toggle_prep_ready(player.member)
            self.assertIsNone(error)
        self.assertFalse(runner.state.prep_ready_event.is_set())
        self.assertFalse(runner.state.prep_confirmed)

        # 二度押しは冪等。宣言は消えず、本人には理由が返る
        _, error = await runner.toggle_prep_ready(players[0].member)
        self.assertIsNotNone(error)
        self.assertIn(players[0].user_id, runner.state.prep_ready_ids)
        self.assertEqual(len(runner.state.prep_ready_ids), 2)
        self.assertFalse(runner.state.prep_ready_event.is_set())

        # 最後の1人でゲートが開く
        _, error = await runner.toggle_prep_ready(players[2].member)
        self.assertIsNone(error)
        self.assertTrue(runner.state.prep_confirmed)
        self.assertTrue(runner.state.prep_ready_event.is_set())

        # 確定後も受け付けない
        _, error = await runner.toggle_prep_ready(players[0].member)
        self.assertIsNotNone(error)
        self.assertEqual(len(runner.state.prep_ready_ids), 3)

    async def test_prep_event_is_not_released_before_checkpoint(self) -> None:
        """DB保存に失敗したら prep_ready_event を立てずロールバックする。"""
        runner = make_runner()
        runner.state.phase = Phase.PREPARATION
        player = add_player(runner, 1)

        async def fail_while_waiter_is_still_blocked() -> None:
            self.assertFalse(runner.state.prep_ready_event.is_set())
            raise RuntimeError("DB down")

        runner._persist_room_state = AsyncMock(
            side_effect=fail_while_waiter_is_still_blocked
        )

        _, error = await runner.toggle_prep_ready(player.member)

        self.assertIsNotNone(error)
        self.assertFalse(runner.state.prep_confirmed)
        self.assertFalse(runner.state.prep_ready_event.is_set())
        self.assertNotIn(player.user_id, runner.state.prep_ready_ids)

    async def test_morning_panel_is_posted_once_to_the_village_channel(self) -> None:
        """朝パネルは #昼 へ1枚だけ。再掲示は冪等で追加送信しない。"""
        runner = make_runner()
        players = [add_player(runner, uid) for uid in (1, 2, 3)]
        dead = add_player(runner, 4)
        dead.alive = False
        runner.state.morning_ready_open = True
        panel = SimpleNamespace(edit=AsyncMock())
        runner._safe_village_send = AsyncMock(return_value=panel)

        await runner._post_morning_panel()

        runner._safe_village_send.assert_awaited_once()
        self.assertIs(runner.state.morning_panel_message, panel)
        sent_view = runner._safe_village_send.await_args.kwargs["view"]
        self.assertIsInstance(sent_view, MorningReadyView)
        self.assertEqual(sent_view.night_generation, runner.state.night_generation)
        # DMは1通も使わない
        for player in (*players, dead):
            player.member.send.assert_not_awaited()

        # 取りこぼし補完で呼ばれても、掲示済みなら何もしない
        await runner._post_morning_panel()
        runner._safe_village_send.assert_awaited_once()

    async def test_morning_panel_deletes_the_one_left_by_a_restart(self) -> None:
        """再起動後はViewが復元されないので、古いパネルを消してから貼る。"""
        runner = make_runner()
        add_player(runner, 1)
        stale = SimpleNamespace(delete=AsyncMock())
        runner.state.village_channel = SimpleNamespace(
            id=500, get_partial_message=Mock(return_value=stale)
        )
        runner.state.morning_panel_message_id = 555
        runner.state.morning_ready_open = True
        panel = SimpleNamespace(id=777, edit=AsyncMock())
        runner._safe_village_send = AsyncMock(return_value=panel)

        await runner._post_morning_panel()

        runner.state.village_channel.get_partial_message.assert_called_once_with(555)
        stale.delete.assert_awaited_once()
        # 新しいパネルのIDは、掲示直後に永続化しておく
        self.assertEqual(runner.state.morning_panel_message_id, 777)
        runner._persist_room_state.assert_awaited()

        # 閉じたパネルは消す対象にしない (ボタンは無効化済み)
        await runner._close_morning_panel()
        self.assertIsNone(runner.state.morning_panel_message_id)

    async def test_prep_panel_id_is_durable_and_stale_panel_is_deleted_before_repost(self) -> None:
        """役職確認パネルも再起動後に古いボタンを残さない。"""
        runner = make_runner()
        runner.state.phase = Phase.PREPARATION
        add_player(runner, 1)
        stale = SimpleNamespace(delete=AsyncMock())
        runner.state.village_channel = SimpleNamespace(
            id=500, get_partial_message=Mock(return_value=stale)
        )
        runner.state.prep_panel_message_id = 555
        panel = SimpleNamespace(id=777, edit=AsyncMock())
        runner._safe_village_send = AsyncMock(return_value=panel)

        await runner._post_prep_panel()

        runner.state.village_channel.get_partial_message.assert_called_once_with(555)
        stale.delete.assert_awaited_once()
        self.assertEqual(runner.state.prep_panel_message_id, 777)
        self.assertEqual(runner._build_room_snapshot()["prep_panel_message_id"], 777)
        runner._persist_room_state.assert_awaited()

        view = runner._prep_view
        await runner._close_prep_panel()

        self.assertTrue(all(item.disabled for item in view.children))
        self.assertTrue(view.is_finished())
        panel.edit.assert_awaited_once()
        self.assertIsNone(runner.state.prep_panel_message_id)

    async def test_recovered_speech_panel_and_dm_views_are_removed_visually(self) -> None:
        """再起動後の弁明終了・人狼DM操作は、古いボタンを表示から外す。"""
        runner = make_runner()
        wolf = add_player(runner, 1, Role.WEREWOLF)

        speech = SimpleNamespace(edit=AsyncMock())
        runner.state.village_channel = SimpleNamespace(
            get_partial_message=Mock(return_value=speech)
        )
        runner.state.speech_panel_message_id = 444
        await runner._disable_recovered_speech_panel()
        speech.edit.assert_awaited_once_with(view=None)
        self.assertIsNone(runner.state.speech_panel_message_id)

        wolf_dm_message = SimpleNamespace(id=555, edit=AsyncMock())
        runner.state.wolf_dm_messages[wolf.user_id] = wolf_dm_message
        runner.state.wolf_dm_message_ids = {wolf.user_id: [555]}
        await runner._disable_live_wolf_vote_dms()
        wolf_dm_message.edit.assert_awaited_once_with(view=None)
        self.assertEqual(runner.state.wolf_dm_message_ids, {})

        surrendered = SimpleNamespace(edit=AsyncMock())
        wolf.member.dm_channel = SimpleNamespace(
            get_partial_message=Mock(return_value=surrendered)
        )
        runner.state.surrender_dm_message_ids = {wolf.user_id: [666]}
        await runner._disable_persisted_dm_views(
            runner.state.surrender_dm_message_ids, label="サレンダー",
        )
        surrendered.edit.assert_awaited_once_with(view=None)
        self.assertEqual(runner.state.surrender_dm_message_ids, {})

    async def test_persisted_dm_cleanup_fetches_uncached_user(self) -> None:
        """再起動後にキャッシュ外でもAPI取得して古いDM Viewを閉じる。"""
        runner = make_runner()
        message = SimpleNamespace(edit=AsyncMock())
        user = FakeMember(99)
        user.dm_channel = SimpleNamespace(
            get_partial_message=Mock(return_value=message)
        )
        fetch_user = AsyncMock(return_value=user)
        runner.manager.bot = SimpleNamespace(
            get_user=Mock(return_value=None),
            fetch_user=fetch_user,
        )
        runner.state.guild = SimpleNamespace(
            get_member=Mock(return_value=None),
        )
        pending = {99: [555]}

        await runner._disable_persisted_dm_views(pending, label="旧ゲーム")

        fetch_user.assert_awaited_once_with(99)
        message.edit.assert_awaited_once_with(view=None)
        self.assertEqual(pending, {})

    async def test_stale_prep_or_speech_panel_failure_does_not_orphan_its_id(self) -> None:
        """古いパネルを閉じられない時は、IDを上書きせず安全停止する。"""
        denied = discord.Forbidden(Mock(status=403), "denied")

        prep_runner = make_runner()
        prep_stale = SimpleNamespace(delete=AsyncMock(side_effect=denied))
        prep_runner.state.village_channel = SimpleNamespace(
            get_partial_message=Mock(return_value=prep_stale)
        )
        prep_runner.state.prep_panel_message_id = 111
        prep_runner._safe_village_send = AsyncMock()
        prep_runner._stop_for_durability_error = AsyncMock()

        with self.assertRaises(StateDurabilityError):
            await prep_runner._post_prep_panel()

        self.assertEqual(prep_runner.state.prep_panel_message_id, 111)
        prep_runner._safe_village_send.assert_not_awaited()
        prep_runner._stop_for_durability_error.assert_awaited_once()

        speech_runner = make_runner()
        speech_stale = SimpleNamespace(edit=AsyncMock(side_effect=denied))
        speech_runner.state.village_channel = SimpleNamespace(
            get_partial_message=Mock(return_value=speech_stale)
        )
        speech_runner.state.speech_panel_message_id = 222
        speech_runner._stop_for_durability_error = AsyncMock()

        with self.assertRaises(StateDurabilityError):
            await speech_runner._require_recovered_speech_panel_closed()

        self.assertEqual(speech_runner.state.speech_panel_message_id, 222)
        speech_runner._stop_for_durability_error.assert_awaited_once()

    async def test_failed_shutdown_ui_cleanup_survives_empty_lobby_and_retries(self) -> None:
        """API一時失敗後も空LOBBY snapshotがIDを保持し、後で消せる。"""
        runner = make_runner()
        source = runner.state
        source.prep_panel_message_id = 111
        source.wolf_dm_message_ids = {1: [222]}
        source.village_channel = SimpleNamespace(id=500)
        # 終了遷移の再試行で同じ掃除対象が既存バッチにも見えても、
        # 二重登録・二重API呼び出しにしない。
        source.pending_ui_cleanup_batches = [{
            "game_run_id": "run-1",
            "channel_id": 500,
            "channel_message_ids": {"prep_panel_message_id": 111},
            "wolf_dm_message_ids": {1: [222]},
            "surrender_dm_message_ids": {},
        }]

        denied = discord.Forbidden(Mock(status=403), "denied")
        stale_panel = SimpleNamespace(delete=AsyncMock(side_effect=denied))
        dm_message = SimpleNamespace(edit=AsyncMock(side_effect=denied))
        member = FakeMember(1)
        member.dm_channel = SimpleNamespace(
            get_partial_message=Mock(return_value=dm_message)
        )
        cleanup_channel = SimpleNamespace(
            id=500,
            get_partial_message=Mock(return_value=stale_panel),
        )
        guild = SimpleNamespace(
            id=123,
            get_channel=Mock(return_value=cleanup_channel),
            get_member=Mock(return_value=member),
        )
        source.guild = guild

        target = runner._make_empty_lobby_state(source)
        runner.state = target
        runner._persist_room_state = AsyncMock()

        await runner._retry_pending_ui_cleanup()

        self.assertIsNone(target.prep_panel_message_id)
        self.assertEqual(target.wolf_dm_message_ids, {})
        self.assertEqual(
            target.pending_ui_cleanup_batches,
            [{
                "game_run_id": "run-1",
                "channel_id": 500,
                "channel_message_ids": {"prep_panel_message_id": 111},
                "wolf_dm_message_ids": {1: [222]},
                "surrender_dm_message_ids": {},
            }],
        )
        failed_snapshot = runner._build_room_snapshot()
        self.assertIsNone(failed_snapshot["prep_panel_message_id"])
        self.assertEqual(
            failed_snapshot["pending_ui_cleanup_batches"][0]["channel_id"],
            500,
        )

        stale_panel.delete.side_effect = None
        dm_message.edit.side_effect = None
        await runner._retry_pending_ui_cleanup()

        self.assertIsNone(target.prep_panel_message_id)
        self.assertEqual(target.wolf_dm_message_ids, {})
        self.assertEqual(target.pending_ui_cleanup_batches, [])
        self.assertGreaterEqual(runner._persist_room_state.await_count, 2)

    async def test_pending_ui_cleanup_uses_old_channel_without_touching_current_ui(self) -> None:
        """旧ゲームの再掃除が、次ゲームの#昼や現在UI IDへ触れない。"""
        runner = make_runner()
        denied = discord.Forbidden(Mock(status=403), "denied")
        old_message = SimpleNamespace(edit=AsyncMock(side_effect=denied))
        old_channel = SimpleNamespace(
            id=500,
            get_partial_message=Mock(return_value=old_message),
        )
        current_channel = SimpleNamespace(
            id=600,
            get_partial_message=Mock(),
        )
        runner.state.guild = SimpleNamespace(
            id=123,
            get_channel=Mock(side_effect=lambda channel_id: (
                old_channel if channel_id == 500 else current_channel
            )),
            get_member=Mock(return_value=None),
        )
        runner.state.village_channel = current_channel
        runner.state.vote_panel_message_id = 999
        runner.state.pending_ui_cleanup_batches = [{
            "game_run_id": "old-run",
            "channel_id": 500,
            "channel_message_ids": {"vote_panel_message_id": 111},
            "wolf_dm_message_ids": {},
            "surrender_dm_message_ids": {},
        }]
        runner._persist_room_state = AsyncMock()

        await runner._retry_pending_ui_cleanup()

        old_channel.get_partial_message.assert_called_once_with(111)
        current_channel.get_partial_message.assert_not_called()
        self.assertEqual(runner.state.vote_panel_message_id, 999)
        self.assertEqual(
            runner.state.pending_ui_cleanup_batches[0]["channel_id"], 500
        )

    def test_pending_ui_cleanup_snapshot_round_trip_is_best_effort(self) -> None:
        """複数バッチを保ち、不正行やbool IDだけを安全に捨てる。"""
        runner = make_runner()
        runner.state.pending_ui_cleanup_batches = [
            {
                "game_run_id": "run-a",
                "channel_id": 500,
                "channel_message_ids": {"prep_panel_message_id": 111},
                "wolf_dm_message_ids": {1: [222, 222]},
                "surrender_dm_message_ids": {},
            },
            {
                "game_run_id": "run-b",
                "channel_id": 600,
                "channel_message_ids": {"vote_panel_message_id": 333},
                "wolf_dm_message_ids": {},
                "surrender_dm_message_ids": {2: [444]},
            },
        ]
        payload = runner._build_room_snapshot()
        payload["pending_ui_cleanup_batches"].append({
            "channel_id": True,
            "channel_message_ids": [
                {"kind": "vote_panel_message_id", "message_id": False},
                {"kind": "unknown", "message_id": 999},
            ],
            "wolf_dm_message_ids": "broken",
        })

        restored = runner._restore_pending_ui_cleanup_batches(payload)

        self.assertEqual(len(restored), 2)
        self.assertEqual(restored[0]["game_run_id"], "run-a")
        self.assertEqual(restored[0]["wolf_dm_message_ids"], {1: [222]})
        self.assertEqual(restored[1]["channel_id"], 600)
        self.assertEqual(restored[1]["surrender_dm_message_ids"], {2: [444]})

    async def test_pending_ui_cleanup_keeps_unknown_channel_id_for_manual_recovery(self) -> None:
        """壊れたchannel IDで未処理パネルを掃除済みと誤認しない。"""
        runner = make_runner()
        get_channel = Mock(return_value=None)
        runner.state.guild = SimpleNamespace(
            id=123,
            get_channel=get_channel,
            get_member=Mock(return_value=None),
        )
        runner.state.pending_ui_cleanup_batches = [{
            "game_run_id": "old-run",
            "channel_id": None,
            "channel_message_ids": {"vote_panel_message_id": 111},
            "wolf_dm_message_ids": {},
            "surrender_dm_message_ids": {},
        }]
        runner._persist_room_state = AsyncMock()

        await runner._retry_pending_ui_cleanup()

        get_channel.assert_not_called()
        self.assertEqual(
            runner.state.pending_ui_cleanup_batches[0]["channel_message_ids"],
            {"vote_panel_message_id": 111},
        )

    async def test_morning_panel_appears_after_time_and_updates_public_count(self) -> None:
        """夜時間中は出さず、受付開始後は0/Nから押下ごとに更新する。"""
        runner = make_runner()
        player = add_player(runner, 1)
        add_player(runner, 2)
        panel = SimpleNamespace(edit=AsyncMock())
        runner._safe_village_send = AsyncMock(return_value=panel)

        # 夜の制限時間中はパネル自体を出さない。
        await runner._post_morning_panel()
        runner._safe_village_send.assert_not_awaited()

        # 制限時間終了のdurable checkpoint後に0/Nで掲示する。
        runner.state.morning_ready_open = True
        await runner._post_morning_panel()
        posted = runner._safe_village_send.await_args.args[0]
        self.assertIn("0 / 2人", posted)

        feedback, error = await runner.toggle_morning_ready(player.member)
        self.assertIsNone(error)
        self.assertIn("宣言しました", feedback)
        self.assertIn("1 / 2人", feedback)
        # 押下の保存後、同じ公開パネルへ即時反映する。
        panel.edit.assert_awaited_once()
        self.assertIn("1 / 2人", panel.edit.await_args.kwargs["content"])

        # 宣言は一方向で、同じ人が押し直しても取り消せない。
        _, error = await runner.toggle_morning_ready(player.member)
        self.assertIn("取り消しはできません", error)
        self.assertIn(player.user_id, runner.state.morning_ready_ids)

    async def test_morning_panel_close_disables_buttons_then_stops(self) -> None:
        """#昼 に1枚なので、夜明け時にボタンを無効化して閉じられる。

        先に表示を無効化してから stop する。逆順だと、編集が着地するまでの
        数百msに押した人へDiscordの汎用エラーが出る。
        """
        runner = make_runner()
        add_player(runner, 1)
        runner.state.morning_ready_open = True
        panel = SimpleNamespace(edit=AsyncMock())
        runner._safe_village_send = AsyncMock(return_value=panel)

        await runner._post_morning_panel()
        view = runner._morning_view
        self.assertIsNotNone(view)

        await runner._close_morning_panel()

        self.assertIsNone(runner._morning_view)
        self.assertIsNone(runner.state.morning_panel_message)
        self.assertTrue(all(item.disabled for item in view.children))
        self.assertTrue(view.is_finished())
        panel.edit.assert_awaited_once()

    async def test_morning_button_rejects_non_players_before_deferring(self) -> None:
        """公開パネルなので観戦者・死亡者にもボタンが見える。

        defer してから弾くと1押下あたり2回APIを使うため、先に検査する。
        """
        runner = make_runner()
        alive = add_player(runner, 1)
        dead = add_player(runner, 2)
        dead.alive = False
        runner.state.morning_ready_open = True
        view = MorningReadyView(runner)

        for member in (dead.member, FakeMember(99)):
            interaction = SimpleNamespace(
                user=member,
                response=SimpleNamespace(
                    send_message=AsyncMock(), defer=AsyncMock()
                ),
                followup=SimpleNamespace(send=AsyncMock()),
            )
            await view.ready_btn.callback(interaction)
            interaction.response.defer.assert_not_awaited()
            self.assertIn(
                "生存中の参加者", interaction.response.send_message.await_args.args[0]
            )

        # 生存者は通る
        interaction = SimpleNamespace(
            user=alive.member,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await view.ready_btn.callback(interaction)
        interaction.response.defer.assert_awaited_once()
        # 結果は本人にだけ返す
        self.assertTrue(
            interaction.followup.send.await_args.kwargs["ephemeral"]
        )
        self.assertIn(alive.user_id, runner.state.morning_ready_ids)

    async def test_wolf_relay_closes_on_time_limit_not_on_morning_declarations(self) -> None:
        """人狼DMの中継は制限時間で閉じ、朝の宣言状況では開閉しない。"""
        runner = make_runner()
        wolf = add_player(runner, 1, Role.WEREWOLF)
        other_wolf = add_player(runner, 2, Role.WEREWOLF)
        add_player(runner, 3)

        # 制限時間中は中継する
        runner.state.wolf_relay_window_open = True
        self.assertTrue(runner.wolf_relay_open())

        message = SimpleNamespace(
            author=SimpleNamespace(id=wolf.user_id, bot=False),
            channel=Mock(spec=discord.DMChannel),
            content="3を噛もう",
        )
        await runner.on_message(message)
        other_wolf.member.send.assert_awaited_once()

        # 全員が「朝を迎える」を押しても、制限時間内なら中継は続く
        runner.state.morning_ready_ids = {1, 2, 3}
        self.assertTrue(runner.wolf_relay_open())

        # 制限時間が切れたら、夜フェーズのままでも中継を打ち切る
        other_wolf.member.send.reset_mock()
        runner.state.wolf_relay_window_open = False
        self.assertFalse(runner.wolf_relay_open())
        self.assertTrue(runner.night_actions_open())  # 襲撃の選択は続けられる

        await runner.on_message(message)
        other_wolf.member.send.assert_not_awaited()

    async def test_wolf_relay_fits_discord_message_limit(self) -> None:
        """中継はプレフィックス込みで2000字に収める。

        超えるとDiscordが送信自体を拒否し (50035)、_relay_to_wolves の
        except がそれを握り潰すので、相談が黙って仲間に届かなくなる。
        """
        from config import DISCORD_MESSAGE_LIMIT

        runner = make_runner()
        wolf = add_player(runner, 1, Role.WEREWOLF)
        # 表示名は「番号2桁.名前(最大32字)」まで伸びうる
        wolf.base_name = "あ" * 32
        add_player(runner, 2, Role.WEREWOLF)
        runner.state.wolf_relay_window_open = True
        relayed: list[str] = []
        runner._relay_to_wolves = AsyncMock(
            side_effect=lambda text, exclude_id=None: relayed.append(text)
        )

        message = SimpleNamespace(
            author=SimpleNamespace(id=wolf.user_id, bot=False),
            channel=Mock(spec=discord.DMChannel),
            content="x" * DISCORD_MESSAGE_LIMIT,  # 通常ユーザーが送れる上限
        )
        await runner.on_message(message)

        self.assertEqual(len(relayed), 1)
        self.assertLessEqual(len(relayed[0]), DISCORD_MESSAGE_LIMIT)
        self.assertTrue(relayed[0].endswith("…"))  # 切り詰めたことが分かる

        # 収まる長さはそのまま流す
        relayed.clear()
        message.content = "3を噛もう"
        await runner.on_message(message)
        self.assertTrue(relayed[0].endswith("3を噛もう"))

    async def test_wolf_target_change_is_not_broadcast_after_time_limit(self) -> None:
        """制限時間後は襲撃先の変更通知も他の狼へ流さない。

        自由文の中継だけ塞いでも、襲撃先を選び直すだけで合図を送れては
        「中継は制限時間まで」が有名無実になる。
        """
        runner = make_runner()
        wolf = add_player(runner, 1, Role.WEREWOLF)
        add_player(runner, 2, Role.WEREWOLF)
        victim = add_player(runner, 3)
        runner.refresh_wolf_dm_displays = AsyncMock()
        runner._relay_to_wolves = AsyncMock()

        view = WolfVoteView(runner, [victim])
        select = next(
            item for item in view.children if isinstance(item, discord.ui.Select)
        )

        def make_interaction(target_id: int) -> SimpleNamespace:
            response = SimpleNamespace(
                defer=AsyncMock(), send_message=AsyncMock()
            )
            return SimpleNamespace(
                user=wolf.member,
                data={"values": [str(target_id)]},
                response=response,
                followup=SimpleNamespace(send=AsyncMock()),
                edit_original_response=AsyncMock(),
            )

        # 制限時間中の変更は他の狼へ伝わる
        runner.state.phase = Phase.NIGHT
        runner.state.morning_confirmed = False
        runner.state.morning_ready_event.clear()
        runner.state.wolf_relay_window_open = True
        await select.callback(make_interaction(victim.user_id))
        runner._relay_to_wolves.assert_awaited_once()
        runner.refresh_wolf_dm_displays.assert_awaited_once()

        # 制限時間後は本人の選択だけ反映し、他の狼へは流さない
        runner._relay_to_wolves.reset_mock()
        runner.refresh_wolf_dm_displays.reset_mock()
        runner.state.wolf_relay_window_open = False
        interaction = make_interaction(-1)
        await select.callback(interaction)

        self.assertEqual(runner.state.wolf_target, -1)  # 選択自体は通る
        runner._relay_to_wolves.assert_not_awaited()
        runner.refresh_wolf_dm_displays.assert_not_awaited()
        # 表示が食い違うので本人には知らせる
        interaction.followup.send.assert_awaited_once()

    async def test_morning_event_is_not_released_before_checkpoint(self) -> None:
        runner = make_runner()
        player = add_player(runner, 1)
        runner.state.morning_ready_open = True

        async def fail_while_waiter_is_still_blocked() -> None:
            self.assertFalse(runner.state.morning_ready_event.is_set())
            raise RuntimeError("DB down")

        runner._persist_room_state = AsyncMock(
            side_effect=fail_while_waiter_is_still_blocked
        )
        _, error = await runner.toggle_morning_ready(player.member)

        self.assertIsNotNone(error)
        self.assertFalse(runner.state.morning_confirmed)
        self.assertFalse(runner.state.morning_ready_event.is_set())
        self.assertNotIn(player.user_id, runner.state.morning_ready_ids)

    async def test_force_morning_event_waits_for_checkpoint(self) -> None:
        runner = make_runner()
        gm = add_player(runner, 1)
        runner.state.gm_id = gm.user_id
        runner.state.morning_ready_open = True

        async def fail_while_waiter_is_still_blocked() -> None:
            self.assertFalse(runner.state.morning_ready_event.is_set())
            raise RuntimeError("DB down")

        runner._persist_room_state = AsyncMock(
            side_effect=fail_while_waiter_is_still_blocked
        )
        _, error = await runner.force_morning(gm.member)

        self.assertIsNotNone(error)
        self.assertFalse(runner.state.morning_confirmed)
        self.assertFalse(runner.state.morning_ready_event.is_set())

    async def test_panel_failure_fallback_event_waits_for_checkpoint(self) -> None:
        runner = make_runner()
        add_player(runner, 1)
        runner.state.morning_panel_message = None
        runner._post_morning_panel = AsyncMock()

        async def fail_while_waiter_is_still_blocked() -> None:
            self.assertFalse(runner.state.morning_ready_event.is_set())
            raise RuntimeError("DB down")

        runner._persist_room_state = AsyncMock(
            side_effect=fail_while_waiter_is_still_blocked
        )
        with self.assertRaisesRegex(RuntimeError, "DB down"):
            await runner._wait_for_morning()

        self.assertFalse(runner.state.morning_confirmed)
        self.assertFalse(runner.state.morning_ready_event.is_set())

    async def test_seer_action_log_is_inside_same_checkpoint_and_rolls_back(self) -> None:
        runner = make_runner()
        seer = add_player(runner, 1, Role.SEER)
        target = add_player(runner, 2, Role.WEREWOLF)
        captured: list[list[dict]] = []

        async def capture_checkpoint() -> None:
            captured.append([dict(item) for item in runner.state.action_log])

        runner._persist_room_state = AsyncMock(side_effect=capture_checkpoint)
        text, committed = await runner.commit_seer_target(seer.user_id, target.user_id)

        self.assertTrue(committed, text)
        self.assertEqual(captured[0][-1]["kind"], "占い")
        self.assertIn("結果=人狼", captured[0][-1]["detail"])

        failed = make_runner()
        failed_seer = add_player(failed, 1, Role.SEER)
        failed_target = add_player(failed, 2, Role.VILLAGER)
        failed._persist_room_state = AsyncMock(side_effect=RuntimeError("DB down"))
        _, committed = await failed.commit_seer_target(
            failed_seer.user_id, failed_target.user_id
        )
        self.assertFalse(committed)
        self.assertIsNone(failed.state.seer_target)
        self.assertEqual(failed.state.action_log, [])

    async def test_guard_and_wolf_logs_roll_back_when_checkpoint_fails(self) -> None:
        guard_runner = make_runner()
        guard = add_player(guard_runner, 1, Role.GUARD)
        target = add_player(guard_runner, 2, Role.VILLAGER)
        guard_runner._persist_room_state = AsyncMock(side_effect=RuntimeError("DB down"))

        _, committed = await guard_runner.commit_guard_target(guard.user_id, target.user_id)

        self.assertFalse(committed)
        self.assertIsNone(guard_runner.state.guard_target)
        self.assertEqual(guard_runner.state.action_log, [])

        wolf_runner = make_runner()
        wolf = add_player(wolf_runner, 1, Role.WEREWOLF)
        victim = add_player(wolf_runner, 2, Role.VILLAGER)
        wolf_view = WolfVoteView(wolf_runner, [victim])
        wolf_runner._persist_room_state = AsyncMock(side_effect=RuntimeError("DB down"))
        interaction = SimpleNamespace(
            user=wolf.member,
            data={"values": [str(victim.user_id)]},
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        await wolf_view.select_callback(interaction)

        self.assertIsNone(wolf_runner.state.wolf_target)
        self.assertEqual(wolf_runner.state.wolf_voters, {})
        self.assertEqual(wolf_runner.state.action_log, [])

    async def test_fresh_night_resets_and_recovered_night_preserves_actions(self) -> None:
        async def prepare(runner: RoomRunner) -> None:
            add_player(runner, 1)
            runner._lock_village = AsyncMock()
            runner._mute_phase = AsyncMock()
            runner._post_morning_panel = AsyncMock()
            runner._pausable_countdown = AsyncMock(return_value=True)
            runner._wait_for_morning = AsyncMock()
            runner._close_morning_panel = AsyncMock()

        fresh = make_runner()
        await prepare(fresh)
        fresh.state.wolf_target = 9
        fresh.state.wolf_voters = {8: 9}
        fresh.state.seer_target = 7
        fresh.state.guard_target = 6
        fresh.state.morning_ready_ids = {1}
        fresh.state.morning_warned_ids = {1}
        old_generation = fresh.state.night_generation
        await fresh._night_phase(resume_existing=False)
        self.assertEqual(fresh.state.night_generation, old_generation + 1)
        self.assertIsNone(fresh.state.wolf_target)
        self.assertEqual(fresh.state.wolf_voters, {})
        self.assertIsNone(fresh.state.seer_target)
        self.assertIsNone(fresh.state.guard_target)
        self.assertEqual(fresh.state.morning_ready_ids, set())
        self.assertEqual(fresh.state.morning_warned_ids, set())

        recovered = make_runner()
        await prepare(recovered)
        recovered.state.wolf_target = 9
        recovered.state.wolf_voters = {8: 9}
        recovered.state.seer_target = 7
        recovered.state.guard_target = 6
        recovered.state.morning_ready_ids = {99}
        recovered.state.morning_warned_ids = {1}
        old_generation = recovered.state.night_generation
        await recovered._night_phase(resume_existing=True)
        self.assertEqual(recovered.state.night_generation, old_generation)
        self.assertEqual(recovered.state.wolf_target, 9)
        self.assertEqual(recovered.state.wolf_voters, {8: 9})
        self.assertEqual(recovered.state.seer_target, 7)
        self.assertEqual(recovered.state.guard_target, 6)
        self.assertEqual(recovered.state.morning_ready_ids, {99})
        self.assertEqual(recovered.state.morning_warned_ids, {1})

    async def test_recovered_morning_wait_does_not_replay_night_start_se(self) -> None:
        """朝パネル待機まで進んだ夜は、復元しても夜開始SEを鳴らさない。"""
        runner = make_runner()
        add_player(runner, 1)
        runner.state.morning_ready_open = True
        runner._lock_village = AsyncMock()
        runner._mute_phase = AsyncMock()
        runner._post_morning_panel = AsyncMock()
        runner._wait_for_morning = AsyncMock()
        runner._close_morning_panel = AsyncMock()
        runner._play_se = Mock()

        await runner._night_phase(resume_existing=True)

        self.assertNotIn(
            call("night"), runner._play_se.call_args_list
        )
        runner._play_se.assert_called_once_with("morning")

    async def test_wolf_relay_timer_starts_only_after_all_night_panels(self) -> None:
        runner = make_runner()
        add_player(runner, 1, Role.WEREWOLF)
        add_player(runner, 2, Role.VILLAGER)
        runner._lock_village = AsyncMock()
        runner._mute_phase = AsyncMock()
        runner._close_morning_panel = AsyncMock()

        async def panels_ready() -> None:
            self.assertFalse(runner.state.wolf_relay_window_open)

        async def countdown(*_args, **_kwargs) -> None:
            self.assertTrue(runner.state.wolf_relay_window_open)

        async def wait_for_morning() -> None:
            self.assertFalse(runner.state.wolf_relay_window_open)

        runner._post_morning_panel = AsyncMock(side_effect=panels_ready)
        runner._pausable_countdown = AsyncMock(side_effect=countdown)
        runner._wait_for_morning = AsyncMock(side_effect=wait_for_morning)

        await runner._night_phase()

        self.assertFalse(runner.state.wolf_relay_window_open)

    async def test_resolved_night_is_not_executed_twice(self) -> None:
        runner = make_runner()
        victim = add_player(runner, 1)
        runner.state.wolf_target = victim.user_id
        runner.state.night_resolved = True
        runner._execute_player = AsyncMock()

        result = await runner._process_night()

        self.assertIsNone(result)
        runner._execute_player.assert_not_awaited()

    async def test_day_checkpoint_recovery_skips_same_day_vote(self) -> None:
        runner = make_runner()
        add_player(runner, 1)
        runner.state.phase = Phase.DAY_VOTE
        runner.state.phase_before_pause = Phase.DAY_VOTE
        runner.state.recovery_phase = Phase.DAY_VOTE
        runner.state.recovered_from_restart = True
        runner.state.day_execution_resolved = True
        runner.state.night_resolved = True
        runner.state.check_win = lambda: None
        runner._morning_log = AsyncMock()
        runner._day_vote = AsyncMock()

        async def stop_after_checkpoint():
            raise asyncio.CancelledError

        runner._day_discussion = stop_after_checkpoint
        await runner._resume_recovered_game()
        runner._day_vote.assert_not_awaited()
        runner._morning_log.assert_awaited_once()

    async def test_day_vote_recovery_reposts_village_panel(self) -> None:
        """DAY_VOTEから再開したとき、_day_discussionを通らない経路でも
        村パネル (post_village_panel) が貼り直されること。

        DAY_VOTE / DAY_RUNOFF_* / DAY_LAST_WILL / MORNING からの再開は
        _day_discussion / _night_phase を経由しないため、対策前は
        CO・結果公開等のボタンが夜まで一切使えなくなる不具合があった。
        """
        runner = make_runner()
        add_player(runner, 1)
        runner.state.phase = Phase.DAY_VOTE
        runner.state.phase_before_pause = Phase.DAY_VOTE
        runner.state.recovery_phase = Phase.DAY_VOTE
        runner.state.recovered_from_restart = True
        runner.state.day_execution_resolved = True
        runner.state.night_resolved = True
        runner.state.check_win = lambda: None
        runner._morning_log = AsyncMock()
        runner._day_vote = AsyncMock()
        runner.post_village_panel = AsyncMock()

        async def stop_after_checkpoint():
            raise asyncio.CancelledError

        runner._day_discussion = stop_after_checkpoint
        await runner._resume_recovered_game()

        runner.post_village_panel.assert_awaited_once()

    async def test_lobby_recovery_does_not_repost_village_panel(self) -> None:
        """LOBBY等の非進行フェーズではpost_village_panelを呼ばないこと。"""
        runner = make_runner()
        add_player(runner, 1)
        runner.state.phase = Phase.LOBBY
        runner.state.phase_before_pause = Phase.LOBBY
        runner.state.recovery_phase = Phase.LOBBY
        runner.state.recovered_from_restart = True
        runner.post_village_panel = AsyncMock()
        runner._day_discussion = AsyncMock(side_effect=asyncio.CancelledError)

        await runner._resume_recovered_game()

        runner.post_village_panel.assert_not_awaited()

    async def test_vote_is_persisted_and_removed_voter_does_not_block_completion(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        runner.state.day_generation = 4
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        removed = add_player(runner, 3)
        runner.state.vote_order = [second.user_id]
        runner.state.vote_slot_index = 0
        runner.state.vote_slot_token = 1
        runner.state.vote_slot_active = True
        runner.state.vote_speech_finished = True
        runner.state.current_speaker_id = second.user_id
        view = VoteView(runner, [first, second, removed], [first, second, removed])
        removed.alive = False
        runner.state.votes = {first.user_id: second.user_id}
        interaction = SimpleNamespace(
            user=second.member,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        button = next(item for item in view.children if item.custom_id == "vote_1")

        await button.callback(interaction)
        confirm_view = interaction.followup.send.await_args.kwargs["view"]
        confirm_interaction = SimpleNamespace(
            user=second.member,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        confirm_button = next(
            item for item in confirm_view.children if item.label == "この人に投票"
        )
        await confirm_button.callback(confirm_interaction)

        self.assertEqual(runner.state.votes[second.user_id], first.user_id)
        runner._persist_room_state.assert_awaited()
        self.assertTrue(runner.state.vote_choice_event.is_set())
        interaction.response.defer.assert_awaited_once()
        confirm_interaction.response.defer.assert_awaited_once()
        # 確定結果は新しいephemeralではなく確認メッセージの書き換えで残す
        confirm_interaction.followup.send.assert_not_awaited()
        self.assertIn(
            "投票しました",
            confirm_interaction.edit_original_response.await_args.kwargs["content"],
        )

    async def test_vote_persist_failure_rolls_back_and_allows_retry(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        voter = add_player(runner, 1)
        target = add_player(runner, 2)
        runner.state.vote_order = [voter.user_id]
        runner.state.vote_slot_token = 1
        runner.state.vote_slot_active = True
        runner.state.vote_speech_finished = True
        runner.state.current_speaker_id = voter.user_id
        view = VoteView(runner, [voter, target], [voter, target])
        runner._persist_room_state = AsyncMock(side_effect=RuntimeError("DB down"))
        interaction = SimpleNamespace(
            user=voter.member,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        button = next(item for item in view.children if item.custom_id == "vote_2")

        await button.callback(interaction)
        confirm_view = interaction.followup.send.await_args.kwargs["view"]
        confirm_interaction = SimpleNamespace(
            user=voter.member,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        confirm_button = next(
            item for item in confirm_view.children if item.label == "この人に投票"
        )
        await confirm_button.callback(confirm_interaction)

        self.assertNotIn(voter.user_id, runner.state.votes)
        self.assertFalse(runner.state.vote_complete_event.is_set())
        self.assertIn(
            "もう一度投票",
            confirm_interaction.edit_original_response.await_args.kwargs["content"],
        )
        self.assertEqual(runner.state.action_log, [])
        # 保存に失敗したら確定せず、確認ボタンを押し直せる状態で残す
        self.assertFalse(confirm_view.is_finished())
        self.assertFalse(any(item.disabled for item in confirm_view.children))

    async def test_vote_is_not_recorded_before_confirmation(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        voter = add_player(runner, 1)
        target = add_player(runner, 2)
        runner.state.vote_order = [voter.user_id]
        runner.state.vote_slot_token = 1
        runner.state.vote_slot_active = True
        runner.state.vote_speech_finished = True
        runner.state.current_speaker_id = voter.user_id
        view = VoteView(runner, [voter, target], [voter, target])
        interaction = SimpleNamespace(
            user=voter.member,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        button = next(item for item in view.children if item.custom_id == "vote_2")
        await button.callback(interaction)

        self.assertNotIn(voter.user_id, runner.state.votes)
        runner._persist_room_state.assert_not_awaited()

    async def test_finished_channel_moves_to_the_log_category(self) -> None:
        """終了した卓は削除せず、試合番号を付けてログカテゴリへ移す。"""
        runner = make_runner()
        default = FakeRole(1, "@everyone", default=True)
        category = FakeLogCategory(
            "ログ-昼",
            overwrites={default: runner._public_log_overwrite()},
        )
        guild = SimpleNamespace(
            id=1, default_role=default,
            categories=[category], create_category=AsyncMock(),
        )
        runner.state.guild = guild
        channel = SimpleNamespace(name="昼", id=500, edit=AsyncMock())

        moved = await runner._archive_game_channel(
            channel, "ログ-昼", 4,
        )

        self.assertTrue(moved)
        guild.create_category.assert_not_awaited()  # 既存を使い回す
        kwargs = channel.edit.await_args.kwargs
        # 番号を先頭に置く (Discordはカテゴリ内を名前順に並べる)
        self.assertEqual(kwargs["name"], "04-昼")
        self.assertIs(kwargs["category"], category)
        self.assertNotIn("sync_permissions", kwargs)
        self.assertEqual(kwargs["overwrites"], category.overwrites)
        category.edit.assert_not_awaited()

    async def test_existing_log_category_permissions_are_normalized(self) -> None:
        """既存の個別allow/denyも公開・書込不可へ補正してから使用する。"""
        runner = make_runner()
        default = FakeRole(1, "@everyone", default=True)
        stale_role = FakeRole(2, "過去の許可ロール")
        stale_member = FakeMember(3)
        bot_member = FakeMember(99, "bot")
        bot_member.bot = True
        category = FakeLogCategory(
            "ログ-昼",
            overwrites={
                stale_role: discord.PermissionOverwrite(
                    view_channel=False, send_messages=True, manage_messages=True,
                ),
                stale_member: discord.PermissionOverwrite(
                    create_public_threads=True, send_messages_in_threads=True,
                ),
            },
        )
        runner.state.guild = SimpleNamespace(
            id=1, default_role=default, me=bot_member,
            categories=[category], create_category=AsyncMock(),
        )

        result = await runner._ensure_log_category("ログ-昼")

        self.assertIs(result, category)
        category.edit.assert_awaited_once()
        for target in (default, stale_role, stale_member):
            overwrite = category.overwrites[target]
            self.assertTrue(overwrite.view_channel)
            self.assertTrue(overwrite.read_message_history)
            self.assertFalse(overwrite.send_messages)
            self.assertFalse(overwrite.add_reactions)
            self.assertFalse(overwrite.create_public_threads)
            self.assertFalse(overwrite.create_private_threads)
            self.assertFalse(overwrite.send_messages_in_threads)
            self.assertFalse(overwrite.send_voice_messages)
            self.assertFalse(overwrite.send_polls)
            self.assertFalse(overwrite.use_application_commands)
            self.assertFalse(overwrite.use_external_apps)
            self.assertFalse(overwrite.manage_channels)
            self.assertFalse(overwrite.manage_roles)
            self.assertFalse(overwrite.manage_messages)
            self.assertFalse(overwrite.manage_threads)
            self.assertFalse(overwrite.manage_webhooks)
        self.assertTrue(category.overwrites[bot_member].view_channel)
        self.assertTrue(category.overwrites[bot_member].manage_channels)
        self.assertTrue(category.overwrites[bot_member].manage_roles)
        self.assertFalse(category.overwrites[bot_member].send_messages)

    async def test_log_category_permission_failure_falls_back_to_delete(self) -> None:
        """既存カテゴリの権限を保証できなければ、そのカテゴリを使用しない。"""
        runner = make_runner()
        default = FakeRole(1, "@everyone", default=True)
        category = FakeLogCategory("ログ-昼")
        category.edit = AsyncMock(
            side_effect=discord.Forbidden(Mock(status=403), "denied")
        )
        runner.state.guild = SimpleNamespace(
            id=1, default_role=default,
            categories=[category], create_category=AsyncMock(),
        )

        result = await runner._ensure_log_category("ログ-昼")

        self.assertIsNone(result)

    async def test_unresolved_log_overwrite_target_is_normalized_atomically(self) -> None:
        """キャッシュ外の対象も型付きObjectのまま一括上書きへ含める。"""
        runner = make_runner()
        default = FakeRole(1, "@everyone", default=True)
        unresolved = discord.Object(id=999, type=discord.Role)
        category = FakeLogCategory(
            "ログ-昼",
            overwrites={unresolved: discord.PermissionOverwrite(send_messages=True)},
        )
        runner.state.guild = SimpleNamespace(
            id=1, default_role=default,
            categories=[category], create_category=AsyncMock(),
        )

        result = await runner._ensure_log_category("ログ-昼")

        self.assertIs(result, category)
        category.edit.assert_awaited_once()
        self.assertFalse(category.overwrites[unresolved].send_messages)

    async def test_existing_unsynced_log_channel_is_normalized(self) -> None:
        """カテゴリと非同期の既存ログも同じ読み取り専用上書きへ戻す。"""
        runner = make_runner()
        default = FakeRole(1, "@everyone", default=True)
        desired = {default: runner._public_log_overwrite()}
        child = FakeLogChannel(
            "01-昼", 100,
            overwrites={default: discord.PermissionOverwrite(send_messages=True)},
        )
        category = FakeLogCategory(
            "ログ-昼", channels=[child], overwrites=desired,
        )
        runner.state.guild = SimpleNamespace(
            id=1, default_role=default,
            categories=[category], create_category=AsyncMock(),
        )

        result = await runner._ensure_log_category("ログ-昼")

        self.assertIs(result, category)
        child.edit.assert_awaited_once()
        self.assertEqual(child.overwrites, desired)

    async def test_unmanaged_same_name_category_is_not_adopted(self) -> None:
        """同名でも管理外の子を含むカテゴリは公開化・整理しない。"""
        runner = make_runner()
        default = FakeRole(1, "@everyone", default=True)
        manual = FakeLogChannel("手動メモ", 100)
        category = FakeLogCategory("ログ-昼", channels=[manual])
        runner.state.guild = SimpleNamespace(
            id=1, default_role=default,
            categories=[category], create_category=AsyncMock(),
        )

        result = await runner._ensure_log_category("ログ-昼")

        self.assertIsNone(result)
        category.edit.assert_not_awaited()
        manual.edit.assert_not_awaited()

    async def test_spirit_log_category_is_not_managed_anymore(self) -> None:
        """#霊界 は退避しない。過去の「ログ-霊界」もBotは触らない。"""
        runner = make_runner()
        self.assertFalse(
            runner._is_managed_log_channel_name(LOG_CATEGORY_SPIRIT, "04-霊界")
        )

        default = FakeRole(1, "@everyone", default=True)
        old_log = FakeLogChannel("04-霊界", 100)
        category = FakeLogCategory(LOG_CATEGORY_SPIRIT, channels=[old_log])
        runner.state.guild = SimpleNamespace(
            id=1, default_role=default,
            categories=[category], create_category=AsyncMock(),
        )

        result = await runner._ensure_log_category(LOG_CATEGORY_SPIRIT)

        self.assertIsNone(result)
        category.edit.assert_not_awaited()
        old_log.edit.assert_not_awaited()
        old_log.delete.assert_not_awaited()

    async def test_archive_uses_safe_overwrites_without_waiting_for_gateway_cache(self) -> None:
        """カテゴリPATCH後も旧キャッシュからunsafe権限をコピーしない。"""
        runner = make_runner()
        default = FakeRole(1, "@everyone", default=True)
        category = FakeLogCategory(
            "ログ-昼",
            overwrites={default: discord.PermissionOverwrite(send_messages=True)},
        )
        # 実discord.pyのedit同様、REST成功だけを返して元オブジェクトは更新しない。
        category.edit = AsyncMock(return_value=category)
        runner.state.guild = SimpleNamespace(
            id=1, default_role=default,
            categories=[category], create_category=AsyncMock(),
        )
        channel = SimpleNamespace(name="昼", id=500, edit=AsyncMock())

        moved = await runner._archive_game_channel(
            channel, "ログ-昼", 4,
        )

        self.assertTrue(moved)
        overwrite = channel.edit.await_args.kwargs["overwrites"][default]
        self.assertTrue(overwrite.view_channel)
        self.assertFalse(overwrite.send_messages)

    async def test_all_room_types_archive_to_public_log(self) -> None:
        """総合・手動権限の固定卓・GM名前村を同じ公開ログへ退避する。"""
        rooms = (
            RoomDefinition("general", "総合"),
            RoomDefinition("local", "ねいとくん村", sync_permissions=False),
            RoomDefinition(
                "private", "GM名前村",
                private_owner_id=10,
            ),
        )
        for room in rooms:
            with self.subTest(room=room.room_id):
                runner = RoomRunner(None, FakeManager(), room)
                self.assertTrue(runner._can_archive_to_public_log())

    async def test_log_category_is_trimmed_when_it_hits_the_limit(self) -> None:
        """上限50に達したら古い順に40まで減らす (IDの昇順 = 作成順)。"""
        runner = make_runner()
        default = FakeRole(1, "@everyone", default=True)
        desired = {default: runner._public_log_overwrite()}
        old = [
            FakeLogChannel(f"{i:02d}-昼", 100 + i, overwrites=desired)
            for i in range(LOG_CATEGORY_LIMIT)
        ]
        category = FakeLogCategory(
            "ログ-昼", channels=list(reversed(old)),
            overwrites=desired,
        )
        runner.state.guild = SimpleNamespace(
            id=1, default_role=default,
            categories=[category], create_category=AsyncMock(),
        )
        channel = SimpleNamespace(name="昼", id=9999, edit=AsyncMock())

        await runner._archive_game_channel(
            channel, "ログ-昼", 51,
        )

        deleted = [ch for ch in old if ch.delete.await_count]
        self.assertEqual(len(deleted), LOG_CATEGORY_LIMIT - LOG_CATEGORY_TRIM_TO)
        # 消えるのは古い順 (IDが小さいほう)
        self.assertEqual([ch.id for ch in deleted], [100 + i for i in range(len(deleted))])

    async def test_archive_failure_falls_back_to_deleting(self) -> None:
        """カテゴリを用意できなければ False を返し、呼び出し側が削除へ倒す。"""
        runner = make_runner()
        runner.state.guild = SimpleNamespace(
            id=1, categories=[],
            default_role=FakeRole(1, "@everyone", default=True),
            create_category=AsyncMock(side_effect=discord.Forbidden(Mock(status=403), "denied")),
        )
        channel = SimpleNamespace(name="昼", id=500, edit=AsyncMock())

        moved = await runner._archive_game_channel(
            channel, "ログ-昼", 4,
        )

        self.assertFalse(moved)
        channel.edit.assert_not_awaited()

    async def test_game_start_snapshots_original_access_boundary(self) -> None:
        """ゲーム中の閲覧境界は、終了ログの公開とは別に開始時点で保存する。"""
        for strict, expected in ((False, True), (True, False)):
            with self.subTest(strict=strict):
                room = RoomDefinition(
                    "test",
                    "テスト村",
                    strict_access_role_names=(frozenset({"ねいと"}) if strict else None),
                )
                runner = RoomRunner(None, FakeManager(), room)
                runner.manager.rooms[room.room_id] = runner
                for user_id in range(1, 14):
                    add_player(runner, user_id)
                game_vc = SimpleNamespace(
                    id=500,
                    members=[player.member for player in runner.state.players.values()],
                )
                for player in runner.state.players.values():
                    player.member.voice = SimpleNamespace(channel=game_vc)
                runner.state.voice_channel = game_vc
                runner._create_game_channels = AsyncMock(
                    side_effect=RuntimeError("test stop"),
                )
                runner._post_lobby_ui = AsyncMock()
                interaction = SimpleNamespace(
                    guild=SimpleNamespace(
                        get_member=lambda user_id: runner.state.players[user_id].member,
                    ),
                    followup=SimpleNamespace(send=AsyncMock()),
                )

                await runner._start_game_locked(interaction)

                self.assertEqual(runner.state.public_log_archive_allowed, expected)
                self.assertEqual(
                    runner._build_room_snapshot()["public_log_archive_allowed"],
                    expected,
                )

    async def test_start_failure_schedules_lobby_panel_recovery(self) -> None:
        """開始失敗でLOBBYへ戻すとき、受付パネルの再掲も落ちたら再試行を予約する。

        ゲームチャンネル作成が失敗した直後はDiscord側が不調なことが多く、
        続く _post_lobby_ui も落ちやすい。ここを黙って捨てると、参加者が
        残ったままのLOBBYから参加/取消・GM管理の操作口だけが消える。
        """
        runner = RoomRunner(None, FakeManager(), RoomDefinition("test", "テスト村"))
        runner.manager.rooms["test"] = runner
        for user_id in range(1, 14):
            add_player(runner, user_id)
        game_vc = SimpleNamespace(
            id=500,
            members=[player.member for player in runner.state.players.values()],
        )
        for player in runner.state.players.values():
            player.member.voice = SimpleNamespace(channel=game_vc)
        runner.state.voice_channel = game_vc
        runner.state.recruitment_id = 42
        runner._create_game_channels = AsyncMock(
            side_effect=RuntimeError("チャンネル作成失敗"),
        )
        runner._post_lobby_ui = AsyncMock(side_effect=RuntimeError("再掲も失敗"))
        runner._schedule_lobby_panel_recovery = Mock()
        interaction = SimpleNamespace(
            guild=SimpleNamespace(
                get_member=lambda user_id: runner.state.players[user_id].member,
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await runner._start_game_locked(interaction)

        self.assertEqual(runner.state.phase, Phase.LOBBY)
        # 参加者は残したままなので、操作口を失わせない。
        self.assertEqual(len(runner.state.players), 13)
        runner._post_lobby_ui.assert_awaited()
        runner._schedule_lobby_panel_recovery.assert_called_once()
        kwargs = runner._schedule_lobby_panel_recovery.call_args.kwargs
        self.assertEqual(kwargs["recruitment_id"], 42)

    async def test_final_preparation_checkpoint_failure_stops_for_resume(self) -> None:
        """役職配布後の最終保存だけ落ちてもPREPARATIONを捨てず再開できる。"""
        runner = make_runner()
        runner.state.phase = Phase.PREPARATION
        runner.state.game_task = None
        failure = RuntimeError("final checkpoint down")
        # 1回目が開始完了checkpoint、2回目が耐久停止checkpoint。
        runner._persist_room_state = AsyncMock(side_effect=[failure, None])

        with self.assertRaisesRegex(
            StateDurabilityError, "開始準備完了状態を保存できません",
        ):
            await runner._persist_preparation_ready_or_stop()

        self.assertTrue(runner.state.paused)
        self.assertEqual(runner.state.phase, Phase.PAUSED)
        self.assertEqual(runner.state.phase_before_pause, Phase.PREPARATION)
        self.assertEqual(runner.state.recovery_phase, Phase.PREPARATION)
        self.assertTrue(runner.state.recovered_from_restart)
        self.assertIsNone(runner.state.game_task)
        self.assertEqual(runner._persist_room_state.await_count, 2)

        # DB復旧後のGM再開が、通常ゲームではなく開始復元タスクを1本だけ作る。
        runner._persist_room_state = AsyncMock()
        runner._sync_server_mutes = AsyncMock(return_value=[])
        runner._await_mute_applied = AsyncMock(return_value=True)
        runner._resume_preparation = AsyncMock()

        result = await runner.resume_game()
        await runner.state.game_task

        self.assertIn("復元ゲームを再開", result)
        runner._resume_preparation.assert_awaited_once()

    async def test_restricted_snapshot_keeps_access_marker_but_archives_log(self) -> None:
        """限定中の閲覧境界は維持しつつ、終了ログは公開する。"""
        source = RoomRunner(
            None,
            FakeManager(),
            RoomDefinition(
                "test", "テスト村", strict_access_role_names=frozenset({"ねいと"}),
            ),
        )
        source.state.public_log_archive_allowed = False
        snapshot = source._build_room_snapshot()
        snapshot["phase"] = Phase.LOBBY.name

        restored = make_runner()
        restored._delete_alive_role = AsyncMock()
        restored._post_lobby_ui = AsyncMock()

        await restored.restore_from_snapshot(snapshot)

        self.assertFalse(restored.state.public_log_archive_allowed)
        self.assertTrue(restored._can_archive_to_public_log())

    async def test_missing_access_marker_stays_safe_but_archives_log(self) -> None:
        """欠損した閲覧境界は安全側へ倒しつつ、終了ログは公開する。"""
        runner = make_runner()
        runner.state.public_log_archive_allowed = True
        runner._delete_alive_role = AsyncMock()
        runner._post_lobby_ui = AsyncMock()

        await runner.restore_from_snapshot({
            "variant_id": "v13_cross",
            "phase": Phase.LOBBY.name,
        })

        self.assertFalse(runner.state.public_log_archive_allowed)
        self.assertTrue(runner._can_archive_to_public_log())

    async def test_only_current_sequential_voter_can_vote(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        voter = add_player(runner, 1)
        first = add_player(runner, 2)
        second = add_player(runner, 3)
        alive = [voter, first, second]
        runner.state.vote_order = [voter.user_id, first.user_id, second.user_id]
        runner.state.vote_slot_index = 0
        runner.state.vote_slot_token = 4
        runner.state.vote_slot_active = True
        runner.state.vote_speech_finished = True
        runner.state.current_speaker_id = voter.user_id
        final = VoteView(runner, alive, alive)
        self.assertIsNone(final._vote_error(voter.user_id, first.user_id))
        self.assertEqual(
            final._vote_error(first.user_id, second.user_id),
            "自分の番になってから投票できます。",
        )

    async def test_sequential_choice_panel_does_not_show_the_voter(self) -> None:
        """自分には投票できない防御に加え、最初から本人ボタンを表示しない。"""
        runner = make_runner()
        voter = add_player(runner, 1)
        first = add_player(runner, 2)
        second = add_player(runner, 3)
        runner.state.phase = Phase.DAY_VOTE
        runner.state.day_generation = 4
        runner.state.vote_day_generation = 4
        runner.state.vote_order = [voter.user_id]
        runner.state.vote_slot_index = 0
        runner._vote_queue_waiting = Mock(return_value=[])
        runner._grant_speaker = AsyncMock()
        runner._clear_speaker = AsyncMock()
        runner._play_transition_se = AsyncMock()
        runner._resolve_day_vote = AsyncMock(return_value=None)
        views_seen: list[object] = []

        async def replace_panel(message, content, view):
            if view is not None:
                views_seen.append(view)
            return SimpleNamespace(id=999)

        async def finish_speech(*_args, **_kwargs) -> bool:
            return False

        async def choose_target(_event) -> None:
            runner.state.votes[voter.user_id] = first.user_id
            runner.state.vote_choice_event.set()

        runner._replace_sequential_vote_panel = AsyncMock(side_effect=replace_panel)
        runner._pausable_countdown = AsyncMock(side_effect=finish_speech)
        runner._pausable_wait_forever = AsyncMock(side_effect=choose_target)

        await runner._day_vote_sequential()

        choice_view = next(view for view in views_seen if isinstance(view, VoteView))
        candidate_ids = {
            item.custom_id
            for item in choice_view.children
            if getattr(item, "custom_id", "").startswith("vote_")
        }
        self.assertNotIn(f"vote_{voter.user_id}", candidate_ids)
        self.assertEqual(
            candidate_ids,
            {f"vote_{first.user_id}", f"vote_{second.user_id}"},
        )

    async def test_current_vote_is_visible_before_cursor_advances(self) -> None:
        runner = make_runner()
        voter = add_player(runner, 1)
        target = add_player(runner, 2)
        runner.state.phase = Phase.DAY_VOTE
        runner.state.vote_order = [voter.user_id, target.user_id]
        runner.state.vote_slot_index = 0
        runner.state.vote_slot_active = True
        runner.state.current_speaker_id = voter.user_id
        runner.state.votes = {voter.user_id: target.user_id}

        detail = runner._sequential_vote_detail()

        self.assertIn(f"{voter.display_name} → {target.display_name}", detail)

    async def test_vote_publication_failure_stops_without_advancing(self) -> None:
        runner = make_runner()
        voter = add_player(runner, 1)
        target = add_player(runner, 2)
        runner.state.phase = Phase.DAY_VOTE
        runner.state.vote_order = [voter.user_id, target.user_id]
        runner.state.vote_slot_index = 0
        runner.state.vote_slot_token = 1
        runner.state.vote_slot_active = True
        runner.state.vote_speech_finished = True
        runner.state.current_speaker_id = voter.user_id
        view = VoteView(runner, [voter, target], [voter])
        runner._refresh_sequential_vote_panel = AsyncMock(return_value=False)

        result, committed = await view.commit_vote(voter.user_id, target.user_id)

        self.assertTrue(committed)
        self.assertIn("安全停止", result)
        self.assertEqual(runner.state.votes[voter.user_id], target.user_id)
        self.assertEqual(runner.state.vote_slot_index, 0)
        self.assertTrue(runner.state.vote_choice_event.is_set())

    async def test_gm_skip_ends_current_vote_speech(self) -> None:
        runner = make_runner()
        voter = add_player(runner, 1)
        gm = SimpleNamespace(id=99)
        runner.state.gm_id = gm.id
        runner.state.phase = Phase.DAY_VOTE
        runner.state.vote_slot_active = True
        runner.state.current_speaker_id = voter.user_id

        result = await runner.force_skip_wait(gm)

        self.assertIn("投票発言", result)
        self.assertTrue(runner.state.speech_done_event.is_set())

    async def test_vote_speech_timeout_waits_for_choice_without_abstention(self) -> None:
        """30秒経過はミュートまで。棄権せず投票確定を無期限で待つ。"""
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        runner.state.day_generation = 1
        voter = add_player(runner, 1)
        add_player(runner, 2)
        runner._grant_speaker = AsyncMock()
        runner._clear_speaker = AsyncMock()
        runner._play_transition_se = AsyncMock()
        runner._pausable_countdown = AsyncMock(return_value=False)
        runner._record_vote_abstentions = AsyncMock(return_value=True)

        task = asyncio.create_task(runner._day_vote())
        for _ in range(5):
            await asyncio.sleep(0)
        await runner.join_vote_queue(voter.member)
        for _ in range(100):
            if runner.state.vote_speech_finished:
                break
            await asyncio.sleep(0)

        self.assertTrue(runner.state.vote_slot_active)
        self.assertTrue(runner.state.vote_speech_finished)
        self.assertNotIn(voter.user_id, runner.state.votes)
        self.assertFalse(task.done())
        self.assertEqual(runner._current_speaker_ids(), set())
        runner._record_vote_abstentions.assert_not_awaited()

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_vote_speech_view_has_no_candidates_and_ends_durably(self) -> None:
        runner = make_runner()
        voter = add_player(runner, 1)
        runner.state.phase = Phase.DAY_VOTE
        runner.state.vote_order = [voter.user_id]
        runner.state.vote_slot_active = True
        runner.state.vote_slot_token = 7
        runner.state.current_speaker_id = voter.user_id

        view = SequentialVoteSpeechView(runner, voter.user_id)
        custom_ids = {item.custom_id for item in view.children}
        self.assertEqual(custom_ids, {"vote_speech_done", "join_vote"})

        result = await runner.finish_sequential_vote_speech(voter.user_id, 7)

        self.assertIn("発言を終了", result)
        self.assertTrue(runner.state.vote_speech_finished)
        self.assertTrue(runner.state.speech_done_event.is_set())

    async def test_gm_force_abstention_only_after_speech(self) -> None:
        runner = make_runner()
        voter = add_player(runner, 1)
        gm = SimpleNamespace(id=99)
        runner.state.gm_id = gm.id
        runner.state.phase = Phase.DAY_VOTE
        runner.state.vote_order = [voter.user_id]
        runner.state.vote_slot_active = True
        runner.state.vote_speech_finished = True
        runner.state.vote_slot_token = 3
        runner.state.current_speaker_id = voter.user_id

        result = await runner.force_skip_wait(
            gm, expected_vote_slot_token=3
        )

        self.assertIn("棄権", result)
        self.assertTrue(runner.state.vote_slot_forced_abstain)
        self.assertTrue(runner.state.vote_choice_event.is_set())
        self.assertNotIn(voter.user_id, runner.state.votes)

    async def test_empty_vote_queue_is_valid_until_someone_presses(self) -> None:
        """発言順は「投票」を押した人だけの列。空のままでも復元を止めない。"""
        runner = make_runner()
        runner.state.day_generation = 2
        payload = {
            "players": [{"user_id": 1, "number": 1}],
            "vote_day_generation": 2,
            "vote_order": [],
        }

        runner._validate_vote_snapshot(payload, Phase.DAY_VOTE, [])

    async def test_unknown_voter_in_vote_queue_fails_closed(self) -> None:
        runner = make_runner()
        runner.state.day_generation = 2
        payload = {
            "players": [{"user_id": 1, "number": 1}],
            "vote_day_generation": 2,
            "vote_order": [1, 7],
        }
        with self.assertRaisesRegex(StateDurabilityError, "重複なし順列"):
            runner._validate_vote_snapshot(payload, Phase.DAY_VOTE, [1, 7])

    async def test_removed_vote_target_requeues_completed_voter(self) -> None:
        runner = make_runner()
        voter = add_player(runner, 1)
        target = add_player(runner, 2)
        later = add_player(runner, 3)
        runner.state.phase = Phase.DAY_VOTE
        runner.state.vote_order = [voter.user_id, later.user_id]
        runner.state.vote_slot_index = 1
        runner.state.votes = {voter.user_id: target.user_id}
        runner._apply_death_effect = AsyncMock()
        runner.state.check_win = lambda: None

        await runner._eliminate_player_mid_game(target, "テスト")

        self.assertNotIn(voter.user_id, runner.state.votes)
        self.assertEqual(
            runner.state.vote_order,
            [later.user_id, voter.user_id],
        )
        self.assertEqual(runner.state.vote_slot_index, 0)

    async def test_removed_target_keeps_current_voter_in_current_slot(self) -> None:
        runner = make_runner()
        voter = add_player(runner, 1)
        target = add_player(runner, 2)
        later = add_player(runner, 3)
        runner.state.phase = Phase.DAY_VOTE
        runner.state.vote_order = [voter.user_id, later.user_id]
        runner.state.vote_slot_index = 0
        runner.state.vote_slot_active = True
        runner.state.current_speaker_id = voter.user_id
        runner.state.votes = {voter.user_id: target.user_id}
        runner._apply_death_effect = AsyncMock()
        runner.state.check_win = lambda: None

        await runner._eliminate_player_mid_game(target, "テスト")

        self.assertNotIn(voter.user_id, runner.state.votes)
        self.assertEqual(runner.state.vote_order, [voter.user_id, later.user_id])
        self.assertEqual(runner.state.vote_slot_index, 0)
        self.assertFalse(runner.state.speech_done_event.is_set())

    async def test_removed_vote_target_requeue_rolls_back_on_save_failure(self) -> None:
        runner = make_runner()
        voter = add_player(runner, 1)
        target = add_player(runner, 2)
        later = add_player(runner, 3)
        runner.state.phase = Phase.DAY_VOTE
        runner.state.vote_order = [voter.user_id, later.user_id]
        runner.state.vote_slot_index = 1
        runner.state.vote_requeue_ids = {later.user_id}
        runner.state.vote_slot_token = 17
        runner.state.vote_slot_active = True
        runner.state.vote_speech_finished = True
        runner.state.vote_slot_forced_abstain = True
        runner.state.vote_speech_window_open = True
        runner.state.vote_remaining_seconds = 12.5
        runner.state.current_speaker_id = later.user_id
        runner.state.votes = {voter.user_id: target.user_id}
        runner._persist_room_state = AsyncMock(side_effect=RuntimeError("DB down"))
        runner._stop_for_durability_error = AsyncMock()

        await runner._eliminate_player_mid_game(target, "テスト")

        self.assertTrue(target.alive)
        self.assertEqual(runner.state.votes, {voter.user_id: target.user_id})
        self.assertEqual(runner.state.vote_order, [voter.user_id, later.user_id])
        self.assertEqual(runner.state.vote_slot_index, 1)
        self.assertEqual(runner.state.vote_requeue_ids, {later.user_id})
        self.assertEqual(runner.state.vote_slot_token, 17)
        self.assertTrue(runner.state.vote_slot_active)
        self.assertTrue(runner.state.vote_speech_finished)
        self.assertTrue(runner.state.vote_slot_forced_abstain)
        self.assertTrue(runner.state.vote_speech_window_open)
        self.assertEqual(runner.state.vote_remaining_seconds, 12.5)
        self.assertEqual(runner.state.current_speaker_id, later.user_id)

    async def test_current_vote_requeue_flag_rolls_back_on_save_failure(self) -> None:
        runner = make_runner()
        voter = add_player(runner, 1)
        target = add_player(runner, 2)
        later = add_player(runner, 3)
        runner.state.phase = Phase.DAY_VOTE
        runner.state.vote_order = [voter.user_id, later.user_id]
        runner.state.vote_slot_index = 0
        runner.state.vote_slot_active = True
        runner.state.vote_speech_finished = True
        runner.state.current_speaker_id = voter.user_id
        runner.state.vote_requeue_ids.clear()
        runner.state.votes = {voter.user_id: target.user_id}
        runner._persist_room_state = AsyncMock(side_effect=RuntimeError("DB down"))
        runner._stop_for_durability_error = AsyncMock()

        await runner._eliminate_player_mid_game(target, "テスト")

        self.assertEqual(runner.state.vote_requeue_ids, set())
        self.assertEqual(runner.state.vote_slot_index, 0)
        self.assertEqual(runner.state.vote_order, [voter.user_id, later.user_id])
        self.assertEqual(runner.state.current_speaker_id, voter.user_id)

    async def test_requeued_voter_order_remains_valid_for_restart(self) -> None:
        runner = make_runner()
        runner.state.day_generation = 4
        payload = {
            "players": [
                {"user_id": 1, "number": 1},
                {"user_id": 2, "number": 2},
            ],
            "vote_day_generation": 4,
            "vote_order": [2, 1],
            "vote_slot_index": 0,
            "vote_slot_token": 3,
            "vote_slot_active": False,
        }

        runner._validate_vote_snapshot(payload, Phase.DAY_VOTE, [2, 1])

    async def test_vote_phase_reuses_saved_order_and_cursor(self) -> None:
        runner = make_runner()
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        runner.state.votes = {first.user_id: second.user_id}
        runner.state.day_generation = 3
        runner.state.vote_day_generation = 3
        runner.state.vote_order = [first.user_id, second.user_id]
        runner.state.vote_slot_index = 1
        runner._grant_speaker = AsyncMock()
        runner._clear_speaker = AsyncMock()

        async def finish_current(*args, **kwargs):
            runner.state.votes[runner.state.current_speaker_id] = second.user_id
            runner.state.speech_done_event.set()
            return False

        runner._pausable_countdown = AsyncMock(side_effect=finish_current)

        executed = await runner._day_vote()

        self.assertEqual(executed, second.user_id)
        self.assertEqual(
            runner.state.votes,
            {first.user_id: second.user_id, second.user_id: second.user_id},
        )
        self.assertEqual(runner.state.vote_order, [first.user_id, second.user_id])
        runner._grant_speaker.assert_awaited_once_with(second.member)

    async def test_crosstalk_discussion_points_to_vote_participation_button(self) -> None:
        """議論タイマーの実表示も、現在のボタン名を案内する。"""
        runner = make_runner()
        add_player(runner, 1)
        runner.state.phase = Phase.DAY_DISCUSSION
        runner.state.day_number = 1
        runner._unmute_alive = AsyncMock(return_value=[])
        runner._wait_for_mute_sync_or_pause = AsyncMock()
        runner._pausable_sleep = AsyncMock()
        runner._play_se = Mock()
        sent_contents: list[str] = []

        async def record_send(content, **kwargs):
            sent_contents.append(content)
            return SimpleNamespace(edit=AsyncMock())

        runner._safe_village_send = AsyncMock(side_effect=record_send)
        runner.post_village_panel = AsyncMock()
        runner._repost_gm_panel = AsyncMock()
        runner._pausable_countdown = AsyncMock()
        runner._lock_village = AsyncMock()
        runner._mute_phase = AsyncMock()

        await runner._day_discussion()

        timer_text = next(text for text in sent_contents if "議論タイム" in text)
        self.assertIn("「投票参加」を押した順", timer_text)
        self.assertNotIn("「投票」を押した順", timer_text)

    async def test_vote_queue_keeps_press_order(self) -> None:
        """発言順は「投票参加」を押した順。二重押しと死亡者は弾く。"""
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        third = add_player(runner, 3)
        third.alive = False

        self.assertIn("1番目", await runner.join_vote_queue(second.member))
        self.assertIn("2番目", await runner.join_vote_queue(first.member))
        self.assertIn("すでに", await runner.join_vote_queue(second.member))
        self.assertIn("投票権がありません", await runner.join_vote_queue(third.member))

        self.assertEqual(runner.state.vote_order, [second.user_id, first.user_id])
        self.assertEqual(runner._vote_queue_waiting(), [])

        # 枠を使い切った人には「並んでいる」と返さない (棄権は取り消せない)
        runner.state.vote_slot_index = 2
        self.assertIn("終了しています", await runner.join_vote_queue(second.member))

    async def test_day_vote_waits_until_someone_presses(self) -> None:
        """列が空の間は誰も発言させず、押された順にそのまま進む。"""
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        runner.state.day_generation = 1
        one = add_player(runner, 1)
        two = add_player(runner, 2)
        three = add_player(runner, 3)
        runner._grant_speaker = AsyncMock()
        runner._clear_speaker = AsyncMock()
        runner._play_transition_se = AsyncMock()
        runner._play_se = Mock()
        spoken: list[int] = []

        async def cast(*_args, **_kwargs) -> bool:
            speaker_id = runner.state.current_speaker_id
            spoken.append(speaker_id)
            runner.state.votes[speaker_id] = (
                three.user_id if speaker_id != three.user_id else one.user_id
            )
            return True

        runner._pausable_countdown = AsyncMock(side_effect=cast)

        task = asyncio.create_task(runner._day_vote())
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(spoken, [])

        await runner.join_vote_queue(two.member)
        await runner.join_vote_queue(one.member)
        await runner.join_vote_queue(three.member)
        executed = await asyncio.wait_for(task, timeout=1)

        self.assertEqual(spoken, [two.user_id, one.user_id, three.user_id])
        self.assertEqual(executed, three.user_id)

    async def test_gm_skip_closes_vote_while_waiting(self) -> None:
        """誰も押さず詰んだときだけ、GMが締め切って集計へ進める。"""
        runner = make_runner()
        gm = SimpleNamespace(id=99)
        runner.state.gm_id = gm.id
        runner.state.phase = Phase.DAY_VOTE
        add_player(runner, 1)

        result = await runner.force_skip_wait(gm)

        self.assertIn("締め切り", result)
        self.assertTrue(runner.state.vote_closed)
        self.assertTrue(runner.state.vote_queue_event.is_set())

    async def test_gm_status_shows_players_who_have_not_pressed(self) -> None:
        """GMは「あと何人が押していないか」で締切要否を判断する。"""
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        first = add_player(runner, 1)
        add_player(runner, 2)
        add_player(runner, 3)
        runner.state.vote_order = [first.user_id]
        runner.state.vote_slot_index = 1

        embed = build_gm_status_embed(runner)
        fields = {field.name: field.value for field in embed.fields}

        self.assertIn("1 / 3人完了", fields["投票発言"])
        self.assertIn("未押下 2人", fields["投票発言"])

    async def test_vote_queue_button_rejects_without_extra_api_call(self) -> None:
        """死亡者・締切後の押下はdeferより前に弾く (1押下=1API)。"""
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        alive = add_player(runner, 1)
        dead = add_player(runner, 2)
        dead.alive = False
        button = VoteQueueView(runner).children[0]

        def make_interaction(member) -> SimpleNamespace:
            return SimpleNamespace(
                user=member,
                response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()),
            )

        dead_press = make_interaction(dead.member)
        await button.callback(dead_press)
        dead_press.response.send_message.assert_awaited_once()
        dead_press.response.defer.assert_not_awaited()

        runner.state.vote_closed = True
        closed_press = make_interaction(alive.member)
        await button.callback(closed_press)
        closed_press.response.defer.assert_not_awaited()
        self.assertEqual(runner.state.vote_order, [])

    async def test_paused_exclusion_still_returns_vote_slot(self) -> None:
        """一時停止中の除外でも、票を失った人へ再投票枠を積み直す。"""
        runner = make_runner()
        voter = add_player(runner, 1)
        target = add_player(runner, 2)
        later = add_player(runner, 3)
        runner.state.phase = Phase.PAUSED
        runner.state.phase_before_pause = Phase.DAY_VOTE
        runner.state.vote_order = [voter.user_id, later.user_id]
        runner.state.vote_slot_index = 1
        runner.state.votes = {voter.user_id: target.user_id}
        runner._apply_death_effect = AsyncMock()
        runner.state.check_win = lambda: None

        await runner._eliminate_player_mid_game(target, "テスト")

        self.assertNotIn(voter.user_id, runner.state.votes)
        self.assertEqual(runner.state.vote_order, [later.user_id, voter.user_id])
        self.assertEqual(runner.state.vote_slot_index, 0)

    async def test_gm_close_while_waiting_tallies_votes_cast_so_far(self) -> None:
        """締切後は待機を抜け、押した人ぶんの票だけで集計する。"""
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        runner.state.day_generation = 1
        gm = SimpleNamespace(id=99)
        runner.state.gm_id = gm.id
        one = add_player(runner, 1)
        add_player(runner, 2)
        three = add_player(runner, 3)
        runner._grant_speaker = AsyncMock()
        runner._clear_speaker = AsyncMock()
        runner._play_transition_se = AsyncMock()
        runner._play_se = Mock()

        async def cast(*_args, **_kwargs) -> bool:
            runner.state.votes[runner.state.current_speaker_id] = three.user_id
            return True

        runner._pausable_countdown = AsyncMock(side_effect=cast)

        task = asyncio.create_task(runner._day_vote())
        for _ in range(5):
            await asyncio.sleep(0)
        await runner.join_vote_queue(one.member)
        for _ in range(100):
            if runner.state.vote_slot_index == 1 and not runner.state.vote_slot_active:
                break
            await asyncio.sleep(0)

        result = await runner.force_skip_wait(gm)
        executed = await asyncio.wait_for(task, timeout=1)

        self.assertIn("締め切り", result)
        self.assertEqual(runner.state.votes, {one.user_id: three.user_id})
        self.assertEqual(executed, three.user_id)
        self.assertEqual(
            await runner.force_skip_wait(gm), "⏳ 現在スキップできる時間待ちはありません。"
        )

    async def test_resume_after_tally_does_not_recount_execution(self) -> None:
        """集計後・遺言前に落ちても、同じ処刑を二重に積まない。"""
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        runner.state.recovery_phase = Phase.DAY_VOTE
        voter = add_player(runner, 1)
        target = add_player(runner, 2)
        runner.state.votes = {voter.user_id: target.user_id}
        runner.state.vote_order = [voter.user_id]
        runner.state.vote_slot_index = 1
        runner.state.pending_execution_target = target.user_id
        runner.state.day_execution_resolved = False
        # 集計時に積まれた記録が既にある (プレイボーナスの基礎データ)
        runner.state.decisive_executions = [{
            "day": runner.state.day_number,
            "target": target.user_id,
            "voters": [voter.user_id],
        }]
        runner._day_vote = AsyncMock()
        runner._last_will = AsyncMock()
        runner._execute_player = AsyncMock()
        runner._end_game = AsyncMock()
        runner.state.check_win = lambda: Team.VILLAGE

        await runner._resume_recovered_game()

        runner._day_vote.assert_not_awaited()
        self.assertEqual(len(runner.state.decisive_executions), 1)
        runner._last_will.assert_awaited_once()

    async def test_vote_queue_state_survives_snapshot_round_trip(self) -> None:
        """途中まで並んだ列・締切・再投票待ちを、DB検証込みで復元する。"""
        source = make_runner()
        source.state.phase = Phase.DAY_VOTE
        source.state.day_generation = 1
        source.state.vote_day_generation = 1
        first = add_player(source, 1)
        second = add_player(source, 2)
        add_player(source, 3)  # まだ「投票」を押していない生存者
        source.state.vote_order = [first.user_id, second.user_id]
        source.state.vote_slot_index = 1
        source.state.vote_slot_token = 4
        source.state.votes = {first.user_id: second.user_id}
        source.state.vote_requeue_ids = {first.user_id}
        source.state.mute_marker_enabled = True
        source.state.gm_id = 99
        source.state.pending_ui_cleanup_batches = [{
            "game_run_id": "older-run",
            "channel_id": 777,
            "channel_message_ids": {"vote_panel_message_id": 888},
            "wolf_dm_message_ids": {},
            "surrender_dm_message_ids": {},
        }]
        snapshot = source._build_room_snapshot()
        snapshot["phase"] = Phase.DAY_VOTE.name

        # 本番と同じDB側の検証を通す (型・重複・ID形式)
        database._validate_room_snapshot(Phase.DAY_VOTE.name, snapshot)

        restored = make_runner()
        restored.state.guild = FakeGuild(
            [player.member for player in source.state.players.values()]
            + [FakeMember(99)],
            [],
        )
        restored.state.village_channel = object()
        restored.state.spirit_channel = object()
        restored._assign_alive_role = AsyncMock()
        restored._prepare_game_vc_permissions = AsyncMock()
        restored._apply_spirit_blocks = AsyncMock()
        restored._post_lobby_ui = AsyncMock()
        restored._repost_gm_panel = AsyncMock()
        restored._enable_mute_markers = AsyncMock()
        restored._reconcile_mute_marker_ownership = AsyncMock()
        restored._reconcile_mute_intents = AsyncMock()
        restored._retry_pending_ui_cleanup = AsyncMock()
        restored._disable_recovered_turn_panel = AsyncMock()
        restored._reconcile_pending_death_effects = AsyncMock()

        await restored.restore_from_snapshot(snapshot)

        self.assertEqual(restored.state.vote_order, [first.user_id, second.user_id])
        self.assertEqual(restored.state.vote_slot_index, 1)
        self.assertEqual(restored.state.vote_requeue_ids, {first.user_id})
        self.assertFalse(restored.state.vote_closed)
        restored._retry_pending_ui_cleanup.assert_awaited_once()
        # 進行中だった枠は満額でやり直すのでactiveは倒れている
        self.assertFalse(restored.state.vote_slot_active)
        # まだ押していない3番だけが待機対象として残る
        self.assertEqual(
            [player.user_id for player in restored._vote_queue_waiting()], [3]
        )

    async def test_gm_skip_does_not_close_vote_before_cursor_advances(self) -> None:
        """枠終了〜cursor更新の間に押しても、並んでいる人の枠を捨てない。"""
        runner = make_runner()
        gm = SimpleNamespace(id=99)
        runner.state.gm_id = gm.id
        runner.state.phase = Phase.DAY_VOTE
        speaker = add_player(runner, 1)
        waiting = add_player(runner, 2)
        runner.state.vote_order = [speaker.user_id, waiting.user_id]
        # ミュート戻し・cursor更新の途中: activeは下りたが列は未消化
        runner.state.vote_slot_index = 0
        runner.state.vote_slot_active = False

        result = await runner.force_skip_wait(gm)

        self.assertNotIn("締め切り", result)
        self.assertFalse(runner.state.vote_closed)

    async def test_exclusion_wakes_vote_queue_wait(self) -> None:
        """最後の未押下者が抜けたら、GMを待たず自動で投票を終える。"""
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        runner.state.day_generation = 1
        one = add_player(runner, 1)
        two = add_player(runner, 2)
        three = add_player(runner, 3)
        runner._grant_speaker = AsyncMock()
        runner._clear_speaker = AsyncMock()
        runner._play_transition_se = AsyncMock()
        runner._play_se = Mock()
        runner._apply_death_effect = AsyncMock()
        runner.state.check_win = lambda: None

        async def cast(*_args, **_kwargs) -> bool:
            runner.state.votes[runner.state.current_speaker_id] = three.user_id
            return True

        runner._pausable_countdown = AsyncMock(side_effect=cast)

        task = asyncio.create_task(runner._day_vote())
        for _ in range(5):
            await asyncio.sleep(0)
        await runner.join_vote_queue(one.member)
        await runner.join_vote_queue(three.member)
        for _ in range(100):
            if runner.state.vote_slot_index == 2 and not runner.state.vote_slot_active:
                break
            await asyncio.sleep(0)

        # 残るは未押下の two だけ。除外されたら待機を続ける理由がない
        await runner._eliminate_player_mid_game(two, "テスト")
        executed = await asyncio.wait_for(task, timeout=1)

        self.assertFalse(runner.state.vote_closed)
        self.assertEqual(executed, three.user_id)

    async def test_vote_slot_is_returned_when_own_target_is_removed(self) -> None:
        """自分の枠の中で投票先が除外されたら、棄権にせず枠を積み直す。"""
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        runner.state.day_generation = 1
        voter = add_player(runner, 1)
        target = add_player(runner, 2)
        third = add_player(runner, 3)
        runner._grant_speaker = AsyncMock()
        runner._clear_speaker = AsyncMock()
        runner._play_transition_se = AsyncMock()
        runner._play_se = Mock()
        runner._apply_death_effect = AsyncMock()
        runner.state.check_win = lambda: None
        spoken: list[int] = []

        async def cast(*_args, **_kwargs) -> bool:
            speaker_id = runner.state.current_speaker_id
            spoken.append(speaker_id)
            if speaker_id == voter.user_id and len(spoken) == 1:
                # 自分の枠の中で投票 → その直後に投票先が除外される
                runner.state.votes[speaker_id] = target.user_id
                await runner._eliminate_player_mid_game(target, "テスト")
                replacement = third.user_id
            else:
                replacement = third.user_id

            async def confirm_choice() -> None:
                # 発言終了後に表示される候補選択を別イベントとして模擬する。
                await asyncio.sleep(0)
                runner.state.votes[speaker_id] = replacement
                runner.state.vote_choice_event.set()

            asyncio.create_task(confirm_choice())
            return True

        runner._pausable_countdown = AsyncMock(side_effect=cast)

        task = asyncio.create_task(runner._day_vote())
        for _ in range(5):
            await asyncio.sleep(0)
        await runner.join_vote_queue(voter.member)
        await runner.join_vote_queue(third.member)
        executed = await asyncio.wait_for(task, timeout=1)

        # 新仕様では発言枠と投票先確定を分離する。発言中に候補が
        # 除外されても、同じ枠の候補選択で投票先を確定する。
        self.assertEqual(spoken, [voter.user_id, third.user_id])
        self.assertEqual(runner.state.votes[voter.user_id], third.user_id)
        self.assertEqual(executed, third.user_id)

    async def test_gm_skip_does_not_close_vote_during_active_slot(self) -> None:
        """投票確定直後の数百msに押しても、投票全体を締め切らない。"""
        runner = make_runner()
        gm = SimpleNamespace(id=99)
        runner.state.gm_id = gm.id
        runner.state.phase = Phase.DAY_VOTE
        voter = add_player(runner, 1)
        add_player(runner, 2)
        runner.state.vote_order = [voter.user_id]
        runner.state.vote_slot_active = True
        runner.state.current_speaker_id = voter.user_id
        # 票を確定した直後: speech_done_eventだけが立った状態
        runner.state.speech_done_event.set()

        result = await runner.force_skip_wait(gm)

        self.assertNotIn("締め切り", result)
        self.assertFalse(runner.state.vote_closed)

    async def test_non_boolean_vote_closed_fails_closed(self) -> None:
        runner = make_runner()
        runner.state.day_generation = 2
        payload = {
            "players": [{"user_id": 1, "number": 1}],
            "vote_day_generation": 2,
            "vote_order": [],
            "vote_closed": "yes",
        }
        with self.assertRaisesRegex(StateDurabilityError, "vote_closed"):
            runner._validate_vote_snapshot(payload, Phase.DAY_VOTE, [])

    def test_legacy_runoff_close_marker_is_treated_as_open(self) -> None:
        """旧決戦snapshotのTrueは通常投票からの持越しと読み替える。"""
        runner = make_runner()

        runner._restore_vote_close_marker(
            {"vote_closed": True}, Phase.DAY_RUNOFF_VOTE,
        )

        self.assertFalse(runner.state.vote_closed)
        self.assertIsNone(runner.state.vote_closed_phase)

    def test_current_closed_runoff_restores_with_phase_marker(self) -> None:
        runner = make_runner()

        runner._restore_vote_close_marker(
            {
                "vote_closed": True,
                "vote_closed_phase": Phase.DAY_RUNOFF_VOTE.name,
            },
            Phase.DAY_RUNOFF_VOTE,
        )

        self.assertTrue(runner.state.vote_closed)
        self.assertEqual(runner.state.vote_closed_phase, Phase.DAY_RUNOFF_VOTE)

    def test_mismatched_vote_close_phase_fails_closed(self) -> None:
        runner = make_runner()

        with self.assertRaisesRegex(StateDurabilityError, "締切フェーズ"):
            runner._restore_vote_close_marker(
                {
                    "vote_closed": True,
                    "vote_closed_phase": Phase.DAY_VOTE.name,
                },
                Phase.DAY_RUNOFF_VOTE,
            )

    async def test_runoff_start_save_failure_uses_durability_stop(self) -> None:
        runner = make_runner()
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        runner.state.phase = Phase.DAY_RUNOFF_VOTE
        runner.state.runoff_candidates = [first.user_id, second.user_id]
        runner._persist_room_state = AsyncMock(side_effect=RuntimeError("DB down"))
        runner._stop_for_durability_error = AsyncMock()

        with self.assertRaisesRegex(StateDurabilityError, "決戦投票の開始"):
            await runner._runoff(
                [first.user_id, second.user_id], resume_vote=True,
            )

        runner._stop_for_durability_error.assert_awaited_once()
        self.assertEqual(
            runner._stop_for_durability_error.await_args.args[0],
            "決戦投票の開始",
        )

    async def test_vote_end_cue_precedes_next_speaker_without_duplicate_start_se(self) -> None:
        """確定票公開後のSEを次枠の開始合図と兼用し、二重再生を避ける。"""
        runner = make_runner()
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        runner.state.day_generation = 3
        runner.state.vote_day_generation = 3
        runner.state.vote_order = [first.user_id, second.user_id]
        runner.state.vote_slot_index = 0
        events: list[str] = []
        async def record_se(scene: str) -> None:
            events.append(f"se:{scene}")

        runner._play_transition_se = AsyncMock(side_effect=record_se)

        async def record_grant(member) -> None:
            events.append(f"grant:{member.id}")

        runner._grant_speaker = AsyncMock(side_effect=record_grant)
        runner._clear_speaker = AsyncMock(
            side_effect=lambda: events.append("clear")
        )

        async def cast_vote(*_args, **_kwargs) -> bool:
            current = runner.state.current_speaker_id
            runner.state.votes[current] = (
                second.user_id if current == first.user_id else first.user_id
            )
            return False

        runner._pausable_countdown = AsyncMock(side_effect=cast_vote)
        runner._resolve_day_vote = AsyncMock(return_value=None)

        executed = await runner._day_vote()

        self.assertIsNone(executed)
        self.assertEqual(
            events,
            [
                "se:speech", f"grant:{first.user_id}", "clear", "se:speech_end",
                "se:reveal", f"grant:{second.user_id}", "clear", "se:speech_end",
            ],
        )

    async def test_last_will_waits_for_transition_se_before_countdown(self) -> None:
        runner = make_runner()
        player = add_player(runner, 1)
        events: list[str] = []
        runner._require_recovered_speech_panel_closed = AsyncMock()
        runner._grant_speaker = AsyncMock(
            side_effect=lambda _member: events.append("grant")
        )

        async def record_se(scene: str) -> None:
            events.append(f"se:{scene}")

        runner._play_transition_se = AsyncMock(side_effect=record_se)
        runner._safe_village_send = AsyncMock(return_value=None)
        runner._remember_speech_panel = AsyncMock()
        runner._repost_gm_panel = AsyncMock(return_value=True)
        runner._pausable_countdown = AsyncMock(
            side_effect=lambda *_args: events.append("countdown")
        )
        runner._close_speech_panel = AsyncMock()
        runner._clear_speaker = AsyncMock(
            side_effect=lambda: events.append("clear")
        )

        await runner._last_will(player)

        self.assertEqual(
            events, ["grant", "se:lastwill", "countdown", "clear"]
        )

    async def test_runoff_random_target_is_checkpointed_before_announcement(self) -> None:
        """告知中に落ちても、抽選結果はすでに復元可能であること。"""
        runner = make_runner()
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        runner.state.phase = Phase.DAY_RUNOFF_VOTE
        runner.state.votes = {}
        runner._pausable_countdown = AsyncMock(return_value=True)
        events: list[str] = []

        async def persist() -> None:
            if runner.state.pending_execution_target is not None:
                events.append("checkpoint")

        async def fail_random_announcement(*args, **kwargs):
            content = args[0] if args else ""
            if isinstance(content, str) and "ランダムで" in content:
                events.append("announcement")
                raise RuntimeError("告知送信中にクラッシュ")
            return None

        runner._persist_room_state = AsyncMock(side_effect=persist)
        runner._safe_village_send = AsyncMock(side_effect=fail_random_announcement)
        with (
            patch("room_runner.secrets.choice", return_value=second),
            self.assertRaisesRegex(RuntimeError, "告知送信中"),
        ):
            await runner._runoff([first.user_id, second.user_id], resume_vote=True)

        self.assertEqual(runner.state.pending_execution_target, second.user_id)
        self.assertEqual(events, ["checkpoint", "announcement"])

    async def test_runoff_candidates_do_not_have_vote_rights(self) -> None:
        runner = make_runner()
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        voter = add_player(runner, 3)
        runner.state.phase = Phase.DAY_RUNOFF_VOTE
        runner.state.runoff_candidates = [first.user_id, second.user_id]
        runner._pausable_countdown = AsyncMock(return_value=False)

        await runner._runoff(
            [first.user_id, second.user_id], resume_vote=True
        )

        sent_view = next(
            call.kwargs["view"]
            for call in runner._safe_village_send.await_args_list
            if call.kwargs.get("view") is not None
        )
        self.assertEqual(sent_view.voters, {voter.user_id})
        self.assertEqual(
            sent_view._vote_error(first.user_id, second.user_id),
            "投票権がありません。",
        )
        self.assertTrue(runner.state.vote_closed)

    async def test_new_runoff_resets_normal_close_then_checkpoints_its_own_close(self) -> None:
        runner = make_runner()
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        runner.state.phase = Phase.DAY_RUNOFF_SPEECH
        runner.state.runoff_candidates = [first.user_id, second.user_id]
        runner.state.runoff_speech_index = 2
        runner.state.vote_closed = True  # 通常投票の締切印
        runner._pausable_countdown = AsyncMock(return_value=True)
        closed_states: list[bool] = []

        async def persist() -> None:
            if runner.state.phase == Phase.DAY_RUNOFF_VOTE:
                closed_states.append(runner.state.vote_closed)

        runner._persist_room_state = AsyncMock(side_effect=persist)
        with patch("room_runner.secrets.choice", return_value=first):
            await runner._runoff(
                [first.user_id, second.user_id], resume_speech=True
            )

        self.assertGreaterEqual(len(closed_states), 2)
        self.assertEqual(closed_states[:2], [False, True])
        self.assertTrue(runner.state.vote_closed)

    async def test_runoff_without_eligible_voters_does_not_wait(self) -> None:
        runner = make_runner()
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        runner.state.phase = Phase.DAY_RUNOFF_VOTE
        runner.state.runoff_candidates = [first.user_id, second.user_id]
        runner._pausable_countdown = AsyncMock(return_value=True)

        with patch("room_runner.secrets.choice", return_value=first):
            executed = await runner._runoff(
                [first.user_id, second.user_id], resume_vote=True
            )

        self.assertEqual(executed, first.user_id)
        runner._pausable_countdown.assert_awaited_once()
        self.assertTrue(runner.state.vote_complete_event.is_set())

    async def test_recovered_runoff_speech_keeps_completed_cursor(self) -> None:
        runner = make_runner()
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        voter = add_player(runner, 3)
        runner.state.runoff_candidates = [first.user_id, second.user_id]
        runner.state.runoff_speech_index = 1
        runner._grant_speaker = AsyncMock()
        runner._clear_speaker = AsyncMock()
        runner._pausable_countdown = AsyncMock(return_value=True)

        await runner._runoff(runner.state.runoff_candidates, resume_speech=True)

        runner._grant_speaker.assert_awaited_once_with(second.member)
        self.assertEqual(runner.state.runoff_speech_index, 2)

    async def test_runoff_with_no_living_candidates_has_no_execution(self) -> None:
        runner = make_runner()
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        first.alive = False
        second.alive = False
        runner.state.phase = Phase.DAY_RUNOFF_VOTE
        runner.state.votes = {}
        runner._pausable_countdown = AsyncMock(return_value=True)

        with patch(
            "room_runner.secrets.choice",
            side_effect=AssertionError("死亡候補から再抽選してはいけない"),
        ):
            executed = await runner._runoff(
                [first.user_id, second.user_id], resume_vote=True
            )

        self.assertIsNone(executed)
        self.assertIsNone(runner.state.pending_execution_target)
        self.assertTrue(
            any(
                call.args and "全員死亡・除外済み" in call.args[0]
                for call in runner._safe_village_send.await_args_list
            )
        )

    async def test_recovered_runoff_reuses_checkpointed_random_target(self) -> None:
        runner = make_runner()
        first = add_player(runner, 1)
        chosen = add_player(runner, 2)
        runner.state.phase = Phase.PAUSED
        runner.state.phase_before_pause = Phase.DAY_RUNOFF_VOTE
        runner.state.recovery_phase = Phase.DAY_RUNOFF_VOTE
        runner.state.recovered_from_restart = True
        runner.state.pending_execution_target = chosen.user_id
        runner.state.runoff_candidates = [first.user_id, chosen.user_id]
        runner._runoff = AsyncMock()
        runner._last_will = AsyncMock(side_effect=asyncio.CancelledError)

        await runner._resume_recovered_game()

        runner._runoff.assert_not_awaited()
        runner._last_will.assert_awaited_once_with(chosen)
        self.assertEqual(runner.state.pending_execution_target, chosen.user_id)

    async def test_force_end_stops_all_views_and_old_generation_is_rejected(self) -> None:
        runner = make_runner()
        fake_view = SimpleNamespace(stop=Mock())
        runner.register_game_view(fake_view, night=True)
        runner._restore_nicknames = AsyncMock(return_value={})
        runner._teardown_game_roles_and_perms = AsyncMock()
        runner._post_lobby_ui = AsyncMock()
        runner._disable_live_wolf_vote_dms = AsyncMock()
        runner._disable_persisted_dm_views = AsyncMock()
        runner._close_morning_panel = AsyncMock()
        runner._close_prep_panel = AsyncMock()
        runner._disable_recovered_speech_panel = AsyncMock(return_value=True)
        runner._disable_recovered_turn_panel = AsyncMock()
        runner._disable_recovered_vote_panel = AsyncMock()
        runner.close_village_panel = AsyncMock()
        async def transition_to_empty_lobby(source, **kwargs):
            target = GameState()
            target.room_id = source.room_id
            target.room_name = source.room_name
            target.guild = source.guild
            runner.state = target
            return True
        runner._transition_to_empty_lobby = transition_to_empty_lobby
        old_run = runner.state.game_run_id

        await runner.force_end("テスト廃村")

        fake_view.stop.assert_called()
        runner._disable_live_wolf_vote_dms.assert_awaited_once()
        self.assertEqual(runner._disable_persisted_dm_views.await_count, 2)
        runner._close_morning_panel.assert_awaited_once()
        runner._close_prep_panel.assert_awaited_once()
        runner._disable_recovered_speech_panel.assert_awaited_once()
        runner._disable_recovered_turn_panel.assert_awaited_once()
        runner._disable_recovered_vote_panel.assert_awaited_once()
        runner.close_village_panel.assert_awaited_once()
        self.assertFalse(runner.is_current_game_view(old_run))
        self.assertEqual(runner.state.phase, Phase.LOBBY)

    async def test_pause_boundary_does_not_overwrite_paused_phase(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.PAUSED
        runner.state.phase_before_pause = Phase.DAY_DISCUSSION
        runner.state.pause_event.clear()

        day_task = asyncio.create_task(runner._day_discussion())
        night_task = asyncio.create_task(runner._night_phase())
        await asyncio.sleep(0)
        self.assertEqual(runner.state.phase, Phase.PAUSED)
        day_task.cancel()
        night_task.cancel()
        await asyncio.gather(day_task, night_task, return_exceptions=True)

    async def test_night_persist_failure_rolls_back_entire_resolution(self) -> None:
        runner = make_runner()
        victim = add_player(runner, 1)
        runner.state.wolf_target = victim.user_id
        runner.state.guard_target = 99
        runner.state.guard_previous = 55
        calls = 0

        async def fail_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("DB down")

        runner._persist_room_state = fail_once
        with self.assertRaises(StateDurabilityError):
            await runner._process_night()

        self.assertTrue(victim.alive)
        self.assertFalse(runner.state.night_resolved)
        self.assertEqual(runner.state.guard_previous, 55)
        self.assertEqual(runner.state.wolf_target, victim.user_id)
        self.assertEqual(runner.state.guard_target, 99)
        self.assertEqual(runner.state.phase, Phase.PAUSED)
        self.assertTrue(runner.state.recovered_from_restart)

    async def test_manual_mute_is_never_unmuted_and_missing_vc_returns_list(self) -> None:
        runner = make_runner()
        self.assertEqual(await runner._sync_server_mutes({1}), [])

        channel = SimpleNamespace(id=10)
        member = FakeMember(1)
        member.voice = FakeVoiceState(mute=True, channel=channel)
        runner.state.voice_channel = SimpleNamespace(id=10, members=[member])
        changed = await runner._sync_server_mutes({member.id})
        self.assertEqual(changed, [])
        member.edit.assert_not_awaited()

    async def test_mute_patch_marker_recovers_ownership_after_checkpoint_crash(self) -> None:
        runner = make_runner()
        channel = SimpleNamespace(id=10)
        member = FakeMember(1)
        everyone = FakeRole(1, "@everyone", default=True)
        marker = FakeRole(2, runner._mute_marker_role_name())
        member.roles = [everyone]
        member.voice = FakeVoiceState(mute=False, channel=channel)
        guild = FakeGuild([member], [everyone, marker])
        member.guild = guild
        runner.state.guild = guild
        runner.state.voice_channel = SimpleNamespace(id=10, members=[member])
        runner.state.mute_marker_enabled = True

        async def apply_patch(**kwargs) -> None:
            member.voice.mute = kwargs["mute"]
            member.roles = [everyone, *kwargs.get("roles", [])]

        member.edit = AsyncMock(side_effect=apply_patch)
        runner._persist_room_state = AsyncMock(side_effect=RuntimeError("DB down"))
        with self.assertRaises(StateDurabilityError):
            await runner._sync_server_mutes(set())

        self.assertTrue(member.voice.mute)
        self.assertIn(marker, member.roles)

        # 次プロセスが読むDBはAPI実行前のままでも、
        # Discord側マーカーからBot所有へ収束できる。
        recovered = make_runner()
        recovered.state.guild = guild
        recovered.state.mute_marker_enabled = True
        recovered.state.bot_muted_ids.clear()
        await recovered._reconcile_mute_marker_ownership()
        self.assertIn(member.id, recovered.state.bot_muted_ids)

    async def test_deleted_mute_marker_role_is_recreated_for_current_owned_mute(self) -> None:
        runner = make_runner()
        channel = SimpleNamespace(id=10)
        member = FakeMember(1)
        everyone = FakeRole(1, "@everyone", default=True)
        marker = FakeRole(2, runner._mute_marker_role_name())
        member.roles = [everyone]
        member.voice = FakeVoiceState(mute=True, channel=channel)
        guild = FakeGuild([member], [everyone])
        guild.create_role = AsyncMock(side_effect=lambda **_kwargs: marker)
        member.guild = guild
        runner.state.guild = guild
        runner.state.voice_channel = SimpleNamespace(id=10, members=[member])
        runner.state.mute_marker_enabled = True
        runner.state.bot_muted_ids = {member.id}

        async def apply_patch(**kwargs) -> None:
            member.voice.mute = kwargs["mute"]
            member.roles = [everyone, *kwargs["roles"]]

        member.edit = AsyncMock(side_effect=apply_patch)

        restored = await runner._enable_mute_markers()
        await runner._reconcile_mute_marker_ownership()

        self.assertIs(restored, marker)
        guild.create_role.assert_awaited_once()
        member.edit.assert_awaited_once()
        self.assertTrue(member.edit.await_args.kwargs["mute"])
        self.assertIn(marker, member.roles)
        self.assertIn(member.id, runner.state.bot_muted_ids)

    async def test_unmute_marker_removal_wins_over_stale_checkpoint(self) -> None:
        runner = make_runner()
        channel = SimpleNamespace(id=10)
        member = FakeMember(1)
        everyone = FakeRole(1, "@everyone", default=True)
        marker = FakeRole(2, runner._mute_marker_role_name())
        member.roles = [everyone, marker]
        member.voice = FakeVoiceState(mute=True, channel=channel)
        guild = FakeGuild([member], [everyone, marker])
        member.guild = guild
        runner.state.guild = guild
        runner.state.voice_channel = SimpleNamespace(id=10, members=[member])
        runner.state.mute_marker_enabled = True
        runner.state.bot_muted_ids = {member.id}

        async def apply_patch(**kwargs) -> None:
            member.voice.mute = kwargs["mute"]
            member.roles = [everyone, *kwargs.get("roles", [])]

        member.edit = AsyncMock(side_effect=apply_patch)
        runner._persist_room_state = AsyncMock(side_effect=RuntimeError("DB down"))
        with self.assertRaises(StateDurabilityError):
            await runner._sync_server_mutes({member.id})

        self.assertFalse(member.voice.mute)
        self.assertNotIn(marker, member.roles)

        # checkpoint前クラッシュ後にモデレーターが手動で
        # 再muteしても、マーカーが無いためBot所有に戻さない。
        member.voice.mute = True
        recovered = make_runner()
        recovered.state.guild = guild
        recovered.state.mute_marker_enabled = True
        recovered.state.bot_muted_ids = {member.id}  # 古いDB checkpoint
        await recovered._reconcile_mute_marker_ownership()
        self.assertNotIn(member.id, recovered.state.bot_muted_ids)

    async def test_participant_gm_death_keeps_manual_mute_and_removes_alive_role(self) -> None:
        runner = make_runner()
        player = add_player(runner, 1)
        # GMを兼ねる参加者は、死亡後もゲーム説明のためmuteを手動管理する。
        runner.state.gm_id = player.user_id
        channel = SimpleNamespace(id=10)
        everyone = FakeRole(1, "@everyone", default=True)
        alive = FakeRole(2, runner._alive_role_name())
        marker = FakeRole(3, runner._mute_marker_role_name())
        player.member.roles = [everyone, alive]
        player.member.voice = FakeVoiceState(mute=False, channel=channel)
        guild = FakeGuild([player.member], [everyone, alive, marker])
        player.member.guild = guild
        runner.state.guild = guild
        runner.state.voice_channel = SimpleNamespace(
            id=channel.id, members=[player.member]
        )
        runner.state.village_channel = permission_text_channel()
        runner.state.mute_marker_enabled = True
        effect = {
            "event_id": "run-1:襲撃:1",
            "player_id": player.user_id,
            "method": "襲撃",
            "reason": None,
        }
        runner.state.pending_death_effects = [effect]

        async def apply_patch(**kwargs):
            player.member.nick = kwargs["nick"]
            if "mute" in kwargs:
                player.member.voice.mute = kwargs["mute"]
            if "roles" in kwargs:
                player.member.roles = [everyone, *kwargs["roles"]]
            return player.member

        player.member.edit = AsyncMock(side_effect=apply_patch)
        runner._remove_alive_role = AsyncMock()

        await runner._apply_death_effect(effect)

        player.member.edit.assert_awaited_once()
        edit_kwargs = player.member.edit.await_args.kwargs
        self.assertIn("nick", edit_kwargs)
        self.assertNotIn("mute", edit_kwargs)
        self.assertNotIn(marker, edit_kwargs["roles"])
        self.assertNotIn(alive, edit_kwargs["roles"])
        self.assertFalse(player.member.voice.mute)
        self.assertNotIn(player.user_id, runner.state.bot_muted_ids)
        runner._remove_alive_role.assert_not_awaited()
        self.assertEqual(runner.state.pending_death_effects, [])

    async def test_disconnected_participant_gm_keeps_legacy_mute_ownership_until_join(self) -> None:
        runner = make_runner()
        player = add_player(runner, 1)
        runner.state.gm_id = player.user_id
        everyone = FakeRole(1, "@everyone", default=True)
        alive = FakeRole(2, runner._alive_role_name())
        marker = FakeRole(3, runner._mute_marker_role_name())
        player.member.roles = [everyone, alive, marker]
        player.member.voice = None
        guild = FakeGuild([player.member], [everyone, alive, marker])
        player.member.guild = guild
        runner.state.guild = guild
        runner.state.voice_channel = SimpleNamespace(id=10, members=[])
        runner.state.village_channel = permission_text_channel()
        runner.state.mute_marker_enabled = True
        runner.state.bot_muted_ids = {player.user_id}
        effect = {
            "event_id": "run-1:除外:1",
            "player_id": player.user_id,
            "method": "除外",
            "reason": None,
        }
        runner.state.pending_death_effects = [effect]

        async def apply_patch(**kwargs):
            player.member.nick = kwargs["nick"]
            if "roles" in kwargs:
                player.member.roles = [everyone, *kwargs["roles"]]
            return player.member

        player.member.edit = AsyncMock(side_effect=apply_patch)
        runner._remove_alive_role = AsyncMock()

        await runner._apply_death_effect(effect)

        edit_kwargs = player.member.edit.await_args.kwargs
        self.assertNotIn("mute", edit_kwargs)
        self.assertIn(marker, player.member.roles)
        self.assertNotIn(alive, player.member.roles)
        self.assertIn(player.user_id, runner.state.bot_muted_ids)
        self.assertEqual(runner.state.pending_death_effects, [])

    async def test_participant_gm_death_safety_stops_if_alive_role_removal_fails(self) -> None:
        runner = make_runner()
        player = add_player(runner, 1)
        runner.state.gm_id = player.user_id
        channel = SimpleNamespace(id=10)
        everyone = FakeRole(1, "@everyone", default=True)
        alive = FakeRole(2, runner._alive_role_name())
        marker = FakeRole(3, runner._mute_marker_role_name())
        player.member.roles = [everyone, alive]
        player.member.voice = FakeVoiceState(mute=False, channel=channel)
        guild = FakeGuild([player.member], [everyone, alive, marker])
        player.member.guild = guild
        runner.state.guild = guild
        runner.state.voice_channel = SimpleNamespace(
            id=channel.id, members=[player.member]
        )
        runner.state.village_channel = permission_text_channel()
        runner.state.mute_marker_enabled = True
        denied = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden", headers={}), "denied"
        )
        player.member.edit = AsyncMock(side_effect=denied)
        runner._remove_alive_role = AsyncMock(return_value=False)
        effect = {
            "event_id": "run-1:襲撃:1",
            "player_id": player.user_id,
            "method": "襲撃",
            "reason": None,
        }
        runner.state.pending_death_effects = [effect]

        with self.assertRaises(StateDurabilityError):
            await runner._apply_death_effect(effect)

        self.assertEqual(runner.state.pending_death_effects, [effect])
        self.assertTrue(runner.state.paused)

    async def test_death_keeps_outbox_and_safety_stops_if_speech_cannot_be_blocked(self) -> None:
        runner = make_runner()
        player = add_player(runner, 1)
        channel = SimpleNamespace(id=10)
        everyone = FakeRole(1, "@everyone", default=True)
        alive = FakeRole(2, runner._alive_role_name())
        marker = FakeRole(3, runner._mute_marker_role_name())
        player.member.roles = [everyone, alive, marker]
        player.member.voice = FakeVoiceState(mute=False, channel=channel)
        guild = FakeGuild([player.member], [everyone, alive, marker])
        player.member.guild = guild
        runner.state.guild = guild
        runner.state.voice_channel = SimpleNamespace(id=channel.id, members=[player.member])
        runner.state.mute_marker_enabled = True
        denied = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden", headers={}), "denied"
        )
        runner.state.village_channel = permission_text_channel(
            set_permissions=AsyncMock(side_effect=denied)
        )
        player.member.edit = AsyncMock(side_effect=denied)
        runner._remove_alive_role = AsyncMock(return_value=False)
        effect = {
            "event_id": "run-1:襲撃:1",
            "player_id": player.user_id,
            "method": "襲撃",
            "reason": None,
        }
        runner.state.pending_death_effects = [effect]

        with self.assertRaises(StateDurabilityError):
            await runner._apply_death_effect(effect)

        self.assertEqual(runner.state.pending_death_effects, [effect])
        self.assertTrue(runner.state.paused)
        self.assertEqual(runner.state.phase, Phase.PAUSED)
        runner._safe_spirit_send.assert_not_awaited()

    async def test_alive_rejoin_combines_nickname_and_role_restore(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.PREPARATION
        player = add_player(runner, 1)
        old_member = player.member
        everyone = FakeRole(1, "@everyone", default=True)
        alive = FakeRole(2, runner._alive_role_name())
        returning = FakeMember(player.user_id, "returning")
        returning.roles = [everyone]
        guild = FakeGuild([returning], [everyone, alive])
        returning.guild = guild
        runner.state.guild = guild

        async def apply_patch(**kwargs):
            returning.nick = kwargs["nick"]
            returning.roles = [everyone, *kwargs["roles"]]
            return returning

        returning.edit = AsyncMock(side_effect=apply_patch)

        await runner.on_member_join(returning)

        self.assertIsNot(player.member, old_member)
        self.assertIs(player.member, returning)
        returning.edit.assert_awaited_once()
        edit_kwargs = returning.edit.await_args.kwargs
        self.assertEqual(edit_kwargs["nick"], player.display_name[:32])
        self.assertIn(alive, edit_kwargs["roles"])

    async def test_alive_rejoin_blocks_spirit_before_restoring_identity(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.PREPARATION
        player = add_player(runner, 1)
        everyone = FakeRole(1, "@everyone", default=True)
        alive = FakeRole(2, runner._alive_role_name())
        returning = FakeMember(player.user_id, "returning")
        returning.roles = [everyone]
        order: list[str] = []

        async def block_spirit(*_args, **_kwargs):
            order.append("spirit")

        async def restore_member(**_kwargs):
            order.append("member")
            return returning

        guild = FakeGuild([returning], [everyone, alive])
        returning.guild = guild
        returning.edit = AsyncMock(side_effect=restore_member)
        runner.state.guild = guild
        runner.state.spirit_channel = permission_text_channel(
            set_permissions=AsyncMock(side_effect=block_spirit)
        )

        await runner.on_member_join(returning)

        self.assertEqual(order, ["spirit", "member"])

    async def test_alive_rejoin_safety_stops_when_spirit_block_fails(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.PREPARATION
        player = add_player(runner, 1)
        returning = FakeMember(player.user_id, "returning")
        guild = FakeGuild([returning], [])
        returning.guild = guild
        runner.state.guild = guild
        denied = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden", headers={}), "denied"
        )
        runner.state.spirit_channel = permission_text_channel(
            set_permissions=AsyncMock(side_effect=denied)
        )

        await runner.on_member_join(returning)

        returning.edit.assert_not_awaited()
        self.assertTrue(runner.state.paused)
        self.assertIn(player.user_id, runner.state.disconnected_players)

    async def test_second_disconnect_and_return_are_persisted(self) -> None:
        runner = make_runner()
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        runner.state.paused = True
        runner.state.phase = Phase.PAUSED
        runner.state.phase_before_pause = Phase.NIGHT

        await runner._pause_for_disconnect(first, "切断")
        await runner._pause_for_disconnect(second, "切断")
        self.assertEqual(runner.state.disconnected_players, {1, 2})
        self.assertGreaterEqual(runner._persist_room_state.await_count, 2)

        await runner._handle_disconnect_return(first)
        self.assertEqual(runner.state.disconnected_players, {2})
        self.assertGreaterEqual(runner._persist_room_state.await_count, 3)

    async def test_elimination_signals_wait_for_durable_state(self) -> None:
        async def assert_failure_keeps_waiters_blocked(
            runner: RoomRunner, victim: Player
        ) -> None:
            async def fail_before_release() -> None:
                self.assertFalse(runner.state.speech_done_event.is_set())
                self.assertFalse(runner.state.night_complete_event.is_set())
                self.assertFalse(runner.state.morning_ready_event.is_set())
                self.assertFalse(runner.state.prep_ready_event.is_set())
                self.assertFalse(runner.state.vote_complete_event.is_set())
                raise RuntimeError("DB down")

            runner._persist_room_state = AsyncMock(side_effect=fail_before_release)
            await runner._eliminate_player_mid_game(victim, "テスト除外")
            self.assertTrue(victim.alive)
            self.assertFalse(runner.state.speech_done_event.is_set())
            self.assertFalse(runner.state.morning_ready_event.is_set())
            self.assertFalse(runner.state.prep_ready_event.is_set())
            self.assertFalse(runner.state.vote_complete_event.is_set())

        night = make_runner()
        night_victim = add_player(night, 1)
        await assert_failure_keeps_waiters_blocked(night, night_victim)

        speech = make_runner()
        speech_victim = add_player(speech, 1)
        speech.state.phase = Phase.DAY_LAST_WILL
        speech.state.current_speaker_id = speech_victim.user_id
        await assert_failure_keeps_waiters_blocked(speech, speech_victim)

        vote = make_runner()
        vote_victim = add_player(vote, 1)
        survivor = add_player(vote, 2)
        vote.state.phase = Phase.DAY_VOTE
        vote.state.votes = {survivor.user_id: survivor.user_id}
        await assert_failure_keeps_waiters_blocked(vote, vote_victim)

        # 役職確認タイム中の除外でも、保存前に prep_ready_event を立てない
        prep = make_runner()
        prep_victim = add_player(prep, 1)
        prep_ready = add_player(prep, 2)
        prep.state.phase = Phase.PREPARATION
        prep.state.prep_ready_ids = {prep_ready.user_id}
        await assert_failure_keeps_waiters_blocked(prep, prep_victim)
        # ロールバックで宣言済みの記録も元に戻っている
        self.assertEqual(prep.state.prep_ready_ids, {prep_ready.user_id})
        self.assertFalse(prep.state.prep_confirmed)

    async def test_durability_stop_can_spawn_a_new_recovery_task_on_gm_resume(self) -> None:
        runner = make_runner()
        add_player(runner, 1)
        runner.state.phase = Phase.PAUSED
        runner.state.phase_before_pause = Phase.DAY_DISCUSSION
        runner.state.recovery_phase = Phase.DAY_DISCUSSION
        runner.state.recovered_from_restart = True
        runner.state.paused = True
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        runner.state.game_task = finished
        runner._resume_recovered_game = AsyncMock()

        result = await runner.resume_game()
        await runner.state.game_task

        self.assertIn("復元ゲーム", result)
        runner._resume_recovered_game.assert_awaited_once()

    async def test_double_resume_spawns_only_one_recovered_game_task(self) -> None:
        runner = make_runner()
        add_player(runner, 1)
        runner.state.phase = Phase.PAUSED
        runner.state.phase_before_pause = Phase.DAY_DISCUSSION
        runner.state.recovery_phase = Phase.DAY_DISCUSSION
        runner.state.recovered_from_restart = True
        runner.state.paused = True
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        runner.state.game_task = finished
        runner._sync_server_mutes = AsyncMock(return_value=[])
        runner._await_mute_applied = AsyncMock(return_value=True)
        runner._resume_recovered_game = AsyncMock()

        first, second = await asyncio.gather(runner.resume_game(), runner.resume_game())
        await runner.state.game_task

        self.assertEqual(sum("復元ゲームを再開しました" in text for text in (first, second)), 1)
        runner._resume_recovered_game.assert_awaited_once()
        runner._sync_server_mutes.assert_awaited_once()

    async def test_force_prep_complete_releases_only_after_durable_checkpoint(self) -> None:
        runner = make_runner()
        gm = add_player(runner, 1)
        runner.state.gm_id = gm.user_id
        runner.state.phase = Phase.PREPARATION
        runner._persist_room_state = AsyncMock(side_effect=RuntimeError("DB down"))

        _, error = await runner.force_prep_complete(gm.member)

        self.assertIsNotNone(error)
        self.assertFalse(runner.state.prep_confirmed)
        self.assertFalse(runner.state.prep_ready_event.is_set())

        runner._persist_room_state = AsyncMock()
        _, error = await runner.force_prep_complete(gm.member)
        self.assertIsNone(error)
        self.assertTrue(runner.state.prep_confirmed)
        self.assertTrue(runner.state.prep_ready_event.is_set())

    async def test_preparation_dm_resume_keeps_same_initial_white(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.PREPARATION
        seer = add_player(runner, 1, Role.SEER)
        white = add_player(runner, 2, Role.VILLAGER)
        runner.state.initial_seer_target = white.user_id
        runner.state.guild = SimpleNamespace(id=1)

        with patch(
            "room_runner.database.record_night_action_once", new=AsyncMock(),
        ) as record:
            failed = await runner._send_role_dms()
            messages = [call.args[0] for call in seer.member.send.await_args_list]
            failed_again = await runner._send_role_dms()

        self.assertEqual(failed, [])
        self.assertEqual(failed_again, [])
        self.assertTrue(runner.state.initial_seer_result_sent)
        self.assertEqual(runner.state.initial_seer_target, white.user_id)
        # v0.51: 初日白はDMせず記録だけ残す。役職DMは1通のまま。
        self.assertEqual(sum("初日占い結果" in message for message in messages), 0)
        self.assertEqual(seer.member.send.await_count, 1)
        self.assertEqual(record.await_count, 1)
        self.assertEqual(record.await_args.kwargs["action"], "初日白")
        self.assertEqual(record.await_args.kwargs["target_id"], white.user_id)

    async def test_initial_white_db_failure_remains_retryable(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.PREPARATION
        seer = add_player(runner, 1, Role.SEER)
        white = add_player(runner, 2, Role.VILLAGER)
        runner.state.initial_seer_target = white.user_id
        runner.state.guild = SimpleNamespace(id=1)

        with patch(
            "room_runner.database.record_night_action_once",
            new=AsyncMock(side_effect=[RuntimeError("DB down"), None]),
        ) as record:
            first_failed = await runner._send_role_dms()
            self.assertFalse(runner.state.initial_seer_result_sent)
            second_failed = await runner._send_role_dms()

        self.assertEqual(first_failed, [])
        self.assertEqual(second_failed, [])
        self.assertTrue(runner.state.initial_seer_result_sent)
        self.assertEqual(record.await_count, 2)
        self.assertEqual(seer.member.send.await_count, 1)
        self.assertEqual(runner.state.initial_seer_target, white.user_id)

    async def test_game_loop_retries_initial_white_without_restart(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.PREPARATION
        seer = add_player(runner, 1, Role.SEER)
        white = add_player(runner, 2, Role.VILLAGER)
        runner.state.initial_seer_target = white.user_id
        runner.state.guild = SimpleNamespace(id=1)

        with patch(
            "room_runner.database.record_night_action_once",
            new=AsyncMock(side_effect=[RuntimeError("DB down"), None]),
        ) as record:
            await runner._send_role_dms()
            self.assertFalse(runner.state.initial_seer_result_sent)

            runner.state.prep_ready_ids = {seer.user_id, white.user_id}
            runner._post_prep_panel = AsyncMock()
            runner.post_village_panel = AsyncMock(return_value=True)
            runner._repost_gm_panel = AsyncMock(return_value=True)
            runner._close_prep_panel = AsyncMock()
            runner._play_se = Mock()
            runner._pausable_countdown = AsyncMock(return_value=True)
            runner._day_discussion = AsyncMock(side_effect=asyncio.CancelledError())

            await runner._game_loop()

        self.assertTrue(runner.state.initial_seer_result_sent)
        self.assertEqual(record.await_count, 2)

    async def test_game_over_checkpoint_failure_does_not_turn_result_into_abandonment(self) -> None:
        runner = make_runner()
        runner.state.guild = SimpleNamespace(id=123)
        add_player(runner, 1, Role.VILLAGER)
        runner._restore_nicknames = AsyncMock(return_value={})
        runner._teardown_game_roles_and_perms = AsyncMock()
        runner._post_lobby_ui = AsyncMock()
        async def transition_to_empty_lobby(source, **kwargs):
            target = GameState()
            target.room_id = source.room_id
            target.room_name = source.room_name
            target.guild = source.guild
            runner.state = target
            return True
        runner._transition_to_empty_lobby = transition_to_empty_lobby
        calls = 0

        async def persist_with_game_over_outage():
            nonlocal calls
            calls += 1
            # 1回目は終了意図、2〜4回目はGAME_OVER checkpoint。
            if 2 <= calls <= 4:
                raise RuntimeError("temporary DB outage")

        runner._persist_room_state = persist_with_game_over_outage
        with (
            patch("room_runner.database.stage_game_settlement", new=AsyncMock()),
            patch(
                "room_runner.database.settle_game_settlement",
                new=AsyncMock(return_value=(False, None, None)),
            ) as settle,
            # 再試行ループの実待機(0,1,2秒)をスキップして高速化する。
            # 再試行回数や最終phaseの検証内容は変えない。
            patch("room_runner.asyncio.sleep", new=AsyncMock()),
        ):
            await runner._end_game(Team.VILLAGE)

        settle.assert_awaited_once()
        self.assertEqual(runner.state.phase, Phase.LOBBY)
        # 終了後LOBBYへの原子遷移は別lifecycleテストで検証する。
        self.assertGreaterEqual(calls, 4)

    async def test_end_game_posts_action_log_before_result_and_rating_last(self) -> None:
        runner = make_runner()
        runner.state.guild = SimpleNamespace(id=123)
        add_player(runner, 1, Role.VILLAGER)
        runner._restore_nicknames = AsyncMock(return_value={})
        runner._teardown_game_roles_and_perms = AsyncMock()
        runner._post_lobby_ui = AsyncMock()

        order: list[str] = []

        async def record_action_log() -> None:
            order.append("action_log")

        async def record_send(*args, **kwargs):
            if kwargs.get("embed") is not None:
                order.append("result_embed")
            elif args and "ランク対象外" in str(args[0]):
                order.append("rating")
            return None

        runner._post_action_log = record_action_log
        runner._safe_village_send = AsyncMock(side_effect=record_send)

        with (
            patch("room_runner.database.stage_game_settlement", new=AsyncMock()),
            patch(
                "room_runner.database.settle_game_settlement",
                new=AsyncMock(return_value=(1, None, None)),
            ),
        ):
            await runner._end_game(Team.VILLAGE)

        # 進行ログ → 勝利陣営+役職公開 → ランク変動 の順で掲示する
        self.assertEqual(order, ["action_log", "result_embed", "rating"])

    def _count_timer_edits(self, seconds: int) -> int:
        """seconds 秒のカウントダウンで発生するメッセージ編集の回数。"""
        edits, last = 0, seconds
        for display in range(seconds, -1, -1):
            if timer_should_update(display, last):
                edits += 1
                last = display
        return edits

    async def test_timer_ticks_coarsely_until_the_last_30_seconds(self) -> None:
        """秒読みは残り30秒から。メッセージ編集はチャンネルバケットを食う。

        1ゲーム (13人・5日) の議論・投票・夜・遺言を合わせると
        1000回規模になるため、終盤以外は粗く刻む。
        """
        # 60秒フェーズ: 55,50,45,40,35 の5回 + 30..1 の30回 + 0 の1回
        # (60は初期表示と同じなので書き換えない)
        self.assertEqual(self._count_timer_edits(60), 36)
        # 残り30秒からは毎秒 (秒読みが要る場面は落とさない)
        for display in (30, 29, 5, 1, 0):
            self.assertTrue(timer_should_update(display, display + 1), display)
        # 60〜31秒は5秒刻みだけ
        self.assertTrue(timer_should_update(45, 46))
        self.assertFalse(timer_should_update(44, 45))
        # 60秒超は30秒刻み
        self.assertTrue(timer_should_update(120, 121))
        self.assertFalse(timer_should_update(119, 120))
        # 同じ表示なら書き換えない
        self.assertFalse(timer_should_update(10, 10))

    async def test_timer_edits_drop_by_about_a_third_over_a_game(self) -> None:
        """5日ゲーム全体で3割以上減ること (回帰で刻みを戻さないための下限)。"""
        def old_count(seconds: int) -> int:
            edits, last = 0, seconds
            for display in range(seconds, -1, -1):
                if display != last and (display == 0 or display <= 60 or display % 30 == 0):
                    edits += 1
                    last = display
            return edits

        phases = [(480, 1), (420, 1), (360, 1), (300, 1), (240, 1),
                  (60, 5), (80, 1), (60, 4), (30, 5), (30, 2)]
        before = sum(old_count(sec) * times for sec, times in phases)
        after = sum(self._count_timer_edits(sec) * times for sec, times in phases)

        self.assertLess(after, before)
        self.assertGreaterEqual((before - after) / before, 0.30, f"{before} -> {after}")

    async def test_death_nick_keeps_number_first_and_marker_survives_truncation(self) -> None:
        # 番号を先頭に残す接尾辞にすることで、VC参加者一覧の並びが動かない
        self.assertEqual(death_nick("01.名前", "処刑"), "01.名前(処刑)")
        self.assertEqual(death_nick("02.名前", "襲撃"), "02.名前(襲撃)")
        self.assertEqual(death_nick("03.名前", "除外"), "03.名前(除外)")
        self.assertEqual(death_nick("04.名前", "不明な死因"), "04.名前(死亡)")

        # 32字ぎりぎりの表示名でもマーカーが消えない (消えると生存中と
        # 同じ文字列になり、死亡が見えなくなる)
        long_name = f"05.{'あ' * 40}"
        nick = death_nick(long_name, "処刑")
        self.assertLessEqual(len(nick), 32)
        self.assertTrue(nick.startswith("05."))
        self.assertTrue(nick.endswith("(処刑)"))

    async def test_apply_death_effect_puts_marker_after_the_number(self) -> None:
        runner = make_runner()
        player = add_player(runner, 1)
        player.base_name = "あ" * 40
        everyone = FakeRole(1, "@everyone", default=True)
        marker = FakeRole(3, runner._mute_marker_role_name())
        player.member.roles = [everyone]
        guild = SimpleNamespace(
            id=1, roles=[everyone, marker], get_member=lambda _: player.member
        )
        runner.state.guild = guild
        runner.state.voice_channel = SimpleNamespace(id=10, members=[player.member])
        runner.state.village_channel = permission_text_channel()
        runner.state.mute_marker_enabled = True
        runner._enable_mute_markers = AsyncMock(return_value=marker)
        runner._remove_alive_role = AsyncMock(return_value=True)
        runner._play_se = Mock()
        effect = {
            "event_id": "run-1:処刑:1",
            "player_id": player.user_id,
            "method": "処刑",
            "reason": None,
        }
        runner.state.pending_death_effects = [effect]
        player.member.edit = AsyncMock(return_value=player.member)

        await runner._apply_death_effect(effect)

        nick = player.member.edit.await_args.kwargs["nick"]
        self.assertTrue(nick.startswith("01."))
        self.assertTrue(nick.endswith("(処刑)"))
        self.assertLessEqual(len(nick), 32)

    async def test_execution_records_medium_result_without_dm(self) -> None:
        """処刑確定時、霊能結果はstateへ記録され、霊媒師への裸DMは廃止済み (v0.51)。"""
        runner = make_runner()
        wolf = add_player(runner, 1, Role.WEREWOLF)
        medium = add_player(runner, 2, Role.MEDIUM)
        wolf.base_name = "処刑対象"
        everyone = FakeRole(1, "@everyone", default=True)
        marker = FakeRole(3, runner._mute_marker_role_name())
        wolf.member.roles = [everyone]
        guild = SimpleNamespace(
            id=1, roles=[everyone, marker], get_member=lambda _: wolf.member
        )
        runner.state.guild = guild
        runner.state.day_number = 2
        runner.state.voice_channel = SimpleNamespace(id=10, members=[wolf.member])
        runner.state.village_channel = permission_text_channel()
        runner.state.mute_marker_enabled = True
        runner._enable_mute_markers = AsyncMock(return_value=marker)
        runner._remove_alive_role = AsyncMock(return_value=True)
        runner._play_se = Mock()
        wolf.member.edit = AsyncMock(return_value=wolf.member)
        medium.member.send = AsyncMock()
        effect = {
            "event_id": "run-1:処刑:2",
            "player_id": wolf.user_id,
            "method": "処刑",
            "reason": None,
        }
        runner.state.pending_death_effects = [effect]

        with patch.object(
            database, "record_night_action", new=AsyncMock()
        ) as record_night_action:
            await runner._apply_death_effect(effect)

        medium.member.send.assert_not_awaited()
        self.assertEqual(len(runner.state.medium_results), 1)
        entry = runner.state.medium_results[0]
        self.assertEqual(entry["day"], 2)
        self.assertEqual(entry["target_id"], wolf.user_id)
        self.assertEqual(entry["result"], "人狼")
        record_night_action.assert_awaited_once()
        kwargs = record_night_action.await_args.kwargs
        self.assertEqual(kwargs["actor_id"], medium.user_id)
        self.assertEqual(kwargs["action"], "霊能")
        self.assertEqual(kwargs["result"], "人狼")

        # クラッシュ再適用 (at-least-once) で同じ死亡が重複追記されない
        record_night_action.reset_mock()
        runner.state.pending_death_effects = [effect]
        with patch.object(
            database, "record_night_action", new=AsyncMock()
        ) as record_night_action_retry:
            await runner._apply_death_effect(effect)
        self.assertEqual(len(runner.state.medium_results), 1)
        record_night_action_retry.assert_not_awaited()

    async def test_vote_buttons_are_ordered_by_number(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        # 参加順とは別の番号を振る (実際も番号はランダム割り当て)
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        third = add_player(runner, 3)
        first.number, second.number, third.number = 7, 2, 11
        alive = [first, second, third]

        view = VoteView(runner, candidates=by_number(alive), voters=alive)

        labels = [
            item.label for item in view.children
            if getattr(item, "custom_id", "") .startswith("vote_")
        ]
        self.assertEqual(labels, [p.display_name for p in (second, first, third)])

    async def test_force_end_initial_checkpoint_failure_still_cleans_up_to_lobby(self) -> None:
        runner = make_runner()
        add_player(runner, 1)
        runner._restore_nicknames = AsyncMock(return_value={})
        runner._teardown_game_roles_and_perms = AsyncMock()
        runner._post_lobby_ui = AsyncMock()
        calls = 0

        async def fail_first_three():
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise RuntimeError("DB down")

        runner._persist_room_state = fail_first_three
        async def transition_to_empty_lobby(source, **kwargs):
            target = GameState()
            target.room_id = source.room_id
            target.room_name = source.room_name
            target.guild = source.guild
            runner.state = target
            return True
        runner._transition_to_empty_lobby = transition_to_empty_lobby
        # 再試行ループの実待機(0,1,2秒)をスキップして高速化する。
        # 再試行回数や最終phaseの検証内容は変えない。
        with patch("room_runner.asyncio.sleep", new=AsyncMock()):
            await runner.force_end("テスト廃村")

        self.assertEqual(runner.state.phase, Phase.LOBBY)
        runner._restore_nicknames.assert_awaited_once()
        runner._teardown_game_roles_and_perms.assert_awaited_once()
        self.assertGreaterEqual(calls, 3)

    async def test_nonactive_snapshot_without_marker_never_claims_mute_ownership(self) -> None:
        confirmed = FakeMember(1)
        confirmed.nick = "01.confirmed"
        confirmed.voice = FakeVoiceState(mute=True, channel=SimpleNamespace(id=1))
        manual = FakeMember(2)
        manual.nick = "別の名前"
        manual.voice = FakeVoiceState(mute=True, channel=SimpleNamespace(id=1))
        snapshot = {
            "bot_muted_ids": [9],
            "bot_mute_intent_ids": [1, 2],
            "players": [
                {"user_id": 1, "number": 1, "base_name": "confirmed"},
                {"user_id": 2, "number": 2, "base_name": "manual"},
            ],
        }

        owned = RoomRunner._resolve_nonactive_owned_mutes(
            snapshot, [confirmed, manual]
        )

        self.assertEqual(owned, set())

    async def test_nonactive_mutes_cover_guild_and_protect_other_active_vc(self) -> None:
        runner = make_runner()
        ordinary = FakeMember(1)
        ordinary.voice = FakeVoiceState(
            mute=True, channel=SimpleNamespace(id=20)
        )
        in_other_game = FakeMember(2)
        in_other_game.voice = FakeVoiceState(
            mute=True, channel=SimpleNamespace(id=30)
        )
        disconnected = FakeMember(3)
        disconnected.voice = None
        guild = FakeGuild([ordinary, in_other_game, disconnected], [])
        runner.manager.is_other_active_game_vc = Mock(
            side_effect=lambda channel_id, exclude_room_id=None: channel_id == 30
        )

        immediate, pending = runner._partition_nonactive_owned_mutes(
            guild, {1, 2, 3, 4}
        )

        self.assertEqual([member.id for member in immediate], [ordinary.id])
        self.assertEqual(pending, {in_other_game.id, disconnected.id, 4})

    async def test_nonactive_marker_is_authority_over_stale_db_ownership(self) -> None:
        runner = make_runner()
        marker_name = runner._mute_marker_role_name()
        marker = FakeRole(10, marker_name)
        confirmed = FakeMember(1)
        confirmed.roles = [marker]
        confirmed.voice = FakeVoiceState(mute=True, channel=SimpleNamespace(id=1))
        stale = FakeMember(2)
        stale.roles = []
        stale.voice = FakeVoiceState(mute=True, channel=SimpleNamespace(id=1))

        owned = RoomRunner._resolve_nonactive_owned_mutes(
            {
                "mute_marker_enabled": True,
                "bot_muted_ids": [2],
            },
            [confirmed, stale],
            marker_role_name=marker_name,
        )

        self.assertEqual(owned, {confirmed.id})

    async def test_pending_unmute_respects_startup_active_map_then_removes_marker(self) -> None:
        manager = object.__new__(GameCog)
        manager.discord_api_sem = asyncio.Semaphore(1)
        manager.rooms = {}
        manager.pending_unmutes = {123: {1}}
        manager._startup_active_vc_rooms = {30: "active-room"}

        async def paced_call(func, *args, **kwargs):
            return await func(*args, **kwargs)

        manager.paced_discord_api_call = AsyncMock(side_effect=paced_call)

        member = FakeMember(1)
        everyone = FakeRole(1, "@everyone", default=True)
        marker = FakeRole(2, "人狼Botミュート:old-room")
        ordinary = FakeRole(3, "ordinary")
        member.roles = [everyone, marker, ordinary]
        member.voice = FakeVoiceState(
            mute=True, channel=SimpleNamespace(id=30)
        )
        member.guild = FakeGuild([member], [everyone, marker, ordinary])

        await manager._resolve_pending_unmute(member)
        member.edit.assert_not_awaited()
        self.assertIn(member.id, manager.pending_unmutes[123])

        # setup/restore期間の保護マップがclearされた後は、
        # 動的room状態を正としてpendingを回収する。
        manager._startup_active_vc_rooms.clear()
        with patch(
            "game.database.remove_pending_unmute", new=AsyncMock()
        ) as remove_pending:
            await manager._resolve_pending_unmute(member)

        kwargs = member.edit.await_args.kwargs
        self.assertFalse(kwargs["mute"])
        self.assertEqual(kwargs["roles"], [ordinary])
        manager.paced_discord_api_call.assert_awaited_once()
        self.assertNotIn(member.id, manager.pending_unmutes[123])
        remove_pending.assert_awaited_once_with(123, member.id)

    async def test_pending_unmute_retries_one_http_failure_then_succeeds(self) -> None:
        manager = object.__new__(GameCog)
        manager.rooms = {}
        manager.pending_unmutes = {123: {1}}
        manager._startup_active_vc_rooms = {}

        async def paced_call(func, *args, **kwargs):
            return await func(*args, **kwargs)

        manager.paced_discord_api_call = AsyncMock(side_effect=paced_call)
        member = FakeMember(1)
        member.voice = FakeVoiceState(channel=SimpleNamespace(id=20))
        member.guild = FakeGuild([member], [])
        member.edit.side_effect = [
            discord.HTTPException(
                SimpleNamespace(status=503, reason="Service Unavailable"),
                {"message": "Service Unavailable", "code": 0},
            ),
            None,
        ]

        with (
            patch("game.asyncio.sleep", new=AsyncMock()) as sleep,
            patch(
                "game.database.remove_pending_unmute", new=AsyncMock()
            ) as remove_pending,
            self.assertLogs("game", level="WARNING"),
        ):
            await manager._resolve_pending_unmute(member)

        sleep.assert_awaited_once_with(MUTE_RETRY_DELAY)
        self.assertEqual(manager.paced_discord_api_call.await_count, 2)
        self.assertEqual(member.edit.await_count, 2)
        self.assertNotIn(member.id, manager.pending_unmutes[123])
        remove_pending.assert_awaited_once_with(123, member.id)

    async def test_active_mute_intent_protects_api_failure_then_manual_mute(self) -> None:
        runner = make_runner()
        member = FakeMember(1)
        member.nick = "手動名"
        member.voice = FakeVoiceState(mute=True, channel=SimpleNamespace(id=1))
        guild = SimpleNamespace(get_member=lambda user_id: member if user_id == 1 else None)
        runner.state.guild = guild
        runner.state.bot_mute_intent_ids = {1}

        await runner._reconcile_mute_intents()

        self.assertNotIn(1, runner.state.bot_muted_ids)
        self.assertNotIn(1, runner.state.bot_mute_intent_ids)
        runner._persist_room_state.assert_awaited()


class PauseResumePhaseConsistencyTest(unittest.IsolatedAsyncioTestCase):
    """一時停止と再開でフェーズがズレないことの回帰テスト。

    一時停止はフェーズ境界の非pausable区間 (遺言終了〜夜入り、ミュート整列中
    など) にも差し込まれる。その間ゲームループは止まらないため、次のフェーズ
    関数が冒頭で state.phase を上書きする。再開時に phase_before_pause を
    無条件へ復元すると、実際の進行位置と state.phase が恒久的にズレて、
    夜なのに night_actions_open() が False になり、占い・護衛・朝の宣言・
    GMの強制夜明けまで全て拒否され、廃村以外に復帰できなくなっていた。
    """

    @staticmethod
    def _runner_in_progress(phase: Phase) -> RoomRunner:
        runner = make_runner()
        runner.state.phase = phase
        runner.state.gm_id = 999
        runner._sync_server_mutes = AsyncMock(return_value=[])
        runner._await_mute_applied = AsyncMock(return_value=True)
        # _is_game_in_progress を満たすため、終わらないタスクを持たせる
        runner.state.game_task = asyncio.create_task(asyncio.Event().wait())
        return runner

    @staticmethod
    def _cleanup(runner: RoomRunner) -> None:
        if runner.state.game_task is not None:
            runner.state.game_task.cancel()

    async def test_resume_keeps_phase_when_loop_advanced_during_pause(self) -> None:
        runner = self._runner_in_progress(Phase.DAY_LAST_WILL)
        add_player(runner, 1, Role.SEER)
        try:
            await runner.pause_game()
            self.assertEqual(runner.state.phase, Phase.PAUSED)

            # ループは止まらず夜へ進み、冒頭で state.phase を上書きする
            runner.state.phase = Phase.NIGHT
            runner.state.morning_ready_open = True

            await runner.resume_game()

            # 巻き戻さず、ループの現在位置を尊重する
            self.assertEqual(runner.state.phase, Phase.NIGHT)
            self.assertTrue(runner.night_actions_open())

            # 夜の脱出手段が塞がれていないこと
            _, error = await runner.force_morning(SimpleNamespace(id=999))
            self.assertIsNone(error)
        finally:
            self._cleanup(runner)

    async def test_resume_restores_phase_when_loop_did_not_advance(self) -> None:
        runner = self._runner_in_progress(Phase.DAY_VOTE)
        try:
            await runner.pause_game()
            self.assertEqual(runner.state.phase, Phase.PAUSED)

            # ループが進んでいない通常ケースでは元のフェーズへ戻す
            await runner.resume_game()

            self.assertEqual(runner.state.phase, Phase.DAY_VOTE)
            self.assertFalse(runner.state.paused)
        finally:
            self._cleanup(runner)

    async def test_resume_after_advance_does_not_break_vote_phase(self) -> None:
        runner = self._runner_in_progress(Phase.DAY_DISCUSSION)
        try:
            # 議論終了後のミュート整列中 (非pausable) に一時停止が入る
            await runner.pause_game()
            # ループは投票フェーズへ進む
            runner.state.phase = Phase.DAY_VOTE

            await runner.resume_game()

            self.assertEqual(runner.state.phase, Phase.DAY_VOTE)
        finally:
            self._cleanup(runner)

    async def test_resume_keeps_game_paused_if_mute_state_cannot_be_confirmed(self) -> None:
        runner = self._runner_in_progress(Phase.DAY_DISCUSSION)
        try:
            await runner.pause_game()
            runner._await_mute_applied = AsyncMock(return_value=False)

            result = await runner.resume_game()

            self.assertIn("停止を継続", result)
            self.assertTrue(runner.state.paused)
            self.assertEqual(runner.state.phase, Phase.PAUSED)
            self.assertFalse(runner.state.pause_event.is_set())
        finally:
            self._cleanup(runner)


class SimulationSandboxGuardTest(unittest.IsolatedAsyncioTestCase):
    """シミュレーションが本番DBへ書き込まないことの回帰テスト。

    simulate_one_game は本物のゲームループを回して games / player_ratings /
    rating_history へ書き込む。DB_PATH をテンポラリへ差し替え忘れると
    偽の試合とレートが本番DBへ入り、統計とランクが壊れる。
    """

    async def test_simulation_refuses_production_db(self) -> None:
        import database
        import simulate_games

        original = database.DB_PATH
        database.DB_PATH = simulate_games.PRODUCTION_DB_PATH
        try:
            with self.assertRaises(RuntimeError) as caught:
                await simulate_games.simulate_selected_game(
                    seed=0,
                    guild_id=1,
                    player_ids=list(range(13)),
                    population_ids=list(range(13)),
                    force_runoff=False,
                )
            self.assertIn("本番DB", str(caught.exception))
        finally:
            database.DB_PATH = original

    async def test_sandbox_db_switches_and_restores_path(self) -> None:
        import database
        import simulate_games

        original = database.DB_PATH
        async with simulate_games.sandbox_db() as sandbox_path:
            self.assertNotEqual(sandbox_path, original)
            self.assertEqual(database.DB_PATH, sandbox_path)
        self.assertEqual(database.DB_PATH, original)


if __name__ == "__main__":
    unittest.main()
