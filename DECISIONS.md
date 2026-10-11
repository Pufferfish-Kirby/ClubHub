# Decisions

Running log of choices that shaped Campus Compass, so they can be revisited on purpose
rather than rediscovered by accident. `CLAUDE.md` says *what the rules are*; this says
*why, and who called it*.

Each entry: the call, the reasoning, what was rejected, and current status.
**Owner** marks who decided — `James` for product/scope calls, `implementation` for
choices made downstream of one.

---

## 2026-09-19 — Data pipeline session

### D1. Sync 250 clubs, not the full catalog
**Owner:** James · **Status:** active, reversible

SOP has 1,309 groups; 897 are unique St. George clubs after dropping off-campus
listings and collapsing `copy-of-*` re-listings. We sync a 250-club sample.

**Why:** the full scrape is 40–60 minutes of wall clock (one HTML fetch per club plus a
1s etiquette throttle, and it can't be parallelised without violating SOP etiquette).
The demo only ever displays 5–8 results, so the marginal club past a few hundred buys
very little. *"Since this is a hackathon I think for time's sake we don't need
everything."*

**Rejected:** all ~1,300 (too slow for the value); ~100 (match results would feel
repetitive across different blurbs).

**To revisit:** `python -m app.services.sop_sync --all`. The sync is resumable — it
skips clubs already scraped — so raising the limit later only pays for what's new.

---

### D2. Sample proportionally across interest areas, with a floor
**Owner:** implementation · **Status:** active

Each SOP interest area gets its proportional share of the 250, floored at
`MIN_PER_AREA` so niche areas never vanish.

**Why:** pagination order is alphabetical, so a naive first-250 inherits that bias. The
first attempt used plain round-robin, which looked fairer but cut `academic` from 51% of
the pool to 10% of the sample — and `academic` is what the demo's own example blurb
("first-year CS, want internships and friends") targets. Proportional-with-floor keeps
`academic` at 120/250 while still guaranteeing ~20 each for media, faith, and athletics.

**Rejected:** first-N (alphabetical bias); pure round-robin (starves the biggest and
most-queried category).

---

### D3. Tags are a controlled vocabulary; outcomes already were
**Owner:** James · **Status:** active

`FIXED_TAGS` in `models.py` — 51 terms. Enrichment picks 3–8 via a JSON-schema enum.

**Why:** free-form tags drifted badly. A 25-club pilot produced 148 unique tags across
177 assignments — only 14% reused — with near-duplicates like `academic` /
`academic inquiry` / `academic support` and `culture` / `afghan culture` /
`african culture`. Unusable for filtering or keyword matching. The controlled enum gives
51 unique across 1,298 assignments at **100% reuse**.

**The principle that made this safe:** a tag is a *classification*, not a claim about
the club, so choosing from a fixed list doesn't violate "never invent detail." Summary
and commitment stay strictly source-grounded.

**Rejected:** post-hoc tag clustering (more machinery, same result later).

---

### D4. Club contact emails stay visible — including personal student addresses
**Owner:** James · **Status:** active — **deliberate, do not "fix"**

`Club.links.email` is populated for 242/250 clubs. About 28 are individual students'
addresses (`firstname.lastname@mail.utoronto.ca`) rather than role accounts. They are
served through the API and shown in the UI.

**Why:** SOP publishes them as the club's official contact, and "how do I actually reach
this club" is the point of the product. *"If they do provide a student email, it is
still the email and the contact so we need to follow through with that."*

**This is a different data path from the hard constraint.** CLAUDE.md's "never display
event-organizer emails" covers SOP's *event* data, where whoever organised an event has
their address attached without publishing it as a contact point. That rule still stands
and applies once events are ingested.

**Revisit if:** a club asks for removal, or the project goes past a demo audience.

---

### D5. Every club must carry at least one outcome
**Owner:** James · **Status:** active

When Claude returns no outcomes, `derive_outcomes_from_tags()` fills them from the
club's tags and sets `Club.outcomes_derived = True`. It never overrides outcomes the
model did assign.

**Why:** the personal graph is `you -> outcomes -> clubs`, so a club with no outcome has
no edge and can never be drawn. 14 of 250 came back empty — all thin listings, which is
the prompt working as instructed, not a bug.

**Why tags rather than `sop_interest_areas`:** 12 of the 14 had SOP areas, but all 14
had tags (minimum tag count across the catalog is 1). Tags cover 100% of cases; SOP
areas would have left 2 clubs permanently invisible in the graph.

**Rejected:** bounded web search to fill the gap. It would have covered *zero* clubs the
tag mapping doesn't already cover, while adding an unverified provenance path — a
similarly-named org at another university is an easy mismatch, and a confidently wrong
club description is the worst failure to have on stage. Still worth revisiting for data
SOP genuinely lacks (current meeting times, recent events) — that's enrichment, not
gap-filling.

---

### D6. Keep `commitment` conservative, even at 37% `unknown`
**Owner:** implementation · **Status:** active — revisit if matching suffers

The enrichment prompt forbids inferring commitment, so 93/250 clubs are `unknown`
(`moderate` 121, `casual` 30, `intense` 6).

**Why:** loosening it trades the "never invent detail" guarantee for a field the
reranker can judge better anyway — at query time Sonnet sees the student's blurb *and*
the club summary, so "not too intense" is better resolved there than guessed during
enrichment.

**Watch for:** blurbs about intensity returning poor matches. That's the signal this was
the wrong call.

---

### D7. Embed the summary *and* the raw description
**Owner:** implementation · **Status:** active

`club_to_document()` includes both, truncated to 1,500 chars — it previously used
summary *or* description.

**Why:** a direct consequence of D3. Once tags became a controlled vocabulary they
stopped carrying club-specific words, and the old `elif` dropped the raw description as
soon as a club was enriched. That would have stripped "k-pop", "poker", and "symphonic
band" out of the vectors entirely — exactly the terms students search by.

---

### D8. Schema changes need an entry in `_COLUMN_MIGRATIONS`
**Owner:** implementation · **Status:** active

`init_db()` applies idempotent `ALTER TABLE`s from a table in `database.py`.

**Why:** `SQLModel.metadata.create_all()` creates missing *tables* but never alters an
existing one, and `backend/data/campus_compass.db` is checked into git. Adding
`outcomes_derived` (D5) without this would have handed anyone who pulled the change a
stale table and `no such column` on their next query.

---

## 2026-09-19 — Why this project, and the stack calls that followed

These four fill in the gap before D1: not *how* the data pipeline works, but *why this
idea* and *why this stack* — the "inspiration" and "how we built it" material the log
above doesn't capture on its own.

### D9. Build the match-and-graph + Dropbox-drop-to-update idea, not a club directory
**Owner:** James · **Status:** locked (demo scope)

Grounded in the original problem framing (`central-hub-for-clubs.md`): SOP already
lists ~1,250 groups with keyword search across 17 broad categories, and as of
2026-09-19 its events API returned **9 upcoming events for the entire campus** — real
event info lives on Instagram posters, PDFs, and word of mouth, not in any structured
system. Club fair is hectic; students find clubs through friends, not the portal.

**Why this shape:** two flows map directly onto the Dropbox challenge prompt instead of
just adjacent to it — matching students to clubs by *goals* answers "connecting
students, clubs, events, and goals," and Dropbox-drop-to-update answers "turn files
into action" / "turn digital chaos into something useful." The personal graph (you →
outcomes → clubs → events) is the "relationship graph / self-organizing content" angle,
kept small on purpose (see the graph-size hard constraint in CLAUDE.md).

