"""One-time sync of a curated club list from SOP's WordPress REST API.

Verified 2026-09-19 against https://sop.utoronto.ca:
  - REST endpoint: {SOP_BASE_URL}/wp/v2/group/{id}, and {SOP_BASE_URL}/wp/v2/group
    with ?search=<term> for discovery. The frontend's own "search this site" box
    (wp-admin/admin-ajax.php?action=alm_get_posts...) hits the same underlying WP
    search, so it has the same problem: fuzzy full-text ranking that often buries
    the exact title you're looking for. Prefer a known post ID once you have one.
  - SOP's WAF returns 403 for the `httpx` client specifically (TLS/HTTP
    fingerprinting, not IP- or header-based -- confirmed by hitting the same URL
    with httpx vs stdlib urllib.request back to back). This module uses
    urllib.request for that reason; don't swap it back to httpx.
  - `acf._expiration-date` and `meta._oasis_original` (the dedupe key from
    CLAUDE.md) are exposed via REST. Website/email/social links and the
    constitution PDF are NOT in the REST payload -- those are scraped off the
    rendered /group/<slug>/ page with BeautifulSoup.

Run as a script (`python -m app.services.sop_sync`), not on every request.
"""

import argparse
import html as html_lib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from bs4 import BeautifulSoup
from sqlmodel import Session, select

from app.config import settings
from app.database import engine, init_db
from app.models import Club
from app.timeutil import as_utc, utc_now

HEADERS = {"User-Agent": settings.sop_user_agent}

# The known-good demo clubs. sync() always pulls these first and they're exempt from
# the --limit sample, so a demo rehearsal can never lose them to sampling. Post IDs were
# found via one-off find_group_by_search() lookups on 2026-09-19 and confirmed by eye;
# add more clubs here as (id, label) pairs. Use --targets-only to sync just these.
TARGET_GROUP_IDS: dict[int, str] = {
    25046: "University of Toronto Machine Intelligence Student Team (UTMIST)",
    20806: "Hart House Symphonic Band",
    25481: "University of Toronto Poker Club",
    26242: "UofT Kpop Dance Club (UTKDC)",
    149718: "University of Toronto Japan Student Association",
}

_CAMPUS_CLASS_RE = re.compile(r"campus-(.+)")
_AREA_CLASS_RE = re.compile(r"areas_of_interest-(.+)")

# Discovery/throttling knobs. WP caps per_page at 100, so the full ~1,250-group catalog
# is ~13 list calls -- cheap. The expensive half is the per-club HTML scrape (one
# request each, +1s etiquette sleep), which is why sync() samples before scraping.
LIST_PER_PAGE = 100
LIST_THROTTLE_SECONDS = 0.5
DETAIL_THROTTLE_SECONDS = 1.0
COMMIT_EVERY = 25  # flush progress so a crash mid-run doesn't lose the whole scrape

DEFAULT_LIMIT = 250
# Match on the taxonomy SLUG, not settings.sop_campus. class_list carries
# "campus-st-george", which _parse_taxonomy_tags title-cases to "St George" -- no
# period -- so comparing against settings.sop_campus ("St. George") matches nothing.
DEFAULT_CAMPUS_SLUG = "st-george"


def _get(url: str, params: dict | None = None) -> str:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_group(post_id: int) -> dict:
    return json.loads(_get(f"{settings.sop_base_url}/wp/v2/group/{post_id}"))


# SOP's event calendar (/tribe/events/v1/events) lives in
# app/services/event_sources/sop_events.py, not here. This module syncs clubs; an
# event needs the future-date filter, the publish gate and the organizer-contact
# scrub that event_sources/runner.py applies, and none of that belongs in a club
# scraper. Run it with:
#     python -m app.services.event_sources.runner sop_events --dry-run


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", html_lib.unescape(s).lower())


def find_group_by_search(name: str, per_page: int = 20) -> dict | None:
    """Fuzzy fallback for clubs not yet in TARGET_GROUP_IDS. Verify the match by eye
    (print the result) before trusting it and adding its id to TARGET_GROUP_IDS --
    SOP's search ranks badly for full multi-word titles.
    """
    body = _get(f"{settings.sop_base_url}/wp/v2/group", {"search": name, "per_page": per_page})
    results = json.loads(body)
    target = _norm(name)
    for r in results:
        if _norm(r["title"]["rendered"]) == target:
            return r
    for r in results:
        t = _norm(r["title"]["rendered"])
        if target in t or t in target:
            return r
    return None


