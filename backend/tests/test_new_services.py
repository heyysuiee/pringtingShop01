"""Invitation standard sizes, the Tarpaulin size price list, large-format uploads, and T-Shirt Printing."""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


_test_directory = tempfile.TemporaryDirectory(prefix="impress-new-services-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from fastapi.testclient import TestClient  # noqa: E402
from httpx import Response  # noqa: E402

from backend.app.core.rate_limit import limiter  # noqa: E402
from backend.app.core.security import SESSION_COOKIE, create_session_token  # noqa: E402
from backend.app.database.catalog_seed import TARPAULIN_PRICE_LIST  # noqa: E402
from backend.app.database.connection import connection_scope  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.repositories import catalog_repository, file_repository  # noqa: E402
from backend.app.services import catalog_services, file_services  # noqa: E402
from backend.tests.support import stored_order  # noqa: E402


PASSWORD = "Correct-Horse-Battery-1"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64
PNG = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()
MB = 1024 * 1024

TARPAULIN = "event-tarpaulins"
TARPAULIN_OPTIONS = {
    "material": "tarpaulin-standard",
    "placement": "indoor",
    "eyelets": "four-corners-only",
    "hemming": "no-hemming",
    "print_text": "Grand Opening — Lola's Kitchen",
    "design_preference": "shop-will-lay-out-my-text-and-photos",
}
HEAT_PRESS_OPTIONS = {
    "tshirt_type": "cotton-round-neck",
    "tshirt_color": "black",
    "tshirt_size": "2xl",
    "print_placement": "front-and-back",
    "print_size": "a4-up-to-8-11-in",
}
SUBLIMATION_OPTIONS = {
    "tshirt_type": "polyester-round-neck",
    "tshirt_size": "m",
    "sublimation_type": "full-sublimation-all-over-print",
}


def key() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]


