# CLAUDE.md

Read this first, every session. It tells you what ClubHub is, how James wants you to
work, and the rules that come from real incidents. `AGENTS.md` points here, so this is
the only copy. Keep it that way.

## What this is

**ClubHub** helps a student find the campus clubs that fit them. They type what they
want out of university in plain English and get 5-8 ranked clubs, each with a reason,
drawn as a small personal star graph (you → outcomes → clubs → next events). Clubs can
also drop a poster into Dropbox (or upload it in the app), and it turns into a live
event.

It started as a 24h HackMIT build (2nd place, Dropbox challenge). It is now a
**long-term project maintained by James** (GitHub: `Pufferfish-Kirby`), forked from the
team repo `hdave07/ClubHub`. The pilot is the University of Toronto, St. George campus.
The goal is for UofT, and later other schools, to actually use it. That makes
**reliability and data quality matter more than feature count**. A club that shows the
wrong date, or a page that's down, costs more trust than a missing feature earns.

Background docs: `README.md` (product + quickstart), `DECISIONS.md` (why things are the
way they are, and who decided), `central-hub-for-clubs.md` (original brainstorm).

## How James works with you

James mostly works from his phone, between lectures. You run as a cloud session and
he checks in when he can. Optimize for that.

1. **One issue → one branch → one PR.** Branch from `main` as `claude/<short-topic>`.
   Never push to `main`. Never merge your own PR. James reviews and merges every change.
2. **Keep PRs small enough to review on a phone**: aim under ~300 changed lines,
   excluding lockfiles. If the task is bigger, ship the first slice and list the rest
   under "Follow-ups" in the PR description.
3. **When a real choice comes up, stop and ask.** "Real choice" means: product
   behavior a student would notice, anything touching data that's already in the DB,
   adding a dependency or service, anything that costs money, or anything on the
   "ask first" list below. Ask in the session as one question with 2-3 labelled options
   and your recommendation first. Don't stop for things a sensible default covers;
   pick the default and say so in the PR.
4. **Every PR description uses this template:**

   ```markdown
   ## What changed
   2-4 bullets, plain language.

   ## Why
   The issue (link it: Closes #N) and the root cause, if this is a fix.

   ## How I verified it
   Exact commands run and their result. Say plainly what you could NOT verify
   (e.g. "didn't hit the real Voyage API").

   ## Decisions I made
   Any default you picked without asking, so James can overrule it.

   ## Worth understanding
   The one concept from this change that matters most (a library behavior, a design
   pattern, why the bug happened), in 3-5 plain sentences, plus the file and line
   where it lives.
   ```

   "Worth understanding" is not optional. James reviews PRs partly to learn the
   codebase, and he has to be able to explain every merged change himself.
5. **Before opening the PR, run the checks** (Commands, below) and make sure they pass.
   If a check was already failing on `main` before your change, say so instead of
   hiding it, and don't widen the PR to fix it unless the issue asks.
6. **Keep this file true.** If your change makes something here wrong (a command, a
   "Known state" line, a constraint), update it in the same PR. Record a new product
   or architecture decision in `DECISIONS.md` (owner `James` if he made the call).

### Ask first, always

Don't start any of these without James saying yes in the session or in the issue:

- Accounts / auth, notifications, or anything storing student personal data
- Scraping Instagram, Discord or LinkedIn (their terms forbid it and accounts get
  blocked; the poster-drop flow exists to get that info with consent instead)
- Adding a new school or campus
- Changing the database engine, hosting, or deploy config (`vercel.json`, Render)
- Editing `FIXED_TAGS` or `FIXED_OUTCOMES` (forces a re-enrichment run that costs money)
- Changing `backend/data/campus_compass.db` or `backend/data/chroma/` contents
- Deleting files or features

## Safety rules for cloud sessions

Cloud sessions have **no real API keys** on purpose. Work that needs Anthropic, Voyage
or Dropbox credentials is James's job on his own machine.

