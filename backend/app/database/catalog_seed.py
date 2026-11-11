"""Initial product catalog and per-product order-form configuration.

This is inserted once, the first time the catalog tables are created. After that the administrator owns
the catalog: products, fields, options, and prices are edited from the admin pages and stored in the
database. No prices are seeded. Every product starts with ``base_price = None`` ("Request a quote") and
every option with no surcharge, until an administrator enters real prices.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "option"


# An option can be a plain label ("Bi-fold") or a full option from opt() (for example one that needs a quote).
OptionSpec = str | dict[str, Any]


def opt(label: str, value: str | None = None, quote: bool = False, price_type: str = "per_unit") -> dict[str, Any]:
    return {"label": label, "value": value or slug(label), "requires_quote": quote, "price_type": price_type}


def choice(
    key: str,
    label: str,
    options: Sequence[OptionSpec],
    *,
    kind: str = "select",
    required: bool = True,
    help_text: str = "",
    default: str = "",
    depends_on: str = "",
    depends_values: list[str] | None = None,
    price_type: str = "per_unit",
) -> dict[str, Any]:
    built = []
    for item in options:
        entry = opt(item) if isinstance(item, str) else dict(item)
        if isinstance(item, str):
            entry["price_type"] = price_type
        built.append(entry)
    return {
        "field_key": key,
        "label": label,
        "field_type": kind,
        "required": required,
        "help_text": help_text,
        "default_value": default,
        "depends_on": depends_on,
        "depends_values": depends_values or [],
        "options": built,
    }


def text(
    key: str,
    label: str,
    *,
    required: bool = False,
    multiline: bool = False,
    placeholder: str = "",
    help_text: str = "",
    max_length: int | None = None,
    depends_on: str = "",
    depends_values: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "field_key": key,
        "label": label,
        "field_type": "textarea" if multiline else "text",
        "required": required,
        "placeholder": placeholder,
        "help_text": help_text,
        "max_length": max_length or (1500 if multiline else 160),
        "depends_on": depends_on,
        "depends_values": depends_values or [],
    }


def number(key: str, label: str, *, minimum: float, maximum: float, required: bool = True, help_text: str = "", placeholder: str = "") -> dict[str, Any]:
    return {
        "field_key": key,
        "label": label,
        "field_type": "number",
        "required": required,
        "min_value": minimum,
        "max_value": maximum,
        "help_text": help_text,
        "placeholder": placeholder,
    }


def upload(
    key: str,
    label: str,
    *,
    required: bool = False,
    max_files: int = 3,
    help_text: str = "JPG, PNG, or PDF · up to 5 MB each.",
    depends_on: str = "",
    depends_values: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "field_key": key,
        "label": label,
        "field_type": "file",
        "required": required,
        "max_files": max_files,
        "help_text": help_text,
        "depends_on": depends_on,
        "depends_values": depends_values or [],
    }


def product(
    product_id: str,
    name: str,
    description: str,
    fields: list[dict[str, Any]],
    *,
    image: str = "",
    pricing_mode: str = "unit",
    event_mode: str = "none",
    min_quantity: int = 1,
    max_quantity: int = 10000,
) -> dict[str, Any]:
    return {
        "id": product_id,
        "name": name,
        "description": description,
        "image": image,
        "pricing_mode": pricing_mode,
        "base_price": None,
        "event_mode": event_mode,
        "min_quantity": min_quantity,
        "max_quantity": max_quantity,
        "fields": fields,
    }


IMG = "/frontend/image"

# ---------------------------------------------------------------- Invitation

ORIENTATION = choice("orientation", "Orientation", ["Portrait", "Landscape"], kind="radio", default="portrait")

INVITATION_WORDING = text(
    "invitation_wording",
    "Invitation wording",
    multiline=True,
    required=True,
    placeholder="Names, program, RSVP contact, dress code, and any other text to print",
)


def date_field(key: str, label: str) -> dict[str, Any]:
    return {"field_key": key, "label": label, "field_type": "date", "required": True}


def time_field(key: str, label: str) -> dict[str, Any]:
    return {"field_key": key, "label": label, "field_type": "time", "required": True}


def invitation_fields(
    *,
    paper: Sequence[OptionSpec],
    folds: Sequence[OptionSpec],
    printing: Sequence[OptionSpec],
    sizes: Sequence[OptionSpec],
    envelopes: Sequence[OptionSpec],
    event_fields: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The Invitation order form, grouped into the sections the order page renders.

    ``""`` basic specifications, ``"design"`` customization, ``"event"`` category-specific event
    details (including the wording), and ``"upload"`` photo/reference files. A package order skips
    the basic and design sections; the server re-validates only the event and upload sections.
    """
    basics = [
        choice("invitation_size", "Invitation size", sizes),
        choice("paper_type", "Paper type", paper),
        choice("paper_finish", "Paper finish", ["Matte", "Glossy", "Satin", "Uncoated (natural texture)"]),
        choice("printing_type", "Printing type", printing),
        ORIENTATION,
        choice("folding_type", "Style / format", folds),
        choice("envelope_option", "Envelope preference", envelopes, default="no-envelope"),
        text(
            "envelope_color",
            "Envelope color",
            required=True,
            placeholder="e.g. Sage green, blush pink",
            depends_on="envelope_option",
            depends_values=["colored-envelope"],
        ),
        number(
            "insert_count",
            "Number of photo / page inserts",
            minimum=0,
            maximum=20,
            required=False,
            help_text="Leave blank if you are not sure yet.",
        ),
    ]
    design = [
        choice(
            "design_source",
            "Design customization",
            ["I will upload my own design", "Shop will design it for me", "Use a sample design as reference"],
            kind="radio",
        ),
        upload(
            "design_file",
            "Your design file",
            required=True,
            max_files=2,
            help_text="Upload your print-ready design. JPG, PNG, or PDF · up to 5 MB each.",
            depends_on="design_source",
            depends_values=["i-will-upload-my-own-design"],
        ),
        text("design_theme", "Theme", placeholder="e.g. Floral garden, geometric, minimalist"),
        text("color_combination", "Color combination", placeholder="e.g. Sage green, cream, and gold"),
        text("design_inspiration", "Design inspiration", multiline=True, placeholder="Styles, motifs, samples, or fonts you like"),
    ]
    uploads = [
        upload("photo_upload", "Photos of the celebrant / couple", max_files=6, help_text="JPG, PNG, or PDF · up to 5 MB each. Up to 6 photos."),
        upload("reference_design", "Reference designs", max_files=3, help_text="Existing invitations or layouts you want us to follow."),
        upload("inspiration_images", "Inspiration images", max_files=3, help_text="Pinterest screenshots, color pegs, or any idea you like."),
    ]
    fields: list[dict[str, Any]] = []
    for field in basics:
        fields.append({**field, "section": ""})
    for field in design:
        fields.append({**field, "section": "design"})
    for field in [*event_fields, INVITATION_WORDING]:
        fields.append({**field, "section": "event"})
    for field in uploads:
        fields.append({**field, "section": "upload"})
    return fields


