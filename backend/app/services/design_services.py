"""Shop-made design rules: what customers can choose, how they personalize it, and admin management.

A design belongs to one service category and, optionally, to specific products of it (no products = every
product of the category). A design order is a third ordering option next to packages and customized
orders: the customer picks a design, types the personalization details the design asks for, and chooses
the print specifications; no design file is uploaded. It is priced like a customized order (the shop's
rates, then the administrator's quotation).

Orders store a snapshot of the design (name, description, image, and the customer's details), and the
design's uploaded image is pinned, so editing, replacing, or deleting a design never changes an order.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException
from fastapi.responses import Response

from backend.app.database.design_seed import CATEGORY_DEFAULT_FIELDS, DEFAULT_FIELDS, DESIGN_FIELD_LIBRARY
from backend.app.models.catalog import CatalogProduct
from backend.app.repositories import catalog_repository, design_repository
from backend.app.services import catalog_services


_bad = catalog_services._bad
_text = catalog_services._text
_bool = catalog_services._bool
_int = catalog_services._int

MAX_FIELDS = len(DESIGN_FIELD_LIBRARY)
DESIGN_IMAGE_MAX_BYTES = 4 * 1024 * 1024
IMAGE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}(\.(jpg|png|webp))?$")


# ================================================================== field definitions


def field_definition(key: str, required: bool, index: int) -> dict[str, Any]:
    """A personalization question in the catalog-field shape (rendered and validated like product fields)."""
    spec = DESIGN_FIELD_LIBRARY[key]
    return {
        "id": f"design-field-{key}",
        "product_id": "",
        "field_key": key,
        "label": spec["label"],
        "field_type": spec["field_type"],
        "required": bool(required),
        "placeholder": spec.get("placeholder", ""),
        "help_text": spec.get("help_text", ""),
        "default_value": "",
        "min_value": spec.get("min_value"),
        "max_value": spec.get("max_value"),
        "max_length": spec.get("max_length"),
        "max_files": 0,
        "depends_on": "",
        "depends_values": [],
        "section": "",
        "sort_order": index,
        "active": True,
        "options": [],
    }


def resolved_fields(design: dict[str, Any]) -> list[dict[str, Any]]:
    """The design's personalization questions in order (unknown keys from older data are skipped)."""
    fields = []
    for item in design.get("fields", []):
        key = item.get("key") if isinstance(item, dict) else None
        if key in DESIGN_FIELD_LIBRARY and all(field["field_key"] != key for field in fields):
            fields.append(field_definition(key, bool(item.get("required")), len(fields)))
    return fields


def field_library() -> list[dict[str, Any]]:
    return [{"key": key, **{name: value for name, value in spec.items()}} for key, spec in DESIGN_FIELD_LIBRARY.items()]


# ================================================================== customer side


def applies_to(design: dict[str, Any], product: dict[str, Any]) -> bool:
    if design["category_id"] != product["category_id"]:
        return False
    return not design["product_ids"] or product["id"] in design["product_ids"]


def public_design(design: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": design["id"],
        "category_id": design["category_id"],
        "product_ids": design["product_ids"],
        "name": design["name"],
        "description": design["description"],
        "style": design["style"],
        "image": design["image"],
        "is_sample": design["is_sample"],
        "fields": resolved_fields(design),
    }


def attach_designs(categories: list[Any]) -> None:
    """Add ``designs`` (active, offered for the product) and ``design_field_keys`` to every product.

    Designs are offered only for products that accept customized orders, because a design order is
    priced the same way (the shop's rates, then a quotation).
    """
    designs = design_repository.list_designs(active_only=True)
    for category in categories:
        for product in category["products"]:
            offered = [public_design(design) for design in designs if applies_to(design, product)] if product.get("allow_customize", True) else []
            product["designs"] = offered
            product["design_field_keys"] = [field["field_key"] for field in catalog_services.design_fields(product)["fields"]]


