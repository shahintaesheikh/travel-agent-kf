# Lane S1.1 — Trunk repair · SEQUENTIAL · BLOCKING

**Branch:** `lane/s1.1-trunk-repair` (already created — do not create branches or worktrees)
**Depends on:** S1 (merged)
**Read first:** `AGENTS.md`, `tasks/lane-s1-trunk.md`, `tasks/prd-travel-agent.md`

Three defects in the S1 trunk, found by lane A1. All three are in files no adapter lane may
touch, so every lane is working around them independently right now — which is how you get
four different compensations and a merge that reconciles none of them.

**One worktree, one branch, all three fixes. Merge before any Wave A lane merges.**

While this is open: Wave A lanes keep building, but nothing merges to `main`. After merge,
all four rebase.

---

## Owns

```
app/shared/cache.py
app/shared/quota.py
app/shared/config.py     (only to add REDIS_URL — see F1)
app/shared/cassettes.py
app/travel/base.py
.gitignore
pyproject.toml        (only the uv/pip discrepancy in §10 docs — see F4)
AGENTS.md             (only §2, adding the datetime convention — see F5)
tests/
```

## Never touches

`app/travel/adapters/*` · `app/models/*` · `app/agent/*` · `app/api/*` · `scripts/*` · anything else

---

## F1 · Cache and quota are in-process dicts — **worst of the three, and larger than first reported**

**Confirmed:** both `cache.py` and `quota.py` are module-level dicts, not Redis. Two
defects, one root cause.

**F1a — nothing expires.** `get_or_fetch` reads `_TTL_SECONDS` but stores no timestamp.
Entries live for the process lifetime. Every fare is cached forever — the exact failure the
required-positional TTL tier was designed to prevent. The ceremony shipped; the mechanism
didn't. US-012 asserts a 15-minute flight cache, so this is a live requirement violation.

**F1b — the dict is per-process.** The web service and the arq worker are separate
processes. A dict lives in one heap; the other cannot see it. So the cache is duplicated and
cold in the worker, and — worse — **`quota.py` has the same defect, confirmed**, which means
the 6-per-turn and 40-per-trip-hour ceilings have never actually been enforced. Each process
counts to 6 independently, and Render can run more than one web instance, so the real ceiling
is 6 × however many processes exist. Fan-out control is the reason quota sits at the adapter boundary at all;
right now it doesn't hold.

**Fix: move both to the Render Key Value instance.** It's already provisioned. Connection
string will be supplied in this lane's environment — test against the real instance, not a
fake.

- [ ] `cache.py` backed by Redis. Expiry via `SET ... EX` / `SETEX`, never a bare `SET` —
      the TTL is the storage call, not separate bookkeeping
- [ ] **Audit `quota.py` and fix it the same way.** `INCR` + `EXPIRE` on
      `quota:{provider}:{hour}`. **Set `EXPIRE` only when `INCR` returns 1** — calling it on
      every increment resets the clock, so a steadily-used counter never expires and the
      hourly window becomes permanent
- [ ] Key prefixes in one instance, no need for three: `cache:{provider}:{tool}:{hash}`,
      `quota:{provider}:{hour}`, arq owns its own
- [ ] **A cache hit must not increment quota.** Count only when the request actually leaves
      the process, or the ceiling limits your own reads rather than outbound spend
- [ ] Connection failure must **fail open, not crash** — but asymmetrically. A dead cache is
      a miss and can log at debug. A dead quota means **no ceiling at all**, which is exactly
      when unmetered spending goes unnoticed — log at warning or error, every occurrence
- [ ] Render's free Key Value is **in-memory, wiped on restart.** That's acceptable and
      already accounted for: a lost cache is a miss, lost quota counters fail open for an
      hour, and veto timers survive because they're rows in `approvals`, not Redis entries.
      Do not add persistence work
- [ ] Test: entry present before TTL, absent after (fake clock or a 1-second tier — do not
      `sleep(900)`)
- [ ] Test: each tier maps to the TTL in AGENTS.md §3
- [ ] Test: quota counter increments across two separate client connections, proving it is
      shared rather than per-process
- [ ] Test: Redis unreachable → `get_or_fetch` still returns a fetched value

## F2 · `api_key` poisons the cassette hash

`cassettes.py` hashes `str(request.url)` and the params dict, both carrying `api_key`. A
cassette recorded with a real key can never be matched on replay. Recording burns approved
live calls and produces unusable fixtures.

The S1 brief specified this and it didn't land. Two separate strips, two separate reasons:

- [ ] **Strip before hashing** — so cassettes survive a key rotation. Rotate the key today
      and every hash changes for requests that didn't
- [ ] **Strip before writing** — cassettes are committed. Unredacted means your SerpApi key
      lands in git history
- [ ] Covers both carriers: `api_key` query param (SerpApi) and `API-Key` header (omkar).
      Redact by key name, not by value matching
