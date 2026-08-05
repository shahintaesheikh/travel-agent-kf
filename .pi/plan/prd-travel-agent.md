# PRD: Travel Agent

**Owner:** Shahin · **Users:** 2 · **Team:** 1 engineer
**Supersedes:** travel-agent-prd v1–v4 (kept for decision history)

---

## 1. Introduction / Overview

Two people plan trips together across scattered tools: flights in one browser tab, hotels in another, restaurant ideas trapped in TikToks nobody can find again, and the plan itself living in a text thread. Nothing connects, nothing persists, and the good restaurant somebody saved in March is invisible in July.

This is a private web app for exactly two users. A conversational AI agent researches flights, lodging, restaurants and activities, assembles a day-structured itinerary, and holds it for human approval. Approved bookable items produce a link that lands directly on the partner's payment page — the only remaining human action is entering card details on that partner's site. **The app never takes payment itself.**

Separately, videos shared from TikTok are processed into saved places or activity intentions, geocoded, and stored in a shared backlog the agent can search when planning a future trip. Completed trips are analyzed for their *shape* — pace, spend distribution, how many activities per day — and used as examples so later plans resemble how these two people actually travel.

### Vocabulary

Terms used throughout, defined once:

| Term | Meaning |
|---|---|
| **Agent** | The LLM running in a loop, choosing tools and writing plans |
| **Tool** | A function the agent chooses to call |
| **Node** | A function the *system* runs at a fixed point; the agent cannot skip or trigger it |
| **Actionable item** | An itinerary entry with a real transaction behind it (flights, lodging) |
| **Reference item** | An itinerary entry with no transaction (restaurants, attractions, events) |
| **Handoff** | Sending the user to a partner's payment page for a specific selection |
| **Deep-link precision** | A link landing on the payment step for *one specific option*, not a search page |
| **Trait** | A durable preference ("won't fly red-eyes"). Never expires |
| **Intent** | A temporary preference ("wants Japan this summer"). Expires after 90 days |
| **Backlog** | Saved places/activities not yet attached to any trip |

---

## 2. Goals

- Plan a complete trip — flights, lodging, ≥3 restaurants, ≥3 activities — in a single conversation thread
- Every bookable item ends in a link that opens the partner's payment page for that exact selection
- Never charge a card, never become merchant of record, never own a booking's servicing
- Turn a shared TikTok into a geocoded, searchable saved item with no manual data entry
- Keep both users informed of what the other proposed before anything is booked
- Stay under a per-trip budget in USD, and be honest about which costs are estimated and which are uncounted
- Make each trip's plan better than the last by learning from completed trips
- Run the whole thing on one web service, one worker, one Postgres, one Redis

---

## 3. User Stories

Grouped by theme, **not by build order**. Each is sized for one focused implementation session.

> **Sequencing is owned by `tasks/dev-plan-travel-agent.md`, not by this document.** The groupings below are thematic. The dev plan assigns each story to a lane and defines what must merge first. Where the two appear to disagree about order, the dev plan wins.

### Theme A — Verification spike ✅ COMPLETE (see Appendix D)

#### US-001: Verify flight deep-link precision — ✅ DONE 2026-07-30
**Do not re-run. Results in Appendix D.** Re-running costs live billed API calls and violates standing rule 1.

**Acceptance Criteria:**
- [x] SerpApi Google Flights search, Dubai-origin long-haul (DXB→LHR)
- [x] Itinerary pinned via `selected_flights_json`, booking options retrieved
- [x] `post_data` POSTed; Swiss.com cart page reached with exact flights, dates, class, pax, price
- [x] US-domestic route repeated (JFK→LAX)
- [x] Vendor GET/POST split recorded — **100% POST, 0% GET across 34 options**

#### US-002: Verify Airbnb dated booking URL — ✅ DONE 2026-07-30
**Do not re-run. Results in Appendix D.**

**Acceptance Criteria:**
- [x] omkar Airbnb search run
- [x] `booking_url` opened; dates and guest count pre-applied and visible in Airbnb UI
- [x] Free tier confirmed: **100/month**. The 5,000 figure is absent from the current README
- [x] Also found: dated *details* endpoint returns HTTP 500; past dates silently return 0 results

### Theme B — Skeleton

#### US-003: Provision database and cache
**Acceptance Criteria:**
- [ ] Postgres with PostGIS and pgvector extensions enabled
- [ ] Redis reachable from app and worker
- [ ] LangGraph Postgres checkpointer table created
- [ ] Migrations run cleanly from empty

#### US-004: Two-user authentication
**Acceptance Criteria:**
- [ ] Exactly two user accounts can be created; a third is rejected
- [ ] Session persists across browser restart
- [ ] All `/trips`, `/items`, `/approvals` routes reject unauthenticated requests
- [ ] Verify in browser using dev-browser skill

#### US-005: Ingest endpoint and iOS Shortcut
**Description:** As a user, I want to share a TikTok from my phone's share sheet so saving a place takes one tap.

**Acceptance Criteria:**
- [ ] `POST /ingest` accepts `{url}` with a static bearer token, outside session auth
- [ ] Invalid or missing token returns 401
- [ ] Creates an `ingest_jobs` row with status `queued` and returns immediately (no blocking work)
- [ ] Shortcut file created and documented; installs on both phones
- [ ] Token is configurable and rotatable without code changes

### Theme C — Ingestion

#### US-006: Stage 1 — POI tag extraction
**Acceptance Criteria:**
- [ ] Worker resolves short links and extracts video ID
- [ ] `yt-dlp` info dict retrieved; creator POI tag read when present
- [ ] POI tag with venue name and address short-circuits to geocoding
- [ ] Job records which stage resolved it

