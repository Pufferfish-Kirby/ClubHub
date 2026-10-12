from datetime import datetime

from pydantic import BaseModel, Field

# A real answer to "what do you want out of university" is a sentence or two.
# The cap is generous for a rambling student and still far below the point where
# the blurb starts crowding out the candidate clubs in the rerank prompt -- an
# unbounded field here is both a cost lever and the widest prompt-injection
# surface in the app. Rejected by pydantic before any paid call is made.
MAX_BLURB_CHARS = 2000


class RecommendRequest(BaseModel):
    blurb: str = Field(min_length=1, max_length=MAX_BLURB_CHARS)


class EventLite(BaseModel):
    """An event as the graph and club cards render it.

    Mirrors frontend/src/types.js EventLite. Deliberately narrower than the Event
    row: no description, no confidence, and above all no organizer or contact
    fields (the privacy rule in CLAUDE.md).
    """

    id: str
    title: str
    start: datetime | None  # UTC; serialized as an ISO string
    location: str | None = None
    source: str  # sop | dropbox
    source_file: str | None = None
    dropbox_link: str | None = None


class PublicEvent(BaseModel):
    """Every event field the frontend renders, and nothing more.

    Narrower than the Event row on purpose. `confidence` is an internal extraction
    score and `status` is a workflow state; neither is a student's business, and
    shipping them invites a reader to reason about rows we have not verified.
    Organizer contact fields cannot leak here because the Event model has none --
    the privacy rule in CLAUDE.md is enforced at the schema level, not by filtering.

    Unlike EventLite this does carry `description`, because the club detail panel
    has room to show it; EventLite feeds the compact graph/card path that does not.
    """

    id: str
    club_id: str
    title: str
    start: datetime | None
    end: datetime | None = None
    location: str | None = None
    description: str | None = None
    rsvp_url: str | None = None
    source: str
    source_file: str | None = None
    dropbox_link: str | None = None


class ClubMatch(BaseModel):
    id: str
    name: str
    summary: str | None
    outcomes: list[str]  # from enrichment, 1-3 of FIXED_OUTCOMES
    tags: list[str]
    commitment: str | None  # casual | moderate | intense | unknown
    why_it_fits: str
    last_updated: datetime | None
    next_event: EventLite | None  # soonest upcoming published event, if any


class RecommendResponse(BaseModel):
    """`outcomes` describes the STUDENT (what the blurb asked for) and becomes the
    middle layer of the personal graph; each club's own `outcomes` come from
    enrichment. The frontend builds the graph itself from these two fields
    (lib/buildGraph.js), so no graph is returned here."""

    outcomes: list[str]
    clubs: list[ClubMatch]


# --- POST /upload -----------------------------------------------------------
# Field names match frontend/src/types.js EventLite so the UI can render an
# uploaded flier's events with the same component as the live /events feed.


class UploadedEvent(BaseModel):
    id: str
    title: str
    start: datetime | None  # UTC; serialized as an ISO string
    location: str | None
    status: str  # published | pending_review
    source: str = "dropbox"
    source_file: str
    dropbox_link: str | None
    # Set only when status is pending_review: which field to ask for (see extraction.missing_fields) and
    # the model's raw local-time read, so the upload UI can prompt for exactly the missing piece right
    # away instead of the event sitting unreachable in pending_review forever. Confirm via
    # POST /events/{id}/confirm.
    missing: list[str] = []
    start_local: str | None = None
    # Present only with `missing`: send it back to POST /events/{id}/confirm. See app/confirm_token.py.
    confirm_token: str | None = None


class UploadedClub(BaseModel):
    id: str
    name: str
    created: bool  # True = we had no record of this club and made one


class UploadResponse(BaseModel):
    """What the app tells a user after they upload a flier.

    `status` is the honest outcome, not an error code -- "we read this but
    couldn't confirm the date" is a normal result the UI should show, not a
    failure to hide.
    """

    status: str  # processed | no_events | duplicate | failed
    message: str  # human-readable, safe to show directly
    club: UploadedClub | None = None
    events: list[UploadedEvent] = []
    confidence: float | None = None
    uncertainties: list[str] = []
