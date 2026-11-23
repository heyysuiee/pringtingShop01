"""Read-only Philippine location data used by the delivery address form and its validation.

The dataset is a bundled snapshot of the Philippine Standard Geographic Code (PSGC) published by the
National Statistics Office (psgc.gitlab.io/api). It contains every province and its cities/municipalities,
plus Metro Manila's cities. It is reference data only: no application data or settings are stored here,
and the order continues to store the plain province/city strings customers have always used.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import HTTPException

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "philippine_locations.json"


@lru_cache(maxsize=1)
def dataset() -> dict[str, Any]:
    with DATA_FILE.open(encoding="utf-8") as handle:
        return json.load(handle)


def _key(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").casefold()).strip()
    return re.sub(r"\s*\([^)]*\)", "", text).strip()


def province_key(value: Any) -> str:
    key = _key(value)
    key = re.sub(r"^province of\s+", "", key)
    return re.sub(r"\s+province$", "", key).strip()


def city_key(value: Any) -> str:
    key = _key(value)
    key = re.sub(r"^city of\s+", "", key)
    return re.sub(r"\s+city$", "", key).strip()


def public_locations() -> dict[str, Any]:
    """The dependent dropdown data: regions, provinces, and each province's cities/municipalities."""
    data = dataset()
    return {
        "source": data["source"],
        "regions": data["regions"],
        "provinces": [
            {
                "name": province["name"],
                "region": province["region"],
                "region_code": province["region_code"],
                "island_group": province["island_group"],
                "cities": [city["name"] for city in province["cities"]],
            }
            for province in data["provinces"]
        ],
    }


def canonical_province(value: Any) -> str | None:
    """The PSGC name for a province alias, or None when it is not in the snapshot.

    Both customer addresses and administrator-configured delivery areas can contain older spellings
    such as “Province of Cavite”, “Cavite Province”, or a parenthesized regional label. Resolving
    them here keeps fee matching and address canonicalization on the same authoritative key.
    """
    key = province_key(value)
    if not key:
        return None
    match = next((item for item in dataset()["provinces"] if province_key(item["name"]) == key), None)
    return str(match["name"]) if match else None


def region_for_province(province: Any) -> str | None:
    """Island group of a known province, or None for legacy/free-text values."""
    key = province_key(province)
    for item in dataset()["provinces"]:
        if province_key(item["name"]) == key:
            return item["island_group"]
    return None


def resolve_address(province: Any, city: Any) -> dict[str, str]:
    """Validate a province/municipality pair and return their canonical names and region.

    Error messages name the field the customer must correct, so the order form can show them inline.
    """
    province_text = str(province or "").strip()
    city_text = str(city or "").strip()
    match = next((item for item in dataset()["provinces"] if province_key(item["name"]) == province_key(province_text)), None)
    if match is None:
        raise HTTPException(
            status_code=422,
            detail=f"Province: “{province_text or 'blank'}” is not a recognized Philippine province. Choose one from the list.",
        )
    city_match = next((item for item in match["cities"] if city_key(item["name"]) == city_key(city_text)), None)
    if city_match is None:
        raise HTTPException(
            status_code=422,
            detail=f"City / Municipality: “{city_text or 'blank'}” is not in {match['name']}. Choose one from the list for the selected province.",
        )
    return {
        "province": match["name"],
        "city": city_match["name"],
        "region": match["region"],
        "region_code": match["region_code"],
        "island_group": match["island_group"],
    }
