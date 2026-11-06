import json
import sqlite3
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from app.constants import HOUSE_NAMES
from app.culture import _connect
from app.publishing import _staff_from_authorization

public_router = APIRouter(prefix="/api", tags=["Site content"])
staff_router = APIRouter(prefix="/api/v1/staff", tags=["Site content management"])


def _validate_media_url(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("/") and not normalized.startswith("//") and ".." not in normalized:
        return normalized
    parsed = urlparse(normalized)
    if parsed.scheme == "https" and parsed.netloc:
        return normalized
    raise ValueError("Media URLs must be HTTPS or a safe same-site path")


class NavigationLink(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    path: str = Field(min_length=1, max_length=120)
    is_active: bool = True

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("Navigation paths must be local site paths")
        return value


class GlobalSettings(BaseModel):
    whatsapp_number: str = Field(pattern=r"^\d{10,15}$")
    whatsapp_message: str = Field(min_length=1, max_length=300)
    navigation: list[NavigationLink] = Field(min_length=1, max_length=12)
    footer_tagline: str = Field(min_length=5, max_length=240)
    footer_copyright: str = Field(min_length=5, max_length=160)
    logo_wordmark_light_url: str = Field(default="/images/kokwet-wordmark-light.png", min_length=1, max_length=1000)
    logo_full_light_url: str = Field(default="/images/kokwet-logo-light.png", min_length=1, max_length=1000)
    marketplace_button_label: str = Field(default="Get a pass", min_length=2, max_length=40)
    marketplace_navigation_label: str = Field(default="Marketplace", min_length=2, max_length=40)

    @field_validator("logo_wordmark_light_url", "logo_full_light_url")
    @classmethod
    def validate_logo_url(cls, value: str) -> str:
        return _validate_media_url(value)


class HeroContent(BaseModel):
    video_mp4_url: str = Field(min_length=1, max_length=1000)
    video_webm_url: str = Field(min_length=1, max_length=1000)
    poster_image_url: str = Field(min_length=1, max_length=1000)
    title_first: str = Field(min_length=1, max_length=60)
    title_highlight: str = Field(min_length=1, max_length=60)
    title_last: str = Field(min_length=1, max_length=80)
    subheadline: str = Field(min_length=1, max_length=300)
    explore_cta_label: str = Field(default="Explore the gathering", min_length=2, max_length=80)
    passes_cta_label: str = Field(default="Get festival passes", min_length=2, max_length=80)

    @field_validator("video_mp4_url", "video_webm_url", "poster_image_url")
    @classmethod
    def validate_media_url(cls, value: str) -> str:
        return _validate_media_url(value)


class ShieldNode(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$", max_length=40)
    word: str = Field(min_length=1, max_length=50)
    meaning: str = Field(min_length=1, max_length=100)
    group: str = Field(min_length=1, max_length=80)
    caption: str = Field(min_length=1, max_length=250)
    image: str = Field(min_length=1, max_length=1000)
    accent: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        return _validate_media_url(value)


class ShieldImage(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$", max_length=40)
    label: str = Field(min_length=1, max_length=80)
    image: str = Field(min_length=1, max_length=1000)
    caption: str = Field(min_length=1, max_length=250)

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        return _validate_media_url(value)


class ShieldImageSet(BaseModel):
    images: list[ShieldImage] = Field(min_length=2, max_length=2)


class ShieldContent(BaseModel):
    backdrop_image_url: str = Field(min_length=1, max_length=1000)
    artwork_image_url: str = Field(min_length=1, max_length=1000)
    nodes: list[ShieldNode] = Field(min_length=2, max_length=2)
    image_sets: list[ShieldImageSet] = Field(min_length=2, max_length=2)
    rotation_interval_seconds: int = Field(default=60, ge=10, le=300)

    @field_validator("backdrop_image_url", "artwork_image_url")
    @classmethod
    def validate_media_url(cls, value: str) -> str:
        return _validate_media_url(value)


class CommunityMissionPoint(BaseModel):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=2, max_length=100)
    description: str = Field(min_length=5, max_length=200)
    icon: Literal["landmark", "handshake"]


class CommunityRoleCard(BaseModel):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9-]+$")
    eyebrow: str = Field(min_length=2, max_length=100)
    title: str = Field(min_length=2, max_length=100)
    description: str = Field(min_length=10, max_length=350)
    image: str = Field(min_length=1, max_length=1000)
    icon: Literal["landmark", "users"]

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        return _validate_media_url(value)


class CommunityPathwayCard(BaseModel):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9-]+$")
    label: str = Field(min_length=2, max_length=100)
    image: str = Field(min_length=1, max_length=1000)
    destination: Literal["/services", "/marketplace", "/impact"]

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        return _validate_media_url(value)


class CommunityContent(BaseModel):
    eyebrow: str = Field(min_length=1, max_length=80)
    title_first: str = Field(min_length=1, max_length=100)
    title_highlight: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=800)
    main_image_url: str = Field(min_length=1, max_length=1000)
    inset_image_url: str = Field(min_length=1, max_length=1000)
    mission_points: list[CommunityMissionPoint] = Field(default_factory=lambda: [
        CommunityMissionPoint(id="culture-preserved", title="Culture preserved", description="Stories and knowledge carried forward.", icon="landmark"),
        CommunityMissionPoint(id="generations-connected", title="Generations connected", description="Respect, guidance, and shared growth.", icon="handshake"),
    ], min_length=2, max_length=2)
    impact_link_label: str = Field(default="Explore our community impact", min_length=2, max_length=100)
    visual_badge_first: str = Field(default="Culture meets", min_length=2, max_length=60)
    visual_badge_last: str = Field(default="the future.", min_length=2, max_length=60)
    visual_caption: str = Field(default="One community · many generations", min_length=2, max_length=120)
    pillars_eyebrow: str = Field(default="People make the community", min_length=2, max_length=100)
    pillars_title_first: str = Field(default="Community", min_length=2, max_length=100)
    pillars_title_highlight: str = Field(default="pillars", min_length=2, max_length=100)
    pillars_copy: str = Field(default="Recognizing the roles that preserve our roots and help the next generation thrive.", min_length=10, max_length=300)
    role_cards: list[CommunityRoleCard] = Field(default_factory=lambda: [
        CommunityRoleCard(id="cultural-stewards", eyebrow="Wisdom & heritage", title="Cultural stewards", description="Intergenerational voices helping keep community history, language, and values alive.", image="https://images.unsplash.com/photo-1511895426328-dc8714191300?auto=format&fit=crop&w=900&q=82", icon="landmark"),
        CommunityRoleCard(id="youth-voices", eyebrow="Ideas & action", title="Youth and campus voices", description="Young people building networks, sharing skills, and shaping what comes next.", image="https://images.unsplash.com/photo-1523240795612-9a054b0db644?auto=format&fit=crop&w=900&q=82", icon="users"),
    ], min_length=2, max_length=2)
    members_eyebrow: str = Field(default="Find your place", min_length=2, max_length=80)
    members_heading: str = Field(default="Active community pathways", min_length=2, max_length=120)
    pathways_link_label: str = Field(default="Explore pathways", min_length=2, max_length=80)
    pathways: list[CommunityPathwayCard] = Field(default_factory=lambda: [
        CommunityPathwayCard(id="campus-comrades", label="Campus comrades", image="https://images.unsplash.com/photo-1523240795612-9a054b0db644?auto=format&fit=crop&w=700&q=80", destination="/services"),
        CommunityPathwayCard(id="creative-leaders", label="Creative leaders", image="https://images.unsplash.com/photo-1521737711867-e3b97375f902?auto=format&fit=crop&w=700&q=80", destination="/marketplace"),
        CommunityPathwayCard(id="young-professionals", label="Young professionals", image="https://images.unsplash.com/photo-1528605248644-14dd04022da1?auto=format&fit=crop&w=700&q=80", destination="/impact"),
    ], min_length=3, max_length=3)
    members_note: str = Field(default="Member spotlights are shared with permission and will grow with the community.", min_length=10, max_length=240)

    @field_validator("main_image_url", "inset_image_url")
    @classmethod
    def validate_media_url(cls, value: str) -> str:
        return _validate_media_url(value)

    @model_validator(mode="after")
    def validate_unique_card_ids(self):
        identifiers = [item.id for item in (*self.mission_points, *self.role_cards, *self.pathways)]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Community spotlight card IDs must be unique")
        return self


