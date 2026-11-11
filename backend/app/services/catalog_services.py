"""Catalog rules: admin input validation, the pricing engine, and order-form validation.

The same functions price the live estimate on the order page (``/api/catalog/quote``) and the submitted
order, so the customer sees exactly what the server will store. Prices only ever come from values an
administrator entered; when a price is missing the result says "quote required" instead of guessing.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
import secrets
from datetime import date, time
from typing import Any

from fastapi import HTTPException
from fastapi.responses import FileResponse

from backend.app.core.config import settings
from backend.app.database.catalog_seed import slug
from backend.app.models.catalog import (
    AREA_HEIGHT_KEY,
    AREA_UNIT_KEY,
    AREA_WIDTH_KEY,
    EVENT_MODES,
    FEET_PER_UNIT,
    FIELD_SECTIONS,
    FIELD_TYPES,
    OPTION_FIELD_TYPES,
    PRICE_TYPES,
    PRICING_MODES,
    CatalogField,
    CatalogProduct,
    FieldOption,
    InvitationPackage,
)
from backend.app.repositories import catalog_repository


CURRENCY = "PHP"
MAX_PRICE = 10_000_000
MAX_QUANTITY = 50_000
MAX_FILES_PER_FIELD = 10
CATALOG_IMAGE_DIR = settings.upload_dir / "catalog"
CATALOG_IMAGE_MAX_BYTES = 2 * 1024 * 1024
CATALOG_IMAGE_TYPES = {
    "image/jpeg": (".jpg", (b"\xff\xd8\xff",)),
    "image/png": (".png", (b"\x89PNG\r\n\x1a\n",)),
    "image/webp": (".webp", (b"RIFF",)),
}
FIELD_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
TEXT_LIMIT = {"text": 160, "textarea": 1500}


def money(value: float) -> str:
    return f"₱{value:,.2f}"


def _bad(detail: str, status: int = 422) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


# ================================================================== public reads


def public_catalog() -> dict[str, Any]:
    """Active categories that have at least one active product, with every active field and option.

    This is everything the order form needs to render without extra requests.
    """
    categories = [category for category in catalog_repository.load_catalog(active_only=True) if category["products"]]
    return {"currency": CURRENCY, "categories": categories}


def public_catalog_full() -> dict[str, Any]:
    """Every active category, including categories that have no active products yet."""
    return {
        "currency": CURRENCY,
        "categories": catalog_repository.load_catalog(active_only=True),
    }


def admin_catalog() -> dict[str, Any]:
    return {
        "currency": CURRENCY,
        "categories": catalog_repository.load_catalog(active_only=False),
        "field_types": list(FIELD_TYPES),
        "pricing_modes": list(PRICING_MODES),
        "price_types": list(PRICE_TYPES),
        "event_modes": list(EVENT_MODES),
        "field_sections": list(FIELD_SECTIONS),
    }


def orderable_product(product_id: Any) -> CatalogProduct:
    if not isinstance(product_id, str) or not product_id.strip():
        raise _bad("Choose a product from the catalog.")
    product = catalog_repository.get_product(product_id.strip(), active_only=True)
    if product is None:
        raise _bad("The selected product is no longer available. Choose another product.")
    return product


def public_product(product_id: str) -> CatalogProduct:
    product = catalog_repository.get_product(product_id, active_only=True)
    if product is None:
        raise _bad("Product not found.", 404)
    return product


def admin_product(product_id: str) -> CatalogProduct:
    product = catalog_repository.get_product(product_id, active_only=False)
    if product is None:
        raise _bad("Product not found.", 404)
    return product


# ================================================================== field visibility & value parsing


def _visible_fields(product: CatalogProduct, selections: dict[str, Any]) -> list[CatalogField]:
    """Fields whose ``depends_on`` condition is met. A field under a hidden parent is hidden too."""
    by_key = {field["field_key"]: field for field in product["fields"]}
    memo: dict[str, bool] = {}

    def visible(key: str, trail: frozenset[str]) -> bool:
        if key in memo:
            return memo[key]
        field = by_key[key]
        parent = field["depends_on"]
        if not parent:
            result = True
        elif parent not in by_key or parent in trail:
            result = False
        else:
            value = selections.get(parent)
            chosen = set(value) if isinstance(value, list) else {value}
            result = visible(parent, trail | {key}) and bool(chosen & set(field["depends_values"]))
        memo[key] = result
        return result

    return [field for field in product["fields"] if visible(field["field_key"], frozenset())]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _format_number(value: float) -> str:
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _options_by_value(field: CatalogField) -> dict[str, FieldOption]:
    return {option["value"]: option for option in field["options"]}


# ================================================================== pricing engine


def _area_square_feet(selections: dict[str, Any]) -> float | None:
    width = _number(selections.get(AREA_WIDTH_KEY))
    height = _number(selections.get(AREA_HEIGHT_KEY))
    unit = selections.get(AREA_UNIT_KEY) or "ft"
    factor = FEET_PER_UNIT.get(unit) if isinstance(unit, str) else None
    if width is None or height is None or factor is None or width <= 0 or height <= 0:
        return None
    return (width * factor) * (height * factor)


def calculate_price(product: CatalogProduct, quantity: int, selections: dict[str, Any]) -> dict[str, Any]:
    """Price an order from the administrator's configured values only.

    unit mode:  (base price + per-piece options) x quantity + per-order options
    area mode:  ((base + per-sq-ft options) x area + per-piece options) x quantity + per-order options
    quote mode, or no base price set, or a "quote required" option selected: no total, quote required.
    """
    mode = product["pricing_mode"]
    base_price = product["base_price"]
    reasons: list[str] = []
    incomplete: list[str] = []
    area = _area_square_feet(selections) if mode == "area" else None

    if mode == "quote":
        reasons.append("This product is priced by quotation after the shop reviews your request.")
    elif base_price is None:
        reasons.append("The shop has not published a price for this product yet, so it will be quoted.")
    if mode == "area" and area is None:
        incomplete.append("Enter the width and height to calculate the size-based price.")

    per_sqft = 0.0
    per_unit = 0.0
    per_order = 0.0
    lines: list[dict[str, Any]] = []
    # Lines charged per square foot only get an amount once the area is known.
    sqft_lines: list[tuple[dict[str, Any], float]] = []
    sqft_text = f"{_format_number(area)} sq ft" if area is not None else "area"

    if base_price is not None and mode != "quote":
        if mode == "area":
            per_sqft += base_price
            line = {"label": "Base price", "detail": f"{money(base_price)} per sq ft × {sqft_text} × {quantity}", "amount": 0.0}
            sqft_lines.append((line, base_price))
        else:
            per_unit += base_price
            line = {"label": "Base price", "detail": f"{money(base_price)} each × {quantity}", "amount": round(base_price * quantity, 2)}
        lines.append(line)

    for field in _visible_fields(product, selections):
        if field["field_type"] not in OPTION_FIELD_TYPES:
            continue
        options = _options_by_value(field)
        value = selections.get(field["field_key"])
        chosen = value if isinstance(value, list) else [value]
        for item in chosen:
            option = options.get(item) if isinstance(item, str) else None
            if option is None:
                continue
            if option["requires_quote"]:
                reasons.append(f"{field['label']}: “{option['label']}” is priced by quotation.")
                continue
            adjustment = option["price_adjustment"]
            if not adjustment:
                continue
            label = f"{field['label']}: {option['label']}"
            sign = "+" if adjustment > 0 else "−"
            amount_text = f"{sign}{money(abs(adjustment))}"
            price_type = option["price_type"]
            if price_type == "per_order":
                per_order += adjustment
                lines.append({"label": label, "detail": f"{amount_text} per order", "amount": round(adjustment, 2)})
            elif price_type == "per_sqft" and mode == "area":
                per_sqft += adjustment
                line = {"label": label, "detail": f"{amount_text} per sq ft × {sqft_text} × {quantity}", "amount": 0.0}
                sqft_lines.append((line, adjustment))
                lines.append(line)
            else:
                # per_sqft options on a product that is not priced by area are charged per piece.
                per_unit += adjustment
                lines.append({"label": label, "detail": f"{amount_text} each × {quantity}", "amount": round(adjustment * quantity, 2)})

    unit_price: float | None = None
    total: float | None = None
    if not reasons and not incomplete:
        piece_price = per_unit + (per_sqft * area if area is not None else 0.0)
        unit_price = piece_price
        total = max(0.0, round(piece_price * quantity + per_order, 2))
        if area is not None:
            for line, rate in sqft_lines:
                line["amount"] = round(rate * area * quantity, 2)

    return {
        "currency": CURRENCY,
        "pricing_mode": mode,
        "base_price": base_price,
        "quantity": quantity,
        "area_sqft": round(area, 2) if area is not None else None,
        "unit_price": round(unit_price, 2) if unit_price is not None else None,
        "lines": lines,
        "estimated_total": total,
        "requires_quote": bool(reasons),
        "quote_reasons": reasons,
        "incomplete": incomplete,
    }


def quote(payload: dict[str, Any]) -> dict[str, Any]:
    product = orderable_product(payload.get("product_id"))
    quantity = _number(payload.get("quantity"))
    quantity_value = int(quantity) if quantity is not None and quantity >= 1 and quantity.is_integer() else product["min_quantity"]
    quantity_value = max(1, min(quantity_value, MAX_QUANTITY))
    raw = payload.get("options")
    selections = _parse_selections(product, raw if isinstance(raw, dict) else {}, strict=False)
    return calculate_price(product, quantity_value, selections)


# ================================================================== order validation


def _parse_selections(product: CatalogProduct, raw: dict[str, Any], *, strict: bool, file_counts: dict[str, int] | None = None) -> dict[str, Any]:
    """Return clean values for the product's fields.

    strict=False (live estimate): silently skip missing or invalid values.
    strict=True (order submission): raise 422 for any missing required or invalid value.
    Values for fields that do not exist on the product, or that are hidden by a condition, are dropped.
    """
    clean: dict[str, Any] = {}
    # First pass collects option values so field conditions can be evaluated.
    for field in product["fields"]:
        key = field["field_key"]
        value = raw.get(key)
        if field["field_type"] in ("select", "radio") and isinstance(value, str) and value in _options_by_value(field):
            clean[key] = value
        elif field["field_type"] == "checkboxes" and isinstance(value, list):
            allowed = _options_by_value(field)
            clean[key] = [item for item in dict.fromkeys(value) if isinstance(item, str) and item in allowed]
    visible = _visible_fields(product, clean)
    visible_keys = {field["field_key"] for field in visible}
    result: dict[str, Any] = {}
    for field in visible:
        key = field["field_key"]
        kind = field["field_type"]
        label = field["label"]
        value = raw.get(key)
        missing = False
        if kind in ("select", "radio"):
            if key in clean:
                result[key] = clean[key]
            elif value not in (None, ""):
                if strict:
                    raise _bad(f"{label}: choose one of the available options.")
            else:
                missing = True
        elif kind == "checkboxes":
            chosen = clean.get(key, [])
            if strict and isinstance(value, list) and len(chosen) != len([item for item in value if item not in (None, "")]):
                raise _bad(f"{label}: choose from the available options.")
            if chosen:
                result[key] = chosen
            else:
                missing = True
        elif kind in ("text", "textarea"):
            text = value.strip() if isinstance(value, str) else ""
            limit = field["max_length"] or TEXT_LIMIT[kind]
            if len(text) > limit:
                if strict:
                    raise _bad(f"{label} must be {limit} characters or fewer.")
                text = text[:limit]
            if text:
                result[key] = text
            else:
                missing = True
        elif kind == "number":
            number = _number(value)
            if number is None:
                if strict and value not in (None, ""):
                    raise _bad(f"{label} must be a number.")
                missing = True
            else:
                low, high = field["min_value"], field["max_value"]
                if (low is not None and number < low) or (high is not None and number > high):
                    if strict:
                        bounds = " and ".join(part for part in (
                            f"at least {_format_number(low)}" if low is not None else "",
                            f"at most {_format_number(high)}" if high is not None else "",
                        ) if part)
                        raise _bad(f"{label} must be {bounds}.")
                    missing = True
                else:
                    result[key] = number
        elif kind == "date":
            text = value.strip() if isinstance(value, str) else ""
            if text:
                try:
                    date.fromisoformat(text)
                    result[key] = text
                except ValueError as error:
                    if strict:
                        raise _bad(f"{label}: enter a valid date.") from error
            else:
                missing = True
        elif kind == "time":
            text = value.strip() if isinstance(value, str) else ""
            if text:
                try:
                    time.fromisoformat(text)
                    result[key] = text
                except ValueError as error:
                    if strict:
                        raise _bad(f"{label}: enter a valid time.") from error
            else:
                missing = True
        elif kind == "file":
            count = (file_counts or {}).get(key, 0)
            if count > field["max_files"]:
                raise _bad(f"{label}: attach up to {field['max_files']} file(s).")
            missing = count == 0
        if strict and missing and field["required"]:
            raise _bad(f"{label} is required." if kind not in OPTION_FIELD_TYPES else f"Choose an option for {label}.")
    if strict and file_counts:
        for key, count in file_counts.items():
            if count and key not in visible_keys:
                field_label = next((field["label"] for field in product["fields"] if field["field_key"] == key), key)
                raise _bad(f"{field_label} does not apply to the options you selected. Remove those files.")
    return result


def _display_value(field: CatalogField, value: Any, selections: dict[str, Any]) -> str:
    kind = field["field_type"]
    if kind in ("select", "radio"):
        return _options_by_value(field)[value]["label"]
    if kind == "checkboxes":
        options = _options_by_value(field)
        return ", ".join(options[item]["label"] for item in value)
    if kind == "number":
        unit = selections.get(AREA_UNIT_KEY) if field["field_key"] in (AREA_WIDTH_KEY, AREA_HEIGHT_KEY) else None
        return f"{_format_number(value)} {unit}" if unit else _format_number(value)
    return str(value)


def product_has_event_section(product: CatalogProduct) -> bool:
    """True for products whose order form collects event details as catalog fields (Invitation)."""
    return any(field["section"] == "event" for field in product["fields"])


def _section_scoped(product: CatalogProduct, sections: set[str] | None) -> CatalogProduct:
    """A shallow copy holding only the fields of the requested form sections."""
    if sections is None:
        return product
    return {**product, "fields": [field for field in product["fields"] if field["section"] in sections]}


def _package_specifications(package: InvitationPackage) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = [
        {"key": "package", "label": "Package", "value": package["name"], "type": "package"},
        {"key": "package_price", "label": "Package price", "value": money(package["price"]), "type": "money"},
    ]
    if package["size"]:
        rows.append({"key": "package_size", "label": "Size", "value": package["size"], "type": "text"})
    if package["material"]:
        rows.append({"key": "package_material", "label": "Material", "value": package["material"], "type": "text"})
    if package["inclusions"]:
        rows.append({"key": "package_inclusions", "label": "Inclusions", "value": "; ".join(package["inclusions"]), "type": "text"})
    if package["add_on_price"] is not None:
        label = package["add_on_label"] or "Additional pieces"
        rows.append({
            "key": "package_add_on",
            "label": "Add-on",
            "value": f"{label}: {money(package['add_on_price'])} per additional piece beyond {package['included_quantity']:,} pcs",
            "type": "money",
        })
    return rows


def _package_pricing(package: InvitationPackage, quantity: int) -> dict[str, Any]:
    """Price a package order from the package's published price and add-on charge only."""
    included = package["included_quantity"]
    price = round(package["price"], 2)
    add_on = package["add_on_price"]
    lines = [{"label": package["name"], "detail": f"{included:,} pcs included", "amount": price}]
    if add_on is None:
        if quantity != included:
            raise _bad(f"{package['name']} is ordered at exactly {included:,} pieces. Change the quantity to {included:,}.")
        total = price
    else:
        extra = max(0, quantity - included)
        total = round(price + extra * float(add_on), 2)
        if extra:
            lines.append({
                "label": package["add_on_label"] or "Additional pieces",
                "detail": f"{extra:,} additional pcs × {money(float(add_on))}",
                "amount": round(extra * float(add_on), 2),
            })
    return {
        "currency": CURRENCY,
        "pricing_mode": "package",
        "base_price": None,
        "quantity": quantity,
        "area_sqft": None,
        "unit_price": None,
        "lines": lines,
        "estimated_total": total,
        "requires_quote": False,
        "quote_reasons": [],
        "incomplete": [],
        "package_id": package["id"],
        "package_name": package["name"],
        "package_price": price,
        "included_quantity": included,
        "add_on_price": add_on,
    }