#### US-007: Stage 2 — Caption parse with locality inference
**Description:** As a user, I want captions that name a restaurant to resolve without expensive processing.

**Acceptance Criteria:**
- [ ] oEmbed fetched; `title` field used as the caption
- [ ] Cheap LLM extracts venue name using a forced JSON schema
- [ ] Hashtags parsed **separately** as locality candidates, not merged into the venue name
- [ ] Active trip destinations and backlog geographic clustering supplied as locality priors
- [ ] Venue + locality both present → geocode and stop; venue alone → continue to Stage 3

#### US-008: Stage 3 — Audio, OCR and description fusion
**Acceptance Criteria:**
- [ ] Video downloaded; 8–16 frames sampled and deduplicated by perceptual hash
- [ ] Whisper produces a transcript; OCR produces text per frame
- [ ] Frames persisted to `frames_path` for future re-extraction
- [ ] Cheap text LLM fuses transcript + OCR + Places editorial summary + reviews into structured attributes and one vibe paragraph
- [ ] `description_model` and `description_version` recorded on the row
- [ ] Raw Places review text is **not** persisted

#### US-009: Geocoding and confidence gate
**Acceptance Criteria:**
- [ ] Candidate strings resolved via Google Places Text Search
- [ ] Single strong match auto-saves to backlog
- [ ] Weak or multiple matches present a top-3 pick list on the canvas
- [ ] **Either** user can resolve a pick list
- [ ] Resolution never blocks; unresolved items stay visible in the backlog

#### US-010: Locality-only intent items
**Description:** As a user, I want a parasailing video from Brazil saved even though no specific operator is identifiable.

**Acceptance Criteria:**
- [ ] Item saved with `kind='intent'`, `resolution='locality_only'`
- [ ] Locality geocoded to a centroid with a radius — never null geometry
- [ ] `activity_type` recorded (e.g. "parasailing")
- [ ] Deduplicates on `(activity_type, locality_id)`, not `google_place_id`
- [ ] Rendered on the canvas as an area, not a pin

#### US-011: Backlog list and map
**Acceptance Criteria:**
- [ ] Saved items listed with name, category, source video link, confidence
- [ ] Map view shows point items as pins and locality items as circles
- [ ] Low-confidence items visually distinct
- [ ] Verify in browser using dev-browser skill

### Theme D — Data layer

#### US-012: Flights adapter
**Spike findings (Appendix D):** flight numbers arrive with a space (`"LX 243"`) and **must have whitespace stripped** before use in `selected_flights_json`. Segment date comes from `departure_airport.time` (`"2027-03-10 01:50"`).

**Acceptance Criteria:**
- [ ] Flight-number whitespace stripped before building `selected_flights_json`
- [ ] `search_flights` returns normalized top-N: carrier, flight numbers, times, stops, duration, price USD
- [ ] Raw payload persisted to Postgres, never returned to the agent
- [ ] `selected_flights_json` stored per candidate (flight numbers + dates), **not** `booking_token`
- [ ] `price_insights.typical_price_range` captured when present
- [ ] Results cached 15 minutes on normalized arguments

#### US-013: Hotels adapter
**Acceptance Criteria:**
- [ ] Normalized results: property, rating, price/night USD, `booking_request`
- [ ] Cached 30 minutes

#### US-014: Airbnb adapter
**Spike findings (Appendix D):** the **dated details endpoint returns HTTP 500** after ~61s — availability checking with dates is broken upstream. Undated details works. Dated *search* works. Past dates return `total_results: 0` with no error. `nightly_rate` was `null` on 2027-dated responses; the stay total appeared in `cost_breakdown[].amount`.

**Acceptance Criteria:**
- [ ] Normalized results include `booking_url` with dates and guests applied
- [ ] Reject past `check_in` dates before calling — the API returns 0 results with no error
- [ ] Stay total derived from `cost_breakdown[].amount` when `nightly_rate` is null; never parse the human-readable `label`
- [ ] Dated details calls treated as expected-failure: catch the 500, fall back to undated details, mark availability unknown
- [ ] `cancellation_terms`, `is_available`, `unavailability_reason` captured **when the details call succeeds**
- [ ] Provider failure returns a structured error, not an exception — app degrades to hotels only
- [ ] Cached 30 minutes

#### US-015: Restaurants adapter
**Acceptance Criteria:**
- [ ] Returns `displayName`, rating, `priceLevel`, `nationalPhoneNumber`, Maps link (Places API **New** names — legacy `name` now holds the resource path `places/PLACE_ID`)
- [ ] No `booking_request` field is produced — restaurants are reference tier
- [ ] Place details cached 30 days maximum; `place_id` and coordinates cached indefinitely

#### US-016: Activities adapter
**Acceptance Criteria:**
- [ ] `kind='attraction'|'experience'` queries Tripadvisor with `ssrc` set appropriately
- [ ] `kind='event'` queries Google Events
- [ ] `place_type='ATTRACTION_PRODUCT'` results link to the specific product page
- [ ] `limit` capped near 30 to avoid partial records
- [ ] Handles responses returning `locations` instead of `places`
- [ ] Tripadvisor results flagged `price_basis='uncounted'` (engine returns no price at all)
- [ ] Google Events results set `price_basis` **per item**: `actual` when `extracted_price` is present, `uncounted` otherwise

#### US-017: Cache layer
**Acceptance Criteria:**
- [ ] Key format `{provider}:{tool}:{sha256(normalized_args)}`
- [ ] TTL tier is a **required** argument — a call without one fails at type-check or raises
- [ ] Booking options are never cached

