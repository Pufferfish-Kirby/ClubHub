"""The club's own side of ClubHub: log in with the club's key and manage its events (D17).

"Logged in" means every request carries `X-Club-Key` (app/club_keys.py). There is no session or
cookie: the browser keeps the key for the tab and sends it each time, like the review page does
with REVIEW_KEY. The difference is that each club holds its own key, so the server knows which
club is acting, and one club's key can be revoked without touching anyone else's.
"""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from sqlmodel import Session, select

from app.club_keys import club_for_key
from app.database import get_session
from app.models import Club, Event
from app.routers.upload import handle_upload
from app.schemas import ClubEvent, ClubMe, UploadResponse

logger = logging.getLogger(__name__)


def require_club(
    x_club_key: str = Header(default=""), session: Session = Depends(get_session)
) -> Club:
    club = club_for_key(session, x_club_key)
    if club is None:
        raise HTTPException(status_code=401, detail="That club key isn't valid.")
    return club


router = APIRouter(prefix="/club", tags=["club"])


@router.get("/me", response_model=ClubMe)
def me(club: Club = Depends(require_club), session: Session = Depends(get_session)) -> ClubMe:
    """Which club this key logs in as, and every event of theirs that isn't discarded. Unlike the
    public feed this includes pending ones, with their status, since they're the club's own."""
    events = session.exec(
        select(Event)
        .where(Event.club_id == club.id, Event.status != "discarded")
        .order_by(Event.start)
    ).all()
    return ClubMe(
        id=club.id,
        name=club.name,
        events=[ClubEvent.model_validate(e, from_attributes=True) for e in events],
    )


@router.post("/upload", response_model=UploadResponse)
async def club_upload(file: UploadFile, club: Club = Depends(require_club)) -> UploadResponse:
    """Same as POST /upload, except the poster is the club's own: it's filed under this club
    whatever name it prints, and a confident read may correct the club's live events. The key is
    checked (by the dependency) before the handler runs, so a bad key never reaches Dropbox or
    Claude."""
    logger.info("club upload by %s: %s", club.id, file.filename)
    return await handle_upload(file, club_id=club.id)
