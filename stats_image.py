"""戦績カード画像の生成。

Discordに一切依存しない純関数 `render_player_card(data)` が本体。
Discord固有の値 (アバターのバイト列・表示名) は呼び出し側で取得し、
`format_card_data()` で描画用の辞書へ整形してから渡す。この二段構えに
しているのは、DB非同期I/Oと画像描画 (CPU拘束) を分離してテストしやすくし、
かつ `render_player_card` を `asyncio.to_thread` へそのまま渡せるようにするため。

フォントが1つも見つからない環境では `font_available()` が False になり、
呼び出し側 (views.py) はボタン自体を出さない。Botは絶対に落とさない
(このモジュール内で未捕捉の例外を外へ出さない設計にはしていないので、
 呼び出し側は必ず try/except で包むこと。フォント欠如はこのモジュールの
 責務として吸収するが、Pillow自体の予期しない失敗までは保証しない)。
"""
from __future__ import annotations

import datetime as _dt
import functools
import io
import logging
import os
from pathlib import Path
from typing import Optional

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # Pillow未導入でもBot全体は起動できなければならない (指摘1)。
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# 得意/苦手 TOP3 に載せる役職の最低試合数。少数試合の偏りをそのまま
# 「得意」と表示しないための閾値 (実装仕様 §4-2)。
ROLE_TOP_MIN_GAMES = 5

# カード本体のサイズ (実装仕様 §4-1: 1500x1000 前後)
CARD_WIDTH = 1500
CARD_HEIGHT = 1000

_NOT_AVAILABLE = "—"

# 環境変数名 (単体で1本だけ指定する想定。ディレクトリではなくファイル)
_FONT_ENV_VAR = "WEREWOLF_STATS_CARD_FONT_PATH"

# リポジトリ同梱フォントの探索先
_BUNDLED_FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

# macOS標準搭載フォント。このBotはmacOS上でのみ稼働させる方針のため、
# 他OSのパスは候補に持たない (増やすと探索が無駄に伸びるだけになる)。
_MACOS_FONT_CANDIDATES = (
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)


def _resolve_font_path() -> Optional[Path]:
    """フォント解決チェーン (実装仕様 §4-1) を毎回評価する。

    起動時に1回だけ解決してキャッシュしない。理由は2つ:
    - 環境変数やバンドル済みフォントの有無をテストでpatchして
      再現するときに、キャッシュがあると古い結果を返してしまう。
    - この関数はボタン押下時にしか呼ばれないので、毎回 Path.exists()
      を数回叩く程度のコストは無視できる。
    """
    env_path = os.getenv(_FONT_ENV_VAR)
    if env_path:
        candidate = Path(env_path)
        if candidate.is_file():
            return candidate
        log.warning(
            "%s が指すファイルが見つかりません: %s", _FONT_ENV_VAR, env_path,
        )

    if _BUNDLED_FONT_DIR.is_dir():
        for pattern in ("*.ttf", "*.otf", "*.ttc"):
            for path in sorted(_BUNDLED_FONT_DIR.glob(pattern)):
                if path.is_file():
                    return path

    for raw in _MACOS_FONT_CANDIDATES:
        candidate = Path(raw)
        if candidate.is_file():
            return candidate

    return None


def font_available() -> bool:
    """戦績カード画像機能を使えるフォントが見つかるか。UI側の出し分けに使う。

    Pillow自体が未導入の環境では、フォントの有無に関わらず常に False を返す
    (指摘1)。呼び出し側 (views.py) はこれを見てボタンを出さないだけで済み、
    Pillow欠如を理由にBot全体を落とさない。
    """
    if Image is None:
        return False
    return _resolve_font_path() is not None


def _strip_emoji(text: str) -> str:
    """絵文字コードポイントの大半を安価に取り除く (グリフ判定の前段フィルタ)。

    使用フォント (ヒラギノ / Arial Unicode) はカラー絵文字
    グリフを持たないため、そのまま描画すると豆腐 (□) になる。ランク絵文字
    は元々Discord embed用の飾りなので、カード上では文字だけで十分。

    ただしこのブロック列挙だけでは、列挙漏れの記号 (フォント側にたまたま
    無い記号など) を取りこぼす。最終防衛として `_strip_unsupported_glyphs`
    (実際のフォントのグリフ有無を見る判定) を描画直前にも通す (指摘1)。
    """
    return "".join(ch for ch in text if not (0x1F000 <= ord(ch) <= 0x1FFFF or ord(ch) in (0x2764, 0x2b50)))


