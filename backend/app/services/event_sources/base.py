"""The contract every event-source adapter implements.

An adapter's only job is to turn one external source into a list of
`ScrapedEvent`. It does not touch the database, decide what gets published, or
filter by date -- `runner.py` does all of that, once, for every source.

That split is deliberate. Three of this project's rules are safety rules, not
preferences: no organizer contact details ever reach a column, no event that has
already happened reaches the feed, and nothing auto-publishes without a title, a
start and a time of day. Spread across N hand-written scrapers those rules get
forgotten exactly once and the failure is silent. Here an adapter physically
cannot skip them, because it never writes a row.

An adapter is a module exposing two names:

    SOURCE_NAME: str    # stable slug, lands in Event.source
    def fetch(*, limit: int | None = None) -> list[ScrapedEvent]

Run `python -m app.services.event_sources.validate` (or
`scripts/validate_adapter.py`) against a new adapter before wiring it up.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime

# Reused rather than redefined: to_utc already handles both shapes an API hands
# back -- a naive local wall-clock string and an offset-aware one -- and gets DST
# right for the campus zone. Adapters should not do their own date arithmetic.
from app.services.extraction import CAMPUS_TZ, to_utc

__all__ = [
    "CAMPUS_TZ",
    "ScrapedEvent",
    "find_contacts",
    "safe_console",
    "scrub_contacts",
    "to_utc",
]


def safe_console() -> None:
    """Stop Windows consoles from crashing on event titles.

    Dev happens on Windows, where stdout defaults to cp1252. Real club events are
    full of en-dashes and curly quotes, so any script that prints a title dies
    with UnicodeEncodeError partway through its output -- including the validator,
    which would fail while reporting a failure. Call this at the top of anything
    with a __main__.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # already wrapped, or not a real stream


@dataclass
class ScrapedEvent:
    """One future event read off an external source, times already in UTC.

    `start` is timezone-aware UTC, matching `Event.start` and what
    `extraction.to_utc()` returns -- not a local time and not naive. Passing a
    local time through here shifts every event by 4-5 hours, which is the kind of bug that only
    shows up as "the 7pm event says 3pm" during a demo.

    `confidence` mirrors the field extraction sets, so the same publish gate can
    judge both paths. A structured JSON API that states a start time explicitly
    is 1.0; anything a language model inferred from prose should come in lower,
    which routes it to review instead of the live feed.
    """

    title: str
    club_name_guess: str
    start: datetime | None  # aware UTC
    end: datetime | None = None
    location: str | None = None
    description: str | None = None
    rsvp_url: str | None = None
    confidence: float = 1.0

    # Provenance. source_url is the page a human can open to check the event;
    # external_id is the upstream record id when the source has one. The runner
    # keys its dedupe log on external_id or source_url, so an adapter that
    # supplies neither will re-ingest the same events on every run.
    source_url: str | None = None
    external_id: str | None = None

    # Set when the source marks the event as all-day or gives only a date. Must
    # be explicit: a local midnight converted to UTC lands on 04:00 or 05:00, so
    # inspecting `start` alone cannot tell a date-only event from one that really
    # starts in the early morning.
    all_day: bool = False

    # True only when the source itself says this date replaces an earlier one.
    # Feeds the same cross-document reschedule path ingest.py uses; leave it
    # False unless the source is explicit, since a false positive collapses a
    # weekly recurring event into a single row.
    is_reschedule: bool = False

    # Raw upstream record, kept for debugging a bad parse. Never persisted.
    raw: dict | None = None

    @property
    def has_time(self) -> bool:
        """Did the source state a time of day, or only a date?

        Feeds the publish gate. A date-only event is real, but its hour is not
        known -- publishing it anyway puts a start time on the feed that nobody
        stated, so the runner routes it to review instead.
        """
        if self.start is None or self.all_day:
            return False
        return (self.start.hour, self.start.minute, self.start.second) != (0, 0, 0)


# CLAUDE.md: event-organizer contact details must never reach the frontend or an
# API response. SOP's event payloads carry students' personal addresses in an
# `organizer` block, so a naive field mapping publishes them. The existing
# defence sits at the response layer (schemas.EventLite) and in
# embeddings.event_to_document -- both of which a new writer path bypasses
# entirely. Scrubbing here means the address never lands in the column at all.
#
# Club contact emails are a separate, deliberate path (Club.links.email) and are
# not affected: this only runs over event text.
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# North American forms a poster or listing actually uses: 416-555-0199,
# (416) 555-0199, +1 416 555 0199, 416.555.0199.
_PHONE_RE = re.compile(
    r"(?:\+?1[\s.-]*)?(?:\(\d{3}\)|\d{3})[\s.-]*\d{3}[\s.-]*\d{4}\b"
)

_REDACTION = "[contact removed]"


def find_contacts(text: str | None) -> list[str]:
    """Every email address and phone number in `text`.

    Used by the validator to fail an adapter that leaks contact details, so it
    reports what it found rather than just that something was wrong.
    """
    if not text:
        return []
    return [m.group(0) for m in _EMAIL_RE.finditer(text)] + [
        m.group(0) for m in _PHONE_RE.finditer(text)
    ]


def scrub_contacts(text: str | None) -> str | None:
    """Replace emails and phone numbers with a redaction marker.

    A marker rather than deletion: "questions to [contact removed]" reads as a
    deliberate omission, while silently cutting the address leaves a dangling
    "questions to" that looks like a parsing bug.
    """
    if not text:
        return text
    return _PHONE_RE.sub(_REDACTION, _EMAIL_RE.sub(_REDACTION, text))
