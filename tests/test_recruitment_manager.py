"""募集から既存ロビーへの移行と通知をDiscord APIなしで検証する。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import database
from config import Phase
from recruitment import (
    RecruitmentCardView,
    RecruitmentHomeView,
    RecruitmentManager,
    RecruitmentOptionsView,
    build_recruitment_help_embed,
)
from views import LobbyView


class RecruitmentManagerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-recruitment-manager-")
        self._old_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "manager.db")
        await database.init_db()

        self.members = {
            user_id: SimpleNamespace(
                id=user_id, nick=None, display_name=f"user-{user_id}",
                send=AsyncMock(),
            )
            for user_id in [1, *range(100, 113)]
        }
        self.guild = SimpleNamespace(
            id=1, get_member=lambda user_id: self.members.get(user_id),
        )
        self.state = SimpleNamespace(
            phase=Phase.LOBBY, players={}, gm_id=None, recruitment_id=None,
            room_name="総合", lobby_channel=None,
        )
        self.room = SimpleNamespace(
            state=self.state,
            room_def=SimpleNamespace(
                allowed_ranks=None, allowed_gm_user_ids=None, owner_only_gm=False,
            ),
            is_private_room=lambda: False,
            action_lock=asyncio.Lock(),
            _is_game_in_progress=lambda: False,
            validate_gm_claim=AsyncMock(return_value=None),
            _persist_room_state=AsyncMock(),
            _post_lobby_ui=AsyncMock(),
        )
        self.cog = SimpleNamespace(rooms={"open": self.room})
        self.manager = RecruitmentManager(SimpleNamespace(), self.cog)
        self.manager.channel = SimpleNamespace(guild=self.guild)
        self.manager.refresh_message = AsyncMock()
        self.manager.validate_candidate = AsyncMock(return_value=None)

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._old_path
        self._tmp.cleanup()

    async def _full_recruitment(self, *, gm_id=None) -> int:
        recruitment_id = await database.create_recruitment(
            1, 1, title="移行テスト",
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            room_id="open", streaming=False, allowed_ranks=None,
        )
        for user_id in range(100, 113):
            await database.add_recruitment_entry(recruitment_id, user_id)
        if gm_id is not None:
            await database.set_recruitment_gm(recruitment_id, gm_id)
        return recruitment_id

    def _interaction(self):
        return SimpleNamespace(
            user=self.members[1], guild=self.guild,
        )

    async def test_transfer_registers_thirteen_and_auto_host_gm(self) -> None:
        recruitment_id = await self._full_recruitment()
        result = await self.manager.transfer(self._interaction(), recruitment_id)
        self.assertIn("13人とGM", result)
        self.assertEqual(len(self.state.players), 13)
        self.assertEqual(self.state.gm_id, 1)
        self.assertEqual(self.state.recruitment_id, recruitment_id)
        start_button = next(
            item for item in LobbyView(self.room).children if item.custom_id == "start_game"
        )
        self.assertFalse(start_button.disabled)
        self.room._persist_room_state.assert_awaited_once()
        self.room._post_lobby_ui.assert_awaited_once()
        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(row["status"], database.RECRUITMENT_HELD)

    async def test_auto_host_gm_condition_failure_stops_before_lobby_change(self) -> None:
        recruitment_id = await self._full_recruitment()
        self.room.validate_gm_claim.return_value = "この卓のGM条件外です。"
        result = await self.manager.transfer(self._interaction(), recruitment_id)
        self.assertIn("GM user-1", result)
        self.assertEqual(self.state.players, {})
        self.assertIsNone(self.state.gm_id)
        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(row["status"], database.RECRUITMENT_OPEN)

    async def test_rank_recheck_failure_stops_before_lobby_change(self) -> None:
        recruitment_id = await self._full_recruitment()

        async def validate(_guild, _row, member):
            return "開催時ランク条件外" if member.id == 105 else None

        self.manager.validate_candidate.side_effect = validate
        result = await self.manager.transfer(self._interaction(), recruitment_id)
        self.assertIn("user-105", result)
        self.assertEqual(self.state.players, {})
        self.room._persist_room_state.assert_not_awaited()

    async def test_nonempty_lobby_requires_explicit_reset(self) -> None:
        recruitment_id = await self._full_recruitment()
        self.state.players[999] = object()
        result = await self.manager.transfer(self._interaction(), recruitment_id)
        self.assertEqual(result, "LOBBY_NOT_EMPTY")
        self.room._persist_room_state.assert_not_awaited()

        result = await self.manager.transfer(
            self._interaction(), recruitment_id, reset_lobby=True,
        )
        self.assertIn("13人とGM", result)
        self.assertNotIn(999, self.state.players)

    async def test_fly_in_lobby_entry_during_validation_is_not_silently_cleared(self) -> None:
        recruitment_id = await self._full_recruitment()
        injected = False

        async def validate(_guild, _row, _member):
            nonlocal injected
            if not injected:
                injected = True
                self.state.players[999] = object()
            return None

        self.manager.validate_candidate.side_effect = validate

        result = await self.manager.transfer(self._interaction(), recruitment_id)

        self.assertEqual(result, "LOBBY_NOT_EMPTY")
        self.assertIn(999, self.state.players)
        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(row["status"], database.RECRUITMENT_OPEN)

    async def test_transfer_can_retry_after_lobby_checkpoint_failure(self) -> None:
        recruitment_id = await self._full_recruitment()
        self.room._persist_room_state.side_effect = RuntimeError("DB down")

        with self.assertRaisesRegex(RuntimeError, "DB down"):
            await self.manager.transfer(self._interaction(), recruitment_id)

        self.assertEqual(self.state.players, {})
        self.assertIsNone(self.state.gm_id)
        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(row["status"], database.RECRUITMENT_HELD)

        self.room._persist_room_state.side_effect = None
        result = await self.manager.transfer(self._interaction(), recruitment_id)
        self.assertIn("13人とGM", result)
        self.assertEqual(len(self.state.players), 13)

    async def test_due_notification_is_sent_once_and_gm_warning_goes_to_host(self) -> None:
        recruitment_id = await database.create_recruitment(
            1, 1, title="通知テスト",
            scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            room_id="open", streaming=False, allowed_ranks=None,
        )
        await database.add_recruitment_entry(recruitment_id, 100)
        await self.manager.process_notifications(self.guild)
        self.members[100].send.assert_awaited_once()
        self.members[1].send.assert_awaited_once()
        await self.manager.process_notifications(self.guild)
        self.members[100].send.assert_awaited_once()
        self.members[1].send.assert_awaited_once()

    async def test_new_recruitment_is_archived_if_card_cannot_be_published(self) -> None:
        recruitment_id = await database.create_recruitment(
            1, 1, title="掲示失敗テスト",
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
            room_id="open", streaming=False, allowed_ranks=None,
        )
        denied = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden", headers={}), "denied"
        )
        self.manager.channel = SimpleNamespace(
            guild=self.guild,
            send=AsyncMock(side_effect=denied),
        )

        with self.assertRaisesRegex(RuntimeError, "作成を取り消しました"):
            await self.manager.publish_new_recruitment(self.guild, recruitment_id)

        row = await database.get_recruitment(recruitment_id)
        self.assertEqual(row["status"], database.RECRUITMENT_ARCHIVED)

    async def test_operations_block_notice_contains_no_mentions(self) -> None:
        channel = SimpleNamespace(send=AsyncMock())
        self.manager.operations_channel = channel
        await database.add_player_block(1, 100, 101)
        await self.manager.notify_block_added(self.guild, 100, 101, 1)
        text = channel.send.await_args.args[0]
        self.assertNotIn("<@", text)
        self.assertIn("ID: 100", text)
        self.assertIn("ID: 101", text)

    async def test_persistent_card_ids_and_open_only_options(self) -> None:
        home = RecruitmentHomeView(self.manager)
        self.assertIsNone(home.timeout)
        self.assertEqual(
            {item.custom_id for item in home.children},
            {"recruitment:create", "recruitment:help"},
        )
        help_embed = build_recruitment_help_embed()
        self.assertEqual(help_embed.title, "募集の使い方")
        self.assertEqual(len(help_embed.fields), 3)

        card = RecruitmentCardView(self.manager, 42)
        self.assertIsNone(card.timeout)
        self.assertEqual(
            {item.custom_id for item in card.children},
            {
                "recruitment:42:join", "recruitment:42:leave",
                "recruitment:42:gm", "recruitment:42:transfer",
                "recruitment:42:host",
            },
        )
        base = {
            "date": "2026-08-03", "hour": "20", "minute": "0",
            "streaming": "0",
        }
        restricted = RecruitmentOptionsView(
            self.manager, 1, {**base, "room": "beginner"},
        )
        self.assertFalse(any(isinstance(item, discord.ui.Select) for item in restricted.children))
        open_room = RecruitmentOptionsView(
            self.manager, 1, {**base, "room": "open"},
        )
        selects = [item for item in open_room.children if isinstance(item, discord.ui.Select)]
        self.assertEqual(len(selects), 1)
        self.assertEqual(selects[0].min_values, 0)
        self.assertEqual(selects[0].max_values, 10)
        self.assertEqual(len(selects[0].options), 10)
        self.assertEqual(selects[0].options[-1].value, "ランク未設定")

    async def test_open_room_uses_selected_rank_names_and_keeps_unranked_distinct(self) -> None:
        permissions = SimpleNamespace(administrator=False, manage_guild=False)
        member = SimpleNamespace(id=200, guild_permissions=permissions, roles=[])
        guild = SimpleNamespace(id=1, owner_id=999)
        row = {"room_id": "open", "allowed_ranks": frozenset({"ダイヤ"})}

        with patch(
            "database.get_player_current_rank_info",
            AsyncMock(return_value={"rank_name": "ダイヤ"}),
        ):
            self.assertIsNone(
                await RecruitmentManager.validate_candidate(self.manager, guild, row, member)
            )

        with patch(
            "database.get_player_current_rank_info",
            AsyncMock(return_value={"rank_name": "シルバー"}),
        ):
            error = await RecruitmentManager.validate_candidate(self.manager, guild, row, member)
            self.assertIn("シルバー", error)

        with patch("database.get_player_current_rank_info", AsyncMock(return_value=None)):
            error = await RecruitmentManager.validate_candidate(self.manager, guild, row, member)
            self.assertIn("ランク未設定", error)
            unranked_row = {
                "room_id": "open", "allowed_ranks": frozenset({"ランク未設定"}),
            }
            self.assertIsNone(
                await RecruitmentManager.validate_candidate(
                    self.manager, guild, unranked_row, member,
                )
            )


class ScheduleRangeTest(unittest.TestCase):
    """日付セレクトが提示する候補と、上限判定の範囲を一致させる。"""

    def test_last_offered_day_is_accepted_at_any_hour(self) -> None:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from config import RECRUITMENT_MAX_DAYS_AHEAD
        from recruitment import _schedule_out_of_range

        jst = ZoneInfo("Asia/Tokyo")
        now = datetime(2026, 8, 3, 10, 0, tzinfo=jst)
        # 日付セレクトは offset=0..MAX の日付を候補に出す
        last_day = (now + timedelta(days=RECRUITMENT_MAX_DAYS_AHEAD)).date()

        for hour in (0, 9, 10, 11, 23):
            with self.subTest(hour=hour):
                local_start = datetime(
                    last_day.year, last_day.month, last_day.day, hour, tzinfo=jst
                )
                self.assertFalse(
                    _schedule_out_of_range(local_start, now),
                    f"提示された最終日の{hour}時が拒否された",
                )

    def test_past_and_beyond_last_day_are_rejected(self) -> None:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        from config import RECRUITMENT_MAX_DAYS_AHEAD
        from recruitment import _schedule_out_of_range

        jst = ZoneInfo("Asia/Tokyo")
        now = datetime(2026, 8, 3, 10, 0, tzinfo=jst)

        # 過去・現在ちょうどは拒否
        self.assertTrue(_schedule_out_of_range(now, now))
        self.assertTrue(
            _schedule_out_of_range(now - timedelta(minutes=1), now)
        )
        # 候補外の翌日は拒否 (上限は日付単位でも、その先まで緩めない)
        beyond = now + timedelta(days=RECRUITMENT_MAX_DAYS_AHEAD + 1)
        self.assertTrue(_schedule_out_of_range(beyond, now))


if __name__ == "__main__":
    unittest.main()
