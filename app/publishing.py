import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi.responses import Response
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from app.constants import HOUSE_NAMES
from app.culture import (
    _client_hash,
    _connect,
    _purge_expired,
    initialize_culture_store,
)

router = APIRouter(prefix="/api/v1", tags=["Publishing and staff"])
StaffRole = Literal["admin", "moderator", "creator"]
LOCAL_TIMEZONE = ZoneInfo("Africa/Nairobi")
SESSION_HOURS = 12
PASSWORD_ITERATIONS = 310_000


class StaffLoginRequest(BaseModel):
    identifier: str | None = Field(default=None, min_length=3, max_length=254)
    email: str | None = Field(default=None, min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def require_identifier(self) -> "StaffLoginRequest":
        if not self.identifier and not self.email:
            raise ValueError("Provide a staff phone number or email address")
        return self


class StaffCreateRequest(BaseModel):
    phone: str = Field(pattern=r"^(?:\+254|254|0)7\d{8}$")
    role: StaffRole
    can_manage_stories: bool = False
    can_manage_live: bool = False
    can_scan_tickets: bool = False
    house_names: list[str] = Field(default_factory=list, max_length=len(HOUSE_NAMES))

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        return _normalize_staff_phone(value)

    @field_validator("house_names")
    @classmethod
    def validate_house_names(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(name not in HOUSE_NAMES for name in value):
            raise ValueError("Choose unique names from the Nine Houses")
        return value


class StaffActivationRequest(BaseModel):
    phone: str = Field(pattern=r"^(?:\+254|254|0)7\d{8}$")
    activation_code: str = Field(pattern=r"^\d{6}$")
    full_name: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        return _normalize_staff_phone(value)


class StaffUpdateRequest(BaseModel):
    role: StaffRole | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256)
    can_manage_stories: bool | None = None
    can_manage_live: bool | None = None
    can_scan_tickets: bool | None = None
    house_names: list[str] | None = Field(default=None, max_length=len(HOUSE_NAMES))

    @field_validator("can_manage_stories")
    @classmethod
    def validate_story_permission(cls, value: bool | None) -> bool:
        if value is None:
            raise ValueError("Provide a story permission value")
        return value

    @field_validator("can_manage_live")
    @classmethod
    def validate_live_permission(cls, value: bool | None) -> bool:
        if value is None:
            raise ValueError("Provide a live-management permission value")
        return value

    @field_validator("can_scan_tickets")
    @classmethod
    def validate_ticket_permission(cls, value: bool | None) -> bool:
        if value is None:
            raise ValueError("Provide a ticket-scanning permission value")
        return value

    @field_validator("house_names")
    @classmethod
    def validate_house_names(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            raise ValueError("Provide a list of assigned house names")
        if len(set(value)) != len(value) or any(name not in HOUSE_NAMES for name in value):
            raise ValueError("Choose unique names from the Nine Houses")
        return value


class StoryCreateRequest(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    body: str = Field(default="", max_length=5000)
    cover_image_url: str | None = Field(default=None, max_length=700_000)
    audio_data: str | None = Field(default=None, max_length=2_800_000)
    is_published: bool = True
    sort_order: int = Field(default=1000, ge=0, le=10000)


class StoryUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=120)
    body: str | None = Field(default=None, max_length=5000)
    cover_image_url: str | None = Field(default=None, max_length=700_000)
    audio_data: str | None = Field(default=None, max_length=2_800_000)
    remove_audio: bool = False
    is_published: bool | None = None
    sort_order: int = Field(default=1000, ge=0, le=10000)


class HouseVoiceStoryRequest(BaseModel):
    title: str = Field(min_length=2, max_length=100)
    body: str = Field(default="", max_length=500)
    cover_image_url: str | None = Field(default=None, max_length=700_000)
    audio_data: str | None = Field(default=None, max_length=2_800_000)
    remove_cover_image: bool = False
    remove_audio: bool = False
    speaker_name: str = Field(default="", max_length=80)
    dialect: str = Field(default="", max_length=80)


class DailyVoiceRequest(BaseModel):
    post_id: UUID


class WeeklyVoicesRequest(BaseModel):
    house_name: str = Field(min_length=1, max_length=80)
    post_ids: list[UUID] = Field(max_length=3)


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, PASSWORD_ITERATIONS
    )
    return "$".join(
        (
            "pbkdf2_sha256",
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode().rstrip("="),
            base64.urlsafe_b64encode(digest).decode().rstrip("="),
        )
    )


def _verify_password(password: str, stored_hash: str) -> bool:
    algorithm, iterations, encoded_salt, encoded_digest = stored_hash.split("$", 3)
    if algorithm != "pbkdf2_sha256":
        return False
    salt = base64.urlsafe_b64decode(encoded_salt + "=" * (-len(encoded_salt) % 4))
    expected = base64.urlsafe_b64decode(
        encoded_digest + "=" * (-len(encoded_digest) % 4)
    )
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, int(iterations)
    )
    return hmac.compare_digest(actual, expected)


def _email(value: str) -> str:
    normalized = value.strip().lower()
    if normalized.count("@") != 1 or "." not in normalized.rsplit("@", 1)[-1]:
        raise HTTPException(status_code=422, detail="Enter a valid staff email address")
    return normalized


def _normalize_staff_phone(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if digits.startswith("0"):
        digits = f"254{digits[1:]}"
    if len(digits) != 12 or not digits.startswith("2547"):
        raise ValueError("Enter a valid Kenyan mobile number")
    return f"+{digits}"


def _activation_secret() -> bytes:
    configured_secret = os.getenv("KOKWET_STAFF_ACTIVATION_SECRET")
    if not configured_secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "KOKWET_STAFF_ACTIVATION_SECRET is required in the backend "
                "environment and must be at least 32 characters."
            ),
        )
    if configured_secret.startswith("hex:"):
        try:
            secret = bytes.fromhex(configured_secret.removeprefix("hex:"))
        except ValueError as error:
            raise HTTPException(
                status_code=503,
                detail=(
                    "A hex-encoded KOKWET_STAFF_ACTIVATION_SECRET must use "
                    "the format hex:<64 hexadecimal characters>."
                ),
            ) from error
        if len(secret) != 32:
            raise HTTPException(
                status_code=503,
                detail=(
                    "A hex-encoded KOKWET_STAFF_ACTIVATION_SECRET must decode "
                    "to exactly 32 bytes."
                ),
            )
        return secret
    if len(configured_secret) < 32:
        raise HTTPException(
            status_code=503,
            detail=(
                "KOKWET_STAFF_ACTIVATION_SECRET must be at least 32 characters "
                "in the backend environment."
            ),
        )
    return configured_secret.encode()


