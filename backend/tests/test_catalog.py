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

# Package orders still collect the finish/envelope options and the structured event details.
INVITATION_PACKAGE_OPTIONS = {"paper_finish": "glossy", "envelope_option": "plain-white-envelope"}
BIRTHDAY_EVENT_OPTIONS = {
    **INVITATION_PACKAGE_OPTIONS,
    "celebrant_name": "Mia Santos",
    "celebrant_age": 7,
    "event_date": "2026-12-12",
    "event_time": "14:00",
    "event_location": "Quezon City",
}
WEDDING_EVENT_OPTIONS = {
    **INVITATION_PACKAGE_OPTIONS,
    "bride_name": "Maria Santos",
    "groom_name": "Juan Dela Cruz",
    "event_date": "2026-12-12",
    "event_time": "15:00",
    "event_location": "Manila Cathedral",
    "reception_venue": "The Peninsula Manila",
}
# The full customized (non-package) Birthday form.
BIRTHDAY_CUSTOM_OPTIONS = {
    "invitation_size": "5-x-7-in",
    "paper_finish": "matte",
    "printing_type": "single-sided-full-color",
    "orientation": "portrait",
    "folding_type": "bi-fold",
    "envelope_option": "plain-white-envelope",
    "design_source": "shop-will-design-it-for-me",
    **BIRTHDAY_EVENT_OPTIONS,
}
CHRISTENING_NO_EVENT_OPTIONS = {
    "invitation_size": "5-x-7-in",
    "paper_finish": "matte",
    "printing_type": "single-sided-full-color",
    "orientation": "portrait",
    "folding_type": "flat-card-no-fold",
    "envelope_option": "plain-white-envelope",
    "design_source": "shop-will-design-it-for-me",
}


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
        customization = {"quantity": quantity, "options": options, "notes": extra.pop("notes", "")}
        package_id = extra.pop("package_id", None)
        if package_id:
            customization["package_id"] = package_id
        if "mockup" in extra:
            customization["mockup"] = extra.pop("mockup")
        payload = {
            "payment_option": extra.pop("payment_option", None),
            "customer": {"name": "Catalog Customer", "email": email, "phone": "+63 917 123 4567", "messenger": ""},
            "product": {"id": product_id},
            "customization": customization,
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
        self.assertEqual(expected, {"invitation": 3, "souvenir": 8, "sticker": 3, "sintra-board": 6, "party-needs": 4, "large-format": 11})

    def test_wall_stickers_are_removed_and_product_labels_come_first(self) -> None:
        catalog = self.client.get("/api/catalog").json()
        stickers = next(category for category in catalog["categories"] if category["id"] == "sticker")
        ids = [product["id"] for product in stickers["products"]]
        self.assertNotIn("wall-stickers-decals", ids)
        self.assertEqual(ids[0], "product-label-stickers")

    def test_invitation_form_has_one_size_two_finishes_and_no_paper_type(self) -> None:
        for product_id in ("birthday-invitation", "wedding-invitation", "christening-invitation"):
            product = catalog_services.admin_product(product_id)
            fields = {field["field_key"]: field for field in product["fields"]}
            self.assertNotIn("paper_type", fields, product_id)
            self.assertNotIn("invitation_wording", fields, product_id)
            self.assertEqual(len(fields["invitation_size"]["options"]), 1, product_id)
            self.assertEqual([option["label"] for option in fields["paper_finish"]["options"]], ["Glossy", "Matte"])
            self.assertEqual(fields["paper_finish"]["section"], "options")
            self.assertEqual(fields["envelope_option"]["section"], "options")
            for key in ("event_date", "event_time", "event_location"):
                self.assertEqual(fields[key]["section"], "event", f"{product_id}.{key}")
                self.assertTrue(fields[key]["required"], f"{product_id}.{key}")
            self.assertEqual(product["min_quantity"], 20, product_id)

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

    def test_invitation_packages_are_seeded_with_published_prices(self) -> None:
        birthday = self.client.get("/api/catalog/products/birthday-invitation/packages")
        self.assertEqual(birthday.status_code, 200, birthday.text)
        sets = birthday.json()["packages"]
        self.assertEqual([item["name"] for item in sets], ["Set A — Birthday Card"])
        self.assertEqual(sets[0]["price"], 300.0)
        self.assertEqual(sets[0]["add_on_price"], 15.0)
        self.assertEqual(sets[0]["included_quantity"], 20)
        self.assertIn("Premium photo paper", sets[0]["inclusions"])

        christening = self.client.get("/api/catalog/products/christening-invitation/packages").json()["packages"]
        self.assertEqual([item["price"] for item in christening], [600.0, 2000.0])

        wedding = self.client.get("/api/catalog/products/wedding-invitation/packages").json()["packages"]
        self.assertEqual([item["price"] for item in wedding], [1800.0, 3000.0, 2400.0])
        self.assertEqual(len(wedding), 3)


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
        folds = next(field for field in product["fields"] if field["field_key"] == "folding_type")
        custom = next(option for option in folds["options"] if option["requires_quote"])
        priced = catalog_services.calculate_price(catalog_services.admin_product("wedding-invitation"), 50, {"folding_type": custom["value"]})
        self.assertIsNone(priced["estimated_total"])
        self.assertTrue(priced["requires_quote"])
        normal = catalog_services.calculate_price(catalog_services.admin_product("wedding-invitation"), 50, {"folding_type": folds["options"][0]["value"]})
        self.assertEqual(normal["estimated_total"], 1750.0)
        self.assertEqual(normal["unit_price"], 35.0)

    def test_package_quote_adds_envelope_upgrade_to_the_package_price(self) -> None:
        product = catalog_services.admin_product("christening-invitation")
        envelope = next(field for field in product["fields"] if field["field_key"] == "envelope_option")
        colored = next(option for option in envelope["options"] if option["value"] == "colored-envelope")
        catalog_services.update_pricing("christening-invitation", {"options": [{"id": colored["id"], "price_adjustment": 5, "price_type": "per_unit"}]})
        try:
            response = self.client.post("/api/catalog/quote", json={
                "product_id": "christening-invitation",
                "package_id": "christening-set-a",
                "quantity": 20,
                "options": {"paper_finish": "matte", "envelope_option": "colored-envelope", "envelope_color": "Blue"},
            })
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            # ₱600 package (20 pcs included) + 20 colored envelopes × ₱5.
            self.assertEqual(result["estimated_total"], 700.0)
            self.assertEqual(result["unit_price"], 35.0)
            self.assertEqual(result["pricing_mode"], "package")
        finally:
            catalog_services.update_pricing("christening-invitation", {"options": [{"id": colored["id"], "price_adjustment": 0, "price_type": "per_unit"}]})


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
        options = {**BIRTHDAY_CUSTOM_OPTIONS, "envelope_option": "colored-envelope"}
        missing_color = self.order(shopper, account["email"], "birthday-invitation", 30, options)
        self.assertEqual(missing_color.status_code, 422)
        self.assertIn("Envelope color", missing_color.json()["detail"])

        plain = self.order(shopper, account["email"], "birthday-invitation", 30, {**options, "envelope_option": "plain-white-envelope", "envelope_color": "ignored"})
        self.assertEqual(plain.status_code, 201, plain.text)
        self.assertNotIn("envelope_color", plain.json()["customization"]["options"])
        self.assertTrue(plain.json()["has_event"])

    def test_required_event_details_are_enforced(self) -> None:
        shopper, account = self.customer()
        response = self.order(shopper, account["email"], "christening-invitation", 20, CHRISTENING_NO_EVENT_OPTIONS)
        self.assertEqual(response.status_code, 422)
        self.assertIn("Celebrant's name", response.json()["detail"])

    def test_invitations_require_at_least_20_pieces(self) -> None:
        shopper, account = self.customer()
        too_few = self.order(shopper, account["email"], "birthday-invitation", 19, BIRTHDAY_CUSTOM_OPTIONS)
        self.assertEqual(too_few.status_code, 422)
        self.assertIn("minimum order 20", too_few.json()["detail"])
        enough = self.order(shopper, account["email"], "birthday-invitation", 20, BIRTHDAY_CUSTOM_OPTIONS)
        self.assertEqual(enough.status_code, 201, enough.text)

    def test_customized_invitation_order_separates_event_details(self) -> None:
        shopper, account = self.customer()
        response = self.order(shopper, account["email"], "birthday-invitation", 30, BIRTHDAY_CUSTOM_OPTIONS)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["order_kind"], "customize")
        event_labels = [row["label"] for row in body["event_fields"]]
        self.assertEqual(event_labels[:5], ["Celebrant's name", "Age to celebrate", "Event date", "Event time", "Event location"])
        # Event answers are not repeated in the specification list.
        spec_labels = [spec["label"] for spec in body["customization"]["specifications"]]
        self.assertNotIn("Celebrant's name", spec_labels)
        stored = order_repository.get_order(body["reference"])
        self.assertIsInstance(stored["event_details"], list)

    def test_birthday_package_uses_published_total_and_add_on(self) -> None:
        shopper, account = self.customer()
        response = self.order(
            shopper, account["email"], "birthday-invitation", 25,
            BIRTHDAY_EVENT_OPTIONS, package_id="birthday-set-a",
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["order_kind"], "package")
        self.assertEqual(body["package"]["name"], "Set A — Birthday Card")
        # ₱300 for the included 20 pcs + 5 additional pcs × ₱15.
        self.assertEqual(body["pricing"]["estimated_total"], 375.0)
        labels = {spec["label"]: spec["value"] for spec in body["customization"]["specifications"]}
        self.assertEqual(labels["Package"], "Set A — Birthday Card")
        self.assertIn("White envelope", labels["Inclusions"])
        self.assertEqual(labels["Paper finish"], "Glossy")
        self.assertEqual([row["label"] for row in body["event_fields"]][0], "Celebrant's name")

    def test_admin_order_views_expose_package_and_event_fields(self) -> None:
        shopper, account = self.customer()
        created = self.order(
            shopper, account["email"], "birthday-invitation", 25,
            BIRTHDAY_EVENT_OPTIONS, package_id="birthday-set-a",
        )
        self.assertEqual(created.status_code, 201, created.text)
        reference = created.json()["reference"]
        admin = self.admin()

        listing = admin.get("/api/admin/orders", params={"kind": "package", "page_size": 100})
        self.assertEqual(listing.status_code, 200, listing.text)
        rows = listing.json()["items"]
        self.assertTrue(rows, "The package order should appear in the filtered list.")
        self.assertTrue(all(row["order_kind"] == "package" for row in rows))
        listed = next(row for row in rows if row["reference"] == reference)
        self.assertEqual(listed["package_name"], "Set A — Birthday Card")

        detail = admin.get(f"/api/admin/orders/{reference}")
        self.assertEqual(detail.status_code, 200, detail.text)
        body = detail.json()
        self.assertEqual(body["order_kind"], "package")
        self.assertEqual(body["package"]["price"], 300.0)
        self.assertEqual(body["package"]["inclusions"][0], "20 pcs 4R (4\" × 6\") birthday cards")
        self.assertEqual(body["event_fields"][0], {"label": "Celebrant's name", "value": "Mia Santos"})

    def test_package_without_add_on_is_locked_to_its_quantity(self) -> None:
        shopper, account = self.customer()
        wrong = self.order(
            shopper, account["email"], "wedding-invitation", 25,
            WEDDING_EVENT_OPTIONS, package_id="wedding-set-b",
        )
        self.assertEqual(wrong.status_code, 422)
        telemetry = self.order(
            shopper, account["email"], "wedding-invitation", 20,
            WEDDING_EVENT_OPTIONS, package_id="wedding-set-b",
        )
        self.assertEqual(telemetry.status_code, 201, telemetry.text)
        self.assertEqual(telemetry.json()["pricing"]["estimated_total"], 3000.0)

    def test_package_from_another_category_is_rejected(self) -> None:
        shopper, account = self.customer()
        response = self.order(
            shopper, account["email"], "birthday-invitation", 20,
            BIRTHDAY_EVENT_OPTIONS, package_id="wedding-set-a",
        )
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

    def test_admin_can_manage_invitation_packages(self) -> None:
        admin = self.admin()
        listed = admin.get("/api/admin/catalog/products/birthday-invitation/packages")
        self.assertEqual(listed.status_code, 200, listed.text)
        name = f"Test Set {uuid.uuid4().hex[:6]}"
        created = admin.post("/api/admin/catalog/products/birthday-invitation/packages", json={
            "name": name,
            "price": 450,
            "size": '5" × 7"',
            "material": "Premium cardstock",
            "inclusions": ["20 pcs invitations", "White envelope"],
            "included_quantity": 20,
        })
        self.assertEqual(created.status_code, 201, created.text)
        made = next(item for item in created.json()["packages"] if item["name"] == name)
        self.assertEqual(made["price"], 450.0)
        self.assertIsNone(made["add_on_price"])

        self.assertEqual(admin.post("/api/admin/catalog/products/birthday-invitation/packages", json={"name": "Bad", "price": 0, "inclusions": ["x"]}).status_code, 422)
        self.assertEqual(admin.post("/api/admin/catalog/products/missing-product/packages", json={"name": "Bad", "price": 10, "inclusions": ["x"]}).status_code, 404)

        renamed = admin.patch(f"/api/admin/catalog/packages/{made['id']}", json={"name": f"{name} edited", "price": 500, "active": False})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        edited = next(item for item in renamed.json()["packages"] if item["id"] == made["id"])
        self.assertEqual((edited["name"], edited["price"], edited["active"]), (f"{name} edited", 500.0, False))

        public = self.client.get("/api/catalog/products/birthday-invitation/packages").json()["packages"]
        self.assertNotIn(made["id"], [item["id"] for item in public])

        shopper, account = self.customer()
        rejected = self.order(shopper, account["email"], "birthday-invitation", 20, BIRTHDAY_EVENT_OPTIONS, package_id=made["id"])
        self.assertEqual(rejected.status_code, 422)

        self.assertEqual(admin.delete(f"/api/admin/catalog/packages/{made['id']}").status_code, 200)
        self.assertEqual(admin.delete(f"/api/admin/catalog/packages/{made['id']}").status_code, 404)

    def test_field_section_is_saved(self) -> None:
        admin = self.admin()
        created = admin.post("/api/admin/catalog/products/birthday-invitation/fields", json={
            "label": "Extra design note", "field_type": "text", "section": "design",
        })
        self.assertEqual(created.status_code, 201, created.text)
        field = next(item for item in created.json()["product"]["fields"] if item["label"] == "Extra design note")
        self.assertEqual(field["section"], "design")
        invalid = admin.post("/api/admin/catalog/products/birthday-invitation/fields", json={
            "label": "Bad section", "field_type": "text", "section": "nowhere",
        })
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(admin.delete(f"/api/admin/catalog/fields/{field['id']}").status_code, 200)

    def test_category_with_products_cannot_be_deleted(self) -> None:
        admin = self.admin()
        self.assertEqual(admin.delete("/api/admin/catalog/categories/souvenir").status_code, 409)
        created = admin.post("/api/admin/catalog/categories", json={"label": f"Apparel {uuid.uuid4().hex[:4]}"})
        self.assertEqual(created.status_code, 201, created.text)
        new_id = next(category["id"] for category in created.json()["categories"] if category["label"].startswith("Apparel"))
        self.assertEqual(admin.delete(f"/api/admin/catalog/categories/{new_id}").status_code, 200)