INVITATION_SIZES = ["4 x 6 in", "5 x 7 in", "A5 (5.8 x 8.3 in)", "Square 5 x 5 in", opt("Custom size", quote=True)]

BIRTHDAY_EVENT = [
    text("celebrant_name", "Celebrant name", required=True, placeholder="e.g. Mia Santos"),
    number("celebrant_age", "Age to celebrate", minimum=1, maximum=120, required=True, placeholder="e.g. 7"),
    date_field("event_date", "Event date"),
    time_field("event_time", "Event time"),
    text("venue", "Venue", required=True, placeholder="e.g. Quezon City"),
]

CHRISTENING_EVENT = [
    text("child_name", "Child's name", required=True, placeholder="e.g. Noah Miguel Santos"),
    text("parents_names", "Parents' names", required=True, placeholder="e.g. Mark and Anna Santos"),
    text("godparents_names", "Godparents", multiline=True, placeholder="One name per line or separated by commas"),
    date_field("baptism_date", "Baptism date"),
    time_field("baptism_time", "Baptism time"),
    text("church", "Church", required=True, placeholder="e.g. St. Joseph Parish"),
    text("reception_venue", "Reception venue", placeholder="e.g. Santos Residence, Pasig City"),
]

WEDDING_EVENT = [
    text("bride_name", "Bride's name", required=True, placeholder="e.g. Maria Santos"),
    text("groom_name", "Groom's name", required=True, placeholder="e.g. Juan Dela Cruz"),
    date_field("wedding_date", "Wedding date"),
    time_field("ceremony_time", "Ceremony time"),
    text("ceremony_venue", "Ceremony venue", required=True, placeholder="e.g. Manila Cathedral"),
    text("reception_venue", "Reception venue", required=True, placeholder="e.g. The Peninsula Manila"),
    text("entourage", "Entourage", multiline=True, placeholder="Principal sponsors, bridesmaids, groomsmen, and other names"),
]

INVITATIONS = [
    product(
        "birthday-invitation",
        "Birthday Invitation",
        "A cheerful invitation for birthdays and milestones.",
        invitation_fields(
            sizes=INVITATION_SIZES,
            paper=["Standard cardstock", "Premium cardstock", "Linen textured cardstock", "Pearlescent cardstock", "Kraft cardstock"],
            folds=["Flat card (no fold)", "Bi-fold", "Tri-fold"],
            printing=["Single-sided, full color", "Double-sided, full color"],
            envelopes=["No envelope", "Plain white envelope", "Colored envelope", "Kraft envelope"],
            event_fields=BIRTHDAY_EVENT,
        ),
        image=f"{IMG}/invitation/Bdayinvitation.jpg",
        event_mode="required",
        min_quantity=10,
    ),
    product(
        "wedding-invitation",
        "Wedding Invitation",
        "Elegant stationery for wedding celebrations.",
        invitation_fields(
            sizes=INVITATION_SIZES + ["Square 6 x 6 in"],
            paper=["Premium cardstock", "Linen textured cardstock", "Pearlescent cardstock", "Kraft cardstock", "Vellum overlay with cardstock"],
            folds=["Flat card (no fold)", "Bi-fold", "Gatefold", "Tri-fold", opt("Pocket fold", quote=True)],
            printing=["Single-sided, full color", "Double-sided, full color", "Full color with foil accents"],
            envelopes=["No envelope", "Plain white envelope", "Colored envelope", "Kraft envelope", "Vellum envelope"],
            event_fields=WEDDING_EVENT,
        ),
        image=f"{IMG}/invitation/weddinginvitationj.jpg",
        event_mode="required",
        min_quantity=10,
    ),
    product(
        "christening-invitation",
        "Christening Invitation",
        "A gentle invitation for a christening or baptism day.",
        invitation_fields(
            sizes=INVITATION_SIZES,
            paper=["Standard cardstock", "Premium cardstock", "Linen textured cardstock", "Pearlescent cardstock"],
            folds=["Flat card (no fold)", "Bi-fold"],
            printing=["Single-sided, full color", "Double-sided, full color"],
            envelopes=["No envelope", "Plain white envelope", "Colored envelope"],
            event_fields=CHRISTENING_EVENT,
        ),
        image=f"{IMG}/invitation/christening.jpg",
        event_mode="required",
        min_quantity=10,
    ),
]

