import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from app.culture import AUDIO_TYPES, MAX_AUDIO_BYTES, _client_hash, _connect
from app.publishing import _staff_from_authorization

public_router = APIRouter(prefix="/api/v1", tags=["Live companion"])
staff_router = APIRouter(prefix="/api/v1/staff", tags=["Live companion management"])


class LiveSessionCreate(BaseModel):
    creator_name: str = Field(default="Kokwet Community", min_length=2, max_length=80)
    platform: Literal["tiktok", "instagram"] | None = None
    stream_url: str | None = Field(default=None, max_length=1000)
    topic_title: str = Field(min_length=3, max_length=160)

    @field_validator("stream_url")
    @classmethod
    def validate_stream_url(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip()
        parsed = urlparse(normalized)
        hostname = (parsed.hostname or "").lower()
        allowed_host = (
            hostname == "tiktok.com"
            or hostname.endswith(".tiktok.com")
            or hostname == "instagram.com"
            or hostname.endswith(".instagram.com")
        )
        if parsed.scheme != "https" or not parsed.netloc or not allowed_host:
            raise ValueError(f"Use a valid HTTPS {info.data.get('platform') or 'social'} live URL")
        return normalized

    @model_validator(mode="after")
    def validate_room(self):
        self.creator_name = self.creator_name.strip() or "Kokwet Community"
        self.topic_title = self.topic_title.strip()
        if len(self.topic_title) < 3:
            raise ValueError("Room topic must contain at least three characters")
        if bool(self.stream_url) != bool(self.platform):
            raise ValueError("Choose a platform and provide a stream URL together")
        return self


class LiveMessageCreate(BaseModel):
    channel: Literal["topic", "public"]
    session_id: UUID | None = None
    author_name: str = Field(min_length=2, max_length=40)
    message_text: str | None = Field(default=None, max_length=500)
    tag_name: str | None = Field(default=None, max_length=33)

    @model_validator(mode="after")
    def validate_channel_session(self):
        if self.channel == "topic" and self.session_id is None:
            raise ValueError("A topic message needs a live session")
        if self.channel == "public" and self.session_id is not None:
            raise ValueError("Public lounge messages cannot target a live session")
        self.author_name = self.author_name.strip()
        if len(self.author_name) < 2:
            raise ValueError("Display name must contain at least two characters")
        self.message_text = self.message_text.strip() if self.message_text else None
        if self.tag_name:
            self.tag_name = self.tag_name.strip().removeprefix("#")
            if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", self.tag_name):
                raise ValueError("Topic tags can contain only letters, numbers, and underscores")
        if not self.message_text and not self.tag_name:
            raise ValueError("Write a message or choose a topic tag")
        return self


def initialize_live_store() -> None:
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS live_sessions (
                session_id TEXT PRIMARY KEY,
                creator_name TEXT NOT NULL,
                platform TEXT NOT NULL CHECK (platform IN ('tiktok', 'instagram')),
                stream_url TEXT NOT NULL,
                topic_title TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                created_by TEXT REFERENCES staff_users(staff_id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_live_sessions_active
                ON live_sessions(is_active, started_at DESC);
            CREATE TABLE IF NOT EXISTS live_messages (
                message_id TEXT PRIMARY KEY,
                session_id TEXT REFERENCES live_sessions(session_id) ON DELETE CASCADE,
                channel TEXT NOT NULL CHECK (channel IN ('topic', 'public')),
                author_name TEXT NOT NULL,
                message_text TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'text'
                    CHECK (message_type IN ('text', 'voice', 'tag')),
                tag_name TEXT,
                audio_content_type TEXT,
                audio_content BLOB,
                client_ip_hash TEXT NOT NULL,
                is_hidden INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                CHECK ((channel = 'topic' AND session_id IS NOT NULL)
                    OR (channel = 'public' AND session_id IS NULL))
            );
            CREATE INDEX IF NOT EXISTS idx_live_messages_channel_time
                ON live_messages(channel, session_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_live_messages_rate
                ON live_messages(client_ip_hash, created_at);
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(live_messages)").fetchall()
        }
        for column, definition in (
            ("message_type", "TEXT NOT NULL DEFAULT 'text'"),
            ("tag_name", "TEXT"),
            ("audio_content_type", "TEXT"),
            ("audio_content", "BLOB"),
        ):
            if column not in columns:
                connection.execute(f"ALTER TABLE live_messages ADD COLUMN {column} {definition}")


def _session_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "session_id": row["session_id"],
        "creator_name": row["creator_name"],
        "platform": row["platform"] if row["stream_url"] else None,
        "stream_url": row["stream_url"] or None,
        "topic_title": row["topic_title"],
        "is_active": bool(row["is_active"]),
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
    }


def _message_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "session_id": row["session_id"],
        "channel": row["channel"],
        "author_name": row["author_name"],
        "message_text": row["message_text"],
        "message_type": row["message_type"],
        "tag_name": row["tag_name"],
        "audio_url": (
            f"/api/v1/live/messages/{row['message_id']}/audio"
            if row["audio_content_type"]
            else None
        ),
        "created_at": row["created_at"],
    }


def _validate_live_channel(
    connection: sqlite3.Connection,
    channel: str,
    session_id: UUID | None,
) -> None:
    if channel == "topic":
        session = connection.execute(
            "SELECT 1 FROM live_sessions WHERE session_id = ? AND is_active = 1",
            (str(session_id),),
        ).fetchone()
        if not session:
            raise HTTPException(status_code=409, detail="This live topic has ended")


def _enforce_live_rate_limit(
    connection: sqlite3.Connection,
    ip_hash: str,
    now: datetime,
) -> None:
    cutoff = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
    recent_count = connection.execute(
        "SELECT COUNT(*) FROM live_messages WHERE client_ip_hash = ? AND created_at >= ?",
        (ip_hash, cutoff),
    ).fetchone()[0]
    if recent_count >= 6:
        raise HTTPException(status_code=429, detail="Please wait before posting another live message")


def _matches_audio_signature(content_type: str, content: bytes) -> bool:
    if content_type == "audio/mpeg":
        return content.startswith(b"ID3") or (
            len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0
        )
    if content_type == "audio/mp4":
        return len(content) >= 12 and content[4:8] == b"ftyp"
    if content_type == "audio/ogg":
        return content.startswith(b"OggS")
    if content_type in {"audio/wav", "audio/x-wav"}:
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WAVE"
    if content_type == "audio/webm":
        return content.startswith(b"\x1a\x45\xdf\xa3")
    return False


def _require_live_manager(staff: sqlite3.Row) -> None:
    if staff["role"] != "admin" and not staff["can_manage_live"]:
        raise HTTPException(status_code=403, detail="Your account is not assigned to manage live sessions")


def _require_live_moderator(staff: sqlite3.Row) -> None:
    if staff["role"] not in {"admin", "moderator"} and not staff["can_manage_live"]:
        raise HTTPException(status_code=403, detail="Your account is not assigned to moderate live chat")


@public_router.get("/live/current")
def get_current_live_session() -> dict[str, Any]:
    initialize_live_store()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT session_id, creator_name, platform, stream_url, topic_title,
                   is_active, started_at, ended_at
            FROM live_sessions WHERE is_active = 1
            ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
    return {"session": _session_payload(row) if row else None}


@public_router.get("/live/sessions/{session_id}")
def get_live_session(session_id: UUID) -> dict[str, Any]:
    initialize_live_store()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT session_id, creator_name, platform, stream_url, topic_title,
                   is_active, started_at, ended_at
            FROM live_sessions WHERE session_id = ?
            """,
            (str(session_id),),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Live session not found")
    return {"session": _session_payload(row)}


@public_router.get("/live/messages")
def get_live_messages(
    channel: Literal["topic", "public"],
    session_id: UUID | None = None,
) -> dict[str, list[dict[str, Any]]]:
    initialize_live_store()
    if (channel == "topic") != (session_id is not None):
        raise HTTPException(status_code=422, detail="Choose a topic session or the public lounge")
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT message_id, session_id, channel, author_name, message_text,
                   message_type, tag_name, audio_content_type, created_at
            FROM live_messages
            WHERE channel = ? AND session_id IS ? AND is_hidden = 0
            ORDER BY created_at DESC LIMIT 50
            """,
            (channel, str(session_id) if session_id else None),
        ).fetchall()
    return {"messages": [_message_payload(row) for row in reversed(rows)]}


