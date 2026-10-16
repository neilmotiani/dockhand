"""Synthetic composites for the extraction eval: real box photos pasted onto Ring Playground frames.

Rules are in eval/SELECTION.md (source 3). Each composite is one delivery event: 1-5 boxes from one
carrier, each with a printed shipping label (carrier name plus a made-up tracking number), or no
labels at all for carrier `Unknown`. Boxes are crops from the CC BY 4.0 MPJ "Package Detection"
dataset (downloaded by eval/build_roboflow.py). Count and carrier are exact by construction.

Backgrounds:
- driveway frames from the Playground "Vehicle" clip with no vehicle in view (base count 0);
- porch frames from the "Package" clip, which already show one unlabelled box (base count 1).

Boxes are placed only inside hand-picked ground regions of each background and never overlap each
other, the existing porch box, or the frame edge. If a layout cannot be placed, the next seed is
tried and the retry is recorded on the item.

Usage:
    uv run python -m eval.build_synthetic
"""

import json
import random
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from eval.build_roboflow import DATA_DIR as ROBOFLOW_DIR
from eval.build_roboflow import DATASETS

SEED = 20261016
N_IMAGES = 36
OUT_DIR = Path("data/eval/synthetic")
ITEMS_FILE = Path("eval/labels/items_synthetic_overlay.jsonl")
CARRIERS = ["UPS", "FedEx", "USPS", "Amazon", "DHL", "Sysco", "US Foods", None]
COUNTS = [1, 2, 3, 4, 5]
MARGIN = 8  # minimum gap between pasted boxes, px
MAX_TRIES = 200

Rect = tuple[int, int, int, int]  # x0, y0, x1, y1


@dataclass(frozen=True)
class Background:
    kind: str
    frames: tuple[str, ...]  # glob prefixes under data/spike/<clip>/
    clip: str
    base_count: int
    regions: tuple[Rect, ...]  # boxes must lie fully inside one region
    obstacles: tuple[Rect, ...]  # things already in the scene that must stay visible
    box_width: tuple[int, int]  # pasted box width range, px (1280x720 frames)


# Regions picked on a grid overlay of frame 40 of each clip (both clips are from a static camera).
BACKGROUNDS = [
    Background(
        kind="porch",
        clip="package",
        frames=("frame_10", "frame_25", "frame_40", "frame_55"),
        base_count=1,
        regions=((165, 335, 560, 475), (165, 480, 392, 665), (650, 470, 900, 640)),
        obstacles=((395, 478, 640, 720), (625, 640, 760, 720)),  # existing box, bottle
        box_width=(85, 130),
    ),
    Background(
        kind="driveway",
        clip="vehicle",
        frames=("frame_25", "frame_35", "frame_45", "frame_55"),
        base_count=0,
        regions=((80, 420, 560, 670), (420, 280, 880, 560)),
        obstacles=(),
        box_width=(80, 130),
    ),
]


def box_sprites() -> list[dict]:
    """Single-box MPJ images where the box is large enough to crop cleanly."""
    ds = DATASETS["mpj_boxes"]
    rows = [json.loads(line) for line in (ROBOFLOW_DIR / ds.key / "candidates.jsonl").open()]
    out = []
    for r in sorted(rows, key=lambda r: r["file"]):
        boxes = [b for b in r["boxes"] if b[4] == ds.package_class]
        if len(boxes) != 1:
            continue
        x, y, w, h, _ = boxes[0]
        if w * h >= 0.15 * r["width"] * r["height"] and w >= 80 and h >= 60:
            out.append({"file": r["file"], "bbox": [round(x), round(y), round(w), round(h)]})
    if not out:
        raise RuntimeError("no usable box crops; run eval.build_roboflow mpj_boxes first")
    return out


def overlaps(a: Rect, b: Rect, margin: int = 0) -> bool:
    return not (
        a[2] + margin <= b[0]
        or b[2] + margin <= a[0]
        or a[3] + margin <= b[1]
        or b[3] + margin <= a[1]
    )


def place(rng: random.Random, bg: Background, sizes: list[tuple[int, int]]) -> list[Rect] | None:
    """Non-overlapping positions for boxes of the given (w, h), each inside one region."""
    placed: list[Rect] = []
    for w, h in sizes:
        fits = [r for r in bg.regions if r[2] - r[0] >= w and r[3] - r[1] >= h]
        if not fits:
            return None
        for _ in range(MAX_TRIES):
            r = rng.choice(fits)
            x0 = rng.randint(r[0], r[2] - w)
            y0 = rng.randint(r[1], r[3] - h)
            rect = (x0, y0, x0 + w, y0 + h)
            if any(overlaps(rect, o, MARGIN) for o in placed) or any(
                overlaps(rect, o) for o in bg.obstacles
            ):
                continue
            placed.append(rect)
            break
        else:
            return None
    return placed