class ServicesTestCase(unittest.TestCase):
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
        response = shopper.post("/api/customer/register", json={
            "name": "Service Customer",
            "email": f"{uuid.uuid4().hex[:12]}@example.test",
            "phone": "+63 917 123 4567",
            "messenger": "",
            "password": PASSWORD,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return shopper, response.json()["customer"]

    def admin(self) -> TestClient:
        admin = TestClient(app)
        admin.cookies.set(SESSION_COOKIE, create_session_token("admin@example.test", "admin"))
        return admin

    def order(self, shopper: TestClient, email: str, product_id: str, quantity: int, options: dict, **extra) -> Response:
        customization = {"quantity": quantity, "options": options, "notes": extra.pop("notes", "")}
        for name in ("package_id", "design_id", "design_details"):
            if name in extra:
                customization[name] = extra.pop(name)
        payload = {
            "customer": {"name": "Service Customer", "email": email, "phone": "+63 917 123 4567", "messenger": ""},
            "product": {"id": product_id},
            "customization": customization,
            "event": {},
            "has_event": False,
            "acknowledged": True,
            "files": extra.pop("files", []),
            "field_files": extra.pop("field_files", {}),
        }
        return shopper.post("/api/orders", json=payload, headers={"Idempotency-Key": key()})

    def catalog_product(self, product_id: str) -> dict:
        catalog = self.client.get("/api/catalog").json()
        return next(product for category in catalog["categories"] for product in category["products"] if product["id"] == product_id)

    def upload(self, shopper: TestClient, product_id: str, field_key: str, contents: bytes, name: str = "design.png", mime: str = "image/png") -> str:
        started = shopper.post("/api/uploads", json={"product_id": product_id, "field_key": field_key, "name": name, "size": len(contents), "mime_type": mime})
        self.assertEqual(started.status_code, 201, started.text)
        upload_id = started.json()["upload_id"]
        size = started.json()["chunk_size"]
        for index, offset in enumerate(range(0, len(contents), size)):
            sent = shopper.put(f"/api/uploads/{upload_id}/chunks/{index}", content=contents[offset:offset + size], headers={"Content-Type": "application/octet-stream"})
            self.assertEqual(sent.status_code, 200, sent.text)
        done = shopper.post(f"/api/uploads/{upload_id}/complete")
        self.assertEqual(done.status_code, 200, done.text)
        return upload_id

    def version(self, admin: TestClient, reference: str) -> int:
        return admin.get(f"/api/admin/orders/{reference}").json()["version"]


# ================================================================== invitations


class InvitationStandardSizeTests(ServicesTestCase):
    def test_each_invitation_type_shows_its_standard_size(self) -> None:
        expected = {"birthday-invitation": "4 x 6 in", "christening-invitation": "4 x 6 in", "wedding-invitation": "5 x 7 in"}
        for product_id, label in expected.items():
            product = self.catalog_product(product_id)
            self.assertEqual(product["standard_size_label"], label, product_id)
            size = next(field for field in product["fields"] if field["field_key"] == "invitation_size")
            default = next(option for option in size["options"] if option["value"] == size["default_value"])
            self.assertEqual(default["label"], label)
            self.assertIn(label, size["help_text"])

    def test_selected_size_is_saved_and_shown_to_the_admin(self) -> None:
        shopper, account = self.customer()
        options = {
            "invitation_size": "4-x-6-in", "paper_finish": "matte", "printing_type": "single-sided-full-color",
            "orientation": "portrait", "folding_type": "flat-card-no-fold", "envelope_option": "plain-white-envelope",
            "celebrant_name": "Noah", "event_date": "2026-12-12", "event_time": "10:00", "event_location": "St. Joseph Parish",
        }
        response = self.order(shopper, account["email"], "christening-invitation", 20, options,
                              field_files={"design_file": [{"name": "mine.png", "dataUrl": PNG}]})
        self.assertEqual(response.status_code, 201, response.text)
        reference = response.json()["reference"]
        admin_view = self.admin().get(f"/api/admin/orders/{reference}").json()
        specs = {spec["label"]: spec["value"] for spec in admin_view["customization"]["specifications"]}
        self.assertEqual(specs["Invitation size"], "4 x 6 in")
        portal = shopper.get(f"/api/account/orders/{reference}").json()
        self.assertEqual(portal["customization"]["options"]["invitation_size"], "4-x-6-in")

    def test_package_without_its_own_size_records_the_standard_size(self) -> None:
        shopper, account = self.customer()
        options = {
            "paper_finish": "glossy", "envelope_option": "plain-white-envelope", "bride_name": "Maria", "groom_name": "Juan",
            "event_date": "2026-12-12", "event_time": "15:00", "event_location": "Manila Cathedral",
        }
        response = self.order(shopper, account["email"], "wedding-invitation", 20, options, package_id="wedding-set-b")
        self.assertEqual(response.status_code, 201, response.text)
        specs = {spec["label"]: spec["value"] for spec in response.json()["customization"]["specifications"]}
        self.assertEqual(specs["Size"], "5 x 7 in (standard size)")
        # Package prices and configurations are unchanged.
        self.assertEqual(response.json()["total_amount"], 3000.0)


# ================================================================== tarpaulin price list


class TarpaulinPriceListTests(ServicesTestCase):
    def test_every_tarpaulin_product_offers_the_eight_sizes_and_prices(self) -> None:
        catalog = self.client.get("/api/catalog").json()
        large = next(category for category in catalog["categories"] if category["id"] == "large-format")
        expected = [(f"{width} × {height} ft", price) for width, height, price in TARPAULIN_PRICE_LIST]
        self.assertEqual(
            expected,
            [("2 × 3 ft", 200.0), ("2 × 4 ft", 250.0), ("3 × 4 ft", 300.0), ("3 × 5 ft", 350.0),
             ("4 × 5 ft", 400.0), ("4 × 6 ft", 500.0), ("4 × 7 ft", 550.0), ("4 × 8 ft", 600.0)],
        )
        for product in large["products"]:
            self.assertEqual(product["pricing_mode"], "size", product["id"])
            self.assertEqual([(size["label"], size["price"]) for size in product["standard_sizes"]], expected, product["id"])
            field = product["fields"][0]
            self.assertEqual(field["field_key"], "standard_size")
            self.assertEqual(field["options"][-1]["value"], "custom")
            self.assertTrue(field["options"][-1]["requires_quote"])
            width = next(item for item in product["fields"] if item["field_key"] == "width")
            self.assertEqual((width["depends_on"], width["depends_values"]), ("standard_size", ["custom"]))

    def test_subtotal_is_size_price_times_quantity_for_every_size(self) -> None:
        for width, height, price in TARPAULIN_PRICE_LIST:
            size_id = f"tarpaulin-{width}x{height}-ft"
            for quantity in (1, 3):
                result = self.client.post("/api/catalog/quote", json={
                    "product_id": TARPAULIN, "quantity": quantity, "options": {**TARPAULIN_OPTIONS, "standard_size": size_id},
                }).json()
                self.assertEqual(result["estimated_total"], price * quantity, size_id)
                self.assertEqual(result["unit_price"], price)
                self.assertEqual(result["price_basis"], "standard_size")
                self.assertEqual(result["standard_size"]["label"], f"{width} × {height} ft")
                self.assertEqual(result["area_sqft"], float(width * height))
                self.assertEqual(result["down_payment"], round(price * quantity / 2, 2))

    def test_custom_size_is_quoted_and_asks_for_dimensions(self) -> None:
        result = self.client.post("/api/catalog/quote", json={
            "product_id": TARPAULIN, "quantity": 2, "options": {**TARPAULIN_OPTIONS, "standard_size": "custom", "width": 10, "height": 20},
        }).json()
        self.assertIsNone(result["estimated_total"])
        self.assertTrue(result["requires_quote"])
        self.assertIsNone(result["price_basis"])
        shopper, account = self.customer()
        missing = self.order(shopper, account["email"], TARPAULIN, 1, {**TARPAULIN_OPTIONS, "standard_size": "custom"})
        self.assertEqual(missing.status_code, 422)
        self.assertIn("Custom width", missing.json()["detail"])
        created = self.order(shopper, account["email"], TARPAULIN, 1, {**TARPAULIN_OPTIONS, "standard_size": "custom", "width": 10, "height": 20, "measurement_unit": "ft"},
                             notes="Stage backdrop")
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertIsNone(body["total_amount"])
        self.assertFalse(body["fixed_price"])
        specs = {spec["label"]: spec["value"] for spec in body["customization"]["specifications"]}
        self.assertEqual(specs["Standard size"], "Custom size (request a quote)")
        self.assertEqual(specs["Custom width"], "10 ft")
        # The administrator quotes it through the normal quotation workflow.
        admin = self.admin()
        reference = body["reference"]
        review = admin.post(f"/api/admin/orders/{reference}/status", json={"status": "Under Review", "expected_version": self.version(admin, reference)})
        self.assertEqual(review.status_code, 200, review.text)
        quoted = admin.post(f"/api/admin/orders/{reference}/quotation", json={"total_amount": 2500, "expected_version": review.json()["version"]})
        self.assertEqual(quoted.status_code, 200, quoted.text)
        self.assertEqual(quoted.json()["status"], "Quotation Sent")

    def test_standard_size_order_is_a_fixed_price_order(self) -> None:
        shopper, account = self.customer()
        options = {**TARPAULIN_OPTIONS, "standard_size": "tarpaulin-3x5-ft", "width": 99}
        response = self.order(shopper, account["email"], TARPAULIN, 4, options)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertTrue(body["fixed_price"])
        self.assertEqual(body["price_basis"], "standard_size")
        self.assertEqual(body["fixed_price_label"], "Price-list total")
        self.assertEqual(body["total_amount"], 1400.0)
        self.assertEqual(body["price_list"], {"size_id": "tarpaulin-3x5-ft", "size": "3 × 5 ft", "unit_price": 350.0, "quantity": 4, "computed_total": 1400.0})
        self.assertNotIn("width", body["customization"]["options"], "Dimensions are only kept for a custom size.")
        self.assertEqual(body["payment"]["down_payment_required"], 700.0)
        admin = self.admin()
        reference = body["reference"]
        quote = admin.post(f"/api/admin/orders/{reference}/quotation", json={"total_amount": 10, "expected_version": self.version(admin, reference)})
        self.assertEqual(quote.status_code, 409, "A published price is approved, not re-quoted.")
        details = admin.get(f"/api/admin/orders/{reference}").json()
        self.assertTrue(details["actions"]["confirm_package"])
        self.assertEqual(details["summary"]["standard_size"], "3 × 5 ft")
        approved = admin.post(f"/api/admin/orders/{reference}/confirm", json={"payment_instructions": "GCash 0917", "expected_version": details["version"]})
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["status"], "Awaiting Down Payment")
        self.assertIn("standard-size order", approved.json()["history"][-1]["text"])

    def test_admin_manages_sizes_without_changing_existing_orders(self) -> None:
        admin = self.admin()
        self.assertEqual(TestClient(app).get("/api/admin/catalog/sizes").status_code, 401)
        shopper, account = self.customer()
        before = self.order(shopper, account["email"], TARPAULIN, 2, {**TARPAULIN_OPTIONS, "standard_size": "tarpaulin-2x4-ft"})
        self.assertEqual(before.status_code, 201, before.text)

        changed = admin.patch("/api/admin/catalog/sizes/tarpaulin-2x4-ft", json={"price": 275})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.addCleanup(admin.patch, "/api/admin/catalog/sizes/tarpaulin-2x4-ft", json={"price": 250, "active": True})
        after = self.order(shopper, account["email"], TARPAULIN, 2, {**TARPAULIN_OPTIONS, "standard_size": "tarpaulin-2x4-ft"})
        self.assertEqual(after.json()["total_amount"], 550.0)
        old = admin.get(f"/api/admin/orders/{before.json()['reference']}").json()
        self.assertEqual(old["total_amount"], 500.0)
        self.assertEqual(old["price_list"]["unit_price"], 250.0)

        hidden = admin.patch("/api/admin/catalog/sizes/tarpaulin-2x4-ft", json={"active": False})
        self.assertEqual(hidden.status_code, 200)
        product = self.catalog_product(TARPAULIN)
        self.assertNotIn("tarpaulin-2x4-ft", [size["id"] for size in product["standard_sizes"]])
        refused = self.order(shopper, account["email"], TARPAULIN, 1, {**TARPAULIN_OPTIONS, "standard_size": "tarpaulin-2x4-ft"})
        self.assertEqual(refused.status_code, 422)

        created = admin.post("/api/admin/catalog/sizes", json={"category_id": "large-format", "width": 5, "height": 10, "unit": "ft", "price": 1200})
        self.assertEqual(created.status_code, 201, created.text)
        size = created.json()["size"]
        self.addCleanup(admin.delete, f"/api/admin/catalog/sizes/{size['id']}")
        self.assertEqual(size["label"], "5 × 10 ft")
        quote = self.client.post("/api/catalog/quote", json={"product_id": TARPAULIN, "quantity": 2, "options": {**TARPAULIN_OPTIONS, "standard_size": size["id"]}}).json()
        self.assertEqual(quote["estimated_total"], 2400.0)
        duplicate = admin.post("/api/admin/catalog/sizes", json={"category_id": "large-format", "width": 5, "height": 10, "unit": "ft", "price": 1})
        self.assertEqual(duplicate.status_code, 422)
        for bad in ({"price": 0}, {"width": -1}, {"unit": "yards"}, {"category_id": "nope"}):
            response = admin.post("/api/admin/catalog/sizes", json={"category_id": "large-format", "width": 6, "height": 12, "unit": "ft", "price": 100, **bad})
            self.assertEqual(response.status_code, 422, bad)

    def test_client_prices_are_ignored(self) -> None:
        shopper, account = self.customer()
        payload_options = {**TARPAULIN_OPTIONS, "standard_size": "tarpaulin-2x3-ft", "price": 1, "unit_price": 1}
        response = self.order(shopper, account["email"], TARPAULIN, 1, payload_options)
        self.assertEqual(response.json()["total_amount"], 200.0)

    def test_migration_moves_area_priced_tarpaulins_to_the_price_list(self) -> None:
        with connection_scope() as connection:
            connection.execute("UPDATE catalog_products SET pricing_mode = 'area' WHERE id = 'welcome-banners'")
            connection.execute("UPDATE catalog_fields SET depends_on = '', depends_values = '[]', label = 'Width' WHERE product_id = 'welcome-banners' AND field_key = 'width'")
            connection.execute("DELETE FROM app_meta WHERE key = ?", (catalog_repository.TARPAULIN_SIZE_MARKER,))
        catalog_repository.initialize()
        product = catalog_services.admin_product("welcome-banners")
        self.assertEqual(product["pricing_mode"], "size")
        width = next(field for field in product["fields"] if field["field_key"] == "width")
        self.assertEqual((width["depends_on"], width["label"]), ("standard_size", "Custom width"))


# ================================================================== uploads


class UploadLimitTests(ServicesTestCase):
    def test_limits_are_per_product(self) -> None:
        self.assertEqual(self.catalog_product(TARPAULIN)["upload_limit_bytes"], 25 * MB)
        self.assertEqual(self.catalog_product("white-mug")["upload_limit_bytes"], 5 * MB)
        self.assertEqual(self.catalog_product("heat-press-printing")["upload_limit_bytes"], 5 * MB)

    def test_large_tarpaulin_file_is_uploaded_in_pieces_and_attached(self) -> None:
        shopper, account = self.customer()
        contents = PNG_BYTES + os.urandom(7 * MB)  # larger than the 5 MB limit of other products
        upload_id = self.upload(shopper, TARPAULIN, "design_upload", contents, name="..\\..\\banner.png")
        options = {**TARPAULIN_OPTIONS, "standard_size": "tarpaulin-4x8-ft", "design_preference": "i-will-upload-a-print-ready-design"}
        response = self.order(shopper, account["email"], TARPAULIN, 1, options, field_files={"design_upload": [{"upload_id": upload_id}]})
        self.assertEqual(response.status_code, 201, response.text)
        attachment = response.json()["attachments"][0]
        self.assertEqual(attachment["name"], "banner.png", "Only the plain file name is kept.")
        self.assertEqual(attachment["size"], len(contents))
        reference = response.json()["reference"]
        stored_name = stored_order(reference)["attachments"][0]["filename"]
        self.assertNotIn("banner", stored_name, "Files are stored under a random name.")
        self.assertTrue((file_services.UPLOAD_DIR / stored_name).is_file())
        served = self.admin().get(f"/api/admin/orders/{reference}/files/{attachment['id']}")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.content, contents)
        own = shopper.get(f"/api/orders/{reference}/files/{attachment['id']}")
        self.assertEqual(own.status_code, 200)
        other, _ = self.customer()
        self.assertEqual(other.get(f"/api/orders/{reference}/files/{attachment['id']}").status_code, 404)
        # An upload is attached once.
        again = self.order(shopper, account["email"], TARPAULIN, 1, options, field_files={"design_upload": [{"upload_id": upload_id}]})
        self.assertEqual(again.status_code, 404)

    def test_upload_rules(self) -> None:
        shopper, account = self.customer()
        too_big = shopper.post("/api/uploads", json={"product_id": "white-mug", "field_key": "design_upload", "name": "a.png", "size": 6 * MB, "mime_type": "image/png"})
        self.assertEqual(too_big.status_code, 413)
        self.assertIn("5 MB", too_big.json()["detail"])
        huge = shopper.post("/api/uploads", json={"product_id": TARPAULIN, "field_key": "design_upload", "name": "a.png", "size": 26 * MB})
        self.assertEqual(huge.status_code, 413)
        self.assertIn("25 MB", huge.json()["detail"])
        for name, mime in (("a.exe", ""), ("a.svg", "image/svg+xml"), ("a.png", "application/pdf")):
            response = shopper.post("/api/uploads", json={"product_id": TARPAULIN, "field_key": "design_upload", "name": name, "size": 10, "mime_type": mime})
            self.assertEqual(response.status_code, 422, name)
        unknown_field = shopper.post("/api/uploads", json={"product_id": TARPAULIN, "field_key": "nope", "name": "a.png", "size": 10})
        self.assertEqual(unknown_field.status_code, 422)
        self.assertEqual(TestClient(app).post("/api/uploads", json={"product_id": TARPAULIN, "name": "a.png", "size": 10}).status_code, 401)

        started = shopper.post("/api/uploads", json={"product_id": TARPAULIN, "field_key": "design_upload", "name": "fake.png", "size": 20}).json()
        upload_id = started["upload_id"]
        other, _ = self.customer()
        self.assertEqual(other.put(f"/api/uploads/{upload_id}/chunks/0", content=b"x" * 20).status_code, 404)
        self.assertEqual(shopper.put(f"/api/uploads/{upload_id}/chunks/1", content=b"x" * 20).status_code, 409)
        self.assertEqual(shopper.put(f"/api/uploads/{upload_id}/chunks/0", content=b"x" * 21).status_code, 413)
        self.assertEqual(shopper.post(f"/api/uploads/{upload_id}/complete").status_code, 409, "incomplete")
        self.assertEqual(shopper.put(f"/api/uploads/{upload_id}/chunks/0", content=b"not a png at all....").status_code, 200)
        self.assertEqual(shopper.post(f"/api/uploads/{upload_id}/complete").status_code, 422, "the signature does not match PNG")
        oversized = shopper.post("/api/uploads", json={"product_id": TARPAULIN, "field_key": "design_upload", "name": "big.png", "size": 5 * MB}).json()
        piece = shopper.put(f"/api/uploads/{oversized['upload_id']}/chunks/0", content=b"\x89PNG" + b"0" * (2 * MB))
        self.assertEqual(piece.status_code, 413, "pieces are at most 2 MiB")

    def test_upload_for_another_product_or_field_is_refused(self) -> None:
        shopper, account = self.customer()
        upload_id = self.upload(shopper, TARPAULIN, "layout_photos", PNG_BYTES)
        options = {**TARPAULIN_OPTIONS, "standard_size": "tarpaulin-2x3-ft", "design_preference": "i-will-upload-a-print-ready-design"}
        wrong_field = self.order(shopper, account["email"], TARPAULIN, 1, options, field_files={"design_upload": [{"upload_id": upload_id}]})
        self.assertEqual(wrong_field.status_code, 422)
        other, other_account = self.customer()
        stolen = self.order(other, other_account["email"], TARPAULIN, 1, {**TARPAULIN_OPTIONS, "standard_size": "tarpaulin-2x3-ft"},
                            field_files={"layout_photos": [{"upload_id": upload_id}]})
        self.assertEqual(stolen.status_code, 404)

    def test_inline_files_keep_the_standard_limit_for_other_products(self) -> None:
        shopper, account = self.customer()
        big = "data:image/png;base64," + base64.b64encode(PNG_BYTES + b"0" * (5 * MB)).decode()
        response = self.order(shopper, account["email"], "photo-mounting-boards", 1, {
            "board_size": "a4-8-3-x-11-7-in", "thickness": "3-mm", "orientation": "portrait",
            "material": "glossy-vinyl-sticker-print", "design_type": "single-photo-full-bleed", "stand_option": "easel-stand",
        }, field_files={"photo_upload": [{"name": "big.png", "dataUrl": big}]})
        self.assertEqual(response.status_code, 413)
        self.assertIn("5 MB", response.json()["detail"])

    def test_database_storage_keeps_files_in_the_database(self) -> None:
        shopper, account = self.customer()
        contents = PNG_BYTES + os.urandom(3 * MB)
        with mock.patch.object(file_services, "_database_storage", return_value=True):
            upload_id = self.upload(shopper, TARPAULIN, "design_upload", contents)
            options = {**TARPAULIN_OPTIONS, "standard_size": "tarpaulin-2x3-ft", "design_preference": "i-will-upload-a-print-ready-design"}
            response = self.order(shopper, account["email"], TARPAULIN, 1, options, field_files={"design_upload": [{"upload_id": upload_id}]})
            self.assertEqual(response.status_code, 201, response.text)
            attachment = response.json()["attachments"][0]
            stored_name = stored_order(response.json()["reference"])["attachments"][0]["filename"]
            self.assertFalse((file_services.UPLOAD_DIR / stored_name).exists())
            self.assertIsNotNone(file_repository.stored_file(stored_name))
            served = self.admin().get(f"/api/admin/orders/{response.json()['reference']}/files/{attachment['id']}")
            self.assertEqual(served.status_code, 200)
            self.assertEqual(served.content, contents)
            self.assertIsNone(file_repository.get_session(upload_id))


