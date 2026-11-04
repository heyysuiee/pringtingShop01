from fastapi import Request

from backend.app.code.security import require_same_origin, require_session


def require_local_admin(request: Request) -> None:
    require_same_origin(request)
    require_session(request, "admin")