def fetch_group_page(page: int, per_page: int = LIST_PER_PAGE) -> list[dict]:
    """One page of /wp/v2/group, or [] once you run off the end.

    Verified 2026-09-19: the LIST response carries everything build_club() needs --
    meta._oasis_original, acf._expiration-date, class_list, content.rendered, link --
    so discovery never needs a follow-up fetch_group() per club.

    SOP strips the X-WP-Total/X-WP-TotalPages headers WP normally sends, so there's no
    total to read up front; page until a short or empty batch comes back. Ordering by
    title (not the default date) keeps runs reproducible as SOP re-lists groups.
    """
    params = {"per_page": per_page, "page": page, "orderby": "title", "order": "asc"}
    try:
        body = _get(f"{settings.sop_base_url}/wp/v2/group", params)
    except urllib.error.HTTPError as exc:
        # WP answers rest_post_invalid_page_number with a 400 past the last page.
        if exc.code == 400:
            return []
        raise
    return json.loads(body)


def discover_groups(*, per_page: int = LIST_PER_PAGE, max_pages: int | None = None) -> list[dict]:
    """Page through the whole group catalog. REST only -- no HTML scraping here."""
    records: list[dict] = []
    page = 1
    while max_pages is None or page <= max_pages:
        batch = fetch_group_page(page, per_page)
        if not batch:
            break
        records.extend(batch)
        print(f"[sop_sync] discovered page {page}: {len(batch)} groups ({len(records)} total)")
        if len(batch) < per_page:
            break
        page += 1
        time.sleep(LIST_THROTTLE_SECONDS)
    return records


def _original_id(record: dict) -> str:
    """Dedupe key per CLAUDE.md: copy-of-* yearly re-listings carry a nonzero
    _oasis_original pointing at the original post id; collapse onto that."""
    oasis = (record.get("meta") or {}).get("_oasis_original") or 0
    return str(oasis) if oasis else str(record["id"])


def _slugs(record: dict, prefix: str) -> list[str]:
    return [c[len(prefix):] for c in record.get("class_list", []) if c.startswith(prefix)]


def _parse_expiry(raw: str) -> datetime:
    """SOP's "_expiration-date" string -> aware UTC.

    SOP doesn't say which zone this is in. It has always been compared as UTC here, and
    the worst case is a listing dropping or lingering a few hours around its expiry, so
    that behaviour is kept rather than guessed at.
    """
    return as_utc(datetime.strptime(raw, "%Y-%m-%d %H:%M:%S"))


def is_expired(record: dict, now: datetime | None = None) -> bool:
    exp_raw = (record.get("acf") or {}).get("_expiration-date")
    if not exp_raw:
        return False
    return _parse_expiry(exp_raw) < (now or utc_now())


def collapse_duplicates(records: list[dict]) -> list[dict]:
    """Collapse copy-of-* re-listings onto one record per _oasis_original.

    Runs at DISCOVERY time, before any scrape or enrichment -- CLAUDE.md's "dedupe
    before enrichment" rule. Keeps the most recently modified re-listing, which is the
    one whose description and links are current.
    """
    by_key: dict[str, dict] = {}
    for record in records:
        key = _original_id(record)
        current = by_key.get(key)
        if current is None or record.get("modified", "") > current.get("modified", ""):
            by_key[key] = record
    return list(by_key.values())


MIN_PER_AREA = 8


def select_diverse(records: list[dict], limit: int | None, *, min_per_area: int = MIN_PER_AREA) -> list[dict]:
    """Sample across primary interest area: proportional, with a floor per area.

    Straight pagination order is lopsided (alphabetical here, date by default), and a
    lopsided sample is what makes a match demo look broken -- every blurb comes back
    with the same cluster of clubs.

    Plain round-robin overcorrects, though: 'academic' is ~half the St. George catalog,
    and flattening it to 1/17th of the sample starves the single most likely demo query
    ("first-year CS..."). So each area gets its proportional share, floored at
    min_per_area so niche areas (faith, media, environment) never vanish entirely.
    """
    if limit is None or len(records) <= limit:
        return records

    buckets: dict[str, list[dict]] = {}
    for record in records:
        areas = _slugs(record, "areas_of_interest-") or ["uncategorized"]
        buckets.setdefault(areas[0], []).append(record)
    for bucket in buckets.values():
        bucket.sort(key=lambda r: r.get("title", {}).get("rendered", ""))

    total = len(records)
    quotas = {
        key: min(len(bucket), max(min_per_area, round(limit * len(bucket) / total)))
        for key, bucket in buckets.items()
    }

    # Floors and rounding push the total off `limit`; settle up against each area's
    # headroom, taking from (or giving back to) the largest areas first.
    by_size = sorted(buckets, key=lambda k: len(buckets[k]), reverse=True)
    while sum(quotas.values()) > limit:
        for key in by_size:
            if sum(quotas.values()) <= limit:
                break
            if quotas[key] > min_per_area:
                quotas[key] -= 1
    while sum(quotas.values()) < limit:
        headroom = [k for k in by_size if quotas[k] < len(buckets[k])]
        if not headroom:
            break
        for key in headroom:
            if sum(quotas.values()) >= limit:
                break
            quotas[key] += 1

    selected: list[dict] = []
    for key in sorted(buckets):
        selected.extend(buckets[key][: quotas[key]])
    return selected


