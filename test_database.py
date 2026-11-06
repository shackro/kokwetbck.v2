import os
import sqlite3
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from migrate_sqlite_to_postgres import (
    _apply_legacy_staff_defaults,
    _digest_rows,
    _ordered_tables,
    _validate_existing_schema,
    MigrationError,
)
from app.database import (
    HybridRow,
    PostgresConnection,
    _split_sql_script,
    _translate_postgres_sql,
    _hybrid_dict_row,
    _postgres_connection,
    database_schema,
    connect,
    database_url,
    initialize_database,
)


class _FakeCursor:
    rowcount = 1

    def fetchone(self):
        return None


class _FakePostgres:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement, parameters=()):
        self.statements.append((statement, parameters))
        return _FakeCursor()

    def close(self):
        pass


class PostgresDialectTests(unittest.TestCase):
    def test_translates_sqlite_types_and_datetime_defaults(self):
        statement = _translate_postgres_sql(
            """
            CREATE TABLE sample (
                sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
                content BLOB,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        self.assertIn("sample_id BIGSERIAL PRIMARY KEY", statement)
        self.assertIn("content BYTEA", statement)
        self.assertIn("TO_CHAR(CURRENT_TIMESTAMP AT TIME ZONE 'UTC'", statement)

    def test_translates_positional_parameters_without_touching_literals(self):
        statement = _translate_postgres_sql(
            "SELECT '?' AS marker, item_id IS ? FROM items WHERE label = 'it''s ?'"
        )
        self.assertEqual(
            statement,
            "SELECT '?' AS marker, item_id IS NOT DISTINCT FROM %s "
            "FROM items WHERE label = 'it''s ?'",
        )

    def test_translates_ignore_inserts(self):
        statement = _translate_postgres_sql(
            "INSERT OR IGNORE INTO content (content_id) VALUES (?)"
        )
        self.assertEqual(
            statement,
            "INSERT INTO content (content_id) VALUES (%s) ON CONFLICT DO NOTHING",
        )

    def test_splits_schema_scripts_without_splitting_quoted_semicolons(self):
        self.assertEqual(
            _split_sql_script("CREATE TABLE a (v TEXT DEFAULT 'x;y'); CREATE INDEX i ON a(v);"),
            ["CREATE TABLE a (v TEXT DEFAULT 'x;y')", "CREATE INDEX i ON a(v)"],
        )

    def test_hybrid_row_supports_mapping_and_sqlite_positional_access(self):
        row = HybridRow((("first", 11), ("second", 22)))
        self.assertEqual(row["second"], 22)
        self.assertEqual(row[0], 11)
        self.assertEqual(row[1], 22)

    def test_row_factory_handles_statements_without_result_columns(self):
        cursor = Mock(description=None)
        make_row = _hybrid_dict_row(cursor)
        self.assertIsNone(make_row(None))

    def test_row_factory_preserves_mapping_and_positional_query_results(self):
        cursor = Mock(
            description=[
                SimpleNamespace(name="first"),
                SimpleNamespace(name="second"),
            ]
        )
        make_row = _hybrid_dict_row(cursor)
        row = make_row((11, 22))
        self.assertEqual(row["second"], 22)
        self.assertEqual(row[0], 11)

    def test_migration_orders_foreign_key_parents_before_children(self):
        source = sqlite3.connect(":memory:")
        source.row_factory = sqlite3.Row
        source.executescript(
            """
            CREATE TABLE parents (parent_id INTEGER PRIMARY KEY);
            CREATE TABLE children (
                child_id INTEGER PRIMARY KEY,
                parent_id INTEGER REFERENCES parents(parent_id)
            );
            """
        )
        self.assertEqual(
            _ordered_tables(source, ["children", "parents"]),
            ["parents", "children"],
        )
        source.close()

    def test_migration_digest_is_order_independent_and_covers_binary_values(self):
        first = _digest_rows([("row", b"\x00\xff"), ("other", None)])
        second = _digest_rows([("other", None), ("row", memoryview(b"\x00\xff"))])
        self.assertEqual(first, second)

    def test_migration_preserves_legacy_staff_permission_backfills(self):
        source = sqlite3.connect(":memory:")
        source.row_factory = sqlite3.Row
        source.execute("CREATE TABLE staff_users (role TEXT)")
        target = _FakePostgres()
        _apply_legacy_staff_defaults(source, target, ["staff_users"])
        self.assertIn("can_manage_stories = 1", target.statements[0][0])
        self.assertIn("house_names_json = %s", target.statements[1][0])
        self.assertTrue(target.statements[1][1][0].startswith("["))
        source.close()

    def test_incompatible_preexisting_live_schema_fails_before_migration(self):
        connection = Mock()
        connection.execute.side_effect = [
            Mock(fetchone=Mock(return_value={"data_type": "uuid"})),
            Mock(
                fetchall=Mock(
                    return_value=[
                        {"column_name": "session_id"},
                        {"column_name": "creator_name"},
                        {"column_name": "platform"},
                        {"column_name": "stream_url"},
                        {"column_name": "topic_title"},
                        {"column_name": "is_active"},
                        {"column_name": "started_at"},
                    ]
                )
            ),
        ]
        with self.assertRaisesRegex(MigrationError, "incompatible live_sessions"):
            _validate_existing_schema(connection)

    def test_connection_translates_queries_and_immediate_transactions(self):
        raw = _FakePostgres()
        connection = PostgresConnection(raw, RuntimeError)
        connection.execute("SELECT value FROM items WHERE item_id IS ?", (None,))
        self.assertEqual(
            raw.statements[-1],
            (
                "SELECT value FROM items WHERE item_id IS NOT DISTINCT FROM %s",
                (None,),
            ),
        )
        connection.execute("BEGIN IMMEDIATE")
        self.assertEqual(raw.statements[-1][0], "SELECT pg_advisory_xact_lock(%s)")

    def test_configured_database_url_never_opens_sqlite(self):
        import app.database as database

        raw = _FakePostgres()
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://private.invalid/kokwet",
                "KOKWET_REQUIRE_DATABASE_URL": "true",
                "KOKWET_USE_SQLITE": "false",
                "KOKWET_DB_SCHEMA": "kokwet_app",
            },
        ), patch.object(
            database,
            "_postgres_connection",
            return_value=(raw, RuntimeError),
        ), patch.object(
            database,
            "_sqlite_connection",
            side_effect=AssertionError("SQLite must not be opened"),
        ):
            with connect() as connection:
                connection.execute("SELECT 1")
        self.assertEqual(raw.statements[0][0], "SELECT 1")

    def test_postgres_connection_enforces_tls_and_pooler_safe_options(self):
        fake_driver = ModuleType("psycopg")
        fake_driver.IntegrityError = type("IntegrityError", (Exception,), {})
        fake_driver.connect = Mock(return_value=object())
        with patch.dict(
            os.environ,
            {
                "KOKWET_DB_SCHEMA": "kokwet_app",
            },
        ), patch.dict(sys.modules, {"psycopg": fake_driver}):
            connection, _ = _postgres_connection("postgresql://db.invalid/kokwet")
            self.assertIsNotNone(connection)
            fake_driver.connect.assert_called_once_with(
                "postgresql://db.invalid/kokwet",
                connect_timeout=10,
                row_factory=_hybrid_dict_row,
                prepare_threshold=None,
                options="-c lock_timeout=10000 -c search_path=kokwet_app",
                sslmode="require",
            )
            with self.assertRaisesRegex(RuntimeError, "TLS"):
                _postgres_connection(
                    "postgresql://db.invalid/kokwet?sslmode=disable"
                )

    def test_postgres_connection_uses_configured_schema_search_path(self):
        fake_driver = ModuleType("psycopg")
        fake_driver.IntegrityError = type("IntegrityError", (Exception,), {})
        fake_driver.connect = Mock(return_value=object())
        with patch.dict(
            os.environ,
            {"KOKWET_DB_SCHEMA": "kokwet_app"},
        ), patch.dict(sys.modules, {"psycopg": fake_driver}):
            _postgres_connection("postgresql://db.invalid/kokwet")
        self.assertEqual(
            fake_driver.connect.call_args.kwargs["options"],
            "-c lock_timeout=10000 -c search_path=kokwet_app",
        )

    def test_database_schema_rejects_sql_or_qualified_names(self):
        with patch.dict(
            os.environ,
            {"KOKWET_DB_SCHEMA": "public; DROP SCHEMA public"},
        ):
            with self.assertRaisesRegex(RuntimeError, "simple PostgreSQL schema"):
                database_schema()

    def test_database_schema_is_required_for_all_postgres_connections(self):
        with patch.dict(
            os.environ,
            {"KOKWET_DB_SCHEMA": ""},
        ):
            with self.assertRaisesRegex(RuntimeError, "KOKWET_DB_SCHEMA is required"):
                database_schema()

    def test_postgres_is_selected_only_for_a_configured_url(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "development",
                "DATABASE_URL": "",
                "KOKWET_REQUIRE_DATABASE_URL": "false",
            },
            clear=True,
        ):
            self.assertIsNone(database_url())
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "development",
                "DATABASE_URL": "",
                "KOKWET_REQUIRE_DATABASE_URL": "true",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL is required"):
                database_url()

    def test_explicit_local_sqlite_overrides_configured_postgres_url(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "development",
                "DATABASE_URL": "postgresql://render.invalid/kokwet",
                "KOKWET_REQUIRE_DATABASE_URL": "false",
                "KOKWET_USE_SQLITE": "true",
            },
            clear=True,
        ):
            self.assertIsNone(database_url())

    def test_local_sqlite_override_is_rejected_when_postgres_is_required(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "DATABASE_URL": "postgresql://render.invalid/kokwet",
                "KOKWET_USE_SQLITE": "true",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot be enabled"):
                database_url()

    def test_production_requires_postgres_even_without_blueprint_guard(self):
        with patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "DATABASE_URL": "",
                "KOKWET_REQUIRE_DATABASE_URL": "false",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "DATABASE_URL is required"):
                database_url()

    def test_database_initialization_creates_dedicated_schema(self):
        raw = _FakePostgres()
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://private.invalid/kokwet",
                "APP_ENV": "production",
                "KOKWET_DB_SCHEMA": "kokwet_app",
            },
            clear=True,
        ), patch("app.database.connect") as mocked_connect:
            mocked_connect.return_value.__enter__.return_value = raw
            initialize_database()
        self.assertEqual(
            raw.statements,
            [
                ('CREATE SCHEMA IF NOT EXISTS "kokwet_app"', ()),
                ("SELECT 1", ()),
            ],
        )

    def test_healthcheck_hides_database_connection_errors(self):
        import main

        with patch.object(main, "check_database_connection"):
            self.assertEqual(main.health()["status"], "online")
        with patch.object(
            main,
            "check_database_connection",
            side_effect=RuntimeError("private connection detail"),
        ):
            with self.assertRaises(HTTPException) as raised:
                main.health()
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "Database unavailable")


if __name__ == "__main__":
    unittest.main()
