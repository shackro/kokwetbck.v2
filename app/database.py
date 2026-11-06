from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


_ADVISORY_LOCK_KEY = 731_428_149_201
_NOW_SQL = "TO_CHAR(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
_QMARK = re.compile(r"\bIS\s+\?", re.IGNORECASE)
_TABLE_INFO = re.compile(
    r"^\s*PRAGMA\s+table_info\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*;?\s*$",
    re.IGNORECASE,
)
_PRAGMA = re.compile(r"^\s*PRAGMA\b", re.IGNORECASE)
_AUTOINCREMENT = re.compile(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", re.IGNORECASE)
_BLOB = re.compile(r"\bBLOB\b", re.IGNORECASE)
_NOW_FUNCTION = re.compile(r"\bdatetime\s*\(\s*'now'\s*\)", re.IGNORECASE)
_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)


class HybridRow(dict[str, Any]):
    """Mapping row with SQLite-compatible positional indexing."""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def _hybrid_dict_row(cursor: Any) -> Any:
    if cursor.description is None:
        return lambda _: None
    columns = [column.name for column in cursor.description]

    def make_row(values: Sequence[Any]) -> HybridRow:
        return HybridRow(zip(columns, values))

    return make_row


class _PostgresCursor:
    def __init__(self, cursor: Any, connection: Any, is_insert: bool) -> None:
        self._cursor = cursor
        self._connection = connection
        self._is_insert = is_insert
        self._lastrowid: int | None = None

    @property
    def lastrowid(self) -> int | None:
        if self._lastrowid is None and self._is_insert and self.rowcount:
            row = self._connection.execute("SELECT LASTVAL() AS last_value").fetchone()
            if row is not None:
                self._lastrowid = row["last_value"]
        return self._lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    def fetchmany(self, size: int | None = None) -> list[Any]:
        return self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()

    def __iter__(self) -> Any:
        return iter(self._cursor)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class PostgresConnection:
    def __init__(self, connection: Any, integrity_error: type[BaseException]) -> None:
        self._connection = connection
        self._integrity_error = integrity_error

    def execute(self, statement: str, parameters: Sequence[Any] | None = None) -> Any:
        sql = statement.strip()
        table_info = _TABLE_INFO.fullmatch(sql)
        if table_info:
            table_name = table_info.group(1)
            cursor = self._run(
                """
                SELECT column_name AS name
                FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s
                ORDER BY ordinal_position
                """,
                (table_name,),
            )
            return _PostgresCursor(cursor, self._connection, False)
        if _PRAGMA.match(sql):
            return _PostgresCursor(_EmptyCursor(), self._connection, False)
        if re.fullmatch(r"BEGIN\s+IMMEDIATE", sql, re.IGNORECASE):
            cursor = self._run(
                "SELECT pg_advisory_xact_lock(%s)",
                (_ADVISORY_LOCK_KEY,),
            )
            return _PostgresCursor(cursor, self._connection, False)
        translated = _translate_postgres_sql(sql)
        bound_parameters = tuple(parameters or ())
        cursor = self._run(translated, bound_parameters)
        is_insert = bool(re.match(r"^\s*INSERT\b", translated, re.IGNORECASE))
        return _PostgresCursor(cursor, self._connection, is_insert)

    def executemany(self, statement: str, parameters: Any) -> Any:
        translated = _translate_postgres_sql(statement.strip())
        try:
            cursor = self._connection.cursor()
            cursor.executemany(translated, parameters)
        except self._integrity_error as error:
            raise sqlite3.IntegrityError(str(error)) from error
        return _PostgresCursor(
            cursor,
            self._connection,
            bool(re.match(r"^\s*INSERT\b", translated, re.IGNORECASE)),
        )

    def executescript(self, script: str) -> None:
        for statement in _split_sql_script(script):
            self.execute(statement)

    def _run(self, statement: str, parameters: Sequence[Any] = ()) -> Any:
        try:
            return self._connection.execute(statement, parameters)
        except self._integrity_error as error:
            # Keep the application’s existing integrity-error handling consistent
            # across SQLite and PostgreSQL without exposing driver details.
            raise sqlite3.IntegrityError(str(error)) from error

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _EmptyCursor:
    rowcount = 0

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []


def _replace_qmark_placeholders(statement: str) -> str:
    result: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(statement):
        char = statement[index]
        if quote:
            result.append(char)
            if char == quote:
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    result.append(statement[index + 1])
                    index += 2
                    continue
                quote = None
            elif char == "\\" and quote == "'":
                if index + 1 < len(statement):
                    result.append(statement[index + 1])
                    index += 2
                    continue
        elif char in {"'", '"'}:
            quote = char
            result.append(char)
        elif char == "?":
            result.append("%s")
        else:
            result.append(char)
        index += 1
    return "".join(result)


