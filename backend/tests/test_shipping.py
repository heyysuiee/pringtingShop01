"""Province-based shipping rates, estimates, saved addresses, and immutable order snapshots.

The PSGC-backed province lookup is the only source of the destination island group. Fees and estimates
come from the administrator's fulfillment settings, are calculated by the server, and are frozen on each
order so later rate changes never rewrite history.
"""
from __future__ import annotations

import copy
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


_test_directory = tempfile.TemporaryDirectory(prefix="impress-shipping-tests-")
os.environ.setdefault("PRINTSHOP_DATABASE_PATH", str(Path(_test_directory.name) / "test.sqlite3"))
os.environ.setdefault("PRINTSHOP_UPLOAD_DIR", str(Path(_test_directory.name) / "uploads"))
os.environ.setdefault("PRINTSHOP_ENV_FILE", "")

from backend.app.services import location_services, report_services  # noqa: E402
from backend.tests import test_order_workflow as workflow  # noqa: E402
from backend.tests.support import stored_order  # noqa: E402


LUZON = ("Nueva Ecija", "City of Cabanatuan", "luzon", 58.0, 3, 5)
VISAYAS = ("Cebu", "City of Cebu", "visayas", 100.0, 5, 10)
MINDANAO = ("Davao Del Sur", "City of Davao", "mindanao", 105.0, 5, 10)
SHIPPING_FIELDS = ("delivery_province", "destination_region", "shipping_fee", "estimated_delivery")


def delivery_for(place: tuple[str, str, str, float, int, int]) -> dict:
    return {**workflow.DELIVERY, "province": place[0], "city": place[1]}


class ShippingTestCase(workflow.WorkflowTestCase):
    def settings(self) -> dict:
        response = self.admin.get("/api/admin/settings/fulfillment")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save_settings(self, settings: dict, expect: int = 200) -> dict:
        response = self.admin.post("/api/admin/settings/fulfillment", json=settings)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def set_fee(self, region: str, fee: float, minimum: int | None = None, maximum: int | None = None) -> dict:
        settings = copy.deepcopy(self.settings())
        for area in settings["areas"]:
            if area["region"] == region:
                area["fee"] = fee
                if minimum is not None:
                    area["estimate_min_days"] = minimum
                if maximum is not None:
                    area["estimate_max_days"] = maximum
        return self.save_settings(settings)

    def quote(self, province: str, city: str = "", expect: int = 200, **params) -> dict:
        response = self.shopper.get("/api/shipping-quote", params={"province": province, "city": city, **params})
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()