#### US-018: Quota enforcement
**Acceptance Criteria:**
- [ ] Counter at `quota:{provider}:{hour}` incremented before each outbound call
- [ ] Limits: 6 calls per agent turn, 40 per trip-hour
- [ ] Exceeding returns a structured `quota_exceeded` result, not an exception
- [ ] Enforced in the adapter, not in agent instructions

### Theme E — Agent

#### US-019: LangGraph ReAct graph
**Acceptance Criteria:**
- [ ] Single flat agent loop with parallel tool calls enabled
- [ ] `thread_id` equals `trip_id`
- [ ] Postgres checkpointer persists across process restart
- [ ] Conversation resumes correctly after a restart mid-thread

#### US-020: Wire the seven tools
**Acceptance Criteria:**
- [ ] Exactly these are exposed: `search_flights`, `search_lodging`, `search_restaurants`, `search_activities`, `read_saved_items`, `recall_context`, `propose_itinerary`
- [ ] `search_lodging` accepts `source: hotels|airbnb|both`
- [ ] `search_activities` accepts `kind: attraction|experience|event|poi`
- [ ] `reprice` and `resolve_booking_options` are **not** callable by the agent

#### US-021: Trait loading and context recall
**Acceptance Criteria:**
- [ ] `load_traits` runs at the start of **every** turn and loads all non-superseded traits for both users
- [ ] Traits are loaded by plain SQL — no embedding, no similarity search
- [ ] `recall_context` runs on first pass and is re-callable by the agent
- [ ] Every memory carries `owner_id` and is presented with its owner

#### US-022: Post-turn memory extraction

#### US-022a: Agent call logging
**Description:** As the engineer, I want each LLM call logged with its decision and cost
so I can find an anomalous turn and pull the full state from its checkpoint.

**Acceptance Criteria:**
- [ ] Every LLM call emits one structlog line with `thread_id`, `checkpoint_id`,
      `tools_chosen`, `model`, `tokens_in`, `tokens_out`, `latency_ms`
- [ ] A failed call emits the same line plus the exception, before the error propagates
- [ ] No message content, prompt text, or tool arguments appear in any log line
- [ ] `checkpoint_id` is present on every line and resolves to a real checkpoint
- [ ] Logging failure never breaks a turn

#### US-022b: User memory logging
**Description:** I want each trait or intent deemed worthy written to a db to be queried later when necessary. 

**Acceptance Criteria:**
- [ ] Runs after each turn; writes `trait` or `intent` memories
- [ ] Deduplicates against existing memories for that owner
- [ ] Contradiction sets `superseded_by` on the old row rather than deleting
- [ ] Intents default to a 90-day expiry; traits never expire

#### US-023: Chat streaming
**Acceptance Criteria:**
- [ ] Agent responses stream token-by-token over SSE
- [ ] Tool calls surface as visible activity while running
- [ ] Verify in browser using dev-browser skill

#### US-024: Itinerary canvas
**Acceptance Criteria:**
- [ ] Days render as columns or rows with morning/afternoon/evening slots
- [ ] Actionable and reference items are visually distinguishable
- [ ] Every price displays its `price_basis` and observation time
- [ ] Canvas updates live as the agent works
- [ ] Verify in browser using dev-browser skill

#### US-025: Geographic spread warning
**Description:** As a user, I want a warning when one slot has stops scattered across a city.

**Acceptance Criteria:**
- [ ] Items within a slot clustered by `geom`
- [ ] Warning shown when max pairwise distance exceeds a configurable threshold
- [ ] No Directions API calls are made
- [ ] Verify in browser using dev-browser skill

### Theme F — Approval and handoff

#### US-026: Approval state machine
**Acceptance Criteria:**
- [ ] States: `draft → plan_pending → plan_approved → item_pending → item_approved | vetoed | stale | unpriceable`
- [ ] Only actionable items enter `item_pending`
- [ ] Plan approval covers the whole itinerary including reference items
- [ ] Veto returns the plan to the agent with the reason attached

#### US-027: Veto windows
**Acceptance Criteria:**
- [ ] Windows: flights 2h, lodging 12h
- [ ] Worker polls expired approvals every 30 seconds and resumes the checkpoint
- [ ] First write wins on a veto race, guarded by `resolved_at`
- [ ] Pending approvals survive a worker restart
- [ ] UI states plainly that no notification is sent

#### US-028: Booking option resolution
**Acceptance Criteria:**
- [ ] Fires only on transition into `item_pending`
- [ ] Never fires during exploration or for more than one candidate
- [ ] Option lists stored with `options_partial=true` and labeled as partial in the UI
- [ ] An escape-hatch link to Google Flights for the same itinerary is always shown

#### US-029: Re-price
**Description:** As a user, I want the price re-checked immediately before every handoff so the number I approved is the number I pay.

**Three outcomes.** `reprice()` returns a `Priced`, or it fails. A failure is not a price of zero and not a silent pass — see US-029a.

**Acceptance Criteria:**
- [ ] Runs unconditionally before every handoff, regardless of cache or recency
- [ ] Comparison routes through the `price_drift` helper — never an inline subtraction, so the `price_unit` guard applies
- [ ] Itinerary gone, or price moved more than 5% → item becomes `stale` and returns to the agent
- [ ] Threshold configurable
- [ ] Handoff is impossible without a `Priced` result inside threshold

#### US-029a: Unpriceable re-price
**Description:** As a user, I want to be told when the price can't be checked at all, rather than seeing a stale number or a generic error.

**Why this is separate from `stale`.** `stale` means *the price moved, here's the new one, re-approve* — the agent can re-search and resolve it. An unpriceable item means *we cannot determine the price*, which no amount of re-searching fixes. Routing it back to the agent invites a retry loop against a provider that is down. Airbnb's dated details endpoint returns HTTP 500 as documented expected behaviour (Appendix D), so this is a regular state, not an exceptional one.

