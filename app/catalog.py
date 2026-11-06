import base64
import hmac
import html
import hashlib
import json
import os
import re
import smtplib
import sqlite3
import secrets
import ssl
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from secrets import token_hex, token_urlsafe
from typing import Annotated, Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from app.database import postgres_mode
from app.culture import (
    MpesaRequestError,
    _connect,
    _daraja_access_token,
    _initiate_stk_push,
    _mpesa_settings,
    _phone_for_daraja,
)
from app.publishing import _staff_from_authorization

router = APIRouter(prefix="/api/v1/staff", tags=["Event and ticket management"])
public_router = APIRouter(prefix="/api/v1/tickets", tags=["Ticket payments and gate"])
TICKET_RESERVATION_MINUTES = 10
TICKET_CALLBACK_PATH = "/api/v1/tickets/webhook/mpesa"
TICKET_QR_PREFIX = "KWT1."
TICKET_QR_AAD = b"kokwet-ticket-qr-v1"
TICKET_STORAGE_AAD = b"kokwet-ticket-storage-v1:"


def _ticket_callback_ack() -> dict[str, int | str]:
    return {"ResultCode": 0, "ResultDesc": "Success"}


class EventContent(BaseModel):
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=3, max_length=160)
    category: str = Field(min_length=2, max_length=80)
    venue: str = Field(min_length=2, max_length=160)
    date: str = Field(min_length=10, max_length=40)
    focus: str = Field(min_length=10, max_length=600)
    external_url: str | None = Field(default=None, max_length=1000)
    is_main_event: bool = False
    is_active: bool = True
    sort_order: int = Field(default=1000, ge=0, le=10000)

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Provide a valid ISO-8601 event date and time") from error
        return value

    @field_validator("external_url")
    @classmethod
    def validate_external_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Event links must use HTTPS")
        return value


class PassContent(BaseModel):
    id: str = Field(min_length=2, max_length=60, pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=2, max_length=100)
    price: int = Field(ge=0, le=10_000_000)
    stock: int = Field(ge=0, le=1_000_000)
    note: str = Field(min_length=2, max_length=300)
    is_active: bool = True
    sort_order: int = Field(default=1000, ge=0, le=10000)


class CheckoutLine(BaseModel):
    pass_id: str = Field(min_length=2, max_length=60)
    quantity: int = Field(ge=1, le=10)


class CheckoutRequest(BaseModel):
    items: list[CheckoutLine] = Field(min_length=1, max_length=10)
    phone: str = Field(pattern=r"^(?:\+254|254|0)7\d{8}$")

    @model_validator(mode="after")
    def unique_pass_lines(self):
        ids = [item.pass_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("Each pass tier can only appear once in an order")
        return self


class TicketEmailRequest(BaseModel):
    email: str = Field(min_length=6, max_length=254)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
            raise ValueError("Enter a valid email address")
        return normalized


class TicketScanRequest(BaseModel):
    ticket_id: str = Field(min_length=8, max_length=256)

    @field_validator("ticket_id")
    @classmethod
    def normalize_ticket_qr(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Scan or enter a ticket QR token")
        return normalized


def _ticket_encryption_key() -> bytes:
    configured_key = os.getenv("TICKET_ENCRYPTION_KEY")
    key_env_name = "TICKET_ENCRYPTION_KEY"
    if not configured_key:
        configured_key = os.getenv("KOKWET_TICKET_ENCRYPTION_KEY")
        key_env_name = "KOKWET_TICKET_ENCRYPTION_KEY"
    if not configured_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "TICKET_ENCRYPTION_KEY is required in the backend environment "
                "and must contain the existing ticket key."
            ),
        )
    if len(configured_key) != 64:
        raise HTTPException(
            status_code=503,
            detail=f"{key_env_name} must be 64 hexadecimal characters.",
        )
    try:
        key = bytes.fromhex(configured_key)
    except ValueError as error:
        raise HTTPException(
            status_code=503,
            detail=f"{key_env_name} must be 64 hexadecimal characters.",
        ) from error
    if len(key) != 32:
        raise HTTPException(
            status_code=503,
            detail=f"{key_env_name} must decode to exactly 32 bytes.",
        )
    return key