- **Never run these in a cloud session.** They hit real APIs, cost money, and write to
  the checked-in database:
  `app.services.sop_sync`, `app.services.enrichment_batch`, and every service's
  `python -m app.services.<name>` smoke test (`dropbox_store`, `extraction`, `matcher`,
  `ingest`, `dropbox_watcher`). These are **not** unit tests. If a task needs one, write
  the code, then put the exact command for James in the PR under "How I verified it".
- Unit tests must not touch the network. Stub external calls the way
  `tests/test_ingest_dates.py` does.
- Never commit a real `.env`. The pre-commit hook in `.githooks/` blocks it
  (`git config core.hooksPath .githooks` once per clone). Keys have leaked from this
  repo before.
- `DROPBOX_WATCHER_ENABLED=0` is set for you in `.claude/settings.json`. Leave it off;
  otherwise every reload re-scans the inbox and spends Claude calls.

## Commands

Cloud sessions are Linux. The SessionStart hook (`scripts/cloud_setup.sh`) creates
`backend/.venv` and installs frontend packages before you start.

```bash
# backend: run from backend/
.venv/bin/python -m pytest -q                       # unit tests, no network
.venv/bin/uvicorn app.main:app --port 8000           # API (only if a task needs it)

# frontend: run from frontend/
npm run lint                                         # oxlint
npm run build                                        # must pass before a PR
npm run dev                                          # proxies /api -> localhost:8000
```

CI (`.github/workflows/ci.yml`) runs the same tests, lint and build on every PR.

James develops locally on **Windows / PowerShell 5.1**: no `&&` (use `;`), activate the
venv with `.venv\Scripts\Activate.ps1`. When a PR includes commands for him to run,
write them in PowerShell form.

Real-API scripts, for James to run locally (venv active, `.env` filled in):

```powershell
python -m app.services.sop_sync --discover-only     # dry run
python -m app.services.sop_sync --limit 250          # sync clubs (resumable)
python -m app.services.enrichment_batch --limit 25   # enrich (Haiku) + embed (Voyage)
python -m app.services.enrichment_batch --index-only # re-embed only, no Claude calls
python -m app.services.enrichment_batch --stale-tags # after a FIXED_TAGS change
```

## Repo layout

```
frontend/   React 19 + Vite, plain JavaScript/JSX (no TypeScript), Tailwind v4 via
            @tailwindcss/vite, @xyflow/react for the graph.
            src/screens = pages, src/components = graph/cards/panels,
            src/lib = API client (api.js), filtering, physics, layout math.
backend/    FastAPI + SQLModel (SQLite) + ChromaDB + Anthropic SDK + Voyage + Dropbox SDK.
            app/routers = /api/recommend, /api/clubs, /api/events, /api/upload
            app/services = sync, enrichment, embeddings, Dropbox, extraction, matching,
            event_sources/ (pluggable event adapters, see REGISTRY.md)
.agents/skills/scraping-club-events/  skill for adding event-source adapters
            (.cursor/skills/ holds an identical copy for Cursor users)
```

Extend `frontend/src/lib/api.js` instead of writing fetch calls elsewhere.

## How it works

Two ingestion paths write to the same tables, tagged by `source` (`sop` | `dropbox`):

1. **Portal sync**: `sop_sync` pulls clubs from UofT's SOP WordPress API → dedupe →
   SQLite. `enrichment` (Haiku) adds summary, outcomes, tags, commitment.
   `embeddings` (Voyage `voyage-4-lite`, 1024 dims) → ChromaDB.
2. **Posters**: `dropbox_watcher` / `POST /upload` → `extraction` (Sonnet, vision/PDF)
   → `matcher` (rapidfuzz club-name match) → `Event` rows with a provenance link.

`POST /recommend`: Voyage query embedding → ~20 nearest clubs from Chroma → Sonnet
reranker (`services/reranker.py`, forced tool use) keeps 5-8 and writes a "why it fits"
line each, mapped to `FIXED_OUTCOMES`.

