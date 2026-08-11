"""起動時の失敗が、他機能や進行中ゲームを巻き添えにしないことを検証する。"""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# CIには本番用.envもトークンも無い。起動失敗通知の関数だけを検証するため、
# import中だけ無害なテスト値を渡し、Botの起動前検査そのものは変更しない。
with patch.dict(os.environ, {"DISCORD_TOKEN": "unit-test-token"}):
    import bot as bot_module
from config import CH_OPERATIONS, OPERATIONS_CATEGORY_NAME
from game import GameCog


def _private_room_row(owner_id: int = 10) -> dict:
    return {
        "owner_id": owner_id,
        "room_id": f"private_{owner_id}",
        "room_name": "テスト専用村",
    }


class PrivateRoomCleanupTest(unittest.IsolatedAsyncioTestCase):
    """作成者ロールを失った専用村の削除タイミングを確認する。"""

    def _manager(self) -> GameCog:
        manager = GameCog(SimpleNamespace(managed_guild_id=1))
        manager.bulk_api_interval = 0
        manager._delete_private_room_by_row = AsyncMock()
        return manager

    def _guild(self, owner) -> SimpleNamespace:
        return SimpleNamespace(
            id=1,
            get_member=lambda member_id: owner if member_id == owner.id else None,
        )

    def _roleless_owner(self, owner_id: int = 10) -> SimpleNamespace:
        return SimpleNamespace(
            id=owner_id, roles=[SimpleNamespace(name="一般参加者")]
        )

    async def test_idle_private_room_is_deleted_immediately(self) -> None:
        manager = self._manager()
        row = _private_room_row()
        guild = self._guild(self._roleless_owner())

        with patch("game.database.load_private_rooms", AsyncMock(return_value=[row])):
            deferred = await manager._cleanup_private_rooms_without_creator_role(guild)

        manager._delete_private_room_by_row.assert_awaited_once()
        self.assertEqual(deferred, set())

    async def test_hidden_variant_private_room_is_quarantined_before_runner_creation(self) -> None:
        manager = self._manager()
        row = {
            **_private_room_row(),
            "status": "active",
            "role_name": "テスト専用村",
            "variant_id": "v13_turn",
        }
        guild = SimpleNamespace(id=1)

        with (
            patch(
                "game.database.load_private_rooms",
                AsyncMock(return_value=[row]),
            ),
            patch(
                "game.database.mark_private_room_status",
                AsyncMock(),
            ) as quarantine,
        ):
            await manager._load_private_room_runners(guild)

        self.assertNotIn(row["room_id"], manager.rooms)
        quarantine.assert_awaited_once()
        self.assertEqual(quarantine.await_args.args[:3], (1, row["room_id"], "error"))
        self.assertIn("非公開", quarantine.await_args.kwargs["error"])

    async def test_in_progress_private_room_deletion_is_deferred(self) -> None:
        """RoomRunner復元前に消すとforce_endを通らないため、後回しにする。"""
        manager = self._manager()
        row = _private_room_row()
        guild = self._guild(self._roleless_owner())

        with patch("game.database.load_private_rooms", AsyncMock(return_value=[row])):
            deferred = await manager._cleanup_private_rooms_without_creator_role(
                guild, defer_room_ids=frozenset({row["room_id"]})
            )

        manager._delete_private_room_by_row.assert_not_awaited()
        self.assertEqual(deferred, {row["room_id"]})

    async def test_owner_with_creator_role_is_never_deferred_or_deleted(self) -> None:
        manager = self._manager()
        row = _private_room_row()
        owner = SimpleNamespace(id=10, roles=[SimpleNamespace(name="GM")])
        guild = self._guild(owner)

        with patch("game.database.load_private_rooms", AsyncMock(return_value=[row])):
            deferred = await manager._cleanup_private_rooms_without_creator_role(
                guild, defer_room_ids=frozenset({row["room_id"]})
            )

        manager._delete_private_room_by_row.assert_not_awaited()
        self.assertEqual(deferred, set())


class StartupFailureNoticeTest(unittest.IsolatedAsyncioTestCase):
    """起動失敗がログだけで終わらず、#運営 に残ることを確認する。"""

    def _guild(self, *, with_channel: bool = True) -> SimpleNamespace:
        category = SimpleNamespace(name=OPERATIONS_CATEGORY_NAME)
        channel = SimpleNamespace(
            name=CH_OPERATIONS, category=category, send=AsyncMock()
        )
        return SimpleNamespace(
            categories=[category],
            text_channels=[channel] if with_channel else [],
        )

    async def test_failure_is_posted_to_operations_channel(self) -> None:
        guild = self._guild()
        channel = guild.text_channels[0]

        await bot_module._notify_startup_failure(
            guild,
            RuntimeError("卓の復元に失敗"),
            verified_operations_channel=channel,
        )

        channel.send.assert_awaited_once()
        embed = channel.send.await_args.kwargs["embed"]
        self.assertIn("起動に失敗", embed.title)
        self.assertIn("卓の復元に失敗", embed.fields[0].value)
        self.assertIn("RuntimeError", embed.fields[0].value)

    async def test_missing_operations_channel_is_not_an_error(self) -> None:
        guild = self._guild(with_channel=False)

        await bot_module._notify_startup_failure(guild, RuntimeError("boom"))

    async def test_unverified_named_channel_is_not_used(self) -> None:
        guild = self._guild()

        await bot_module._notify_startup_failure(guild, RuntimeError("boom"))

        guild.text_channels[0].send.assert_not_awaited()

    async def test_notification_failure_never_masks_the_original_error(self) -> None:
        guild = self._guild()
        guild.text_channels[0].send = AsyncMock(side_effect=RuntimeError("送信不可"))

        await bot_module._notify_startup_failure(
            guild,
            RuntimeError("boom"),
            verified_operations_channel=guild.text_channels[0],
        )


if __name__ == "__main__":
    unittest.main()
