"""レーティング制度の長期シミュレーション (数理モデル版)

simulate_games.py が「実エンジンを偽Discordで回す」のに対し、こちらは
レート計算 (rating.calculate_game_results) とシーズンリセットだけを抜き出し、
百人〜数百人規模 × 数シーズンを高速に回して制度設計を検証する。

モデル:
- 各プレイヤーは skill (実力, 正規分布) と activity (活動量, 対数正規) を持つ
- 1ゲーム = activity重み付きで13人を抽選し、役職をランダム配布
- 勝敗は「基本村勝率」を両陣営の平均skill差で補正したロジスティックで決定
  (固定プール制ではレートが勝敗に影響しないため、skillは勝率の偏りを作るだけ)
- SEASON_LENGTH_DAYS ごとに database.season_half_reset と同じ式でリセット

使い方:
  python simulate_rating.py --players 150 --games-per-day 8 \\
      --season-days 90 --seasons 3 --village-winrate 0.45 --seed 1
  python simulate_rating.py --compare-cadence   # 月次 vs 四半期の比較レポート
"""
from __future__ import annotations

import argparse
import math
import statistics
from dataclasses import dataclass, field
from random import Random

import rating as rating_lib
from config import INITIAL_RATING, MAX_PLAYERS, RATING_FLOOR, SEASON_RANK_MIN_GAMES, Team

WOLF_SIDE_COUNT = 4  # 人狼3+狂人1


@dataclass
class SimPlayer:
    player_id: int
    skill: float
    activity: float
    rating: int = INITIAL_RATING
    games: int = 0
    wins: int = 0
    season_games: int = 0
    season_wins: int = 0
    reset_loss_total: int = 0  # シーズンリセットで失った累計ポイント (>0 = 失った)
    history: list[int] = field(default_factory=list)


def half_reset(rating: int) -> int:
    """database.season_half_reset と同一式"""
    return INITIAL_RATING + (rating - INITIAL_RATING) // 2


def simulate(
    *,
    players: int,
    games_per_day: float,
    season_days: int,
    seasons: int,
    village_winrate: float,
    skill_effect: float,
    seed: int,
    min_rank_games: int = SEASON_RANK_MIN_GAMES,
    rating_floor: int | None = RATING_FLOOR,
    quiet: bool = False,
) -> dict:
    """rating_lib の確定条件・レート下限をパッチして実行する。

    rating_floor=None で「下限なし」の比較ベースラインを再現できる。
    パッチは finally で必ず復元する (途中例外でも他の実行を汚染しない)。
    """
    if players < MAX_PLAYERS:
        raise ValueError(f"players must be at least {MAX_PLAYERS}")
    if games_per_day < 0 or not math.isfinite(games_per_day):
        raise ValueError("games_per_day must be a finite non-negative number")
    if season_days <= 0:
        raise ValueError("season_days must be positive")
    if seasons <= 0:
        raise ValueError("seasons must be positive")
    if not 0.0 < village_winrate < 1.0:
        raise ValueError("village_winrate must be greater than 0 and less than 1")
    if not math.isfinite(skill_effect):
        raise ValueError("skill_effect must be finite")
    if min_rank_games < 0:
        raise ValueError("min_rank_games must be non-negative")

    original_min_games = rating_lib.SEASON_RANK_MIN_GAMES
    original_floor = rating_lib.RATING_FLOOR
    rating_lib.SEASON_RANK_MIN_GAMES = min_rank_games
    rating_lib.RATING_FLOOR = rating_floor if rating_floor is not None else -(10 ** 9)
    try:
        return _simulate_inner(
            players=players,
            games_per_day=games_per_day,
            season_days=season_days,
            seasons=seasons,
            village_winrate=village_winrate,
            skill_effect=skill_effect,
            seed=seed,
            min_rank_games=min_rank_games,
            rating_floor=rating_floor,
            quiet=quiet,
        )
    finally:
        rating_lib.SEASON_RANK_MIN_GAMES = original_min_games
        rating_lib.RATING_FLOOR = original_floor


