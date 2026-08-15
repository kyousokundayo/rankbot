"""永続化・精算・シーズン競合の基盤テスト。"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord
import database
import rating as rating_lib
from config import LOG_CATEGORY_SPIRIT, LOG_CATEGORY_VILLAGE, RoomDefinition, Team
from game import GameCog
from permissions import RoomPermissionMixin, RoomVisibilityError


class _PermissionTarget:
    def __init__(self, target_id: int, name: str) -> None:
        self.id = target_id
        self.name = name


class _PermissionChannel:
    def __init__(self, channel_id: int, name: str, *, category=None) -> None:
        self.id = channel_id
        self.name = name
        self.category = category
        self.overwrites: dict[object, discord.PermissionOverwrite] = {}

    def overwrites_for(self, target):
        return self.overwrites.get(target, discord.PermissionOverwrite())

    async def set_permissions(self, target, *, overwrite, reason=None) -> None:
        if overwrite is None:
            self.overwrites.pop(target, None)
        else:
            self.overwrites[target] = overwrite


class _PermissionManager(RoomPermissionMixin):
    def __init__(self) -> None:
        self.discord_api_sem = asyncio.Semaphore(1)


class _PacedPermissionManager(_PermissionManager):
    def __init__(self) -> None:
        super().__init__()
        self.paced_calls = 0

    async def paced_discord_api_call(self, func, *args, **kwargs):
        self.paced_calls += 1
        async with self.discord_api_sem:
            return await func(*args, **kwargs)


class _PrivateMember:
    def __init__(self, guild, member_id: int, name: str, roles=None) -> None:
        self.guild = guild
        self.id = member_id
        self.display_name = name
        self.roles = list(roles or [])
        self.edit_calls: list[dict] = []

    async def add_roles(self, role, reason=None) -> None:
        if role not in self.roles:
            self.roles.append(role)

    async def remove_roles(self, *roles, reason=None) -> None:
        remove_ids = {role.id for role in roles}
        self.roles = [role for role in self.roles if role.id not in remove_ids]

    async def edit(self, *, roles=None, reason=None):
        self.edit_calls.append({"roles": roles, "reason": reason})
        if roles is not None:
            self.roles = list(roles)
        return self


class PlatformDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="werewolf-platform-test-")
        self._original_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self._tmp.name) / "test.db")
        await database.init_db()

    async def asyncTearDown(self) -> None:
        database.DB_PATH = self._original_db_path
        self._tmp.cleanup()

    async def test_foreign_keys_enabled_on_every_connection(self) -> None:
        async with database.connect_db() as db:
            rows = await db.execute_fetchall("PRAGMA foreign_keys")
        self.assertEqual(rows[0][0], 1)

    async def test_new_private_room_schema_and_api_do_not_expose_legacy_access_roles(self) -> None:
        async with database.connect_db() as db:
            columns = await db.execute_fetchall("PRAGMA table_info(private_rooms)")
        self.assertNotIn("role_name", {row[1] for row in columns})
        self.assertNotIn("role_id", {row[1] for row in columns})

        await database.save_private_room(1, "private_10", 10, "十村")
        row = await database.get_private_room_by_owner(1, 10)
        assert row is not None
        self.assertNotIn("role_name", row)
        self.assertNotIn("role_id", row)

    async def test_existing_private_room_legacy_columns_are_retained_but_ignored(self) -> None:
        async with database.connect_db() as db:
            await db.execute("ALTER TABLE private_rooms ADD COLUMN role_name TEXT")
            await db.execute("ALTER TABLE private_rooms ADD COLUMN role_id INTEGER")
            await db.commit()

        await database.init_db()
        async with database.connect_db() as db:
            columns = await db.execute_fetchall("PRAGMA table_info(private_rooms)")
        self.assertTrue({"role_name", "role_id"}.issubset({row[1] for row in columns}))

        await database.save_private_room(1, "private_10", 10, "十村")
        row = await database.get_private_room_by_owner(1, 10)
        assert row is not None
        self.assertNotIn("role_name", row)
        self.assertNotIn("role_id", row)

    async def test_orphan_in_unused_legacy_table_is_ignored(self) -> None:
        async with database.connect_db() as db:
            await db.execute("PRAGMA foreign_keys = OFF")
            await db.execute("""
                CREATE TABLE private_room_members (
                    guild_id INTEGER NOT NULL,
                    room_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, room_id, user_id),
                    FOREIGN KEY (guild_id, room_id)
                        REFERENCES private_rooms(guild_id, room_id)
                )
            """)
            await db.execute(
                "INSERT INTO private_room_members "
                "(guild_id, room_id, user_id) VALUES (1, 'removed', 10)"
            )
            await db.commit()

        await database.init_db()
        async with database.connect_db() as db:
            count = (await db.execute_fetchall(
                "SELECT COUNT(*) FROM private_room_members"
            ))[0][0]
        self.assertEqual(count, 1)

    async def test_init_db_rejects_not_null_legacy_private_room_role_without_changes(self) -> None:
        async with database.connect_db() as db:
            await db.execute(
                "ALTER TABLE private_rooms ADD COLUMN role_name TEXT NOT NULL"
            )
            await db.commit()
            schema_version_before = (await db.execute_fetchall(
                "PRAGMA schema_version"
            ))[0][0]
            journal_mode_before = (await db.execute_fetchall(
                "PRAGMA journal_mode"
            ))[0][0]
            table_sql_before = (await db.execute_fetchall(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='private_rooms'"
            ))[0][0]

        with self.assertRaisesRegex(RuntimeError, "private_rooms.role_nameがNOT NULL"):
            await database.init_db()

        async with database.connect_db() as db:
            schema_version_after = (await db.execute_fetchall(
                "PRAGMA schema_version"
            ))[0][0]
            journal_mode_after = (await db.execute_fetchall(
                "PRAGMA journal_mode"
            ))[0][0]
            table_sql_after = (await db.execute_fetchall(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='private_rooms'"
            ))[0][0]
        self.assertEqual(schema_version_after, schema_version_before)
        self.assertEqual(journal_mode_after, journal_mode_before)
        self.assertEqual(table_sql_after, table_sql_before)

    async def test_init_db_rejects_partial_private_room_unique_keys(self) -> None:
        async with database.connect_db() as db:
            await db.execute("PRAGMA foreign_keys = OFF")
            await db.execute("ALTER TABLE private_rooms RENAME TO private_rooms_old")
            await db.execute("""
                CREATE TABLE private_rooms (
                    guild_id INTEGER NOT NULL,
                    room_id TEXT NOT NULL,
                    owner_id INTEGER NOT NULL,
                    room_name TEXT NOT NULL,
                    variant_id TEXT NOT NULL DEFAULT 'v13_cross',
                    status TEXT NOT NULL DEFAULT 'active',
                    category_id INTEGER,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, room_id)
                )
            """)
            await db.execute(
                "CREATE UNIQUE INDEX private_rooms_owner_partial "
                "ON private_rooms(guild_id, owner_id) WHERE owner_id > 0"
            )
            await db.execute(
                "CREATE UNIQUE INDEX private_rooms_name_partial "
                "ON private_rooms(guild_id, room_name) WHERE room_name <> ''"
            )
            await db.execute("DROP TABLE private_rooms_old")
            await db.commit()
            schema_version_before = (await db.execute_fetchall(
                "PRAGMA schema_version"
            ))[0][0]

        with self.assertRaisesRegex(RuntimeError, "private_roomsの一意制約不足"):
            await database.init_db()

        async with database.connect_db() as db:
            schema_version_after = (await db.execute_fetchall(
                "PRAGMA schema_version"
            ))[0][0]
        self.assertEqual(schema_version_after, schema_version_before)

    async def test_init_db_rejects_schema_without_required_foreign_key(self) -> None:
        async with database.connect_db() as db:
            await db.execute("PRAGMA foreign_keys = OFF")
            await db.execute(
                "ALTER TABLE recruitment_notification_deliveries "
                "RENAME TO recruitment_notification_deliveries_old"
            )
            await db.execute("""
                CREATE TABLE recruitment_notification_deliveries (
                    recruitment_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    notified_at TEXT NOT NULL,
                    delivery_status TEXT NOT NULL DEFAULT 'sent',
                    PRIMARY KEY (recruitment_id, user_id)
                )
            """)
            await db.execute("DROP TABLE recruitment_notification_deliveries_old")
            await db.commit()

        with self.assertRaisesRegex(
            RuntimeError,
            "recruitment_notification_deliveriesの外部キー不足",
        ):
            await database.init_db()

    async def test_init_db_rejects_foreign_key_orphan(self) -> None:
        async with database.connect_db() as db:
            await db.execute("PRAGMA foreign_keys = OFF")
            await db.execute(
                "INSERT INTO game_players "
                "(game_id, player_id, role, team, won) VALUES (?, ?, ?, ?, ?)",
                (999_999, 1, "villager", "village", 0),
            )
            await db.commit()

        with self.assertRaisesRegex(RuntimeError, "DBの外部キー整合性"):
            await database.init_db()

    async def test_init_db_rejects_column_and_check_contract_drift_unchanged(self) -> None:
        recruitment_template = """
            CREATE TABLE recruitment_entries (
                recruitment_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                kind {kind_definition},
                joined_at {joined_definition},
                PRIMARY KEY (recruitment_id, user_id),
                FOREIGN KEY (recruitment_id) REFERENCES recruitments(id)
            )
        """
        scenarios = (
            (
                "column_type",
                "recruitment_entries",
                recruitment_template.format(
                    kind_definition="INTEGER NOT NULL CHECK(kind IN ('参加', '補欠'))",
                    joined_definition="TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                ),
                r"recruitment_entries\.kindの型",
            ),
            (
                "not_null",
                "recruitment_entries",
                recruitment_template.format(
                    kind_definition="TEXT CHECK(kind IN ('参加', '補欠'))",
                    joined_definition="TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                ),
                r"recruitment_entries\.kindのNOT NULL",
            ),
            (
                "default",
                "recruitment_entries",
                recruitment_template.format(
                    kind_definition="TEXT NOT NULL CHECK(kind IN ('参加', '補欠'))",
                    joined_definition="TIMESTAMP",
                ),
                r"recruitment_entries\.joined_atのDEFAULT",
            ),
            (
                "entry_check",
                "recruitment_entries",
                recruitment_template.format(
                    kind_definition="TEXT NOT NULL",
                    joined_definition="TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                ),
                r"recruitment_entriesのCHECK制約不足",
            ),
            (
                "self_block_check",
                "player_blocks",
                """
                    CREATE TABLE player_blocks (
                        guild_id INTEGER NOT NULL,
                        blocker_id INTEGER NOT NULL,
                        blocked_id INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (guild_id, blocker_id, blocked_id)
                    )
                """,
                r"player_blocksのCHECK制約不足",
            ),
        )
        original_test_db_path = database.DB_PATH
        try:
            for scenario_name, table_name, create_sql, error_pattern in scenarios:
                with self.subTest(scenario=scenario_name):
                    database.DB_PATH = str(
                        Path(self._tmp.name) / f"contract-{scenario_name}.db"
                    )
                    await database.init_db()
                    async with database.connect_db() as db:
                        await db.execute("PRAGMA foreign_keys = OFF")
                        await db.execute(
                            f"ALTER TABLE {table_name} RENAME TO {table_name}_old"
                        )
                        await db.execute(create_sql)
                        await db.execute(f"DROP TABLE {table_name}_old")
                        await db.commit()
                        schema_version_before = (await db.execute_fetchall(
                            "PRAGMA schema_version"
                        ))[0][0]
                        journal_mode_before = (await db.execute_fetchall(
                            "PRAGMA journal_mode"
                        ))[0][0]
                        table_sql_before = (await db.execute_fetchall(
                            "SELECT sql FROM sqlite_master "
                            "WHERE type='table' AND name=?",
                            (table_name,),
                        ))[0][0]

                    with self.assertRaisesRegex(RuntimeError, error_pattern):
                        await database.init_db()

                    async with database.connect_db() as db:
                        schema_version_after = (await db.execute_fetchall(
                            "PRAGMA schema_version"
                        ))[0][0]
                        journal_mode_after = (await db.execute_fetchall(
                            "PRAGMA journal_mode"
                        ))[0][0]
                        table_sql_after = (await db.execute_fetchall(
                            "SELECT sql FROM sqlite_master "
                            "WHERE type='table' AND name=?",
                            (table_name,),
                        ))[0][0]
                    self.assertEqual(schema_version_after, schema_version_before)
                    self.assertEqual(journal_mode_after, journal_mode_before)
                    self.assertEqual(table_sql_after, table_sql_before)
        finally:
            database.DB_PATH = original_test_db_path

    async def test_init_db_rejects_rows_violating_current_checks(self) -> None:
        original_test_db_path = database.DB_PATH
        scenarios = (
            (
                "invalid_entry_kind",
                (
                    "INSERT INTO recruitments "
                    "(id, guild_id, host_id, title, scheduled_at, room_id, status) "
                    "VALUES (1, 1, 10, '終了済み', '2099-01-01T00:00:00', "
                    "'private_10', '終了済み')",
                    "INSERT INTO recruitment_entries "
                    "(recruitment_id, user_id, kind) VALUES (1, 20, '不正')",
                ),
                r"recruitment_entries\.kindが現行制約に違反",
            ),
            (
                "self_block",
                (
                    "INSERT INTO player_blocks "
                    "(guild_id, blocker_id, blocked_id) VALUES (1, 20, 20)",
                ),
                r"player_blocksに自己ブロック",
            ),
        )
        try:
            for scenario_name, statements, error_pattern in scenarios:
                with self.subTest(scenario=scenario_name):
                    database.DB_PATH = str(
                        Path(self._tmp.name) / f"invalid-row-{scenario_name}.db"
                    )
                    await database.init_db()
                    async with database.connect_db() as db:
                        await db.execute("PRAGMA ignore_check_constraints = ON")
                        for statement in statements:
                            await db.execute(statement)
                        await db.commit()
                    with self.assertRaisesRegex(RuntimeError, error_pattern):
                        await database.init_db()
        finally:
            database.DB_PATH = original_test_db_path

    async def test_init_db_repairs_indexes_once_without_rebuilding_current_schema(self) -> None:
        index_names = tuple(database._CURRENT_INDEX_DEFINITIONS)
        async with database.connect_db() as db:
            for index_name in index_names:
                await db.execute(f"DROP INDEX {index_name}")
                await db.execute(
                    f"CREATE INDEX {index_name} ON games(game_id)"
                )
            await db.commit()

        await database.init_db()
        async with database.connect_db() as db:
            index_rows = await db.execute_fetchall(
                "SELECT name, sql FROM sqlite_master WHERE type='index' "
                f"AND name IN ({', '.join('?' for _ in index_names)})",
                index_names,
            )
            index_sql = {str(row[0]): str(row[1]) for row in index_rows}
            schema_version = (await db.execute_fetchall("PRAGMA schema_version"))[0][0]
        expected_index_sql = database._CURRENT_INDEX_DEFINITIONS
        self.assertEqual(set(index_sql), set(expected_index_sql))
        for index_name, expected_sql in expected_index_sql.items():
            self.assertEqual(
                database._normalized_schema_sql(index_sql[index_name]),
                database._normalized_schema_sql(expected_sql),
            )

        await database.init_db()
        async with database.connect_db() as db:
            second_schema_version = (
                await db.execute_fetchall("PRAGMA schema_version")
            )[0][0]
        self.assertEqual(second_schema_version, schema_version)

    async def test_failed_unique_index_repair_rolls_back_all_index_changes(self) -> None:
        async with database.connect_db() as db:
            await db.execute("DROP INDEX idx_games_run_unique")
            await db.execute(
                "CREATE INDEX idx_games_run_unique "
                "ON games(guild_id, room_id, game_run_id)"
            )
            for winner_team in ("village", "wolf"):
                await db.execute(
                    "INSERT INTO games "
                    "(guild_id, room_id, game_run_id, winner_team) "
                    "VALUES (1, 'open', 'duplicate-run', ?)",
                    (winner_team,),
                )
            await db.commit()
            schema_version_before = (await db.execute_fetchall(
                "PRAGMA schema_version"
            ))[0][0]
            journal_mode_before = (await db.execute_fetchall(
                "PRAGMA journal_mode"
            ))[0][0]
            index_sql_before = (await db.execute_fetchall(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='idx_games_run_unique'"
            ))[0][0]

        with self.assertRaisesRegex(RuntimeError, "idx_games_run_unique"):
            await database.init_db()

        async with database.connect_db() as db:
            schema_version_after = (await db.execute_fetchall(
                "PRAGMA schema_version"
            ))[0][0]
            journal_mode_after = (await db.execute_fetchall(
                "PRAGMA journal_mode"
            ))[0][0]
            index_sql_after = (await db.execute_fetchall(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='idx_games_run_unique'"
            ))[0][0]
        self.assertEqual(schema_version_after, schema_version_before)
        self.assertEqual(journal_mode_after, journal_mode_before)
        self.assertEqual(index_sql_after, index_sql_before)

    async def test_backup_names_are_unique_and_retention_is_per_label(self) -> None:
        original_backup_dir = database.BACKUP_DIR
        database.BACKUP_DIR = Path(self._tmp.name) / "backups"
        try:
            first = await database.backup_db(label="rapid", keep=2)
            second = await database.backup_db(label="rapid", keep=2)
            third = await database.backup_db(label="rapid", keep=2)
            other = await database.backup_db(label="season_reset", keep=2)
        finally:
            database.BACKUP_DIR = original_backup_dir
        self.assertEqual(len({first, second, third}), 3)
        self.assertTrue(Path(third).exists())
        self.assertTrue(Path(other).exists())
        rapid_files = list((Path(self._tmp.name) / "backups").glob("*_rapid.db"))
        self.assertEqual(len(rapid_files), 2)

    async def test_backup_retention_also_removes_wal_and_shm(self) -> None:
        """本体だけ消すと -wal / -shm が孤児として溜まり続ける。

        バックアップ先がWALモードだとSQLiteが同名の -wal / -shm を作る。
        世代を捨てるときに一緒に消さないと、.db を消してもディスクを
        食い続ける (実際に本番で46ファイル取り残していた)。
        """
        backup_dir = Path(self._tmp.name) / "backups"
        original_backup_dir = database.BACKUP_DIR
        database.BACKUP_DIR = backup_dir
        try:
            first = await database.backup_db(label="keep1", keep=1)
            # 作成直後はチェックポイント済みでサイドカーが残らない
            self.assertFalse(Path(f"{first}-wal").exists())
            self.assertFalse(Path(f"{first}-shm").exists())
            # 過去にSQLiteが残していたぶんを再現する
            for suffix in ("-wal", "-shm"):
                Path(f"{first}{suffix}").write_bytes(b"")
            # 本体だけ消していた頃の取り残しも用意する
            # (DB_PATH の stem は asyncSetUp で "test")
            orphan = backup_dir / "test_20000101_000000_000000_old.db-wal"
            orphan.write_bytes(b"")

            second = await database.backup_db(label="keep1", keep=1)
        finally:
            database.BACKUP_DIR = original_backup_dir

        # 世代落ちした first は本体もサイドカーも残さない
        self.assertFalse(Path(first).exists())
        self.assertFalse(Path(f"{first}-wal").exists())
        self.assertFalse(Path(f"{first}-shm").exists())
        # 過去の取り残しも回収する
        self.assertFalse(orphan.exists())
        # 残す世代には手を付けない
        self.assertTrue(Path(second).exists())

    async def test_gm_named_room_uses_public_visibility_without_access_role(self) -> None:
        default = _PermissionTarget(1, "@everyone")
        bot_member = _PermissionTarget(2, "bot")
        private_role = _PermissionTarget(3, "招待ロール")
        stale_role = _PermissionTarget(4, "過去の許可ロール")
        stale_member = _PermissionTarget(5, "過去の許可メンバー")
        category = _PermissionChannel(100, "専用村")
        lobby = _PermissionChannel(101, "参加受付", category=category)
        category.overwrites[stale_role] = discord.PermissionOverwrite(view_channel=True)
        category.overwrites[stale_member] = discord.PermissionOverwrite(read_messages=True)
        lobby.overwrites[stale_role] = discord.PermissionOverwrite(connect=True)
        guild = SimpleNamespace(
            default_role=default,
            me=bot_member,
            roles=[private_role, stale_role],
            text_channels=[lobby],
            voice_channels=[],
            get_member=lambda member_id: stale_member if member_id == stale_member.id else None,
        )
        room_def = SimpleNamespace(
            room_id="private_1",
            name="専用村",
            private_owner_id=1,
            allowed_ranks=frozenset(),
        )
        await _PermissionManager()._apply_room_visibility(guild, category, room_def)
        self.assertTrue(category.overwrites[default].view_channel)
        self.assertTrue(category.overwrites[default].read_messages)
        self.assertTrue(category.overwrites[default].connect)
        self.assertTrue(lobby.overwrites[default].view_channel)
        self.assertTrue(lobby.overwrites[default].connect)
        self.assertNotIn(private_role, category.overwrites)

    async def test_manual_room_skips_static_permission_sync(self) -> None:
        default = _PermissionTarget(1, "@everyone")
        bot_member = _PermissionTarget(2, "bot")
        manual_role = _PermissionTarget(3, "手動閲覧ロール")
        category = _PermissionChannel(100, "ねいとくん村")
        lobby = _PermissionChannel(101, "参加受付", category=category)
        category.overwrites[manual_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
        )
        lobby.overwrites[manual_role] = discord.PermissionOverwrite(
            read_message_history=True,
        )
        before_category = dict(category.overwrites)
        before_lobby = dict(lobby.overwrites)
        guild = SimpleNamespace(
            default_role=default,
            me=bot_member,
            roles=[manual_role],
            text_channels=[lobby],
            voice_channels=[],
        )
        room_def = SimpleNamespace(
            room_id="nate",
            name="ねいとくん村",
            sync_permissions=False,
        )
        manager = _PacedPermissionManager()

        await manager._apply_room_visibility(guild, category, room_def)

        self.assertEqual(manager.paced_calls, 0)
        self.assertEqual(category.overwrites, before_category)
        self.assertEqual(lobby.overwrites, before_lobby)

    async def test_permission_diff_uses_pacer_and_skips_unchanged_write(self) -> None:
        manager = _PacedPermissionManager()
        channel = _PermissionChannel(100, "テスト")
        target = _PermissionTarget(1, "対象")
        overwrite = discord.PermissionOverwrite(view_channel=False)

        await manager._set_permission_if_changed(
            channel, target, overwrite, reason="テスト"
        )
        await manager._set_permission_if_changed(
            channel, target, overwrite, reason="テスト"
        )

        self.assertEqual(manager.paced_calls, 1)
        self.assertEqual(channel.overwrites[target], overwrite)

    async def test_rank_room_is_visible_only_to_its_own_rank_roles(self) -> None:
        """ランク別卓は該当ランクにだけ見せる。

        以前は ADMIN_ONLY_ROOM_IDS で人間のロールへ一切allowを付けず、
        Administratorだけが@everyone denyを迂回できる「一時的に隠す」運用
        だった。正式リリースに向けてその措置をやめ、該当ランクの保持者には
        見えるのが正しい状態になった。
        """
        default = _PermissionTarget(1, "@everyone")
        bot_member = _PermissionTarget(2, "bot")
        allowed_rank_role = _PermissionTarget(
            3, rating_lib.get_rank_role_name("ブロンズ")
        )
        allowed_rank_role.permissions = SimpleNamespace(manage_guild=False)
        other_rank_role = _PermissionTarget(
            4, rating_lib.get_rank_role_name("ゴールド")
        )
        other_rank_role.permissions = SimpleNamespace(manage_guild=False)
        guild = SimpleNamespace(
            default_role=default,
            me=bot_member,
            roles=[default, allowed_rank_role, other_rank_role],
        )
        room_def = SimpleNamespace(
            room_id="beginner",
            name="初心者",
            private_owner_id=None,
            allowed_ranks=frozenset({"ブロンズ"}),
        )

        overwrites = _PermissionManager()._build_room_overwrites(guild, room_def)

        # @everyone は拒否のまま。参加条件のランクだけが迂回できる。
        self.assertFalse(overwrites[default].view_channel)
        self.assertIn(allowed_rank_role, overwrites)
        self.assertTrue(overwrites[allowed_rank_role].view_channel)
        # 帯が違うランクへは広げない。
        self.assertNotIn(other_rank_role, overwrites)

    async def test_local_room_allows_access_roles_and_server_managers(self) -> None:
        default = _PermissionTarget(1, "@everyone")
        bot_member = _PermissionTarget(2, "bot")
        member_role = _PermissionTarget(3, "コミュニティ参加者")
        invited_role = _PermissionTarget(4, "招待者")
        manager_role = _PermissionTarget(5, "運営")
        outsider_role = _PermissionTarget(6, "一般")
        for role in (member_role, invited_role, outsider_role):
            role.permissions = SimpleNamespace(manage_guild=False)
        manager_role.permissions = SimpleNamespace(manage_guild=True)
        guild = SimpleNamespace(
            default_role=default,
            me=bot_member,
            roles=[default, member_role, invited_role, manager_role, outsider_role],
        )
        room_def = SimpleNamespace(
            room_id="community",
            name="コミュニティ村",
            private_owner_id=None,
            allowed_ranks=None,
            access_role_names=frozenset({"コミュニティ参加者", "招待者"}),
        )

        overwrites = _PermissionManager()._build_room_overwrites(guild, room_def)

        self.assertFalse(overwrites[default].view_channel)
        self.assertTrue(overwrites[member_role].view_channel)
        self.assertTrue(overwrites[invited_role].view_channel)
        self.assertTrue(overwrites[manager_role].view_channel)
        self.assertNotIn(outsider_role, overwrites)

    async def test_strict_role_room_allows_only_named_role_not_managers(self) -> None:
        default = _PermissionTarget(1, "@everyone")
        bot_member = _PermissionTarget(2, "bot")
        naito_role = _PermissionTarget(3, "ねいと")
        manager_role = _PermissionTarget(4, "運営")
        outsider_role = _PermissionTarget(5, "一般")
        manager_role.permissions = SimpleNamespace(manage_guild=True)
        guild = SimpleNamespace(
            default_role=default,
            me=bot_member,
            roles=[default, naito_role, manager_role, outsider_role],
        )
        room_def = SimpleNamespace(
            room_id="open_9_turn",
            name="総合-9ターン",
            private_owner_id=None,
            allowed_ranks=None,
            access_role_names=None,
            strict_access_role_names=frozenset({"ねいと"}),
        )

        overwrites = _PermissionManager()._build_room_overwrites(guild, room_def)

        self.assertFalse(overwrites[default].view_channel)
        self.assertTrue(overwrites[naito_role].view_channel)
        self.assertNotIn(manager_role, overwrites)
        self.assertNotIn(outsider_role, overwrites)

    async def test_strict_role_room_rejects_missing_or_duplicate_role_before_writes(self) -> None:
        default = _PermissionTarget(1, "@everyone")
        bot_member = _PermissionTarget(2, "bot")
        stale_role = _PermissionTarget(3, "過去の許可")
        room_def = SimpleNamespace(
            room_id="open_9_cross",
            name="総合-9クロストーク",
            private_owner_id=None,
            allowed_ranks=None,
            access_role_names=None,
            strict_access_role_names=frozenset({"ねいと"}),
        )

        for roles in (
            [default],
            [default, _PermissionTarget(4, "ねいと"), _PermissionTarget(5, "ねいと")],
        ):
            with self.subTest(roles=[role.name for role in roles]):
                category = _PermissionChannel(100, "総合-9")
                category.overwrites[stale_role] = discord.PermissionOverwrite(
                    view_channel=True
                )
                guild = SimpleNamespace(
                    default_role=default,
                    me=bot_member,
                    roles=roles,
                    text_channels=[],
                    voice_channels=[],
                )

                with self.assertRaises(RoomVisibilityError):
                    await _PermissionManager()._apply_room_visibility(
                        guild, category, room_def,
                    )

                self.assertEqual(
                    category.overwrites[stale_role].view_channel, True,
                )
                self.assertEqual(len(category.overwrites), 1)

    async def test_strict_role_room_keeps_village_and_spirit_restricted(self) -> None:
        default = _PermissionTarget(1, "@everyone")
        bot_member = _PermissionTarget(2, "bot")
        naito_role = _PermissionTarget(3, "ねいと")
        manager_role = _PermissionTarget(4, "運営")
        manager_role.permissions = SimpleNamespace(manage_guild=True)
        category = _PermissionChannel(100, "総合-9")
        village = _PermissionChannel(101, "昼", category=category)
        spirit = _PermissionChannel(102, "霊界", category=category)
        for channel in (category, village, spirit):
            channel.overwrites[manager_role] = discord.PermissionOverwrite(
                view_channel=True
            )
        guild = SimpleNamespace(
            default_role=default,
            me=bot_member,
            roles=[default, naito_role, manager_role],
            text_channels=[village, spirit],
            voice_channels=[],
        )
        room_def = SimpleNamespace(
            room_id="open_9_cross",
            name="総合-9クロストーク",
            private_owner_id=None,
            allowed_ranks=None,
            access_role_names=None,
            strict_access_role_names=frozenset({"ねいと"}),
        )

        await _PermissionManager()._apply_room_visibility(guild, category, room_def)

        for channel in (category, village, spirit):
            self.assertFalse(channel.overwrites[default].view_channel)
            self.assertTrue(channel.overwrites[naito_role].view_channel)
            self.assertNotIn(manager_role, channel.overwrites)

    async def test_local_room_invited_role_can_join_but_outsider_cannot(self) -> None:
        from room_runner import RoomRunner

        manager = SimpleNamespace(find_user_room=lambda *_args, **_kwargs: None)
        runner = RoomRunner(
            None,
            manager,
            RoomDefinition(
                "community",
                "コミュニティ村",
                allowed_gm_user_ids=frozenset({10}),
                access_role_names=frozenset({"コミュニティ参加者"}),
            ),
        )
        runner.state.guild = SimpleNamespace(owner_id=999)

        def member(role_names: list[str]):
            return SimpleNamespace(
                id=10,
                roles=[SimpleNamespace(name=name) for name in role_names],
                guild_permissions=SimpleNamespace(
                    administrator=False,
                    manage_guild=False,
                ),
            )

        self.assertIsNotNone(await runner.validate_join(member([])))
        self.assertIsNone(await runner.validate_join(member(["コミュニティ参加者"])))

    async def test_strict_role_is_required_for_both_join_and_gm_claim(self) -> None:
        from room_runner import RoomRunner

        naito_role = _PermissionTarget(10, "ねいと")
        manager = SimpleNamespace(find_user_room=lambda *_args, **_kwargs: None)
        runner = RoomRunner(
            None,
            manager,
            RoomDefinition(
                "open_9_turn",
                "総合-9ターン",
                strict_access_role_names=frozenset({"ねいと"}),
            ),
        )
        runner.state.guild = SimpleNamespace(owner_id=999, roles=[naito_role])

        def member(*, has_naito: bool, manage_guild: bool):
            roles = [naito_role] if has_naito else []
            return SimpleNamespace(
                id=10 if has_naito else 11,
                roles=roles,
                guild_permissions=SimpleNamespace(
                    administrator=False,
                    manage_guild=manage_guild,
                ),
            )

        manager_only = member(has_naito=False, manage_guild=True)
        self.assertIn("ねいと", await runner.validate_join(manager_only))
        self.assertIn("ねいと", await runner.validate_gm_claim(manager_only))

        allowed = member(has_naito=True, manage_guild=False)
        self.assertIsNone(await runner.validate_join(allowed))
        self.assertIsNone(await runner.validate_gm_claim(allowed))

    async def test_private_room_owner_and_name_queries(self) -> None:
        await database.save_private_room(1, "private_10", 10, "十村")
        by_owner = await database.get_private_room_by_owner(1, 10)
        by_name = await database.get_private_room_by_name(1, "十村")
        self.assertEqual(by_owner["room_id"], "private_10")
        self.assertEqual(by_name["owner_id"], 10)
        self.assertEqual(by_owner["status"], "creating")

    async def test_feedback_report_keeps_game_context(self) -> None:
        report_id = await database.save_feedback_report(
            guild_id=1,
            user_id=10,
            category="分かりにくい",
            summary="投票確定の場所が分からなかった",
            details="昼の投票中に発生",
            bot_version="v0.test",
            room_id="beginner",
            room_name="初心者村",
            phase="DAY_VOTE",
            source_channel_id=123,
        )

        reports = await database.load_recent_feedback_reports(1)

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["report_id"], report_id)
        self.assertEqual(reports[0]["category"], "分かりにくい")
        self.assertEqual(reports[0]["room_id"], "beginner")
        self.assertEqual(reports[0]["phase"], "DAY_VOTE")
        self.assertEqual(reports[0]["source_channel_id"], 123)

    async def test_corrupt_room_state_is_quarantined_per_row(self) -> None:
        current_flags = {
            "public_log_archive_allowed": True,
            "vc_default_permissions_captured": False,
            "vc_gm_speak_captured": False,
            "morning_confirmed": False,
            "prep_confirmed": False,
            "mute_marker_enabled": False,
        }
        await database.save_room_state(
            1, "good", "LOBBY", {**current_flags, "players": []},
        )
        await database.save_room_state(
            1,
            "no_bite",
            "NIGHT",
            {
                **current_flags,
                "players": [
                    {"user_id": 101, "role": "WEREWOLF", "number": 1, "alive": True}
                ],
                "mute_marker_enabled": True,
                "wolf_target": -1,
                "wolf_voters": [{"user_id": 101, "target_id": -1}],
            },
        )
        async with database.connect_db() as db:
            await db.execute(
                "INSERT INTO room_states (guild_id, room_id, phase, payload) VALUES (1, 'bad', 'LOBBY', '{')"
            )
            await db.execute(
                "INSERT INTO room_states (guild_id, room_id, phase, payload) "
                "VALUES (1, 'bad_role', 'NIGHT', "
                "'{\"players\":[{\"user_id\":1,\"role\":\"NOT_A_ROLE\"}]}')"
            )
            await db.execute(
                "INSERT INTO room_states (guild_id, room_id, phase, payload) "
                "VALUES (1, 'bad_channel', 'PAUSED', "
                "'{\"players\":[],\"recovery_phase\":\"NIGHT\","
                "\"channel_ids\":{\"voice\":\"not-an-id\"}}')"
            )
            await db.execute(
                "INSERT INTO room_states (guild_id, room_id, phase, payload) "
                "VALUES (1, 'bad_winner', 'PAUSED', "
                "'{\"players\":[],\"pending_winner\":\"UNKNOWN\"}')"
            )
            await db.execute(
                "INSERT INTO room_states (guild_id, room_id, phase, payload) "
                "VALUES (1, 'bad_active_player', 'NIGHT', "
                "'{\"players\":[{\"user_id\":2,\"role\":null,\"number\":0}]}')"
            )
            await db.execute(
                "INSERT INTO room_states (guild_id, room_id, phase, payload) "
                "VALUES (1, 'bad_version', 'LOBBY', "
                "'{\"_schema_version\":0,\"players\":[]}')"
            )
            await db.execute(
                "INSERT INTO room_states (guild_id, room_id, phase, payload) "
                "VALUES (1, 'bad_marker', 'NIGHT', "
                "'{\"_schema_version\":1,"
                "\"public_log_archive_allowed\":true,"
                "\"vc_default_permissions_captured\":false,"
                "\"vc_gm_speak_captured\":false,"
                "\"morning_confirmed\":false,"
                "\"prep_confirmed\":false,\"players\":[]}')"
            )
            await db.execute(
                "INSERT INTO room_states (guild_id, room_id, phase, payload) "
                "VALUES (1, 'bad_seer_target', 'PREPARATION', "
                "'{\"_schema_version\":1,"
                "\"public_log_archive_allowed\":true,"
                "\"vc_default_permissions_captured\":false,"
                "\"vc_gm_speak_captured\":false,"
                "\"morning_confirmed\":false,\"prep_confirmed\":false,"
                "\"mute_marker_enabled\":true,"
                "\"players\":[{\"user_id\":1,\"role\":\"SEER\","
                "\"number\":1,\"alive\":true}]}')"
            )
            await db.commit()
        loaded = await database.load_room_states(1)
        self.assertIn("good", loaded)
        self.assertIn("no_bite", loaded)
        self.assertEqual(loaded["no_bite"]["wolf_target"], -1)
        self.assertNotIn("bad", loaded)
        async with database.connect_db() as db:
            rows = await db.execute_fetchall(
                "SELECT room_id, error FROM room_state_quarantine WHERE guild_id = 1 ORDER BY room_id"
            )
        self.assertEqual(
            {row[0] for row in rows},
            {
                "bad", "bad_role", "bad_channel", "bad_winner",
                "bad_active_player", "bad_version", "bad_marker", "bad_seer_target",
            },
        )
        unresolved = await database.load_unresolved_room_state_quarantine_ids(1)
        self.assertEqual(
            unresolved,
            {
                "bad", "bad_role", "bad_channel", "bad_winner",
                "bad_active_player", "bad_version", "bad_marker", "bad_seer_target",
            },
        )

        # 運用者が有効なsnapshotを復旧した卓は、隔離履歴が残っていても
        # 起動時の未解決エラーとして扱わない。
        await database.save_room_state(
            1, "bad", "LOBBY", {**current_flags, "players": []},
        )
        unresolved = await database.load_unresolved_room_state_quarantine_ids(1)
        self.assertNotIn("bad", unresolved)

    def test_snapshot_validator_covers_recovered_vote_fields(self) -> None:
        base = {
            "public_log_archive_allowed": True,
            "vc_default_permissions_captured": False,
            "vc_gm_speak_captured": False,
            "morning_confirmed": False,
            "prep_confirmed": False,
            "mute_marker_enabled": True,
            "players": [
                {"user_id": 101, "role": "VILLAGER", "number": 1, "alive": True}
            ],
        }
        valid = {
            **base,
            "pending_execution_target": 101,
            "runoff_candidates": [101],
            "vote_day_generation": 1,
            "vote_order": [101],
            "vote_slot_index": 0,
            "vote_slot_token": 2,
            "vote_slot_active": True,
            "vote_current_speaker_id": 101,
            "vote_panel_message_id": 999,
            "votes": [{"voter_id": 101, "target_id": 102}],
            "pending_death_effects": [
                {
                    "event_id": "run:処刑:1:101",
                    "player_id": 101,
                    "method": "処刑",
                    "reason": None,
                }
            ],
        }
        database._validate_room_snapshot("DAY_VOTE", valid)

        for field, value in (
            ("pending_execution_target", "bad"),
            ("runoff_candidates", [0]),
            ("vote_order", [0]),
            ("vote_order", [101, 101]),
            ("vote_slot_index", -1),
            ("vote_slot_active", "yes"),
            ("vote_panel_message_id", 0),
            ("votes", [{"voter_id": 101}]),
            ("pending_death_effects", [{"player_id": 101, "method": "処刑"}]),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                database._validate_room_snapshot("DAY_VOTE", {**base, field: value})

    def test_active_seer_snapshot_requires_initial_target(self) -> None:
        active_seer = {
            "public_log_archive_allowed": True,
            "vc_default_permissions_captured": False,
            "vc_gm_speak_captured": False,
            "morning_confirmed": False,
            "prep_confirmed": False,
            "mute_marker_enabled": True,
            "players": [
                {"user_id": 101, "role": "SEER", "number": 1, "alive": True}
            ],
        }
        with self.assertRaisesRegex(ValueError, "initial_seer_target"):
            database._validate_room_snapshot("PREPARATION", active_seer)
        database._validate_room_snapshot(
            "PREPARATION", {**active_seer, "initial_seer_target": 102},
        )
        database._validate_room_snapshot("LOBBY", active_seer)
        database._validate_room_snapshot("GAME_OVER", active_seer)

    @staticmethod
    def _records() -> list[dict]:
        records = []
        for index in range(13):
            wolf_team = index < 4
            records.append({
                "player_id": 1000 + index,
                "role": "人狼" if index < 3 else "村人",
                "team": Team.WOLF.value if wolf_team else Team.VILLAGE.value,
                "won": int(wolf_team),
            })
        return records

    async def test_settlement_is_idempotent_and_pending_is_recoverable(self) -> None:
        await database.stage_game_settlement(
            1,
            "open",
            "run-1",
            room_name="総合",
            rated=True,
            winner_team=Team.WOLF.value,
            player_records=self._records(),
        )
        pending = await database.load_pending_game_settlements(1)
        self.assertEqual([row["game_run_id"] for row in pending], ["run-1"])

        first_id, first_results, first_created = await database.settle_game_settlement(
            1, "open", "run-1"
        )
        second_id, second_results, second_created = await database.settle_game_settlement(
            1, "open", "run-1"
        )
        self.assertEqual(first_id, second_id)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_results, second_results)

        async with database.connect_db() as db:
            games = (await db.execute_fetchall("SELECT COUNT(*) FROM games"))[0][0]
            players = (await db.execute_fetchall("SELECT COUNT(*) FROM game_players"))[0][0]
            history = (await db.execute_fetchall("SELECT COUNT(*) FROM rating_history"))[0][0]
            season_games = (await db.execute_fetchall(
                "SELECT SUM(season_games) FROM player_ratings"
            ))[0][0]
        self.assertEqual(games, 1)
        self.assertEqual(players, 13)
        self.assertEqual(history, 13)
        self.assertEqual(season_games, 13)

    async def test_postgame_recommendations_stack_and_are_idempotent(self) -> None:
        await database.stage_game_settlement(
            1,
            "open",
            "run-recommendation",
            room_name="総合",
            rated=True,
            winner_team=Team.WOLF.value,
            player_records=self._records(),
        )
        game_id, _, _ = await database.settle_game_settlement(
            1, "open", "run-recommendation"
        )
        voters = {1000, 1004, 1005}
        await database.create_game_recommendation_ballots(
            game_id, 1, voters, timeout_seconds=180
        )

        self.assertEqual(
            await database.confirm_game_recommendation(game_id, 1, 1000, 1000),
            "self",
        )
        for voter_id in voters:
            self.assertEqual(
                await database.confirm_game_recommendation(
                    game_id, 1, voter_id, 1006
                ),
                "confirmed",
            )
        self.assertEqual(
            await database.confirm_game_recommendation(game_id, 1, 1000, 1007),
            "already_other",
        )

        before = {r["player_id"]: r["rating"]
                  for r in await database.get_all_player_ratings(1)}[1006]
        first = await database.finalize_game_recommendations(game_id, 1)
        second = await database.finalize_game_recommendations(game_id, 1)
        after = {r["player_id"]: r["rating"]
                 for r in await database.get_all_player_ratings(1)}[1006]

        self.assertEqual(first, [{
            "player_id": 1006,
            "bonus": 3,
            "rating_before": before,
            "rating_after": before + 3,
        }])
        self.assertEqual(second, [])
        self.assertEqual(after, before + 3)
        history = await database.get_player_recent_games(1006, 1, limit=1)
        self.assertEqual(history[0]["recommendation_bonus"], 3)
        self.assertEqual(
            history[0]["rating_after"] - history[0]["rating_before"],
            history[0]["elo_delta"] + history[0]["bonus"] + 3,
        )

    async def test_recommendation_voter_conditions_are_deduplicated(self) -> None:
        from config import Role
        from models import GameState, Player
        from room_runner import RoomRunner

        state = GameState()
        players = {
            1: Player(1, SimpleNamespace(display_name="GM参加者"), role=Role.MEDIUM),
            2: Player(2, SimpleNamespace(display_name="初夜死者"), role=Role.VILLAGER),
        }
        state.players = players
        state.gm_id = 1
        state.day1_executed_id = 1
        state.night1_killed_id = 2

        self.assertEqual(RoomRunner._postgame_recommendation_voters(state), {1, 2})

    async def test_game_cog_periodic_recovery_uses_pending_queue(self) -> None:
        await database.stage_game_settlement(
            1,
            "open",
            "run-periodic",
            room_name="総合",
            rated=True,
            winner_team=Team.WOLF.value,
            player_records=self._records(),
        )
        manager = GameCog(SimpleNamespace(managed_guild_id=1))
        await manager._recover_pending_settlements(SimpleNamespace(id=1))
        self.assertEqual(await database.load_pending_game_settlements(1), [])
        self.assertEqual(manager._settlement_recovered_since_notice, 1)

    async def test_pending_unmutes_concurrent_add_remove(self) -> None:
        await asyncio.gather(
            database.add_pending_unmutes(1, {1, 2}),
            database.add_pending_unmutes(1, {2, 3}),
        )
        self.assertEqual(await database.load_pending_unmute_ids(1), {1, 2, 3})
        await asyncio.gather(
            database.remove_pending_unmute(1, 1),
            database.remove_pending_unmute(1, 3),
        )
        self.assertEqual(await database.load_pending_unmute_ids(1), {2})

    async def test_private_room_rename_journal(self) -> None:
        await database.save_private_room(1, "private_10", 10, "旧村")
        self.assertTrue(await database.checkpoint_private_room_asset_ids(
            1, "private_10", category_id=100,
        ))
        self.assertFalse(await database.checkpoint_private_room_asset_ids(
            1, "private_10", category_id=101,
        ))
        await database.mark_private_room_active(
            1, "private_10", category_id=100
        )
        await database.update_private_room_names(1, "private_10", "新村")
        renamed = await database.get_private_room_by_name(1, "新村")
        self.assertEqual(renamed["status"], "renaming")
        self.assertEqual(renamed["category_id"], 100)

    async def test_public_log_category_names_are_reserved_for_private_rooms(self) -> None:
        manager = GameCog(SimpleNamespace(managed_guild_id=1))
        guild = SimpleNamespace(categories=[], roles=[])

        for name in (LOG_CATEGORY_VILLAGE, LOG_CATEGORY_SPIRIT):
            self.assertIsNotNone(manager._private_room_name_error(guild, name))

    async def test_rank_role_replacement_is_one_atomic_member_patch(self) -> None:
        ordinary = _PermissionTarget(100, "通常ロール")
        old_rank = _PermissionTarget(
            101, rating_lib.get_rank_role_name("アイアン")
        )
        stale_rank = _PermissionTarget(
            102, rating_lib.get_rank_role_name("ブロンズ")
        )
        target_rank = _PermissionTarget(
            103, rating_lib.get_rank_role_name("シルバー")
        )
        guild = SimpleNamespace(
            id=1,
            roles=[ordinary, old_rank, stale_rank, target_rank],
            members=[],
        )
        member = _PrivateMember(
            guild, 10, "player", roles=[ordinary, old_rank, stale_rank]
        )
        guild.members = [member]
        guild.get_member = lambda member_id: member if member_id == member.id else None
        manager = GameCog(SimpleNamespace(managed_guild_id=1))
        manager.bulk_api_interval = 0

        outcome = await manager._sync_rank_role(
            member,
            "シルバー",
            roles_map={target_rank.name: target_rank},
        )

        self.assertEqual(outcome, "updated")
        self.assertEqual(len(member.edit_calls), 1)
        self.assertEqual(member.roles, [ordinary, target_rank])

    async def test_three_ladder_rank_roles_are_merged_into_one_member_patch(self) -> None:
        ordinary = _PermissionTarget(110, "通常ロール")
        old_l13_rank = _PermissionTarget(
            111, rating_lib.get_rank_role_name("アイアン")
        )
        target_l13_rank = _PermissionTarget(
            112, rating_lib.get_rank_role_name("シルバー")
        )
        target_l9_gm = _PermissionTarget(
            113, rating_lib.special_grandmaster_role_name("l9_cross")
        )
        target_l9t_gm = _PermissionTarget(
            114, rating_lib.special_grandmaster_role_name("l9_turn")
        )
        guild = SimpleNamespace(
            id=1,
            roles=[
                ordinary, old_l13_rank, target_l13_rank,
                target_l9_gm, target_l9t_gm,
            ],
            members=[],
        )
        member = _PrivateMember(
            guild, 10, "player", roles=[ordinary, old_l13_rank]
        )
        guild.members = [member]
        guild.get_member = lambda member_id: member if member_id == member.id else None
        manager = GameCog(SimpleNamespace(managed_guild_id=1))
        manager.bulk_api_interval = 0

        outcome = await manager._sync_rank_role(
            member,
            roles_map={
                target_l13_rank.name: target_l13_rank,
                target_l9_gm.name: target_l9_gm,
                target_l9t_gm.name: target_l9t_gm,
            },
            rank_names_by_ladder={
                "l13": "シルバー",
                "l9_cross": "グランドマスター",
                "l9_turn": "グランドマスター",
            },
        )

        self.assertEqual(outcome, "updated")
        self.assertEqual(len(member.edit_calls), 1)
        self.assertEqual(member.roles[0], ordinary)
        self.assertCountEqual(
            member.roles[1:], [target_l13_rank, target_l9_gm, target_l9t_gm],
        )


    async def test_only_grandmaster_roles_are_forced_to_hoist(self) -> None:
        class EditableRole(_PermissionTarget):
            def __init__(
                self, target_id: int, name: str, *, hoist: bool, position: int
            ) -> None:
                super().__init__(target_id, name)
                self.hoist = hoist
                self.position = position
                self.edit_calls: list[dict] = []

            async def edit(self, **kwargs):
                self.edit_calls.append(dict(kwargs))
                if "hoist" in kwargs:
                    self.hoist = bool(kwargs["hoist"])
                if "position" in kwargs:
                    self.position = int(kwargs["position"])
                return self

        roles = []
        for index, (name, _color) in enumerate(rating_lib.all_rank_role_specs(), 1):
            roles.append(
                EditableRole(
                    200 + index,
                    name,
                    # 通常ロールの手動hoistはBotが勝手に戻さない。
                    hoist=name == rating_lib.get_rank_role_name("シルバー"),
                    position=index,
                )
            )
        roles_by_name = {role.name: role for role in roles}
        l13_gm = roles_by_name[
            rating_lib.special_grandmaster_role_name("l13")
        ]
        l9_gm = roles_by_name[
            rating_lib.special_grandmaster_role_name("l9_cross")
        ]
        l9t_gm = roles_by_name[
            rating_lib.special_grandmaster_role_name("l9_turn")
        ]
        # 並びも逆にし、13人村側を上へ直す経路を同時に確認する。
        l13_gm.position = 5
        l9_gm.position = 10
        l9t_gm.position = 11
        silver = roles_by_name[rating_lib.get_rank_role_name("シルバー")]

        async def unexpected_create_role(**_kwargs):
            self.fail("全ランクロールを用意済みなのにcreate_roleが呼ばれました")

        guild = SimpleNamespace(
            id=1,
            roles=roles,
            create_role=unexpected_create_role,
        )
        manager = GameCog(SimpleNamespace(managed_guild_id=1))
        manager.bulk_api_interval = 0

        result = await manager._ensure_rank_roles(guild)

        self.assertEqual(set(result), set(roles_by_name))
        self.assertTrue(l13_gm.hoist)
        self.assertTrue(l9_gm.hoist)
        self.assertTrue(l9t_gm.hoist)
        self.assertTrue(silver.hoist)
        self.assertEqual(silver.edit_calls, [])
        self.assertTrue(any(call.get("hoist") is True for call in l13_gm.edit_calls))
        self.assertTrue(any(call.get("hoist") is True for call in l9_gm.edit_calls))
        self.assertTrue(any(call.get("hoist") is True for call in l9t_gm.edit_calls))
        self.assertTrue(any("position" in call for call in l13_gm.edit_calls))

    async def test_duplicate_history_keeps_nonunique_lookup_indexes(self) -> None:
        async with database.connect_db() as db:
            cursor = await db.execute(
                "INSERT INTO games (guild_id, winner_team) VALUES (1, '人狼陣営')"
            )
            game_id = int(cursor.lastrowid)
            await db.executemany(
                "INSERT INTO game_players (game_id, player_id, role, team, won) "
                "VALUES (?, 99, '村人', '村人陣営', 0)",
                [(game_id,), (game_id,)],
            )
            await db.executemany(
                "INSERT INTO rating_history "
                "(player_id, guild_id, game_id, rating_before, rating_after, elo_delta, bonus) "
                "VALUES (99, 1, ?, 1500, 1490, -10, 0)",
                [(game_id,), (game_id,)],
            )
            await db.commit()
        # 重複履歴を破壊せずinitが完了し、検索用indexは非uniqueになる。
        await database.init_db()
        async with database.connect_db() as db:
            gp_count = (await db.execute_fetchall(
                "SELECT COUNT(*) FROM game_players WHERE game_id = ? AND player_id = 99",
                (game_id,),
            ))[0][0]
            indexes = await db.execute_fetchall("PRAGMA index_list(game_players)")
        self.assertEqual(gp_count, 2)
        target = next(row for row in indexes if row[1] == "idx_game_players_game_player")
        self.assertEqual(target[2], 0)

    async def test_season_reset_rejects_stale_or_consecutive_request(self) -> None:
        await database.stage_game_settlement(
            1,
            "open",
            "run-season",
            room_name="総合",
            rated=True,
            winner_team=Team.WOLF.value,
            player_records=self._records(),
        )
        await database.settle_game_settlement(1, "open", "run-season")
        expected = await database.get_season_start(1)
        reset_id, affected = await database.season_half_reset(
            1, 999, expected_season_start=expected
        )
        self.assertGreater(reset_id, 0)
        self.assertEqual(affected, 13)
        with self.assertRaises(database.SeasonResetConflict):
            await database.season_half_reset(
                1, 999, expected_season_start=expected
            )

    async def test_feedback_reports_are_capped_per_user_per_day(self) -> None:
        """誰でも押せるフォームなので、1人あたりの日次件数で上限をかける。"""
        from config import FEEDBACK_MAX_PER_DAY

        async def submit(user_id: int) -> int:
            return await database.save_feedback_report(
                guild_id=1,
                user_id=user_id,
                category="不具合",
                summary="テスト報告",
                details=None,
                bot_version="v0.0",
            )

        for _ in range(FEEDBACK_MAX_PER_DAY):
            await submit(100)

        with self.assertRaises(database.FeedbackRateLimited):
            await submit(100)

        # 上限は利用者ごと。他の人の報告は引き続き受け付ける
        await submit(200)

        rows = await database.load_recent_feedback_reports(1, limit=200)
        self.assertEqual(
            len([r for r in rows if r["user_id"] == 100]), FEEDBACK_MAX_PER_DAY
        )
        self.assertEqual(len([r for r in rows if r["user_id"] == 200]), 1)


class ParseSelectIdTest(unittest.TestCase):
    """セレクトの値はクライアント送信なので、int()前に必ず検証する。"""

    def test_accepts_ids_and_rejects_crafted_values(self) -> None:
        from models import parse_select_id

        self.assertEqual(parse_select_id("123456789012345678"), 123456789012345678)
        self.assertEqual(parse_select_id("-1"), -1)  # 「噛みなし」の番兵
        self.assertEqual(parse_select_id(42), 42)

        crafted_values = (
            "", "abc", "1; DROP TABLE games", None, [], {}, 1.5,
            # int() はUnicode数字も解釈するので明示的に弾く
            "１２３", "١٢٣", "０",
            # 前後の空白・符号・区切り・別表記も受け付けない
            " 12", "12 ", "+12", "--1", "-", "12_3", "0x1f", "1e3",
        )
        for crafted in crafted_values:
            with self.subTest(crafted=crafted):
                self.assertIsNone(parse_select_id(crafted))

    def test_bool_is_not_treated_as_id(self) -> None:
        from models import parse_select_id

        # PythonではTrueがint扱いになるため明示的に弾く
        self.assertIsNone(parse_select_id(True))
        self.assertIsNone(parse_select_id(False))


if __name__ == "__main__":
    unittest.main()
