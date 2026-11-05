import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.catalog import (
    CheckoutLine,
    CheckoutRequest,
    _ticket_encryption_key,
    checkout_cart as create_ticket_order,
    get_events,
    get_passes as get_catalog_passes,
    expire_pending_ticket_orders,
    initialize_catalog_store,
    public_router as ticket_router,
    router as catalog_staff_router,
)
from app.culture import (
    initialize_culture_store,
    purge_expired_culture_posts,
    router as culture_router,
)
from app.database import check_database_connection, initialize_database
from app.intake import (
    create_feedback_submission,
    create_mentorship_request,
    initialize_intake_store,
)
from app.impact import (
    initialize_impact_store,
    public_router as impact_router,
    staff_router as staff_impact_router,
)
from app.live import (
    initialize_live_store,
    public_router as live_router,
    staff_router as staff_live_router,
)
from app.legacy_compat import router as legacy_compat_router
from app.members import initialize_member_store, router as members_router
from app.media import initialize_media_store, router as media_router
from app.publishing import (
    _activation_secret,
    initialize_publishing_store,
    record_super_admin_audit,
    router as publishing_router,
    super_admin_audit_actor,
)
from app.roadmap import (
    initialize_roadmap_store,
    public_router as roadmap_router,
    staff_router as staff_roadmap_router,
)
from app.site_content import (
    get_house_profiles,
    initialize_site_content_store,
    public_router as site_content_router,
    staff_router as staff_site_content_router,
)


async def _expire_culture_posts_periodically() -> None:
    while True:
        await asyncio.sleep(900)
        await asyncio.to_thread(purge_expired_culture_posts)


async def _expire_ticket_reservations_periodically() -> None:
    while True:
        await asyncio.sleep(60)
        await asyncio.to_thread(expire_pending_ticket_orders)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    initialize_database()
    _ticket_encryption_key()
    _activation_secret()
    initialize_culture_store()
    initialize_publishing_store()
    initialize_impact_store()
    initialize_roadmap_store()
    initialize_site_content_store()
    initialize_live_store()
    initialize_catalog_store()
    initialize_member_store()
    initialize_media_store()
    initialize_intake_store()
    cleanup_task = asyncio.create_task(_expire_culture_posts_periodically())
    ticket_expiry_task = asyncio.create_task(_expire_ticket_reservations_periodically())
    try:
        yield
    finally:
        cleanup_task.cancel()
        ticket_expiry_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        with suppress(asyncio.CancelledError):
            await ticket_expiry_task


app = FastAPI(title="Kokwet Festival API", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def audit_super_admin_mutations(request: Request, call_next):
    actor = None
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        actor = super_admin_audit_actor(request.headers.get("authorization"))
    response = await call_next(request)
    if actor and response.status_code < 400:
        record_super_admin_audit(
            actor,
            request.method,
            request.url.path,
            response.status_code,
        )
    return response


configured_origins = os.getenv("KOKWET_CORS_ORIGINS")
allowed_origins = (
    [origin.strip().rstrip("/") for origin in configured_origins.split(",") if origin.strip()]
    if configured_origins
    else [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://staff.localhost:5173",
    ]
)
development_origin_regex = (
    None
    if configured_origins
    else r"^https?://(?:(?:localhost|staff\.localhost|127\.0\.0\.1)|(?:10(?:\.\d{1,3}){3})|(?:192\.168(?:\.\d{1,3}){2})|(?:172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})):5173$"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=development_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(culture_router)
app.include_router(publishing_router)
app.include_router(impact_router)
app.include_router(staff_impact_router)
app.include_router(roadmap_router)
app.include_router(staff_roadmap_router)
app.include_router(site_content_router)
app.include_router(staff_site_content_router)
app.include_router(live_router)
app.include_router(staff_live_router)
app.include_router(legacy_compat_router)
app.include_router(catalog_staff_router)
app.include_router(ticket_router)
app.include_router(members_router)
app.include_router(media_router)

class CheckoutCreate(BaseModel):
    pass_id: str
    phone: str = Field(pattern=r"^(?:\+254|254|0)7\d{8}$")

class MentorshipCreate(BaseModel):
    phone: str = Field(pattern=r"^(?:\+254|254|0)7\d{8}$")
    focus: str = Field(min_length=8, max_length=180)

class FeedbackCreate(BaseModel):
    message: str = Field(min_length=8, max_length=500)

@app.get("/api/health")
def health():
    try:
        check_database_connection()
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    return {"status": "online", "service": "kokwet-api", "time": datetime.now().isoformat()}

@app.get("/api/activities")
def get_activities():
    return get_events()

@app.get("/api/houses")
def get_houses(tribe: str | None = None):
    profiles = get_house_profiles()
    if tribe:
        return [house for house in profiles if house["name"].lower() == tribe.lower()]
    return profiles

@app.get("/api/passes")
def get_passes():
    return get_catalog_passes()

@app.post("/api/checkout")
def checkout(payload: CheckoutCreate):
    return create_ticket_order(
        CheckoutRequest(
            items=[CheckoutLine(pass_id=payload.pass_id, quantity=1)],
            phone=payload.phone,
        )
    )

@app.post("/api/checkout/cart")
def checkout_cart(payload: CheckoutRequest):
    return create_ticket_order(payload)

@app.post("/api/mentorship", status_code=201)
def request_mentorship(payload: MentorshipCreate):
    return create_mentorship_request(payload.phone, payload.focus)

@app.post("/api/feedback", status_code=201)
def submit_feedback(payload: FeedbackCreate):
    item = create_feedback_submission(payload.message)
    return {"status": "received", "id": item["id"]}
