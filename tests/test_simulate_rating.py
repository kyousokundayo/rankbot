"""simulate_rating入力検証の回帰テスト。"""
from __future__ import annotations

import unittest

import config
import simulate_rating


class TestSimulationValidation(unittest.TestCase):
    def base_args(self):
        return {
            "players": config.MAX_PLAYERS,
            "games_per_day": 0,
            "season_days": 1,
            "seasons": 1,
            "village_winrate": 0.45,
            "skill_effect": 0.8,
            "seed": 1,
            "quiet": True,
        }

    def test_rejects_too_few_players(self):
        args = self.base_args()
        args["players"] = config.MAX_PLAYERS - 1
        with self.assertRaisesRegex(ValueError, "players must be at least"):
            simulate_rating.simulate(**args)

    def test_rejects_invalid_village_winrate(self):
        for invalid in (0.0, 1.0):
            with self.subTest(invalid=invalid):
                args = self.base_args()
                args["village_winrate"] = invalid
                with self.assertRaisesRegex(ValueError, "village_winrate"):
                    simulate_rating.simulate(**args)

    def test_rejects_negative_games_per_day(self):
        args = self.base_args()
        args["games_per_day"] = -1
        with self.assertRaisesRegex(ValueError, "games_per_day"):
            simulate_rating.simulate(**args)


if __name__ == "__main__":
    unittest.main()
