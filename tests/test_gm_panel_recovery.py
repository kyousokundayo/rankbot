"""再起動後の古いGM操作入口を追跡して増殖させない。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from config import Phase
from tests.test_village_panel_role_actions import make_runner


class GmPanelRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_saved_old_panel_is_deleted_when_reposted(self) -> None:
        runner = make_runner(phase=Phase.DAY_DISCUSSION)
        old_message = SimpleNamespace(delete=AsyncMock())
        new_message = SimpleNamespace(id=202)
        channel = SimpleNamespace(
            id=303,
            send=AsyncMock(return_value=new_message),
            get_partial_message=MagicMock(return_value=old_message),
        )
        runner.state.village_channel = channel
        runner.state.gm_panel_message = None
        runner.state.gm_panel_message_id = 101
        runner._persist_room_state = AsyncMock()

        posted = await runner._repost_gm_panel()

        self.assertTrue(posted)
        channel.get_partial_message.assert_called_once_with(101)
        old_message.delete.assert_awaited_once()
        self.assertEqual(runner.state.gm_panel_message_id, 202)
        runner._persist_room_state.assert_awaited_once()

    def test_gm_panel_id_is_part_of_durable_snapshot(self) -> None:
        runner = make_runner(phase=Phase.NIGHT)
        runner.state.gm_panel_message_id = 404

        payload = runner._build_room_snapshot()

        self.assertEqual(payload["gm_panel_message_id"], 404)


if __name__ == "__main__":
    unittest.main()