# ---------------------------------------------------------------- Invitation packages

# The exact sets offered for the three invitation categories. ``add_on_price`` is the charge for each
# additional piece beyond ``included_quantity``; when it is None the set is ordered at exactly that
# quantity. Prices are the shop's published package prices.
INVITATION_PACKAGES: list[dict[str, Any]] = [
    {
        "id": "birthday-set-a",
        "product_id": "birthday-invitation",
        "name": "Set A — Birthday Card",
        "price": 300.00,
        "size": '4R (4" × 6")',
        "material": "Premium photo paper",
        "inclusions": [
            "20 pcs 4R (4\" × 6\") birthday cards",
            "1 photo insert",
            "White envelope",
            "Sticker lock",
            "Premium photo paper",
        ],
        "add_on_label": "Additional set (20 pcs)",
        "add_on_price": 15.00,
        "included_quantity": 20,
        "image": f"{IMG}/invitation/Bdayinvitation.jpg",
        "sort_order": 0,
    },
    {
        "id": "christening-set-a",
        "product_id": "christening-invitation",
        "name": "Set A — Christening Card",
        "price": 600.00,
        "size": '4R (4" × 6")',
        "material": "Premium photo paper",
        "inclusions": [
            "20 pcs 4R (4\" × 6\") christening cards",
            "2 photo inserts",
            "White envelope",
            "Sticker lock",
            "Premium photo paper",
        ],
        "add_on_label": "Additional set (20 pcs)",
        "add_on_price": 30.00,
        "included_quantity": 20,
        "image": f"{IMG}/invitation/christening.jpg",
        "sort_order": 0,
    },
    {
        "id": "christening-set-b",
        "product_id": "christening-invitation",
        "name": "Set B — Geometric Flip Invitation",
        "price": 2000.00,
        "size": '5" × 7"',
        "material": "",
        "inclusions": [
            "20 pcs 5\" × 7\" invitations",
            "20 geometric covers with glittered finish",
            "Tag and parchment",
            "3 page inserts",
            "Free layout",
            "2 revisions",
        ],
        "add_on_label": "",
        "add_on_price": None,
        "included_quantity": 20,
        "image": f"{IMG}/invitation/christening.jpg",
        "sort_order": 1,
    },
    {
        "id": "wedding-set-a",
        "product_id": "wedding-invitation",
        "name": "Set A — Floral Theme + Premium Envelope",
        "price": 1800.00,
        "size": '5" × 7"',
        "material": "",
        "inclusions": [
            "20 pcs 5\" × 7\" invitations",
            "20 premium envelopes",
            "Pearl, ribbon, and parchment details",
            "3 page inserts",
            "Free layout",
            "2 revisions",
        ],
        "add_on_label": "",
        "add_on_price": None,
        "included_quantity": 20,
        "image": f"{IMG}/invitation/weddinginvitationj.jpg",
        "sort_order": 0,
    },
    {
        "id": "wedding-set-b",
        "product_id": "wedding-invitation",
        "name": "Set B — Passport Theme",
        "price": 3000.00,
        "size": "",
        "material": "",
        "inclusions": [
            "20 invitations",
            "Free layout",
            "2 revisions",
        ],
        "add_on_label": "",
        "add_on_price": None,
        "included_quantity": 20,
        "image": f"{IMG}/invitation/weddinginvitationj.jpg",
        "sort_order": 1,
    },
    {
        "id": "wedding-set-c",
        "product_id": "wedding-invitation",
        "name": "Set C — Laser Cut Floral",
        "price": 2400.00,
        "size": '5" × 7"',
        "material": "",
        "inclusions": [
            "20 pcs 5\" × 7\" invitations",
            "20 laser-cut envelopes",
            "Tag and ribbon",
            "2 page inserts",
            "Free layout",
            "2 revisions",
        ],
        "add_on_label": "",
        "add_on_price": None,
        "included_quantity": 20,
        "image": f"{IMG}/invitation/weddinginvitationj.jpg",
        "sort_order": 2,
    },
]

# ---------------------------------------------------------------- Souvenir

PERSONALIZATION = text(
    "personalization",
    "Personalization text",
    multiline=True,
    placeholder="Names, dates, messages, or other text to print",
)


