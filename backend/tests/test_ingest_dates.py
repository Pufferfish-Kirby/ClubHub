"""Same club, same event title, different dates -- correction vs recurring series.

These two look identical in the extracted data, so the pipeline separates them by
provenance: the same source document saying a new date supersedes itself, a
different document is a new occurrence. See ingest._find_existing_event.

No network: extraction, download and sharing are all stubbed.
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import app.services.ingest as ingest
from app.models import Event, IngestLog
from app.services.dropbox_store import InboxFile
from app.services.extraction import ExtractedEvent, Extraction

OCT16 = datetime(2026, 10, 16, 23, 30, tzinfo=timezone.utc)
OCT23 = datetime(2026, 10, 23, 23, 30, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _file(name: str, file_id: str, content_hash: str) -> InboxFile:
    return InboxFile(
        id=file_id,
        name=name,
        path=f"/inbox/{name}",
        content_hash=content_hash,
        rev=content_hash,
        size=1,
        server_modified=datetime(2026, 9, 19),
    )


def _event(title: str, start: datetime, is_reschedule: bool = False, **overrides) -> ExtractedEvent:
    fields = dict(
        title=title,
        start=start,
        end=None,
        location=None,
        description=None,
        rsvp_url=None,
        status="published",
        start_local=None,
        is_reschedule=is_reschedule,
    )
    return ExtractedEvent(**{**fields, **overrides})


def _ingest(session, file: InboxFile, events: list[ExtractedEvent]):
    extraction = Extraction(
        doc_type="poster",
        club_name_guess="UofT Poker Club",
        events=events,
        club_updates={},
        confidence=0.95,
        uncertainties=[],
        raw={},
    )
    with (
        patch.object(ingest, "download_file", return_value=b"x"),
        patch.object(ingest, "mime_type_for", return_value="image/png"),
        patch.object(ingest, "extract_file", return_value=extraction),
        patch.object(ingest, "get_shared_link", return_value="https://dropbox/x"),
    ):
        return ingest.process_file(session, file, force=True)


def _events(session) -> list[Event]:
    return session.exec(select(Event)).all()


def _held(session) -> list[dict]:
    rows = session.exec(select(IngestLog).where(IngestLog.status == "held_change")).all()
    return [json.loads(r.result_json) for r in rows]


def test_edited_poster_holds_a_new_date_instead_of_duplicating(session):
    """Same file re-uploaded with a different date: still one row, and since that row is
    live, the new date waits for a person rather than replacing it (see _changes_live_facts)."""
    _ingest(session, _file("poker.png", "id1", "h1"), [_event("Tournament Night", OCT16)])
    result = _ingest(session, _file("poker.png", "id1", "h2"), [_event("Tournament Night", OCT23)])

    rows = _events(session)
    assert len(rows) == 1
    assert rows[0].start == OCT16, "an unreviewed poster must not move a live date"
    assert result.held == 1


def test_a_different_poster_is_a_new_occurrence(session):
    """Next week's flier for a recurring event keeps both sessions."""
    _ingest(session, _file("wk1.png", "id2", "h1"), [_event("Tournament Night", OCT16)])
    _ingest(session, _file("wk2.png", "id3", "h2"), [_event("Tournament Night", OCT23)])

    assert {e.start for e in _events(session)} == {OCT16, OCT23}


def test_one_poster_listing_two_dates_writes_two_rows(session):
    """"Auditions Oct 16 and Oct 23" must not collapse into one row."""
    _ingest(
        session,
        _file("auditions.png", "id4", "h1"),
        [_event("Auditions", OCT16), _event("Auditions", OCT23)],
    )

    assert {e.start for e in _events(session)} == {OCT16, OCT23}


def test_unchanged_poster_is_idempotent(session):
    _ingest(session, _file("poker.png", "id5", "h1"), [_event("Tournament Night", OCT16)])
    _ingest(session, _file("poker.png", "id5", "h1"), [_event("Tournament Night", OCT16)])

    assert len(_events(session)) == 1


