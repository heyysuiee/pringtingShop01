from __future__ import annotations

import base64
import os
import tempfile
import unittest
import uuid
from pathlib import Path


_test_directory = tempfile.TemporaryDirectory(prefix="impress-catalog-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
# Never read the developer's .env file (SMTP credentials, production mode) during tests.
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.rate_limit import limiter  # noqa: E402
from backend.app.core.security import SESSION_COOKIE, create_session_token  # noqa: E402
from backend.app.database.catalog_seed import CATALOG_SEED  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.repositories import catalog_repository, order_repository  # noqa: E402
from backend.app.services import catalog_services  # noqa: E402


PASSWORD = "Correct-Horse-Battery-1"
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()

MUG_OPTIONS = {"size": "11-oz", "print_area": "one-side", "inner_color": "white-classic", "packaging": "no-packaging"}


def idempotency_key() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]


class CatalogTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._context = TestClient(app)
        cls.client = cls._context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._context.__exit__(None, None, None)

    def setUp(self) -> None:
        limiter.clear()

    def customer(self) -> tuple[TestClient, dict]:
        shopper = TestClient(app)
        response = shopper.post(
            "/api/customer/register",
            json={
                "name": "Catalog Customer",
                "email": f"{uuid.uuid4().hex[:12]}@example.test",
                "phone": "+63 917 123 4567",
                "messenger": "",
                "password": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return shopper, response.json()["customer"]

    def admin(self) -> TestClient:
        admin = TestClient(app)
        admin.cookies.set(SESSION_COOKIE, create_session_token("admin@example.test", "admin"))
        return admin

    def order(self, shopper: TestClient, email: str, product_id: str, quantity: int, options: dict, **extra) -> object:
        payload = {
            "customer": {"name": "Catalog Customer", "email": email, "phone": "+63 917 123 4567", "messenger": ""},
            "product": {"id": product_id},
            "customization": {"quantity": quantity, "options": options, "notes": extra.pop("notes", "")},
            "event": extra.pop("event", {}),
            "has_event": extra.pop("has_event", False),
            "acknowledged": True,
            "files": extra.pop("files", []),
            "field_files": extra.pop("field_files", {}),
        }
        return shopper.post("/api/orders", json=payload, headers={"Idempotency-Key": idempotency_key()})


class SeedTests(CatalogTestCase):
    def test_all_six_categories_and_products_are_seeded(self) -> None:
        catalog = self.client.get("/api/catalog").json()
        by_id = {category["id"]: category for category in catalog["categories"]}
        expected = {category["id"]: len(category["products"]) for category in CATALOG_SEED}
        for category_id, count in expected.items():
            self.assertIn(category_id, by_id)
            self.assertGreaterEqual(len(by_id[category_id]["products"]), count)
        self.assertEqual(expected, {"invitation": 3, "souvenir": 8, "sticker": 4, "sintra-board": 6, "party-needs": 4, "large-format": 11})

    def test_every_seeded_product_has_its_own_fields_and_no_invented_prices(self) -> None:
        for category in CATALOG_SEED:
            for product in category["products"]:
                self.assertIsNone(product["base_price"], product["id"])
                self.assertGreaterEqual(len(product["fields"]), 4, product["id"])
                for field in product["fields"]:
                    for option in field.get("options", []):
                        self.assertNotIn("price_adjustment", option, f"{product['id']}.{field['field_key']}")

    def test_seeding_runs_only_once(self) -> None:
        before = sum(len(category["products"]) for category in catalog_repository.load_catalog(active_only=False))
        catalog_repository.initialize()
        after = sum(len(category["products"]) for category in catalog_repository.load_catalog(active_only=False))
        self.assertEqual(before, after)


class PricingTests(CatalogTestCase):
    def test_unpriced_product_requires_a_quote(self) -> None:
        product = catalog_services.orderable_product("acrylic-keychain")
        result = catalog_services.calculate_price(product, 5, {})
        self.assertIsNone(result["estimated_total"])
        self.assertTrue(result["requires_quote"])

    def test_area_pricing_uses_admin_prices_and_unit_conversion(self) -> None:
        product = catalog_services.admin_product("entrance-signs")
        material = next(field for field in product["fields"] if field["field_key"] == "material")
        hemming = next(field for field in product["fields"] if field["field_key"] == "hemming")
        heavy, hemmed = material["options"][1], hemming["options"][1]
        catalog_services.update_pricing("entrance-signs", {
            "base_price": 18,
            "options": [
                {"id": heavy["id"], "price_adjustment": 4, "price_type": "per_sqft"},
                {"id": hemmed["id"], "price_adjustment": 25, "price_type": "per_unit"},
            ],
        })
        response = self.client.post("/api/catalog/quote", json={
            "product_id": "entrance-signs",
            "quantity": 3,
            "options": {"width": "24", "height": "48", "measurement_unit": "in", "material": heavy["value"], "hemming": hemmed["value"]},
        })
        result = response.json()
        # 24 x 48 in = 2 x 4 ft = 8 sq ft. (18 + 4) x 8 + 25 = 201 each, x 3 = 603.
        self.assertEqual(result["area_sqft"], 8.0)
        self.assertEqual(result["unit_price"], 201.0)
        self.assertEqual(result["estimated_total"], 603.0)

    def test_quote_required_option_blocks_the_estimate(self) -> None:
        product = catalog_services.admin_product("wedding-invitation")
        catalog_services.update_pricing("wedding-invitation", {"base_price": 35})
        size = next(field for field in product["fields"] if field["field_key"] == "invitation_size")
        custom = next(option for option in size["options"] if option["requires_quote"])
        priced = catalog_services.calculate_price(catalog_services.admin_product("wedding-invitation"), 50, {"invitation_size": custom["value"]})
        self.assertIsNone(priced["estimated_total"])
        self.assertTrue(priced["requires_quote"])
        normal = catalog_services.calculate_price(catalog_services.admin_product("wedding-invitation"), 50, {"invitation_size": size["options"][0]["value"]})
        self.assertEqual(normal["estimated_total"], 1750.0)


class OrderValidationTests(CatalogTestCase):
    def test_order_saves_specifications_and_catalog_estimate(self) -> None:
        catalog_services.update_pricing("white-mug", {"base_price": 150})
        shopper, account = self.customer()
        response = self.order(shopper, account["email"], "white-mug", 4, MUG_OPTIONS, notes="Gift wrap please")
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["product"]["name"], "White Mug")
        self.assertEqual(body["product"]["category"], "Souvenir")
        self.assertIsNone(body["total_amount"], "The confirmed total is set by the administrator's quotation.")
        self.assertEqual(body["pricing"]["estimated_total"], 600.0)
        labels = {spec["label"]: spec["value"] for spec in body["customization"]["specifications"]}
        self.assertEqual(labels["Capacity"], "11 oz")
        self.assertEqual(labels["Inner and handle color"], "White (classic)")
        self.assertEqual(body["customization"]["notes"], "Gift wrap please")

    def test_client_cannot_override_product_name_or_price(self) -> None:
        shopper, account = self.customer()
        response = self.order(shopper, account["email"], "white-mug", 1, MUG_OPTIONS)
        self.assertEqual(response.status_code, 201, response.text)
        stored = order_repository.get_order(response.json()["reference"])
        self.assertEqual(stored["product"]["id"], "white-mug")

    def test_missing_required_option_is_rejected(self) -> None:
        shopper, account = self.customer()
        options = dict(MUG_OPTIONS)
        options.pop("inner_color")
        response = self.order(shopper, account["email"], "white-mug", 1, options)
        self.assertEqual(response.status_code, 422)
        self.assertIn("Inner and handle color", response.json()["detail"])

    def test_option_from_another_product_is_rejected(self) -> None:
        shopper, account = self.customer()
        response = self.order(shopper, account["email"], "white-mug", 1, {**MUG_OPTIONS, "size": "a4-8-3-x-11-7-in"})
        self.assertEqual(response.status_code, 422)

    def test_quantity_limits_come_from_the_product(self) -> None:
        shopper, account = self.customer()
        product = catalog_services.orderable_product("birthday-invitation")
        response = self.order(shopper, account["email"], "birthday-invitation", product["min_quantity"] - 1, {})
        self.assertEqual(response.status_code, 422)
        self.assertIn("between", response.json()["detail"])

    def test_conditional_field_is_required_only_when_shown(self) -> None:
        shopper, account = self.customer()
        options = {
            "invitation_size": "5-x-7-in",
            "paper_type": "premium-cardstock",
            "paper_finish": "matte",
            "printing_type": "single-sided-full-color",
            "orientation": "portrait",
            "folding_type": "bi-fold",
            "envelope_option": "colored-envelope",
            "design_preference": "shop-will-design-it-for-me",
            "invitation_wording": "Mia turns 7! RSVP 0917 000 0000",
        }
        event = {"celebrant": "Mia", "event_type": "Birthday", "event_date": "2026-12-12", "event_time": "14:00", "event_location": "Quezon City"}
        missing_color = self.order(shopper, account["email"], "birthday-invitation", 30, options, event=event)
        self.assertEqual(missing_color.status_code, 422)
        self.assertIn("Envelope color", missing_color.json()["detail"])

        plain = self.order(shopper, account["email"], "birthday-invitation", 30, {**options, "envelope_option": "plain-white-envelope", "envelope_color": "ignored"}, event=event)
        self.assertEqual(plain.status_code, 201, plain.text)
        self.assertNotIn("envelope_color", plain.json()["customization"]["options"])
        self.assertTrue(plain.json()["has_event"])

    def test_required_event_details_are_enforced(self) -> None:
        shopper, account = self.customer()
        response = self.order(shopper, account["email"], "christening-invitation", 20, {}, event={})
        self.assertEqual(response.status_code, 422)

    def test_product_upload_field_files_are_saved_and_labelled(self) -> None:
        shopper, account = self.customer()
        options = {
            "board_size": "a4-8-3-x-11-7-in",
            "thickness": "3-mm",
            "orientation": "portrait",
            "material": "glossy-vinyl-sticker-print",
            "design_type": "single-photo-full-bleed",
            "stand_option": "easel-stand",
        }
        without_photo = self.order(shopper, account["email"], "photo-mounting-boards", 1, options)
        self.assertEqual(without_photo.status_code, 422)
        self.assertIn("Photo upload", without_photo.json()["detail"])

        with_photo = self.order(
            shopper,
            account["email"],
            "photo-mounting-boards",
            1,
            options,
            field_files={"photo_upload": [{"name": "family.png", "dataUrl": PNG}]},
        )
        self.assertEqual(with_photo.status_code, 201, with_photo.text)
        attachment = with_photo.json()["attachments"][0]
        self.assertEqual(attachment["field_key"], "photo_upload")
        self.assertEqual(attachment["field_label"], "Photo upload")

    def test_files_for_a_hidden_upload_field_are_rejected(self) -> None:
        shopper, account = self.customer()
        options = {
            "width": 3, "height": 5, "measurement_unit": "ft", "material": "tarpaulin-standard",
            "placement": "outdoor", "eyelets": "no-eyelets", "hemming": "no-hemming",
            "print_text": "Grand opening", "design_preference": "shop-will-lay-out-my-text-and-photos",
        }
        response = self.order(
            shopper, account["email"], "store-banners", 1, options,
            field_files={"design_upload": [{"name": "design.png", "dataUrl": PNG}]},
        )
        self.assertEqual(response.status_code, 422)

    def test_inactive_product_cannot_be_ordered(self) -> None:
        created = catalog_services.create_product({"name": f"Temp {uuid.uuid4().hex[:6]}", "category_id": "sticker"})
        catalog_services.create_field(created["id"], {"label": "Size", "field_type": "select", "required": True, "options": [{"label": "Small"}]})
        catalog_services.update_product(created["id"], {"active": False})
        with self.assertRaises(HTTPException):
            catalog_services.orderable_product(created["id"])


class AdminCatalogTests(CatalogTestCase):
    def test_admin_endpoints_require_an_admin_session(self) -> None:
        anonymous = TestClient(app)
        self.assertEqual(anonymous.get("/api/admin/catalog").status_code, 401)
        shopper, _ = self.customer()
        self.assertEqual(shopper.post("/api/admin/catalog/products", json={"name": "X", "category_id": "sticker"}).status_code, 401)

    def test_admin_changes_appear_in_the_customer_catalog(self) -> None:
        admin = self.admin()
        name = f"Vinyl Banner {uuid.uuid4().hex[:6]}"
        created = admin.post("/api/admin/catalog/products", json={
            "name": name, "category_id": "large-format", "description": "Test", "pricing_mode": "unit", "base_price": 99,
        })
        self.assertEqual(created.status_code, 201, created.text)
        product_id = created.json()["product"]["id"]
        field = admin.post(f"/api/admin/catalog/products/{product_id}/fields", json={
            "label": "Finish",
            "field_type": "radio",
            "required": True,
            "options": [{"label": "Matte"}, {"label": "Glossy", "price_adjustment": 10}],
        })
        self.assertEqual(field.status_code, 201, field.text)

        public = self.client.get(f"/api/catalog/products/{product_id}").json()["product"]
        self.assertEqual(public["name"], name)
        finish = public["fields"][0]
        self.assertEqual([option["label"] for option in finish["options"]], ["Matte", "Glossy"])
        quote = self.client.post("/api/catalog/quote", json={"product_id": product_id, "quantity": 2, "options": {"finish": "glossy"}}).json()
        self.assertEqual(quote["estimated_total"], 218.0)

        # Edit the option list: rename one, remove one, add one.
        field_id = finish["id"]
        matte = finish["options"][0]
        updated = admin.patch(f"/api/admin/catalog/fields/{field_id}", json={
            "options": [{"id": matte["id"], "label": "Matte (soft touch)"}, {"label": "Satin"}],
        })
        self.assertEqual(updated.status_code, 200, updated.text)
        labels = [option["label"] for option in updated.json()["product"]["fields"][0]["options"]]
        self.assertEqual(labels, ["Matte (soft touch)", "Satin"])
        self.assertEqual(updated.json()["product"]["fields"][0]["options"][0]["value"], "matte", "Renaming keeps the stored value.")

        hidden = admin.patch(f"/api/admin/catalog/products/{product_id}", json={"active": False})
        self.assertEqual(hidden.status_code, 200)
        self.assertEqual(self.client.get(f"/api/catalog/products/{product_id}").status_code, 404)

        self.assertEqual(admin.delete(f"/api/admin/catalog/products/{product_id}").status_code, 204)
        self.assertEqual(admin.get(f"/api/admin/catalog/products/{product_id}").status_code, 404)

    def test_invalid_admin_input_is_rejected(self) -> None:
        admin = self.admin()
        self.assertEqual(admin.post("/api/admin/catalog/products", json={"name": "", "category_id": "sticker"}).status_code, 422)
        self.assertEqual(admin.post("/api/admin/catalog/products", json={"name": "Bad", "category_id": "missing"}).status_code, 422)
        self.assertEqual(admin.post("/api/admin/catalog/products", json={"name": "Bad", "category_id": "sticker", "base_price": -5}).status_code, 422)
        self.assertEqual(admin.post("/api/admin/catalog/products/white-mug/fields", json={"label": "Choice", "field_type": "select", "options": []}).status_code, 422)

    def test_category_with_products_cannot_be_deleted(self) -> None:
        admin = self.admin()
        self.assertEqual(admin.delete("/api/admin/catalog/categories/souvenir").status_code, 409)
        created = admin.post("/api/admin/catalog/categories", json={"label": f"Apparel {uuid.uuid4().hex[:4]}"})
        self.assertEqual(created.status_code, 201, created.text)
        new_id = next(category["id"] for category in created.json()["categories"] if category["label"].startswith("Apparel"))
        self.assertEqual(admin.delete(f"/api/admin/catalog/categories/{new_id}").status_code, 200)


if __name__ == "__main__":
    unittest.main()
