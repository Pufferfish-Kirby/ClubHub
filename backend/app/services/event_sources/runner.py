"""Turn an adapter's ScrapedEvents into Event rows, applying every safety rule.

Mirrors ingest.process_file() for the scraped path: same club matching, same
publish gate, same dedupe-then-update shape. The differences are all consequences
of the key -- a Dropbox file has an id and a content hash, a scraped record has an
upstream id or a URL -- plus two rules the file path gets for free and this one
does not:

  * **Future only.** A poster in the Dropbox inbox is something a club just put
    there. A listing API hands back its whole history, so an unfiltered adapter
    fills the feed with last year's events. Filtering here rather than in each
    adapter means a source with no server-side date parameter is still safe.

  * **Contact scrubbing.** SOP's event payloads carry students' personal emails
    in an `organizer` block (CLAUDE.md: never display event-organizer emails).
    The existing defences are at the response layer and in the embedding text,
    both of which a new writer bypasses -- so text is scrubbed before it reaches
    a column.

Run an adapter with `python -m app.services.event_sources.runner <source>`.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from importlib import import_module

from rapidfuzz import fuzz
from sqlmodel import Session, select

from app.models import Event, IngestLog
from app.services.event_sources.base import (
    ScrapedEvent,
    safe_console,
    scrub_contacts,
)
from app.services.extraction import decide_status
from app.services.matcher import get_or_create_club
from app.timeutil import utc_now

# Adapters live in this package and are referred to by module name.
_ADAPTER_PACKAGE = "app.services.event_sources"

# An event further out than this is a parse error, not a real listing. Club
# calendars run a term or two ahead; a 2027 date on a page written in 2026 is
# almost always a two-digit year misread or a template placeholder. CLAUDE.md's
# rule is to push ambiguity into review rather than guess, so these are dropped
# from the publish path instead of being trusted.
MAX_HORIZON_DAYS = 550  # ~18 months

# How long after an event starts it still counts as current. An event that began
# an hour ago is still worth showing someone looking at tonight; one that started
# yesterday is not.
GRACE_PERIOD = timedelta(hours=3)

# Same floor ingest.py uses for "these two titles are the same event".
_TITLE_MATCH_THRESHOLD = 85


@dataclass
class RunResult:
    """What one adapter run did, for the CLI and for the validator."""

    source: str
    fetched: int = 0
    written: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped_past: list[str] = field(default_factory=list)
    skipped_horizon: list[str] = field(default_factory=list)
    skipped_duplicate: list[str] = field(default_factory=list)
    scrubbed: list[str] = field(default_factory=list)
    clubs_created: list[str] = field(default_factory=list)
    pending_review: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.source}: fetched {self.fetched}, "
            f"wrote {len(self.written)}, updated {len(self.updated)}, "
            f"skipped {len(self.skipped_past)} past / "
            f"{len(self.skipped_horizon)} beyond horizon / "
            f"{len(self.skipped_duplicate)} already seen, "
            f"{len(self.pending_review)} need review"
        )


def load_adapter(name: str):
    """Import an adapter module by slug and check it satisfies the contract.

    Failing here with a clear message beats an AttributeError deep inside the
    run, which is what a half-written adapter would otherwise produce.
    """
    try:
        module = import_module(f"{_ADAPTER_PACKAGE}.{name}")
    except ModuleNotFoundError as exc:
        raise SystemExit(f"No adapter named {name!r} in {_ADAPTER_PACKAGE}/") from exc

    missing = [attr for attr in ("SOURCE_NAME", "fetch") if not hasattr(module, attr)]
    if missing:
        raise SystemExit(
            f"Adapter {name!r} is missing {', '.join(missing)} -- see base.py for the contract"
        )
    return module


def is_current(event: ScrapedEvent, now_utc: datetime) -> bool:
    """Has this event not already happened?

    An event with no start is *not* filtered out: the club and title are still
    real information, and decide_status() sends it to review rather than the
    feed. Dropping it would lose a genuine event because one field was missing.
    """
    if event.start is None:
        return True
    return event.start >= now_utc - GRACE_PERIOD


def within_horizon(event: ScrapedEvent, now_utc: datetime) -> bool:
    if event.start is None:
        return True
    return event.start <= now_utc + timedelta(days=MAX_HORIZON_DAYS)


def scrub(event: ScrapedEvent) -> list[str]:
    """Strip contact details from an event's free text, in place.

    Returns the fields that were changed so the caller can log that a source
    leaks contact information -- an adapter that trips this repeatedly is mapping
    an organizer block it should not be reading at all.
    """
    changed = []
    for attr in ("title", "description", "location"):
        original = getattr(event, attr)
        cleaned = scrub_contacts(original)
        if cleaned != original:
            setattr(event, attr, cleaned)
            changed.append(attr)
    return changed


def _dedupe_key(event: ScrapedEvent) -> str | None:
    return event.external_id or event.source_url


def already_ingested(session: Session, source_name: str, key: str | None) -> bool:
    """Has this exact upstream record been processed before?

    An adapter that supplies neither an external id nor a URL gets no dedupe
    protection here; _find_existing_event() below is then the only thing stopping
    duplicate rows, and it will usually catch it on club + start + title.
    """
    if not key:
        return False
    return (
        session.exec(
            select(IngestLog).where(
                IngestLog.source_name == source_name,
                IngestLog.source_key == key,
            )
        ).first()
        is not None
    )


def _title_key(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", title.lower()).split())


def _find_existing_event(
    session: Session,
    club_id: str,
    event: ScrapedEvent,
    claimed: set[str],
) -> Event | None:
    """The stored row this scraped event should update, rather than duplicate.

    Narrower than ingest.py's version by design. Upstream records carry stable
    ids, so the ordinary repeat is caught by the IngestLog check before we get
    here; this exists for the case where the same event reaches us from two
    sources, or an upstream id changed. Matching on club + start + fuzzy title is
    the safe subset -- two different events from one club starting the same
    minute is implausible.
    """
    if event.start is None:
        return next(
            (
                row
                for row in session.exec(
                    select(Event).where(
                        Event.club_id == club_id,
                        Event.title == event.title,
                        Event.start.is_(None),
                    )
                ).all()
                if row.id not in claimed
            ),
            None,
        )

    candidates = session.exec(
        select(Event).where(Event.club_id == club_id, Event.start == event.start)
    ).all()
    for candidate in candidates:
        if candidate.id in claimed:
            continue
        if (
            fuzz.token_set_ratio(_title_key(event.title), _title_key(candidate.title))
            >= _TITLE_MATCH_THRESHOLD
        ):
            return candidate

    if event.is_reschedule:
        # The source said this date replaces an earlier one, so look past the
        # start time. Best title match rather than first, since a club can have
        # several near-namesake events.
        scored = [
            (fuzz.token_set_ratio(_title_key(event.title), _title_key(row.title)), row)
            for row in session.exec(select(Event).where(Event.club_id == club_id)).all()
            if row.id not in claimed
        ]
        hits = [pair for pair in scored if pair[0] >= _TITLE_MATCH_THRESHOLD]
        if hits:
            return max(hits, key=lambda pair: pair[0])[1]

    return None


def stage(
    events: list[ScrapedEvent],
    result: RunResult,
    now_utc: datetime | None = None,
) -> list[ScrapedEvent]:
    """Apply the date filters and the contact scrub; return what should be written.

    Pure apart from mutating `events` and `result`, so the validator can run it
    against a live adapter without a database or a commit.
    """
    if now_utc is None:
        now_utc = utc_now()

    keep: list[ScrapedEvent] = []
    for event in events:
        if not is_current(event, now_utc):
            result.skipped_past.append(f"{event.title} ({event.start})")
            continue
        if not within_horizon(event, now_utc):
            result.skipped_horizon.append(f"{event.title} ({event.start})")
            continue
        changed = scrub(event)
        if changed:
            result.scrubbed.append(f"{event.title} ({', '.join(changed)})")
        keep.append(event)
    return keep


def run_source(
    session: Session,
    source: str,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    now_utc: datetime | None = None,
) -> RunResult:
    """Fetch one source and write its future events. Commits unless dry_run."""
    module = load_adapter(source)
    source_name = module.SOURCE_NAME

    fetched = module.fetch(limit=limit)
    result = RunResult(source=source_name, fetched=len(fetched))

    staged = stage(fetched, result, now_utc)

    claimed: set[str] = set()
    for event in staged:
        key = _dedupe_key(event)
        if already_ingested(session, source_name, key):
            result.skipped_duplicate.append(event.title)
            continue

        club, created = get_or_create_club(session, event.club_name_guess)
        if created:
            result.clubs_created.append(club.name)

        status = decide_status(
            event.title, event.start, event.confidence, event.has_time
        )

        existing = _find_existing_event(session, club.id, event, claimed)
        row = existing or Event(club_id=club.id, title=event.title)

        # Set on update too: an upstream correction may have reworded the title.
        row.title = event.title
        row.start = event.start
        row.end = event.end
        row.location = event.location
        row.description = event.description
        row.rsvp_url = event.rsvp_url
        row.source = source_name
        row.source_file = event.source_url
        row.status = status
        row.confidence = event.confidence

        session.add(row)
        session.flush()
        claimed.add(row.id)

        (result.updated if existing else result.written).append(event.title)
        if status == "pending_review":
            result.pending_review.append(event.title)

        if key:
            session.add(
                IngestLog(
                    # Legacy column, NOT NULL: the scraped path has no Dropbox
                    # file, so it carries the same key for a consistent row.
                    dropbox_file_id=key,
                    source_name=source_name,
                    source_key=key,
                    status="processed",
                    result_json=json.dumps(
                        {
                            "title": event.title,
                            "club": club.name,
                            "start": event.start,
                            "status": status,
                            "url": event.source_url,
                        },
                        default=str,
                    ),
                )
            )

    if dry_run:
        session.rollback()
    else:
        session.commit()
    return result


def main() -> None:
    safe_console()
    parser = argparse.ArgumentParser(
        description="Fetch events from one source adapter into SQLite"
    )
    parser.add_argument("source", help="Adapter module name, e.g. sop_events")
    parser.add_argument("--limit", type=int, default=None, help="Max events to fetch")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and stage but roll back instead of committing",
    )
    args = parser.parse_args()

    from app.database import engine, init_db

    init_db()
    with Session(engine) as session:
        result = run_source(
            session, args.source, limit=args.limit, dry_run=args.dry_run
        )

    print(result.summary())
    for label, items in (
        ("wrote", result.written),
        ("updated", result.updated),
        ("needs review", result.pending_review),
        ("clubs created", result.clubs_created),
        ("scrubbed contacts", result.scrubbed),
        ("skipped (past)", result.skipped_past),
        ("skipped (beyond horizon)", result.skipped_horizon),
    ):
        if items:
            print(f"\n{label}:")
            for item in items:
                print(f"  {item}")
    if args.dry_run:
        print("\n[dry run] rolled back -- nothing written")


if __name__ == "__main__":
    main()