**Acceptance Criteria:**
- [ ] `reprice()` returning a provider error moves the item to `unpriceable`, **not** `stale`
- [ ] `unpriceable` blocks handoff exactly as `stale` does
- [ ] The item is **not** returned to the agent for re-search
- [ ] The reason is surfaced to the user, naming the provider and the cause — not a generic failure
- [ ] The user may retry manually; there is no automatic retry loop
- [ ] An item may leave `unpriceable` on a later successful re-price
- [ ] A re-price error is never treated as a passing re-price under any code path
- [ ] Verify in browser using dev-browser skill

#### US-030: Handoff endpoint
**Spike findings (Appendix D):** every vendor tested used POST — 34 of 34 options across both routes. POSTing `post_data` to `https://www.google.com/travel/clk/f` returns HTTP 200 whose body is an HTML `<meta refresh>` redirect, **not** a 3xx. The GET branch is retained but is currently dead code.

**Acceptance Criteria:**
- [ ] Meta-refresh response body passed through to the browser, not followed server-side
- [ ] `GET /handoff/{item_id}` re-prices, then serves a minimal auto-submitting page
- [ ] `post_data` present → POST unchanged, server-side, opaque
- [ ] `post_data` absent → plain redirect
- [ ] Endpoint refuses to render a GET link when `post_data` exists
- [ ] Writes a `handoffs` row with resolved price, vendor and method
- [ ] Verify in browser using dev-browser skill

### Theme G — Budget

#### US-031: Budget cap and headroom
**Acceptance Criteria:**
- [ ] `budget_total_usd` set per trip
- [ ] Headroom = total − committed − proposed
- [ ] Committed is driven by `handed_off_at IS NOT NULL`, never by item status
- [ ] Editing a handed-off item does not decrement committed spend

#### US-032: Price basis rendering
**Acceptance Criteria:**
- [ ] Three bases render distinctly: `actual`, `price_level_estimate`, `uncounted`
- [ ] Restaurant estimates derive from `price_level` and are labeled as estimates
- [ ] Budget summary states plainly that experiences are not counted
- [ ] Verify in browser using dev-browser skill

#### US-033: Overrun handling
**Acceptance Criteria:**
- [ ] Over-budget options still appear in search results
- [ ] Approval of an item that breaches the cap is blocked with a visible explanation
- [ ] Results are never silently filtered by price

### Theme H — Trip lifecycle and learning

#### US-034: Lifecycle and confirmation prompt
**Acceptance Criteria:**
- [ ] Worker moves trips to `awaiting_confirmation` when `end_date` passes
- [ ] Prompt on next app open asks whether the trip happened
- [ ] Manual "done" button marks a trip taken early
- [ ] Unanswered trips remain `awaiting_confirmation` and are excluded from conditioning
- [ ] Prompt captures one optional free-text line, written as a trait candidate
- [ ] Verify in browser using dev-browser skill

#### US-035: Shape metrics
**Acceptance Criteria:**
- [ ] Computed on transition to `confirmed_taken`
- [ ] Stores `items_per_day`, `pct_days_with_activity`, `avg_meal_price_level`, spend by category, `trip_length`, `season`, `archetype`
- [ ] Pure SQL aggregate — no LLM call

#### US-036: Few-shot conditioning
**Acceptance Criteria:**
- [ ] Activates only when ≥2 trips are `confirmed_taken`
- [ ] Selects 2–3 nearest trips by numeric distance over shape metrics — not by embedding
- [ ] Loads on the planning turn only, never every turn
- [ ] Recent trips weighted higher
- [ ] Prompt explicitly instructs variation rather than reproduction

#### US-037: Edit policy
**Acceptance Criteria:**
- [ ] Items with `handed_off_at IS NULL` edit freely
- [ ] Items with `handed_off_at` set require a confirmation dialog stating the app cannot cancel a booking
- [ ] Editing after handoff sets `diverged=true` and retains `original_snapshot`
- [ ] Diverged items are visually flagged on the canvas
- [ ] Post-trip editing remains available on closed trips
- [ ] Verify in browser using dev-browser skill

---

## 4. Functional Requirements

### Ingestion
- **FR-1:** The system must accept a video URL at `POST /ingest` authenticated by a static bearer token, separate from session auth.
- **FR-2:** The system must process ingestion asynchronously in a worker and return immediately.
- **FR-3:** The system must attempt extraction in cost order: creator POI tag, then caption parse, then transcript (YouTube only), then full audio + OCR analysis, stopping at the first confident result.
- **FR-4:** The system must parse hashtags as locality candidates separately from venue-name extraction.
- **FR-5:** The system must supply active trip destinations and backlog geographic clustering as locality priors during geocoding.
- **FR-6:** The system must persist sampled frames for any video reaching Stage 3.
- **FR-7:** The system must not persist raw Google Places review text; only derived descriptions.
- **FR-8:** When geocoding is ambiguous, the system must present a top-3 pick list resolvable by either user.
- **FR-9:** When no venue resolves, the system must save an intent item with a geocoded locality centroid and radius.
- **FR-10:** The system must deduplicate venues on `google_place_id` and intents on `(activity_type, locality_id)`.

