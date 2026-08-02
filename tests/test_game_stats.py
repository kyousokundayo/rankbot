"""action_logから作る試合統計の手計算回帰テスト。"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from config import Phase, RANK_SPECS, Role
from room_runner import build_game_stats, build_rank_bucket


class GameStatsAggregationTest(unittest.TestCase):
    @staticmethod
    def _players() -> list[dict]:
        roles = [
            Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF,
            Role.MADMAN, Role.SEER, Role.MEDIUM, Role.GUARD,
            *([Role.VILLAGER] * 6),
        ]
        return [
            {"player_id": index, "role": role.value, "display_name": f"P{index}"}
            for index, role in enumerate(roles, 1)
        ]

    @staticmethod
    def _entry(day: int, phase: Phase, kind: str, **kwargs) -> dict:
        return {
            "day": day,
            "phase": phase.name,
            "kind": kind,
            "actor": "",
            "target": "",
            "detail": "",
            "actor_id": None,
            "target_id": None,
            **kwargs,
        }

    def test_known_four_day_game_matches_manual_totals(self) -> None:
        log = [
            self._entry(1, Phase.DAY_VOTE, "死亡", target="P1", target_id=1,
                        detail="処刑 / 役職=人狼"),
            self._entry(1, Phase.NIGHT, "占い", actor="P5", actor_id=5,
                        target="P2", target_id=2, detail="結果=人狼"),
            self._entry(1, Phase.NIGHT, "護衛", actor="P7", actor_id=7,
                        target="P6", target_id=6),
            self._entry(1, Phase.NIGHT, "死亡", target="P6", target_id=6,
                        detail="襲撃 / 役職=霊媒師"),
            self._entry(2, Phase.DAY_VOTE, "死亡", target="P8", target_id=8,
                        detail="処刑 / 役職=村人"),
            self._entry(2, Phase.NIGHT, "占い", actor="P5", actor_id=5,
                        target="P9", target_id=9, detail="結果=村人"),
            self._entry(2, Phase.NIGHT, "護衛", actor="P7", actor_id=7,
                        target="P9", target_id=9),
            self._entry(2, Phase.NIGHT, "護衛成功", target="P9", target_id=9,
                        detail="襲撃を防いだ"),
            self._entry(2, Phase.NIGHT, "平和", detail="護衛成功"),
            # 明示ログと噛みなしログが両方あっても、同じ朝は1回だけ数える。
            self._entry(3, Phase.NIGHT, "襲撃先", detail="噛みなし"),
            self._entry(3, Phase.NIGHT, "平和", detail="噛みなし"),
            self._entry(3, Phase.DAY_VOTE, "死亡", target="P2", target_id=2,
                        detail="処刑 / 役職=人狼"),
            self._entry(4, Phase.DAY_VOTE, "死亡", target="P3", target_id=3,
                        detail="処刑 / 役職=人狼"),
        ]

        stats, deaths = build_game_stats(log, self._players(), days=4)

        self.assertEqual(stats["days"], 4)
        self.assertEqual(stats["peaceful_mornings"], 2)
        self.assertEqual(stats["guard_successes"], 1)
        # 3夜目は狩人が未行動でも生存しているため分母へ含める。
        self.assertEqual(stats["guard_checks"], 3)
        self.assertEqual(stats["seer_checks"], 2)
        self.assertEqual(stats["seer_wolf_hits"], 1)
        self.assertEqual(stats["day1_execution_was_wolf"], 1)
        self.assertEqual(stats["executions_total"], 4)
        self.assertEqual(stats["executions_wolf"], 3)
        self.assertEqual(stats["night1_kill_had_role"], 1)
        self.assertEqual(stats["wolf_alive_by_day"], [3, 2, 2, 1])
        self.assertEqual(deaths[6], {"died_on_day": 1, "death_cause": "襲撃"})
        self.assertNotIn(7, deaths)

    def test_rejected_actions_and_duplicate_deaths_are_not_counted(self) -> None:
        log = [
            self._entry(1, Phase.NIGHT, "占い(拒否)", actor_id=5),
            self._entry(1, Phase.NIGHT, "死亡", target="P1", target_id=1,
                        detail="処刑 / 役職=人狼"),
            self._entry(1, Phase.NIGHT, "死亡", target="P1", target_id=1,
                        detail="処刑 / 役職=人狼"),
        ]
        stats, _deaths = build_game_stats(log, self._players(), days=1)
        self.assertEqual(stats["seer_checks"], 0)
        self.assertEqual(stats["executions_total"], 1)
        self.assertEqual(stats["executions_wolf"], 1)

    def test_guard_rate_denominator_is_alive_nights_not_actions(self) -> None:
        log = [
            self._entry(1, Phase.NIGHT, "平和", detail="襲撃なし"),
            self._entry(2, Phase.NIGHT, "護衛", actor="P7", actor_id=7,
                        target="P9", target_id=9),
            self._entry(2, Phase.NIGHT, "護衛成功", target="P9", target_id=9),
            self._entry(2, Phase.NIGHT, "平和", detail="護衛成功"),
        ]
        stats, _ = build_game_stats(log, self._players(), days=3)
        self.assertEqual(stats["guard_successes"], 1)
        self.assertEqual(stats["guard_checks"], 2)

    def test_executed_guard_is_not_counted_for_same_night(self) -> None:
        log = [
            self._entry(1, Phase.DAY_VOTE, "死亡", target="P7", target_id=7,
                        detail="処刑 / 役職=狩人"),
            self._entry(1, Phase.NIGHT, "平和", detail="襲撃なし"),
        ]
        stats, _ = build_game_stats(log, self._players(), days=2)
        self.assertEqual(stats["guard_checks"], 0)

    def test_rank_bucket_uses_existing_order_and_lower_even_median(self) -> None:
        rank_names = [name for name, _emoji, _color in RANK_SPECS]
        values = [8, 0, 1, 2, 3, 4, 5, 6, 7, 8, 0, 1, 2]
        contexts = {
            index: SimpleNamespace(rank_name=rank_names[value], provisional=index % 2 == 0)
            for index, value in enumerate(values, 1)
        }
        expected = rank_names[sorted(values)[6]]
        self.assertEqual(build_rank_bucket(contexts, list(contexts)), expected)

        # 除外等で12人になった場合も、中央2人のうち下位側を採る。
        even_values = [8, 0, 1, 2, 3, 4, 5, 6, 7, 8, 7, 6]
        even_contexts = {
            index: SimpleNamespace(rank_name=rank_names[value])
            for index, value in enumerate(even_values, 1)
        }
        expected_lower = rank_names[sorted(even_values)[5]]
        self.assertEqual(
            build_rank_bucket(even_contexts, list(even_contexts)),
            expected_lower,
        )
        self.assertIsNone(
            build_rank_bucket(even_contexts, [*even_contexts, 99]),
        )


if __name__ == "__main__":
    unittest.main()
