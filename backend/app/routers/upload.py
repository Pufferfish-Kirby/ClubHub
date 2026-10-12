"""POST /upload -- a user photographs a flier in the app and it becomes an event.

The second entrance into the same pipeline the Dropbox watcher uses. The flow is
deliberately "store first, then read":

    bytes from the browser
      -> dropbox_store.upload_file()   Dropbox is the durable store for every
                                       file, however it arrived
      -> ingest.process_file()         the same function the watcher calls
      -> Event rows + a provenance link back to the uploaded image

Uploading to Dropbox before extracting is what keeps Event.dropbox_link
meaningful for user uploads too: an AI-extracted event is only as trustworthy as
the ability to click through and see the poster it came from.

Writing the IngestLog row at upload time (process_file does this) is also what
stops the watcher re-processing the same file when it next scans the folder.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, UploadFile
from sqlmodel import Session

from app.confirm_token import make_confirm_token
from app.database import engine
from app.schemas import UploadedClub, UploadedEvent, UploadResponse
from app.services.dropbox_store import MIME_TYPES, mime_type_for, upload_file
from app.services.ingest import IngestResult, process_file

router = APIRouter(tags=["upload"])

logger = logging.getLogger(__name__)

# Claude's limits are far higher, but a flier photo has no business being larger
# than this, and an unbounded read is a trivial way to exhaust memory.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 64 * 1024

_MESSAGES = {
    "no_events": "We read the file but couldn't find an event on it.",
    "duplicate": "We've already processed this file.",
    "failed": "We couldn't read this file.",
}


def _store_and_process(data: bytes, filename: str) -> IngestResult:
    """Upload to Dropbox and run the pipeline. Synchronous, runs in a worker thread.

    Opens its own session rather than accepting one from the request: the work
    happens on a different thread, and a session handed across the boundary would
    be used from two threads over its lifetime.
    """
    inbox_file = upload_file(data, filename)
    with Session(engine) as session:
        return process_file(session, inbox_file)


def _describe(result: IngestResult) -> str:
    """The `message` string. `missing` events never sit in an unreachable review queue -- they carry
    which field to ask for (event.missing), and AddEventPanel prompts for it right on this response, so
    the copy here says "confirm", not "review"."""
    if result.status != "processed":
        return _MESSAGES.get(result.status, "Something went wrong.")

    published = result.published_count
    unconfirmed = len(result.events) - published
    club = result.club_name or "this club"

    if published and not unconfirmed:
        return f"Added {published} event{'s' if published > 1 else ''} for {club}."
    if published and unconfirmed:
        return (
            f"Added {published} event{'s' if published > 1 else ''} for {club}. "
            f"{unconfirmed} needs a quick confirmation below."
        )
    return (
        f"We found {len(result.events)} event{'s' if len(result.events) > 1 else ''} "
        f"for {club}, but couldn't read every detail -- confirm the rest below."
    )


@router.post("/upload", response_model=UploadResponse)
async def upload_flier(file: UploadFile) -> UploadResponse:
    """Accept a flier image or PDF, extract its events, and return what we found."""
    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="No filename provided.")

    if mime_type_for(filename) is None:
        raise HTTPException(
            status_code=415,
            detail=(
                "We can read images and PDFs. "
                f"Supported: {', '.join(sorted(MIME_TYPES))}"
            ),
        )

    # Read in chunks and stop at the cap. `await file.read()` enforced the limit
    # only once the whole body was already in memory, which is the exhaustion the
    # cap exists to prevent: a 2 GB POST was buffered in full and only then refused.
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(UPLOAD_CHUNK_BYTES):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File is too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB).",
            )
        chunks.append(chunk)

    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=400, detail="The file is empty.")

    # Dropbox upload + Claude extraction are both blocking and take seconds.
    # On the event loop they would stall every other request for the duration.
    try:
        result = await asyncio.to_thread(_store_and_process, data, filename)
    except Exception as e:
        # Dropbox or Anthropic being unreachable is an upstream failure, not the
        # user's fault -- say so with the status code rather than a generic 500.
        # The cause is logged, never returned: these exceptions carry provider
        # internals, and a Dropbox client error can quote the request it failed on.
        logger.exception("upload pipeline failed for %s", filename)
        raise HTTPException(
            status_code=502, detail="Couldn't process the file right now. Try again."
        ) from e

    return UploadResponse(
        status=result.status,
        message=_describe(result),
        club=(
            UploadedClub(
                id=result.club_id, name=result.club_name, created=result.club_created
            )
            if result.club_id and result.club_name
            else None
        ),
        events=[
            UploadedEvent(
                id=e.id,
                title=e.title,
                start=e.start,
                location=e.location,
                status=e.status,
                source_file=result.file.name,
                dropbox_link=e.dropbox_link,
                missing=e.missing,
                start_local=e.start_local,
                confirm_token=(
                    make_confirm_token(e.id) if e.status == "pending_review" else None
                ),
            )
            for e in result.events
        ],
        confidence=result.confidence,
        uncertainties=result.uncertainties,
    )
