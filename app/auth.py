"""Admin bearer-token check shared by the operator endpoints (trigger, expected deliveries)."""

import hmac

from fastapi import Header, HTTPException, Request


async def require_admin(request: Request, authorization: str | None = Header(default=None)) -> None:
    """Require `Authorization: Bearer $ADMIN_TOKEN`. With ADMIN_TOKEN unset the admin endpoints
    do not exist (404), so a deployment without a token exposes nothing."""
    admin = request.app.state.settings.admin_token
    if admin is None:
        raise HTTPException(404, "admin endpoints are disabled (ADMIN_TOKEN is unset)")
    expected = f"Bearer {admin.get_secret_value()}"
    if not hmac.compare_digest((authorization or "").encode(), expected.encode()):
        raise HTTPException(401, "invalid admin token")