def _activation_code_hash(phone: str, code: str) -> str:
    return hmac.new(
        _activation_secret(),
        f"{phone}:{code}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _seed_admin(connection: sqlite3.Connection) -> None:
    email = os.getenv("KOKWET_STAFF_ADMIN_EMAIL")
    password = os.getenv("KOKWET_STAFF_ADMIN_PASSWORD")
    if not email and not password:
        return
    if not email or not password:
        raise RuntimeError(
            "Set both KOKWET_STAFF_ADMIN_EMAIL and KOKWET_STAFF_ADMIN_PASSWORD"
        )
    if len(password) < 12:
        raise RuntimeError("KOKWET_STAFF_ADMIN_PASSWORD must be at least 12 characters")
    normalized_email = _email(email)
    exists = connection.execute(
        "SELECT 1 FROM staff_users WHERE email = ?", (normalized_email,)
    ).fetchone()
    if exists:
        return
    connection.execute(
        """
        INSERT INTO staff_users
            (staff_id, email, full_name, password_hash, role, is_active, created_at)
        VALUES (?, ?, ?, ?, 'admin', 1, ?)
        """,
        (
            str(uuid4()),
            normalized_email,
            "Kokwet Administrator",
            _password_hash(password),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )


def _insert_super_admin(
    connection: sqlite3.Connection,
    email: str,
    phone: str,
    password: str,
    source: str,
) -> bool:
    if len(password) < 16:
        raise ValueError("Super Admin passwords must be at least 16 characters")
    normalized_email = _email(email)
    normalized_phone = _normalize_staff_phone(phone)
    existing = connection.execute(
        """
        SELECT staff_id, email, phone, is_super_admin
        FROM staff_users WHERE email = ? OR phone = ?
        """,
        (normalized_email, normalized_phone),
    ).fetchall()
    if existing:
        if (
            len(existing) != 1
            or not existing[0]["is_super_admin"]
            or existing[0]["email"] != normalized_email
            or existing[0]["phone"] != normalized_phone
        ):
            raise ValueError(
                "The configured Super Admin email or phone already belongs to another account"
            )
        return False
    staff_id = str(uuid4())
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection.execute(
        """
        INSERT INTO staff_users
            (staff_id, email, phone, full_name, password_hash, role, is_active,
             created_at, can_manage_stories, house_names_json, can_manage_live,
             can_scan_tickets, is_super_admin)
        VALUES (?, ?, ?, 'Super Admin 1', ?, 'admin', 1, ?, 1, ?, 1, 1, 1)
        """,
        (
            staff_id,
            normalized_email,
            normalized_phone,
            _password_hash(password),
            created_at,
            json.dumps(HOUSE_NAMES),
        ),
    )
    connection.execute(
        """
        INSERT INTO staff_audit_events
            (actor_staff_id, actor_name, method, path, response_status, created_at)
        VALUES (?, 'Super Admin 1', 'BOOTSTRAP', ?, 201, ?)
        """,
        (staff_id, source, created_at),
    )
    return True


def _seed_super_admin(connection: sqlite3.Connection) -> None:
    email = os.getenv("KOKWET_SUPER_ADMIN_EMAIL")
    phone = os.getenv("KOKWET_SUPER_ADMIN_PHONE")
    password = os.getenv("KOKWET_SUPER_ADMIN_PASSWORD")
    if not email and not phone and not password:
        return
    if not email or not phone or not password:
        raise RuntimeError(
            "Set KOKWET_SUPER_ADMIN_EMAIL, KOKWET_SUPER_ADMIN_PHONE, and "
            "KOKWET_SUPER_ADMIN_PASSWORD together"
        )
    _insert_super_admin(connection, email, phone, password, "environment-seed")


def create_super_admin_account(email: str, phone: str, password: str) -> str:
    initialize_publishing_store()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM staff_users WHERE is_super_admin = 1 LIMIT 1"
        ).fetchone():
            raise ValueError("A Super Admin account is already configured")
        if not _insert_super_admin(connection, email, phone, password, "interactive-cli"):
            raise ValueError("A Super Admin account is already configured")
        row = connection.execute(
            "SELECT staff_id FROM staff_users WHERE is_super_admin = 1"
        ).fetchone()
        if not row:
            raise RuntimeError("Super Admin creation did not persist")
        return row["staff_id"]


def initialize_publishing_store(*, seed_data: bool = True) -> None:
    initialize_culture_store()
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS staff_users (
                staff_id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                full_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'moderator', 'creator')),
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                can_manage_stories INTEGER NOT NULL DEFAULT 0,
                house_names_json TEXT NOT NULL DEFAULT '[]',
                can_manage_live INTEGER NOT NULL DEFAULT 0,
                can_scan_tickets INTEGER NOT NULL DEFAULT 0,
                phone TEXT,
                is_super_admin INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS staff_sessions (
                token_hash TEXT PRIMARY KEY,
                staff_id TEXT NOT NULL REFERENCES staff_users(staff_id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_staff_sessions_expiry
                ON staff_sessions(expires_at);
            CREATE TABLE IF NOT EXISTS staff_login_attempts (
                attempt_key TEXT NOT NULL,
                failed_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_staff_login_attempts
                ON staff_login_attempts(attempt_key, failed_at);
            CREATE TABLE IF NOT EXISTS staff_audit_events (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_staff_id TEXT NOT NULL,
                actor_name TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                response_status INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_staff_audit_created
                ON staff_audit_events(created_at DESC);
            CREATE TABLE IF NOT EXISTS staff_activation_codes (
                activation_id TEXT PRIMARY KEY,
                phone TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'moderator', 'creator')),
                can_manage_stories INTEGER NOT NULL DEFAULT 0,
                house_names_json TEXT NOT NULL DEFAULT '[]',
                can_manage_live INTEGER NOT NULL DEFAULT 0,
                can_scan_tickets INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL REFERENCES staff_users(staff_id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                used_at TEXT,
                revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_staff_activation_phone
                ON staff_activation_codes(phone, expires_at);
            CREATE TABLE IF NOT EXISTS festival_stories (
                story_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                cover_image_url TEXT,
                audio_content BLOB,
                audio_content_type TEXT,
                author_id TEXT REFERENCES staff_users(staff_id) ON DELETE SET NULL,
                is_published INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 1000,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_stories_public
                ON festival_stories(is_published, updated_at DESC);
            CREATE TABLE IF NOT EXISTS weekly_house_voices (
                week_start TEXT NOT NULL,
                house_name TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 5),
                post_id TEXT NOT NULL REFERENCES culture_posts(post_id) ON DELETE CASCADE,
                selected_by TEXT REFERENCES staff_users(staff_id) ON DELETE SET NULL,
                selected_at TEXT NOT NULL,
                PRIMARY KEY (week_start, house_name, position),
                UNIQUE (week_start, house_name, post_id)
            );
            CREATE INDEX IF NOT EXISTS idx_weekly_house_voices
                ON weekly_house_voices(house_name, week_start);
            CREATE TABLE IF NOT EXISTS house_voice_stories (
                house_name TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 3),
                story_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                cover_image_url TEXT NOT NULL,
                audio_content BLOB NOT NULL,
                audio_content_type TEXT NOT NULL,
                speaker_name TEXT NOT NULL DEFAULT '',
                dialect TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_by TEXT REFERENCES staff_users(staff_id) ON DELETE SET NULL,
                PRIMARY KEY (house_name, position)
            );
            CREATE INDEX IF NOT EXISTS idx_house_voice_stories_expiry
                ON house_voice_stories(house_name, expires_at);
            CREATE TABLE IF NOT EXISTS house_voice_story_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                house_name TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 3),
                story_id TEXT NOT NULL,
                actor_staff_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('published', 'updated', 'removed')),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_house_voice_story_events_window
                ON house_voice_story_events(house_name, position, created_at);
            """
        )
        user_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(staff_users)").fetchall()
        }
        if "can_manage_stories" not in user_columns:
            connection.execute(
                "ALTER TABLE staff_users ADD COLUMN can_manage_stories INTEGER NOT NULL DEFAULT 0"
            )
            if seed_data:
                connection.execute(
                    "UPDATE staff_users SET can_manage_stories = 1 WHERE role = 'creator'"
                )
        if "house_names_json" not in user_columns:
            connection.execute(
                "ALTER TABLE staff_users ADD COLUMN house_names_json TEXT NOT NULL DEFAULT '[]'"
            )
            if seed_data:
                connection.execute(
                    "UPDATE staff_users SET house_names_json = ? WHERE role = 'moderator'",
                    (json.dumps(HOUSE_NAMES),),
                )
        if "can_manage_live" not in user_columns:
            connection.execute(
                "ALTER TABLE staff_users ADD COLUMN can_manage_live INTEGER NOT NULL DEFAULT 0"
            )
        if "can_scan_tickets" not in user_columns:
            connection.execute(
                "ALTER TABLE staff_users ADD COLUMN can_scan_tickets INTEGER NOT NULL DEFAULT 0"
            )
        if "phone" not in user_columns:
            connection.execute("ALTER TABLE staff_users ADD COLUMN phone TEXT")
        if "is_super_admin" not in user_columns:
            connection.execute(
                "ALTER TABLE staff_users ADD COLUMN is_super_admin INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_users_phone
            ON staff_users(phone) WHERE phone IS NOT NULL
            """
        )
        story_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(festival_stories)").fetchall()
        }
        if "audio_content" not in story_columns:
            connection.execute("ALTER TABLE festival_stories ADD COLUMN audio_content BLOB")
        if "audio_content_type" not in story_columns:
            connection.execute("ALTER TABLE festival_stories ADD COLUMN audio_content_type TEXT")
        if "sort_order" not in story_columns:
            connection.execute(
                "ALTER TABLE festival_stories ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 1000"
            )
        if seed_data:
            _seed_admin(connection)
            _seed_super_admin(connection)
            connection.execute(
                "DELETE FROM staff_sessions WHERE expires_at <= ?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
            )