def _mask_signature(font: "ImageFont.FreeTypeFont", ch: str) -> tuple[tuple[int, int], bytes]:
    """1文字を描画した際のビットマップ形状 (サイズ, ピクセル列) を返す。"""
    mask = font.getmask(ch)
    canvas = Image.new("L", mask.size)
    canvas.im.paste(mask, (0, 0, *mask.size))
    return mask.size, canvas.tobytes()


@functools.lru_cache(maxsize=512)
def _notdef_signature(font_path: str, font_size: int) -> tuple[tuple[int, int], bytes]:
    """そのフォント・サイズにおける「グリフ無し」(.notdef、通称「豆腐」) の見た目。

    未使用が確実なコードポイント (U+10FFFE, Unicode非文字) を描画して基準にする。
    """
    font = ImageFont.truetype(font_path, font_size)
    return _mask_signature(font, "\U0010fffe")


@functools.lru_cache(maxsize=2048)
def _has_glyph_cached(font_path: str, font_size: int, ch: str) -> bool:
    """そのフォントが `ch` の実グリフを持つか (フォントの `.notdef` と比較して判定)。

    絵文字ブロックの列挙 (`_strip_emoji`) だけでは新しい記号や
    列挙漏れの絵文字を取りこぼし、豆腐 (□) として描画されてしまう
    (指摘1)。ここでは実際にフォントへ描かせたビットマップが `.notdef`
    と一致するかどうかで、そのフォントで本当に描けるかを判定する。
    キャッシュは (font_path, font_size, 文字) 単位。カード描画は
    同じフォント・同じ文字列をボタン押下のたびに描くため、都度
    フォントを読み直して比較するとコストが無視できない。
    """
    font = ImageFont.truetype(font_path, font_size)
    if ch.isspace():
        return True
    try:
        signature = _mask_signature(font, ch)
    except Exception:
        return False
    if signature[0] == (0, 0):
        # 幅・高さ0は「空白として実在する」ケース (結合文字など) が多く、
        # 豆腐 (.notdefは通常サイズを持つ) とは区別できるのでそのまま許可する。
        return True
    return signature != _notdef_signature(font_path, font_size)


def _strip_unsupported_glyphs(text: str, font: "ImageFont.FreeTypeFont") -> str:
    """フォントに実グリフが無い文字を落とす (豆腐 (□) 対策、指摘1)。"""
    if not text:
        return text
    path, size = str(font.path), int(font.size)
    return "".join(ch for ch in text if _has_glyph_cached(path, size, ch))


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    path = _resolve_font_path()
    if path is None:
        # font_available() を先に確認してから呼ぶ契約。ここに来たら
        # 呼び出し側の契約違反なので、原因が分かるように例外を出す。
        raise RuntimeError("戦績カード画像用のフォントが見つかりません")
    return ImageFont.truetype(str(path), size)


# ============================================================
# データ整形 (DB非依存・純関数)
# ============================================================

def _rate_text(numerator: Optional[int], denominator: Optional[int]) -> str:
    if not denominator:
        return _NOT_AVAILABLE
    return f"{numerator / denominator * 100:.1f}%"


def _optional_rate_text(rate: Optional[float]) -> str:
    if rate is None:
        return _NOT_AVAILABLE
    return f"{rate * 100:.1f}%"


def _role_winrates(roles: dict) -> list[dict]:
    """{role: {count, wins}} を勝率つきのリストへ (試合数の多い順)。"""
    result = []
    for role, data in roles.items():
        count = int(data.get("count", 0) or 0)
        wins = int(data.get("wins", 0) or 0)
        result.append({
            "role": role,
            "count": count,
            "wins": wins,
            "winrate_text": _rate_text(wins, count),
            "winrate": wins / count if count else None,
        })
    result.sort(key=lambda r: (-r["count"], r["role"]))
    return result


