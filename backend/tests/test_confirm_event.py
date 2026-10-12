"""POST /events/{id}/confirm is gated by a token and only works on pending events."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.confirm_token import make_confirm_token
from app.database import get_session
from app.main import app
from app.models import Club, Event
from app.security import RateLimitMiddleware


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


def _event(session, status: str) -> Event:
    club = Club(name="Club", outcomes=["friends"], tags=["x"])
    session.add(club)
    session.commit()
    event = Event(club_id=club.id, title="Meetup", status=status, source="dropbox", confidence=0.4)
    session.add(event)
    session.commit()
    return event


def _confirm(client, event_id, token):
    return client.post(
        f"/api/events/{event_id}/confirm", json={"start": "2026-11-05T19:00:00", "token": token}
    )


def test_valid_token_publishes_and_hides_internal_fields(client, session):
    event = _event(session, "pending_review")
    res = _confirm(client, event.id, make_confirm_token(event.id))
    assert res.status_code == 200
    body = res.json()
    # 2026-11-05 19:00 Toronto is EST (UTC-5), so 00:00 UTC the next day.
    assert body["start"].startswith("2026-11-06T00:00:00")
    assert "confidence" not in body and "status" not in body
    session.refresh(event)
    assert event.status == "published"


def test_wrong_token_is_refused_and_changes_nothing(client, session):
    event = _event(session, "pending_review")
    res = _confirm(client, event.id, "not-the-token")
    assert res.status_code == 404
    session.refresh(event)
    assert event.status == "pending_review" and event.start is None


def test_token_for_another_event_is_refused(client, session):
    a = _event(session, "pending_review")
    b = _event(session, "pending_review")
    assert _confirm(client, b.id, make_confirm_token(a.id)).status_code == 404


def test_missing_token_is_rejected(client, session):
    event = _event(session, "pending_review")
    res = client.post(f"/api/events/{event.id}/confirm", json={"start": "2026-11-05T19:00:00"})
    assert res.status_code == 422


def test_published_event_cannot_be_rewritten(client, session):
    event = _event(session, "published")
    res = _confirm(client, event.id, make_confirm_token(event.id))
    assert res.status_code == 409
    session.refresh(event)
    assert event.start is None


def test_unknown_event_looks_the_same_as_a_bad_token(client):
    assert _confirm(client, "nope", make_confirm_token("nope")).status_code == 404
