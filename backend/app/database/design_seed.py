"""Shop-made designs: the personalization field library and the TEMPORARY SAMPLE designs.

Customers who choose "Shop Design" on the order page pick one of the shop's own designs and only type the
details that personalize it (names, age, date, venue, ...). They never upload a design file in that flow.

The shop owner has not supplied the real designs yet, so the gallery starts with the sample designs below
(SVG previews in ``frontend/assets/image/designs``). Each is flagged ``is_sample``: the admin screen labels
it "TEMPORARY SAMPLE" and the customer gallery labels it "Sample design". The administrator replaces them
with real designs from Services & Products → Designs (upload a new image, rename, clear the sample flag, or
delete them all at once). Orders keep their own snapshot of the design, so removing samples never changes
an existing order.
"""

from __future__ import annotations

from typing import Any


# Every personalization question a design can ask. A design stores only the keys it uses and whether each
# one is required; the label, type, and limits come from here (so they are validated the same way everywhere).
# The shape matches a catalog field, which lets the order page and the server reuse the catalog form code.
DESIGN_FIELD_LIBRARY: dict[str, dict[str, Any]] = {
    "celebrant_name": {"label": "Celebrant's name", "field_type": "text", "max_length": 120, "placeholder": "e.g. Mia Santos"},
    "celebrant_age": {"label": "Age", "field_type": "number", "min_value": 0, "max_value": 120, "placeholder": "e.g. 7"},
    "couple_names": {"label": "Couple's names", "field_type": "text", "max_length": 160, "placeholder": "e.g. Maria & Juan"},
    "parents_names": {"label": "Parents' names", "field_type": "text", "max_length": 160, "placeholder": "e.g. Mark and Anna Reyes"},
    "headline": {"label": "Headline or title", "field_type": "text", "max_length": 120, "placeholder": "e.g. Happy 7th Birthday!"},
    "event_date": {"label": "Event date", "field_type": "date"},
    "event_time": {"label": "Event time", "field_type": "time"},
    "event_location": {"label": "Event location", "field_type": "text", "max_length": 240, "placeholder": "Venue name and address"},
    "reception_venue": {"label": "Reception venue", "field_type": "text", "max_length": 240, "placeholder": "e.g. Santos Residence, Pasig City"},
    "rsvp_contact": {"label": "RSVP contact", "field_type": "text", "max_length": 120, "placeholder": "e.g. Anna · 0917 000 0000"},
    "message": {"label": "Message or greeting", "field_type": "textarea", "max_length": 500, "placeholder": "e.g. Thank you for celebrating with us!"},
    "business_name": {"label": "Business or brand name", "field_type": "text", "max_length": 120, "placeholder": "e.g. Lola's Kitchen"},
    "product_name": {"label": "Product name or tagline", "field_type": "text", "max_length": 160, "placeholder": "e.g. Ube Halaya · Net wt. 250 g"},
    "contact_details": {"label": "Contact details", "field_type": "text", "max_length": 200, "placeholder": "Phone number, page, or address"},
    "names_list": {"label": "Names to print", "field_type": "textarea", "max_length": 1500, "placeholder": "One name per line"},
    "other_details": {"label": "Other details", "field_type": "textarea", "max_length": 1000, "placeholder": "Anything else that should appear on the design"},
}

FieldSpec = tuple[str, bool]


def _fields(*specs: FieldSpec) -> list[dict[str, Any]]:
    return [{"key": key, "required": required} for key, required in specs]


# Suggested questions per service, preselected when the administrator adds a design to that service.
EVENT_FIELDS = _fields(
    ("celebrant_name", True), ("celebrant_age", False), ("event_date", True), ("event_time", True),
    ("event_location", True), ("rsvp_contact", False), ("other_details", False),
)
CATEGORY_DEFAULT_FIELDS: dict[str, list[dict[str, Any]]] = {
    "invitation": EVENT_FIELDS,
    "souvenir": _fields(("celebrant_name", True), ("headline", False), ("event_date", False), ("message", False)),
    "sticker": _fields(("business_name", False), ("product_name", False), ("names_list", False), ("contact_details", False)),
    "sintra-board": _fields(("headline", True), ("celebrant_name", False), ("event_date", False), ("other_details", False)),
    "party-needs": _fields(("celebrant_name", True), ("celebrant_age", False), ("event_date", False), ("message", False)),
    "large-format": _fields(("headline", True), ("celebrant_name", False), ("event_date", False), ("event_location", False), ("other_details", False)),
    # Shirt templates: the text and names to print on the shirts.
    "tshirt-printing": _fields(("headline", False), ("business_name", False), ("names_list", False), ("other_details", False)),
}
DEFAULT_FIELDS = _fields(("headline", False), ("other_details", False))

