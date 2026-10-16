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
