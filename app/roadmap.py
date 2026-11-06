import json
import sqlite3
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field, field_validator, model_validator

from app.culture import _connect
from app.publishing import _staff_from_authorization

public_router = APIRouter(prefix="/api", tags=["Roadmap"])
staff_router = APIRouter(prefix="/api/v1/staff", tags=["Roadmap management"])

RoadmapStatus = Literal["planned", "in_progress", "completed"]


def _validate_media_url(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return normalized
    if normalized.startswith("/") and not normalized.startswith("//") and ".." not in normalized:
        return normalized
    parsed = urlparse(normalized)
    if parsed.scheme == "https" and parsed.netloc:
        return normalized
    raise ValueError("Media URLs must be HTTPS or a safe same-site path")


class RoadmapPhase(BaseModel):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=3, max_length=120)
    subtitle: str = Field(min_length=3, max_length=140)
    description: str = Field(min_length=20, max_length=600)
    status: RoadmapStatus = "planned"
    milestones: list[str] = Field(min_length=1, max_length=8)
    image_url: str = Field(default="", max_length=1000)

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, value: str) -> str:
        return _validate_media_url(value)

    @model_validator(mode="after")
    def validate_milestones(self):
        if any(not item.strip() or len(item) > 180 for item in self.milestones):
            raise ValueError("Each roadmap milestone must be 1–180 characters")
        return self


class RoadmapContent(BaseModel):
    eyebrow: str = Field(min_length=2, max_length=80)
    title: str = Field(min_length=3, max_length=120)
    highlight: str = Field(min_length=2, max_length=100)
    introduction: str = Field(min_length=20, max_length=800)
    hero_image_url: str = Field(default="", max_length=1000)
    hero_cta_label: str = Field(default="Explore the journey", min_length=2, max_length=80)
    status_note: str = Field(min_length=10, max_length=300)
    journey_eyebrow: str = Field(default="A shared direction", min_length=2, max_length=80)
    journey_title_first: str = Field(default="One step,", min_length=2, max_length=80)
    journey_title_highlight: str = Field(default="together.", min_length=2, max_length=80)
    phases: list[RoadmapPhase] = Field(min_length=1, max_length=8)
    feedback_eyebrow: str = Field(default="Your voice matters", min_length=2, max_length=80)
    feedback_title: str = Field(min_length=3, max_length=100)
    feedback_description: str = Field(min_length=10, max_length=300)
    feedback_prompt: str = Field(min_length=5, max_length=150)
    feedback_success_message: str = Field(
        default="Thank you. Your voice was recorded.",
        min_length=5,
        max_length=160,
    )
    feedback_button_label: str = Field(default="Send feedback", min_length=2, max_length=80)
    feedback_placeholder: str = Field(
        default="Share an idea or priority…",
        min_length=2,
        max_length=120,
    )

    @field_validator("hero_image_url")
    @classmethod
    def validate_hero_image_url(cls, value: str) -> str:
        return _validate_media_url(value)

    @model_validator(mode="after")
    def validate_phase_ids(self):
        identifiers = [phase.id for phase in self.phases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Roadmap phase IDs must be unique")
        return self


DEFAULT_ROADMAP = RoadmapContent(
    eyebrow="The Ibinda Path",
    title="Our journey through",
    highlight="the Rift Valley",
    introduction=(
        "A community-led direction for what Kokwet can build together. "
        "These phases describe the work ahead, not achievements or funding "
        "that have already been verified."
    ),
    status_note=(
        "Phases are plans, not measured outcomes. The Kokwet team updates "
        "statuses as progress is confirmed."
    ),
    phases=[
        RoadmapPhase(
            id="the-gathering",
            title="The Gathering",
            subtitle="The foothills · Community foundations",
            description=(
                "Make it easier for people across generations and communities "
                "to meet, share ideas and shape the priorities together."
            ),
            milestones=[
                "Listen to community priorities and document what matters.",
                "Improve access to community stories and shared knowledge.",
                "Invite young people and elders into ongoing conversations.",
            ],
        ),
        RoadmapPhase(
            id="the-forge",
            title="The Forge",
            subtitle="The ascent · Co-creation and opportunity",
            description=(
                "Turn shared ideas into practical learning, mentorship and "
                "enterprise opportunities shaped with community input."
            ),
            milestones=[
                "Develop mentorship and skills-sharing opportunities.",
                "Connect local talent with relevant enterprise pathways.",
                "Invite community contributors to shape useful digital tools.",
            ],
        ),
        RoadmapPhase(
            id="the-summit",
            title="The Summit",
            subtitle="The peak · Stewardship and sustainable growth",
            description=(
                "Explore long-term community-led stewardship and sustainable "
                "ways to grow the work when resources and evidence allow."
            ),
            milestones=[
                "Explore locally useful community gathering points.",
                "Build transparent reporting around confirmed activities.",
                "Support community-led decisions about future priorities.",
            ],
        ),
    ],
    feedback_title="Help shape what comes next",
    feedback_description=(
        "Share a practical idea or priority. Feedback is an invitation to "
        "listen, not a promise that every suggestion will be funded or built."
    ),
    feedback_prompt="What should Kokwet work on next?",
)


def initialize_roadmap_store(*, seed_data: bool = True) -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS roadmap_content (
                content_id TEXT PRIMARY KEY,
                content_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT
            )
            """
        )
        if seed_data:
            connection.execute(
                """
                INSERT OR IGNORE INTO roadmap_content
                    (content_id, content_json, updated_at, updated_by)
                VALUES ('public', ?, datetime('now'), NULL)
                """,
                (DEFAULT_ROADMAP.model_dump_json(),),
            )


def _read_roadmap(connection: sqlite3.Connection) -> dict:
    row = connection.execute(
        "SELECT content_json FROM roadmap_content WHERE content_id = 'public'"
    ).fetchone()
    if not row:
        return DEFAULT_ROADMAP.model_dump(mode="json")
    return RoadmapContent.model_validate_json(row["content_json"]).model_dump(mode="json")


@public_router.get("/roadmap")
def get_roadmap() -> dict:
    initialize_roadmap_store()
    with _connect() as connection:
        return _read_roadmap(connection)


@staff_router.get("/roadmap")
def get_staff_roadmap(
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    _staff_from_authorization(authorization, ("admin",))
    initialize_roadmap_store()
    with _connect() as connection:
        return _read_roadmap(connection)


@staff_router.put("/roadmap")
def update_roadmap(
    payload: RoadmapContent,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    staff = _staff_from_authorization(authorization, ("admin",))
    initialize_roadmap_store()
    content = payload.model_dump(mode="json")
    with _connect() as connection:
        connection.execute(
            """
            UPDATE roadmap_content
            SET content_json = ?, updated_at = datetime('now'), updated_by = ?
            WHERE content_id = 'public'
            """,
            (json.dumps(content, ensure_ascii=False), str(UUID(staff["staff_id"]))),
        )
        return _read_roadmap(connection)