def parse_group_page_links(page_html: str) -> dict:
    """Website, contact email, socials, constitution PDF -- not exposed via REST."""
    soup = BeautifulSoup(page_html, "lxml")

    def find_h3(text: str):
        return soup.find("h3", string=lambda s: s and s.strip() == text)

    def anchors_until_next_h3(start_h3):
        # Direct-sibling <a> tags belong to this section; a sibling <h3> ends it.
        # (find_next_siblings("a") alone isn't bounded by the next heading and will
        # walk into later sections -- e.g. it grabbed the Constitution PDF link and
        # overwrote the real website URL until this boundary check was added.)
        for sib in start_h3.find_next_siblings():
            if sib.name == "h3":
                break
            if sib.name == "a" and sib.get("href"):
                yield sib

    links: dict = {}

    contact_h3 = find_h3("Contact")
    if contact_h3:
        for a in anchors_until_next_h3(contact_h3):
            href = a["href"]
            if href.startswith("mailto:"):
                links["email"] = href.removeprefix("mailto:")
            else:
                links["website"] = href

    socials_h3 = find_h3("Socials")
    if socials_h3:
        container = socials_h3.find_next_sibling("div")
        if container:
            for a in container.find_all("a", href=True):
                href = a["href"]
                if "facebook.com" in href:
                    links["facebook"] = href
                elif "instagram.com" in href:
                    links["instagram"] = href

    constitution_h3 = find_h3("Group Constitution")
    if constitution_h3:
        pdf = constitution_h3.find_next("a", href=True)
        if pdf:
            links["constitution_pdf"] = pdf["href"]

    return links


def _parse_taxonomy_tags(class_list: list[str]) -> tuple[str | None, list[str]]:
    """class_list carries taxonomy slugs directly -- cheaper than resolving term IDs
    via the group_type/campus/areas_of_interest taxonomy endpoints."""
    campus = None
    areas: list[str] = []
    for c in class_list:
        m = _CAMPUS_CLASS_RE.fullmatch(c)
        if m:
            campus = m.group(1).replace("-", " ").title()
        m = _AREA_CLASS_RE.fullmatch(c)
        if m:
            areas.append(m.group(1).replace("-", " ").title())
    return campus, areas


def _strip_html(raw: str) -> str:
    return BeautifulSoup(raw, "lxml").get_text(separator=" ", strip=True)


def build_club(record: dict, links: dict) -> Club:
    acf = record.get("acf", {})
    sop_id = str(record["id"])
    sop_original_id = _original_id(record)

    campus, areas = _parse_taxonomy_tags(record.get("class_list", []))

    listing_expires = None
    exp_raw = acf.get("_expiration-date")
    if exp_raw:
        listing_expires = _parse_expiry(exp_raw)

    return Club(
        sop_id=sop_id,
        sop_original_id=sop_original_id,
        name=html_lib.unescape(record["title"]["rendered"]),
        campus=campus,
        sop_interest_areas=areas,
        description_raw=_strip_html(record.get("content", {}).get("rendered", "")),
        links=links,
        sop_url=record.get("link"),
        listing_expires=listing_expires,
        last_updated=utc_now(),
        source="sop",
    )


def upsert_club(session: Session, club: Club) -> Club:
    """Dedupe on sop_original_id, not sop_id -- collapses copy-of-* re-listings onto
    one row instead of creating a new "club" every year SOP re-lists it."""
    existing = session.exec(
        select(Club).where(Club.sop_original_id == club.sop_original_id)
    ).first()
    if existing:
        for field in (
            "sop_id", "name", "campus", "sop_interest_areas", "description_raw",
            "links", "sop_url", "listing_expires", "last_updated",
        ):
            setattr(existing, field, getattr(club, field))
        session.add(existing)
        return existing
    session.add(club)
    return club


def _already_synced(session: Session) -> set[str]:
    """sop_original_ids that already have a scraped description -- safe to skip.

    This is what makes the run resumable: the ~1s-per-club HTML scrape is the expensive
    part, so a re-run after a crash (or a bumped --limit) only pays for what's new.
    """
    rows = session.exec(
        select(Club.sop_original_id).where(Club.description_raw.is_not(None))
    ).all()
    return {r for r in rows if r}


