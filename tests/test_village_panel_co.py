"""#昼 常設村パネルのCO宣言・撤回・結果公開の回帰テスト (v0.51 工程3)。"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import Phase, RoomDefinition
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

class VillagePanelContentTest(unittest.IsolatedAsyncioTestCase):
    """パネル本文の組み立て。役職名の表示と長さ上限の防御。

    v0.51で [結果を公開] を廃止したため、本文に載るのは役職CO (自己申告)
    だけになった。占い・霊能の結果はVCで口頭で伝える。
    """

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
        for number, role in ((3, "占い師"), (5, "占い師"), (7, "霊能者"), (9, "狩人")):
            add_player(runner, number)
            runner.state.co_claims[number] = {
                "number": number, "display_name": f"p{number}",
                "role": role, "day": 1,
            }
        content = runner.build_village_panel_content()
        self.assertIn("【占い師CO 2人】", content)
        self.assertIn("【霊能者CO 1人】", content)
        self.assertIn("【狩人CO 1人】", content)
        # 並びは CO_CLAIMABLE_ROLES の順 (占い師→霊能者→狩人)。
        self.assertLess(content.index("占い師CO"), content.index("霊能者CO"))
        self.assertLess(content.index("霊能者CO"), content.index("狩人CO"))

    async def test_panel_never_carries_result_claims(self) -> None:
        """Botは占い・霊能の結果を公開しない (真偽を扱わないため)。"""
        runner = make_runner()
        add_player(runner, 3)
        runner.state.co_claims[3] = {
            "number": 3, "display_name": "太郎", "role": "占い師", "day": 1,
        }
        content = runner.build_village_panel_content()
        self.assertIn("・3番 太郎", content)
        self.assertNotIn("　└", content)
        self.assertNotIn("結果を公開", content)
        self.assertFalse(hasattr(runner, "publish_co_result"))
        self.assertFalse(hasattr(runner.state, "co_result_claims"))

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
