"""Claude (Sonnet) file extraction: poster/PDF -> structured events + club updates.

Prompt shape (see project spec / CLAUDE.md):
  "Today is {date}, timezone America/Toronto. This file was dropped by a
   student club. Return: doc_type (poster | schedule | roster | other),
   club_name_guess, events[] (title, start, end, location, description,
   rsvp_url), club_updates (meeting_info, links), confidence (0-1),
   uncertainties[]. Only extract what is visibly stated. If the date or
   year is ambiguous, list it under uncertainties instead of guessing."

Safety rule: auto-publish (status="published") only if title + start are
present AND confidence is high; otherwise status="pending_review".
Wrong dates in a live demo are the #1 failure mode -- always pass today's
date and never let the model guess an ambiguous year.

Scope: this module turns bytes into structured data and nothing else. No
database writes, no club matching, no Dropbox. That keeps the safety rule a
pure function of the extracted fields, testable without a network call.
"""

import base64
from dataclasses import dataclass, field
from datetime import date, datetime

import anthropic

from app.config import settings
from app.timeutil import CAMPUS_TZ, as_utc, local_to_utc

MODEL = "claude-sonnet-5"

# Auto-publish threshold. Below this an event still gets written, but as
# pending_review, so it never reaches the live feed unreviewed.
CONFIDENCE_THRESHOLD = 0.8

def _nullable(kind: str, description: str | None = None) -> dict:
    """A field that may be null.

    Written as anyOf rather than {"type": ["string", "null"]} because strict mode
    does not accept JSON-Schema type-unions -- and strict mode is what stops the
    model returning `events` as a list of strings, which it did on one run and
    which crashed the pipeline.
    """
    schema = {"anyOf": [{"type": kind}, {"type": "null"}]}
    if description:
        schema["description"] = description
    return schema


# strict=True makes the API guarantee the arguments validate against this schema.
# Every object therefore needs additionalProperties:false and a `required` listing
# ALL its properties -- nullable ones are still required, they just may be null.
EXTRACTION_TOOL_SCHEMA = {
    "name": "extract_club_file",
    "description": "Structured extraction from a club-dropped poster/PDF/doc.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "doc_type": {"type": "string", "enum": ["poster", "schedule", "roster", "other"]},
            "club_name_guess": _nullable("string"),
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "start": _nullable(
                            "string",
                            "Local Toronto wall-clock time as naive ISO 8601 "
                            "(2026-09-24T19:00:00). No UTC offset, no 'Z'. "
                            "Null if the date cannot be determined without guessing.",
                        ),
                        "end": _nullable("string", "Same format as start. Null if not stated."),
                        "location": _nullable("string"),
                        "description": _nullable("string"),
                        "rsvp_url": _nullable("string"),
                        "is_reschedule": {
                            "type": "boolean",
                            "description": (
                                "True ONLY if the document itself explicitly says this date "
                                "replaces an earlier one -- words like 'RESCHEDULED', "
                                "'CORRECTION', 'NEW DATE', 'moved to', 'updated time'. "
                                "This is the one signal used to update a previously stored "
                                "date instead of adding a second event, so it must come from "
                                "text actually on the document, never inferred from context. "
                                "False for an ordinary announcement, even a recurring one."
                            ),
                        },
                    },
                    "required": [
                        "title", "start", "end", "location", "description", "rsvp_url",
                        "is_reschedule",
                    ],
                },
            },
            "club_updates": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "meeting_info": _nullable("string"),
                    # An array of pairs rather than a free-form object: strict mode
                    # needs additionalProperties:false, which would forbid the
                    # arbitrary keys a {name: url} map depends on.
                    "links": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "label": {"type": "string"},
                                "url": {"type": "string"},
                            },
                            "required": ["label", "url"],
                        },
                    },
                },
                "required": ["meeting_info", "links"],
            },
            "confidence": {"type": "number"},
            "uncertainties": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "doc_type", "club_name_guess", "events", "club_updates",
            "confidence", "uncertainties",
        ],
    },
}


