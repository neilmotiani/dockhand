"""Download the pinned Roboflow datasets for the extraction eval and list review candidates.

Each dataset version is exported in COCO format through the Roboflow REST API
(GET https://api.roboflow.com/{workspace}/{project}/{version}/coco?api_key=...), unzipped under
data/eval/roboflow/<key>/, and a hash of the extracted files is written to eval/labels/sources.json
so a later download can be checked against the one that was labelled. (The zip itself is not
hashed: re-exports carry new timestamps.)

Then every image is listed with its bounding-box counts, in the fixed review order from
eval/SELECTION.md (seeded shuffle), in data/eval/roboflow/<key>/candidates.jsonl. Review and
labelling happen afterwards and are recorded in eval/labels/.

Needs ROBOFLOW_API_KEY (in .env or the environment). The key is never printed or saved.

Usage:
    uv run python -m eval.build_roboflow            # all datasets
    uv run python -m eval.build_roboflow porch_piracy
"""

import argparse
import hashlib
import io
import json
import os
import random
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import dotenv_values

API = "https://api.roboflow.com"
DATA_DIR = Path("data/eval/roboflow")
SOURCES_FILE = Path("eval/labels/sources.json")
REVIEW_SEED = 20261016  # eval/SELECTION.md, source 1 rule 2


@dataclass(frozen=True)
class Dataset:
    key: str
    workspace: str
    project: str
    version: int
    license: str
    title: str
    author: str
    package_class: str = "package"

    @property
    def url(self) -> str:
        return f"https://universe.roboflow.com/{self.workspace}/{self.project}"


DATASETS = {
    d.key: d
    for d in [
        Dataset("porch_piracy", "gluwxyws", "porch-piracy", 8, "CC BY 4.0",
                "Porch Piracy Dataset", "gluwxyws"),
        Dataset("packages_pd", "joseph-nelson", "packages-g0ton", 5, "Public Domain",
                "Packages Dataset", "Roboflow Community"),
        # Box crops for the synthetic composites only; its images are not eval items.
        Dataset("mpj_boxes", "mpj", "package-detection-f03pa", 1, "CC BY 4.0",
                "Package Detection Dataset", "MPJ", package_class="Box"),
    ]
}  # fmt: skip


def api_key() -> str:
    key = os.environ.get("ROBOFLOW_API_KEY") or dotenv_values(".env").get("ROBOFLOW_API_KEY")
    if not key:
        sys.exit("ROBOFLOW_API_KEY is not set (add it to .env)")
    return key


def export_link(client: httpx.Client, ds: Dataset, key: str) -> str:
    """Ask for a COCO export. Roboflow may need to generate it first, so poll until a link."""
    url = f"{API}/{ds.workspace}/{ds.project}/{ds.version}/coco"
    for _ in range(30):
        r = client.get(url, params={"api_key": key})
        if r.status_code != 200:
            # The response body can echo request details; print status and error text only.
            err = r.json().get("error") if "json" in r.headers.get("content-type", "") else ""
            sys.exit(f"{ds.key}: export request failed with HTTP {r.status_code}: {err}")
        link = (r.json().get("export") or {}).get("link")
        if link:
            return link
        time.sleep(5)
    sys.exit(f"{ds.key}: export link not ready after polling")


def download(ds: Dataset, key: str) -> tuple[Path, str]:
    out = DATA_DIR / ds.key / f"v{ds.version}"
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        link = export_link(client, ds, key)
        blob = client.get(link).content
    out.mkdir(parents=True, exist_ok=True)
    zipfile.ZipFile(io.BytesIO(blob)).extractall(out)
    return out, content_digest(out)


def content_digest(root: Path) -> str:
    """sha256 over (relative path, file sha256) of every extracted file, in sorted order."""
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(root)).encode())
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def candidates(root: Path, ds: Dataset) -> list[dict]:
    """One row per image across all splits, with bounding-box counts per class."""
    rows = []
    for ann in sorted(root.glob("*/_annotations.coco.json")):
        coco = json.loads(ann.read_text())
        names = {c["id"]: c["name"] for c in coco["categories"]}
        counts: dict[int, dict[str, int]] = {}
        boxes: dict[int, list] = {}
        for a in coco["annotations"]:
            per = counts.setdefault(a["image_id"], {})
            per[names[a["category_id"]]] = per.get(names[a["category_id"]], 0) + 1
            boxes.setdefault(a["image_id"], []).append([*a["bbox"], names[a["category_id"]]])
        for img in coco["images"]:
            per = counts.get(img["id"], {})
            rows.append(
                {
                    "file": str((ann.parent / img["file_name"]).relative_to(DATA_DIR.parent)),
                    "split": ann.parent.name,
                    "width": img["width"],
                    "height": img["height"],
                    "bbox_count": per.get(ds.package_class, 0),
                    "other_boxes": {k: v for k, v in per.items() if k != ds.package_class},
                    "boxes": boxes.get(img["id"], []),  # [x, y, w, h, class] in pixels
                }
            )
    rows.sort(key=lambda r: r["file"])
    random.Random(REVIEW_SEED).shuffle(rows)
    for i, r in enumerate(rows):
        r["review_rank"] = i
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("keys", nargs="*", default=list(DATASETS), choices=list(DATASETS))
    args = p.parse_args()
    key = api_key()
    sources = json.loads(SOURCES_FILE.read_text()) if SOURCES_FILE.exists() else {}
    for k in args.keys:
        ds = DATASETS[k]
        root, digest = download(ds, key)
        rows = candidates(root, ds)
        (root.parent / "candidates.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        n_pos = sum(r["bbox_count"] > 0 for r in rows)
        print(f"{k}: v{ds.version}, {len(rows)} images ({n_pos} with a package box), {root}")
        prev = sources.get(k, {}).get("content_sha256")
        if prev and prev != digest:
            print(f"  WARNING: files differ from the labelled download ({prev[:12]}...)")
        sources[k] = {
            "title": ds.title,
            "author": ds.author,
            "url": ds.url,
            "version": ds.version,
            "license": ds.license,
            "images": len(rows),
            "content_sha256": prev or digest,
        }
    SOURCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SOURCES_FILE.write_text(json.dumps(sources, indent=2) + "\n")


if __name__ == "__main__":
    main()
