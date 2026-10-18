"""Run the official MCP conformance suite (@modelcontextprotocol/conformance) against the
Dockhand MCP server and record the results in bench/results/mcp_conformance.json.

    uv run python -m scripts.mcp_conformance            # needs Node (npx) and the dev database

The server runs in this process under uvicorn on 127.0.0.1, exactly as deployed except for one
wrapper: the suite has no option to send an Authorization header, so `InjectBearer` adds the
MCP token to every request before the real app (bearer check included) sees it. The wire tests
in tests/test_mcp_protocol.py cover authentication itself.

The default run is the scenarios that test a server's own surface (APPLICABLE). The rest of
the suite calls its own fixture tools, prompts and resources (`test_simple_text`,
`json_schema_2020_12_tool`, ...): they test an SDK's reference server, and fail here only
because Dockhand doesn't define those names. `--scenario` runs any of them by hand.
"""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from app.config import get_settings
from mcp_server.server import create_app

SUITE = "@modelcontextprotocol/conformance@0.1.16"
SPEC_VERSION = "2025-11-25"
OUT = Path("bench/results/mcp_conformance.json")
# Scenarios that exercise a server's own surface: lifecycle, ping, tool listing, transport
# security. Everything else in the suite calls fixture tools or features Dockhand doesn't have.
APPLICABLE = ["server-initialize", "ping", "tools-list", "dns-rebinding-protection"]


class InjectBearer:
    def __init__(self, app, token: str) -> None:
        self.app, self.header = app, (b"authorization", f"Bearer {token}".encode())

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = [h for h in scope["headers"] if h[0] != b"authorization"]
            scope = {**scope, "headers": [*headers, self.header]}
        await self.app(scope, receive, send)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_suite(url: str, scenario: str, out_dir: Path) -> dict:
    cmd = ["npx", "-y", SUITE, "server", "--url", url, "--scenario", scenario]
    cmd += ["--output-dir", str(out_dir), "--verbose"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    checks = []
    for f in sorted(out_dir.rglob("checks.json")):
        if scenario in str(f.parent):
            checks = json.loads(f.read_text())
    return {
        "scenario": scenario,
        "exit_code": proc.returncode,
        "checks": [
            {k: c.get(k) for k in ("id", "status", "errorMessage") if c.get(k) is not None}
            for c in checks
        ],
        "stdout_tail": proc.stdout[-2000:] if not checks else None,
    }


async def main(scenarios: list[str]) -> dict:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    os.environ.setdefault("MCP_TOKEN", "conformance-run")
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"mcp_public_url": base})
    app = InjectBearer(create_app(settings), settings.mcp_token.get_secret_value())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 (uvicorn exposes a flag, not an event)
        await asyncio.sleep(0.05)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            results = [
                await asyncio.to_thread(run_suite, f"{base}/mcp", s, Path(tmp)) for s in scenarios
            ]
    finally:
        server.should_exit = True
        await task
    checks = [c for r in results for c in r["checks"]]
    return {
        "suite": SUITE,
        "spec_version": SPEC_VERSION,
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "scenarios": results,
        "checks_total": len(checks),
        "checks_passed": sum(c["status"] == "SUCCESS" for c in checks),
        "checks_failed": sum(c["status"] == "FAILURE" for c in checks),
        "checks_other": sum(c["status"] not in ("SUCCESS", "FAILURE") for c in checks),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--scenario", action="append", help="default: the applicable scenarios")
    p.add_argument("--no-write", action="store_true", help="print only")
    args = p.parse_args()
    report = asyncio.run(main(args.scenario or APPLICABLE))
    text = json.dumps(report, indent=2)
    print(text)
    if not args.no_write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text + "\n")
    sys.exit(0 if report["checks_failed"] == 0 else 1)
