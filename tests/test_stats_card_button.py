"""StatsView の「画像で見る」ボタン (戦績カード画像・実装仕様 §4-3) を検証する。

- フォントが無い環境でボタンを押しても例外にならず案内を返す
- 生成できたときはPNGファイルをephemeralで返す
- 連打はクールダウンで弾かれる
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
import stats_image
import views


def _make_interaction(guild, user):
    return SimpleNamespace(
        guild=guild,
        user=user,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class StatsCardButtonTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-stats-card-")
        self._old_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "stats_card.db")
        await database.init_db()

        # モジュール共有のクールダウン状態はテスト間で引き継がせない。
        views._stats_card_cooldown_until.clear()

        self.guild = SimpleNamespace(id=1)
        self.user = SimpleNamespace(
            id=100,
            display_name="テストユーザー",
            display_avatar=SimpleNamespace(read=AsyncMock(return_value=None)),
        )

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._old_path
        self._tmp.cleanup()
        views._stats_card_cooldown_until.clear()

    # ボタンの既定OFF自体が仕様(シーズン1開始前は閉じておく)なので、
    # ボタンの中身を検証するテストは有効化した状態で行う。
    # 既定OFFの検証は別クラス (StatsCardButtonVisibilityTest) で行う。

    async def test_font_unavailable_shows_guidance_without_raising(self) -> None:
        with patch.object(stats_image, "font_available", return_value=False), \
                patch.object(views, "STATS_CARD_BUTTON_ENABLED", True):
            view = views.StatsView(SimpleNamespace())
            interaction = _make_interaction(self.guild, self.user)
            await view.stats_card_image.callback(interaction)

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        args, _kwargs = interaction.followup.send.call_args
        self.assertIn("利用できません", args[0])

    async def test_zero_games_player_gets_png_file(self) -> None:
        with patch.object(stats_image, "font_available", return_value=True), \
                patch.object(views, "STATS_CARD_BUTTON_ENABLED", True):
            view = views.StatsView(SimpleNamespace())
            interaction = _make_interaction(self.guild, self.user)
            await view.stats_card_image.callback(interaction)

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        _, kwargs = interaction.followup.send.call_args
        sent_file = kwargs.get("file")
        self.assertIsNotNone(sent_file)

    async def test_second_press_within_cooldown_is_rejected(self) -> None:
        with patch.object(stats_image, "font_available", return_value=True), \
                patch.object(views, "STATS_CARD_BUTTON_ENABLED", True):
            view = views.StatsView(SimpleNamespace())
            first = _make_interaction(self.guild, self.user)
            await view.stats_card_image.callback(first)

            second = _make_interaction(self.guild, self.user)
            await view.stats_card_image.callback(second)

        # 2回目はDB照会に進まず、即座に案内だけ返す。
        second.response.defer.assert_not_awaited()
        second.response.send_message.assert_awaited_once()
        second.followup.send.assert_not_awaited()


class StatsCardButtonVisibilityTest(unittest.TestCase):
    """STATS_CARD_BUTTON_ENABLED によるボタンの出し分け (既定OFF)。"""

    def test_button_hidden_by_default(self) -> None:
        with patch.object(stats_image, "font_available", return_value=True), \
                patch.object(views, "STATS_CARD_BUTTON_ENABLED", False):
            view = views.StatsView(SimpleNamespace())

        item = discord_utils_get(view, "stats_card_image")
        self.assertIsNone(item)

    def test_button_shown_when_enabled_and_font_available(self) -> None:
        with patch.object(stats_image, "font_available", return_value=True), \
                patch.object(views, "STATS_CARD_BUTTON_ENABLED", True):
            view = views.StatsView(SimpleNamespace())

        item = discord_utils_get(view, "stats_card_image")
        self.assertIsNotNone(item)

    def test_button_hidden_when_enabled_but_font_unavailable(self) -> None:
        with patch.object(stats_image, "font_available", return_value=False), \
                patch.object(views, "STATS_CARD_BUTTON_ENABLED", True):
            view = views.StatsView(SimpleNamespace())

        item = discord_utils_get(view, "stats_card_image")
        self.assertIsNone(item)


def discord_utils_get(view, custom_id: str):
    import discord
    return discord.utils.get(view.children, custom_id=custom_id)


if __name__ == "__main__":
    unittest.main()
