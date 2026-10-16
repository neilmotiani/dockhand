"""Contact sheets for reviewing Roboflow candidates by eye (eval/SELECTION.md).

Draws each candidate with its bounding boxes (package boxes in yellow, other classes in cyan) and a
caption with its review rank and box count, 3 x 3 per sheet so counts stay readable.

Usage:
    uv run python -m eval.review_sheet porch_piracy 0 9     # ranks 0-8
"""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

from eval.build_roboflow import DATA_DIR, DATASETS

SHEET_DIR = Path("data/eval/review")
CELL_W, CELL_H, COLS = 640, 480, 3


def render(row: dict, package_class: str) -> Image.Image:
    img = Image.open(DATA_DIR.parent / row["file"]).convert("RGB")
    d = ImageDraw.Draw(img)
    width = max(2, img.width // 300)
    for x, y, w, h, cls in row["boxes"]:
        d.rectangle([x, y, x + w, y + h], outline="yellow" if cls == package_class else "cyan",
                    width=width)  # fmt: skip
    img.thumbnail((CELL_W, CELL_H - 24))
    cell = Image.new("RGB", (CELL_W, CELL_H), "black")
    cell.paste(img, ((CELL_W - img.width) // 2, 24))
    caption = f"#{row['review_rank']}  boxes={row['bbox_count']}  {Path(row['file']).name[:48]}"
    ImageDraw.Draw(cell).text((6, 4), caption, fill="white")
    return cell


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("key", choices=list(DATASETS))
    p.add_argument("start", type=int)
    p.add_argument("count", type=int)
    args = p.parse_args()
    ds = DATASETS[args.key]
    rows = [json.loads(line) for line in (DATA_DIR / ds.key / "candidates.jsonl").open()]
    rows = rows[args.start : args.start + args.count]
    rows_n = (len(rows) + COLS - 1) // COLS
    sheet = Image.new("RGB", (CELL_W * COLS, CELL_H * rows_n), "black")
    for i, row in enumerate(rows):
        sheet.paste(render(row, ds.package_class), ((i % COLS) * CELL_W, (i // COLS) * CELL_H))
    SHEET_DIR.mkdir(parents=True, exist_ok=True)
    out = SHEET_DIR / f"{ds.key}_{args.start:04d}.jpg"
    sheet.save(out, quality=85)
    print(out)


if __name__ == "__main__":
    main()