class HouseProfile(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    anchor: str = Field(min_length=2, max_length=120)
    blueprint: str = Field(min_length=10, max_length=250)
    tagline: str = Field(min_length=2, max_length=160)
    summary: str = Field(min_length=20, max_length=500)
    history: str = Field(min_length=20, max_length=800)
    achievement: str = Field(min_length=20, max_length=500)
    dialect: str = Field(min_length=2, max_length=200)


class ArtistProfile(BaseModel):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=70)
    style: str = Field(min_length=1, max_length=70)
    image: str = Field(min_length=1, max_length=1000)

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        return _validate_media_url(value)


class SectionCopy(BaseModel):
    eyebrow: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=2, max_length=120)
    description: str = Field(min_length=10, max_length=400)


class EnterpriseProfile(BaseModel):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=2, max_length=100)
    description: str = Field(min_length=10, max_length=350)
    image: str = Field(min_length=1, max_length=1000)

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        return _validate_media_url(value)


class CohortOption(BaseModel):
    name: Literal[
        "Campus Comrade",
        "Young Professional",
        "Creative Leader",
        "Established Elder",
    ]
    description: str = Field(min_length=3, max_length=120)


class HomeJoinContent(BaseModel):
    eyebrow: str = Field(min_length=2, max_length=80)
    title_first: str = Field(min_length=2, max_length=100)
    title_highlight: str = Field(min_length=2, max_length=100)
    description: str = Field(min_length=10, max_length=300)
    note: str = Field(min_length=3, max_length=120)
    form_eyebrow: str = Field(min_length=2, max_length=80)
    form_title: str = Field(min_length=2, max_length=120)
    form_copy: str = Field(min_length=5, max_length=250)
    submit_label: str = Field(min_length=2, max_length=80)
    cohorts: list[CohortOption] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def validate_cohorts(self):
        names = [cohort.name for cohort in self.cohorts]
        if len(set(names)) != len(names):
            raise ValueError("Each onboarding cohort must appear exactly once")
        return self