@public_router.post("/live/messages", status_code=201)
def post_live_message(payload: LiveMessageCreate, request: Request) -> dict[str, Any]:
    initialize_live_store()
    ip_hash = _client_hash(request)
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _validate_live_channel(connection, payload.channel, payload.session_id)
        _enforce_live_rate_limit(connection, ip_hash, now)
        message_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO live_messages
                (message_id, session_id, channel, author_name, message_text,
                 message_type, tag_name, client_ip_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                str(payload.session_id) if payload.session_id else None,
                payload.channel,
                payload.author_name.strip(),
                payload.message_text or "",
                "tag" if payload.tag_name else "text",
                payload.tag_name,
                ip_hash,
                timestamp,
            ),
        )
        row = connection.execute(
            """
            SELECT message_id, session_id, channel, author_name, message_text,
                   message_type, tag_name, audio_content_type, created_at
            FROM live_messages WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()
    return {"message": _message_payload(row)}


@public_router.post("/live/messages/voice", status_code=201)
async def post_live_voice_message(
    request: Request,
    channel: Annotated[Literal["topic", "public"], Form()],
    author_name: Annotated[str, Form()],
    audio_file: Annotated[UploadFile, File()],
    session_id: Annotated[UUID | None, Form()] = None,
) -> dict[str, Any]:
    initialize_live_store()
    normalized_name = author_name.strip()
    if not 2 <= len(normalized_name) <= 40:
        raise HTTPException(status_code=422, detail="Display name must contain 2 to 40 characters")

    content_type = (audio_file.content_type or "").split(";", maxsplit=1)[0].strip().lower()
    if content_type not in AUDIO_TYPES:
        raise HTTPException(status_code=415, detail="Use MP3, WAV, MP4, OGG, or WebM audio")
    audio_content = await audio_file.read(MAX_AUDIO_BYTES + 1)
    if len(audio_content) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Voice notes must be 2 MB or smaller")
    if not audio_content or not _matches_audio_signature(content_type, audio_content):
        raise HTTPException(status_code=415, detail="The uploaded file does not match its declared audio type")

    ip_hash = _client_hash(request)
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat(timespec="seconds")
    message_id = str(uuid4())
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _validate_live_channel(connection, channel, session_id)
        _enforce_live_rate_limit(connection, ip_hash, now)
        connection.execute(
            """
            INSERT INTO live_messages
                (message_id, session_id, channel, author_name, message_text,
                 message_type, audio_content_type, audio_content,
                 client_ip_hash, created_at)
            VALUES (?, ?, ?, ?, '', 'voice', ?, ?, ?, ?)
            """,
            (
                message_id,
                str(session_id) if session_id else None,
                channel,
                normalized_name,
                content_type,
                sqlite3.Binary(audio_content),
                ip_hash,
                timestamp,
            ),
        )
        row = connection.execute(
            """
            SELECT message_id, session_id, channel, author_name, message_text,
                   message_type, tag_name, audio_content_type, created_at
            FROM live_messages WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()
    return {"message": _message_payload(row)}


@public_router.get("/live/messages/{message_id}/audio")
def get_live_message_audio(message_id: UUID) -> Response:
    initialize_live_store()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT audio_content, audio_content_type FROM live_messages
            WHERE message_id = ? AND is_hidden = 0
            """,
            (str(message_id),),
        ).fetchone()
    if not row or not row["audio_content"]:
        raise HTTPException(status_code=404, detail="Voice note not found")
    return Response(
        content=row["audio_content"],
        media_type=row["audio_content_type"],
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@staff_router.get("/live")
def list_live_sessions(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, list[dict[str, Any]]]:
    staff = _staff_from_authorization(authorization)
    _require_live_manager(staff)
    initialize_live_store()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT session_id, creator_name, platform, stream_url, topic_title,
                   is_active, started_at, ended_at
            FROM live_sessions ORDER BY started_at DESC LIMIT 30
            """
        ).fetchall()
    return {"sessions": [_session_payload(row) for row in rows]}


