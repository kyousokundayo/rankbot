"""変種定義とシミュレーション計画の回帰テスト。"""
from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path
from random import Random
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import rating
import simulate_games
from game import GameCog
from models import GameState
from views import build_rule_embeds


EXPECTED_VARIANT_IDS = (
    "v13_cross",
    "v13_turn",
    "v9_cross",
    "v9_turn",
)


class VariantDefinitionTest(unittest.TestCase):
    def test_all_variant_definitions_match_the_public_contract(self) -> None:
        self.assertEqual(tuple(config.VARIANT_DEFINITIONS), EXPECTED_VARIANT_IDS)
        self.assertEqual(config.DEFAULT_VARIANT_ID, "v13_cross")
        self.assertEqual(config.DEFAULT_LADDER_ID, "l13")
        self.assertEqual(
            config.VARIANT_TO_LADDER,
            {
                "v13_cross": "l13",
                "v13_turn": "l13",
                "v9_cross": "l9",
                "v9_turn": "l9",
            },
        )

        expected = {
            "v13_cross": (13, "crosstalk", 180, 120, 3, 6, (), 90, 0, (480, 60, 180)),
            "v13_turn": (13, "turn", 180, 120, 3, 6, (50, 80, 90), 150, 2, ()),
            "v9_cross": (9, "crosstalk", 90, 110, 2, 4, (), 90, 0, (420, 60, 120)),
            "v9_turn": (9, "turn", 90, 110, 2, 4, (50, 80, 90), 90, 1, ()),
        }
        for variant_id, values in expected.items():
            variant = config.VARIANT_DEFINITIONS[variant_id]
            self.assertEqual(
                (
                    variant.player_count,
                    variant.discussion_mode,
                    variant.village_win_pool,
                    variant.wolf_win_pool,
                    variant.wolf_guess_slots,
                    variant.final_day_threshold,
                    variant.turn_round_seconds,
                    variant.recruitment_occupancy_minutes,
                    variant.turn_interrupts_per_day,
                    variant.crosstalk_discussion_seconds,
                ),
                values,
            )
            self.assertEqual(sum(variant.role_distribution.values()), variant.player_count)

    def test_role_distributions_are_exact(self) -> None:
        expected_13 = {
            config.Role.WEREWOLF: 3,
            config.Role.MADMAN: 1,
            config.Role.SEER: 1,
            config.Role.MEDIUM: 1,
            config.Role.GUARD: 1,
            config.Role.VILLAGER: 6,
        }
        expected_9 = {
            config.Role.WEREWOLF: 2,
            config.Role.MADMAN: 1,
            config.Role.SEER: 1,
            config.Role.MEDIUM: 1,
            config.Role.GUARD: 1,
            config.Role.VILLAGER: 3,
        }
        self.assertEqual(dict(config.VARIANT_DEFINITIONS["v13_cross"].role_distribution), expected_13)
        self.assertEqual(dict(config.VARIANT_DEFINITIONS["v13_turn"].role_distribution), expected_13)
        self.assertEqual(dict(config.VARIANT_DEFINITIONS["v9_cross"].role_distribution), expected_9)
        self.assertEqual(dict(config.VARIANT_DEFINITIONS["v9_turn"].role_distribution), expected_9)

    def test_crosstalk_discussion_time_and_rule_embed_follow_variant(self) -> None:
        expected = {
            "v13_cross": (
                (480, 60, 180),
                (480, 420, 360, 300, 240, 180, 180),
                "議論 **初日8分 / 毎日1分短縮 / 最低3分**",
            ),
            "v9_cross": (
                (420, 60, 120),
                (420, 360, 300, 240, 180, 120, 120),
                "議論 **初日7分 / 毎日1分短縮 / 最低2分**",
            ),
        }
        state = GameState()
        for variant_id, (timing, days, rule_text) in expected.items():
            with self.subTest(variant_id=variant_id):
                variant = config.VARIANT_DEFINITIONS[variant_id]
                self.assertEqual(variant.crosstalk_discussion_seconds, timing)
                actual_days = []
                for day_number in range(1, 8):
                    state.day_number = day_number
                    actual_days.append(state.get_day_discussion_time(timing))
                self.assertEqual(
                    tuple(actual_days),
                    days,
                )
                embed = build_rule_embeds(variant)[0]
                self.assertIn(
                    rule_text,
                    "\n".join(field.value for field in embed.fields),
                )

    def test_room_activation_keeps_disabled_definition_but_hides_live_surface(self) -> None:
        disabled = config.ROOM_DEFINITION_MAP["open_13_turn"]
        self.assertFalse(disabled.enabled)
        self.assertIn(disabled, config.ROOM_DEFINITIONS)
        self.assertNotIn(disabled, config.ACTIVE_ROOM_DEFINITIONS)
        self.assertNotIn(disabled.room_id, config.ACTIVE_ROOM_IDS)
        self.assertNotIn(disabled.room_id, config.OPEN_ROOM_IDS)
        self.assertNotIn(disabled.room_id, config.PUBLIC_ROOM_IDS)
        self.assertNotIn(disabled.room_id, config.RATED_ROOM_IDS)
        self.assertNotIn(disabled.room_id, config.RECRUITMENT_ROOM_IDS)

        for room_id in ("open_9_cross", "open_9_turn"):
            room = config.ROOM_DEFINITION_MAP[room_id]
            self.assertTrue(room.enabled)
            self.assertIn(room, config.ACTIVE_ROOM_DEFINITIONS)
            self.assertIn(room.room_id, config.ACTIVE_ROOM_IDS)
            self.assertEqual(room.strict_access_role_names, frozenset({"ねいと"}))
            self.assertNotIn(room.room_id, config.ADMIN_ONLY_ROOM_IDS)
            self.assertIn(room.room_id, config.PUBLIC_ROOM_IDS)

        self.assertTrue(
            config.VARIANT_ROLLOUT_ROOM_IDS
            <= config.RECRUITMENT_DISABLED_ROOM_IDS
        )
        self.assertEqual(config.ROOM_DEFINITION_MAP["open"].variant_id, "v13_cross")
        self.assertEqual(disabled.variant_id, "v13_turn")
        self.assertEqual(config.ROOM_DEFINITION_MAP["open_9_cross"].variant_id, "v9_cross")
        self.assertEqual(config.ROOM_DEFINITION_MAP["open_9_turn"].variant_id, "v9_turn")

    def test_game_cog_creates_runners_for_active_rooms_only(self) -> None:
        cog = GameCog(SimpleNamespace(managed_guild_id=1))
        self.assertEqual(set(cog.rooms), set(config.ACTIVE_ROOM_IDS))
        self.assertNotIn("open_13_turn", cog.rooms)

    def test_ladder_grandmaster_roles_have_an_explicit_priority(self) -> None:
        ladder_13 = config.LADDER_DEFINITIONS["l13"]
        ladder_9 = config.LADDER_DEFINITIONS["l9"]
        self.assertEqual(ladder_13.grandmaster_role_name, "グランドマスター")
        self.assertEqual(ladder_9.grandmaster_role_name, "グランドマスター（9人村）")
        self.assertGreater(ladder_13.role_position_priority, ladder_9.role_position_priority)