class ServiceCard(BaseModel):
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=2, max_length=100)
    description: str = Field(min_length=10, max_length=300)
    icon: Literal["users", "landmark", "ticket"]


class NameInspirationIdea(BaseModel):
    id: Literal["dawn", "rain", "harvest", "cattle_return", "night"]
    context_label: str = Field(min_length=2, max_length=80)
    male_name: str = Field(min_length=2, max_length=80)
    female_name: str = Field(min_length=2, max_length=80)
    meaning: str = Field(min_length=10, max_length=300)
    source_attribution: str = Field(default="", max_length=160)
    source_consent: bool = False

    @model_validator(mode="after")
    def validate_source_consent(self):
        if self.source_attribution.strip() and not self.source_consent:
            raise ValueError("Public source attribution requires explicit consent")
        return self


DEFAULT_NAME_IDEAS = [
    NameInspirationIdea(id="dawn", context_label="At dawn", male_name="Kipkoech", female_name="Chepkoech", meaning="A name associated here with dawn and new beginnings."),
    NameInspirationIdea(id="rain", context_label="During the rains", male_name="Kipchirchir", female_name="Chepchirchir", meaning="A name associated here with the rains, renewal and abundance."),
    NameInspirationIdea(id="harvest", context_label="During harvest", male_name="Kiprono", female_name="Chepono", meaning="A name associated here with harvest, provision and community sustenance."),
    NameInspirationIdea(id="cattle_return", context_label="When cattle returned home", male_name="Kipkirui", female_name="Cherotich", meaning="A name associated here with cattle returning home, peace and a journey completed."),
    NameInspirationIdea(id="night", context_label="At night", male_name="Kipkosgei", female_name="Chepkosgei", meaning="A name associated here with night, reflection and quiet strength."),
]


