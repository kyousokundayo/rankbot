"""常設パネルの段数と入口を守る回帰テスト。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from config import Phase
from views import (
    DangerConfirmView,
    GMControlView,
    GMPanelEntryView,
    LobbyView,
    StatsView,
)


def _assert_within_three_rows(test: unittest.TestCase, view) -> None:
    rows: dict[int, int] = {}
    for item in view.children:
        test.assertIsNotNone(item.row)
        rows[item.row] = rows.get(item.row, 0) + 1
    test.assertLessEqual(len(rows), 3)
    test.assertLessEqual(max(rows, default=0), 2)
    test.assertTrue(all(item_count <= 5 for item_count in rows.values()))


class UsabilityViewLayoutTest(unittest.TestCase):
    @staticmethod
    def _lobby_cog(*, private: bool):
        state = SimpleNamespace(
            phase=Phase.LOBBY,
            players={},
            gm_id=None,
            room_id="private_1" if private else "beginner",
        )
        return SimpleNamespace(state=state, is_private_room=lambda: private)

    def test_lobby_panel_stays_within_three_rows(self) -> None:
        for private in (False, True):
            with self.subTest(private=private):
                view = LobbyView(self._lobby_cog(private=private))
                _assert_within_three_rows(self, view)
                self.assertIsNotNone(
                    next(
                        item
                        for item in view.children
                        if item.custom_id == "lobby_gm_menu"
                    )
                )

    def test_stats_panel_stays_within_three_rows(self) -> None:
        view = StatsView(SimpleNamespace())

        _assert_within_three_rows(self, view)
        self.assertIsNotNone(
            next(item for item in view.children if item.custom_id == "feedback_report")
        )

    def test_public_gm_panel_is_one_button_and_private_menu_is_two_rows(self) -> None:
        state = SimpleNamespace(
            game_run_id="run-1",
            phase=Phase.DAY_DISCUSSION,
            phase_before_pause=None,
            paused=False,
            ending=False,
            pending_winner=None,
        )
        cog = SimpleNamespace(state=state, register_game_view=lambda _view: None)

        entry = GMPanelEntryView(cog)
        menu = GMControlView(cog)

        self.assertEqual(len(entry.children), 1)
        _assert_within_three_rows(self, menu)
        self.assertEqual({item.row for item in menu.children}, {0, 1})


class DangerConfirmationTest(unittest.IsolatedAsyncioTestCase):
    async def test_action_runs_only_after_same_user_confirms(self) -> None:
        action = AsyncMock()
        view = DangerConfirmView(1, action)
        wrong_user = SimpleNamespace(
            user=SimpleNamespace(id=2),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        confirm = next(item for item in view.children if item.label == "実行する")

        await confirm.callback(wrong_user)

        action.assert_not_awaited()
        wrong_user.response.send_message.assert_awaited_once()

        actor = SimpleNamespace(
            user=SimpleNamespace(id=1),
            response=SimpleNamespace(defer=AsyncMock()),
        )
        await confirm.callback(actor)

        actor.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        action.assert_awaited_once_with(actor)


if __name__ == "__main__":
    unittest.main()
