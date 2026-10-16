"""Capture stage: resolve the device, record one WHEP clip, select frames."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.capture.selection import Selection, select_frames
from app.config import Settings
from app.ring.client import RingClient
from app.ring.devices import Device, pick_device
from app.ring.whep import Clip, capture_clip

DEVICE_CACHE_TTL = timedelta(minutes=5)


class CaptureError(RuntimeError):
    pass


class DeviceUnavailable(CaptureError):
    """Unknown or offline device. Not retried: the clip would be gone by the next attempt."""


@dataclass(frozen=True)
class CaptureResult:
    device: Device
    clip: Clip
    selection: Selection
    latency_ms: dict[str, float]


CaptureFn = Callable[..., Awaitable[Clip]]


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


class Capturer:
    def __init__(
        self,
        ring: RingClient,
        settings: Settings,
        *,
        capture_fn: CaptureFn = capture_clip,
        clock: Callable[[], datetime],
    ) -> None:
        self._ring = ring
        self._s = settings
        self._capture_fn = capture_fn
        self._clock = clock
        self._devices: list[Device] = []
        self._devices_at: datetime | None = None
        # One WHEP session per device at a time: two events from the same camera seconds apart
        # would otherwise open two sessions on the same clip.
        self._locks: dict[str, asyncio.Lock] = {}

    async def _list_devices(self) -> list[Device]:
        now = self._clock()
        if self._devices_at is None or now - self._devices_at > DEVICE_CACHE_TTL:
            self._devices = await self._ring.list_devices()
            self._devices_at = now
        return self._devices

    async def resolve(self, query: str | None, *, exact: bool) -> Device:
        """`exact` for webhook/poll events, which carry a device ID: falling back to "the only
        camera" there could capture a different device than the one that saw the event."""
        devices = await self._list_devices()
        if exact:
            device = next((d for d in devices if d.id == query), None)
            if device is None:
                raise DeviceUnavailable(f"device {query!r} is not in the device list")
        else:
            try:
                # Cameras only: "back door" should find the camera, not its contact sensor.
                device = pick_device([d for d in devices if d.is_camera], query)
            except LookupError as e:
                raise DeviceUnavailable(str(e)) from None
        if not device.is_camera:
            raise DeviceUnavailable(f"{device.name!r} has no camera")
        # Ring answers WHEP for an offline device with 201 and then sends no media
        # (FRICTION_LOG.md), so fail before opening a session.
        if device.online is False:
            raise DeviceUnavailable(f"{device.name!r} is offline")
        return device

    def _reference(self, device_id: str) -> bytes | None:
        if self._s.capture_reference_dir is None:
            return None
        path = Path(self._s.capture_reference_dir) / f"{device_id}.jpg"
        return path.read_bytes() if path.is_file() else None

    async def capture(self, query: str | None, *, exact: bool) -> CaptureResult:
        t = time.perf_counter()
        device = await self.resolve(query, exact=exact)
        resolve_ms = _ms(t)
        async with self._locks.setdefault(device.id, asyncio.Lock()):
            clip = await self._capture_fn(
                self._ring,
                device.id,
                stall_s=self._s.capture_stall_s,
                timeout_s=self._s.capture_timeout_s,
            )
        if len(clip.frames) < 2:
            raise CaptureError(
                f"WHEP session on {device.name!r} returned {len(clip.frames)} frames"
            )
        t = time.perf_counter()
        selection = await asyncio.to_thread(
            select_frames,
            clip.frames,
            n_sample=self._s.capture_sample_frames,
            k=self._s.capture_select_frames,
            reference=self._reference(device.id),
            sharpness_weight=self._s.capture_sharpness_weight,
        )
        return CaptureResult(
            device=device,
            clip=clip,
            selection=selection,
            latency_ms={
                "device_resolve": resolve_ms,
                "whep_setup": clip.setup_ms or 0.0,
                "capture": clip.total_ms,
                "select": _ms(t),
            },
        )
