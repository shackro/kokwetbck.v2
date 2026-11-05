from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query

from app.database import connect as _database_connect
from app.database import postgres_mode
from app.publishing import _staff_from_authorization

router = APIRouter(prefix="/api/legacy", tags=["Legacy data"])

_PUBLIC_SETTING_KEYS = (
    "whatsapp_number",
    "whatsapp_message",
    "navigation",
    "footer_tagline",
    "footer_copyright",
    "logo_wordmark_light_url",
    "logo_full_light_url",
    "marketplace_button_label",
    "marketplace_navigation_label",
)


def _require_postgres() -> None:
    if not postgres_mode():
        raise HTTPException(
            status_code=503,
            detail="Legacy Supabase data is available only in PostgreSQL mode",
        )


def _require_legacy_table(connection: Any, table_name: str) -> None:
    available = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = ?
              AND table_type = 'BASE TABLE'
        ) AS available
        """,
        (table_name,),
    ).fetchone()
    if not available or not available["available"]:
        raise HTTPException(
            status_code=503,
            detail=f"Legacy table public.{table_name} is unavailable",
        )


def _safe_https_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme == "https" and parsed.netloc:
        return value
    return None


@router.get("/live-sessions")
def list_legacy_live_sessions(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, list[dict[str, Any]]]:
    _require_postgres()
    with _database_connect() as connection:
        _require_legacy_table(connection, "live_sessions")
        rows = connection.execute(
            """
            SELECT session_id, creator_name, platform, stream_url, topic_title,
                   is_active, started_at
            FROM public.live_sessions
            WHERE is_active = TRUE
            ORDER BY started_at DESC
            LIMIT ?
            OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return {"sessions": [dict(row) for row in rows]}


@router.get("/live-messages")
def list_legacy_live_messages(
    session_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, list[dict[str, Any]]]:
    _require_postgres()
    with _database_connect() as connection:
        _require_legacy_table(connection, "live_chat_messages")
        if session_id is None:
            rows = connection.execute(
                """
                SELECT message_id, session_id, author_name, message_text,
                       message_type, audio_url, tag_name, created_at
                FROM public.live_chat_messages
                ORDER BY created_at DESC
                LIMIT ?
                OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT message_id, session_id, author_name, message_text,
                       message_type, audio_url, tag_name, created_at
                FROM public.live_chat_messages
                WHERE session_id = ?::uuid
                ORDER BY created_at DESC
                LIMIT ?
                OFFSET ?
                """,
                (str(session_id), limit, offset),
            ).fetchall()
    messages = []
    for row in reversed(rows):
        message = dict(row)
        message["audio_url"] = _safe_https_url(message["audio_url"])
        messages.append(message)
    return {"messages": messages}


@router.get("/culture-posts")
def list_legacy_culture_posts(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, list[dict[str, Any]]]:
    _require_postgres()
    with _database_connect() as connection:
        _require_legacy_table(connection, "open_stream_interactions")
        rows = connection.execute(
            """
            SELECT post_id, text_message, audio_clip_url, retention_lifespan,
                   created_at
            FROM public.open_stream_interactions
            WHERE is_visible = TRUE
            ORDER BY created_at DESC
            LIMIT ?
            OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    posts = []
    for row in reversed(rows):
        post = dict(row)
        post["audio_clip_url"] = _safe_https_url(post["audio_clip_url"])
        posts.append(post)
    return {"posts": posts}


@router.get("/site-settings")
def get_legacy_site_settings() -> dict[str, str]:
    _require_postgres()
    placeholders = ", ".join("?" for _ in _PUBLIC_SETTING_KEYS)
    with _database_connect() as connection:
        _require_legacy_table(connection, "site_settings")
        rows = connection.execute(
            f"""
            SELECT setting_key, setting_value
            FROM public.site_settings
            WHERE setting_key IN ({placeholders})
            """,
            _PUBLIC_SETTING_KEYS,
        ).fetchall()
    return {row["setting_key"]: row["setting_value"] for row in rows}


@router.get("/event-passes")
def list_legacy_event_passes(
        authorization: Annotated[str | None, Header()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, list[dict[str, Any]]]:
        _staff_from_authorization(authorization, ("admin",))
        _require_postgres()
        with _database_connect() as connection:
            _require_legacy_table(connection, "secure_event_passes")
            rows = connection.execute(
                """
                SELECT pass_id, buyer_phone, pass_tier, status, scanned_at,
                       mpesa_receipt, created_at
                FROM public.secure_event_passes
                ORDER BY created_at DESC
                LIMIT ?
                OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return {"passes": [dict(row) for row in rows]}


@router.get("/profiles")
def list_legacy_profiles(
    authorization: Annotated[str | None, Header()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, list[dict[str, Any]]]:
    _staff_from_authorization(authorization, ("admin",))
    _require_postgres()
    with _database_connect() as connection:
        _require_legacy_table(connection, "user_service_profiles")
        rows = connection.execute(
            """
            SELECT profile_id, phone_number, full_name, audience_cohort, role,
                   is_super_admin, is_menjo_verified, is_active, manager_id,
                   created_at
            FROM public.user_service_profiles
            ORDER BY created_at DESC
            LIMIT ?
            OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return {"profiles": [dict(row) for row in rows]}


@router.get("/activation-summary")
def get_legacy_activation_summary(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, list[dict[str, Any]]]:
    _staff_from_authorization(authorization, ("admin",))
    _require_postgres()
    with _database_connect() as connection:
        _require_legacy_table(connection, "activation_codes")
        rows = connection.execute(
            """
            SELECT assigned_role, is_used, expires_at <= CURRENT_TIMESTAMP AS expired,
                   COUNT(*) AS code_count
            FROM public.activation_codes
            GROUP BY assigned_role, is_used, expires_at <= CURRENT_TIMESTAMP
            ORDER BY assigned_role, is_used, expired
            """
        ).fetchall()
    return {"summary": [dict(row) for row in rows]}
