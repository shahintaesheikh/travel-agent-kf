# Development Plan: Travel Agent (agent-executed)

**Owner:** Shahin
**Execution:** Agentic coding, git worktrees, multiple concurrent agents
**Deploy:** Railway/Render
**Testing:** Money path + adapter normalizers
**Companion:** `tasks/prd-travel-agent.md` — agents have access; this plan references FR/US numbers rather than restating requirements

---

## How to read this

Work is organized as **lanes**. One lane = one worktree = one branch = one agent.

Every lane brief carries the same five fields:

- **Owns** — files this lane creates and modifies
- **Never touches** — files belonging to another lane
- **Depends on** — lanes that must be merged first
- **Contract** — the interface it implements or consumes, defined in the trunk
- **Done when** — PRD acceptance criteria, verifiable without a human

Lanes marked **SEQUENTIAL** cannot overlap with anything. Lanes in the same **wave** run concurrently.

Realistic concurrency is 3–4 agents. Queuing more produces merge debt faster than throughput.

## Document authority

**This plan owns sequencing. The PRD owns requirements.**

- Lane, order, dependency, file ownership → here
- What "done" means, FR/US numbers, acceptance criteria → PRD
- The PRD's story groupings are **thematic, not chronological**. Do not derive build order from them
- Where the two disagree about *order*, this plan wins. Where they disagree about *behavior*, the PRD wins

**Every story belongs to exactly one lane.** The table below is the authority. If a story appears unassigned, stop and ask — do not open a lane for it.

| Lane | Stories |
|---|---|
| S0 | US-001, US-002 — **complete, do not re-run** |
| S1 | US-003, US-004, US-017, US-018 |
| A1 | US-012 |
| A2 | US-013, US-014 |
| A3 | US-015 |
| A4 | US-016 |
| B1 | US-023, US-024 (mocked) |
| B3 | US-011, US-025 |
| D1 | US-034, US-035 |
| S2 | US-019, US-021, US-022 |
| E-lanes | US-020 (one tool per lane) |
| C1 | US-006, US-007, US-009, US-010 |
| S3 | US-026 – US-030 |
| F1 | US-031, US-032, US-033 |
| C2 | US-008 |
| D2 | US-036 |
| G1 | US-037 |
| H1 | US-005 |

**Per AGENTS.md §0: agents never run `git worktree add` or create branches. Shahin does.**

---

## Why some work cannot parallelize

**Single-file bottlenecks.** The LangGraph state schema, the Alembic revision chain, and `pyproject.toml` are each one file every lane wants to edit. Two agents adding state keys concurrently produce `InvalidUpdateError` at runtime and a conflict at merge. Two agents writing migrations produce multiple Alembic heads.

**Type ownership.** If the flights lane and the lodging lane each invent a normalized result model, integration becomes a rewrite. Shared types must exist before any consumer.

**Interlocked logic.** Approvals, `reprice`, `resolve_booking_options` and `/handoff` call each other in a fixed order. Split across lanes you get a state machine with holes and no lane able to test it.

**Verification dependency.** Nothing should be built against providers that haven't been proven to work — which is why gates come first, alone.

---

## S0 — Gates · ✅ COMPLETE 2026-07-30

**Findings recorded in PRD Appendix D. Do not re-run — live calls are billed and standing rule 1 forbids it.**

**Verdict:** flights pass on both routes. Airbnb passes with a caveat — `booking_url` with dates works; the dated *details* endpoint returns HTTP 500, so availability checking is unreliable.

**Three findings that change downstream lanes:**
- **100% of flight vendors use POST** (34/34 options). The GET branch in `/handoff` is dead code — keep it, don't test against it
- **Airbnb dated details 500s.** A2 must treat this as expected failure, not an exception
- **Flight numbers arrive with a space** (`"LX 243"`) — strip before building `selected_flights_json`

<details><summary>Original gate definition (retained for reference)</summary>

No repository yet. Curl and a browser.