@staff_router.get("/live/messages")
def list_live_messages(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, list[dict[str, Any]]]:
    staff = _staff_from_authorization(authorization)
    _require_live_moderator(staff)
    initialize_live_store()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT message_id, session_id, channel, author_name, message_text,
                   message_type, tag_name, audio_content_type, created_at, is_hidden
            FROM live_messages
            ORDER BY created_at DESC LIMIT 100
            """
        ).fetchall()
    return {
        "messages": [
            {**_message_payload(row), "is_hidden": bool(row["is_hidden"])}
            for row in rows
        ]
    }


@staff_router.post("/live", status_code=201)
def start_live_session(
    payload: LiveSessionCreate,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    staff = _staff_from_authorization(authorization)
    _require_live_manager(staff)
    initialize_live_store()
    session_id = str(uuid4())
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE live_sessions SET is_active = 0, ended_at = ? WHERE is_active = 1",
            (started_at,),
        )
        connection.execute(
            """
            INSERT INTO live_sessions
                (session_id, creator_name, platform, stream_url, topic_title,
                 is_active, started_at, created_by)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                session_id,
                payload.creator_name.strip(),
                payload.platform or "tiktok",
                payload.stream_url or "",
                payload.topic_title.strip(),
                started_at,
                staff["staff_id"],
            ),
        )
        row = connection.execute(
            """
            SELECT session_id, creator_name, platform, stream_url, topic_title,
                   is_active, started_at, ended_at
            FROM live_sessions WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    return {"session": _session_payload(row)}


@staff_router.patch("/live/{session_id}/end")
def end_live_session(
    session_id: UUID,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    staff = _staff_from_authorization(authorization)
    _require_live_manager(staff)
    initialize_live_store()
    ended_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        result = connection.execute(
            """
            UPDATE live_sessions SET is_active = 0, ended_at = ?
            WHERE session_id = ? AND is_active = 1
            """,
            (ended_at, str(session_id)),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Active live session not found")
    return {"status": "ended", "session_id": str(session_id), "ended_at": ended_at}


@staff_router.delete("/live/messages/{message_id}")
def delete_live_message(
    message_id: UUID,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    staff = _staff_from_authorization(authorization)
    _require_live_moderator(staff)
    initialize_live_store()
    with _connect() as connection:
        result = connection.execute(
            "UPDATE live_messages SET is_hidden = 1 WHERE message_id = ?",
            (str(message_id),),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Live message not found")
    return {"status": "hidden"}
