"""games.game_id を古い順に 1 から振り直す一度きりの移行。

`game_id` は AUTOINCREMENT で欠番を再利用しないため、開発中の検証で消費した
ぶんが穴として残る (本番では 3, 69, 70, 71 のように飛んでいた)。統計UIには
表示用の通し番号 (database._GAME_SEQ_CTE) を出しているので**画面上の見え方は
振り直しても変わらない**。内部IDまで揃えたいとき (ログと表示番号を突き合わせ
たいなど) だけ使う。

**Botを停止してから実行すること。** 稼働中・進行中のゲーム・未精算の結果・
未確定の推薦があれば拒否する。既定はドライランで、実際に書き換えるには
--apply を付ける。

    python scripts/renumber_game_ids.py            # 何がどう変わるかだけ表示
    python scripts/renumber_game_ids.py --apply    # バックアップを取って実行

外部キーは全て games.game_id を ON UPDATE NO ACTION で参照しているため、
親を先に更新すると子が孤児になる。そのため PRAGMA foreign_keys = OFF で
全テーブルを同一トランザクション内で書き換え、最後に foreign_key_check で
参照整合性を確かめてからコミットする (この PRAGMA は**トランザクション内では
効かない**ので、必ず BEGIN より前に置く)。新旧のIDが重なる (70 -> 3 など)
ので、一度オフセット領域へ退避してから確定値へ移す2段構えにする。
game_players と rating_history には game_id の一意制約が無く、1段で順序を
誤ると無警告で行が混ざるため、この2段構えは必須。

失敗したときの復元:

    # Botを止めた状態で
    rm -f data/werewolf_stats.db-wal data/werewolf_stats.db-shm
    cp data/backups/<移行前のバックアップ>.db data/werewolf_stats.db

**-wal / -shm を先に消すこと。** 残したまま本体だけ差し替えると、SQLiteが
古いWALを再生して移行後のデータが復活し、しかもエラーにならない。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database  # noqa: E402

# game_id を持つ全テーブル。games が親で、残りが参照側。
GAME_ID_TABLES = (
    "games",
    "game_players",
    "rating_history",
    "game_stats",
    "game_recommendations",
    "game_settlements",
)

# 新旧のIDが重なるので、一度ここへ逃がしてから確定値へ移す。
# 実在の game_id より十分大きければよい。
RENUMBER_OFFSET = 1_000_000_000


class MigrationBlocked(RuntimeError):
    """前提条件を満たしていないので実行しない。"""


def build_mapping(db: sqlite3.Connection) -> list[tuple[int, int, int]]:
    """(guild_id, 旧game_id, 新game_id) を古い順で作る。

    game_id はDB全体で一意なので、サーバーごとに1から振ると複数サーバー間で
    衝突する。全体を通して古い順に1から振る (統計UIの表示連番はサーバー単位
    なので、複数サーバーでは内部IDと表示番号が一致しなくなるが、これは
    グローバルな主キーである以上避けられない)。
    """
    rows = db.execute("SELECT game_id, guild_id FROM games ORDER BY game_id").fetchall()
    return [
        (int(guild_id), int(old_id), new_id)
        for new_id, (old_id, guild_id) in enumerate(rows, start=1)
    ]


def find_game_id_tables(db: sqlite3.Connection) -> tuple[str, ...]:
    """game_id 列を持つテーブルを実際のスキーマから拾う。

    ハードコードのリストだけを頼りにすると、あとから game_id を持つ
    テーブルが増えたときに黙って取り残し、参照が壊れる。
    """
    found = []
    for (name,) in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({name})")}
        if "game_id" in columns:
            found.append(name)
    return tuple(found)


def check_preconditions(db: sqlite3.Connection) -> None:
    """振り直して安全な状態かを確かめる。"""
    # Bot稼働中は、メモリ上に game_id を持っている処理 (終了後推薦の集計は
    # 最大3分保持する) が宙に浮き、レート加算が黙って失われる
    running = _running_bot_pids()
    if running:
        raise MigrationBlocked(
            f"Botが稼働中です (PID {', '.join(map(str, running))})。停止してから実行してください"
        )

    # 想定外のテーブルが game_id を持っていたら、取り残して参照を壊す
    actual = find_game_id_tables(db)
    unknown = set(actual) - set(GAME_ID_TABLES)
    if unknown:
        raise MigrationBlocked(
            f"未対応のテーブルが game_id を持っています: {sorted(unknown)}。"
            "GAME_ID_TABLES に足してから実行してください"
        )

    # 進行中のゲームがあると、卓スナップショットの復元と競合する
    active = db.execute(
        "SELECT room_id, phase FROM room_states WHERE phase NOT IN ('LOBBY', 'GAME_OVER')"
    ).fetchall()
    if active:
        detail = ", ".join(f"{room_id}({phase})" for room_id, phase in active)
        raise MigrationBlocked(f"進行中の卓があります: {detail}")

    # 未精算が残っていると、精算時に別のIDが振られて対応が崩れる
    pending = db.execute(
        "SELECT room_id, game_run_id, status FROM game_settlements WHERE status != 'settled'"
    ).fetchall()
    if pending:
        detail = ", ".join(f"{room_id}/{run_id}({status})" for room_id, run_id, status in pending)
        raise MigrationBlocked(f"未精算の結果が残っています: {detail}")

    # 受付中・集計待ちの推薦があると、DMのViewが持つ game_id とずれる。
    # pending だけでなく confirmed (投票済み・未加算) も対象にする。
    open_ballots = db.execute(
        "SELECT status, COUNT(*) FROM game_recommendations "
        "WHERE status IN ('pending', 'confirmed') GROUP BY status"
    ).fetchall()
    if open_ballots:
        detail = ", ".join(f"{status}={count}" for status, count in open_ballots)
        raise MigrationBlocked(f"未確定の終了後推薦があります: {detail}")


def _running_bot_pids() -> list[int]:
    """稼働中の bot.py のPIDを返す (見つからなければ空)。"""
    import subprocess

    target = str(Path(__file__).resolve().parent.parent / "bot.py")
    try:
        output = subprocess.run(
            ["ps", "-Ao", "pid=,command="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in output.splitlines():
        line = line.strip()
        if not line or target not in line:
            continue
        head = line.split(None, 1)[0]
        if head.isdigit():
            pids.append(int(head))
    return pids


def snapshot_totals(db: sqlite3.Connection) -> dict[str, object]:
    """移行の前後で変わってはいけない値をまとめて取る。"""
    totals: dict[str, object] = {
        f"count:{table}": db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in GAME_ID_TABLES
    }
    totals["rating_history:sums"] = db.execute(
        "SELECT SUM(elo_delta), SUM(bonus), SUM(recommendation_bonus), "
        "SUM(rating_before), SUM(rating_after) FROM rating_history"
    ).fetchone()
    totals["game_players:wins"] = db.execute(
        "SELECT SUM(won), COUNT(DISTINCT player_id) FROM game_players"
    ).fetchone()
    return totals


def renumber(db: sqlite3.Connection, mapping: list[tuple[int, int, int]]) -> None:
    """全テーブルの game_id を同一トランザクションで振り替える。"""
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("BEGIN IMMEDIATE")
    try:
        # 1段目: 新旧が重なっても衝突しない領域へ退避する
        for table in GAME_ID_TABLES:
            db.execute(
                f"UPDATE {table} SET game_id = game_id + ? WHERE game_id IS NOT NULL",
                (RENUMBER_OFFSET,),
            )
        # 2段目: 確定値へ移す
        for _guild_id, old_id, new_id in mapping:
            for table in GAME_ID_TABLES:
                db.execute(
                    f"UPDATE {table} SET game_id = ? WHERE game_id = ?",
                    (new_id, old_id + RENUMBER_OFFSET),
                )
        # 退避したまま残った行があれば、対応表の作りが間違っている
        for table in GAME_ID_TABLES:
            stranded = db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE game_id >= ?", (RENUMBER_OFFSET,)
            ).fetchone()[0]
            if stranded:
                raise MigrationBlocked(
                    f"{table} に対応付けできない game_id が {stranded} 件残りました"
                )

        # 次に作る試合が既存IDと衝突しないよう、採番カウンタを最大値へ合わせる
        max_new = max((new for _g, _o, new in mapping), default=0)
        db.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'games'", (max_new,))

        violations = db.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise MigrationBlocked(f"参照整合性が壊れました: {violations[:5]}")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="実際に書き換える (既定はドライラン)",
    )
    parser.add_argument(
        "--db", default=database.DB_PATH,
        help=f"対象DB (既定: {database.DB_PATH})",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DBがありません: {db_path}")
        return 1
    # backup_db は database.DB_PATH と BACKUP_DIR を見る。--db で別のDBを
    # 指定したときに本番をバックアップしたり、本番の backups へ書いたり
    # しないよう、どちらも対象DBへ揃える。
    database.DB_PATH = str(db_path)
    database.BACKUP_DIR = db_path.parent / "backups"

    mode = "rw" if args.apply else "ro"
    db = sqlite3.connect(f"file:{db_path}?mode={mode}", uri=True, isolation_level=None)
    db.execute("PRAGMA busy_timeout = 5000")
    try:
        check_preconditions(db)
        mapping = build_mapping(db)
        if not mapping:
            print("試合がまだありません。")
            return 0

        changes = [(g, o, n) for g, o, n in mapping if o != n]
        print(f"対象: {len(mapping)}試合 / 振り替え: {len(changes)}件")
        for guild_id, old_id, new_id in mapping:
            arrow = f"{old_id:>6} -> {new_id:<6}" if old_id != new_id else f"{old_id:>6} (据え置き)"
            print(f"  guild={guild_id}  {arrow}")
        if not changes:
            print("既に1から連番です。何もしません。")
            return 0

        if not args.apply:
            print("\nドライランです。実行するには --apply を付けてください。")
            print("(Botを停止してから実行すること。実行前にDBをバックアップします)")
            return 0

        before = snapshot_totals(db)
        database.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        import asyncio

        saved = asyncio.run(database.backup_db(label="renumber_game_ids"))
        if not saved or not Path(saved).exists():
            print("バックアップを取れませんでした。復元手段が無い状態では実行しません。")
            return 1
        print(f"\nバックアップ: {saved}")

        renumber(db, mapping)

        after = snapshot_totals(db)
        if before != after:
            diff = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
            print(f"⚠️ 移行前後で値が変わりました: {diff}")
            print(f"バックアップから復元してください: {saved}")
            return 1
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            print(f"⚠️ 整合性チェックに失敗: {integrity}")
            return 1

        print("完了。件数・レート履歴の合計・参照整合性はいずれも変化なし。")
        rows = db.execute(
            "SELECT game_id, room_name, winner_team FROM games ORDER BY game_id"
        ).fetchall()
        for game_id, room_name, winner in rows:
            print(f"  {game_id:>4}  {room_name} / {winner}")
        return 0
    except MigrationBlocked as e:
        print(f"実行できません: {e}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
