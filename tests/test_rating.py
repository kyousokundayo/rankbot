"""rating.py の単体テスト

実行: .venv/bin/python -m unittest discover -s tests -v
(scripts/run_checks.sh と CI からも実行される)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rating as rating_lib
import database
from config import (
    GRANDMASTER_PERCENTAGE,
    GRANDMASTER_SLOTS,
    INITIAL_RATING,
    RANK_BAND,
    RANK_SPECS,
    RATING_FLOOR,
    ROLE_TEAM,
    Role,
    SEASON_RANK_MIN_GAMES,
    SEASON_RANK_PERCENTAGES,
    VARIANT_DEFINITIONS,
    WIN_PARTICIPATION_BONUS,
    Team,
)


V13_RATING_PARAMETERS = VARIANT_DEFINITIONS["v13_cross"]
WOLF_WIN_FIXED_POOL = V13_RATING_PARAMETERS.wolf_win_pool
VILLAGE_WIN_FIXED_POOL = V13_RATING_PARAMETERS.village_win_pool


def make_players(
    winner_count: int,
    loser_count: int,
    rating: int = INITIAL_RATING,
) -> list[dict]:
    players = []
    for i in range(winner_count):
        players.append({"player_id": 100 + i, "rating": rating, "won": True})
    for i in range(loser_count):
        players.append({"player_id": 200 + i, "rating": rating, "won": False})
    return players


def make_ranked_players(
    winner_count: int,
    loser_count: int,
    *,
    winner_rank: str,
    loser_rank: str,
    rating: int = INITIAL_RATING,
) -> list[dict]:
    players = make_players(winner_count, loser_count, rating)
    for player in players:
        player["rank_name"] = winner_rank if player["won"] else loser_rank
    return players


class TestCalculateGameResults(unittest.TestCase):
    def test_adopted_rating_constants(self):
        self.assertEqual(INITIAL_RATING, 1500)
        self.assertEqual(RATING_FLOOR, 1000)
        self.assertEqual(WIN_PARTICIPATION_BONUS, 1)
        self.assertEqual(WOLF_WIN_FIXED_POOL, 120)
        self.assertEqual(VILLAGE_WIN_FIXED_POOL, 180)

    def test_pool_ratio_implies_sixty_percent_wolf_winrate(self):
        """W/V = (1-p)/p。120:180 は「狼勝率60%で全体EV=0」を意味する。

        倍率だけを変えても均衡勝率は動かない。実測が溜まったら比率のほうを直す。
        """
        break_even = VILLAGE_WIN_FIXED_POOL / (
            VILLAGE_WIN_FIXED_POOL + WOLF_WIN_FIXED_POOL
        )
        self.assertAlmostEqual(break_even, 0.60)

    def test_nine_player_pool_ratio_implies_forty_five_percent_wolf_winrate(self):
        """9人村は狼勝率45%を前提に、両進行方式で同じプールを使う。"""
        parameters = {
            variant_id: rating_lib.resolve_variant_rating_parameters(variant_id)
            for variant_id in NINE_VARIANT_IDS
        }
        self.assertEqual(parameters["v9_cross"], parameters["v9_turn"])
        break_even = parameters["v9_cross"]["village_win_pool"] / (
            parameters["v9_cross"]["village_win_pool"]
            + parameters["v9_cross"]["wolf_win_pool"]
        )
        self.assertAlmostEqual(break_even, 0.45)

    def test_wolf_win_pool_is_zero_sum_except_bonus(self):
        """狼勝ち (勝者4/敗者9): elo_deltaはゼロサム、deltaはボーナス分だけ正"""
        results = rating_lib.calculate_game_results(
            make_players(4, 9), winner_team="狼陣営"
        )
        elo_sum = sum(r["elo_delta"] for r in results)
        delta_sum = sum(r["delta"] for r in results)
        self.assertEqual(elo_sum, 0)
        self.assertEqual(delta_sum, 4 * WIN_PARTICIPATION_BONUS)

    def test_village_win_pool_is_zero_sum_except_bonus(self):
        """村勝ち (勝者9/敗者4): elo_deltaはゼロサム、deltaはボーナス分だけ正"""
        results = rating_lib.calculate_game_results(
            make_players(9, 4), winner_team="村陣営"
        )
        elo_sum = sum(r["elo_delta"] for r in results)
        delta_sum = sum(r["delta"] for r in results)
        self.assertEqual(elo_sum, 0)
        self.assertEqual(delta_sum, 9 * WIN_PARTICIPATION_BONUS)

    def test_nine_player_variant_uses_its_own_zero_sum_pool(self):
        parameters = rating_lib.resolve_variant_rating_parameters("v9_cross")
        players = [
            {
                "player_id": record["player_id"],
                "rating": INITIAL_RATING,
                "won": record["won"],
            }
            for record in make_nine_records()
        ]
        results = rating_lib.calculate_game_results(
            players,
            winner_team=Team.VILLAGE,
            variant_id="v9_cross",
        )
        self.assertEqual(sum(row["elo_delta"] for row in results), 0)
        self.assertEqual(
            sum(row["elo_delta"] for row in results if row["elo_delta"] > 0),
            parameters["village_win_pool"],
        )
        self.assertEqual(
            sum(row["elo_delta"] for row in results if row["elo_delta"] < 0),
            -parameters["village_win_pool"],
        )

    def test_nine_variants_share_the_same_base_rating_rules(self):
        """進行方式の違いで9人村のレート精算が変わらない。"""
        players = [
            {
                "player_id": record["player_id"],
                "rating": INITIAL_RATING,
                "won": record["won"],
                # 卓帯補正を通した結果も比較する。
                "rank_name": "プラチナ" if record["won"] else "シルバー",
            }
            for record in make_nine_records()
        ]
        parameters = {
            variant_id: rating_lib.resolve_variant_rating_parameters(variant_id)
            for variant_id in NINE_VARIANT_IDS
        }
        results = {
            variant_id: rating_lib.calculate_game_results(
                players,
                winner_team=Team.VILLAGE,
                variant_id=variant_id,
            )
            for variant_id in NINE_VARIANT_IDS
        }

        self.assertEqual(parameters["v9_cross"], parameters["v9_turn"])
        self.assertEqual(results["v9_cross"], results["v9_turn"])
        self.assertEqual(sum(row["elo_delta"] for row in results["v9_cross"]), 0)

    def test_pool_selection_by_winner_count(self):
        """勝者が少数側 (狼勝ち) なら120プール、多数側 (村勝ち) なら180プール"""
        wolf_win = rating_lib.calculate_game_results(
            make_players(4, 9), winner_team="狼陣営"
        )
        winners_gain = sum(r["elo_delta"] for r in wolf_win if r["elo_delta"] > 0)
        self.assertEqual(winners_gain, WOLF_WIN_FIXED_POOL)

        village_win = rating_lib.calculate_game_results(
            make_players(9, 4), winner_team="村陣営"
        )
        winners_gain = sum(r["elo_delta"] for r in village_win if r["elo_delta"] > 0)
        self.assertEqual(winners_gain, VILLAGE_WIN_FIXED_POOL)

    def test_bonus_only_for_winners(self):
        results = rating_lib.calculate_game_results(
            make_players(4, 9), winner_team="狼陣営"
        )
        for r in results:
            if r["player_id"] >= 200:  # 敗者
                self.assertEqual(r["bonus"], 0)
                self.assertLess(r["delta"], 0)
            else:
                self.assertEqual(r["bonus"], WIN_PARTICIPATION_BONUS)
                self.assertGreater(r["delta"], 0)

    def test_rating_after_consistency(self):
        results = rating_lib.calculate_game_results(
            make_players(9, 4, rating=1500), winner_team="村陣営"
        )
        for r in results:
            self.assertEqual(r["rating_after"], r["rating_before"] + r["delta"])

    def test_remainder_split_is_deterministic_and_fair(self):
        """端数の分配は決定的で、各人の差は最大1"""
        players = make_players(9, 4)
        first = rating_lib.calculate_game_results(players, winner_team="村陣営")
        second = rating_lib.calculate_game_results(players, winner_team="村陣営")
        self.assertEqual(first, second)

        winner_deltas = [r["elo_delta"] for r in first if r["player_id"] < 200]
        self.assertLessEqual(max(winner_deltas) - min(winner_deltas), 1)
        loser_deltas = [r["elo_delta"] for r in first if r["player_id"] >= 200]
        self.assertLessEqual(max(loser_deltas) - min(loser_deltas), 1)

    def test_floor_clamps_losses(self):
        """フロア上の敗者はそれ以上下がらない (delta=0)、勝者は通常どおり上がる"""
        results = rating_lib.calculate_game_results(
            make_players(9, 4, rating=RATING_FLOOR), winner_team="村陣営"
        )
        for r in results:
            self.assertGreaterEqual(r["rating_after"], RATING_FLOOR)
            if r["player_id"] >= 200:  # 敗者
                self.assertEqual(r["rating_after"], RATING_FLOOR)
                self.assertEqual(r["delta"], 0)
            else:
                self.assertGreater(r["delta"], 0)

    def test_floor_partial_clamp(self):
        """フロア直上の敗者は損失がフロアまでで打ち止めになる"""
        start = RATING_FLOOR + 5
        results = rating_lib.calculate_game_results(
            make_players(9, 4, rating=start), winner_team="村陣営"
        )
        for r in results:
            if r["player_id"] >= 200:  # 村勝ち時の狼側敗者: 名目-22前後 → 実際は-5
                self.assertEqual(r["rating_after"], RATING_FLOOR)
                self.assertEqual(r["delta"], RATING_FLOOR - start)
                self.assertLess(r["elo_delta"], r["delta"])  # 名目値は実適用より大きい損失

    def test_below_floor_is_frozen_not_lifted(self):
        """フロア未満の既存レートは敗北で引き上げられず据え置き、勝利では通常どおり上がる"""
        start = RATING_FLOOR - 100
        results = rating_lib.calculate_game_results(
            make_players(9, 4, rating=start), winner_team="村陣営"
        )
        for r in results:
            if r["player_id"] >= 200:  # 敗者: 据え置き
                self.assertEqual(r["rating_after"], start)
                self.assertEqual(r["delta"], 0)
            else:  # 勝者: 通常どおり上昇
                self.assertGreater(r["delta"], 0)

    def test_no_clamp_above_floor(self):
        """フロアから十分離れていればゼロサムが保たれる (既存テストの前提確認)"""
        results = rating_lib.calculate_game_results(
            make_players(4, 9, rating=INITIAL_RATING), winner_team="狼陣営"
        )
        self.assertEqual(sum(r["elo_delta"] for r in results), 0)
        self.assertEqual(
            sum(r["delta"] for r in results), 4 * WIN_PARTICIPATION_BONUS
        )

    def test_all_winners_or_all_losers_is_noop(self):
        """勝者か敗者が空 (廃村相当の防御) は全員変動なし"""
        for results in (
            rating_lib.calculate_game_results(
                make_players(13, 0), winner_team="村陣営"
            ),
            rating_lib.calculate_game_results(
                make_players(0, 13), winner_team="狼陣営"
            ),
        ):
            for r in results:
                self.assertEqual(r["delta"], 0)
                self.assertEqual(r["rating_after"], r["rating_before"])


class TestBandCoefficient(unittest.TestCase):
    """卓帯 (初心者/中級者/上級者) の中央値差によるプール補正"""

    def test_rank_band_covers_every_rank(self):
        """9段階すべてが3帯のどれかに入る (制限卓の参加条件から導出している)"""
        for rank_name, _emoji, _color in RANK_SPECS:
            self.assertIn(rank_name, RANK_BAND)
        self.assertEqual(set(RANK_BAND.values()), {0, 1, 2})

    def test_same_band_is_neutral(self):
        """同じ帯どうしなら等倍。制限卓は参加条件で帯が揃うのでここに入る"""
        for winner_team, winners, losers, pool in (
            ("狼陣営", 4, 9, WOLF_WIN_FIXED_POOL),
            ("村陣営", 9, 4, VILLAGE_WIN_FIXED_POOL),
        ):
            with self.subTest(winner_team=winner_team):
                results = rating_lib.calculate_game_results(
                    make_ranked_players(
                        winners, losers,
                        winner_rank="ゴールド", loser_rank="エメラルド",
                    ),
                    winner_team=winner_team,
                )
                gain = sum(r["elo_delta"] for r in results if r["elo_delta"] > 0)
                self.assertEqual(gain, pool)

    def test_stronger_side_winning_shrinks_pool(self):
        """上級者の狼が初心者の村に勝つ: 2帯差の格上勝ち → 0.8倍"""
        results = rating_lib.calculate_game_results(
            make_ranked_players(4, 9, winner_rank="マスター", loser_rank="ブロンズ"),
            winner_team="狼陣営",
        )
        gain = sum(r["elo_delta"] for r in results if r["elo_delta"] > 0)
        self.assertEqual(gain, WOLF_WIN_FIXED_POOL * 80 // 100)

    def test_weaker_side_winning_grows_pool(self):
        """初心者の狼が上級者の村に勝つ: 2帯差の格下勝ち → 1.2倍"""
        results = rating_lib.calculate_game_results(
            make_ranked_players(4, 9, winner_rank="ブロンズ", loser_rank="マスター"),
            winner_team="狼陣営",
        )
        gain = sum(r["elo_delta"] for r in results if r["elo_delta"] > 0)
        self.assertEqual(gain, WOLF_WIN_FIXED_POOL * 120 // 100)

    def test_one_band_difference_is_ten_percent(self):
        """中級者の村が初心者の狼に勝つ: 1帯差の格上勝ち → 0.9倍"""
        results = rating_lib.calculate_game_results(
            make_ranked_players(9, 4, winner_rank="プラチナ", loser_rank="シルバー"),
            winner_team="村陣営",
        )
        gain = sum(r["elo_delta"] for r in results if r["elo_delta"] > 0)
        self.assertEqual(gain, VILLAGE_WIN_FIXED_POOL * 90 // 100)

    def test_coefficient_keeps_zero_sum(self):
        """係数を掛けてもプール本体のゼロサムは崩れない"""
        for winner_rank, loser_rank in (
            ("マスター", "ブロンズ"),
            ("ブロンズ", "マスター"),
            ("ゴールド", "シルバー"),
        ):
            with self.subTest(winner_rank=winner_rank, loser_rank=loser_rank):
                results = rating_lib.calculate_game_results(
                    make_ranked_players(
                        4, 9, winner_rank=winner_rank, loser_rank=loser_rank,
                    ),
                    winner_team="狼陣営",
                )
                self.assertEqual(sum(r["elo_delta"] for r in results), 0)

    def test_missing_rank_falls_back_to_neutral(self):
        """1人でもランクが取れなければ補正しない (片側だけ歪むのを防ぐ)"""
        players = make_ranked_players(
            4, 9, winner_rank="マスター", loser_rank="ブロンズ",
        )
        players[0]["rank_name"] = None
        results = rating_lib.calculate_game_results(players, winner_team="狼陣営")
        gain = sum(r["elo_delta"] for r in results if r["elo_delta"] > 0)
        self.assertEqual(gain, WOLF_WIN_FIXED_POOL)

    def test_no_rank_key_at_all_is_neutral(self):
        """rank_name を渡さない既存の呼び出しはそのまま等倍で通る"""
        results = rating_lib.calculate_game_results(
            make_players(4, 9), winner_team="狼陣営"
        )
        gain = sum(r["elo_delta"] for r in results if r["elo_delta"] > 0)
        self.assertEqual(gain, WOLF_WIN_FIXED_POOL)

    def test_median_uses_lower_side_on_even_count(self):
        """狼4人の帯が割れたら下位側を代表にする (build_rank_bucket と同じ規則)"""
        players = [
            {"player_id": 100, "rating": INITIAL_RATING, "won": True, "rank_name": "マスター"},
            {"player_id": 101, "rating": INITIAL_RATING, "won": True, "rank_name": "ダイヤ"},
            {"player_id": 102, "rating": INITIAL_RATING, "won": True, "rank_name": "ブロンズ"},
            {"player_id": 103, "rating": INITIAL_RATING, "won": True, "rank_name": "シルバー"},
        ]
        players += [
            {
                "player_id": 200 + i, "rating": INITIAL_RATING,
                "won": False, "rank_name": "シルバー",
            }
            for i in range(9)
        ]
        results = rating_lib.calculate_game_results(players, winner_team="狼陣営")
        gain = sum(r["elo_delta"] for r in results if r["elo_delta"] > 0)
        # 狼の帯は上位側なら2だが下位側を採る規則で0。村も0なので等倍
        self.assertEqual(gain, WOLF_WIN_FIXED_POOL)


WOLF_IDS = (1, 2, 3)
MADMAN_ID = 4
SEER_ID = 5
MEDIUM_ID = 6
GUARD_ID = 7
VILLAGER_IDS = (8, 9, 10, 11, 12, 13)

# 9人村の実配役。13人用の fixture を切り詰めると人狼3人になってしまうため、
# 9人固有の加点・精算は必ずこの構成で検証する。
NINE_WOLF_IDS = (21, 22)
NINE_MADMAN_ID = 23
NINE_SEER_ID = 24
NINE_MEDIUM_ID = 25
NINE_GUARD_ID = 26
NINE_VILLAGER_IDS = (27, 28, 29)
NINE_VARIANT_IDS = ("v9_cross", "v9_turn")


def make_records(deaths: dict[int, dict] | None = None) -> list[dict]:
    """13人固定構成の参加者レコード。deaths で死亡日と死因を差し込む"""
    layout = (
        [(pid, Role.WEREWOLF) for pid in WOLF_IDS]
        + [(MADMAN_ID, Role.MADMAN), (SEER_ID, Role.SEER)]
        + [(MEDIUM_ID, Role.MEDIUM), (GUARD_ID, Role.GUARD)]
        + [(pid, Role.VILLAGER) for pid in VILLAGER_IDS]
    )
    records = []
    for player_id, role in layout:
        team = ROLE_TEAM[role]
        record = {
            "player_id": player_id,
            "role": role.value,
            "team": team.value,
            "won": team is Team.VILLAGE,
            "died_on_day": None,
            "death_cause": None,
        }
        record.update((deaths or {}).get(player_id, {}))
        records.append(record)
    return records


def make_nine_records(deaths: dict[int, dict] | None = None) -> list[dict]:
    """9人固定構成（狼2・狂1・占霊狩各1・村3）の参加者レコード。"""
    layout = (
        [(pid, Role.WEREWOLF) for pid in NINE_WOLF_IDS]
        + [(NINE_MADMAN_ID, Role.MADMAN), (NINE_SEER_ID, Role.SEER)]
        + [(NINE_MEDIUM_ID, Role.MEDIUM), (NINE_GUARD_ID, Role.GUARD)]
        + [(pid, Role.VILLAGER) for pid in NINE_VILLAGER_IDS]
    )
    records = []
    for player_id, role in layout:
        team = ROLE_TEAM[role]
        record = {
            "player_id": player_id,
            "role": role.value,
            "team": team.value,
            "won": team is Team.VILLAGE,
            "died_on_day": None,
            "death_cause": None,
        }
        record.update((deaths or {}).get(player_id, {}))
        records.append(record)
    return records


class TestCalculatePlayBonuses(unittest.TestCase):
    """勝敗とは別枠の非ゼロサム加点"""

    def test_no_facts_gives_nothing(self):
        for facts in (None, {}):
            with self.subTest(facts=facts):
                self.assertEqual(
                    rating_lib.calculate_play_bonuses(make_records(), facts), {}
                )

    def test_empty_records_is_safe(self):
        self.assertEqual(rating_lib.calculate_play_bonuses([], {"days": 9}), {})

    def test_wolf_execution_vote_only_pays_village(self):
        """処刑された人狼へ投票した村陣営だけが+2。狂人と人狼は入らない"""
        bonuses = rating_lib.calculate_play_bonuses(
            make_records(),
            {"executions": [{"day": 2, "target": WOLF_IDS[0],
                             "voters": [8, 9, MADMAN_ID, WOLF_IDS[1]]}]},
        )
        self.assertEqual(bonuses.get(8), 2)
        self.assertEqual(bonuses.get(9), 2)
        self.assertNotIn(MADMAN_ID, bonuses)
        self.assertNotIn(WOLF_IDS[1], bonuses)

    def test_executing_the_madman_pays_nobody(self):
        bonuses = rating_lib.calculate_play_bonuses(
            make_records(),
            {"executions": [{"day": 1, "target": MADMAN_ID, "voters": [8, 9, 10]}]},
        )
        self.assertEqual(bonuses, {})

    def test_random_execution_without_voters_pays_nobody(self):
        bonuses = rating_lib.calculate_play_bonuses(
            make_records(),
            {"executions": [{"day": 3, "target": WOLF_IDS[0], "voters": []}]},
        )
        self.assertEqual(bonuses, {})

    def test_multiple_wolf_executions_stack(self):
        bonuses = rating_lib.calculate_play_bonuses(
            make_records(),
            {"executions": [
                {"day": 1, "target": WOLF_IDS[0], "voters": [8]},
                {"day": 2, "target": WOLF_IDS[1], "voters": [8]},
            ]},
        )
        self.assertEqual(bonuses.get(8), 4)

    def test_final_day_pays_wolves_only(self):
        bonuses = rating_lib.calculate_play_bonuses(make_records(), {"days": 6})
        for wolf_id in WOLF_IDS:
            self.assertEqual(bonuses.get(wolf_id), 2)
        self.assertNotIn(MADMAN_ID, bonuses)
        self.assertNotIn(SEER_ID, bonuses)

    def test_final_day_threshold_is_exclusive_below_six(self):
        self.assertEqual(rating_lib.calculate_play_bonuses(make_records(), {"days": 5}), {})

    def test_wolf_guess_scores_one_per_hit(self):
        """3日目以降の死亡は等倍。的中2人なら+2"""
        records = make_records({8: {"died_on_day": 3, "death_cause": "襲撃"}})
        bonuses = rating_lib.calculate_play_bonuses(
            records, {"wolf_guesses": {"8": [WOLF_IDS[0], WOLF_IDS[1], MADMAN_ID]}},
        )
        self.assertEqual(bonuses.get(8), 2)

    def test_wolf_guess_doubles_for_early_deaths(self):
        """初日・2日目の死亡は2倍。全的中なら+6"""
        for day in (1, 2):
            with self.subTest(day=day):
                records = make_records({8: {"died_on_day": day, "death_cause": "処刑"}})
                bonuses = rating_lib.calculate_play_bonuses(
                    records, {"wolf_guesses": {8: list(WOLF_IDS)}},
                )
                self.assertEqual(bonuses.get(8), 6)

    def test_wolf_guess_scores_madman_who_does_not_know_wolves(self):
        """人狼を知らない狂人は村役職と同じく採点する。"""
        records = make_records({MADMAN_ID: {"died_on_day": 1, "death_cause": "処刑"}})
        bonuses = rating_lib.calculate_play_bonuses(
            records, {"wolf_guesses": {MADMAN_ID: list(WOLF_IDS)}},
        )
        self.assertEqual(bonuses, {MADMAN_ID: 6})

    def test_wolf_guess_ignores_actual_wolf_who_knows_answer(self):
        """正解を知っている実人狼の提出は採点しない。"""
        wolf_id = WOLF_IDS[0]
        records = make_records({wolf_id: {"died_on_day": 1, "death_cause": "処刑"}})
        bonuses = rating_lib.calculate_play_bonuses(
            records, {"wolf_guesses": {wolf_id: list(WOLF_IDS)}},
        )
        self.assertEqual(bonuses, {})

    def test_wolf_guess_ignores_removed_players(self):
        """除外 (途中離脱) は対象外"""
        records = make_records({8: {"died_on_day": 1, "death_cause": "除外"}})
        bonuses = rating_lib.calculate_play_bonuses(
            records, {"wolf_guesses": {8: list(WOLF_IDS)}},
        )
        self.assertEqual(bonuses, {})

    def test_wolf_guess_ignores_survivors(self):
        """死亡日がない (終了時まで生存) 人は対象外"""
        bonuses = rating_lib.calculate_play_bonuses(
            make_records(), {"wolf_guesses": {8: list(WOLF_IDS)}},
        )
        self.assertEqual(bonuses, {})

    def test_wolf_guess_caps_at_three_hits(self):
        """4人以上を提出しても的中は3人ぶんまで"""
        records = make_records({8: {"died_on_day": 4, "death_cause": "襲撃"}})
        bonuses = rating_lib.calculate_play_bonuses(
            records, {"wolf_guesses": {8: list(WOLF_IDS) + [MADMAN_ID, SEER_ID]}},
        )
        self.assertEqual(bonuses.get(8), 3)

    def test_guess_slots_and_final_day_threshold_are_arguments(self):
        records = make_records({8: {"died_on_day": 3, "death_cause": "襲撃"}})
        facts = {"days": 4, "wolf_guesses": {"8": [1, 2, 3]}}
        hits = rating_lib.count_wolf_guess_hits(
            records, facts, wolf_guess_slots=2,
        )
        bonuses = rating_lib.calculate_play_bonuses(
            records,
            facts,
            wolf_guess_slots=2,
            final_day_threshold=4,
        )
        self.assertEqual(hits[8], 2)
        self.assertEqual(bonuses[8], 2)
        for wolf_id in WOLF_IDS:
            self.assertEqual(bonuses[wolf_id], 2)

    def test_nine_early_wolf_guess_is_capped_at_two_hits_and_doubled_to_four(self):
        """9人村の早期狼予想は実狼2人を全的中して最大+4。"""
        records = make_nine_records({
            NINE_VILLAGER_IDS[0]: {"died_on_day": 1, "death_cause": "処刑"},
        })
        facts = {
            "wolf_guesses": {
                NINE_VILLAGER_IDS[0]: [*NINE_WOLF_IDS, NINE_MADMAN_ID],
            },
        }
        bonuses_by_variant = {}
        for variant_id in NINE_VARIANT_IDS:
            params = rating_lib.resolve_variant_rating_parameters(variant_id)
            hits = rating_lib.count_wolf_guess_hits(
                records,
                facts,
                wolf_guess_slots=params["wolf_guess_slots"],
            )
            bonuses_by_variant[variant_id] = rating_lib.calculate_play_bonuses(
                records,
                facts,
                wolf_guess_slots=params["wolf_guess_slots"],
                final_day_threshold=params["final_day_threshold"],
            )
            self.assertEqual(hits[NINE_VILLAGER_IDS[0]], 2)
            self.assertEqual(bonuses_by_variant[variant_id][NINE_VILLAGER_IDS[0]], 4)
        self.assertEqual(bonuses_by_variant["v9_cross"], bonuses_by_variant["v9_turn"])

    def test_nine_wolf_execution_votes_max_out_at_four(self):
        """9人村では実狼2人への投票を両方当てた村陣営が最大+4。"""
        voter_id = NINE_VILLAGER_IDS[1]
        facts = {
            "executions": [
                {"day": 1, "target": NINE_WOLF_IDS[0], "voters": [voter_id]},
                {"day": 2, "target": NINE_WOLF_IDS[1], "voters": [voter_id]},
            ],
        }
        bonuses_by_variant = {}
        for variant_id in NINE_VARIANT_IDS:
            params = rating_lib.resolve_variant_rating_parameters(variant_id)
            bonuses_by_variant[variant_id] = rating_lib.calculate_play_bonuses(
                make_nine_records(),
                facts,
                wolf_guess_slots=params["wolf_guess_slots"],
                final_day_threshold=params["final_day_threshold"],
            )
            self.assertEqual(bonuses_by_variant[variant_id][voter_id], 4)
        self.assertEqual(bonuses_by_variant["v9_cross"], bonuses_by_variant["v9_turn"])

    def test_nine_day_four_pays_each_real_wolf_two(self):
        """9人村の終盤到達ボーナスは4日目に狼2人だけへ各+2。"""
        bonuses_by_variant = {}
        for variant_id in NINE_VARIANT_IDS:
            params = rating_lib.resolve_variant_rating_parameters(variant_id)
            bonuses_by_variant[variant_id] = rating_lib.calculate_play_bonuses(
                make_nine_records(),
                {"days": 4},
                wolf_guess_slots=params["wolf_guess_slots"],
                final_day_threshold=params["final_day_threshold"],
            )
            self.assertEqual(
                bonuses_by_variant[variant_id],
                {wolf_id: 2 for wolf_id in NINE_WOLF_IDS},
            )
        self.assertEqual(bonuses_by_variant["v9_cross"], bonuses_by_variant["v9_turn"])

    def test_nine_night_one_seer_kill_pays_exactly_two_wolves(self):
        """初夜の占い師襲撃成功は、9人村の狼2人へだけ各+1。"""
        records = make_nine_records({
            NINE_SEER_ID: {"died_on_day": 1, "death_cause": "襲撃"},
        })
        bonuses_by_variant = {}
        for variant_id in NINE_VARIANT_IDS:
            params = rating_lib.resolve_variant_rating_parameters(variant_id)
            bonuses_by_variant[variant_id] = rating_lib.calculate_play_bonuses(
                records,
                {"night1_kill_target": NINE_SEER_ID},
                wolf_guess_slots=params["wolf_guess_slots"],
                final_day_threshold=params["final_day_threshold"],
            )
            self.assertEqual(
                bonuses_by_variant[variant_id],
                {wolf_id: 1 for wolf_id in NINE_WOLF_IDS},
            )
        self.assertEqual(bonuses_by_variant["v9_cross"], bonuses_by_variant["v9_turn"])

    def test_guard_success_pays_the_guard(self):
        bonuses = rating_lib.calculate_play_bonuses(
            make_records(), {"guard_successes": 2},
        )
        self.assertEqual(bonuses.get(GUARD_ID), 2)
        self.assertEqual(len(bonuses), 1)

    def test_no_guard_success_pays_nothing(self):
        self.assertEqual(
            rating_lib.calculate_play_bonuses(make_records(), {"guard_successes": 0}), {}
        )

    def test_night1_seer_kill_pays_wolves(self):
        records = make_records({SEER_ID: {"died_on_day": 1, "death_cause": "襲撃"}})
        bonuses = rating_lib.calculate_play_bonuses(
            records, {"night1_kill_target": SEER_ID},
        )
        for wolf_id in WOLF_IDS:
            self.assertEqual(bonuses.get(wolf_id), 1)
        self.assertNotIn(MADMAN_ID, bonuses)

    def test_night1_kill_of_other_role_pays_nothing(self):
        records = make_records({MEDIUM_ID: {"died_on_day": 1, "death_cause": "襲撃"}})
        bonuses = rating_lib.calculate_play_bonuses(
            records, {"night1_kill_target": MEDIUM_ID},
        )
        self.assertEqual(bonuses, {})

    def test_seer_dying_later_is_not_a_night1_kill(self):
        """初夜が護衛成功で、占い師が後日死んだ場合は入らない"""
        records = make_records({SEER_ID: {"died_on_day": 3, "death_cause": "襲撃"}})
        bonuses = rating_lib.calculate_play_bonuses(
            records, {"night1_kill_target": SEER_ID},
        )
        self.assertEqual(bonuses, {})

    def test_bonuses_from_different_rules_stack(self):
        records = make_records({
            SEER_ID: {"died_on_day": 1, "death_cause": "襲撃"},
            8: {"died_on_day": 1, "death_cause": "処刑"},
        })
        bonuses = rating_lib.calculate_play_bonuses(records, {
            "days": 6,
            "guard_successes": 1,
            "night1_kill_target": SEER_ID,
            "executions": [{"day": 2, "target": WOLF_IDS[0], "voters": [9, 10]}],
            "wolf_guesses": {8: [WOLF_IDS[0], SEER_ID, MADMAN_ID]},
        })
        # 人狼: 最終日+2、初夜占い噛み+1
        for wolf_id in WOLF_IDS:
            self.assertEqual(bonuses.get(wolf_id), 3)
        self.assertEqual(bonuses.get(GUARD_ID), 1)      # GJ 1回
        self.assertEqual(bonuses.get(8), 2)             # 的中1人 × 初日2倍
        self.assertEqual(bonuses.get(9), 2)             # 人狼処刑への投票
        self.assertEqual(bonuses.get(10), 2)
        self.assertNotIn(MADMAN_ID, bonuses)

    def test_unknown_player_ids_are_ignored(self):
        """参加者にいないIDが混ざっても落ちない"""
        bonuses = rating_lib.calculate_play_bonuses(
            make_records(),
            {
                "executions": [{"day": 1, "target": WOLF_IDS[0], "voters": [999, None, "x"]}],
                "wolf_guesses": {"999": [1, 2, 3]},
                "night1_kill_target": "not-an-id",
            },
        )
        self.assertEqual(bonuses, {})


def make_rows(count: int, games: int = SEASON_RANK_MIN_GAMES) -> list[dict]:
    # レートは player_id が小さいほど高い (順位が一意に決まる)
    # ランクの母集団は通算 (games) で決まる
    return [
        {
            "player_id": i + 1,
            "rating": 2000 - i,
            "games": games,
            "season_games": games,
            "season_wins": games // 2,
        }
        for i in range(count)
    ]


class TestBuildRankContextMap(unittest.TestCase):
    def test_under_threshold_is_provisional_bronze(self):
        rows = make_rows(10, games=SEASON_RANK_MIN_GAMES - 1)
        ctx = rating_lib.build_rank_context_map(rows)
        for c in ctx.values():
            self.assertTrue(c.provisional)
            self.assertEqual(c.rank_name, "ブロンズ")
            self.assertIsNone(c.position)

    def test_rank_survives_a_season_reset(self):
        """同着後もID順が元の順位と一致するデータではランクを維持する。

        母集団を通算で数えることで、リセット直後に全員が暫定ブロンズへ
        落ちないことも確認する。
        """
        rows = make_rows(100)
        before = rating_lib.build_rank_context_map(rows)

        # season_half_reset と同じ変換: レート半減 + 今季戦績ゼロ (通算は残す)
        after_rows = [
            {
                **row,
                "rating": INITIAL_RATING + (row["rating"] - INITIAL_RATING) // 2,
                "season_games": 0,
                "season_wins": 0,
            }
            for row in rows
        ]
        after = rating_lib.build_rank_context_map(after_rows)

        for player_id, ctx in before.items():
            self.assertEqual(ctx.rank_name, after[player_id].rank_name, player_id)
            self.assertEqual(ctx.position, after[player_id].position, player_id)
            self.assertFalse(after[player_id].provisional, player_id)

    def test_half_reset_can_reorder_adjacent_ratings_that_become_tied(self):
        """整数丸めで同着になれば、リセット後のタイブレークで順位は変わりうる。"""
        rows = [
            {
                "player_id": 2,
                "rating": 1503,
                "games": SEASON_RANK_MIN_GAMES,
                "season_games": SEASON_RANK_MIN_GAMES,
                "season_wins": 2,
            },
            {
                "player_id": 1,
                "rating": 1502,
                "games": SEASON_RANK_MIN_GAMES,
                "season_games": SEASON_RANK_MIN_GAMES,
                "season_wins": 1,
            },
        ]
        before = rating_lib.build_rank_context_map(rows)
        after = rating_lib.build_rank_context_map([
            {
                **row,
                "rating": INITIAL_RATING + (row["rating"] - INITIAL_RATING) // 2,
                "season_games": 0,
                "season_wins": 0,
            }
            for row in rows
        ])

        self.assertEqual(before[2].position, 1)
        self.assertEqual(after[2].position, 2)
        self.assertEqual(after[1].position, 1)

    def test_brand_new_player_is_still_provisional_bronze(self):
        """通算0戦だけが暫定ブロンズ (初心者卓へ入れる状態にしておく)。"""
        rows = make_rows(50)
        rows.append({
            "player_id": 999,
            "rating": INITIAL_RATING,
            "games": 0,
            "season_games": 0,
            "season_wins": 0,
        })

        ctx = rating_lib.build_rank_context_map(rows)

        self.assertTrue(ctx[999].provisional)
        self.assertEqual(ctx[999].rank_name, "ブロンズ")
        self.assertIsNone(ctx[999].position)

    def test_everyone_gets_context(self):
        rows = make_rows(100)
        ctx = rating_lib.build_rank_context_map(rows)
        self.assertEqual(len(ctx), 100)
        self.assertTrue(all(not c.provisional for c in ctx.values()))

    def test_grandmaster_slots(self):
        """アクティブ200人 → 上位5%=10人がGM、残り10人がマスター"""
        rows = make_rows(200)
        ctx = rating_lib.build_rank_context_map(rows)
        gm = [c for c in ctx.values() if c.rank_name == "グランドマスター"]
        master = [c for c in ctx.values() if c.rank_name == "マスター"]
        expected_gm = int(200 * GRANDMASTER_PERCENTAGE)
        self.assertEqual(len(gm), expected_gm)
        self.assertEqual(len(master), 20 - expected_gm)
        self.assertEqual(
            sorted(c.position for c in gm), list(range(1, expected_gm + 1))
        )

    def test_grandmaster_count_is_capped_at_thirteen(self):
        rows = make_rows(400)
        ctx = rating_lib.build_rank_context_map(rows)
        gm = [c for c in ctx.values() if c.rank_name == "グランドマスター"]
        master = [c for c in ctx.values() if c.rank_name == "マスター"]
        self.assertEqual(len(gm), GRANDMASTER_SLOTS)
        self.assertEqual(len(master), 40 - GRANDMASTER_SLOTS)

    def test_small_population_gm_capped_by_master_zone(self):
        """少人数でも上位5%相当だけをGMにし、マスターを残す"""
        rows = make_rows(30)  # マスター帯 = 10% = 3人
        ctx = rating_lib.build_rank_context_map(rows)
        gm = [c for c in ctx.values() if c.rank_name == "グランドマスター"]
        self.assertEqual(len(gm), 2)
        master = [c for c in ctx.values() if c.rank_name == "マスター"]
        self.assertEqual(len(master), 1)

    def test_emerald_rank_is_part_of_production_distribution(self):
        rows = make_rows(100)
        ctx = rating_lib.build_rank_context_map(rows)
        emerald = [c for c in ctx.values() if c.rank_name == "エメラルド"]
        self.assertEqual(len(emerald), 10)
        # 絵文字・色は運用で変わりうるので値は固定しない。
        # 「RANK_SPECS に1件だけ在り、絵文字と色が空でない」ことだけ保証する
        specs = [spec for spec in RANK_SPECS if spec[0] == "エメラルド"]
        self.assertEqual(len(specs), 1)
        _, emoji, color_hex = specs[0]
        self.assertTrue(emoji)
        self.assertTrue(color_hex.startswith("#"))

    def test_rank_distribution_sums_to_population(self):
        rows = make_rows(137)
        ctx = rating_lib.build_rank_context_map(rows)
        counts: dict[str, int] = {}
        for c in ctx.values():
            counts[c.rank_name] = counts.get(c.rank_name, 0) + 1
        self.assertEqual(sum(counts.values()), 137)
        # 配分比率の合計が1なので全員に行き渡る (取り残しゼロ)
        self.assertAlmostEqual(sum(SEASON_RANK_PERCENTAGES.values()), 1.0)

    def test_ordering_by_rating(self):
        """順位はレート降順 (同点はseason_wins, player_idで安定)"""
        rows = make_rows(50)
        ctx = rating_lib.build_rank_context_map(rows)
        by_position = sorted(ctx.values(), key=lambda c: c.position)
        ratings = [rows[c.player_id - 1]["rating"] for c in by_position]
        self.assertEqual(ratings, sorted(ratings, reverse=True))

    def test_ladder_specific_grandmaster_roles_do_not_expand_rank_specs(self):
        self.assertEqual(
            rating_lib.special_grandmaster_role_name("l13"),
            rating_lib.get_rank_role_name("グランドマスター"),
        )
        l9_role = rating_lib.get_rank_role_name("グランドマスター9")
        l9t_role = rating_lib.get_rank_role_name("グランドマスター9T")
        self.assertEqual(
            rating_lib.special_grandmaster_role_name("l9_cross"), l9_role,
        )
        self.assertEqual(
            rating_lib.special_grandmaster_role_name("l9_turn"), l9t_role,
        )
        self.assertIn(l9_role, rating_lib.all_rank_role_names())
        self.assertIn(l9t_role, rating_lib.all_rank_role_names())
        rank_specs = {row[0] for row in RANK_SPECS}
        self.assertNotIn("グランドマスター9", rank_specs)
        self.assertNotIn("グランドマスター9T", rank_specs)


class TestSeasonHalfResetProduction(unittest.IsolatedAsyncioTestCase):
    """式の写しではなく、本番DB処理を実行して検証する。"""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="werewolf-rating-test-")
        self.original_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self.temp_dir.name) / "rating.db")
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_pulls_actual_database_rows_toward_initial(self):
        guild_id = 1234
        rows = (
            (1, 1500, 1500, 10, 5),
            (2, 1901, 2000, 20, 12),
            (3, 1099, 1500, 7, 2),
        )
        async with database.connect_db() as db:
            await db.executemany(
                "INSERT INTO player_ratings "
                "(player_id, guild_id, rating, peak_rating, season_games, season_wins) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(pid, guild_id, rating, peak, games, wins)
                 for pid, rating, peak, games, wins in rows],
            )
            await db.commit()

        reset_id, affected = await database.season_half_reset(
            guild_id, executed_by=999, note="unit-test"
        )
        self.assertGreater(reset_id, 0)
        self.assertEqual(affected, 3)

        async with database.connect_db() as db:
            actual = await db.execute_fetchall(
                "SELECT player_id, rating, peak_rating, season_games, season_wins "
                "FROM player_ratings WHERE guild_id = ? ORDER BY player_id",
                (guild_id,),
            )
            snapshots = await db.execute_fetchall(
                "SELECT player_id, rating_before, rating_after FROM rating_snapshots "
                "WHERE season_reset_id = ? ORDER BY player_id",
                (reset_id,),
            )

        self.assertEqual(
            actual,
            [
                (1, 1500, 1500, 0, 0),
                (2, 1700, 2000, 0, 0),
                (3, 1299, 1500, 0, 0),
            ],
        )
        self.assertEqual(
            snapshots,
            [(1, 1500, 1500), (2, 1901, 1700), (3, 1099, 1299)],
        )

    async def test_reset_never_leaves_peak_below_current_rating(self):
        """初戦黒星でpeakが1500未満に確定した人でも、peak < 現在レートにしない。

        ハーフリセットは1500より下の人を1500側へ引き上げるため、
        peak=現在=1470 の人は 1485 へ上がり、そのままだと戦績カードに
        「1485 (最高 1470)」と矛盾が出る。
        """
        guild_id = 5555
        rows = (
            # (player_id, rating, peak, season_games, season_wins)
            (1, 1470, 1470, 1, 0),   # 初戦黒星のまま。引き上げでpeakを超える
            (2, 1300, 1500, 8, 2),   # 通常の下振れ。peakは据え置きのまま
            (3, 1900, 2000, 9, 6),   # 上位者。引き下げなのでpeakは動かない
        )
        async with database.connect_db() as db:
            await db.executemany(
                "INSERT INTO player_ratings "
                "(player_id, guild_id, rating, peak_rating, season_games, season_wins) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(pid, guild_id, rating, peak, games, wins)
                 for pid, rating, peak, games, wins in rows],
            )
            await db.commit()

        await database.season_half_reset(guild_id, executed_by=999)

        async with database.connect_db() as db:
            actual = await db.execute_fetchall(
                "SELECT player_id, rating, peak_rating FROM player_ratings "
                "WHERE guild_id = ? ORDER BY player_id",
                (guild_id,),
            )
        self.assertEqual(
            actual,
            [
                (1, 1485, 1485),   # peakを現在レートまで底上げ
                (2, 1400, 1500),   # 過去の最高値はそのまま残す
                (3, 1700, 2000),
            ],
        )
        for player_id, rating, peak in actual:
            self.assertGreaterEqual(
                peak, rating, f"player {player_id} の最高レートが現在を下回った",
            )

    async def test_no_players_is_noop(self):
        self.assertEqual(
            await database.season_half_reset(9876, executed_by=999),
            (0, 0),
        )

    async def test_one_boundary_resets_each_ladder_and_counts_people_once(self):
        guild_id = 4321
        async with database.connect_db() as db:
            await db.executemany(
                "INSERT INTO player_ratings "
                "(player_id, guild_id, ladder_id, rating, peak_rating, games, "
                "season_games, season_wins) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, guild_id, "l13", 1900, 1900, 10, 10, 6),
                    (1, guild_id, "l9_cross", 1700, 1700, 4, 4, 2),
                    (1, guild_id, "l9_turn", 1300, 1500, 3, 3, 1),
                ],
            )
            await db.commit()

        reset_id, affected = await database.season_half_reset(
            guild_id, executed_by=999,
        )
        self.assertEqual(affected, 1)
        async with database.connect_db() as db:
            current = await db.execute_fetchall(
                "SELECT ladder_id, rating, season_games FROM player_ratings "
                "WHERE guild_id=? ORDER BY ladder_id",
                (guild_id,),
            )
            snapshots = await db.execute_fetchall(
                "SELECT ladder_id, rating_before, rating_after FROM rating_snapshots "
                "WHERE season_reset_id=? ORDER BY ladder_id",
                (reset_id,),
            )
        self.assertEqual(
            current,
            [("l13", 1700, 0), ("l9_cross", 1600, 0), ("l9_turn", 1400, 0)],
        )
        self.assertEqual(
            snapshots,
            [
                ("l13", 1900, 1700),
                ("l9_cross", 1700, 1600),
                ("l9_turn", 1300, 1400),
            ],
        )



class TestLadderSchema(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="werewolf-ladder-schema-")
        self.original_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self.temp_dir.name) / "ladder.db")
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_snapshot_keeps_id_pk_and_is_unique_per_ladder(self):
        async with database.connect_db() as db:
            reset = await db.execute(
                "INSERT INTO season_resets (guild_id, executed_by) VALUES (1, 999)"
            )
            reset_id = int(reset.lastrowid)
            await db.executemany(
                "INSERT INTO rating_snapshots "
                "(season_reset_id, player_id, guild_id, ladder_id, "
                "rating_before, rating_after) VALUES (?, 10, 1, ?, 1600, 1550)",
                [(reset_id, "l13"), (reset_id, "l9_cross")],
            )
            with self.assertRaises(database.aiosqlite.IntegrityError):
                await db.execute(
                    "INSERT INTO rating_snapshots "
                    "(season_reset_id, player_id, guild_id, ladder_id, "
                    "rating_before, rating_after) "
                    "VALUES (?, 10, 1, 'l13', 1700, 1600)",
                    (reset_id,),
                )
            table_info = await db.execute_fetchall(
                "PRAGMA table_info(rating_snapshots)"
            )
            await db.rollback()
        id_info = next(row for row in table_info if row[1] == "id")
        self.assertEqual(id_info[5], 1)


if __name__ == "__main__":
    unittest.main()
