"""Server-side Ring (Amazon Vision) API client. Paths and shapes follow the Ring API docs;
recorded responses live in tests/fixtures/ring/."""

from typing import Any
from urllib.parse import urljoin

import httpx

from app.ring.devices import Device, summarize_devices
from app.ring.tokens import TokenProvider


class RingAPIError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} -> {status}: {body[:300]}")
        self.status = status


class RingAuthError(RingAPIError):
    """401: the token expired (Playground tokens last ~30 min) or was revoked."""


class RingClient:
    def __init__(self, http: httpx.AsyncClient, tokens: TokenProvider) -> None:
        # `http` carries base_url and timeouts; the token is added per request so a provider
        # can rotate it.
        self._http = http
        self._tokens = tokens

    @property
    def base_url(self) -> str:
        return str(self._http.base_url).rstrip("/")

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        token = await self._tokens.token()
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {token}"}
        r = await self._http.request(method, url, headers=headers, **kwargs)
        if r.status_code == 401:
            await self._tokens.invalidate()
            raise RingAuthError(method, url, r.status_code, r.text)
        return r

    async def list_devices(self) -> list[Device]:
        path = "/v1/devices"
        r = await self._request("GET", path, params={"include": "status,capabilities"})
        if r.status_code != 200:
            raise RingAPIError("GET", path, r.status_code, r.text)
        return summarize_devices(r.json())

    async def history_events(self, device_id: str, event_types: str) -> list[dict]:
        """First page of Event History (newest first). The poller runs often enough that one
        page covers the gap since the last poll; dedupe makes overlap harmless."""
        path = f"/v1/history/devices/{device_id}/events"
        r = await self._request("GET", path, params={"event_types": event_types})
        if r.status_code != 200:
            raise RingAPIError("GET", path, r.status_code, r.text)
        return r.json().get("data") or []

    async def whep_create(self, device_id: str, offer_sdp: str) -> tuple[str, str]:
        """POST an SDP offer; return (answer SDP, absolute session URL).

        The session URL is the Location header used verbatim: it carries an undocumented
        `?location=` query that a URL rebuilt from IDs would drop (FRICTION_LOG.md).
        """
        path = f"/v1/devices/{device_id}/media/streaming/whep/sessions"
        r = await self._request(
            "POST", path, content=offer_sdp, headers={"Content-Type": "application/sdp"}
        )
        if r.status_code != 201:
            raise RingAPIError("POST", path, r.status_code, r.text)
        location = r.headers.get("location")
        if not location:
            raise RingAPIError("POST", path, r.status_code, "no Location header")
        return r.text, urljoin(self.base_url + "/", location)

    async def whep_delete(self, session_url: str) -> int:
        r = await self._request("DELETE", session_url)
        return r.status_code