class ServicesPageContent(BaseModel):
    eyebrow: str = Field(min_length=2, max_length=80)
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=10, max_length=400)
    cards: list[ServiceCard] = Field(min_length=3, max_length=3)
    mentorship_eyebrow: str = Field(min_length=2, max_length=80)
    mentorship_title: str = Field(min_length=5, max_length=160)
    phone_label: str = Field(min_length=2, max_length=80)
    focus_label: str = Field(min_length=2, max_length=120)
    submit_label: str = Field(min_length=2, max_length=80)
    success_message: str = Field(min_length=10, max_length=240)
    name_inspiration_eyebrow: str = Field(default="Cultural name inspiration", min_length=2, max_length=80)
    name_inspiration_title: str = Field(default="Explore a name inspired by the moment.", min_length=5, max_length=160)
    name_inspiration_description: str = Field(default="Choose a naming pattern and a circumstance to see an example name and its associated meaning.", min_length=10, max_length=300)
    name_inspiration_note: str = Field(default="Name traditions and meanings vary across families and communities. These are illustrative examples, not an authoritative naming record; please check spellings and interpretations with fluent speakers and cultural elders.", min_length=20, max_length=500)
    name_ideas: list[NameInspirationIdea] = Field(default_factory=lambda: [idea.model_copy(deep=True) for idea in DEFAULT_NAME_IDEAS], min_length=5, max_length=5)

    @model_validator(mode="after")
    def validate_unique_ids(self):
        ids = [card.id for card in self.cards]
        if len(ids) != len(set(ids)):
            raise ValueError("Service card IDs must be unique")
        name_ids = [idea.id for idea in self.name_ideas]
        if len(name_ids) != len(set(name_ids)) or set(name_ids) != {"dawn", "rain", "harvest", "cattle_return", "night"}:
            raise ValueError("Keep exactly one naming example for each supported circumstance")
        return self


class MarketplacePageContent(BaseModel):
    eyebrow: str = Field(min_length=2, max_length=80)
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=10, max_length=400)
    multi_order_label: str = Field(min_length=3, max_length=100)


DEFAULT_ARTISTS = [
    ArtistProfile(id="artist-1", name="Artist 01", style="Headline act", image="https://images.unsplash.com/photo-1501386761578-eac5c94b800a?auto=format&fit=crop&w=900&q=82"),
    ArtistProfile(id="artist-2", name="Artist 02", style="Cultural performer", image="https://images.unsplash.com/photo-1501612780327-45045538702b?auto=format&fit=crop&w=900&q=82"),
    ArtistProfile(id="artist-3", name="Artist 03", style="Afro-fusion", image="https://images.unsplash.com/photo-1516280440614-37939bbacd81?auto=format&fit=crop&w=900&q=82"),
    ArtistProfile(id="artist-4", name="Artist 04", style="Live music", image="https://images.unsplash.com/photo-1470229722913-7c0e2dbbafd3?auto=format&fit=crop&w=900&q=82"),
    ArtistProfile(id="artist-5", name="Artist 05", style="Dance & heritage", image="https://images.unsplash.com/photo-1506157786151-b8491531f063?auto=format&fit=crop&w=900&q=82"),
]


