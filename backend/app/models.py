import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from app.timeutil import utc_now


def _uuid() -> str:
    return str(uuid.uuid4())


class Club(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    sop_id: Optional[str] = Field(default=None, index=True)
    sop_original_id: Optional[str] = Field(default=None, index=True)  # dedupe key

    name: str
    campus: Optional[str] = None
    sop_interest_areas: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    description_raw: Optional[str] = None
    summary: Optional[str] = None  # AI-written; "limited info" if thin

    outcomes: list[str] = Field(default_factory=list, sa_column=Column(JSON))  # 1-3 from fixed list
    # True when outcomes came from derive_outcomes_from_tags() rather than from Claude,
    # i.e. the listing was too thin for the model to assign any. Kept separate so the
    # reranker and the graph can tell a derived edge from an asserted one.
    outcomes_derived: bool = False
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))  # 5-8 lowercase
    commitment: Optional[str] = None  # casual | moderate | intense | unknown

    meeting_info: Optional[str] = None
    links: dict = Field(default_factory=dict, sa_column=Column(JSON))
    sop_url: Optional[str] = None

    listing_expires: Optional[datetime] = None
    last_updated: datetime = Field(default_factory=utc_now)
    source: str = "sop"  # sop | dropbox


class Event(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    club_id: str = Field(foreign_key="club.id", index=True)

    title: str
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    location: Optional[str] = None
    description: Optional[str] = None
    rsvp_url: Optional[str] = None

    source: str = "sop"  # sop | dropbox
    source_file: Optional[str] = None  # provenance
    dropbox_link: Optional[str] = None

    status: str = "published"  # published | pending_review
    confidence: Optional[float] = None  # 0-1


class IngestLog(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    dropbox_file_id: str = Field(index=True)
    content_hash: Optional[str] = None  # prevents reprocessing the same file
    status: str = "pending"
    result_json: Optional[str] = None
    timestamp: datetime = Field(default_factory=utc_now)

    # Scraped sources (app/services/event_sources/) have no Dropbox file to key
    # on, so they log against the adapter name plus a per-record key -- the
    # upstream event id when the source has one, else the event's URL. Nullable
    # because every existing row predates them and belongs to the Dropbox path.
    source_name: Optional[str] = Field(default=None, index=True)  # sop | dropbox | adapter slug
    source_key: Optional[str] = Field(default=None, index=True)  # upstream id or URL

    # Fixed outcome list used across enrichment + the personal graph.
    # Keep this list in sync with the frontend's outcome legend.


FIXED_OUTCOMES = [
    "Make friends",
    "Build skills/portfolio",
    "Career and networking",
    "Leadership",
    "Give back",
    "Culture and identity",
    "Wellness and recreation",
    "Academic/research",
]

# Controlled tag vocabulary. Free-form tags drifted badly: a 25-club pilot produced 148
# unique tags across 177 assignments (only 14% reused), with near-duplicates like
# academic / academic inquiry / academic support and culture / afghan culture /
# african culture. That's unusable for filtering or keyword matching.
#
# Tags are a CLASSIFICATION, not a factual claim, so picking from this list doesn't
# violate the "never invent detail" rule -- summary and commitment stay conservative.
# Club-specific terms ("k-pop", "poker") live in the embedding document via the raw
# description (see embeddings.club_to_document), not here.
#
# Derived from the 16 SOP interest areas plus the tags that actually recurred in the
# pilot. Keep it flat and lowercase; enrichment enforces it as a JSON-schema enum.
FIXED_TAGS = [
    # Fields of study and practice
    "artificial intelligence",
    "technology",
    "engineering",
    "science",
    "health and medicine",
    "mental health",
    "business",
    "finance",
    "entrepreneurship",
    "consulting",
    "law",
    "politics",
    "humanities",
    "social sciences",
    "languages",
    "education",
    # Arts and creative
    "music",
    "dance",
    "theatre",
    "visual arts",
    "creative writing",
    "film and media",
    "design",
    # What the club actually does
    "workshops",
    "competitions",
    "conferences",
    "research",
    "mentorship",
    "volunteering",
    "networking",
    "social events",
    "performance",
    "training and lessons",
    "hackathons",
    "publishing",
    "fundraising",
    # Community and identity
    "cultural heritage",
    "faith and spirituality",
    "international students",
    "graduate students",
    "advocacy",
    "human rights",
    "equity and inclusion",
    "community outreach",
    "sustainability",
    "student government",
    # Recreation
    "sports",
    "fitness",
    "games",
    "outdoors",
    "food",
]

# Which tags point at which outcome. Used ONLY as a fallback: when enrichment returns an
# empty outcomes array (the prompt tells it to, for listings too thin to support one),
# the club would otherwise have no edge in the personal graph -- which is built as
# you -> outcomes -> clubs -- and so could never be rendered at all.
#
# Deriving from tags rather than from sop_interest_areas covers every club: 14 rows came
# back with no outcomes, and while 12 had SOP areas, all 14 had tags (minimum tag count
# across the catalog is 1). Tags are already grounded in the source text, so this stays
# inside the "never invent detail" rule.
#
# A tag may point at several outcomes; scoring picks the strongest. Keep every entry of
# FIXED_TAGS represented here -- unmapped_tags() reports any that are missing.
OUTCOME_TAGS: dict[str, tuple[str, ...]] = {
    "Make friends": (
        "social events", "games", "food", "sports", "fitness", "outdoors",
        "international students",
    ),
    "Build skills/portfolio": (
        "workshops", "training and lessons", "hackathons", "competitions",
        "performance", "design", "creative writing", "publishing", "visual arts",
        "music", "dance", "theatre", "film and media",
    ),
    "Career and networking": (
        "networking", "conferences", "business", "finance", "entrepreneurship",
        "consulting", "law", "mentorship",
    ),
    "Leadership": ("student government", "mentorship"),
    "Give back": (
        "volunteering", "community outreach", "fundraising", "advocacy",
        "human rights", "equity and inclusion", "sustainability", "politics",
    ),
    "Culture and identity": (
        "cultural heritage", "faith and spirituality", "languages",
        "international students",
    ),
    "Wellness and recreation": (
        "sports", "fitness", "outdoors", "mental health", "games",
    ),
    "Academic/research": (
        "research", "science", "engineering", "technology", "artificial intelligence",
        "humanities", "social sciences", "education", "health and medicine",
        "graduate students", "conferences",
    ),
}


def unmapped_tags() -> set[str]:
    """FIXED_TAGS entries no outcome claims -- a club tagged only with these would
    still derive nothing. Should stay empty; check it after editing either list."""
    claimed = {tag for tags in OUTCOME_TAGS.values() for tag in tags}
    return set(FIXED_TAGS) - claimed


def derive_outcomes_from_tags(tags: list[str], *, limit: int = 3) -> list[str]:
    """Best-effort outcomes for a club whose listing was too thin for Claude to assign
    any. Scores each outcome by how many of the club's tags point at it; ties break on
    FIXED_OUTCOMES order so the result is deterministic across runs."""
    if not tags:
        return []
    tag_set = {t.lower() for t in tags}
    scored = [
        (sum(1 for tag in outcome_tags if tag in tag_set), -FIXED_OUTCOMES.index(outcome), outcome)
        for outcome, outcome_tags in OUTCOME_TAGS.items()
    ]
    return [outcome for score, _, outcome in sorted(scored, reverse=True) if score > 0][:limit]