TOTE_CUSTOM_OPTIONS = {
    "size": "medium-approx-12-x-14-in",
    "material": "canvas",
    "color": "natural-cream",
    "print_area": "front-only",
    "print_type": "full-color-heat-transfer",
    "packaging": "folded-no-packaging",
}


class PackageAndImageTests(CatalogTestCase):
    def tote_package(self, admin: TestClient, **overrides: object) -> dict:
        payload = {
            "name": f"Tote Set {uuid.uuid4().hex[:6]}",
            "description": "Test package",
            "price": 1000,
            "included_quantity": 10,
            "inclusions": ["10 canvas tote bags", "Front print"],
            "image_uploads": [{"name": "a.png", "dataUrl": PNG}, {"name": "b.png", "dataUrl": PNG}],
            **overrides,
        }
        created = admin.post("/api/admin/catalog/products/personalized-tote-bag/packages", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        return next(item for item in created.json()["packages"] if item["id"] == created.json()["package_id"])

    def test_any_product_can_have_packages_with_several_images(self) -> None:
        admin = self.admin()
        package = self.tote_package(admin)
        self.assertEqual(len(package["images"]), 2)
        self.assertEqual(package["image"], package["images"][0])
        for path in package["images"]:
            self.assertEqual(self.client.get(path).status_code, 200)

        # Remove the first image and add another one.
        updated = admin.patch(f"/api/admin/catalog/packages/{package['id']}", json={
            "images": package["images"][1:],
            "image_uploads": [{"name": "c.png", "dataUrl": PNG}],
        })
        self.assertEqual(updated.status_code, 200, updated.text)
        edited = next(item for item in updated.json()["packages"] if item["id"] == package["id"])
        self.assertEqual(len(edited["images"]), 2)
        self.assertEqual(edited["images"][0], package["images"][1])

        catalog = self.client.get("/api/catalog").json()
        tote = next(product for category in catalog["categories"] for product in category["products"] if product["id"] == "personalized-tote-bag")
        self.assertIn(package["id"], [item["id"] for item in tote["packages"]])
        self.assertIn("personalization", tote["package_field_keys"])
        self.assertNotIn("material", tote["package_field_keys"], "A package fixes the specifications.")

        listing = admin.get("/api/admin/catalog/packages")
        self.assertEqual(listing.status_code, 200, listing.text)
        listed = next(item for item in listing.json()["packages"] if item["id"] == package["id"])
        self.assertEqual(listed["product_name"], "Personalized Tote Bag")
        self.assertEqual(listed["price_per_piece"], 100.0)
        admin.delete(f"/api/admin/catalog/packages/{package['id']}")

    def test_package_order_for_a_non_invitation_product(self) -> None:
        admin = self.admin()
        package = self.tote_package(admin, add_on_price=90, add_on_label="Extra tote")
        shopper, account = self.customer()
        response = self.order(shopper, account["email"], "personalized-tote-bag", 12, {"personalization": "Team Rocket"}, package_id=package["id"])
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["order_kind"], "package")
        self.assertEqual(body["package"]["name"], package["name"])
        # ₱1,000 for 10 included + 2 extra × ₱90.
        self.assertEqual(body["pricing"]["estimated_total"], 1180.0)
        self.assertEqual(body["pricing"]["unit_price"], round(1180 / 12, 2))
        labels = {spec["label"]: spec["value"] for spec in body["customization"]["specifications"]}
        self.assertEqual(labels["Personalization text"], "Team Rocket")
        self.assertNotIn("Material", labels)
        # A customized order of the same product still uses the full form.
        custom = self.order(shopper, account["email"], "personalized-tote-bag", 3, TOTE_CUSTOM_OPTIONS)
        self.assertEqual(custom.status_code, 201, custom.text)
        self.assertEqual(custom.json()["order_kind"], "customize")
        admin.delete(f"/api/admin/catalog/packages/{package['id']}")

    def test_product_can_have_several_images(self) -> None:
        admin = self.admin()
        created = admin.post("/api/admin/catalog/products", json={
            "name": f"Gallery Product {uuid.uuid4().hex[:6]}", "category_id": "souvenir",
            "image_uploads": [{"name": "1.png", "dataUrl": PNG}, {"name": "2.png", "dataUrl": PNG}, {"name": "3.png", "dataUrl": PNG}],
        })
        self.assertEqual(created.status_code, 201, created.text)
        product = created.json()["product"]
        self.assertEqual(len(product["images"]), 3)
        self.assertEqual(product["image"], product["images"][0])
        reordered = admin.patch(f"/api/admin/catalog/products/{product['id']}", json={"images": [product["images"][2], product["images"][0]]})
        self.assertEqual(reordered.status_code, 200, reordered.text)
        self.assertEqual(reordered.json()["product"]["images"], [product["images"][2], product["images"][0]])
        self.assertEqual(reordered.json()["product"]["image"], product["images"][2])
        too_many = admin.patch(f"/api/admin/catalog/products/{product['id']}", json={"images": [f"/x/{index}.png" for index in range(13)]})
        self.assertEqual(too_many.status_code, 422)
        admin.delete(f"/api/admin/catalog/products/{product['id']}")