@dataclass(frozen=True)
class ExtractedEvent:
    """One event read off a document, with times already converted to UTC."""

    title: str
    start: datetime | None  # naive UTC
    end: datetime | None  # naive UTC
    location: str | None
    description: str | None
    rsvp_url: str | None
    status: str  # published | pending_review
    start_local: str | None = None  # what the model actually said, for debugging
    is_reschedule: bool = False  # poster explicitly says this replaces an earlier date
    # Which field a human needs to supply before a pending_review event can publish. Always empty for
    # a published event. See missing_fields() -- kept in lock-step with decide_status() by construction.
    missing: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Extraction:
    doc_type: str
    club_name_guess: str | None
    events: list[ExtractedEvent]
    club_updates: dict
    confidence: float
    uncertainties: list[str]
    raw: dict = field(repr=False)  # unmodified tool input


class ExtractionError(RuntimeError):
    """Claude returned something we can't use (refusal, truncation, no tool call)."""


_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    """The single Anthropic client.

    The key is passed explicitly rather than left to the SDK's env lookup: this
    project keeps config in .env loaded by pydantic-settings, which populates
    `settings` but never touches os.environ. A bare Anthropic() finds nothing.
    """
    global _client
    if _client is None:
        if not settings.anthropic_api_key:
            raise ExtractionError("ANTHROPIC_API_KEY is not set in backend/.env")
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def has_time_component(value: str | None) -> bool:
    """Did the model give a time of day, or only a date?

    A bare ISO date is exactly "YYYY-MM-DD" (10 characters); anything longer
    carries a time. Length is the reliable test here because fromisoformat
    happily accepts the date-only form and silently returns midnight -- the value
    looks like a fully-specified datetime afterwards, with nothing to distinguish
    "the poster said midnight" from "the poster didn't say".
    """
    return bool(value) and len(value.strip()) > 10


def decide_status(
    title: str | None,
    start: datetime | None,
    confidence: float,
    has_time: bool = True,
) -> str:
    """The safety rule, as a pure function.

    Auto-publish only when we have a title, a resolved start, a time of day, and
    model confidence. Everything else waits for a human. A wrong date on the live
    feed is the single most damaging failure mode, so the bar to reach it is
    deliberately explicit and in one place.

    `has_time` exists because a date-only poster parses to midnight and would
    otherwise publish as "12:00 AM" -- a time nobody stated and which is almost
    certainly wrong. The event is real; the hour isn't known, so it goes to
    review rather than onto the feed with a fabricated start time.
    """
    if title and start is not None and has_time and confidence >= CONFIDENCE_THRESHOLD:
        return "published"
    return "pending_review"


def missing_fields(
    title: str | None, start: datetime | None, has_time: bool, confidence: float
) -> list[str]:
    """Which single field to ask a human for, when decide_status() sends this event to review.

    A pending_review event never reaches a review queue nobody looks at -- the upload flow prompts
    for this field immediately (AddEventPanel.jsx) so the person who just dropped the poster can
    supply it on the spot. Deliberately narrow: title is required to create an Event row at all (see
    the `if not title: continue` guard in extract_file), so it can never actually be missing here, and
    location was never part of the safety gate. That leaves exactly one question to ask -- the date, or
    just the time when the date is already known -- matching decide_status()'s own condition so the two
    can't drift apart.

    Low confidence with every field present still asks for the date rather than nothing: the model
    itself is unsure of its own read, and a re-typed date is a cheap way to catch a misread one before
    it reaches the live feed.
    """
    if decide_status(title, start, confidence, has_time) == "published":
        return []
    return ["time"] if start is not None and not has_time else ["date"]


