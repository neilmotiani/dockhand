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
docker compose up --build   # Postgres + API on http://localhost:8000
```

## Extraction eval (package count and carrier)

`uv run python -m eval.extraction` runs both configured Bedrock models on every labelled item and writes `eval/results/extraction.json`. Selection and labelling rules were committed before any model run (`eval/SELECTION.md`, plus amendment 1). Each source is reported separately and is never pooled. N is shown on every row, with 95% Wilson intervals. Run of 2026-10-16, prompt `v1`, temperature 0; Nova Pro with prompted JSON, Sonnet 4.6 with Converse structured output.

| Source | Footage | Model | N | Count exact match | False alarms on empty scenes | Carrier | p50 latency | $/call |
|---|---|---|---|---|---|---|---|---|
| Porch Piracy (Roboflow, CC BY 4.0) | real doorbell/security cameras | Nova Pro | 28 | 92.9% (26/28, CI 77–98%) | 5.0% (1/20) | not labelled | 1.05 s | $0.0017 |
| | | Sonnet 4.6 | 28 | 89.3% (25/28, CI 73–96%) | 5.0% (1/20) | not labelled | 2.55 s | $0.0058 |
| Packages (Roboflow, Public Domain) | real, hand-held phone photos | Nova Pro | 11 | 90.9% (10/11, CI 62–98%) | n/a | not labelled | 1.71 s | $0.0019 |
| | | Sonnet 4.6 | 11 | 100% (11/11, CI 74–100%) | n/a | not labelled | 3.77 s | $0.0087 |
| Synthetic composites | synthetic, on Ring Playground frames | Nova Pro | 36 | 33.3% (12/36, CI 20–50%) | n/a | 97.2% (35/36) | 1.48 s | $0.0023 |
| | | Sonnet 4.6 | 36 | 50.0% (18/36, CI 34–66%) | n/a | 100% (36/36) | 3.63 s | $0.0079 |
| Ring Playground live view | Ring Playground | Nova Pro | 2 | 2/2 | 0/1 | not labelled | 2.28 s | $0.0056 |
| | | Sonnet 4.6 | 2 | 2/2 | 0/1 | not labelled | 4.71 s | $0.0153 |

How to read this:
- **Small N.** The real-footage rows are small (28 and 11), so the intervals are wide and the two models overlap on every count row. These numbers do not show either model to be more accurate.
- **Synthetic counts, found after the run.** On the 20 porch composites, both models mostly come in exactly one short. Their reasoning shows they count the pasted, labelled boxes and leave out the unlabelled box that was already on the step in the Playground footage. On the 16 driveway composites, where no box was already present, exact match is 9/16 (Nova Pro) and 15/16 (Sonnet 4.6). The labels were not changed after the results. Details are in `post_hoc_diagnosis` in the results file.
- **Ring Playground items** were also used during development (N=2), so they are a smoke test, not a held-out result.
- **Cost** uses AWS us-east-1 on-demand prices from the public AWS price list (`eval/prices.json`). Total Bedrock spend for this run was $0.73.

### Data attribution

- *Porch Piracy Dataset* by gluwxyws, Roboflow Universe, version 8, licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): https://universe.roboflow.com/gluwxyws/porch-piracy. 28 images are used unmodified as eval items.
- *Package Detection Dataset* by MPJ, Roboflow Universe, version 1, licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): https://universe.roboflow.com/mpj/package-detection-f03pa. Box photos were cropped, resized and given made-up shipping labels to build the synthetic composites.
- *Packages Dataset* by Roboflow Community, Roboflow Universe, version 6, Public Domain: https://universe.roboflow.com/joseph-nelson/packages-g0ton.
- Ring Developer Playground live-view footage (simulated device), captured with `scripts/ring_spike.py`. It is also the background of the synthetic composites.

Dataset images are not committed. `eval/build_roboflow.py` downloads the pinned versions (it needs `ROBOFLOW_API_KEY`) and checks them against the content hashes in `eval/labels/sources.json`.