def _staff_from_authorization(
    authorization: str | None, allowed_roles: tuple[str, ...] | None = None
) -> sqlite3.Row:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Staff sign-in required")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Staff sign-in required")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        staff = connection.execute(
            """
            SELECT u.staff_id, u.email, u.phone, u.full_name, u.role, u.is_active,
                   u.can_manage_stories, u.can_manage_live, u.can_scan_tickets,
                   u.house_names_json, u.created_at, u.is_super_admin
            FROM staff_sessions AS s
            JOIN staff_users AS u ON u.staff_id = s.staff_id
            WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1
            """,
            (token_hash, now),
        ).fetchone()
    if not staff:
        raise HTTPException(status_code=401, detail="Staff session expired or revoked")
    if allowed_roles and staff["role"] not in allowed_roles:
        raise HTTPException(status_code=403, detail="Your staff role cannot perform this action")
    return staff


def _can_manage_stories(staff: sqlite3.Row) -> bool:
    return staff["role"] == "admin" or bool(staff["can_manage_stories"])


def _require_story_access(staff: sqlite3.Row) -> None:
    if not _can_manage_stories(staff):
        raise HTTPException(status_code=403, detail="Your account is not assigned to manage festival stories")


def _managed_house_names(staff: sqlite3.Row) -> tuple[str, ...]:
    if staff["role"] == "admin":
        return HOUSE_NAMES
    return tuple(json.loads(staff["house_names_json"]))


def _require_house_access(staff: sqlite3.Row, house_name: str) -> None:
    if house_name not in _managed_house_names(staff):
        raise HTTPException(status_code=403, detail="Your account is not assigned to this house")


def _staff_public(staff: sqlite3.Row) -> dict[str, Any]:
    is_super_admin = bool(staff["is_super_admin"])
    return {
        "staff_id": staff["staff_id"],
        "email": staff["email"],
        "phone": staff["phone"],
        "full_name": staff["full_name"],
        "role": "super_admin" if is_super_admin else staff["role"],
        "is_super_admin": is_super_admin,
        "is_active": bool(staff["is_active"]),
        "can_manage_stories": bool(staff["can_manage_stories"]),
        "can_manage_live": bool(staff["can_manage_live"]),
        "can_scan_tickets": bool(staff["can_scan_tickets"]),
        "house_names": json.loads(staff["house_names_json"]),
        "created_at": staff["created_at"],
    }


def super_admin_audit_actor(authorization: str | None) -> tuple[str, str] | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        staff = connection.execute(
            """
            SELECT u.staff_id, u.full_name
            FROM staff_sessions AS s
            JOIN staff_users AS u ON u.staff_id = s.staff_id
            WHERE s.token_hash = ? AND s.expires_at > ?
              AND u.is_active = 1 AND u.is_super_admin = 1
            """,
            (hashlib.sha256(token.encode()).hexdigest(), now),
        ).fetchone()
    return (staff["staff_id"], staff["full_name"]) if staff else None


def record_super_admin_audit(
    actor: tuple[str, str],
    method: str,
    path: str,
    response_status: int,
) -> None:
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO staff_audit_events
                (actor_staff_id, actor_name, method, path, response_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                actor[0],
                actor[1],
                method,
                path,
                response_status,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )


def _validated_image_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    if normalized.startswith("data:"):
        header, separator, encoded = normalized.partition(",")
        if (
            not separator
            or header not in {
                "data:image/jpeg;base64",
                "data:image/png;base64",
                "data:image/webp;base64",
            }
        ):
            raise HTTPException(status_code=422, detail="Use a JPEG, PNG or WebP story cover")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except binascii.Error as error:
            raise HTTPException(status_code=422, detail="Story cover image data is invalid") from error
        if not image_bytes or len(image_bytes) > 512 * 1024:
            raise HTTPException(status_code=413, detail="Story cover images must be 512 KB or smaller")
        valid_signature = (
            header == "data:image/jpeg;base64" and image_bytes.startswith(b"\xff\xd8\xff")
        ) or (
            header == "data:image/png;base64"
            and image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        ) or (
            header == "data:image/webp;base64"
            and len(image_bytes) >= 12
            and image_bytes[:4] == b"RIFF"
            and image_bytes[8:12] == b"WEBP"
        )
        if not valid_signature:
            raise HTTPException(status_code=422, detail="Story cover data does not match its image format")
        return normalized
    parsed = urlparse(normalized)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="Story image must use an http(s) URL")
    return normalized


def _validated_audio_data(value: str) -> tuple[bytes, str]:
    header, separator, encoded = value.partition(",")
    if not separator or not header.startswith("data:") or not header.endswith(";base64"):
        raise HTTPException(status_code=422, detail="Choose a valid encoded audio recording")
    content_type = header[5:-7].lower()
    if content_type == "audio/x-wav":
        content_type = "audio/wav"
    if content_type not in {
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
    }:
        raise HTTPException(status_code=422, detail="Use an MP3, M4A, OGG, WAV or WebM recording")
    try:
        audio = base64.b64decode(encoded, validate=True)
    except binascii.Error as error:
        raise HTTPException(status_code=422, detail="Audio recording data is invalid") from error
    if not audio or len(audio) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio recordings must be 2 MB or smaller")
    has_valid_signature = (
        content_type == "audio/wav"
        and len(audio) >= 12
        and audio[:4] == b"RIFF"
        and audio[8:12] == b"WAVE"
    ) or (
        content_type == "audio/ogg" and audio.startswith(b"OggS")
    ) or (
        content_type == "audio/webm" and audio.startswith(b"\x1aE\xdf\xa3")
    ) or (
        content_type == "audio/mp4" and len(audio) >= 12 and audio[4:8] == b"ftyp"
    ) or (
        content_type == "audio/mpeg"
        and (audio.startswith(b"ID3") or (len(audio) >= 2 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0))
    )
    if not has_valid_signature:
        raise HTTPException(status_code=422, detail="Audio data does not match its format")
    return audio, content_type


def _story_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "story_id": row["story_id"],
        "title": row["title"],
        "body": row["body"],
        "cover_image_url": row["cover_image_url"],
        "audio_clip_url": (
            f"/api/v1/stories/{row['story_id']}/audio"
            if row["audio_content_type"]
            else None
        ),
        "author_name": row["author_name"] or "Kokwet Festival",
        "is_published": bool(row["is_published"]),
        "sort_order": row["sort_order"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _story_query() -> str:
    return """
        SELECT s.story_id, s.title, s.body, s.cover_image_url, s.audio_content_type,
               s.is_published, s.sort_order,
               s.created_at, s.updated_at, u.full_name AS author_name
        FROM festival_stories AS s
        LEFT JOIN staff_users AS u ON u.staff_id = s.author_id
    """


def _local_day() -> date:
    return datetime.now(LOCAL_TIMEZONE).date()


def _week_start() -> str:
    today = _local_day()
    return (today - timedelta(days=today.weekday())).isoformat()


def _voice_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "post_id": row["post_id"],
        "text_message": row["text_message"],
        "audio_clip_url": (
            f"/api/v1/culture/audio/{row['post_id']}"
            if row["audio_content_type"]
            else None
        ),
        "speaker_name": row["speaker_name"] or "Community voice",
        "dialect": row["dialect"],
        "house_name": row["house_name"],
        "created_at": row["created_at"],
    }


def _house_voice_story_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "story_id": row["story_id"],
        "house_name": row["house_name"],
        "position": row["position"],
        "title": row["title"],
        "body": row["body"],
        "cover_image_url": row["cover_image_url"] or None,
        "audio_clip_url": (
            f"/api/v1/house-voice-stories/{row['story_id']}/audio"
            if row["audio_content_type"]
            else None
        ),
        "speaker_name": row["speaker_name"],
        "dialect": row["dialect"],
        "published_at": row["published_at"],
        "expires_at": row["expires_at"],
    }


