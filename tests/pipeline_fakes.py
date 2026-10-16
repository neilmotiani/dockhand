"""Fakes shared by the pipeline and idempotency tests: no WHEP, no Bedrock."""

import io
import itertools
import uuid
from datetime import UTC, datetime, timedelta

from PIL import Image
from sqlalchemy import insert

from app.capture.selection import ScoredFrame, Selection
from app.capture.service import CaptureResult, DeviceUnavailable
from app.config import Settings
from app.db.models import expected_deliveries
from app.extract.client import ExtractionOutcome, Usage
from app.extract.schemas import ExtractionResult
from app.pipeline import Pipeline
from app.ring.devices import Device
from app.ring.whep import Clip

T0 = datetime(2026, 10, 16, 17, 0, tzinfo=UTC)  # 10:00 in America/Los_Angeles
CAM = Device(id="ava1.ring.device.TEST", name="Back Door", online=True, codecs=["AVC"])

_counter = itertools.count()


def tiny_jpeg(seed: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (seed % 256, (seed // 256) % 256, 7)).save(buf, format="JPEG")
    return buf.getvalue()


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class FakeCapturer:
    def __init__(self, unavailable: set[str] | None = None, same_frames: bool = False) -> None:
        self.calls: list[tuple[str | None, bool]] = []
        self.unavailable = unavailable or set()
        self.same_frames = same_frames

    async def capture(self, query: str | None, *, exact: bool) -> CaptureResult:
        self.calls.append((query, exact))
        if query in self.unavailable:
            raise DeviceUnavailable(f"{query} is offline")
        seed = 0 if self.same_frames else next(_counter)
        frames = [
            ScoredFrame(
                index=i + 1,
                t=0.5 * i,
                jpeg=tiny_jpeg(seed * 10 + i),
                sharpness=1.0,
                change=1.0,
                score=0.1 * i,
            )
            for i in range(3)
        ]
        sel = Selection(frames=frames, decoded=40, sampled=10)
        return CaptureResult(
            device=CAM,
            clip=Clip(frames=[], setup_ms=250.0, total_ms=6000.0),
            selection=sel,
            latency_ms={
                "device_resolve": 1.0,
                "whep_setup": 250.0,
                "capture": 6000.0,
                "select": 12.0,
            },
        )


class FakeExtractor:
    model_id = "fake-model"
    prompt_version = "v1"
    structured_output = False

    def __init__(self, count: int = 2, carrier: str = "UPS") -> None:
        self.count = count
        self.carrier = carrier
        self.calls = 0

    def extract(self, images) -> ExtractionOutcome:
        self.calls += 1
        assert images and all(fmt == "jpeg" for _, fmt in images)
        return ExtractionOutcome(
            result=ExtractionResult(
                reasoning="two brown boxes, driver in UPS uniform",
                carrier=self.carrier,
                package_count=self.count,
                label_text=["1Z999"],
                confidence=0.9,
            ),
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            attempts=1,
            usage=Usage(input_tokens=1000, output_tokens=50),
            latency_ms=900.0,
            bedrock_latency_ms=850,
            structured_output=False,
        )


def make_pipeline(engine, tmp_path, clock, capturer=None, extractor=None, **overrides):
    settings = Settings(_env_file=None, media_dir=tmp_path / "media", **overrides)
    capturer = capturer or FakeCapturer()
    extractor = extractor or FakeExtractor()
    return Pipeline(engine, settings, capturer, extractor, clock=clock), capturer, extractor


async def add_expected(
    engine,
    supplier="Sysco",
    carrier="UPS",
    count=2,
    start=T0 - timedelta(hours=1),
    end=T0 + timedelta(hours=1),
) -> uuid.UUID:
    eid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            insert(expected_deliveries).values(
                id=eid,
                supplier=supplier,
                carrier=carrier,
                expected_count=count,
                window_start=start,
                window_end=end,
            )
        )
    return eid
