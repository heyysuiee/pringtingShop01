from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from backend.app.code.security import hash_password, verify_password
from backend.app.database import customer_store
from backend.app.schemas.user import CustomerRegistration


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def public_customer(customer: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": customer["id"],
        "email": customer["email"],
        "name": customer["name"],
        "phone": customer["phone"],
        "messenger": customer["messenger"],
        "created_at": customer["created_at"],
        "updated_at": customer["updated_at"],
    }


def register_customer(payload: CustomerRegistration) -> dict[str, Any]:
    customer = payload.model_dump()
    customer["password_hash"] = hash_password(customer.pop("password"))
    return public_customer(customer_store.create(customer, now_iso()))


def authenticate_customer(email: str, password: str) -> dict[str, Any] | None:
    customer = customer_store.get_by_email(email)
    if customer is None or not verify_password(password, customer["password_hash"]):
        return None
    return public_customer(customer)


def get_customer(customer_id: str) -> dict[str, Any] | None:
    customer = customer_store.get_by_id(customer_id)
    return public_customer(customer) if customer else None


def update_customer(customer_id: str, profile: dict[str, str]) -> dict[str, Any] | None:
    customer = customer_store.update(customer_id, profile, now_iso())
    return public_customer(customer) if customer else None