class PaymentOptionTests(CatalogTestCase):
    def quote(self, admin: TestClient, reference: str, amount: float, cod_allowed: bool = False) -> dict:
        version = admin.get(f"/api/admin/orders/{reference}").json()["version"]
        review = admin.post(f"/api/admin/orders/{reference}/status", json={"status": "Under Review", "expected_version": version})
        self.assertEqual(review.status_code, 200, review.text)
        quoted = admin.post(
            f"/api/admin/orders/{reference}/quotation",
            json={"total_amount": amount, "cod_allowed": cod_allowed, "expected_version": review.json()["version"]},
        )
        self.assertEqual(quoted.status_code, 200, quoted.text)
        self.assertEqual(quoted.json()["status"], "Quotation Sent")
        return quoted.json()

    def accept(self, shopper: TestClient, email: str, quoted: dict, option: str) -> dict:
        accepted = shopper.post(
            f"/api/orders/{quoted['reference']}/quotation/accept",
            json={"email": email, "payment_option": option, "quotation_revision": quoted["quotation"]["revision"]},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        return accepted.json()

    def test_down_payment_breakdown(self) -> None:
        shopper, account = self.customer()
        created = self.order(shopper, account["email"], "white-mug", 2, MUG_OPTIONS, payment_option="down_payment")
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        # Customized orders choose how to pay only after accepting the quotation.
        self.assertEqual(body["payment"]["payment_option"], "")
        self.assertEqual(body["payment"]["payment_option_label"], "Chosen after the quotation is accepted")
        admin = self.admin()
        self.quote(admin, body["reference"], 500)
        quoted = self.accept(shopper, account["email"], admin.get(f"/api/admin/orders/{body['reference']}").json(), "down_payment")
        self.assertEqual(quoted["status"], "Awaiting Down Payment")
        self.assertEqual(quoted["payment"]["payment_option_label"], "50% Down Payment")
        self.assertEqual(quoted["payment"]["down_payment_required"], 250.0)
        self.assertEqual(quoted["payment"]["down_payment_status"], "Pending")
        draft = admin.post(f"/api/admin/orders/{body['reference']}/draft", json={"file": {"name": "draft.png", "dataUrl": PNG}, "expected_version": quoted["version"]})
        self.assertEqual(draft.status_code, 409, "A draft needs the down payment first.")
        paid = admin.post(f"/api/admin/orders/{body['reference']}/payments", json={"amount": 250, "method": "GCash", "received": True, "expected_version": quoted["version"]})
        self.assertEqual(paid.status_code, 200, paid.text)
        self.assertEqual(paid.json()["status"], "Draft Preparation")
        self.assertEqual(paid.json()["payment"]["down_payment_status"], "Paid")
        self.assertEqual(paid.json()["payment"]["balance"], 250.0)

    def test_cash_on_delivery_needs_no_down_payment(self) -> None:
        shopper, account = self.customer()
        created = self.order(shopper, account["email"], "white-mug", 2, MUG_OPTIONS, payment_option="cod")
        self.assertEqual(created.status_code, 201, created.text)
        reference = created.json()["reference"]
        admin = self.admin()
        quoted = self.accept(shopper, account["email"], self.quote(admin, reference, 400, cod_allowed=True), "cod")
        self.assertEqual(quoted["status"], "Awaiting COD Confirmation")
        self.assertEqual(quoted["payment"]["down_payment_required"], 0.0)
        confirmed = admin.post(f"/api/admin/orders/{reference}/confirm-cod", json={"expected_version": quoted["version"]})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        quoted = confirmed.json()
        self.assertEqual(quoted["status"], "Draft Preparation", "A confirmed COD order skips the down payment stage.")
        self.assertEqual(quoted["payment"]["down_payment_status"], "Not required (COD)")
        detail = admin.get(f"/api/admin/orders/{reference}").json()
        self.assertTrue(detail["actions"]["send_draft"])
        self.assertEqual(detail["summary"]["payment_option"], "cod")
        draft = admin.post(f"/api/admin/orders/{reference}/draft", json={"file": {"name": "draft.png", "dataUrl": PNG}, "expected_version": quoted["version"]})
        self.assertEqual(draft.status_code, 200, draft.text)
        listing = admin.get("/api/admin/orders", params={"payment_option": "cod", "page_size": 100}).json()["items"]
        self.assertIn(reference, [row["reference"] for row in listing])
        self.assertTrue(all(row["payment_option"] == "cod" for row in listing))

    def test_invalid_payment_option_is_rejected(self) -> None:
        shopper, account = self.customer()
        response = self.order(shopper, account["email"], "white-mug", 1, MUG_OPTIONS, payment_option="credit")
        self.assertEqual(response.status_code, 422)

    def test_admin_can_switch_the_payment_option_before_payment(self) -> None:
        shopper, account = self.customer()
        created = self.order(shopper, account["email"], "white-mug", 1, MUG_OPTIONS)
        reference = created.json()["reference"]
        admin = self.admin()
        quoted = self.accept(shopper, account["email"], self.quote(admin, reference, 300), "down_payment")
        self.assertEqual(quoted["status"], "Awaiting Down Payment")
        switched = admin.post(f"/api/admin/orders/{reference}/payment-option", json={"payment_option": "cod", "expected_version": quoted["version"]})
        self.assertEqual(switched.status_code, 200, switched.text)
        self.assertEqual((switched.json()["payment_option"], switched.json()["status"]), ("cod", "Draft Preparation"))
        paid = admin.post(f"/api/admin/orders/{reference}/payments", json={"amount": 100, "method": "Cash", "received": True, "expected_version": switched.json()["version"]})
        self.assertEqual(paid.status_code, 200, paid.text)
        blocked = admin.post(f"/api/admin/orders/{reference}/payment-option", json={"payment_option": "down_payment", "expected_version": paid.json()["version"]})
        self.assertEqual(blocked.status_code, 409)


class MockupTests(CatalogTestCase):
    def test_invitation_mockup_is_saved_with_the_event_details(self) -> None:
        shopper, account = self.customer()
        response = self.order(
            shopper, account["email"], "wedding-invitation", 20, WEDDING_EVENT_OPTIONS, package_id="wedding-set-b",
            mockup={"template": "botanical", "palette": 1, "headline": "Together with our families", "message": "Join us"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        mockup = response.json()["mockup"]
        self.assertEqual((mockup["kind"], mockup["template"], mockup["palette"]), ("wedding", "botanical", 1))
        self.assertEqual(mockup["details"]["names"], "Maria Santos & Juan Dela Cruz")
        self.assertEqual(mockup["details"]["location"], "Manila Cathedral")
        labels = {spec["label"]: spec["value"] for spec in response.json()["customization"]["specifications"]}
        self.assertIn("Botanical Greenery", labels["Invitation mockup"])

    def test_mockup_is_only_for_invitations_and_known_templates(self) -> None:
        shopper, account = self.customer()
        wrong_template = self.order(shopper, account["email"], "birthday-invitation", 20, BIRTHDAY_CUSTOM_OPTIONS, mockup={"template": "botanical"})
        self.assertEqual(wrong_template.status_code, 422)
        mug = self.order(shopper, account["email"], "white-mug", 1, MUG_OPTIONS, mockup={"template": "confetti"})
        self.assertEqual(mug.status_code, 422)
        too_long = self.order(shopper, account["email"], "birthday-invitation", 20, BIRTHDAY_CUSTOM_OPTIONS, mockup={"template": "confetti", "headline": "x" * 81})
        self.assertEqual(too_long.status_code, 422)


class AdminInsightTests(CatalogTestCase):
    def test_counts_customer_totals_and_payment_summary(self) -> None:
        admin = self.admin()
        before = admin.get("/api/admin/counts")
        self.assertEqual(before.status_code, 200, before.text)
        shopper, account = self.customer()
        created = self.order(shopper, account["email"], "white-mug", 2, MUG_OPTIONS)
        reference = created.json()["reference"]
        after = admin.get("/api/admin/counts").json()
        self.assertEqual(after["new_requests"], before.json()["new_requests"] + 1)
        self.assertEqual(after["orders"], after["new_requests"] + after["to_review"])
        self.assertNotEqual(after["signature"], before.json()["signature"])
        self.assertGreaterEqual(after["customers"], 1)

        version = admin.get(f"/api/admin/orders/{reference}").json()["version"]
        review = admin.post(f"/api/admin/orders/{reference}/status", json={"status": "Under Review", "expected_version": version}).json()
        quoted = admin.post(f"/api/admin/orders/{reference}/quotation", json={"total_amount": 800, "expected_version": review["version"]}).json()
        accepted = shopper.post(
            f"/api/orders/{reference}/quotation/accept",
            json={"email": account["email"], "payment_option": "down_payment", "quotation_revision": quoted["quotation"]["revision"]},
        ).json()
        paid = admin.post(f"/api/admin/orders/{reference}/payments", json={"amount": 500, "method": "Maya", "received": True, "expected_version": accepted["version"]})
        self.assertEqual(paid.status_code, 200, paid.text)

        customers = admin.get("/api/admin/customers", params={"search": account["email"]}).json()
        row = customers["items"][0]
        self.assertEqual(row["total_transaction_amount"], 800.0)
        self.assertEqual(row["total_paid"], 500.0)
        self.assertEqual(row["outstanding_balance"], 300.0)
        self.assertEqual(row["payment_status"], "With balance")
        filtered = admin.get("/api/admin/customers", params={"payment_status": "With balance", "page_size": 100}).json()
        self.assertIn(account["email"], [item["email"] for item in filtered["items"]])

        summary = admin.get("/api/admin/reports/summary", params={"preset": "today"}).json()
        payments = summary["payments"]
        self.assertGreaterEqual(payments["received_total"], 500.0)
        self.assertGreaterEqual(payments["down_payments"], 400.0)
        self.assertIn("Maya", [item["method"] for item in payments["by_method"]])
        for file_format in ("pdf", "xlsx"):
            export = admin.get("/api/admin/exports/summary", params={"preset": "today", "format": file_format})
            self.assertEqual(export.status_code, 200, export.text[:200])
        customers_export = admin.get("/api/admin/exports/customers", params={"format": "xlsx"})
        self.assertEqual(customers_export.status_code, 200)


if __name__ == "__main__":
    unittest.main()