def design_upload(required: bool = False, label: str = "Design or photo upload", max_files: int = 3) -> dict[str, Any]:
    return upload("design_upload", label, required=required, max_files=max_files)


def packaging(options: list[str]) -> dict[str, Any]:
    return choice("packaging", "Packaging", options, default=slug(options[0]))


SOUVENIRS = [
    product(
        "refrigerator-magnet",
        "Ref Magnet",
        "Personalized refrigerator magnets for giveaways and keepsakes.",
        [
            choice("size", "Size", ["2 x 3 in", "2.5 x 3.5 in", "3 x 4 in", opt("Custom size", quote=True)]),
            choice("shape", "Shape", ["Rectangle", "Square", "Round", "Heart", opt("Custom die-cut shape", quote=True)]),
            choice("material", "Material", ["Flexible rubber magnet", "Acrylic magnet", "Laminated photo-paper magnet"]),
            choice("finish", "Finish", ["Glossy lamination", "Matte lamination"]),
            PERSONALIZATION,
            design_upload(),
            packaging(["No packaging", "Individual clear plastic sleeve", "Gift box with ribbon"]),
        ],
        image=f"{IMG}/souvenir/refmagnet.jpg",
        event_mode="optional",
    ),
    product(
        "acrylic-keychain",
        "Acrylic Keychain",
        "Custom acrylic keychains with your own design.",
        [
            choice("size", "Size", ["1.5 in", "2 in", "2.5 in", "3 in"]),
            choice("shape", "Shape", ["Round", "Square", "Rectangle", "Heart", opt("Custom cut following the design", quote=True)]),
            choice("material", "Acrylic type", ["Clear acrylic", "Frosted acrylic", "Glitter acrylic"]),
            choice("print_sides", "Printed sides", ["One side", "Both sides"], kind="radio", default="one-side"),
            choice("ring_type", "Ring / attachment", ["Silver split ring", "Lobster clasp with chain", "Ball chain"]),
            PERSONALIZATION,
            design_upload(),
            packaging(["No packaging", "Clear plastic pouch", "Gift box"]),
        ],
        image=f"{IMG}/souvenir/keychain.jpg",
        event_mode="optional",
    ),
    product(
        "personalized-nail-cutter",
        "Personalized Nail Cutter",
        "A practical keepsake with a personalized printed label.",
        [
            choice("size", "Size", ["Small (approx. 6 cm)", "Large (approx. 8 cm)"]),
            choice("color", "Body color", ["Silver", "Gold", "Rose gold", "Black"]),
            choice("print_method", "Personalization method", ["Printed label on the handle", "Full-color printed handle"]),
            PERSONALIZATION,
            design_upload(),
            packaging(["Plastic pouch", "Window box", "Organza pouch with tag"]),
        ],
        event_mode="optional",
    ),
    product(
        "white-mug",
        "White Mug",
        "Classic white ceramic mug with a full-color print.",
        [
            choice("size", "Capacity", ["11 oz", "15 oz"]),
            choice("print_area", "Print area", ["One side", "Both sides", "Full wrap (360°)"], kind="radio", default="one-side"),
            choice("inner_color", "Inner and handle color", ["White (classic)", "Black", "Red", "Blue", "Pink", "Yellow"]),
            PERSONALIZATION,
            design_upload(),
            packaging(["No packaging", "White mug box", "Window box with ribbon"]),
        ],
        image=f"{IMG}/souvenir/whitemug.jpg",
        event_mode="optional",
    ),
    product(
        "glass-mug",
        "Glass Mug",
        "A personalized glass mug, clear or frosted.",
        [
            choice("size", "Capacity", ["11 oz", "15 oz"]),
            choice("material", "Glass type", ["Clear glass", "Frosted glass"]),
            choice("print_area", "Print area", ["One side", "Both sides", "Full wrap (360°)"], kind="radio", default="one-side"),
            PERSONALIZATION,
            design_upload(),
            packaging(["No packaging", "Mug box", "Window box with ribbon"]),
        ],
        image=f"{IMG}/souvenir/glassmug.jpg",
        event_mode="optional",
    ),
    product(
        "nordic-mug",
        "Nordic Mug",
        "A Nordic-style ceramic mug in soft colors.",
        [
            choice("color", "Mug color", ["White", "Pastel pink", "Pastel blue", "Pastel green", "Pastel yellow", "Gray"]),
            choice("lid_option", "Lid and spoon", ["Mug only", "With bamboo lid", "With bamboo lid and spoon"]),
            choice("print_area", "Print area", ["One side", "Both sides"], kind="radio", default="one-side"),
            PERSONALIZATION,
            design_upload(),
            packaging(["No packaging", "Mug box", "Window box with ribbon"]),
        ],
        event_mode="optional",
    ),
    product(
        "personalized-tote-bag",
        "Personalized Tote Bag",
        "A custom-printed tote for gifts or everyday use.",
        [
            choice("size", "Size", ["Small (approx. 10 x 12 in)", "Medium (approx. 12 x 14 in)", "Large (approx. 14 x 16 in)"]),
            choice("material", "Material", ["Canvas", "Katsa (cotton)", "Non-woven fabric"]),
            choice("color", "Bag color", ["Natural / cream", "White", "Black"]),
            choice("print_area", "Print area", ["Front only", "Front and back"], kind="radio", default="front-only"),
            choice("print_type", "Print type", ["Full-color heat transfer", "Single-color vinyl"]),
            PERSONALIZATION,
            design_upload(),
            packaging(["Folded, no packaging", "Folded in clear plastic"]),
        ],
        image=f"{IMG}/souvenir/totebag.jpg",
        event_mode="optional",
    ),
    product(
        "coloring-book",
        "Coloring Book",
        "A personalized coloring book with a custom cover.",
        [
            choice("size", "Book size", ["A5 (5.8 x 8.3 in)", "A4 (8.3 x 11.7 in)"]),
            choice("page_count", "Number of pages", ["8 pages", "12 pages", "16 pages", "20 pages", "24 pages"]),
            choice("cover_finish", "Cover finish", ["Glossy cover", "Matte cover"]),
            choice("inner_paper", "Inside paper", ["Bond paper", "Book paper"]),
            choice("binding", "Binding", ["Saddle stitch (stapled)", "Ring binding"]),
            choice("coloring_set", "Coloring materials", ["Book only", "With crayons pack"]),
            text("theme", "Theme / content", required=True, placeholder="e.g. Dinosaurs, princesses, farm animals"),
            PERSONALIZATION,
            design_upload(label="Cover photo or design"),
            packaging(["No packaging", "Clear plastic sleeve", "Bundled with ribbon"]),
        ],
        event_mode="optional",
    ),
]