**S0.1 — Provider spike** (US-001, US-002)
Flight search → `selected_flights_json` → booking options → POST a `post_data` option → **confirm a real payment page loads for that exact itinerary.** Repeat for a Dubai-origin long-haul and a US-domestic route. Then one omkar Airbnb search → open `booking_url` → confirm dates and guests carry through.

**Human required.** An agent cannot judge whether a payment page is the right one.

**S0.2 — Extension check**
Confirm the managed Postgres offers **PostGIS and pgvector**. Fallback: Neon or Supabase for the database, app still on Railway.

**S0.3 — Record findings**
Write results into the PRD: GET vs POST per vendor, omkar's real free-tier limit, observed field shapes. These become the first cassettes.

**Gate:** if S0.1 fails, stop. The data layer reopens and everything downstream is wasted.

</details>

---

## S1 — Trunk · SEQUENTIAL · one agent

Everything below imports from here. This is where merge-friendliness is designed in.

**Owns**
```
pyproject.toml · ruff · docker-compose.yml · alembic/
app/shared/           config, db, cache, quota, cassettes
app/models/           SQLAlchemy models + Pydantic typed results
app/travel/base.py    ProviderAdapter Protocol
app/travel/ports.py   GeocodePort
app/agent/registry.py tool auto-discovery
app/api/__init__.py   router include-by-loop
scripts/seed.py
```

**Deliverables**

1. **Schema + initial migration** — full DDL from PRD Appendix C, including `updated_at`.
2. **Typed result models** — `NormalizedFlight`, `NormalizedLodging`, `NormalizedPlace`, `NormalizedActivity`. Downstream lanes import; none redefine.
3. **`ProviderAdapter` Protocol** — `search()`, `resolve()`, `reprice()`.
4. **Cache adapter with TTL tier as a required argument.** Misclassification must fail loudly, never default silently.
5. **Quota counter** at the adapter boundary, returning structured `quota_exceeded`.
6. **Cassette transport** — `PROVIDER_MODE=live|record|replay`, writing `fixtures/cassettes/{provider}/{args_hash}.json`. Replay is the default everywhere.
7. **Tool registry by auto-discovery** — one file per tool in `app/agent/tools/`, collected at import. *This is what makes tool lanes parallelizable.*
8. **Router include-by-loop** — each module exposes `router`; the aggregator iterates. Same reason.
9. **Seed script** — ~20 `saved_items` across two cities, mixed venue/intent, mixed confidence. **This is what removes ingestion from the critical path.**
10. **Deploy empty** to Railway/Render with Postgres + Redis attached, migrations on release, health check green.

**Done when:** `alembic upgrade head` runs clean from empty, seed populates, cassette replay returns a fixture, deploy is green, and one throwaway adapter proves the Protocol end to end.

---

## Wave A — 4 parallel lanes

Depend on S1 only. Disjoint files. Widest point in the plan.

### A1 · Flights adapter
- **Owns:** `app/travel/adapters/flights.py` + cassettes + tests
- **Notes:** persist `selected_flights_json` (flight numbers + dates), **never** `booking_token`. **Strip whitespace from flight numbers** — SerpApi returns `"LX 243"`, the API needs `"LX243"` (Appendix D). Segment date comes from `departure_airport.time`. Capture `price_insights.typical_price_range`. Preserve `post_data` verbatim. Read `serpapi-google-flights`.
- **Done when:** US-012

### A2 · Lodging adapters
- **Owns:** `adapters/hotels.py`, `adapters/airbnb.py`
- **Notes:** omkar returns `booking_url` with dates applied — **capture during search; the details endpoint does not return it.** Dated details calls return HTTP 500 (Appendix D): catch, fall back to undated, mark availability unknown. Reject past check-in dates before calling — the API returns 0 results with no error. `nightly_rate` may be null; derive the stay total from `cost_breakdown[].amount`. Provider failure returns a structured error so the app degrades to hotels only.
- **Also:** Google Hotels per-vendor `prices[]` entries carry **no booking URL** — hotels do not clear deep-link precision. Surface this rather than engineering around it. Read `serpapi-google-hotels` and `omkar-airbnb` first.
- **Done when:** US-013, US-014