def tracking_number(rng: random.Random, carrier: str) -> str:
    digits = "".join(rng.choice("0123456789") for _ in range(12))
    return f"{carrier[:2].upper()} {digits[:4]} {digits[4:8]} {digits[8:]}"


def label_box(
    sprite: Image.Image, carrier: str | None, rng: random.Random
) -> tuple[Image.Image, str | None]:
    """Draw a white shipping label on the box face. Returns the sprite and the tracking text."""
    if carrier is None:
        return sprite, None
    img = sprite.copy()
    d = ImageDraw.Draw(img)
    lw, lh = int(img.width * 0.6), int(img.width * 0.36)
    x0 = (img.width - lw) // 2
    y0 = max(2, (img.height - lh) // 2)
    d.rectangle([x0, y0, x0 + lw, y0 + lh], fill=(250, 250, 248), outline=(30, 30, 30), width=2)
    big = ImageFont.load_default(size=max(14, int(lh * 0.42)))
    small = ImageFont.load_default(size=max(9, int(lh * 0.18)))
    d.text((x0 + 6, y0 + 4), carrier, fill=(0, 0, 0), font=big)
    trk = tracking_number(rng, carrier)
    d.text((x0 + 6, y0 + lh - int(lh * 0.28)), trk, fill=(0, 0, 0), font=small)
    return img, trk


def plan(rng: random.Random) -> list[dict]:
    """Balanced assignment of background frame, carrier and pasted count to each composite."""
    frames = [(bg, f) for bg in BACKGROUNDS for f in bg.frames]
    carriers = (CARRIERS * (N_IMAGES // len(CARRIERS) + 1))[:N_IMAGES]
    counts = (COUNTS * (N_IMAGES // len(COUNTS) + 1))[:N_IMAGES]
    rng.shuffle(carriers)
    rng.shuffle(counts)
    return [
        {"bg": frames[i % len(frames)][0], "frame": frames[i % len(frames)][1],
         "carrier": carriers[i], "pasted": counts[i]}
        for i in range(N_IMAGES)
    ]  # fmt: skip


def build() -> list[dict]:
    rng = random.Random(SEED)
    sprites = box_sprites()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for i, p in enumerate(plan(rng)):
        bg: Background = p["bg"]
        frame_path = next(Path(f"data/spike/{bg.clip}").glob(p["frame"] + "_*.jpg"))
        retries = 0
        while True:
            layout_rng = random.Random(SEED * 1000 + i * 37 + retries)
            chosen = [layout_rng.choice(sprites) for _ in range(p["pasted"])]
            sizes = []
            for s in chosen:
                w = layout_rng.randint(*bg.box_width)
                sizes.append((w, max(40, round(w * s["bbox"][3] / s["bbox"][2]))))
            rects = place(layout_rng, bg, sizes)
            if rects is not None:
                break
            retries += 1
            if retries > 50:
                raise RuntimeError(f"cannot place {p['pasted']} boxes on {bg.kind}")
        canvas = Image.open(frame_path).convert("RGB")
        tracking = []
        for s, rect in zip(chosen, rects, strict=True):
            x, y, w, h = s["bbox"]
            src = Image.open(ROBOFLOW_DIR.parent / s["file"]).convert("RGB")
            crop = src.crop((x, y, x + w, y + h)).resize((rect[2] - rect[0], rect[3] - rect[1]))
            crop, trk = label_box(crop, p["carrier"], layout_rng)
            canvas.paste(crop, rect[:2])
            if trk:
                tracking.append(trk)
        name = f"syn_{i:03d}.jpg"
        canvas.save(OUT_DIR / name, quality=92)
        items.append(
            {
                "id": f"synthetic_overlay:{name}",
                "source": "synthetic_overlay",
                "provenance": "synthetic",
                "media": [str((OUT_DIR / name).relative_to("data"))],
                "package_count": bg.base_count + p["pasted"],
                "carrier": p["carrier"] or "Unknown",
                "background": str(frame_path.relative_to("data")),
                "background_kind": bg.kind,
                "base_count": bg.base_count,
                "pasted_count": p["pasted"],
                "tracking_numbers": tracking,
                "box_sources": [s["file"] for s in chosen],
                "box_rects": [list(r) for r in rects],
                "layout_retries": retries,
            }
        )
    ITEMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ITEMS_FILE.write_text("".join(json.dumps(it) + "\n" for it in items))
    return items


def main() -> None:
    items = build()
    print(f"wrote {len(items)} composites to {OUT_DIR} and {ITEMS_FILE}")


if __name__ == "__main__":
    main()
