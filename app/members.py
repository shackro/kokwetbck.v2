import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.database import connect as _database_connect


router = APIRouter(tags=["Community members"])
Cohort = Literal["Campus Comrade", "Young Professional", "Creative Leader", "Established Elder"]


class ProfileCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=80)
    phone: str = Field(pattern=r"^(?:\+254|254|0)7\d{8}$")
    cohort: Cohort
    interest: str = Field(default="General ecosystem", max_length=120)


class JourneyProfileCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=80)
    phone_number: str = Field(pattern=r"^(?:\+254|254|0)7\d{8}$")
    audience_cohort: Cohort
    active_service_seeking: str = Field(default="General ecosystem", max_length=120)


def _connect() -> Any:
    return _database_connect()


def initialize_member_store() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS community_profiles (
                profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                cohort TEXT NOT NULL,
                interest TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )


def _profile_payload(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["profile_id"],
        "full_name": row["full_name"],
        "phone": row["phone"],
        "cohort": row["cohort"],
        "interest": row["interest"],
        "verified": bool(row["verified"]),
        "created_at": row["created_at"],
    }


def _create_profile(payload: ProfileCreate) -> dict[str, object]:
    initialize_member_store()
    try:
        with _connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO community_profiles
                    (full_name, phone, cohort, interest, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    payload.full_name.strip(),
                    payload.phone,
                    payload.cohort,
                    payload.interest.strip(),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            row = connection.execute(
                "SELECT * FROM community_profiles WHERE profile_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail="A profile already exists for this phone number") from error
    if row is None:
        raise RuntimeError("The newly registered profile could not be read back")
    return {"status": "success", "profile": _profile_payload(row)}


@router.post("/api/profiles", status_code=201)
def register_profile(payload: ProfileCreate) -> dict[str, object]:
    return _create_profile(payload)


@router.post("/api/v1/users/register", status_code=201)
def register_journey_profile(payload: JourneyProfileCreate) -> dict[str, object]:
    return _create_profile(
        ProfileCreate(
            full_name=payload.full_name,
            phone=payload.phone_number,
            cohort=payload.audience_cohort,
            interest=payload.active_service_seeking,
        )
    )