### A3 · Places adapter
- **Owns:** `adapters/places.py`, implements `GeocodePort`
- **Notes:** Places API **(New)** — `nationalPhoneNumber`, not `formatted_phone_number`; `displayName` for the title, since `name` holds the resource path `places/PLACE_ID`. Field masks mandatory. No `booking_request` — restaurants are reference tier. 30-day cache ceiling on Places content; IDs and coordinates indefinite. **Read the `google-places-api` skill first.**
- **Done when:** US-015. **Unblocks C1.**

### A4 · Activities adapter
- **Owns:** `adapters/activities.py`
- **Notes:** Tripadvisor `ssrc` per kind (**case-sensitive** — `a` is All, `A` is Things to Do); `limit` near 30 or records return partial; handle responses with `locations` instead of `places`. Tripadvisor → `price_basis='uncounted'`. **Google Events sets `price_basis` per item** — `actual` when `extracted_price` is present. Events dates carry no year; resolve against the trip range. Read `serpapi-tripadvisor` and `serpapi-google-events`.
- **Done when:** US-016

### Also startable now

**B1 · Frontend shell** — owns `web/` entirely, depends on the OpenAPI stub only. Chat SSE view, day/slot canvas, actionable vs reference styling, `price_basis` + observation time on every price, mocked API. Done when US-023, US-024 render against mocks.

**D1 · Trips module** — owns `app/trips/`. Lifecycle states, shape metrics as a pure SQL aggregate, confirmation endpoints. Zero agent dependency. Done when US-034, US-035.

**B3 · Backlog + spread views** — owns `web/backlog/`, `web/canvas/spread.*`. Backlog list and map (points as pins, locality items as circles, low-confidence visually distinct) and the in-slot geographic spread warning clustered on `geom` with no Directions calls. Depends on S1 only; runs against seed fixtures. Done when US-011, US-025.

---

## S2 — Agent core · SEQUENTIAL

**Depends on:** S1 + A1 + A3 merged.

**Owns:** `app/agent/graph.py`, `state.py`, `nodes/`

Sequential because the state schema is a single file every future lane would otherwise edit, and LangGraph's parallel-write semantics punish concurrent additions.

- Flat ReAct graph, parallel tool calls, `thread_id = trip_id`
- Postgres checkpointer — never `MemorySaver`
- Reducers on any state key parallel nodes can write
- `load_traits` (every turn, plain SQL, all rows), `extract_memories` (post-turn)
- **Freeze the state schema at the end of this lane.** Veto windows keep checkpoints alive up to 12 hours; a schema change mid-window breaks the resume.

**Done when:** US-019, US-021, US-022, and a conversation resumes after a process restart.

---

## Wave B — parallel

### E-lanes · Tools, one file each
Auto-discovery makes each tool its own lane. Run three or four at a time: `search_flights`, `search_lodging`, `search_restaurants`, `read_saved_items`, `recall_context`, `propose_itinerary`. `search_activities` after A4.

- **Never touches:** `graph.py`, `state.py`, `registry.py`
- **Each tool needs its adapter merged first:** `search_flights`←A1, `search_lodging`←A2, `search_restaurants`←A3, `search_activities`←A4. `read_saved_items`, `recall_context` and `propose_itinerary` need only S1.
- **Done when:** US-020 for that tool; agent selects it correctly against cassettes

### C1 · Ingestion ladder
- **Owns:** `app/ingest/` · **Depends on:** S1 + A3 only — **not** S2. May start as soon as A3 merges, concurrently with Wave A. Listed here for readability, not as a dependency.
- **Notes:** Stages 1–2 first — POI tag, then caption parse with hashtags extracted *separately* as locality candidates plus trip/backlog priors. **Measure the Stage-1/2 resolution rate before building Stage 3.**
- **Done when:** US-006, US-007, US-009, US-010

