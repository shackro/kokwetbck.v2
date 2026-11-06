import asyncio
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.constants import HOUSE_NAMES
from app.database import connect as _database_connect

router = APIRouter(prefix="/api/v1", tags=["Culture"])

MAX_AUDIO_BYTES = 2 * 1024 * 1024
MAX_TEXT_LENGTH = 1000
MAX_POSTS_PER_DAY = 3
AUDIO_TYPES = {
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/webm": "webm",
    "audio/x-wav": "wav",
}


class CultureExtensionRequest(BaseModel):
    post_id: UUID
    phone_number: str = Field(pattern=r"^(?:07|01|\+254|254)[0-9]{8,9}$")
    amount: Literal[30] = 30


class CultureModerationRequest(BaseModel):
    is_visible: bool


def _connect() -> Any:
    return _database_connect()


def initialize_culture_store() -> None:
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS culture_posts (
                post_id TEXT PRIMARY KEY,
                client_ip_hash TEXT NOT NULL,
                text_message TEXT,
                audio_content BLOB,
                audio_content_type TEXT,
                retention_lifespan TEXT NOT NULL DEFAULT '24_HOURS'
                    CHECK (retention_lifespan IN ('24_HOURS', '7_DAYS')),
                mpesa_checkout_id TEXT,
                is_visible INTEGER NOT NULL DEFAULT 1,
                is_rejected INTEGER NOT NULL DEFAULT 0,
                is_voice_submission INTEGER NOT NULL DEFAULT 0,
                speaker_name TEXT,
                dialect TEXT,
                house_name TEXT,
                featured_date TEXT,
                preserved_until TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_culture_posts_feed
                ON culture_posts(is_visible, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_culture_posts_rate
                ON culture_posts(client_ip_hash, created_at);
            CREATE TABLE IF NOT EXISTS culture_reports (
                report_id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id TEXT NOT NULL REFERENCES culture_posts(post_id) ON DELETE CASCADE,
                client_ip_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(post_id, client_ip_hash)
            );
            CREATE TABLE IF NOT EXISTS culture_pending_payments (
                checkout_request_id TEXT PRIMARY KEY,
                post_id TEXT NOT NULL REFERENCES culture_posts(post_id) ON DELETE CASCADE,
                account_reference TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('initiating', 'pending', 'completed', 'failed')),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_culture_pending_post
                ON culture_pending_payments(post_id, status);
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(culture_posts)").fetchall()
        }
        for column, definition in (
            ("is_voice_submission", "INTEGER NOT NULL DEFAULT 0"),
            ("is_rejected", "INTEGER NOT NULL DEFAULT 0"),
            ("speaker_name", "TEXT"),
            ("dialect", "TEXT"),
            ("house_name", "TEXT"),
            ("featured_date", "TEXT"),
            ("preserved_until", "TEXT"),
        ):
            if column not in columns:
                connection.execute(f"ALTER TABLE culture_posts ADD COLUMN {column} {definition}")
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_culture_daily_feature
            ON culture_posts(featured_date)
            WHERE featured_date IS NOT NULL
            """
        )


def _purge_expired(connection: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc)
    rows = connection.execute(
        """
        SELECT post_id, created_at, preserved_until, retention_lifespan
        FROM culture_posts
        """
    ).fetchall()
    expired_ids: list[str] = []
    for row in rows:
        expiry_value = (
            row["created_at"]
            if row["preserved_until"] is None
            else row["preserved_until"]
        )
        try:
            expires_at = datetime.fromisoformat(expiry_value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if row["preserved_until"] is None:
            hours = 168 if row["retention_lifespan"] == "7_DAYS" else 24
            expires_at += timedelta(hours=hours)
        if expires_at <= now:
            expired_ids.append(row["post_id"])
    for start in range(0, len(expired_ids), 500):
        batch = expired_ids[start : start + 500]
        placeholders = ", ".join("?" for _ in batch)
        connection.execute(
            f"DELETE FROM culture_posts WHERE post_id IN ({placeholders})",
            batch,
        )


def purge_expired_culture_posts() -> None:
    initialize_culture_store()
    with _connect() as connection:
        _purge_expired(connection)


def _client_hash(request: Request) -> str:
    client_host = request.client.host if request.client else "unknown"
    secret = os.getenv("CULTURE_IP_HASH_SECRET", "kokwet-culture-rate-limit")
    return hmac.new(secret.encode(), client_host.encode(), hashlib.sha256).hexdigest()


def _require_admin(x_admin_key: str | None) -> None:
    configured_key = os.getenv("KOKWET_ADMIN_API_KEY")
    if not configured_key:
        raise HTTPException(status_code=503, detail="Admin moderation is not configured")
    if not x_admin_key or not hmac.compare_digest(x_admin_key, configured_key):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")


def _active_post_exists(post_id: str) -> bool:
    with _connect() as connection:
        _purge_expired(connection)
        row = connection.execute(
            "SELECT 1 FROM culture_posts WHERE post_id = ? AND is_visible = 1",
            (post_id,),
        ).fetchone()
    return row is not None


def _phone_for_daraja(phone: str) -> str:
    digits = phone.removeprefix("+")
    if digits.startswith("0"):
        return "254" + digits[1:]
    if digits.startswith("254"):
        return digits
    raise HTTPException(status_code=422, detail="Enter a valid Kenyan mobile number")


def _mpesa_settings() -> tuple[str, str, str, str, str]:
    environment = os.getenv("MPESA_ENV", "sandbox")
    if environment not in {"sandbox", "production"}:
        raise HTTPException(status_code=503, detail="MPESA_ENV must be sandbox or production")

    required = {
        "MPESA_CONSUMER_KEY": os.getenv("MPESA_CONSUMER_KEY"),
        "MPESA_CONSUMER_SECRET": os.getenv("MPESA_CONSUMER_SECRET"),
        "MPESA_SHORTCODE": os.getenv("MPESA_SHORTCODE"),
        "MPESA_PASSKEY": os.getenv("MPESA_PASSKEY"),
        "MPESA_CALLBACK_URL": os.getenv("MPESA_CALLBACK_URL"),
        "CULTURE_WEBHOOK_SECRET": os.getenv("CULTURE_WEBHOOK_SECRET"),
    }
    if any(not value for value in required.values()):
        raise HTTPException(status_code=503, detail="M-Pesa payments are not configured")

    callback = urllib.parse.urlsplit(required["MPESA_CALLBACK_URL"] or "")
    if callback.scheme != "https" or not callback.netloc:
        raise HTTPException(status_code=503, detail="MPESA_CALLBACK_URL must be a public HTTPS URL")
    return (
        environment,
        required["MPESA_CONSUMER_KEY"] or "",
        required["MPESA_CONSUMER_SECRET"] or "",
        required["MPESA_SHORTCODE"] or "",
        required["MPESA_PASSKEY"] or "",
    )


def _callback_url(path: str | None = None) -> str:
    url = os.getenv("MPESA_CALLBACK_URL", "")
    parts = urllib.parse.urlsplit(url)
    if path:
        parts = parts._replace(path=path, query="", fragment="")
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "token"]
    query.append(("token", os.getenv("CULTURE_WEBHOOK_SECRET", "")))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


def _request_json(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_body = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=request_body, headers=headers, method="POST" if body else "GET")
    with urllib.request.urlopen(request, timeout=20) as response:
        decoded = json.loads(response.read())
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=502, detail="M-Pesa returned an invalid response")
    return decoded


def _daraja_access_token(environment: str, consumer_key: str, consumer_secret: str) -> str:
    host = (
        "https://sandbox.safaricom.co.ke"
        if environment == "sandbox"
        else "https://api.safaricom.co.ke"
    )
    credentials = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()
    try:
        token_response = _request_json(
            f"{host}/oauth/v1/generate?grant_type=client_credentials",
            headers={"Authorization": f"Basic {credentials}"},
        )
    except urllib.error.HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail=f"Daraja rejected the OAuth request (HTTP {error.code})",
        ) from error
    except urllib.error.URLError as error:
        raise HTTPException(status_code=502, detail="Daraja sandbox could not be reached") from error
    except (TimeoutError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=502, detail="Daraja returned an unreadable OAuth response") from error

    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(status_code=502, detail="Daraja did not provide an OAuth access token")
    return access_token


def _initiate_stk_push(
    phone: str,
    account_reference: str,
    *,
    amount: int = 30,
    description: str = "Kokwet 7-day post preservation",
    callback_path: str | None = None,
) -> dict[str, Any]:
    environment, consumer_key, consumer_secret, shortcode, passkey = _mpesa_settings()
    host = "https://sandbox.safaricom.co.ke" if environment == "sandbox" else "https://api.safaricom.co.ke"
    try:
        access_token = _daraja_access_token(environment, consumer_key, consumer_secret)

        timestamp = datetime.now(ZoneInfo("Africa/Nairobi")).strftime("%Y%m%d%H%M%S")
        password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()
        return _request_json(
            f"{host}/mpesa/stkpush/v1/processrequest",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            body={
                "BusinessShortCode": shortcode,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": amount,
                "PartyA": phone,
                "PartyB": shortcode,
                "PhoneNumber": phone,
                "CallBackURL": _callback_url(callback_path),
                "AccountReference": account_reference,
                "TransactionDesc": description,
            },
        )
    except urllib.error.HTTPError as error:
        raise HTTPException(status_code=502, detail="M-Pesa rejected the checkout request") from error
    except urllib.error.URLError as error:
        raise HTTPException(status_code=502, detail="M-Pesa could not be reached") from error
    except (TimeoutError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=502, detail="M-Pesa returned an unreadable response") from error


@router.post("/culture/post", status_code=201)
async def create_culture_post(
    request: Request,
    text_message: Annotated[str | None, Form(max_length=MAX_TEXT_LENGTH)] = None,
    audio_file: Annotated[UploadFile | None, File()] = None,
    voice_submission: Annotated[bool, Form()] = False,
    speaker_name: Annotated[str | None, Form(max_length=60)] = None,
    dialect: Annotated[str | None, Form(max_length=60)] = None,
    house_name: Annotated[str | None, Form(max_length=80)] = None,
) -> dict[str, str]:
    text = text_message.strip() if text_message else None
    if not text and not audio_file:
        raise HTTPException(status_code=400, detail="Write a message or attach a voice note")
    if text and len(text) > MAX_TEXT_LENGTH:
        raise HTTPException(status_code=413, detail="Text must be 1,000 characters or fewer")
    if voice_submission and not house_name:
        raise HTTPException(status_code=422, detail="Choose a house when nominating a voice")
    if house_name and house_name not in HOUSE_NAMES:
        raise HTTPException(status_code=422, detail="Choose one of the Nine Houses")
    if not voice_submission:
        speaker_name = None
        dialect = None
        house_name = None

    audio_content: bytes | None = None
    content_type: str | None = None
    if audio_file:
        content_type = (audio_file.content_type or "").lower().split(";", maxsplit=1)[0].strip()
        if content_type not in AUDIO_TYPES:
            raise HTTPException(status_code=415, detail="Use MP3, WAV, MP4, OGG, or WebM audio")
        audio_content = await audio_file.read(MAX_AUDIO_BYTES + 1)
        if len(audio_content) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="Voice notes must be 2 MB or smaller")

    initialize_culture_store()
    client_hash = _client_hash(request)
    post_id = str(uuid4())
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    local_now = datetime.now(ZoneInfo("Africa/Nairobi"))
    local_day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_day_start = local_day_start.astimezone(timezone.utc).isoformat(timespec="seconds")
    utc_next_day_start = (local_day_start + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _purge_expired(connection)
        count = connection.execute(
            """
            SELECT COUNT(*) FROM culture_posts
            WHERE client_ip_hash = ? AND created_at >= ?
            """,
            (
                client_hash,
                (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"),
            ),
        ).fetchone()[0]
        if count >= MAX_POSTS_PER_DAY:
            raise HTTPException(status_code=429, detail="You can share up to 3 posts every 24 hours")
        if voice_submission:
            daily_count = connection.execute(
                """
                SELECT COUNT(*) FROM culture_posts
                WHERE is_voice_submission = 1 AND created_at >= ? AND created_at < ?
                """,
                (utc_day_start, utc_next_day_start),
            ).fetchone()[0]
            if daily_count >= 100:
                raise HTTPException(status_code=429, detail="Today's 100 featured-voice nominations are full")
            already_nominated = connection.execute(
                """
                SELECT 1 FROM culture_posts
                WHERE is_voice_submission = 1 AND client_ip_hash = ?
                  AND created_at >= ? AND created_at < ?
                LIMIT 1
                """,
                (client_hash, utc_day_start, utc_next_day_start),
            ).fetchone()
            if already_nominated:
                raise HTTPException(status_code=429, detail="Only one featured-voice nomination per network address is allowed each day")
        connection.execute(
            """
            INSERT INTO culture_posts
                (post_id, client_ip_hash, text_message, audio_content, is_visible,
                 audio_content_type, is_voice_submission, speaker_name,
                 dialect, house_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                client_hash,
                text,
                audio_content,
                int(not voice_submission),
                content_type,
                int(voice_submission),
                speaker_name.strip() if speaker_name else None,
                dialect.strip() if dialect else None,
                house_name,
                created_at,
            ),
        )

    message = (
        "Your voice is live and has been added to today's featured-voice review"
        if voice_submission
        else "Your post is live for 24 hours"
    )
    return {"status": "success", "post_id": post_id, "message": message}


