"""シーズン末に全員分の戦績カード画像 (PNG) を生成し、ローカルへ書き出す。

**DMでの自動配布はしない** (一斉DMになるため。配布は人間の判断に委ねる)。
このスクリプトは画像を出力ディレクトリへ書き出すだけで、Discordへは
一切送信しない。読み取り専用: `data/werewolf_stats.db` (本番DB) を
参照はするが書き込みは行わない。

Bot本体とは別プロセスで、Bot外から実行する想定 (StatsViewの「画像で見る」
ボタンと同じデータ組み立て・描画ロジック `views._build_stats_card_png` を
そのまま再利用する。集計は既存API止まりで、新しいSQLは書かない)。

    # 対象ギルドに参加しているBotが1つだけなら --guild-id は省略できる
    .venv/bin/python scripts/generate_season_cards.py --output-dir ./season_cards

    # 対象を絞って試す
    .venv/bin/python scripts/generate_season_cards.py \
        --guild-id 123456789012345678 --output-dir ./season_cards --limit 5

0戦のプレイヤー (player_ratings行はあるが試合実績が無いケースは通常ない)
はデフォルトで除外する。--include-zero-games を付けると含める。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

BOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT_DIR))
load_dotenv(BOT_DIR / ".env")

import database  # noqa: E402
import stats_image  # noqa: E402
from config import DEFAULT_VARIANT_ID  # noqa: E402
from scripts.bot_runtime_guard import bot_stopped_guard as _bot_stopped_guard  # noqa: E402
from views import _build_stats_card_png  # noqa: E402

log = logging.getLogger("generate-season-cards")

# ファイル名に使えない文字を置き換える (Windows/mac双方の禁則文字をまとめて弾く)
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


def _safe_filename_part(text: str) -> str:
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", text).strip("_")
    return cleaned or "unknown"


def _prepare_output_directory(path: Path) -> Path:
    """個人情報を含む出力先を空の非公開ディレクトリとして確保する。"""
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise RuntimeError(f"出力先にシンボリックリンクは使えません: {path}")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError(f"安全な出力ディレクトリではありません: {path}")
        try:
            if any(path.iterdir()):
                raise RuntimeError(
                    f"出力先が空ではありません。新しい空ディレクトリを指定してください: {path}"
                )
        except OSError as exc:
            raise RuntimeError(f"出力先の内容を確認できません: {path}") from exc
    except OSError as exc:
        raise RuntimeError(f"出力先を作成できません: {path}") from exc
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise RuntimeError(f"出力先を非公開にできません: {path}") from exc
    return path


def _write_private_png(path: Path, png_bytes: bytes) -> None:
    """既存ファイルを上書きせず、所有者だけが読めるPNGを作る。"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        file_descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"出力ファイルが既に存在します: {path}") from exc
    except OSError as exc:
        raise RuntimeError(f"出力ファイルを作成できません: {path}") from exc
    with os.fdopen(file_descriptor, "wb") as output:
        output.write(png_bytes)


