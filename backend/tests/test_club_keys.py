"""Club keys (D17): a club logged in with its own key may correct its own live events.

No network: extraction, download, sharing and the Dropbox upload are all stubbed.
"""

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.routers.upload as upload
import app.services.ingest as ingest
from app.club_keys import club_for_key, hash_key, issue_key
from app.database import get_session
from app.main import app
from app.models import Club, ClubKey, Event, IngestLog
from app.security import RateLimitMiddleware
from app.services.extraction import Extraction
from app.timeutil import utc_now
from tests.test_ingest_dates import OCT16, OCT23, _event, _file


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    with patch.object(RateLimitMiddleware, "_exceeded", return_value=None):
        yield TestClient(app)
    app.dependency_overrides.clear()


def _club(session, name="UofT Poker Club") -> Club:
    club = Club(name=name, outcomes=["friends"], tags=["x"])
    session.add(club)
    session.commit()
    return club


def _ingest(session, file, events, *, club_id=None, name_guess="UofT Poker Club"):
    extraction = Extraction(
        doc_type="poster", club_name_guess=name_guess, events=events, club_updates={},
        confidence=0.95, uncertainties=[], raw={},
    )
    with (
        patch.object(ingest, "download_file", return_value=b"x"),
        patch.object(ingest, "mime_type_for", return_value="image/png"),
        patch.object(ingest, "extract_file", return_value=extraction),
        patch.object(ingest, "get_shared_link", return_value="https://dropbox/x"),
    ):
        return ingest.process_file(session, file, force=True, club_id=club_id)


def _log_rows(session, status):
    rows = session.exec(select(IngestLog).where(IngestLog.status == status)).all()
    return [json.loads(r.result_json) for r in rows]


# --- the key itself -----------------------------------------------------------


def test_an_issued_key_logs_in_as_its_club_and_only_its_hash_is_stored(session):
    club = _club(session)
    key = issue_key(session, club, "exec 2026")

    assert club_for_key(session, key).id == club.id
    stored = session.exec(select(ClubKey)).one()
    assert stored.key_hash == hash_key(key) and key not in stored.key_hash


@pytest.mark.parametrize("key", [None, "", "ck_not-a-real-key", "no-prefix-at-all"])
def test_unknown_or_malformed_keys_log_in_as_nobody(session, key):
    issue_key(session, _club(session))
    assert club_for_key(session, key) is None


def test_a_revoked_key_stops_working_without_touching_other_keys(session):
    club = _club(session)
    old, new = issue_key(session, club), issue_key(session, club)
    row = session.exec(select(ClubKey).where(ClubKey.key_hash == hash_key(old))).one()
    row.revoked_at = utc_now()
    session.add(row)
    session.commit()

    assert club_for_key(session, old) is None
    assert club_for_key(session, new).id == club.id


# --- what a logged-in upload may do -------------------------------------------


def test_the_clubs_own_correction_moves_its_live_event_and_keeps_the_old_facts(session):
    club = _club(session)
    _ingest(session, _file("poker.png", "id1", "h1"), [_event("Tournament Night", OCT16)])
    result = _ingest(
        session, _file("poker.png", "id1", "h2"), [_event("Tournament Night", OCT23)],
        club_id=club.id,
    )

    event = session.exec(select(Event)).one()
    assert event.start == OCT23 and event.status == "published"
    assert result.updated == 1 and result.held == 0
    assert not _log_rows(session, "held_change")
    previous = _log_rows(session, "processed")[-1]["owner_updates"][0]
    assert previous["event_id"] == event.id
    assert previous["previous"]["start"].startswith("2026-10-16")


def test_without_the_key_the_same_correction_is_still_held(session):
    _club(session)
    _ingest(session, _file("poker.png", "id1", "h1"), [_event("Tournament Night", OCT16)])
    result = _ingest(session, _file("poker.png", "id1", "h2"), [_event("Tournament Night", OCT23)])

    assert session.exec(select(Event)).one().start == OCT16
    assert result.held == 1 and result.updated == 0


def test_a_shaky_read_is_held_even_from_the_club(session):
    """The key proves who sent the poster, not that the date was read right."""
    club = _club(session)
    _ingest(session, _file("poker.png", "id1", "h1"), [_event("Tournament Night", OCT16)])
    shaky = _event("Tournament Night", OCT23, status="pending_review")
    result = _ingest(session, _file("poker.png", "id1", "h2"), [shaky], club_id=club.id)

    assert session.exec(select(Event)).one().start == OCT16
    assert result.held == 1 and result.updated == 0


def test_a_logged_in_upload_is_filed_under_the_keys_club_whatever_the_poster_says(session):
    cssu = _club(session, "Computer Science Student Union (CSSU)")
    result = _ingest(
        session, _file("panel.png", "id9", "h1"), [_event("Fumbles & Failures", OCT16)],
        club_id=cssu.id, name_guess="WiCS x CSSU",
    )

    assert result.club_id == cssu.id and not result.club_created
    assert len(session.exec(select(Club)).all()) == 1, "no placeholder club"


# --- the routes ---------------------------------------------------------------


def test_me_needs_a_valid_key(client, session):
    issue_key(session, _club(session))
    assert client.get("/api/club/me").status_code == 401
    assert client.get("/api/club/me", headers={"X-Club-Key": "ck_wrong"}).status_code == 401


def test_me_lists_only_this_clubs_events_with_their_status(client, session):
    mine, other = _club(session), _club(session, "Chess Club")
    for club_id, title, status in [
        (mine.id, "Tournament Night", "published"),
        (mine.id, "Poker 101", "pending_review"),
        (mine.id, "Old Poster", "discarded"),
        (other.id, "Blitz Night", "published"),
    ]:
        session.add(Event(club_id=club_id, title=title, status=status, source="dropbox"))
    session.commit()
    key = issue_key(session, mine)

    body = client.get("/api/club/me", headers={"X-Club-Key": key}).json()
    assert body["name"] == "UofT Poker Club"
    assert {(e["title"], e["status"]) for e in body["events"]} == {
        ("Tournament Night", "published"),
        ("Poker 101", "pending_review"),
    }
    assert "confidence" not in body["events"][0]


def test_upload_with_a_bad_key_never_reaches_the_pipeline(client, session):
    with patch.object(upload, "_store_and_process") as pipeline:
        res = client.post(
            "/api/club/upload",
            headers={"X-Club-Key": "ck_wrong"},
            files={"file": ("poster.png", b"png", "image/png")},
        )
    assert res.status_code == 401
    pipeline.assert_not_called()


def test_upload_with_a_good_key_runs_the_pipeline_as_that_club(client, session):
    club = _club(session)
    key = issue_key(session, club)
    result = ingest.IngestResult(file=_file("poster.png", "id1", "h1"), status="no_events")
    with patch.object(upload, "_store_and_process", return_value=result) as pipeline:
        res = client.post(
            "/api/club/upload",
            headers={"X-Club-Key": key},
            files={"file": ("poster.png", b"png", "image/png")},
        )
    assert res.status_code == 200
    assert pipeline.call_args.args[2] == club.id
