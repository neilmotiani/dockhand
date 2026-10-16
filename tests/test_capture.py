import asyncio
import io
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.capture.selection import evenly_spaced, laplacian_variance, select_frames, to_gray
from app.capture.service import CaptureError, Capturer, DeviceUnavailable
from app.config import Settings
from app.ring.devices import Device
from app.ring.whep import Clip, capture_clip

FIXTURES = Path(__file__).parent / "fixtures" / "capture"


def jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def scene(box: bool = False, blur: float = 0.0, seed: int = 0) -> bytes:
    """A 'doorstep': smooth texture with step edges (white noise would be unrealistic: blur
    erases all of it), optionally with a box on it, optionally blurred."""
    rng = np.random.default_rng(seed)
    coarse = Image.fromarray((rng.random((18, 32)) * 255).astype(np.uint8))
    img = coarse.resize((320, 180), Image.Resampling.BILINEAR).convert("RGB")
    draw = ImageDraw.Draw(img)
    for y in range(20, 180, 40):  # steps
        draw.line([(0, y), (320, y)], fill=(30, 30, 30), width=3)
    if box:
        ImageDraw.Draw(img).rectangle([120, 60, 220, 150], fill=(160, 110, 60))
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    return jpeg(img)


# --- selection ----------------------------------------------------------------------------


def test_evenly_spaced():
    assert evenly_spaced(5, 10) == [0, 1, 2, 3, 4]
    idx = evenly_spaced(280, 60)
    assert idx[0] == 0 and idx[-1] == 279 and len(set(idx)) == 60
    assert evenly_spaced(10, 1) == [0]


def test_laplacian_ranks_blur_lower():
    sharp, blurred = to_gray(scene()), to_gray(scene(blur=2))
    assert laplacian_variance(sharp) > 5 * laplacian_variance(blurred)
    assert laplacian_variance(np.zeros((2, 2), dtype=np.float32)) == 0.0


def test_real_playground_first_frame_is_softer():
    f0 = (FIXTURES / "playground_package_frame00.jpg").read_bytes()
    f5 = (FIXTURES / "playground_package_frame05.jpg").read_bytes()
    assert laplacian_variance(to_gray(f0)) < laplacian_variance(to_gray(f5))


def test_first_frame_is_never_selected():
    # Frame 0 is the sharpest frame with a box, so only the drop rule can exclude it.
    clip = [(0.0, scene(box=True))] + [(i * 0.1, scene(blur=3)) for i in range(1, 6)]
    sel = select_frames(clip, n_sample=10, k=3)
    assert all(f.index != 0 for f in sel.frames)
    assert sel.decoded == 6 and sel.sampled == 5 and sel.sent == 3


def test_selects_sharp_frames_that_differ_from_background():
    frames = []
    for i in range(1, 21):
        if i in (7, 13):
            frames.append(scene(box=True))  # the delivery: sharp and different
        elif i % 2:
            frames.append(scene(blur=2.5))  # motion blur
        else:
            frames.append(scene())  # sharp empty step
    clip = [(0.0, scene())] + [(i * 0.1, f) for i, f in enumerate(frames, start=1)]
    sel = select_frames(clip, n_sample=20, k=2)
    assert [f.index for f in sel.frames] == [7, 13]


def test_reference_image_is_used_when_given():
    # Every frame shows the box, so the clip median contains it too; only a stored empty
    # reference makes the box count as change.
    clip = [(0.0, scene())] + [(i * 0.1, scene(box=True)) for i in range(1, 5)]
    no_ref = select_frames(clip, k=4)
    with_ref = select_frames(clip, k=4, reference=scene())
    assert max(f.change for f in with_ref.frames) > 5 * max(f.change for f in no_ref.frames)


def test_reference_with_wrong_size_falls_back_to_median():
    clip = [(i * 0.1, scene()) for i in range(4)]
    small = jpeg(Image.new("RGB", (64, 36)))
    assert select_frames(clip, k=2, reference=small).sent == 2


def test_output_is_chronological_and_spread():
    clip = [(i * 0.1, scene(seed=i)) for i in range(100)]
    sel = select_frames(clip, n_sample=10, k=3)
    assert sel.sampled == 10 and sel.sent == 3
    assert [f.index for f in sel.frames] == sorted(f.index for f in sel.frames)


def test_short_clips():
    assert select_frames([], k=3).sent == 0
    assert select_frames([(0.0, scene())], k=3).sent == 0
    sel = select_frames([(0.0, scene()), (0.1, scene(box=True))], k=3)
    assert [f.index for f in sel.frames] == [1]


# --- WHEP session lifecycle ---------------------------------------------------------------


class FakeFrame:
    def to_image(self):
        return Image.new("RGB", (4, 4))


class FakeTrack:
    kind = "video"

    def __init__(self, n: int) -> None:
        self.n = n

    async def recv(self):
        if self.n == 0:
            await asyncio.sleep(3600)  # clip over: RTP stops, connection stays up
        self.n -= 1
        await asyncio.sleep(0.001)
        return FakeFrame()


class FakeDescription:
    sdp = "v=0 offer"


class FakePC:
    def __init__(self, frames: int = 5, send_track: bool = True) -> None:
        self.handlers: dict = {}
        self.frames = frames
        self.send_track = send_track
        self.closed = False
        self.localDescription = FakeDescription()

    def on(self, name):
        def register(fn):
            self.handlers[name] = fn
            return fn

        return register

    def addTransceiver(self, kind, direction):
        assert direction == "recvonly"

    async def createOffer(self):
        return FakeDescription()

    async def setLocalDescription(self, d):
        pass

    async def setRemoteDescription(self, d):
        assert d.sdp == "v=0 answer" and d.type == "answer"
        if self.send_track:
            self.handlers["track"](FakeTrack(self.frames))

    async def close(self):
        self.closed = True


