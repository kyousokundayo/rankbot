"""レーティング/ランクシステム"""
from __future__ import annotations

import math
from dataclasses import dataclass

from config import (
    GRANDMASTER_PERCENTAGE,
    GRANDMASTER_SLOTS,
    RANK_ROLE_PREFIX,
    RANK_SPECS,
    RATING_FLOOR,
    SEASON_RANK_MIN_GAMES,
    SEASON_RANK_PERCENTAGES,
    VILLAGE_WIN_FIXED_POOL,
    WIN_PARTICIPATION_BONUS,
    WOLF_WIN_FIXED_POOL,
    Team,
)


@dataclass(frozen=True)
class RankContext:
    player_id: int
    rank_name: str
    emoji: str
    color: int
    percentile: float | None
    position: int | None
    active_count: int
    season_games: int
    season_wins: int
    provisional: bool


RANK_INDEX = {name: idx for idx, (name, _, _) in enumerate(RANK_SPECS)}
RANK_SPEC_MAP = {
    name: {"emoji": emoji, "color": int(color_hex.lstrip("#"), 16)}
    for name, emoji, color_hex in RANK_SPECS
}


# ============================================================
# レーティング計算
# ============================================================

def _remainder_start(player_ids: list[int]) -> int:
    if not player_ids:
        return 0
    seed = sum(pid * 31 for pid in player_ids)
    return seed % len(player_ids)


def _split_pool_evenly(
    player_ids: list[int],
    total: int,
    *,
    negative: bool = False,
) -> dict[int, int]:
    """整数プールをできるだけ均等に配分する"""
    if not player_ids:
        return {}

    count = len(player_ids)
    base, remainder = divmod(total, count)
    sign = -1 if negative else 1
    allocations = {pid: sign * base for pid in player_ids}

    if remainder:
        start = _remainder_start(player_ids)
        for offset in range(remainder):
            pid = player_ids[(start + offset) % count]
            allocations[pid] += sign

    return allocations


def calculate_game_results(
    player_data: list[dict],
    *,
    winner_team: Team | str,
) -> list[dict]:
    """
    6:4 環境を前提にした固定プール方式。

    狼勝ち:
      本体 +60 / -60 を勝敗人数で配分し、勝者へ参加ボーナス
    村勝ち:
      本体 +90 / -90 を勝敗人数で配分し、勝者へ参加ボーナス
    """
    winners = [p for p in player_data if p["won"]]
    losers = [p for p in player_data if not p["won"]]

    if not winners or not losers:
        return [{
            "player_id": p["player_id"],
            "rating_before": p["rating"],
            "rating_after": p["rating"],
            "delta": 0,
            "elo_delta": 0,
            "bonus": 0,
        } for p in player_data]

    winner_ids = [p["player_id"] for p in winners]
    loser_ids = [p["player_id"] for p in losers]
    winner_value = winner_team.value if isinstance(winner_team, Team) else str(winner_team)
    if winner_value == Team.WOLF.value or winner_value == Team.WOLF.name:
        pool = WOLF_WIN_FIXED_POOL
    elif winner_value == Team.VILLAGE.value or winner_value == Team.VILLAGE.name:
        pool = VILLAGE_WIN_FIXED_POOL
    else:
        raise ValueError(f"unknown winner_team: {winner_team}")
    winner_elo_map = _split_pool_evenly(winner_ids, pool)
    loser_elo_map = _split_pool_evenly(loser_ids, pool, negative=True)

    results = []
    for p in player_data:
        pid = p["player_id"]
        if p["won"]:
            elo_delta = winner_elo_map[pid]
            bonus = WIN_PARTICIPATION_BONUS
        else:
            elo_delta = loser_elo_map[pid]
            bonus = 0

        # レート下限: RATING_FLOOR より下には落とさない
        # (deltaは実際に適用された変動量。elo_delta/bonusは名目値のまま残す)
        # 既にフロア未満のレート (データ移入やフロア引き上げ時) は
        # 敗北で引き上げず「据え置き」とし、勝利でのみ通常どおり上がる
        effective_floor = min(p["rating"], RATING_FLOOR)
        rating_after = max(effective_floor, p["rating"] + elo_delta + bonus)
        delta = rating_after - p["rating"]
        results.append({
            "player_id": pid,
            "rating_before": p["rating"],
            "rating_after": rating_after,
            "delta": delta,
            "elo_delta": elo_delta,
            "bonus": bonus,
        })

    return results


# ============================================================
# シーズン相対ランク
# ============================================================

def _largest_remainder_counts(total: int, percentages: dict[str, float]) -> dict[str, int]:
    raw = {name: total * ratio for name, ratio in percentages.items()}
    counts = {name: int(value) for name, value in raw.items()}
    remainder = total - sum(counts.values())
    if remainder > 0:
        order = sorted(
            raw.items(),
            key=lambda item: (item[1] - counts[item[0]], RANK_INDEX[item[0]]),
            reverse=True,
        )
        for idx in range(remainder):
            counts[order[idx][0]] += 1
    return counts