# ================================================================== T-shirt printing


class TshirtPrintingTests(ServicesTestCase):
    def test_category_has_two_separate_services(self) -> None:
        catalog = self.client.get("/api/catalog").json()
        tshirt = next(category for category in catalog["categories"] if category["id"] == "tshirt-printing")
        self.assertEqual(tshirt["label"], "T-Shirt Printing")
        self.assertEqual([product["id"] for product in tshirt["products"]], ["heat-press-printing", "sublimation-printing"])
        heat, sublimation = tshirt["products"]
        heat_keys = {field["field_key"] for field in heat["fields"]}
        sub_keys = {field["field_key"] for field in sublimation["fields"]}
        self.assertTrue({"tshirt_type", "tshirt_color", "tshirt_size", "print_placement", "design_upload", "reference_image", "special_requests"} <= heat_keys)
        self.assertTrue({"tshirt_type", "tshirt_size", "sublimation_type", "design_upload", "reference_image"} <= sub_keys)
        self.assertNotIn("tshirt_color", sub_keys)
        placement = next(field for field in heat["fields"] if field["field_key"] == "print_placement")
        self.assertEqual([option["label"] for option in placement["options"]], ["Front", "Back", "Front and Back"])
        self.assertIsNone(heat["base_price"], "No invented prices: the shop enters them.")

    def test_independent_pricing(self) -> None:
        heat = catalog_services.admin_product("heat-press-printing")
        sub = catalog_services.admin_product("sublimation-printing")
        option = lambda product, field_key, value: next(  # noqa: E731
            item for field in product["fields"] if field["field_key"] == field_key for item in field["options"] if item["value"] == value
        )
        catalog_services.update_pricing("heat-press-printing", {"base_price": 250, "options": [
            {"id": option(heat, "tshirt_size", "2xl")["id"], "price_adjustment": 30, "price_type": "per_unit"},
            {"id": option(heat, "print_placement", "front-and-back")["id"], "price_adjustment": 80, "price_type": "per_unit"},
        ]})
        catalog_services.update_pricing("sublimation-printing", {"base_price": 400, "options": [
            {"id": option(sub, "sublimation_type", "full-sublimation-all-over-print")["id"], "price_adjustment": 50, "price_type": "per_unit"},
        ]})
        self.addCleanup(catalog_services.update_pricing, "heat-press-printing", {"base_price": None})
        self.addCleanup(catalog_services.update_pricing, "sublimation-printing", {"base_price": None})
        heat_quote = self.client.post("/api/catalog/quote", json={"product_id": "heat-press-printing", "quantity": 10, "options": HEAT_PRESS_OPTIONS}).json()
        self.assertEqual(heat_quote["unit_price"], 360.0)
        self.assertEqual(heat_quote["estimated_total"], 3600.0)
        sub_quote = self.client.post("/api/catalog/quote", json={"product_id": "sublimation-printing", "quantity": 10, "options": SUBLIMATION_OPTIONS}).json()
        self.assertEqual(sub_quote["unit_price"], 450.0)
        self.assertEqual(sub_quote["estimated_total"], 4500.0)
        # Heat Press option prices never leak into Sublimation (and vice versa).
        partial = self.client.post("/api/catalog/quote", json={"product_id": "sublimation-printing", "quantity": 10, "options": {
            **SUBLIMATION_OPTIONS, "sublimation_type": "partial-sublimation-print-area-only", "print_placement": "front-and-back"}}).json()
        self.assertEqual(partial["unit_price"], 400.0)

        shopper, account = self.customer()
        response = self.order(shopper, account["email"], "heat-press-printing", 10, {**HEAT_PRESS_OPTIONS, "special_requests": "Pack per size"},
                              field_files={"design_upload": [{"name": "logo.png", "dataUrl": PNG}]})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertIsNone(body["total_amount"], "T-shirt orders use the estimate, then the administrator's quotation.")
        self.assertEqual(body["pricing"]["estimated_total"], 3600.0)
        specs = {spec["label"]: spec["value"] for spec in body["customization"]["specifications"]}
        self.assertEqual(specs["T-shirt color"], "Black")
        self.assertEqual(specs["Print placement"], "Front and Back")
        self.assertEqual(specs["Your design"], "logo.png")

    def test_conditional_questions(self) -> None:
        shopper, account = self.customer()
        files = {"design_upload": [{"name": "art.png", "dataUrl": PNG}]}
        mixed = self.order(shopper, account["email"], "sublimation-printing", 12, {**SUBLIMATION_OPTIONS, "tshirt_size": "mixed-sizes-list-below"}, field_files=files)
        self.assertEqual(mixed.status_code, 422)
        self.assertIn("Size breakdown", mixed.json()["detail"])
        no_design = self.order(shopper, account["email"], "sublimation-printing", 12, SUBLIMATION_OPTIONS)
        self.assertEqual(no_design.status_code, 422)
        full = self.order(shopper, account["email"], "sublimation-printing", 12, {**SUBLIMATION_OPTIONS, "print_placement": "back"}, field_files=files)
        self.assertEqual(full.status_code, 201, full.text)
        self.assertNotIn("print_placement", full.json()["customization"]["options"], "Placement applies to partial sublimation only.")
        other = self.order(shopper, account["email"], "heat-press-printing", 5, {**HEAT_PRESS_OPTIONS, "tshirt_color": "other-color-specify"}, field_files=files)
        self.assertEqual(other.status_code, 422)
        self.assertIn("Preferred color", other.json()["detail"])

    def test_design_template_order(self) -> None:
        admin = self.admin()
        created = admin.post("/api/admin/designs", json={
            "name": "Team Jersey Template", "description": "Bold numbers", "style": "Sports",
            "category_id": "tshirt-printing", "product_ids": ["sublimation-printing"],
            "fields": [{"key": "names_list", "required": True}], "image_upload": {"name": "t.png", "dataUrl": PNG},
        })
        self.assertEqual(created.status_code, 201, created.text)
        design_id = created.json()["design_id"]
        self.addCleanup(admin.delete, f"/api/admin/designs/{design_id}")
        product = self.catalog_product("sublimation-printing")
        self.assertIn(design_id, [design["id"] for design in product["designs"]])
        self.assertNotIn(design_id, [design["id"] for design in self.catalog_product("heat-press-printing")["designs"]])
        shopper, account = self.customer()
        response = self.order(shopper, account["email"], "sublimation-printing", 12, SUBLIMATION_OPTIONS,
                              design_id=design_id, design_details={"names_list": "Santos 7\nReyes 10"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["design"]["name"], "Team Jersey Template")

    def test_admin_can_filter_orders_by_category_and_printing_service(self) -> None:
        shopper, account = self.customer()
        files = {"design_upload": [{"name": "art.png", "dataUrl": PNG}]}
        heat = self.order(shopper, account["email"], "heat-press-printing", 5, HEAT_PRESS_OPTIONS, field_files=files)
        sub = self.order(shopper, account["email"], "sublimation-printing", 5, SUBLIMATION_OPTIONS, field_files=files)
        self.assertEqual((heat.status_code, sub.status_code), (201, 201))
        admin = self.admin()
        by_category = admin.get("/api/admin/orders", params={"service": "tshirt-printing", "page_size": "all"}).json()
        references = {row["reference"] for row in by_category["items"]}
        self.assertTrue({heat.json()["reference"], sub.json()["reference"]} <= references)
        self.assertTrue(all(row["service_id"] == "tshirt-printing" for row in by_category["items"]))
        by_product = admin.get("/api/admin/orders", params={"product": "heat-press-printing", "page_size": "all"}).json()
        self.assertIn(heat.json()["reference"], {row["reference"] for row in by_product["items"]})
        self.assertNotIn(sub.json()["reference"], {row["reference"] for row in by_product["items"]})
        self.assertIn("heat-press-printing", {item["id"] for item in by_product["products"]})

    def test_migration_adds_the_category_to_an_existing_catalog_once(self) -> None:
        catalog_repository.initialize()
        catalog_repository.initialize()
        with connection_scope() as connection:
            count = connection.execute("SELECT COUNT(*) FROM catalog_categories WHERE id = 'tshirt-printing'").fetchone()[0]
            products = connection.execute("SELECT COUNT(*) FROM catalog_products WHERE category_id = 'tshirt-printing'").fetchone()[0]
        self.assertEqual((count, products), (1, 2))


class PagesTests(ServicesTestCase):
    def test_pages_and_assets_are_served(self) -> None:
        for path in ("/order", "/services", "/home", "/assets/admin/size-admin.js", "/frontend/image/tshirt/heat-press.svg", "/frontend/image/tshirt/sublimation.svg"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        page = self.client.get("/home").text
        self.assertIn("/order?service=tshirt-printing", page)


if __name__ == "__main__":
    unittest.main()
