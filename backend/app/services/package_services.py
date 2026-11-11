"""Invitation package rules: admin input validation and the one package an order may choose."""

from __future__ import annotations

from typing import Any

from backend.app.models.catalog import CatalogProduct, InvitationPackage
from backend.app.repositories import package_repository
from backend.app.services import catalog_services


_bad = catalog_services._bad
_text = catalog_services._text
_bool = catalog_services._bool
_int = catalog_services._int
_price = catalog_services._price
MAX_QUANTITY = catalog_services.MAX_QUANTITY


def _product(product_id: str, *, active_only: bool) -> CatalogProduct:
    product = catalog_services.public_product(product_id) if active_only else catalog_services.admin_product(product_id)
    return product


def public_packages(product_id: str) -> dict[str, Any]:
    product = _product(product_id, active_only=True)
    return {"packages": package_repository.list_packages(product["id"], active_only=True)}


def admin_packages(product_id: str) -> dict[str, Any]:
    product = _product(product_id, active_only=False)
    return {"packages": package_repository.list_packages(product["id"], active_only=False)}


def orderable_package(product: CatalogProduct, package_id: Any) -> InvitationPackage:
    """The selected package, confirmed to be active and offered for this exact invitation product."""
    if not isinstance(package_id, str) or not package_id.strip():
        raise _bad("Choose a package for this invitation.")
    package = package_repository.get_package(package_id.strip())
    if package is None or not package["active"] or package["product_id"] != product["id"]:
        raise _bad("The selected package is not available for this invitation. Choose another package.")
    return package


def _inclusions(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 50:
        raise _bad("Inclusions must be a list with at most 50 items.")
    cleaned = []
    for item in value:
        if not isinstance(item, str):
            raise _bad("Each inclusion must be text.")
        item = item.strip()
        if len(item) > 200:
            raise _bad("Each inclusion must be 200 characters or fewer.")
        if item:
            cleaned.append(item)
    if not cleaned:
        raise _bad("Add at least one inclusion for this package.")
    return cleaned


def _package_values(payload: dict[str, Any], current: InvitationPackage | None) -> dict[str, Any]:
    creating = current is None

    def wants(key: str) -> bool:
        return creating or key in payload

    values: dict[str, Any] = {}
    if wants("name"):
        values["name"] = _text(payload, "name", "Package name", 120, required=True)
    if wants("price"):
        price = _price(payload.get("price"), "Package price", allow_none=False)
        if price is None or price <= 0:
            raise _bad("Package price must be greater than zero.")
        values["price"] = price
    if wants("size"):
        values["size"] = _text(payload, "size", "Package size", 80)
    if wants("material"):
        values["material"] = _text(payload, "material", "Package material", 120)
    if wants("inclusions"):
        values["inclusions"] = _inclusions(payload.get("inclusions"))
    if wants("add_on_label"):
        values["add_on_label"] = _text(payload, "add_on_label", "Add-on label", 120)
    if wants("add_on_price"):
        values["add_on_price"] = _price(payload.get("add_on_price"), "Add-on price", allow_none=True)
    if wants("included_quantity"):
        values["included_quantity"] = _int(payload, "included_quantity", "Included quantity", 1, MAX_QUANTITY, 20)
    if wants("image"):
        image = _text(payload, "image", "Package image", 300)
        if image and not image.startswith("/"):
            raise _bad("Package image must be a site path that starts with “/”.")
        values["image"] = image
    if wants("active"):
        values["active"] = _bool(payload, "active", True)
    if "sort_order" in payload:
        values["sort_order"] = _int(payload, "sort_order", "Sort order", 0, 10_000, 0)
    return values


def create_package(product_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    product = _product(product_id, active_only=False)
    values = _package_values(payload, None)
    values["product_id"] = product["id"]
    values.setdefault("sort_order", package_repository.next_sort_order(product["id"]))
    package_repository.create_package(values)
    return admin_packages(product["id"])


def update_package(package_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    current = package_repository.get_package(package_id)
    if current is None:
        raise _bad("Package not found.", 404)
    package_repository.update_package(package_id, _package_values(payload, current))
    return admin_packages(current["product_id"])


def delete_package(package_id: str) -> dict[str, Any]:
    current = package_repository.get_package(package_id)
    if current is None:
        raise _bad("Package not found.", 404)
    package_repository.delete_package(package_id)
    return admin_packages(current["product_id"])
