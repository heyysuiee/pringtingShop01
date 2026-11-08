from __future__ import annotations

from functools import lru_cache
from pathlib import Path


ASSETS_DIR = Path(__file__).resolve().parents[3] / "frontend" / "assets"
STATIC_PREFIX = "/frontend/image/"


@lru_cache(maxsize=512)
def optimized_image(path: str) -> str:
    """Return the web-optimized copy of a bundled product photo, when one exists.

    The bundled product photos were originally 2–4 MB PNG files. Smaller JPEG copies sit next to
    them; catalog rows and older orders that still store the ``.png`` path are served the JPEG.
    Uploaded images and any other path are returned unchanged.
    """
    if not isinstance(path, str) or not path.startswith(STATIC_PREFIX) or not path.lower().endswith(".png"):
        return path
    relative = path[len("/frontend/"):]
    candidate = (ASSETS_DIR / relative).with_suffix(".jpg")
    return path[:-4] + ".jpg" if candidate.is_file() else path