def _top_bottom_roles(role_winrates: list[dict]) -> tuple[list[dict], list[dict]]:
    """得意/苦手 TOP3 (最低 ROLE_TOP_MIN_GAMES 戦)。

    同じ役職が得意・苦手の両方に出ないようにする。優先度は得意側とし、
    苦手側は得意側で使われた役職を除外して選ぶ (指摘2)。対象役職が少なく
    苦手側が3件揃わない場合は、無理に埋めず件数を減らす
    (0件なら描画側で「データ不足」表示になる)。
    """
    eligible = [r for r in role_winrates if r["count"] >= ROLE_TOP_MIN_GAMES]
    best = sorted(eligible, key=lambda r: (-r["winrate"], -r["count"]))[:3]
    best_roles = {r["role"] for r in best}
    worst_sorted = sorted(eligible, key=lambda r: (r["winrate"], -r["count"]))
    worst = [r for r in worst_sorted if r["role"] not in best_roles][:3]
    return best, worst


def _recent_games_summary(recent_games: list[dict]) -> list[dict]:
    return [
        {
            "won": bool(row.get("won")),
            "role": str(row.get("role") or "?"),
        }
        for row in recent_games
    ]


def format_card_data(
    *,
    display_name: str,
    avatar_bytes: Optional[bytes],
    stats: dict,
    rating_info: Optional[dict],
    vote_stats: Optional[dict] = None,
    co_stats: Optional[dict] = None,
    wolf_guess: Optional[dict] = None,
    recent_games: Optional[list[dict]] = None,
) -> dict:
    """DBから取得済みの生データを `render_player_card` 用の辞書へ整形する。

    どの引数も「取得できなかった」場合は None を渡してよい。その項目は
    描画側で `_NOT_AVAILABLE` (「—」) になる。0戦のプレイヤーでも
    `stats` は最低限 total/wins/winrate を持つゼロ埋め辞書として渡すこと
    (呼び出し側で `get_player_stats` が None を返したケースを埋める)。
    """
    total = int(stats.get("total", 0) or 0)
    wins = int(stats.get("wins", 0) or 0)
    roles = _role_winrates(stats.get("roles") or {})
    teams = _role_winrates(stats.get("teams") or {})
    best_roles, worst_roles = _top_bottom_roles(roles)

    rating_text = _NOT_AVAILABLE
    peak_rating_text = _NOT_AVAILABLE
    rank_name = _NOT_AVAILABLE
    rank_emoji = ""
    if rating_info is not None:
        rating_text = str(rating_info.get("rating", _NOT_AVAILABLE))
        peak_rating_text = str(rating_info.get("peak_rating", _NOT_AVAILABLE))
        rank_name = str(rating_info.get("rank_name") or _NOT_AVAILABLE)
        rank_emoji = str(rating_info.get("emoji") or "")

    vote_stats = vote_stats or {}
    co_stats = co_stats or {}
    wolf_guess = wolf_guess or {}

    wolf_guess_samples = int(wolf_guess.get("samples", 0) or 0)
    wolf_guess_text = (
        _optional_rate_text(wolf_guess.get("value"))
        if wolf_guess_samples >= ROLE_TOP_MIN_GAMES
        else _NOT_AVAILABLE
    )

    return {
        "display_name": display_name,
        "avatar_bytes": avatar_bytes,
        "rank_name": rank_name,
        "rank_emoji": rank_emoji,
        "rating_text": rating_text,
        "peak_rating_text": peak_rating_text,
        "total_games": total,
        "wins": wins,
        "winrate_text": _rate_text(wins, total),
        "roles": roles,
        "teams": teams,
        "best_roles": best_roles,
        "worst_roles": worst_roles,
        # database.get_player_stats() が既存の履歴取得SQL (history) から
        # 集計した平均生存日数。死亡した試合は died_on_day、生存したまま
        # 終わった試合 (died_on_day が NULL) はゲーム全体の日数 (gs.days)
        # を生存日数として扱う (database.py 側で計算済み)。集計対象が
        # 0件なら None が入っているので「—」のままにする。
        "average_survival_days_text": (
            f"{stats['average_survival_days']:.1f}日"
            if stats.get("average_survival_days") is not None
            else _NOT_AVAILABLE
        ),
        "wolf_guess_accuracy_text": wolf_guess_text,
        "vote_participation_text": _optional_rate_text(
            vote_stats.get("participation_rate")
        ),
        "vote_execution_match_text": _optional_rate_text(
            vote_stats.get("execution_match_rate")
        ),
        "co_rate_text": (
            _optional_rate_text(co_stats.get("co_rate"))
            if co_stats.get("total_games")
            else _NOT_AVAILABLE
        ),
        "recent_games": _recent_games_summary(recent_games or []),
    }


