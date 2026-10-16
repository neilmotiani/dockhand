"""Spike: list Ring devices, open a WHEP live-view session on one, save 10 frames, close it.

Steps:
1. GET /v1/devices?include=status,capabilities and print each device (name, online, codecs).
2. Pick the device whose name matches --device (default "package"), or the only online camera.
3. Create a recvonly H.264 WebRTC offer with aiortc, POST it to the WHEP sessions endpoint,
   apply the SDP answer and decode frames from the video track.
4. Save --frames frames (default 10) to data/spike/, then DELETE the session.

With --record, every raw request/response is written to tests/fixtures/ring/ with the access
token, ICE passwords, DTLS fingerprints and client IPs redacted. The token itself is never printed.

Usage:
    uv run python -m scripts.ring_spike [--device NAME_OR_ID] [--frames 10] [--out DIR]
        [--interval 0.5] [--timeout 25] [--record] [--spread]
"""

import argparse
import asyncio
import io
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

from app.config import get_settings

SPIKE_DIR = Path("data/spike")
FIXTURE_DIR = Path("tests/fixtures/ring")
STUN = "stun:stun.l.google.com:19302"
# Battery cameras end WHEP sessions after 30 s and line-powered ones after 60 s (Ring WHEP
# docs), so the whole capture has to fit well inside 30 s.
CAPTURE_TIMEOUT_S = 25.0
FRAME_INTERVAL_S = 0.5
# The Playground's simulated events are short clips (~10 s observed for Package); once a clip
# ends Ring stops sending RTP while ICE stays connected. A gap this long means the clip is over.
STALL_S = 2.0
SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "x-amz-security-token"}


def redact_sdp(sdp: str, *, mask_addresses: bool = False) -> str:
    """Strip SDP session credentials (short-lived, but no reason to commit them). With
    mask_addresses, also mask IPv4/IPv6 addresses, used on our own offer so fixtures do not carry
    the developer's LAN or home IP."""
    sdp = re.sub(r"(a=ice-pwd:)[^\r\n]+", r"\1<REDACTED>", sdp)
    sdp = re.sub(r"(a=fingerprint:\S+ )[^\r\n]+", r"\1<REDACTED>", sdp)
    if mask_addresses:
        sdp = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "0.0.0.0", sdp)
        sdp = re.sub(r"\b[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{0,4}){2,7}\b", "::", sdp)
    return sdp


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("<REDACTED>" if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}


def record(name: str, response: httpx.Response, secrets: list[str]) -> None:
    """Save one request/response pair as a fixture, redacted."""
    req = response.request
    req_body = req.content.decode("utf-8", "replace") if req.content else None
    body: Any
    if response.headers.get("content-type", "").startswith("application/sdp"):
        body = redact_sdp(response.text)
    else:
        try:
            body = response.json()
        except ValueError:
            body = response.text
    if req_body and req.headers.get("content-type", "").startswith("application/sdp"):
        req_body = redact_sdp(req_body, mask_addresses=True)
    fixture = {
        "request": {
            "method": req.method,
            "url": str(req.url),
            "headers": redact_headers(dict(req.headers)),
            "body": req_body,
        },
        "response": {
            "status": response.status_code,
            "headers": redact_headers(dict(response.headers)),
            "body": body,
        },
    }
    text = json.dumps(fixture, indent=2)
    for s in secrets:  # belt and braces: the token must never reach disk
        if s:
            text = text.replace(s, "<REDACTED>")
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / f"{name}.json").write_text(text + "\n")


def summarize_devices(doc: dict) -> list[dict]:
    """Flatten the JSON:API compound document into one dict per device.

    Related resources are matched by (type, id) from relationships into `included`, as the
    Device Discovery docs describe.
    """
    included = {(r["type"], r["id"]): r.get("attributes", {}) for r in doc.get("included", [])}
    out = []
    for d in doc.get("data", []):
        rels = d.get("relationships", {})

        def rel_attrs(name: str, rels: dict = rels) -> dict:
            ref = (rels.get(name) or {}).get("data") or {}
            return included.get((ref.get("type"), ref.get("id")), {})

        status = rel_attrs("status")
        caps = rel_attrs("capabilities")
        video = caps.get("video") or {}
        out.append(
            {
                "id": d["id"],
                "name": (d.get("attributes") or {}).get("name", ""),
                "online": status.get("online"),
                "codecs": video.get("codecs"),
            }
        )
    return out