# ---------------------------------------------------------------- Sticker

STICKER_SIZES = ["1 x 1 in", "1.5 x 1.5 in", "2 x 2 in", "3 x 3 in", "4 x 4 in", opt("Custom size", quote=True)]
STICKER_MATERIALS = [
    opt("White vinyl", "white-vinyl"),
    opt("Clear vinyl (transparent)", "clear-vinyl"),
    opt("Holographic vinyl", "holographic-vinyl"),
    opt("Sticker paper (non-waterproof)", "sticker-paper"),
]
VINYL_MATERIALS = ["white-vinyl", "clear-vinyl", "holographic-vinyl"]
STICKER_FINISH = choice("finish", "Finish", ["Glossy lamination", "Matte lamination", "No lamination"])
WATERPROOF = choice(
    "waterproof",
    "Waterproof option",
    ["Waterproof", "Standard (not waterproof)"],
    kind="radio",
    help_text="Available for vinyl materials.",
    depends_on="material",
    depends_values=VINYL_MATERIALS,
)
CUT_TYPE = choice("cut_type", "Cut type", ["Kiss cut (on sheet)", "Die cut (individually cut)", "Sheet (square trim, uncut)"])

STICKERS = [
    product(
        "business-logo-stickers",
        "Business Logo Stickers",
        "Brand stickers for packaging, giveaways, and storefronts.",
        [
            choice("size", "Size", STICKER_SIZES),
            choice("shape", "Shape", ["Round", "Square", "Rectangle", "Oval", "Custom die-cut (follows logo)"]),
            choice("material", "Material", STICKER_MATERIALS),
            STICKER_FINISH,
            WATERPROOF,
            CUT_TYPE,
            upload("design_upload", "Logo / design upload", required=True, help_text="Upload your logo (PNG with transparent background or PDF preferred)."),
            text("design_notes", "Design notes", multiline=True, placeholder="Colors, text to add, or layout notes"),
        ],
        image=f"{IMG}/Sticker/sticker.jpg",
        min_quantity=10,
    ),
    product(
        "product-label-stickers",
        "Product Label Stickers",
        "Labels for jars, bottles, boxes, and small-business products.",
        [
            choice("size", "Size", STICKER_SIZES + ["2 x 3 in", "3 x 4 in"]),
            choice("shape", "Shape", ["Round", "Square", "Rectangle", "Oval", "Custom die-cut"]),
            choice("material", "Material", STICKER_MATERIALS + [opt("Kraft paper", "kraft-paper")]),
            STICKER_FINISH,
            WATERPROOF,
            CUT_TYPE,
            upload("design_upload", "Label design upload", required=True),
            text("label_text", "Label text", multiline=True, placeholder="Product name, ingredients, net weight, or other text"),
        ],
        min_quantity=10,
    ),
    product(
        "name-stickers",
        "Name Stickers",
        "Personalized name stickers for school supplies, gifts, and events.",
        [
            choice("size", "Size", ["Small (0.5 x 1.5 in)", "Medium (1 x 2.5 in)", "Large (1.5 x 3 in)"]),
            choice("shape", "Shape", ["Rectangle", "Rounded rectangle", "Oval"]),
            choice("material", "Material", STICKER_MATERIALS),
            STICKER_FINISH,
            WATERPROOF,
            CUT_TYPE,
            text("names", "Names to print", multiline=True, required=True, placeholder="One name per line"),
            text("theme", "Theme / design", placeholder="e.g. Dinosaurs, pastel florals"),
            design_upload(),
        ],
        event_mode="optional",
        min_quantity=1,
    ),
    product(
        "wall-stickers-decals",
        "Wall Stickers / Decals",
        "Decorative wall decals for rooms, shops, and displays.",
        [
            choice("size", "Size", ["12 x 12 in", "12 x 24 in", "24 x 24 in", "24 x 36 in", opt("Custom size", quote=True)]),
            choice("shape", "Shape", ["Square", "Rectangle", "Round", "Contour (follows design)"]),
            choice("material", "Material", ["Matte wall vinyl", "Glossy wall vinyl", "Removable vinyl"]),
            choice("finish", "Finish", ["Matte", "Glossy"]),
            choice("cut_type", "Cut type", ["Contour cut", "Square / rectangle trim", "Lettering cut (no background)"]),
            upload("design_upload", "Design upload"),
            text("design_notes", "Design description", multiline=True, required=True, placeholder="Describe the decal, wall color, and placement"),
        ],
    ),
]