# ============================================================
# 描画 (Discord非依存の純関数)
# ============================================================

_BG_COLOR = (43, 45, 49)
_PANEL_COLOR = (54, 57, 63)
_TEXT_COLOR = (220, 221, 222)
_MUTED_COLOR = (148, 150, 155)
_ACCENT_COLOR = (88, 101, 242)
_WIN_COLOR = (87, 242, 135)
_LOSS_COLOR = (237, 66, 69)


def _draw_avatar(image: Image.Image, avatar_bytes: Optional[bytes], box: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = box
    size = (right - left, bottom - top)
    avatar = None
    if avatar_bytes:
        try:
            avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            avatar = avatar.resize(size)
        except Exception as exc:
            # 破損データ等でも画像生成自体は止めない。既定アイコンへ落とす。
            log.warning("アバター画像の読み込みに失敗しました: %s", exc)
            avatar = None
    if avatar is None:
        avatar = Image.new("RGBA", size, _ACCENT_COLOR)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, *size), fill=255)
    image.paste(avatar, (left, top), mask)


def _draw_text_block(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    lines: list[tuple[str, ImageFont.FreeTypeFont, tuple[int, int, int]]],
    line_gap: int = 8,
) -> int:
    """(text, font, color) のリストを縦に並べて描き、消費した高さを返す。"""
    x, y = xy
    for text, font, color in lines:
        text = _strip_unsupported_glyphs(text, font)
        draw.text((x, y), text, font=font, fill=color)
        bbox = font.getbbox(text) if text else (0, 0, 0, font.size)
        y += (bbox[3] - bbox[1]) + line_gap
    return y - xy[1]


def _content_height(
    lines: list[tuple[str, "ImageFont.FreeTypeFont", tuple[int, int, int]]],
    line_gap: int = 8,
) -> int:
    """`_draw_text_block` が消費する高さを、実際には描画せず先に計算する。

    パネルの枠 (rounded_rectangle) を件数に応じて詰めるために使う (指摘4)。
    グリフの有無で行の見た目は変わらない (幅は変わり得るが高さは同じ) ので、
    ここでは `_strip_unsupported_glyphs` は掛けなくてよい。
    """
    y = 0
    for text, font, _color in lines:
        bbox = font.getbbox(text) if text else (0, 0, 0, font.size)
        y += (bbox[3] - bbox[1]) + line_gap
    return y