def build_rank_context_map(player_rows: list[dict]) -> dict[int, RankContext]:
    """
    レート順の相対ランクを返す。

    母集団は**通算**の試合数で決める。シーズンのハーフリセットは
    `season_games` をゼロにするので、今季の試合数を条件にすると
    リセット直後は全員が母集団から外れ、レートを保持しているのに
    ランクだけ暫定ブロンズへ落ちてしまう。
    レート変換 (1500 + (r - 1500) / 2) は単調増加でレート順を保つため、
    通算で数えればリセットをまたいでランク分布がそのまま維持される。
    インフレ分だけが圧縮され、順位関係は変わらない。

    player_rows:
      [{"player_id", "rating", "games", "season_games", "season_wins"}]
    """
    contexts: dict[int, RankContext] = {}
    active_rows = [
        row for row in player_rows
        if row.get("games", 0) >= SEASON_RANK_MIN_GAMES
    ]
    active_rows.sort(
        key=lambda row: (-row["rating"], -row.get("season_wins", 0), row["player_id"])
    )
    active_count = len(active_rows)

    if active_rows:
        base_counts = _largest_remainder_counts(active_count, SEASON_RANK_PERCENTAGES)
        master_zone = base_counts["マスター"]
        gm_target = math.ceil(active_count * GRANDMASTER_PERCENTAGE)
        gm_count = min(GRANDMASTER_SLOTS, master_zone, gm_target)
        master_count = max(master_zone - gm_count, 0)

        segments = [
            ("グランドマスター", gm_count),
            ("マスター", master_count),
            ("ダイヤ", base_counts["ダイヤ"]),
            ("エメラルド", base_counts["エメラルド"]),
            ("プラチナ", base_counts["プラチナ"]),
            ("ゴールド", base_counts["ゴールド"]),
            ("シルバー", base_counts["シルバー"]),
            ("ブロンズ", base_counts["ブロンズ"]),
            ("アイアン", base_counts["アイアン"]),
        ]

        cursor = 0
        for rank_name, count in segments:
            for _ in range(count):
                if cursor >= active_count:
                    break
                row = active_rows[cursor]
                spec = RANK_SPEC_MAP[rank_name]
                percentile = ((cursor + 1) / active_count) * 100
                contexts[row["player_id"]] = RankContext(
                    player_id=row["player_id"],
                    rank_name=rank_name,
                    emoji=spec["emoji"],
                    color=spec["color"],
                    percentile=percentile,
                    position=cursor + 1,
                    active_count=active_count,
                    season_games=row.get("season_games", 0),
                    season_wins=row.get("season_wins", 0),
                    provisional=False,
                )
                cursor += 1

    # 通算3戦未満 (SEASON_RANK_MIN_GAMES未満) のプレイヤー:
    # 相対評価の母集団 (枠の消費) には入れないが、1戦目からランクを表示する。
    # アクティブ勢の順位に「仮スロット」した位置から同じ帯割りでランクを求める。
    # アクティブ勢が1人もいない場合と、まだ1戦もしていない人はブロンズ暫定
    # (完全な新規だけがここに来る。初心者卓へ入れる状態にしておく)
    segment_bounds: list[tuple[str, int]] = []
    if active_rows:
        cursor = 0
        for rank_name, count in segments:
            cursor += count
            if count > 0:
                segment_bounds.append((rank_name, cursor))

    def _provisional_rank_for(row: dict) -> str:
        if not active_rows or row.get("games", 0) <= 0:
            return "ブロンズ"
        sort_key = (-row["rating"], -row.get("season_wins", 0), row["player_id"])
        virtual_position = 1 + sum(
            1 for active in active_rows
            if (-active["rating"], -active.get("season_wins", 0), active["player_id"]) < sort_key
        )
        for rank_name, upper in segment_bounds:
            if virtual_position <= upper:
                # グランドマスターはアクティブ上位5%相当・最大13人の専有枠。
                # 暫定表示がGMを名乗れないようマスター止まりにする
                return "マスター" if rank_name == "グランドマスター" else rank_name
        return segment_bounds[-1][0] if segment_bounds else "ブロンズ"

    for row in player_rows:
        if row["player_id"] in contexts:
            continue
        rank_name = _provisional_rank_for(row)
        spec = RANK_SPEC_MAP[rank_name]
        contexts[row["player_id"]] = RankContext(
            player_id=row["player_id"],
            rank_name=rank_name,
            emoji=spec["emoji"],
            color=spec["color"],
            percentile=None,
            position=None,
            active_count=active_count,
            season_games=row.get("season_games", 0),
            season_wins=row.get("season_wins", 0),
            provisional=True,
        )

    return contexts


def rank_order_value(rank_name: str) -> int:
    return RANK_INDEX[rank_name]


def is_promoted(old_rank_name: str, new_rank_name: str) -> bool:
    return rank_order_value(new_rank_name) > rank_order_value(old_rank_name)


def is_demoted(old_rank_name: str, new_rank_name: str) -> bool:
    return rank_order_value(new_rank_name) < rank_order_value(old_rank_name)


def get_rank_emoji_by_name(rank_name: str) -> str:
    return RANK_SPEC_MAP[rank_name]["emoji"]


def get_rank_color_by_name(rank_name: str) -> int:
    return RANK_SPEC_MAP[rank_name]["color"]


def get_rank_role_name(rank_name: str) -> str:
    return f"{RANK_ROLE_PREFIX}{rank_name}"


def all_rank_role_names() -> list[str]:
    return [get_rank_role_name(name) for name, _, _ in RANK_SPECS]


def all_rank_role_specs() -> list[tuple[str, int]]:
    return [(get_rank_role_name(name), get_rank_color_by_name(name)) for name, _, _ in RANK_SPECS]


