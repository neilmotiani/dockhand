# Design decisions

## uv + pyproject.toml
One tool for the virtualenv, dependency resolution and a committed lockfile (`uv.lock`). CI and the Docker image install with `--frozen`, so every environment gets the same versions.

## Configuration via pydantic-settings
All model IDs, prompt versions, thresholds and secrets come from environment variables (`.env` locally). Secrets are `SecretStr` so they don't show up in logs or reprs. `MEDIA_SOURCE` is a `Literal`, so a typo in the provenance tag fails at startup instead of polluting the ledger.

## Bedrock model ID default
`BEDROCK_MODEL_ID` defaults to Amazon Nova Pro (`us.amazon.nova-pro-v1:0`) with `BEDROCK_STRUCTURED_OUTPUT=false`. This is provisional. The original default, Claude Sonnet 5.5, is not available to the project's AWS account, and the newest Claude Sonnet the account can call is 4.6. In the 2026-10-15 smoke test (one call per scene, 3 Playground frames each, N=2 scenes), Nova Pro and Sonnet 4.6 both returned the correct package count (1 and 0). Nova cost $0.0056 per call and Sonnet $0.0151 (US East, `us.` cross-region rates from the AWS price list, published 2026-10-12/14). Nova was also about twice as fast. Two scenes is not an accuracy result, so `BEDROCK_COMPARISON_MODEL_ID` (Sonnet 4.6, structured output on) is run by the extraction eval next to the default on every report, and the default is chosen from those numbers. Models are invoked through cross-region inference profiles; Sonnet's `us.` geo profile costs 10% more than its global profile.

## httpx2 for the test client
Starlette 1.8's `TestClient` tries to import `httpx2` first. If only `httpx` is installed, it falls back to `httpx` and warns: "Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead." We add `httpx2` as a dev-only dependency rather than silencing the warning. The runtime dependency is still `httpx`.

Checked on 2026-10-14 before adopting it:
- **Maintainer:** Pydantic Services Inc. (engineering@pydantic.dev). It is not the encode/httpx team. The PyPI author field credits Tom Christie, the original HTTPX author.
- **Repo:** github.com/pydantic/httpx2. The README describes it as a continuation of HTTPX, with Pydantic taking over maintenance because HTTPX has seen limited activity.
- **Releases:** 18 since 2026-05-11. The latest checked was 2.13.1 (2026-09-23).
- **Downloads:** about 234M in the last month (pypistats), against about 619M for `httpx`.
- **Recommended by Starlette:** the deprecation warning above, in `starlette/testclient.py`, names it directly.

Revisit if maintenance stalls or Starlette changes its recommendation.

## Ring device IDs are not configured
The capture worker discovers devices from `GET /v1/devices` at runtime. Hardcoding an ID would break when switching between Playground devices and a real camera.

## Reconciliation as an assignment problem with "unpaired" slots
`app/reconcile/matcher.py` pairs detected deliveries (rows) with expected deliveries (columns) using `scipy.optimize.linear_sum_assignment`. A greedy matcher (best pair first) can make an early choice that ruins a later one, for example two UPS orders on the same Wednesday. The assignment solver minimizes total cost over all pairs together.

Plain `linear_sum_assignment` on a rectangular matrix always pairs `min(rows, cols)` items, and thresholding afterwards can then discard pairs the solver was forced into. It can also give up a perfect pair to make room for two mediocre ones that both fail the threshold (`test_solver_does_not_sacrifice_a_good_pair`). So the matrix is padded with one dummy column per delivery and one dummy row per expected delivery, each costing `max_cost / 2`. A real pair beats leaving both items unpaired exactly when its cost is below `max_cost`, and the solver considers that option for every item at once. `test_solver_matches_brute_force` checks it against exhaustive search on random small matrices.

## Cost terms and default weights
cost = carrier + time + count, in units of the acceptance threshold (`max_cost = 1.0`):
- **Carrier mismatch 0.5**, unknown 0.2. This is below the threshold on purpose: the vision model can misread a carrier, and a delivery that is on time with the right count should still match. A wrong carrier combined with being late or off on count tips it over.
- **Time 0.15 per hour outside the window**, infeasible beyond 8 h. A 2 h late truck still matches easily. One 6+ h late with a wrong carrier does not.
- **Count 0.5 × relative difference**, capped. Relative, so 1 short of 2 costs more than 1 short of 10. Short and over deliveries must still pair (they get the `short`/`over` statuses), so a full-count miss costs at most half the budget.

