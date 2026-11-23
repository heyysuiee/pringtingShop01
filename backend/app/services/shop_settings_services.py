"""Administrator-configured shop settings: e-wallet payment methods and fulfillment (delivery and pickup).

* **Payment methods** (``payment_methods`` table): provider, merchant/account name, account number, QR code,
  instructions, active flag, display order. Customers see only active methods; a customer payment must
  name an active method. Historical payments keep the provider name they were made with, so editing or
  removing a method never changes recorded payments.
* **Fulfillment settings** (``app_meta`` key ``shop.fulfillment_settings``): whether delivery is offered,
  delivery areas with their fee and estimated courier delivery period, couriers (J&T Express by default),
  and pickup locations. Delivery fees are always calculated here, on the server.

Estimated delivery periods are courier estimates, not guaranteed arrival dates, and they never mark an
order delivered. They are separate from the shop's estimated production completion date.
"""
from __future__ import annotations

import json
import math
import re
import secrets
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException
from fastapi.responses import Response

from backend.app.core.config import settings as app_settings
from backend.app.database.connection import connection_scope
from backend.app.repositories import payment_method_repository
from backend.app.services import file_services, location_services, payment_verification_services


SETTINGS_KEY = "shop.fulfillment_settings"
MAX_QR_BYTES = 3 * 1024 * 1024
QR_TYPES = {"image/png", "image/jpeg", "image/webp"}
DISCLAIMER = (
    "Estimated delivery periods are courier estimates, not guaranteed arrival dates. Actual delivery may vary "
    "depending on courier operations, location, weather, holidays, and other circumstances."
)
REGIONS = {"luzon": "Luzon", "visayas": "Visayas", "mindanao": "Mindanao"}
# Provinces outside Luzon, for working out the island group of a delivery address. Every other province
# (Metro Manila, Palawan, Masbate, Romblon, ...) is in Luzon.
VISAYAS_PROVINCES = {
    "aklan", "antique", "capiz", "guimaras", "iloilo", "negros occidental", "negros oriental", "bohol", "cebu",
    "siquijor", "biliran", "eastern samar", "leyte", "northern samar", "samar", "western samar", "southern leyte",
    "negros island region",
}
MINDANAO_PROVINCES = {
    "zamboanga del norte", "zamboanga del sur", "zamboanga sibugay", "bukidnon", "camiguin", "lanao del norte",
    "misamis occidental", "misamis oriental", "davao de oro", "compostela valley", "davao del norte",
    "davao del sur", "davao occidental", "davao oriental", "cotabato", "north cotabato", "sarangani",
    "south cotabato", "sultan kudarat", "agusan del norte", "agusan del sur", "dinagat islands",
    "surigao del norte", "surigao del sur", "basilan", "lanao del sur", "maguindanao", "maguindanao del norte",
    "maguindanao del sur", "sulu", "tawi-tawi", "tawi tawi",
}
DEFAULT_SETTINGS: dict[str, Any] = {
    "delivery_enabled": True,
    # Fees start at 0 because the shop has not published delivery fees; set them in Admin → Settings.
    "areas": [
        {"id": "luzon", "label": "Luzon (including Metro Manila)", "region": "luzon", "provinces": [], "fee": 0.0,
         "estimate_min_days": 3, "estimate_max_days": 7, "active": True},
        {"id": "visayas", "label": "Visayas", "region": "visayas", "provinces": [], "fee": 0.0,
         "estimate_min_days": 5, "estimate_max_days": 10, "active": True},
        {"id": "mindanao", "label": "Mindanao", "region": "mindanao", "provinces": [], "fee": 0.0,
         "estimate_min_days": None, "estimate_max_days": None, "active": True},
    ],
    "couriers": [
        # No live J&T tracking API is integrated: tracking numbers are entered by the administrator. Add a
        # tracking page address with {tracking} (for example the courier's tracking page) to show a link.
        {"id": "jnt", "name": "J&T Express", "service": "Standard", "tracking_url": "", "active": True},
    ],
    "default_courier": "jnt",
    "pickup_locations": [],
    "pickup_instructions": "",
    "delivery_instructions": "",
    "delivery_disclaimer": DISCLAIMER,
}


