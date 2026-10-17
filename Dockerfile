FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv

WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
# Committed eval and bench results, served by GET /v1/metrics.
COPY eval/results ./eval/results
COPY bench/results ./bench/results

EXPOSE 8000
CMD ["uv", "run", "--no-dev", "uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