def _simulate_inner(
    *,
    players: int,
    games_per_day: float,
    season_days: int,
    seasons: int,
    village_winrate: float,
    skill_effect: float,
    seed: int,
    min_rank_games: int,
    rating_floor: int | None,
    quiet: bool,
) -> dict:
    rng = Random(seed)
    pop = [
        SimPlayer(
            player_id=i + 1,
            skill=rng.gauss(0.0, 1.0),
            activity=rng.lognormvariate(0.0, 0.8),
        )
        for i in range(players)
    ]
    base_logit = math.log(village_winrate / (1.0 - village_winrate))

    games_per_season = round(games_per_day * season_days)
    season_summaries = []
    negative_rating_events = 0
    min_rating_ever = INITIAL_RATING
    floor_hits = 0          # フロアで損失がクランプされた回数
    floor_injected = 0      # フロアが経済に注入した合計ポイント (ゼロサム破れの量)

    for season in range(1, seasons + 1):
        for p in pop:
            p.season_games = 0
            p.season_wins = 0

        for _ in range(games_per_season):
            # activity重み付きで13人抽選 (重複なし)
            table: list[SimPlayer] = []
            candidates = pop[:]
            weights = [p.activity for p in candidates]
            for _ in range(MAX_PLAYERS):
                total = sum(weights)
                r = rng.random() * total
                acc = 0.0
                for idx, w in enumerate(weights):
                    acc += w
                    if acc >= r:
                        break
                table.append(candidates.pop(idx))
                weights.pop(idx)

            rng.shuffle(table)
            wolf_side = table[:WOLF_SIDE_COUNT]
            village_side = table[WOLF_SIDE_COUNT:]

            skill_gap = (
                statistics.fmean(p.skill for p in village_side)
                - statistics.fmean(p.skill for p in wolf_side)
            )
            p_village = 1.0 / (1.0 + math.exp(-(base_logit + skill_effect * skill_gap)))
            village_won = rng.random() < p_village
            winners = village_side if village_won else wolf_side
            winner_ids = {p.player_id for p in winners}

            calc_input = [
                {"player_id": p.player_id, "rating": p.rating, "won": p in winners}
                for p in table
            ]
            results = rating_lib.calculate_game_results(
                calc_input,
                winner_team=Team.VILLAGE if village_won else Team.WOLF,
            )
            by_id = {p.player_id: p for p in table}
            for r_ in results:
                p = by_id[r_["player_id"]]
                # フロアは rating_lib 側で適用済み。クランプ量を検出して集計する
                unclamped = r_["rating_before"] + r_["elo_delta"] + r_["bonus"]
                if r_["rating_after"] > unclamped:
                    floor_hits += 1
                    floor_injected += r_["rating_after"] - unclamped
                p.rating = r_["rating_after"]
                p.games += 1
                p.season_games += 1
                if p.player_id in winner_ids:
                    p.wins += 1
                    p.season_wins += 1
                if p.rating < 0:
                    negative_rating_events += 1
                min_rating_ever = min(min_rating_ever, p.rating)

        # ---- シーズン末の集計 ----
        ratings = sorted((p.rating for p in pop), reverse=True)
        ranked = [p for p in pop if p.season_games >= min_rank_games]
        rank_rows = [
            {
                "player_id": p.player_id,
                "rating": p.rating,
                "season_games": p.season_games,
                "season_wins": p.season_wins,
            }
            for p in pop
        ]
        ctx = rating_lib.build_rank_context_map(rank_rows)
        gm_players = [pid for pid, c in ctx.items() if c.rank_name == "グランドマスター"]

        def corr(xs: list[float], ys: list[float]) -> float:
            if len(set(xs)) < 2 or len(set(ys)) < 2:
                return 0.0
            return statistics.correlation(xs, ys)

        season_summaries.append({
            "season": season,
            "mean": round(statistics.fmean(ratings), 1),
            "median": ratings[len(ratings) // 2],
            "p90": ratings[int(len(ratings) * 0.10)],
            "p10": ratings[int(len(ratings) * 0.90)],
            "max": ratings[0],
            "min": ratings[-1],
            "below_initial_pct": round(
                100 * sum(1 for r in ratings if r < INITIAL_RATING) / len(ratings), 1
            ),
            "ranked_pct": round(100 * len(ranked) / len(pop), 1),
            "avg_games_per_player": round(statistics.fmean(p.season_games for p in pop), 1),
            "corr_rating_skill": round(corr([p.skill for p in pop], [float(p.rating) for p in pop]), 3),
            "corr_rating_games": round(corr([float(p.season_games) for p in pop], [float(p.rating) for p in pop]), 3),
            "gm_avg_skill_pct": round(
                100 * statistics.fmean(
                    sum(1 for q in pop if q.skill < next(p for p in pop if p.player_id == pid).skill) / len(pop)
                    for pid in gm_players
                ), 1
            ) if gm_players else None,
        })

        # ---- ハーフリセット (最終シーズン後は実施しない) ----
        if season < seasons:
            for p in pop:
                new_rating = half_reset(p.rating)
                if p.rating > new_rating:
                    p.reset_loss_total += p.rating - new_rating
                p.rating = new_rating

    high_activity = [p for p in pop if p.activity >= statistics.median(q.activity for q in pop)]
    low_activity = [p for p in pop if p not in high_activity]
    avg_skill_players = [p for p in pop if abs(p.skill) < 0.5]
    avg_skill_active = [p for p in avg_skill_players if p.games >= statistics.fmean(q.games for q in pop)]
    avg_skill_idle = [p for p in avg_skill_players if p.games < statistics.fmean(q.games for q in pop)]

    result = {
        "config": {
            "players": players,
            "games_per_day": games_per_day,
            "season_days": season_days,
            "seasons": seasons,
            "village_winrate": village_winrate,
            "skill_effect": skill_effect,
            "seed": seed,
            "min_rank_games": min_rank_games,
            "rating_floor": rating_floor,
        },
        "seasons": season_summaries,
        "min_rating_ever": min_rating_ever,
        "negative_rating_events": negative_rating_events,
        "floor_hits": floor_hits,
        "floor_injected": floor_injected,
        "avg_reset_loss_top10pct": round(
            statistics.fmean(
                p.reset_loss_total
                for p in sorted(pop, key=lambda q: -q.rating)[: max(1, players // 10)]
            ), 1
        ),
        "activity_premium": {
            "high_activity_mean": round(statistics.fmean(p.rating for p in high_activity), 1),
            "low_activity_mean": round(statistics.fmean(p.rating for p in low_activity), 1),
        },
        "avg_skill_incentive": {
            "active_mean": round(statistics.fmean(p.rating for p in avg_skill_active), 1) if avg_skill_active else None,
            "idle_mean": round(statistics.fmean(p.rating for p in avg_skill_idle), 1) if avg_skill_idle else None,
        },
    }

    if not quiet:
        _print_report(result)
    return result


def _print_report(result: dict) -> None:
    cfg = result["config"]
    print(f"=== config: {cfg} ===")
    for s in result["seasons"]:
        print(
            f"  S{s['season']}: mean={s['mean']} median={s['median']} "
            f"p90={s['p90']} p10={s['p10']} max={s['max']} min={s['min']} "
            f"<{INITIAL_RATING}: {s['below_initial_pct']}% | ランク確定率: {s['ranked_pct']}% "
            f"(平均{s['avg_games_per_player']}戦) | corr(レート,実力)={s['corr_rating_skill']} "
            f"corr(レート,試合数)={s['corr_rating_games']} | GM平均実力百分位={s['gm_avg_skill_pct']}"
        )
    print(f"  最低到達レート: {result['min_rating_ever']} / 負レート発生: {result['negative_rating_events']}回")
    if cfg.get("rating_floor") is not None:
        total_games = round(cfg["games_per_day"] * cfg["season_days"] * cfg["seasons"])
        print(
            f"  フロア({cfg['rating_floor']})発動: {result['floor_hits']}回 / "
            f"全{total_games}ゲーム / 注入ポイント計{result['floor_injected']}pt"
        )
    print(f"  上位10%がリセットで失った累計: 平均{result['avg_reset_loss_top10pct']}pt")
    ap = result["activity_premium"]
    print(f"  活動量プレミアム: 高活動層平均{ap['high_activity_mean']} vs 低活動層平均{ap['low_activity_mean']}")
    ai = result["avg_skill_incentive"]
    print(f"  平均実力帯のやり込み差: 多くやる人{ai['active_mean']} vs あまりやらない人{ai['idle_mean']}")
    print()


def compare_cadence(
    players: int,
    games_per_day: float,
    seed: int,
    *,
    min_rank_games: int = SEASON_RANK_MIN_GAMES,
    rating_floor: int | None = RATING_FLOOR,
) -> None:
    """同じ9ヶ月間を「毎月リセット」と「3ヶ月リセット」で比較する"""
    print("#" * 70)
    print(f"# リセット周期比較 (同一{players}人・{games_per_day}戦/日・9ヶ月間)")
    print("#" * 70)
    for label, season_days, seasons in (
        ("3ヶ月リセット x3", 90, 3),
        ("毎月リセット x9", 30, 9),
    ):
        print(f"--- {label} ---")
        simulate(
            players=players,
            games_per_day=games_per_day,
            season_days=season_days,
            seasons=seasons,
            village_winrate=0.45,
            skill_effect=0.8,
            seed=seed,
            min_rank_games=min_rank_games,
            rating_floor=rating_floor,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="レート制度の長期シミュレーション")
    parser.add_argument("--players", type=int, default=150)
    parser.add_argument("--games-per-day", type=float, default=8.0)
    parser.add_argument("--season-days", type=int, default=90)
    parser.add_argument("--seasons", type=int, default=3)
    parser.add_argument("--village-winrate", type=float, default=0.45,
                        help="skill差ゼロのときの村陣営勝率")
    parser.add_argument("--skill-effect", type=float, default=0.8,
                        help="陣営の平均skill差1.0あたりのロジット補正")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--min-rank-games", type=int, default=SEASON_RANK_MIN_GAMES,
                        help="相対ランク確定に必要なシーズン試合数")
    parser.add_argument("--rating-floor", type=int, default=RATING_FLOOR,
                        help=f"レート下限 (既定: 本番設定の{RATING_FLOOR}。0以下を指定すると下限なし)")
    parser.add_argument("--compare-cadence", action="store_true",
                        help="毎月 vs 3ヶ月リセットの比較レポート")
    args = parser.parse_args()

    if args.compare_cadence:
        compare_cadence(
            args.players,
            args.games_per_day,
            args.seed,
            min_rank_games=args.min_rank_games,
            rating_floor=args.rating_floor if args.rating_floor > 0 else None,
        )
        return

    simulate(
        players=args.players,
        games_per_day=args.games_per_day,
        season_days=args.season_days,
        seasons=args.seasons,
        village_winrate=args.village_winrate,
        skill_effect=args.skill_effect,
        seed=args.seed,
        min_rank_games=args.min_rank_games,
        rating_floor=args.rating_floor if args.rating_floor > 0 else None,
    )


if __name__ == "__main__":
    main()