def test_a_correction_in_a_new_file_is_matched_and_held_when_the_poster_says_so(session):
    """The gap a plain provenance check can't close on its own.

    A club fixes a mistake by posting a brand new graphic rather than editing the
    old file -- same club, similar title, different file, different date. That is
    indistinguishable from a recurring weekly meetup by provenance alone. The
    poster saying "RESCHEDULED" (modelled here as is_reschedule=True, which is
    what extraction.py sets when the document itself uses that language) is what
    ties it to the old row instead of creating a second one. The old row is live,
    so the new date is held for review: "RESCHEDULED" is easy to misread or fake.
    """
    _ingest(session, _file("poker_v1.png", "id6", "h1"), [_event("Tournament Night", OCT16)])
    result = _ingest(
        session,
        _file("poker_corrected.png", "id7", "h2"),  # a genuinely different file
        [_event("Tournament Night", OCT23, is_reschedule=True)],
    )

    rows = _events(session)
    assert len(rows) == 1, "the reschedule signal should match the row, not add one"
    assert rows[0].start == OCT16 and rows[0].source_file == "poker_v1.png"
    held = _held(session)
    assert len(held) == 1 and result.held == 1
    assert held[0]["event_id"] == rows[0].id
    assert held[0]["proposed"]["start"].startswith("2026-10-23")
    assert held[0]["is_reschedule"] is True


def test_without_the_reschedule_signal_a_new_file_is_still_a_new_occurrence(session):
    """The narrow-exception guarantee: no explicit signal means tier-2 behaviour
    is unchanged, so an ordinary recurring event is never silently collapsed."""
    _ingest(session, _file("wk1.png", "id8", "h1"), [_event("Tournament Night", OCT16)])
    _ingest(
        session,
        _file("wk2.png", "id9", "h2"),
        [_event("Tournament Night", OCT23, is_reschedule=False)],
    )

    assert {e.start for e in _events(session)} == {OCT16, OCT23}


def test_reschedule_with_no_prior_event_just_creates_one(session):
    """A false is_reschedule flag with nothing to supersede shouldn't crash or
    attach itself to an unrelated event -- it's simply a new event."""
    _ingest(
        session,
        _file("poker.png", "id10", "h1"),
        [_event("Tournament Night", OCT16, is_reschedule=True)],
    )

    rows = _events(session)
    assert len(rows) == 1
    assert rows[0].start == OCT16


def test_a_pending_event_still_takes_the_new_date(session):
    """Holding protects what students can already see. A row still in pending_review is
    not live, so a newer poster may fill it in as before."""
    first = _event("Tournament Night", OCT16, status="pending_review")
    _ingest(session, _file("poker.png", "id11", "h1"), [first])
    _ingest(session, _file("poker.png", "id11", "h2"), [_event("Tournament Night", OCT23)])

    rows = _events(session)
    assert len(rows) == 1 and rows[0].start == OCT23 and rows[0].status == "published"
    assert _held(session) == []


def test_a_blurrier_copy_cannot_unpublish_a_live_event(session):
    """Same date, but this read came back low-confidence: the event stays on the feed."""
    _ingest(session, _file("poker.png", "id12", "h1"), [_event("Tournament Night", OCT16)])
    blurry = _event("Tournament Night", OCT16, status="pending_review")
    _ingest(session, _file("poker.png", "id12", "h2"), [blurry])

    rows = _events(session)
    assert len(rows) == 1 and rows[0].status == "published"


def test_a_poster_without_a_location_does_not_erase_it(session):
    first = _event("Tournament Night", OCT16, location="Hart House")
    _ingest(session, _file("poker.png", "id13", "h1"), [first])
    _ingest(session, _file("poker.png", "id13", "h2"), [_event("Tournament Night", OCT16)])

    rows = _events(session)
    assert rows[0].location == "Hart House"
    assert _held(session) == []


def test_a_new_location_on_a_live_event_is_held(session):
    first = _event("Tournament Night", OCT16, location="Hart House")
    _ingest(session, _file("poker.png", "id14", "h1"), [first])
    moved = _event("Tournament Night", OCT16, location="Sidney Smith 2117")
    _ingest(session, _file("poker.png", "id14", "h2"), [moved])

    assert _events(session)[0].location == "Hart House"
    assert _held(session)[0]["proposed"]["location"] == "Sidney Smith 2117"
