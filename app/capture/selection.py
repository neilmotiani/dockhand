"""Pick the few frames of a clip worth sending to the vision model (SPEC.md section 5).

1. Drop the first decoded frame of the session. In one Playground Package capture
   (2026-10-16 00:11 UTC, 60 frames kept with --spread) frame 0 was visibly smeared and had
   the lowest Laplacian variance of all 60 (753, against a median of 1183); frames 0 and 5
   are in tests/fixtures/capture/. That fits a WebRTC encoder starting at a low bitrate and
   ramping up. It is one session, so this is a cheap guard, not a measured rule.
2. Sample `n_sample` frames evenly over the rest of the clip (the `--spread` logic from
   scripts/ring_spike.py). Clip length varies by session, so sampling by position adapts to
   whatever arrived, where a fixed interval could come back nearly empty.
3. Score each sample on sharpness (variance of the Laplacian) and on change from a reference
   of the empty scene, and keep the top `k`, in time order.

The reference is a stored empty-doorstep image for the device when one exists. Otherwise it
is the per-pixel median of the samples: in a short clip the background is what most frames
share, so frames where something (a driver, a box being set down) covers it score higher.
Both scores are min-max normalised within the clip, because only the ranking matters and
the raw scales depend on resolution and scene.
"""

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

from app.ring.whep import Frame

ANALYSIS_WIDTH = 320  # score on a downscaled copy; sharpness ranking survives the resize
# Change is measured on a further 5x box-downscaled copy (64 px wide). Differencing at full
# resolution counts motion blur and sensor noise as "change"; the low-pass copy keeps only
# scene content (a person, a box), so a blurry frame cannot win on change alone.
CHANGE_DOWNSCALE = 5


def evenly_spaced(total: int, k: int) -> list[int]:
    """k indices spread evenly over range(total), always including the first and last."""
    if total <= k:
        return list(range(total))
    if k == 1:
        return [0]
    return [round(i * (total - 1) / (k - 1)) for i in range(k)]


def to_gray(jpeg: bytes, width: int = ANALYSIS_WIDTH) -> np.ndarray:
    img = Image.open(io.BytesIO(jpeg)).convert("L")
    if img.width > width:
        img = img.resize((width, max(1, round(img.height * width / img.width))))
    return np.asarray(img, dtype=np.float32)


def lowpass(gray: np.ndarray, factor: int = CHANGE_DOWNSCALE) -> np.ndarray:
    h, w = gray.shape
    img = Image.fromarray(gray.astype(np.uint8), mode="L")
    small = img.resize((max(1, w // factor), max(1, h // factor)), Image.Resampling.BOX)
    return np.asarray(small, dtype=np.float32)


def laplacian_variance(gray: np.ndarray) -> float:
    """Variance of the 4-neighbour Laplacian. Blur removes high frequencies, lowering it."""
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    c = gray[1:-1, 1:-1]
    lap = gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:] - 4 * c
    return float(lap.var())


def _normalise(x: np.ndarray) -> np.ndarray:
    span = float(x.max() - x.min()) if x.size else 0.0
    return (x - x.min()) / span if span > 0 else np.zeros_like(x)


@dataclass(frozen=True)
class ScoredFrame:
    index: int  # position in the original clip (frame 0 = first decoded frame)
    t: float
    jpeg: bytes
    sharpness: float
    change: float
    score: float


@dataclass(frozen=True)
class Selection:
    frames: list[ScoredFrame]  # chosen, in time order
    decoded: int  # frames in the clip
    sampled: int  # frames scored (the LLM calls an unselected pipeline would have made)

    @property
    def sent(self) -> int:
        return len(self.frames)


def select_frames(
    clip: list[Frame],
    *,
    n_sample: int = 10,
    k: int = 3,
    reference: bytes | None = None,
    sharpness_weight: float = 0.5,
) -> Selection:
    candidates = list(enumerate(clip))[1:]  # drop the session's first frame (see module doc)
    if not candidates:
        return Selection(frames=[], decoded=len(clip), sampled=0)
    sampled = [candidates[i] for i in evenly_spaced(len(candidates), n_sample)]
    grays = [to_gray(jpeg) for _, (_, jpeg) in sampled]
    smalls = [lowpass(g) for g in grays]

    ref = lowpass(to_gray(reference)) if reference is not None else None
    if ref is None or any(s.shape != ref.shape for s in smalls):
        # No stored reference (or one from a different resolution): use the clip's median.
        same_shape = len({s.shape for s in smalls}) == 1
        ref = np.median(np.stack(smalls), axis=0) if same_shape else None

    sharp = np.array([laplacian_variance(g) for g in grays])
    change = (
        np.array([float(np.abs(s - ref).mean()) for s in smalls])
        if ref is not None
        else np.zeros(len(grays))
    )
    score = sharpness_weight * _normalise(sharp) + (1 - sharpness_weight) * _normalise(change)

    scored = [
        ScoredFrame(
            index=idx,
            t=t,
            jpeg=jpeg,
            sharpness=round(float(sharp[i]), 2),
            change=round(float(change[i]), 3),
            score=round(float(score[i]), 4),
        )
        for i, (idx, (t, jpeg)) in enumerate(sampled)
    ]
    # Highest score first; ties go to the later frame (more of the event has happened).
    best = sorted(scored, key=lambda f: (-f.score, -f.index))[:k]
    return Selection(
        frames=sorted(best, key=lambda f: f.index), decoded=len(clip), sampled=len(sampled)
    )
