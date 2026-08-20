"""[レート推移] ボタンが本人の統計を見ているときだけ出ることを検証する (親レビュー指摘3)。

CompatibilityButton (相性) は既に「本人が自分の統計を見ているときだけ」に
揃っている。RatingChartButton だけ font_available() のみで生えていたため、
`#統計` のユーザー選択 (誰の統計でも開ける) から他人のレート推移を
引けてしまっていた。_rebuild の条件と、押下時のコールバック側の両方を
CompatibilityButton と同じ形に揃える。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import stats_image
import views


def _make_interaction(guild, user):
    return SimpleNamespace(
        guild=guild,
        user=user,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class RatingChartButtonVisibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cog = SimpleNamespace()
        self.target_user = SimpleNamespace(id=100, display_name="対象ユーザー")

    def test_button_is_hidden_when_viewing_someone_else(self) -> None:
        """#統計のユーザー選択で他人を開いたときは [レート推移] を出さない。"""
        with patch.object(stats_image, "font_available", return_value=True):
            view = views.PlayerStatsVariantView(
                self.cog, guild_id=1, user=self.target_user, viewer_id=999,
            )
        labels = [item.label for item in view.children if hasattr(item, "label")]
        self.assertNotIn("レート推移", labels)
        self.assertNotIn("相性", labels)

    def test_button_is_shown_when_viewing_own_stats(self) -> None:
        with patch.object(stats_image, "font_available", return_value=True):
            view = views.PlayerStatsVariantView(
                self.cog, guild_id=1, user=self.target_user, viewer_id=100,
            )
        labels = [item.label for item in view.children if hasattr(item, "label")]
        self.assertIn("レート推移", labels)
        self.assertIn("相性", labels)

    def test_button_stays_hidden_without_font_even_for_self(self) -> None:
        """フォントが無い環境では、本人が自分を見ていても出さない (既存仕様)。"""
        with patch.object(stats_image, "font_available", return_value=False):
            view = views.PlayerStatsVariantView(
                self.cog, guild_id=1, user=self.target_user, viewer_id=100,
            )
        labels = [item.label for item in view.children if hasattr(item, "label")]
        self.assertNotIn("レート推移", labels)


class RatingChartButtonCallbackAuthorizationTest(unittest.IsolatedAsyncioTestCase):
    """Viewが他人へ渡らない前提(ephemeral)だけに頼らず、押下時にも本人確認する。"""

    async def test_callback_rejects_non_viewer(self) -> None:
        target_user = SimpleNamespace(id=100, display_name="対象ユーザー")
        with patch.object(stats_image, "font_available", return_value=True):
            view = views.PlayerStatsVariantView(
                SimpleNamespace(), guild_id=1, user=target_user, viewer_id=100,
            )
        button = next(
            item for item in view.children if getattr(item, "label", None) == "レート推移"
        )
        guild = SimpleNamespace(id=1)
        impostor = SimpleNamespace(id=999)
        interaction = _make_interaction(guild, impostor)

        await button.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            "本人だけが見られます。", ephemeral=True,
        )
        interaction.response.defer.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()