def orderable_design(product: CatalogProduct, design_id: Any) -> dict[str, Any]:
    """The chosen design, confirmed to be active and offered for this exact product."""
    if not isinstance(design_id, str) or not design_id.strip():
        raise _bad("Choose one of the shop's designs.")
    design = design_repository.get_design(design_id.strip())
    if design is None or not design["active"] or not applies_to(design, product):
        raise _bad(f"The selected design is not available for {product['name']}. Choose another design.")
    return design


def personalization(design: dict[str, Any], raw: Any) -> list[dict[str, str]]:
    """Validate the customer's details for a design; returns readable [{key, label, value}] rows."""
    fields = resolved_fields(design)
    pseudo: Any = {"id": design["id"], "name": design["name"], "fields": fields}
    values = catalog_services._parse_selections(pseudo, raw if isinstance(raw, dict) else {}, strict=True)
    rows = []
    for field in fields:
        key = field["field_key"]
        if key in values:
            rows.append({"key": key, "label": field["label"], "value": catalog_services._display_value(field, values[key], values)})
    return rows


def order_snapshot(design: dict[str, Any], product: CatalogProduct, details: list[dict[str, str]]) -> dict[str, Any]:
    """What an order keeps about its design. The uploaded image is pinned so it is never deleted."""
    design_repository.pin_image(design["image"])
    return {
        "id": design["id"],
        "name": design["name"],
        "description": design["description"],
        "style": design["style"],
        "image": design["image"],
        "category_id": design["category_id"],
        "category_label": product["category_label"],
        "is_sample": design["is_sample"],
        "details": details,
    }


def design_image(filename: str) -> Response:
    if not IMAGE_ID_PATTERN.fullmatch(filename):
        raise _bad("Image not found.", 404)
    stored = design_repository.get_image(filename.split(".", 1)[0])
    if stored is None:
        raise _bad("Image not found.", 404)
    mime_type, contents = stored
    # Each upload gets a new id, so the bytes behind a URL never change.
    return Response(contents, media_type=mime_type, headers={
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    })


# ================================================================== admin


def _catalog_index() -> tuple[list[Any], dict[str, Any], dict[str, Any]]:
    categories = catalog_repository.load_catalog(active_only=False)
    by_category = {category["id"]: category for category in categories}
    by_product = {product["id"]: product for category in categories for product in category["products"]}
    return categories, by_category, by_product


def _admin_view(design: dict[str, Any], by_category: dict[str, Any], by_product: dict[str, Any]) -> dict[str, Any]:
    category = by_category.get(design["category_id"])
    products = [by_product[product_id] for product_id in design["product_ids"] if product_id in by_product]
    return {
        **design,
        "fields": [{"key": field["field_key"], "label": field["label"], "required": field["required"], "field_type": field["field_type"]}
                   for field in resolved_fields(design)],
        "category_label": category["label"] if category else "(removed category)",
        "category_active": bool(category and category["active"]),
        "product_names": [product["name"] for product in products],
        "uploaded_image": design_repository.image_id_from_url(design["image"]) is not None,
    }


def admin_designs() -> dict[str, Any]:
    categories, by_category, by_product = _catalog_index()
    designs = [_admin_view(design, by_category, by_product) for design in design_repository.list_designs(active_only=False)]
    return {
        "designs": designs,
        "services": [
            {
                "id": category["id"],
                "label": category["label"],
                "active": category["active"],
                "products": [{"id": product["id"], "name": product["name"], "allow_customize": product.get("allow_customize", True)}
                             for product in category["products"]],
                "default_fields": CATEGORY_DEFAULT_FIELDS.get(category["id"], DEFAULT_FIELDS),
            }
            for category in categories
        ],
        "field_library": field_library(),
        "sample_count": sum(1 for design in designs if design["is_sample"]),
    }


