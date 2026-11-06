import json
import sqlite3
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field, field_validator, model_validator

from app.culture import _connect
from app.publishing import _staff_from_authorization

public_router = APIRouter(prefix="/api", tags=["Impact"])
staff_router = APIRouter(prefix="/api/v1/staff", tags=["Impact management"])

ImpactDestination = Literal[
    "open_hearth",
    "community_services",
    "festival_marketplace",
    "none",
]
ReportMode = Literal["unpublished", "proposed", "verified"]
GoalStatus = Literal["planned", "in_progress", "completed"]


class ImpactHero(BaseModel):
    eyebrow: str = Field(min_length=2, max_length=80)
    title: str = Field(min_length=3, max_length=120)
    highlight: str = Field(min_length=2, max_length=100)
    body: str = Field(min_length=20, max_length=800)
    image_url: str = Field(default="", max_length=1000)

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, value: str) -> str:
        return _validate_media_url(value)


class ImpactPathway(BaseModel):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=2, max_length=100)
    description: str = Field(min_length=10, max_length=350)
    actions: list[str] = Field(min_length=1, max_length=5)
    cta_label: str = Field(min_length=2, max_length=60)
    destination: ImpactDestination
    image_url: str = Field(default="", max_length=1000)

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, value: str) -> str:
        return _validate_media_url(value)

    @model_validator(mode="after")
    def validate_actions(self):
        if any(not action.strip() or len(action) > 160 for action in self.actions):
            raise ValueError("Each contribution action must be 1–160 characters")
        return self


class ResourceAllocation(BaseModel):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9-]+$")
    label: str = Field(min_length=2, max_length=100)
    percentage: int = Field(ge=0, le=100)


class ImpactTransparency(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    body: str = Field(min_length=20, max_length=800)
    report_mode: ReportMode = "unpublished"
    report_period: str = Field(default="", max_length=100)
    report_source: str = Field(default="", max_length=250)
    report_url: str = Field(default="", max_length=1000)
    allocations: list[ResourceAllocation] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_report(self):
        if self.report_url:
            parsed_url = urlparse(self.report_url)
            if parsed_url.scheme not in {"https", "http"} or not parsed_url.netloc:
                raise ValueError("Report links must use http or https")
        if self.report_mode == "verified":
            if not self.report_period.strip() or not self.report_source.strip():
                raise ValueError("A verified report needs a reporting period and source")
            if not self.allocations or sum(item.percentage for item in self.allocations) != 100:
                raise ValueError("Verified report allocations must add up to 100%")
        elif self.allocations and sum(item.percentage for item in self.allocations) != 100:
            raise ValueError("When provided, allocation percentages must add up to 100%")
        return self


class ImpactGoal(BaseModel):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=4, max_length=180)
    description: str = Field(min_length=10, max_length=350)
    status: GoalStatus = "planned"


class ImpactContent(BaseModel):
    hero: ImpactHero
    pathways: list[ImpactPathway] = Field(min_length=1, max_length=8)
    pathways_eyebrow: str = Field(default="A shared effort", min_length=2, max_length=80)
    pathways_title_first: str = Field(default="Plug into the", min_length=2, max_length=100)
    pathways_title_highlight: str = Field(default="ecosystem", min_length=2, max_length=100)
    pathways_copy: str = Field(
        default="Choose a practical way to contribute. These are invitations to take part—not claims about results already achieved.",
        min_length=10,
        max_length=500,
    )
    transparency: ImpactTransparency
    accountability_label: str = Field(default="Accountability", min_length=2, max_length=80)
    unpublished_report_title: str = Field(default="No verified figures published", min_length=3, max_length=120)
    unpublished_report_body: str = Field(
        default="We will show a breakdown here when a dated report and its source are ready.",
        min_length=10,
        max_length=300,
    )
    report_link_label: str = Field(default="Open supporting report", min_length=2, max_length=100)
    resource_reporting_label: str = Field(default="Resource reporting", min_length=2, max_length=100)
    resource_empty_title: str = Field(default="Trust is built with evidence.", min_length=3, max_length=120)
    resource_empty_body: str = Field(
        default="There are no allocation percentages to display until a proposal or sourced report is published.",
        min_length=10,
        max_length=300,
    )
    goals_heading: str = Field(min_length=3, max_length=120)
    goals_eyebrow: str = Field(default="Looking ahead", min_length=2, max_length=80)
    goals_body: str = Field(
        default="These priorities are updated by the Kokwet team. A status describes the plan, not a measured outcome.",
        min_length=10,
        max_length=300,
    )
    goals: list[ImpactGoal] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_unique_ids(self):
        identifiers = [item.id for item in (*self.pathways, *self.goals)]
        identifiers.extend(item.id for item in self.transparency.allocations)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Pathways, goals, and allocations need unique IDs")
        return self


