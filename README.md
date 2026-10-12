# ClubHub

**Find the clubs that actually fit you — as a map, not a list — and watch it update live
the moment a club posts something new.**

Built at HackMIT. The pilot deployment uses University of Toronto data; the problem is
the same on almost every campus.

## The problem

Universities have an club directory, a student-union site,
a registrar page, or something to answer one question well: *"does a club named
X exist?"* They do not answer the question a new student actually has: *"which of these
hundreds of groups should **I** join?"*

The gap shows up in three places:

1. **Discovery is keyword search and broad categories.** A first-year who wants
   internships, friends, and a low time commitment has to guess which of 17 interest
   areas to click through — and still read hundreds of blurbs by hand.
2. **Event listings are sparse and stale.** Official portals often carry a tiny fraction
   of what is actually happening. Real event info lives on Instagram posters, PDFs on
   hallway corkboards, Linktree pages, and word of mouth — not in any structured system
   a new student can search.
3. **Club fairs are a firehose.** Most students end up joining whatever their friends
   already did.

ClubHub treats this as a campus-wide data problem, not a single-school quirk. The
architecture is source-agnostic: pull what the official portal exposes, enrich it with
AI, and layer in unstructured updates (posters, PDFs) as they appear.

## What ClubHub does

Three things, done well, rather than ten things done halfway:

### 1. Match — a blurb becomes a ranked, reasoned shortlist

Type what you want out of university in plain English —
*"first-year CS, want internships and friends, not too intense"* — and get back 5–8
clubs, ranked, each with one sentence tying something concrete in the club's own
description to something you actually said. No generic hype; a club that can only get a
generic sentence written about it doesn't make the list.

Under the hood: Voyage embeddings narrow the enriched catalog down to the ~20 nearest by
meaning, then Claude (Sonnet) reads the candidates against your blurb, drops the ones
that don't genuinely fit, and writes the "why."

### 2. The constellation — your matches as a small, living graph

Results render as a personal star graph — **you → your outcomes → clubs → their next
events** — instead of a list of cards. It's deliberately small (15–25 nodes, never the
full catalog hairball) and it reacts: hovering a club or an outcome lights the exact
path that explains it, stars carry a soft cursor-reactive physics, and a club's own
detail panel opens beside the graph instead of replacing it.

Want to look past your own 5–8 matches? **Full Directory** opens the entire enriched
catalog in its own tab — filterable by tag, commitment level, skills-to-build, or
professional field — with its own independent search box that builds its *own* graph,
so exploring never disturbs the constellation you already have.

### 3. Drop a poster, watch it become an event

A club drags a flyer into a shared Dropbox folder (or a student uploads one straight in
the app). Claude reads it — vision for images, native document input for PDFs — and
extracts the event: title, start time, location, RSVP link. If the poster is clear and
Claude is confident, it's live within seconds, badged **"from Dropbox"** with a link back
to the source file. If a date is blurry or ambiguous, the app **asks the person who just
uploaded it** to fill in exactly the missing piece — never a silent queue nobody looks
at, and never a fabricated date on the live feed.

## Why it's built this way

- **The graph stays small on purpose.** A force-directed hairball of every club on
  campus is a worse UI than a search box. The personal graph earns the "creative
  visualization" only because it's scoped to *your* results.
- **Two ingestion paths write to the same tables, honestly.** A synced official listing
  and a Dropbox-extracted poster both become `Club`/`Event` rows, tagged by `source`, so
  the app never has to pretend one is more real than the other.
- **A wrong date is worse than no date.** Auto-publish requires a title, a resolved
  start, a time of day, *and* model confidence — anything short of that goes to a human
  for one quick confirmation instead of guessing.
- **Tags are a controlled vocabulary, not free text.** An early pilot with free-form tags
  produced 148 near-duplicate tags across 25 clubs ("academic" / "academic inquiry" /
  "academic support"). A fixed 51-term enum made tags reusable and made filtering in Full
  Directory possible at all.
- **New campuses are adapters, not rewrites.** Official portal sync, club-site event
  scrapers, and Dropbox ingestion all land in the same tables through a shared adapter
  contract — see `.cursor/skills/scraping-club-events/` and
  `backend/app/services/event_sources/`.

The full reasoning — and the incidents that produced each rule — is in
[`DECISIONS.md`](./DECISIONS.md). The condensed, code-facing spec (data model,
architecture diagram, hard constraints) is in [`CLAUDE.md`](./CLAUDE.md). The original
brainstorm is [`central-hub-for-clubs.md`](./central-hub-for-clubs.md).

## Architecture

