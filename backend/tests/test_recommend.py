"""POST /recommend end to end, with every paid call stubbed.

Voyage + Chroma (`embeddings.query_clubs`) and Claude (`reranker.get_client`) are
replaced, so these run offline and cost nothing. They pin what the endpoint
guarantees whatever the models say: the reranker's order survives, ids it
invented never become cards, outcomes come only from FIXED_OUTCOMES, at most
MAX_CLUBS come back, and `next_event` is a published, upcoming event or nothing.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_session
from app.main import app
from app.models import FIXED_OUTCOMES, Club, Event
from app.security import RateLimitMiddleware
from app.services import embeddings, reranker
from app.timeutil import utc_now


@pytest.fixture
def session():
    # StaticPool: one shared in-memory connection, so the TestClient's worker
    # threads see the same database the test seeded.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    # The limiter is per-process and every test shares one client address, so a
    # full run would trip /recommend's budget. It has its own concerns; not tested here.
    with patch.object(RateLimitMiddleware, "_exceeded", return_value=None):
        yield TestClient(app)
    app.dependency_overrides.clear()


def _clubs(session, n: int) -> list[Club]:
    clubs = [
        Club(name=f"Club {i}", summary=f"About club {i}", outcomes=["friends"], tags=["x"])
        for i in range(n)
    ]
    session.add_all(clubs)
    session.commit()
    return clubs


def _claude_says(raw: dict, stop_reason: str = "tool_use"):
    """A stand-in Anthropic client whose one tool call returns `raw`."""
    block = SimpleNamespace(type="tool_use", name=reranker.RERANK_TOOL_SCHEMA["name"], input=raw)
    response = SimpleNamespace(content=[block], stop_reason=stop_reason, stop_details=None)
    return SimpleNamespace(messages=SimpleNamespace(create=lambda **_: response))


def _recommend(client, clubs, raw, blurb="I want friends"):
    """POST /recommend where vector search returns `clubs` and Claude returns `raw`."""
    with (
        patch.object(embeddings, "query_clubs", return_value=[c.id for c in clubs]),
        patch.object(reranker, "get_client", return_value=_claude_says(raw)),
    ):
        return client.post("/api/recommend", json={"blurb": blurb})


def _pick(club: Club, why: str = "Fits you.") -> dict:
    return {"id": club.id, "why_it_fits": why}


def test_keeps_the_rerankers_order_not_the_searchs(client, session):
    a, b, c = _clubs(session, 3)
    res = _recommend(client, [a, b, c], {"outcomes": ["friends"], "clubs": [_pick(c), _pick(a)]})
    assert res.status_code == 200
    assert [m["id"] for m in res.json()["clubs"]] == [c.id, a.id]


def test_ids_the_model_invented_or_repeated_are_dropped(client, session):
    a, b = _clubs(session, 2)
    picks = [_pick(a), {"id": "not-a-real-id", "why_it_fits": "ghost"}, _pick(a), _pick(b)]
    res = _recommend(client, [a, b], {"outcomes": [], "clubs": picks})
    assert [m["id"] for m in res.json()["clubs"]] == [a.id, b.id]


def test_never_returns_more_than_the_cap(client, session):
    clubs = _clubs(session, 12)
    res = _recommend(client, clubs, {"outcomes": [], "clubs": [_pick(c) for c in clubs]})
    assert len(res.json()["clubs"]) == reranker.MAX_CLUBS


def test_student_outcomes_are_only_from_the_fixed_list(client, session):
    (a,) = _clubs(session, 1)
    real = FIXED_OUTCOMES[0]
    res = _recommend(client, [a], {"outcomes": [real, "world domination"], "clubs": [_pick(a)]})
    assert res.json()["outcomes"] == [real]


def test_next_event_is_the_soonest_published_upcoming_one(client, session):
    (a,) = _clubs(session, 1)
    now = utc_now()
    session.add_all(
        [
            Event(club_id=a.id, title="Last week", start=now - timedelta(days=7)),
            Event(club_id=a.id, title="Unconfirmed", start=now + timedelta(days=1), status="pending_review"),
            Event(club_id=a.id, title="Later", start=now + timedelta(days=9)),
            Event(club_id=a.id, title="Soon", start=now + timedelta(days=2)),
        ]
    )
    session.commit()
    res = _recommend(client, [a], {"outcomes": [], "clubs": [_pick(a)]})
    assert res.json()["clubs"][0]["next_event"]["title"] == "Soon"


def test_club_with_only_past_or_unconfirmed_events_has_no_next_event(client, session):
    (a,) = _clubs(session, 1)
    now = utc_now()
    session.add_all(
        [
            Event(club_id=a.id, title="Past", start=now - timedelta(days=1)),
            Event(club_id=a.id, title="Pending", start=now + timedelta(days=1), status="pending_review"),
        ]
    )
    session.commit()
    res = _recommend(client, [a], {"outcomes": [], "clubs": [_pick(a)]})
    assert res.json()["clubs"][0]["next_event"] is None


def test_response_never_carries_event_organizer_fields(client, session):
    (a,) = _clubs(session, 1)
    session.add(Event(club_id=a.id, title="Soon", start=utc_now() + timedelta(days=1)))
    session.commit()
    res = _recommend(client, [a], {"outcomes": [], "clubs": [_pick(a)]})
    event = res.json()["clubs"][0]["next_event"]
    assert not any("email" in key or "organizer" in key for key in event)


def test_blank_blurb_is_a_400_and_spends_nothing(client):
    with patch.object(embeddings, "query_clubs") as search:
        res = client.post("/api/recommend", json={"blurb": "   "})
    assert res.status_code == 400
    search.assert_not_called()


def test_over_long_blurb_is_rejected_before_any_paid_call(client):
    with patch.object(embeddings, "query_clubs") as search:
        res = client.post("/api/recommend", json={"blurb": "x" * 2001})
    assert res.status_code == 422
    search.assert_not_called()


def test_no_search_hits_is_an_empty_result_not_an_error(client):
    with patch.object(embeddings, "query_clubs", return_value=[]):
        res = client.post("/api/recommend", json={"blurb": "anything"})
    assert res.status_code == 200
    assert res.json() == {"outcomes": [], "clubs": []}


def test_search_failure_is_a_generic_503(client):
    with patch.object(embeddings, "query_clubs", side_effect=embeddings.EmbeddingError("org billing: secret")):
        res = client.post("/api/recommend", json={"blurb": "anything"})
    assert res.status_code == 503
    assert "secret" not in res.text


@pytest.mark.parametrize(
    "response",
    [
        _claude_says({"outcomes": [], "clubs": []}, stop_reason="refusal"),
        _claude_says({"outcomes": [], "clubs": []}, stop_reason="max_tokens"),
        SimpleNamespace(
            messages=SimpleNamespace(
                create=lambda **_: SimpleNamespace(content=[], stop_reason="end_turn", stop_details=None)
            )
        ),
    ],
    ids=["refusal", "truncated", "no-tool-call"],
)
def test_unusable_rerank_is_a_generic_503(client, session, response):
    (a,) = _clubs(session, 1)
    with (
        patch.object(embeddings, "query_clubs", return_value=[a.id]),
        patch.object(reranker, "get_client", return_value=response),
    ):
        res = client.post("/api/recommend", json={"blurb": "anything"})
    assert res.status_code == 503
    assert res.json()["detail"].startswith("Couldn't rank")


def test_malformed_model_output_degrades_to_an_empty_list(client, session):
    (a,) = _clubs(session, 1)
    raw = {"outcomes": None, "clubs": ["junk", {"id": a.id, "why_it_fits": None}]}
    res = _recommend(client, [a], raw)
    assert res.status_code == 200
    body = res.json()
    assert body["outcomes"] == []
    assert body["clubs"][0]["why_it_fits"] == ""
