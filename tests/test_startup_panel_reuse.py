"""再起動しても常設パネルが新着メッセージを出さないことを検証する。

削除→再投稿だと、起動のたびに #統計 / #村作成 / #参加受付 へ新着が積まれ、
チャンネルに未読と通知が出る。保存したメッセージIDへ編集で当てる限り
Discordは通知を出さないので、その経路を固定する。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

import database
from game import GameCog
from room_runner import RoomRunner


class _Message:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.edit_calls: list[dict] = []

    async def edit(self, **kwargs):
        self.edit_calls.append(dict(kwargs))
        return self


class _PartialMessage:
    def __init__(self, message_id: int, *, missing: bool = False) -> None:
        self.id = message_id
        self.missing = missing
        self.edit_calls: list[dict] = []

    async def edit(self, **kwargs):
        if self.missing:
            raise discord.NotFound(SimpleNamespace(status=404, reason=""), "unknown")
        self.edit_calls.append(dict(kwargs))
        return _Message(self.id)


class _Channel:
    def __init__(self, guild, *, partial: _PartialMessage | None = None) -> None:
        self.guild = guild
        self.partial = partial
        self.sent: list[dict] = []
        self.purge_calls = 0

    def get_partial_message(self, message_id: int) -> _PartialMessage:
        assert self.partial is not None
        assert self.partial.id == message_id
        return self.partial

    async def purge(self, **_kwargs):
        self.purge_calls += 1
        return []

    async def send(self, **kwargs):
        self.sent.append(dict(kwargs))
        return _Message(4242)


class StartupPanelReuseTest(unittest.IsolatedAsyncioTestCase):
    def _manager(self) -> GameCog:
        manager = GameCog(SimpleNamespace(managed_guild_id=1))
        manager.bot = SimpleNamespace()
        return manager

    def _channel(self, *, partial: _PartialMessage | None = None) -> _Channel:
        return _Channel(SimpleNamespace(id=1), partial=partial)

    async def test_stored_panel_is_edited_without_new_message(self) -> None:
        partial = _PartialMessage(555)
        channel = self._channel(partial=partial)
        manager = self._manager()
        set_meta = AsyncMock()

        with patch.object(
            database, "get_meta", AsyncMock(return_value="555")
        ), patch.object(database, "set_meta", set_meta):
            message = await manager._upsert_startup_panel(
                channel,
                "stats_panel_message_id",
                embed=discord.Embed(title="統計"),
                view=discord.ui.View(timeout=None),
                label="統計",
            )

        self.assertEqual(message.id, 555)
        self.assertEqual(len(partial.edit_calls), 1)
        # 新着を出さないので、送信も掃除も走らない。
        self.assertEqual(channel.sent, [])
        self.assertEqual(channel.purge_calls, 0)
        set_meta.assert_not_awaited()

    async def test_missing_panel_falls_back_to_repost(self) -> None:
        partial = _PartialMessage(555, missing=True)
        channel = self._channel(partial=partial)
        manager = self._manager()
        set_meta = AsyncMock()

        with patch.object(
            database, "get_meta", AsyncMock(return_value="555")
        ), patch.object(database, "set_meta", set_meta):
            message = await manager._upsert_startup_panel(
                channel,
                "stats_panel_message_id",
                embed=discord.Embed(title="統計"),
                view=discord.ui.View(timeout=None),
                label="統計",
            )

        self.assertEqual(message.id, 4242)
        self.assertEqual(channel.purge_calls, 1)
        self.assertEqual(len(channel.sent), 1)
        # 次の起動から再利用できるよう、投稿したIDを覚える。
        set_meta.assert_awaited_once_with(1, "stats_panel_message_id", "4242")

    async def test_first_run_without_stored_id_posts_once(self) -> None:
        channel = self._channel()
        manager = self._manager()
        set_meta = AsyncMock()

        with patch.object(
            database, "get_meta", AsyncMock(return_value=None)
        ), patch.object(database, "set_meta", set_meta):
            await manager._upsert_startup_panel(
                channel,
                "gm_hub_panel_message_id",
                embed=discord.Embed(title="村作成"),
                view=discord.ui.View(timeout=None),
                label="村作成",
            )

        self.assertEqual(len(channel.sent), 1)
        set_meta.assert_awaited_once_with(1, "gm_hub_panel_message_id", "4242")


class LobbyPanelReuseTest(unittest.IsolatedAsyncioTestCase):
    """#参加受付 のロビーパネルも起動時は同じメッセージへ戻す。"""

    def _runner(self, channel) -> RoomRunner:
        runner = RoomRunner.__new__(RoomRunner)
        runner.bot = SimpleNamespace()
        runner.manager = SimpleNamespace(_startup_in_progress=False)
        runner.state = SimpleNamespace(
            room_id="nate",
            room_name="ねいとくん村",
            lobby_channel=channel,
            lobby_message=None,
        )
        runner._purge_bot_messages = AsyncMock()
        return runner

    def _channel(self, *, partial: _PartialMessage | None = None) -> _Channel:
        return _Channel(SimpleNamespace(id=1), partial=partial)

    async def test_startup_edits_stored_lobby_panel(self) -> None:
        partial = _PartialMessage(777)
        channel = self._channel(partial=partial)
        runner = self._runner(channel)

        with patch.object(
            database, "get_meta", AsyncMock(return_value="777")
        ), patch.object(database, "set_meta", AsyncMock()), patch(
            "room_runner.LobbyView"
        ) as lobby_view:
            lobby_view.return_value = SimpleNamespace(
                _build_embed=lambda: discord.Embed(title="参加受付")
            )
            await runner._post_lobby_ui(reuse_existing=True)

        self.assertEqual(runner.state.lobby_message.id, 777)
        self.assertEqual(channel.sent, [])
        runner._purge_bot_messages.assert_not_awaited()

    async def test_startup_reposts_when_panel_is_gone(self) -> None:
        partial = _PartialMessage(777, missing=True)
        channel = self._channel(partial=partial)
        runner = self._runner(channel)
        set_meta = AsyncMock()

        with patch.object(
            database, "get_meta", AsyncMock(return_value="777")
        ), patch.object(database, "set_meta", set_meta), patch(
            "room_runner.LobbyView"
        ) as lobby_view:
            lobby_view.return_value = SimpleNamespace(
                _build_embed=lambda: discord.Embed(title="参加受付")
            )
            await runner._post_lobby_ui(reuse_existing=True)

        runner._purge_bot_messages.assert_awaited_once()
        self.assertEqual(len(channel.sent), 1)
        set_meta.assert_awaited_once_with(1, "lobby_panel_message_id:nate", "4242")

    async def test_startup_flag_covers_restore_and_recruitment_reposts(self) -> None:
        """起動中は、復元・募集復旧からの再掲示も編集で済ませる。

        ロビーの掲示は setup_channels だけでなく restore_from_snapshot や
        募集カードの復旧からも走る。呼び出し口の指定漏れで新着が出ないよう、
        起動中フラグだけで再利用に入ることを固定する。
        """
        partial = _PartialMessage(777)
        channel = self._channel(partial=partial)
        runner = self._runner(channel)
        runner.manager = SimpleNamespace(_startup_in_progress=True)

        with patch.object(
            database, "get_meta", AsyncMock(return_value="777")
        ), patch.object(database, "set_meta", AsyncMock()), patch(
            "room_runner.LobbyView"
        ) as lobby_view:
            lobby_view.return_value = SimpleNamespace(
                _build_embed=lambda: discord.Embed(title="参加受付")
            )
            await runner._post_lobby_ui()

        self.assertEqual(runner.state.lobby_message.id, 777)
        self.assertEqual(channel.sent, [])
        runner._purge_bot_messages.assert_not_awaited()

    async def test_runtime_repost_still_posts_at_the_bottom(self) -> None:
        """ゲーム終了後などの再掲示は、末尾に出す従来動作のまま。"""
        partial = _PartialMessage(777)
        channel = self._channel(partial=partial)
        runner = self._runner(channel)

        with patch.object(
            database, "get_meta", AsyncMock(return_value="777")
        ), patch.object(database, "set_meta", AsyncMock()), patch(
            "room_runner.LobbyView"
        ) as lobby_view:
            lobby_view.return_value = SimpleNamespace(
                _build_embed=lambda: discord.Embed(title="参加受付")
            )
            await runner._post_lobby_ui()

        runner._purge_bot_messages.assert_awaited_once()
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(partial.edit_calls, [])


if __name__ == "__main__":
    unittest.main()