The defaults were set from these rules of thumb and checked on simulator seed 7. Reported numbers use seeds 101–105, so the weights were never tuned on the evaluation data. All weights are env-configurable (`RECONCILE_*`).

## Idempotent reconciliation = full recompute
`reconcile()` is a pure function of (deliveries, expected, now, config). It does not update previous decisions incrementally. Inputs are deduped by ID and sorted before the matrix is built, so replays, duplicates and input order cannot change the output. Row IDs are `uuid5(namespace, expected_id|delivery_id)`, so storage can upsert or replace the day's set without creating duplicates. A pair's status can only change when the inputs change, for example when a new order lets an "unexpected" delivery match.

Unpaired expected deliveries before `window_end + grace` are returned as `pending_expected_ids` with no row. `next_missing_deadline()` tells the timer when to run next.

Known limitation: matching is one-to-one, so an order split across two drops shows as `short` plus `unexpected`.

## Reconciliation eval is synthetic
`eval/simulate.py` generates the café scenario with injected missing/short/over/late/early/carrier-mislabel/extra deliveries. `eval/reconciliation.py` scores a predicted row as correct only if (expected_id, delivery_id, status) all match the truth. The simulator and matcher share assumptions, and package-count misreads are not simulated, so these numbers describe the matcher on the problems we injected. They are not field accuracy. The results file says so.

## Bedrock auth: API key or SigV4
`AWS_BEARER_TOKEN_BEDROCK` (a Bedrock API key) is loaded into `Settings` as a `SecretStr`. botocore reads bearer tokens only from that environment variable, so `make_bedrock_client()` exports it before creating the client. If the variable is already set in the real environment, that value wins, matching pydantic-settings precedence. Without a key, boto3 uses the normal credential chain and signs with SigV4. Never enable botocore debug logging: it logs request headers, including the bearer token.

## Extraction: Converse structured output + Pydantic
The JSON schema goes in Converse `outputConfig.textFormat` (constrained decoding), and Pydantic validates the result again. The schema is hand-written because constrained decoding supports only part of JSON Schema (no numeric bounds), so ranges such as `0 <= confidence <= 1` are enforced by Pydantic. On a validation failure or truncation, we retry once and show the model the error. `reasoning` is the first property so the model writes its observations before the answer fields. Temperature is 0 for repeatable evals. Prompts are versioned in `app/extract/prompts.py` and never edited in place.

## Structured output is a switch, not an assumption

`BedrockExtractor` normally constrains the model with Converse structured output
(`outputConfig` JSON schema). Amazon Nova rejects that field (`ValidationException: This model
doesn't support the outputConfig field`), and on 2026-10-15 Nova was the only vision family this
account could call (Anthropic models were blocked pending the use case form). So
`BEDROCK_STRUCTURED_OUTPUT=false` drops `outputConfig`, appends the same JSON schema to the
system prompt, and strips Markdown fences; Pydantic validation and the single retry still apply.

The schema instruction is appended at runtime instead of becoming a new prompt version, so `v1`
stays byte-identical for models that support structured output. Every `ExtractionOutcome`
records `structured_output`, and eval results from the two modes must be reported separately:
the effective prompt differs, and only one mode has a decoder-level schema guarantee.

## Ingest: the events table is the queue

`ring_events` stores every event and doubles as the work queue (`status`, `attempts`, `claimed_at`). The webhook handler verifies the signature, inserts the row with `INSERT ... ON CONFLICT DO NOTHING`, and returns 200. The ack happens after the commit, so an event Ring got a 200 for survives a crash. Dedupe is enforced by the database (unique `request_id` and unique `ring_event_id`), so ten concurrent deliveries of one webhook still insert one row (`test_concurrent_duplicate_deliveries_insert_once`). An in-memory `asyncio.Queue` would be faster, but it would lose acked events on restart and could not dedupe across processes.