def evaluate_order(
    product: CatalogProduct,
    quantity: Any,
    raw_selections: Any,
    file_names: dict[str, list[str]],
    *,
    sections: set[str] | None = None,
    package: InvitationPackage | None = None,
) -> tuple[int, dict[str, Any], list[dict[str, str]], list[dict[str, str]], dict[str, Any]]:
    """Validate a submitted product configuration and price it.

    Returns (quantity, selections, specifications, event_rows, pricing). ``specifications`` and
    ``event_rows`` are the human-readable, ordered label/value lists the admin dashboard and the
    customer portal display. ``sections`` limits validation to the form sections a package order
    still shows; ``package`` prices from the package instead of the product's option prices.
    """
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise _bad("Quantity must be a whole number.")
    low, high = product["min_quantity"], min(product["max_quantity"], MAX_QUANTITY)
    if quantity < low or quantity > high:
        raise _bad(f"Quantity for {product['name']} must be between {low:,} and {high:,}.")
    if not isinstance(raw_selections, dict):
        raw_selections = {}
    scoped = _section_scoped(product, sections)
    counts = {key: len(names) for key, names in file_names.items()}
    selections = _parse_selections(scoped, raw_selections, strict=True, file_counts=counts)
    specifications: list[dict[str, str]] = []
    event_rows: list[dict[str, str]] = []
    for field in _visible_fields(scoped, selections):
        key = field["field_key"]
        value: str | None = None
        if field["field_type"] == "file":
            names = file_names.get(key, [])
            if names:
                value = ", ".join(names)
        elif key in selections:
            value = _display_value(field, selections[key], selections)
        if value is None:
            continue
        if field["section"] == "event":
            event_rows.append({"label": field["label"], "value": value})
            continue
        specifications.append({"key": key, "label": field["label"], "value": value, "type": field["field_type"]})
    if package is not None:
        pricing = _package_pricing(package, quantity)
        specifications = _package_specifications(package) + specifications
    else:
        pricing = calculate_price(scoped, quantity, selections)
        if pricing["area_sqft"] is not None:
            specifications.append({"key": "area_sqft", "label": "Print area", "value": f"{_format_number(pricing['area_sqft'])} sq ft per piece", "type": "computed"})
    return quantity, selections, specifications, event_rows, pricing


