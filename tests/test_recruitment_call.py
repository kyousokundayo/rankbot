"""募集カードの「募集」ボタン (DM一斉通知) をDiscord APIなしで検証する。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

import database
from config import Phase, RECRUITMENT_CALL_DM_DAILY_LIMIT
from recruitment import RecruitmentCardView, RecruitmentManager


def _make_interaction(guild, member):
    return SimpleNamespace(
        guild=guild,
        user=member,
        response=SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class RecruitmentCallTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-recruitment-call-")
        self._old_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "call.db")
        await database.init_db()

        self.room_id = "private_1"
        await database.save_private_room(1, self.room_id, 1, "GM村", "v13_cross")
        await database.mark_private_room_active(1, self.room_id, category_id=101)

        self.members: dict[int, SimpleNamespace] = {}

        def _get_member(user_id):
            return self.members.get(user_id)

        self.guild = SimpleNamespace(id=1, get_member=_get_member)

        self.state = SimpleNamespace(
            phase=Phase.LOBBY, players={}, gm_id=None, recruitment_id=None,
            room_id=self.room_id, room_name="GM村", lobby_channel=None,
        )
        self.room = SimpleNamespace(
            state=self.state,
            room_def=SimpleNamespace(
                room_id=self.room_id,
                name="GM村",
                variant_id="v13_cross",
                private_owner_id=1,
                allowed_ranks=None, allowed_gm_user_ids=None, owner_only_gm=False,
                access_role_names=None, strict_access_role_names=None,
            ),
            is_private_room=lambda: True,
        )
        self.cog = SimpleNamespace(rooms={self.room_id: self.room})
        self.manager = RecruitmentManager(SimpleNamespace(), self.cog)

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._old_path
        self._tmp.cleanup()

    def _add_member(self, user_id: int) -> MagicMock:
        member = MagicMock(spec=discord.Member)
        member.id = user_id
        member.send = AsyncMock()
        self.members[user_id] = member
        return member

    async def _create_recruitment(self, host_id: int = 1, *, title: str = "募集通知テスト") -> int:
        return await database.create_recruitment(
            1, host_id, title=title,
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            room_id=self.room_id, variant_id="v13_cross",
            streaming=False, allowed_ranks=None,
        )

    async def _press_call(self, recruitment_id: int, host_id: int = 1):
        if host_id not in self.members:
            self._add_member(host_id)
        card = RecruitmentCardView(self.manager, recruitment_id)
        interaction = _make_interaction(self.guild, self.members[host_id])
        await card.call(interaction)
        return interaction

    async def _wait_background_tasks(self) -> None:
        # asyncio.create_task で起動したバックグラウンド送信の完了を待つ。
        pending = [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        if pending:
            await asyncio.gather(*pending)

    # --- 1村1日1回 -----------------------------------------------------

    async def test_open_recruitment_call_is_once_per_day(self) -> None:
        recruitment_id = await self._create_recruitment()
        called_on = "2026-08-20"
        first = await database.open_recruitment_call(recruitment_id, 1, 1, called_on)
        second = await database.open_recruitment_call(recruitment_id, 1, 1, called_on)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    async def test_second_press_same_day_is_rejected(self) -> None:
        recruitment_id = await self._create_recruitment()
        self._add_member(200)
        await database.set_user_notification_prefs(
            1, 200, allow_notifications=True, notify_on_call=True,
        )
        first = await self._press_call(recruitment_id)
        await self._wait_background_tasks()
        first.followup.send.assert_any_await(
            "1人へ順次送信します。", ephemeral=True,
        )

        second = await self._press_call(recruitment_id)
        second.followup.send.assert_awaited_with(
            "この村の募集通知は今日すでに送りました。", ephemeral=True,
        )

    # --- 宛先の除外 -------------------------------------------------

    async def test_excludes_participant_backup_host_and_blocked(self) -> None:
        recruitment_id = await self._create_recruitment(host_id=1)
        await database.add_recruitment_entry(recruitment_id, 300)  # 参加
        for uid in (300, 400, 500):
            self._add_member(uid)
            await database.set_user_notification_prefs(
                1, uid, allow_notifications=True, notify_on_call=True,
            )
        # 400を主催者(1)の同村拒否にする→通知対象から外れる
        await database.add_player_block(1, 400, 1)
        # 500だけが宛先として残るはず
        interaction = await self._press_call(recruitment_id)
        await self._wait_background_tasks()

        self.members[300].send.assert_not_awaited()
        self.members[400].send.assert_not_awaited()
        self.members[500].send.assert_awaited_once()
        interaction.followup.send.assert_any_await(
            "1人へ順次送信します。", ephemeral=True,
        )

    async def test_excludes_users_over_daily_cap(self) -> None:
        recruitment_id = await self._create_recruitment(host_id=1)
        capped = self._add_member(600)
        ok = self._add_member(601)
        for uid in (600, 601):
            await database.set_user_notification_prefs(
                1, uid, allow_notifications=True, notify_on_call=True,
            )
        # 600の当日送信済み件数を上限まで積んでおく。UNIQUE(recruitment_id,
        # called_on) があるため、同じ called_on で複数回呼ぶには村を分けて
        # 別の recruitment_id を用意する。
        called_on = datetime.now().astimezone().date().isoformat()
        for i in range(RECRUITMENT_CALL_DM_DAILY_LIMIT):
            filler_room_id = f"private_filler_{i}"
            await database.save_private_room(1, filler_room_id, 1, f"GM村filler{i}", "v13_cross")
            await database.mark_private_room_active(1, filler_room_id, category_id=200 + i)
            filler_recruitment_id = await database.create_recruitment(
                1, 1, title=f"別枠{i}",
                scheduled_at=datetime.now(timezone.utc) + timedelta(hours=2),
                room_id=filler_room_id, variant_id="v13_cross",
                streaming=False, allowed_ranks=None,
            )
            filler_call_id = await database.open_recruitment_call(
                filler_recruitment_id, 1, 1, called_on,
            )
            self.assertIsNotNone(filler_call_id)
            await database.mark_recruitment_call_delivery(
                filler_call_id, capped.id, "2026-08-20T00:00:00+00:00", "sent",
            )

        await self._press_call(recruitment_id)
        await self._wait_background_tasks()

        capped.send.assert_not_awaited()
        ok.send.assert_awaited_once()

    # --- 送達結果の記録 -------------------------------------------------

    async def test_forbidden_is_recorded_permanently(self) -> None:
        recruitment_id = await self._create_recruitment()
        blocked_dm = self._add_member(700)
        blocked_dm.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="forbidden"), "no dm",
        )
        await database.set_user_notification_prefs(
            1, 700, allow_notifications=True, notify_on_call=True,
        )
        await self._press_call(recruitment_id)
        await self._wait_background_tasks()

        async with database.connect_db() as db:
            rows = await db.execute_fetchall(
                "SELECT delivery_status FROM recruitment_call_deliveries d "
                "JOIN recruitment_calls c ON c.id = d.call_id "
                "WHERE c.recruitment_id = ? AND d.user_id = ?",
                (recruitment_id, 700),
            )
        self.assertEqual(list(rows), [("forbidden",)])

    async def test_error_embed_sends_nothing(self) -> None:
        recruitment_id = await self._create_recruitment()
        subscriber = self._add_member(800)
        await database.set_user_notification_prefs(
            1, 800, allow_notifications=True, notify_on_call=True,
        )
        with patch.object(
            self.manager, "build_embed",
            AsyncMock(return_value=discord.Embed(title="エラー", color=discord.Color.red())),
        ):
            interaction = await self._press_call(recruitment_id)
            await self._wait_background_tasks()
        subscriber.send.assert_not_awaited()
        interaction.followup.send.assert_awaited_with(
            "募集情報を取得できなかったため、通知を送信しませんでした。", ephemeral=True,
        )

    # --- ペーサーの独立性 -------------------------------------------------

    async def test_does_not_use_shared_bulk_api_lock(self) -> None:
        """募集通知DMは全卓共有の bulk_api_lock を占有しない (専用ペーサーで送る)。"""
        recruitment_id = await self._create_recruitment()
        subscriber = self._add_member(900)
        await database.set_user_notification_prefs(
            1, 900, allow_notifications=True, notify_on_call=True,
        )
        self.manager.game_cog.paced_discord_api_call = AsyncMock(
            side_effect=AssertionError("paced_discord_api_call は使ってはいけない"),
        )
        await self._press_call(recruitment_id)
        await self._wait_background_tasks()
        subscriber.send.assert_awaited_once()
        self.manager.game_cog.paced_discord_api_call.assert_not_awaited()


    # --- タスク参照の保持 (指摘1) ----------------------------------------

    async def test_delivery_task_is_strongly_referenced_until_done(self) -> None:
        """asyncio.create_task の戻り値を握らないとGCで消えうる問題への回帰。

        送信中は _call_delivery_tasks に強参照が入り、完了後は自動で
        取り除かれることを確認する。
        """
        recruitment_id = await self._create_recruitment()
        subscriber = self._add_member(950)
        await database.set_user_notification_prefs(
            1, 950, allow_notifications=True, notify_on_call=True,
        )
        gate = asyncio.Event()

        async def blocked_send(member, embed):
            await gate.wait()

        with patch.object(self.manager, "_send_call_dm_paced", blocked_send):
            await self._press_call(recruitment_id)
            # まだ送信は完了していないが、タスクへの強参照が保持されている。
            self.assertEqual(len(self.manager._call_delivery_tasks), 1)
            gate.set()
            await self._wait_background_tasks()
        # 完了後は自己除去される (done_callback)。
        self.assertEqual(len(self.manager._call_delivery_tasks), 0)
        subscriber.send.assert_not_awaited()  # _send_call_dm_pacedを差し替えたため

    # --- 想定外例外でも残り全員へ届く (指摘3) ------------------------------

    async def test_unexpected_exception_for_one_recipient_does_not_stop_others(self) -> None:
        recruitment_id = await self._create_recruitment()
        broken = self._add_member(960)
        ok = self._add_member(961)
        for uid in (960, 961):
            await database.set_user_notification_prefs(
                1, uid, allow_notifications=True, notify_on_call=True,
            )
        original = self.manager._send_call_dm_paced

        async def flaky_send(member, embed):
            if member.id == 960:
                raise ConnectionError("network down")
            return await original(member, embed)

        with patch.object(self.manager, "_send_call_dm_paced", flaky_send):
            await self._press_call(recruitment_id)
            await self._wait_background_tasks()

        ok.send.assert_awaited_once()
        async with database.connect_db() as db:
            rows = await db.execute_fetchall(
                "SELECT d.user_id, d.delivery_status FROM recruitment_call_deliveries d "
                "JOIN recruitment_calls c ON c.id = d.call_id "
                "WHERE c.recruitment_id = ?",
                (recruitment_id,),
            )
        status_by_user = dict(rows)
        self.assertEqual(status_by_user.get(960), "failed")
        self.assertEqual(status_by_user.get(961), "sent")

    # --- 再起動後の再開 (指摘2) --------------------------------------------

    async def test_stalled_call_resumes_from_periodic_loop(self) -> None:
        """再起動でDM未送のまま残った呼び出しを、定期ループから再開する。"""
        recruitment_id = await self._create_recruitment()
        subscriber = self._add_member(970)
        await database.set_user_notification_prefs(
            1, 970, allow_notifications=True, notify_on_call=True,
        )
        called_on = datetime.now(timezone.utc).astimezone().date().isoformat()
        # open_recruitment_call だけ呼び、配信タスクは起動しない
        # (=Botが落ちて誰にも送達記録が残っていない状態を再現)。
        call_id = await database.open_recruitment_call(
            recruitment_id, 1, 1, called_on,
        )
        self.assertIsNotNone(call_id)
        subscriber.send.assert_not_awaited()

        await self.manager._resume_stalled_recruitment_calls(
            self.guild, datetime.now(timezone.utc),
        )
        await self._wait_background_tasks()

        subscriber.send.assert_awaited_once()

    async def test_call_older_than_an_hour_is_not_resumed(self) -> None:
        recruitment_id = await self._create_recruitment()
        subscriber = self._add_member(980)
        await database.set_user_notification_prefs(
            1, 980, allow_notifications=True, notify_on_call=True,
        )
        called_on = datetime.now(timezone.utc).astimezone().date().isoformat()
        call_id = await database.open_recruitment_call(
            recruitment_id, 1, 1, called_on,
        )
        self.assertIsNotNone(call_id)
        async with database.connect_db() as db:
            await db.execute(
                "UPDATE recruitment_calls SET called_at = ? WHERE id = ?",
                (
                    (datetime.now(timezone.utc) - timedelta(hours=2))
                    .replace(microsecond=0).isoformat(),
                    call_id,
                ),
            )
            await db.commit()

        await self.manager._resume_stalled_recruitment_calls(
            self.guild, datetime.now(timezone.utc),
        )
        await self._wait_background_tasks()

        subscriber.send.assert_not_awaited()

    async def test_resume_does_not_spawn_second_task_while_delivery_in_progress(self) -> None:
        """配信中に定期ループのresumeが割り込んでも、同じcall_idへ2つ目の
        配信タスクを立てない (_active_call_ids ガード, 指摘2)。
        """
        recruitment_id = await self._create_recruitment()
        subscriber = self._add_member(971)
        await database.set_user_notification_prefs(
            1, 971, allow_notifications=True, notify_on_call=True,
        )
        gate = asyncio.Event()

        async def blocked_send(member, embed):
            await gate.wait()

        with patch.object(self.manager, "_send_call_dm_paced", blocked_send):
            await self._press_call(recruitment_id)
            # 配信タスクはまだ完了していない (gateで止めている)。
            self.assertEqual(len(self.manager._call_delivery_tasks), 1)

            await self.manager._resume_stalled_recruitment_calls(
                self.guild, datetime.now(timezone.utc),
            )
            # resumeが割り込んでも新しいタスクは立たない。
            self.assertEqual(len(self.manager._call_delivery_tasks), 1)

            gate.set()
            await self._wait_background_tasks()

        self.assertEqual(len(self.manager._call_delivery_tasks), 0)

    async def test_concurrent_delivery_on_same_call_id_does_not_double_send(self) -> None:
        """指摘2の直接再現: 同じ call_id へ2つの配信処理が並行して走っても
        (再起動直後の再開処理が、まだ終わっていない前回の配信と鉢合わせした
        状況を模擬) 各受信者への実送信は高々1回に抑えられる (at-most-once)。

        _active_call_ids によるプロセス内ガードを迂回し、DB側の
        claim_recruitment_call_delivery が最終防波堤として機能することを
        確かめる (10人へ0.2秒間隔で配信中にresumeを差し込むケースの再現)。
        """
        recruitment_id = await self._create_recruitment()
        member_ids = list(range(9100, 9110))  # 10人
        for uid in member_ids:
            self._add_member(uid)
            await database.set_user_notification_prefs(
                1, uid, allow_notifications=True, notify_on_call=True,
            )
        called_on = datetime.now(timezone.utc).astimezone().date().isoformat()
        call_id = await database.open_recruitment_call(recruitment_id, 1, 1, called_on)
        self.assertIsNotNone(call_id)
        embed = await self.manager.build_embed(self.guild, recruitment_id)
        recipients = [self.members[uid] for uid in member_ids]

        send_counts: dict[int, int] = {uid: 0 for uid in member_ids}

        async def paced_send(member, embed):
            # 実運用の0.2秒間隔を模した小さな遅延。この間に2つ目の配信処理が
            # 同じ受信者へ追いつけるかどうかが再現のポイント。実際にDMが
            # 何回「送られようとしたか」をここで直接数える。
            send_counts[member.id] += 1
            await asyncio.sleep(0.02)

        with patch.object(self.manager, "_send_call_dm_paced", paced_send):
            # 前回tickの配信 (まだ終わっていない) と、resumeが起動した2つ目の
            # 配信処理を、同じ call_id・同じ宛先リストに対して並行実行する。
            await asyncio.gather(
                self.manager._deliver_recruitment_call(
                    self.guild, call_id, called_on, embed, list(recipients),
                ),
                self.manager._deliver_recruitment_call(
                    self.guild, call_id, called_on, embed, list(recipients),
                ),
            )

        # 修正前はここが最大2 (二重送信) になっていた。at-most-onceなので
        # 全員ちょうど1回 (取りこぼしがあれば0だが、今回は宛先が競合しない
        # 限りどちらか一方のタスクが必ず拾うので1になるはず)。
        self.assertEqual(send_counts, {uid: 1 for uid in member_ids})

    async def test_claim_recruitment_call_delivery_is_exclusive(self) -> None:
        """claim_recruitment_call_delivery自体が「先着1人だけ確保できる」ことの直接テスト。"""
        recruitment_id = await self._create_recruitment()
        called_on = datetime.now(timezone.utc).astimezone().date().isoformat()
        call_id = await database.open_recruitment_call(recruitment_id, 1, 1, called_on)
        self.assertIsNotNone(call_id)

        results = await asyncio.gather(
            database.claim_recruitment_call_delivery(call_id, 42, "t1"),
            database.claim_recruitment_call_delivery(call_id, 42, "t2"),
        )
        self.assertEqual(sorted(results), [False, True])


if __name__ == "__main__":
    unittest.main()
