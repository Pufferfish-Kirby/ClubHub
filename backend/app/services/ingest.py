"""One file in -> Event rows out. The pipeline both entrances converge on.

A file can arrive two ways: a club drops a poster in the shared Dropbox folder,
or a user uploads a flier in the app. Both call process_file(). Keeping that
single is what stops the date-safety rule from existing in two places and
drifting apart.

    InboxFile
      -> IngestLog check      (have we already done this file?)
      -> download_file        (dropbox_store)
      -> extract_file         (extraction -- Claude reads it)
      -> get_or_create_club   (matcher -- never guesses)
      -> get_shared_link      (dropbox_store -- provenance)
      -> Event rows + IngestLog

Synchronous, like the modules it calls. Async callers wrap it in
asyncio.to_thread; running it on the event loop would stall every request for
the several seconds extraction takes.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from rapidfuzz import fuzz
from sqlmodel import Session, select

from app.models import Event, IngestLog
from app.services.dropbox_store import InboxFile, download_file, get_shared_link, mime_type_for
from app.services.extraction import ExtractedEvent, ExtractionError, extract_file
from app.services.matcher import get_or_create_club


@dataclass(frozen=True)
class IngestedEvent:
    """A written Event, as plain data."""

    id: str
    title: str
    start: datetime | None  # aware UTC
    location: str | None
    status: str
    dropbox_link: str | None
    # Set only when status is pending_review: which field (date, or just time) a human needs to supply
    # before this event can publish, and the model's raw local-time read (for pre-filling that prompt).
    # See extraction.missing_fields -- the upload endpoint surfaces these so the person who just
    # uploaded the file can confirm it immediately instead of the event sitting unreachable forever.
    missing: list[str] = field(default_factory=list)
    start_local: str | None = None


@dataclass(frozen=True)
class IngestResult:
    """What happened to one file. The watcher and the upload endpoint both use this.

    Deliberately plain data, not ORM objects. process_file() commits and closes
    its session, which expires every attribute on the Club and Event instances it
    wrote -- reading `club.name` afterwards raises DetachedInstanceError, because
    SQLAlchemy tries to reload from a session that no longer exists. Snapshotting
    the fields while the session is open removes that trap for every caller
    instead of making each one remember it.
    """

    file: InboxFile
    status: str  # processed | duplicate | unsupported | no_events | failed
    events: list[IngestedEvent] = field(default_factory=list)
    club_id: str | None = None
    club_name: str | None = None
    club_created: bool = False
    confidence: float | None = None
    uncertainties: list[str] = field(default_factory=list)
    doc_type: str | None = None
    error: str | None = None

    @property
    def published_count(self) -> int:
        return sum(1 for e in self.events if e.status == "published")


def already_ingested(session: Session, file: InboxFile) -> bool:
    """Have we processed this exact file content before?

    Keyed on (dropbox_file_id, content_hash) so an *edited* file reprocesses --
    a club fixing a date on their poster should update the app -- while the same
    bytes seen on every 5-second poll do not. Without this the watcher re-sends
    every poster to Claude forever.

    content_hash is nullable in Dropbox's API; rev is the fallback, since it also
    changes on edit.
    """
    fingerprint = file.content_hash or file.rev
    existing = session.exec(
        select(IngestLog).where(
            IngestLog.dropbox_file_id == file.id,
            IngestLog.content_hash == fingerprint,
        )
    ).first()
    return existing is not None


def _find_existing_event(
    session: Session,
    club_id: str,
    title: str,
    start,
    source_file: str | None = None,
    exclude: set[str] | None = None,
    is_reschedule: bool = False,
) -> Event | None:
    """The stored row this extracted event should update, or None to create one.

    Three things count as "the same event":

    1. **Same club, same start, near-identical title.** The ordinary case: the
       same poster read twice. Titles are compared fuzzily because the model does
       not transcribe punctuation identically across runs -- one poster produced
       "Fall Hiking Trip - Rattlesnake Point" and "Fall Hiking Trip: Rattlesnake
       Point" on two passes. Two genuinely different events from one club
       starting the same minute is implausible, so club+start is a safe key, and
       the similarity floor stops unrelated titles merging.

    2. **Same club, same source document, near-identical title, DIFFERENT
       start.** A club correcting a date by re-uploading the same file: it was
       edited, its content hash changed, we re-read it, and it now says a
       different day. Without this the old date stays on the live feed forever
       beside the new one.

    3. **Same club, near-identical title, DIFFERENT file, but the poster
       explicitly says it's a reschedule.** A club can also fix a mistake by
       posting a brand new image rather than editing the old one -- provenance
       alone can't tell that apart from next week's ordinary meetup, since both
       look like "same club, similar title, different file, different date."
       The deciding signal is textual: extraction.py asks Claude whether the
       document itself uses reschedule language ("RESCHEDULED", "NEW DATE",
       "corrected to..."), the same way a person reading the poster would know.
       Only when that's true do we search across files at all -- an ordinary
       recurring announcement with no such language still gets its own row.

    Provenance (tiers 1-2) is the default; the explicit signal (tier 3) is the
    deliberately narrow exception, because a false positive there means the
    weekly-meetup case above wrongly collapses into one row every week.

    `exclude` holds rows already claimed earlier in this same extraction, so a
    poster that legitimately lists one event on two dates ("Auditions Oct 3 and
    Oct 5") writes two rows rather than the second overwriting the first.
    """
    exclude = exclude or set()

    def matches(candidate: Event) -> bool:
        return (
            candidate.id not in exclude
            and fuzz.token_set_ratio(_title_key(title), _title_key(candidate.title)) >= 85
        )

    if start is None:
        # No time to key on -- fall back to an exact title match within the club.
        return next(
            (
                c
                for c in session.exec(
                    select(Event).where(
                        Event.club_id == club_id,
                        Event.title == title,
                        Event.start.is_(None),
                    )
                ).all()
                if c.id not in exclude
            ),
            None,
        )

    same_slot = session.exec(
        select(Event).where(Event.club_id == club_id, Event.start == start)
    ).all()
    for candidate in same_slot:
        if matches(candidate):
            return candidate

    if source_file:
        same_document = session.exec(
            select(Event).where(
                Event.club_id == club_id, Event.source_file == source_file
            )
        ).all()
        for candidate in same_document:
            if matches(candidate):
                return candidate

    if is_reschedule:
        # Only reached when the document itself said this is a correction. Search
        # every event for this club regardless of file or date -- take the best
        # title match rather than the first, since a club can have more than one
        # near-namesake event ("Weekly Practice" vs "Weekly Practice: Finals").
        all_club_events = session.exec(select(Event).where(Event.club_id == club_id)).all()
        scored = [
            (fuzz.token_set_ratio(_title_key(title), _title_key(c.title)), c)
            for c in all_club_events
            if c.id not in exclude
        ]
        scored = [(score, c) for score, c in scored if score >= 85]
        if scored:
            return max(scored, key=lambda pair: pair[0])[1]

    return None


def _title_key(title: str) -> str:
    """Lowercase, punctuation-free form used only for comparing two titles."""
    return " ".join(re.sub(r"[^\w\s]", " ", title.lower()).split())


def _log(session: Session, file: InboxFile, status: str, payload: dict) -> None:
    session.add(
        IngestLog(
            dropbox_file_id=file.id,
            content_hash=file.content_hash or file.rev,
            status=status,
            result_json=json.dumps(payload, default=str),
        )
    )


def process_file(session: Session, file: InboxFile, *, force: bool = False) -> IngestResult:
    """Run one Dropbox file through the whole pipeline and write the rows.

    `force` re-processes a file we've already seen -- useful when iterating on the
    prompt, and for re-running the demo without editing the poster.

    Commits on success. On a permanent failure it records an IngestLog row so the
    file isn't retried on every poll; transient failures (network, rate limits)
    are deliberately allowed to propagate *without* a log entry, so the next poll
    picks them up again.
    """
    if not force and already_ingested(session, file):
        return IngestResult(file=file, status="duplicate")

    mime_type = mime_type_for(file.name)
    if mime_type is None:
        _log(session, file, "unsupported", {"name": file.name})
        session.commit()
        return IngestResult(file=file, status="unsupported")

    try:
        extraction = extract_file(download_file(file.path), mime_type)
    except ExtractionError as e:
        # Deterministic failure (refusal, truncation, no tool call): retrying the
        # same bytes gets the same answer, so log it and stop.
        _log(session, file, "failed", {"name": file.name, "error": str(e)})
        session.commit()
        return IngestResult(file=file, status="failed", error=str(e))

    if not extraction.events:
        # A roster, a logo, a photo of a whiteboard. Don't create a placeholder
        # club for a document with nothing to attribute to it.
        _log(
            session,
            file,
            "no_events",
            {"name": file.name, "doc_type": extraction.doc_type},
        )
        session.commit()
        return IngestResult(
            file=file,
            status="no_events",
            doc_type=extraction.doc_type,
            confidence=extraction.confidence,
            uncertainties=extraction.uncertainties,
        )

    club, club_created = get_or_create_club(session, extraction.club_name_guess or file.name)

    # Provenance: the link behind the "from Dropbox" badge. An extracted event is
    # only as trustworthy as the ability to click through to the source, so a
    # failure here shouldn't lose the event -- fall back to no link.
    try:
        dropbox_link = get_shared_link(file.path)
    except Exception:
        dropbox_link = None

    written: list[Event] = []
    written_items: list[ExtractedEvent] = []  # parallel to `written`, for missing/start_local in the snapshot
    claimed: set[str] = set()
    reschedules: list[str] = []
    for item in extraction.events:
        existing = _find_existing_event(
            session,
            club.id,
            item.title,
            item.start,
            file.name,
            claimed,
            item.is_reschedule,
        )
        if existing is not None and item.is_reschedule and existing.source_file != file.name:
            reschedules.append(
                f"{item.title!r}: {existing.start} -> {item.start} "
                f"(was {existing.source_file}, now {file.name})"
            )
        event = existing or Event(club_id=club.id, title=item.title)

        # Set on update too: a corrected poster may have reworded the title.
        event.title = item.title
        event.start = item.start
        event.end = item.end
        event.location = item.location
        event.description = item.description
        event.rsvp_url = item.rsvp_url
        event.source = "dropbox"
        event.source_file = file.name
        event.dropbox_link = dropbox_link
        event.status = item.status
        event.confidence = extraction.confidence

        session.add(event)
        session.flush()  # assign the id before we snapshot it
        claimed.add(event.id)
        written.append(event)
        written_items.append(item)

    _log(
        session,
        file,
        "processed",
        {
            "name": file.name,
            "club": club.name,
            "club_created": club_created,
            "events": [e.title for e in written],
            "confidence": extraction.confidence,
            "uncertainties": extraction.uncertainties,
            "reschedules": reschedules,
        },
    )

    # Snapshot before commit expires the instances.
    snapshot = IngestResult(
        file=file,
        status="processed",
        events=[
            IngestedEvent(
                id=e.id,
                title=e.title,
                start=e.start,
                location=e.location,
                status=e.status,
                dropbox_link=e.dropbox_link,
                missing=item.missing,
                start_local=item.start_local,
            )
            for e, item in zip(written, written_items)
        ],
        club_id=club.id,
        club_name=club.name,
        club_created=club_created,
        confidence=extraction.confidence,
        uncertainties=extraction.uncertainties,
        doc_type=extraction.doc_type,
    )
    session.commit()
    return snapshot


def process_inbox(session: Session, *, force: bool = False) -> list[IngestResult]:
    """Run every supported file currently in the Dropbox inbox."""
    from app.services.dropbox_store import list_inbox

    return [process_file(session, f, force=force) for f in list_inbox()]


def _smoke_test() -> None:
    """Run the real posters end to end and print what landed in the database.

    WRITES TO THE DATABASE and makes one Claude API call per file. Re-running is
    safe: the IngestLog check skips files already processed, and re-processing
    with force=True updates existing events rather than duplicating them.
    """
    import sys

    from app.database import engine, init_db
    from app.models import Club
    from app.services.event_sources.base import safe_console

    safe_console()
    force = "--force" in sys.argv
    init_db()

    with Session(engine) as session:
        results = process_inbox(session, force=force)

        if not results:
            print("No supported files in the Dropbox inbox.")
            return

        for r in results:
            print("=" * 70)
            print(f"{r.file.name}  ->  {r.status}")
            if r.status == "duplicate":
                print("   already ingested (re-run with --force to redo)")
                continue
            if r.error:
                print(f"   {r.error}")
                continue
            if r.club_name:
                tag = "CREATED placeholder" if r.club_created else "matched existing"
                print(f"   club: {r.club_name}  [{tag}]")
            if r.confidence is not None:
                print(f"   confidence: {r.confidence}  doc_type={r.doc_type}")
            for e in r.events:
                print(f"   event: {e.title}")
                print(f"          start={e.start} (UTC)  status={e.status}")
                print(f"          link={(e.dropbox_link or '(none)')[:60]}")
            for u in r.uncertainties:
                print(f"   uncertainty: {u}")
            print()

        clubs = session.exec(select(Club)).all()
        events = session.exec(select(Event)).all()
        print("=" * 70)
        print(f"DB now holds {len(clubs)} clubs, {len(events)} events "
              f"({sum(1 for e in events if e.status == 'published')} published)")


if __name__ == "__main__":
    _smoke_test()
