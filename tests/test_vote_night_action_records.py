"""v0.49 工程3/3: 投票ログ (record_vote_event) と夜行動ログ
(record_night_action: 襲撃投票・襲撃確定) の回帰テスト。

占い・護衛・霊能の record_night_action は工程2/3で既にフックずみ
(tests/test_village_panel_role_actions.py, room_runner.py の
_apply_death_effect 参照)。ここでは投票ログと、狼DMのコードに触れず
_process_night 側から記録する襲撃投票・襲撃確定を検証する。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import Phase, Role, RoomDefinition
from models import Player
from room_runner import RoomRunner
from views import RunoffVoteView, VoteView


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


def make_runner(*, phase: Phase = Phase.DAY_VOTE) -> RoomRunner:
    # 投票発言枠 (クロストーク) の設定を避けるため、一斉投票のターン制
    # variant を使う。ログの記録経路はどちらのモードでも commit_vote に
    # 共通なので、投票方式そのものはこのテストの本題ではない。
    runner = RoomRunner(
        None,
        FakeManager(),
        RoomDefinition("record", "記録確認村", variant_id="v9_turn", rated=False),
    )
    runner.state.game_run_id = "run-record"
    runner.state.guild = SimpleNamespace(id=777)
    runner.state.room_id = "record"
    runner.state.phase = phase
    runner.state.day_number = 1
    runner.state.pause_event.set()
    runner._persist_room_state = AsyncMock()
    runner._safe_village_send = AsyncMock()
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


class VoteEventRecordingTest(unittest.IsolatedAsyncioTestCase):
    """本投票・決選投票の commit_vote が record_vote_event を追記すること。"""

    async def test_regular_vote_records_with_kind_and_round_zero(self) -> None:
        runner = make_runner()
        voter = add_player(runner, 1)
        target = add_player(runner, 2)
        view = VoteView(runner, [voter, target], [voter])

        with patch("views.database.record_vote_event", new=AsyncMock()) as record:
            result, committed = await view.commit_vote(voter.user_id, target.user_id)

        self.assertTrue(committed)
        record.assert_awaited_once()
        _args, kwargs = record.await_args
        self.assertEqual(kwargs["vote_kind"], "本投票")
        self.assertEqual(kwargs["round_index"], 0)
        self.assertEqual(kwargs["voter_id"], voter.user_id)
        self.assertEqual(kwargs["target_id"], target.user_id)
        self.assertEqual(kwargs["day_number"], 1)
        self.assertGreaterEqual(kwargs["event_seq"], 1)

    async def test_runoff_vote_records_with_kind_and_round_one(self) -> None:
        runner = make_runner(phase=Phase.DAY_RUNOFF_VOTE)
        voter = add_player(runner, 1)
        candidate = add_player(runner, 2)
        view = RunoffVoteView(runner, [candidate], [voter])

        with patch("views.database.record_vote_event", new=AsyncMock()) as record:
            result, committed = await view.commit_vote(voter.user_id, candidate.user_id)

        self.assertTrue(committed)
        _args, kwargs = record.await_args
        self.assertEqual(kwargs["vote_kind"], "決選投票")
        self.assertEqual(kwargs["round_index"], 1)

    async def test_vote_change_appends_new_row_without_overwriting(self) -> None:
        """票を差し替えた場合 (投票取り消し→再投票) も1行ずつ追記され、
        event_seq が単調増加すること (上書きしない設計)。
        """
        runner = make_runner()
        voter = add_player(runner, 1)
        first_target = add_player(runner, 2)
        second_target = add_player(runner, 3)
        view = VoteView(runner, [first_target, second_target], [voter])

        with patch("views.database.record_vote_event", new=AsyncMock()) as record:
            await view.commit_vote(voter.user_id, first_target.user_id)
            first_seq = record.await_args.kwargs["event_seq"]

            # 既存の実装 (GM除外などによる票の失効) と同じく、
            # 一度票を取り除いてから再投票させる。
            runner.state.votes.pop(voter.user_id, None)
            await view.commit_vote(voter.user_id, second_target.user_id)
            second_seq = record.await_args.kwargs["event_seq"]

        self.assertEqual(record.await_count, 2)
        self.assertGreater(second_seq, first_seq)
        self.assertEqual(
            [c.kwargs["target_id"] for c in record.await_args_list],
            [first_target.user_id, second_target.user_id],
        )

    async def test_abstention_is_recorded_with_null_target(self) -> None:
        runner = make_runner()
        absent = add_player(runner, 1)

        with patch("views.database.record_vote_event", new=AsyncMock()) as record:
            await runner._record_vote_abstentions([absent], "本投票", 0)

        record.assert_awaited_once()
        _args, kwargs = record.await_args
        self.assertIsNone(kwargs["target_id"])
        self.assertIsNone(kwargs["target_number"])
        self.assertEqual(kwargs["voter_id"], absent.user_id)
        self.assertEqual(kwargs["vote_kind"], "本投票")
        self.assertEqual(kwargs["round_index"], 0)

    async def test_runoff_abstention_uses_round_one(self) -> None:
        runner = make_runner(phase=Phase.DAY_RUNOFF_VOTE)
        absent = add_player(runner, 1)

        with patch("views.database.record_vote_event", new=AsyncMock()) as record:
            await runner._record_vote_abstentions([absent], "決選投票", 1)

        self.assertEqual(record.await_args.kwargs["vote_kind"], "決選投票")
        self.assertEqual(record.await_args.kwargs["round_index"], 1)

    async def test_db_failure_during_vote_does_not_block_commit(self) -> None:
        """DB書き込みが失敗しても投票そのものは成立すること。"""
        runner = make_runner()
        voter = add_player(runner, 1)
        target = add_player(runner, 2)
        view = VoteView(runner, [target], [voter])

        with patch(
            "views.database.record_vote_event",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            result, committed = await view.commit_vote(voter.user_id, target.user_id)

        self.assertTrue(committed)
        self.assertEqual(runner.state.votes[voter.user_id], target.user_id)

    async def test_db_failure_during_abstention_does_not_raise(self) -> None:
        runner = make_runner()
        absent = add_player(runner, 1)

        with patch(
            "views.database.record_vote_event",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            await runner._record_vote_abstentions([absent], "本投票", 0)  # 例外が出ないこと


class NightActionRecordingTest(unittest.IsolatedAsyncioTestCase):
    """_process_night が record_night_action へ襲撃投票・襲撃確定を記録すること。

    狼DMのコード (WolfVoteView 等) には一切触れず、wolf_voters / wolf_target
    が確定した後にこの受付関数側で読み取るだけであることを確認する。
    """

    def _runner_for_attack(self) -> tuple[RoomRunner, Player, Player, Player]:
        runner = make_runner(phase=Phase.NIGHT)
        wolf1 = add_player(runner, 1, Role.WEREWOLF)
        wolf2 = add_player(runner, 2, Role.WEREWOLF)
        victim = add_player(runner, 3, Role.VILLAGER)
        runner._execute_player = AsyncMock(
            side_effect=lambda pid, method: setattr(
                runner.state.get_player(pid), "alive", False
            )
        )
        return runner, wolf1, wolf2, victim

    async def test_records_each_wolf_vote_and_final_attack_confirmation(self) -> None:
        runner, wolf1, wolf2, victim = self._runner_for_attack()
        runner.state.wolf_voters = {wolf1.user_id: victim.user_id, wolf2.user_id: victim.user_id}
        runner.state.wolf_target = victim.user_id

        with patch("room_runner.database.record_night_action", new=AsyncMock()) as record:
            killed_id = await runner._process_night()

        self.assertEqual(killed_id, victim.user_id)
        calls = {c.kwargs["action"] for c in record.await_args_list}
        self.assertEqual(calls, {"襲撃投票", "襲撃確定"})

        vote_calls = [c.kwargs for c in record.await_args_list if c.kwargs["action"] == "襲撃投票"]
        self.assertEqual(len(vote_calls), 2)
        voter_ids = {c["actor_id"] for c in vote_calls}
        self.assertEqual(voter_ids, {wolf1.user_id, wolf2.user_id})
        for c in vote_calls:
            self.assertEqual(c["actor_role"], "人狼")
            self.assertEqual(c["target_id"], victim.user_id)

        confirm = next(c.kwargs for c in record.await_args_list if c.kwargs["action"] == "襲撃確定")
        self.assertEqual(confirm["target_id"], victim.user_id)
        self.assertEqual(confirm["result"], "襲撃成功")
        # event_seq は投票分と重複せず、全体で単調増加していること
        seqs = [c.kwargs["event_seq"] for c in record.await_args_list]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertEqual(len(seqs), len(set(seqs)))

    async def test_guard_success_records_gj_result(self) -> None:
        runner, wolf1, wolf2, victim = self._runner_for_attack()
        runner.state.wolf_voters = {wolf1.user_id: victim.user_id}
        runner.state.wolf_target = victim.user_id
        runner.state.guard_target = victim.user_id

        with patch("room_runner.database.record_night_action", new=AsyncMock()) as record:
            killed_id = await runner._process_night()

        self.assertIsNone(killed_id)
        confirm = next(c.kwargs for c in record.await_args_list if c.kwargs["action"] == "襲撃確定")
        self.assertEqual(confirm["result"], "GJ")
        self.assertEqual(confirm["target_id"], victim.user_id)
        runner._execute_player.assert_not_awaited()

    async def test_no_bite_records_null_target(self) -> None:
        runner, wolf1, wolf2, victim = self._runner_for_attack()
        runner.state.wolf_voters = {wolf1.user_id: -1}
        runner.state.wolf_target = -1

        with patch("room_runner.database.record_night_action", new=AsyncMock()) as record:
            killed_id = await runner._process_night()

        self.assertIsNone(killed_id)
        confirm = next(c.kwargs for c in record.await_args_list if c.kwargs["action"] == "襲撃確定")
        self.assertEqual(confirm["result"], "噛みなし")
        self.assertIsNone(confirm["target_id"])
        vote_call = next(c.kwargs for c in record.await_args_list if c.kwargs["action"] == "襲撃投票")
        self.assertIsNone(vote_call["target_id"])
        self.assertEqual(vote_call["result"], "噛みなし")

    async def test_no_vote_at_all_records_no_attack(self) -> None:
        runner, wolf1, wolf2, victim = self._runner_for_attack()
        runner.state.wolf_voters = {}
        runner.state.wolf_target = None

        with patch("room_runner.database.record_night_action", new=AsyncMock()) as record:
            killed_id = await runner._process_night()

        self.assertIsNone(killed_id)
        self.assertEqual(len(record.await_args_list), 1)  # 襲撃確定のみ (投票が無い)
        confirm = record.await_args_list[0].kwargs
        self.assertEqual(confirm["action"], "襲撃確定")
        self.assertEqual(confirm["result"], "襲撃なし")

    async def test_db_failure_does_not_block_night_resolution(self) -> None:
        runner, wolf1, wolf2, victim = self._runner_for_attack()
        runner.state.wolf_voters = {wolf1.user_id: victim.user_id}
        runner.state.wolf_target = victim.user_id

        with patch(
            "room_runner.database.record_night_action",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            killed_id = await runner._process_night()  # 例外が出ないこと

        self.assertEqual(killed_id, victim.user_id)
        self.assertTrue(runner.state.night_resolved)


if __name__ == "__main__":
    unittest.main()
