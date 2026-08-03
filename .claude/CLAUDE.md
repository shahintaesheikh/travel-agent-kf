# AGENTS.md

Repo-root guidance for coding agents. Read this before touching anything.

**Companion docs:** `tasks/prd-travel-agent.md` (what to build) · `tasks/dev-plan-travel-agent.md` (lane assignments, sequencing) · `travel-agent-architecture.mermaid`

---

## 0. Escalation — read first

**You are scoped to the code implementation of your assigned lane. Everything else is a conversation with Shahin.**

### Never do without asking

- **Create a git worktree.** Worktree layout is owner-managed. If your work seems to need a new one, stop and say so.
- **Create or delete a branch.** Work on the branch you were given.
- **Merge, rebase onto, or push to `main`.**
- **Write an Alembic revision** unless your lane is explicitly designated the migration owner. Open an issue describing the column you need.
- **Modify the LangGraph state schema** after it's frozen (end of lane S2). Live checkpoints break, and veto windows keep them alive up to 12 hours.
- **Add a dependency** to `pyproject.toml`. Batch the request; the integrator applies it.
- **Run providers in live mode.** Every live call costs money. Replay is the default; recording requires approval.
- **Change a frozen type** in `app/models/`. Other lanes import these.
- **Edit files outside your lane's Owns list.** Ever.
- **Redefine scope.** If a requirement seems wrong, say so instead of reinterpreting it.

### Always surface, don't decide

- A PRD requirement that appears incorrect, ambiguous, or unbuildable
- A provider whose real behavior contradicts `travel-providers`
- A design tradeoff with more than one defensible answer
- Anything touching money, approval flow, or the handoff path
- Cost implications you notice

**Default posture:** when unsure whether something is in scope, it isn't. Ask.

---

## 1. What this is

A private trip-planning app for exactly two users. A LangGraph agent researches flights, lodging, restaurants and activities, assembles a day-structured itinerary, and holds it for human approval. Approved bookable items produce a link landing on a **partner's** payment page.

**The app never collects payment.** It is never merchant of record. This is not a nice-to-have; it's the constraint the architecture is shaped around.

---

## 2. Frozen types

Defined in `app/models/`. Import them. Never redeclare a local equivalent — two lanes inventing two flight models is the single most expensive merge failure available.

```python
class BookingRequest(BaseModel):
    url: str
    post_data: dict | None = None       # when present, MUST be POSTed unchanged
    vendor: str

class NormalizedFlight(BaseModel):
    carrier: str
    flight_numbers: list[str]
    depart: datetime
    arrive: datetime
    stops: int
    duration_minutes: int
    price_usd: Decimal
    selected_flights_json: dict          # NOT booking_token — see §6
    booking_request: BookingRequest | None
    observed_at: datetime

class NormalizedLodging(BaseModel):
    property_id: str
    name: str
    rating: float | None
    price_per_night_usd: Decimal
    booking_request: BookingRequest
    cancellation_terms: str | None
    observed_at: datetime

class NormalizedPlace(BaseModel):
    google_place_id: str        # Places API (New) `id` — NOT `name`, see below
    name: str                   # from `displayName`
    address: str
    lat: float
    lon: float
    rating: float | None
    price_level: int | None
    phone: str | None           # from `nationalPhoneNumber`
    maps_url: str
    # no booking_request — reference tier, see §5

class NormalizedActivity(BaseModel):
    external_id: str
    name: str
    rating: float | None
    kind: Literal["attraction", "experience", "event", "poi"]
    link: str
    price_basis: Literal["uncounted"] = "uncounted"

class PriceStatus(StrEnum):
    available          = "available"            # one booking, one link
    split_options_only = "split_options_only"   # exists and is priced, needs two bookings
    unavailable        = "unavailable"          # gone; price_usd is None

class Priced(BaseModel):                        # frozen
    ref: str
    status: PriceStatus
    price_usd: Decimal | None = None            # None when unavailable — never invented
    observed_at: datetime                       # UTC-aware
    booking_request: BookingRequest | None = None
    unavailable_reason: str | None = None
```

`PriceStatus` is an enum rather than two booleans on purpose. `available` and
`split_options_only` both mean the itinerary exists and has a price, but only the
first can become a handoff — and `if priced.still_available: hand_off()` reads as
correct while being wrong. Three cases force the reader to handle the third.

`Priced.booking_request` carries a currently-valid way to charge the selection when
the provider returned one. A flight re-price POSTs `selected_flights_json` and gets
fresh `post_data` back in the same response; without this field the adapter would
fetch a valid payload, discard it, and the handoff would POST the copy captured at
`item_pending` — up to a 12-hour veto window earlier. It is None for Airbnb (dated
GET link) and Google Hotels (no per-vendor booking URL); callers fall back to the
stored `booking_request`.

### Datetimes: provider-local naive, system UTC-aware

**Rule, not observation. Follow it in every adapter.**