# ---------------------------------------------------------------- Sintra Board

BOARD_SIZES = ["A4 (8.3 x 11.7 in)", "A3 (11.7 x 16.5 in)", "12 x 18 in", "18 x 24 in", "24 x 36 in", "3 x 4 ft", opt("Custom size", quote=True)]
THICKNESS = choice("thickness", "Thickness", ["3 mm", "5 mm"], kind="radio", default="3-mm")
BOARD_MATERIAL = choice("material", "Print material", ["Glossy vinyl sticker print", "Matte vinyl sticker print", "Laminated photo-paper print"])
STAND = choice("stand_option", "Stand option", ["No stand", "Easel stand", "Foldable back stand", "Wall-hanging hooks"])


def board_fields(*, design_types: list[str], extra: list[dict[str, Any]], photo_required: bool, photo_max: int) -> list[dict[str, Any]]:
    return [
        choice("board_size", "Board size", BOARD_SIZES),
        THICKNESS,
        ORIENTATION,
        BOARD_MATERIAL,
        choice("design_type", "Design type", design_types),
        STAND,
        *extra,
        upload("photo_upload", "Photo upload", required=photo_required, max_files=photo_max),
    ]


SIGN_DESIGNS = ["Upload my print-ready design", "Shop layout using my text and photos", "Customize a sample design"]
SIGN_TEXT = text("sign_text", "Text to display", multiline=True, required=True, placeholder="Names, greeting, date, and other text on the board")

SINTRA_BOARDS = [
    product(
        "birthday-welcome-signs",
        "Birthday / Welcome Signs",
        "Welcome signs for parties and celebrations.",
        board_fields(design_types=SIGN_DESIGNS, extra=[SIGN_TEXT], photo_required=False, photo_max=5),
        image=f"{IMG}/SintraBoard/welcomeBoard.jpg",
        event_mode="optional",
    ),
    product(
        "wedding-signs",
        "Wedding Signs",
        "Welcome, seating, and program signs for wedding venues.",
        board_fields(
            design_types=SIGN_DESIGNS,
            extra=[choice("sign_purpose", "Sign purpose", ["Welcome sign", "Seating chart", "Program / timeline", "Unplugged ceremony", "Other"]), SIGN_TEXT],
            photo_required=False,
            photo_max=5,
        ),
        image=f"{IMG}/SintraBoard/welcomeBoard.jpg",
        event_mode="optional",
    ),
    product(
        "menu-board",
        "Menu Board",
        "A clear, custom menu board for an event or business.",
        board_fields(
            design_types=["Upload my print-ready design", "Shop layout using my menu text"],
            extra=[text("menu_content", "Menu content", multiline=True, required=True, max_length=3000, placeholder="Menu items, descriptions, and sections to display")],
            photo_required=False,
            photo_max=5,
        ),
        image=f"{IMG}/SintraBoard/menuBoard.jpg",
    ),
    product(
        "photo-mounting-boards",
        "Photo Mounting Boards",
        "Your photo printed and mounted on a rigid board.",
        board_fields(
            design_types=["Single photo, full bleed", "Single photo with border", "Photo with text caption"],
            extra=[text("caption", "Caption text", placeholder="Optional caption", depends_on="design_type", depends_values=["photo-with-text-caption"], required=True)],
            photo_required=True,
            photo_max=1,
        ),
        image=f"{IMG}/SintraBoard/customizePhotoDesign.jpg",
        event_mode="optional",
    ),
    product(
        "customized-photo-displays",
        "Customized Photo Displays",
        "Personalized photo displays with text and design elements.",
        board_fields(
            design_types=["Shop layout using my photos and text", "Customize a sample design", "Upload my print-ready design"],
            extra=[text("display_text", "Text to display", multiline=True, placeholder="Names, dates, quotes, or messages")],
            photo_required=True,
            photo_max=5,
        ),
        image=f"{IMG}/SintraBoard/customizePhotoDesign.jpg",
        event_mode="optional",
    ),
    product(
        "photo-collages",
        "Photo Collages",
        "A composed collage of your photos for memories and milestones.",
        board_fields(
            design_types=["Grid layout", "Free-form collage", "Collage with center photo", "Customize a sample design"],
            extra=[
                choice("photo_count", "Number of photos", ["Up to 6 photos", "7 to 12 photos", "13 to 20 photos", opt("More than 20 photos", quote=True)]),
                text("display_text", "Title or text", placeholder="e.g. Happy 18th, Mia!"),
            ],
            photo_required=True,
            photo_max=10,
        ),
        image=f"{IMG}/SintraBoard/photocollage.jpg",
        event_mode="optional",
    ),
]

