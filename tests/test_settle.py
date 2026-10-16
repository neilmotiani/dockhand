import io
import re
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.capture.settle import SettleDetector, max_cell_change, replay
from app.ring.whep import buffer_until_stall

CLIPS = Path(__file__).parent / "fixtures" / "capture"


def load_clip(name: str) -> list[tuple[float, bytes]]:
    """Spread-sampled Playground clips (60 frames each, grayscale, 320 px), recorded
    2026-10-16 with scripts/ring_spike.py --spread; t comes from the file name."""
    files = sorted((CLIPS / f"clip_{name}").glob("frame_*.jpg"))
    return [(float(re.search(r"t(\d+\.\d+)s", f.name).group(1)), f.read_bytes()) for f in files]


def jpeg(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr.astype(np.uint8)).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def test_static_package_clip_exits_early():
    clip = load_clip("package")
    assert clip[-1][0] > 25  # the stream ran past the old 25 s timeout
    d = SettleDetector()
    settled = replay(clip, d)
    assert settled is not None and 4.0 <= settled < 6.0, d.history
    assert max(c for _, c in d.history) < d.threshold


def test_vehicle_clip_does_not_stop_while_the_car_moves():
    """The car crosses the lot until ~5 s and is gone by ~6.2 s (checked by eye on a contact
    sheet of the clip). Something in the scene keeps changing after that, so the detector
    should not stop at all before the clip ends; at minimum never before 6.2 s."""
    d = SettleDetector()
    settled = replay(load_clip("vehicle"), d)
    assert settled is None, d.history
    moving = [c for t, c in d.history if t <= 4.2]
    assert min(moving) > 5 * d.threshold


def test_min_capture_is_respected():
    still = np.full((180, 320), 120.0)
    frames = [(i * 0.5, jpeg(still)) for i in range(20)]
    d = SettleDetector(settle_s=1.0, min_s=4.0)
    assert replay(frames, d) == 4.0


def test_motion_resets_the_calm_window():
    still = np.full((180, 320), 120.0)
    moved = still.copy()
    moved[60:120, 100:180] = 255  # something enters the frame at t = 3.0 s and stays
    frames = [(i * 0.5, jpeg(still if i < 6 else moved)) for i in range(30)]
    d = SettleDetector(settle_s=3.0, min_s=4.0)
    # The (2.5 s, 3.0 s) pair sees the change; calm restarts at 3.0 s, so it settles at 6.0 s
    # rather than at min_s = 4.0 s.
    assert replay(frames, d) == 6.0
    assert [t for t, c in d.history if c >= d.threshold] == [3.0]


def test_checks_are_rate_independent():
    still = jpeg(np.full((180, 320), 90.0))
    fast = [(i / 30, still) for i in range(30 * 8)]  # 30 fps for 8 s
    d = SettleDetector()
    replay(fast, d)
    gaps = np.diff([t for t, _ in d.history])
    assert gaps.min() >= 0.5 - 1e-9 and len(d.history) <= 16


def test_watermark_corners_are_ignored():
    a = np.full((36, 64), 100.0)
    b = a.copy()
    b[33:, :20] = 255  # bottom-left timestamp area
    b[:3, 50:] = 255  # top-right text area
    assert max_cell_change(a, b) == 0.0
    b[15:20, 30:35] = 255  # centre of the frame
    assert max_cell_change(a, b) > 50
    assert max_cell_change(a, np.zeros((10, 10))) == float("inf")


class Track:
    def __init__(self, frames):
        self.frames = list(frames)

    async def recv(self):
        import asyncio

        if not self.frames:
            await asyncio.sleep(3600)
        await asyncio.sleep(0)
        return self.frames.pop(0)


class FakeFrame:
    def to_image(self):
        return Image.new("RGB", (64, 36), (100, 100, 100))


async def test_buffer_stops_when_settled_or_stalled():
    buf: list = []
    calls = []

    def stop_after_five(t, jpeg_bytes):
        calls.append(t)
        return len(calls) >= 5

    reason = await buffer_until_stall(Track([FakeFrame()] * 50), 1.0, buf, stop_after_five)
    assert reason == "settled" and len(buf) == 5
    buf2: list = []
    assert await buffer_until_stall(Track([FakeFrame()] * 3), 0.05, buf2) == "stalled"
    assert len(buf2) == 3


@pytest.mark.parametrize("settle_s,expect_detector", [(3.0, True), (0.0, False)])
async def test_capturer_passes_detector_only_when_enabled(settle_s, expect_detector):
    from datetime import UTC, datetime

    from app.capture.service import Capturer
    from app.config import Settings
    from app.ring.whep import Clip
    from tests.test_capture import CAM, DeviceRing, scene

    seen = {}

    async def fake_capture(ring, device_id, **kw):
        seen.update(kw)
        return Clip(frames=[(i * 0.1, scene(seed=i)) for i in range(5)], total_ms=1.0)

    c = Capturer(
        DeviceRing([CAM]),
        Settings(_env_file=None, capture_settle_s=settle_s),
        capture_fn=fake_capture,
        clock=lambda: datetime(2026, 10, 16, tzinfo=UTC),
    )
    await c.capture("cam-1", exact=True)
    assert (seen["should_stop"] is not None) is expect_detector
