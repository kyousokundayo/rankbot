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

    # 実際に消す (直前に自動バックアップを取る)
    .venv/bin/python scripts/purge_preseason_stats.py --execute

    # レートも白紙に戻す (0戦なのにレートだけ残るのを避ける)
    .venv/bin/python scripts/purge_preseason_stats.py --execute --reset-ratings

Botは停止してから実行すること。稼働中のプロセスが同じDBへ書いている最中に
消すと、精算途中の試合だけが半端に残る。`--execute` はBot本体と同じロックを
取得できない場合、削除を開始せず中止する。
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database  # noqa: E402

# 試合1件から伸びている記録。games を最後に消せるよう、参照している側から並べる。
# レート本体 (player_ratings ほか) は既定では残す。--reset-ratings 指定時だけ
# _RATING_TABLES も一緒に消す。
_GAME_SCOPED_TABLES = (
    "game_recommendations",
    "game_settlements",
    "rating_history",
    "game_stats",
    "game_players",
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


def _bot_lock_path() -> Path:
    runtime_dir = Path(
        os.getenv(
            "WEREWOLF_BOT_RUNTIME_DIR",
            str(Path(tempfile.gettempdir()) / f"werewolf-bot-{os.getuid()}"),
        )
    ).expanduser().absolute()
    return Path(
        os.getenv("WEREWOLF_BOT_LOCK_FILE", str(runtime_dir / "bot.lock"))
    ).expanduser().absolute()


@contextmanager
def _bot_stopped_guard():
    """Bot本体と同じロックを保持し、稼働中と同時にDBを変更させない。"""
    lock_path = _bot_lock_path()
    parent = lock_path.parent
    if parent.is_symlink() or lock_path.is_symlink():
        raise RuntimeError(f"安全なBotロックではありません: {lock_path}")
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.is_symlink() or parent.stat().st_uid != os.getuid():
            raise RuntimeError(f"Botロックの所有者を確認できません: {lock_path}")
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Bot停止状態を確認できません: {lock_path}") from exc

    acquired = False
    try:
        if lock_path.is_symlink() or lock_path.stat().st_uid != os.getuid():
            raise RuntimeError(f"安全なBotロックではありません: {lock_path}")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "人狼Botが稼働中です。Botを停止してから実行してください。"
            ) from exc
        acquired = True
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


async def _counts(db, tables: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        rows = await db.execute_fetchall(f"SELECT COUNT(*) FROM {table}")
        counts[table] = int(rows[0][0])
    return counts


async def _run(args: argparse.Namespace) -> int:
    tables = _GAME_SCOPED_TABLES + (_RATING_TABLES if args.reset_ratings else ())
    async with database.connect_db() as db:
        before = await _counts(db, tables)

    print("削除対象:")
    for table, count in before.items():
        print(f"  {table}: {count}")
    if not any(before.values()):
        print("消す記録がありません。何もしません。")
        return 0

    if not args.execute:
        print("\n--execute を付けると削除します (直前に自動バックアップを取ります)。")
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
    print(f"バックアップ: {backup_path}")

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
    args = parser.parse_args()
    if not args.execute:
        return await _run(args)
    try:
        with _bot_stopped_guard():
            return await _run(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