def plan_sync(
    *,
    limit: int | None = DEFAULT_LIMIT,
    campus_slug: str | None = DEFAULT_CAMPUS_SLUG,
    max_pages: int | None = None,
) -> list[dict]:
    """Discover -> filter expired/off-campus -> collapse re-listings -> sample.

    All REST, no scraping, so it's cheap to run on its own (--discover-only) to see what
    a given --limit would actually pull before committing to the scrape.
    """
    records = discover_groups(max_pages=max_pages)
    print(f"[sop_sync] discovered {len(records)} groups")

    now = utc_now()
    live = [r for r in records if not is_expired(r, now)]
    print(f"[sop_sync] {len(records) - len(live)} expired listing(s) dropped")

    if campus_slug:
        on_campus = [r for r in live if campus_slug in _slugs(r, "campus-")]
        print(f"[sop_sync] {len(live) - len(on_campus)} off-campus group(s) dropped "
              f"(keeping campus-{campus_slug})")
        live = on_campus

    collapsed = collapse_duplicates(live)
    print(f"[sop_sync] {len(live) - len(collapsed)} copy-of-* re-listing(s) collapsed "
          f"-> {len(collapsed)} unique clubs")

    # The demo clubs are pinned: they bypass the sample so rehearsals can't lose them.
    target_ids = {str(i) for i in TARGET_GROUP_IDS}
    pinned = [r for r in collapsed if str(r["id"]) in target_ids or _original_id(r) in target_ids]
    pinned_ids = {r["id"] for r in pinned}
    rest = [r for r in collapsed if r["id"] not in pinned_ids]

    remaining = None if limit is None else max(limit - len(pinned), 0)
    selected = pinned + select_diverse(rest, remaining)
    print(f"[sop_sync] selected {len(selected)} club(s) "
          f"({len(pinned)} pinned + {len(selected) - len(pinned)} sampled)")
    return selected


def sync(
    *,
    limit: int | None = DEFAULT_LIMIT,
    campus_slug: str | None = DEFAULT_CAMPUS_SLUG,
    targets_only: bool = False,
    force: bool = False,
    max_pages: int | None = None,
) -> None:
    init_db()

    if targets_only:
        records = []
        for post_id in TARGET_GROUP_IDS:
            records.append(fetch_group(post_id))
            time.sleep(DETAIL_THROTTLE_SECONDS)
        records = [r for r in records if not is_expired(r)]
    else:
        records = plan_sync(limit=limit, campus_slug=campus_slug, max_pages=max_pages)

    with Session(engine) as session:
        done = set() if force else _already_synced(session)
        if done:
            print(f"[sop_sync] {len(done)} club(s) already scraped -- skipping (use --force to redo)")

        scraped = 0
        for index, record in enumerate(records, start=1):
            name = html_lib.unescape(record.get("title", {}).get("rendered", "?"))
            if _original_id(record) in done:
                continue

            try:
                links = parse_group_page_links(_get(record["link"]))
            except (urllib.error.URLError, urllib.error.HTTPError) as exc:
                # One unreachable club page shouldn't kill a 250-club run.
                print(f"[sop_sync] FAILED {name!r}: {exc}")
                continue

            saved = upsert_club(session, build_club(record, links))
            scraped += 1
            print(f"[sop_sync] [{index}/{len(records)}] synced {saved.name!r} "
                  f"(sop_original_id={saved.sop_original_id})")

            if scraped % COMMIT_EVERY == 0:
                session.commit()
            time.sleep(DETAIL_THROTTLE_SECONDS)  # SOP etiquette: throttle between requests

        session.commit()
        print(f"[sop_sync] done -- {scraped} club(s) scraped this run")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync UofT clubs from SOP into SQLite")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Max clubs to sync (default: {DEFAULT_LIMIT})")
    parser.add_argument("--all", action="store_true",
                        help="Sync the whole catalog (~1,250 clubs, 40-60 min). Overrides --limit")
    parser.add_argument("--campus", default=DEFAULT_CAMPUS_SLUG,
                        help=f"Campus taxonomy slug, or 'any' (default: {DEFAULT_CAMPUS_SLUG})")
    parser.add_argument("--targets-only", action="store_true",
                        help="Sync just TARGET_GROUP_IDS -- the fast path for testing")
    parser.add_argument("--force", action="store_true",
                        help="Re-scrape clubs already in the database")
    parser.add_argument("--discover-only", action="store_true",
                        help="Show what would be synced without scraping anything")
    args = parser.parse_args()

    limit = None if args.all else args.limit
    campus = None if args.campus == "any" else args.campus

    if args.discover_only:
        plan_sync(limit=limit, campus_slug=campus)
        return

    sync(limit=limit, campus_slug=campus, targets_only=args.targets_only, force=args.force)


if __name__ == "__main__":
    main()
