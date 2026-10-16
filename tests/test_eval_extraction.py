import json
import random
from pathlib import Path

import pytest
from PIL import Image

from app.config import Settings
from app.extract.client import ExtractionOutcome, Usage
from app.extract.schemas import ExtractionResult
from eval import build_roboflow, build_synthetic, extraction

PRICES = {
    "us.amazon.nova-pro-v1:0": {"input_per_mtok": 0.80, "output_per_mtok": 3.20},
    "us.anthropic.claude-sonnet-4-6": {"input_per_mtok": 3.30, "output_per_mtok": 16.50},
}


def item(i: str, count: int, carrier: str | None = None, source: str = "s") -> dict:
    return {"id": i, "source": source, "provenance": "p", "media": ["x.jpg"],
            "package_count": count, "carrier": carrier}  # fmt: skip


def pred(count: int | None, carrier: str = "Unknown", ms: float = 100.0, usd: float = 0.01):
    if count is None:
        return {"ok": False, "error": "bad", "latency_ms": ms, "usd": None}
    return {"ok": True, "latency_ms": ms, "usd": usd,
            "result": {"package_count": count, "carrier": carrier}}  # fmt: skip


# --- extraction eval ----------------------------------------------------------------------------


def test_cost_uses_per_million_rates() -> None:
    assert extraction.cost(PRICES, "us.amazon.nova-pro-v1:0", 6733, 66) == pytest.approx(0.0055976)
    sonnet = extraction.cost(PRICES, "us.anthropic.claude-sonnet-4-6", 4175, 99)
    assert sonnet == pytest.approx(0.0154110, abs=1e-6)


def test_estimate_is_an_upper_bound_on_the_smoke_test() -> None:
    # 2026-10-15 smoke test, 3 frames per call: real costs were 0.0056 (Nova) and 0.0154 (Sonnet).
    assert extraction.estimate(PRICES, "us.amazon.nova-pro-v1:0", 3) > 0.0056
    assert extraction.estimate(PRICES, "us.anthropic.claude-sonnet-4-6", 3) > 0.0154


def test_summarize_counts_negatives_carrier_and_failures() -> None:
    rows = [
        {"item": item("a", 2, "UPS"), "pred": pred(2, "UPS", ms=100)},
        {"item": item("b", 3, "FedEx"), "pred": pred(1, "UPS", ms=200)},
        {"item": item("c", 0), "pred": pred(1, ms=300)},  # false alarm
        {"item": item("d", 0), "pred": pred(0, ms=400)},
        {"item": item("e", 1, "DHL"), "pred": pred(None, ms=500)},  # failure counts as wrong
    ]
    s = extraction.summarize(rows)
    assert s["n"] == 5
    assert (s["count_exact"]["k"], s["count_exact"]["n"]) == (2, 5)
    assert s["count_mae"] == pytest.approx((0 + 2 + 1 + 0) / 4)
    assert (s["negatives_false_alarm"]["k"], s["negatives_false_alarm"]["n"]) == (1, 2)
    assert (s["carrier_accuracy"]["k"], s["carrier_accuracy"]["n"]) == (1, 3)
    assert s["failures"] == 1
    assert s["latency_ms_p50"] == 300.0
    assert s["usd_total"] == pytest.approx(0.04)


def test_percentile_interpolates() -> None:
    assert extraction.percentile([], 0.5) is None
    assert extraction.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert extraction.percentile([10, 20, 30, 40, 50], 0.95) == 48.0


def test_load_items_rejects_duplicates_and_missing_fields(tmp_path: Path) -> None:
    (tmp_path / "items_a.jsonl").write_text(json.dumps(item("x", 1)) + "\n")
    (tmp_path / "items_b.jsonl").write_text(json.dumps(item("y", 0)) + "\n")
    assert [i["id"] for i in extraction.load_items(tmp_path)] == ["x", "y"]
    (tmp_path / "items_c.jsonl").write_text(json.dumps(item("x", 2)) + "\n")
    with pytest.raises(ValueError, match="duplicate"):
        extraction.load_items(tmp_path)
    (tmp_path / "items_c.jsonl").write_text(json.dumps({"id": "z"}) + "\n")
    with pytest.raises(ValueError, match="missing"):
        extraction.load_items(tmp_path)


def test_model_settings_pairs_default_with_comparison() -> None:
    default, comparison = extraction.model_settings(Settings(_env_file=None))
    assert (default.bedrock_model_id, default.bedrock_structured_output) == (
        "us.amazon.nova-pro-v1:0",
        False,
    )
    assert (comparison.bedrock_model_id, comparison.bedrock_structured_output) == (
        "us.anthropic.claude-sonnet-4-6",
        True,
    )


def test_cache_path_separates_model_prompt_and_mode() -> None:
    s = Settings(_env_file=None)
    a = extraction.cache_path(s, "abc")
    b = extraction.cache_path(s.model_copy(update={"bedrock_structured_output": True}), "abc")
    c = extraction.cache_path(s.model_copy(update={"extraction_prompt_version": "v2"}), "abc")
    assert len({a, b, c}) == 3
    assert ":" not in str(a)


class _FakeExtractor:
    model_id = "us.amazon.nova-pro-v1:0"

    def __init__(self, fail: bool = False):
        self.fail = fail

    def extract(self, images):
        if self.fail:
            raise extraction.ExtractionError("invalid model output")
        return ExtractionOutcome(
            result=ExtractionResult(reasoning="r", carrier="UPS", package_count=2,
                                    label_text=[], confidence=0.9),  # fmt: skip
            model_id=self.model_id, prompt_version="v1", attempts=1,
            usage=Usage(input_tokens=1000, output_tokens=50), latency_ms=12.0,
            bedrock_latency_ms=10, structured_output=False,
        )  # fmt: skip


