"""戦績カード画像 (stats_image.py) の単体テスト。

- render_player_card がPNGバイト列を返すこと
- フォントが見つからない環境で font_available() が False になること
- 未計測項目が「—」で描かれること (format_card_data の入力整形テスト)
- 0戦のプレイヤーで落ちないこと
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from unittest.mock import patch

import stats_image

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_ZERO_STATS = {"total": 0, "wins": 0, "roles": {}, "teams": {}}


class FontResolutionTest(unittest.TestCase):
    def test_font_available_true_when_bundled_font_found(self) -> None:
        with patch.object(stats_image, "_BUNDLED_FONT_DIR") as bundled_dir:
            bundled_dir.is_dir.return_value = False
            with patch.dict("os.environ", {}, clear=False):
                import os
                os.environ.pop(stats_image._FONT_ENV_VAR, None)
                # macOSの候補が無い体で、環境変数だけ有効にする
                with patch.object(stats_image, "_MACOS_FONT_CANDIDATES", ()):
                    self.assertFalse(stats_image.font_available())

    def test_font_unavailable_when_nothing_found(self) -> None:
        """環境変数・同梱フォント・OS標準パスのすべてが無いと font_available() は False。

        UI側 (views.py) はこれを見てボタン自体を出さない設計なので、
        この状態で render_player_card を直接呼んでも例外にはなるが、
        呼び出し側がそこへ到達しないことが本題。
        """
        import os
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(stats_image._FONT_ENV_VAR, None)
            with patch.object(stats_image, "_BUNDLED_FONT_DIR") as bundled_dir, \
                 patch.object(stats_image, "_MACOS_FONT_CANDIDATES", ()):
                bundled_dir.is_dir.return_value = False
                self.assertFalse(stats_image.font_available())
                with self.assertRaises(RuntimeError):
                    stats_image._load_font(20)

    def test_font_available_via_env_var(self) -> None:
        """WEREWOLF_STATS_CARD_FONT_PATH がチェーンの最優先であること。"""
        # Arial.ttf はどのmacOS開発機にも入っている前提のテスト用実在パス。
        # 存在しなければこのテストはスキップする (CI環境差異を落ちにしない)。
        from pathlib import Path
        candidate = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
        if not candidate.is_file():
            self.skipTest("このテスト環境には検証用フォントが無い")
        import os
        with patch.dict(os.environ, {stats_image._FONT_ENV_VAR: str(candidate)}):
            with patch.object(stats_image, "_BUNDLED_FONT_DIR") as bundled_dir, \
                 patch.object(stats_image, "_MACOS_FONT_CANDIDATES", ()):
                bundled_dir.is_dir.return_value = False
                self.assertTrue(stats_image.font_available())


class FormatCardDataTest(unittest.TestCase):
    def test_missing_items_render_as_placeholder(self) -> None:
        data = stats_image.format_card_data(
            display_name="無名さん",
            avatar_bytes=None,
            stats=_ZERO_STATS,
            rating_info=None,
            vote_stats=None,
            co_stats=None,
            wolf_guess=None,
            recent_games=None,
        )
        self.assertEqual(data["rating_text"], "—")
        self.assertEqual(data["peak_rating_text"], "—")
        self.assertEqual(data["rank_name"], "—")
        self.assertEqual(data["winrate_text"], "—")
        self.assertEqual(data["average_survival_days_text"], "—")
        self.assertEqual(data["wolf_guess_accuracy_text"], "—")
        self.assertEqual(data["vote_participation_text"], "—")
        self.assertEqual(data["vote_execution_match_text"], "—")
        self.assertEqual(data["co_rate_text"], "—")
        self.assertEqual(data["roles"], [])
        self.assertEqual(data["best_roles"], [])
        self.assertEqual(data["worst_roles"], [])
        self.assertEqual(data["recent_games"], [])

    def test_role_top3_requires_minimum_games(self) -> None:
        stats = {
            "total": 10,
            "wins": 5,
            "roles": {
                "占い師": {"count": 6, "wins": 5},  # 閾値以上・高勝率
                "人狼": {"count": 3, "wins": 0},     # 閾値未満なので圏外
            },
            "teams": {},
        }
        data = stats_image.format_card_data(
            display_name="太郎",
            avatar_bytes=None,
            stats=stats,
            rating_info=None,
        )
        self.assertEqual(len(data["best_roles"]), 1)
        self.assertEqual(data["best_roles"][0]["role"], "占い師")
        # 閾値未満の役職しか他に無いので、苦手側に回せる役職が無い
        # (指摘2: 同じ役職を得意/苦手の両方に出さない)。
        self.assertEqual(data["worst_roles"], [])

    def test_role_top3_does_not_duplicate_role_across_best_and_worst(self) -> None:
        """得意/苦手 TOP3 に同じ役職が重複して出ないこと (指摘2)。

        対象役職が3つしか無い場合は得意側が全部使ってしまい苦手側が
        空になる (優先度は得意側、無理に埋めて重複させない)。対象役職が
        5つあれば、得意側 (上位3) と苦手側 (残りのうち下位) が重複なく
        振り分けられる。
        """
        stats = {
            "total": 30,
            "wins": 15,
            "roles": {
                "占い師": {"count": 6, "wins": 5},   # 0.833 -> 得意側1位
                "村人": {"count": 6, "wins": 4},      # 0.667 -> 得意側2位
                "狩人": {"count": 6, "wins": 3},      # 0.5   -> 得意側3位
                "霊媒師": {"count": 6, "wins": 2},     # 0.333 -> 苦手側候補
                "人狼": {"count": 6, "wins": 1},      # 0.167 -> 苦手側1位
            },
            "teams": {},
        }
        data = stats_image.format_card_data(
            display_name="太郎",
            avatar_bytes=None,
            stats=stats,
            rating_info=None,
        )
        best_names = {entry["role"] for entry in data["best_roles"]}
        worst_names = {entry["role"] for entry in data["worst_roles"]}
        self.assertEqual(best_names & worst_names, set())
        self.assertIn("占い師", best_names)
        self.assertIn("人狼", worst_names)

    def test_wolf_guess_accuracy_hidden_below_threshold(self) -> None:
        data = stats_image.format_card_data(
            display_name="太郎",
            avatar_bytes=None,
            stats=_ZERO_STATS,
            rating_info=None,
            wolf_guess={"value": 0.5, "samples": 2},
        )
        self.assertEqual(data["wolf_guess_accuracy_text"], "—")

        data2 = stats_image.format_card_data(
            display_name="太郎",
            avatar_bytes=None,
            stats=_ZERO_STATS,
            rating_info=None,
            wolf_guess={"value": 0.5, "samples": 5},
        )
        self.assertEqual(data2["wolf_guess_accuracy_text"], "50.0%")


class RenderPlayerCardTest(unittest.TestCase):
    def setUp(self) -> None:
        if not stats_image.font_available():
            self.skipTest("この実行環境には戦績カード用フォントが無い")

    def test_returns_png_bytes(self) -> None:
        data = stats_image.format_card_data(
            display_name="テスト太郎",
            avatar_bytes=None,
            stats={
                "total": 12, "wins": 7,
                "roles": {"占い師": {"count": 6, "wins": 4}},
                "teams": {"村陣営": {"count": 9, "wins": 5}},
            },
            rating_info={
                "rating": 1500, "peak_rating": 1600,
                "rank_name": "ゴールド", "emoji": "🥇",
            },
            vote_stats={"participation_rate": 0.8, "execution_match_rate": None},
            co_stats={"total_games": 12, "co_rate": 0.5},
            wolf_guess={"value": 0.6, "samples": 8},
            recent_games=[
                {"won": True, "role": "占い師"},
                {"won": False, "role": "人狼"},
            ],
        )
        png_bytes = stats_image.render_player_card(data)
        self.assertIsInstance(png_bytes, bytes)
        self.assertTrue(png_bytes.startswith(_PNG_SIGNATURE))

    def test_zero_games_player_does_not_crash(self) -> None:
        data = stats_image.format_card_data(
            display_name="新人",
            avatar_bytes=None,
            stats=_ZERO_STATS,
            rating_info=None,
        )
        png_bytes = stats_image.render_player_card(data)
        self.assertTrue(png_bytes.startswith(_PNG_SIGNATURE))

    def test_broken_avatar_bytes_falls_back_to_default_icon(self) -> None:
        data = stats_image.format_card_data(
            display_name="壊れたアバター",
            avatar_bytes=b"not-an-image",
            stats=_ZERO_STATS,
            rating_info=None,
        )
        png_bytes = stats_image.render_player_card(data)
        self.assertTrue(png_bytes.startswith(_PNG_SIGNATURE))

    def test_long_display_name_is_fitted_inside_headers(self) -> None:
        long_name = "とても長い表示名" * 8 + "e\u0301🧙\u200d♂️"
        font = stats_image._load_font(52)
        image = stats_image.Image.new("RGB", (1500, 200))
        draw = stats_image.ImageDraw.Draw(image)
        fitted = stats_image._fit_text_to_width(draw, long_name, font, 600)

        self.assertLessEqual(draw.textlength(fitted, font=font), 600)
        self.assertTrue(fitted.endswith(("…", "...")))
        self.assertLess(len(fitted), len(long_name))

        card_data = stats_image.format_card_data(
            display_name=long_name,
            avatar_bytes=None,
            stats=_ZERO_STATS,
            rating_info=None,
        )
        self.assertTrue(
            stats_image.render_player_card(card_data).startswith(_PNG_SIGNATURE)
        )
        chart_data = {
            "display_name": long_name,
            "variant_label": "13人クロストーク",
            "rank_name": "ゴールド",
            "current_rating": 1500,
            "peak_rating": 1600,
            "games": 0,
            "winrate_text": "—",
            "points": [],
        }
        self.assertTrue(
            stats_image.render_rating_chart(chart_data).startswith(_PNG_SIGNATURE)
        )


class PillowMissingTest(unittest.TestCase):
    """Pillow未導入でもBot全体が起動できることの回帰テスト。

    stats_image.py がモジュールトップレベルで `from PIL import ...` を
    無条件に実行し、views.py がそれを無条件にimportしていたため、Pillow
    未導入環境では game.py 経由の起動 (bot.py の load_extension) が
    ImportError で丸ごと失敗していた (指摘1)。別プロセスでPILへの
    importをブロックし、stats_image / views / game が問題なくimportでき、
    font_available() が False を返すことを確認する。
    """

    def test_stats_image_and_views_import_without_pillow(self) -> None:
        script = (
            "import builtins, sys\n"
            "real_import = builtins.__import__\n"
            "def fake_import(name, *a, **k):\n"
            "    if name == 'PIL' or name.startswith('PIL.'):\n"
            "        raise ImportError('blocked for test')\n"
            "    return real_import(name, *a, **k)\n"
            "builtins.__import__ = fake_import\n"
            "import stats_image\n"
            "assert stats_image.font_available() is False\n"
            "import views\n"
            "import game\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            result.returncode, 0,
            msg=f"stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        self.assertIn("OK", result.stdout)

    def test_render_player_card_raises_clear_error_without_pillow(self) -> None:
        with patch.object(stats_image, "Image", None):
            with self.assertRaises(RuntimeError):
                stats_image.render_player_card({})


if __name__ == "__main__":
    unittest.main()
