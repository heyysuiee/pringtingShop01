"""Dynamic package inclusions and add-on pricing for every service (the Invitation package system, reused).

A package has a base price for its minimum order, an optional additional price per piece, and a list of
inclusions (name + description), all set by the administrator:

    additional cost = (quantity - minimum quantity) x additional price per piece
    total           = base package price + additional cost
"""
from __future__ import annotations

import base64
import os
import tempfile
import unittest
import uuid
from pathlib import Path


_test_directory = tempfile.TemporaryDirectory(prefix="impress-package-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.rate_limit import limiter  # noqa: E402
from backend.app.core.security import SESSION_COOKIE, create_session_token  # noqa: E402
from backend.app.main import app  # noqa: E402


PASSWORD = "Correct-Horse-Battery-1"
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()
UPLOAD = [{"name": "design.png", "dataUrl": PNG}]
# Per service: the product, the personalization the package order still asks for, and its file uploads.
SERVICES = {
    "souvenir": ("refrigerator-magnet", {}, {}),
    "sticker": ("product-label-stickers", {}, {"design_upload": UPLOAD}),
    "sintra": ("birthday-welcome-signs", {"sign_text": "Happy 7th Birthday, Mia!"}, {}),
    "party": ("party-hats", {"design": "Dinosaurs"}, {}),
    "tarpaulin": ("birthday-wedding-graduation-tarpaulins", {"print_text": "Happy Graduation, Leo!"}, {}),
}


class PackageTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._context = TestClient(app)
        cls.client = cls._context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._context.__exit__(None, None, None)

    def setUp(self) -> None:
        limiter.clear()
        self.admin = TestClient(app)
        self.admin.cookies.set(SESSION_COOKIE, create_session_token("admin@example.test", "admin"))
        self.shopper = TestClient(app)
        response = self.shopper.post("/api/customer/register", json={
            "name": "Package Customer", "email": f"{uuid.uuid4().hex[:12]}@example.test",
            "phone": "+63 917 123 4567", "messenger": "", "password": PASSWORD,
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.email = response.json()["customer"]["email"]
        self.created: list[str] = []

    def tearDown(self) -> None:
        for package_id in self.created:
            self.admin.delete(f"/api/admin/catalog/packages/{package_id}")

    def create(self, product_id: str, expect: int = 201, **values) -> dict:
        body = {
            "name": f"Package {uuid.uuid4().hex[:6]}",
            "price": 300,
            "included_quantity": 20,
            "add_on_price": 15,
            "inclusions": [{"name": "Printed items", "description": "Full colour"}, {"name": "Clear sleeve", "description": ""}],
            **values,
        }
        response = self.admin.post(f"/api/admin/catalog/products/{product_id}/packages", json=body)
        self.assertEqual(response.status_code, expect, response.text)
        if expect != 201:
            return response.json()
        package = next(item for item in response.json()["packages"] if item["name"] == body["name"])
        self.created.append(package["id"])
        return package

    def quote(self, product_id: str, package_id: str, quantity: int, options: dict | None = None) -> dict:
        response = self.client.post("/api/catalog/quote", json={"product_id": product_id, "package_id": package_id, "quantity": quantity, "options": options or {}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def public_package(self, product_id: str, package_id: str) -> dict | None:
        packages = self.client.get(f"/api/catalog/products/{product_id}/packages").json()["packages"]
        return next((item for item in packages if item["id"] == package_id), None)

    def order(self, service: str, package_id: str, quantity: int, expect: int = 201) -> dict:
        product_id, options, files = SERVICES[service]
        response = self.shopper.post("/api/orders", headers={"Idempotency-Key": uuid.uuid4().hex * 2}, json={
            "customer": {"name": "Package Customer", "email": self.email, "phone": "+63 917 123 4567", "messenger": ""},
            "product": {"id": product_id},
            "customization": {"quantity": quantity, "options": options, "notes": "", "package_id": package_id},
            "event": {}, "has_event": False, "acknowledged": True, "files": [], "field_files": files,
        })
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()


class PackagePricingTests(PackageTestCase):
    def test_quantity_adds_the_additional_price_for_every_piece(self) -> None:
        package = self.create("refrigerator-magnet")
        for quantity, total in ((20, 300.0), (21, 315.0), (22, 330.0), (23, 345.0), (25, 375.0)):
            quote = self.quote("refrigerator-magnet", package["id"], quantity)
            self.assertEqual((quote["quantity"], quote["estimated_total"]), (quantity, total), quantity)
            self.assertEqual(quote["additional_quantity"], quantity - 20)
            self.assertEqual(quote["additional_cost"], (quantity - 20) * 15.0)
            self.assertEqual(quote["minimum_quantity"], 20)
            self.assertEqual(quote["down_payment"], total / 2)
        self.assertIn("(25 − 20) = 5 additional pcs", self.quote("refrigerator-magnet", package["id"], 25)["lines"][1]["detail"])
        # A live estimate below the minimum is shown at the minimum order.
        self.assertEqual(self.quote("refrigerator-magnet", package["id"], 3)["quantity"], 20)

    def test_every_service_can_have_its_own_packages(self) -> None:
        settings = {
            "souvenir": (250, 10, 20),
            "sticker": (180, 50, 2.5),
            "sintra": (900, 1, 450),
            "party": (300, 20, 15),
            "tarpaulin": (650, 1, None),
        }
        made = {}
        for service, (price, minimum, extra) in settings.items():
            product_id = SERVICES[service][0]
            made[service] = self.create(product_id, price=price, included_quantity=minimum, add_on_price=extra,
                                        inclusions=[{"name": f"{service} inclusion", "description": f"for {service}"}])
            shown = self.public_package(product_id, made[service]["id"])
            self.assertEqual((shown["price"], shown["minimum_quantity"], shown["add_on_price"]), (float(price), minimum, extra), service)
            self.assertEqual(shown["inclusion_details"], [{"name": f"{service} inclusion", "description": f"for {service}"}])
            self.assertEqual(shown["inclusions"], [f"{service} inclusion"])
        catalog = self.client.get("/api/catalog").json()["categories"]
        products = {product["id"]: product for category in catalog for product in category["products"]}
        for service, package in made.items():
            self.assertIn(package["id"], [item["id"] for item in products[SERVICES[service][0]]["packages"]], service)
        # Stickers: 50 included for ₱180, ₱2.50 each after that.
        self.assertEqual(self.quote("product-label-stickers", made["sticker"]["id"], 60)["estimated_total"], 205.0)
        # Tarpaulin without an additional price: a fixed quantity, and not priced by area.
        self.assertEqual(self.quote("birthday-wedding-graduation-tarpaulins", made["tarpaulin"]["id"], 1)["estimated_total"], 650.0)

    def test_backend_validates_quantity_before_saving_the_order(self) -> None:
        package = self.create("party-hats", included_quantity=20)
        self.order("party", package["id"], 19, expect=422)
        self.order("party", package["id"], 0, expect=422)
        self.order("party", package["id"], 50001, expect=422)
        placed = self.order("party", package["id"], 23)
        self.assertEqual(placed["total_amount"], 345.0)
        fixed = self.create("birthday-wedding-graduation-tarpaulins", price=650, included_quantity=2, add_on_price=None)
        self.order("tarpaulin", fixed["id"], 3, expect=422)
        self.assertEqual(self.order("tarpaulin", fixed["id"], 2)["total_amount"], 650.0)

    def test_the_package_minimum_applies_instead_of_the_product_minimum(self) -> None:
        # Party hats are customized from 5 pieces; this package can be ordered from 2.
        small = self.create("party-hats", price=60, included_quantity=2, add_on_price=25)
        self.assertEqual(self.order("party", small["id"], 2)["total_amount"], 60.0)
        self.assertEqual(self.order("party", small["id"], 4)["total_amount"], 110.0)
        self.order("party", small["id"], 1, expect=422)

    def test_every_service_order_saves_its_package_and_computation(self) -> None:
        for service in SERVICES:
            product_id = SERVICES[service][0]
            package = self.create(product_id, price=300, included_quantity=2, add_on_price=15)
            placed = self.order(service, package["id"], 5)
            self.assertTrue(placed["fixed_price"], service)
            self.assertEqual(placed["total_amount"], 345.0, service)
            self.assertEqual(placed["status"], "Pending Confirmation")
            self.assertEqual(placed["payment"]["down_payment_required"], 172.5)
            saved = placed["package"]
            self.assertEqual((saved["quantity"], saved["additional_quantity"], saved["additional_cost"], saved["computed_total"]), (5, 3, 45.0, 345.0), service)
            self.assertEqual(saved["minimum_quantity"], 2)
            self.assertEqual(saved["inclusion_details"][0], {"name": "Printed items", "description": "Full colour"})


class PackageManagementTests(PackageTestCase):
    def test_admin_adds_edits_and_deletes_inclusions(self) -> None:
        package = self.create("white-mug")
        edited = self.admin.patch(f"/api/admin/catalog/packages/{package['id']}", json={"inclusions": [
            {"name": "Printed mug", "description": "11 oz, both sides"},  # renamed + described
            {"name": "Gift box", "description": "Kraft box with ribbon"},  # added; "Clear sleeve" deleted
        ]})
        self.assertEqual(edited.status_code, 200, edited.text)
        shown = self.public_package("white-mug", package["id"])
        self.assertEqual(shown["inclusion_details"], [
            {"name": "Printed mug", "description": "11 oz, both sides"},
            {"name": "Gift box", "description": "Kraft box with ribbon"},
        ])
        self.assertEqual(shown["inclusions"], ["Printed mug", "Gift box"])
        # Plain text inclusions (the earlier admin screen) still work.
        plain = self.admin.patch(f"/api/admin/catalog/packages/{package['id']}", json={"inclusions": ["Mug", "Box"]})
        self.assertEqual(plain.status_code, 200, plain.text)
        self.assertEqual(self.public_package("white-mug", package["id"])["inclusion_details"], [{"name": "Mug", "description": ""}, {"name": "Box", "description": ""}])
        for bad in ([], [{"name": "", "description": "orphan"}], [{"name": "x" * 201}], [{"name": "Mug", "description": "x" * 501}], [{"name": 5}], "Mug"):
            response = self.admin.patch(f"/api/admin/catalog/packages/{package['id']}", json={"inclusions": bad})
            self.assertEqual(response.status_code, 422, f"{bad!r}: {response.text}")

    def test_admin_sets_base_price_minimum_and_additional_price(self) -> None:
        package = self.create("acrylic-keychain", price=300, included_quantity=20, add_on_price=15)
        updated = self.admin.patch(f"/api/admin/catalog/packages/{package['id']}", json={"price": 420, "minimum_quantity": 30, "add_on_price": 12})
        self.assertEqual(updated.status_code, 200, updated.text)
        shown = self.public_package("acrylic-keychain", package["id"])
        self.assertEqual((shown["price"], shown["minimum_quantity"], shown["included_quantity"], shown["add_on_price"]), (420.0, 30, 30, 12.0))
        self.assertEqual(self.quote("acrylic-keychain", package["id"], 32)["estimated_total"], 444.0)
        for bad in ({"price": 0}, {"price": -5}, {"minimum_quantity": 0}, {"add_on_price": -1}, {"price": "abc"}):
            response = self.admin.patch(f"/api/admin/catalog/packages/{package['id']}", json=bad)
            self.assertEqual(response.status_code, 422, f"{bad}: {response.text}")
        self.assertEqual(self.client.post("/api/admin/catalog/products/acrylic-keychain/packages", json={"name": "x", "price": 1, "inclusions": ["a"]}).status_code, 401)

    def test_existing_orders_keep_the_package_they_were_placed_with(self) -> None:
        package = self.create("party-hats", price=300, included_quantity=20, add_on_price=15)
        first = self.order("party", package["id"], 25)
        self.assertEqual(first["total_amount"], 375.0)
        self.admin.patch(f"/api/admin/catalog/packages/{package['id']}", json={
            "price": 400, "add_on_price": 20, "inclusions": [{"name": "New inclusion", "description": "added later"}],
        })
        # The customer site shows the latest package at once…
        shown = self.public_package("party-hats", package["id"])
        self.assertEqual((shown["price"], shown["add_on_price"], shown["inclusions"]), (400.0, 20.0, ["New inclusion"]))
        # …while the earlier order keeps its price, inclusions, and computation.
        kept = self.shopper.get(f"/api/account/orders/{first['reference']}").json()
        self.assertEqual(kept["total_amount"], 375.0)
        self.assertEqual((kept["package"]["price"], kept["package"]["add_on_price"]), (300.0, 15.0))
        self.assertEqual(kept["package"]["inclusions"], ["Printed items", "Clear sleeve"])
        self.assertEqual(kept["package"]["additional_cost"], 75.0)
        self.assertEqual(self.order("party", package["id"], 25)["total_amount"], 500.0)

    def test_inactive_packages_are_hidden_and_cannot_be_ordered(self) -> None:
        package = self.create("party-hats")
        self.admin.patch(f"/api/admin/catalog/packages/{package['id']}", json={"active": False})
        self.assertIsNone(self.public_package("party-hats", package["id"]))
        self.order("party", package["id"], 20, expect=422)

    def test_invitation_packages_are_unchanged(self) -> None:
        shown = self.public_package("birthday-invitation", "birthday-set-a")
        self.assertEqual((shown["price"], shown["minimum_quantity"], shown["add_on_price"]), (300.0, 20, 15.0))
        self.assertEqual([item["name"] for item in shown["inclusion_details"]], shown["inclusions"])
        self.assertEqual(self.quote("birthday-invitation", "birthday-set-a", 25, {"paper_finish": "glossy", "envelope_option": "plain-white-envelope"})["estimated_total"], 375.0)


if __name__ == "__main__":
    unittest.main()