Workers claim rows with `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`. A claim is a lease: a row left in `processing` longer than `QUEUE_LEASE_S` is assumed orphaned and claimed again, and a late result from the old worker is discarded (guarded on `claimed_at`). Workers wake through an in-process `asyncio.Event` on insert and also poll every second, which covers rows written by another process. Failures retry up to `QUEUE_MAX_ATTEMPTS` and then stay as `failed` with the error.

Irrelevant events (`device_online`, unknown types) are stored as `ignored` and acked with 200. Unknown event types must not get a 4xx, because Ring may add types and would retry them.

## Webhooks need the signing key, and blank secrets are unset

With `RING_WEBHOOK_SECRET` unset, the webhook returns 503 rather than accepting unsigned events. An empty `KEY=` in `.env` counts as unset; otherwise an empty key would make anyone able to sign. Verification uses `hmac.compare_digest` on the raw request bytes.

## The Playground doesn't send webhooks: poller + manual trigger

The Ring docs (checked with the knowledge MCP server on 2026-10-16) only describe a webhook URL registered on an app ("Webhook URL is configured during partner onboarding"), and we found no place to give the Playground one. So:
- `HistoryPoller` (`RING_POLL_INTERVAL_S > 0`) reads Event History for each camera with `event_types=motion.human,ding` and enqueues new items under `request_id = poll:<event id>`. `on_demand` is left out on purpose, because our own WHEP capture creates `on_demand` events and polling them would loop. Items older than startup minus `CAPTURE_MAX_EVENT_AGE_S` are skipped, since they can't be captured live.
- `POST /v1/trigger` (bearer `ADMIN_TOKEN`, disabled when unset) enqueues a `manual_trigger` event for a device. It takes an optional `idempotency_key`, so a retried demo click doesn't start a second capture.

## Schema without migrations (for now)

`init_db()` runs `metadata.create_all` at startup. Nothing is deployed yet, so there's no data to migrate. Alembic comes in with the first change to an existing table.

## Database tests use a real Postgres

The queue's correctness rests on `SKIP LOCKED`, unique constraints and `ON CONFLICT`, which SQLite or a fake wouldn't show. CI runs a Postgres service with `REQUIRE_DB=1`. Locally the tests skip if no database is reachable at `TEST_DATABASE_URL`.

## Frame selection: drop frame 0, spread, score, top K

`app/capture/selection.py` decides which frames reach Bedrock:
1. **Drop the session's first decoded frame.** In the one Playground capture checked (60 frames), frame 0 had the lowest Laplacian variance of all of them (753 against a median of 1183) and looks smeared. That fits a WebRTC encoder ramping up its bitrate. One frame costs nothing to drop; this is a guard, not a measured rule.
2. **Spread** `CAPTURE_SAMPLE_FRAMES` (10) evenly over the rest of the clip, the `--spread` logic from the spike. Playground clips ran anywhere from 1.5 s to 26 s, so position-based sampling adapts where a fixed interval would not.
3. **Score** = `w · sharpness + (1 − w) · change`, each min-max normalised within the clip (only the ranking matters). Sharpness is the variance of the Laplacian. Change is the mean absolute difference from an empty-scene reference, measured on a 64 px wide low-pass copy: at full resolution, motion blur itself counts as change, and a blurry frame could win on that alone. The reference is `CAPTURE_REFERENCE_DIR/<device_id>.jpg` when present, otherwise the per-pixel median of the samples (in a short clip the background is what most frames share).
4. Send the top `CAPTURE_SELECT_FRAMES` (3), in time order.

Every delivery records `frames_sampled` and `frames_sent`, which is what "LLM image calls saved by frame selection" is computed from.