# ---------------------------------------------------------------- Party Needs

PARTY_NEEDS = [
    product(
        "party-hats",
        "Party Hats",
        "Personalized cone party hats in your party theme.",
        [
            choice("size", "Product size", ["Kids (approx. 6 in tall)", "Adult (approx. 7.5 in tall)", "Mini (approx. 4 in tall)"]),
            choice("color", "Base color", ["Full-color printed design", "Gold", "Silver", "Pink", "Blue"]),
            choice("material", "Material", ["Glossy cardstock", "Matte cardstock", "Glitter cardstock"]),
            text("design", "Design / theme", required=True, placeholder="e.g. Safari, unicorn, superhero"),
            text("personalization", "Personalization", multiline=True, placeholder="Celebrant name, age, or text on each hat"),
            choice("top_decoration", "Top decoration", ["None", "Pom-pom", "Tinsel"]),
            choice("packaging", "Packaging", ["Flat-packed (unassembled)", "Assembled"], default="flat-packed-unassembled"),
            design_upload(),
        ],
        image=f"{IMG}/partyNeeds/partyNeeds.jpg",
        event_mode="optional",
        min_quantity=5,
    ),
    product(
        "gable-boxes",
        "Gable Boxes",
        "Custom favor boxes for guests and giveaways.",
        [
            choice("size", "Product size", ["Small (approx. 3 x 2 x 4 in)", "Medium (approx. 4 x 3 x 6 in)", "Large (approx. 6 x 4 x 8 in)"]),
            choice("color", "Box color", ["Full-color printed design", "Kraft brown with print", "White with print"]),
            choice("material", "Material", ["Matte cardstock", "Glossy cardstock", "Kraft board"]),
            choice("window", "Window", ["No window", "With clear window"], kind="radio", default="no-window"),
            text("design", "Design / theme", required=True, placeholder="Theme, colors, and motif"),
            text("personalization", "Personalization", multiline=True, placeholder="Names, message, or event date"),
            choice("packaging", "Packaging", ["Flat-packed (unassembled)", "Assembled"], default="flat-packed-unassembled"),
            design_upload(),
        ],
        image=f"{IMG}/partyNeeds/partyNeeds.jpg",
        event_mode="optional",
        min_quantity=10,
    ),
    product(
        "gift-thank-you-tags",
        "Gift / Thank You Tags",
        "Tags for gifts, favors, and thank-you notes.",
        [
            choice("size", "Product size", ["1.5 x 2.5 in", "2 x 3.5 in", "Round 2 in"]),
            choice("shape", "Shape", ["Rectangle", "Tag (cut corners)", "Round", "Scalloped"]),
            choice("color", "Paper color", ["White", "Cream", "Kraft brown"]),
            choice("material", "Material", ["Matte cardstock", "Glossy cardstock", "Linen textured cardstock"]),
            choice("hole_and_string", "Hole and string", ["No hole", "Hole only", "Hole with twine", "Hole with ribbon"]),
            text("design", "Design / theme", placeholder="Theme, colors, and motif"),
            text("personalization", "Personalization", multiline=True, required=True, placeholder="e.g. Thank you for celebrating with us! — Mia"),
            choice("packaging", "Packaging", ["Loose", "Bundled per 10"], default="loose"),
            design_upload(),
        ],
        event_mode="optional",
        min_quantity=10,
    ),
    product(
        "personalized-candles",
        "Personalized Candles",
        "Candles with a personalized label for events and gifts.",
        [
            choice("size", "Product size", ["Small (approx. 2 in)", "Medium (approx. 3 in)", "Large (approx. 4 in)"]),
            choice("material", "Candle type", ["Glass jar candle", "Tin can candle", "Pillar candle with label"]),
            choice("color", "Wax color", ["White", "Ivory", "Pink", "Lavender", "Blue"]),
            choice("scent", "Scent", ["Unscented", "Vanilla", "Lavender", "Rose"]),
            text("design", "Label design / theme", placeholder="Theme, colors, and motif"),
            text("personalization", "Label text", multiline=True, required=True, placeholder="Names, date, or message for the label"),
            choice("packaging", "Packaging", ["No packaging", "Organza pouch", "Gift box"], default="no-packaging"),
            design_upload(),
        ],
        event_mode="optional",
    ),
]

# ---------------------------------------------------------------- Tarpaulins & Large Format