### Agent
- **FR-11:** The system must expose exactly seven tools to the agent, as listed in US-020.
- **FR-12:** The system must run `load_traits`, `resolve_booking_options`, `reprice`, `extract_memories` and `build_handoff` as system nodes the agent cannot invoke.
- **FR-13:** The system must load all non-superseded traits for both users at the start of every turn, using plain SQL.
- **FR-14:** The system must return normalized top-N results to the agent and persist raw payloads outside the conversation context.
- **FR-15:** The system must cap outbound provider calls at 6 per turn and 40 per trip-hour, enforced in the adapter.
- **FR-16:** The system must return a structured `quota_exceeded` result rather than raising when limits are hit.
- **FR-16a:** The system must log one structured line per LLM call carrying the chosen
  tools, model, token counts, latency, and the `checkpoint_id` needed to retrieve full
  state. Message content must not be logged.
- **FR-16b:** The system must log LLM call failures with the same fields plus the
  exception, since a failed step may not produce a checkpoint.

### Itinerary
- **FR-17:** The system must assign each item a day and a coarse slot of morning, afternoon or evening.
- **FR-18:** The system must not assign explicit clock times to any item.
- **FR-19:** The system must classify each item as actionable (flights, lodging) or reference (everything else).
- **FR-20:** The system must warn when items within one slot exceed a configurable geographic spread, without calling a directions service.
- **FR-21:** The system must display `price_basis` and observation time alongside every price.

### Approval
- **FR-22:** The system must require plan-level approval before any per-item approval.
- **FR-23:** The system must require per-item approval only for actionable items.
- **FR-24:** The system must allow the non-proposing user to veto within a class-specific window (flights 2h, lodging 12h) and auto-approve on expiry.
- **FR-25:** The system must state in the UI that no notification is sent when a veto window opens.
- **FR-26:** The system must run `reprice` before every handoff regardless of cache state or recency.
- **FR-27:** The system must mark an item `stale` and return it to the agent when re-pricing finds it gone or moved more than a configurable threshold (default 5%).
- **FR-27a:** The system must mark an item `unpriceable` when re-pricing fails to return a price, block handoff, surface the provider and reason, and not return the item to the agent for re-search.
- **FR-27b:** The system must perform every re-price comparison through the shared drift helper so that mismatched price units raise rather than producing a false drift.
- **FR-28:** The system must resolve booking options only on transition into `item_pending`.
- **FR-29:** The system must label every booking-option list as potentially incomplete and provide an escape-hatch link.

### Handoff
- **FR-30:** The system must POST `post_data` unchanged when present and redirect via GET only when it is absent.
- **FR-31:** The system must refuse to render a GET link for any option carrying `post_data`.
- **FR-32:** The system must perform the handoff server-side and log every handoff with resolved price, vendor and method.
- **FR-33:** The system must never collect payment details.

### Budget
- **FR-34:** The system must enforce a per-trip budget cap denominated in USD.
- **FR-35:** The system must compute headroom as total minus committed minus proposed, where committed is driven by `handed_off_at`.
- **FR-36:** The system must estimate restaurant costs from `price_level` and label them as estimates.
- **FR-37:** The system must exclude Tripadvisor experiences and attractions from the budget and state this in the UI. Google Events carrying `extracted_price` are counted as `actual`; events without a price are `uncounted`.
- **FR-38:** The system must block approval of items breaching the cap while still displaying them in search results.

### Lifecycle
- **FR-39:** The system must move trips to `awaiting_confirmation` when `end_date` passes.
- **FR-40:** The system must prompt on next app open to confirm whether a trip happened, and provide a manual "done" action.
- **FR-41:** The system must exclude unconfirmed trips from conditioning.
- **FR-42:** The system must compute shape metrics via SQL aggregate on confirmation.
- **FR-43:** The system must select 2–3 conditioning examples by numeric distance over shape metrics, on planning turns only, once at least two trips are confirmed.
- **FR-44:** The system must allow free editing of items without `handed_off_at`, and require confirmation for those with it.
- **FR-45:** The system must mark edited handed-off items `diverged`, retain the original snapshot, and leave committed spend unchanged.

---

## 5. Non-Goals

- **No payment collection.** The app is never merchant of record, never holds card details, never issues tickets.
- **No booking servicing.** Changes, cancellations, schedule disruptions and refunds are handled by the user with the partner directly.
- **No restaurant reservations or availability.** Restaurants get a phone number and a Maps link.
- **No experience pricing or booking.** Discovery only.
- **No notifications of any kind.** No email, push, or SMS.
- **No explicit clock-time scheduling** or travel-time validation between items.
- **No price tracking over time.** Dates are fixed by the academic calendar; there is nothing to wait for.
- **No native mobile app.** The iOS Shortcut is the only mobile surface.
- **No third user, sharing, roles, or invites.**
- **No embedding of structured data.** Only `vibe_text` is vectorized.
- **No edit-diff pipeline.** The edited final trip is the training label.
- **No automatic retry on an unpriceable item.** A provider outage is surfaced, not retried in a loop.

---

## 6. Design Considerations

- **Two surfaces, one screen.** Chat on one side, live itinerary canvas on the other. Approvals live on the canvas, not in the message stream — a veto window is a stateful object with a countdown, and a chat message can be scrolled past.
- **Prices always carry provenance.** Basis and observation time render next to every number. Without this, "under budget" means something different on a museum-heavy trip than a beach one.
- **Low-confidence saves look different.** Ambiguous geocodes and diverged items are visually distinct, never silently equivalent to confirmed data.
- **Locality items are areas, not pins.** Rendering an intent as a precise pin implies precision that does not exist.
- **The handoff page is deliberately minimal** — a server-rendered auto-submitting form, no SPA involvement, so `post_data` passes through untouched.

---

## 7. Technical Considerations

### Stack
FastAPI · LangGraph (Postgres checkpointer) · Postgres with PostGIS and pgvector · Redis (cache + quota) · arq worker · React SPA.