### B2 · Frontend wiring
Replace mocks with real endpoints as they land.

---

## S3 — Money path · SEQUENTIAL

**Depends on:** S2, A1, A2.

**Owns:** `app/approvals/`, `app/handoff/`, `agent/nodes/reprice.py`, `nodes/resolve_booking_options.py`

One lane because these four call each other in a fixed order and a partial implementation is untestable.

- Approval state machine, plan → per-item, actionable items only
- Veto timers on the worker's 30s due-queue; expiry resumes the checkpoint
- `resolve_booking_options` fires **only** on transition into `item_pending`
- `reprice` unconditional before every handoff
- `/handoff/:item_id` server-renders an auto-submitting form, POSTs `post_data` unchanged, **refuses to render a GET link when `post_data` exists**

**Tripwire tests ship in this lane, not after.**

**Done when:** US-026 through US-030, and a real flight reaches a real payment page.

---

## Wave C — independent, any order

| Lane | Owns | Done when |
|---|---|---|
| F1 · Budget | `app/budget/` | US-031, US-032, US-033 |
| C2 · Stage 3 ingestion | `app/ingest/analyze/` | US-008 |
| A5 · Adapter polish | **only** `adapters/airbnb.py`, `adapters/activities.py` — A2 and A4 must be merged and closed first; never run concurrently with them | — |
| D2 · Conditioning | `app/trips/conditioning.py` | US-036 |
| G1 · Edit policy | `app/api/items.py` | US-037 |
| H1 · Shortcut + `/ingest` | `app/api/ingest.py` | US-005 |

**D2 cannot be validated until two trips are confirmed** — a calendar constraint, not an engineering one. Build it last regardless of capacity.

---

## Merge hazards

| Hazard | Mitigation |
|---|---|
| **Alembic multiple heads** | Only S1 and one designated lane write migrations. Others open a request; integrator runs `alembic merge` if it happens |
| **LangGraph state schema edits** | Trunk-owned, frozen after S2. Additions need a sequential lane and break live checkpoints |
| **Tool registry / router aggregator** | Solved structurally in S1 by auto-discovery and include-by-loop |
| **`pyproject.toml`** | All known deps declared in S1; later additions batched by the integrator |
| **Typed models redefined per lane** | Frozen in S1; import, never redeclare |
| **Cassette collisions** | Namespaced per provider; each lane records only its own |

**Integration cadence:** merge to main after every lane, not at wave boundaries. Run tripwires on merge. A lane unmerged for two days is a lane that will conflict.

---

## Where you intervene

1. **S0.1** — judging the payment page. Non-delegable.
2. **Cassette recording** — approving live sessions, since they cost money.
3. **Prompt quality** — no automated oracle for "is this itinerary reasonable." Time-box it.
4. **Merges** between lanes on adjacent surfaces.
5. **Shortcut install** on two physical phones.
6. **Model bake-off** (C2) — scoring venue accuracy and vibe quality across twenty videos.

---

## Standing rules for every lane

1. **Replay mode by default.** Never point a debug loop at live providers.
2. **Never edit outside your Owns list.** Open an issue instead.
3. **Use the PRD's FR/US numbers in Done-when.** Don't reinterpret requirements.
4. **Cache TTL tier is required.** Not optional, not defaulted.
5. **Raw payloads persist to Postgres, never into agent context.**
6. **Tools are agent-chosen; nodes are system-run.** Never expose `reprice` or `resolve_booking_options` as tools.
7. **Tests ship in the lane that writes the code.**
8. **Read your provider's skill before writing the adapter.** `serpapi-google-flights`, `serpapi-google-hotels`, `omkar-airbnb`, `google-places-api`, `serpapi-tripadvisor`, `serpapi-google-events`, plus `travel-providers` for the cross-cutting rules. Several fields mean the opposite of their names.
9. **Never open a worktree or branch.** AGENTS.md §0. Ask Shahin.
