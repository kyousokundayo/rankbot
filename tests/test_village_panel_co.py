"""#昼 常設村パネルのCO宣言・撤回・結果公開の回帰テスト (v0.51 工程3)。"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import Phase, Role, RoomDefinition
from models import Player
from room_runner import RoomRunner
from views import COResultJudgementView, COResultTargetView, VillagePanelView


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
        RoomDefinition("co-test", "CO確認村", variant_id="v9_cross", rated=False),
    )
    runner.state.game_run_id = "run-co"
    runner.state.guild = SimpleNamespace(id=555)
    runner.state.room_id = "co-test"
    runner.state.phase = phase
    runner.state.day_number = 1
    runner.state.pause_event.set()
    runner._persist_room_state = AsyncMock()
    runner.refresh_village_panel = lambda: None  # 本文更新APIは別テスト対象
    return runner


def add_player(runner: RoomRunner, user_id: int, *, alive: bool = True) -> Player:
    member = FakeMember(user_id)
    player = Player(
        user_id=user_id,
        member=member,
        alive=alive,
        number=user_id,
        base_name=member.display_name,
    )
    runner.state.players[user_id] = player
    return player


class CoClaimTest(unittest.IsolatedAsyncioTestCase):
    async def test_cannot_declare_two_roles_at_once(self) -> None:
        runner = make_runner()
        add_player(runner, 1)
        with patch("room_runner.database.record_co_event", new=AsyncMock()) as record:
            first = await runner.declare_co(1, "占い師")
            second = await runner.declare_co(1, "狂人")

        self.assertIsNone(first)
        self.assertIsNotNone(second)
        self.assertIn("撤回", second)
        self.assertEqual(runner.state.co_claims[1]["role"], "占い師")
        record.assert_awaited_once()

    async def test_can_declare_different_role_after_withdrawal(self) -> None:
        runner = make_runner()
        add_player(runner, 1)
        with patch("room_runner.database.record_co_event", new=AsyncMock()):
            await runner.declare_co(1, "占い師")
            withdraw_error = await runner.withdraw_co(1)
            second = await runner.declare_co(1, "狂人")

        self.assertIsNone(withdraw_error)
        self.assertIsNone(second)
        self.assertEqual(runner.state.co_claims[1]["role"], "狂人")

    async def test_withdraw_without_co_is_rejected(self) -> None:
        runner = make_runner()
        add_player(runner, 1)
        error = await runner.withdraw_co(1)
        self.assertIsNotNone(error)
        self.assertIn("CO中ではありません", error)


class CoRejectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_dead_player_is_rejected(self) -> None:
        runner = make_runner()
        add_player(runner, 1, alive=False)
        error = await runner.declare_co(1, "占い師")
        self.assertIsNotNone(error)
        self.assertEqual(runner.state.co_claims, {})

    async def test_disconnected_player_is_rejected(self) -> None:
        runner = make_runner()
        add_player(runner, 1)
        runner.state.disconnected_players.add(1)
        error = await runner.declare_co(1, "占い師")
        self.assertIsNotNone(error)
        self.assertIn("VCへ復帰", error)
        self.assertEqual(runner.state.co_claims, {})

    async def test_night_phase_is_rejected(self) -> None:
        runner = make_runner(phase=Phase.NIGHT)
        add_player(runner, 1)
        error = await runner.declare_co(1, "占い師")
        self.assertIsNotNone(error)
        self.assertEqual(runner.state.co_claims, {})


class CoResultClaimTest(unittest.IsolatedAsyncioTestCase):
    async def test_any_living_player_can_publish_fake_result_about_dead_player(self) -> None:
        runner = make_runner()
        actor = add_player(runner, 1)
        actor.role = Role.VILLAGER
        target = add_player(runner, 2, alive=False)

        with patch(
            "room_runner.database.record_co_result_events", new=AsyncMock()
        ) as record, patch(
            "room_runner.database.record_co_event", new=AsyncMock()
        ) as record_co:
            error = await runner.publish_co_result(
                actor.user_id,
                Role.SEER.value,
                target.user_id,
                "黒",
                expected_game_run_id="run-co",
            )

        self.assertIsNone(error)
        self.assertEqual(len(runner.state.co_result_claims), 1)
        self.assertEqual(runner.state.co_result_claims[0]["target_id"], target.user_id)
        self.assertEqual(runner.state.co_result_claims[0]["judgement"], "黒")
        self.assertEqual(runner.state.co_claims[actor.user_id]["role"], Role.SEER.value)
        record_co.assert_awaited_once()
        events = record.await_args.kwargs["events"]
        self.assertEqual([event["event_type"] for event in events], ["公開"])

    async def test_same_day_same_type_is_idempotent_or_atomically_replaced(self) -> None:
        runner = make_runner()
        actor = add_player(runner, 1)
        first = add_player(runner, 2)
        second = add_player(runner, 3)

        with patch(
            "room_runner.database.record_co_result_events", new=AsyncMock()
        ) as record, patch(
            "room_runner.database.record_co_event", new=AsyncMock()
        ) as record_co:
            await runner.publish_co_result(
                actor.user_id, Role.SEER.value, first.user_id, "白",
                expected_game_run_id="run-co",
            )
            seq_after_first = runner.state.record_event_seq
            await runner.publish_co_result(
                actor.user_id, Role.SEER.value, first.user_id, "白",
                expected_game_run_id="run-co",
            )
            await runner.publish_co_result(
                actor.user_id, Role.SEER.value, second.user_id, "黒",
                expected_game_run_id="run-co",
            )

        self.assertEqual(seq_after_first, 2)
        self.assertEqual(runner.state.record_event_seq, 4)
        record_co.assert_awaited_once()
        self.assertEqual(record.await_count, 2)
        replacement = record.await_args.kwargs["events"]
        self.assertEqual(
            [event["event_type"] for event in replacement], ["取消", "公開"]
        )
        self.assertEqual([event["event_seq"] for event in replacement], [3, 4])
        self.assertEqual(len(runner.state.co_result_claims), 1)
        self.assertEqual(runner.state.co_result_claims[0]["target_id"], second.user_id)

    async def test_same_result_can_recreate_co_after_withdrawal(self) -> None:
        runner = make_runner()
        actor = add_player(runner, 1)
        target = add_player(runner, 2)

        with patch(
            "room_runner.database.record_co_result_events", new=AsyncMock()
        ) as record_result, patch(
            "room_runner.database.record_co_event", new=AsyncMock()
        ) as record_co:
            await runner.publish_co_result(
                actor.user_id, Role.SEER.value, target.user_id, "白",
                expected_game_run_id="run-co",
            )
            await runner.withdraw_co(actor.user_id)
            error = await runner.publish_co_result(
                actor.user_id, Role.SEER.value, target.user_id, "白",
                expected_game_run_id="run-co",
            )

        self.assertIsNone(error)
        self.assertEqual(runner.state.co_claims[actor.user_id]["role"], Role.SEER.value)
        self.assertEqual(record_co.await_count, 3)
        self.assertEqual(record_result.await_count, 2)

    async def test_result_rejects_stale_run_night_self_and_invalid_judgement(self) -> None:
        runner = make_runner()
        actor = add_player(runner, 1)
        target = add_player(runner, 2)
        stale = await runner.publish_co_result(
            actor.user_id, Role.SEER.value, target.user_id, "白",
            expected_game_run_id="old-run",
        )
        self_target = await runner.publish_co_result(
            actor.user_id, Role.SEER.value, actor.user_id, "白",
            expected_game_run_id="run-co",
        )
        invalid = await runner.publish_co_result(
            actor.user_id, Role.GUARD.value, target.user_id, "白",
            expected_game_run_id="run-co",
        )
        runner.state.phase = Phase.NIGHT
        night = await runner.publish_co_result(
            actor.user_id, Role.SEER.value, target.user_id, "白",
            expected_game_run_id="run-co",
        )

        self.assertIn("終了", stale)
        self.assertIn("自分自身", self_target)
        self.assertIn("申告できません", invalid)
        self.assertIn("受け付けていません", night)
        self.assertEqual(runner.state.co_result_claims, [])

    async def test_medium_co_accepts_only_latest_executed_player(self) -> None:
        runner = make_runner()
        actor = add_player(runner, 1)
        earlier = add_player(runner, 2, alive=False)
        latest = add_player(runner, 3, alive=False)
        runner.state.medium_results = [
            {"target_id": earlier.user_id},
            {"target_id": latest.user_id},
        ]

        rejected = await runner.publish_co_result(
            actor.user_id, Role.MEDIUM.value, earlier.user_id, "白",
            expected_game_run_id="run-co",
        )
        with patch(
            "room_runner.database.record_co_result_events", new=AsyncMock()
        ), patch(
            "room_runner.database.record_co_event", new=AsyncMock()
        ):
            accepted = await runner.publish_co_result(
                actor.user_id, Role.MEDIUM.value, latest.user_id, "黒",
                expected_game_run_id="run-co",
            )

        self.assertIn("処刑された参加者", rejected)
        self.assertIsNone(accepted)
        self.assertEqual(runner.state.co_claims[actor.user_id]["role"], Role.MEDIUM.value)

    async def test_medium_co_uses_frozen_target_after_discord_departure(self) -> None:
        runner = make_runner()
        actor = add_player(runner, 1)
        departed_id = 99
        runner.state.medium_results = [{
            "day": 2,
            "target_id": departed_id,
            "target_number": 7,
            "display_name": "07.退出済み",
            "result": "人狼",
        }]

        target = runner.co_result_targets(actor.user_id, Role.MEDIUM.value)[0]
        self.assertEqual(target.user_id, departed_id)
        self.assertEqual(target.number, 7)
        self.assertEqual(target.display_name, "07.退出済み")

        with patch(
            "room_runner.database.record_co_result_events", new=AsyncMock()
        ), patch(
            "room_runner.database.record_co_event", new=AsyncMock()
        ):
            error = await runner.publish_co_result(
                actor.user_id,
                Role.MEDIUM.value,
                departed_id,
                "黒",
                expected_game_run_id="run-co",
            )

        self.assertIsNone(error)
        result = runner.state.co_result_claims[0]
        self.assertEqual(result["target_id"], departed_id)
        self.assertEqual(result["target_number"], 7)
        self.assertEqual(result["target_name"], "07.退出済み")


class CoButtonLayoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_role_specific_co_buttons_replace_generic_buttons(self) -> None:
        runner = make_runner()
        actor = add_player(runner, 1)
        add_player(runner, 2)
        view = VillagePanelView(runner)
        labels = [item.label for item in view.children]

        self.assertIn("占いCO", labels)
        self.assertIn("霊媒CO", labels)
        self.assertIn("狩人CO", labels)
        self.assertIn("CO撤回", labels)
        self.assertNotIn("CO", labels)
        self.assertNotIn("結果を公開", labels)

        interaction = SimpleNamespace(
            user=actor.member,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await view.seer_co_btn.callback(interaction)
        sent = interaction.response.send_message.await_args
        self.assertIn("結果の対象", sent.args[0])
        self.assertIsInstance(sent.kwargs["view"], COResultTargetView)

    async def test_medium_co_skips_target_selection_and_asks_only_white_black(self) -> None:
        runner = make_runner()
        actor = add_player(runner, 1)
        earlier = add_player(runner, 2, alive=False)
        latest = add_player(runner, 3, alive=False)
        runner.state.medium_results = [
            {"target_id": earlier.user_id},
            {"target_id": latest.user_id},
        ]
        view = VillagePanelView(runner)
        interaction = SimpleNamespace(
            user=actor.member,
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await view.medium_co_btn.callback(interaction)

        sent = interaction.response.send_message.await_args
        self.assertIn(latest.display_name, sent.args[0])
        self.assertIsInstance(sent.kwargs["view"], COResultJudgementView)
        self.assertEqual(sent.kwargs["view"].target_id, latest.user_id)

    async def test_old_village_panel_cannot_open_role_co_in_new_game(self) -> None:
        runner = make_runner()
        actor = add_player(runner, 1)
        add_player(runner, 2)
        old_view = VillagePanelView(runner)
        runner.state.game_run_id = "new-run"
        interaction = SimpleNamespace(
            user=actor.member,
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await old_view.seer_co_btn.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            "⏳ この村パネルは終了しています。",
            ephemeral=True,
        )

    async def test_medium_co_button_uses_departed_target_snapshot(self) -> None:
        runner = make_runner()
        actor = add_player(runner, 1)
        runner.state.medium_results = [{
            "day": 2,
            "target_id": 99,
            "target_number": 7,
            "display_name": "07.退出済み",
            "result": "人狼",
        }]
        view = VillagePanelView(runner)
        interaction = SimpleNamespace(
            user=actor.member,
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        await view.medium_co_btn.callback(interaction)

        sent = interaction.response.send_message.await_args
        self.assertIn("07.退出済み", sent.args[0])
        self.assertIsInstance(sent.kwargs["view"], COResultJudgementView)
        self.assertEqual(sent.kwargs["view"].target_id, 99)


class VillagePanelContentTest(unittest.IsolatedAsyncioTestCase):
    """パネル本文のCO・公開結果表示と長さ上限の防御。"""

    def _seed(self, runner: RoomRunner) -> None:
        for number in range(1, 14):
            add_player(runner, number)
            runner.state.co_claims[number] = {
                "number": number,
                "display_name": f"とてもながいなまえのプレイヤー{number}",
                "role": "占い師",
                "day": 1,
            }

    async def test_co_list_is_grouped_by_claimed_role_with_counts(self) -> None:
        runner = make_runner()
        for number, role in ((3, "占い師"), (5, "占い師"), (7, "霊媒師"), (9, "狩人")):
            add_player(runner, number)
            runner.state.co_claims[number] = {
                "number": number, "display_name": f"p{number}",
                "role": role, "day": 1,
            }
        content = runner.build_village_panel_content()
        self.assertIn("【占い師CO 2人】", content)
        self.assertIn("【霊媒師CO 1人】", content)
        self.assertIn("【狩人CO 1人】", content)
        # 並びは CO_CLAIMABLE_ROLES の順 (占い師→霊媒師→狩人)。
        self.assertLess(content.index("占い師CO"), content.index("霊媒師CO"))
        self.assertLess(content.index("霊媒師CO"), content.index("狩人CO"))

    async def test_panel_carries_result_claims_during_night_too(self) -> None:
        runner = make_runner(phase=Phase.NIGHT)
        add_player(runner, 3)
        add_player(runner, 5, alive=False)
        runner.state.co_claims[3] = {
            "number": 3, "display_name": "太郎", "role": "占い師", "day": 1,
        }
        runner.state.co_result_claims.append({
            "user_id": 3, "number": 3, "display_name": "太郎",
            "role": Role.MEDIUM.value, "target_id": 5, "target_number": 5,
            "target_name": "次郎", "judgement": "黒", "day": 1,
        })
        content = runner.build_village_panel_content()
        self.assertIn("現在フェーズ: 夜", content)
        self.assertIn("・3番 太郎", content)
        self.assertIn("　└ 1日目 霊媒: 5番 次郎 → 黒", content)

    async def test_full_house_of_long_names_stays_within_the_limit(self) -> None:
        runner = make_runner()
        self._seed(runner)
        content = runner.build_village_panel_content()
        # 2000字上限で編集が落ちるとCO受付ごと死ぬので、必ず上限内に収める。
        self.assertLessEqual(len(content), runner._VILLAGE_PANEL_MAX_LENGTH)
        self.assertEqual(content.count("番 とてもながいなまえのプレイヤー"), 13)

    async def test_panel_carries_no_instructions(self) -> None:
        """本文はCO状況だけ。操作説明はボタンのラベルに任せる。"""
        runner = make_runner()
        add_player(runner, 1)
        content = runner.build_village_panel_content()
        self.assertNotIn("操作:", content)
        self.assertNotIn("[CO]", content)
        self.assertNotIn("人狼予想", content)
        self.assertIn("CO一覧", content)


if __name__ == "__main__":
    unittest.main()
