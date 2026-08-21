#!/usr/bin/env python3
"""シーズン1開始時に、プレシーズンの試合記録を一度だけ消す。

シーズンリセット (`/season_reset`) はレートを圧縮するだけで試合記録は残す。
シーズン1を「1試合目」から始めるにはこちらを使う。消すのは試合まわりだけで、
同村拒否・募集・GM村・不具合報告といった運用データには触れない。

**試合番号は1から振り直しになる**。番号は `games` の件数から数え直しているため、
サーバーに残っている `NN-昼` ログチャンネルの番号とはずれる。ログも消す前提で
使うこと (Discord側のログカテゴリは手で削除する。Botは触らない)。

    # 何が消えるかだけ見る (既定)
    .venv/bin/python scripts/purge_preseason_stats.py

    # 実際に消す (直前に自動バックアップを取り、内容を検証する)
    .venv/bin/python scripts/purge_preseason_stats.py \
        --execute --confirm-season1 ERASE-PRESEASON

    # レートも白紙に戻す (0戦なのにレートだけ残るのを避ける)
    .venv/bin/python scripts/purge_preseason_stats.py \
        --execute --confirm-season1 ERASE-PRESEASON --reset-ratings

Botは停止してから実行すること。稼働中のプロセスが同じDBへ書いている最中に
消すと、精算途中の試合だけが半端に残る。`--execute` はBot本体と同じロックを
取得できない場合、削除を開始せず中止する。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

BOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BOT_DIR))
load_dotenv(BOT_DIR / ".env")

import database  # noqa: E402
from scripts.bot_runtime_guard import bot_stopped_guard as _bot_stopped_guard  # noqa: E402

# 試合1件から伸びている記録。games を最後に消せるよう、参照している側から並べる。
# レート本体 (player_ratings ほか) は既定では残す。--reset-ratings 指定時だけ
# _RATING_TABLES も一緒に消す。
_GAME_SCOPED_TABLES = (
    "game_recommendations",
    "game_settlements",
    "rating_history",
    "game_stats",
    "game_players",
    # CO宣言・結果申告・投票・夜行動ログ (v0.51)。FKがONなので、
    # 参照先の games を消すより前に子テーブルを空にする必要がある。
    "game_co_events",
    "game_co_results",
    "game_vote_events",
    "game_night_actions",
    "game_turn_events",
    "games",
)

# --reset-ratings で追加削除する。試合だけ消してレートを残すと、0戦なのに
# レートとランクだけが付いている状態になる。完全な白紙で始めるとき用。
# 実行後は全員が初期レートの未計測 (暫定) 扱いへ戻るので、ランクロールは
# Botの通常同期で剥がれる。
_RATING_TABLES = (
    "rating_snapshots",
    "season_resets",
    "player_ratings",
)

_EXECUTE_CONFIRMATION = "ERASE-PRESEASON"


async def _counts(db, tables: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        rows = await db.execute_fetchall(f"SELECT COUNT(*) FROM {table}")
        counts[table] = int(rows[0][0])
    return counts


async def _verify_backup(
    backup_path: str,
    tables: tuple[str, ...],
    expected_counts: dict[str, int],
) -> None:
    """削除前バックアップが読め、対象行数も元DBと一致することを確認する。"""
    path = Path(backup_path)
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            raise RuntimeError("バックアップファイルが存在しないか空です")
    except OSError as exc:
        raise RuntimeError("バックアップファイルを確認できません") from exc

    try:
        async with database.aiosqlite.connect(str(path)) as backup_db:
            integrity_rows = await backup_db.execute_fetchall("PRAGMA integrity_check")
            integrity = [str(row[0]) for row in integrity_rows]
            if integrity != ["ok"]:
                raise RuntimeError(
                    "バックアップのintegrity_checkに失敗しました: "
                    + "; ".join(integrity[:3])
                )
            for table in tables:
                foreign_key_rows = await backup_db.execute_fetchall(
                    f"PRAGMA foreign_key_check({table})"
                )
                if foreign_key_rows:
                    raise RuntimeError(
                        "バックアップのforeign_key_checkに失敗しました: "
                        f"{table} {foreign_key_rows[0]}"
                    )
            actual_counts = await _counts(backup_db, tables)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("バックアップを読み取って検証できません") from exc

    if actual_counts != expected_counts:
        raise RuntimeError(
            "バックアップの対象件数が元DBと一致しません: "
            f"expected={expected_counts}, actual={actual_counts}"
        )


async def _run(args: argparse.Namespace) -> int:
    tables = _GAME_SCOPED_TABLES + (_RATING_TABLES if args.reset_ratings else ())
    print(f"対象DB: {Path(database.DB_PATH).expanduser().absolute()}")
    async with database.connect_db() as db:
        before = await _counts(db, tables)
        pending = int((await db.execute_fetchall(
            "SELECT COUNT(*) FROM game_settlements WHERE status = 'pending'"
        ))[0][0])

    if pending:
        # 未精算の試合は起動時の自動精算で拾われる。statusを見ずに消すと、
        # その試合の戦績とレートが一度も反映されないまま永久に失われる。
        print(
            f"未精算の試合が {pending} 件残っています。"
            "Botを一度起動して精算を終わらせてから実行してください。",
            file=sys.stderr,
        )
        return 1

    print("削除対象:")
    for table, count in before.items():
        print(f"  {table}: {count}")
    if not any(before.values()):
        print("消す記録がありません。何もしません。")
        return 0

    if not args.execute:
        print(
            "\n削除する場合は --execute --confirm-season1 "
            f"{_EXECUTE_CONFIRMATION} を付けてください。"
        )
        print("直前に自動バックアップを取り、整合性と対象件数を検証します。")
        if not args.reset_ratings:
            print(
                "レートは残ります。0戦なのにレートだけ付いた状態を避けるなら "
                "--reset-ratings も付けてください。"
            )
        print("Botは停止してから実行してください。")
        return 0

    backup_path = await database.backup_db(label="pre_season1_purge")
    if backup_path is None:
        print("バックアップを作成できませんでした。中止します。", file=sys.stderr)
        return 1
    try:
        await _verify_backup(backup_path, tables, before)
    except RuntimeError as exc:
        print(f"バックアップ検証に失敗しました。中止します: {exc}", file=sys.stderr)
        return 1
    print(f"バックアップ: {backup_path}")
    print("バックアップ検証: integrity_check=ok / foreign_key_check=ok / 対象件数一致")

    async with database.connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            for table in tables:
                await db.execute(f"DELETE FROM {table}")
        except Exception:
            await db.rollback()
            raise
        await db.commit()
        after = await _counts(db, tables)

    print("削除後:")
    for table, count in after.items():
        print(f"  {table}: {count}")
    remaining = {table: count for table, count in after.items() if count}
    if remaining:
        print(f"残っている行があります: {remaining}", file=sys.stderr)
        return 1
    print("\n完了しました。次の試合から番号は 01 に戻ります。")
    print("Discordの ログ-昼 カテゴリは手で削除してください (Botは触りません)。")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="プレシーズンの試合記録を削除する (シーズン1開始用)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="実際に削除する (省略時は件数を表示するだけ)",
    )
    parser.add_argument(
        "--reset-ratings",
        action="store_true",
        help="レート・シーズン履歴も消して全員を初期レートへ戻す",
    )
    parser.add_argument(
        "--confirm-season1",
        metavar=_EXECUTE_CONFIRMATION,
        help=(
            "--executeの誤実行防止。実行時は "
            f"{_EXECUTE_CONFIRMATION} をそのまま指定する"
        ),
    )
    args = parser.parse_args()
    if not args.execute:
        return await _run(args)
    if args.confirm_season1 != _EXECUTE_CONFIRMATION:
        print(
            "実行確認がありません。削除する場合は "
            f"--confirm-season1 {_EXECUTE_CONFIRMATION} を付けてください。",
            file=sys.stderr,
        )
        return 2
    try:
        with _bot_stopped_guard():
            return await _run(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
