"""Review queue for Dropbox posters nobody was there to confirm.

A poster uploaded in the app is confirmed on the spot by its uploader (app/confirm_token.py). One
dropped straight into the Dropbox folder has no uploader present, so a pending_review event from it
waits here instead. A reviewer is whoever holds REVIEW_KEY: one shared secret, no accounts. With the
key unset every route here answers 404, so a deploy that never sets it exposes nothing.
"""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from app.config import settings
from app.database import get_session
from app.models import Club, Event
from app.routers.events import publish_pending, require_pending
from app.schemas import PublicEvent
from app.timeutil import to_campus


def require_reviewer(x_review_key: str = Header(default="")) -> None:
    if not settings.review_key:
        raise HTTPException(status_code=404, detail="Not found")
    # compare_digest: response timing can't reveal how much of a guessed key was right.
    if not hmac.compare_digest(x_review_key.encode(), settings.review_key.encode()):
        raise HTTPException(status_code=401, detail="Wrong review key.")


router = APIRouter(prefix="/review", tags=["review"], dependencies=[Depends(require_reviewer)])


class PendingEvent(BaseModel):
    id: str
    title: str
    club_name: str
    # The model's read in campus wall-clock time ("2026-10-31T19:00"), the format a
    # datetime-local input takes, to pre-fill the form. None when it found no date.
    start_local: str | None
    location: str | None
    source_file: str | None
    dropbox_link: str | None


class PublishRequest(BaseModel):
    start: str  # local campus wall-clock time, same format as ConfirmEventRequest.start


def _pending_event(session: Session, event_id: str) -> Event:
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    require_pending(event)
    return event


@router.get("/pending", response_model=list[PendingEvent])
def list_pending(session: Session = Depends(get_session)) -> list[PendingEvent]:
    rows = session.exec(
        select(Event, Club)
        .join(Club, Event.club_id == Club.id)
        .where(Event.status == "pending_review", Event.source == "dropbox")
        .order_by(Event.start)
    ).all()
    return [
        PendingEvent(
            id=event.id,
            title=event.title,
            club_name=club.name,
            start_local=to_campus(event.start).strftime("%Y-%m-%dT%H:%M") if event.start else None,
            location=event.location,
            source_file=event.source_file,
            dropbox_link=event.dropbox_link,
        )
        for event, club in rows
    ]


@router.post("/{event_id}/publish", response_model=PublicEvent)
def publish(
    event_id: str, body: PublishRequest, session: Session = Depends(get_session)
) -> PublicEvent:
    """Same rules as the uploader's confirm: the reviewer read the poster and typed the start."""
    return publish_pending(session, _pending_event(session, event_id), body.start)


@router.post("/{event_id}/discard", status_code=204)
def discard(event_id: str, session: Session = Depends(get_session)) -> Response:
    """A misread or junk poster. Hidden, not deleted: every public query shows only `published`."""
    event = _pending_event(session, event_id)
    event.status = "discarded"
    session.add(event)
    session.commit()
    return Response(status_code=204)