def _fields_value(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_FIELDS:
        raise _bad("Choose the personalization details from the list.")
    cleaned: list[dict[str, Any]] = []
    for item in value:
        key = item.get("key") if isinstance(item, dict) else item
        if key not in DESIGN_FIELD_LIBRARY:
            raise _bad("Unknown personalization detail. Choose from the list.")
        if any(existing["key"] == key for existing in cleaned):
            continue
        cleaned.append({"key": key, "required": bool(item.get("required")) if isinstance(item, dict) else False})
    if not cleaned:
        raise _bad("Choose at least one detail the customer fills in to personalize this design.")
    return cleaned


def _design_values(payload: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    creating = current is None
    _, by_category, by_product = _catalog_index()

    def wants(key: str) -> bool:
        return creating or key in payload

    values: dict[str, Any] = {}
    if wants("name"):
        values["name"] = _text(payload, "name", "Design name", 120, required=True)
    if wants("description"):
        values["description"] = _text(payload, "description", "Design description", 600)
    if wants("style"):
        values["style"] = _text(payload, "style", "Style", 60)
    category_id = current["category_id"] if current else ""
    if wants("category_id"):
        category_id = payload.get("category_id")
        if not isinstance(category_id, str) or category_id not in by_category:
            raise _bad("Choose the service category this design belongs to.")
        values["category_id"] = category_id
    if wants("product_ids") or "category_id" in values:
        keep = current["product_ids"] if current and current["category_id"] == category_id else []
        product_ids = payload.get("product_ids", keep)
        if not isinstance(product_ids, list) or any(not isinstance(item, str) for item in product_ids):
            raise _bad("Choose the products this design is offered for.")
        unknown = [item for item in product_ids if item not in by_product or by_product[item]["category_id"] != category_id]
        if unknown:
            raise _bad("Every selected product must belong to the design's service category.")
        values["product_ids"] = list(dict.fromkeys(product_ids))
    if wants("fields"):
        values["fields"] = _fields_value(payload.get("fields"))
    if wants("active"):
        values["active"] = _bool(payload, "active", True)
    if "is_sample" in payload:
        values["is_sample"] = _bool(payload, "is_sample", False)
    if "sort_order" in payload:
        values["sort_order"] = _int(payload, "sort_order", "Sort order", 0, 10_000, 0)
    upload = payload.get("image_upload")
    if upload:
        mime_type, extension, contents = catalog_services.decode_catalog_image(upload, "Design", DESIGN_IMAGE_MAX_BYTES)
        values["image"] = design_repository.save_image(mime_type, contents, extension)
    elif creating:
        raise _bad("Upload the design image customers will choose from.")
    return values


def create_design(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _bad("Invalid design.")
    values = _design_values(payload, None)
    values.setdefault("sort_order", design_repository.next_sort_order())
    values.setdefault("is_sample", False)
    design_id = design_repository.create_design(values)
    return {**admin_designs(), "design_id": design_id}


def _existing(design_id: str) -> dict[str, Any]:
    design = design_repository.get_design(design_id)
    if design is None:
        raise _bad("Design not found.", 404)
    return design


def update_design(design_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _bad("Invalid design.")
    current = _existing(design_id)
    values = _design_values(payload, current)
    design_repository.update_design(design_id, values)
    if "image" in values and values["image"] != current["image"]:
        # The old picture is removed unless an order was placed with it.
        design_repository.discard_image(current["image"])
    return admin_designs()


def delete_design(design_id: str) -> dict[str, Any]:
    current = _existing(design_id)
    design_repository.delete_designs([design_id])
    design_repository.discard_image(current["image"])
    return admin_designs()


def delete_sample_designs() -> dict[str, Any]:
    """Remove every TEMPORARY SAMPLE design at once (orders keep their own snapshot)."""
    samples = [design for design in design_repository.list_designs(active_only=False) if design["is_sample"]]
    design_repository.delete_designs([design["id"] for design in samples])
    for design in samples:
        design_repository.discard_image(design["image"])
    return {**admin_designs(), "deleted": len(samples)}