def test_call_records_cost_or_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(extraction, "DATA", tmp_path)
    Image.new("RGB", (8, 8)).save(tmp_path / "x.jpg")
    ok = extraction.call(_FakeExtractor(), item("a", 2), PRICES)
    assert ok["ok"] and ok["result"]["package_count"] == 2
    assert ok["usd"] == pytest.approx(1000 * 0.8e-6 + 50 * 3.2e-6)
    bad = extraction.call(_FakeExtractor(fail=True), item("a", 2), PRICES)
    assert bad == {"ok": False, "error": "invalid model output", "latency_ms": bad["latency_ms"],
                   "usd": None}  # fmt: skip


# --- Roboflow download helpers ------------------------------------------------------------------


def _fake_export(root: Path) -> None:
    for split, imgs in {"train": [("a.jpg", 2), ("b.jpg", 0)], "valid": [("c.jpg", 1)]}.items():
        d = root / split
        d.mkdir(parents=True)
        coco = {
            "categories": [{"id": 0, "name": "packages"}, {"id": 1, "name": "package"},
                           {"id": 2, "name": "person"}],
            "images": [{"id": i, "file_name": f, "width": 100, "height": 80}
                       for i, (f, _) in enumerate(imgs)],
            "annotations": [{"image_id": i, "category_id": 1, "bbox": [1, 2, 3, 4]}
                            for i, (_, n) in enumerate(imgs) for _ in range(n)]
            + [{"image_id": 1, "category_id": 2, "bbox": [5, 5, 5, 5]}] if split == "train" else
            [{"image_id": 0, "category_id": 1, "bbox": [1, 2, 3, 4]}],
        }  # fmt: skip
        (d / "_annotations.coco.json").write_text(json.dumps(coco))
        for f, _ in imgs:
            (d / f).write_bytes(f.encode())


def test_candidates_counts_boxes_and_uses_fixed_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(build_roboflow, "DATA_DIR", tmp_path / "roboflow")
    root = tmp_path / "roboflow" / "porch_piracy" / "v8"
    _fake_export(root)
    ds = build_roboflow.DATASETS["porch_piracy"]
    rows = build_roboflow.candidates(root, ds)
    by_name = {Path(r["file"]).name: r for r in rows}
    assert by_name["a.jpg"]["bbox_count"] == 2
    assert by_name["b.jpg"]["bbox_count"] == 0
    assert by_name["b.jpg"]["other_boxes"] == {"person": 1}
    assert by_name["c.jpg"]["bbox_count"] == 1
    assert [r["review_rank"] for r in rows] == [0, 1, 2]
    assert [r["file"] for r in rows] == [r["file"] for r in build_roboflow.candidates(root, ds)]


def test_content_digest_ignores_nothing_but_order(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"1")
    (tmp_path / "b").write_bytes(b"2")
    first = build_roboflow.content_digest(tmp_path)
    assert build_roboflow.content_digest(tmp_path) == first
    (tmp_path / "b").write_bytes(b"3")
    assert build_roboflow.content_digest(tmp_path) != first


# --- synthetic composites -----------------------------------------------------------------------


def test_place_never_overlaps_and_stays_in_regions() -> None:
    bg = build_synthetic.BACKGROUNDS[0]
    rng = random.Random(1)
    for _ in range(50):
        rects = build_synthetic.place(rng, bg, [(150, 100)] * 5)
        if rects is None:
            continue
        for i, a in enumerate(rects):
            assert any(r[0] <= a[0] and r[1] <= a[1] and a[2] <= r[2] and a[3] <= r[3]
                       for r in bg.regions)  # fmt: skip
            assert not any(build_synthetic.overlaps(a, o) for o in bg.obstacles)
            for b in rects[i + 1 :]:
                assert not build_synthetic.overlaps(a, b, build_synthetic.MARGIN)


def test_place_gives_up_when_boxes_cannot_fit() -> None:
    bg = build_synthetic.BACKGROUNDS[0]
    assert build_synthetic.place(random.Random(1), bg, [(2000, 100)]) is None


def test_plan_is_balanced_and_deterministic() -> None:
    a = build_synthetic.plan(random.Random(build_synthetic.SEED))
    b = build_synthetic.plan(random.Random(build_synthetic.SEED))
    assert [(p["frame"], p["carrier"], p["pasted"]) for p in a] == [
        (p["frame"], p["carrier"], p["pasted"]) for p in b
    ]
    assert len(a) == build_synthetic.N_IMAGES
    per_carrier = {c: sum(p["carrier"] == c for p in a) for c in build_synthetic.CARRIERS}
    assert max(per_carrier.values()) - min(per_carrier.values()) <= 1
    assert {p["pasted"] for p in a} == set(build_synthetic.COUNTS)


def test_label_box_draws_only_when_carrier_known() -> None:
    sprite = Image.new("RGB", (160, 110), (180, 140, 100))
    same, trk = build_synthetic.label_box(sprite, None, random.Random(0))
    assert trk is None and same is sprite
    labelled, trk = build_synthetic.label_box(sprite, "FedEx", random.Random(0))
    assert trk.startswith("FE ")
    assert labelled.tobytes() != sprite.tobytes()
