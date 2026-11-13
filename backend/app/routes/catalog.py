from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import FileResponse

from backend.app.core.dependencies import require_admin
from backend.app.core.security import require_same_origin
from backend.app.services import catalog_services, package_services


router = APIRouter(tags=["catalog"])


# ---------------------------------------------------------------- public (customer order form)


@router.get("/api/catalog")
def read_catalog(response: Response) -> dict[str, Any]:
    # Always revalidate so customers see administrator changes immediately.
    response.headers["Cache-Control"] = "no-store"
    return catalog_services.public_catalog()


@router.get("/api/catalog/full")
def read_catalog_full(response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return catalog_services.public_catalog_full()


@router.get("/api/catalog/products/{product_id}")
def read_product(product_id: str, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {"product": catalog_services.public_product(product_id)}


@router.get("/api/catalog/products/{product_id}/packages")
def read_packages(product_id: str, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return package_services.public_packages(product_id)


@router.post("/api/catalog/quote")
def price_quote(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    require_same_origin(request)
    return catalog_services.quote(payload)


@router.get("/api/catalog/images/{filename}")
def catalog_image(filename: str) -> FileResponse:
    return catalog_services.catalog_image(filename)


# ---------------------------------------------------------------- admin catalog management


@router.get("/api/admin/catalog")
def admin_catalog(request: Request) -> dict[str, Any]:
    require_admin(request)
    return catalog_services.admin_catalog()


@router.post("/api/admin/catalog/categories", status_code=201)
def admin_create_category(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    require_admin(request)
    return catalog_services.create_category(payload)


@router.patch("/api/admin/catalog/categories/{category_id}")
def admin_update_category(category_id: str, request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    require_admin(request)
    return catalog_services.update_category(category_id, payload)


@router.delete("/api/admin/catalog/categories/{category_id}")
def admin_delete_category(category_id: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    return catalog_services.delete_category(category_id)


@router.post("/api/admin/catalog/products", status_code=201)
def admin_create_product(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    require_admin(request)
    return {"product": catalog_services.create_product(payload)}


@router.get("/api/admin/catalog/products/{product_id}")
def admin_read_product(product_id: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    return {"product": catalog_services.admin_product(product_id)}


@router.patch("/api/admin/catalog/products/{product_id}")
def admin_update_product(product_id: str, request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    require_admin(request)
    return {"product": catalog_services.update_product(product_id, payload)}


@router.delete("/api/admin/catalog/products/{product_id}", status_code=204)
def admin_delete_product(product_id: str, request: Request) -> Response:
    require_admin(request)
    catalog_services.delete_product(product_id)
    return Response(status_code=204)


@router.get("/api/admin/catalog/packages")
def admin_read_all_packages(request: Request) -> dict[str, Any]:
    """Every package of every product (the Package Management page)."""
    require_admin(request)
    return package_services.admin_all_packages()


@router.get("/api/admin/catalog/products/{product_id}/packages")
def admin_read_packages(product_id: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    return package_services.admin_packages(product_id)


@router.post("/api/admin/catalog/products/{product_id}/packages", status_code=201)
def admin_create_package(product_id: str, request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    require_admin(request)
    return package_services.create_package(product_id, payload)


@router.patch("/api/admin/catalog/packages/{package_id}")
def admin_update_package(package_id: str, request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    require_admin(request)
    return package_services.update_package(package_id, payload)


@router.delete("/api/admin/catalog/packages/{package_id}")
def admin_delete_package(package_id: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    return package_services.delete_package(package_id)


@router.post("/api/admin/catalog/products/{product_id}/pricing")
def admin_update_pricing(product_id: str, request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    require_admin(request)
    return {"product": catalog_services.update_pricing(product_id, payload)}


@router.post("/api/admin/catalog/products/{product_id}/fields", status_code=201)
def admin_create_field(product_id: str, request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    require_admin(request)
    return {"product": catalog_services.create_field(product_id, payload)}


@router.post("/api/admin/catalog/products/{product_id}/field-order")
def admin_reorder_fields(product_id: str, request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    require_admin(request)
    return {"product": catalog_services.reorder_fields(product_id, payload)}


@router.patch("/api/admin/catalog/fields/{field_id}")
def admin_update_field(field_id: str, request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    require_admin(request)
    return {"product": catalog_services.update_field(field_id, payload)}


@router.delete("/api/admin/catalog/fields/{field_id}")
def admin_delete_field(field_id: str, request: Request) -> dict[str, Any]:
    require_admin(request)
    return {"product": catalog_services.delete_field(field_id)}