def to_utc(value: str | None) -> datetime | None:
    """Local campus-time ISO string -> timezone-aware UTC datetime.

    The prompt asks for wall-clock time with no offset, because a poster states
    wall-clock time and nothing else -- making the model also compute a UTC offset
    invites it to get DST wrong on top of everything else. It reports what it sees;
    this function does the arithmetic.

    Returns None rather than raising on garbage: one unparseable date shouldn't
    discard the other events on the same poster, and a None start means the safety
    rule routes that event to pending_review anyway.
    """
    if not value:
        return None
    try:
        local = datetime.fromisoformat(value.strip())
    except ValueError:
        return None

    if local.tzinfo is not None:
        # Model supplied an offset despite the instruction -- trust it rather than
        # double-applying a zone.
        return as_utc(local)

    return local_to_utc(local)


def _build_content_blocks(file_bytes: bytes, mime_type: str, prompt: str) -> list[dict]:
    """Wrap the file in the content block Claude expects for its type.

    Images and PDFs take different block shapes -- this is the only place that
    distinction exists. The mime_type comes from dropbox_store.mime_type_for(), so
    the allowlist and this branch can't drift apart.
    """
    data = base64.standard_b64encode(file_bytes).decode()

    if mime_type == "application/pdf":
        source_block = {
            "type": "document",
            "source": {"type": "base64", "media_type": mime_type, "data": data},
        }
    elif mime_type.startswith("image/"):
        source_block = {
            "type": "image",
            "source": {"type": "base64", "media_type": mime_type, "data": data},
        }
    else:
        raise ValueError(f"Unsupported mime type for extraction: {mime_type}")

    return [source_block, {"type": "text", "text": prompt}]


def _build_prompt(today: date) -> str:
    """Assembled per call, because interpolating today's date is the whole point.

    Without an anchor date the model has nothing to resolve "Thursday the 24th"
    against and will invent a year. The repeated instruction to push ambiguity into
    `uncertainties` is the other half: an event that admits it couldn't confirm the
    year is recoverable on stage, a confidently wrong one is not.
    """
    return f"""Today is {today:%A, %B %d, %Y}. The timezone is {settings.school_timezone}.

This file was shared by a student club at the University of Toronto. Extract any
events and club details it advertises.

Rules:
- Extract only what is visibly stated. Do not infer a location, an RSVP link, or a
  description that isn't there.
- Report times as local Toronto wall-clock time in naive ISO 8601
  (e.g. 2026-09-24T19:00:00). Do not add a UTC offset or convert the time yourself.
- If the poster does not print a year, infer it from today's date ONLY when the
  result is unambiguous. If there is any doubt, set `start` to null and describe the
  problem in `uncertainties`.
- Same for a missing time of day: if only a date is given, say so in `uncertainties`.
- Set `is_reschedule` to true only if the document itself uses explicit
  reschedule/correction language ("RESCHEDULED", "NEW DATE", "moved to",
  "correction"). A club posting its normal weekly or recurring meeting is NOT a
  reschedule even though the date changed from last time -- that flag exists
  for the specific case of a club fixing a mistake, not for routine announcements.
- `confidence` should reflect how legible and complete this document is: high for a
  clear poster stating an explicit date and time, low for a blurry photo, a partial
  schedule, or anything where you had to work to read it.
- If this isn't an event document at all (a roster, a logo, a random photo), set
  `doc_type` accordingly and return an empty `events` list.

Never guess a date to fill the field. An empty field is useful; a wrong date is not."""