@router.get("/culture/feed")
def get_culture_feed() -> dict[str, list[dict[str, Any]]]:
    initialize_culture_store()
    with _connect() as connection:
        _purge_expired(connection)
        rows = connection.execute(
            """
            SELECT post_id, text_message, audio_content_type,
                   retention_lifespan, created_at
            FROM culture_posts
            WHERE is_visible = 1
            ORDER BY created_at DESC
            LIMIT 20
            """
        ).fetchall()
    return {
        "posts": [
            {
                "post_id": row["post_id"],
                "text_message": row["text_message"],
                "audio_clip_url": (
                    f"/api/v1/culture/audio/{row['post_id']}"
                    if row["audio_content_type"]
                    else None
                ),
                "retention_lifespan": row["retention_lifespan"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    }


@router.get("/culture/audio/{post_id}")
def get_culture_audio(post_id: UUID) -> Response:
    initialize_culture_store()
    with _connect() as connection:
        _purge_expired(connection)
        row = connection.execute(
            """
            SELECT audio_content, audio_content_type FROM culture_posts
            WHERE post_id = ? AND is_visible = 1
            """,
            (str(post_id),),
        ).fetchone()
    if not row or not row["audio_content"]:
        raise HTTPException(status_code=404, detail="Voice note not found")
    return Response(
        content=row["audio_content"],
        media_type=row["audio_content_type"],
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/culture/post/{post_id}/flag")
def flag_culture_post(post_id: UUID, request: Request) -> dict[str, str]:
    initialize_culture_store()
    client_hash = _client_hash(request)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        _purge_expired(connection)
        exists = connection.execute(
            "SELECT 1 FROM culture_posts WHERE post_id = ? AND is_visible = 1",
            (str(post_id),),
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Post not found")
        connection.execute(
            """
            INSERT OR IGNORE INTO culture_reports(post_id, client_ip_hash, created_at)
            VALUES (?, ?, ?)
            """,
            (str(post_id), client_hash, created_at),
        )
    return {"status": "received", "message": "Thank you for helping keep the Hearth respectful"}


@router.get("/admin/culture/posts")
def get_culture_posts_for_moderation(
    x_admin_key: Annotated[str | None, Header()] = None,
) -> dict[str, list[dict[str, Any]]]:
    _require_admin(x_admin_key)
    initialize_culture_store()
    with _connect() as connection:
        _purge_expired(connection)
        rows = connection.execute(
            """
            SELECT p.post_id, p.text_message, p.retention_lifespan, p.created_at,
                   p.is_visible, COUNT(r.report_id) AS report_count
            FROM culture_posts AS p
            LEFT JOIN culture_reports AS r ON r.post_id = p.post_id
            GROUP BY p.post_id
            ORDER BY report_count DESC, p.created_at DESC
            LIMIT 200
            """
        ).fetchall()
    return {"posts": [dict(row) for row in rows]}


@router.patch("/admin/culture/post/{post_id}/moderate")
def moderate_culture_post(
    post_id: UUID,
    payload: CultureModerationRequest,
    x_admin_key: Annotated[str | None, Header()] = None,
) -> dict[str, str | bool]:
    _require_admin(x_admin_key)
    initialize_culture_store()
    with _connect() as connection:
        result = connection.execute(
            """
            UPDATE culture_posts
            SET is_visible = ?, is_rejected = ?,
                featured_date = CASE WHEN ? = 0 THEN NULL ELSE featured_date END
            WHERE post_id = ?
            """,
            (
                int(payload.is_visible),
                int(not payload.is_visible),
                int(payload.is_visible),
                str(post_id),
            ),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Post not found")
    return {"status": "success", "is_visible": payload.is_visible}


@router.post("/culture/extend")
async def extend_culture_post(payload: CultureExtensionRequest) -> dict[str, str]:
    initialize_culture_store()
    post_id = str(payload.post_id)
    if not _active_post_exists(post_id):
        raise HTTPException(status_code=404, detail="Post not found")

    _mpesa_settings()
    request_marker = f"initiating:{uuid4()}"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    account_reference = f"KOK{uuid4().hex[:9].upper()}"
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT status FROM culture_pending_payments
            WHERE post_id = ? AND status IN ('initiating', 'pending', 'completed')
            """,
            (post_id,),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="A preservation payment already exists for this post")
        connection.execute(
            """
            INSERT INTO culture_pending_payments
                (checkout_request_id, post_id, account_reference, amount, status, created_at)
            VALUES (?, ?, ?, 30, 'initiating', ?)
            """,
            (request_marker, post_id, account_reference, now),
        )

    try:
        response = await asyncio.to_thread(
            _initiate_stk_push,
            _phone_for_daraja(payload.phone_number),
            account_reference,
        )
    except HTTPException:
        with _connect() as connection:
            connection.execute(
                "DELETE FROM culture_pending_payments WHERE checkout_request_id = ?",
                (request_marker,),
            )
        raise

    checkout_request_id = response.get("CheckoutRequestID")
    if response.get("ResponseCode") != "0" or not isinstance(checkout_request_id, str):
        with _connect() as connection:
            connection.execute(
                "DELETE FROM culture_pending_payments WHERE checkout_request_id = ?",
                (request_marker,),
            )
        raise HTTPException(status_code=502, detail="M-Pesa could not start the preservation payment")

    with _connect() as connection:
        connection.execute(
            """
            UPDATE culture_pending_payments
            SET checkout_request_id = ?, status = 'pending'
            WHERE checkout_request_id = ?
            """,
            (checkout_request_id, request_marker),
        )
    return {
        "status": "pending_payment",
        "message": "Approve the KSh 30 M-Pesa prompt. The post is preserved after payment confirmation.",
        "checkout_request_id": checkout_request_id,
    }


@router.post("/culture/webhook/extend-lifespan")
def culture_lifespan_webhook(
    payload: dict[str, Any],
    token: Annotated[str | None, Query()] = None,
) -> dict[str, str]:
    webhook_secret = os.getenv("CULTURE_WEBHOOK_SECRET")
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="M-Pesa callbacks are not configured")
    if not token or not hmac.compare_digest(token, webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid callback credentials")

    body = payload.get("Body")
    callback = body.get("stkCallback") if isinstance(body, dict) else None
    if not isinstance(callback, dict):
        raise HTTPException(status_code=400, detail="Invalid M-Pesa callback")
    checkout_request_id = callback.get("CheckoutRequestID")
    result_code = callback.get("ResultCode")
    if not isinstance(checkout_request_id, str) or not isinstance(result_code, int):
        raise HTTPException(status_code=400, detail="Invalid M-Pesa callback")
    initialize_culture_store()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        payment = connection.execute(
            """
            SELECT post_id, amount, status FROM culture_pending_payments
            WHERE checkout_request_id = ?
            """,
            (checkout_request_id,),
        ).fetchone()
        if not payment:
            raise HTTPException(status_code=404, detail="Payment request not found")
        if payment["status"] == "completed":
            return {"status": "success", "message": "Payment was already processed"}
        if result_code != 0:
            connection.execute(
                "UPDATE culture_pending_payments SET status = 'failed' WHERE checkout_request_id = ?",
                (checkout_request_id,),
            )
            return {"status": "ignored", "message": "Payment failed or was cancelled"}

        callback_metadata = callback.get("CallbackMetadata")
        metadata_items = callback_metadata.get("Item", []) if isinstance(callback_metadata, dict) else []
        if not isinstance(metadata_items, list):
            raise HTTPException(status_code=400, detail="Invalid M-Pesa payment details")
        amount = next(
            (item.get("Value") for item in metadata_items if item.get("Name") == "Amount"),
            None,
        )
        if amount != payment["amount"]:
            raise HTTPException(status_code=400, detail="Payment amount does not match the preservation fee")
        connection.execute(
            """
            UPDATE culture_posts
            SET retention_lifespan = '7_DAYS', mpesa_checkout_id = ?
            WHERE post_id = ?
            """,
            (checkout_request_id, payment["post_id"]),
        )
        connection.execute(
            "UPDATE culture_pending_payments SET status = 'completed' WHERE checkout_request_id = ?",
            (checkout_request_id,),
        )
    return {"status": "success", "message": "Post preservation confirmed"}
