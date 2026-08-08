"""定数・Enum定義"""
from __future__ import annotations

import os
from enum import Enum, auto
from pathlib import Path

from room_config import (
    RoomDefinition,
    load_local_room_json,
    parse_local_room_config,
)

# Botのバージョン (ヘルプに表示。ソース公開された派生でも識別できるように)
BOT_VERSION = "v0.36"

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

# 役職配分 (13人固定)
ROLE_DISTRIBUTION = {
    Role.WEREWOLF: 3,
    Role.MADMAN: 1,
    Role.SEER: 1,
    Role.MEDIUM: 1,
    Role.GUARD: 1,
    Role.VILLAGER: 6,
}

MAX_PLAYERS = 13

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
DAY_DISCUSSION_BASE = 480      # 8分
DAY_DISCUSSION_DECREASE = 60   # 毎日60秒減少
DAY_DISCUSSION_MIN = 180       # 最低3分
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

# 利用人数が増えるまで一般ユーザーから隠す固定卓。
# @everyone と全ロールへの閲覧許可を外す。DiscordのAdministrator権限を
# 持つメンバーはチャンネル上書きをバイパスするため閲覧できる。
# 一般公開するときは対象IDをこの集合から外す。
ADMIN_ONLY_ROOM_IDS = frozenset({"beginner", "intermediate", "advanced"})

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

# 全員に表示される公開卓 / レート変動の対象卓
PUBLIC_ROOM_IDS = frozenset({"open"})
RATED_ROOM_IDS = frozenset(
    [room.room_id for room in _BUILTIN_ROOM_DEFINITIONS]
    + [
        registration.room.room_id
        for registration in _LOCAL_ROOM_REGISTRATIONS
        if registration.rated
    ]
)
RECRUITMENT_ROOM_IDS = frozenset(
    [room.room_id for room in _BUILTIN_ROOM_DEFINITIONS]
    + [
        registration.room.room_id
        for registration in _LOCAL_ROOM_REGISTRATIONS
        if registration.recruitment_enabled
    ]
)
RATED_ROOM_NAMES = tuple(
    room.name for room in ROOM_DEFINITIONS if room.room_id in RATED_ROOM_IDS
)

PRIVATE_ROOM_CREATOR_ROLE_NAME = "村長"

# ============================================================
# レーティング設定
# ============================================================
INITIAL_RATING = 1500

# 勝利陣営への参加ボーナス（本体ゼロサムとは別枠でレートに加算）
WIN_PARTICIPATION_BONUS = 1

# 6:4 の環境を基準にした固定プール。
# 狼勝ち: 狼4人で +120 / 村9人で -120
# 村勝ち: 村9人で +180 / 狼4人で -180
# 比 120:180 は「狼勝率60%で全体EV=0」を意味する。勝率の実測が溜まったら
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