def _bad(detail: str, status: int = 422) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def _text(value: Any, label: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise _bad(f"{label} must be text.")
    value = value.strip()
    if required and not value:
        raise _bad(f"{label} is required.")
    if len(value) > limit:
        raise _bad(f"{label} must be {limit} characters or fewer.")
    return value


def _money(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1_000_000:
        raise _bad(f"{label} must be an amount from 0 to 1,000,000.")
    return round(float(value), 2)


def _days(value: Any, label: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 120:
        raise _bad(f"{label} must be a whole number of days from 0 to 120.")
    return value


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:40] or secrets.token_hex(3)


# ================================================================== payment methods


def _method_view(method: dict[str, Any], admin: bool = False) -> dict[str, Any]:
    view = {
        "id": method["id"],
        "provider": method["provider"],
        "account_name": method["account_name"],
        "account_number": method["account_number"],
        "instructions": method["instructions"],
        "qr_url": f"/api/payment-methods/{method['id']}/qr" if method.get("qr_file") else None,
        "active": method["active"],
        "display_order": method["display_order"],
        # The customer portal shows a notice instead of blank details while the shop has not configured them.
        "configured": bool(method["account_number"] or method.get("qr_file")),
    }
    if admin:
        view.update({
            "created_at": method["created_at"],
            "updated_at": method["updated_at"],
            "verification_method": method.get("verification_method", "manual"),
            # Never return an API key: only whether a gateway is configured.
            "gateway": payment_verification_services.gateway_view(method.get("gateway_config")),
        })
    return view


def public_payment_methods() -> dict[str, Any]:
    # The proof limit and allowed types come from the same server setting the upload endpoint enforces,
    # so the customer's form cannot drift from the backend (Vercel's 4.5 MB request cap included).
    return {
        "methods": [_method_view(item) for item in payment_method_repository.list_methods(active_only=True)],
        "max_proof_bytes": app_settings.max_upload_mb * 1024 * 1024,
        "allowed_proof_types": ["image/jpeg", "image/png", "application/pdf"],
        "ocr_enabled": app_settings.ocr_configured,
    }


def admin_payment_methods() -> dict[str, Any]:
    return {"methods": [_method_view(item, admin=True) for item in payment_method_repository.list_methods()]}


def active_provider(value: Any) -> str:
    """The canonical provider name of an active payment method, for a customer payment submission."""
    name = _text(value, "Payment method", 60, True)
    for method in payment_method_repository.list_methods(active_only=True):
        if method["provider"].casefold() == name.casefold():
            return method["provider"]
    raise _bad(f"{name} is not an available payment method. Choose one of the shop's active e-wallet options.")


def default_provider() -> str:
    """The method used when an older client submits a payment without naming one: GCash, if active."""
    methods = payment_method_repository.list_methods(active_only=True)
    for method in methods:
        if method["provider"].casefold() == "gcash":
            return method["provider"]
    if methods:
        return methods[0]["provider"]
    raise _bad("The shop has no active payment method. Contact the shop.", 409)


def configured_providers() -> list[str]:
    return [item["provider"] for item in payment_method_repository.list_methods()]


def verification_settings(provider: str) -> dict[str, Any]:
    """Verification configuration of one provider, for receipt matching (gateway keys stay server-side)."""
    for method in payment_method_repository.list_methods():
        if method["provider"].casefold() == str(provider).casefold():
            return {
                "account_name": method.get("account_name", ""),
                "verification_method": method.get("verification_method", "manual"),
                "gateway_config": method.get("gateway_config", {}),
            }
    return {"account_name": "", "verification_method": "manual", "gateway_config": {}}


def _qr_upload(upload: Any) -> dict[str, Any]:
    metadata, contents = file_services.decode_data_url(upload, MAX_QR_BYTES)
    if metadata["mime_type"] not in QR_TYPES:
        raise _bad("Upload the QR code as a PNG, JPG, or WebP image.")
    file_services.save(metadata, contents)
    return metadata


def _gateway_values(value: Any, current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate optional gateway settings. Keys are stored but are never returned by any view."""
    data = value if isinstance(value, dict) else {}
    existing = dict(current or {})
    config = dict(existing)
    if "provider" in data:
        config["provider"] = _text(data.get("provider"), "Gateway provider", 60)
    if "endpoint" in data:
        endpoint = _text(data.get("endpoint"), "Gateway endpoint", 200)
        if endpoint:
            parsed = urlparse(endpoint)
            if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
                raise _bad("Enter a valid gateway endpoint URL without embedded credentials.")
            if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise _bad("Use an HTTPS gateway endpoint so the API key is not sent in cleartext.")
            # Reuse the runtime SSRF validation so an endpoint can never be saved in a form that the
            # confirmation request itself would refuse (private hosts, credentials, fragments, etc.).
            problem = payment_verification_services.endpoint_problem(endpoint)
            if problem:
                raise _bad(f"Enter a safe gateway endpoint: {problem}.")
        config["endpoint"] = endpoint
    if "api_key" in data:
        config["api_key"] = _text(data.get("api_key"), "Gateway API key", 300)
    if "timeout_seconds" in data:
        timeout = data.get("timeout_seconds", 10)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 30:
            raise _bad("Gateway timeout must be a whole number from 1 to 30 seconds.")
        config["timeout_seconds"] = timeout
    return {key: value for key, value in config.items() if value not in (None, "")}


def _method_values(payload: dict[str, Any], partial: bool, current: dict[str, Any] | None = None) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if not partial or "provider" in payload:
        values["provider"] = _text(payload.get("provider"), "Provider name", 60, True)
    for key, label, limit in (("account_name", "Account / merchant name", 120), ("account_number", "Account number", 60),
                              ("instructions", "Payment instructions", 1000)):
        if not partial or key in payload:
            values[key] = _text(payload.get(key), label, limit)
    if "account_number" in values and values["account_number"] and not re.fullmatch(r"[0-9+()\-\s.A-Za-z]{3,60}", values["account_number"]):
        raise _bad("Enter a valid account number.")
    if not partial or "active" in payload:
        active = payload.get("active", True)
        if not isinstance(active, bool):
            raise _bad("Active must be true or false.")
        values["active"] = active
    if not partial or "display_order" in payload:
        order = payload.get("display_order", 0)
        if isinstance(order, bool) or not isinstance(order, int) or not 0 <= order <= 999:
            raise _bad("Display order must be a whole number from 0 to 999.")
        values["display_order"] = order
    if not partial or "verification_method" in payload:
        verification = payload.get("verification_method", "manual")
        if verification not in {"manual", "api"}:
            raise _bad("Verification method must be manual or api.")
        values["verification_method"] = verification
    if not partial or "gateway_config" in payload:
        values["gateway_config"] = _gateway_values(payload.get("gateway_config"), (current or {}).get("gateway_config"))
    return values


def _ensure_unique_provider(provider: str, ignore_id: str | None = None) -> None:
    for method in payment_method_repository.list_methods():
        if method["id"] != ignore_id and method["provider"].casefold() == provider.casefold():
            raise _bad(f"A payment method named {provider} already exists.", 409)


def _validate_verification(values: dict[str, Any], current: dict[str, Any] | None = None) -> None:
    verification = values.get("verification_method", (current or {}).get("verification_method", "manual"))
    config = values.get("gateway_config", (current or {}).get("gateway_config", {}))
    if verification == "api" and not (str(config.get("endpoint") or "").strip() and str(config.get("api_key") or "").strip()):
        raise _bad("Configure a gateway endpoint and API key before enabling provider confirmation.")


@file_services.compensating_files
def create_payment_method(payload: dict[str, Any]) -> dict[str, Any]:
    values = _method_values(payload, partial=False)
    _validate_verification(values)
    _ensure_unique_provider(values["provider"])
    values["qr_file"] = _qr_upload(payload["qr"]) if payload.get("qr") else None
    return _method_view(payment_method_repository.insert(values), admin=True)


@file_services.compensating_files
def update_payment_method(method_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    current = payment_method_repository.get_method(method_id)
    if current is None:
        raise _bad("Payment method not found.", 404)
    values = _method_values(payload, partial=True, current=current)
    _validate_verification(values, current)
    if "provider" in values:
        _ensure_unique_provider(values["provider"], method_id)
    old_qr = current.get("qr_file")
    if payload.get("qr"):
        values["qr_file"] = _qr_upload(payload["qr"])
    elif payload.get("remove_qr") is True:
        values["qr_file"] = None
    if values.get("active") is False and current["active"]:
        others = [item for item in payment_method_repository.list_methods(active_only=True) if item["id"] != method_id]
        if not others:
            raise _bad("Keep at least one payment method active so customers can pay.", 409)
    updated = payment_method_repository.update(method_id, values)
    if "qr_file" in values and old_qr and old_qr.get("filename") != (values["qr_file"] or {}).get("filename"):
        file_services.delete(old_qr["filename"])
    return _method_view(updated, admin=True)  # type: ignore[arg-type]


def delete_payment_method(method_id: str) -> dict[str, Any]:
    current = payment_method_repository.get_method(method_id)
    if current is None:
        raise _bad("Payment method not found.", 404)
    if current["active"] and len(payment_method_repository.list_methods(active_only=True)) <= 1:
        raise _bad("Keep at least one payment method active so customers can pay.", 409)
    payment_method_repository.delete(method_id)
    if current.get("qr_file"):
        file_services.delete(current["qr_file"]["filename"])
    return admin_payment_methods()


def qr_response(method_id: str) -> Response:
    method = payment_method_repository.get_method(method_id)
    if method is None or not method.get("qr_file") or not method["active"]:
        raise _bad("QR code not found.", 404)
    qr = method["qr_file"]
    response = file_services.response(qr["filename"], qr["mime_type"], qr["name"], inline=True)
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ================================================================== fulfillment settings


def fulfillment_settings() -> dict[str, Any]:
    with connection_scope() as connection:
        row = connection.execute("SELECT value FROM app_meta WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    stored = json.loads(row["value"]) if row else {}
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    if isinstance(stored, dict):
        settings.update({key: value for key, value in stored.items() if key in DEFAULT_SETTINGS})
    return settings


def _clean_area(item: Any, number: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise _bad(f"Delivery area {number} is invalid.")
    label = _text(item.get("label"), f"Delivery area {number} name", 80, True)
    region = item.get("region")
    if region not in REGIONS:
        raise _bad(f"{label}: choose Luzon, Visayas, or Mindanao.")
    provinces = item.get("provinces") or []
    if not isinstance(provinces, list) or len(provinces) > 120:
        raise _bad(f"{label}: invalid province list.")
    low = _days(item.get("estimate_min_days"), f"{label}: minimum delivery days")
    high = _days(item.get("estimate_max_days"), f"{label}: maximum delivery days")
    if low is not None and high is not None and low > high:
        raise _bad(f"{label}: the minimum delivery days cannot be more than the maximum.")
    active = item.get("active", True)
    if not isinstance(active, bool):
        raise _bad(f"{label}: active must be true or false.")
    # Store the PSGC spelling when one exists so an alias typed by an administrator cannot make a
    # valid customer address silently fall out of its configured area (QA-016). Unknown legacy names
    # are still kept, because old installations may use province lists from before the PSGC snapshot.
    canonical = []
    for value in provinces:
        text = _text(value, "Province", 80, True)
        canonical.append(location_services.canonical_province(text) or text)
    return {
        "id": _text(item.get("id"), "Area id", 40) or _slug(label),
        "label": label,
        "region": region,
        "provinces": sorted(set(canonical)),
        "fee": _money(item.get("fee", 0), f"{label}: delivery fee"),
        "estimate_min_days": low,
        "estimate_max_days": high,
        "active": active,
    }


def _clean_courier(item: Any, number: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise _bad(f"Courier {number} is invalid.")
    name = _text(item.get("name"), f"Courier {number} name", 80, True)
    url = _text(item.get("tracking_url"), f"{name}: tracking page address", 300)
    if url and (not url.startswith("https://") or "{tracking}" not in url):
        raise _bad(f"{name}: the tracking page address must start with https:// and contain {{tracking}}.")
    active = item.get("active", True)
    if not isinstance(active, bool):
        raise _bad(f"{name}: active must be true or false.")
    return {
        "id": _text(item.get("id"), "Courier id", 40) or _slug(name),
        "name": name,
        "service": _text(item.get("service"), f"{name}: service", 80),
        "tracking_url": url,
        "active": active,
    }


def _clean_location(item: Any, number: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise _bad(f"Pickup location {number} is invalid.")
    name = _text(item.get("name"), f"Pickup location {number} name", 120, True)
    active = item.get("active", True)
    if not isinstance(active, bool):
        raise _bad(f"{name}: active must be true or false.")
    return {
        "id": _text(item.get("id"), "Location id", 40) or _slug(name),
        "name": name,
        "address": _text(item.get("address"), f"{name}: address", 300, True),
        "hours": _text(item.get("hours"), f"{name}: hours", 160),
        "instructions": _text(item.get("instructions"), f"{name}: instructions", 500),
        "active": active,
    }


def _unique_ids(items: list[dict[str, Any]], what: str) -> None:
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise _bad(f"Two {what} have the same id. Give each a different name.")


def save_fulfillment_settings(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _bad("Invalid settings.")
    current = fulfillment_settings()
    enabled = payload.get("delivery_enabled", current["delivery_enabled"])
    if not isinstance(enabled, bool):
        raise _bad("Delivery availability must be true or false.")
    areas = payload.get("areas", current["areas"])
    couriers = payload.get("couriers", current["couriers"])
    locations = payload.get("pickup_locations", current["pickup_locations"])
    for value, label in ((areas, "delivery areas"), (couriers, "couriers"), (locations, "pickup locations")):
        if not isinstance(value, list) or len(value) > 60:
            raise _bad(f"Invalid {label}.")
    cleaned = {
        "delivery_enabled": enabled,
        "areas": [_clean_area(item, number) for number, item in enumerate(areas, start=1)],
        "couriers": [_clean_courier(item, number) for number, item in enumerate(couriers, start=1)],
        "pickup_locations": [_clean_location(item, number) for number, item in enumerate(locations, start=1)],
        "pickup_instructions": _text(payload.get("pickup_instructions", current["pickup_instructions"]), "Pickup instructions", 1000),
        "delivery_instructions": _text(payload.get("delivery_instructions", current["delivery_instructions"]), "Delivery instructions", 1000),
        "delivery_disclaimer": _text(payload.get("delivery_disclaimer", current["delivery_disclaimer"]), "Delivery disclaimer", 600) or DISCLAIMER,
    }
    _unique_ids(cleaned["areas"], "delivery areas")
    _unique_ids(cleaned["couriers"], "couriers")
    _unique_ids(cleaned["pickup_locations"], "pickup locations")
    default = payload.get("default_courier", current["default_courier"])
    active_couriers = [item["id"] for item in cleaned["couriers"] if item["active"]]
    cleaned["default_courier"] = default if default in active_couriers else (active_couriers[0] if active_couriers else "")
    with connection_scope() as connection:
        connection.execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SETTINGS_KEY, json.dumps(cleaned)),
        )
    return fulfillment_settings()


def public_fulfillment_options() -> dict[str, Any]:
    """What the order page and customer portal need: delivery areas with fees and estimates, pickup locations."""
    settings = fulfillment_settings()
    return {
        "delivery_enabled": settings["delivery_enabled"],
        "areas": [
            # `provinces` lets the order page pick the exact configured area for the selected address
            # instead of guessing from the region; the server still recalculates the fee on submit.
            {key: area[key] for key in ("id", "label", "region", "provinces", "fee", "estimate_min_days", "estimate_max_days")}
            for area in settings["areas"] if area["active"]
        ],
        "pickup_locations": [
            {key: item[key] for key in ("id", "name", "address", "hours", "instructions")}
            for item in settings["pickup_locations"] if item["active"]
        ],
        "pickup_instructions": settings["pickup_instructions"],
        "delivery_instructions": settings["delivery_instructions"],
        "delivery_disclaimer": settings["delivery_disclaimer"],
        "default_courier": courier_name(settings["default_courier"], settings),
    }


def region_of(province: str) -> str:
    # The PSGC snapshot is authoritative for known provinces. The legacy sets below still cover old
    # free-text orders and province names that are not in the current dataset.
    known = location_services.region_for_province(province)
    if known in REGIONS:
        return known
    # Use the same canonical key as address validation so legacy aliases keep working in the fee
    # fallback instead of being treated as an unknown Luzon province.
    key = location_services.province_key(province)
    if key in VISAYAS_PROVINCES:
        return "visayas"
    if key in MINDANAO_PROVINCES:
        return "mindanao"
    return "luzon"


def estimate_text(area: dict[str, Any]) -> str:
    low, high = area.get("estimate_min_days"), area.get("estimate_max_days")
    if low is None and high is None:
        return "Estimated delivery period confirmed by the shop"
    if low is None or high is None or low == high:
        return f"About {high if low is None else low} days (estimate)"
    return f"{low}–{high} days (estimate)"


def delivery_quote(province: str, area_id: Any = None) -> dict[str, Any]:
    """The delivery area, fee, and estimated courier period for an address. Calculated only on the server."""
    settings = fulfillment_settings()
    if not settings["delivery_enabled"]:
        raise _bad("Delivery is not available right now. Choose Store Pickup.")
    active = [area for area in settings["areas"] if area["active"]]
    # Both sides use the PSGC key, so “Cavite Province”, “Province of Cavite” and “Cavite” all match.
    province_key = location_services.province_key(province)
    area = None
    if area_id not in (None, ""):
        area = next((item for item in active if item["id"] == area_id), None)
        if area is None:
            raise _bad("Choose an available delivery area.")
        # The chosen area must fit the address, so a cheaper area cannot be picked for a farther province.
        listed = {location_services.province_key(value) for value in area["provinces"]}
        if (listed and province_key not in listed) or (not listed and region_of(province) != area["region"]):
            raise _bad(f"{province} is not in the {area['label']} delivery area. Choose the area that matches the address.")
    else:
        area = next((item for item in active if province_key in {location_services.province_key(value) for value in item["provinces"]}), None)
        if area is None:
            region = region_of(province)
            area = next((item for item in active if item["region"] == region and not item["provinces"]), None)
        if area is None:
            raise _bad(f"Delivery to {province} is not available. Choose Store Pickup or contact the shop.")
    return {
        "area_id": area["id"],
        "area_label": area["label"],
        "region": area["region"],
        "delivery_fee": area["fee"],
        "estimate_min_days": area["estimate_min_days"],
        "estimate_max_days": area["estimate_max_days"],
        "estimate_text": estimate_text(area),
    }


def pickup_location(location_id: Any) -> dict[str, Any] | None:
    settings = fulfillment_settings()
    active = [item for item in settings["pickup_locations"] if item["active"]]
    if location_id in (None, ""):
        return active[0] if len(active) == 1 else None
    found = next((item for item in active if item["id"] == location_id), None)
    if found is None:
        raise _bad("Choose an available pickup location.")
    return found


def courier(courier_id: Any, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or fulfillment_settings()
    chosen = courier_id if courier_id not in (None, "") else settings["default_courier"]
    found = next((item for item in settings["couriers"] if item["id"] == chosen and item["active"]), None)
    if found is None:
        raise _bad("Choose an active courier (Admin → Settings → Delivery & couriers).")
    return found


def courier_name(courier_id: str, settings: dict[str, Any] | None = None) -> str:
    settings = settings or fulfillment_settings()
    found = next((item for item in settings["couriers"] if item["id"] == courier_id), None)
    return found["name"] if found else ""


def tracking_link(courier_record: dict[str, Any], tracking_number: str) -> str:
    template = courier_record.get("tracking_url") or ""
    if not template or not tracking_number:
        return ""
    from urllib.parse import quote

    return template.replace("{tracking}", quote(tracking_number, safe=""))