Known limitation: with the median fallback, an object present in every frame (the Playground's static box) contributes no change, so sharpness decides. That's acceptable because any of those frames shows the box.

## Capture safety rules

- Webhook and poll events carry a device ID, and it must match exactly. Falling back to "the only camera" could film a different device than the one that saw the event. Manual triggers may use a name or no device at all, and only cameras are considered.
- Offline devices are refused before opening WHEP. Ring answers with 201 and then sends no media.
- One WHEP session per device at a time (per-device `asyncio.Lock`).
- The WHEP session is always closed (`DELETE` on the verbatim `Location`) in a `finally`.

## Pipeline idempotency, layer by layer

Replaying an event must never create a second delivery or reconciliation (`tests/test_idempotency.py` replays every webhook 3× in random order, interleaved with Event History polls of the same events):
1. **Event:** unique `request_id` / `ring_event_id` + `ON CONFLICT DO NOTHING`, so a replay is acked as `duplicate` and never queued.
2. **Delivery:** `delivery.id = uuid5(event.id)` and `deliveries.event_id` is unique. If a worker dies after storing the delivery, the retried job finds it and skips capture and the model call (`test_reprocessing_after_crash_reuses_stored_delivery`).
3. **Model call:** results are cached by `sha256(model, prompt version, output mode, frames)`.
4. **Reconciliation:** one transaction per business day takes `pg_advisory_xact_lock`, deletes the day's rows and inserts the deterministic set. Without the lock, concurrent runs interleave and leave duplicates; the test fails when the lock is removed.

I checked each guarantee by breaking it and confirming its test fails.

## Pipeline behaviour worth knowing

- **Stale events are skipped, not captured.** Live view only shows the delivery while it's happening. An event older than `CAPTURE_MAX_EVENT_AGE_S` (120 s) when a worker claims it is marked `skipped`. Ring's own retries, a backlog after downtime, or a slow queue all end up here. Recorded clips (`media/video/download`) would be the way to process old events; not built yet.
- **Zero packages = no delivery row.** A person at the door with nothing left is recorded on the event (`outcome`), not in the ledger, so it doesn't show up as an `unexpected` delivery.
- **`detected_at` is Ring's detection time**, not our processing time, so reconciliation compares against the delivery window using when the driver was actually there.
- **Auth failures are final.** An expired Playground token fails the event once, with the reason, instead of burning three retries in milliseconds. Other errors retry immediately, up to `QUEUE_MAX_ATTEMPTS`; there's no backoff, because a capture retried later would be stale anyway.
- **Reconciliation batches are business days** (`BUSINESS_TIMEZONE`). An expected delivery belongs to the day its window starts, a delivery to the day it was detected. Limitation: a delivery just after midnight for a window ending just before it lands in a different batch (`unexpected` + `missing`). The timer (`RECONCILE_INTERVAL_S`) re-runs today and yesterday so overdue windows turn `missing`.
- **Latency stamps** (`deliveries.latency_ms`): `ring_to_received`, `queue_wait`, `device_resolve`, `whep_setup`, `capture`, `select`, `extract`, `save_frames`, `ledger_insert`, `reconcile`, plus the end-to-end spans `event_to_ledger` and `received_to_ledger`. `bench/latency.py` reports nearest-rank p50/p95 with N per stage.

## Expected deliveries: a minimal admin endpoint for now

`POST /v1/expected` (bearer `ADMIN_TOKEN`) stores one expected delivery and reconciles its business day right away. A delivery that already arrived gets matched without waiting for the timer, and the response says where the order stands. The client may supply `id`. The same id with the same fields returns the stored row (`200`, `duplicate`); the same id with different fields is a `409`, so a retried request can never silently rewrite an order. `carrier` uses the extraction enum, so it's compared in the same vocabulary the model answers in. Leaving it out costs the matcher's "unknown carrier" amount.

`scripts/seed_expected.py` seeds the live Playground test through this endpoint. IDs are `uuid5(date, supplier)` and the windows are fixed clock times on that date, so re-running it is a no-op.

## Event History filter is verified, not trusted

The Playground returned the same `on_demand` items for every `event_types` value, including a nonsense one (FRICTION_LOG.md, 2026-10-16). The poller therefore only infers a subtype from the filter when every returned item has one of the requested types. Otherwise motion items get no subtype, and the default `motion_detected.human` rule won't capture them. The poller also no longer stores history items that fail the relevance rules. Unlike webhooks, nothing was pushed to us, and our own live views would otherwise add a row each.

## Capture time is dominated by the timeout on the Playground

In the 2026-10-16 live run (N=2), the Package stream never stalled, so each capture ran the full `CAPTURE_TIMEOUT_S` (25 s) on top of ~5.5 s of WHEP setup: 31 s of a 33 s event-to-ledger time. Setup plus timeout (~30.5 s) is also right at the 30 s session limit Ring documents for battery cameras. Lowering the timeout is the obvious lever. It's not changed yet, because shorter clips trade latency against the chance of catching the driver mid-drop, which a static Playground scene can't measure.

## "Scene settled" early exit

The capture now stops when nothing has moved for `CAPTURE_SETTLE_S` (3 s), but never before `CAPTURE_MIN_S` (4 s). `CAPTURE_TIMEOUT_S` (25 s) stays as the hard cap. Stall detection (RTP stops) is unchanged. `deliveries.usage.capture_end` records which rule ended each clip (`settled`, `stalled` or `timeout`).

- **Signal:** every 0.5 s, the newest frame against the previous checked frame (not the previous decoded frame). That makes it independent of frame rate and replayable on the spread-sampled clips. The change is the **most-changed 4×4 cell** of the 64 px low-pass copy, with the watermark corners masked. The frame mean doesn't work: the far-away car in the Vehicle clip moves it by 0.6–0.75, the same as encoder noise on the static Package clip (up to 0.62). Max-cell separates them: 26–60 while the car moves against 0.5–1.9 static.
- **Threshold 3.0**, tuned and validated on two Ring Playground clips only (`tests/fixtures/capture/clip_*`: Package, static; Vehicle, moving). No real delivery video exists for this project; we can't film deliveries. Replay: Package settles at 4.39 s of a 26.2 s clip; Vehicle never settles (change ≥ 8.6 through 8.1 s; the car is gone by about 6.2 s, but something keeps changing). `tests/test_settle.py` pins both.
- **Risk:** a driver who stands still for 3 s mid-drop would end the clip early. Frame selection still sees everything captured up to that point. This is unmeasured: validating the threshold on real delivery video is listed as future work in the README.

## Dashboard: the token stays on the dashboard server

The Next.js dashboard (`web/`) never calls the API from the browser. Pages are server components that fetch with `DOCKHAND_ADMIN_TOKEN` (`web/lib/api.ts`, marked `server-only` so a client import fails the build). Writes (add an order, simulate a delivery) are server actions, and evidence images go through `web/app/media/[...path]/route.ts`. That route forwards only `deliveries/<uuid>/snapshot|clip|frames/<n>`, so it can't be used to reach other endpoints with the token. The alternative, a token in the browser or a CORS-enabled API, would expose the admin token to anyone who opens the page.

Known limitation: the dashboard itself has no login. Anyone who can reach port 3010 can read the ledger, add orders and trigger a capture. That's fine on localhost. A deployment needs auth in front of it (the deploy milestone).

The read endpoints (`app/ledger/api.py`) also require the admin token. The ledger holds supplier orders and camera stills, and the dashboard is the only reader for now. Queries live in `app/ledger/queries.py`, separate from the routes, so the MCP server can reuse them.

## Dashboard: what it shows and how

- **Live updates by polling, not push.** The Today page calls `router.refresh()` every 3 s while the tab is visible, and once more when it becomes visible again. That re-runs the server components against the API. A WebSocket or SSE channel would cut the delay, but the pipeline itself takes 12–15 s (Playground, N=2), so a 3 s poll is not the bottleneck. It also needs no extra moving parts.
- **In-flight events are `pending` rows.** `GET /v1/activity` returns the day's pipeline events with their queue status. Events still `pending` or `processing` are shown in the ledger before a delivery row exists, so a demo shows the event arriving, not just the result. Finished events without a delivery show their outcome (no packages, stale, Ring auth).
- **Times are in the business timezone,** from the API's `timezone` field, not the viewer's. The order form's wall-clock times are converted to offset timestamps on the dashboard server (`web/lib/time.ts`, `zonedToIso`), because the API rejects naive timestamps. Ambiguous and skipped DST times follow Temporal's "compatible" rule: in the fall-back hour, the earlier instant; in the spring-forward gap, the later wall time. Reconciliation reasons are written in the same timezone (`reconcile(..., tz=...)`). Before, they were UTC and disagreed with every other time on the page.
- **The order form sends a client-chosen ID** (`POST /v1/expected` already accepts one). After a failure the same ID is reused, so a retry after a timeout can't create the order twice. After a success a new one is issued.
- **Metrics are copied, not computed.** `GET /v1/metrics` (`app/ledger/metrics.py`) reads the committed results files and returns each number with its N, file and commit. A missing file gives `null`, never a default. A test checks every value against the files.
- **Dispute summary** (`web/lib/dispute.ts`) is plain text built from the stored facts (ordered, received, time, PO), worded per status. It asks for a specific remedy: send the rest or credit (short), confirm billing (over), status (missing), which order (unexpected). It never claims more than the camera saw.

## Playground token: re-read on every call

`PlaygroundTokenProvider` reads `RING_ACCESS_TOKEN` again for each Ring call: an exported environment variable first, then `.env`, then the value loaded at startup (the same precedence as Settings). Playground tokens last 30 minutes (the token's own `exp − iat`) and can't be refreshed by the app. Requiring a restart for each one would interrupt every demo take. Parsing a small file per call costs microseconds; a capture takes seconds.

Two guards come with it. After a 401, that exact token is marked dead and refused until a different one appears, so a rejected token isn't retried. And the JWT `exp` claim is read (unverified; Ring still checks the signature), so an expired token fails locally with "expired at HH:MM UTC" instead of a round trip ending in an empty 401. The OAuth provider for real devices will replace this behind the same `TokenProvider` interface.

## Reasons read like a receiving clerk wrote them

Reconciliation reasons give times as "11:00 AM" in `BUSINESS_TIMEZONE`, without the date. Every row in a batch belongs to the same business day, which the page already shows. A carrier the model reported as `Unknown` is written "carrier not visible" in reasons, and the dashboard shows **Not visible** with a tooltip. `Unknown` stays in the database and the API, because it's the model's enum value and the eval compares against it.

## Demo reset: an endpoint behind two switches

`POST /v1/admin/reset-day` (called by `scripts/reset_demo_day.py`) deletes a day's Playground deliveries and evidence files, the orders whose window starts that day, their reconciliations, and the day's pipeline events. It needs the admin token and `ALLOW_DEMO_RESET=true`, which defaults to off, so a deployed API can't be wiped with the admin token alone. It goes through the API rather than straight to Postgres, so it works against a deployment without database credentials, and it reuses the reconciliation advisory lock.

Safety rules: dry run unless asked; `ring_device` deliveries and their events are never deleted (the remaining day is reconciled again); refused while an event of the day is queued or processing, because the running capture would then write to a deleted event; media folders are removed only when they are a direct child of `MEDIA_DIR` named by a deleted delivery's ID.

## Simulated demo history: tagged, reconciled apart, never measured

`scripts/seed_demo_history.py` fills the past 21 days so the Suppliers page and day navigation have something to show. The rule is that simulated data can never be mistaken for, or mixed into, real data:

- **Tagged at the row.** Deliveries get a third `source` value, `simulated`; orders get a `simulated` boolean. A flag on the row, not a separate table, so every existing query, page and the reset see it and can filter on it.
- **Reconciled apart.** `reconcile_day` runs the matcher twice per day, once for real rows and once for simulated rows. Otherwise a simulated Sysco order on Oct 16 could have been paired with that day's real Playground delivery.
- **Rerun = replace.** One transaction deletes every simulated row and inserts the new set. Each date gets its own seed (from `--seed` and the date), so running the script on a later day regenerates overlapping dates identically instead of shuffling history.
- **Kept on reset.** The demo reset clears a day's live rows only.
- **Never measured.** Metrics come from committed results files; the latency benchmark (the one script that reads the ledger) filters to `ring_playground`/`ring_device` whatever its arguments. `tests/test_simulated_isolation.py` enforces this.
- **Schema change without migrations.** `create_all` doesn't alter existing tables, so `init_db` replays a short list of idempotent DDL statements (`UPGRADES` in `app/db/session.py`). That holds until the first deploy brings real migrations.

The seed (2027) was chosen by looking at a few seeds for a mix that contains every kind of problem. That's acceptable for demo data; it would not be for an eval.

## Supplier reliability: definitions

On-time % = orders delivered by the window end / orders due, so a missed order counts against it. Short rate = short deliveries / delivered orders. Orders still inside window plus grace are left out of every rate. "Worst" = the highest share of due orders with any problem (missing, short or late), ties broken by boxes short, then by missed deliveries. If no supplier had a problem, none is highlighted. Every rate shows k/n, because "100%" from 2 orders and from 20 orders are not the same thing.