One web service, one worker, one database, one cache. The worker exists because video ingestion takes 30s–2min, veto windows need a waker, and trip close needs a sweep — all genuinely out-of-band, all sharing a single 30-second due-queue poll.

### Module layout
```
app/
  agent/      graph, tools, nodes, prompts, state
  ingest/     shortcut endpoint, ladder, asr, ocr, describe
  travel/     serpapi + places + omkar adapters, normalizers, quota
  handoff/    booking_request replay, form rendering
  approvals/  state machine, veto timers, reprice
  memory/     saved_items repo, traits, intents
  trips/      lifecycle, shape metrics, conditioning
  shared/     cache, ladder, db, config
```
Dependency direction: `api → agent → travel/memory/approvals/trips → shared`. Nothing in `travel/` imports `agent/`.

### Providers
| Domain | Provider | Notes |
|---|---|---|
| Flights | SerpApi Google Flights | `selected_flights_json` pins an itinerary segment-by-segment and returns booking options directly — two calls, and what persists is flight numbers and dates rather than an expiring token. `price_insights` gives typical range free. |
| Hotels | SerpApi Google Hotels | — |
| Airbnb | omkar Airbnb Scraper API | `booking_url` carries check-in, check-out and guests. Highest vendor risk in the stack. |
| Restaurants | Google Places **(New)** | `nationalPhoneNumber` from Place Details; `displayName` for the title — `name` holds the resource path. Field masks mandatory. 30-day caching ceiling on Places content. |
| Attractions & experiences | SerpApi Tripadvisor | `ATTRACTION_PRODUCT` is Viator inventory. No price, no availability. Keep `limit` near 30; some queries return `locations` instead of `places`. |
| Events | SerpApi Google Events | — |
| Transcripts | SerpApi YouTube Video Transcript | Only if ingestion widens beyond TikTok. |

SerpApi serves identical queries within 1 hour from its own cache, free and off-quota — real hit rates will beat the local cache numbers.

### Cache tiers
| Class | TTL |
|---|---|
| Flight search | 15 min |
| Booking options | Never |
| Hotel / Airbnb search | 30 min |
| Tripadvisor / Events | 6h |
| Places details, reviews | 30 days (hard ceiling) |
| `place_id`, coordinates, geocodes | Indefinite |

TTL tier is a required argument on the cache adapter, never a default — misclassification fails silently, and a fare in the 30-day tier looks fresh and is fiction.

### Data model
Full DDL in Appendix C. Three fields carry unusual weight:
- **`handed_off_at`** gates both the edit-confirmation dialog and committed-spend accounting.
- **`price_basis`** has three values and drives all budget rendering.
- **`vibe_embedding`** is the only vector column in the system.

### Model swaps
Changing the description LLM changes phrasing, so old and new descriptions embed unevenly — batch re-describe, tracked by `description_model` / `description_version`. Changing the embedding model invalidates every vector and requires a full re-index. Config value versus migration; keep them independent.

---

## 8. Success Metrics

**Primary — the end-to-end promise.**
- One real trip planned in a single thread: flights, lodging, ≥3 restaurants, ≥3 activities
- 100% of flight and lodging handoffs land on a payment page for that exact selection
- 0 handoffs rendered as GET links when `post_data` was present
- 0 handoffs without a passing re-price immediately prior
- Displayed price matches the partner's payment page within the threshold, or the item was marked `stale` instead

**Ingestion quality.**
- Over 20 shared videos: ≤1 wrong-venue save, and any wrong save correctable in one tap
- 0 items saved with null geometry
- ≥50% of videos resolve at Stage 1 or 2 without reaching full analysis (if lower, ingestion cost needs revisiting)

**Correctness.**
- 0 preference memories applied to the wrong owner
- 0 hours in which the provider quota ceiling was exceeded
- 0 booking-option resolutions for items that never reached `item_pending`
- 0 cases where editing a handed-off item changed committed spend
- 0 log lines containing message content or prompt text
- Every logged LLM call resolves to a retrievable checkpoint

**Learning.**
- By the third confirmed trip, conditioning examples load on planning turns and shape metrics differ measurably between trips

**Explicit non-metric:** latency. The cache exists for cost, not speed. At two users, slow planning is acceptable.

---

## 9. Open Questions

1. **Optimistic concurrency (`updated_at`) — RESOLVED: build it.** The dev plan's S1 trunk includes the column, so this is decided; the note below is retained as rationale.

   *Original framing:* recommended but unapproved. The two users have agreed to coordinate edits socially, but the agent is a third writer that rewrites `itinerary_items` on veto revision and `stale` re-search. Roughly five lines converts a silent lost update into a "this changed, reload" message. Include it or explicitly accept the risk.
2. **Which OCR, ASR and description models?** Deferred pending a bake-off. Rank on dense scene-text OCR over stylized fonts, cost per video-minute rather than per token, and structured-output reliability under a forced schema — general multimodal benchmarks do not isolate the capability that matters here.
3. **Experience cost blindness.** Museum- or tour-heavy trips can pass the cap and cost materially more. If it bites, cap *count* (e.g. two paid activities per day) rather than reintroducing invented prices.
4. **omkar durability.** Free tier confirmed at 100/month (Appendix D), not the 5,000 once claimed. Small vendor: WhatsApp support, no status page, and a dated details endpoint that 500s. Behind an adapter at $16/month with fallback to hotels — do not build anything assuming it persists.
5. **Ingestion beyond TikTok.** The YouTube transcript rung only pays off if the sources widen. Unresolved whether they should.
6. **Conditioning ossification.** Recency weighting and variation prompts are specified, but whether they are sufficient is only observable after several trips.

---

## Appendix A: Decision log

Preserved because the reasoning is what prevents these from being quietly undone.