```
Official club portal API ──┐                    ┌── enrichment (Haiku) ── embeddings (Voyage) ── ChromaDB
  (e.g. SOP at UofT)        ├─→ portal sync ────┤
                            │   (dedupe, sample) └────────────────────────────────────────┐
                            │                                                             ▼
Dropbox Inbox ──────────────┴─→ dropbox_watcher ─→ extraction (Sonnet) ─→ matcher ─→  SQLite (Club, Event)
   (or in-app upload)                            vision / PDF input     (fuzzy,        │
                                                  confidence-gated       never guesses) │
                                                  auto-publish rule                    │
                                                                                        ▼
                                                            FastAPI: /recommend  /clubs  /events  /upload
                                                                                        │
                                                                                        ▼
                                                    React + @xyflow/react: the star graph, Full Directory

Club / campus event sites ──→ event-source adapters (JSON endpoints) ──→ same runner ──→ SQLite
  (skill: scraping-club-events)
```

## Tech stack

| | |
|---|---|
| **Backend** | FastAPI · SQLModel (SQLite) · ChromaDB · Anthropic (Claude Sonnet + Haiku) · Voyage AI embeddings · Dropbox SDK |
| **Frontend** | React + Vite (JavaScript, no TypeScript) · Tailwind v4 · `@xyflow/react` for the star graph |

## Quickstart

**Backend**
```bash
cd backend
python -m venv .venv
source .venv/bin/activate       # .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env            # fill in ANTHROPIC_API_KEY, VOYAGE_API_KEY, DROPBOX_*
uvicorn app.main:app --reload --port 8000
```
The Dropbox watcher starts with the app and re-scans the inbox on every `--reload`
restart. If you're iterating on backend code and don't want that burning Claude calls,
set `DROPBOX_WATCHER_ENABLED=0` first.

**Frontend**
```bash
cd frontend
npm install
npm run dev                     # http://localhost:5173, proxies /api -> :8000
```
Needs Node ≥ 20.19 (or ≥ 22.12) — the graph's build tooling (`vite`/`rolldown`) won't
install its native binding on an older Node and fails with a confusing "optional
dependencies" error. `node --version` first if `npm run dev` won't start.

**Data**: `backend/data/campus_compass.db` is checked into git with a sample catalog
already synced and enriched for the UofT pilot. `backend/data/chroma/` (the vector
index) is *not* checked in — if `/recommend` comes back empty or thin, index it once:
```bash
cd backend && python -m app.services.enrichment_batch --index-only --limit 250
```

To point ClubHub at another school, swap the portal sync config and add event-source
adapters for that campus's calendars — the match, graph, and Dropbox flows stay the
same.

## Project layout

```
frontend/   React + Vite. src/screens for pages, src/components for the graph/cards/panels,
            src/lib for the API client, filtering, physics and layout math.
backend/    FastAPI + SQLModel. app/routers for the four endpoints, app/services for
            portal sync, Dropbox, Claude, Voyage, and pluggable event-source adapters.
```

## Status

Both the backend and the core frontend are built and running against real data — this
isn't scaffolding. Concretely:

- **250 clubs** in the pilot catalog (sampled proportionally across interest areas from
  UofT's official portal), **100% enriched** (summary, outcomes, tags, commitment) and
  embedded for search.
- **`/recommend`** is live: vector search + Claude rerank, tested end to end against the
  real database, plus offline unit tests that stub the model and vector calls.
- **The constellation and Full Directory** are both built, sharing one soft-physics graph
  and one hover/highlight model between the graph, the cards, and the club detail panel.
- **The Dropbox pipeline** (watcher, extraction, fuzzy club matching, confidence-gated
  publish, human-confirm for ambiguous dates) is built and has been exercised against
  real uploads, including the "couldn't read this clearly" confirmation path.
- **Event-source adapters** are wired through a shared runner with dedupe, contact
  scrubbing, and publish gates — the SOP events API is the first adapter; the scraping
  skill documents how to add club-site and campus-calendar sources.

**Deliberately out of scope for this build**, tracked as roadmap in
[`central-hub-for-clubs.md`](./central-hub-for-clubs.md): accounts/auth, push
notifications, Instagram/Discord scraping, multi-campus support, and a review dashboard
for posters a club drops directly into Dropbox with no one at the keyboard to confirm an
ambiguous date (the in-app upload flow has one; the unattended watcher path doesn't).

## Team

[Angela Koo](https://github.com/angbengi) · [Avi Patel](https://github.com/avi1277) · [Harini Dave](https://github.com/hdave07) · [James Romasco](https://github.com/Pufferfish-Kirby)