- **Provider-local times stay naive** — `depart`, `arrive`, and any other wall-clock
  time a provider reports. SerpApi returns local airport time with no offset
  (`"2027-03-10 01:50"`).
- **System timestamps are UTC-aware** — `observed_at`, `handed_off_at`, `resolved_at`,
  anything the app itself stamps. Use `datetime.now(UTC)`.

The reason: the source carries no offset, and a fabricated one is worse than a naive
datetime — it looks authoritative and is wrong, and 01:50 at DXB is not a moment in
time until you know which airport. Naive is honest about what the provider said.

The hazard is mixing them. Comparing a naive datetime to an aware one raises
`TypeError` at runtime, in whichever lane touches both first. Never compare or
subtract across the two classes; localize explicitly at the boundary if you need to.

### Adapter contract

```python
class ProviderAdapter(Protocol):
    async def search(self, q: SearchQuery) -> list[Normalized]: ...
    async def resolve(self, ref: str) -> BookingRequest: ...
    async def reprice(self, ref: str) -> Priced: ...
```

Every provider implements this. Adapters own normalization, caching, and quota. Nothing above the adapter layer sees a raw provider response.

---

## 3. Cache and quota

```python
async def get_or_fetch(key: str, tier: TTLTier, fetch: Callable[[], Awaitable[T]]) -> T
```

**`tier` is required and positional.** Not optional, not defaulted. A call without one must fail at type-check.

The reason: a fare silently landing in the 30-day tier looks fresh and is fiction. Misclassification is invisible at runtime, so it has to be impossible at authoring time.

| Tier | TTL |
|---|---|
| `FLIGHT_SEARCH` | 15 min |
| `LODGING_SEARCH` | 30 min |
| `ACTIVITY_SEARCH` | 6h |
| `PLACES_CONTENT` | 30 days — **contractual ceiling, not a tuning knob** |
| `GEO_STABLE` | indefinite (`place_id`, coordinates, geocodes) |

Booking options are **never** cached.

Quota is enforced in the adapter, never in agent instructions — the agent cannot be trusted to self-limit. Ceilings: 6 provider calls per turn, 40 per trip-hour. Exceeding returns `QuotaExceeded` **as a value**; it never raises.

---

## 4. Tools versus nodes

**A tool is something the agent chooses. A node is something the system runs.** Conflating them is how the agent gains authority it shouldn't have.

```
app/agent/tools/    one file per tool, auto-discovered at import
app/agent/nodes/    wired into the graph by the trunk lane
```

| Tools (7) — agent chooses | Nodes (5) — system runs |
|---|---|
| `search_flights` | `load_traits` — every turn |
| `search_lodging` | `extract_memories` — post-turn |
| `search_restaurants` | `resolve_booking_options` — only at `item_pending` |
| `search_activities` | `reprice` — before every handoff |
| `read_saved_items` | `build_handoff` |
| `recall_context` | |
| `propose_itinerary` | |

**`reprice` and `resolve_booking_options` must never be exposed as tools.** An agent that *can* skip the unconditional re-price eventually will, and re-price is what stands between a stale number and a real charge.

One file per tool exists so tool work parallelizes across worktrees. Don't consolidate them.

---

## 5. Layering

```
api → agent → travel / memory / approvals / trips → shared
```

Nothing in `travel/` imports `agent/`. Check your imports before opening a PR.

### Item tiers

**Actionable** — flights and lodging. Real transaction. Gets approval, veto window, unconditional re-price, handoff.

**Reference** — restaurants, attractions, experiences, events, POIs. No transaction. Name, rating, phone, link. **No `booking_request` field.** Restaurants are reference because there's no availability source; experiences because Tripadvisor returns no price.

---

## 6. Anti-patterns, with reasons

The reasons matter more than the rules — they generalize to cases not listed here.

**Don't embed structured data.** Only `saved_items.vibe_text` is vectorized. "Saved places in Lisbon under $30" is a `WHERE`, and a `WHERE` is exact. Embedding a structured field destroys the precision that made the column worth having.

**Don't return raw payloads to the model.** Ten flight offers of provider JSON degrade reasoning three turns later. Normalized top-N to the agent; raw to Postgres.

**Don't persist `booking_token`.** It's opaque and it expires. Persist `selected_flights_json` — flight numbers and dates — which is durable and replayable at re-price time.

**Don't render a `booking_request` as a hyperlink when `post_data` is present.** It must be POSTed unchanged, server-side. The handoff endpoint asserts on this and refuses.

**Don't cache Places content past 30 days.** Contractual ceiling.

**Don't use `MemorySaver`.** Veto windows resume checkpoints hours later; in-memory state doesn't survive a restart.

**Don't let the agent decide when to stop searching.** Fan-out limits live in the adapter.

**Don't fabricate prices.** Experiences have no price and are excluded from the budget. An invented estimate is worse than an honest gap.

**Don't save an item with null geometry.** Locality-only items geocode to a centroid and radius; without geometry they never return from spatial queries and rot in the backlog.

