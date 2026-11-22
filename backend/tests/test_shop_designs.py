"""Shop-made design selection: the TEMPORARY SAMPLE gallery, design orders, and admin design management.

Workflow covered end to end:
    customer opens a service → browses the (sample) designs → selects one → enters the personalization
    details → gets the price preview → submits → the administrator sees the design and the details →
    the administrator uploads a real design, replaces/deletes the samples → the old order is unchanged.
"""
from __future__ import annotations

import base64
import os
import tempfile
import unittest
import uuid
from pathlib import Path


_test_directory = tempfile.TemporaryDirectory(prefix="impress-design-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.rate_limit import limiter  # noqa: E402
from backend.app.core.security import SESSION_COOKIE, create_session_token  # noqa: E402
from backend.app.database.connection import connection_scope  # noqa: E402
from backend.app.database.design_seed import SAMPLE_DESIGNS  # noqa: E402
from backend.app.repositories import catalog_repository, design_repository  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.tests.support import restore_sample_designs, stored_order  # noqa: E402


PASSWORD = "Correct-Horse-Battery-1"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64
PNG = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()
BIRTHDAY_DETAILS = {
    "celebrant_name": "Mia Santos",
    "celebrant_age": 7,
    "event_date": "2026-03-14",
    "event_time": "15:00",
    "event_location": "Santos Residence, Pasig City",
}
INVITATION_SPECS = {
    "invitation_size": "5-x-7-in", "printing_type": "single-sided-full-color", "folding_type": "flat-card-no-fold",
    "orientation": "portrait", "paper_finish": "glossy", "envelope_option": "plain-white-envelope",
}


def setUpModule() -> None:
    # Other test modules sharing the database may have removed the sample designs.
    restore_sample_designs()


class DesignTestCase(unittest.TestCase):
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
            "name": "Design Customer", "email": f"{uuid.uuid4().hex[:12]}@example.test",
            "phone": "+63 917 123 4567", "messenger": "", "password": PASSWORD,
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.email = response.json()["customer"]["email"]

    def catalog_product(self, product_id: str) -> dict:
        for category in self.client.get("/api/catalog").json()["categories"]:
            for product in category["products"]:
                if product["id"] == product_id:
                    return product
        raise AssertionError(f"{product_id} is not in the public catalog")

    def order(self, product_id: str, design_id: str, details: dict, options: dict | None = None, quantity: int = 20,
              expect: int = 201, **extra) -> dict:
        body = {
            "customer": {"name": "Design Customer", "email": self.email, "phone": "+63 917 123 4567", "messenger": ""},
            "product": {"id": product_id},
            "customization": {"quantity": quantity, "options": options if options is not None else INVITATION_SPECS,
                              "notes": "", "design_id": design_id, "design_details": details},
            "event": {}, "has_event": False, "acknowledged": True, "files": [], "field_files": {},
        }
        body.update(extra)
        response = self.shopper.post("/api/orders", headers={"Idempotency-Key": uuid.uuid4().hex * 2}, json=body)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def create_design(self, **values) -> dict:
        body = {
            "name": f"Shop Design {uuid.uuid4().hex[:6]}",
            "description": "Real artwork from the shop.",
            "style": "Floral",
            "category_id": "invitation",
            "product_ids": ["birthday-invitation"],
            "fields": [{"key": "celebrant_name", "required": True}, {"key": "event_date", "required": False}],
            "image_upload": {"name": "design.png", "dataUrl": PNG},
            **values,
        }
        response = self.admin.post("/api/admin/designs", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        result = response.json()
        return next(design for design in result["designs"] if design["id"] == result["design_id"])


class SampleGalleryTests(DesignTestCase):
    def test_every_service_category_has_temporary_sample_designs(self) -> None:
        data = self.admin.get("/api/admin/designs").json()
        categories = {design["category_id"] for design in data["designs"] if design["is_sample"]}
        self.assertTrue({"invitation", "souvenir", "sticker", "sintra-board", "party-needs", "large-format"} <= categories)
        self.assertGreaterEqual(data["sample_count"], len(SAMPLE_DESIGNS) - 5)
        self.assertTrue(data["field_library"])

    def test_sample_images_exist_and_are_marked_as_samples(self) -> None:
        for design in SAMPLE_DESIGNS:
            path = Path("frontend/assets") / design["image"][len("/frontend/"):]
            self.assertTrue(path.is_file(), path)
            self.assertIn("TEMPORARY SAMPLE", path.read_text(encoding="utf-8"))
        response = self.client.get(SAMPLE_DESIGNS[0]["image"])
        self.assertEqual(response.status_code, 200)
        self.assertIn("svg", response.headers["content-type"])

    def test_customer_sees_the_designs_of_the_chosen_product_only(self) -> None:
        birthday = self.catalog_product("birthday-invitation")
        names = {design["id"] for design in birthday["designs"]}
        self.assertIn("sample-birthday-confetti", names)
        self.assertNotIn("sample-wedding-blush-garden", names)
        design = next(item for item in birthday["designs"] if item["id"] == "sample-birthday-confetti")
        self.assertTrue(design["is_sample"])
        self.assertEqual([field["field_key"] for field in design["fields"]][:2], ["celebrant_name", "celebrant_age"])
        self.assertNotIn("file", {field["field_type"] for field in design["fields"]})
        # The design order asks for the print specifications only, never a design file or event section.
        self.assertIn("invitation_size", birthday["design_field_keys"])
        self.assertIn("paper_finish", birthday["design_field_keys"])
        self.assertNotIn("design_file", birthday["design_field_keys"])
        self.assertNotIn("photo_upload", birthday["design_field_keys"])
        self.assertNotIn("celebrant_name", birthday["design_field_keys"])

    def test_non_invitation_design_form_drops_artwork_questions(self) -> None:
        tarpaulin = self.catalog_product("birthday-wedding-graduation-tarpaulins")
        self.assertIn("width", tarpaulin["design_field_keys"])
        for key in ("design_preference", "design_upload", "layout_photos", "print_text"):
            self.assertNotIn(key, tarpaulin["design_field_keys"])
        self.assertTrue(tarpaulin["designs"])


class DesignOrderTests(DesignTestCase):
    def test_quote_for_a_design_uses_the_product_rates(self) -> None:
        response = self.client.post("/api/catalog/quote", json={
            "product_id": "birthday-invitation", "design_id": "sample-birthday-confetti", "quantity": 20, "options": INVITATION_SPECS,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("estimated_total", response.json())
        wrong = self.client.post("/api/catalog/quote", json={
            "product_id": "birthday-invitation", "design_id": "sample-wedding-blush-garden", "quantity": 20, "options": {},
        })
        self.assertEqual(wrong.status_code, 422)

    def test_customer_orders_a_design_and_admin_reviews_it(self) -> None:
        order = self.order("birthday-invitation", "sample-birthday-confetti", BIRTHDAY_DETAILS)
        self.assertEqual(order["order_kind"], "design")
        self.assertEqual(order["design"]["id"], "sample-birthday-confetti")
        self.assertEqual(order["design"]["name"], "Confetti Celebration")
        self.assertTrue(order["design"]["is_sample"])
        self.assertEqual(order["attachments"], [])
        self.assertIsNone(order["total_amount"])  # priced by the administrator's quotation
        details = {row["label"]: row["value"] for row in order["design"]["details"]}
        self.assertEqual(details["Celebrant's name"], "Mia Santos")
        self.assertEqual(details["Age"], "7")
        self.assertEqual(details["Event location"], "Santos Residence, Pasig City")

        admin_view = self.admin.get(f"/api/admin/orders/{order['reference']}").json()
        self.assertEqual(admin_view["design"]["image"], "/frontend/image/designs/invitation/birthday-confetti.svg")
        self.assertEqual(admin_view["order_kind"], "design")
        listed = self.admin.get("/api/admin/orders", params={"kind": "design", "search": order["reference"]}).json()
        rows = listed["items"]
        self.assertTrue(any(row["reference"] == order["reference"] and row["design_name"] == "Confetti Celebration" for row in rows), listed)

    def test_customer_can_still_send_their_own_design(self) -> None:
        # "Use my own design": a customized order with the customer's own file, for a product that has designs.
        own_design = {**INVITATION_SPECS,
                      "celebrant_name": "Mia Santos", "event_date": "2027-03-14", "event_time": "15:00",
                      "event_location": "Santos Residence, Pasig City"}
        order = self.order("birthday-invitation", "", {}, options=own_design,
                           field_files={"design_file": [{"name": "my-design.png", "dataUrl": PNG}]})
        self.assertEqual(order["order_kind"], "customize")
        self.assertIsNone(order["design"])
        self.assertEqual([item["name"] for item in order["attachments"]], ["my-design.png"])

    def test_own_design_needs_the_design_file_and_offers_no_other_design_choice(self) -> None:
        birthday = self.catalog_product("birthday-invitation")
        keys = [field["field_key"] for field in birthday["fields"]]
        self.assertNotIn("design_source", keys)
        design_file = next(field for field in birthday["fields"] if field["field_key"] == "design_file")
        self.assertTrue(design_file["required"])
        self.assertEqual(design_file["depends_on"], "")
        options = {**INVITATION_SPECS, "celebrant_name": "Mia", "event_date": "2027-03-14", "event_time": "15:00", "event_location": "Pasig"}
        response = self.order("birthday-invitation", "", {}, options=options, expect=422)
        self.assertIn("Your design file", response["detail"])

    def test_existing_databases_drop_the_design_customization_choice_once(self) -> None:
        # An older database still has "Design customization" (upload / shop designs it / sample as reference).
        with connection_scope() as connection:
            product = "christening-invitation"
            design_file = connection.execute(
                "SELECT id FROM catalog_fields WHERE product_id = ? AND field_key = 'design_file'", (product,)).fetchone()["id"]
            connection.execute(
                "UPDATE catalog_fields SET depends_on = 'design_source', depends_values = ?, required = 1 WHERE id = ?",
                ('["i-will-upload-my-own-design"]', design_file))
            connection.execute(
                "INSERT INTO catalog_fields (id, product_id, field_key, label, field_type, required, placeholder, help_text, default_value, "
                "max_files, depends_on, depends_values, section, sort_order, active, created_at, updated_at) "
                "VALUES ('fld_old_source', ?, 'design_source', 'Design customization', 'radio', 1, '', '', '', 1, '', '[]', 'design', 0, 1, 'x', 'x')",
                (product,))
            connection.execute(
                "INSERT INTO catalog_field_options (id, field_id, value, label, price_adjustment, price_type, requires_quote, sort_order, active, created_at, updated_at) "
                "VALUES ('opt_old_shop', 'fld_old_source', 'shop-will-design-it-for-me', 'Shop will design it for me', 0, 'per_unit', 0, 0, 1, 'x', 'x')")
            connection.execute("DELETE FROM app_meta WHERE key = ?", (catalog_repository.OWN_DESIGN_MARKER,))
        catalog_repository.initialize()
        fields = {field["field_key"]: field for field in self.catalog_product("christening-invitation")["fields"]}
        self.assertNotIn("design_source", fields)
        self.assertEqual(fields["design_file"]["depends_on"], "")
        self.assertTrue(fields["design_file"]["required"])
        with connection_scope() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM catalog_field_options WHERE id = 'opt_old_shop'").fetchone())

    def test_required_personalization_is_enforced(self) -> None:
        missing = {key: value for key, value in BIRTHDAY_DETAILS.items() if key != "celebrant_name"}
        response = self.order("birthday-invitation", "sample-birthday-confetti", missing, expect=422)
        self.assertIn("Celebrant's name", response["detail"])
        bad_date = {**BIRTHDAY_DETAILS, "event_date": "not-a-date"}
        self.order("birthday-invitation", "sample-birthday-confetti", bad_date, expect=422)

    def test_design_orders_never_accept_uploaded_files(self) -> None:
        response = self.order("birthday-invitation", "sample-birthday-confetti", BIRTHDAY_DETAILS, expect=422,
                              field_files={"photo_upload": [{"name": "mine.png", "dataUrl": PNG}]})
        self.assertIn("shop's design", response["detail"])
        self.order("birthday-invitation", "sample-birthday-confetti", BIRTHDAY_DETAILS, expect=422,
                   files=[{"name": "mine.png", "dataUrl": PNG}])

    def test_design_must_belong_to_the_product_and_be_active(self) -> None:
        self.order("birthday-invitation", "sample-wedding-blush-garden", {"couple_names": "A & B"}, expect=422)
        design = self.create_design()
        self.admin.patch(f"/api/admin/designs/{design['id']}", json={"active": False})
        self.assertNotIn(design["id"], {item["id"] for item in self.catalog_product("birthday-invitation")["designs"]})
        self.order("birthday-invitation", design["id"], {"celebrant_name": "Mia"}, expect=422)
        self.admin.delete(f"/api/admin/designs/{design['id']}")

    def test_non_invitation_design_order(self) -> None:
        order = self.order(
            "birthday-wedding-graduation-tarpaulins", "sample-birthday-confetti-tarpaulin",
            {"celebrant_name": "Mia", "headline": "Happy 7th Birthday!"},
            # A custom size (quoted by the shop) asks for the width and height.
            options={"standard_size": "custom", "width": 3, "height": 5, "measurement_unit": "ft", "material": "tarpaulin-standard", "placement": "indoor",
                     "eyelets": "four-corners-only", "hemming": "no-hemming", "occasion": "birthday",
                     # Artwork questions are not part of a design order and are dropped.
                     "design_preference": "i-will-upload-a-print-ready-design"},
            quantity=1,
        )
        self.assertEqual(order["order_kind"], "design")
        self.assertEqual(order["customization"]["options"].get("width"), 3)
        self.assertNotIn("design_preference", order["customization"]["options"])


class AdminDesignManagementTests(DesignTestCase):
    @staticmethod
    def restore_samples() -> None:
        """Seed the samples again so the other tests still find them."""
        with connection_scope() as connection:
            connection.execute("DELETE FROM app_meta WHERE key = ?", (design_repository.SEED_MARKER,))
        design_repository.initialize()

    def test_admin_requires_a_session(self) -> None:
        self.assertEqual(TestClient(app).get("/api/admin/designs").status_code, 401)
        self.assertEqual(self.shopper.post("/api/admin/designs", json={}).status_code, 401)

    def test_upload_edit_and_delete_a_real_design(self) -> None:
        design = self.create_design()
        self.assertFalse(design["is_sample"])
        self.assertTrue(design["image"].startswith("/api/designs/images/"))
        image = self.client.get(design["image"])
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.content, PNG_BYTES)
        self.assertEqual(image.headers["content-type"], "image/png")

        response = self.admin.patch(f"/api/admin/designs/{design['id']}", json={
            "name": "Renamed Design", "description": "Updated", "product_ids": [], "category_id": "invitation",
        })
        self.assertEqual(response.status_code, 200, response.text)
        updated = next(item for item in response.json()["designs"] if item["id"] == design["id"])
        self.assertEqual(updated["name"], "Renamed Design")
        # No products selected: offered for every product of the category.
        self.assertIn(design["id"], {item["id"] for item in self.catalog_product("wedding-invitation")["designs"]})

        self.assertEqual(self.admin.delete(f"/api/admin/designs/{design['id']}").status_code, 200)
        self.assertEqual(self.client.get(design["image"]).status_code, 404)

    def test_validation(self) -> None:
        base = {"name": "X", "category_id": "invitation", "fields": [{"key": "celebrant_name", "required": True}]}
        self.assertEqual(self.admin.post("/api/admin/designs", json=base).status_code, 422)  # no image
        with_image = {**base, "image_upload": {"name": "a.png", "dataUrl": PNG}}
        self.assertEqual(self.admin.post("/api/admin/designs", json={**with_image, "fields": []}).status_code, 422)
        self.assertEqual(self.admin.post("/api/admin/designs", json={**with_image, "fields": [{"key": "nope"}]}).status_code, 422)
        self.assertEqual(self.admin.post("/api/admin/designs", json={**with_image, "product_ids": ["white-mug"]}).status_code, 422)
        self.assertEqual(self.admin.post("/api/admin/designs", json={**with_image, "category_id": "missing"}).status_code, 422)
        gif = {"name": "a.gif", "dataUrl": "data:image/gif;base64," + base64.b64encode(b"GIF89a").decode()}
        self.assertEqual(self.admin.post("/api/admin/designs", json={**base, "image_upload": gif}).status_code, 422)

    def test_replacing_and_deleting_designs_keeps_existing_orders(self) -> None:
        # An order placed with an uploaded design keeps its picture after the design is replaced and deleted.
        design = self.create_design()
        order = self.order("birthday-invitation", design["id"], {"celebrant_name": "Mia"})
        old_image = order["design"]["image"]
        replaced = self.admin.patch(f"/api/admin/designs/{design['id']}", json={
            "name": "Replacement", "image_upload": {"name": "new.png", "dataUrl": PNG},
        }).json()
        new_image = next(item for item in replaced["designs"] if item["id"] == design["id"])["image"]
        self.assertNotEqual(new_image, old_image)
        self.admin.delete(f"/api/admin/designs/{design['id']}")
        self.assertEqual(self.client.get(old_image).status_code, 200)  # pinned by the order
        self.assertEqual(self.client.get(new_image).status_code, 404)  # unused, removed with the design
        saved = stored_order(order["reference"])
        self.assertEqual(saved["customization"]["design"]["name"], design["name"])
        self.assertEqual(saved["customization"]["design"]["image"], old_image)

    def test_replace_a_sample_with_the_real_design(self) -> None:
        sample_id = "sample-wedding-modern-monogram"
        order = self.order("wedding-invitation", sample_id, {
            "couple_names": "Andrea & Joshua", "event_date": "2026-11-21", "event_time": "16:00", "event_location": "Tagaytay",
        })
        response = self.admin.patch(f"/api/admin/designs/{sample_id}", json={
            "name": "Monogram (official)", "is_sample": False, "image_upload": {"name": "real.png", "dataUrl": PNG},
        })
        self.assertEqual(response.status_code, 200, response.text)
        replaced = next(item for item in response.json()["designs"] if item["id"] == sample_id)
        self.assertFalse(replaced["is_sample"])
        self.assertTrue(replaced["uploaded_image"])
        # The old order still shows the sample it was placed with.
        self.assertEqual(stored_order(order["reference"])["customization"]["design"]["name"], "Modern Monogram")

    def test_remove_all_temporary_samples(self) -> None:
        self.addCleanup(self.restore_samples)
        real = self.create_design()
        order = self.order("christening-invitation", "sample-christening-peace-dove", {
            "celebrant_name": "Sofia", "event_date": "2026-01-24", "event_time": "09:00", "event_location": "Antipolo",
        })
        response = self.admin.delete("/api/admin/designs/samples")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertGreater(result["deleted"], 0)
        self.assertEqual(result["sample_count"], 0)
        self.assertIn(real["id"], {design["id"] for design in result["designs"]})
        self.assertEqual({design["id"] for design in self.catalog_product("christening-invitation")["designs"]}, set())
        # Existing orders keep the design snapshot, and the admin can still open them.
        view = self.admin.get(f"/api/admin/orders/{order['reference']}").json()
        self.assertEqual(view["design"]["name"], "Peace Dove")
        self.assertTrue(view["design"]["is_sample"])
        self.admin.delete(f"/api/admin/designs/{real['id']}")


if __name__ == "__main__":
    unittest.main()