class VariantSimulationPlanTest(unittest.TestCase):
    def test_default_plan_is_deterministic_and_covers_every_variant(self) -> None:
        first = simulate_games.build_simulation_scenarios(0)
        second = simulate_games.build_simulation_scenarios(0)
        self.assertEqual(first, second)
        rated_counts = Counter(
            scenario.variant_id for scenario in first if scenario.rated
        )
        self.assertEqual(set(rated_counts), set(EXPECTED_VARIANT_IDS))
        self.assertTrue(all(rated_counts[variant_id] >= 1 for variant_id in EXPECTED_VARIANT_IDS))
        self.assertTrue(first[0].force_runoff)
        self.assertTrue(first[0].fail_rank_lookup)
        self.assertFalse(first[-1].rated)

    def test_additional_games_cycle_variants_in_a_fixed_order(self) -> None:
        scenarios = simulate_games.build_simulation_scenarios(6)
        additional = scenarios[4:-1]
        self.assertEqual(
            [scenario.variant_id for scenario in additional],
            [
                "v13_cross",
                "v13_turn",
                "v9_cross",
                "v9_turn",
                "v13_cross",
                "v13_turn",
            ],
        )

    def test_nine_player_population_selection_uses_requested_capacity(self) -> None:
        player_ids = list(range(20))
        selected = simulate_games._select_balanced_players(
            rng=Random(123),
            player_ids=player_ids,
            games_played={player_id: 0 for player_id in player_ids},
            player_count=9,
        )
        self.assertEqual(len(selected), 9)
        self.assertEqual(len(set(selected)), 9)

    def test_each_variant_rating_pool_keeps_elo_delta_zero_sum(self) -> None:
        for variant_id in EXPECTED_VARIANT_IDS:
            variant = config.VARIANT_DEFINITIONS[variant_id]
            wolf_winners = variant.wolf_team_size
            players = [
                {"player_id": index, "rating": 1500, "won": index < wolf_winners}
                for index in range(variant.player_count)
            ]
            results = rating.calculate_game_results(
                players,
                winner_team=config.Team.WOLF,
                variant_id=variant_id,
                village_win_pool=variant.village_win_pool,
                wolf_win_pool=variant.wolf_win_pool,
            )
            self.assertEqual(sum(result["elo_delta"] for result in results), 0)
            self.assertGreater(sum(result["delta"] for result in results), 0)


if __name__ == "__main__":
    unittest.main()