def render_player_card(data: dict) -> bytes:
    """`format_card_data()` が返した辞書からPNGバイト列を作る。

    Discordに一切触れない純関数。フォントが無ければ RuntimeError
    (呼び出し側は `font_available()` を先に見て、この関数自体を呼ばない契約)。
    Pillow自体が未導入の場合も同様に RuntimeError (指摘1)。
    """
    if Image is None:
        raise RuntimeError("Pillowが導入されていないため戦績カード画像を生成できません")
    font_xl = _load_font(56)
    font_lg = _load_font(34)
    font_md = _load_font(26)
    font_sm = _load_font(22)

    # パネルの高さは中身の行数から決める (指摘4)。1件だけの枠が3行分の
    # 空きを抱えるのを避け、逆に行数が多いときは切り詰めずに収まる。
    # PANEL_PAD_TOP/BOTTOM は角丸の見映え確保の最小マージン。
    panel_pad_top, panel_pad_bottom = 24, 22

    def _panel_height(lines: list, *, line_gap: int, min_height: int) -> int:
        return max(min_height, panel_pad_top + _content_height(lines, line_gap) + panel_pad_bottom)

    # 基本成績パネルと役職別勝率パネルの中身を先に組み立てる
    basic_lines = [
        ("基本成績", font_md, _ACCENT_COLOR),
        (
            f"試合数 {data.get('total_games', 0)} / 勝利 {data.get('wins', 0)} "
            f"/ 勝率 {data.get('winrate_text', _NOT_AVAILABLE)}",
            font_sm, _TEXT_COLOR,
        ),
        (
            f"平均生存日数 {data.get('average_survival_days_text', _NOT_AVAILABLE)} "
            f"/ 3人予想的中率 {data.get('wolf_guess_accuracy_text', _NOT_AVAILABLE)}",
            font_sm, _TEXT_COLOR,
        ),
        (
            f"投票参加率 {data.get('vote_participation_text', _NOT_AVAILABLE)} "
            f"/ 処刑投票一致率 {data.get('vote_execution_match_text', _NOT_AVAILABLE)}",
            font_sm, _TEXT_COLOR,
        ),
        (f"CO率 {data.get('co_rate_text', _NOT_AVAILABLE)}", font_sm, _TEXT_COLOR),
    ]
    role_lines = [("役職別勝率", font_md, _ACCENT_COLOR)]
    for entry in data.get("roles", [])[:5] or []:
        role_lines.append((
            f"{entry['role']}: {entry['winrate_text']} ({entry['wins']}/{entry['count']})",
            font_sm, _TEXT_COLOR,
        ))
    if not data.get("roles"):
        role_lines.append(("試合記録がありません", font_sm, _MUTED_COLOR))

    panel_top = 300
    # 左右で同じ高さに揃える (中身が少ない方に合わせて縮めるのではなく、
    # 多い方に揃えて枠を切らない)
    row2_height = max(
        _panel_height(basic_lines, line_gap=10, min_height=140),
        _panel_height(role_lines, line_gap=10, min_height=140),
    )

    # 得意/苦手 TOP3 + 陣営別勝率 (指摘4: 既に取得済みの陣営別勝率を
    # 3列目に置き、TOP3が少数件でも空きを作らず密度を保つ)
    best_lines = [("得意な役職 TOP3", font_md, _WIN_COLOR)]
    for entry in data.get("best_roles", []):
        best_lines.append((
            f"{entry['role']}: {entry['winrate_text']} ({entry['count']}戦)",
            font_sm, _TEXT_COLOR,
        ))
    if not data.get("best_roles"):
        best_lines.append((f"{ROLE_TOP_MIN_GAMES}戦以上の役職がありません", font_sm, _MUTED_COLOR))

    worst_lines = [("苦手な役職 TOP3", font_md, _LOSS_COLOR)]
    for entry in data.get("worst_roles", []):
        worst_lines.append((
            f"{entry['role']}: {entry['winrate_text']} ({entry['count']}戦)",
            font_sm, _TEXT_COLOR,
        ))
    if not data.get("worst_roles"):
        worst_lines.append((f"{ROLE_TOP_MIN_GAMES}戦以上の役職がありません", font_sm, _MUTED_COLOR))

    team_lines = [("陣営別勝率", font_md, _ACCENT_COLOR)]
    for entry in data.get("teams", [])[:5] or []:
        team_lines.append((
            f"{entry['role']}: {entry['winrate_text']} ({entry['wins']}/{entry['count']})",
            font_sm, _TEXT_COLOR,
        ))
    if not data.get("teams"):
        team_lines.append(("試合記録がありません", font_sm, _MUTED_COLOR))

    top3_top = panel_top + row2_height + 30
    row3_height = max(
        _panel_height(best_lines, line_gap=10, min_height=110),
        _panel_height(worst_lines, line_gap=10, min_height=110),
        _panel_height(team_lines, line_gap=10, min_height=110),
    )
    col_gap = 30
    col_width = (CARD_WIDTH - 120 - 2 * col_gap) // 3
    col_left = [60, 60 + col_width + col_gap, 60 + 2 * (col_width + col_gap)]

    # 直近10戦パネル
    recent_top = top3_top + row3_height + 30
    recent_height = 160
    bottom_margin = 60

    # ここまでで全パネルの高さが確定したので、カード全体の高さを
    # 実際の中身に合わせて決める (指摘4: データが薄い試合ほど画像が
    # 間延びしないように、固定 CARD_HEIGHT ではなく積み上げた実高を使う。
    # ただしヘッダー周りの余白確保のため最低 CARD_HEIGHT は下回らない)
    total_height = max(CARD_HEIGHT, recent_top + recent_height + bottom_margin)

    image = Image.new("RGB", (CARD_WIDTH, total_height), _BG_COLOR)
    draw = ImageDraw.Draw(image)

    # ヘッダー: アバター + 名前 + ランク + Rating
    avatar_box = (60, 60, 260, 260)
    _draw_avatar(image, data.get("avatar_bytes"), avatar_box)

    header_x = avatar_box[2] + 40
    rank_line = _strip_emoji(
        f"{data.get('rank_emoji', '')} {data.get('rank_name', _NOT_AVAILABLE)}"
    ).strip()
    _draw_text_block(
        draw,
        (header_x, 70),
        [
            (str(data.get("display_name", "")), font_xl, _TEXT_COLOR),
            (rank_line, font_lg, _ACCENT_COLOR),
            (
                f"Rating {data.get('rating_text', _NOT_AVAILABLE)} "
                f"(最高 {data.get('peak_rating_text', _NOT_AVAILABLE)})",
                font_md, _MUTED_COLOR,
            ),
        ],
        line_gap=14,
    )

    draw.rounded_rectangle(
        (60, panel_top, 740, panel_top + row2_height), radius=16, fill=_PANEL_COLOR,
    )
    _draw_text_block(draw, (90, panel_top + panel_pad_top), basic_lines, line_gap=10)

    draw.rounded_rectangle(
        (780, panel_top, CARD_WIDTH - 60, panel_top + row2_height), radius=16, fill=_PANEL_COLOR,
    )
    _draw_text_block(draw, (810, panel_top + panel_pad_top), role_lines, line_gap=10)

    for left, lines in zip(col_left, (best_lines, worst_lines, team_lines)):
        draw.rounded_rectangle(
            (left, top3_top, left + col_width, top3_top + row3_height), radius=16, fill=_PANEL_COLOR,
        )
        _draw_text_block(draw, (left + 30, top3_top + panel_pad_top), lines, line_gap=10)

    draw.rounded_rectangle(
        (60, recent_top, CARD_WIDTH - 60, recent_top + recent_height), radius=16, fill=_PANEL_COLOR,
    )
    draw.text((90, recent_top + 20), "直近10戦", font=font_md, fill=_ACCENT_COLOR)
    recent = data.get("recent_games", [])
    if not recent:
        draw.text((90, recent_top + 80), "試合記録がありません", font=font_sm, fill=_MUTED_COLOR)
    else:
        cell = 100
        for index, game in enumerate(recent[:10]):
            x = 90 + index * (cell + 12)
            color = _WIN_COLOR if game["won"] else _LOSS_COLOR
            draw.rounded_rectangle(
                (x, recent_top + 70, x + cell, recent_top + 70 + 60),
                radius=8, fill=color,
            )
            label = "勝" if game["won"] else "敗"
            draw.text(
                (x + cell / 2 - 12, recent_top + 78),
                label, font=font_sm, fill=(20, 20, 20),
            )
            role_label = _strip_unsupported_glyphs(str(game["role"])[:4], font_sm)
            draw.text(
                (x, recent_top + 132),
                role_label, font=font_sm, fill=_TEXT_COLOR,
            )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ============================================================
