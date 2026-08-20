"""#昼 常設村パネルの [霊媒][人狼予想][占い][狩人] ボタンの回帰テスト
(v0.51 工程1・工程2/3)。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import Phase, Role, RoomDefinition
from models import Player
from room_runner import RoomRunner
from views import (
    VillageGuardTargetView,
    VillagePanelView,
    VillageSeerTargetView,
    VillageWolfGuessView,
)


class FakeManager:
    def __init__(self) -> None:
        self.discord_api_sem = asyncio.Semaphore(20)
        self.start_lock = asyncio.Lock()
        self.rating_lock = asyncio.Lock()
        self.join_lock = asyncio.Lock()

    async def paced_discord_api_call(self, func, *args, **kwargs):
        async with self.discord_api_sem:
            return await func(*args, **kwargs)

    def spawn_bg_task(self, coro):
        coro.close()
        return None


class FakeMember:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.display_name = f"user-{user_id}"
        self.nick = None
        self.roles = []
        self.voice = None


def make_runner(*, phase: Phase = Phase.DAY_DISCUSSION) -> RoomRunner:
    runner = RoomRunner(
        None,
        FakeManager(),
        RoomDefinition("role-act", "役職行動確認村", variant_id="v9_cross", rated=False),
    )
    runner.state.game_run_id = "run-role"
    runner.state.guild = SimpleNamespace(id=555)
    runner.state.room_id = "role-act"
    runner.state.phase = phase
    runner.state.day_number = 1
    runner.state.pause_event.set()
    runner._persist_room_state = AsyncMock()
    return runner


def add_player(
    runner: RoomRunner, user_id: int, role: Role = Role.VILLAGER, *, alive: bool = True,
) -> Player:
    member = FakeMember(user_id)
    player = Player(
        user_id=user_id,
        member=member,
        role=role,
        alive=alive,
        number=user_id,
        base_name=member.display_name,
    )
    runner.state.players[user_id] = player
    return player


def make_interaction(member: FakeMember) -> SimpleNamespace:
    return SimpleNamespace(
        user=member,
        response=SimpleNamespace(
            send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def reply_of(interaction: SimpleNamespace) -> tuple[str, dict]:
    """response.send_message / followup.send のどちらで返しても中身を取る。

    履歴を読むボタン (占い・狩人) は defer してから followup で返すため、
    テスト側は両方を等しく扱えるようにする。
    """
    if interaction.response.send_message.await_args is not None:
        call = interaction.response.send_message.await_args
    else:
        call = interaction.followup.send.await_args
    assert call is not None, "no reply was sent"
    return call.args[0], call.kwargs


class MediumButtonTest(unittest.IsolatedAsyncioTestCase):
    async def test_non_medium_is_rejected(self) -> None:
        runner = make_runner()
        villager = add_player(runner, 1, Role.VILLAGER)
        view = VillagePanelView(runner)
        interaction = make_interaction(villager.member)

        await view.medium_btn.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        self.assertIn("霊能者", interaction.response.send_message.await_args.args[0])

    async def test_dead_medium_can_still_review_records(self) -> None:
        """死亡後も本人の記録は見返せる (既に知っている情報しか出さない)。"""
        runner = make_runner()
        medium = add_player(runner, 1, Role.MEDIUM, alive=False)
        runner.state.medium_results = [
            {
                "day": 1, "target_id": 2, "target_number": 2,
                "display_name": "02.village-target", "result": "人狼",
            },
        ]
        view = VillagePanelView(runner)
        interaction = make_interaction(medium.member)

        await view.medium_btn.callback(interaction)

        content, _ = reply_of(interaction)
        self.assertIn("02.village-target", content)
        self.assertIn("記録の確認のみ", content)

    async def test_alive_medium_sees_results_at_any_phase(self) -> None:
        runner = make_runner(phase=Phase.NIGHT)  # 夜でも押せる (能動行動ではない)
        medium = add_player(runner, 1, Role.MEDIUM)
        runner.state.medium_results = [
            {
                "day": 1, "target_id": 2, "target_number": 2,
                "display_name": "02.village-target", "result": "人狼",
            },
        ]
        view = VillagePanelView(runner)
        interaction = make_interaction(medium.member)

        await view.medium_btn.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        content = interaction.response.send_message.await_args.args[0]
        self.assertIn("02.village-target", content)
        self.assertIn("人狼", content)
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_no_results_shows_placeholder(self) -> None:
        runner = make_runner()
        medium = add_player(runner, 1, Role.MEDIUM)
        view = VillagePanelView(runner)
        interaction = make_interaction(medium.member)

        await view.medium_btn.callback(interaction)

        content = interaction.response.send_message.await_args.args[0]
        self.assertIn("まだありません", content)


class WolfGuessButtonTest(unittest.IsolatedAsyncioTestCase):
    async def test_player_without_spirit_hold_is_rejected(self) -> None:
        runner = make_runner()
        alive_player = add_player(runner, 1, Role.VILLAGER)
        view = VillagePanelView(runner)
        interaction = make_interaction(alive_player.member)

        await view.wolf_guess_btn.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        self.assertIn(
            "死亡直後の未提出", interaction.response.send_message.await_args.args[0]
        )

    async def test_already_submitted_player_is_rejected(self) -> None:
        runner = make_runner()
        dead = add_player(runner, 1, Role.VILLAGER, alive=False)
        runner.state.spirit_hold_ids.add(1)
        runner.state.wolf_guesses[1] = [2, 3]  # 提出済み
        view = VillagePanelView(runner)
        interaction = make_interaction(dead.member)

        await view.wolf_guess_btn.callback(interaction)

        interaction.response.send_message.assert_awaited_once()

    async def test_holding_player_opens_selection_view(self) -> None:
        runner = make_runner()
        dead = add_player(runner, 1, Role.VILLAGER, alive=False)
        add_player(runner, 2, Role.VILLAGER)
        add_player(runner, 3, Role.VILLAGER)
        runner.state.spirit_hold_ids.add(1)
        runner.state.spirit_hold_events[1] = "run-role:処刑:1:1"
        view = VillagePanelView(runner)
        interaction = make_interaction(dead.member)

        await view.wolf_guess_btn.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertIsInstance(kwargs["view"], VillageWolfGuessView)
        self.assertEqual(kwargs["view"].guess_slots, 2)  # v9系は2人

    async def test_selecting_exact_slot_count_submits_and_releases_hold(self) -> None:
        runner = make_runner()
        dead = add_player(runner, 1, Role.VILLAGER, alive=False)
        add_player(runner, 2, Role.VILLAGER)
        add_player(runner, 3, Role.VILLAGER)
        runner.state.spirit_hold_ids.add(1)
        runner.state.spirit_hold_events[1] = "run-role:処刑:1:1"
        guess_view = VillageWolfGuessView(runner, 1, "run-role:処刑:1:1")
        self.assertEqual(guess_view.guess_slots, 2)
        self.assertEqual(len(guess_view.children), 2)  # 自分以外の2人

        first_btn, second_btn = guess_view.children

        interaction1 = make_interaction(dead.member)
        await first_btn.callback(interaction1)
        interaction1.response.edit_message.assert_awaited_once()
        self.assertEqual(guess_view.selected, [2])

        interaction2 = make_interaction(dead.member)
        with patch(
            "room_runner.database.record_night_action", new=AsyncMock(),
        ) as record:
            await second_btn.callback(interaction2)
        # 最後の選択は霊界開放まで待つ前にACKし、同じephemeralメッセージを
        # 書き換えて結果を残す。
        interaction2.response.defer.assert_awaited_once_with()
        interaction2.response.edit_message.assert_not_awaited()
        interaction2.edit_original_response.assert_awaited_once()

        # 予想の中身は1人1行で記録する (正解数だけでは後から傾向を追えない)。
        self.assertEqual(record.await_count, 2)
        recorded = {
            call.kwargs["target_id"]: call.kwargs["action"]
            for call in record.await_args_list
        }
        self.assertEqual(recorded, {2: "人狼予想", 3: "人狼予想"})
        self.assertEqual(sorted(guess_view.selected), [2, 3])
        self.assertEqual(runner.state.wolf_guesses[1], sorted(guess_view.selected))
        self.assertNotIn(1, runner.state.spirit_hold_ids)
        content = interaction2.edit_original_response.await_args.kwargs["content"]
        self.assertIn("提出しました", content)

    async def test_final_selection_rewrites_same_message_when_submission_is_rejected(self) -> None:
        runner = make_runner()
        dead = add_player(runner, 1, Role.VILLAGER, alive=False)
        add_player(runner, 2, Role.VILLAGER)
        add_player(runner, 3, Role.VILLAGER)
        runner.state.spirit_hold_ids.add(1)
        runner.state.spirit_hold_events[1] = "run-role:処刑:1:1"
        runner.submit_wolf_guess = AsyncMock(return_value=False)
        guess_view = VillageWolfGuessView(runner, 1, "run-role:処刑:1:1")
        first_btn, second_btn = guess_view.children

        await first_btn.callback(make_interaction(dead.member))
        final_interaction = make_interaction(dead.member)
        await second_btn.callback(final_interaction)

        final_interaction.response.defer.assert_awaited_once_with()
        final_interaction.edit_original_response.assert_awaited_once()
        kwargs = final_interaction.edit_original_response.await_args.kwargs
        self.assertIn("受付は終了", kwargs["content"])
        self.assertIsNone(kwargs["view"])
        self.assertTrue(guess_view.is_finished())


class SeerButtonTest(unittest.IsolatedAsyncioTestCase):
    """[占い] は #昼常設パネル経由 (v0.51 工程2/3)。占い師DMは廃止した。"""

    async def test_non_seer_is_rejected(self) -> None:
        runner = make_runner(phase=Phase.NIGHT)
        villager = add_player(runner, 1, Role.VILLAGER)
        view = VillagePanelView(runner)
        interaction = make_interaction(villager.member)

        await view.seer_btn.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        self.assertIn("占い師", interaction.response.send_message.await_args.args[0])

    async def test_dead_seer_can_still_review_records(self) -> None:
        """死亡後は占えないが、初日からの結果は見返せる。"""
        runner = make_runner(phase=Phase.NIGHT)
        seer = add_player(runner, 1, Role.SEER, alive=False)
        view = VillagePanelView(runner)
        interaction = make_interaction(seer.member)

        with patch(
            "room_runner.database.list_night_actions_for_run",
            new=AsyncMock(return_value=[
                {
                    "event_seq": 0, "night_number": 0, "actor_id": 1,
                    "actor_number": 1, "actor_role": "占い師", "action": "初日白",
                    "target_id": 2, "target_number": 2, "result": "村人",
                    "created_at": None,
                },
            ]),
        ):
            await view.seer_btn.callback(interaction)

        content, kwargs = reply_of(interaction)
        self.assertIn("初日白", content)
        self.assertIn("記録の確認のみ", content)
        self.assertNotIn("view", kwargs)

    async def test_seer_sees_history_during_the_day_without_acting(self) -> None:
        """昼に押しても弾かず、記録を出したうえで「夜だけ」と添える。"""
        runner = make_runner(phase=Phase.DAY_DISCUSSION)
        seer = add_player(runner, 1, Role.SEER)
        add_player(runner, 2, Role.VILLAGER)
        view = VillagePanelView(runner)
        interaction = make_interaction(seer.member)

        with patch(
            "room_runner.database.list_night_actions_for_run",
            new=AsyncMock(return_value=[
                {
                    "event_seq": 3, "night_number": 1, "actor_id": 1,
                    "actor_number": 1, "actor_role": "占い師", "action": "占い",
                    "target_id": 2, "target_number": 2, "result": "人狼",
                    "created_at": None,
                },
            ]),
        ):
            await view.seer_btn.callback(interaction)

        content, kwargs = reply_of(interaction)
        self.assertIn("占いの記録", content)
        self.assertIn("1日目夜", content)
        self.assertIn("今は使えません", content)
        self.assertNotIn("view", kwargs)

    async def test_confirming_returns_the_result_immediately(self) -> None:
        runner = make_runner(phase=Phase.NIGHT)
        seer = add_player(runner, 1, Role.SEER)
        wolf = add_player(runner, 2, Role.WEREWOLF)
        view = VillagePanelView(runner)
        interaction = make_interaction(seer.member)

        with patch(
            "room_runner.database.list_night_actions_for_run",
            new=AsyncMock(return_value=[]),
        ):
            await view.seer_btn.callback(interaction)
        target_view = reply_of(interaction)[1]["view"]
        self.assertIsInstance(target_view, VillageSeerTargetView)
        self.assertEqual(len(target_view.children), 1)  # 自分以外の1人だけ

        select_interaction = make_interaction(seer.member)
        await target_view.children[0].callback(select_interaction)
        confirm_view = select_interaction.response.edit_message.await_args.kwargs["view"]

        confirm_interaction = SimpleNamespace(
            user=seer.member,
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        confirm_btn = next(c for c in confirm_view.children if c.label == "実行する")
        with patch("room_runner.database.record_night_action", new=AsyncMock()) as record:
            await confirm_btn.callback(confirm_interaction)

        self.assertEqual(runner.state.seer_target, wolf.user_id)
        content = confirm_interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("人狼", content)
        # v0.51: 占い結果の通常DMは廃止 ([占い]から何度でも見返せるため)。
        # FakeMemberにsendが無いこと自体が「DMを送らない」ことの担保になる。
        self.assertFalse(hasattr(runner, "deliver_seer_result"))
        self.assertFalse(hasattr(seer.member, "send"))
        record.assert_awaited_once()

    async def test_pressing_again_after_confirmed_replays_the_same_result(self) -> None:
        """夜が明けるまで何度でも押せ、確定済みなら同じ結果を再表示する。"""
        runner = make_runner(phase=Phase.NIGHT)
        seer = add_player(runner, 1, Role.SEER)
        target = add_player(runner, 2, Role.VILLAGER)
        runner.state.seer_target = target.user_id
        view = VillagePanelView(runner)
        interaction = make_interaction(seer.member)

        # DB追記に失敗していても今夜ぶんを落とさない (state から補う) 経路。
        with patch(
            "room_runner.database.list_night_actions_for_run",
            new=AsyncMock(return_value=[]),
        ):
            await view.seer_btn.callback(interaction)

        content, kwargs = reply_of(interaction)
        self.assertIn("確定", content)
        self.assertIn(target.display_name, content)
        self.assertIn("村人", content)
        self.assertNotIn("view", kwargs)


class GuardButtonTest(unittest.IsolatedAsyncioTestCase):
    """[狩人] も #昼常設パネル経由 (v0.51 工程2/3)。狩人DMは廃止した。"""

    async def test_non_guard_is_rejected(self) -> None:
        runner = make_runner(phase=Phase.NIGHT)
        villager = add_player(runner, 1, Role.VILLAGER)
        view = VillagePanelView(runner)
        interaction = make_interaction(villager.member)

        await view.guard_btn.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        self.assertIn("狩人", interaction.response.send_message.await_args.args[0])

    async def test_dead_guard_can_still_review_records(self) -> None:
        runner = make_runner(phase=Phase.NIGHT)
        guard = add_player(runner, 1, Role.GUARD, alive=False)
        view = VillagePanelView(runner)
        interaction = make_interaction(guard.member)

        with patch(
            "room_runner.database.list_night_actions_for_run",
            new=AsyncMock(return_value=[]),
        ):
            await view.guard_btn.callback(interaction)

        content, kwargs = reply_of(interaction)
        self.assertIn("記録の確認のみ", content)
        self.assertNotIn("view", kwargs)

    async def test_guard_history_shows_targets_without_gj(self) -> None:
        """昼でも護衛の記録を見返せる。GJの有無は出さない。

        朝の公開文は護衛成功・噛みなし・襲撃なしをすべて「平和な朝」に
        統一しているので、狩人にだけGJを教えると村に出ていない情報を
        本人だけが持つことになる (記録はDBに残す)。
        """
        runner = make_runner(phase=Phase.DAY_DISCUSSION)
        guard = add_player(runner, 1, Role.GUARD)
        add_player(runner, 2, Role.VILLAGER)
        view = VillagePanelView(runner)
        interaction = make_interaction(guard.member)

        requested_actions: list = []

        async def fake_list(*args, actor_id=None, actions=None, **kwargs):
            requested_actions.append(actions)
            return [{
                "event_seq": 4, "night_number": 1, "actor_id": 1,
                "actor_number": 1, "actor_role": "狩人", "action": "護衛",
                "target_id": 2, "target_number": 2, "result": None,
                "created_at": None,
            }]

        with patch(
            "room_runner.database.list_night_actions_for_run", new=fake_list,
        ):
            await view.guard_btn.callback(interaction)

        content, kwargs = reply_of(interaction)
        self.assertIn("護衛の記録", content)
        self.assertIn("1日目夜", content)
        self.assertNotIn("GJ", content)
        self.assertIn("今は使えません", content)
        self.assertNotIn("view", kwargs)
        # 襲撃確定の照会自体を行わない (DBアクセスも1本で済む)
        self.assertEqual(requested_actions, [("護衛",)])

    async def test_confirming_locks_in_the_target(self) -> None:
        runner = make_runner(phase=Phase.NIGHT)
        guard = add_player(runner, 1, Role.GUARD)
        target = add_player(runner, 2, Role.VILLAGER)
        view = VillagePanelView(runner)
        interaction = make_interaction(guard.member)

        with patch(
            "room_runner.database.list_night_actions_for_run",
            new=AsyncMock(return_value=[]),
        ):
            await view.guard_btn.callback(interaction)
        target_view = reply_of(interaction)[1]["view"]
        self.assertIsInstance(target_view, VillageGuardTargetView)

        select_interaction = make_interaction(guard.member)
        await target_view.children[0].callback(select_interaction)
        confirm_view = select_interaction.response.edit_message.await_args.kwargs["view"]

        confirm_interaction = SimpleNamespace(
            user=guard.member,
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        confirm_btn = next(c for c in confirm_view.children if c.label == "実行する")
        with patch("room_runner.database.record_night_action", new=AsyncMock()) as record:
            await confirm_btn.callback(confirm_interaction)

        self.assertEqual(runner.state.guard_target, target.user_id)
        content = confirm_interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("護衛を確定しました", content)
        record.assert_awaited_once()

    async def test_pressing_again_after_confirmed_replays_the_same_target(self) -> None:
        runner = make_runner(phase=Phase.NIGHT)
        guard = add_player(runner, 1, Role.GUARD)
        target = add_player(runner, 2, Role.VILLAGER)
        runner.state.guard_target = target.user_id
        view = VillagePanelView(runner)
        interaction = make_interaction(guard.member)

        with patch(
            "room_runner.database.list_night_actions_for_run",
            new=AsyncMock(return_value=[]),
        ):
            await view.guard_btn.callback(interaction)

        content, kwargs = reply_of(interaction)
        self.assertIn("確定", content)
        self.assertIn(target.display_name, content)
        self.assertNotIn("view", kwargs)

    async def test_reselecting_after_target_is_invalidated_offers_fresh_candidates(self) -> None:
        """GM除外等でguard_targetがNoneへ戻された後、[狩人]を押すと選び直せる。

        「有効なguard_targetなしで夜を解決しない」不変条件のうち、パネル側の
        再選択入口を担う (room_runner._request_guard_reselection 参照)。
        """
        runner = make_runner(phase=Phase.NIGHT)
        guard = add_player(runner, 1, Role.GUARD)
        excluded = add_player(runner, 2, Role.VILLAGER, alive=False)
        fresh = add_player(runner, 3, Role.VILLAGER)
        # _reopen_night_for_required_guard / GM除外相当: 無効値を残さず未選択に戻す
        runner.state.guard_target = None
        runner.state.guard_previous = excluded.user_id
        view = VillagePanelView(runner)
        interaction = make_interaction(guard.member)

        with patch(
            "room_runner.database.list_night_actions_for_run",
            new=AsyncMock(return_value=[]),
        ):
            await view.guard_btn.callback(interaction)

        target_view = reply_of(interaction)[1]["view"]
        self.assertIsInstance(target_view, VillageGuardTargetView)
        candidate_names = {child.label for child in target_view.children}
        self.assertEqual(candidate_names, {fresh.display_name[:80]})