def _ticket_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ticket_owner_binding(phone: str, key: bytes) -> str:
    canonical_phone = _phone_for_daraja(phone)
    binding_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"kokwet-ticket-owner-binding-v1",
    ).derive(key)
    return hmac.new(
        binding_key,
        canonical_phone.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _mint_ticket_qr(ticket_id: int, phone: str, key: bytes) -> str:
    payload = json.dumps(
        {
            "v": 2,
            "id": ticket_id,
            "owner": _ticket_owner_binding(phone, key),
            "n": token_urlsafe(24),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, payload, TICKET_QR_AAD)
    encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii").rstrip("=")
    return f"{TICKET_QR_PREFIX}{encoded}"


def _decode_ticket_qr(token: str, key: bytes) -> tuple[int, str | None] | None:
    if not token.startswith(TICKET_QR_PREFIX):
        return None
    encoded = token.removeprefix(TICKET_QR_PREFIX)
    try:
        encrypted = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
        if len(encrypted) < 12 + 16:
            return None
        payload_bytes = AESGCM(key).decrypt(encrypted[:12], encrypted[12:], TICKET_QR_AAD)
        payload = json.loads(payload_bytes)
    except (InvalidTag, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or type(payload.get("v")) is not int:
        return None
    version = payload["v"]
    owner_binding = payload.get("owner")
    if (
        version not in {1, 2}
        or type(payload.get("id")) is not int
        or not isinstance(payload.get("n"), str)
        or len(payload["n"]) < 24
        or (version == 2 and (
            not isinstance(owner_binding, str)
            or not re.fullmatch(r"[a-f0-9]{64}", owner_binding)
        ))
    ):
        return None
    return payload["id"], owner_binding if version == 2 else None


def _encrypt_stored_ticket_token(ticket_id: int, token: str, key: bytes) -> str:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(
        nonce,
        token.encode("utf-8"),
        TICKET_STORAGE_AAD + str(ticket_id).encode("ascii"),
    )
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def _decrypt_stored_ticket_token(ticket_id: int, encrypted_token: str, key: bytes) -> str:
    try:
        encrypted = base64.b64decode(encrypted_token, altchars=b"-_", validate=True)
        if len(encrypted) < 12 + 16:
            raise ValueError("Stored ticket token is truncated")
        plaintext = AESGCM(key).decrypt(
            encrypted[:12],
            encrypted[12:],
            TICKET_STORAGE_AAD + str(ticket_id).encode("ascii"),
        )
        return plaintext.decode("utf-8")
    except (InvalidTag, ValueError, UnicodeDecodeError) as error:
        raise HTTPException(
            status_code=503,
            detail="A ticket credential could not be decrypted; check the persistent ticket key.",
        ) from error


DEFAULT_EVENTS = [
    {
        "slug": "pokotfest",
        "title": "PokotFest 2nd Edition – Nyinyoö Nyete",
        "category": "Festival",
        "venue": "See official site for venue details",
        "date": "2026-11-07T12:00:00+03:00",
        "focus": "Join the second edition of PokotFest for a celebration of Pokot culture, music and community.",
        "external_url": "https://pokotfest.com/",
        "is_main_event": False,
        "is_active": True,
        "sort_order": 10,
    },
    {
        "slug": "kokwet-festival-2026",
        "title": "Kokwet Festival 2026",
        "category": "Festival",
        "venue": "Thika",
        "date": "2026-11-20T12:00:00+03:00",
        "focus": "Come together for the main Kokwet Festival: culture, community and a future built together.",
        "external_url": None,
        "is_main_event": True,
        "is_active": True,
        "sort_order": 20,
    },
    {
        "slug": "sustainable-escarpment-forum",
        "title": "Sustainable Escarpment Agribusiness Forum",
        "category": "Enterprise Expansion",
        "venue": "Nakuru Agriculture Complex",
        "date": "2026-12-05T09:00:00+03:00",
        "focus": "Explore cooperative dairy and tea enterprise opportunities.",
        "external_url": None,
        "is_main_event": False,
        "is_active": True,
        "sort_order": 30,
    },
    {
        "slug": "nine-houses-assembly",
        "title": "The Nine Houses Cultural Assembly",
        "category": "Festival",
        "venue": "Kokwet Festival Grounds",
        "date": "2027-01-23T10:00:00+03:00",
        "focus": "A shared celebration of history, art, sport and living tradition.",
        "external_url": None,
        "is_main_event": False,
        "is_active": True,
        "sort_order": 40,
    },
]

DEFAULT_PASSES = [
    {"id": "regular", "name": "Festival Regular", "price": 1200, "stock": 420, "note": "Main grounds & cultural sessions", "is_active": True, "sort_order": 10},
    {"id": "family", "name": "Family Assembly", "price": 3500, "stock": 96, "note": "Four entries & children’s programme", "is_active": True, "sort_order": 20},
    {"id": "premium", "name": "Future Circle", "price": 6500, "stock": 42, "note": "Fast entry, lounge & mentor salon", "is_active": True, "sort_order": 30},
]


def initialize_catalog_store(*, seed_data: bool = True) -> None:
    with _connect() as connection:
        if postgres_mode():
            connection.execute("BEGIN IMMEDIATE")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS festival_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                venue TEXT NOT NULL,
                event_date TEXT NOT NULL,
                focus TEXT NOT NULL,
                external_url TEXT,
                is_main_event INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 1000
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_festival_main_event
                ON festival_events(is_main_event) WHERE is_main_event = 1;
            CREATE INDEX IF NOT EXISTS idx_festival_events_order
                ON festival_events(is_active, sort_order, event_date);
            CREATE TABLE IF NOT EXISTS festival_passes (
                pass_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                price INTEGER NOT NULL CHECK (price >= 0),
                stock INTEGER NOT NULL CHECK (stock >= 0),
                note TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 1000
            );
            CREATE INDEX IF NOT EXISTS idx_festival_pass_order
                ON festival_passes(is_active, sort_order);
            CREATE TABLE IF NOT EXISTS festival_orders (
                order_id TEXT PRIMARY KEY,
                phone TEXT NOT NULL,
                total INTEGER NOT NULL,
                status TEXT NOT NULL,
                simulated INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                account_reference TEXT,
                checkout_request_id TEXT UNIQUE,
                reservation_expires_at TEXT,
                inventory_released INTEGER NOT NULL DEFAULT 0,
                mpesa_receipt_number TEXT,
                mpesa_transaction_date TEXT,
                mpesa_phone TEXT
            );
            CREATE TABLE IF NOT EXISTS festival_tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL REFERENCES festival_orders(order_id) ON DELETE CASCADE,
                pass_id TEXT NOT NULL,
                pass_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                token_hash TEXT,
                encrypted_token TEXT,
                status TEXT NOT NULL,
                simulated INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                used_at TEXT,
                scanned_by TEXT
            );
            """
        )
        order_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(festival_orders)").fetchall()
        }
        for column, definition in (
            ("account_reference", "TEXT"),
            ("checkout_request_id", "TEXT"),
            ("reservation_expires_at", "TEXT"),
            ("inventory_released", "INTEGER NOT NULL DEFAULT 0"),
            ("receipt_email", "TEXT"),
            ("receipt_email_sent_at", "TEXT"),
            ("mpesa_receipt_number", "TEXT"),
            ("mpesa_transaction_date", "TEXT"),
            ("mpesa_phone", "TEXT"),
        ):
            if column not in order_columns:
                connection.execute(f"ALTER TABLE festival_orders ADD COLUMN {column} {definition}")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_festival_orders_checkout_request "
            "ON festival_orders(checkout_request_id) WHERE checkout_request_id IS NOT NULL"
        )
        ticket_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(festival_tickets)").fetchall()
        }
        for column, definition in (
            ("used_at", "TEXT"),
            ("token_hash", "TEXT"),
            ("encrypted_token", "TEXT"),
            ("scanned_by", "TEXT"),
        ):
            if column not in ticket_columns:
                connection.execute(f"ALTER TABLE festival_tickets ADD COLUMN {column} {definition}")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_festival_tickets_token_hash "
            "ON festival_tickets(token_hash) WHERE token_hash IS NOT NULL"
        )
        if not seed_data:
            return

        key = _ticket_encryption_key()
        legacy_tickets = connection.execute(
            """
            SELECT ticket_id, token FROM festival_tickets
            WHERE token_hash IS NULL OR encrypted_token IS NULL
            """
        ).fetchall()
        for ticket in legacy_tickets:
            token = ticket["token"]
            token_hash = _ticket_token_hash(token)
            encrypted_token = _encrypt_stored_ticket_token(ticket["ticket_id"], token, key)
            connection.execute(
                """
                UPDATE festival_tickets
                SET token = ?, token_hash = ?, encrypted_token = ?
                WHERE ticket_id = ?
                """,
                (token_hex(32), token_hash, encrypted_token, ticket["ticket_id"]),
            )
        for event in DEFAULT_EVENTS:
            connection.execute(
                """
                INSERT OR IGNORE INTO festival_events
                    (slug, title, category, venue, event_date, focus, external_url,
                     is_main_event, is_active, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["slug"], event["title"], event["category"], event["venue"],
                    event["date"], event["focus"], event["external_url"],
                    int(event["is_main_event"]), int(event["is_active"]), event["sort_order"],
                ),
            )
        for item in DEFAULT_PASSES:
            connection.execute(
                """
                INSERT OR IGNORE INTO festival_passes
                    (pass_id, name, price, stock, note, is_active, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["id"], item["name"], item["price"], item["stock"], item["note"],
                    int(item["is_active"]), item["sort_order"],
                ),
            )


def _event_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["event_id"],
        "slug": row["slug"],
        "title": row["title"],
        "category": row["category"],
        "venue": row["venue"],
        "date": row["event_date"],
        "focus": row["focus"],
        "external_url": row["external_url"],
        "is_main_event": bool(row["is_main_event"]),
        "is_active": bool(row["is_active"]),
        "sort_order": row["sort_order"],
    }


def _pass_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["pass_id"],
        "name": row["name"],
        "price": row["price"],
        "stock": row["stock"],
        "note": row["note"],
        "is_active": bool(row["is_active"]),
        "sort_order": row["sort_order"],
    }


def get_events(*, include_inactive: bool = False) -> list[dict[str, Any]]:
    where = "" if include_inactive else "WHERE is_active = 1"
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT event_id, slug, title, category, venue, event_date, focus,
                   external_url, is_main_event, is_active, sort_order
            FROM festival_events {where}
            ORDER BY sort_order ASC, event_date ASC
            """
        ).fetchall()
    return [_event_payload(row) for row in rows]


def _release_reserved_inventory(connection: sqlite3.Connection, order_id: str) -> None:
    reservations = connection.execute(
        """
        SELECT pass_id, COUNT(*) AS quantity
        FROM festival_tickets
        WHERE order_id = ?
        GROUP BY pass_id
        """,
        (order_id,),
    ).fetchall()
    for reservation in reservations:
        connection.execute(
            "UPDATE festival_passes SET stock = stock + ? WHERE pass_id = ?",
            (reservation["quantity"], reservation["pass_id"]),
        )


def expire_pending_ticket_orders() -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        orders = connection.execute(
            """
            SELECT order_id FROM festival_orders
            WHERE status IN ('Payment pending', 'Payment review')
              AND reservation_expires_at IS NOT NULL
              AND reservation_expires_at <= ?
              AND inventory_released = 0
            """,
            (now,),
        ).fetchall()
        for order in orders:
            order_id = order["order_id"]
            _release_reserved_inventory(connection, order_id)
            connection.execute(
                "UPDATE festival_orders SET status = 'Expired', inventory_released = 1 WHERE order_id = ?",
                (order_id,),
            )
            connection.execute(
                "UPDATE festival_tickets SET status = 'Expired' WHERE order_id = ?",
                (order_id,),
            )


def get_passes(*, include_inactive: bool = False) -> list[dict[str, Any]]:
    expire_pending_ticket_orders()
    where = "" if include_inactive else "WHERE is_active = 1"
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT pass_id, name, price, stock, note, is_active, sort_order
            FROM festival_passes {where} ORDER BY sort_order ASC, pass_id ASC
            """
        ).fetchall()
    return [_pass_payload(row) for row in rows]


def _require_admin(authorization: str | None) -> sqlite3.Row:
    return _staff_from_authorization(authorization, ("admin",))


@router.get("/catalog")
def get_staff_catalog(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, list[dict[str, Any]]]:
    _require_admin(authorization)
    return {"events": get_events(include_inactive=True), "passes": get_passes(include_inactive=True)}


@router.post("/daraja/sandbox-check")
def check_daraja_sandbox(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    _require_admin(authorization)
    environment, consumer_key, consumer_secret, _, _ = _mpesa_settings()
    if environment != "sandbox":
        raise HTTPException(
            status_code=409,
            detail="Set MPESA_ENV=sandbox in Render before testing the Daraja sandbox.",
        )
    _daraja_access_token(environment, consumer_key, consumer_secret)
    return {
        "environment": "sandbox",
        "status": "connected",
        "message": "Daraja sandbox OAuth succeeded. No payment request was sent.",
    }


@router.post("/events", status_code=201)
def create_event(
    payload: EventContent,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_admin(authorization)
    initialize_catalog_store()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if payload.is_main_event:
            connection.execute("UPDATE festival_events SET is_main_event = 0")
        try:
            connection.execute(
                """
                INSERT INTO festival_events
                    (slug, title, category, venue, event_date, focus, external_url,
                     is_main_event, is_active, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.slug, payload.title.strip(), payload.category.strip(),
                    payload.venue.strip(), payload.date, payload.focus.strip(),
                    payload.external_url, int(payload.is_main_event),
                    int(payload.is_active), payload.sort_order,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="An event already uses that slug") from error
        row = connection.execute(
            """
            SELECT event_id, slug, title, category, venue, event_date, focus,
                   external_url, is_main_event, is_active, sort_order
            FROM festival_events WHERE slug = ?
            """,
            (payload.slug,),
        ).fetchone()
    return {"event": _event_payload(row)}


@router.put("/events/{slug}")
def update_event(
    slug: str,
    payload: EventContent,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_admin(authorization)
    if slug != payload.slug:
        raise HTTPException(status_code=422, detail="The event slug cannot be changed here")
    initialize_catalog_store()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        exists = connection.execute(
            "SELECT 1 FROM festival_events WHERE slug = ?", (slug,)
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Festival event not found")
        if payload.is_main_event:
            connection.execute("UPDATE festival_events SET is_main_event = 0")
        connection.execute(
            """
            UPDATE festival_events SET title = ?, category = ?, venue = ?,
                event_date = ?, focus = ?, external_url = ?, is_main_event = ?,
                is_active = ?, sort_order = ?
            WHERE slug = ?
            """,
            (
                payload.title.strip(), payload.category.strip(), payload.venue.strip(),
                payload.date, payload.focus.strip(), payload.external_url,
                int(payload.is_main_event), int(payload.is_active), payload.sort_order, slug,
            ),
        )
        row = connection.execute(
            """
            SELECT event_id, slug, title, category, venue, event_date, focus,
                   external_url, is_main_event, is_active, sort_order
            FROM festival_events WHERE slug = ?
            """,
            (slug,),
        ).fetchone()
    return {"event": _event_payload(row)}


@router.post("/passes", status_code=201)
def create_pass(
    payload: PassContent,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_admin(authorization)
    initialize_catalog_store()
    with _connect() as connection:
        try:
            connection.execute(
                """
                INSERT INTO festival_passes
                    (pass_id, name, price, stock, note, is_active, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.id, payload.name.strip(), payload.price, payload.stock,
                    payload.note.strip(), int(payload.is_active), payload.sort_order,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(status_code=409, detail="A pass already uses that ID") from error
        row = connection.execute(
            """
            SELECT pass_id, name, price, stock, note, is_active, sort_order
            FROM festival_passes WHERE pass_id = ?
            """,
            (payload.id,),
        ).fetchone()
    return {"pass": _pass_payload(row)}


@router.put("/passes/{pass_id}")
def update_pass(
    pass_id: str,
    payload: PassContent,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _require_admin(authorization)
    if pass_id != payload.id:
        raise HTTPException(status_code=422, detail="The pass ID cannot be changed here")
    initialize_catalog_store()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        result = connection.execute(
            """
            UPDATE festival_passes
            SET name = ?, price = ?, stock = ?, note = ?, is_active = ?, sort_order = ?
            WHERE pass_id = ?
            """,
            (
                payload.name.strip(), payload.price, payload.stock, payload.note.strip(),
                int(payload.is_active), payload.sort_order, pass_id,
            ),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Festival pass not found")
        row = connection.execute(
            """
            SELECT pass_id, name, price, stock, note, is_active, sort_order
            FROM festival_passes WHERE pass_id = ?
            """,
            (pass_id,),
        ).fetchone()
    return {"pass": _pass_payload(row)}


@router.get("/ticket-orders/review")
def list_ticket_payment_reviews(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, list[dict[str, Any]]]:
    _require_admin(authorization)
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT order_id, phone, total, status, created_at
            FROM festival_orders
            WHERE status = 'Payment review'
            ORDER BY created_at ASC
            LIMIT 200
            """
        ).fetchall()
    return {"orders": [dict(row) for row in rows]}


def checkout_cart(payload: CheckoutRequest) -> dict[str, Any]:
    _mpesa_settings()
    expire_pending_ticket_orders()
    initialize_catalog_store()
    order_id = token_urlsafe(32)
    account_reference = f"KT{token_hex(5).upper()}"
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reservation_expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=TICKET_RESERVATION_MINUTES)
    ).isoformat(timespec="seconds")
    total = 0
    ticket_key = _ticket_encryption_key()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        selected: list[tuple[sqlite3.Row, int]] = []
        for item in payload.items:
            row = connection.execute(
                """
                SELECT pass_id, name, price, stock, note, is_active, sort_order
                FROM festival_passes WHERE pass_id = ?
                """,
                (item.pass_id,),
            ).fetchone()
            if not row or not row["is_active"]:
                raise HTTPException(status_code=404, detail=f"Pass not found: {item.pass_id}")
            if item.quantity > row["stock"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"Only {row['stock']} {row['name']} passes remain",
                )
            selected.append((row, item.quantity))
        total = sum(row["price"] * quantity for row, quantity in selected)
        if total <= 0:
            raise HTTPException(status_code=422, detail="A ticket order must have a positive M-Pesa total")
        connection.execute(
            """
            INSERT INTO festival_orders
                (order_id, phone, total, status, simulated, created_at, account_reference,
                 reservation_expires_at)
            VALUES (?, ?, ?, 'Payment pending', 0, ?, ?, ?)
            """,
            (order_id, payload.phone, total, created_at, account_reference, reservation_expires_at),
        )
        for row, quantity in selected:
            connection.execute(
                "UPDATE festival_passes SET stock = stock - ? WHERE pass_id = ?",
                (quantity, row["pass_id"]),
            )
            for _ in range(quantity):
                cursor = connection.execute(
                    """
                    INSERT INTO festival_tickets
                        (order_id, pass_id, pass_name, phone, token, status, simulated, created_at)
                    VALUES (?, ?, ?, ?, ?, 'Payment pending', 0, ?)
                    """,
                    (
                        order_id,
                        row["pass_id"],
                        row["name"],
                        payload.phone,
                        token_hex(32),
                        created_at,
                    ),
                )
                ticket_id = cursor.lastrowid
                if ticket_id is None:
                    raise RuntimeError("The ticket database did not return its new ticket ID")
                token = _mint_ticket_qr(ticket_id, payload.phone, ticket_key)
                connection.execute(
                    """
                    UPDATE festival_tickets
                    SET token_hash = ?, encrypted_token = ?
                    WHERE ticket_id = ?
                    """,
                    (
                        _ticket_token_hash(token),
                        _encrypt_stored_ticket_token(ticket_id, token, ticket_key),
                        ticket_id,
                    ),
                )

    try:
        response = _initiate_stk_push(
            _phone_for_daraja(payload.phone),
            account_reference,
            amount=total,
            description="Kokwet Festival ticket",
            callback_path=TICKET_CALLBACK_PATH,
        )
    except MpesaRequestError as error:
        with _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if error.payment_may_have_started:
                connection.execute(
                    """
                    UPDATE festival_orders SET status = 'Payment review'
                    WHERE order_id = ? AND status = 'Payment pending'
                    """,
                    (order_id,),
                )
                connection.execute(
                    "UPDATE festival_tickets SET status = 'Payment review' WHERE order_id = ?",
                    (order_id,),
                )
                detail = (
                    f"{error.detail}. Do not retry until payment status is confirmed; "
                    f"contact support with order reference {order_id}."
                )
            else:
                _release_reserved_inventory(connection, order_id)
                connection.execute(
                    """
                    UPDATE festival_orders
                    SET status = 'Payment failed', inventory_released = 1
                    WHERE order_id = ? AND status = 'Payment pending'
                    """,
                    (order_id,),
                )
                connection.execute(
                    "UPDATE festival_tickets SET status = 'Payment failed' WHERE order_id = ?",
                    (order_id,),
                )
                detail = (
                    f"{error.detail}. No payment prompt was accepted and the "
                    "reservation has been released."
                )
        raise HTTPException(status_code=502, detail=detail) from error

    checkout_request_id = response.get("CheckoutRequestID")
    if response.get("ResponseCode") != "0" or not isinstance(checkout_request_id, str):
        with _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _release_reserved_inventory(connection, order_id)
            connection.execute(
                "UPDATE festival_orders SET status = 'Payment failed', inventory_released = 1 WHERE order_id = ?",
                (order_id,),
            )
            connection.execute(
                "UPDATE festival_tickets SET status = 'Payment failed' WHERE order_id = ?",
                (order_id,),
            )
        raise HTTPException(status_code=502, detail="M-Pesa could not start the ticket payment")

    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE festival_orders
            SET checkout_request_id = ?
            WHERE order_id = ? AND status = 'Payment pending'
            """,
            (checkout_request_id, order_id),
        )
    return {
        "order_id": order_id,
        "total": total,
        "status": "Payment pending",
        "simulated": False,
        "reservation_expires_at": reservation_expires_at,
        "message": "Approve the M-Pesa prompt. Tickets appear here after payment confirmation.",
        "tickets": [],
    }


def _ticket_order_payload(connection: sqlite3.Connection, order_id: str) -> dict[str, Any]:
    order = connection.execute(
        """
        SELECT order_id, total, status, reservation_expires_at, receipt_email_sent_at
        FROM festival_orders WHERE order_id = ?
        """,
        (order_id,),
    ).fetchone()
    if not order:
        raise HTTPException(status_code=404, detail="Ticket order not found")
    tickets = connection.execute(
        """
        SELECT ticket_id, pass_name, token_hash, encrypted_token, status
        FROM festival_tickets WHERE order_id = ? ORDER BY ticket_id
        """,
        (order_id,),
    ).fetchall()
    ticket_payloads = []
    if order["status"] == "Paid":
        key = _ticket_encryption_key()
        for row in tickets:
            if not row["token_hash"] or not row["encrypted_token"]:
                raise HTTPException(
                    status_code=503,
                    detail="A paid ticket credential is incomplete; contact support.",
                )
            token = _decrypt_stored_ticket_token(
                row["ticket_id"], row["encrypted_token"], key
            )
            if not secrets.compare_digest(_ticket_token_hash(token), row["token_hash"]):
                raise HTTPException(
                    status_code=503,
                    detail="A paid ticket credential failed its integrity check; contact support.",
                )
            ticket_payloads.append(
                {
                    "id": row["ticket_id"],
                    "ticket_id": token,
                    "pass": row["pass_name"],
                    "token": token,
                    "status": row["status"],
                }
            )
    return {
        "order_id": order["order_id"],
        "total": order["total"],
        "status": order["status"],
        "reservation_expires_at": order["reservation_expires_at"],
        "email_sent": bool(order["receipt_email_sent_at"]),
        "tickets": ticket_payloads,
    }


def _ticket_access_url(order_id: str) -> str:
    site_url = os.getenv("KOKWET_PUBLIC_SITE_URL", "").strip()
    parsed = urlsplit(site_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=503,
            detail="Ticket email delivery is not configured. Your tickets remain available through the checkout page.",
        )
    return f"{site_url.rstrip('/')}/tickets/{urllib.parse.quote(order_id, safe='')}"


def _send_ticket_email(email: str, order_id: str, ticket_url: str) -> None:
    host = os.getenv("KOKWET_SMTP_HOST", "").strip()
    username = os.getenv("KOKWET_SMTP_USERNAME", "").strip()
    password = os.getenv("KOKWET_SMTP_PASSWORD", "")
    sender = os.getenv("KOKWET_SMTP_FROM_EMAIL", "").strip()
    raw_port = os.getenv("KOKWET_SMTP_PORT", "587").strip()
    try:
        port = int(raw_port)
    except ValueError as error:
        raise HTTPException(status_code=503, detail="Ticket email delivery is not configured.") from error
    if not host or not username or not password or not sender or not 1 <= port <= 65535:
        raise HTTPException(status_code=503, detail="Ticket email delivery is not configured.")

    message = EmailMessage()
    message["Subject"] = "Your Kokwet Festival tickets"
    message["From"] = sender
    message["To"] = email
    message["X-Kokwet-Order-Reference"] = order_id
    message.set_content(
        "Your Kokwet Festival payment is confirmed.\n\n"
        f"Open your tickets and entry QR codes here:\n{ticket_url}\n\n"
        "Keep this private link secure. Anyone with the link can view the tickets."
    )
    message.add_alternative(
        "<p>Your Kokwet Festival payment is confirmed.</p>"
        f'<p><a href="{html.escape(ticket_url, quote=True)}">Open your tickets and entry QR codes</a></p>'
        "<p>Keep this private link secure. Anyone with the link can view the tickets.</p>",
        subtype="html",
    )
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP(host, port, timeout=15) as client:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
            client.login(username, password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise HTTPException(
            status_code=502,
            detail="Your tickets are available on this page, but the email could not be sent. Please try again.",
        ) from error


@public_router.get("/orders/{order_id}")
def get_ticket_order(order_id: str) -> dict[str, Any]:
    expire_pending_ticket_orders()
    with _connect() as connection:
        return _ticket_order_payload(connection, order_id)


@public_router.post("/orders/{order_id}/email")
def email_ticket_order(
    order_id: str,
    payload: TicketEmailRequest,
) -> dict[str, str]:
    with _connect() as connection:
        order = connection.execute(
            "SELECT status FROM festival_orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    if not order:
        raise HTTPException(status_code=404, detail="Ticket order not found")
    if order["status"] != "Paid":
        raise HTTPException(
            status_code=409,
            detail="Ticket email is available after the payment has been confirmed.",
        )

    ticket_url = _ticket_access_url(order_id)
    _send_ticket_email(payload.email, order_id, ticket_url)
    sent_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as connection:
        connection.execute(
            """
            UPDATE festival_orders
            SET receipt_email = ?, receipt_email_sent_at = ?
            WHERE order_id = ? AND status = 'Paid'
            """,
            (payload.email, sent_at, order_id),
        )
    return {"status": "sent", "email": payload.email, "ticket_url": ticket_url}


@public_router.post("/webhook/mpesa")
def ticket_payment_webhook(
    payload: dict[str, Any],
    token: Annotated[str | None, Query()] = None,
) -> dict[str, int | str]:
    webhook_secret = os.getenv("CULTURE_WEBHOOK_SECRET")
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="M-Pesa callbacks are not configured")
    if not token or not secrets.compare_digest(token, webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid callback credentials")

    body = payload.get("Body")
    callback = body.get("stkCallback") if isinstance(body, dict) else None
    if not isinstance(callback, dict):
        raise HTTPException(status_code=400, detail="Invalid M-Pesa callback")
    checkout_request_id = callback.get("CheckoutRequestID")
    result_code = callback.get("ResultCode")
    if not isinstance(checkout_request_id, str) or type(result_code) is not int:
        raise HTTPException(status_code=400, detail="Invalid M-Pesa callback")
    expire_pending_ticket_orders()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute(
            """
            SELECT order_id, total, status, phone, reservation_expires_at, inventory_released
            FROM festival_orders WHERE checkout_request_id = ?
            """,
            (checkout_request_id,),
        ).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Ticket payment request not found")
        if order["status"] == "Paid":
            return _ticket_callback_ack()
        if order["status"] in {"Payment failed", "Payment cancelled"}:
            return _ticket_callback_ack()
        if result_code != 0:
            payment_status = (
                "Payment cancelled" if result_code == 1032 else "Payment failed"
            )
            if not order["inventory_released"]:
                _release_reserved_inventory(connection, order["order_id"])
            connection.execute(
                "UPDATE festival_orders SET status = ?, inventory_released = 1 WHERE order_id = ?",
                (payment_status, order["order_id"]),
            )
            connection.execute(
                "UPDATE festival_tickets SET status = ? WHERE order_id = ?",
                (payment_status, order["order_id"]),
            )
            return _ticket_callback_ack()

        metadata = callback.get("CallbackMetadata")
        items = metadata.get("Item", []) if isinstance(metadata, dict) else []
        if not isinstance(items, list):
            raise HTTPException(status_code=400, detail="Invalid M-Pesa payment details")
        metadata_items = {
            item.get("Name"): item.get("Value")
            for item in items
            if isinstance(item, dict) and isinstance(item.get("Name"), str)
        }
        amount = metadata_items.get("Amount")
        if type(amount) is not int or amount != order["total"]:
            connection.execute(
                "UPDATE festival_orders SET status = 'Payment review' WHERE order_id = ?",
                (order["order_id"],),
            )
            connection.execute(
                "UPDATE festival_tickets SET status = 'Payment review' WHERE order_id = ?",
                (order["order_id"],),
            )
            return _ticket_callback_ack()

        if order["inventory_released"]:
            reservations = connection.execute(
                """
                SELECT pass_id, COUNT(*) AS quantity
                FROM festival_tickets WHERE order_id = ? GROUP BY pass_id
                """,
                (order["order_id"],),
            ).fetchall()
            can_reallocate = True
            for reservation in reservations:
                available = connection.execute(
                    "SELECT stock FROM festival_passes WHERE pass_id = ?",
                    (reservation["pass_id"],),
                ).fetchone()
                if not available or available["stock"] < reservation["quantity"]:
                    can_reallocate = False
                    break
            if not can_reallocate:
                connection.execute(
                    "UPDATE festival_orders SET status = 'Payment review' WHERE order_id = ?",
                    (order["order_id"],),
                )
                connection.execute(
                    "UPDATE festival_tickets SET status = 'Payment review' WHERE order_id = ?",
                    (order["order_id"],),
                )
                return _ticket_callback_ack()
            for reservation in reservations:
                connection.execute(
                    "UPDATE festival_passes SET stock = stock - ? WHERE pass_id = ?",
                    (reservation["quantity"], reservation["pass_id"]),
                )

        connection.execute(
            """
            UPDATE festival_orders
            SET status = 'Paid', simulated = 0, reservation_expires_at = NULL,
                inventory_released = 0, mpesa_receipt_number = ?,
                mpesa_transaction_date = ?, mpesa_phone = ?
            WHERE order_id = ?
            """,
            (
                str(metadata_items["MpesaReceiptNumber"])
                if metadata_items.get("MpesaReceiptNumber") is not None
                else None,
                str(metadata_items["TransactionDate"])
                if metadata_items.get("TransactionDate") is not None
                else None,
                str(metadata_items["PhoneNumber"])
                if metadata_items.get("PhoneNumber") is not None
                else None,
                order["order_id"],
            ),
        )
        connection.execute(
            "UPDATE festival_tickets SET status = 'Paid', simulated = 0 WHERE order_id = ?",
            (order["order_id"],),
        )
    return _ticket_callback_ack()


@public_router.post("/validate")
def validate_ticket(
    payload: TicketScanRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    staff = _staff_from_authorization(authorization)
    if staff["role"] != "admin" and not staff["can_scan_tickets"]:
        raise HTTPException(status_code=403, detail="Your account is not assigned to scan tickets")
    expire_pending_ticket_orders()
    token_hash = _ticket_token_hash(payload.ticket_id)
    ticket_key = _ticket_encryption_key()
    decoded_ticket = _decode_ticket_qr(payload.ticket_id, ticket_key)
    if payload.ticket_id.startswith(TICKET_QR_PREFIX) and decoded_ticket is None:
        return {"status": "invalid", "message": "Ticket QR failed its cryptographic integrity check"}
    encrypted_ticket_id, qr_owner_binding = decoded_ticket or (None, None)
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if encrypted_ticket_id is None:
            ticket = connection.execute(
                """
                SELECT ticket_id, pass_name, status, used_at, phone
                FROM festival_tickets WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
        else:
            ticket = connection.execute(
                """
                SELECT ticket_id, pass_name, status, used_at, phone
                FROM festival_tickets
                WHERE ticket_id = ? AND token_hash = ?
                """,
                (encrypted_ticket_id, token_hash),
            ).fetchone()
        if not ticket:
            return {"status": "invalid", "message": "Ticket not recognized"}
        if qr_owner_binding is not None and not hmac.compare_digest(
            qr_owner_binding,
            _ticket_owner_binding(ticket["phone"], ticket_key),
        ):
            return {
                "status": "invalid",
                "message": "Ticket owner verification failed; ask the attendee to contact support.",
            }
        scanned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        burned = connection.execute(
            """
            UPDATE festival_tickets
            SET status = 'Used', used_at = ?, scanned_by = ?
            WHERE ticket_id = ? AND token_hash = ? AND status = 'Paid'
            RETURNING pass_name
            """,
            (scanned_at, staff["staff_id"], ticket["ticket_id"], token_hash),
        ).fetchone()
        if burned:
            return {
                "status": "valid",
                "message": "Access granted",
                "pass": burned["pass_name"],
                "owner_phone_last4": _phone_for_daraja(ticket["phone"])[-4:],
                "scanned_at": scanned_at,
            }
        latest = connection.execute(
            "SELECT status, used_at FROM festival_tickets WHERE ticket_id = ?",
            (ticket["ticket_id"],),
        ).fetchone()
        if latest and latest["status"] == "Used":
            return {
                "status": "duplicate",
                "message": "Ticket has already been scanned",
                "scanned_at": latest["used_at"] or "",
            }
        if latest and latest["status"] == "Payment pending":
            return {"status": "unpaid", "message": "Ticket is not valid for entry"}
        return {
            "status": "invalid",
            "message": "Ticket is not valid for entry",
        }