DEFAULT_IMPACT = ImpactContent(
    hero=ImpactHero(
        eyebrow="A community-built future",
        title="Building the future,",
        highlight="rooted in culture.",
        body=(
            "Kokwet brings people together around culture, shared knowledge, "
            "mentorship and opportunity. This page focuses on the work ahead "
            "and the ways the community can help shape it."
        ),
    ),
    pathways=[
        ImpactPathway(
            id="campus-comrades",
            title="Campus comrades",
            description="Bring student perspectives, curiosity and practical ideas into the community.",
            actions=[
                "Share a question or idea in the Open Hearth.",
                "Help shape the topics for future community sessions.",
                "Invite peers to contribute their skills and perspectives.",
            ],
            cta_label="Join the Open Hearth",
            destination="open_hearth",
        ),
        ImpactPathway(
            id="young-professionals",
            title="Young professionals",
            description="Make your experience useful to people building their next step.",
            actions=[
                "Offer a focused mentorship conversation.",
                "Share a relevant opportunity or practical skill.",
                "Help connect community members to useful networks.",
            ],
            cta_label="Explore community support",
            destination="community_services",
        ),
        ImpactPathway(
            id="cultural-stewards",
            title="Cultural stewards",
            description="Help carry language, lived experience and community knowledge forward with care.",
            actions=[
                "Share a memory, reflection or voice note.",
                "Add context to community-submitted cultural material.",
                "Help ensure contributions are shared respectfully.",
            ],
            cta_label="Share your voice",
            destination="open_hearth",
        ),
        ImpactPathway(
            id="partners",
            title="Partners and supporters",
            description="Work with the community on practical, clearly communicated ways to contribute.",
            actions=[
                "Discuss a skills, venue or outreach partnership.",
                "Support a community activity with an agreed purpose.",
                "Ask for the current plan before making a contribution.",
            ],
            cta_label="Contact the community team",
            destination="community_services",
        ),
    ],
    transparency=ImpactTransparency(
        title="Resources, reported responsibly",
        body=(
            "No verified resource allocation report has been published yet. "
            "When financial information is ready to share, it will include its "
            "reporting period and source. Proposed figures, if shown, will be "
            "clearly distinguished from actual expenditure."
        ),
    ),
    goals_heading="The work ahead",
    goals=[
        ImpactGoal(
            id="community-platform",
            title="Keep the community platform useful and accessible",
            description="Improve the low-bandwidth experience and make it easier for people to participate.",
        ),
        ImpactGoal(
            id="cultural-contributions",
            title="Grow a community-led cultural record",
            description="Invite contributions with permission, context and respect for the people represented.",
        ),
        ImpactGoal(
            id="public-reporting",
            title="Publish an evidence-backed impact update",
            description="Share dated progress and financial reporting only when supporting information is available.",
        ),
    ],
)


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


def initialize_impact_store(*, seed_data: bool = True) -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS impact_content (
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
                INSERT OR IGNORE INTO impact_content
                    (content_id, content_json, updated_at, updated_by)
                VALUES ('public', ?, datetime('now'), NULL)
                """,
                (DEFAULT_IMPACT.model_dump_json(),),
            )


def _read_impact(connection: sqlite3.Connection) -> dict:
    row = connection.execute(
        "SELECT content_json FROM impact_content WHERE content_id = 'public'"
    ).fetchone()
    if not row:
        return DEFAULT_IMPACT.model_dump(mode="json")
    return ImpactContent.model_validate_json(row["content_json"]).model_dump(mode="json")


@public_router.get("/impact")
def get_impact() -> dict:
    initialize_impact_store()
    with _connect() as connection:
        return _read_impact(connection)


@staff_router.get("/impact")
def get_staff_impact(
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    _staff_from_authorization(authorization, ("admin", "moderator"))
    initialize_impact_store()
    with _connect() as connection:
        return _read_impact(connection)


@staff_router.put("/impact")
def update_impact(
    payload: ImpactContent,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    staff = _staff_from_authorization(authorization, ("admin", "moderator"))
    initialize_impact_store()
    content = payload.model_dump(mode="json")
    with _connect() as connection:
        connection.execute(
            """
            UPDATE impact_content
            SET content_json = ?, updated_at = datetime('now'), updated_by = ?
            WHERE content_id = 'public'
            """,
            (json.dumps(content, ensure_ascii=False), str(UUID(staff["staff_id"]))),
        )
        return _read_impact(connection)