class OfficialRateTests(ShippingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.original = self.settings()

    def tearDown(self) -> None:
        self.save_settings(self.original)
        super().tearDown()

    def test_each_island_group_uses_its_official_rate_and_estimate(self) -> None:
        for province, city, region, fee, minimum, maximum in (LUZON, VISAYAS, MINDANAO):
            with self.subTest(province=province):
                quote = self.quote(province, city)
                self.assertEqual(
                    (quote["province"], quote["region"], quote["destination_region"], quote["shipping_fee"], quote["estimate_min_days"], quote["estimate_max_days"]),
                    (province, region, region.capitalize(), fee, minimum, maximum),
                )
                self.assertIn(f"{minimum}–{maximum} days", quote["estimated_delivery"])

                order = self.place_package(fulfillment=delivery_for((province, city, region, fee, minimum, maximum)))
                shipping = order["shipping"]
                self.assertEqual(
                    (shipping["delivery_province"], shipping["region"], shipping["destination_region"], shipping["shipping_fee"], shipping["estimated_delivery"]),
                    (province, region, region.capitalize(), fee, quote["estimated_delivery"]),
                )
                self.assertTrue(shipping["snapshot_recorded"])

    def test_changing_the_province_recalculates_the_fee_and_clears_the_old_area(self) -> None:
        first = self.place_package(fulfillment=delivery_for(LUZON))
        second = self.place_package(fulfillment=delivery_for(VISAYAS))
        self.assertEqual(first["shipping"]["shipping_fee"], 58.0)
        self.assertEqual(second["shipping"]["shipping_fee"], 100.0)
        self.assertEqual(second["shipping"]["delivery_province"], VISAYAS[0])
        self.assertEqual(second["shipping"]["area_id"], "visayas")
        self.assertNotEqual(first["shipping"]["area_id"], second["shipping"]["area_id"])
        # The later address does not inherit the earlier province's area or estimate.
        self.assertNotEqual(first["shipping"]["estimated_delivery"], second["shipping"]["estimated_delivery"])

    def test_saved_address_is_validated_and_prefills_checkout(self) -> None:
        body = {key: value for key, value in delivery_for(VISAYAS).items() if key != "method"}
        response = self.shopper.patch("/api/account/delivery-address", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()["customer"]["saved_address"]
        self.assertEqual((saved["province"], saved["city"], saved["island_group"]), (VISAYAS[0], VISAYAS[1], "visayas"))
        session = self.shopper.get("/api/customer/session").json()["customer"]
        self.assertEqual(session["saved_address"], saved)

        # A stale city is rejected and does not replace the valid saved address.
        rejected = self.shopper.patch("/api/account/delivery-address", json={**body, "city": "City of Cabanatuan"})
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(self.shopper.get("/api/customer/session").json()["customer"]["saved_address"], saved)

        order = self.place_package(fulfillment=delivery_for(VISAYAS))
        self.assertEqual(order["shipping"]["delivery_province"], saved["province"])
        self.assertEqual(order["shipping"]["shipping_fee"], 100.0)

    def test_missing_unknown_and_mismatched_destinations_are_refused(self) -> None:
        self.quote("", VISAYAS[1], expect=422)
        self.quote("Atlantis", "Poseidia", expect=422)
        self.quote(VISAYAS[0], "City of Cabanatuan", expect=422)
        for body in (
            {**delivery_for(VISAYAS), "province": ""},
            {**delivery_for(VISAYAS), "city": "City of Cabanatuan"},
        ):
            response = self.shopper.post(
                "/api/orders", json=self.order_payload(fulfillment=body), headers={"Idempotency-Key": workflow.idempotency_key()}
            )
            self.assertEqual(response.status_code, 422, response.text)

    def test_unavailable_province_lookup_never_guesses_a_region(self) -> None:
        with mock.patch.object(location_services, "dataset", side_effect=OSError("offline")):
            self.quote(VISAYAS[0], VISAYAS[1], expect=503)
            response = self.shopper.post(
                "/api/orders",
                json=self.order_payload(fulfillment=delivery_for(VISAYAS)),
                headers={"Idempotency-Key": workflow.idempotency_key()},
            )
            self.assertEqual(response.status_code, 503, response.text)


class ShippingTotalsTests(ShippingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.original = self.settings()

    def tearDown(self) -> None:
        self.save_settings(self.original)
        super().tearDown()

    def test_fixed_price_total_subtotal_and_payments_include_one_fee(self) -> None:
        order = self.place_package(fulfillment=delivery_for(VISAYAS))
        payment = order["payment"]
        self.assertEqual((payment["order_subtotal"], payment["shipping_fee"], payment["total_amount"]), (375.0, 100.0, 475.0))
        self.assertEqual(order["total_amount"], 475.0)
        self.assertEqual(payment["down_payment_required"], 237.5)
        self.assertEqual(payment["remaining_after_down_payment"], 237.5)
        self.assertEqual(payment["delivery_fee"], 100.0)

    def test_quotation_adds_the_snapped_fee_once(self) -> None:
        order = self.place(fulfillment={**delivery_for(VISAYAS), "delivery_fee": 9999.0, "shipping": {"delivery_fee": 9999.0}})
        self.admin_post(order["reference"], "status", {"status": "Under Review"})
        quoted = self.admin_post(order["reference"], "quotation", {
            "total_amount": 2000,
            "delivery_fee": 9999.0,
            "shipping": {"delivery_fee": 9999.0},
            "payment_instructions": "GCash 0917 000 0000",
        })
        self.assertEqual((quoted["payment"]["order_subtotal"], quoted["payment"]["shipping_fee"], quoted["total_amount"]), (2000.0, 100.0, 2100.0))
        self.assertEqual(quoted["quotation"]["delivery_fee"], 100.0)
        self.assertEqual(quoted["payment"]["down_payment_required"], 1050.0)
        self.assertEqual(self.accept(quoted)["payment"]["remaining_after_down_payment"], 1050.0)

    def test_rate_changes_apply_only_to_new_orders(self) -> None:
        old = self.place_package(fulfillment=delivery_for(MINDANAO))
        self.set_fee("mindanao", 999, 1, 2)
        new = self.place_package(fulfillment=delivery_for(MINDANAO))
        self.assertEqual(old["shipping"]["shipping_fee"], 105.0)
        self.assertEqual(old["total_amount"], 375.0 + 105.0)
        self.assertEqual(new["shipping"]["shipping_fee"], 999.0)
        self.assertEqual(new["total_amount"], 375.0 + 999.0)

        old_row = report_services.order_row(stored_order(old["reference"]), report_services.ServiceIndex())
        new_row = report_services.order_row(stored_order(new["reference"]), report_services.ServiceIndex())
        self.assertEqual((old_row["shipping_fee"], old_row["estimated_delivery"]), (105.0, old["shipping"]["estimated_delivery"]))
        self.assertEqual((new_row["shipping_fee"], new_row["estimated_delivery"]), (999.0, new["shipping"]["estimated_delivery"]))
        self.assertEqual(old_row["order_subtotal"], new_row["order_subtotal"])

    def test_customer_admin_and_report_views_show_the_same_snapshot(self) -> None:
        order = self.place_package(fulfillment=delivery_for(LUZON))
        customer = self.shopper.get(f"/api/orders/{order['reference']}").json()
        admin = self.admin_view(order["reference"])
        row = report_services.order_row(stored_order(order["reference"]), report_services.ServiceIndex())
        for view in (customer, admin):
            for field in SHIPPING_FIELDS:
                self.assertEqual(view["shipping"][field], customer["shipping"][field])
            self.assertEqual(view["shipping"]["shipping_fee"], 58.0)
            self.assertEqual(view["payment"]["order_subtotal"], 375.0)
            self.assertEqual(view["payment"]["total_amount"], 433.0)
        for field in SHIPPING_FIELDS:
            self.assertEqual(row[field], customer["shipping"][field])
        self.assertEqual(row["total_amount"], 433.0)
        self.assertEqual(row["order_subtotal"], 375.0)


class CheckoutAddressWiringTests(unittest.TestCase):
    """Guard the saved-address prefill order that keeps the dependent city select usable."""

    def test_saved_address_fills_province_before_rebuilding_city_options(self) -> None:
        source = (Path(__file__).resolve().parents[2] / "frontend" / "assets" / "order.js").read_text(encoding="utf-8")
        start = source.index("function applySavedAddress")
        end = source.index('    fetch("/api/locations"', start)
        body = source[start:end]
        fill_province = body.index("deliveryInputs.forEach")
        rebuild_cities = body.index("syncProvinceSelection();")
        self.assertLess(fill_province, rebuild_cities, "Fill the saved province before rebuilding the city options.")
        self.assertIn("savedCity", body, "Restore the saved city from the freshly rebuilt options.")


if __name__ == "__main__":
    unittest.main()