class FakeRing:
    def __init__(self, fail_create: bool = False) -> None:
        self.deleted: list[str] = []
        self.fail_create = fail_create

    async def whep_create(self, device_id, sdp):
        assert sdp == "v=0 offer"
        if self.fail_create:
            raise RuntimeError("503")
        return "v=0 answer", "https://api.amazonvision.com/v1/devices/d/whep/sessions/s?location=x"

    async def whep_delete(self, url):
        self.deleted.append(url)
        return 200


async def test_capture_clip_buffers_until_stall_and_closes():
    ring, pc = FakeRing(), FakePC(frames=5)
    clip = await capture_clip(ring, "d", stall_s=0.1, timeout_s=5, pc_factory=lambda: pc)
    assert len(clip.frames) == 5 and not clip.timed_out
    assert clip.setup_ms is not None and clip.total_ms > 0
    assert pc.closed
    assert ring.deleted == ["https://api.amazonvision.com/v1/devices/d/whep/sessions/s?location=x"]


async def test_capture_clip_timeout_keeps_frames():
    class Endless(FakeTrack):
        async def recv(self):
            await asyncio.sleep(0.005)
            return FakeFrame()

    pc = FakePC()
    pc.setRemoteDescription = lambda d: _emit(pc, Endless(0))
    clip = await capture_clip(FakeRing(), "d", stall_s=1, timeout_s=0.2, pc_factory=lambda: pc)
    assert clip.timed_out and len(clip.frames) > 3


async def _emit(pc, track):
    pc.handlers["track"](track)


async def test_capture_clip_closes_session_when_no_track_arrives():
    ring, pc = FakeRing(), FakePC(send_track=False)
    with pytest.raises(TimeoutError):
        await capture_clip(ring, "d", track_timeout_s=0.05, pc_factory=lambda: pc)
    assert pc.closed and len(ring.deleted) == 1


async def test_capture_clip_create_failure_closes_peer():
    ring, pc = FakeRing(fail_create=True), FakePC()
    with pytest.raises(RuntimeError):
        await capture_clip(ring, "d", pc_factory=lambda: pc)
    assert pc.closed and ring.deleted == []


# --- capture service ----------------------------------------------------------------------

CAM = Device(id="cam-1", name="Back Door", online=True, codecs=["AVC"])
OFFLINE = Device(id="cam-2", name="Front Porch", online=False, codecs=["AVC"])
SENSOR = Device(id="sensor-1", name="Back Door Contact", online=True, codecs=None)


class DeviceRing:
    def __init__(self, devices):
        self.devices = devices
        self.list_calls = 0

    async def list_devices(self):
        self.list_calls += 1
        return self.devices


def capturer(devices, clip_frames: int = 12, **settings) -> tuple[Capturer, list]:
    calls: list = []

    async def fake_capture(ring, device_id, **kw):
        calls.append((device_id, kw))
        frames = [(i * 0.1, scene(box=i > 5, seed=i)) for i in range(clip_frames)]
        return Clip(frames=frames, setup_ms=300.0, total_ms=4000.0)

    s = Settings(_env_file=None, **settings)
    c = Capturer(
        DeviceRing(devices),
        s,
        capture_fn=fake_capture,
        clock=lambda: datetime(2026, 10, 16, tzinfo=UTC),
    )
    return c, calls


async def test_capture_runs_selection_with_settings():
    c, calls = capturer([CAM, SENSOR], capture_select_frames=2, capture_sample_frames=6)
    result = await c.capture("cam-1", exact=True)
    assert result.device == CAM
    assert result.selection.sent == 2 and result.selection.sampled == 6
    assert set(result.latency_ms) == {"device_resolve", "whep_setup", "capture", "select"}
    assert calls[0][0] == "cam-1" and calls[0][1]["timeout_s"] == 25.0


async def test_exact_lookup_never_falls_back_to_another_camera():
    c, _ = capturer([CAM])
    with pytest.raises(DeviceUnavailable):
        await c.capture("removed-device", exact=True)


async def test_manual_lookup_by_name_or_only_camera():
    c, _ = capturer([CAM, SENSOR])
    assert (await c.resolve("back door", exact=False)) == CAM
    assert (await c.resolve(None, exact=False)) == CAM


async def test_offline_and_sensor_devices_are_refused():
    c, calls = capturer([CAM, OFFLINE, SENSOR])
    with pytest.raises(DeviceUnavailable, match="offline"):
        await c.capture("cam-2", exact=True)
    with pytest.raises(DeviceUnavailable, match="no camera"):
        await c.capture("sensor-1", exact=True)
    assert calls == []


async def test_device_list_is_cached():
    c, _ = capturer([CAM])
    await c.resolve("cam-1", exact=True)
    await c.resolve("cam-1", exact=True)
    assert c._ring.list_calls == 1


async def test_empty_clip_is_an_error():
    c, _ = capturer([CAM], clip_frames=1)
    with pytest.raises(CaptureError):
        await c.capture("cam-1", exact=True)


async def test_reference_dir_is_read_per_device(tmp_path):
    (tmp_path / "cam-1.jpg").write_bytes(scene())
    c, _ = capturer([CAM], capture_reference_dir=tmp_path)
    assert c._reference("cam-1") is not None
    assert c._reference("cam-9") is None
