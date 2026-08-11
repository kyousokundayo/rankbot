"""レーティング/ランクシステム"""
from __future__ import annotations

import math
from dataclasses import dataclass

import config as config_lib
from config import (
    BONUS_FINAL_DAY_THRESHOLD,
    BONUS_FINAL_DAY_WOLF,
    BONUS_GUARD_SUCCESS,
    BONUS_NIGHT1_SEER_KILL,
    BONUS_WOLF_EXECUTION_VOTE,
    BONUS_WOLF_GUESS_DEATH_CAUSES,
    BONUS_WOLF_GUESS_EARLY_MAX_DAY,
    BONUS_WOLF_GUESS_EARLY_MULTIPLIER,
    BONUS_WOLF_GUESS_HIT,
    BONUS_WOLF_GUESS_SLOTS,
    GRANDMASTER_PERCENTAGE,
    GRANDMASTER_SLOTS,
    RANK_BAND,
    RANK_BAND_COEFFICIENT_STEP_PERCENT,
    RANK_ROLE_PREFIX,
    RANK_SPECS,
    RATING_FLOOR,
    SEASON_RANK_MIN_GAMES,
    SEASON_RANK_PERCENTAGES,
    WIN_PARTICIPATION_BONUS,
    Role,
    Team,
)


DEFAULT_VARIANT_ID = config_lib.DEFAULT_VARIANT_ID
DEFAULT_LADDER_ID = config_lib.DEFAULT_LADDER_ID
LEGACY_NINE_GRANDMASTER_ROLE_NAME = "グランドマスター（9人村）"


def ladder_id_for_variant(variant_id: str) -> str:
    """永続化する変種IDに対応するラダーIDを返す。"""
    try:
        return str(config_lib.VARIANT_TO_LADDER[variant_id])
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unknown variant_id: {variant_id}") from exc


def grandmaster_slots_for_ladder(ladder_id: str) -> int:
    """configに定義されたラダーごとのGM上限を返す。"""
    try:
        slots = int(config_lib.LADDER_DEFINITIONS[ladder_id].grandmaster_slots)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"unknown ladder_id: {ladder_id}") from exc
    if slots <= 0:
        raise ValueError("grandmaster_slots must be positive")
    return slots


def special_grandmaster_role_name(ladder_id: str) -> str:
    """ラダー固有のGM Discordロール名を返す。

    l13 / 9人クロストーク / 9人ターン制で、それぞれ指定された
    ``グランドマスター`` / ``グランドマスター9`` /
    ``グランドマスター9T`` を使う。
    通常ランク9段の ``RANK_SPECS`` 自体は増やさない。
    """
    try:
        rank_name = str(config_lib.LADDER_DEFINITIONS[ladder_id].grandmaster_role_name)
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unknown ladder_id: {ladder_id}") from exc
    return get_rank_role_name(rank_name)


def _special_grandmaster_role_color(ladder_id: str) -> int:
    try:
        return int(config_lib.LADDER_DEFINITIONS[ladder_id].grandmaster_role_color)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"unknown ladder_id: {ladder_id}") from exc


def resolve_variant_rating_parameters(
    variant_id: str = DEFAULT_VARIANT_ID,
    *,
    village_win_pool: int | None = None,
    wolf_win_pool: int | None = None,
    wolf_guess_slots: int | None = None,
    final_day_threshold: int | None = None,
) -> dict[str, int]:
    """変種ごとの精算値を検証し、未指定値を確定する。

    呼び出し側が卓定義の値を渡せばそれを優先する。省略時は既存の
    13人クロストークと同じ値になり、旧APIの挙動を維持する。
    """
    definition = config_lib.get_variant_definition(variant_id)
    defaults = {
        "village_win_pool": int(definition.village_win_pool),
        "wolf_win_pool": int(definition.wolf_win_pool),
        "wolf_guess_slots": int(definition.wolf_guess_slots),
        "final_day_threshold": int(definition.final_day_threshold),
    }
    resolved = {
        "village_win_pool": defaults["village_win_pool"]
        if village_win_pool is None else village_win_pool,
        "wolf_win_pool": defaults["wolf_win_pool"]
        if wolf_win_pool is None else wolf_win_pool,
        "wolf_guess_slots": defaults["wolf_guess_slots"]
        if wolf_guess_slots is None else wolf_guess_slots,
        "final_day_threshold": defaults["final_day_threshold"]
        if final_day_threshold is None else final_day_threshold,
    }
    for key, value in resolved.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{key} must be a positive integer")
    return resolved


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


