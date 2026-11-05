from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

from app.database import _postgres_connection, database_url

_BATCH_SIZE = 500
_DEFAULT_SQLITE_PATH = Path(__file__).resolve().parent / "data" / "kokwet.sqlite3"


class MigrationError(RuntimeError):
    pass


def _sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _open_source(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError("The specified SQLite database file does not exist")
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro",
        uri=True,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("BEGIN")
    return connection


def _source_tables(connection: sqlite3.Connection) -> list[str]:
    tables = [
        row["name"]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
    ]
    if not tables:
        raise MigrationError("The SQLite database contains no application tables")
    return tables


def _ordered_tables(connection: sqlite3.Connection, tables: list[str]) -> list[str]:
    dependencies: dict[str, set[str]] = {table: set() for table in tables}
    for table in tables:
        pragma = f"PRAGMA foreign_key_list({_sqlite_identifier(table)})"
        for row in connection.execute(pragma).fetchall():
            parent = row["table"]
            if parent in dependencies and parent != table:
                dependencies[table].add(parent)

    pending = set(tables)
    ordered: list[str] = []
    while pending:
        ready = sorted(
            table
            for table in pending
            if dependencies[table].isdisjoint(pending)
        )
        if not ready:
            raise MigrationError("The SQLite schema has cyclic foreign-key dependencies")
        ordered.extend(ready)
        pending.difference_update(ready)
    return ordered


def _column_names(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = connection.execute(
        f"PRAGMA table_info({_sqlite_identifier(table)})"
    ).fetchall()
    return [row["name"] for row in rows]


def _canonical_value(value: Any) -> Any:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if hasattr(value, "isoformat"):
        return {"isoformat": value.isoformat()}
    return value


def _digest_rows(rows: Iterable[Iterable[Any]]) -> tuple[int, str]:
    row_hashes: list[bytes] = []
    count = 0
    for row in rows:
        encoded = json.dumps(
            [_canonical_value(value) for value in row],
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        row_hashes.append(hashlib.sha256(encoded).digest())
        count += 1
    digest = hashlib.sha256(b"".join(sorted(row_hashes))).hexdigest()
    return count, digest


def _insert_statement(psycopg: Any, table: str, columns: list[str]) -> Any:
    identifiers = psycopg.sql.SQL(", ").join(
        psycopg.sql.Identifier(column) for column in columns
    )
    placeholders = psycopg.sql.SQL(", ").join(
        psycopg.sql.Placeholder() for _ in columns
    )
    return psycopg.sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        psycopg.sql.Identifier(table),
        identifiers,
        placeholders,
    )


def _target_columns(connection: Any, table: str) -> set[str]:
    rows = connection.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = %s
        """,
        (table,),
    ).fetchall()
    return {row["column_name"] for row in rows}


def _assert_empty_destination(connection: Any, psycopg: Any) -> None:
    tables = connection.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    ).fetchall()
    for row in tables:
        table = row["table_name"]
        has_rows = connection.execute(
            psycopg.sql.SQL("SELECT EXISTS (SELECT 1 FROM {} LIMIT 1)").format(
                psycopg.sql.Identifier(table)
            )
        ).fetchone()[0]
        if has_rows:
            raise MigrationError(
                f"Destination table {table} is not empty; refusing to merge data"
            )


def _validate_existing_schema(connection: Any) -> None:
    live_sessions = connection.execute(
        """
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'live_sessions'
          AND column_name = 'session_id'
        """
    ).fetchone()
    if live_sessions is None:
        return

    columns = {
        row["column_name"]
        for row in connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'live_sessions'
            """
        ).fetchall()
    }
    required_columns = {
        "session_id",
        "creator_name",
        "platform",
        "stream_url",
        "topic_title",
        "is_active",
        "started_at",
        "ended_at",
        "created_by",
    }
    if live_sessions["data_type"] not in {"text", "character varying"} or not required_columns <= columns:
        raise MigrationError(
            "The destination already has an incompatible live_sessions table. "
            "Use a clean PostgreSQL schema for migration; do not drop the existing table."
        )


def _check_destination_before_schema(url: str, psycopg: Any) -> None:
    target, _ = _postgres_connection(url)
    try:
        with target:
            _assert_empty_destination(target, psycopg)
            _validate_existing_schema(target)
    finally:
        target.close()


def _target_row_digest(connection: Any, psycopg: Any, table: str, columns: list[str]) -> tuple[int, str]:
    selected_columns = psycopg.sql.SQL(", ").join(
        psycopg.sql.Identifier(column) for column in columns
    )
    statement = psycopg.sql.SQL("SELECT {} FROM {}").format(
        selected_columns,
        psycopg.sql.Identifier(table),
    )
    cursor = connection.execute(statement)

    def rows() -> Iterable[tuple[Any, ...]]:
        while batch := cursor.fetchmany(_BATCH_SIZE):
            for row in batch:
                yield tuple(row[column] for column in columns)

    return _digest_rows(rows())