def _translate_postgres_sql(statement: str) -> str:
    if _TABLE_INFO.fullmatch(statement) or _PRAGMA.match(statement):
        return statement
    translated = _AUTOINCREMENT.sub("BIGSERIAL PRIMARY KEY", statement)
    translated = _BLOB.sub("BYTEA", translated)
    translated = _NOW_FUNCTION.sub(_NOW_SQL, translated)
    translated = _QMARK.sub("IS NOT DISTINCT FROM ?", translated)
    ignored_insert = bool(_INSERT_OR_IGNORE.search(translated))
    translated = _INSERT_OR_IGNORE.sub("INSERT INTO", translated)
    translated = _replace_qmark_placeholders(translated)
    if ignored_insert:
        translated = translated.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return translated


def _split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    start = 0
    quote: str | None = None
    index = 0
    while index < len(script):
        char = script[index]
        if quote:
            if char == quote:
                if index + 1 < len(script) and script[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == ";":
            statement = script[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1
        index += 1
    remainder = script[start:].strip()
    if remainder:
        statements.append(remainder)
    return statements


def database_url() -> str | None:
    value = os.getenv("DATABASE_URL", "").strip()
    use_sqlite = os.getenv("KOKWET_USE_SQLITE", "").strip().lower() == "true"
    requires_database_url = (
        os.getenv("KOKWET_REQUIRE_DATABASE_URL", "").strip().lower() == "true"
        or os.getenv("APP_ENV", "").strip().lower() == "production"
    )
    if use_sqlite:
        if requires_database_url:
            raise RuntimeError(
                "KOKWET_USE_SQLITE cannot be enabled when PostgreSQL is required"
            )
        return None
    if not value and requires_database_url:
        raise RuntimeError("DATABASE_URL is required for this deployment")
    return value or None


def initialize_database() -> None:
    if database_url() is None:
        return

    schema = database_schema()
    with connect() as connection:
        connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        connection.execute("SELECT 1").fetchone()


def postgres_mode() -> bool:
    return database_url() is not None


def database_schema() -> str:
    schema = os.getenv("KOKWET_DB_SCHEMA", "").strip()
    if not schema:
        raise RuntimeError(
            "KOKWET_DB_SCHEMA is required when DATABASE_URL selects PostgreSQL"
        )
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise RuntimeError(
            "KOKWET_DB_SCHEMA must be a simple PostgreSQL schema name"
        )
    return schema


def _postgres_connection(url: str) -> tuple[Any, Any]:
    from urllib.parse import parse_qs, urlsplit

    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError("psycopg is required when DATABASE_URL is configured") from error

    parsed = urlsplit(url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("DATABASE_URL must be a PostgreSQL connection URL")
    sslmodes = {
        mode.lower()
        for mode in parse_qs(parsed.query).get("sslmode", [])
    }
    if len(sslmodes) > 1 or (sslmodes and not sslmodes <= {"require", "verify-ca", "verify-full"}):
        raise RuntimeError("DATABASE_URL must use TLS for PostgreSQL connections")
    sslmode = next(iter(sslmodes), None)
    options: dict[str, Any] = {
        "connect_timeout": 10,
        "row_factory": _hybrid_dict_row,
        "prepare_threshold": None,
        "options": "-c lock_timeout=10000",
    }
    schema = database_schema()
    if schema:
        options["options"] += f" -c search_path={schema}"
    if sslmode is None:
        options["sslmode"] = "require"
    return psycopg.connect(url, **options), psycopg.IntegrityError


def _sqlite_connection() -> sqlite3.Connection:
    configured_path = os.getenv("KOKWET_DB_PATH")
    path = (
        Path(configured_path)
        if configured_path
        else Path(__file__).resolve().parents[1] / "data" / "kokwet.sqlite3"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


@contextmanager
def connect() -> Iterator[Any]:
    url = database_url()
    if url is None:
        connection = _sqlite_connection()
        try:
            with connection:
                yield connection
        finally:
            connection.close()
        return

    raw_connection, integrity_error = _postgres_connection(url)
    connection = PostgresConnection(raw_connection, integrity_error)
    try:
        with raw_connection:
            yield connection
    finally:
        raw_connection.close()


def check_database_connection() -> None:
    with connect() as connection:
        connection.execute("SELECT 1").fetchone()