# レート推移グラフ (v0.50)
#
# 戦績カードと同じ二段構え: DBの生データ → format_rating_chart_data() で
# 描画用の辞書へ → render_rating_chart() でPNG。描画側はDiscordにもDBにも
# 触れないので、そのまま asyncio.to_thread へ渡せる。
# ============================================================

CHART_WIDTH = 1400
CHART_HEIGHT = 760

_GRID_COLOR = (72, 76, 84)
_LINE_COLOR = (114, 137, 218)
_RESET_COLOR = (250, 166, 26)


def _chart_date_text(value: object) -> str:
    """DBのTIMESTAMPをJSTの 'MM/DD' 表記にする。読めない値は空文字。"""
    if value is None:
        return ""
    try:
        parsed = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    jst = parsed.astimezone(_dt.timezone(_dt.timedelta(hours=9)))
    return f"{jst.month:02d}/{jst.day:02d}"


def format_rating_chart_data(
    *,
    display_name: str,
    series: dict,
    variant_label: str,
    rank_name: Optional[str] = None,
) -> dict:
    """database.get_rating_series() の戻り値を描画用の辞書へ整形する。"""
    raw_points = list(series.get("points") or [])
    points = [
        {
            "rating": int(point.get("rating_after", 0)),
            "won": point.get("won"),
            "date": _chart_date_text(point.get("played_at")),
            "raw_played_at": str(point.get("played_at") or ""),
        }
        for point in raw_points
    ]
    # シーズンリセットは「その時刻以降の最初の試合」の手前に線を引く。
    reset_indexes: list[int] = []
    for reset_at in series.get("season_resets") or []:
        marker = str(reset_at or "")
        if not marker:
            continue
        for index, point in enumerate(points):
            if point["raw_played_at"] and point["raw_played_at"] >= marker:
                if index not in reset_indexes and index > 0:
                    reset_indexes.append(index)
                break
    games = int(series.get("games") or 0)
    wins = int(series.get("wins") or 0)
    recent_raw = raw_points[-10:]
    recent_delta = (
        int(recent_raw[-1].get("rating_after", 0))
        - int(recent_raw[0].get("rating_before", 0))
        if recent_raw else 0
    )
    return {
        "display_name": display_name,
        "variant_label": variant_label,
        "rank_name": rank_name,
        "current_rating": series.get("current_rating"),
        "peak_rating": series.get("peak_rating"),
        "games": games,
        "wins": wins,
        "winrate_text": _rate_text(wins, games),
        "points": points,
        "reset_indexes": reset_indexes,
        "recent_delta": recent_delta,
        "recent_count": len(recent_raw),
        "truncated": bool(series.get("truncated")),
    }