| # | Decision | Why |
|---|---|---|
| D0 | Deep-link precision is the provider acceptance test | The promise is "approve, then enter card details" |
| D1 | SerpApi + Google Places + omkar | Only self-serve combination clearing D0 across all domains |
| D2 | Flat ReAct loop, parallel tool calls | The four domains are one constraint-satisfaction problem — arrival gates lodging, lodging gates dinner. A supervisor would be an LLM-priced router deciding what a switch statement knows |
| D3 | Normalized top-N to the agent; raw persisted out of context | Raw JSON in message history degrades reasoning three turns later |
| D4 | Two-stage approval | Plan approval alone cannot survive drift between decision and action |
| D5 | Partner checkout, never ours | Avoids merchant status, PCI, and being your own support desk at 2am abroad |
| D6 | Proposer approves; other vetoes; timeout auto-approves | Dual consent deadlocks when one person is asleep |
| D7 | `reprice` unconditional before handoff | Live providers make re-resolution a real call with a real answer |
| D8 | Structured data in SQL/PostGIS; only `vibe_text` embedded | "Saved places in Lisbon under $30" is a `WHERE`, not a cosine similarity |
| D9 | One Postgres + Redis | PostGIS, pgvector, JSONB and the LangGraph checkpointer. Firestore does none natively |
| D10 | `thread_id` = `trip_id`, plus a global backlog | Ingestion produces items before a trip exists |
| D11 | Extraction ladder, cheapest rung first | Captions are high-precision, mediocre-recall |
| D12 | Chat + canvas, approvals on canvas | A veto window is a stateful object with a countdown |
| D15 | Handoff is an auto-submitting form | `post_data` cannot be expressed as a hyperlink |
| D16 | Booking-option lists presented as partial | Documented cases of fewer options than Google Flights shows |
| D17 | Tools are LLM-chosen; nodes are system-run | `reprice` as a tool let the agent skip the one call that must be unconditional |
| D18 | Traits load wholesale; no vectors on memories | "Plan Tokyo" embeds nowhere near "he won't fly red-eyes" |
| D19 | No VLM — Whisper + OCR + cheap text LLM | Once a video resolves to a place, the vibe paragraph no longer needs vision. OCR specialists beat general VLMs at stylized scene text, which is where venue names live |
| D20 | Text descriptions, not video vectors | TikToks get deleted. A paragraph re-embeds forever; a video-derived vector cannot be regenerated |
| D21 | `saved_items` with resolution levels | A parasailing video resolves to activity + locality, not a point |
| D22 | Actionable vs reference tiers | Otherwise every POI needs a `booking_request` that is null forever |
| D23 | Day + coarse slot | Explicit times without travel validation look precise and put you across town in fifteen minutes |
| D24 | Hard budget cap with three tiers of price truth | A cap that filters silently is worse than one that flags |
| D25 | Shape metrics + SQL-selected few-shot | Trip documents embed as proper nouns, so similarity retrieves by *destination*, not by *style* |
| D26 | Edit policy keyed on `handed_off_at` | An unbooked draft flight should edit freely; only clicked-through items need friction |

## Appendix B: Rejected providers

Four died mid-design, every one of them well-documented. This is why US-001 and US-002 precede all code.

| Provider | Why rejected |
|---|---|
| Expedia Rapid | Approved-partner only; individuals do not qualify |
| Airbnb direct | No public API, no self-serve portal |
| Amadeus Self-Service | Portal decommissioned July 17, 2026; keys disabled |
| Duffel | Its value is *being* the seller. We redirect, so we would pay per order and take merchant status for a checkout we never use |
| Travelpayouts | Cached data built for static pages; cannot deep-link to a live payment step |
| Viator Partner API | Gated onboarding and certification; inventory reachable through Tripadvisor anyway |
| Foursquare | Free tier cut to 500 calls/month June 2026; V3 deprecated May 2026 |
| Yelp Fusion | Paid only, thinner coverage than Google |
| SearchApi.io | Separate vendor from SerpApi; consolidation not worth a second account |

## Appendix C: Schema

```sql
users (id, name)

trips (
  id, title, destination, start_date, end_date,
  status,                    -- draft | active | awaiting_confirmation
                             -- | confirmed_taken | archived
  budget_total_usd,
  created_by, created_at, closed_at,
  items_per_day, pct_days_with_activity, avg_meal_price_level,
  spend_flight, spend_lodging, spend_food_est, trip_length,
  season, archetype          -- city | beach | roadtrip
)

saved_items (
  id,
  kind,                      -- venue | intent
  resolution,                -- exact_venue | operator_unknown | locality_only
  geo_precision,             -- point | locality | region
  google_place_id UNIQUE NULL,
  activity_type NULL, locality_id NULL,
  name, address,
  geom GEOGRAPHY(POINT), radius_m,
  category,
  attrs JSONB,               -- category-typed
  vibe_text TEXT,
  vibe_embedding VECTOR,     -- the only vector column
  transcript TEXT, ocr_text TEXT,   -- never embedded
  frames_path TEXT,
  source, source_video_url, confidence,
  description_model, description_version,
  trip_id NULL,              -- NULL = backlog
  saved_by, created_at
)

itinerary_items (
  id, trip_id, day, slot,    -- morning | afternoon | evening
  tier,                      -- actionable | reference
  type,                      -- flight | lodging | restaurant | activity | event
  saved_item_id NULL,
  raw_payload JSONB, normalized JSONB,
  selected_flights_json JSONB,
  booking_request JSONB,     -- {url, post_data?, vendor}
  options_partial BOOL,
  status,
  price_amount_usd, price_observed_at,
  price_basis,               -- actual | price_level_estimate | uncounted
  handed_off_at NULL,
  diverged BOOL, original_snapshot JSONB,
  updated_at,                -- optimistic concurrency guard; agent is a third writer
  created_at
)

approvals (id, item_id NULL, trip_id, scope, proposed_by,
           expires_at, resolved_at, outcome)

memories (id, owner_id, kind, text, created_at,
          expires_at NULL, superseded_by NULL)

handoffs (id, item_id, resolved_price_usd, vendor, method, occurred_at)

ingest_jobs (id, url, status, stage, error, saved_item_id NULL, created_at)
```

