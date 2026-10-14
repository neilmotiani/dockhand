# Design decisions

## uv + pyproject.toml
One tool for the virtualenv, dependency resolution and a committed lockfile (`uv.lock`). CI and the Docker image install with `--frozen`, so every environment gets the same versions.

## Configuration via pydantic-settings
All model IDs, prompt versions, thresholds and secrets come from environment variables (`.env` locally). Secrets are `SecretStr` so they don't show up in logs or reprs. `MEDIA_SOURCE` is a `Literal`, so a typo in the provenance tag fails at startup instead of polluting the ledger.

## Bedrock model ID default
`BEDROCK_MODEL_ID` defaults to the US cross-region inference profile for the current Sonnet (`us.anthropic.claude-sonnet-5-5`). Newer Anthropic models on Bedrock are invoked through inference profiles rather than base model IDs for on-demand throughput. This is a config value so the Sonnet vs. Haiku comparison in `eval/` can swap it per run.

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
