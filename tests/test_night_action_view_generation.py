"""占い・護衛のephemeral UIを開いた夜だけに固定する。"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from config import Phase, Role
from tests.test_village_panel_role_actions import (
    add_player,
    make_interaction,
    make_runner,
)
from views import (
    VillageGuardConfirmView,
    VillageGuardTargetView,
    VillageSeerConfirmView,
    VillageSeerTargetView,
)


class NightActionViewGenerationTest(unittest.IsolatedAsyncioTestCase):
    async def test_old_seer_confirmation_cannot_commit_in_next_night(self) -> None:
        runner = make_runner(phase=Phase.NIGHT)
        seer = add_player(runner, 1, Role.SEER)
        target = add_player(runner, 2)
        runner.state.night_generation = 1
        runner.commit_seer_target = AsyncMock(return_value=("ok", True))
        view = VillageSeerConfirmView(
            runner, seer.user_id, target.user_id, target.display_name,
        )
        runner.state.night_generation = 2
        interaction = make_interaction(seer.member)

        await view.confirm_btn.callback(interaction)

        runner.commit_seer_target.assert_not_awaited()
        self.assertIn(
            "この夜", interaction.response.send_message.await_args.args[0]
        )

    async def test_old_seer_target_picker_cannot_open_next_night_confirmation(self) -> None:
        runner = make_runner(phase=Phase.NIGHT)
        seer = add_player(runner, 1, Role.SEER)
        target = add_player(runner, 2)
        runner.state.night_generation = 1
        view = VillageSeerTargetView(runner, seer.user_id, [target])
        runner.state.night_generation = 2
        interaction = make_interaction(seer.member)

        await view.children[0].callback(interaction)

        interaction.response.edit_message.assert_not_awaited()
        self.assertIn(
            "この夜", interaction.response.send_message.await_args.args[0]
        )

    async def test_old_guard_confirmation_cannot_commit_in_next_night(self) -> None:
        runner = make_runner(phase=Phase.NIGHT)
        guard = add_player(runner, 1, Role.GUARD)
        target = add_player(runner, 2)
        runner.state.night_generation = 1
        runner.commit_guard_target = AsyncMock(return_value=("ok", True))
        view = VillageGuardConfirmView(
            runner, guard.user_id, target.user_id, target.display_name,
        )
        runner.state.night_generation = 2
        interaction = make_interaction(guard.member)

        await view.confirm_btn.callback(interaction)

        runner.commit_guard_target.assert_not_awaited()
        self.assertIn(
            "この夜", interaction.response.send_message.await_args.args[0]
        )

    async def test_old_guard_target_picker_cannot_open_next_night_confirmation(self) -> None:
        runner = make_runner(phase=Phase.NIGHT)
        guard = add_player(runner, 1, Role.GUARD)
        target = add_player(runner, 2)
        runner.state.night_generation = 1
        view = VillageGuardTargetView(runner, guard.user_id, [target])
        runner.state.night_generation = 2
        interaction = make_interaction(guard.member)

        await view.children[0].callback(interaction)

        interaction.response.edit_message.assert_not_awaited()
        self.assertIn(
            "この夜", interaction.response.send_message.await_args.args[0]
        )


if __name__ == "__main__":
    unittest.main()