**Rejected:** a plain club search/directory tool — technically useful, but doesn't
demonstrate turning unstructured files into structured, actionable data, which is what
the prompt actually judges. Full StudentOS scope (courses, professors, research labs as
graph nodes) — real long-term vision, not a 24h build; pushed to roadmap.

---

### D10. Demo commits to three flows; everything else is roadmap-only
**Owner:** James · **Status:** locked

Must-have list frozen at: SOP sync + dedupe, AI enrichment, the match flow (blurb →
ranked clubs), the personal graph view, and Dropbox drop-to-update. Auth,
notifications, Instagram/Discord/LinkedIn scraping, multi-campus polish, and
courses/professors/labs-as-graph-nodes are explicitly out of scope for the build.

**Why:** a hackathon demo is judged on flows that work flawlessly live, not on feature
count — one flaky flow (auth breaking mid-demo, say) costs more than a fourth feature
gains. Same reasoning as D1's data-volume cut ("since this is a hackathon... we don't
need everything"), applied to features instead of row count.

**Revisit:** post-demo, against the Roadmap section of `central-hub-for-clubs.md` (club
exec dashboard, full StudentOS graph nodes, weekly digest, multi-campus).

---

### D11. Voyage AI over local sentence-transformers for embeddings
**Owner:** James · **Status:** active