class HomeContent(BaseModel):
    hero: HeroContent
    shield: ShieldContent
    community: CommunityContent
    events_section: SectionCopy = Field(default_factory=lambda: SectionCopy(
        eyebrow="The live noticeboard",
        title="Gather with purpose",
        description="Find upcoming gatherings, community events and opportunities.",
    ))
    artists_section: SectionCopy = Field(default_factory=lambda: SectionCopy(
        eyebrow="On the main stage",
        title="Featured Artists",
        description="Meet the headline acts and cultural performers bringing energy, heritage and world-class entertainment to the main stage.",
    ))
    enterprise_section: SectionCopy = Field(default_factory=lambda: SectionCopy(
        eyebrow="Featured enterprise",
        title="The exhibition showcase",
        description="Discover youth-led ventures, regional makers and agricultural teams shaping Kokwet's next economy.",
    ))
    pass_section: SectionCopy = Field(default_factory=lambda: SectionCopy(
        eyebrow="Secure in-house ticketing",
        title="Choose your festival pass",
        description="Every confirmed pass includes reserved inventory, secure checkout and a unique digital entry token.",
    ))
    enterprises: list[EnterpriseProfile] = Field(default_factory=lambda: [
        EnterpriseProfile(id="fashion-design", title="Fashion & design", description="Community-run textiles and contemporary cultural garments.", image="https://images.unsplash.com/photo-1606760227091-3dd870d97f1d?auto=format&fit=crop&w=900&q=78"),
        EnterpriseProfile(id="agribusiness", title="Agribusiness", description="Dairy, tea and cooperative management for regional growers.", image="https://images.unsplash.com/photo-1500382017468-9049fed747ef?auto=format&fit=crop&w=900&q=78"),
        EnterpriseProfile(id="digital-studio", title="Digital studio", description="Youth-built media, technology and cultural storytelling.", image="https://images.unsplash.com/photo-1516321318423-f06f85e504b3?auto=format&fit=crop&w=900&q=78"),
    ], max_length=6)
    join: HomeJoinContent = Field(default_factory=lambda: HomeJoinContent(
        eyebrow="Join the ecosystem",
        title_first="Your place is",
        title_highlight="waiting.",
        description="Find your community, share what you need, and take your next step with Kokwet.",
        note="Four paths. One community.",
        form_eyebrow="Start here",
        form_title="How can we support you?",
        form_copy="Choose the path that feels right for you.",
        submit_label="Enter the ecosystem",
        cohorts=[
            CohortOption(name="Campus Comrade", description="Mentorship & career resources"),
            CohortOption(name="Young Professional", description="Corporate networking"),
            CohortOption(name="Creative Leader", description="Venture showcase"),
            CohortOption(name="Established Elder", description="Intergenerational guidance"),
        ],
    ))


class SiteContent(BaseModel):
    settings: GlobalSettings
    home: HomeContent
    houses: list[HouseProfile] = Field(min_length=len(HOUSE_NAMES), max_length=len(HOUSE_NAMES))
    artists: list[ArtistProfile] = Field(default_factory=lambda: [artist.model_copy(deep=True) for artist in DEFAULT_ARTISTS], max_length=12)
    services: ServicesPageContent = Field(default_factory=lambda: ServicesPageContent(
        eyebrow="Beyond the festival",
        title="The high-utility service hub",
        description="A practical bridge between campus ambition, established experience and local enterprise.",
        cards=[
            ServiceCard(id="mentorship", title="Mentorship nexus", description="Request a resume review or career guidance from verified professionals.", icon="users"),
            ServiceCard(id="enterprise", title="Creative enterprise", description="Promote a venture, cultural work or agri-enterprise to the wider community.", icon="landmark"),
            ServiceCard(id="festival-access", title="Festival access", description="Claim a pass and receive a unique, secure digital token.", icon="ticket"),
        ],
        mentorship_eyebrow="Intergenerational mentorship",
        mentorship_title="Bring your ambition. We’ll find a guide.",
        phone_label="Mobile number",
        focus_label="What can an elder help you with?",
        submit_label="Request mentorship",
        success_message="Request received. A mentor coordinator will be in touch.",
    ))
    marketplace: MarketplacePageContent = Field(default_factory=lambda: MarketplacePageContent(
        eyebrow="Ticket engine",
        title="Festival marketplace",
        description="Choose one or more passes, review your total, and continue in the secure ticket drawer.",
        multi_order_label="Build a multi-pass order",
    ))

    @model_validator(mode="after")
    def validate_house_profiles(self):
        names = [house.name for house in self.houses]
        if len(set(names)) != len(names) or set(names) != set(HOUSE_NAMES):
            raise ValueError("Keep exactly one profile for each of the Nine Houses")
        return self

    @model_validator(mode="after")
    def validate_artist_ids(self):
        ids = [artist.id for artist in self.artists]
        if len(set(ids)) != len(ids):
            raise ValueError("Artist IDs must be unique")
        return self


