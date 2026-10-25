"""/review is gated by REVIEW_KEY and only acts on pending Dropbox events."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.database import get_session
from app.main import app
from app.models import Club, Event
from app.security import RateLimitMiddleware

KEY = {"X-Review-Key": "test-key"}


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
    with (
        patch.object(RateLimitMiddleware, "_exceeded", return_value=None),
        patch.object(settings, "review_key", "test-key"),
    ):
        yield TestClient(app)
    app.dependency_overrides.clear()


def _event(session, status="pending_review", source="dropbox", start=None) -> Event:
    club = Club(name="Board Games Society", outcomes=["friends"], tags=["x"])
    session.add(club)
    session.commit()
    event = Event(
        club_id=club.id, title="Games Night", status=status, source=source, start=start,
        confidence=0.4, dropbox_link="https://dropbox.example/poster",
    )
    session.add(event)
    session.commit()
    return event


def test_unset_key_hides_the_whole_router(client, session):
    with patch.object(settings, "review_key", ""):
        res = client.get("/api/review/pending", headers=KEY)
    assert res.status_code == 404


def test_wrong_or_missing_key_is_refused(client, session):
    _event(session)
    assert client.get("/api/review/pending").status_code == 401
    assert client.get("/api/review/pending", headers={"X-Review-Key": "nope"}).status_code == 401


def test_lists_only_pending_dropbox_events_in_campus_time(client, session):
    # 2026-10-31 23:00 UTC is 19:00 in Toronto (EDT, UTC-4).
    pending = _event(session, start=datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc))
    _event(session, status="published")
    _event(session, source="sop")
    res = client.get("/api/review/pending", headers=KEY)
    assert res.status_code == 200
    body = res.json()
    assert [e["id"] for e in body] == [pending.id]
    assert body[0]["club_name"] == "Board Games Society"
    assert body[0]["start_local"] == "2026-10-31T19:00"
    assert "confidence" not in body[0]


def test_publish_sets_the_typed_start(client, session):
    event = _event(session)
    res = client.post(
        f"/api/review/{event.id}/publish", json={"start": "2026-11-05T19:00"}, headers=KEY
    )
    assert res.status_code == 200
    # 2026-11-05 19:00 Toronto is EST (UTC-5), so 00:00 UTC the next day.
    assert res.json()["start"].startswith("2026-11-06T00:00:00")
    session.refresh(event)
    assert event.status == "published" and event.confidence == 1.0


def test_discard_hides_without_deleting(client, session):
    event = _event(session)
    assert client.post(f"/api/review/{event.id}/discard", headers=KEY).status_code == 204
    session.refresh(event)
    assert event.status == "discarded"
    assert client.get("/api/review/pending", headers=KEY).json() == []


def test_published_events_cannot_be_changed_here(client, session):
    event = _event(session, status="published")
    publish = client.post(
        f"/api/review/{event.id}/publish", json={"start": "2026-11-05T19:00"}, headers=KEY
    )
    assert publish.status_code == 409
    assert client.post(f"/api/review/{event.id}/discard", headers=KEY).status_code == 409
    session.refresh(event)
    assert event.status == "published"
