"""複数待機登録と、実ゲーム1卓制限の共通判定を検証する。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from config import Phase
from game import GameCog


def _room(
    room_id: str,
    phase: Phase,
    *,
    players: set[int] | None = None,
    gm_id: int | None = None,
    ending: bool = False,
):
    return SimpleNamespace(
        state=SimpleNamespace(
            room_id=room_id,
            room_name=room_id,
            phase=phase,
            players={user_id: object() for user_id in players or set()},
            gm_id=gm_id,
            ending=ending,
        )
    )


class ActiveUserRoomTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cog = GameCog.__new__(GameCog)

    def test_waiting_registration_does_not_hide_a_later_active_room(self) -> None:
        lobby = _room("lobby", Phase.LOBBY, players={10})
        active = _room("active", Phase.DAY_DISCUSSION, players={10})
        self.cog.rooms = {"lobby": lobby, "active": active}

        self.assertIs(self.cog.find_active_user_room(10), active)
        self.assertIsNone(
            self.cog.find_active_user_room(10, exclude_room_id="active")
        )

    def test_game_over_is_active_only_while_cleanup_is_running(self) -> None:
        finished = _room("finished", Phase.GAME_OVER, gm_id=20, ending=False)
        cleanup = _room("cleanup", Phase.GAME_OVER, players={30}, ending=True)
        self.cog.rooms = {"finished": finished, "cleanup": cleanup}

        self.assertIsNone(self.cog.find_active_user_room(20))
        self.assertIs(self.cog.find_active_user_room(30), cleanup)
        self.assertIsNone(
            self.cog.find_active_user_room(
                30, include_ending_cleanup=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