def pick_device(devices: list[dict], query: str) -> dict:
    """Exact ID match first, then case-insensitive name substring, then the only video device."""
    for d in devices:
        if d["id"] == query:
            return d
    q = query.lower()
    by_name = [d for d in devices if q in d["name"].lower()]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        raise LookupError(f"{len(by_name)} devices match {query!r}: {[d['name'] for d in by_name]}")
    # Sensors share the device collection but have no media; a null `video` capability marks them.
    cameras = [d for d in devices if d["codecs"]]
    if len(cameras) == 1:
        return cameras[0]
    raise LookupError(f"no device matches {query!r}; pass --device with a name or ID")


def session_url(location: str | None, base_url: str) -> str:
    if not location:
        raise RuntimeError("WHEP response has no Location header; cannot close the session")
    return urljoin(base_url + "/", location)


async def collect_frames(
    track, n: int, out_dir: Path, interval_s: float, saved: list[Path]
) -> list[Path]:
    """Decode frames from the track and save one every interval_s until n are saved.

    Appends to the caller's `saved` list so a timeout still reports what reached disk.
    """
    await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)
    last = 0.0
    while len(saved) < n:
        try:
            frame = await track.recv()
        except MediaStreamError:
            break
        now = time.monotonic()
        if saved and now - last < interval_s:
            continue
        last = now
        path = out_dir / f"frame_{len(saved):02d}.jpg"
        await asyncio.to_thread(frame.to_image().save, path, quality=92)
        saved.append(path)
        print(f"  saved {path} ({frame.width}x{frame.height})")
    return saved


def evenly_spaced(total: int, k: int) -> list[int]:
    """k indices spread evenly over range(total), always including the first and last."""
    if total <= k:
        return list(range(total))
    if k == 1:
        return [0]
    return [round(i * (total - 1) / (k - 1)) for i in range(k)]


def _jpeg(frame) -> bytes:
    buf = io.BytesIO()
    frame.to_image().save(buf, format="JPEG", quality=92)
    return buf.getvalue()


async def buffer_until_stall(track, stall_s: float, buf: list[tuple[float, bytes]]) -> None:
    """Decode every frame into buf as (seconds since first frame, JPEG bytes) until no frame
    arrives for stall_s. Before the first frame it waits indefinitely (bounded by the caller)."""
    t_first = None
    while True:
        try:
            if t_first is None:
                frame = await track.recv()
            else:
                frame = await asyncio.wait_for(track.recv(), timeout=stall_s)
        except (TimeoutError, MediaStreamError):
            return
        now = time.monotonic()
        t_first = t_first if t_first is not None else now
        # JPEG-encode now: ~300 raw 720p frames would hold ~400 MB of decoded YUV.
        buf.append((now - t_first, await asyncio.to_thread(_jpeg, frame)))


