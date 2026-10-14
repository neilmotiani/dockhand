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
