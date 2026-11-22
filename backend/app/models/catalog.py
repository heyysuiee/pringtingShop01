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
# "options" holds choices that apply to package orders as well (for example paper finish and
# envelope); their price adjustments are added to the package price.
FIELD_SECTIONS = ("", "options", "design", "event", "upload")
# Field types a package order of a non-invitation product still collects (names, text, files):
# the package fixes the specifications, the customer only personalizes it.
PACKAGE_PERSONALIZATION_TYPES = ("text", "textarea", "file", "date", "time")

# unit: price per piece. area: price per square foot (needs width/height fields). quote: always quoted by the shop.
# size: the category's standard size price list (Tarpaulin: 2 x 3 ft = PHP 200, ...); each standard size has its
# own price per piece and "Custom size" is quoted by the shop.
PRICING_MODES = ("unit", "area", "size", "quote")
# How an option's price adjustment is applied.
PRICE_TYPES = ("per_unit", "per_order", "per_sqft")
# Whether the order form collects event details (celebrant, date, venue) for a product.
EVENT_MODES = ("none", "optional", "required")

# Reserved field keys used by area pricing.
AREA_WIDTH_KEY = "width"
AREA_HEIGHT_KEY = "height"
AREA_UNIT_KEY = "measurement_unit"
# The standard-size choice of a "size" priced product. It is not stored as a catalog field: the order form
# receives it from the category's standard size price list (standard_sizes table), so one price list serves
# every product of the category and prices are edited in one place.
STANDARD_SIZE_KEY = "standard_size"
CUSTOM_SIZE_VALUE = "custom"
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


class StandardSize(TypedDict):
    """One row of a category's standard size price list (for example Tarpaulin 2 x 3 ft, PHP 200)."""

    id: str
    category_id: str
    label: str
    width: float
    height: float
    unit: str
    price: float
    sort_order: int
    active: bool


class CatalogProduct(TypedDict):
    id: str
    category_id: str
    category_label: str
    name: str
    description: str
    # The primary image (first of ``images``), kept for the pages that show one picture.
    image: str
    images: list[str]
    pricing_mode: str
    base_price: float | None
    min_quantity: int
    max_quantity: int
    event_mode: str
    # Whether customers may submit a customized order (quotation request) instead of choosing a package.
    allow_customize: bool
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
    """A ready-made package (set) of one product with a fixed price and its own inclusions/add-on.

    The name is historical: packages started with Invitations and now exist for every service.
    """

    id: str
    product_id: str
    name: str
    description: str
    price: float
    size: str
    material: str
    # Inclusion names; inclusion_details has [{"name", "description"}] in the same order.
    inclusions: list[str]
    inclusion_details: list[dict[str, str]]
    add_on_label: str
    # Price of each piece above the minimum; None means the package is ordered at exactly the minimum.
    add_on_price: float | None
    # The minimum order (pieces covered by the base price). minimum_quantity is the same value.
    included_quantity: int
    minimum_quantity: int
    image: str
    images: list[str]
    sort_order: int
    active: bool


# The same record under its current meaning.
ProductPackage = InvitationPackage
