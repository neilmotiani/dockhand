"""Early exit for live capture once the scene has settled.

The Playground Package stream never stalls, so capture used to run the full 25 s timeout on a
static scene (31 s of a 33 s event-to-ledger time, N=2 on 2026-10-16). This detector ends the
capture once nothing has moved for `settle_s`, after at least `min_s` of video. The timeout
stays as the hard maximum.

Motion signal: every `check_interval_s` the newest frame is compared with the previous check
frame (not the previous decoded frame, so the signal doesn't depend on frame rate and can be
replayed on the spread-sampled clips in tests/fixtures/capture/). Both are reduced to the same
64 px wide low-pass grayscale copy used for frame selection, and the change is the mean
absolute difference of the most-changed 4x4 cell. Max-cell rather than the frame mean,
because a delivery happens in a small part of the frame: the Vehicle clip's car, far away,
moves the frame mean (0.6–0.75) no more than encoder noise on the static Package clip
(up to 0.62), while its max-cell change is 26–60 against the static clip's 0.5–1.9.
The Ring watermark corners are masked: the overlay clocks change every second.

THRESHOLD = 3.0 was chosen on exactly two recorded clips (one static, one moving), so treat
it as a starting point, not a measured operating point.
"""

from dataclasses import dataclass, field

import numpy as np

from app.capture.selection import lowpass, to_gray

DEFAULT_THRESHOLD = 3.0
CELL = 4


def _mask(h: int, w: int) -> np.ndarray:
    m = np.ones((h, w), dtype=bool)
    m[int(h * 0.88) :, : int(w * 0.40)] = False  # bottom-left timestamp
    m[int(h * 0.88) :, int(w * 0.60) :] = False  # bottom-right timestamp
    m[: int(h * 0.12), int(w * 0.60) :] = False  # top-right device / partner text
    m[: int(h * 0.14), : int(w * 0.15)] = False  # Ring logo
    return m


def max_cell_change(a: np.ndarray, b: np.ndarray, cell: int = CELL) -> float:
    if a.shape != b.shape:
        return float("inf")  # resolution switch mid-stream: treat as motion
    d = np.where(_mask(*a.shape), np.abs(a - b), 0.0)
    h, w = (d.shape[0] // cell) * cell, (d.shape[1] // cell) * cell
    if h == 0 or w == 0:
        return float(d.mean()) if d.size else 0.0
    cells = d[:h, :w].reshape(h // cell, cell, w // cell, cell).mean(axis=(1, 3))
    return float(cells.max())


@dataclass
class SettleDetector:
    threshold: float = DEFAULT_THRESHOLD
    settle_s: float = 3.0
    min_s: float = 4.0
    check_interval_s: float = 0.5
    # (t, change) for every check, for logging and tests.
    history: list[tuple[float, float]] = field(default_factory=list)
    settled_at: float | None = None
    _prev: np.ndarray | None = None
    _prev_t: float | None = None
    _calm_since: float | None = None

    def update(self, t: float, jpeg: bytes) -> bool:
        """Feed a frame at t seconds since the first frame; True once the scene has settled."""
        if self.settled_at is not None:
            return True
        if self._prev_t is not None and t - self._prev_t < self.check_interval_s:
            return False
        g = lowpass(to_gray(jpeg))
        if self._prev is not None:
            change = max_cell_change(g, self._prev)
            self.history.append((round(t, 2), round(change, 2)))
            if change >= self.threshold:
                self._calm_since = None
            elif self._calm_since is None:
                # Calm since the earlier frame of the first calm pair.
                self._calm_since = self._prev_t
        self._prev, self._prev_t = g, t
        if (
            self._calm_since is not None
            and t - self._calm_since >= self.settle_s
            and t >= self.min_s
        ):
            self.settled_at = t
            return True
        return False


def replay(frames: list[tuple[float, bytes]], detector: SettleDetector) -> float | None:
    """Run the detector over a recorded clip; the time it would have stopped, or None."""
    for t, jpeg in frames:
        if detector.update(t, jpeg):
            return detector.settled_at
    return None