@router.post("/staff/auth/login")
def staff_login(payload: StaffLoginRequest, request: Request) -> dict[str, Any]:
    initialize_publishing_store()
    identifier = (payload.identifier or payload.email or "").strip()
    is_phone = bool(re.fullmatch(r"(?:\+254|254|0)7\d{8}", identifier))
    if is_phone:
        normalized_identifier = _normalize_staff_phone(identifier)
        lookup_column = "phone"
    else:
        normalized_identifier = _email(identifier)
        lookup_column = "email"
    client_hash = _client_hash(request)
    email_ip_key = hashlib.sha256(f"{normalized_identifier}:{client_hash}".encode()).hexdigest()
    ip_key = hashlib.sha256(f"ip:{client_hash}".encode()).hexdigest()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=15)).isoformat(timespec="seconds")
    blocked = False
    valid = False
    staff = None
    token = ""
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM staff_login_attempts WHERE failed_at <= ?", (cutoff,)
        )
        attempts = connection.execute(
            """
            SELECT attempt_key, COUNT(*) AS count
            FROM staff_login_attempts
            WHERE attempt_key IN (?, ?) AND failed_at > ?
            GROUP BY attempt_key
            """,
            (email_ip_key, ip_key, cutoff),
        ).fetchall()
        attempt_counts = {row["attempt_key"]: row["count"] for row in attempts}
        blocked = (
            attempt_counts.get(email_ip_key, 0) >= 5
            or attempt_counts.get(ip_key, 0) >= 20
        )
        if not blocked:
            staff = connection.execute(
                f"""
                SELECT staff_id, email, phone, full_name, password_hash, role, is_active,
                       can_manage_stories, can_manage_live, can_scan_tickets,
                       house_names_json, created_at, is_super_admin
                FROM staff_users WHERE {lookup_column} = ?
                """,
                (normalized_identifier,),
            ).fetchone()
            valid = bool(
                staff
                and staff["is_active"]
                and _verify_password(payload.password, staff["password_hash"])
            )
            if valid:
                token = secrets.token_urlsafe(32)
                connection.execute(
                    "DELETE FROM staff_login_attempts WHERE attempt_key = ?",
                    (email_ip_key,),
                )
                connection.execute(
                    """
                    INSERT INTO staff_sessions(token_hash, staff_id, expires_at, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        hashlib.sha256(token.encode()).hexdigest(),
                        staff["staff_id"],
                        (now + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds"),
                        now.isoformat(timespec="seconds"),
                    ),
                )
            else:
                connection.executemany(
                    """
                    INSERT INTO staff_login_attempts(attempt_key, failed_at)
                    VALUES (?, ?)
                    """,
                    ((email_ip_key, now.isoformat(timespec="seconds")), (ip_key, now.isoformat(timespec="seconds"))),
                )
    if blocked:
        raise HTTPException(
            status_code=429,
            detail="Too many sign-in attempts. Try again in 15 minutes.",
        )
    if not valid or not staff:
        raise HTTPException(status_code=401, detail="Phone/email or password is incorrect")
    return {
        "token": token,
        "expires_in": SESSION_HOURS * 60 * 60,
        "staff": _staff_public(staff),
    }


@router.post("/staff/auth/activate", status_code=201)
def activate_staff_account(
    payload: StaffActivationRequest,
    request: Request,
) -> dict[str, Any]:
    initialize_publishing_store()
    email = _email(payload.email)
    code_hash = _activation_code_hash(payload.phone, payload.activation_code)
    client_hash = _client_hash(request)
    phone_ip_key = hashlib.sha256(
        f"activation:{payload.phone}:{client_hash}".encode()
    ).hexdigest()
    ip_key = hashlib.sha256(f"activation-ip:{client_hash}".encode()).hexdigest()
    now = datetime.now(timezone.utc)
    now_text = now.isoformat(timespec="seconds")
    cutoff = (now - timedelta(minutes=15)).isoformat(timespec="seconds")
    blocked = False
    valid_code = False
    staff: sqlite3.Row | None = None
    token = ""
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM staff_login_attempts WHERE failed_at <= ?", (cutoff,)
        )
        attempts = connection.execute(
            """
            SELECT attempt_key, COUNT(*) AS count
            FROM staff_login_attempts
            WHERE attempt_key IN (?, ?) AND failed_at > ?
            GROUP BY attempt_key
            """,
            (phone_ip_key, ip_key, cutoff),
        ).fetchall()
        attempt_counts = {row["attempt_key"]: row["count"] for row in attempts}
        blocked = (
            attempt_counts.get(phone_ip_key, 0) >= 5
            or attempt_counts.get(ip_key, 0) >= 20
        )
        invitation = None
        if not blocked:
            invitation = connection.execute(
                """
                SELECT activation_id, code_hash, role, can_manage_stories,
                       house_names_json, can_manage_live, can_scan_tickets
                FROM staff_activation_codes
                WHERE phone = ? AND expires_at > ? AND used_at IS NULL
                      AND revoked_at IS NULL AND failed_attempts < 5
                ORDER BY created_at DESC LIMIT 1
                """,
                (payload.phone, now_text),
            ).fetchone()
            valid_code = bool(
                invitation
                and hmac.compare_digest(code_hash, invitation["code_hash"])
            )
        if not valid_code or invitation is None:
            if invitation and not blocked:
                connection.execute(
                    """
                    UPDATE staff_activation_codes
                    SET failed_attempts = failed_attempts + 1
                    WHERE activation_id = ?
                    """,
                    (invitation["activation_id"],),
                )
            if not blocked:
                connection.executemany(
                    """
                    INSERT INTO staff_login_attempts(attempt_key, failed_at)
                    VALUES (?, ?)
                    """,
                    (
                        (phone_ip_key, now_text),
                        (ip_key, now_text),
                    ),
                )
        else:
            conflict = connection.execute(
                "SELECT 1 FROM staff_users WHERE email = ? OR phone = ? LIMIT 1",
                (email, payload.phone),
            ).fetchone()
            if conflict:
                raise HTTPException(
                    status_code=409,
                    detail="A staff account already uses that email or phone number.",
                )
            staff_id = str(uuid4())
            token = secrets.token_urlsafe(32)
            connection.execute(
                """
                INSERT INTO staff_users
                    (staff_id, email, phone, full_name, password_hash, role,
                     is_active, created_at, can_manage_stories, house_names_json,
                     can_manage_live, can_scan_tickets)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    staff_id,
                    email,
                    payload.phone,
                    payload.full_name.strip(),
                    _password_hash(payload.password),
                    invitation["role"],
                    now_text,
                    invitation["can_manage_stories"],
                    invitation["house_names_json"],
                    invitation["can_manage_live"],
                    invitation["can_scan_tickets"],
                ),
            )
            connection.execute(
                "UPDATE staff_activation_codes SET used_at = ? WHERE activation_id = ?",
                (now_text, invitation["activation_id"]),
            )
            connection.execute(
                """
                INSERT INTO staff_sessions(token_hash, staff_id, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    staff_id,
                    (now + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds"),
                    now_text,
                ),
            )
            connection.execute(
                "DELETE FROM staff_login_attempts WHERE attempt_key IN (?, ?)",
                (phone_ip_key, ip_key),
            )
            staff = connection.execute(
                """
                SELECT staff_id, email, phone, full_name, role, is_active,
                       can_manage_stories, can_manage_live, can_scan_tickets,
                       house_names_json, created_at, is_super_admin
                FROM staff_users WHERE staff_id = ?
                """,
                (staff_id,),
            ).fetchone()
    if blocked:
        raise HTTPException(
            status_code=429,
            detail="Too many activation attempts. Try again in 15 minutes.",
        )
    if not valid_code or not staff:
        raise HTTPException(status_code=400, detail="Activation code is invalid or expired.")
    return {
        "token": token,
        "expires_in": SESSION_HOURS * 60 * 60,
        "staff": _staff_public(staff),
    }


@router.get("/staff/auth/me")
def staff_me(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    staff = _staff_from_authorization(authorization)
    return {"staff": _staff_public(staff)}


@router.post("/staff/auth/logout")
def staff_logout(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    staff = _staff_from_authorization(authorization)
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    with _connect() as connection:
        connection.execute(
            "DELETE FROM staff_sessions WHERE token_hash = ?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )
    return {"status": "signed out", "staff_id": staff["staff_id"]}


@router.get("/staff/users")
def list_staff_users(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, list[dict[str, Any]]]:
    _staff_from_authorization(authorization, ("admin",))
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT staff_id, email, phone, full_name, role, is_active, can_manage_stories,
                   can_manage_live, can_scan_tickets, house_names_json, created_at,
                   is_super_admin
            FROM staff_users ORDER BY created_at ASC
            """
        ).fetchall()
    return {"users": [_staff_public(row) for row in rows]}


