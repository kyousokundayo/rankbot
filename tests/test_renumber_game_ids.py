"""game_id 振り直し移行の安全装置を守る回帰テスト。

一度きりの移行だが、取り返しがつかないので前提条件と2段構えの
振り替えだけは壊さないようにする。
"""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database

_SPEC = importlib.util.spec_from_file_location(
    "renumber_game_ids",
    Path(__file__).resolve().parent.parent / "scripts" / "renumber_game_ids.py",
)
renumber_game_ids = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(renumber_game_ids)

MigrationBlocked = renumber_game_ids.MigrationBlocked


class RenumberGameIdsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-renumber-test-")
        self._original_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "renumber.db")
        await database.init_db()
        self.db = sqlite3.connect(database.DB_PATH, isolation_level=None)
        # 本番で起きた並び (3, 69, 70, 71) を再現する
        for game_id, guild_id in ((3, 1), (69, 1), (70, 1), (71, 1)):
            self.db.execute(
                "INSERT INTO games (game_id, guild_id, winner_team, room_id, room_name) "
                "VALUES (?, ?, '村陣営', 'open', '総合')",
                (game_id, guild_id),
            )
            self.db.execute(
                "INSERT INTO game_players (game_id, player_id, role, team, won) "
                "VALUES (?, ?, '村人', '村陣営', 1)",
                (game_id, 500 + game_id),
            )
        self.db.execute("UPDATE sqlite_sequence SET seq = 71 WHERE name = 'games'")

    async def asyncTearDown(self) -> None:
        self.db.close()
        database.DB_PATH = self._original_db_path
        self._tmp.cleanup()

    def _no_bot(self):
        return patch.object(renumber_game_ids, "_running_bot_pids", return_value=[])

    def test_mapping_numbers_from_one_in_chronological_order(self) -> None:
        mapping = renumber_game_ids.build_mapping(self.db)

        self.assertEqual([(old, new) for _g, old, new in mapping], [(3, 1), (69, 2), (70, 3), (71, 4)])

    def test_mapping_stays_unique_across_guilds(self) -> None:
        """game_id はDB全体で一意なので、サーバーごとに1から振ってはいけない。"""
        self.db.execute(
            "INSERT INTO games (game_id, guild_id, winner_team, room_id, room_name) "
            "VALUES (80, 2, '狼陣営', 'open', '総合')"
        )

        mapping = renumber_game_ids.build_mapping(self.db)

        new_ids = [new for _g, _o, new in mapping]
        self.assertEqual(len(new_ids), len(set(new_ids)), "新IDが重複している")
        self.assertEqual(new_ids, [1, 2, 3, 4, 5])

    def test_renumber_keeps_children_attached_to_their_own_game(self) -> None:
        renumber_game_ids.renumber(self.db, renumber_game_ids.build_mapping(self.db))

        # 親子の対応が入れ替わっていないこと (旧69の子は新2に付く)
        pairs = self.db.execute(
            "SELECT g.game_id, p.player_id FROM games g "
            "JOIN game_players p ON p.game_id = g.game_id ORDER BY g.game_id"
        ).fetchall()
        self.assertEqual(pairs, [(1, 503), (2, 569), (3, 570), (4, 571)])
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(
            self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='games'").fetchone()[0],
            4,
        )

    def test_next_game_does_not_collide_after_renumber(self) -> None:
        renumber_game_ids.renumber(self.db, renumber_game_ids.build_mapping(self.db))

        cursor = self.db.execute(
            "INSERT INTO games (guild_id, winner_team, room_id, room_name) "
            "VALUES (1, '村陣営', 'open', '総合')"
        )
        self.assertEqual(cursor.lastrowid, 5)

    def test_blocked_while_the_bot_is_running(self) -> None:
        with patch.object(renumber_game_ids, "_running_bot_pids", return_value=[4242]):
            with self.assertRaisesRegex(MigrationBlocked, "稼働中"):
                renumber_game_ids.check_preconditions(self.db)

    def test_blocked_by_a_game_in_progress(self) -> None:
        self.db.execute(
            "INSERT INTO room_states (guild_id, room_id, phase, payload) "
            "VALUES (1, 'open', 'NIGHT', '{}')"
        )
        with self._no_bot():
            with self.assertRaisesRegex(MigrationBlocked, "進行中"):
                renumber_game_ids.check_preconditions(self.db)

    def test_blocked_by_an_unsettled_result(self) -> None:
        self.db.execute(
            "INSERT INTO game_settlements (guild_id, room_id, game_run_id, rated, winner_team, "
            "player_records, status) VALUES (1, 'open', 'run-x', 0, '村陣営', '[]', 'pending')"
        )
        with self._no_bot():
            with self.assertRaisesRegex(MigrationBlocked, "未精算"):
                renumber_game_ids.check_preconditions(self.db)

    def test_blocked_by_a_recommendation_that_is_voted_but_not_awarded(self) -> None:
        """pending だけでなく confirmed (投票済み・未加算) も止める。"""
        self.db.execute(
            "INSERT INTO game_recommendations "
            "(game_id, guild_id, voter_id, recipient_id, status, expires_at) "
            "VALUES (3, 1, 900, 901, 'confirmed', '2026-08-07T00:00:00+00:00')"
        )
        with self._no_bot():
            with self.assertRaisesRegex(MigrationBlocked, "推薦"):
                renumber_game_ids.check_preconditions(self.db)

    def test_blocked_by_an_unknown_table_holding_game_id(self) -> None:
        """game_id を持つテーブルが増えたら、黙って取り残さず止める。"""
        self.db.execute("CREATE TABLE future_feature (game_id INTEGER, note TEXT)")
        with self._no_bot():
            with self.assertRaisesRegex(MigrationBlocked, "未対応のテーブル"):
                renumber_game_ids.check_preconditions(self.db)

    def test_passes_when_everything_is_settled(self) -> None:
        with self._no_bot():
            renumber_game_ids.check_preconditions(self.db)  # 例外が出なければよい


if __name__ == "__main__":
    unittest.main()
