"""Invitation mockup designer: validation of the design a customer previews before ordering.

The mockup is drawn in the browser by ``frontend/assets/mockup.js`` (the same file draws it again in the
admin order details and the customer portal). The server only accepts a known template for the
invitation category, short custom texts, and a colour variant, and stores a snapshot of the event
details shown on the card. Mockups exist only for Birthday, Wedding, and Christening invitations.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from backend.app.models.catalog import CatalogProduct


# Keep in sync with TEMPLATES in frontend/assets/mockup.js.
TEMPLATES: dict[str, dict[str, str]] = {
    "birthday": {"confetti": "Confetti Pop", "balloons": "Balloon Party", "starry": "Starry Night"},
    "wedding": {"classic": "Classic Elegance", "botanical": "Botanical Greenery", "monogram": "Modern Monogram"},
    "christening": {"clouds": "Soft Clouds", "dove": "Peace Dove", "floral": "Pastel Florals"},
}
PALETTES = 3
KIND_LABELS = {"birthday": "Birthday", "wedding": "Wedding", "christening": "Christening"}
TEXT_LIMITS = {"headline": 80, "message": 300}


def invitation_kind(product: CatalogProduct | dict[str, Any]) -> str | None:
    """birthday / wedding / christening for the three invitation products, otherwise None."""
    if not any(field.get("section") == "event" for field in product.get("fields", [])):
        return None
    text = f"{product.get('id', '')} {product.get('name', '')}".lower()
    if "wedding" in text:
        return "wedding"
    if "christening" in text or "baptism" in text:
        return "christening"
    if "birthday" in text:
        return "birthday"
    return None


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _short_text(value: Any, label: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _bad(f"Mockup {label} must be text.")
    value = value.strip()
    if len(value) > limit:
        raise _bad(f"Mockup {label} must be {limit} characters or fewer.")
    return value


def _detail(selections: dict[str, Any], key: str) -> str:
    value = selections.get(key)
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def clean_mockup(product: CatalogProduct, raw: Any, selections: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the submitted mockup. Returns None when the customer did not design one."""
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise _bad("The invitation mockup is invalid.")
    kind = invitation_kind(product)
    if kind is None:
        raise _bad("The mockup designer is only available for Birthday, Wedding, and Christening invitations.")
    template = raw.get("template")
    if not isinstance(template, str) or template not in TEMPLATES[kind]:
        raise _bad(f"Choose one of the {KIND_LABELS[kind].lower()} invitation templates for your mockup.")
    palette = raw.get("palette", 0)
    if isinstance(palette, bool) or not isinstance(palette, int) or not 0 <= palette < PALETTES:
        raise _bad("Choose one of the mockup colour options.")
    if kind == "wedding":
        names = " & ".join(part for part in (_detail(selections, "bride_name"), _detail(selections, "groom_name")) if part)
    else:
        names = _detail(selections, "celebrant_name")
    return {
        "kind": kind,
        "template": template,
        "template_name": TEMPLATES[kind][template],
        "palette": palette,
        "headline": _short_text(raw.get("headline"), "headline", TEXT_LIMITS["headline"]),
        "message": _short_text(raw.get("message"), "message", TEXT_LIMITS["message"]),
        # Snapshot of the event details printed on the mockup, taken from the validated order form.
        "details": {
            "names": names,
            "age": _detail(selections, "celebrant_age"),
            "date": _detail(selections, "event_date"),
            "time": _detail(selections, "event_time"),
            "location": _detail(selections, "event_location"),
            "rsvp": _detail(selections, "rsvp_contact"),
        },
    }