BIRTHDAY_FIELDS = EVENT_FIELDS
WEDDING_FIELDS = _fields(
    ("couple_names", True), ("event_date", True), ("event_time", True), ("event_location", True),
    ("reception_venue", False), ("rsvp_contact", False), ("other_details", False),
)
CHRISTENING_FIELDS = _fields(
    ("celebrant_name", True), ("parents_names", False), ("event_date", True), ("event_time", True),
    ("event_location", True), ("reception_venue", False), ("other_details", False),
)

IMG = "/frontend/image/designs"


def sample(
    slug: str,
    category_id: str,
    products: list[str],
    name: str,
    style: str,
    description: str,
    image: str,
    fields: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": f"sample-{slug}",
        "category_id": category_id,
        "product_ids": products,
        "name": name,
        "style": style,
        "description": description,
        "image": f"{IMG}/{image}",
        "fields": fields,
        "is_sample": True,
        "active": True,
    }


SAMPLE_DESIGNS: list[dict[str, Any]] = [
    # Invitations
    sample("birthday-confetti", "invitation", ["birthday-invitation"], "Confetti Celebration", "Playful",
           "A bright confetti border with a bold age numeral, made for kids' parties.", "invitation/birthday-confetti.svg", BIRTHDAY_FIELDS),
    sample("birthday-balloons", "invitation", ["birthday-invitation"], "Balloon Bash", "Pastel",
           "Soft pastel balloons and clouds with a hand-lettered greeting.", "invitation/birthday-balloons.svg", BIRTHDAY_FIELDS),
    sample("birthday-gold-milestone", "invitation", ["birthday-invitation"], "Golden Milestone", "Elegant",
           "Black and gold with sparkles, for debuts and milestone birthdays.", "invitation/birthday-gold-milestone.svg", BIRTHDAY_FIELDS),
    sample("wedding-blush-garden", "invitation", ["wedding-invitation"], "Blush Garden", "Floral",
           "Blush roses and sage greenery framing classic script names.", "invitation/wedding-blush-garden.svg", WEDDING_FIELDS),
    sample("wedding-modern-monogram", "invitation", ["wedding-invitation"], "Modern Monogram", "Minimalist",
           "A clean monogram crest with spaced serif lettering.", "invitation/wedding-modern-monogram.svg", WEDDING_FIELDS),
    sample("wedding-rustic-kraft", "invitation", ["wedding-invitation"], "Rustic Kraft", "Rustic",
           "Kraft-paper texture, eucalyptus sprigs, and a save-the-date script.", "invitation/wedding-rustic-kraft.svg", WEDDING_FIELDS),
    sample("christening-little-clouds", "invitation", ["christening-invitation"], "Little Angel Clouds", "Soft",
           "Sky blue with clouds, stars, and a little angel.", "invitation/christening-little-clouds.svg", CHRISTENING_FIELDS),
    sample("christening-peace-dove", "invitation", ["christening-invitation"], "Peace Dove", "Classic",
           "A cream card with a gold double frame and a dove with an olive branch.", "invitation/christening-peace-dove.svg", CHRISTENING_FIELDS),
    sample("christening-pastel-blossom", "invitation", ["christening-invitation"], "Pastel Blossom", "Floral",
           "Pastel blossoms around a round name panel.", "invitation/christening-pastel-blossom.svg", CHRISTENING_FIELDS),
    # Souvenirs
    sample("magnet-thank-you-floral", "souvenir", ["refrigerator-magnet"], "Floral Thank-You Magnet", "Floral",
           "A keepsake magnet with roses and a thank-you script.",
           "souvenir/magnet-thank-you-floral.svg", _fields(("celebrant_name", True), ("event_date", False), ("message", False))),
    sample("mug-name-script", "souvenir", ["white-mug", "glass-mug", "nordic-mug"], "Name Script Mug", "Personalized",
           "A wrap-around mug print with a large script name and a short line of text.",
           "souvenir/mug-name-script.svg", _fields(("celebrant_name", True), ("headline", False), ("message", False))),
    sample("tote-line-art", "souvenir", ["personalized-tote-bag"], "Line-Art Tote", "Minimalist",
           "A calm potted-plant illustration with a short quote and your name.",
           "souvenir/tote-line-art.svg", _fields(("headline", True), ("celebrant_name", False))),
    # Stickers
    sample("label-kraft-homemade", "sticker", ["product-label-stickers"], "Homemade Kraft Label", "Rustic",
           "A round kraft label for jars and bottles of home-made goods.",
           "sticker/label-kraft-homemade.svg", _fields(("business_name", True), ("product_name", True), ("contact_details", False))),
    sample("name-scalloped", "sticker", ["name-stickers"], "Scalloped Name Label", "Playful",
           "A scalloped pink name label for school supplies.",
           "sticker/name-scalloped.svg", _fields(("names_list", True), ("other_details", False))),
    sample("favor-thank-you", "sticker", [], "Thank-You Favor Sticker", "Playful",
           "A round confetti sticker for party favors and giveaways.",
           "sticker/favor-thank-you.svg", _fields(("headline", True), ("celebrant_name", False))),
    # Sintra boards
    sample("welcome-floral-arch", "sintra-board", ["birthday-welcome-signs", "wedding-signs"], "Floral Arch Welcome", "Floral",
           "An arched welcome board with roses and greenery.",
           "sintra-board/welcome-floral-arch.svg", _fields(("headline", True), ("couple_names", False), ("celebrant_name", False), ("event_date", False))),
    sample("welcome-modern-minimal", "sintra-board", ["birthday-welcome-signs"], "Modern Minimal Welcome", "Minimalist",
           "Deep green with a thin gold frame and big milestone lettering.",
           "sintra-board/welcome-modern-minimal.svg", _fields(("celebrant_name", True), ("celebrant_age", False), ("headline", False))),
    sample("menu-chalkboard", "sintra-board", ["menu-board"], "Chalkboard Menu", "Rustic",
           "A chalkboard-style menu with a wooden frame.",
           "sintra-board/menu-chalkboard.svg", _fields(("headline", True), ("names_list", True))),
    # Party needs
    sample("party-hat-pastel", "party-needs", ["party-hats"], "Pastel Party Hat", "Playful",
           "Striped pastel cone hats with the celebrant's name and age.",
           "party-needs/party-hat-pastel.svg", _fields(("celebrant_name", True), ("celebrant_age", False))),
    sample("thank-you-tag-floral", "party-needs", ["gift-thank-you-tags", "gable-boxes"], "Floral Thank-You Tag", "Floral",
           "A tag-shaped thank-you with blossoms and greenery.",
           "party-needs/thank-you-tag-floral.svg", _fields(("headline", False), ("celebrant_name", True))),
    sample("candle-label-botanical", "party-needs", ["personalized-candles"], "Botanical Candle Label", "Elegant",
           "A dark botanical label with a scent line and a script title.",
           "party-needs/candle-label-botanical.svg", _fields(("headline", True), ("product_name", False), ("message", False))),
    # Tarpaulins & large format
    sample("birthday-confetti-tarpaulin", "large-format", ["birthday-wedding-graduation-tarpaulins", "event-tarpaulins"], "Confetti Birthday Tarpaulin", "Playful",
           "Balloons, bunting, and confetti for a party backdrop.",
           "large-format/birthday-confetti-tarpaulin.svg", _fields(("celebrant_name", True), ("headline", True), ("event_date", False))),
    sample("graduation-night-gold", "large-format", ["birthday-wedding-graduation-tarpaulins"], "Graduation Night & Gold", "Elegant",
           "Navy and gold with a graduation cap for the class of the year.",
           "large-format/graduation-night-gold.svg", _fields(("celebrant_name", True), ("headline", False), ("other_details", False))),
    sample("grand-opening-banner", "large-format", ["store-banners", "promotional-banners"], "Grand Opening Banner", "Bold",
           "An awning-striped store banner with a promo badge.",
           "large-format/grand-opening-banner.svg", _fields(("business_name", True), ("headline", False), ("contact_details", False), ("other_details", False))),
]
