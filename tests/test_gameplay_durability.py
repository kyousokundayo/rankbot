"""夜ゲート・復元チェックポイント・Discord競合の回帰テスト。"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from config import (
    LOG_CATEGORY_LIMIT,
    LOG_CATEGORY_TRIM_TO,
    Phase,
    Role,
    RoomDefinition,
    Team,
)
from game import GameCog
from models import Player, by_number
from room_runner import (
    RoomRunner,
    StateDurabilityError,
    death_nick,
    timer_should_update,
)
from views import (
    GuardView,
    MorningReadyView,
    NightActionConfirmView,
    SeerView,
    VoteView,
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
        self.send = AsyncMock()
        self.edit = AsyncMock()


class FakeRole:
    def __init__(self, role_id: int, name: str, *, default: bool = False) -> None:
        self.id = role_id
        self.name = name
        self._default = default

    def is_default(self) -> bool:
        return self._default


class FakeGuild:
    def __init__(self, members: list[FakeMember], roles: list[FakeRole]) -> None:
        self.id = 123
        self.members = members
        self.roles = roles

    def get_member(self, user_id: int):
        return next((member for member in self.members if member.id == user_id), None)


def make_runner(variant_id: str = "v13_cross") -> RoomRunner:
    runner = RoomRunner(
        None,
        FakeManager(),
        RoomDefinition("test", "テスト村", variant_id=variant_id),
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

    async def test_v9_guard_view_rejects_self_previous_and_offered_out_targets(self) -> None:
        for variant_id in ("v9_cross", "v9_turn"):
            with self.subTest(variant_id=variant_id):
                runner = make_runner(variant_id)
                guard = add_player(runner, 1, Role.GUARD)
                allowed = add_player(runner, 2, Role.VILLAGER)
                previous = add_player(runner, 3, Role.VILLAGER)
                outside = add_player(runner, 4, Role.VILLAGER)
                # 通常の候補生成では自己/前夜対象を含めない。ここでは確認UIを
                # 迂回された場合の最終検証を直接通すため、意図的に含める。
                view = GuardView(runner, [guard, allowed, previous])
                view.actor_id = guard.user_id

                text, committed = await view.commit(guard)
                self.assertFalse(committed)
                self.assertIn("自分", text)

                runner.state.guard_previous = previous.user_id
                text, committed = await view.commit(previous)
                self.assertFalse(committed)
                self.assertIn("前回", text)

                runner.state.guard_previous = None
                text, committed = await view.commit(outside)
                self.assertFalse(committed)
                self.assertIn("対象は護衛できません", text)
                self.assertIsNone(runner.state.guard_target)

                text, committed = await view.commit(allowed)
                self.assertTrue(committed, text)
                self.assertEqual(runner.state.guard_target, allowed.user_id)
                view.stop()

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

    async def test_v9_legacy_morning_confirmation_reopens_before_unprotected_night_resolves(self) -> None:
        """旧snapshotのmorning_confirmedでも、未護衛のまま解決しない。"""
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
            get_partial_message=Mock(return_value=stale)
        )
        runner.state.morning_panel_message_id = 555
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

    async def test_morning_panel_does_not_reveal_the_count_until_time_is_up(self) -> None:
        """夜の間は宣言人数を村へ出さず、押した本人にだけ返す。

        13人固定で夜に行動があるのは狼3+占い+狩人の5人だけなので、
        人数を常時公開すると未宣言者から生存役職を推定できてしまう。
        """
        runner = make_runner()
        player = add_player(runner, 1)
        add_player(runner, 2)
        panel = SimpleNamespace(edit=AsyncMock())
        runner._safe_village_send = AsyncMock(return_value=panel)

        await runner._post_morning_panel()
        posted = runner._safe_village_send.await_args.args[0]
        self.assertNotIn("人**", posted)

        # 押した本人にも人数は返さない (押して確かめて押し直せば
        # 進捗を測れてしまうため)
        feedback, error = await runner.toggle_morning_ready(player.member)
        self.assertIsNone(error)
        self.assertIn("宣言しました", feedback)
        self.assertNotIn("人**", feedback)
        self.assertNotIn("/ 2", feedback)
        # 押下では公開パネルを編集しない
        panel.edit.assert_not_awaited()

        # 目安時間が切れたときに初めてパネルへ人数を出す
        await runner._reveal_morning_count()
        panel.edit.assert_awaited_once()
        self.assertIn("1 / 2人", panel.edit.await_args.kwargs["content"])

    async def test_morning_panel_close_disables_buttons_then_stops(self) -> None:
        """#昼 に1枚なので、夜明け時にボタンを無効化して閉じられる。

        先に表示を無効化してから stop する。逆順だと、編集が着地するまでの
        数百msに押した人へDiscordの汎用エラーが出る。
        """
        runner = make_runner()
        add_player(runner, 1)
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
        other_wolf = add_player(runner, 2, Role.WEREWOLF)
        victim = add_player(runner, 3)
        runner.refresh_wolf_dm_displays = AsyncMock()
        runner._relay_to_wolves = AsyncMock()

        view = WolfVoteView(runner, [victim], wolf)
        select = view.children[0]

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

    async def test_other_confirmation_timeout_and_cancel_never_unlock_after_commit(self) -> None:
        runner = make_runner()
        seer = add_player(runner, 1, Role.SEER)
        target = add_player(runner, 2, Role.VILLAGER)
        origin = SeerView(runner, [target])
        origin.actor_id = seer.user_id
        message = SimpleNamespace(edit=AsyncMock())
        timed_out = NightActionConfirmView(
            origin, target, label="占い先", origin_message=message
        )
        cancelled = NightActionConfirmView(
            origin, target, label="占い先", origin_message=message
        )
        runner.state.seer_target = target.user_id

        await timed_out.on_timeout()
        timeout_view = message.edit.await_args.kwargs["view"]
        self.assertTrue(all(item.disabled for item in timeout_view.children))

        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        cancel_button = next(item for item in cancelled.children if item.label == "やめる")
        await cancel_button.callback(interaction)
        cancel_view = message.edit.await_args.kwargs["view"]
        self.assertTrue(all(item.disabled for item in cancel_view.children))

    async def test_confirmation_persist_failure_rebuilds_unlocked_for_retry(self) -> None:
        runner = make_runner()
        seer = add_player(runner, 1, Role.SEER)
        target = add_player(runner, 2, Role.VILLAGER)
        origin = SeerView(runner, [target])
        origin.actor_id = seer.user_id
        origin.commit = AsyncMock(return_value=("DB保存失敗", False))
        message = SimpleNamespace(edit=AsyncMock())
        confirm = NightActionConfirmView(
            origin, target, label="占い先", origin_message=message
        )
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        confirm_button = next(item for item in confirm.children if item.label == "実行する")

        await confirm_button.callback(interaction)

        interaction.response.defer.assert_awaited_once()
        rebuilt = message.edit.await_args.kwargs["view"]
        self.assertTrue(any(not item.disabled for item in rebuilt.children))

    async def test_seer_action_log_is_inside_same_checkpoint_and_rolls_back(self) -> None:
        runner = make_runner()
        seer = add_player(runner, 1, Role.SEER)
        target = add_player(runner, 2, Role.WEREWOLF)
        view = SeerView(runner, [target])
        view.actor_id = seer.user_id
        captured: list[list[dict]] = []

        async def capture_checkpoint() -> None:
            captured.append([dict(item) for item in runner.state.action_log])

        runner._persist_room_state = AsyncMock(side_effect=capture_checkpoint)
        runner.deliver_seer_result = AsyncMock()
        text, committed = await view.commit(target)

        self.assertTrue(committed, text)
        self.assertEqual(captured[0][-1]["kind"], "占い")
        self.assertIn("結果=人狼", captured[0][-1]["detail"])

        failed = make_runner()
        failed_seer = add_player(failed, 1, Role.SEER)
        failed_target = add_player(failed, 2, Role.VILLAGER)
        failed_view = SeerView(failed, [failed_target])
        failed_view.actor_id = failed_seer.user_id
        failed._persist_room_state = AsyncMock(side_effect=RuntimeError("DB down"))
        _, committed = await failed_view.commit(failed_target)
        self.assertFalse(committed)
        self.assertIsNone(failed.state.seer_target)
        self.assertEqual(failed.state.action_log, [])

    async def test_guard_and_wolf_logs_roll_back_when_checkpoint_fails(self) -> None:
        guard_runner = make_runner()
        guard = add_player(guard_runner, 1, Role.GUARD)
        target = add_player(guard_runner, 2, Role.VILLAGER)
        guard_view = GuardView(guard_runner, [target])
        guard_view.actor_id = guard.user_id
        guard_runner._persist_room_state = AsyncMock(side_effect=RuntimeError("DB down"))

        _, committed = await guard_view.commit(target)

        self.assertFalse(committed)
        self.assertIsNone(guard_runner.state.guard_target)
        self.assertEqual(guard_runner.state.action_log, [])

        wolf_runner = make_runner()
        wolf = add_player(wolf_runner, 1, Role.WEREWOLF)
        victim = add_player(wolf_runner, 2, Role.VILLAGER)
        wolf_view = WolfVoteView(wolf_runner, [victim], wolf)
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

    async def test_vote_is_persisted_and_removed_voter_does_not_block_completion(self) -> None:
        runner = make_runner()
        runner.state.phase = Phase.DAY_VOTE
        runner.state.day_generation = 4
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        removed = add_player(runner, 3)
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
        self.assertTrue(runner.state.vote_complete_event.is_set())
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
        category = SimpleNamespace(name="ログ-昼", channels=[])
        guild = SimpleNamespace(
            id=1, categories=[category], create_category=AsyncMock()
        )
        runner.state.guild = guild
        channel = SimpleNamespace(name="昼", id=500, edit=AsyncMock())

        moved = await runner._archive_game_channel(channel, "ログ-昼", 4)

        self.assertTrue(moved)
        guild.create_category.assert_not_awaited()  # 既存を使い回す
        kwargs = channel.edit.await_args.kwargs
        # 番号を先頭に置く (Discordはカテゴリ内を名前順に並べる)
        self.assertEqual(kwargs["name"], "04-昼")
        self.assertIs(kwargs["category"], category)
        self.assertTrue(kwargs["sync_permissions"])

    async def test_strict_access_room_never_archives_to_public_log(self) -> None:
        """ねいと限定卓の終了ログを共通ログカテゴリへ漏らさない。"""
        runner = RoomRunner(
            None,
            FakeManager(),
            RoomDefinition(
                "strict", "限定卓",
                strict_access_role_names=frozenset({"ねいと"}),
            ),
        )
        runner.state.guild = SimpleNamespace(
            id=1, categories=[], create_category=AsyncMock()
        )
        channel = SimpleNamespace(name="昼", id=500, edit=AsyncMock())

        moved = await runner._archive_game_channel(channel, "ログ-昼", 4)

        self.assertFalse(moved)
        runner.state.guild.create_category.assert_not_awaited()
        channel.edit.assert_not_awaited()

    async def test_log_category_is_trimmed_when_it_hits_the_limit(self) -> None:
        """上限50に達したら古い順に40まで減らす (IDの昇順 = 作成順)。"""
        runner = make_runner()
        old = [SimpleNamespace(name=f"{i:02d}-昼", id=100 + i, delete=AsyncMock())
               for i in range(LOG_CATEGORY_LIMIT)]
        category = SimpleNamespace(name="ログ-昼", channels=list(reversed(old)))
        runner.state.guild = SimpleNamespace(
            id=1, categories=[category], create_category=AsyncMock()
        )
        channel = SimpleNamespace(name="昼", id=9999, edit=AsyncMock())

        await runner._archive_game_channel(channel, "ログ-昼", 51)

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

        moved = await runner._archive_game_channel(channel, "ログ-昼", 4)

        self.assertFalse(moved)
        channel.edit.assert_not_awaited()

    async def test_provisional_vote_can_be_changed_but_the_final_one_cannot(self) -> None:
        """議論中の仮投票は入れ替え自由、投票フェーズでは確定。

        議論の途中で心変わりできないと、早く入れた人が話し合いから
        降りてしまうため、確定は投票フェーズまで遅らせる。
        """
        runner = make_runner()
        runner.state.phase = Phase.DAY_DISCUSSION
        voter = add_player(runner, 1)
        first = add_player(runner, 2)
        second = add_player(runner, 3)
        alive = [voter, first, second]

        provisional = VoteView(runner, alive, alive, provisional=True)
        self.assertIsNone(provisional._vote_error(voter.user_id, first.user_id))
        result, committed = await provisional.commit_vote(voter.user_id, first.user_id)
        self.assertTrue(committed)
        self.assertIn("入れ替えられます", result)

        # 入れ替えできる
        self.assertIsNone(provisional._vote_error(voter.user_id, second.user_id))
        _, committed = await provisional.commit_vote(voter.user_id, second.user_id)
        self.assertTrue(committed)
        self.assertEqual(runner.state.votes[voter.user_id], second.user_id)

        # 投票フェーズに入ると、同じ票のまま変更を受け付けない
        runner.state.phase = Phase.DAY_VOTE
        final = VoteView(runner, alive, alive)
        self.assertEqual(final._vote_error(voter.user_id, first.user_id), "投票済みです。")
        # 仮投票パネルもフェーズ違いで弾かれる
        self.assertIsNotNone(provisional._vote_error(voter.user_id, first.user_id))

    async def test_all_provisional_votes_end_the_discussion_early(self) -> None:
        """生存者全員が仮投票を終えたら議論を切り上げる。"""
        runner = make_runner()
        runner.state.phase = Phase.DAY_DISCUSSION
        players = [add_player(runner, uid) for uid in (1, 2, 3)]

        view = VoteView(runner, players, players, provisional=True)
        for voter, target in zip(players, [players[1], players[2], players[0]]):
            await view.commit_vote(voter.user_id, target.user_id)
            if voter is not players[-1]:
                self.assertFalse(runner.state.vote_complete_event.is_set())

        self.assertTrue(runner.state.vote_complete_event.is_set())

    async def test_vote_phase_keeps_votes_cast_before_it_started(self) -> None:
        """議論中の仮投票と、復元した投票の両方をそのまま引き継ぐ。

        投票フェーズは votes をクリアしない。議論中に入れた票が
        持ち越され、全員そろっていればそのまま開示される。
        """
        runner = make_runner()
        first = add_player(runner, 1)
        second = add_player(runner, 2)
        runner.state.votes = {first.user_id: second.user_id}
        runner._pausable_countdown = AsyncMock(return_value=True)

        executed = await runner._day_vote()

        self.assertEqual(executed, second.user_id)
        self.assertEqual(runner.state.votes, {first.user_id: second.user_id})

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
        runner._restore_nicknames = AsyncMock()
        runner._teardown_game_roles_and_perms = AsyncMock()
        runner._post_lobby_ui = AsyncMock()
        old_run = runner.state.game_run_id

        await runner.force_end("テスト廃村")

        fake_view.stop.assert_called()
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

    async def test_death_combines_nickname_mute_and_alive_role_removal(self) -> None:
        runner = make_runner()
        player = add_player(runner, 1)
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
        runner.state.village_channel = SimpleNamespace(set_permissions=AsyncMock())
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
            player.member.voice.mute = kwargs["mute"]
            player.member.roles = [everyone, *kwargs["roles"]]
            return player.member

        player.member.edit = AsyncMock(side_effect=apply_patch)
        runner._remove_alive_role = AsyncMock()

        await runner._apply_death_effect(effect)

        player.member.edit.assert_awaited_once()
        edit_kwargs = player.member.edit.await_args.kwargs
        self.assertIn("nick", edit_kwargs)
        self.assertTrue(edit_kwargs["mute"])
        self.assertIn(marker, edit_kwargs["roles"])
        self.assertNotIn(alive, edit_kwargs["roles"])
        runner._remove_alive_role.assert_not_awaited()
        self.assertEqual(runner.state.pending_death_effects, [])

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
        denied = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden", headers={}), "denied"
        )
        runner.state.village_channel = SimpleNamespace(
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
        runner.state.spirit_channel = SimpleNamespace(
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
        runner.state.spirit_channel = SimpleNamespace(
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

        failed = await runner._send_role_dms()
        first_messages = [call.args[0] for call in seer.member.send.await_args_list]
        failed_again = await runner._send_role_dms()

        self.assertEqual(failed, [])
        self.assertEqual(failed_again, [])
        self.assertTrue(runner.state.initial_seer_result_sent)
        self.assertEqual(runner.state.initial_seer_target, white.user_id)
        self.assertEqual(
            sum("初日占い結果" in message for message in first_messages),
            1,
        )
        self.assertEqual(seer.member.send.await_count, len(first_messages))

    async def test_game_over_checkpoint_failure_does_not_turn_result_into_abandonment(self) -> None:
        runner = make_runner()
        runner.state.guild = SimpleNamespace(id=123)
        add_player(runner, 1, Role.VILLAGER)
        runner._restore_nicknames = AsyncMock()
        runner._teardown_game_roles_and_perms = AsyncMock()
        runner._post_lobby_ui = AsyncMock()
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
        ):
            await runner._end_game(Team.VILLAGE)

        settle.assert_awaited_once()
        self.assertEqual(runner.state.phase, Phase.LOBBY)
        self.assertGreaterEqual(calls, 5)

    async def test_end_game_posts_action_log_before_result_and_rating_last(self) -> None:
        runner = make_runner()
        runner.state.guild = SimpleNamespace(id=123)
        add_player(runner, 1, Role.VILLAGER)
        runner._restore_nicknames = AsyncMock()
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
        runner.state.village_channel = SimpleNamespace(set_permissions=AsyncMock())
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
        runner._restore_nicknames = AsyncMock()
        runner._teardown_game_roles_and_perms = AsyncMock()
        runner._post_lobby_ui = AsyncMock()
        calls = 0

        async def fail_first_three():
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise RuntimeError("DB down")

        runner._persist_room_state = fail_first_three
        await runner.force_end("テスト廃村")

        self.assertEqual(runner.state.phase, Phase.LOBBY)
        runner._restore_nicknames.assert_awaited_once()
        runner._teardown_game_roles_and_perms.assert_awaited_once()
        self.assertGreaterEqual(calls, 4)

    async def test_nonactive_mute_intent_only_promotes_same_patch_evidence(self) -> None:
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

        self.assertEqual(owned, {1, 9})
        self.assertNotIn(2, owned)

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
