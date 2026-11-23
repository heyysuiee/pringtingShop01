"""Read-only Philippine location data used by the delivery address form and its validation.

The dataset is a bundled snapshot of the Philippine Standard Geographic Code (PSGC) published by the
National Statistics Office (psgc.gitlab.io/api). It contains every province and its cities/municipalities,
plus Metro Manila's cities. It is reference data only: no application data or settings are stored here,
and the order continues to store the plain province/city strings customers have always used.

The bundled snapshot is the application's Province API. A failed or unreadable lookup is reported as a
503: callers must never fall back to guessing an island group, because a guessed group would charge the
wrong shipping fee.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import HTTPException

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "philippine_locations.json"
PROVINCE_UNAVAILABLE = "The province lookup service is unavailable right now. Try again later."


@lru_cache(maxsize=1)
def dataset() -> dict[str, Any]:
    with DATA_FILE.open(encoding="utf-8") as handle:
        return json.load(handle)


def _safe_dataset() -> dict[str, Any]:
    """The Province API boundary: any lookup failure becomes a clear 503, never a default region."""
    try:
        return dataset()
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=503, detail=PROVINCE_UNAVAILABLE) from error


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
    data = _safe_dataset()
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
    match = next((item for item in _safe_dataset()["provinces"] if province_key(item["name"]) == key), None)
    return str(match["name"]) if match else None


def region_for_province(province: Any) -> str | None:
    """Island group of a known province, or None for legacy/free-text values."""
    key = province_key(province)
    for item in _safe_dataset()["provinces"]:
        if province_key(item["name"]) == key:
            return item["island_group"]
    return None


def _province_match(data: dict[str, Any], province: Any) -> dict[str, Any]:
    text = str(province or "").strip()
    match = next(
        (item for item in data["provinces"] if province_key(item["name"]) == province_key(text)),
        None,
    )
    if match is None:
        raise HTTPException(
            status_code=422,
            detail=f"Province: “{text or 'blank'}” is not a recognized Philippine province. Choose one from the list.",
        )
    return match


def _province_view(match: dict[str, Any]) -> dict[str, str]:
    group = str(match["island_group"]).casefold()
    return {
        "province": str(match["name"]),
        "region": str(match["region"]),
        "region_code": str(match["region_code"]),
        "island_group": group,
        "region_label": group.capitalize(),
    }


def resolve_province(province: Any) -> dict[str, str]:
    """Resolve a province through the Province API without ever guessing its island group.

    The returned ``island_group`` is the shipping region key (luzon/visayas/mindanao), ``region`` is
    the named PSGC region, and ``region_label`` is the island group's display name.
    """
    return _province_view(_province_match(_safe_dataset(), province))


def resolve_address(province: Any, city: Any) -> dict[str, str]:
    """Validate a province/municipality pair and return their canonical names and region.

    Error messages name the field the customer must correct, so the order form can show them inline.
    """
    match = _province_match(_safe_dataset(), province)
    city_text = str(city or "").strip()
    city_match = next((item for item in match["cities"] if city_key(item["name"]) == city_key(city_text)), None)
    if city_match is None:
        raise HTTPException(
            status_code=422,
            detail=f"City / Municipality: “{city_text or 'blank'}” is not in {match['name']}. Choose one from the list for the selected province.",
        )
    return {**_province_view(match), "city": str(city_match["name"])}