async def run(
    device_query: str,
    n_frames: int,
    out_dir: Path = SPIKE_DIR,
    interval_s: float = FRAME_INTERVAL_S,
    timeout_s: float = CAPTURE_TIMEOUT_S,
    record_fixtures: bool = False,
    spread: bool = False,
) -> int:
    settings = get_settings()
    if settings.ring_access_token is None:
        print("RING_ACCESS_TOKEN is not set", file=sys.stderr)
        return 2
    token = settings.ring_access_token.get_secret_value()
    secrets = [token]

    def save(name: str, response: httpx.Response) -> None:
        if record_fixtures:
            record(name, response, secrets)

    base = settings.ring_api_base_url.rstrip("/")
    auth = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=base, headers=auth, timeout=20.0) as http:
        t0 = time.perf_counter()
        r = await http.get("/v1/devices", params={"include": "status,capabilities"})
        save("devices_list", r)
        print(f"GET /v1/devices -> {r.status_code} ({(time.perf_counter() - t0) * 1000:.0f} ms)")
        if r.status_code != 200:
            print(r.text, file=sys.stderr)
            return 1
        devices = summarize_devices(r.json())
        for d in devices:
            print(f"  {d['name']!r:32} online={d['online']} codecs={d['codecs']} id={d['id']}")
        device = pick_device(devices, device_query)
        print(f"Using {device['name']!r}")
        # An offline Playground device still gets 201 from WHEP and a connected peer, but no RTP
        # ever arrives (see FRICTION_LOG.md), so fail here instead of waiting out the timeout.
        if device["online"] is False:
            print(
                "Device is offline; Ring will not send video. Check the Playground.",
                file=sys.stderr,
            )
            return 1

        pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=STUN)]))
        track_ready: asyncio.Future = asyncio.get_running_loop().create_future()

        # Handlers go on before the offer is created (the WHEP docs call late `ontrack` the most
        # common cause of "connected but no video").
        @pc.on("track")
        def on_track(track):
            print(f"  track: {track.kind}")
            if track.kind == "video" and not track_ready.done():
                track_ready.set_result(track)

        @pc.on("connectionstatechange")
        async def on_state():
            print(f"  connection: {pc.connectionState}")

        pc.addTransceiver("video", direction="recvonly")
        # aiortc finishes ICE gathering inside setLocalDescription, so the offer is complete.
        await pc.setLocalDescription(await pc.createOffer())

        whep_path = f"/v1/devices/{device['id']}/media/streaming/whep/sessions"
        sess_url = None
        saved: list[Path] = []
        try:
            t0 = time.perf_counter()
            r = await http.post(
                whep_path,
                content=pc.localDescription.sdp,
                headers={"Content-Type": "application/sdp"},
            )
            save("whep_create", r)
            ms = (time.perf_counter() - t0) * 1000
            print(f"POST whep/sessions -> {r.status_code} ({ms:.0f} ms)")
            if r.status_code != 201:
                print(r.text, file=sys.stderr)
                return 1
            sess_url = session_url(r.headers.get("location"), base)
            await pc.setRemoteDescription(RTCSessionDescription(sdp=r.text, type="answer"))

            t0 = time.perf_counter()
            track = await asyncio.wait_for(track_ready, timeout=10)
            if spread:
                buf: list[tuple[float, bytes]] = []
                try:
                    await asyncio.wait_for(buffer_until_stall(track, STALL_S, buf), timeout_s)
                except TimeoutError:
                    print("Capture timeout reached before the stream stalled", file=sys.stderr)
                saved.extend(await asyncio.to_thread(write_spread, buf, n_frames, out_dir))
                span = buf[-1][0] if buf else 0.0
                print(f"Decoded {len(buf)} frames over {span:.1f} s; kept {len(saved)} evenly")
            else:
                await asyncio.wait_for(
                    collect_frames(track, n_frames, out_dir, interval_s, saved), timeout_s
                )
                print(f"Captured {len(saved)} frames in {time.perf_counter() - t0:.1f} s")
        except TimeoutError:
            print(f"Timed out with {len(saved)} frames saved", file=sys.stderr)
        finally:
            await pc.close()
            if sess_url:
                r = await http.delete(sess_url)
                save("whep_delete", r)
                print(f"DELETE session -> {r.status_code}")
    return 0 if len(saved) >= n_frames else 1


def write_spread(buf: list[tuple[float, bytes]], n: int, out_dir: Path) -> list[Path]:
    """Write n frames evenly spaced over the buffered clip, replacing earlier frame_*.jpg."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("frame_*.jpg"):
        old.unlink()
    paths = []
    for rank, i in enumerate(evenly_spaced(len(buf), n)):
        t, data = buf[i]
        path = out_dir / f"frame_{rank:02d}_t{t:05.2f}s.jpg"
        path.write_bytes(data)
        paths.append(path)
    return paths


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--device", default="package", help="device name substring or exact ID")
    p.add_argument("--frames", type=int, default=10)
    p.add_argument("--out", type=Path, default=SPIKE_DIR, help="directory for saved frames")
    p.add_argument(
        "--interval", type=float, default=FRAME_INTERVAL_S, help="seconds between frames"
    )
    p.add_argument("--timeout", type=float, default=CAPTURE_TIMEOUT_S, help="capture timeout (s)")
    p.add_argument(
        "--record", action="store_true", help=f"write redacted fixtures to {FIXTURE_DIR}"
    )
    p.add_argument(
        "--spread",
        action="store_true",
        help="decode the whole clip until the stream stalls, then keep --frames evenly spaced "
        "(ignores --interval)",
    )
    args = p.parse_args()
    sys.exit(
        asyncio.run(
            run(
                args.device,
                args.frames,
                args.out,
                args.interval,
                args.timeout,
                args.record,
                args.spread,
            )
        )
    )


if __name__ == "__main__":
    main()
