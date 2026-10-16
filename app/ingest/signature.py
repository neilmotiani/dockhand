"""Webhook signature verification.

Ring signs every webhook as `X-Signature: sha256=<hex HMAC-SHA256(key, raw body)>`, with the
HMAC Signature Key used as UTF-8 bytes (not Base64-decoded) and the body exactly as received
(Ring docs: Notifications, "HMAC Signature Verification").
"""

import hashlib
import hmac

PREFIX = "sha256="


def sign(key: str, body: bytes) -> str:
    return PREFIX + hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify(key: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith(PREFIX):
        return False
    # Hex case is not significant; Ring documents lowercase, so normalise before comparing.
    received = PREFIX + header[len(PREFIX) :].strip().lower()
    # compare_digest is constant-time in the length of the inputs, so a forged signature
    # cannot be found byte by byte from response timings.
    return hmac.compare_digest(sign(key, body).encode(), received.encode())
