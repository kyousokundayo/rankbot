"""定数・Enum定義"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from room_config import (
    RoomDefinition,
    load_local_room_json,
    parse_local_room_config,
)

# Botのバージョン (ヘルプに表示。ソース公開された派生でも識別できるように)
BOT_VERSION = "v0.59"

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
    INITIAL_NIGHT = auto()    # 0日目: 人狼の挨拶専用（能力行使なし）
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
        "v9_cross", "9人クロストーク", "l9_cross", 9,
        # 9人村は狼陣営勝率45%を基準に、W/V=11/9となるよう設定する。
        # 現時点のプール値は同じでも、レートと順位の母集団は進行別に分ける。
        _ROLE_DISTRIBUTION_9, "crosstalk", 90, 110, 2, 4,
        crosstalk_discussion_seconds=CROSSTALK_DISCUSSION_SECONDS_9,
    ),
    "v9_turn": VariantDefinition(
        "v9_turn", "9人ターン制", "l9_turn", 9,
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
        "l13", "13人村", 13, "グランドマスター", 0xE74C3C,
    ),
    "l9_cross": LadderDefinition(
        "l9_cross", "9人クロストーク", 9, "グランドマスター9", 0xF39C12,
    ),
    "l9_turn": LadderDefinition(
        "l9_turn", "9人ターン制", 9, "グランドマスター9T", 0x3498DB,
    ),
}

# 9人2変種はラダーも分離する。狼勝率の実測が30〜50試合たまったら、
# 変種ごとに W/V = (1-p)/p でプール比を再評価する。倍率を一律に
# 変えても均衡勝率は動かないため、必要なら比率を個別に直す。


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

    if (
        VARIANT_DEFINITIONS["v9_cross"].ladder_id
        == VARIANT_DEFINITIONS["v9_turn"].ladder_id
    ):
        raise RuntimeError("v9 discussion modes must use separate ladders")


_validate_variant_definitions()


def get_variant_definition(variant_id: str) -> VariantDefinition:
    """未知の変種を従来ルールとして黙って実行せず、起動・復元を安全に止める。"""
    try:
        return VARIANT_DEFINITIONS[variant_id]
    except KeyError as exc:
        raise ValueError(f"unknown variant_id: {variant_id}") from exc


# 従来APIは13人クロストークのエイリアスとして残す。
MAX_PLAYERS = VARIANT_DEFINITIONS[DEFAULT_VARIANT_ID].player_count

# Discordの1メッセージあたりの文字数上限。超えると送信自体が
# HTTPException (50035 Invalid Form Body) で失敗するため、
# 名前などを前置して中継する箇所では本文をこの範囲へ収める
DISCORD_MESSAGE_LIMIT = 2000

# タイマー設定 (秒)
PREPARATION_TIME = 30
# 役職確認後、1日目の議論より前に置く人狼の挨拶専用時間。
# この間は人狼DMの自由文中継だけを開き、襲撃・占い・護衛は行わない。
INITIAL_NIGHT_GREETING_TIME = 30
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

# シーン切替SE (役職確認終了/議論開始・終了/投票開示/決戦弁明/遺言/処刑/夜/夜明け)。
# Trueでは起動前にdavey/PyNaCl/libopusを検査し、欠落時はBotを起動しない。
# Falseでは依存検査とSE再生を行わず、無音で運用する。
SE_ENABLED = True
VOTE_TIMEOUT = 60              # 決戦の一斉投票制限時間 (秒)
VOTE_SPEECH_TIME = 30          # クロストーク通常投票の1人あたり発言時間 (秒)
VOTE_TRANSITION_GRACE = 2.0    # 投票発言の開始・終了SEを聞くための切替時間 (秒)
VOTE_SE_MAX_WAIT = 3.0         # VC接続不調で投票進行を止めないSE待機上限 (秒)
CHANNEL_DELETE_DELAY = 300     # 結果発表後の削除待ち (秒)
# 人狼予想の受付時間 (秒)。死亡した瞬間から数え、この間だけ #霊界 を開けない。
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
CH_OPERATIONS = "運営"
VC_GAME = "人狼ゲーム"
CH_GM_INFO = "村作成"

# 募集作成時のメンションを希望する人が自分で付け外しする通知専用ロール。
# Discord上では @通知 と表示される。閲覧権限などは持たせず、Botだけが
# 募集作成時にこのロールをメンションする。
RECRUITMENT_NOTIFICATION_ROLE_NAME = "通知"

# 統計チャンネルの配置先
STATS_PARENT_CHANNEL_NAME = "総合"
GM_INFO_CATEGORY_NAME = "GM"

# 終了した #昼 を消さずに退避するログカテゴリ。
# 試合番号を先頭に付けて移す (Discordはカテゴリ内を名前順に並べるため、
# 番号が前にあると自然に試合順になる)。読むだけで書き込みはできない。
LOG_CATEGORY_VILLAGE = "ログ-昼"
# #霊界 は退避せず従来どおり削除する。中身は入室通知と死亡者の雑談で
# 読み返す価値が薄く、カテゴリ上限50本ぶんのチャンネル枠と試合ごとの
# 退避APIに見合わない。名前だけは固定卓・名前村の予約語として残す
# (サーバーに残る手動作成の同名カテゴリと衝突させないため)。
LOG_CATEGORY_SPIRIT = "ログ-霊界"
# Discordの上限がカテゴリあたり50チャンネル。上限に達したら
# 古い順にまとめて減らす (毎回1つずつ消すより整理の頻度が下がる)。
LOG_CATEGORY_LIMIT = 50
LOG_CATEGORY_TRIM_TO = 40
# #運営はこの既存カテゴリ内だけで探し、移動・改名・新規作成しない。
# 開発カテゴリまたは#運営が無くてもゲーム本体は継続し、運営UI・通知だけを
# 無効化する。
# @everyoneを含む閲覧者設定はDiscord側の手動運用を正本として変更しない。
# Bot自身の必要権限だけを保証できない場合は通知先に採用しない。
OPERATIONS_CATEGORY_NAME = "開発"
# Administratorと同等に#運営メニューを操作できるロール名。チャンネル閲覧は
# Discord側で手動設定し、この値からBotが閲覧overwriteを追加・削除しない。
# 公開コードへ個別サーバーのロール名を含めないため.envから受け取る
# (例: WEREWOLF_OPERATIONS_STAFF_ROLES=ねいと,運営)。未設定ならAdministratorと
# サーバー所有者だけ。既存#運営でDiscordから手動設定した閲覧権限は、
# この設定に含まれなくても起動時に削除しない。
OPERATIONS_STAFF_ROLE_NAMES = frozenset(
    name.strip()
    for name in os.getenv("WEREWOLF_OPERATIONS_STAFF_ROLES", "").split(",")
    if name.strip()
)


def _parse_user_id_set(env_name: str) -> frozenset[int]:
    """カンマ区切りのDiscordユーザーIDを読む。

    綴り間違いを黙って「権限なし」に落とすと、設定したつもりの人が
    運営メニューを押せない理由に気づけない。起動時に止めて知らせる。
    """
    raw_values = [
        value.strip()
        for value in os.getenv(env_name, "").split(",")
        if value.strip()
    ]
    invalid = [value for value in raw_values if not value.isdigit()]
    if invalid:
        raise RuntimeError(
            f"{env_name} にDiscordユーザーIDでない値があります: "
            + " / ".join(invalid)
        )
    return frozenset(int(value) for value in raw_values)


# ロールを持たせずに、特定の人だけへ#運営メニューの操作を許すユーザーID。
# ロール付与が使えない相手 (別アカウント運用など) を名指しで足すための枠で、
# ロール指定 (WEREWOLF_OPERATIONS_STAFF_ROLES) と併用できる。
# 閲覧権限はここでも変更せず、ボタン操作の認可だけに使う。
# 例: WEREWOLF_OPERATIONS_STAFF_USER_IDS=268251382098690049
OPERATIONS_STAFF_USER_IDS = _parse_user_id_set("WEREWOLF_OPERATIONS_STAFF_USER_IDS")

# 同村拒否・報告を1件ずつ流す記録チャンネル。#運営はボタンだけの操作盤に保ち、
# 読み返す記録はこちらへ分ける。開発カテゴリ内だけで探し、無ければ
# OPERATIONS_LOG_ROLE_NAMES のロールだけが読める形で作る (書き込みはBotのみ)。
CH_OPERATIONS_LOG = "運営記録"
# #運営記録 を閲覧できるロール名 (カンマ区切り)。Botが新規作成するときの
# 閲覧許可先で、公開コードへ個別サーバーのロール名を含めないため.envで渡す
# (例: WEREWOLF_OPERATIONS_LOG_ROLES=ねいと)。未設定・同名ロールが複数ある
# 場合は作成せず、記録は#運営へ流す従来どおりの動きに戻す。
# 既にチャンネルがある場合はDiscord側の手動設定を正本とし、閲覧overwriteを
# Botから追加・削除しない (#運営と同じ扱い)。
OPERATIONS_LOG_ROLE_NAMES = frozenset(
    name.strip()
    for name in os.getenv("WEREWOLF_OPERATIONS_LOG_ROLES", "").split(",")
    if name.strip()
)

# 募集システム。占有時間は各変種の定義に保持し、終端と次の開始が
# 同時刻なら重複扱いにしない。
RECRUITMENT_MAX_DAYS_AHEAD = 7
# 「今すぐ」募集の開始までの猶予 (分)。
# _schedule_out_of_range が「現在より後」を要求するので0にはできない。
# 通知対象には作成時点から入り、10分巡回が開始時刻をまたいだ場合も
# 開催枠内で補完する。通知後に参加・補欠繰上げとなった人は処理直後に
# 個別台帳を確認し、補完失敗時だけ次回巡回で再試行する。
RECRUITMENT_IMMEDIATE_LEAD_MINUTES = 10
RECRUITMENT_BACKUP_CAPACITY = 3
RECRUITMENT_NOTIFICATION_WINDOW_MINUTES = 15
# 「参加者へ一括連絡」の再送までの間隔 (秒)。1回で最大17人へDMが飛ぶため、
# 連打・誤操作で参加者のDMが埋まるのを防ぐ
RECRUITMENT_CONTACT_COOLDOWN_SECONDS = 300
# Discordの選択肢上限25件を2ページまで使い、本人ごとに最大50人を管理する。
PLAYER_BLOCK_LIMIT = 50

# ------------------------------------------------------------
# 「募集」ボタン (v0.51 §3-2) のDM送信ペース設定。
# 既存の paced_discord_api_call は全卓共有の bulk_api_lock を占有し、
# ゲーム開始時のロール付与・ミュートを数十秒待たせてしまうため、
# 募集通知DMはRecruitmentManager専用のペーサーで別枠にする。
# 環境変数は WEREWOLF_ 接頭辞、不正値は起動時に RuntimeError で止める。
# ------------------------------------------------------------
def _parse_positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        raise RuntimeError(f"{name} は数値で指定してください: {raw!r}")
    if not (value > 0):
        raise RuntimeError(f"{name} は正の数で指定してください: {raw!r}")
    return value


def _parse_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise RuntimeError(f"{name} は整数で指定してください: {raw!r}")
    if value <= 0:
        raise RuntimeError(f"{name} は正の整数で指定してください: {raw!r}")
    return value


# 募集通知DMの送信間隔 (秒)。連続DM送信でのレート制限回避が目的。
RECRUITMENT_CALL_DM_INTERVAL_SECONDS = _parse_positive_float_env(
    "WEREWOLF_RECRUITMENT_CALL_DM_INTERVAL_SECONDS", 0.7,
)
# 1人が1日に受け取れる募集通知DMの上限。複数の村から立て続けに
# 呼ばれてDMが埋まるのを防ぐ (超過分は skipped_cap として記録)。
RECRUITMENT_CALL_DM_DAILY_LIMIT = _parse_positive_int_env(
    "WEREWOLF_RECRUITMENT_CALL_DM_DAILY_LIMIT", 5,
)


def _parse_bool_env(name: str, default: bool) -> bool:
    """真偽値の環境変数を厳格パースする。"1"/"0" のみ許可し、それ以外は
    起動時に RuntimeError で止める (数値・浮動小数点パーサと同じ方針)。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    stripped = raw.strip()
    if stripped == "1":
        return True
    if stripped == "0":
        return False
    raise RuntimeError(f"{name} は 1 か 0 で指定してください: {raw!r}")