**Don't read Places API `name` as a title.** In Places API (New), `name` holds the resource path `places/PLACE_ID` and `displayName` holds the human-readable name — the reverse of the legacy API most examples describe. Store `id` as `google_place_id`. Field masks are also mandatory; omitting one errors rather than defaulting. See the `travel-providers` skill.

---

## 7. Coordination

**Migrations.** Only the trunk lane and the designated migration owner write Alembic revisions. Others open an issue. Multiple heads cost more to untangle than the wait costs.

**State schema.** Trunk-owned, frozen after S2. Additions require a sequential lane and break in-flight checkpoints.

**Cassettes.** `fixtures/cassettes/{provider}/{args_hash}.json`, namespaced per provider. `PROVIDER_MODE=replay` everywhere by default. Record only your own lane's provider, only with approval.

**Owns / Never touches.** Every lane brief lists both. Treat them as hard boundaries.

**Merge cadence.** Merge to `main` after every lane, not at wave boundaries — but see §0: you don't merge, you request it.

---

## 8. Testing scope

Tripwires, not a suite. Roughly a dozen tests total. **Tests ship in the lane that writes the code**, never a later cleanup pass.

**Money path**
- `reprice` is called before every handoff — assert on the state machine, not the happy path
- Handoff refuses to render a GET link when `post_data` is present
- Fare moved beyond threshold → item becomes `stale`, no handoff
- Editing a handed-off item leaves committed spend unchanged
- Budget breach blocks approval but does **not** filter search results

**Adapter normalizers** (against cassettes — free and deterministic)
- Each adapter maps a recorded response to typed output with no field loss
- Partial Tripadvisor responses (`locations` instead of `places`) parse without crashing
- `QuotaExceeded` returns as a value, never raises
- Cache adapter rejects a call with no TTL tier

**Don't write:** agent behavior tests, UI tests, ranking-quality tests. Non-deterministic, low value at two users.

---

## 9. Glossary

| Term | Meaning |
|---|---|
| **Tool** | Function the agent chooses to call |
| **Node** | Function the system runs at a fixed point; the agent cannot trigger or skip it |
| **Actionable item** | Itinerary entry with a real transaction (flights, lodging) |
| **Reference item** | Itinerary entry with no transaction (restaurants, attractions, events) |
| **Handoff** | Sending the user to a partner's payment page for one specific selection |
| **Deep-link precision** | A link landing on the payment step for one option, not a search page |
| **Trait** | Durable preference. Never expires; superseded only |
| **Intent** | Temporary preference. 90-day TTL |
| **Backlog** | Saved items not attached to any trip |
| **Resolution** | `exact_venue` \| `operator_unknown` \| `locality_only` |
| **Price basis** | `actual` \| `price_level_estimate` \| `uncounted` |
| **Lane** | One worktree, one branch, one agent, one feature |

---

## 10. Commands

**`uv` is required.** Dev dependencies live in `[dependency-groups]` (PEP 735), which
`uv sync` installs via `[tool.uv] default-groups`. `pip install -e ".[dev]"` does *not*
work — there is no `dev` extra — and `pip --group` needs pip ≥ 25.1. Install uv first:

```bash
brew install uv                  # or: curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
uv sync                          # install (creates .venv)
docker compose up -d             # local postgres + redis
uv run alembic upgrade head      # migrate
uv run python scripts/seed.py    # ~20 saved_items for agent development
uv run pytest                    # tripwires
uv run ruff check . && uv run ruff format .    # lint
PROVIDER_MODE=replay uv run uvicorn app.main:app --reload
```

`PROVIDER_MODE` defaults to `replay`. Setting `live` or `record` requires approval — see §0.

### Environment variables

`.env` at the repo root; `.env.example` lists the full set. Required in every
environment:

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | Postgres. `postgres://` is normalized to `postgresql://` | local docker compose |
| `REDIS_URL` | Cache + quota + arq. **Must be set on both the web service and the worker** — an unset worker silently gets its own empty cache and no quota ceiling | `redis://localhost:6379/0` |
| `PROVIDER_MODE` | `replay` \| `record` \| `live` | `replay` |
| `SESSION_SECRET` | Signed session cookies | dev placeholder |
| `INGEST_BEARER_TOKEN` | `POST /ingest` auth, rotatable | dev placeholder |
| `SERPAPI_API_KEY` | Flights, hotels, Tripadvisor, events | unset |
| `OMKAR_API_KEY` | Airbnb (`API-Key` header, exact casing) | unset |
| `GOOGLE_PLACES_API_KEY` | Places API (New) | unset |
| `ANTHROPIC_API_KEY` | Agent model | unset |
| `OPENAI_API_KEY` | Embeddings only | unset |

Provider keys are optional — `replay` needs none. They are `SecretStr`, so they never
render in a repr, traceback or log line; read with `.get_secret_value()` at the call
site. Unknown variables are ignored rather than rejected, because a deployment
environment always carries variables this app doesn't read.
