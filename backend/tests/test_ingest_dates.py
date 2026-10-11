"""Same club, same event title, different dates -- correction vs recurring series.

These two look identical in the extracted data, so the pipeline separates them by
provenance: the same source document saying a new date supersedes itself, a
different document is a new occurrence. See ingest._find_existing_event.

No network: extraction, download and sharing are all stubbed.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import app.services.ingest as ingest
from app.models import Event
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


def _event(title: str, start: datetime, is_reschedule: bool = False) -> ExtractedEvent:
    return ExtractedEvent(
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


def test_corrected_poster_moves_the_date_instead_of_duplicating(session):
    """Same file re-uploaded with a fixed date: one row, new date."""
    _ingest(session, _file("poker.png", "id1", "h1"), [_event("Tournament Night", OCT16)])
    _ingest(session, _file("poker.png", "id1", "h2"), [_event("Tournament Night", OCT23)])

    rows = _events(session)
    assert len(rows) == 1
    assert rows[0].start == OCT23, "the stale date must not survive on the feed"


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


def test_a_correction_in_a_new_file_still_supersedes_when_the_poster_says_so(session):
    """The gap a plain provenance check can't close on its own.

    A club fixes a mistake by posting a brand new graphic rather than editing the
    old file -- same club, similar title, different file, different date. That is
    indistinguishable from a recurring weekly meetup by provenance alone. The
    poster saying "RESCHEDULED" (modelled here as is_reschedule=True, which is
    what extraction.py sets when the document itself uses that language) is what
    tells the pipeline to update the old row instead of creating a second one.
    """
    _ingest(session, _file("poker_v1.png", "id6", "h1"), [_event("Tournament Night", OCT16)])
    _ingest(
        session,
        _file("poker_corrected.png", "id7", "h2"),  # a genuinely different file
        [_event("Tournament Night", OCT23, is_reschedule=True)],
    )

    rows = _events(session)
    assert len(rows) == 1, "the reschedule signal should update the row, not add one"
    assert rows[0].start == OCT23
    assert rows[0].source_file == "poker_corrected.png"


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