class SeasonCardExportClient(discord.Client):
    """全員分のカードを生成して書き出すだけの使い捨てクライアント。

    ゲートウェイのMembers Intent (特権インテント) は要求しない。
    対象プレイヤーは `player_ratings` から個別に guild.fetch_member() で
    引くので、ギルド全体のメンバーキャッシュは不要。
    """

    def __init__(
        self,
        *,
        guild_id: int | None,
        output_dir: Path,
        variant_id: str,
        include_zero_games: bool,
        limit: int | None,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.guild_id = guild_id
        # Discord接続や本番DB読取より前に、出力先の安全性を確定する。
        self.output_dir = _prepare_output_directory(output_dir)
        self.variant_id = variant_id
        self.include_zero_games = include_zero_games
        self.limit = limit
        self.exit_code = 1
        self._done = False

    async def on_ready(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            await self._run()
            self.exit_code = 0
        except Exception:
            log.exception("戦績カード画像の一括生成に失敗しました")
        finally:
            await self.close()

    async def _resolve_guild(self) -> discord.Guild:
        if self.guild_id is not None:
            guild = self.get_guild(self.guild_id)
            if guild is None:
                raise RuntimeError(f"参加サーバーが見つかりません: {self.guild_id}")
            return guild
        if len(self.guilds) != 1:
            raise RuntimeError(
                f"参加サーバーが{len(self.guilds)}件あります。--guild-id で指定してください"
            )
        return self.guilds[0]

    async def _run(self) -> None:
        if not stats_image.font_available():
            raise RuntimeError(
                "このマシンには戦績カード用のフォントがありません。"
                "WEREWOLF_STATS_CARD_FONT_PATH を設定するか、"
                "assets/fonts/ にフォントを置いてください。"
            )

        guild = await self._resolve_guild()
        from rating import ladder_id_for_variant  # 遅延import (循環回避)

        ladder_id = ladder_id_for_variant(self.variant_id)
        ratings = await database.get_all_player_ratings(guild.id, ladder_id)
        if not self.include_zero_games:
            ratings = [row for row in ratings if int(row.get("games", 0) or 0) > 0]
        ratings.sort(key=lambda row: -int(row.get("rating", 0) or 0))
        if self.limit is not None:
            ratings = ratings[: self.limit]

        log.info("対象プレイヤー数: %d", len(ratings))
        generated = 0
        skipped = 0
        for row in ratings:
            player_id = int(row["player_id"])
            try:
                member = await guild.fetch_member(player_id)
            except discord.NotFound:
                log.warning("サーバーに残っていないためスキップ: player_id=%s", player_id)
                skipped += 1
                continue
            except discord.HTTPException as exc:
                log.warning("メンバー取得に失敗しました (player_id=%s): %s", player_id, exc)
                skipped += 1
                continue

            try:
                png_bytes = await _build_stats_card_png(
                    guild, member, variant_id=self.variant_id,
                )
            except Exception:
                log.exception("画像生成に失敗しました (player_id=%s)", player_id)
                skipped += 1
                continue
            if png_bytes is None:
                log.warning("フォント未検出のため画像機能が無効です")
                skipped += 1
                continue

            filename = f"{player_id}_{_safe_filename_part(member.display_name)}.png"
            _write_private_png(self.output_dir / filename, png_bytes)
            generated += 1
            log.info("生成しました: %s", filename)

        log.info("完了: 生成 %d件 / スキップ %d件 → %s", generated, skipped, self.output_dir)


async def _main() -> int:
    parser = argparse.ArgumentParser(
        description="シーズン末に全員分の戦績カード画像を生成する (DM配布はしない)",
    )
    parser.add_argument("--guild-id", type=int, default=None, help="対象サーバーID")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("season_cards"),
        help="PNGの書き出し先ディレクトリ (既定: ./season_cards)",
    )
    parser.add_argument(
        "--variant-id", default=DEFAULT_VARIANT_ID,
        help=f"対象の変種ID (既定: {DEFAULT_VARIANT_ID})",
    )
    parser.add_argument(
        "--include-zero-games", action="store_true",
        help="0戦のプレイヤー(通常は存在しない)も含める",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="生成件数の上限 (試し実行用)",
    )
    args = parser.parse_args()

    try:
        # Botの停止を確認してからtoken・DBを読み、Discord切断までロックを保持する。
        with _bot_stopped_guard():
            token = os.getenv("DISCORD_TOKEN")
            if not token:
                print("DISCORD_TOKENを.envへ設定してください", file=sys.stderr)
                return 2

            client = SeasonCardExportClient(
                guild_id=args.guild_id,
                output_dir=args.output_dir,
                variant_id=args.variant_id,
                include_zero_games=args.include_zero_games,
                limit=args.limit,
            )
            await client.start(token, reconnect=False)
            return client.exit_code
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    )
    raise SystemExit(asyncio.run(_main()))
