"""
Plain-OOP domain layer for Club/Event, independent of the SQLModel/SQLite
persistence layer in models.py + database.py (still being built out).

These @dataclass types are what routers hand back to FastAPI for now. Once
the real DB session is ready, ClubRepository's internals swap from an
in-memory dict to SQLModel queries -- callers (routers) don't change.
"""

from dataclasses import dataclass, field
from datetime import datetime

from app.models import FIXED_OUTCOMES


@dataclass
class Event:
    id: str
    club_id: str
    title: str
    start: datetime | None = None
    end: datetime | None = None
    location: str | None = None
    description: str | None = None
    rsvp_url: str | None = None
    source: str = "sop"  # sop | dropbox
    status: str = "published"  # published | pending_review | discarded


@dataclass
class Club:
    id: str
    name: str
    campus: str | None = None
    summary: str | None = None
    outcomes: list[str] = field(default_factory=list)  # 1-3 from FIXED_OUTCOMES
    tags: list[str] = field(default_factory=list)
    commitment: str | None = None  # casual | moderate | intense | unknown
    meeting_info: str | None = None
    sop_url: str | None = None
    source: str = "sop"  # sop | dropbox
    events: list[Event] = field(default_factory=list)

    def __post_init__(self) -> None:
        invalid = set(self.outcomes) - set(FIXED_OUTCOMES)
        if invalid:
            raise ValueError(f"outcomes must come from FIXED_OUTCOMES, got: {invalid}")


def _seed_clubs() -> list[Club]:
    """Placeholder data so GET /clubs has something to return before sop_sync runs."""
    return [
        Club(
            id="club-robotics",
            name="Robotics Club",
            campus="Main",
            summary="Builds competition robots; open to all skill levels.",
            outcomes=["Build skills/portfolio", "Career and networking"],
            tags=["engineering", "robotics", "competition"],
            commitment="intense",
            meeting_info="Tuesdays 7pm, Engineering Building rm 104",
            source="sop",
            events=[
                Event(
                    id="event-robotics-kickoff",
                    club_id="club-robotics",
                    title="Fall Kickoff Meeting",
                    start=datetime(2026, 9, 25, 19, 0),
                    location="Engineering Building rm 104",
                    source="sop",
                    status="published",
                )
            ],
        ),
        Club(
            id="club-hiking",
            name="Outdoors & Hiking Club",
            campus="Main",
            summary="Weekend day hikes and a few overnight trips per semester.",
            outcomes=["Make friends", "Wellness and recreation"],
            tags=["outdoors", "hiking", "social"],
            commitment="casual",
            meeting_info="No regular meetings; trips posted per week",
            source="sop",
            events=[],
        ),
    ]


class ClubRepository:
    """
    In-memory Club store.

    Swap the constructor + these two methods for SQLModel session queries
    once the SQLite baseline lands -- the return types (Club/Event) can stay
    the same if the DB layer builds a Club.from_orm()-style mapper.
    """

    def __init__(self, clubs: list[Club] | None = None):
        self._clubs: dict[str, Club] = {c.id: c for c in (clubs or _seed_clubs())}

    def list_clubs(self) -> list[Club]:
        return list(self._clubs.values())

    def get_club(self, club_id: str) -> Club | None:
        return self._clubs.get(club_id)
