"""定数・Enum定義"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from room_config import (
    RoomDefinition,
    load_local_room_json,
    parse_local_room_config,
)

# Botのバージョン (ヘルプに表示。ソース公開された派生でも識別できるように)
BOT_VERSION = "v0.37"

# 新規導入先に同名カテゴリが既にある場合、無関係なDiscord構成をBot所有と
# 誤認しない。既存運用は保存済みchannel IDで自動再利用できる。
ADOPT_EXISTING_LAYOUT = os.getenv("ADOPT_EXISTING_LAYOUT", "0").strip().lower() in {
    "1", "true", "yes", "on",
}


class Role(Enum):
    WEREWOLF = "人狼"
    MADMAN = "狂人"
    SEER = "占い師"
    MEDIUM = "霊媒師"
    GUARD = "狩人"
    VILLAGER = "村人"


class Team(Enum):
    VILLAGE = "村陣営"
    WOLF = "狼陣営"


class Phase(Enum):
    LOBBY = auto()
    PREPARATION = auto()
    DAY_DISCUSSION = auto()
    DAY_VOTE = auto()
    DAY_RUNOFF_SPEECH = auto()
    DAY_RUNOFF_VOTE = auto()
    DAY_LAST_WILL = auto()   # 処刑確定者の遺言タイム
    NIGHT = auto()
    MORNING = auto()
    GAME_OVER = auto()
    PAUSED = auto()


ROLE_TEAM = {
    Role.WEREWOLF: Team.WOLF,
    Role.MADMAN: Team.WOLF,
    Role.SEER: Team.VILLAGE,
    Role.MEDIUM: Team.VILLAGE,
    Role.GUARD: Team.VILLAGE,
    Role.VILLAGER: Team.VILLAGE,
}


DEFAULT_VARIANT_ID = "v13_cross"
DEFAULT_LADDER_ID = "l13"

# ターン制の持ち時間。実運用で長すぎると判明しても、進行コードへ触れず
# この3値だけを調整できるよう独立した名前を維持する。
TURN_DAY1_ROUND1_SECONDS = 50
TURN_DAY1_ROUND2_SECONDS = 80
TURN_LATER_DAY_SECONDS = 90

# クロストークの昼時間 (初日、日ごとの短縮、下限)。
# 9人村は13人村より各日1分短く、初日7分・下限2分とする。
CROSSTALK_DISCUSSION_SECONDS_13 = (480, 60, 180)
CROSSTALK_DISCUSSION_SECONDS_9 = (420, 60, 120)


@dataclass(frozen=True)
class VariantDefinition:
    """人数・進行・レート調整値を1か所で定義する。

    ``crosstalk_discussion_seconds`` はクロストークだけ
    ``(初日、日ごとの短縮、下限)``。
    ``turn_round_seconds`` はターン制だけ ``(50, 80, 90)``。
    先頭2値が初日の2巡、末尾が2日目以降の1巡を表す。
    """

    variant_id: str
    label: str
    ladder_id: str
    player_count: int
    role_distribution: Mapping[Role, int]
    discussion_mode: str
    village_win_pool: int
    wolf_win_pool: int
    wolf_guess_slots: int
    final_day_threshold: int
    turn_round_seconds: tuple[int, ...] = ()
    recruitment_occupancy_minutes: int = 90
    turn_interrupts_per_day: int = 0
    crosstalk_discussion_seconds: tuple[int, ...] = ()

    @property
    def wolf_team_size(self) -> int:
        return sum(
            count
            for role, count in self.role_distribution.items()
            if ROLE_TEAM[role] is Team.WOLF
        )

    @property
    def village_team_size(self) -> int:
        return self.player_count - self.wolf_team_size


@dataclass(frozen=True)
class LadderDefinition:
    ladder_id: str
    label: str
    grandmaster_slots: int
    grandmaster_role_name: str
    grandmaster_role_color: int
    # Discordのメンバー一覧では最上位hoist欄1件だけに表示される。
    role_position_priority: int


_ROLE_DISTRIBUTION_13 = MappingProxyType({
    Role.WEREWOLF: 3,
    Role.MADMAN: 1,
    Role.SEER: 1,
    Role.MEDIUM: 1,
    Role.GUARD: 1,
    Role.VILLAGER: 6,
})
_ROLE_DISTRIBUTION_9 = MappingProxyType({
    Role.WEREWOLF: 2,
    Role.MADMAN: 1,
    Role.SEER: 1,
    Role.MEDIUM: 1,
    Role.GUARD: 1,
    Role.VILLAGER: 3,
})

VARIANT_DEFINITIONS = {
    "v13_cross": VariantDefinition(
        "v13_cross", "13人クロストーク", "l13", 13,
        _ROLE_DISTRIBUTION_13, "crosstalk", 180, 120, 3, 6,
        crosstalk_discussion_seconds=CROSSTALK_DISCUSSION_SECONDS_13,
    ),
    "v13_turn": VariantDefinition(
        "v13_turn", "13人ターン制", "l13", 13,
        _ROLE_DISTRIBUTION_13, "turn", 180, 120, 3, 6,
        turn_round_seconds=(
            TURN_DAY1_ROUND1_SECONDS,
            TURN_DAY1_ROUND2_SECONDS,
            TURN_LATER_DAY_SECONDS,
        ),
        recruitment_occupancy_minutes=150,
        turn_interrupts_per_day=2,
    ),
    "v9_cross": VariantDefinition(
        "v9_cross", "9人クロストーク", "l9", 9,
        # 9人村は狼陣営勝率45%を基準に、W/V=11/9となるよう設定する。
        # ターン制とクロストークでレート・加点条件を分けない。
        _ROLE_DISTRIBUTION_9, "crosstalk", 90, 110, 2, 4,
        crosstalk_discussion_seconds=CROSSTALK_DISCUSSION_SECONDS_9,
    ),
    "v9_turn": VariantDefinition(
        "v9_turn", "9人ターン制", "l9", 9,
        _ROLE_DISTRIBUTION_9, "turn", 90, 110, 2, 4,
        turn_round_seconds=(
            TURN_DAY1_ROUND1_SECONDS,
            TURN_DAY1_ROUND2_SECONDS,
            TURN_LATER_DAY_SECONDS,
        ),
        turn_interrupts_per_day=1,
    ),
}
VARIANT_TO_LADDER = {
    variant_id: definition.ladder_id
    for variant_id, definition in VARIANT_DEFINITIONS.items()
}
LADDER_DEFINITIONS = {
    "l13": LadderDefinition(
        "l13", "13人村", 13, "グランドマスター", 0xE74C3C, 2,
    ),
    "l9": LadderDefinition(
        "l9", "9人村", 9, "グランドマスター（9人村）", 0xF39C12, 1,
    ),
}

# ラダーは共通でも、変種ごとに狼勝率が違えばプール比も違う。比率を
# 揃えると勝ちやすい変種でレートを稼げてしまう。実測が30〜50試合
# たまったら変種ごとに W/V = (1-p)/p で直すこと。倍率を一律に
# 変えても均衡勝率は動かない。


def _validate_variant_definitions() -> None:
    for variant_id, definition in VARIANT_DEFINITIONS.items():
        if definition.variant_id != variant_id:
            raise RuntimeError(f"variant key mismatch: {variant_id}")
        if definition.ladder_id not in LADDER_DEFINITIONS:
            raise RuntimeError(
                f"unknown ladder for {variant_id}: {definition.ladder_id}"
            )
        if sum(definition.role_distribution.values()) != definition.player_count:
            raise RuntimeError(f"role distribution mismatch: {variant_id}")
        if definition.wolf_guess_slots != definition.role_distribution[Role.WEREWOLF]:
            raise RuntimeError(f"wolf guess slots mismatch: {variant_id}")
        if definition.discussion_mode == "turn":
            if len(definition.turn_round_seconds) != 3:
                raise RuntimeError(f"turn timer mismatch: {variant_id}")
            if definition.turn_interrupts_per_day <= 0:
                raise RuntimeError(f"turn interrupts mismatch: {variant_id}")
            if definition.crosstalk_discussion_seconds:
                raise RuntimeError(f"turn has crosstalk settings: {variant_id}")
        elif definition.discussion_mode == "crosstalk":
            if definition.turn_round_seconds or definition.turn_interrupts_per_day:
                raise RuntimeError(f"crosstalk has turn settings: {variant_id}")
            discussion_seconds = definition.crosstalk_discussion_seconds
            if len(discussion_seconds) != 3:
                raise RuntimeError(f"crosstalk timer mismatch: {variant_id}")
            base, decrease, minimum = discussion_seconds
            if (
                any(not isinstance(seconds, int) for seconds in discussion_seconds)
                or min(base, decrease, minimum) <= 0
                or base < minimum
            ):
                raise RuntimeError(f"invalid crosstalk timer: {variant_id}")
        else:
            raise RuntimeError(
                f"unknown discussion mode: {definition.discussion_mode}"
            )
        if min(definition.village_win_pool, definition.wolf_win_pool) <= 0:
            raise RuntimeError(f"invalid rating pool: {variant_id}")

    # 9人クロストークと9人ターン制は同一ラダーを共有する。進行方式によって
    # レート・活躍ボーナスの入口が変わらないよう、変種ごとの値も固定する。
    nine_rating_fields = (
        "village_win_pool",
        "wolf_win_pool",
        "wolf_guess_slots",
        "final_day_threshold",
    )
    nine_cross = VARIANT_DEFINITIONS["v9_cross"]
    nine_turn = VARIANT_DEFINITIONS["v9_turn"]
    if any(
        getattr(nine_cross, field) != getattr(nine_turn, field)
        for field in nine_rating_fields
    ):
        raise RuntimeError("v9 rating settings must match across discussion modes")


_validate_variant_definitions()


def get_variant_definition(variant_id: str) -> VariantDefinition:
    """未知の変種を従来ルールとして黙って実行せず、起動・復元を安全に止める。"""
    try:
        return VARIANT_DEFINITIONS[variant_id]
    except KeyError as exc:
        raise ValueError(f"unknown variant_id: {variant_id}") from exc


# 従来APIは13人クロストークのエイリアスとして残す。
ROLE_DISTRIBUTION = dict(
    VARIANT_DEFINITIONS[DEFAULT_VARIANT_ID].role_distribution
)
MAX_PLAYERS = VARIANT_DEFINITIONS[DEFAULT_VARIANT_ID].player_count

# 陣営ごとの人数。プール配分の説明や表示で手書きするとズレるので配分から導く
WOLF_TEAM_SIZE = sum(
    count for role, count in ROLE_DISTRIBUTION.items()
    if ROLE_TEAM[role] is Team.WOLF
)
VILLAGE_TEAM_SIZE = MAX_PLAYERS - WOLF_TEAM_SIZE

# Discordの1メッセージあたりの文字数上限。超えると送信自体が
# HTTPException (50035 Invalid Form Body) で失敗するため、
# 名前などを前置して中継する箇所では本文をこの範囲へ収める
DISCORD_MESSAGE_LIMIT = 2000

# タイマー設定 (秒)
PREPARATION_TIME = 30
# 13人クロストークの従来互換エイリアス。ゲーム進行は各変種の
# ``crosstalk_discussion_seconds`` を使うため、新規の変種設定には使わない。
DAY_DISCUSSION_BASE, DAY_DISCUSSION_DECREASE, DAY_DISCUSSION_MIN = (
    CROSSTALK_DISCUSSION_SECONDS_13
)
NIGHT_BASE = 80                # 初日の夜は80秒
NIGHT_DECREASE = 20            # 2日目以降は60秒固定 (80-20, 下限60)
NIGHT_MIN = 60
RUNOFF_SPEECH_TIME = 30        # 弁明時間 (秒)
LAST_WILL_TIME = 30            # 処刑確定者の遺言時間 (秒)

# 議論開始前の猶予 (秒)。毎日、夜が明けてから議論に入る前に挟む。
# ミュート解除が全員へ行き渡る時間を確保し、話し始めるタイミングを揃える
DISCUSSION_GRACE_TIME = 5

# ミュート整列フェーズ (秒)。議論終了など「全員ミュート」へ移るときに挟む。
# member.edit(mute=) はギルド共有バケットで429になりやすく、13人分が
# 行き渡るまで数秒〜十数秒かかる。この待ち時間を明示のフェーズにすることで
# 「議論終了と表示されたのにまだ喋れる」状態をなくす
MUTE_GRACE_TIME = 5

# ミュート適用に失敗したメンバーを再試行するまでの待ち (秒)。
# 429リトライを使い切ってHTTPExceptionになった人を取りこぼさないための保険
MUTE_RETRY_DELAY = 3

# ゲーム進行・復元・専用村管理でメンバー/権限等を連続変更するときの最小間隔。
# discord_api_sem は同時実行数だけを制限するため、ギルド共有バケットへ
# 短時間に集中する呼び出しは別途ここで平準化する (約9件/10秒)。
BULK_DISCORD_API_INTERVAL = 1.1

# 全員が同時に押すボタン (朝を迎える/役職を確認した/投票) が、この秒数より
# 遅れたらWARNINGでログへ残す。「押したのに反応しない」の原因が
# Discordへの応答・卓ロックの待ち・DB保存のどこにあるかを切り分けるため、
# 段階ごとの所要時間も一緒に出す (views.InteractionTimer)。
SLOW_INTERACTION_SECONDS = 2.0

# シーン切替SE (朝/処刑/投票/投票開示/遺言/夜)。
# 依存 (davey/PyNaCl/libopus) が無い環境ではTrueでも自動で無効になる
SE_ENABLED = True
VOTE_TIMEOUT = 60              # 投票制限時間 (秒)
CHANNEL_DELETE_DELAY = 300     # 結果発表後の削除待ち (秒)
# 3狼提出の受付時間 (秒)。死亡した瞬間から数え、この間だけ #霊界 を開けない。
# 霊界へ入れてしまうと先に死んだ人から答えを聞けるので、提出は必ずこの窓の中で
# 締める。提出するか時間切れになった時点で解放する
WOLF_GUESS_TIMEOUT = 120

# 終了後投票の受付時間。#昼 のパネル1枚で受ける (DMは送らない)。
# #昼の削除待ち (300秒) より短くし、集計結果を同じチャンネルへ出せるようにする。
POSTGAME_RECOMMENDATION_TIMEOUT = 180

# チャンネル名
CH_VILLAGE = "昼"
CH_SPIRIT = "霊界"
CH_LOBBY = "参加受付"
CH_STATS = "統計"
CH_RECRUITMENT = "募集"
CH_OPERATIONS = "運営"
VC_GAME = "人狼ゲーム"
CH_MAYOR_INFO = "専用村作成"

# 統計チャンネルの配置先
STATS_PARENT_CHANNEL_NAME = "総合"
MAYOR_INFO_CATEGORY_NAME = "村長ロール説明"

# 終了した #昼 / #霊界 を消さずに退避するログカテゴリ。
# 試合番号を先頭に付けて移す (Discordはカテゴリ内を名前順に並べるため、
# 番号が前にあると自然に試合順になる)。読むだけで書き込みはできない。
LOG_CATEGORY_VILLAGE = "ログ-昼"
LOG_CATEGORY_SPIRIT = "ログ-霊界"
# Discordの上限がカテゴリあたり50チャンネル。上限に達したら
# 古い順にまとめて減らす (毎回1つずつ消すより整理の頻度が下がる)。
LOG_CATEGORY_LIMIT = 50
LOG_CATEGORY_TRIM_TO = 40
OPERATIONS_CATEGORY_NAME = "開発"

# 募集システム。占有区間は [開催時刻, 開催時刻+90分) とし、
# 終端と次の開始が同時刻なら重複扱いにしない。
RECRUITMENT_OCCUPANCY_MINUTES = 90
RECRUITMENT_MAX_DAYS_AHEAD = 7
# 「今すぐ」募集の開始までの猶予 (分)。
# _schedule_out_of_range が「現在より後」を要求するので0にはできない。
# 通知は開催15分以内で飛ぶので、この値を15より小さくしておくと
# 作成直後の巡回で参加者へDMが届く。
RECRUITMENT_IMMEDIATE_LEAD_MINUTES = 10
RECRUITMENT_MAX_PER_HOST = 3
RECRUITMENT_CAPACITY = MAX_PLAYERS
RECRUITMENT_BACKUP_CAPACITY = 3
RECRUITMENT_NOTIFICATION_WINDOW_MINUTES = 15
RECRUITMENT_ARCHIVE_RETENTION_DAYS = 30
# 「参加者へ一括連絡」の再送までの間隔 (秒)。1回で最大16人へDMが飛ぶため、
# 連打・誤操作で参加者のDMが埋まるのを防ぐ
RECRUITMENT_CONTACT_COOLDOWN_SECONDS = 300
PLAYER_BLOCK_LIMIT = 10

# 不具合・改善報告の1人あたり投稿上限 (直近24時間)。
# 誰でも押せるフォームなので、連投でDBとバックアップが膨らむのを防ぐ。
# 正当な報告を妨げない程度に余裕を持たせる。
FEEDBACK_MAX_PER_DAY = 10

# 実装だけを保持し、一般ユーザーへ出さない固定卓。
# 一般公開するときは対象IDをこの集合から外す。
VARIANT_ROLLOUT_ROOM_IDS = frozenset({
    "open_13_turn",
})
ADMIN_ONLY_ROOM_IDS = frozenset({
    "beginner",
    "intermediate",
    "advanced",
})

# 13人ターン制だけは実装のみで完全未公開とし、募集も停止する。
RECRUITMENT_DISABLED_ROOM_IDS = VARIANT_ROLLOUT_ROOM_IDS

# 村長制度を一般公開するときはFalseへ戻す。
MAYOR_INFO_ADMIN_ONLY = True


_BUILTIN_ROOM_DEFINITIONS = [
    RoomDefinition("beginner", "初心者", frozenset({"アイアン", "ブロンズ", "シルバー"})),
    RoomDefinition(
        "intermediate",
        "中級者",
        frozenset({"ゴールド", "プラチナ", "エメラルド"}),
    ),
    RoomDefinition("advanced", "上級者", frozenset({"ダイヤ", "マスター", "グランドマスター"})),
    RoomDefinition("open", "総合"),
    RoomDefinition(
        "open_13_turn", "総合-13ターン", variant_id="v13_turn", enabled=False,
    ),
    RoomDefinition(
        "open_9_cross",
        "総合-9クロストーク",
        variant_id="v9_cross",
        enabled=True,
    ),
    RoomDefinition(
        "open_9_turn",
        "総合-9ターン",
        variant_id="v9_turn",
        enabled=True,
    ),
]

# 公開コードには個別サーバーの卓名・ID・ユーザーID・ロール名を含めない。
# 環境変数を優先し、未指定の場合だけこのプロジェクト直下の.envを参照する。
_LOCAL_ROOMS_RAW = load_local_room_json(
    Path(__file__).resolve().parent / ".env",
    os.environ,
)
_LOCAL_ROOM_REGISTRATIONS = parse_local_room_config(
    _LOCAL_ROOMS_RAW,
    reserved_room_ids={room.room_id for room in _BUILTIN_ROOM_DEFINITIONS},
    reserved_room_names={room.name for room in _BUILTIN_ROOM_DEFINITIONS},
)

ROOM_DEFINITIONS = [
    *_BUILTIN_ROOM_DEFINITIONS,
    *(registration.room for registration in _LOCAL_ROOM_REGISTRATIONS),
]
ROOM_DEFINITION_MAP = {room.room_id: room for room in ROOM_DEFINITIONS}
if len(ROOM_DEFINITION_MAP) != len(ROOM_DEFINITIONS):
    raise RuntimeError("room_id is duplicated")
for _room_definition in ROOM_DEFINITIONS:
    get_variant_definition(_room_definition.variant_id)

# ROOM_DEFINITIONS/MAP は全定義を保持する。統計・履歴・シミュレーションでは
# 無効な変種も参照するため、ここで落とさない。Discord上のRunner作成と各ライブ
# 導線だけが ACTIVE_* を使う。
ACTIVE_ROOM_DEFINITIONS = tuple(
    room for room in ROOM_DEFINITIONS if room.enabled
)
ACTIVE_ROOM_IDS = frozenset(room.room_id for room in ACTIVE_ROOM_DEFINITIONS)
# 利用者向けの変種選択も、有効な固定卓に対応するものだけを出す。無効卓の
# 定義・履歴・シミュレーションは残るが、将来 enabled を戻すまでUIでは公開しない。
USER_VISIBLE_VARIANT_IDS = tuple(
    dict.fromkeys(room.variant_id for room in ACTIVE_ROOM_DEFINITIONS)
)

# 全員に表示される公開卓 / レート変動の対象卓。無効卓はカテゴリ・VC・参加受付を
# 作らず、これらのライブ集合にも含めない。
_OPEN_ROOM_CANDIDATE_IDS = frozenset({
    "open", "open_13_turn", "open_9_cross", "open_9_turn",
})
OPEN_ROOM_IDS = _OPEN_ROOM_CANDIDATE_IDS & ACTIVE_ROOM_IDS
PUBLIC_ROOM_IDS = OPEN_ROOM_IDS
RATED_ROOM_IDS = frozenset(
    [room.room_id for room in _BUILTIN_ROOM_DEFINITIONS if room.enabled]
    + [
        registration.room.room_id
        for registration in _LOCAL_ROOM_REGISTRATIONS
        if registration.rated and registration.room.enabled
    ]
)
RECRUITMENT_ROOM_IDS = frozenset(
    [room.room_id for room in _BUILTIN_ROOM_DEFINITIONS if room.enabled]
    + [
        registration.room.room_id
        for registration in _LOCAL_ROOM_REGISTRATIONS
        if registration.recruitment_enabled and registration.room.enabled
    ]
)
RATED_ROOM_NAMES = tuple(
    room.name for room in ACTIVE_ROOM_DEFINITIONS if room.room_id in RATED_ROOM_IDS
)

if not (OPEN_ROOM_IDS <= ACTIVE_ROOM_IDS):
    raise RuntimeError("OPEN_ROOM_IDS contains a disabled room")
if not (PUBLIC_ROOM_IDS <= ACTIVE_ROOM_IDS):
    raise RuntimeError("PUBLIC_ROOM_IDS contains a disabled room")
if not (RATED_ROOM_IDS <= ACTIVE_ROOM_IDS):
    raise RuntimeError("RATED_ROOM_IDS contains a disabled room")
if not (RECRUITMENT_ROOM_IDS <= ACTIVE_ROOM_IDS):
    raise RuntimeError("RECRUITMENT_ROOM_IDS contains a disabled room")

PRIVATE_ROOM_CREATOR_ROLE_NAME = "村長"

# ============================================================
# レーティング設定
# ============================================================
INITIAL_RATING = 1500

# 勝利陣営への参加ボーナス（本体ゼロサムとは別枠でレートに加算）
WIN_PARTICIPATION_BONUS = 1

# 13人村 (狼勝率60%を基準) の従来互換固定プール。
# 狼勝ち: 狼4人で +120 / 村9人で -120
# 村勝ち: 村9人で +180 / 狼4人で -180
# 比 120:180 は「狼勝率60%で全体EV=0」を意味する。9人村は変種定義内で
# 狼勝率45%を基準に 110:90 としている。勝率の実測が溜まったら変種ごとに
# W/V = (1-p)/p に合わせ直す。倍率だけを変えても均衡勝率は動かない。
WOLF_WIN_FIXED_POOL = 120
VILLAGE_WIN_FIXED_POOL = 180

# 卓帯 (初心者/中級者/上級者) のインデックス。制限卓の参加条件をそのまま流用し、
# 卓の定義とランク帯の対応がずれないようにする。
RANK_BAND_ROOM_IDS = ("beginner", "intermediate", "advanced")
RANK_BAND = {
    rank_name: band_index
    for band_index, _room_id in enumerate(RANK_BAND_ROOM_IDS)
    for rank_name in (ROOM_DEFINITION_MAP[_room_id].allowed_ranks or frozenset())
}

# 陣営の代表帯の差1につき ±10%。差は最大2帯なので係数は 0.8〜1.2 に収まる。
# 制限卓は参加条件で帯が揃うため、構造的に常に等倍 (100) になる。
RANK_BAND_COEFFICIENT_STEP_PERCENT = 10

# ------------------------------------------------------------
# プレイボーナス (勝敗とは別に、試合中の働きへ加点する)
#
# 本体プールと違い、これらは**非ゼロサム**でレートを注入する。
# 意図的にそうしている (プレイするインセンティブを作るため) が、
# 「試合数の多い人ほど積み上がる」性質があるので、増やすときは
# 本体プールとの比率が崩れていないかを確認すること。
# ------------------------------------------------------------
# 処刑された人狼へ投票していた村陣営 (狂人は対象外)。処刑を確定させた
# 最終ラウンドの投票だけを見る。0票やランダム処刑では誰にも入らない
BONUS_WOLF_EXECUTION_VOTE = 2
# 6回目の議論に到達したときの人狼 (狂人は対象外)
BONUS_FINAL_DAY_WOLF = 2
BONUS_FINAL_DAY_THRESHOLD = 6
# 3狼提出の的中1人につき。初日・2日目に死亡した人は倍率をかける
BONUS_WOLF_GUESS_HIT = 1
BONUS_WOLF_GUESS_SLOTS = 3
BONUS_WOLF_GUESS_EARLY_MULTIPLIER = 2
BONUS_WOLF_GUESS_EARLY_MAX_DAY = 2
# 3狼提出の対象となる死因。除外 (途中離脱) は対象外
BONUS_WOLF_GUESS_DEATH_CAUSES = frozenset({"処刑", "襲撃"})
# 狩人の護衛成功1回につき
BONUS_GUARD_SUCCESS = 1
# 初夜に占い師を襲撃して殺しきったときの人狼 (狂人は対象外)
BONUS_NIGHT1_SEER_KILL = 1
# 終了後の投票 (勝利陣営→敗北陣営の1票 / 推薦の1票) 1票あたり
BONUS_POSTGAME_VOTE = 1

# レート下限 (これ以上は下がらない。底にいる人の継続意欲を守る)
RATING_FLOOR = 1000

# シーズンランク判定: 3戦目以降は相対評価の母集団に入る。
# 3戦未満はアクティブ勢のレート順位に仮スロットした「暫定ランク」を1戦目から表示する
SEASON_RANK_MIN_GAMES = 3
GRANDMASTER_SLOTS = 13
# マスター帯（上位10%）のうち、上位5%相当をGMにする。
# 人数が増えてもGMは最大13人まで。
GRANDMASTER_PERCENTAGE = 0.05
GRANDMASTER_SLOTS_BY_LADDER = {
    ladder_id: definition.grandmaster_slots
    for ladder_id, definition in LADDER_DEFINITIONS.items()
}

# シーズン長 (日)。経過すると #統計 に管理者向けのリセットリマインダーを出す。
# リセット自体は /season_reset で手動実行する
SEASON_LENGTH_DAYS = 90

# 統計の率・平均を表示する最低サンプル数。
# 母数がこれ未満なら、見かけ上の極端な数値を出さず「試合数不足」とする。
STATS_MIN_SAMPLES = 20

# 項目別ランキングに載る最低サンプル数。1〜2戦の外れ値が上位を占めるのを防ぐ。
# STATS_MIN_SAMPLES より緩いのは、母数が指標ごと (村での試合数・狼勝利数・
# 3狼提出回数など) に分かれて全体の試合数より小さくなるため。
LEADERBOARD_MIN_SAMPLES = 5
LEADERBOARD_LIMIT = 10

# 下位→上位。マスターは最上位10%の帯で、上位5%相当（最大13人）をGM扱いにする。
RANK_SPECS = [
    ("アイアン", "🔩", "#4f4f4f"),
    ("ブロンズ", "🥉", "#cd7f32"),
    ("シルバー", "🥈", "#c0c0c0"),
    ("ゴールド", "🥇", "#f1c40f"),
    ("プラチナ", "💠", "#1abc9c"),
    ("エメラルド", "🍀", "#2ecc71"),
    ("ダイヤ", "💎", "#3498db"),
    ("マスター", "👑", "#9b59b6"),
    ("グランドマスター", "🌟", "#e74c3c"),
]

# 総合卓の募集で指定できる参加ランク。表示ランク9段階に加えて、
# まだ対戦記録がなくランクが付いていない人を明示的に扱う。
RECRUITMENT_UNRANKED_LABEL = "ランク未設定"
RECRUITMENT_RANK_OPTIONS = tuple(
    rank_name for rank_name, _emoji, _color in RANK_SPECS
) + (RECRUITMENT_UNRANKED_LABEL,)

# アクティブプレイヤー（SEASON_RANK_MIN_GAMES 以上）のみを対象にした相対ランク比率。
# 上位10%をマスター帯として確保し、その中の上位5%相当（最大13人）をGMに切り出す。
SEASON_RANK_PERCENTAGES = {
    "アイアン": 0.15,
    "ブロンズ": 0.15,
    "シルバー": 0.15,
    "ゴールド": 0.15,
    "プラチナ": 0.10,
    "エメラルド": 0.10,
    "ダイヤ": 0.10,
    "マスター": 0.10,
}

# 旧シミュレーション/補助用途向けの固定閾値（本番ランク表示では使わない）
RANK_TIERS = [
    (0,    "アイアン",         "🔩", "#4f4f4f"),
    (1500, "ブロンズ",         "🥉", "#cd7f32"),
    (1700, "シルバー",         "🥈", "#c0c0c0"),
    (1900, "ゴールド",         "🥇", "#f1c40f"),
    (2100, "プラチナ",         "💠", "#1abc9c"),
    (2300, "エメラルド",       "🍀", "#2ecc71"),
    (2500, "ダイヤ",           "💎", "#3498db"),
    (2700, "マスター",         "👑", "#9b59b6"),
    (2900, "グランドマスター", "🌟", "#e74c3c"),
]

# Discordに付与するランクロール名のプレフィックス
RANK_ROLE_PREFIX = ""