DEFAULT_SITE_CONTENT = SiteContent(
    settings=GlobalSettings(
        whatsapp_number="254700000000",
        whatsapp_message="Hello Kokwet Festival, I would like to know more.",
        navigation=[
            NavigationLink(label="Home", path="/"),
            NavigationLink(label="Voice Circle", path="/culture"),
            NavigationLink(label="Impact", path="/impact"),
            NavigationLink(label="Roadmap", path="/roadmap"),
            NavigationLink(label="Community", path="/services"),
        ],
        footer_tagline="One people. Many houses. A digital home for shared culture and a future built together.",
        footer_copyright="© 2026 Kokwet Festival · Culture Meets the Future",
    ),
    home=HomeContent(
        hero=HeroContent(
            video_mp4_url="/videos/hero-bg.mp4",
            video_webm_url="/videos/hero-bg.webm",
            poster_image_url="/images/hero-poster.jpg",
            title_first="Culture",
            title_highlight="Meets",
            title_last="the Future.",
            subheadline="A new generation. Diverse cultures. One shared future.",
        ),
        shield=ShieldContent(
            backdrop_image_url="https://images.unsplash.com/photo-1511895426328-dc8714191300?auto=format&fit=crop&w=1200&q=82",
            artwork_image_url="https://media.istockphoto.com/id/165549580/vector/masai-shield-spears.jpg?s=612x612&w=0&k=20&c=tT4uDtEcOws-7HTDRkAvNmbYIMY8VdbhdgGwAuqBxOA=",
            nodes=[
                ShieldNode(
                    id="children",
                    word="IMMANIT",
                    meaning="Core truth",
                    group="Children & roots",
                    caption="Children learning, growing, and carrying culture forward.",
                    image="https://images.unsplash.com/photo-1488521787991-ed7bbaae773c?auto=format&fit=crop&w=900&q=82",
                    accent="#F69B10",
                ),
                ShieldNode(
                    id="youth",
                    word="KONYIT",
                    meaning="Respect",
                    group="Campus & youth",
                    caption="Students and young people learning, connecting, and building a shared future.",
                    image="https://images.unsplash.com/photo-1523240795612-9a054b0db644?auto=format&fit=crop&w=900&q=82",
                    accent="#0A5A1E",
                ),
            ],
            image_sets=[
                ShieldImageSet(
                    images=[
                        ShieldImage(
                            id="children-roots",
                            label="Children & roots",
                            image="https://images.unsplash.com/photo-1488521787991-ed7bbaae773c?auto=format&fit=crop&w=900&q=82",
                            caption="Children learning, growing, and carrying culture forward.",
                        ),
                        ShieldImage(
                            id="campus-youth",
                            label="Campus & youth",
                            image="https://images.unsplash.com/photo-1523240795612-9a054b0db644?auto=format&fit=crop&w=900&q=82",
                            caption="Students and young people learning, connecting, and building a shared future.",
                        ),
                    ]
                ),
                ShieldImageSet(
                    images=[
                        ShieldImage(
                            id="community-voices",
                            label="Community voices",
                            image="https://images.unsplash.com/photo-1529156069898-49953e39b3ac?auto=format&fit=crop&w=900&q=82",
                            caption="Community members gathering across generations.",
                        ),
                        ShieldImage(
                            id="young-leaders",
                            label="Young leaders",
                            image="https://images.unsplash.com/photo-1521737711867-e3b97375f902?auto=format&fit=crop&w=900&q=82",
                            caption="Young leaders collaborating and building together.",
                        ),
                    ]
                ),
            ],
        ),
        community=CommunityContent(
            eyebrow="Our core mission",
            title_first="Building the future,",
            title_highlight="rooted in culture.",
            body="We connect generations through shared heritage, mentorship, and opportunity—bringing community knowledge into the spaces where young people learn, create, and build.",
            main_image_url="https://images.unsplash.com/photo-1529156069898-49953e39b3ac?auto=format&fit=crop&w=1200&q=82",
            inset_image_url="https://images.unsplash.com/photo-1511895426328-dc8714191300?auto=format&fit=crop&w=700&q=82",
        ),
    ),
    houses=[
        HouseProfile(
            name="Kipsigis",
            anchor="Kericho & Bomet Highlands",
            blueprint="Agro-pastoral heritage and intergenerational unity.",
            tagline="Living traditions of the Kipsigis community",
            summary="Explore the language, oral history and contemporary achievements of the Kipsigis community.",
            history="The Kipsigis carry distinctive knowledge systems shaped by community, land and intergenerational stewardship.",
            achievement="Kipsigis leaders continue to contribute across education, enterprise, culture and public service.",
            dialect="A shared greeting from the elders",
        ),
        HouseProfile(
            name="Nandi",
            anchor="Nandi Hills Escarpment",
            blueprint="Historical resistance and leadership frameworks.",
            tagline="Living traditions of the Nandi community",
            summary="Explore the language, oral history and contemporary achievements of the Nandi community.",
            history="The Nandi carry distinctive knowledge systems shaped by community, land and intergenerational stewardship.",
            achievement="Nandi leaders continue to contribute across education, enterprise, culture and public service.",
            dialect="A shared greeting from the elders",
        ),
        HouseProfile(
            name="Tugen",
            anchor="Baringo Viewpoints",
            blueprint="Regional escarpment histories and community wisdom.",
            tagline="Living traditions of the Tugen community",
            summary="Explore the language, oral history and contemporary achievements of the Tugen community.",
            history="The Tugen carry distinctive knowledge systems shaped by community, land and intergenerational stewardship.",
            achievement="Tugen leaders continue to contribute across education, enterprise, culture and public service.",
            dialect="A shared greeting from the elders",
        ),
        HouseProfile(
            name="Keiyo",
            anchor="Elgeyo Escarpment",
            blueprint="Distinct highland agricultural and social structures.",
            tagline="Living traditions of the Keiyo community",
            summary="Explore the language, oral history and contemporary achievements of the Keiyo community.",
            history="The Keiyo carry distinctive knowledge systems shaped by community, land and intergenerational stewardship.",
            achievement="Keiyo leaders continue to contribute across education, enterprise, culture and public service.",
            dialect="A shared greeting from the elders",
        ),
        HouseProfile(
            name="Marakwet",
            anchor="Cherangany Slopes",
            blueprint="Traditional furrow irrigation and shared stewardship.",
            tagline="Living traditions of the Marakwet community",
            summary="Explore the language, oral history and contemporary achievements of the Marakwet community.",
            history="The Marakwet carry distinctive knowledge systems shaped by community, land and intergenerational stewardship.",
            achievement="Marakwet leaders continue to contribute across education, enterprise, culture and public service.",
            dialect="A shared greeting from the elders",
        ),
        HouseProfile(
            name="Pokot",
            anchor="Western Rift Valley",
            blueprint="Resilient pastoralist traditions and cultural endurance.",
            tagline="Living traditions of the Pokot community",
            summary="Explore the language, oral history and contemporary achievements of the Pokot community.",
            history="The Pokot carry distinctive knowledge systems shaped by community, land and intergenerational stewardship.",
            achievement="Pokot leaders continue to contribute across education, enterprise, culture and public service.",
            dialect="A shared greeting from the elders",
        ),
        HouseProfile(
            name="Sabaot",
            anchor="Mount Elgon Foothills",
            blueprint="Agricultural heritage and mountain folklore.",
            tagline="Living traditions of the Sabaot community",
            summary="Explore the language, oral history and contemporary achievements of the Sabaot community.",
            history="The Sabaot carry distinctive knowledge systems shaped by community, land and intergenerational stewardship.",
            achievement="Sabaot leaders continue to contribute across education, enterprise, culture and public service.",
            dialect="A shared greeting from the elders",
        ),
        HouseProfile(
            name="Terik",
            anchor="Kakamega Borderlands",
            blueprint="Linguistic and cultural cross-pollination.",
            tagline="Living traditions of the Terik community",
            summary="Explore the language, oral history and contemporary achievements of the Terik community.",
            history="The Terik carry distinctive knowledge systems shaped by community, land and intergenerational stewardship.",
            achievement="Terik leaders continue to contribute across education, enterprise, culture and public service.",
            dialect="A shared greeting from the elders",
        ),
        HouseProfile(
            name="Ogiek & Sengwer",
            anchor="Mau Forest & Cherangany",
            blueprint="Indigenous forest stewardship and ancestral guardianship.",
            tagline="Living traditions of the Ogiek & Sengwer community",
            summary="Explore the language, oral history and contemporary achievements of the Ogiek & Sengwer community.",
            history="The Ogiek & Sengwer carry distinctive knowledge systems shaped by community, land and intergenerational stewardship.",
            achievement="Ogiek & Sengwer leaders continue to contribute across education, enterprise, culture and public service.",
            dialect="A shared greeting from the elders",
        ),
    ],
)