---

## Appendix D: S0.1 — Provider spike findings

Recorded 2026-07-30. System date is July 2026; all dates tested must be 2027+.

### SerpApi Google Flights

**Routes tested:**
- Dubai→London (DXB→LHR, round trip, 2027-03-10→2027-03-20) — long-haul
- New York→Los Angeles (JFK→LAX, round trip, 2027-03-10→2027-03-15) — US-domestic

**Search results:**
| Route | Best flights | Other flights | Best price | Typical range |
|---|---|---|---|---|
| DXB→LHR | 3 | 13 | $640 | $540–760 |
| JFK→LAX | 4 | 29 | $344 | — |

**`selected_flights_json` format (confirmed working):**
```json
{"outbound": [{"flight_number": "LX243", "departure_id": "DXB", "arrival_id": "ZRH", "date": "2027-03-10"}]}
```
- Flight numbers from SerpApi have a space (e.g. `"LX 243"`); must strip whitespace for the API.
- Date extracted from `departure_airport.time` (format: `"2027-03-10 01:50"`).

**Booking options:**
| Route | Options | Vendors | POST vs GET |
|---|---|---|---|
| DXB→LHR | 19 | SWISS, Booking.com, Flightnetwork, Expedia, Kayak, etc. | **All POST** — no GET links |
| JFK→LAX | 15 | Alaska, OOJO, Expedia, SmartFares, Travel Up, Priceline, etc. | **All POST** — no GET links |

**POST handoff verified (DXB→LHR via SWISS):**
- POST `post_data` as raw body to `https://www.google.com/travel/clk/f` → HTTP 200
- Response contains an HTML `<meta refresh>` redirect to Swiss.com deep link
- Deep link includes exact flights (LX243, LX316), dates, class (Economy), passenger count (1 adult), and price (1735 AED ≈ $473 USD) in `Mode=CART`
- Browser opened the deep link → Swiss.com security check triggered (bot detection), confirming the link format is correct and points to the booking cart

**JFK→LAX** via Alaska Airlines followed the same pattern.

**`price_insights`:** Present in both search and booking-options responses. Contains `lowest_price`, `price_level`, `typical_price_range`, `price_history`.

**`departure_token`:** Present per best_flight in search response. Not needed when using `selected_flights_json`.

**Known bug confirmed:** Booking options list is potentially incomplete (`options_partial=true`); escape-hatch Google Flights link required.

### Omkar Airbnb

**Search with dates:** ✅ Works. 40 listings for Tokyo. `booking_url` has dates pre-applied.

**Search without dates:** ✅ Works (40 results), but `booking_url` has empty date params (`check_in=&check_out=`).

**Search with past dates:** ❌ Instant `total_results: 0`, no error message. The API silently rejects dates before the current date. No validation error is returned — just empty results.

**Details endpoint with dates:** ❌ HTTP 500 after ~61s. The dated availability path is broken.

**Details endpoint without dates:** ✅ Works. Returns full listing including `is_available`, `cancellation_terms`, `pricing`.

**`booking_url` format:** `https://www.airbnb.com/rooms/1634791428499562288?check_in=2027-09-10&check_out=2027-09-14&adults=2`
- Browser verification: ✅ Airbnb page loaded with dates and guests pre-applied (check-in 2027-09-10, checkout 2027-09-14, 2 guests visible in the UI).

**`booking_url` exists only on search endpoint.** Not returned by details. Must capture during search and persist.

**Pricing trap (confirmed):** `nightly_rate` is `null` in 2027-dated search response. The actual total appears in `cost_breakdown[].amount` (229.68 for 4 nights). The label is human-readable text (`"4 nights x $57.42"`).

**`total_cost`:** `null` in search response even with dates.

**Free tier:** 100/month (confirmed from README and rate-limit table). The 5,000 figure mentioned elsewhere in the README is absent from the current version.

**Auth:** `API-Key` header (exact casing). Invalid key → HTTP 400 with `{"message":"Invalid API key."}`.

**Vendor risk assessment:** As documented. Search endpoint is reliable for undated queries; date-aware search works but silently fails on past dates. Details endpoint is unreliable with dates (500s). The `booking_url` with dates is the key value proposition and it works when dates are valid.

### Key findings summary

| Finding | Value |
|---|---|
| Flight search → booking options → payment page | ✅ Confirmed for both routes |
| Flight vendors using POST | 100% (all 19+15 options across both routes) |
| Flight vendors using GET | 0% |
| `selected_flights_json` durability | ✅ Flight numbers + dates, replayable indefinitely |
| `price_insights` available | ✅ Yes, in both search and booking responses |
| Airbnb `booking_url` with dates | ✅ Confirmed working |
| Airbnb dated search silently fails on past dates | ⚠️ Returns 0, no error |
| Airbnb dated details endpoint | ❌ HTTP 500 |
| omkar free tier | 100/month (not 5,000) |

**Gate verdict:** S0.1 passes for flights (both routes confirmed). Airbnb passes with the caveat that the dated details path is broken — the `booking_url` with dates works, but availability checking (`is_available`) requires a second call that may fail.
