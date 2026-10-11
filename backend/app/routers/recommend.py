"""POST /recommend -- a free-text blurb becomes a ranked, explained shortlist.

    blurb
      -> embeddings.query_clubs()   Voyage + Chroma: ~20 nearest clubs
      -> reranker.rank_clubs()      Claude Sonnet: 5-8 of them, ranked, with a
                                    reason each, plus the student's outcomes
      -> + next_event per club      so the graph can show "what to show up to"

Two stages because they answer different questions. Vector search finds clubs
whose description resembles the blurb; it has no opinion on whether an intense
competition team suits someone who said "not too intense", and cannot explain
itself. The rerank supplies the judgement and the sentence.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.database import get_session
from app.models import Club, Event
from app.schemas import ClubMatch, EventLite, RecommendRequest, RecommendResponse
from app.services import embeddings, reranker
from app.timeutil import utc_now

router = APIRouter(tags=["recommend"])

logger = logging.getLogger(__name__)

CANDIDATE_POOL = 20

# Upstream failures are logged in full and reported generically. A provider's
# error text is written for whoever holds the account, not for the public: the
# Voyage quota error, for one, spells out the billing state of the org. Nothing
# from an exception reaches the client.
_SEARCH_UNAVAILABLE = "Search is unavailable right now. Try again in a moment."
_RANKING_UNAVAILABLE = "Couldn't rank your matches right now. Try again in a moment."


def _next_events(session: Session, club_ids: list[str]) -> dict[str, Event]:
    """Soonest upcoming published event per club, as one query rather than N.

    Only `published` -- a pending_review event has an unconfirmed date, and the
    graph's event node is the thing telling a student where to physically turn
    up. Only future events, for the same reason: "next step" is the entire point
    of that node.
    """
    if not club_ids:
        return {}

    rows = session.exec(
        select(Event)
        .where(
            Event.club_id.in_(club_ids),
            Event.status == "published",
            Event.start.is_not(None),
            Event.start >= utc_now(),
        )
        .order_by(Event.start)
    ).all()

    soonest: dict[str, Event] = {}
    for event in rows:  # ordered by start, so the first per club wins
        soonest.setdefault(event.club_id, event)
    return soonest


@router.post("/recommend", response_model=RecommendResponse)
async def recommend(
    request: RecommendRequest, session: Session = Depends(get_session)
) -> RecommendResponse:
    blurb = request.blurb.strip()
    if not blurb:
        raise HTTPException(status_code=400, detail="Tell us a bit about what you want.")

    # Both calls are blocking and network-bound (Voyage, then Anthropic); on the
    # event loop they would stall every other request for several seconds.
    try:
        candidate_ids = await asyncio.to_thread(
            embeddings.query_clubs, blurb, CANDIDATE_POOL
        )
    except embeddings.EmbeddingError as e:
        logger.exception("vector search failed for /recommend")
        raise HTTPException(status_code=503, detail=_SEARCH_UNAVAILABLE) from e

    if not candidate_ids:
        # No embedded clubs yet. An empty result is the honest answer -- the
        # frontend renders an empty state rather than showing a broken graph.
        return RecommendResponse(outcomes=[], clubs=[])

    # Chroma returns ids in similarity order; preserve it so the rerank sees the
    # search's opinion as its starting point.
    found = {c.id: c for c in session.exec(select(Club).where(Club.id.in_(candidate_ids))).all()}
    candidates = [found[cid] for cid in candidate_ids if cid in found]
    if not candidates:
        return RecommendResponse(outcomes=[], clubs=[])

    try:
        ranking = await asyncio.to_thread(reranker.rank_clubs, blurb, candidates)
    except reranker.RerankError as e:
        logger.exception("rerank failed for /recommend")
        raise HTTPException(status_code=503, detail=_RANKING_UNAVAILABLE) from e

    upcoming = _next_events(session, [r.club.id for r in ranking.clubs])

    return RecommendResponse(
        outcomes=ranking.outcomes,
        clubs=[
            ClubMatch(
                id=r.club.id,
                name=r.club.name,
                summary=r.club.summary,
                outcomes=r.club.outcomes,
                tags=r.club.tags,
                commitment=r.club.commitment,
                why_it_fits=r.why_it_fits,
                last_updated=r.club.last_updated,
                next_event=_to_event_lite(upcoming.get(r.club.id)),
            )
            for r in ranking.clubs
        ],
    )


def _to_event_lite(event: Event | None) -> EventLite | None:
    if event is None:
        return None
    return EventLite(
        id=event.id,
        title=event.title,
        start=event.start,
        location=event.location,
        source=event.source,
        source_file=event.source_file,
        dropbox_link=event.dropbox_link,
    )
