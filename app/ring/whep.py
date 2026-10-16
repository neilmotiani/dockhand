"""Capture one clip from a Ring camera over WHEP (WebRTC live view) with aiortc.

Behaviour observed in the Playground spike (scripts/ring_spike.py, FRICTION_LOG.md):
- A simulated event plays as a short clip (1.5–26 s) and then RTP simply stops while ICE stays
  connected, so the end of a clip is detected as a gap of `stall_s` with no frames.
- An offline device still gets 201 and a connected peer but never sends media, so callers
  check `online` from device discovery first.
- Battery cameras end WHEP sessions after 30 s, so the whole capture must fit inside that.
"""

import asyncio
import io
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

from app.ring.client import RingClient

STUN = "stun:stun.l.google.com:19302"

# (seconds since the first frame, JPEG bytes)
Frame = tuple[float, bytes]


def _jpeg(frame) -> bytes:
    buf = io.BytesIO()
    frame.to_image().save(buf, format="JPEG", quality=92)
    return buf.getvalue()


async def buffer_until_stall(
    track,
    stall_s: float,
    buf: list[Frame],
    should_stop: Callable[[float, bytes], bool] | None = None,
) -> str:
    """Decode every frame into buf as (seconds since first frame, JPEG bytes) until no frame
    arrives for stall_s, or `should_stop(t, jpeg)` says so. Before the first frame it waits
    indefinitely (bounded by the caller). Returns why it stopped: "stalled" or "settled"."""
    t_first = None
    while True:
        try:
            if t_first is None:
                frame = await track.recv()
            else:
                frame = await asyncio.wait_for(track.recv(), timeout=stall_s)
        except (TimeoutError, MediaStreamError):
            return "stalled"
        now = time.monotonic()
        t_first = t_first if t_first is not None else now
        # JPEG-encode now: ~300 raw 720p frames would hold ~400 MB of decoded YUV.
        t, jpeg = now - t_first, await asyncio.to_thread(_jpeg, frame)
        buf.append((t, jpeg))
        # The detector decodes a frame every 0.5 s; keep that off the event loop too.
        if should_stop is not None and await asyncio.to_thread(should_stop, t, jpeg):
            return "settled"


@dataclass
class Clip:
    frames: list[Frame] = field(default_factory=list)
    # Offer creation, POST whep/sessions and ICE/DTLS until the video track arrives.
    setup_ms: float | None = None
    total_ms: float = 0.0
    timed_out: bool = False
    # "settled" (scene stopped changing), "stalled" (RTP stopped) or "timeout".
    end_reason: str | None = None


def default_peer_connection() -> RTCPeerConnection:
    return RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=STUN)]))


async def capture_clip(
    ring: RingClient,
    device_id: str,
    *,
    stall_s: float = 2.0,
    timeout_s: float = 25.0,
    track_timeout_s: float = 10.0,
    pc_factory: Callable[[], RTCPeerConnection] = default_peer_connection,
    should_stop: Callable[[float, bytes], bool] | None = None,
) -> Clip:
    """Open a WHEP session, decode until the clip stalls, the scene settles (`should_stop`)
    or timeout_s passes, and always close it."""
    start = time.perf_counter()
    clip = Clip()
    pc = pc_factory()
    track_ready: asyncio.Future = asyncio.get_running_loop().create_future()

    # Handlers go on before the offer is created (the WHEP docs call a late `ontrack` the most
    # common cause of "connected but no video").
    @pc.on("track")
    def on_track(track) -> None:
        if track.kind == "video" and not track_ready.done():
            track_ready.set_result(track)

    session_url = None
    try:
        pc.addTransceiver("video", direction="recvonly")
        # aiortc finishes ICE gathering inside setLocalDescription, so the offer is complete.
        await pc.setLocalDescription(await pc.createOffer())
        answer, session_url = await ring.whep_create(device_id, pc.localDescription.sdp)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        track = await asyncio.wait_for(track_ready, timeout=track_timeout_s)
        clip.setup_ms = round((time.perf_counter() - start) * 1000, 1)
        try:
            clip.end_reason = await asyncio.wait_for(
                buffer_until_stall(track, stall_s, clip.frames, should_stop), timeout=timeout_s
            )
        except TimeoutError:
            # Not an error: a stream that keeps going (a real camera) is cut at timeout_s.
            clip.timed_out = True
            clip.end_reason = "timeout"
    finally:
        await pc.close()
        if session_url:
            try:
                await ring.whep_delete(session_url)
            except Exception:
                pass  # the session expires on Ring's side anyway (30/60 s)
        clip.total_ms = round((time.perf_counter() - start) * 1000, 1)
    return clip
