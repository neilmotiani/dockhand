# Dockhand

Dockhand turns a Ring camera at a business's back door into an automatic receiving clerk. It logs every delivery (carrier, package count, time, video proof) and reconciles it against what the business expected, flagging short, missing and unexpected deliveries.

Work in progress.

## Development

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js 24+ and Docker.

```sh
cp .env.example .env        # fill in tokens; set ADMIN_TOKEN
uv sync                     # install dependencies
uv run pytest               # tests
uv run ruff check .         # lint
docker compose up --build   # Postgres (host port 55432), API on :8010, MCP server on :8020
```

Run the API and dashboard locally (two terminals):

```sh
docker compose up -d db
uv run uvicorn --factory app.main:create_app --port 8010

cd web
cp .env.example .env.local  # DOCKHAND_ADMIN_TOKEN = the API's ADMIN_TOKEN
npm install
npm run dev                 # http://localhost:3010
```

## Dashboard

`web/` is a Next.js + TypeScript app on port 3010 with six pages (the Assistant is described under [MCP server and voice assistant](#mcp-server-and-voice-assistant-alexa-path)):

- **Today:** the live delivery ledger (snapshot, time, carrier, box count, status, and the reason for it). It refreshes every 3 s. Events still being captured appear as `pending` rows, and the activity list says why an event produced no delivery (no packages, stale, Ring auth).
- **Expected:** add an order (supplier, carrier, box count, time window, PO) and see today's orders with their status. Times are entered in `BUSINESS_TIMEZONE` (an IANA name such as `America/New_York`; it also sets the business day and every time shown, reasons included).
- **Discrepancies:** each short, over, missing or unexpected item with its reason, the snapshot and the other frames the model read (or the clip, when one is stored), and a **Copy dispute summary** button that produces a short email to the supplier. Set `DOCKHAND_BUSINESS_NAME` to sign it.
- **Suppliers:** each supplier's on-time %, short-delivery rate, late and missed deliveries and total boxes short over the last 30 days, with k/n next to every rate. Sortable; the supplier with the highest share of problem orders (missing, short or late) is highlighted. A banner says when the figures include simulated history.
- **How it works:** the pipeline at a glance, and the accuracy and latency numbers read from `eval/results/` and `bench/results/` through `GET /v1/metrics`, each with its N, file and commit.

**Simulate delivery** in the header calls `POST /v1/trigger`, so a demo can show an event flowing in live.

Snapshots open full size on click, with ← / → through every frame the model read. When no uniform, vehicle, logo or label was readable, the carrier shows as **Not visible** (the model is told not to guess). A tooltip explains why.

Recording takes:

```sh
uv run python -m scripts.reset_demo_day          # dry run: what would be deleted today
uv run python -m scripts.reset_demo_day --yes    # clear today's Playground deliveries, orders, events
uv run python -m scripts.seed_expected           # fresh demo orders
```

The reset needs `ADMIN_TOKEN` and `ALLOW_DEMO_RESET=true` on the API (off by default; keep it off in deployments). It never deletes deliveries from a real Ring device or simulated history, and it refuses while a capture is still running.

### Simulated demo history

A dashboard with one day of Playground deliveries has nothing to compare against, so the past 21 days, and today up to the current time, can be filled with simulated deliveries:

```sh
uv run python -m scripts.seed_demo_history             # safe to rerun: replaces the simulated rows only
uv run python -m scripts.seed_demo_history --no-today  # past days only
```

Today gets the deliveries "seen" before now and the orders whose window has started (or whose delivery already arrived), so the Today page looks like a business day in progress. Orders still inside their window show as pending. Rerun later in the day to add what has happened since. Weekends are quiet: on Saturday only the bakery delivers.

It uses `eval/simulate.py` with five suppliers (Sysco, US Foods, Daybreak Bakery, Ridgeline Roasters, Amazon Business) on realistic schedules. Most orders match; a handful are short, late or missing, plus the odd unexpected parcel, and US Foods is set up to be the least reliable. Every row is stored as `source = simulated` (orders as `simulated = true`), never as Ring footage. The dashboard badges each one **Simulated**, and Today, Discrepancies and Suppliers have a **Show simulated data** toggle (on by default). Simulated rows are reconciled in their own batch, so they never pair with a real delivery or order, and their dispute summaries say they are not real.

Snapshots for simulated deliveries reuse the synthetic composites from the extraction eval (attribution below), picked by carrier and nearest box count. They illustrate the row: the box count in the picture need not match the simulated count.

Simulated history feeds no metric. The README numbers and the How it works page come only from the committed results files, `bench/latency.py` always skips `simulated` rows, and `tests/test_simulated_isolation.py` checks both.

Ring Playground tokens last 30 minutes (from the token's own `iat`/`exp`). Paste a new one into `RING_ACCESS_TOKEN` in `.env`. The API re-reads it on every Ring call, so there's no restart.

The admin token stays on the dashboard's server. Pages fetch from the API in server components, writes go through server actions, and images go through a proxy route that forwards only evidence paths. Read endpoints, all admin-only: `GET /v1/deliveries`, `/v1/expected`, `/v1/discrepancies`, `/v1/activity` (each takes `?date=YYYY-MM-DD`, default today in `BUSINESS_TIMEZONE`; the first three also take `?include_simulated=false`), `/v1/suppliers?days=30`, `/v1/metrics`, and `/v1/deliveries/{id}/snapshot|frames/{rank}|clip`.

For screen recordings, `npm run start:demo` (in `web/`) serves a production build on port 3011 without the Next.js dev indicator. It builds into `web/.next-demo/`, so it can run next to `npm run dev` without disturbing it.

Checks: `npm run lint`, `npm run typecheck`, `npm test`, `npm run format:check` (all run in CI).

## MCP server and voice assistant (Alexa+ path)

`mcp_server/` exposes the ledger as MCP tools over Streamable HTTP (spec 2025-11-25, official Python SDK `mcp` 2.x) at `http://localhost:8020/mcp`:

| Tool | What it answers |
|---|---|
| `get_todays_deliveries(date?)` | The day's deliveries with status, plus orders still pending or missing |
| `check_delivery(supplier \| reference, date?)` | Did it come? Status, time, box count, snapshot. Misheard names ("Cisco") resolve to known suppliers |
| `list_discrepancies(start_date?, end_date?)` | Short, over, missing and unexpected deliveries with reasons (up to 31 days) |
| `add_expected_delivery(supplier, count, date, window_start, window_end, carrier?, reference?)` | Adds an order; repeating the call adds it once |
| `get_supplier_scorecard(supplier?, days?)` | On time, short, boxes short and missed per supplier; who shorts you the most |

Every result has a `summary` written to be spoken, `cards` for a screen (status, facts and a signed snapshot URL that expires after an hour), typed rows, and `includes_simulated` when demo history is in the answer. Requests need `Authorization: Bearer $MCP_TOKEN`; the server won't start without one.

```sh
# MCP_TOKEN=... in .env (any long random string), then:
uv run uvicorn --factory mcp_server.server:create_app --port 8020
```

The **Assistant** page (`/assistant`) simulates an Alexa+ device. Hold the mic button (or type), and `POST /v1/assistant/turn` runs a Bedrock Converse tool-use loop (`BEDROCK_MODEL_ID` by default) that calls the MCP server as an ordinary MCP client. The reply is spoken with the browser's speech synthesis, with cards and snapshots below it. Adding an order always stops for **Confirm** or a spoken "yes". A collapsible panel lists every `tools/list` and `tools/call` with arguments, timing and the raw `structuredContent`. Voice input needs a browser with the Web Speech API (Chrome, Edge, Safari); typing works everywhere.

Conformance: `tests/test_mcp_protocol.py` checks the wire behaviour against the spec text (handshake, 202 for notifications, `-32602` for unknown tools, `isError` for bad arguments, 401 with `WWW-Authenticate`, 403 on a bad Origin, 400 on a bad `MCP-Protocol-Version`, 405 for GET and DELETE). `uv run python -m scripts.mcp_conformance` runs the official suite (`@modelcontextprotocol/conformance` 0.1.16) against a live server. Last run (`bench/results/mcp_conformance.json`): 5 of 5 checks passed across the 4 scenarios that test a server's own surface (`server-initialize`, `ping`, `tools-list`, `dns-rebinding-protection`). The other scenarios call the suite's own fixture tools, which Dockhand doesn't define. Design notes are in `docs/decisions.md`.

## Ingest pipeline

```
Ring webhook ─┐                                   ┌─ WHEP clip → drop frame 0 → spread 10 → top 3
Event History ├─► ring_events (dedupe + queue) ─► worker ─┤  → Bedrock extract → deliveries
poll          │                                   └─ reconcile the business day → reconciliations
POST /v1/trigger ┘
```

- `POST /webhooks/ring` verifies `X-Signature`, dedupes on `meta.request_id` (and Ring's event ID), stores the event and acks. Processing happens in background workers.
- The Ring Developer Playground does not deliver webhooks (see `docs/decisions.md`). For Playground runs, set `RING_POLL_INTERVAL_S=5` to poll Event History, or start a run by hand:

  ```sh
  curl -X POST localhost:8010/v1/trigger -H "Authorization: Bearer $ADMIN_TOKEN" \
       -H 'Content-Type: application/json' -d '{"device": "Playground", "idempotency_key": "demo-1"}'
  ```
- Expected deliveries: `POST /v1/expected` (admin token; send your own `id` to make retries safe). For a live Playground test, `uv run python -m scripts.seed_expected` creates today's two demo orders: Sysco expecting 1 box, US Foods expecting 3. Then trigger twice. The two one-box Playground deliveries should come out `matched` and `short`; in the 2026-10-16 live run they did (N=2, results below). Orders can also be added from the dashboard's Expected page.
- Events older than `CAPTURE_MAX_EVENT_AGE_S` (120 s) when a worker picks them up are skipped, since live view can no longer show the delivery.
- Every delivery stores per-stage timings in `deliveries.latency_ms`. `uv run python -m bench.latency` reports nearest-rank p50/p95 per stage with N.
- Live Playground runs, 2026-10-16, manual trigger, Package scene, Nova Pro. **N = 2 per row**, so these are individual observations, not a distribution:

  | Capture rule | Results file | Event → ledger | WHEP capture (incl. ~5.5 s setup) | Why capture ended |
  |---|---|---|---|---|
  | Fixed 25 s timeout | `bench/results/latency_playground_manual.json` | 33.2 s, 33.5 s | 31.0 s, 31.0 s | timeout, inferred: not recorded yet then, but capture = setup + the 25 s cap |
  | Scene settled (3 s still, min 4 s) | `bench/results/latency_playground_manual_settle.json` | 12.5 s, 14.7 s | 10.5 s, 12.1 s | settled after 4.1 s and 4.4 s of video |

  Bedrock extraction took 1.9–2.3 s in all four runs. WHEP setup (about 5.5 s) is now the largest single stage. Both runs reconciled as seeded: one `matched`, one `short`. The rule is in `docs/decisions.md`. Its motion threshold was tuned and validated on two Ring Playground clips only (Package and Vehicle). It hasn't been tested on real delivery video.
- Tests need Postgres: `docker compose up -d db` (host port 55432, which `DATABASE_URL` and the test default point at), or point `TEST_DATABASE_URL` at another one. Without it, the database tests are skipped locally. CI runs them against a Postgres service.

### Future work

- **Clip download for late events.** Events too old for live view (Ring retries, a backlog after downtime) are skipped today. They could be processed from the recorded clip (`POST /v1/devices/{device_id}/media/video/download`) instead.
- **Validate the settle threshold on real delivery video.** `CAPTURE_SETTLE_THRESHOLD` (3.0) comes from the two Playground clips only. Real deliveries (a driver who pauses mid-drop, lighting changes, wind, other cameras) may need a different value or a different signal.

## Extraction eval (package count and carrier)

`uv run python -m eval.extraction` runs both configured Bedrock models on every labelled item and writes `eval/results/extraction.json`. Selection and labelling rules were committed before any model run (`eval/SELECTION.md`, plus amendment 1); amendment 2 rebuilt the porch composites after the first run (see below). Each source is reported separately and is never pooled. N is shown on every row, with 95% Wilson intervals. Run of 2026-10-16, prompt `v1`, temperature 0; Nova Pro with prompted JSON, Sonnet 4.6 with Converse structured output.

| Source | Footage | Model | N | Count exact match | False alarms on empty scenes | Carrier | p50 latency | $/call |
|---|---|---|---|---|---|---|---|---|
| Porch Piracy (Roboflow, CC BY 4.0) | real doorbell/security cameras | Nova Pro | 28 | 92.9% (26/28, CI 77–98%) | 5.0% (1/20) | not labelled | 1.05 s | $0.0017 |
| | | Sonnet 4.6 | 28 | 89.3% (25/28, CI 73–96%) | 5.0% (1/20) | not labelled | 2.55 s | $0.0058 |
| Packages (Roboflow, Public Domain) | real, hand-held phone photos | Nova Pro | 11 | 90.9% (10/11, CI 62–98%) | n/a | not labelled | 1.71 s | $0.0019 |
| | | Sonnet 4.6 | 11 | 100% (11/11, CI 74–100%) | n/a | not labelled | 3.77 s | $0.0087 |
| Synthetic composites | synthetic, on Ring Playground frames | Nova Pro | 36 | 41.7% (15/36, CI 27–58%) | n/a | 97.2% (35/36) | 1.65 s | $0.0023 |
| | | Sonnet 4.6 | 36 | 75.0% (27/36, CI 59–86%) | n/a | 100% (36/36) | 3.78 s | $0.0080 |
| ↳ porch backgrounds | | Nova Pro | 20 | 30.0% (6/20, CI 15–52%) | n/a | 95.0% (19/20) | | |
| | | Sonnet 4.6 | 20 | 60.0% (12/20, CI 39–78%) | n/a | 100% (20/20) | | |
| ↳ driveway backgrounds | | Nova Pro | 16 | 56.2% (9/16, CI 33–77%) | n/a | 100% (16/16) | | |
| | | Sonnet 4.6 | 16 | 93.8% (15/16, CI 72–99%) | n/a | 100% (16/16) | | |
| Ring Playground live view | Ring Playground | Nova Pro | 2 | 2/2 | 0/1 | not labelled | 2.28 s | $0.0056 |
| | | Sonnet 4.6 | 2 | 2/2 | 0/1 | not labelled | 4.71 s | $0.0153 |

How to read this:
- **Small N.** The real-footage rows are small (28, 11, and 2 for Ring Playground live view), so the intervals are wide and the two models overlap on every count row. These numbers do not show either model to be more accurate.
- **Synthetic porch composites were rebuilt after the first run.** In the first build, the box already on the step in the Playground footage had no carrier label while the pasted boxes did, so each image mixed two deliveries. Both models counted only the labelled boxes (porch exact match 3/20 for both). Every Playground "Package" frame shows that box, so an empty porch was not available. Amendment 2 in `eval/SELECTION.md`, committed before the re-run, gives the existing box the same printed label in the 16 porch composites that have a carrier. Truth labels, layouts and the other 20 composites are unchanged. Only the 16 changed images were re-run (32 calls, $0.17). Because the fix came after seeing results, the porch row is a second build, not a held-out measurement. First-run numbers are in commit `c25d058`.
- **What is left on porch.** In the 16 rebuilt porch composites, exact match is 5/16 for Nova Pro and 11/16 for Sonnet 4.6; every miss is one short. The models' reasoning does not say which box they left out. The 4 porch composites with no carrier (all boxes unlabelled) were not changed: Nova Pro got 1/4 and Sonnet 4.6 1/4 exact.
- **Ring Playground items** were also used during development (N=2), so they are a smoke test, not a held-out result.
- **Cost** uses AWS us-east-1 on-demand prices from the public AWS price list (`eval/prices.json`). Total Bedrock spend across both runs was $0.90 (ledger in `data/eval/spend.jsonl`, not committed).

### Data attribution

- *Porch Piracy Dataset* by gluwxyws, Roboflow Universe, version 8, licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): https://universe.roboflow.com/gluwxyws/porch-piracy. 28 images are used unmodified as eval items.
- *Package Detection Dataset* by MPJ, Roboflow Universe, version 1, licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): https://universe.roboflow.com/mpj/package-detection-f03pa. Box photos were cropped, resized and given made-up shipping labels to build the synthetic composites. The same composites are reused as illustrative snapshots for simulated demo history on the dashboard.
- *Packages Dataset* by Roboflow Community, Roboflow Universe, version 6, Public Domain: https://universe.roboflow.com/joseph-nelson/packages-g0ton.
- Ring Developer Playground live-view footage (simulated device), captured with `scripts/ring_spike.py`. It is also the background of the synthetic composites (and so of the simulated-history snapshots).

Dataset images are not committed. `eval/build_roboflow.py` downloads the pinned versions (it needs `ROBOFLOW_API_KEY`) and checks them against the content hashes in `eval/labels/sources.json`.

## License

Dockhand's code is released under the [MIT License](LICENSE). Copyright (c) 2026 Neil Motiani.

The eval datasets keep their own licenses (CC BY 4.0 and Public Domain, listed above). Their images aren't committed to this repo, and the MIT License doesn't cover them.