- [ ] Normalize before hashing — sort keys, canonicalize — so `{"a":1,"b":2}` and
      `{"b":2,"a":1}` produce one cassette, not two
- [ ] Test: same request with two different keys resolves to the same cassette
- [ ] Test: no written cassette contains a credential

## F3 · `Priced` is undefined

`base.py` declares `reprice(ref) -> ...`. A1 returns a dict, matching the toy adapter.

S3 consumes `reprice` on the money path and F1's budget math depends on the shape. Freeze it
now, while one adapter exists, not after four do.

- [ ] Define `Priced` in `app/models/` as a frozen Pydantic type, exported like the others
- [ ] Type `ProviderAdapter.reprice` against it
- [ ] Minimum: current price USD, `observed_at`, whether the itinerary still exists
- [ ] **Include A1's `split_options_only`.** SerpApi can return an itinerary sellable only as
      separate departing/returning options — no single link can charge it, so it cannot be an
      actionable handoff. Distinguishing "sold out" from "two bookings required" is D0 applied
      to a case the PRD never anticipated. It belongs in the type, not in one adapter
- [ ] Report the type before implementing consumers

## F4 · `uv` isn't installed; docs say `uv sync`

AGENTS.md §10 documents `uv sync`. A1 couldn't run it and used a scratch venv outside the
repo. Three more agents will each invent their own workaround.

- [ ] Pick one: install `uv`, or change §10 to `pip install -e ".[dev]"`
- [ ] Whichever wins, `pyproject.toml` and AGENTS.md must agree
- [ ] Verify the documented command works from a clean checkout

## F5 · Datetime convention undeclared

SerpApi returns local airport time with no offset. A1 kept `depart`/`arrive` naive and
`observed_at` UTC-aware — reasonable, since inventing an offset would be fabrication.

The hazard is mixing them. Comparing a naive datetime to an aware one raises `TypeError` at
runtime, in whichever lane touches both first.

- [ ] Document in AGENTS.md §2: provider-local times naive, system timestamps UTC-aware
- [ ] State it as a rule, not an observation, so A2 and A3 follow it
- [ ] One sentence on why: no offset in the source, and a fabricated one is worse than a
      naive one

## F6 · Repo hygiene

- [ ] `__pycache__` removed from tracking and added to `.gitignore`
- [ ] `.gitignore` covers `__pycache__/`, `*.pyc`, `.venv/`, `.env`, `.ruff_cache/`,
      `.pytest_cache/`
- [ ] **`fixtures/cassettes/` stays tracked** — shared fixtures are why a fresh worktree can
      run tests immediately. Don't ignore them

## F7 · Deploy config

S1 deliverable 11 was deploy config, and no `render.yaml` or Procfile exists. The web service
is being created now and needs something to start.

- [ ] `render.yaml` or Procfile: web start command, pre-deploy `alembic upgrade head`
- [ ] `/health` returns 200 and checks Postgres and Redis reachability, not a bare literal
- [ ] Document required env vars: `DATABASE_URL`, `REDIS_URL`, provider keys, `PROVIDER_MODE`
- [ ] **Do not deploy.** Config only — deploying is Shahin's

---

## Done when — you can verify

- [ ] Cache entries expire at their tier TTL; test proves before/after
- [ ] Cassette hash is key-independent; test proves two keys, one cassette
- [ ] No credential appears in any written cassette
- [ ] `Priced` is a frozen type and `reprice` is typed against it
- [ ] The documented install command works from a clean checkout
- [ ] Datetime rule is in AGENTS.md §2
- [ ] Deploy config exists; `/health` checks Postgres and Redis rather than returning a literal
- [ ] `git status` clean on a fresh checkout; no `__pycache__` tracked
- [ ] `ruff check .` clean

## Done when — Shahin verifies

- [ ] All four Wave A lanes rebase onto this and still pass
- [ ] `REDIS_URL` set in Render for both the web service and the worker
- [ ] Cache and quota confirmed shared across processes, not per-process

---

## Report before implementing

**F3** — the `Priced` shape, before anything consumes it. Six lanes will import it and per §0
cannot change it afterward.

---

## Escalate, don't decide

Anything in AGENTS.md §0. No branches, no worktrees, no merges, no live provider calls, no
new dependencies, no edits outside Owns.

Do not "improve" adapters while you're in here. A1's workarounds stay until it rebases.

---

## Not in this lane

**A1's authored cassettes** (its item 3) are fixtures, not trunk defects — they belong to A1
and get re-verified at the first record session. Fixing F2 is what makes that session
worthwhile.

**A1's round-trip call count** (its item 4) contradicts PRD Appendix D, which records booking
options retrieved from an outbound-only `selected_flights_json`. That's a spike-verification
question for Shahin, not a trunk fix. The quota concern is smaller than reported anyway:
`resolve_booking_options` fires at `item_pending` and `reprice` at handoff — separate turns
from the search, so the 6-per-turn ceiling isn't absorbing all four at once.