Voyage has separate *document* and *query* embeddings: index with `embed_documents()`,
search with `embed_query()`. Changing model or dimensions means re-indexing everything.

## Hard constraints (each one came from a real incident, not style)

- **Never expose event-organizer emails** in API responses or the UI. SOP event data
  includes students' personal emails. **Club** contact emails (`Club.links.email`) ARE
  shown on purpose: SOP publishes them as the club's official contact. Different
  data paths, different rules.
- **A wrong date is worse than no date.** Auto-publish an extracted event only with
  title + resolved start + time of day + high confidence (`services/extraction.py`).
  Everything else → `pending_review`. Always pass today's date and `America/Toronto`
  into the extraction prompt; never let the model guess an ambiguous year.
- **A poster never moves a live event.** A changed start, end or location on a
  `published` event is held as an `IngestLog` `held_change` row, not applied (D15).
- **Never invent club facts.** Thin descriptions get "limited info" summaries.
  `commitment` is `unknown` for ~40% of clubs because the prompt forbids guessing it.
  Don't loosen that; the reranker judges intensity at query time.
- **Tags come only from `FIXED_TAGS` (51 terms); outcomes only from `FIXED_OUTCOMES`
  (8).** Free-form tags produced near-duplicate junk in the pilot. Every club needs at
  least one outcome or it can't appear in the graph; `derive_outcomes_from_tags()` is
  the fallback.
- **`club_to_document()` embeds the summary AND the raw description.** Club-specific
  words ("k-pop", "poker") only reach the vector through the description.
- **The personal graph stays small** (~15-25 nodes). Never render the full catalog
  as one graph.
- **Dedupe before enrichment.** `copy-of-*` slugs are yearly re-listings; the dedupe
  key is `sop_original_id` (SOP's `_oasis_original`).
- **Times are stored as timezone-aware UTC; the school's zone is only for reading and
  showing them.** Use `app/timeutil.py` (`utc_now`, `as_utc`, `local_to_utc`,
  `to_campus`); never `datetime.utcnow()` or `.replace(tzinfo=None)`. SQLModel 0.0.46+
  rejects naive datetimes on write. The zone is the `SCHOOL_TIMEZONE` setting, an IANA
  name (`America/Toronto`), never a fixed offset like EST: Toronto switches to daylight
  time and an offset would put events from March to November an hour off.
- **Filter campus by taxonomy slug** (`campus-st-george`), not `settings.sop_campus`.
  The title-cased label has no period and silently matches zero rows.
- **Sample across interest areas** (`select_diverse()`), never "first N".
- **SOP etiquette:** call SOP only from one-off scripts with a real User-Agent and
  throttling, never from a request handler.
- **Adding a model column? Add it to `_COLUMN_MIGRATIONS` in `database.py` too.**
  `create_all()` never alters existing tables, and the DB is checked into git.
- **Dropbox uses the refresh-token flow** (`DROPBOX_REFRESH_TOKEN`), not a short-lived
  access token.

## Known state (update this when it changes)

As of 2026-10-11:

- **Backend dependencies are pinned** in `requirements.txt` (SQLModel 0.0.48). Bump them
  on purpose and run the tests; don't loosen the pins to ranges.
- **Deployment belongs to a teammate** (Vercel frontend; `vercel.json` rewrites `/api`
  to a Render backend). The fork has no deployment of its own yet.
- **Render's free tier has no persistent disk**, so SQLite and Chroma ship inside git
  and anything written at runtime (uploaded posters, confirmed events) is lost on
  restart or redeploy.
- **Catalog covers 250 of 897** St. George clubs (sampled, 100% enriched).
- `GET /api/clubs/{id}` returns `similar: []`; it's a TODO in `routers/clubs.py`.
- Frontend main bundle is ~609 kB (Vite warns above 500 kB). Lint is clean (0 warnings).
- No review screen for posters dropped straight into Dropbox with nobody there to
  confirm an ambiguous date (the in-app upload has one).
