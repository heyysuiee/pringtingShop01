from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi import HTTPException, Request


_test_directory = tempfile.TemporaryDirectory(prefix="impress-tests-")
os.environ["PRINTSHOP_DATABASE_PATH"] = str(Path(_test_directory.name) / "test.sqlite3")
os.environ["PRINTSHOP_UPLOAD_DIR"] = str(Path(_test_directory.name) / "uploads")

from backend.app.code.security import hash_password, read_session_token, require_same_origin, verify_password
from backend.app.database import customer_store
from backend.app.order_store import create_order, get_order_by_idempotency_key, list_orders_for_customer
from backend.app.schemas.user import CustomerRegistration
from backend.app.services.auth_services import authenticate_customer, register_customer, update_customer


class CustomerAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        customer_store.initialize()
        self.payload = CustomerRegistration(
            name="Print Shop Customer",
            email=f"{uuid.uuid4().hex}@example.test",
            phone="+63 917 123 4567",
            messenger="Customer",
            password="Correct-Horse-Battery-1",
        )

    def test_password_hashing_and_customer_authentication(self) -> None:
        saved = register_customer(self.payload)
        self.assertNotIn("password_hash", saved)
        self.assertEqual(saved["email"], self.payload.email)
        self.assertIsNotNone(authenticate_customer(self.payload.email.upper(), self.payload.password))
        self.assertIsNone(authenticate_customer(self.payload.email, "incorrect-password"))
        digest = hash_password("another-long-password")
        self.assertNotEqual(digest, "another-long-password")
        self.assertTrue(verify_password("another-long-password", digest))
        self.assertFalse(verify_password("wrong-password", digest))

    def test_customer_profile_update(self) -> None:
        saved = register_customer(self.payload)
        updated = update_customer(
            saved["id"],
            {"name": "Updated Customer", "phone": "+63 918 765 4321", "messenger": "New Name"},
        )
        if updated is None:
            self.fail("Updated customer could not be retrieved.")
        self.assertEqual(updated["name"], "Updated Customer")
        self.assertEqual(updated["email"], self.payload.email)

    def test_session_signature_and_role_claims(self) -> None:
        from backend.app.code.security import create_session_token

        token = create_session_token("account-id", "customer")
        claims = read_session_token(token)
        if claims is None:
            self.fail("A valid session token could not be decoded.")
        self.assertEqual(claims["role"], "customer")
        self.assertIsNone(read_session_token(token + "tampered"))

    def test_same_origin_guard_rejects_external_origins(self) -> None:
        scope = {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("shop.example.test", 80),
            "client": ("127.0.0.1", 12345),
            "path": "/api/customer/session",
            "query_string": b"",
            "headers": [
                (b"host", b"shop.example.test"),
                (b"origin", b"https://untrusted.example.test"),
            ],
        }
        with self.assertRaises(HTTPException) as error:
            require_same_origin(Request(scope))
        self.assertEqual(error.exception.status_code, 403)

    def test_order_links_to_account_and_rejects_duplicate_idempotency_key(self) -> None:
        customer = register_customer(self.payload)
        key = uuid.uuid4().hex
        order = {
            "reference": f"IPS-{uuid.uuid4().hex.upper()}",
            "created_at": "2026-11-02T00:00:00+00:00",
            "updated_at": "2026-11-02T00:00:00+00:00",
            "customer": {
                "name": self.payload.name,
                "email": self.payload.email,
                "phone": self.payload.phone,
                "messenger": self.payload.messenger,
            },
            "product": {"name": "Test print", "category": "Test"},
            "customization": {"quantity": 1},
            "event_details": {},
            "has_event": False,
            "status": "Pending Confirmation",
            "attachments": [],
            "history": [],
            "customer_id": customer["id"],
        }
        create_order(order, key)
        stored_order = get_order_by_idempotency_key(key)
        if stored_order is None:
            self.fail("The idempotent order could not be retrieved.")
        self.assertEqual(stored_order["reference"], order["reference"])
        self.assertEqual(len(list_orders_for_customer(customer["id"])), 1)
        order["reference"] = f"IPS-{uuid.uuid4().hex.upper()}"
        with self.assertRaises(sqlite3.IntegrityError):
            create_order(order, key)


if __name__ == "__main__":
    unittest.main()