def extract_file(
    file_bytes: bytes, mime_type: str, today: date | None = None
) -> Extraction:
    """Poster/PDF bytes -> structured events with UTC times and a publish decision.

    `today` is a parameter rather than being read inside so tests can pin it --
    "does this poster resolve to 2026 or 2027?" is only answerable deterministically
    if the test controls the anchor date.

    Synchronous by design, matching dropbox_store: service functions are sync,
    callers wrap them in asyncio.to_thread. This call takes several seconds, so
    running it directly on the event loop would stall every other request.
    """
    if today is None:
        today = datetime.now(CAMPUS_TZ).date()

    blocks = _build_content_blocks(file_bytes, mime_type, _build_prompt(today))

    response = get_client().messages.create(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "medium"},
        tools=[EXTRACTION_TOOL_SCHEMA],
        tool_choice={
            "type": "tool",
            "name": EXTRACTION_TOOL_SCHEMA["name"],
            "disable_parallel_tool_use": True,
        },
        messages=[{"role": "user", "content": blocks}],
    )

    # Guards before touching content. A refusal returns HTTP 200 with no tool block,
    # and a max_tokens truncation is worse: the partial tool input can still look
    # like valid JSON, so it fails silently as "this poster had fewer events".
    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "explanation", None)
        raise ExtractionError(f"Model declined to process this file: {detail}")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("Response hit max_tokens; extraction may be truncated.")

    raw = next(
        (
            block.input
            for block in response.content
            if block.type == "tool_use" and block.name == EXTRACTION_TOOL_SCHEMA["name"]
        ),
        None,
    )
    if raw is None:
        raise ExtractionError(f"No tool_use block in response (stop_reason={response.stop_reason})")

    try:
        confidence = min(1.0, max(0.0, float(raw.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0

    events = []
    for item in raw.get("events") or []:
        # strict mode should guarantee dicts here, but this ran once against a
        # response whose `events` was a list of bare strings and crashed the whole
        # pipeline. A malformed item should cost one event, not the file.
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not title:
            continue  # title is required by the schema; skip rather than write a blank row
        start_raw = item.get("start")
        start = to_utc(start_raw)
        has_time = has_time_component(start_raw)
        events.append(
            ExtractedEvent(
                title=title,
                start=start,
                end=to_utc(item.get("end")),
                location=item.get("location"),
                description=item.get("description"),
                rsvp_url=item.get("rsvp_url"),
                status=decide_status(title, start, confidence, has_time),
                start_local=start_raw,
                is_reschedule=bool(item.get("is_reschedule")),
                missing=missing_fields(title, start, has_time, confidence),
            )
        )

    return Extraction(
        doc_type=raw.get("doc_type") or "other",
        club_name_guess=raw.get("club_name_guess"),
        events=events,
        club_updates=raw.get("club_updates") or {},
        confidence=confidence,
        uncertainties=raw.get("uncertainties") or [],
        raw=raw,
    )


def _smoke_test() -> None:
    """Extract the real posters sitting in Dropbox and show the timezone conversion.

    Uses real files rather than a fixture because they exercise the image AND document
    content-block paths, and because they are the actual demo data -- a fixture would
    prove the code runs, this proves the demo works.

    Makes one real Claude API call per file.
    """
    from app.services.dropbox_store import download_file, list_inbox, mime_type_for

    files = list_inbox()
    if not files:
        print("No supported files in the Dropbox inbox -- nothing to extract.")
        return

    print(f"Extracting {len(files)} file(s) with {MODEL}\n")

    for f in files:
        print("=" * 72)
        print(f.name)
        print("=" * 72)
        try:
            result = extract_file(download_file(f.path), mime_type_for(f.name))
        except ExtractionError as e:
            print(f"  EXTRACTION FAILED: {e}\n")
            continue

        print(f"  doc_type   {result.doc_type}")
        print(f"  club       {result.club_name_guess}")
        print(f"  confidence {result.confidence}")

        if not result.events:
            print("  events     (none)")
        for e in result.events:
            print(f"\n  - {e.title}")
            # Local and UTC side by side: a timezone bug is invisible when you only
            # see one of them. September in Toronto is UTC-4, so expect local + 4h.
            print(f"      local  {e.start_local}")
            print(f"      utc    {e.start}")
            print(f"      where  {e.location}")
            print(f"      status {e.status}")

        if result.uncertainties:
            print("\n  uncertainties:")
            for u in result.uncertainties:
                print(f"      - {u}")
        print()


if __name__ == "__main__":
    _smoke_test()