@router.post("/staff/users", status_code=201)
def create_staff_user(
    payload: StaffCreateRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    issuer = _staff_from_authorization(authorization, ("admin",))
    now = datetime.now(timezone.utc)
    now_text = now.isoformat(timespec="seconds")
    expires_at = (now + timedelta(hours=24)).isoformat(timespec="seconds")
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = _activation_code_hash(payload.phone, code)
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM staff_users WHERE phone = ?", (payload.phone,)
        ).fetchone():
            raise HTTPException(status_code=409, detail="A staff account already uses that phone number")
        connection.execute(
            """
            UPDATE staff_activation_codes SET revoked_at = ?
            WHERE phone = ? AND used_at IS NULL AND revoked_at IS NULL
            """,
            (now_text, payload.phone),
        )
        activation_id = str(uuid4())
        try:
            connection.execute(
                """
                INSERT INTO staff_activation_codes
                    (activation_id, phone, code_hash, role, can_manage_stories,
                     house_names_json, can_manage_live, can_scan_tickets,
                     created_by, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    activation_id,
                    payload.phone,
                    code_hash,
                    payload.role,
                    int(payload.can_manage_stories),
                    json.dumps(payload.house_names),
                    int(payload.can_manage_live),
                    int(payload.can_scan_tickets),
                    issuer["staff_id"],
                    now_text,
                    expires_at,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="Unable to issue this staff invitation") from error
    return {
        "activation": {
            "activation_id": activation_id,
            "phone": payload.phone,
            "code": code,
            "expires_at": expires_at,
        }
    }


@router.get("/staff/activation-codes")
def list_staff_activation_codes(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, list[dict[str, Any]]]:
    _staff_from_authorization(authorization, ("admin",))
    now_text = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT activation_id, phone, role, created_at, expires_at,
                   failed_attempts,
                   CASE
                     WHEN used_at IS NOT NULL THEN 'used'
                     WHEN revoked_at IS NOT NULL THEN 'revoked'
                     WHEN expires_at <= ? THEN 'expired'
                     WHEN failed_attempts >= 5 THEN 'locked'
                     ELSE 'pending'
                   END AS status
            FROM staff_activation_codes
            ORDER BY created_at DESC LIMIT 100
            """,
            (now_text,),
        ).fetchall()
    return {"invitations": [
        {
            "activation_id": row["activation_id"],
            "phone": row["phone"],
            "role": row["role"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "failed_attempts": row["failed_attempts"],
            "status": row["status"],
        }
        for row in rows
    ]}


@router.delete("/staff/activation-codes/{activation_id}")
def revoke_staff_activation_code(
    activation_id: UUID,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    _staff_from_authorization(authorization, ("admin",))
    now_text = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE staff_activation_codes SET revoked_at = ?
            WHERE activation_id = ? AND used_at IS NULL AND revoked_at IS NULL
            """,
            (now_text, str(activation_id)),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Pending staff invitation not found")
    return {"status": "revoked"}


@router.get("/staff/super-admin/audit")
def list_super_admin_audit(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, list[dict[str, Any]]]:
    staff = _staff_from_authorization(authorization, ("admin",))
    if not staff["is_super_admin"]:
        raise HTTPException(status_code=403, detail="Super Admin access is required")
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT audit_id, actor_staff_id, actor_name, method, path,
                   response_status, created_at
            FROM staff_audit_events
            ORDER BY audit_id DESC LIMIT 200
            """
        ).fetchall()
    return {
        "events": [
            {
                "audit_id": row["audit_id"],
                "actor_staff_id": row["actor_staff_id"],
                "actor_name": row["actor_name"],
                "method": row["method"],
                "path": row["path"],
                "response_status": row["response_status"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


@router.patch("/staff/users/{staff_id}")
def update_staff_user(
    staff_id: UUID,
    payload: StaffUpdateRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    current = _staff_from_authorization(authorization, ("admin",))
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="Choose account details, access, status, or password to update")
    if current["staff_id"] == str(staff_id) and updates.get("is_active") is False:
        raise HTTPException(status_code=409, detail="You cannot disable your own staff account")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT staff_id, role, is_active, is_super_admin
            FROM staff_users WHERE staff_id = ?
            """,
            (str(staff_id),),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Staff account not found")
        if existing["is_super_admin"] and (
            not current["is_super_admin"]
            or "role" in updates
            or updates.get("is_active") is False
        ):
            raise HTTPException(
                status_code=403,
                detail="The Super Admin identity and role are protected",
            )
        if existing["is_super_admin"] and updates.get("password") and len(updates["password"]) < 16:
            raise HTTPException(
                status_code=422,
                detail="Super Admin passwords must be at least 16 characters",
            )
        next_role = updates.get("role", existing["role"])
        next_active = updates.get("is_active", bool(existing["is_active"]))
        if existing["role"] == "admin" and existing["is_active"] and (
            next_role != "admin" or not next_active
        ):
            other_admin = connection.execute(
                """
                SELECT 1 FROM staff_users
                WHERE role = 'admin' AND is_active = 1 AND staff_id != ?
                LIMIT 1
                """,
                (str(staff_id),),
            ).fetchone()
            if not other_admin:
                raise HTTPException(status_code=409, detail="Keep at least one active administrator")
        if "role" in updates:
            connection.execute(
                "UPDATE staff_users SET role = ? WHERE staff_id = ?",
                (updates["role"], str(staff_id)),
            )
        if "is_active" in updates:
            connection.execute(
                "UPDATE staff_users SET is_active = ? WHERE staff_id = ?",
                (int(updates["is_active"]), str(staff_id)),
            )
            if not updates["is_active"]:
                connection.execute(
                    "DELETE FROM staff_sessions WHERE staff_id = ?", (str(staff_id),)
                )
        if "can_manage_stories" in updates:
            connection.execute(
            "UPDATE staff_users SET can_manage_stories = ? WHERE staff_id = ?",
            (int(updates["can_manage_stories"]), str(staff_id)),
            )
        if "can_manage_live" in updates:
            connection.execute(
                "UPDATE staff_users SET can_manage_live = ? WHERE staff_id = ?",
                (int(updates["can_manage_live"]), str(staff_id)),
            )
        if "can_scan_tickets" in updates:
            connection.execute(
                "UPDATE staff_users SET can_scan_tickets = ? WHERE staff_id = ?",
                (int(updates["can_scan_tickets"]), str(staff_id)),
            )
        if "house_names" in updates:
            connection.execute(
                "UPDATE staff_users SET house_names_json = ? WHERE staff_id = ?",
                (json.dumps(updates["house_names"]), str(staff_id)),
            )
        if updates.get("password"):
            connection.execute(
                "UPDATE staff_users SET password_hash = ? WHERE staff_id = ?",
                (_password_hash(updates["password"]), str(staff_id)),
            )
            connection.execute(
                "DELETE FROM staff_sessions WHERE staff_id = ?", (str(staff_id),)
            )
        row = connection.execute(
            """
            SELECT staff_id, email, phone, full_name, role, is_active, can_manage_stories,
                   can_manage_live, can_scan_tickets, house_names_json, created_at,
                   is_super_admin
            FROM staff_users WHERE staff_id = ?
            """,
            (str(staff_id),),
        ).fetchone()
    return {"user": _staff_public(row)}


@router.delete("/staff/users/{staff_id}")
def delete_staff_user(
    staff_id: UUID,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    current = _staff_from_authorization(authorization, ("admin",))
    if current["staff_id"] == str(staff_id):
        raise HTTPException(status_code=409, detail="You cannot delete your own staff account")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        target = connection.execute(
            "SELECT role, is_active, is_super_admin FROM staff_users WHERE staff_id = ?",
            (str(staff_id),),
        ).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="Staff account not found")
        if target["is_super_admin"]:
            raise HTTPException(
                status_code=409,
                detail="The Super Admin identity cannot be deleted",
            )
        if target["role"] == "admin" and target["is_active"]:
            other_admin = connection.execute(
                """
                SELECT 1 FROM staff_users
                WHERE role = 'admin' AND is_active = 1 AND staff_id != ?
                LIMIT 1
                """,
                (str(staff_id),),
            ).fetchone()
            if not other_admin:
                raise HTTPException(status_code=409, detail="Keep at least one active administrator")
        connection.execute("DELETE FROM staff_users WHERE staff_id = ?", (str(staff_id),))
    return {"status": "deleted"}


@router.get("/stories")
def public_stories() -> dict[str, list[dict[str, Any]]]:
    initialize_publishing_store()
    with _connect() as connection:
        rows = connection.execute(
            _story_query()
            + """
            WHERE s.is_published = 1
            ORDER BY s.sort_order ASC, s.updated_at DESC
            LIMIT 10
            """
        ).fetchall()
    return {"stories": [_story_payload(row) for row in rows]}


@router.get("/staff/stories")
def staff_stories(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, list[dict[str, Any]]]:
    staff = _staff_from_authorization(authorization, ("admin", "moderator", "creator"))
    _require_story_access(staff)
    query = _story_query()
    params: tuple[str, ...] = ()
    if staff["role"] == "creator":
        query += " WHERE s.author_id = ?"
        params = (staff["staff_id"],)
    query += " ORDER BY s.sort_order ASC, s.updated_at DESC LIMIT 200"
    with _connect() as connection:
        rows = connection.execute(query, params).fetchall()
    return {"stories": [_story_payload(row) for row in rows]}


@router.get("/stories/{story_id}/audio")
def public_story_audio(story_id: UUID) -> Response:
    initialize_publishing_store()
    initialize_publishing_store()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT audio_content, audio_content_type
            FROM festival_stories
            WHERE story_id = ? AND is_published = 1
            """,
            (str(story_id),),
        ).fetchone()
    if not row or not row["audio_content"]:
        raise HTTPException(status_code=404, detail="Published story recording not found")
    return Response(
        content=row["audio_content"],
        media_type=row["audio_content_type"],
        headers={"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/staff/stories", status_code=201)
def create_story(
    payload: StoryCreateRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    staff = _staff_from_authorization(authorization, ("admin", "moderator", "creator"))
    _require_story_access(staff)
    if not payload.cover_image_url or not payload.audio_data:
        raise HTTPException(
            status_code=422,
            detail="New homepage stories require a cover image and a voice recording",
        )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    story_id = str(uuid4())
    audio_content, audio_content_type = _validated_audio_data(payload.audio_data)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO festival_stories
                (story_id, title, body, cover_image_url, audio_content, audio_content_type,
                 author_id, is_published, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                story_id,
                payload.title.strip(),
                payload.body.strip(),
                _validated_image_url(payload.cover_image_url),
                audio_content,
                audio_content_type,
                staff["staff_id"],
                int(payload.is_published),
                payload.sort_order,
                now,
                now,
            ),
        )
        row = connection.execute(
            _story_query() + " WHERE s.story_id = ?", (story_id,)
        ).fetchone()
    return {"story": _story_payload(row)}


@router.patch("/staff/stories/{story_id}")
def update_story(
    story_id: UUID,
    payload: StoryUpdateRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    staff = _staff_from_authorization(authorization, ("admin", "moderator", "creator"))
    _require_story_access(staff)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="No story changes were supplied")
    if updates.get("audio_data") and updates.get("remove_audio"):
        raise HTTPException(status_code=422, detail="Choose a new recording or remove the current one")
    with _connect() as connection:
        existing = connection.execute(
            "SELECT author_id, body, audio_content_type FROM festival_stories WHERE story_id = ?",
            (str(story_id),),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Story not found")
        if staff["role"] == "creator" and existing["author_id"] != staff["staff_id"]:
            raise HTTPException(status_code=403, detail="Creators can only edit their own stories")
        final_body = updates.get("body", existing["body"]) or ""
        has_audio = bool(updates.get("audio_data")) or (
            bool(existing["audio_content_type"]) and not updates.get("remove_audio")
        )
        if not final_body.strip() and not has_audio:
            raise HTTPException(
                status_code=422,
                detail="A published festival story must include a voice recording or a short written note",
            )
        assignments: list[str] = []
        values: list[Any] = []
        for field in ("title", "body", "cover_image_url", "is_published", "sort_order"):
            if field not in updates:
                continue
            value = updates[field]
            if field == "title" and isinstance(value, str):
                value = value.strip()
            elif field == "body" and isinstance(value, str):
                value = value.strip()
            elif field == "cover_image_url":
                value = _validated_image_url(value)
            elif field == "is_published":
                value = int(value)
            assignments.append(f"{field} = ?")
            values.append(value)
        if updates.get("audio_data"):
            audio_content, audio_content_type = _validated_audio_data(updates["audio_data"])
            assignments.extend(("audio_content = ?", "audio_content_type = ?"))
            values.extend((audio_content, audio_content_type))
        elif updates.get("remove_audio"):
            assignments.extend(("audio_content = NULL", "audio_content_type = NULL"))
        assignments.append("updated_at = ?")
        values.extend((datetime.now(timezone.utc).isoformat(timespec="seconds"), str(story_id)))
        connection.execute(
            f"UPDATE festival_stories SET {', '.join(assignments)} WHERE story_id = ?",
            values,
        )
        row = connection.execute(
            _story_query() + " WHERE s.story_id = ?", (str(story_id),)
        ).fetchone()
    return {"story": _story_payload(row)}


@router.delete("/staff/stories/{story_id}")
def delete_story(
    story_id: UUID,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    staff = _staff_from_authorization(authorization, ("admin", "moderator", "creator"))
    _require_story_access(staff)
    with _connect() as connection:
        existing = connection.execute(
            "SELECT author_id FROM festival_stories WHERE story_id = ?",
            (str(story_id),),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Story not found")
        if staff["role"] == "creator" and existing["author_id"] != staff["staff_id"]:
            raise HTTPException(status_code=403, detail="Creators can only delete their own stories")
        connection.execute(
            "DELETE FROM festival_stories WHERE story_id = ?", (str(story_id),)
        )
    return {"status": "deleted"}


@router.get("/voices/status")
def voice_submission_status() -> dict[str, int | str]:
    initialize_publishing_store()
    today = _local_day().isoformat()
    local_start = datetime.combine(_local_day(), datetime.min.time(), LOCAL_TIMEZONE)
    start_utc = local_start.astimezone(timezone.utc).isoformat(timespec="seconds")
    next_utc = (local_start + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        count = connection.execute(
            """
            SELECT COUNT(*) FROM culture_posts
            WHERE is_voice_submission = 1 AND created_at >= ? AND created_at < ?
            """,
            (start_utc, next_utc),
        ).fetchone()[0]
    return {"date": today, "submitted": count, "limit": 100, "remaining": max(0, 100 - count)}


@router.get("/voices/daily")
def public_daily_voice() -> dict[str, Any]:
    initialize_publishing_store()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT post_id, text_message, audio_content_type, speaker_name, dialect,
                   house_name, created_at
            FROM culture_posts
            WHERE featured_date = ? AND is_visible = 1 AND is_voice_submission = 1
            LIMIT 1
            """,
            (_local_day().isoformat(),),
        ).fetchone()
    return {"voice": _voice_payload(row) if row else None, "date": _local_day().isoformat()}


@router.get("/voices/weekly/{house_name}")
def public_weekly_voices(house_name: str) -> dict[str, Any]:
    initialize_publishing_store()
    if house_name not in HOUSE_NAMES:
        raise HTTPException(status_code=404, detail="House not found")
    week_start = _week_start()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT p.post_id, p.text_message, p.audio_content_type, p.speaker_name,
                   p.dialect, p.house_name, p.created_at
            FROM weekly_house_voices AS v
            JOIN culture_posts AS p ON p.post_id = v.post_id
            WHERE v.week_start = ? AND v.house_name = ? AND p.is_visible = 1
            ORDER BY v.position ASC
            LIMIT 3
            """,
            (week_start, house_name),
        ).fetchall()
    return {
        "house_name": house_name,
        "week_start": week_start,
        "voices": [_voice_payload(row) for row in rows],
    }


@router.get("/house-voice-stories/{house_name}")
def public_house_voice_stories(house_name: str) -> dict[str, Any]:
    initialize_publishing_store()
    if house_name not in HOUSE_NAMES:
        raise HTTPException(status_code=404, detail="House not found")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT house_name, position, story_id, title, body, cover_image_url,
                   audio_content_type, speaker_name, dialect, published_at, expires_at
            FROM house_voice_stories
            WHERE house_name = ? AND expires_at > ?
            ORDER BY position ASC
            """,
            (house_name, now),
        ).fetchall()
    return {
        "house_name": house_name,
        "stories": [_house_voice_story_payload(row) for row in rows],
    }


@router.get("/house-voice-stories/{story_id}/audio")
def public_house_voice_story_audio(story_id: UUID) -> Response:
    initialize_publishing_store()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT audio_content, audio_content_type
            FROM house_voice_stories
            WHERE story_id = ? AND expires_at > ? AND audio_content_type != ''
            """,
            (str(story_id), now),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="House voice story recording not found")
    return Response(
        content=row["audio_content"],
        media_type=row["audio_content_type"],
        headers={"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/staff/house-voice-stories/{house_name}")
def staff_house_voice_stories(
    house_name: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    initialize_publishing_store()
    staff = _staff_from_authorization(authorization, ("admin", "moderator", "creator"))
    if house_name not in HOUSE_NAMES:
        raise HTTPException(status_code=422, detail="Choose one of the Nine Houses")
    _require_house_access(staff, house_name)
    now = datetime.now(timezone.utc)
    now_text = now.isoformat(timespec="seconds")
    cutoff = (now - timedelta(hours=48)).isoformat(timespec="seconds")
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT house_name, position, story_id, title, body, cover_image_url,
                   audio_content_type, speaker_name, dialect, published_at, expires_at
            FROM house_voice_stories
            WHERE house_name = ? AND expires_at > ?
            ORDER BY position ASC
            """,
            (house_name, now_text),
        ).fetchall()
        counts = connection.execute(
            """
            SELECT position, COUNT(*) AS publish_count
            FROM house_voice_story_events
            WHERE house_name = ? AND created_at >= ?
              AND action IN ('published', 'updated')
            GROUP BY position
            """,
            (house_name, cutoff),
        ).fetchall()
    published_counts = {row["position"]: row["publish_count"] for row in counts}
    stories_by_position = {row["position"]: row for row in rows}
    return {
        "house_name": house_name,
        "slots": [
            {
                "position": position,
                "story": (
                    _house_voice_story_payload(stories_by_position[position])
                    if position in stories_by_position
                    else None
                ),
                "publishes_in_48_hours": published_counts.get(position, 0),
                "publishes_remaining": max(0, 5 - published_counts.get(position, 0)),
            }
            for position in range(1, 4)
        ],
    }


@router.put("/staff/house-voice-stories/{house_name}/{position}")
def save_house_voice_story(
    house_name: str,
    position: int,
    payload: HouseVoiceStoryRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    initialize_publishing_store()
    staff = _staff_from_authorization(authorization, ("admin", "moderator", "creator"))
    if house_name not in HOUSE_NAMES:
        raise HTTPException(status_code=422, detail="Choose one of the Nine Houses")
    if position not in range(1, 4):
        raise HTTPException(status_code=422, detail="Story slot must be between 1 and 3")
    _require_house_access(staff, house_name)
    cover_image_url = _validated_image_url(payload.cover_image_url)
    now = datetime.now(timezone.utc)
    now_text = now.isoformat(timespec="seconds")
    expires_at = (now + timedelta(hours=48)).isoformat(timespec="seconds")
    cutoff = (now - timedelta(hours=48)).isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        publishes = connection.execute(
            """
            SELECT COUNT(*) FROM house_voice_story_events
            WHERE house_name = ? AND position = ? AND created_at >= ?
              AND action IN ('published', 'updated')
            """,
            (house_name, position, cutoff),
        ).fetchone()[0]
        if publishes >= 5:
            raise HTTPException(
                status_code=429,
                detail="This house story reached its five-publish limit for the last 48 hours",
            )
        existing = connection.execute(
            """
            SELECT story_id, audio_content, audio_content_type, expires_at
            FROM house_voice_stories
            WHERE house_name = ? AND position = ?
            """,
            (house_name, position),
        ).fetchone()
        if payload.audio_data:
            audio_content, audio_content_type = _validated_audio_data(payload.audio_data)
        elif (
            existing
            and existing["expires_at"] > now_text
            and not payload.remove_audio
        ):
            audio_content = existing["audio_content"]
            audio_content_type = existing["audio_content_type"]
        else:
            audio_content = b""
            audio_content_type = ""
        story_body = payload.body.strip()
        if not audio_content and not story_body:
            raise HTTPException(
                status_code=422,
                detail="Add a voice recording or a short written note before publishing",
            )
        if (
            not cover_image_url
            and existing
            and existing["expires_at"] > now_text
            and not payload.remove_cover_image
        ):
            cover_image_url = existing["cover_image_url"] or None
        story_id = existing["story_id"] if existing else str(uuid4())
        action = "updated" if existing else "published"
        connection.execute(
            """
            INSERT INTO house_voice_stories
                (house_name, position, story_id, title, body, cover_image_url,
                 audio_content, audio_content_type, speaker_name, dialect,
                 published_at, expires_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(house_name, position) DO UPDATE SET
                story_id = excluded.story_id,
                title = excluded.title,
                body = excluded.body,
                cover_image_url = excluded.cover_image_url,
                audio_content = excluded.audio_content,
                audio_content_type = excluded.audio_content_type,
                speaker_name = excluded.speaker_name,
                dialect = excluded.dialect,
                published_at = excluded.published_at,
                expires_at = excluded.expires_at,
                updated_by = excluded.updated_by
            """,
            (
                house_name,
                position,
                story_id,
                payload.title.strip(),
                story_body,
                cover_image_url or "",
                audio_content,
                audio_content_type,
                payload.speaker_name.strip(),
                payload.dialect.strip(),
                now_text,
                expires_at,
                staff["staff_id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO house_voice_story_events
                (house_name, position, story_id, actor_staff_id, action, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (house_name, position, story_id, staff["staff_id"], action, now_text),
        )
        row = connection.execute(
            """
            SELECT house_name, position, story_id, title, body, cover_image_url,
                   audio_content_type, speaker_name, dialect, published_at, expires_at
            FROM house_voice_stories WHERE house_name = ? AND position = ?
            """,
            (house_name, position),
        ).fetchone()
    return {
        "story": _house_voice_story_payload(row),
        "publishes_in_48_hours": publishes + 1,
        "publishes_remaining": max(0, 4 - publishes),
    }


@router.delete("/staff/house-voice-stories/{house_name}/{position}")
def remove_house_voice_story(
    house_name: str,
    position: int,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    initialize_publishing_store()
    staff = _staff_from_authorization(authorization, ("admin", "moderator", "creator"))
    if house_name not in HOUSE_NAMES:
        raise HTTPException(status_code=422, detail="Choose one of the Nine Houses")
    if position not in range(1, 4):
        raise HTTPException(status_code=422, detail="Story slot must be between 1 and 3")
    _require_house_access(staff, house_name)
    now_text = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT story_id FROM house_voice_stories
            WHERE house_name = ? AND position = ?
            """,
            (house_name, position),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="House voice story not found")
        connection.execute(
            """
            INSERT INTO house_voice_story_events
                (house_name, position, story_id, actor_staff_id, action, created_at)
            VALUES (?, ?, ?, ?, 'removed', ?)
            """,
            (house_name, position, row["story_id"], staff["staff_id"], now_text),
        )
        connection.execute(
            "DELETE FROM house_voice_stories WHERE house_name = ? AND position = ?",
            (house_name, position),
        )
    return {"status": "removed"}


@router.get("/staff/culture/posts")
def staff_culture_posts(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, list[dict[str, Any]]]:
    _staff_from_authorization(authorization, ("admin", "moderator"))
    initialize_publishing_store()
    with _connect() as connection:
        _purge_expired(connection)
        rows = connection.execute(
            """
            SELECT p.post_id, p.text_message, p.audio_content_type, p.retention_lifespan,
                   p.is_visible, p.is_rejected, p.is_voice_submission, p.speaker_name, p.dialect,
                   p.house_name, p.created_at, COUNT(r.report_id) AS report_count
            FROM culture_posts AS p
            LEFT JOIN culture_reports AS r ON r.post_id = p.post_id
            GROUP BY p.post_id
            ORDER BY report_count DESC, p.created_at DESC
            LIMIT 300
            """
        ).fetchall()
    return {
        "posts": [
            {
                **dict(row),
                "is_visible": bool(row["is_visible"]),
                "is_rejected": bool(row["is_rejected"]),
                "is_voice_submission": bool(row["is_voice_submission"]),
                "audio_clip_url": (
                    f"/api/v1/culture/audio/{row['post_id']}"
                    if row["audio_content_type"]
                    else None
                ),
            }
            for row in rows
        ]
    }


@router.get("/staff/culture/audio/{post_id}")
def staff_culture_audio(
    post_id: UUID,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    _staff_from_authorization(authorization, ("admin", "moderator"))
    initialize_publishing_store()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT audio_content, audio_content_type
            FROM culture_posts WHERE post_id = ?
            """,
            (str(post_id),),
        ).fetchone()
    if not row or not row["audio_content"]:
        raise HTTPException(status_code=404, detail="Voice recording not found")
    return Response(
        content=row["audio_content"],
        media_type=row["audio_content_type"],
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.patch("/staff/culture/posts/{post_id}")
def staff_moderate_culture_post(
    post_id: UUID,
    payload: dict[str, bool],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    _staff_from_authorization(authorization, ("admin", "moderator"))
    if set(payload) != {"is_visible"} or not isinstance(payload["is_visible"], bool):
        raise HTTPException(status_code=422, detail="Provide an is_visible boolean")
    initialize_publishing_store()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        result = connection.execute(
            """
            UPDATE culture_posts
            SET is_visible = ?, is_rejected = ?,
                featured_date = CASE WHEN ? = 0 THEN NULL ELSE featured_date END
            WHERE post_id = ?
            """,
            (
                int(payload["is_visible"]),
                int(not payload["is_visible"]),
                int(payload["is_visible"]),
                str(post_id),
            ),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Community post not found")
        if not payload["is_visible"]:
            connection.execute(
                "DELETE FROM weekly_house_voices WHERE post_id = ?", (str(post_id),)
            )
    return {"is_visible": payload["is_visible"]}


@router.get("/staff/voices/candidates")
def staff_voice_candidates(
    house_name: str | None = None,
    daily: bool = False,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, list[dict[str, Any]]]:
    staff = _staff_from_authorization(authorization, ("admin", "moderator", "creator"))
    if house_name and house_name not in HOUSE_NAMES:
        raise HTTPException(status_code=422, detail="Choose one of the Nine Houses")
    managed_houses = _managed_house_names(staff)
    if house_name:
        _require_house_access(staff, house_name)
    elif staff["role"] != "admin" and not managed_houses:
        raise HTTPException(status_code=403, detail="Your account is not assigned to a house")
    initialize_publishing_store()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    query = """
        SELECT post_id, text_message, audio_content_type, speaker_name, dialect,
               house_name, created_at
        FROM culture_posts
        WHERE is_voice_submission = 1 AND is_rejected = 0 AND created_at >= ?
    """
    params: tuple[str, ...] = (cutoff,)
    if daily:
        today = _local_day()
        local_start = datetime.combine(today, datetime.min.time(), LOCAL_TIMEZONE)
        params = (
            local_start.astimezone(timezone.utc).isoformat(timespec="seconds"),
            (local_start + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds"),
        )
        query = """
            SELECT post_id, text_message, audio_content_type, speaker_name, dialect,
                   house_name, created_at
            FROM culture_posts
            WHERE is_voice_submission = 1 AND is_rejected = 0 AND created_at >= ? AND created_at < ?
        """
    if house_name:
        query += " AND house_name = ?"
        params += (house_name,)
    elif staff["role"] != "admin":
        placeholders = ",".join("?" for _ in managed_houses)
        query += f" AND house_name IN ({placeholders})"
        params += managed_houses
    query += " ORDER BY created_at DESC LIMIT 200"
    with _connect() as connection:
        _purge_expired(connection)
        rows = connection.execute(query, params).fetchall()
    return {"voices": [_voice_payload(row) for row in rows]}


@router.put("/staff/voices/daily")
def select_daily_voice(
    payload: DailyVoiceRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    staff = _staff_from_authorization(authorization, ("admin", "moderator"))
    initialize_publishing_store()
    today = _local_day()
    local_start = datetime.combine(today, datetime.min.time(), LOCAL_TIMEZONE)
    start_utc = local_start.astimezone(timezone.utc).isoformat(timespec="seconds")
    next_utc = (local_start + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _purge_expired(connection)
        candidate = connection.execute(
            """
            SELECT house_name FROM culture_posts
            WHERE post_id = ? AND is_rejected = 0 AND is_voice_submission = 1
              AND created_at >= ? AND created_at < ?
            """,
            (str(payload.post_id), start_utc, next_utc),
        ).fetchone()
        if not candidate:
            raise HTTPException(status_code=404, detail="Today's eligible voice nomination was not found")
        if not candidate["house_name"] and staff["role"] != "admin":
            raise HTTPException(status_code=403, detail="A house assignment is required to feature this voice")
        if candidate["house_name"]:
            _require_house_access(staff, candidate["house_name"])
        connection.execute(
            "UPDATE culture_posts SET featured_date = NULL WHERE featured_date = ?",
            (today.isoformat(),),
        )
        connection.execute(
            "UPDATE culture_posts SET featured_date = ?, is_visible = 1 WHERE post_id = ?",
            (today.isoformat(), str(payload.post_id)),
        )
    return {"status": "selected", "date": today.isoformat(), "selected_by": staff["full_name"]}


@router.put("/staff/voices/weekly")
def select_weekly_voices(
    payload: WeeklyVoicesRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    staff = _staff_from_authorization(authorization, ("admin", "moderator", "creator"))
    if payload.house_name not in HOUSE_NAMES:
        raise HTTPException(status_code=422, detail="Choose one of the Nine Houses")
    _require_house_access(staff, payload.house_name)
    post_ids = [str(post_id) for post_id in payload.post_ids]
    if len(set(post_ids)) != len(post_ids):
        raise HTTPException(status_code=422, detail="Select different community voices")
    week_start = _week_start()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _purge_expired(connection)
        placeholders = ",".join("?" for _ in post_ids)
        candidates = (
            connection.execute(
                f"""
                SELECT post_id FROM culture_posts
                WHERE post_id IN ({placeholders}) AND is_voice_submission = 1
                  AND is_rejected = 0 AND house_name = ?
                """,
                (*post_ids, payload.house_name),
            ).fetchall()
            if post_ids
            else []
        )
        if len(candidates) != len(post_ids):
            raise HTTPException(
                status_code=422,
                detail="Choose eligible voice nominations belonging to this house",
            )
        connection.execute(
            "DELETE FROM weekly_house_voices WHERE week_start = ? AND house_name = ?",
            (week_start, payload.house_name),
        )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for position, post_id in enumerate(post_ids, start=1):
            connection.execute(
                """
                INSERT INTO weekly_house_voices
                    (week_start, house_name, position, post_id, selected_by, selected_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (week_start, payload.house_name, position, post_id, staff["staff_id"], now),
            )
            connection.execute(
                """
                UPDATE culture_posts
                SET retention_lifespan = '7_DAYS', preserved_until = ?, is_visible = 1
                WHERE post_id = ?
                """,
                (
                    (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(
                        timespec="seconds"
                    ),
                    post_id,
                ),
            )
    return {
        "status": "selected",
        "house_name": payload.house_name,
        "week_start": week_start,
        "count": len(post_ids),
    }