def _copy_table(
    source: sqlite3.Connection,
    target: Any,
    psycopg: Any,
    table: str,
    columns: list[str],
) -> tuple[int, str]:
    source_cursor = source.execute(
        f"SELECT * FROM {_sqlite_identifier(table)}"
    )
    source_column_positions = {
        description[0]: index
        for index, description in enumerate(source_cursor.description)
    }
    column_positions = [source_column_positions[column] for column in columns]
    insert = _insert_statement(psycopg, table, columns)
    row_hashes: list[bytes] = []
    count = 0

    with target.cursor() as cursor:
        while batch := source_cursor.fetchmany(_BATCH_SIZE):
            values = [
                tuple(row[position] for position in column_positions)
                for row in batch
            ]
            cursor.executemany(insert, values)
            for row in values:
                encoded = json.dumps(
                    [_canonical_value(value) for value in row],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                row_hashes.append(hashlib.sha256(encoded).digest())
            count += len(values)
    digest = hashlib.sha256(b"".join(sorted(row_hashes))).hexdigest()
    return count, digest


def _reset_sequences(
    source: sqlite3.Connection,
    target: Any,
    psycopg: Any,
    tables: list[str],
) -> None:
    for table in tables:
        for column in source.execute(
            f"PRAGMA table_info({_sqlite_identifier(table)})"
        ).fetchall():
            if not column["pk"] or "INT" not in column["type"].upper():
                continue
            sequence = target.execute(
                """
                SELECT n.nspname AS schema_name, c.relname AS sequence_name
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE c.oid = pg_get_serial_sequence(%s, %s)::regclass
                """,
                (table, column["name"]),
            ).fetchone()
            if not sequence:
                continue
            maximum = target.execute(
                psycopg.sql.SQL("SELECT MAX({}) AS maximum FROM {}").format(
                    psycopg.sql.Identifier(column["name"]),
                    psycopg.sql.Identifier(table),
                )
            ).fetchone()["maximum"]
            restart_with = 1 if maximum is None else maximum + 1
            target.execute(
                psycopg.sql.SQL("ALTER SEQUENCE {}.{} RESTART WITH {}").format(
                    psycopg.sql.Identifier(sequence["schema_name"]),
                    psycopg.sql.Identifier(sequence["sequence_name"]),
                    psycopg.sql.Literal(restart_with),
                )
            )


def _apply_legacy_staff_defaults(
    source: sqlite3.Connection,
    target: Any,
    source_tables: list[str],
) -> None:
    if "staff_users" not in source_tables:
        return
    columns = set(_column_names(source, "staff_users"))
    if "can_manage_stories" not in columns:
        target.execute(
            "UPDATE staff_users SET can_manage_stories = 1 WHERE role = 'creator'"
        )
    if "house_names_json" not in columns:
        from app.constants import HOUSE_NAMES

        target.execute(
            "UPDATE staff_users SET house_names_json = %s WHERE role = 'moderator'",
            (json.dumps(HOUSE_NAMES),),
        )


def _initialize_schema() -> None:
    from app.catalog import initialize_catalog_store
    from app.culture import initialize_culture_store
    from app.database import initialize_database
    from app.impact import initialize_impact_store
    from app.intake import initialize_intake_store
    from app.live import initialize_live_store
    from app.media import initialize_media_store
    from app.members import initialize_member_store
    from app.publishing import initialize_publishing_store
    from app.roadmap import initialize_roadmap_store
    from app.site_content import initialize_site_content_store

    initialize_database()
    initialize_culture_store()
    initialize_publishing_store(seed_data=False)
    initialize_impact_store(seed_data=False)
    initialize_roadmap_store(seed_data=False)
    initialize_site_content_store(seed_data=False)
    initialize_live_store()
    initialize_catalog_store(seed_data=False)
    initialize_member_store()
    initialize_media_store()
    initialize_intake_store()


def migrate(sqlite_path: Path) -> int:
    url = database_url()
    if not url:
        raise MigrationError("DATABASE_URL must contain a private PostgreSQL connection URL")

    source = _open_source(sqlite_path)
    try:
        source_tables = _source_tables(source)
        ordered_tables = _ordered_tables(source, source_tables)
        import psycopg

        _check_destination_before_schema(url, psycopg)
        _initialize_schema()
        target, _ = _postgres_connection(url)
        try:
            with target:
                _assert_empty_destination(target, psycopg)
                for table in ordered_tables:
                    target_columns = _target_columns(target, table)
                    source_columns = _column_names(source, table)
                    if not target_columns:
                        raise MigrationError(f"Destination schema is missing table {table}")
                    missing_columns = set(source_columns) - target_columns
                    if missing_columns:
                        raise MigrationError(f"Destination schema is missing columns in {table}")

                migrated = 0
                expected: dict[str, tuple[int, str]] = {}
                for table in ordered_tables:
                    columns = _column_names(source, table)
                    expected[table] = _copy_table(
                        source,
                        target,
                        psycopg,
                        table,
                        columns,
                    )
                    migrated += expected[table][0]

                for table in ordered_tables:
                    columns = _column_names(source, table)
                    actual = _target_row_digest(target, psycopg, table, columns)
                    if actual != expected[table]:
                        raise MigrationError(f"Validation failed for table {table}")

                _apply_legacy_staff_defaults(source, target, source_tables)
                _reset_sequences(source, target, psycopg, ordered_tables)
            return migrated
        finally:
            target.close()
    finally:
        source.close()


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    parser = argparse.ArgumentParser(
        description="Copy all application tables from SQLite into an empty PostgreSQL database."
    )
    parser.add_argument(
        "--sqlite-path",
        type=Path,
        default=Path(os.getenv("KOKWET_DB_PATH", _DEFAULT_SQLITE_PATH)),
        help="SQLite database file (defaults to KOKWET_DB_PATH or backend/data/kokwet.sqlite3)",
    )
    arguments = parser.parse_args()
    try:
        total_rows = migrate(arguments.sqlite_path)
    except Exception as error:
        detail = str(error) if isinstance(error, MigrationError) else type(error).__name__
        print(
            "Migration failed; SQLite was not changed and any uncommitted PostgreSQL "
            f"data-copy writes were rolled back ({detail}).",
            file=sys.stderr,
        )
        return 1
    print(f"Migration complete: validated and copied {total_rows} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