# 戦績カード画像 (§4) の「画像で見る」ボタンを出すかどうか。
# 描画エンジン・シーズン末の一括生成スクリプト・テストは常に動かせるが、
# プレイヤーから見える入口 (StatsView のボタン) だけはシーズン1開始前に
# 集めるデータ項目とレイアウトを確定するまで既定で閉じておく。
# 有効化したい場合のみ環境変数を "1" にする。
STATS_CARD_BUTTON_ENABLED = _parse_bool_env(
    "WEREWOLF_STATS_CARD_BUTTON_ENABLED", False,
)

# 不具合・改善報告の1人あたり投稿上限 (直近24時間)。
# 誰でも押せるフォームなので、連投でDBとバックアップが膨らむのを防ぐ。
# 正当な報告を妨げない程度に余裕を持たせる。
FEEDBACK_MAX_PER_DAY = 10

# GM／仮GMがGM/#村作成へ到達できるよう、両ロールへ閲覧を許可する。
# 作成ボタン側でも同じロール名を再検証する。
GM_INFO_ADMIN_ONLY = False


_BUILTIN_ROOM_DEFINITIONS = [
    RoomDefinition(
        "beginner",
        "初心者",
        frozenset({"アイアン", "ブロンズ", "シルバー"}),
        enabled=False,
    ),
    RoomDefinition(
        "intermediate",
        "中級者",
        frozenset({"ゴールド", "プラチナ", "エメラルド"}),
        enabled=False,
    ),
    RoomDefinition(
        "advanced",
        "上級者",
        frozenset({"ダイヤ", "マスター", "グランドマスター"}),
        enabled=False,
    ),
    # v0.47で常設卓を全廃し、村はGMの名前村だけにした。過去7試合ぶんの
    # 統計・履歴が room_id="open" を参照するため、定義自体は残す
    # (ROOM_DEFINITIONS は履歴用に全定義を保持する。落とすと過去試合の
    # 卓名解決が壊れる)。Discord上のカテゴリ・受付・VCは作らない。
    RoomDefinition("open", "総合", enabled=False),
    RoomDefinition(
        "open_13_turn", "総合-13ターン", variant_id="v13_turn", enabled=False,
    ),
    RoomDefinition(
        "open_9_cross",
        "総合-9クロストーク",
        variant_id="v9_cross",
        enabled=False,
    ),
    RoomDefinition(
        "open_9_turn",
        "総合-9ターン",
        variant_id="v9_turn",
        enabled=False,
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
    reserved_room_names={
        *(room.name for room in _BUILTIN_ROOM_DEFINITIONS),
        LOG_CATEGORY_VILLAGE,
        LOG_CATEGORY_SPIRIT,
    },
    # このサーバーでDiscord側の静的権限を正本にする既存ローカル卓。
    # 他のローカル卓はsync_permissionsを省略すると従来どおり自動同期する。
    manual_static_room_names={"ねいとくん村"},
    # 身内の練習卓。レートを動かさず、統計の集計・卓フィルタからも外す。
    # .envで rated を明示した場合はそちらが優先される。
    unrated_room_names={"ねいとくん村"},
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
# 利用者向けの変種選択は固定卓の有効・無効とは分離する。9人固定卓を廃止しても、
# GMが作成する名前村では13人クロストーク／9人クロストーク／9人ターン制を選べる。
# 13人ターン制は実装・履歴だけを保持し、引き続き非公開とする。
USER_VISIBLE_VARIANT_IDS = (
    "v13_cross",
    "v9_cross",
    "v9_turn",
)

# 全員に表示される公開卓。無効卓はカテゴリ・VC・参加受付を作らず、
# ライブ集合にも含めない。
_OPEN_ROOM_CANDIDATE_IDS = frozenset({
    "open", "open_13_turn", "open_9_cross", "open_9_turn",
})
OPEN_ROOM_IDS = _OPEN_ROOM_CANDIDATE_IDS & ACTIVE_ROOM_IDS
PUBLIC_ROOM_IDS = OPEN_ROOM_IDS
# レート対象の固定卓。動的なGM名前村はこの静的集合へ列挙できないため、
# RoomRunner.is_rated_room() 側で追加判定する。rated=False のローカル卓は
# レートを動かさず、統計の集計・卓フィルタからも外す (UNRATED_ROOM_IDS)。
RATED_ROOM_IDS = frozenset(
    room.room_id for room in ACTIVE_ROOM_DEFINITIONS if room.rated
)
# 統計から除外する卓。無効卓は過去の試合を集計に残すため含めない
# (常設卓を畳んでも、それまでの統計は消さない)。
UNRATED_ROOM_IDS = frozenset(
    room.room_id for room in ACTIVE_ROOM_DEFINITIONS if not room.rated
)
if not (OPEN_ROOM_IDS <= ACTIVE_ROOM_IDS):
    raise RuntimeError("OPEN_ROOM_IDS contains a disabled room")
if not (PUBLIC_ROOM_IDS <= ACTIVE_ROOM_IDS):
    raise RuntimeError("PUBLIC_ROOM_IDS contains a disabled room")
if not (RATED_ROOM_IDS <= ACTIVE_ROOM_IDS):
    raise RuntimeError("RATED_ROOM_IDS contains a disabled room")

# 専用村・募集を管理できるDiscordロール。
# `GM` は `仮GM` より上に置く。グランドマスターの略称には使わない。
GM_ROLE_NAME = "GM"
TEMP_GM_ROLE_NAME = "仮GM"
PRIVATE_ROOM_CREATOR_ROLE_NAMES = frozenset({GM_ROLE_NAME, TEMP_GM_ROLE_NAME})
PRIVATE_ROOM_CREATOR_ROLE_LABEL = "GM または 仮GM"

# 1人が同時に持てるGM村の数。募集と村が一体なので、同時に複数の受付を
# 出したい人ほど多く要る。複数のロールを持つ場合は**最大値**を採用する
# (仮GM＋GMなら3)。作成そのものの可否は PRIVATE_ROOM_CREATOR_ROLE_NAMES 側で
# 判定するため、ここに無いロールだけの人は0村のままになる。
PRIVATE_ROOM_LIMIT_BY_ROLE = {
    TEMP_GM_ROLE_NAME: 1,
    GM_ROLE_NAME: 3,
}
# 設定運営ロール (WEREWOLF_OPERATIONS_STAFF_ROLES) を併せ持つ運用担当の枠。
PRIVATE_ROOM_LIMIT_STAFF = 7
# サーバー全体で同時に持てるGM村の数。1村がカテゴリ1つと平常時2チャンネル
# (ゲーム中は4) を占有し、Discordの上限は1サーバーあたりカテゴリ50・
# チャンネル500。ログカテゴリ・GM・開発ぶんを残してここで頭打ちにする。
PRIVATE_ROOM_GUILD_LIMIT = 12


def private_room_limit_for_roles(role_names: Iterable[str]) -> int:
    """保持ロール名から、その人が持てるGM村の上限を返す。

    上限の決め方 (最大値を採る／運営ロールは別枠) を1か所に置くため、
    ロールの取り出し方が違う呼出側からも同じこの関数を使う。
    """
    names = [str(name) for name in role_names]
    limits = [
        PRIVATE_ROOM_LIMIT_BY_ROLE[name]
        for name in names
        if name in PRIVATE_ROOM_LIMIT_BY_ROLE
    ]
    if any(name in OPERATIONS_STAFF_ROLE_NAMES for name in names):
        limits.append(PRIVATE_ROOM_LIMIT_STAFF)
    return max(limits, default=0)

# ============================================================
# レーティング設定
# ============================================================
INITIAL_RATING = 1500

# 勝利陣営への参加ボーナス（本体ゼロサムとは別枠でレートに加算）
WIN_PARTICIPATION_BONUS = 1

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
# 人狼予想の的中1人につき。初日・2日目に死亡した人は倍率をかける
BONUS_WOLF_GUESS_HIT = 1
BONUS_WOLF_GUESS_SLOTS = 3
BONUS_WOLF_GUESS_EARLY_MULTIPLIER = 2
BONUS_WOLF_GUESS_EARLY_MAX_DAY = 2
# 人狼予想の対象となる死因。除外 (途中離脱) は対象外
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

# シーズン長 (日)。経過すると #統計 に管理者向けのリセットリマインダーを出す。
# リセット自体は /season_reset で手動実行する
SEASON_LENGTH_DAYS = 90

# 統計の率・平均を表示する最低サンプル数。
# 母数がこれ未満なら、見かけ上の極端な数値を出さず「試合数不足」とする。
STATS_MIN_SAMPLES = 20

# 項目別ランキングに載る最低サンプル数。1〜2戦の外れ値が上位を占めるのを防ぐ。
# STATS_MIN_SAMPLES より緩いのは、母数が指標ごと (村での試合数・狼勝利数・
# 人狼予想提出回数など) に分かれて全体の試合数より小さくなるため。
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

# Discordに付与するランクロール名のプレフィックス
RANK_ROLE_PREFIX = ""
