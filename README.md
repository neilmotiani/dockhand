# Dockhand

Dockhand turns a Ring camera at a business's back door into an automatic receiving clerk. It logs every delivery (carrier, package count, time, video proof) and reconciles it against what the business expected, flagging short, missing and unexpected deliveries.

Work in progress.

## Development

Requires Python 3.12, [uv](https://docs.astral.sh/uv/) and Docker.

```sh
cp .env.example .env        # fill in tokens
uv sync                     # install dependencies
uv run pytest               # tests
uv run ruff check .         # lint
docker compose up --build   # Postgres (host port 55432) + API on http://localhost:8000
```

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
  curl -X POST localhost:8000/v1/trigger -H "Authorization: Bearer $ADMIN_TOKEN" \
       -H 'Content-Type: application/json' -d '{"device": "Playground", "idempotency_key": "demo-1"}'
  ```
- Expected deliveries: `POST /v1/expected` (admin token; send your own `id` to make retries safe). For a live Playground test, `uv run python -m scripts.seed_expected` creates today's two demo orders: Sysco expecting 1 box, US Foods expecting 3. Then trigger twice. The two one-box Playground deliveries should come out `matched` and `short`; in the 2026-10-16 live run they did (N=2, results below). A dashboard editor replaces this later.
- Events older than `CAPTURE_MAX_EVENT_AGE_S` (120 s) when a worker picks them up are skipped, since live view can no longer show the delivery.
- Every delivery stores per-stage timings in `deliveries.latency_ms`. `uv run python -m bench.latency` reports nearest-rank p50/p95 per stage with N.
- Live Playground run, 2026-10-16, manual trigger, Package scene, Nova Pro (`bench/results/latency_playground_manual.json`): **N = 2**, so these are two observations, not a distribution. Event → ledger took 33.2 s and 33.5 s. Of that, WHEP capture took 31.0 s each time: about 5.5 s of session setup plus the full 25 s `CAPTURE_TIMEOUT_S`, because the Package stream never stalled. Bedrock extraction took 2.1–2.2 s (3 frames, 6,733 input tokens).
- Tests need Postgres: `docker compose up -d db` (host port 55432, which `DATABASE_URL` and the test default point at), or point `TEST_DATABASE_URL` at another one. Without it, the database tests are skipped locally. CI runs them against a Postgres service.

### Future work

- **Clip download for late events.** Events too old for live view (Ring retries, a backlog after downtime) are skipped today. They could be processed from the recorded clip (`POST /v1/devices/{device_id}/media/video/download`) instead.

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
- **Small N.** The real-footage rows are small (28 and 11), so the intervals are wide and the two models overlap on every count row. These numbers do not show either model to be more accurate.
- **Synthetic porch composites were rebuilt after the first run.** In the first build, the box already on the step in the Playground footage had no carrier label while the pasted boxes did, so each image mixed two deliveries. Both models counted only the labelled boxes (porch exact match 3/20 for both). Every Playground "Package" frame shows that box, so an empty porch was not available. Amendment 2 in `eval/SELECTION.md`, committed before the re-run, gives the existing box the same printed label in the 16 porch composites that have a carrier. Truth labels, layouts and the other 20 composites are unchanged. Only the 16 changed images were re-run (32 calls, $0.17). Because the fix came after seeing results, the porch row is a second build, not a held-out measurement. First-run numbers are in commit `c25d058`.
- **What is left on porch.** In the 16 rebuilt porch composites, exact match is 5/16 for Nova Pro and 11/16 for Sonnet 4.6; every miss is one short. The models' reasoning does not say which box they left out. The 4 porch composites with no carrier (all boxes unlabelled) were not changed: Nova Pro got 1/4 and Sonnet 4.6 1/4 exact.
- **Ring Playground items** were also used during development (N=2), so they are a smoke test, not a held-out result.
- **Cost** uses AWS us-east-1 on-demand prices from the public AWS price list (`eval/prices.json`). Total Bedrock spend across both runs was $0.90 (ledger in `data/eval/spend.jsonl`, not committed).

### Data attribution

- *Porch Piracy Dataset* by gluwxyws, Roboflow Universe, version 8, licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): https://universe.roboflow.com/gluwxyws/porch-piracy. 28 images are used unmodified as eval items.
- *Package Detection Dataset* by MPJ, Roboflow Universe, version 1, licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): https://universe.roboflow.com/mpj/package-detection-f03pa. Box photos were cropped, resized and given made-up shipping labels to build the synthetic composites.
- *Packages Dataset* by Roboflow Community, Roboflow Universe, version 6, Public Domain: https://universe.roboflow.com/joseph-nelson/packages-g0ton.
- Ring Developer Playground live-view footage (simulated device), captured with `scripts/ring_spike.py`. It is also the background of the synthetic composites.

Dataset images are not committed. `eval/build_roboflow.py` downloads the pinned versions (it needs `ROBOFLOW_API_KEY`) and checks them against the content hashes in `eval/labels/sources.json`.
