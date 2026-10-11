from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.database import get_session
from app.models import Event
from app.schemas import PublicEvent
from app.services.extraction import to_utc
from app.timeutil import as_utc

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=list[PublicEvent])
def list_events(since: datetime | None = None, session: Session = Depends(get_session)):
    """Live feed of published events, including Dropbox-sourced updates.

    Shaped by PublicEvent rather than returned raw: the row carries an internal
    `confidence` score that has no meaning to a student and no place on the wire.
    """
    query = select(Event).where(Event.status == "published")
    if since:
        query = query.where(Event.start >= as_utc(since))
    return session.exec(query.order_by(Event.start)).all()


class ConfirmEventRequest(BaseModel):
    """Local Toronto wall-clock time, naive ISO 8601 -- the same format the extraction prompt asks
    Claude for (e.g. "2026-09-24T19:00:00"), so both paths share one conversion (extraction.to_utc)."""

    start: str


@router.post("/{event_id}/confirm")
def confirm_event(
    event_id: str, body: ConfirmEventRequest, session: Session = Depends(get_session)
) -> Event:
    """A human supplies the field extraction couldn't confidently determine (see
    extraction.missing_fields, surfaced on the upload response as `missing`), right in the same
    upload flow rather than the event sitting unreachable in pending_review forever. Publishing here
    is not the auto-publish path the confidence gate guards against -- a person read the poster and
    typed the date themselves, which is exactly what the gate exists to fall back on.
    """
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    start = to_utc(body.start)
    if start is None:
        raise HTTPException(status_code=400, detail="Couldn't read that date and time.")

    event.start = start
    event.status = "published"
    event.confidence = 1.0
    session.add(event)
    session.commit()
    session.refresh(event)
    return event