LEGACY_DEMO_PASS_DESCRIPTION = (
    "Reserved inventory, a clear secure checkout and a digital token for every confirmed demo pass."
)
LEGACY_NAME_INSPIRATION_EYEBROW = "Interactive cultural sandbox"


def initialize_site_content_store(*, seed_data: bool = True) -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS site_content (
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
                INSERT OR IGNORE INTO site_content
                    (content_id, content_json, updated_at, updated_by)
                VALUES ('public', ?, datetime('now'), NULL)
                """,
                (DEFAULT_SITE_CONTENT.model_dump_json(),),
            )


def _read_site_content(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        "SELECT content_json FROM site_content WHERE content_id = 'public'"
    ).fetchone()
    if not row:
        return DEFAULT_SITE_CONTENT.model_dump(mode="json")
    stored = json.loads(row["content_json"])
    merged = _merge_missing_content(
        DEFAULT_SITE_CONTENT.model_dump(mode="json"),
        stored,
    )
    content = SiteContent.model_validate(merged).model_dump(mode="json")
    if content["home"]["pass_section"]["description"] == LEGACY_DEMO_PASS_DESCRIPTION:
        content["home"]["pass_section"]["description"] = (
            "Every confirmed pass includes reserved inventory, secure checkout "
            "and a unique digital entry token."
        )
    if content["services"]["name_inspiration_eyebrow"] == LEGACY_NAME_INSPIRATION_EYEBROW:
        content["services"]["name_inspiration_eyebrow"] = "Cultural name inspiration"
    return content


def _merge_missing_content(default: Any, stored: Any) -> Any:
    if isinstance(default, dict) and isinstance(stored, dict):
        merged = {
            key: _merge_missing_content(value, stored[key]) if key in stored else value
            for key, value in default.items()
        }
        merged.update({key: value for key, value in stored.items() if key not in default})
        return merged
    return stored


@public_router.get("/site-content")
def get_site_content() -> dict[str, Any]:
    initialize_site_content_store()
    with _connect() as connection:
        return _read_site_content(connection)


def get_house_profiles() -> list[dict[str, Any]]:
    return get_site_content()["houses"]


@staff_router.get("/site-content")
def get_staff_site_content(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _staff_from_authorization(authorization, ("admin",))
    initialize_site_content_store()
    with _connect() as connection:
        return _read_site_content(connection)


@staff_router.put("/site-content")
def update_site_content(
    payload: SiteContent,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    staff = _staff_from_authorization(authorization, ("admin",))
    initialize_site_content_store()
    content = payload.model_dump(mode="json")
    with _connect() as connection:
        connection.execute(
            """
            UPDATE site_content
            SET content_json = ?, updated_at = datetime('now'), updated_by = ?
            WHERE content_id = 'public'
            """,
            (json.dumps(content, ensure_ascii=False), staff["staff_id"]),
        )
        return _read_site_content(connection)
