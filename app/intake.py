from typing import Any

from app.culture import _connect


def initialize_intake_store() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS mentorship_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                focus TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending assignment',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )


def create_mentorship_request(phone: str, focus: str) -> dict[str, Any]:
    with _connect() as connection:
        cursor = connection.execute(
            "INSERT INTO mentorship_requests (phone, focus) VALUES (?, ?)",
            (phone, focus),
        )
        row = connection.execute(
            """
            SELECT id, phone, focus, status, created_at
            FROM mentorship_requests
            WHERE id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
    if row is None:
        raise RuntimeError("The mentorship request could not be read after insertion")
    return dict(row)


def create_feedback_submission(message: str) -> dict[str, Any]:
    with _connect() as connection:
        cursor = connection.execute(
            "INSERT INTO feedback_submissions (message) VALUES (?)",
            (message,),
        )
        row = connection.execute(
            """
            SELECT id, message, created_at
            FROM feedback_submissions
            WHERE id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
    if row is None:
        raise RuntimeError("The feedback submission could not be read after insertion")
    return dict(row)