The spec left this open: Claude has no embeddings API, so it was Voyage AI or a local
`sentence-transformers` model (`all-MiniLM-L6-v2`) — the latter needs no extra key and
works offline.

**Why:** two reasons, one dev-time and one deployment-shape. First, prior hands-on
experience with Voyage's API meant faster, more confident setup under a 24h clock than
learning a new local-inference path from scratch. Second, a local embedding model has
to load its weights into memory on the deployed backend — on Railway's smaller tiers
that's a real memory cost — where Voyage keeps the backend a thin API client instead,
and query-time semantic search comes back faster over the network call than local
inference would on constrained hardware.

**Rejected:** local `sentence-transformers` — no API key, works offline, but loses on
both deployment memory footprint and setup speed under time pressure.

**Trade-off accepted:** an extra API key (`VOYAGE_API_KEY`) and embeddings pinned to
Voyage's model versioning — changing models means re-indexing every record (see
CLAUDE.md's Claude-usage section).

---

### D12. @xyflow/react over react-force-graph-2d for the personal graph
**Owner:** implementation (frontend) · **Status:** active

Spec allowed either — "force graph for the vibe, React Flow if layout gets messy."

**Why:** the personal graph has a fixed logical shape (you → outcomes → clubs → next
events, in that layer order) that a physics-based force simulation doesn't guarantee —
nodes can drift, overlap, or swap layers frame to frame. React Flow's controlled node
positioning keeps the four layers visually distinct on every render, which matters more
for a graph meant to be read at a glance in a live demo than the organic,
Obsidian-style look a force simulation gives for free.

**Rejected:** react-force-graph-2d — closer to the literal "Obsidian-style" vibe named
in the pitch, but controlled legibility won for a graph capped at ~15-25 nodes that
still has to read correctly on stage.

---

## 2026-10-11 — Time handling

### D13. Store UTC, read and show in the school's zone
**Owner:** James · **Status:** active

Every datetime is timezone-aware UTC in the database and between services
(`app/timeutil.py`). The school's zone is one setting, `school_timezone`
(default `America/Toronto`), used only to interpret wall-clock times from posters and
SOP and to show times to students.

**Why:** SQLModel 0.0.46+ rejects naive datetimes on write, which broke 7 of 15 tests
on a fresh install. The old code did `astimezone(utc).replace(tzinfo=None)` to match
the naive column. Aware UTC is also what makes a second school in another zone possible
without ambiguity.

**An IANA name, not "EST":** Toronto is UTC-5 in winter and UTC-4 in summer. A fixed
EST offset would put every event from March to November an hour off.

**Existing data is untouched.** Old rows are naive UTC text; SQLModel reads them back as
aware UTC, and `tests/test_timeutil.py` pins that behavior. No migration was needed.

**Not done yet:** the frontend still hardcodes `America/Toronto` in `format.js`. That
needs to come from the API once a second school exists.

**Rejected:** rewriting stored values with an explicit offset (touches the checked-in
DB for no behavior change); a per-school timezone column (a schema change, and adding a
school is ask-first).

---

### D14. Pin backend dependencies to exact versions
**Owner:** James · **Status:** active

`requirements.txt` uses `==` pins, set to the versions the test suite passes on.

**Why:** unpinned, the resolver can hand a fresh install or a redeploy a different
SQLModel than the code was written against. That is how D13's bug arrived: it only
showed up where 0.0.46+ resolved, and it passed on the same machine with 0.0.44.
Production (Render) rebuilds from this file, so it had the same exposure.

**To upgrade:** bump a pin on purpose, run `pytest`, and say so in the PR. Don't
loosen to ranges. `pytest` stays out of the file (CI and the cloud setup script add it).

**Rejected:** a lockfile (pip-tools/uv) for now. It would pin transitive packages too,
but adds a tool and a workflow step; revisit if a transitive bump ever bites.

---

## Accomplishments (verified 2026-09-19)

Measured results the decisions above actually produced — the "accomplishments" raw
material:

- **250 clubs synced** from SOP (897 unique St. George clubs discovered after dedupe),
  via the proportional sample from D2 — keeps `academic` properly represented, which is
  what the demo's own example blurb targets.
- **250/250 synced clubs enriched**, and **0 with missing outcomes** — D5's
  tag-derived fallback closed what was originally a 14-club gap in the you→outcomes→
  clubs graph.
- **Tag vocabulary reuse went from 14% to 100%** (D3): a 25-club free-form pilot
  produced 148 unique tags across 177 assignments; the controlled `FIXED_TAGS` enum
  gives 51 unique tags across 1,298 assignments, all reused.
- **`POST /recommend` built end-to-end**: Voyage/Chroma vector search for ~20
  candidates, then a Sonnet rerank to 5-8 clubs with a "why it fits" line and the
  student's own outcomes for the graph, with both network calls pushed off the event
  loop (R1).
- **Personal graph and club detail panel built and working** on the frontend —
  24 commits on `frontend/sprint-6` (graph layout, panel, empty/error states) — though
  not yet merged into `main` as of this session (O2 below).

---

## Open — needs a decision

### O1. `GET /api/clubs` still returns two hardcoded fake clubs
`clubs.py` uses the in-memory `ClubRepository` seed ("Robotics Club", "Outdoors &
Hiking Club"), while `GET /api/clubs/{id}` reads real SQLite — so the list returns clubs
that 404 on detail. The seed's own docstring says to swap it once sop_sync runs, which
it now has.

**Needs:** confirmation that the frontend isn't already coded against the current shape,
and a call on whether the list paginates. Recommended approach keeps the `ClubDTO`
response shape byte-compatible so nothing downstream breaks.

**Status update (2026-09-19, later same day):** resolved — `clubs.py` now queries
`Club` from SQLite directly; the docstring documents the old bug rather than repeating
it. Left here rather than moved to "Resolved elsewhere" since the fix landed without a
dedicated log entry of its own.

---

### O2. `frontend/sprint-6` was never merged into `main`
`main`'s `frontend/src` currently has no `App.jsx` — it was removed in commit `458cd9d`
("deleted avi frontend"), but `main.jsx` still imports it, so `npm run dev` on `main`
does not build right now. All the working frontend code (the graph, club detail panel,
empty/error states) lives on `origin/frontend/sprint-6` instead, which branched before
the backend rebase and so has neither `POST /recommend` nor the Dropbox pipeline work.

**Needs:** a real merge between the two lines, done with whoever owns each side rather
than one branch overwriting the other blind — both sides changed substantially since
they diverged.

---

### O3. Dropbox drop-to-update has never run end-to-end
`Event` and `IngestLog` are both empty in the live database (verified 2026-09-19) — the
pipeline (`dropbox_watcher` → `extraction` → `matcher` → `ingest`) is fully written but
unproven. `POST /upload` (`routers/upload.py`) — a newer, undocumented endpoint that
lets a user photograph a flier directly — reuses `ingest.process_file()`, so it shares
the same unverified-path risk.

**Needs:** an actual poster or PDF pushed through (Dropbox inbox or `/upload`) before
the demo, to confirm extraction, club matching, and the provenance link all work. The
original spec already flags wrong-date extraction as the single most likely live-demo
failure mode — this is that risk, still unretired.

---

### O4. `POST /api/events/{id}/confirm` has no authentication
Anyone who knows an event ID can overwrite its start time and publish it, and the
response returns the raw `Event` row (including internal `confidence`). Found by the
automated security review on the D13 change; the behavior predates it.

**Needs:** a decision, because the fix is on the ask-first list (accounts/auth or a
per-upload token). Suggested shape: a one-time confirm token created at upload and checked
with `hmac.compare_digest`, a `409` unless the event is `pending_review`, and a
`PublicEvent` response.

---

## Resolved elsewhere

### R1. `POST /recommend` — implemented
Built outside this log (`routers/recommend.py` + `services/reranker.py`): vector search
for ~20 candidates, then Sonnet reranks to 5–8 with a "why it fits" line each, plus a
`next_event` per club for the graph's outer layer. Both network calls are pushed off the
event loop with `asyncio.to_thread`, and an empty candidate list returns an empty
response rather than a broken graph.

**Rationale not captured here** — worth a short entry from whoever wrote it, especially
on how the student's `outcomes` are chosen for the graph's middle layer.
