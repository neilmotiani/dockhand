"""Device discovery helpers (GET /v1/devices?include=status,capabilities)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    online: bool | None
    codecs: list[str] | None

    @property
    def is_camera(self) -> bool:
        # Sensors share the device collection but have no media; a null `video` capability
        # marks them.
        return bool(self.codecs)


def summarize_devices(doc: dict) -> list[Device]:
    """Flatten the JSON:API compound document into one Device per entry.

    Related resources are matched by (type, id) from relationships into `included`, as the
    Device Discovery docs describe.
    """
    included = {(r["type"], r["id"]): r.get("attributes", {}) for r in doc.get("included", [])}
    out = []
    for d in doc.get("data", []):
        rels = d.get("relationships", {})

        def rel_attrs(name: str, rels: dict = rels) -> dict:
            ref = (rels.get(name) or {}).get("data") or {}
            return included.get((ref.get("type"), ref.get("id")), {})

        video = rel_attrs("capabilities").get("video") or {}
        out.append(
            Device(
                id=d["id"],
                name=(d.get("attributes") or {}).get("name", ""),
                online=rel_attrs("status").get("online"),
                codecs=video.get("codecs"),
            )
        )
    return out


def pick_device(devices: list[Device], query: str) -> Device:
    """Exact ID match first, then case-insensitive name substring, then the only camera."""
    for d in devices:
        if d.id == query:
            return d
    q = query.lower()
    by_name = [d for d in devices if q in d.name.lower()]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        raise LookupError(f"{len(by_name)} devices match {query!r}: {[d.name for d in by_name]}")
    cameras = [d for d in devices if d.is_camera]
    if len(cameras) == 1:
        return cameras[0]
    raise LookupError(f"no device matches {query!r}; pass a device name or ID")