def _band_median(players: list[dict]) -> int | None:
    """
    陣営の代表となる卓帯を返す。偶数人数は下位側を採用する
    (room_runner.build_rank_bucket と同じ規則に揃える)。

    1人でもランクが取れなければ None を返し、呼び出し側は補正を諦める。
    ランク取得失敗時に片側だけ歪んだ係数が掛かるのを防ぐため。
    """
    bands: list[int] = []
    for player in players:
        band = RANK_BAND.get(player.get("rank_name") or "")
        if band is None:
            return None
        bands.append(band)
    if not bands:
        return None
    bands.sort()
    return bands[(len(bands) - 1) // 2]


def band_coefficient_percent(
    wolf_players: list[dict],
    village_players: list[dict],
    *,
    wolf_won: bool,
) -> int:
    """
    プールへ掛ける係数を百分率で返す (100 = 等倍)。

    格上の陣営が勝つと減り (最小80)、格下の陣営が勝つと増える (最大120)。
    プール全体へ掛けるので、勝者と敗者が同じプールから出るゼロサム性は保たれる。
    """
    wolf_band = _band_median(wolf_players)
    village_band = _band_median(village_players)
    if wolf_band is None or village_band is None:
        return 100

    delta = wolf_band - village_band
    step = -delta if wolf_won else delta
    step = max(-2, min(2, step))
    return 100 + step * RANK_BAND_COEFFICIENT_STEP_PERCENT


def calculate_game_results(
    player_data: list[dict],
    *,
    winner_team: Team | str,
    variant_id: str = DEFAULT_VARIANT_ID,
    village_win_pool: int | None = None,
    wolf_win_pool: int | None = None,
) -> list[dict]:
    """
    変種定義ごとの固定プール方式。

    既定の13人クロストークでは、狼勝ち120・村勝ち180の本体を
    勝敗人数で配分し、勝者へ参加ボーナスを加える。別変種はconfigの
    プール値を使い、いずれも本体部分のゼロサム性を保つ。

    player_data に "rank_name" (試合時表示ランク) があれば、陣営ごとの
    卓帯の中央値差から係数 (0.8〜1.2) をプールへ掛ける。無ければ等倍。
    """
    parameters = resolve_variant_rating_parameters(
        variant_id,
        village_win_pool=village_win_pool,
        wolf_win_pool=wolf_win_pool,
    )
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
        wolf_won = True
        pool = parameters["wolf_win_pool"]
    elif winner_value == Team.VILLAGE.value or winner_value == Team.VILLAGE.name:
        wolf_won = False
        pool = parameters["village_win_pool"]
    else:
        raise ValueError(f"unknown winner_team: {winner_team}")

    # 勝敗と勝利陣営が決まれば各人の陣営は一意に決まる。
    # 係数はプールへ掛ける (勝者側だけに掛けるとゼロサムが崩れる)
    coefficient = band_coefficient_percent(
        winners if wolf_won else losers,
        losers if wolf_won else winners,
        wolf_won=wolf_won,
    )
    pool = pool * coefficient // 100

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
# プレイボーナス (勝敗とは別枠の非ゼロサム加点)
# ============================================================

def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _index_records(player_records: list[dict]) -> dict[int, dict]:
    by_id: dict[int, dict] = {}
    for record in player_records or []:
        if not isinstance(record, dict):
            continue
        player_id = _as_int(record.get("player_id"))
        if player_id is not None:
            by_id[player_id] = record
    return by_id


def count_wolf_guess_hits(
    player_records: list[dict],
    facts: dict | None,
    *,
    wolf_guess_slots: int = BONUS_WOLF_GUESS_SLOTS,
) -> dict[int, int]:
    """3狼提出の的中数を player_id -> 的中数 で返す。

    **提出した人は的中0でもキーを持つ** (統計の分母になるため)。
    ボーナス計算と「3狼予想の的中率」の両方から使うので、
    誰の提出を有効とみなすかの判定はここへ集約する。
    """
    if isinstance(wolf_guess_slots, bool) or not isinstance(wolf_guess_slots, int) or wolf_guess_slots <= 0:
        raise ValueError("wolf_guess_slots must be a positive integer")
    by_id = _index_records(player_records)
    facts = facts or {}
    wolf_ids = {
        player_id for player_id, record in by_id.items()
        if record.get("role") == Role.WEREWOLF.value
    }
    hits: dict[int, int] = {}
    for raw_id, raw_guess in (facts.get("wolf_guesses") or {}).items():
        guesser_id = _as_int(raw_id)
        record = by_id.get(guesser_id) if guesser_id is not None else None
        if record is None or record.get("team") != Team.VILLAGE.value:
            continue
        if record.get("death_cause") not in BONUS_WOLF_GUESS_DEATH_CAUSES:
            continue
        if _as_int(record.get("died_on_day")) is None:
            continue
        guessed = {
            value for value in (_as_int(v) for v in (raw_guess or []))
            if value is not None
        }
        hits[guesser_id] = min(len(guessed & wolf_ids), wolf_guess_slots)
    return hits


def calculate_play_bonuses(
    player_records: list[dict],
    facts: dict | None,
    *,
    wolf_guess_slots: int = BONUS_WOLF_GUESS_SLOTS,
    final_day_threshold: int = BONUS_FINAL_DAY_THRESHOLD,
) -> dict[int, int]:
    """
    1試合ぶんのプレイボーナスを player_id -> 合計点 で返す。

    player_records:
      精算キューへ積んだ参加者情報
      ({"player_id", "role", "team", "won", "died_on_day", "death_cause"})
    facts:
      同じく精算キューへ積んだ試合中の事実。項目が欠けていればその加点が
      入らないだけで、精算そのものは止めない (付加統計の保存失敗で
      勝敗とレートの精算を止めないのと同じ方針)。

    本体プールと違い**非ゼロサム**なので、必ず別枠で加算し履歴にも分けて残す。
    """
    if (
        isinstance(final_day_threshold, bool)
        or not isinstance(final_day_threshold, int)
        or final_day_threshold <= 0
    ):
        raise ValueError("final_day_threshold must be a positive integer")
    bonuses: dict[int, int] = {}
    by_id = _index_records(player_records)
    if not by_id:
        return bonuses

    def add(player_id: int | None, points: int) -> None:
        if not points or player_id is None or player_id not in by_id:
            return
        bonuses[player_id] = bonuses.get(player_id, 0) + points

    facts = facts or {}
    wolf_ids = {
        player_id for player_id, record in by_id.items()
        if record.get("role") == Role.WEREWOLF.value
    }

    # 処刑された人狼へ投票していた村陣営 (狂人は狼陣営なので入らない)
    for execution in facts.get("executions") or []:
        if not isinstance(execution, dict):
            continue
        if _as_int(execution.get("target")) not in wolf_ids:
            continue
        for raw_voter in execution.get("voters") or []:
            voter_id = _as_int(raw_voter)
            record = by_id.get(voter_id) if voter_id is not None else None
            if record is None or record.get("team") != Team.VILLAGE.value:
                continue
            add(voter_id, BONUS_WOLF_EXECUTION_VOTE)

    # 6回目の議論へ到達した試合の人狼
    if (_as_int(facts.get("days")) or 0) >= final_day_threshold:
        for wolf_id in wolf_ids:
            add(wolf_id, BONUS_FINAL_DAY_WOLF)

    # 3狼提出 (村陣営限定)。情報が少ない初日・2日目の死亡ほど倍率が高い
    for guesser_id, hits in count_wolf_guess_hits(
        player_records, facts, wolf_guess_slots=wolf_guess_slots,
    ).items():
        if not hits:
            continue
        points = hits * BONUS_WOLF_GUESS_HIT
        died_on_day = _as_int(by_id[guesser_id].get("died_on_day"))
        if died_on_day is not None and died_on_day <= BONUS_WOLF_GUESS_EARLY_MAX_DAY:
            points *= BONUS_WOLF_GUESS_EARLY_MULTIPLIER
        add(guesser_id, points)

    # 狩人の護衛成功
    guard_successes = _as_int(facts.get("guard_successes")) or 0
    if guard_successes > 0:
        for player_id, record in by_id.items():
            if record.get("role") == Role.GUARD.value:
                add(player_id, guard_successes * BONUS_GUARD_SUCCESS)

    # 初夜に占い師を襲撃して殺しきった (護衛成功・噛みなしでは入らない)
    night1_target = _as_int(facts.get("night1_kill_target"))
    target_record = by_id.get(night1_target) if night1_target is not None else None
    if (
        target_record is not None
        and target_record.get("role") == Role.SEER.value
        and _as_int(target_record.get("died_on_day")) == 1
    ):
        for wolf_id in wolf_ids:
            add(wolf_id, BONUS_NIGHT1_SEER_KILL)

    return bonuses


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


def build_rank_context_map(
    player_rows: list[dict],
    *,
    grandmaster_slots: int = GRANDMASTER_SLOTS,
) -> dict[int, RankContext]:
    """
    レート順の相対ランクを返す。

    母集団は**通算**の試合数で決める。シーズンのハーフリセットは
    `season_games` をゼロにするので、今季の試合数を条件にすると
    リセット直後は全員が母集団から外れ、レートを保持しているのに
    ランクだけ暫定ブロンズへ落ちてしまう。
    レート変換 (1500 + (r - 1500) // 2) は単調非減少なので基本の並びを
    保つ。ただし整数除算で隣接レートが同着になると、season_winsをゼロへ
    戻した後のplayer_idタイブレークで順位・ランクが入れ替わる場合がある。

    player_rows:
      [{"player_id", "rating", "games", "season_games", "season_wins"}]
    """
    if (
        isinstance(grandmaster_slots, bool)
        or not isinstance(grandmaster_slots, int)
        or grandmaster_slots <= 0
    ):
        raise ValueError("grandmaster_slots must be a positive integer")
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
        # 「GM」はこのコードベースではゲームマスターを指すため、
        # グランドマスターの変数は略さない (database.gm_counts はGM=ゲームマスター)
        grandmaster_target = math.ceil(active_count * GRANDMASTER_PERCENTAGE)
        grandmaster_count = min(grandmaster_slots, master_zone, grandmaster_target)
        master_count = max(master_zone - grandmaster_count, 0)

        segments = [
            ("グランドマスター", grandmaster_count),
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
                # グランドマスターはアクティブ上位5%相当・ラダー別上限の専有枠。
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
    names = [get_rank_role_name(name) for name, _, _ in RANK_SPECS]
    for ladder_id in config_lib.LADDER_DEFINITIONS:
        role_name = special_grandmaster_role_name(str(ladder_id))
        if role_name not in names:
            names.append(role_name)
    # v0.40以前に自動作成された旧9人GMロールは、新ロールへの移行時だけ
    # Bot管理ロールとして扱う。新規作成対象には含めない。
    if LEGACY_NINE_GRANDMASTER_ROLE_NAME not in names:
        names.append(LEGACY_NINE_GRANDMASTER_ROLE_NAME)
    return names


def all_rank_role_specs() -> list[tuple[str, int]]:
    specs = [
        (get_rank_role_name(name), get_rank_color_by_name(name))
        for name, _, _ in RANK_SPECS
    ]
    known_names = {name for name, _color in specs}
    for ladder_id in config_lib.LADDER_DEFINITIONS:
        role_name = special_grandmaster_role_name(str(ladder_id))
        if role_name not in known_names:
            specs.append((role_name, _special_grandmaster_role_color(str(ladder_id))))
            known_names.add(role_name)
    return specs