def large_format_fields(extra: list[dict[str, Any]] | None = None, *, outdoor_default: str = "") -> list[dict[str, Any]]:
    return [
        number("width", "Width", minimum=0.5, maximum=1000, placeholder="e.g. 3"),
        number("height", "Height", minimum=0.5, maximum=1000, placeholder="e.g. 5"),
        choice(
            "measurement_unit",
            "Measurement unit",
            [opt("Feet (ft)", "ft"), opt("Inches (in)", "in"), opt("Centimeters (cm)", "cm"), opt("Meters (m)", "m")],
            default="ft",
        ),
        choice("material", "Material", ["Tarpaulin, standard", "Tarpaulin, heavy duty", "Mesh tarpaulin (wind-permeable)", "Vinyl sticker print"]),
        choice("placement", "Indoor / outdoor", ["Indoor", "Outdoor"], kind="radio", default=outdoor_default),
        choice("eyelets", "Eyelets", ["No eyelets", "Four corners only", "Every 2 ft around the edges", "Top edge only"]),
        choice("hemming", "Hemming", ["No hemming", "Hemmed on all sides", "Pole pocket (top and bottom)"]),
        *(extra or []),
        choice("design_preference", "Design", ["I will upload a print-ready design", "Shop will lay out my text and photos"], kind="radio"),
        upload(
            "design_upload",
            "Design upload",
            required=True,
            max_files=3,
            depends_on="design_preference",
            depends_values=["i-will-upload-a-print-ready-design"],
        ),
        upload(
            "layout_photos",
            "Photos to include",
            max_files=5,
            depends_on="design_preference",
            depends_values=["shop-will-lay-out-my-text-and-photos"],
        ),
    ]


def wording(label: str = "Text to print", placeholder: str = "Headline, names, dates, and other text") -> dict[str, Any]:
    return text("print_text", label, multiline=True, required=True, max_length=3000, placeholder=placeholder)


LARGE_FORMAT = [
    product(
        "birthday-wedding-graduation-tarpaulins",
        "Birthday / Wedding / Graduation Tarpaulins",
        "Celebration tarpaulins for birthdays, weddings, and graduations.",
        large_format_fields([choice("occasion", "Occasion", ["Birthday", "Wedding", "Graduation", "Other celebration"], kind="radio"), wording()]),
        image=f"{IMG}/tarpaulin/event.jpg",
        pricing_mode="area",
        event_mode="optional",
    ),
    product("event-tarpaulins", "Event Tarpaulins", "Tarpaulins for events, programs, and gatherings.", large_format_fields([wording()]), image=f"{IMG}/tarpaulin/event.jpg", pricing_mode="area", event_mode="optional"),
    product("store-banners", "Store Banners", "Storefront banners for your business name and services.", large_format_fields([wording("Banner text", "Business name, services, contact number")], outdoor_default="outdoor"), pricing_mode="area"),
    product("promotional-banners", "Promotional Banners", "Banners for sales, promos, and product advertisements.", large_format_fields([wording("Promo text", "Promo headline, details, validity dates")]), pricing_mode="area"),
    product(
        "stage-backdrops",
        "Stage Backdrops",
        "Large backdrops for stages, events, and photo areas.",
        large_format_fields([choice("mounting", "Mounting", ["Tarpaulin only", opt("With frame or stand", quote=True)]), wording()]),
        pricing_mode="area",
        event_mode="optional",
    ),
    product("welcome-banners", "Welcome Banners", "Welcome banners for guests and event venues.", large_format_fields([wording()]), pricing_mode="area", event_mode="optional"),
    product("program-banners", "Program Banners", "Banners that show an event program or schedule.", large_format_fields([wording("Program content", "Program flow, schedule, and segment names")]), pricing_mode="area", event_mode="optional"),
    product("school-announcements", "School Announcements", "Announcement and schedule banners for schools.", large_format_fields([wording("Announcement text", "Announcement, schedule, and school name")]), pricing_mode="area"),
    product("public-notices", "Public Notices", "Clear, readable public notice banners.", large_format_fields([wording("Notice text", "Notice title and full text")], outdoor_default="outdoor"), pricing_mode="area"),
    product("entrance-signs", "Entrance Signs", "Entrance signs for venues, events, and businesses.", large_format_fields([wording("Sign text", "Venue or event name and greeting")]), pricing_mode="area"),
    product(
        "directional-banners",
        "Directional Banners",
        "Wayfinding banners that guide visitors.",
        large_format_fields([choice("arrow_direction", "Arrow direction", ["Left", "Right", "Straight ahead", "No arrow"]), wording("Directions text", "e.g. Reception this way")]),
        pricing_mode="area",
    ),
]

CATALOG_SEED: list[dict[str, Any]] = [
    {
        "id": "invitation",
        "label": "Invitation",
        "description": "Invitations for birthdays, weddings, and christenings.",
        "products": INVITATIONS,
    },
    {
        "id": "souvenir",
        "label": "Souvenir",
        "description": "Personalized keepsakes and giveaways.",
        "products": SOUVENIRS,
    },
    {
        "id": "sticker",
        "label": "Sticker",
        "description": "Custom stickers for branding, labels, names, and decor.",
        "products": STICKERS,
    },
    {
        "id": "sintra-board",
        "label": "Sintra Board",
        "description": "Rigid display boards for signs, menus, and photos.",
        "products": SINTRA_BOARDS,
    },
    {
        "id": "party-needs",
        "label": "Party Needs",
        "description": "Personalized party pieces for your celebration.",
        "products": PARTY_NEEDS,
    },
    {
        "id": "large-format",
        "label": "Tarpaulins & Large Format Printing",
        "description": "Tarpaulins and banners priced by size.",
        "products": LARGE_FORMAT,
    },
]
