# Starter issues

Paste each one into GitHub → Issues → New issue → "Agent task". Do #1 first: CI is red
until it's fixed, and every later PR needs green CI to be trustworthy.

---

## 1. Pin backend dependencies and fix naive datetimes  `agent-ready`

**Goal:** Backend tests pass on a fresh install, and a redeploy can't silently pull
in breaking library versions.

**Done when**
- [ ] `requirements.txt` pins exact versions of every package (`==`)
- [ ] `datetime.utcnow()` is replaced with timezone-aware UTC everywhere in `backend/app`
- [ ] All 15 tests pass on the latest SQLModel, not only on an old pin
- [ ] CI is green

**Out of scope:** changing what's stored in `campus_compass.db`.

**Notes:** 7 of 15 tests fail on SQLModel 0.0.46+ ("Datetime values must have
timezone information"); they pass on 0.0.44. Existing DB rows were written as naive
UTC. Explain in the PR how old rows and new rows compare, and whether that matters.

---

## 2. Similar clubs on the club detail endpoint  `agent-ready`

**Goal:** `GET /api/clubs/{id}` returns up to 5 genuinely similar clubs instead of
`similar: []`.

**Done when**
- [ ] Uses the existing Chroma club embeddings (no new API calls per request)
- [ ] Excludes the club itself; returns id, name, and a short summary for each
- [ ] Unit test with a stubbed vector store
- [ ] Frontend club panel shows them (small list, links open that club)

**Out of scope:** re-indexing, changing the embedding model.

---

## 3. Split the frontend bundle  `agent-ready`

**Goal:** first load ships less JavaScript. The main chunk is ~609 kB today.

**Done when**
- [ ] Heavy screens (Directory, Constellation) load with `React.lazy` / dynamic import
- [ ] `npm run build` has no "chunk larger than 500 kB" warning
- [ ] PR lists before/after chunk sizes

**Out of scope:** removing dependencies or redesigning screens.

---

## 4. Fix the 3 lint warnings  `agent-ready`

**Goal:** `npm run lint` prints nothing.

**Notes:** `ClubPanel.jsx:186` sets state inside an effect; `button.jsx` and
`badge.jsx` export non-components (breaks fast refresh). Explain the
setState-in-effect fix in "Worth understanding".

---

## 5. Decide: hosting and a persistent database for the fork  `needs-decision`

**Goal:** a written comparison so James can choose, not code.

**Done when**
- [ ] `docs/hosting-options.md` compares 2-3 setups that are free or close to it
      (e.g. Render + a hosted Postgres; keep SQLite on a paid persistent disk; others),
      for: monthly cost, effort to migrate, what happens to uploaded posters on
      redeploy, and fit for a school pilot
- [ ] A recommendation, with the main reason against it

**Out of scope:** migrating anything. Today the deploy belongs to a teammate, and
Render's free tier loses everything written at runtime.

---

## 6. Full St. George catalog  `needs-james`

**Goal:** all 897 St. George clubs synced and enriched, not 250.

**Notes:** needs real API keys and costs money, so James runs it locally:
`python -m app.services.sop_sync --all` (~40-60 min, resumable), then
`python -m app.services.enrichment_batch --limit 1000`. An agent can prepare it
first: estimate the Haiku + Voyage cost from the code and add a `--dry-run` that
prints how many clubs would be enriched.
