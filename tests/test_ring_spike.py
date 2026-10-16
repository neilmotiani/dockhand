import json
from pathlib import Path

import httpx
import pytest

from scripts import ring_spike
from scripts.ring_spike import pick_device, redact_sdp, session_url, summarize_devices

FIXTURES = Path(__file__).parent / "fixtures" / "ring"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_summarize_devices_matches_included_by_type_and_id():
    devices = summarize_devices(load("devices_list")["response"]["body"])
    assert len(devices) == 1
    d = devices[0]
    assert d["name"] == "Playground Device"
    assert d["online"] is True
    assert d["codecs"] == ["AVC"]
    assert d["id"].startswith("ava1.ring.device.")


def _devices():
    return [
        {"id": "cam-a", "name": "Back Door", "online": True, "codecs": ["AVC"]},
        {"id": "cam-b", "name": "Front Porch", "online": True, "codecs": ["AVC"]},
        {"id": "sensor", "name": "Back Door Contact", "online": True, "codecs": None},
    ]


def test_pick_device_prefers_exact_id_then_name():
    assert pick_device(_devices(), "cam-b")["id"] == "cam-b"
    assert pick_device(_devices(), "porch")["id"] == "cam-b"


def test_pick_device_rejects_ambiguous_name():
    with pytest.raises(LookupError, match="2 devices"):
        pick_device(_devices(), "back door")


def test_pick_device_falls_back_to_the_only_camera():
    devices = [_devices()[0], _devices()[2]]
    assert pick_device(devices, "package")["id"] == "cam-a"


def test_pick_device_no_match_with_several_cameras():
    with pytest.raises(LookupError, match="no device"):
        pick_device(_devices(), "package")


def test_session_url_keeps_location_query_and_resolves_relative():
    # The real Location header carries a ?location= routing token that DELETE needs.
    loc = load("whep_create")["response"]["headers"]["location"]
    assert "?location=" in loc
    assert session_url(loc, "https://api.amazonvision.com") == loc
    assert (
        session_url("/v1/devices/d/media/streaming/whep/sessions/s?location=x", "https://h")
        == "https://h/v1/devices/d/media/streaming/whep/sessions/s?location=x"
    )
    with pytest.raises(RuntimeError):
        session_url(None, "https://h")


def test_redact_sdp_strips_credentials_line_by_line():
    sdp = (
        "a=ice-ufrag:abcd\r\na=ice-pwd:secretpwd\r\n"
        "a=fingerprint:sha-256 AA:BB:CC\r\na=setup:actpass\r\n"
        "c=IN IP4 192.168.1.76\r\n"
        "a=candidate:1 1 udp 1 203.0.113.9 5000 typ srflx raddr 192.168.1.76 rport 5000\r\n"
    )
    out = redact_sdp(sdp)
    assert "secretpwd" not in out and "AA:BB:CC" not in out
    assert "a=setup:actpass" in out  # only the credential lines change
    assert "192.168.1.76" in out
    masked = redact_sdp(sdp, mask_addresses=True)
    assert "192.168.1.76" not in masked and "203.0.113.9" not in masked
    assert "a=ice-ufrag:abcd" in masked


def test_record_never_writes_token(tmp_path, monkeypatch):
    monkeypatch.setattr(ring_spike, "FIXTURE_DIR", tmp_path)
    token = "tok-SECRET-123"
    req = httpx.Request(
        "POST",
        "https://api.amazonvision.com/x",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/sdp"},
        content=b"v=0\r\na=ice-pwd:p\r\nc=IN IP4 10.0.0.5\r\n",
    )
    resp = httpx.Response(
        201,
        request=req,
        headers={"content-type": "application/sdp"},
        text=f"v=0\r\na=ice-pwd:q\r\nx-echo:{token}\r\n",
    )
    ring_spike.record("t", resp, [token])
    text = (tmp_path / "t.json").read_text()
    assert token not in text
    assert "10.0.0.5" not in text
    assert "<REDACTED>" in text


def test_committed_fixtures_are_redacted():
    for path in FIXTURES.glob("*.json"):
        fx = json.loads(path.read_text())
        assert fx["request"]["headers"].get("authorization") == "<REDACTED>"
        body = fx["request"]["body"] or ""
        assert "ice-pwd:<REDACTED>" in body or "ice-pwd" not in body


def test_offline_fixture_parses_as_offline():
    devices = summarize_devices(load("devices_list_offline")["response"]["body"])
    assert devices[0]["online"] is False


class _FakeFrame:
    width, height = 4, 4

    def to_image(self):
        from PIL import Image

        return Image.new("RGB", (4, 4))


class _FakeTrack:
    async def recv(self):
        return _FakeFrame()


async def test_collect_frames_reports_partial_progress_on_timeout(tmp_path):
    import asyncio

    saved: list[Path] = []
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            ring_spike.collect_frames(_FakeTrack(), 100, tmp_path, 0.05, saved), timeout=0.3
        )
    assert 1 <= len(saved) < 100
    assert all(p.exists() for p in saved)


def test_evenly_spaced():
    assert ring_spike.evenly_spaced(5, 10) == [0, 1, 2, 3, 4]
    assert ring_spike.evenly_spaced(280, 60)[0] == 0
    assert ring_spike.evenly_spaced(280, 60)[-1] == 279
    idx = ring_spike.evenly_spaced(280, 60)
    assert len(idx) == 60 and len(set(idx)) == 60 and idx == sorted(idx)
    assert ring_spike.evenly_spaced(10, 1) == [0]


class _ClipTrack:
    """Delivers `n` frames quickly, then stalls like the Playground stream after a clip ends."""

    def __init__(self, n: int):
        self.n = n

    async def recv(self):
        import asyncio

        if self.n == 0:
            await asyncio.sleep(3600)
        self.n -= 1
        await asyncio.sleep(0.001)
        return _FakeFrame()


async def test_buffer_until_stall_stops_after_gap():
    buf: list = []
    await ring_spike.buffer_until_stall(_ClipTrack(5), 0.1, buf)
    assert len(buf) == 5
    assert buf[0][0] == 0.0
    assert all(data[:2] == b"\xff\xd8" for _, data in buf)  # JPEG magic


def test_write_spread_replaces_old_frames(tmp_path):
    (tmp_path / "frame_99.jpg").write_bytes(b"old")
    buf = [(i * 0.1, b"\xff\xd8" + bytes([i])) for i in range(20)]
    paths = ring_spike.write_spread(buf, 5, tmp_path)
    assert len(paths) == 5
    assert not (tmp_path / "frame_99.jpg").exists()
    assert paths[0].name == "frame_00_t00.00s.jpg"
    assert paths[-1].name == "frame_04_t01.90s.jpg"
