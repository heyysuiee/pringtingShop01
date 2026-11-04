from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from backend.app.code.config import settings
from backend.app.code.security import (
    clear_session_cookie,
    create_session_cookie,
    require_same_origin,
    require_session,
    verify_password,
)
from backend.app.schemas.user import CustomerLogin, CustomerProfileUpdate, CustomerRegistration
from backend.app.services import auth_services


router = APIRouter(tags=["authentication"])


@router.post("/api/customer/register", status_code=201)
def register_customer(payload: CustomerRegistration, request: Request, response: Response) -> dict[str, object]:
    require_same_origin(request)
    try:
        customer = auth_services.register_customer(payload)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    create_session_cookie(response, request, customer["id"], "customer")
    return {"customer": customer}


@router.post("/api/customer/session")
def login_customer(payload: CustomerLogin, request: Request, response: Response) -> dict[str, object]:
    require_same_origin(request)
    customer = auth_services.authenticate_customer(payload.email, payload.password)
    if customer is None:
        raise HTTPException(status_code=401, detail="Email address or password is incorrect.")
    create_session_cookie(response, request, customer["id"], "customer")
    return {"customer": customer}


@router.get("/api/customer/session")
def customer_session(request: Request) -> dict[str, object]:
    claims = require_session(request, "customer")
    customer = auth_services.get_customer(claims["sub"])
    if customer is None:
        raise HTTPException(status_code=401, detail="The customer account is no longer available.")
    return {"customer": customer}


@router.delete("/api/customer/session", status_code=204)
def logout_customer(request: Request, response: Response) -> Response:
    require_same_origin(request)
    response.delete_cookie("impress_session", httponly=True, samesite="strict", path="/")
    response.status_code = 204
    return response


@router.get("/api/account/profile")
def read_customer_profile(request: Request) -> dict[str, object]:
    claims = require_session(request, "customer")
    customer = auth_services.get_customer(claims["sub"])
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer account not found.")
    return {"customer": customer}


@router.patch("/api/account/profile")
def update_customer_profile(payload: CustomerProfileUpdate, request: Request) -> dict[str, object]:
    require_same_origin(request)
    claims = require_session(request, "customer")
    customer = auth_services.update_customer(claims["sub"], payload.model_dump())
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer account not found.")
    return {"customer": customer}


@router.post("/api/admin/session")
def create_admin_session(payload: CustomerLogin, request: Request, response: Response) -> dict[str, str]:
    require_same_origin(request)
    if not settings.admin_email or not settings.admin_password_hash:
        raise HTTPException(status_code=503, detail="Administrator credentials have not been configured.")
    if payload.email != settings.admin_email or not verify_password(payload.password, settings.admin_password_hash):
        raise HTTPException(status_code=401, detail="Administrator credentials are incorrect.")
    create_session_cookie(response, request, settings.admin_email, "admin")
    return {"mode": "authenticated"}


@router.get("/api/admin/session")
def admin_session(request: Request) -> dict[str, str]:
    claims = require_session(request, "admin")
    return {"mode": "authenticated", "email": claims["sub"]}


@router.delete("/api/admin/session", status_code=204)
def delete_admin_session(request: Request, response: Response) -> Response:
    require_same_origin(request)
    clear_session_cookie(response)
    response.status_code = 204
    return response