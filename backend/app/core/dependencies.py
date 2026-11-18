from typing import Any

from fastapi import Request

from backend.app.core.security import require_same_origin, require_session


def require_admin(request: Request) -> dict[str, Any]:
    """Require an administrator session (same origin) and return its claims (``sub`` is the admin email)."""
    require_same_origin(request)
    return require_session(request, "admin")
