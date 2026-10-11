"""SOP's event calendar -- the reference adapter, and the worked example the skill points at.

SOP runs The Events Calendar (a WordPress plugin) behind
`/wp-json/tribe/events/v1/events`. That endpoint is what the listing page's own
JavaScript calls, which is exactly the kind of source the scraping-club-events
skill is for: no HTML parsing, no language model, stated start times.

A `fetch_events()` helper for this endpoint has sat in sop_sync.py since the
first sync and was never called by anything. This module is that code finished:
paginated, filtered to future events server-side, organizer contacts dropped,
and returning the adapter contract instead of raw dicts.

Two details worth keeping:

  * **`start_date` filters upstream.** Without it the endpoint returns the whole
    archive and every past event has to be thrown away after transfer. The runner
    filters again regardless -- this just avoids fetching a year of history.

  * **`utc_start_date` is already UTC.** The payload carries both local and UTC
    forms; taking the UTC one avoids re-deriving an offset that the source
    already resolved, including across a DST boundary.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html import unescape

from bs4 import BeautifulSoup

from app.config import settings
from app.services.event_sources.base import ScrapedEvent, safe_console
from app.timeutil import as_utc, local_to_utc

SOURCE_NAME = "sop"

EVENTS_PATH = "/tribe/events/v1/events"
PER_PAGE = 50
THROTTLE_SECONDS = 0.5  # SOP etiquette, same as sop_sync's list pagination
MAX_PAGES = 40  # stop runaway pagination if next_rest_url ever loops

# urllib rather than httpx, for the reason documented at the top of sop_sync.py:
# SOP's WAF 403s the httpx client specifically (TLS fingerprinting). The dead
# fetch_events() used httpx and carried a note that it had never been verified.
HEADERS = {"User-Agent": settings.sop_user_agent}


def _get_json(url: str, params: dict | None = None) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _clean_text(raw: str | None) -> str | None:
    """Decode HTML entities and collapse whitespace.

    The API returns entity-encoded text in every plain-text field, not just the
    HTML ones: titles arrive as "AI Career &amp; Innovation Association" and
    venues as "Undergrad Lounge &#8211; 19 Ursula Franklin St". Left encoded, the
    club name reaches matcher.normalize() with "amp" as a distinctive token,
    which drags the fuzzy score away from the real club and can create a
    placeholder row for a club already in the database.
    """
    if not raw:
        return None
    text = " ".join(unescape(raw).split())
    return text or None


def _strip_html(raw: str | None) -> str | None:
    """Descriptions come back as rendered HTML."""
    if not raw:
        return None
    text = BeautifulSoup(raw, "lxml").get_text(separator=" ", strip=True)
    return _clean_text(text)


def _parse_utc(value: str | None) -> datetime | None:
    """"2026-09-24 23:00:00" (already UTC) -> aware UTC datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace(" ", "T"))
    except ValueError:
        return None
    return as_utc(parsed)


def _parse_local(value: str | None) -> datetime | None:
    """Fallback for records missing the UTC fields: local campus time -> aware UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace(" ", "T"))
    except ValueError:
        return None
    return local_to_utc(parsed)


def _club_name(record: dict) -> str | None:
    """The club this event belongs to.

    `organizer` is the right field -- on SOP the organizer of a club event is the
    club. Only the display name is read: the same objects carry `email` and
    `phone` for individual students, which CLAUDE.md forbids surfacing, so they
    are never mapped. (The runner scrubs text as a backstop, but the fix is not
    reading them in the first place.)
    """
    organizers = record.get("organizer") or []
    for organizer in organizers:
        name = _clean_text(organizer.get("organizer"))
        if name:
            return name

    # Some listings attach the club as a category instead of an organizer.
    for category in record.get("categories") or []:
        name = _clean_text(category.get("name"))
        if name:
            return name
    return None


def _venue(record: dict) -> str | None:
    venue = record.get("venue") or {}
    parts = [
        _clean_text(venue.get("venue")),
        _clean_text(venue.get("address")),
        _clean_text(venue.get("city")),
    ]
    joined = ", ".join(p for p in parts if p)
    return joined or None


def to_scraped_event(record: dict) -> ScrapedEvent | None:
    """One Tribe Events record -> ScrapedEvent, or None if unusable.

    Unusable means no title or no identifiable club: an event with no club has no
    edge in the you -> outcomes -> clubs graph and would only create a junk
    placeholder row.
    """
    title = _clean_text(record.get("title"))
    club = _club_name(record)
    if not title or not club:
        return None

    start = _parse_utc(record.get("utc_start_date")) or _parse_local(
        record.get("start_date")
    )
    end = _parse_utc(record.get("utc_end_date")) or _parse_local(record.get("end_date"))

    return ScrapedEvent(
        title=title,
        club_name_guess=club,
        start=start,
        end=end,
        location=_venue(record),
        description=_strip_html(record.get("description")),
        rsvp_url=record.get("website") or record.get("url"),
        # The endpoint states start times outright, so there is nothing inferred
        # to be less than certain about.
        confidence=1.0,
        source_url=record.get("url"),
        external_id=str(record["id"]) if record.get("id") is not None else None,
        all_day=bool(record.get("all_day")),
        raw=record,
    )


def fetch(*, limit: int | None = None) -> list[ScrapedEvent]:
    """Future events from SOP's calendar, newest listings first.

    `start_date=today` keeps the archive on the server. The runner re-checks every
    date anyway -- an adapter is never the thing standing between a stale event
    and the feed.
    """
    today = datetime.now(CAMPUS_TZ).date()
    url = f"{settings.sop_base_url}{EVENTS_PATH}"
    params: dict | None = {
        "per_page": PER_PAGE,
        "start_date": today.isoformat(),
        "status": "publish",
    }

    events: list[ScrapedEvent] = []
    for _ in range(MAX_PAGES):
        try:
            payload = _get_json(url, params)
        except urllib.error.HTTPError as exc:
            # The plugin 404s past the last page rather than returning an empty list.
            if exc.code in (400, 404):
                break
            raise

        records = payload.get("events") or []
        if not records:
            break

        for record in records:
            event = to_scraped_event(record)
            if event is not None:
                events.append(event)
            if limit is not None and len(events) >= limit:
                return events

        next_url = payload.get("next_rest_url")
        if not next_url:
            break
        url, params = next_url, None  # next_rest_url already carries the query
        time.sleep(THROTTLE_SECONDS)

    return events


def _smoke_test() -> None:
    """Hit the real endpoint and print what the adapter makes of it.

    Read-only -- nothing is written. Use `python -m app.services.event_sources.runner
    sop_events --dry-run` to see the staging decisions too.
    """
    safe_console()
    events = fetch(limit=10)
    print(f"{len(events)} events from {settings.sop_base_url}{EVENTS_PATH}\n")
    for event in events:
        print(f"  {event.title}")
        print(f"      club={event.club_name_guess!r}")
        print(f"      start={event.start} (utc)  all_day={event.all_day}")
        print(f"      where={event.location!r}")
        print(f"      url={event.source_url}")
    if not events:
        print("Nothing returned. Check the endpoint is reachable and not WAF-blocked.")


if __name__ == "__main__":
    _smoke_test()
