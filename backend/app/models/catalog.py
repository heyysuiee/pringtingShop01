from __future__ import annotations

from typing import TypedDict


# Field types the order form knows how to render and the server knows how to validate.
OPTION_FIELD_TYPES = ("select", "radio", "checkboxes")
VALUE_FIELD_TYPES = ("text", "textarea", "number", "date", "time")
FILE_FIELD_TYPES = ("file",)
FIELD_TYPES = OPTION_FIELD_TYPES + VALUE_FIELD_TYPES + FILE_FIELD_TYPES

# Which part of the order form a field belongs to. "" is the basic specification section.
# The Invitation ordering process uses these to build its multi-part form and to let a package
# order skip the specification/design sections while still collecting the event and uploads.
FIELD_SECTIONS = ("", "design", "event", "upload")

# unit: price per piece. area: price per square foot (needs width/height fields). quote: always quoted by the shop.
PRICING_MODES = ("unit", "area", "quote")
# How an option's price adjustment is applied.
PRICE_TYPES = ("per_unit", "per_order", "per_sqft")
# Whether the order form collects event details (celebrant, date, venue) for a product.
EVENT_MODES = ("none", "optional", "required")

# Reserved field keys used by area pricing.
AREA_WIDTH_KEY = "width"
AREA_HEIGHT_KEY = "height"
AREA_UNIT_KEY = "measurement_unit"
# Conversion of one unit to feet.
FEET_PER_UNIT = {"ft": 1.0, "in": 1 / 12, "cm": 1 / 30.48, "m": 3.280839895}


class FieldOption(TypedDict):
    id: str
    field_id: str
    value: str
    label: str
    price_adjustment: float
    price_type: str
    requires_quote: bool
    sort_order: int
    active: bool


class CatalogField(TypedDict):
    id: str
    product_id: str
    field_key: str
    label: str
    field_type: str
    required: bool
    placeholder: str
    help_text: str
    default_value: str
    min_value: float | None
    max_value: float | None
    max_length: int | None
    max_files: int
    depends_on: str
    depends_values: list[str]
    section: str
    sort_order: int
    active: bool
    options: list[FieldOption]


class CatalogProduct(TypedDict):
    id: str
    category_id: str
    category_label: str
    name: str
    description: str
    image: str
    pricing_mode: str
    base_price: float | None
    min_quantity: int
    max_quantity: int
    event_mode: str
    sort_order: int
    active: bool
    fields: list[CatalogField]


class CatalogCategory(TypedDict):
    id: str
    label: str
    description: str
    sort_order: int
    active: bool
    products: list[CatalogProduct]


class InvitationPackage(TypedDict):
    """A ready-made invitation set with a fixed price and its own inclusions/add-on."""

    id: str
    product_id: str
    name: str
    price: float
    size: str
    material: str
    inclusions: list[str]
    add_on_label: str
    add_on_price: float | None
    included_quantity: int
    image: str
    sort_order: int
    active: bool