def file_field(product: CatalogProduct, key: str) -> CatalogField | None:
    return next((field for field in product["fields"] if field["field_key"] == key and field["field_type"] == "file"), None)


# ================================================================== admin validation helpers


def _text(payload: dict[str, Any], key: str, label: str, limit: int, *, required: bool = False, default: str = "") -> str:
    value = payload.get(key, default)
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


def _bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise _bad(f"{key.replace('_', ' ').capitalize()} must be true or false.")
    return value


def _int(payload: dict[str, Any], key: str, label: str, low: int, high: int, default: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _bad(f"{label} must be a whole number from {low:,} to {high:,}.")
    return value


def _price(value: Any, label: str, *, allow_none: bool, allow_negative: bool = False) -> float | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise _bad(f"{label} is required.")
    number = _number(value)
    low = -MAX_PRICE if allow_negative else 0
    if number is None or not low <= number <= MAX_PRICE:
        raise _bad(f"{label} must be an amount from {low:,} to {MAX_PRICE:,}.")
    return round(number, 2)


def _choice(payload: dict[str, Any], key: str, label: str, allowed: tuple[str, ...], default: str) -> str:
    value = payload.get(key, default)
    if value not in allowed:
        raise _bad(f"{label} must be one of: {', '.join(allowed)}.")
    return value


# ================================================================== admin: categories


def create_category(payload: dict[str, Any]) -> dict[str, Any]:
    label = _text(payload, "label", "Category name", 80, required=True)
    category_id = catalog_repository.unique_id("catalog_categories", slug(label))
    catalog_repository.create_category({
        "id": category_id,
        "label": label,
        "description": _text(payload, "description", "Category description", 300),
        "sort_order": _int(payload, "sort_order", "Sort order", 0, 10_000, len(catalog_repository.load_catalog(active_only=False))),
        "active": _bool(payload, "active", True),
    })
    return admin_catalog()


def update_category(category_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if catalog_repository.get_category(category_id) is None:
        raise _bad("Category not found.", 404)
    updates: dict[str, Any] = {}
    if "label" in payload:
        updates["label"] = _text(payload, "label", "Category name", 80, required=True)
    if "description" in payload:
        updates["description"] = _text(payload, "description", "Category description", 300)
    if "sort_order" in payload:
        updates["sort_order"] = _int(payload, "sort_order", "Sort order", 0, 10_000, 0)
    if "active" in payload:
        updates["active"] = _bool(payload, "active", True)
    catalog_repository.update_category(category_id, updates)
    return admin_catalog()


def delete_category(category_id: str) -> dict[str, Any]:
    if catalog_repository.get_category(category_id) is None:
        raise _bad("Category not found.", 404)
    if catalog_repository.count_products(category_id):
        raise _bad("Move or delete this category's products first, or mark the category unavailable.", 409)
    catalog_repository.delete_category(category_id)
    return admin_catalog()


# ================================================================== admin: products


def _save_catalog_image(upload: Any) -> str:
    if not isinstance(upload, dict):
        raise _bad("Invalid product image.")
    data_url = upload.get("dataUrl")
    if not isinstance(data_url, str) or not data_url.startswith("data:") or ";base64," not in data_url:
        raise _bad("The product image could not be read.")
    mime_type = data_url[5:data_url.index(";")]
    allowed = CATALOG_IMAGE_TYPES.get(mime_type)
    if allowed is None:
        raise _bad("Product images must be JPG, PNG, or WebP.")
    extension, signatures = allowed
    try:
        contents = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except (binascii.Error, ValueError) as error:
        raise _bad("The product image could not be decoded.") from error
    if not contents or len(contents) > CATALOG_IMAGE_MAX_BYTES:
        raise _bad("Product images must be no larger than 2 MB.", 413)
    if not any(contents.startswith(signature) for signature in signatures) or (mime_type == "image/webp" and contents[8:12] != b"WEBP"):
        raise _bad("The product image file is not a valid image.")
    CATALOG_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(16)}{extension}"
    try:
        (CATALOG_IMAGE_DIR / filename).write_bytes(contents)
    except OSError as error:
        raise _bad("The product image could not be saved.", 500) from error
    return f"/api/catalog/images/{filename}"


def catalog_image(filename: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{32}\.(jpg|png|webp)", filename):
        raise _bad("Image not found.", 404)
    path = CATALOG_IMAGE_DIR / filename
    if not path.is_file():
        raise _bad("Image not found.", 404)
    media = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[filename.rsplit(".", 1)[1]]
    return FileResponse(path, media_type=media)


def _product_values(payload: dict[str, Any], current: CatalogProduct | None) -> dict[str, Any]:
    values: dict[str, Any] = {}
    creating = current is None

    def wants(key: str) -> bool:
        return creating or key in payload

    if wants("name"):
        values["name"] = _text(payload, "name", "Product name", 120, required=True)
    if wants("category_id"):
        category_id = payload.get("category_id")
        if not isinstance(category_id, str) or catalog_repository.get_category(category_id) is None:
            raise _bad("Choose an existing category.")
        values["category_id"] = category_id
    if wants("description"):
        values["description"] = _text(payload, "description", "Description", 600)
    if payload.get("image_upload"):
        values["image"] = _save_catalog_image(payload["image_upload"])
    elif wants("image"):
        image = _text(payload, "image", "Image path", 300)
        if image and not image.startswith("/"):
            raise _bad("Image path must be a site path that starts with “/”, or upload an image.")
        values["image"] = image
    if wants("pricing_mode"):
        values["pricing_mode"] = _choice(payload, "pricing_mode", "Pricing method", PRICING_MODES, "unit")
    if wants("base_price"):
        values["base_price"] = _price(payload.get("base_price"), "Base price", allow_none=True)
    if wants("min_quantity"):
        values["min_quantity"] = _int(payload, "min_quantity", "Minimum quantity", 1, MAX_QUANTITY, 1)
    if wants("max_quantity"):
        values["max_quantity"] = _int(payload, "max_quantity", "Maximum quantity", 1, MAX_QUANTITY, 10_000)
    low = values.get("min_quantity", current["min_quantity"] if current else 1)
    high = values.get("max_quantity", current["max_quantity"] if current else 10_000)
    if low > high:
        raise _bad("Minimum quantity cannot be greater than maximum quantity.")
    if wants("event_mode"):
        values["event_mode"] = _choice(payload, "event_mode", "Event details", EVENT_MODES, "none")
    if wants("active"):
        values["active"] = _bool(payload, "active", True)
    if "sort_order" in payload:
        values["sort_order"] = _int(payload, "sort_order", "Sort order", 0, 10_000, 0)
    return values


def create_product(payload: dict[str, Any]) -> CatalogProduct:
    values = _product_values(payload, None)
    values["id"] = catalog_repository.unique_id("catalog_products", slug(values["name"]))
    values.setdefault("sort_order", catalog_repository.next_sort_order("catalog_products", "category_id", values["category_id"]))
    catalog_repository.create_product(values)
    return admin_product(values["id"])


def update_product(product_id: str, payload: dict[str, Any]) -> CatalogProduct:
    current = admin_product(product_id)
    catalog_repository.update_product(product_id, _product_values(payload, current))
    return admin_product(product_id)


def delete_product(product_id: str) -> None:
    admin_product(product_id)
    catalog_repository.delete_product(product_id)


def update_pricing(product_id: str, payload: dict[str, Any]) -> CatalogProduct:
    product = admin_product(product_id)
    product_updates: dict[str, Any] = {}
    if "pricing_mode" in payload:
        product_updates["pricing_mode"] = _choice(payload, "pricing_mode", "Pricing method", PRICING_MODES, product["pricing_mode"])
    if "base_price" in payload:
        product_updates["base_price"] = _price(payload.get("base_price"), "Base price", allow_none=True)
    known = {option["id"]: (field, option) for field in product["fields"] for option in field["options"]}
    option_updates = []
    raw_options = payload.get("options", [])
    if not isinstance(raw_options, list):
        raise _bad("Option prices must be a list.")
    for item in raw_options:
        if not isinstance(item, dict) or item.get("id") not in known:
            raise _bad("An option in the price list does not belong to this product. Reload and try again.")
        field, option = known[item["id"]]
        option_updates.append({
            "id": option["id"],
            "price_adjustment": _price(item.get("price_adjustment", 0), f"{field['label']} · {option['label']} price", allow_none=False, allow_negative=True),
            "price_type": _choice(item, "price_type", "Charge type", PRICE_TYPES, option["price_type"]),
            "requires_quote": _bool(item, "requires_quote", option["requires_quote"]),
        })
    catalog_repository.update_pricing(product_id, product_updates, option_updates)
    return admin_product(product_id)


# ================================================================== admin: fields & options


def _clean_options(raw: Any, current: CatalogField | None) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise _bad("Add at least one option for this field.")
    if len(raw) > 100:
        raise _bad("A field can have at most 100 options.")
    existing = {option["id"]: option for option in (current["options"] if current else [])}
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise _bad(f"Option {index} is invalid.")
        label = _text(item, "label", f"Option {index} label", 120, required=True)
        option_id = item.get("id") if item.get("id") in existing else None
        value = item.get("value")
        if not isinstance(value, str) or not value.strip():
            value = existing[option_id]["value"] if option_id else slug(label)
        value = slug(value)[:60]
        base_value, sequence = value, 1
        while value in seen:
            sequence += 1
            value = f"{base_value}-{sequence}"
        seen.add(value)
        cleaned.append({
            "id": option_id,
            "label": label,
            "value": value,
            "price_adjustment": _price(item.get("price_adjustment", 0), f"Option “{label}” price", allow_none=False, allow_negative=True),
            "price_type": _choice(item, "price_type", f"Option “{label}” charge type", PRICE_TYPES, "per_unit"),
            "requires_quote": _bool(item, "requires_quote", False),
            "active": _bool(item, "active", True),
        })
    return cleaned


def _field_values(product: CatalogProduct, payload: dict[str, Any], current: CatalogField | None) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    creating = current is None

    def wants(key: str) -> bool:
        return creating or key in payload

    values: dict[str, Any] = {}
    label = _text(payload, "label", "Field label", 80, required=True) if wants("label") else current["label"]  # type: ignore[index]
    if wants("label"):
        values["label"] = label
    if wants("field_key"):
        raw_key = payload.get("field_key") or slug(label).replace("-", "_")
        key = str(raw_key).strip().lower()
        if key and key[0].isdigit():
            key = f"f_{key}"
        if not FIELD_KEY_PATTERN.fullmatch(key):
            raise _bad("Field key must start with a letter and use only lowercase letters, numbers, and underscores (max 40).")
        clash = next((field for field in product["fields"] if field["field_key"] == key and (current is None or field["id"] != current["id"])), None)
        if clash:
            raise _bad(f"Another field on this product already uses the key “{key}”.", 409)
        if current and key != current["field_key"] and any(field["depends_on"] == current["field_key"] for field in product["fields"]):
            raise _bad("Other fields depend on this field's key. Update their conditions before renaming it.", 409)
        values["field_key"] = key
    field_type = _choice(payload, "field_type", "Field type", FIELD_TYPES, "select") if wants("field_type") else current["field_type"]  # type: ignore[index]
    if wants("field_type"):
        values["field_type"] = field_type
    if wants("required"):
        values["required"] = _bool(payload, "required", False)
    if wants("section"):
        values["section"] = _choice(payload, "section", "Form section", FIELD_SECTIONS, "")
    if wants("placeholder"):
        values["placeholder"] = _text(payload, "placeholder", "Placeholder", 160)
    if wants("help_text"):
        values["help_text"] = _text(payload, "help_text", "Help text", 300)
    for bound, bound_label in (("min_value", "Minimum value"), ("max_value", "Maximum value")):
        if wants(bound):
            raw_bound = payload.get(bound)
            number = None if raw_bound in (None, "") else _number(raw_bound)
            if raw_bound not in (None, "") and number is None:
                raise _bad(f"{bound_label} must be a number.")
            values[bound] = number
    low = values.get("min_value", current["min_value"] if current else None)
    high = values.get("max_value", current["max_value"] if current else None)
    if low is not None and high is not None and low > high:
        raise _bad("Minimum value cannot be greater than maximum value.")
    if wants("max_length"):
        max_length = payload.get("max_length")
        values["max_length"] = None if max_length in (None, "") else _int(payload, "max_length", "Maximum length", 1, 5000, 160)
    if wants("max_files"):
        values["max_files"] = _int(payload, "max_files", "Maximum files", 1, MAX_FILES_PER_FIELD, 1)
    if wants("depends_on"):
        depends_on = _text(payload, "depends_on", "Show-when field", 40)
        own_key = values.get("field_key", current["field_key"] if current else "")
        if depends_on:
            parent = next((field for field in product["fields"] if field["field_key"] == depends_on), None)
            if parent is None or depends_on == own_key:
                raise _bad("“Show only when” must name another field on this product.")
            if parent["field_type"] not in OPTION_FIELD_TYPES:
                raise _bad("“Show only when” must point to a dropdown, choice, or checkbox field.")
        values["depends_on"] = depends_on
    if wants("depends_values"):
        depends_values = payload.get("depends_values", [])
        if not isinstance(depends_values, list) or len(depends_values) > 50 or not all(isinstance(item, str) for item in depends_values):
            raise _bad("“Show only when” values must be a list of option values.")
        values["depends_values"] = [item.strip() for item in depends_values if item.strip()]
    if values.get("depends_on", current["depends_on"] if current else "") and not values.get("depends_values", current["depends_values"] if current else []):
        raise _bad("Choose which option values make this field appear.")
    if wants("active"):
        values["active"] = _bool(payload, "active", True)

    options: list[dict[str, Any]] | None = None
    if field_type in OPTION_FIELD_TYPES:
        if creating or "options" in payload or (current and current["field_type"] not in OPTION_FIELD_TYPES):
            options = _clean_options(payload.get("options"), current)
    elif current and current["options"]:
        options = []  # Field changed to a non-choice type: drop its old options.
    elif creating:
        options = []

    default_value = _text(payload, "default_value", "Default value", 160) if wants("default_value") else (current["default_value"] if current else "")
    if field_type in OPTION_FIELD_TYPES and default_value:
        option_values = {option["value"] for option in (options if options is not None else current["options"] if current else [])}
        if default_value not in option_values:
            raise _bad("The default value must be one of this field's option values.")
    if wants("default_value"):
        values["default_value"] = default_value
    return values, options


def create_field(product_id: str, payload: dict[str, Any]) -> CatalogProduct:
    product = admin_product(product_id)
    values, options = _field_values(product, payload, None)
    values["options"] = options or []
    values["sort_order"] = catalog_repository.next_sort_order("catalog_fields", "product_id", product_id)
    catalog_repository.create_field(product_id, values)
    return admin_product(product_id)


def update_field(field_id: str, payload: dict[str, Any]) -> CatalogProduct:
    current = catalog_repository.get_field(field_id)
    if current is None:
        raise _bad("Field not found.", 404)
    product = admin_product(current["product_id"])
    values, options = _field_values(product, payload, current)
    catalog_repository.update_field(field_id, values, options)
    return admin_product(current["product_id"])


def delete_field(field_id: str) -> CatalogProduct:
    current = catalog_repository.get_field(field_id)
    if current is None:
        raise _bad("Field not found.", 404)
    product = admin_product(current["product_id"])
    dependents = [field["label"] for field in product["fields"] if field["depends_on"] == current["field_key"]]
    if dependents:
        raise _bad(f"These fields only appear based on this one: {', '.join(dependents)}. Change or remove their condition first.", 409)
    catalog_repository.delete_field(field_id)
    return admin_product(current["product_id"])


def reorder_fields(product_id: str, payload: dict[str, Any]) -> CatalogProduct:
    product = admin_product(product_id)
    field_ids = payload.get("field_ids")
    known = [field["id"] for field in product["fields"]]
    if not isinstance(field_ids, list) or sorted(field_ids) != sorted(known):
        raise _bad("The field order must list every field of this product exactly once.")
    catalog_repository.reorder_fields(product_id, field_ids)
    return admin_product(product_id)