def _chart_bounds(values: list[int]) -> tuple[int, int]:
    """目盛りが半端な値にならないよう、上下を50刻みへ丸める。"""
    low, high = min(values), max(values)
    if high - low < 100:  # 変動が小さい試合数でも潰れた線にしない
        center = (high + low) // 2
        low, high = center - 50, center + 50
    padding = max(20, (high - low) // 10)
    low = ((low - padding) // 50) * 50
    high = ((high + padding) // 50 + 1) * 50
    return low, high


def render_rating_chart(data: dict) -> bytes:
    """レート推移の折れ線PNGを返す。

    フォント/Pillowが無い環境では RuntimeError (呼び出し側は
    font_available() を先に見る契約。戦績カードと同じ)。
    """
    if Image is None:
        raise RuntimeError("Pillowが導入されていないためレート推移を描画できません")
    font_xl = _load_font(52)
    font_lg = _load_font(32)
    font_md = _load_font(24)
    font_sm = _load_font(20)

    image = Image.new("RGB", (CHART_WIDTH, CHART_HEIGHT), _BG_COLOR)
    draw = ImageDraw.Draw(image)

    header = _strip_unsupported_glyphs(
        f"{data.get('display_name', '')} のレート推移", font_xl,
    )
    draw.text((60, 44), header, font=font_xl, fill=_TEXT_COLOR)
    subtitle_parts = [str(data.get("variant_label") or "")]
    if data.get("rank_name"):
        subtitle_parts.append(str(data["rank_name"]))
    subtitle_parts.append(f"{data.get('games', 0)}戦 {data.get('winrate_text', _NOT_AVAILABLE)}")
    draw.text(
        (62, 112),
        _strip_unsupported_glyphs(" / ".join(part for part in subtitle_parts if part), font_md),
        font=font_md, fill=_MUTED_COLOR,
    )

    current = data.get("current_rating")
    peak = data.get("peak_rating")
    box_left = CHART_WIDTH - 380
    draw.rounded_rectangle(
        (box_left, 40, CHART_WIDTH - 60, 150), radius=16, fill=_PANEL_COLOR,
    )
    draw.text((box_left + 28, 56), "現在レート", font=font_sm, fill=_MUTED_COLOR)
    draw.text(
        (box_left + 28, 84),
        str(current) if current is not None else _NOT_AVAILABLE,
        font=font_lg, fill=_TEXT_COLOR,
    )
    draw.text((box_left + 200, 56), "最高レート", font=font_sm, fill=_MUTED_COLOR)
    draw.text(
        (box_left + 200, 84),
        str(peak) if peak is not None else _NOT_AVAILABLE,
        font=font_lg, fill=_WIN_COLOR,
    )

    points = data.get("points") or []
    plot = (140, 200, CHART_WIDTH - 70, CHART_HEIGHT - 110)
    left, top, right, bottom = plot
    draw.rounded_rectangle((60, 180, CHART_WIDTH - 60, CHART_HEIGHT - 50), radius=18, fill=_PANEL_COLOR)

    if not points:
        draw.text(
            (left, (top + bottom) // 2),
            _strip_unsupported_glyphs("レート変動の記録がまだありません。", font_md),
            font=font_md, fill=_MUTED_COLOR,
        )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    ratings = [int(point["rating"]) for point in points]
    low, high = _chart_bounds(ratings)
    span = max(1, high - low)

    def to_y(rating: int) -> float:
        return bottom - (rating - low) / span * (bottom - top)

    def to_x(index: int) -> float:
        if len(points) == 1:
            return (left + right) / 2
        return left + index / (len(points) - 1) * (right - left)

    # 横グリッドと目盛り
    steps = 5
    for step in range(steps + 1):
        value = low + span * step // steps
        y = to_y(value)
        draw.line((left, y, right, y), fill=_GRID_COLOR, width=1)
        draw.text((left - 78, y - 12), f"{value}", font=font_sm, fill=_MUTED_COLOR)

    # シーズンリセット位置 (縦線)
    for index in data.get("reset_indexes") or []:
        if 0 <= index < len(points):
            x = to_x(index)
            for segment in range(int(top), int(bottom), 14):
                draw.line((x, segment, x, min(segment + 7, bottom)), fill=_RESET_COLOR, width=2)
            draw.text((x + 6, top + 4), "リセット", font=font_sm, fill=_RESET_COLOR)

    # 折れ線と塗り
    line_points = [(to_x(index), to_y(rating)) for index, rating in enumerate(ratings)]
    if len(line_points) >= 2:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).polygon(
            [(line_points[0][0], bottom)] + line_points + [(line_points[-1][0], bottom)],
            fill=(114, 137, 218, 54),
        )
        image.paste(Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB"), (0, 0))
        draw = ImageDraw.Draw(image)
        draw.line(line_points, fill=_LINE_COLOR, width=4, joint="curve")

    # 勝敗の点。試合数が多いときは間引く (点で線が潰れるのを防ぐ)。
    stride = max(1, len(points) // 60)
    for index, point in enumerate(points):
        if index % stride and index != len(points) - 1:
            continue
        x, y = to_x(index), to_y(int(point["rating"]))
        won = point.get("won")
        color = _WIN_COLOR if won else (_LOSS_COLOR if won is False else _MUTED_COLOR)
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=color)

    # 横軸の日付ラベル (最大6個)
    label_count = min(6, len(points))
    seen_x: list[float] = []
    for step in range(label_count):
        index = round(step * (len(points) - 1) / max(1, label_count - 1))
        text = points[index].get("date") or ""
        if not text:
            continue
        x = to_x(index)
        if any(abs(x - other) < 90 for other in seen_x):
            continue
        seen_x.append(x)
        draw.text((x - 30, bottom + 16), text, font=font_sm, fill=_MUTED_COLOR)

    footer = (
        f"直近{data.get('recent_count', 0)}戦: "
        f"{data.get('recent_delta', 0):+d}"
    )
    if data.get("truncated"):
        footer += "（表示は直近ぶんのみ）"
    draw.text(
        (left, CHART_HEIGHT - 40),
        _strip_unsupported_glyphs(footer, font_sm),
        font=font_sm, fill=_MUTED_COLOR,
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
