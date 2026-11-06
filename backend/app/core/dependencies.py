from fastapi import Request

from backend.app.core.security import require_same_origin, require_session


def require_admin(request: Request) -> None:
    require_same_origin(request)
    require_session(request, "admin")
