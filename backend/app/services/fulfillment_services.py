"""Production monitoring, quality checking, packaging, store pickup, and courier delivery.

Workflow after the customer approves the draft (every step is checked here, on the server):

    Approved for Printing → In Production → Quality Checking → Packaging
      → Awaiting Full Payment (skipped when already fully paid)
      → Store pickup: Ready for Pickup → Order Claimed → Completed
      → Delivery:     Ready for Dispatch → Out for Delivery → Delivered → Completed
                      (Out for Delivery → Delivery Failed → rescheduled → Ready for Dispatch → ...)

* Printing needs the verified 50% down payment and the customer's approved draft (order_services).
* Packaging needs a passed quality check; a failed check sends the order back to In Production for rework.
* Nothing is released (pickup or dispatch) before the order is fully paid.
* Pickup needs the claimant's name, whether they are the customer or an authorized person, the pickup code
  the customer received, and an acknowledgment. An order can be claimed once.
* Courier tracking is entered by the administrator. No live J&T Express API is integrated: the tracking
  number is shown to the customer with the courier's tracking page link when one is configured.
* Every delivery attempt is kept. A failed delivery never cancels or refunds the order by itself, and an
  estimated delivery date never marks an order delivered.
"""
from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import date
from typing import Any

from fastapi import HTTPException

from backend.app.models.order import FULFILLMENT_METHODS, OrderRecord
from backend.app.repositories import order_repository
from backend.app.services import file_services, notification_services, order_services, shop_settings_services
from backend.app.utils.clock import now_iso


ADMIN = order_services.ADMIN
CUSTOMER = order_services.CUSTOMER
MAX_PHOTOS = 4
QUALITY_CHECKS = (
    ("product_quality", "Product quality"),
    ("print_quality", "Print quality"),
    ("color_accuracy", "Color accuracy"),
    ("quantity_verified", "Quantity verified"),
    ("material_condition", "Material condition"),
)
FAILURE_REASONS = {
    "recipient_unavailable": "Recipient unavailable",
    "incorrect_address": "Incorrect address",
    "unreachable": "Unable to contact recipient",
    "personnel_issue": "Delivery personnel issue",
    "weather_transport": "Weather or transportation issue",
    "other": "Other",
}
ISSUE_TYPES = {
    "not_received": "Order not received",
    "damaged": "Damaged on arrival",
    "wrong_item": "Wrong or incomplete item",
    "late": "Late delivery",
    "other": "Other",
}
CLAIMANT_TYPES = {"customer": "Customer", "authorized": "Authorized representative"}
ISSUE_STATUSES = {"Out for Delivery", "Delivery Failed", "Delivered", "Completed"}

_text = order_services._text
_audit = order_services._audit
_save = order_services._save
_admin_order = order_services._admin_order


# ================================================================== small helpers


def _date(value: Any, label: str, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise HTTPException(status_code=422, detail=f"{label} is required.")
        return None
    text = _text(value, label, 10)
    try:
        date.fromisoformat(text)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"Enter a valid {label.lower()} (YYYY-MM-DD).") from error
    return text


def _quantity(value: Any, label: str, maximum: int | None = None) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 1_000_000:
        raise HTTPException(status_code=422, detail=f"{label} must be a whole number of pieces.")
    if maximum is not None and value > maximum:
        raise HTTPException(status_code=422, detail=f"{label} cannot be more than {maximum:,}.")
    return value


def _photos(value: Any, label: str, required: bool = False) -> list[dict[str, Any]]:
    """Validated image/PDF files stored with the order (same type and signature checks as every upload)."""
    if value in (None, ""):
        value = []
    if not isinstance(value, list) or len(value) > MAX_PHOTOS:
        raise HTTPException(status_code=422, detail=f"Attach up to {MAX_PHOTOS} {label.lower()} files.")
    if required and not value:
        raise HTTPException(status_code=422, detail=f"Attach the {label.lower()}.")
    decoded = [order_services._decode_upload(item) for item in value]
    saved = []
    for metadata, contents in decoded:
        order_services.file_services.save(metadata, contents)  # type: ignore[arg-type]
        saved.append(metadata)
    return saved


def _discard(files: list[dict[str, Any]]) -> None:
    for item in files:
        order_services.file_services.delete(item["filename"])


def _save_with_files(order: OrderRecord, files: list[dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return _save(order, *args, **kwargs)
    except HTTPException:
        _discard(files)
        raise


def _ordered_quantity(order: Mapping[str, Any]) -> int:
    quantity = (order.get("customization") or {}).get("quantity")
    return quantity if isinstance(quantity, int) and quantity > 0 else 1


def _cycle_quantity(order: Mapping[str, Any], record: Mapping[str, Any]) -> int:
    """Pieces to produce in the current production cycle: the reprinted quantity during a reprint."""
    if record.get("reprint_id") and isinstance(record.get("ordered_quantity"), int) and record["ordered_quantity"] > 0:
        return record["ordered_quantity"]
    return _ordered_quantity(order)


def _require_status(order: Mapping[str, Any], *statuses: str) -> None:
    if order["status"] not in statuses:
        raise HTTPException(status_code=409, detail=f"This action is not available while the order is {order['status']}.")


def _require_paid(order: Mapping[str, Any], what: str) -> None:
    pending_excess = order_services.pending_excess_amount(order)
    if pending_excess > order_services.MONEY_TOLERANCE:
        raise HTTPException(
            status_code=409,
            detail=f"Resolve the flagged excess of PHP {pending_excess:,.2f} before {what}. Unreviewed money is never treated as paid.",
        )
    if not order_services.is_fully_paid(order):
        breakdown = order_services.payment_breakdown(order)
        raise HTTPException(
            status_code=409,
            detail=f"Verify the remaining balance of PHP {float(breakdown['balance'] or 0):,.2f} before {what}. Unpaid orders are never released.",
        )


def _file_view(item: dict[str, Any] | None, url) -> dict[str, Any] | None:
    if not item:
        return None
    return {"id": item["id"], "name": item["name"], "mime_type": item["mime_type"], "size": item["size"], "url": url(item["id"])}


def _url_for(order: Mapping[str, Any], admin: bool):
    prefix = f"/api/admin/orders/{order['reference']}/files" if admin else f"/api/orders/{order['reference']}/files"
    return lambda file_id: f"{prefix}/{file_id}"


# ================================================================== views used by order_view


def draft_history(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every draft sent to the customer. Orders from before the history existed show their one draft."""
    stored = order.get("drafts")
    drafts = order_services.json_copy(stored) if isinstance(stored, list) else []
    if not drafts and order.get("draft_attachment"):
        draft = order["draft_attachment"]
        waiting = order["status"] == "Awaiting Customer Approval"
        drafts = [{
            "id": draft["id"], "file": draft, "remarks": "", "sent_at": None, "sent_by": "",
            "status": "pending" if waiting else ("approved" if order["status"] not in {"Draft Preparation", "Confirmed"} else "revision_requested"),
            "responded_at": None, "revision_note": "", "number": 1, "legacy": True,
        }]
    return drafts


DRAFT_LABELS = {
    "pending": "Waiting for your approval",
    "approved": "Approved",
    "revision_requested": "Revision requested",
    "superseded": "Replaced by a newer draft",
}


def drafts_view(order: Mapping[str, Any], url, admin: bool) -> list[dict[str, Any]]:
    return [
        {
            "id": item.get("id"),
            "number": item.get("number", index),
            "file": _file_view(item.get("file"), url),
            "remarks": item.get("remarks", ""),
            "sent_at": item.get("sent_at"),
            "status": item.get("status", ""),
            "status_label": DRAFT_LABELS.get(item.get("status", ""), item.get("status", "")),
            "responded_at": item.get("responded_at"),
            "revision_note": item.get("revision_note", ""),
            **({"sent_by": item.get("sent_by", "")} if admin else {}),
        }
        for index, item in enumerate(draft_history(order), start=1)
    ]


def production_view(order: Mapping[str, Any], url, admin: bool) -> dict[str, Any]:
    record = order_services.production(order)
    quality = record.get("quality_check") or {}
    packaging = record.get("packaging") or {}
    view = {
        "started_at": record.get("started_at"),
        "estimated_completion": record.get("estimated_completion"),
        "personnel": record.get("personnel", ""),
        "ordered_quantity": record.get("ordered_quantity") or _ordered_quantity(order),
        "printed_quantity": record.get("printed_quantity"),
        "completed_quantity": record.get("completed_quantity"),
        "progress": record.get("progress", 0),
        "remarks": record.get("remarks", ""),
        "photos": [_file_view(item, url) for item in record.get("photos", [])],
        "updates": [
            {key: value for key, value in item.items() if admin or key != "by"}
            for item in record.get("updates", [])
        ],
        "quality_check": {
            **{key: value for key, value in quality.items() if key not in {"photos", "by"} or (admin and key == "by")},
            "photos": [_file_view(item, url) for item in quality.get("photos", [])],
        } if quality else None,
        "quality_history": [
            {key: value for key, value in item.items() if key != "photos" and (admin or key != "by")}
            for item in record.get("quality_history", [])
        ],
        "packaging": {
            **{key: value for key, value in packaging.items() if key not in {"proof", "by"} or (admin and key == "by")},
            "proof": [_file_view(item, url) for item in packaging.get("proof", [])],
        } if packaging else None,
        "cycle": record.get("cycle", 1),
        "reprint": _reprint_view(record.get("reprint"), admin),
        "reprints": [_reprint_view(item, admin) for item in record.get("reprints", [])],
        "previous_cycles": [
            {
                "cycle": item.get("cycle", index),
                "started_at": item.get("started_at"),
                "quality_passed": (item.get("quality_check") or {}).get("passed"),
                "packed_at": (item.get("packaging") or {}).get("completed_at"),
                "completed_quantity": item.get("completed_quantity"),
            }
            for index, item in enumerate(record.get("previous_cycles", []), start=1)
        ],
    }
    return view


def _reprint_view(reprint: Any, admin: bool) -> dict[str, Any] | None:
    if not isinstance(reprint, dict):
        return None
    hidden = set() if admin else {"authorized_by", "completed_by"}
    return {key: value for key, value in reprint.items() if key not in hidden}


def _attempt_view(item: dict[str, Any], url, admin: bool) -> dict[str, Any]:
    view = {key: value for key, value in item.items() if key not in {"proof", "by"}}
    view["proof"] = [_file_view(proof, url) for proof in item.get("proof", [])]
    view["failure_label"] = FAILURE_REASONS.get(item.get("failure_reason", ""), "") if item.get("failure_reason") else ""
    if admin:
        view["by"] = item.get("by", "")
    return view


def fulfillment_details(order: Mapping[str, Any], admin: bool) -> dict[str, Any]:
    """Delivery fee, courier and tracking, delivery attempts, pickup details and confirmation, and issues."""
    stored = order_services.fulfillment(order)
    url = _url_for(order, admin)
    method = order_services.fulfillment_method(order)
    settings = shop_settings_services.fulfillment_settings()
    pickup = stored.get("pickup_confirmation")
    code = stored.get("pickup_code")
    details: dict[str, Any] = {
        "delivery_fee": order_services.delivery_fee(order),
        "area_label": stored.get("area_label", ""),
        "estimate_text": stored.get("estimate_text", ""),
        "estimate_min_days": stored.get("estimate_min_days"),
        "estimate_max_days": stored.get("estimate_max_days"),
        "delivery_disclaimer": settings["delivery_disclaimer"] if method == "delivery" else "",
        "pickup_location": stored.get("pickup_location") or (None if method == "delivery" else _default_location(settings)),
        "preferred_pickup_date": stored.get("preferred_pickup_date"),
        "pickup_notes": stored.get("pickup_notes", ""),
        "pickup_schedule": stored.get("pickup_schedule"),
        "pickup_instructions": settings["pickup_instructions"] if method == "pickup" else "",
        "courier": stored.get("courier", ""),
        "courier_service": stored.get("courier_service", ""),
        "tracking_number": stored.get("tracking_number", ""),
        "tracking_url": stored.get("tracking_url", ""),
        "shipment_date": stored.get("shipment_date"),
        "scheduled_date": stored.get("scheduled_date"),
        "delivery_personnel": stored.get("delivery_personnel", ""),
        "dispatched_at": stored.get("dispatched_at"),
        "delivered_at": stored.get("delivered_at"),
        "released_at": stored.get("released_at"),
        "attempts": [_attempt_view(item, url, admin) for item in stored.get("attempts", [])],
        "previous_cycles": [
            {
                "cycle": cycle.get("cycle"),
                "archived_at": cycle.get("archived_at"),
                "delivered_at": cycle.get("delivered_at"),
                "pickup_confirmation": {key: value for key, value in (cycle.get("pickup_confirmation") or {}).items() if admin or key != "by"} or None,
                "attempts": [_attempt_view(item, url, admin) for item in cycle.get("attempts", [])],
            }
            for cycle in stored.get("previous_cycles", [])
        ],
        "pickup_confirmation": {key: value for key, value in pickup.items() if admin or key != "by"} if pickup else None,
        "issues": [
            {
                **{key: value for key, value in item.items() if key != "by" and (admin or key != "resolved_by")},
                "type_label": ISSUE_TYPES.get(item.get("type", ""), item.get("type", "")),
            }
            for item in stored.get("issues", [])
        ],
    }
    if admin:
        # The administrator never sees the code in advance: the claimant shows it at the counter.
        details["pickup_code_set"] = bool(code)
    elif code and order["status"] == "Ready for Pickup":
        details["pickup_code"] = code
    return details


def _default_location(settings: dict[str, Any]) -> dict[str, Any] | None:
    active = [item for item in settings["pickup_locations"] if item["active"]]
    return {key: active[0][key] for key in ("id", "name", "address", "hours", "instructions")} if len(active) == 1 else None


def _production_files(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = list(record.get("photos", []))
    for entry in [record.get("quality_check") or {}, *record.get("quality_history", [])]:
        items += entry.get("photos", [])
    items += (record.get("packaging") or {}).get("proof", [])
    return items


def files(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    record = order_services.production(order)
    stored = order_services.fulfillment(order)
    # Earlier production and fulfillment cycles (before a reprint) keep their photos and proofs.
    items = _production_files(record)
    for cycle in record.get("previous_cycles", []):
        items += _production_files(cycle)
    for attempt in stored.get("attempts", []) + [item for cycle in stored.get("previous_cycles", []) for item in cycle.get("attempts", [])]:
        items += attempt.get("proof", [])
    for draft in draft_history(order):
        if draft.get("file"):
            items.append(draft["file"])
    return items


def can_report_issue(order: Mapping[str, Any]) -> bool:
    return order_services.fulfillment_method(order) == "delivery" and order["status"] in ISSUE_STATUSES


# ================================================================== production


@file_services.compensating_files
def update_production(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Record production progress: schedule, personnel, printed/completed quantities, remarks, photos."""
    order = _admin_order(reference)
    _require_status(order, "Approved for Printing", "In Production", "Quality Checking", "Packaging")
    order_services.ensure_no_pending_cancellation(order)
    record = order_services.production(order)
    # A reprint cycle produces only the reprinted quantity.
    ordered = _cycle_quantity(order, record)
    changes: dict[str, Any] = {}
    estimated = _date(payload.get("estimated_completion"), "Estimated completion date")
    if estimated is not None:
        changes["estimated_completion"] = estimated
    if payload.get("personnel") not in (None, ""):
        changes["personnel"] = _text(payload.get("personnel"), "Production personnel", 120)
    printed = _quantity(payload.get("printed_quantity"), "Printed quantity")
    completed = _quantity(payload.get("completed_quantity"), "Completed quantity")
    if printed is not None:
        changes["printed_quantity"] = printed
    if completed is not None:
        limit = printed if printed is not None else record.get("printed_quantity")
        if isinstance(limit, int) and completed > limit:
            raise HTTPException(status_code=422, detail="Completed quantity cannot be more than the printed quantity.")
        if completed > ordered:
            raise HTTPException(status_code=422, detail=f"Completed quantity cannot be more than the ordered {ordered:,} pcs.")
        changes["completed_quantity"] = completed
    progress = payload.get("progress")
    if progress not in (None, ""):
        if isinstance(progress, bool) or not isinstance(progress, int) or not 0 <= progress <= 100:
            raise HTTPException(status_code=422, detail="Progress must be a whole percentage from 0 to 100.")
        changes["progress"] = progress
    remarks = _text(payload.get("remarks", "") or "", "Production remarks", 1000)
    if remarks:
        changes["remarks"] = remarks
    photos = _photos(payload.get("photos"), "Production photos")
    if not changes and not photos:
        raise HTTPException(status_code=422, detail="Enter at least one production detail to save.")
    now = now_iso()
    record.update(changes)
    record.setdefault("ordered_quantity", ordered)
    record["photos"] = record.get("photos", []) + photos
    record.setdefault("updates", []).append({
        "at": now, "by": actor, **changes, "photo_count": len(photos),
    })
    summary = "; ".join(f"{key.replace('_', ' ')}: {value}" for key, value in changes.items())
    return _save_with_files(
        order, photos,
        {"production": record},
        f"Administrator updated production ({summary or 'photos added'})." + (f" {len(photos)} photo(s) attached." if photos else ""),
        payload.get("expected_version"),
        admin=True,
        audit=_audit("production.updated", ADMIN, actor, **changes, photos=len(photos)),
    )


@file_services.compensating_files
def record_quality_check(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Quality checklist. Passed: the order moves to Packaging. Failed: back to In Production for rework."""
    order = _admin_order(reference)
    _require_status(order, "Quality Checking")
    order_services.ensure_no_pending_cancellation(order)
    checks = payload.get("checks")
    if not isinstance(checks, dict):
        raise HTTPException(status_code=422, detail="Complete the quality checklist.")
    results: dict[str, bool] = {}
    for key, label in QUALITY_CHECKS:
        value = checks.get(key)
        if not isinstance(value, bool):
            raise HTTPException(status_code=422, detail=f"Quality checklist: mark “{label}” as passed or failed.")
        results[key] = value
    passed = all(results.values())
    defects = _text(payload.get("defects", "") or "", "Defects or issues", 1000)
    corrective = _text(payload.get("corrective_actions", "") or "", "Corrective actions", 1000)
    remarks = _text(payload.get("remarks", "") or "", "Inspection remarks", 1000)
    inspected = _quantity(payload.get("inspected_quantity"), "Inspected quantity")
    if not passed and (not defects or not corrective):
        raise HTTPException(status_code=422, detail="Describe the defects and the corrective action for a failed quality check.")
    photos = _photos(payload.get("photos"), "Quality check photos")
    now = now_iso()
    record = order_services.production(order)
    entry = {
        "at": now, "by": actor, "checks": results, "passed": passed, "defects": defects,
        "corrective_actions": corrective, "remarks": remarks, "inspected_quantity": inspected, "photos": photos,
    }
    if record.get("quality_check"):
        record.setdefault("quality_history", []).append(record["quality_check"])
    record["quality_check"] = entry
    failed = [label for key, label in QUALITY_CHECKS if not results[key]]
    notifications = []
    if passed:
        record["packaging"] = {"started_at": now}
        status = "Packaging"
        event = "Administrator recorded a passed quality check. The order moved to Packaging."
        notifications.append(notification_services.build(
            order, "quality_checked", f"quality:{now}", "Quality check completed",
            f"Your order {order['reference']} passed the shop's quality check and is being packed.",
        ))
    else:
        status = "In Production"
        event = f"Quality check failed ({', '.join(failed)}). Defects: {defects}. Corrective action: {corrective}. The order returned to In Production for rework."
    if remarks:
        event += f" Remarks: {remarks}"
    return _save_with_files(
        order, photos,
        {"production": record, "status": status},
        event,
        payload.get("expected_version"),
        admin=True,
        audit=_audit("quality.checked", ADMIN, actor, passed=passed, failed_checks=failed, defects=defects, corrective_actions=corrective),
        notifications=notifications,
    )


@file_services.compensating_files
def complete_packaging(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Finish packing. A fully paid order is released for pickup or dispatch; otherwise it waits for the balance."""
    order = _admin_order(reference)
    _require_status(order, "Packaging")
    order_services.ensure_no_pending_cancellation(order)
    record = order_services.production(order)
    if not (record.get("quality_check") or {}).get("passed"):
        raise HTTPException(status_code=409, detail="Packaging needs a passed quality check.")
    ordered = _cycle_quantity(order, record)
    quantity = _quantity(payload.get("completed_quantity"), "Packed quantity", ordered)
    if not quantity:
        raise HTTPException(status_code=422, detail="Enter the number of pieces packed.")
    remarks = _text(payload.get("remarks", "") or "", "Packaging remarks", 1000)
    proof = _photos(payload.get("proof"), "Packaging proof")
    now = now_iso()
    record["packaging"] = {
        **(record.get("packaging") or {}), "completed_at": now, "completed_quantity": quantity, "remarks": remarks,
        "proof": proof, "by": actor,
    }
    record.setdefault("completed_quantity", quantity)
    updates: dict[str, Any] = {"production": record}
    notifications = [notification_services.build(
        order, "packaging_completed", "packaging-completed", "Packaging completed",
        f"Your order {order['reference']} ({quantity:,} pcs) is packed.",
    )]
    if order_services.is_fully_paid(order):
        status, released, notice = order_services.release_updates(order)
        updates.update({"status": status, "fulfillment": released})
        notifications.append(notice)
        event = f"Administrator completed packaging ({quantity:,} pcs). The order is fully paid and now {status}."
    else:
        balance = float(order_services.payment_breakdown(order)["balance"] or 0)
        updates["status"] = "Awaiting Full Payment"
        how = "pickup" if order_services.fulfillment_method(order) == "pickup" else "dispatch"
        notifications.append(notification_services.build(
            order, "balance_due", "balance-due", "Remaining balance due",
            f"Your order {order['reference']} is packed. Pay the remaining balance of PHP {balance:,.2f} in your customer "
            f"portal so the shop can release it for {how}.",
        ))
        event = f"Administrator completed packaging ({quantity:,} pcs). Waiting for the remaining balance of PHP {balance:,.2f} before release."
    if remarks:
        event += f" Remarks: {remarks}"
    return _save_with_files(
        order, proof, updates, event, payload.get("expected_version"), admin=True,
        audit=_audit("packaging.completed", ADMIN, actor, quantity=quantity, proof=len(proof)),
        notifications=notifications,
    )


# ================================================================== delivery


def _tracking_unused(tracking: str, reference: str) -> None:
    """A tracking number belongs to one parcel. Redelivery attempts of the same parcel (same order, same cycle)
    may reuse it; another order or an earlier cycle (before a reprint) may not."""
    key = tracking.replace(" ", "").casefold()
    for other in order_repository.list_orders():
        stored = order_services.fulfillment(other)
        archived = [item for cycle in stored.get("previous_cycles", []) for item in cycle.get("attempts", [])]
        for attempt in archived if other["reference"] == reference else stored.get("attempts", []) + archived:
            if str(attempt.get("tracking_number") or "").replace(" ", "").casefold() == key:
                raise HTTPException(status_code=409, detail=f"Tracking number {tracking} is already recorded on order {other['reference']}.")


def dispatch_order(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Hand a fully paid delivery order to the courier (J&T Express by default) or the shop's own rider."""
    order = _admin_order(reference)
    _require_status(order, "Ready for Dispatch")
    order_services.ensure_no_pending_cancellation(order)
    _require_paid(order, "dispatch")
    settings = shop_settings_services.fulfillment_settings()
    courier = shop_settings_services.courier(payload.get("courier_id"), settings)
    tracking = _text(payload.get("tracking_number", "") or "", "Tracking number", 60)
    personnel = _text(payload.get("delivery_personnel", "") or "", "Delivery personnel", 120)
    if not tracking and not personnel:
        raise HTTPException(status_code=422, detail=f"Enter the {courier['name']} tracking number, or the shop's delivery personnel for an own delivery.")
    if tracking:
        if len(tracking.replace(" ", "")) < 6:
            raise HTTPException(status_code=422, detail="Enter the complete tracking number.")
        _tracking_unused(tracking, reference)
    shipment = _date(payload.get("shipment_date"), "Shipment date") or now_iso()[:10]
    scheduled = _date(payload.get("scheduled_date"), "Scheduled delivery date")
    service = _text(payload.get("service", "") or courier.get("service", ""), "Courier service", 80)
    remarks = _text(payload.get("remarks", "") or "", "Delivery remarks", 1000)
    now = now_iso()
    stored = order_services.fulfillment(order)
    link = shop_settings_services.tracking_link(courier, tracking)
    attempt = {
        "number": len(stored.get("attempts", [])) + 1, "dispatched_at": now, "courier": courier["name"],
        "courier_id": courier["id"], "service": service, "tracking_number": tracking, "tracking_url": link,
        "shipment_date": shipment, "scheduled_date": scheduled, "personnel": personnel, "status": "out_for_delivery",
        "remarks": remarks, "by": actor,
    }
    stored.update({
        "courier": courier["name"], "courier_id": courier["id"], "courier_service": service, "tracking_number": tracking,
        "tracking_url": link, "shipment_date": shipment, "scheduled_date": scheduled, "delivery_personnel": personnel,
        "dispatched_at": now,
    })
    stored["attempts"] = stored.get("attempts", []) + [attempt]
    estimate = stored.get("estimate_text") or ""
    message = f"Your order {order['reference']} is out for delivery with {courier['name']}"
    message += f" (tracking number {tracking})." if tracking else f" (delivered by {personnel})."
    if estimate:
        message += f" Estimated delivery: {estimate}. This is a courier estimate, not a guaranteed arrival date."
    cycle = str(order_services.production(order).get("cycle") or 1)
    return _save(
        order,
        {"status": "Out for Delivery", "fulfillment": stored},
        f"Administrator dispatched the order via {courier['name']}"
        + (f" {service}" if service else "")
        + (f", tracking number {tracking}" if tracking else "")
        + (f", delivery personnel {personnel}" if personnel else "")
        + f" (attempt {attempt['number']}, shipment date {shipment})." + (f" Remarks: {remarks}" if remarks else ""),
        payload.get("expected_version"),
        admin=True,
        audit=_audit("delivery.dispatched", ADMIN, actor, courier=courier["name"], tracking=tracking, attempt=attempt["number"]),
        notifications=[notification_services.build(order, "out_for_delivery", f"dispatch:{attempt['number']}", "Out for delivery", message)],
        claim_ops=[{
            "op": "acquire", "kind": "tracking", "key": tracking.replace(" ", "").casefold(),
            "context": f"cycle:{cycle}", "allow_context": f"cycle:{cycle}",
            "detail": f"Tracking number {tracking} is already recorded on another order or an earlier cycle.",
        }] if tracking else [],
    )


def _current_attempt(stored: dict[str, Any]) -> dict[str, Any] | None:
    attempts = stored.get("attempts", [])
    return attempts[-1] if attempts and attempts[-1].get("status") == "out_for_delivery" else None


@file_services.compensating_files
def confirm_delivery(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    order = _admin_order(reference)
    _require_status(order, "Out for Delivery")
    order_services.ensure_no_pending_cancellation(order)
    received_by = _text(payload.get("received_by", "") or "", "Received by", 120)
    remarks = _text(payload.get("remarks", "") or "", "Delivery remarks", 1000)
    proof = _photos(payload.get("proof"), "Proof of delivery", required=True)
    now = now_iso()
    stored = order_services.fulfillment(order)
    attempt = _current_attempt(stored)
    if attempt is None:
        # Released before delivery attempts were recorded.
        attempt = {"number": len(stored.get("attempts", [])) + 1, "dispatched_at": stored.get("dispatched_at"), "status": "out_for_delivery", "legacy": True}
        stored["attempts"] = stored.get("attempts", []) + [attempt]
    attempt.update({"status": "delivered", "delivered_at": now, "received_by": received_by, "proof": proof, "delivery_remarks": remarks, "confirmed_by": actor})
    stored["delivered_at"] = now
    return _save_with_files(
        order, proof,
        {"status": "Delivered", "fulfillment": stored, **_finish_reprint(order, now, actor)},
        "Administrator confirmed the delivery" + (f" (received by {received_by})" if received_by else "") + " with proof of delivery."
        + (f" Remarks: {remarks}" if remarks else ""),
        payload.get("expected_version"),
        admin=True,
        audit=_audit("delivery.delivered", ADMIN, actor, attempt=attempt["number"], received_by=received_by, proof=len(proof)),
        notifications=[notification_services.build(
            order, "delivered", "delivered", "Order delivered",
            f"Your order {order['reference']} was delivered. If anything is wrong with it, report it in your customer portal.",
        )],
    )


def delivery_failed(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    order = _admin_order(reference)
    _require_status(order, "Out for Delivery")
    reason = payload.get("reason")
    if reason not in FAILURE_REASONS:
        raise HTTPException(status_code=422, detail="Choose the reason the delivery failed.")
    remarks = _text(payload.get("remarks", "") or "", "Remarks", 1000, required=reason == "other")
    now = now_iso()
    stored = order_services.fulfillment(order)
    attempt = _current_attempt(stored)
    if attempt is None:
        attempt = {"number": len(stored.get("attempts", [])) + 1, "dispatched_at": stored.get("dispatched_at"), "status": "out_for_delivery", "legacy": True}
        stored["attempts"] = stored.get("attempts", []) + [attempt]
    attempt.update({"status": "failed", "failed_at": now, "failure_reason": reason, "failure_remarks": remarks, "reported_by": actor})
    label = FAILURE_REASONS[reason]
    return _save(
        order,
        {"status": "Delivery Failed", "fulfillment": stored},
        f"Delivery attempt {attempt['number']} failed: {label}." + (f" {remarks}" if remarks else "") + " The order was not cancelled or refunded.",
        payload.get("expected_version"),
        admin=True,
        audit=_audit("delivery.failed", ADMIN, actor, attempt=attempt["number"], reason=reason, remarks=remarks),
        notifications=[notification_services.build(
            order, "delivery_failed", f"delivery-failed:{attempt['number']}", "Delivery attempt failed",
            f"We could not deliver order {order['reference']} ({label}). The shop will contact you to reschedule"
            + (f": {remarks}" if remarks else ".") ,
        )],
    )


def reschedule_delivery(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    order = _admin_order(reference)
    _require_status(order, "Delivery Failed")
    order_services.ensure_no_pending_cancellation(order)
    scheduled = _date(payload.get("scheduled_date"), "New delivery date", required=True)
    remarks = _text(payload.get("remarks", "") or "", "Remarks", 1000)
    stored = order_services.fulfillment(order)
    stored["scheduled_date"] = scheduled
    stored.setdefault("reschedules", []).append({"at": now_iso(), "by": actor, "scheduled_date": scheduled, "remarks": remarks})
    return _save(
        order,
        {"status": "Ready for Dispatch", "fulfillment": stored},
        f"Administrator rescheduled the delivery for {scheduled}." + (f" Remarks: {remarks}" if remarks else ""),
        payload.get("expected_version"),
        admin=True,
        audit=_audit("delivery.rescheduled", ADMIN, actor, scheduled_date=scheduled, remarks=remarks),
        notifications=[notification_services.build(
            order, "delivery_rescheduled", f"reschedule:{len(stored['reschedules'])}", "Delivery rescheduled",
            f"The delivery of order {order['reference']} was rescheduled for {scheduled}.",
        )],
    )


# ================================================================== pickup


def schedule_pickup(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    order = _admin_order(reference)
    if order_services.fulfillment_method(order) != "pickup":
        raise HTTPException(status_code=409, detail="This order is for delivery.")
    _require_status(order, "Packaging", "Awaiting Full Payment", "Ready for Pickup")
    scheduled = _date(payload.get("pickup_date"), "Pickup date", required=True)
    window = _text(payload.get("pickup_time", "") or "", "Pickup time", 60)
    remarks = _text(payload.get("remarks", "") or "", "Remarks", 500)
    stored = order_services.fulfillment(order)
    stored["pickup_schedule"] = {"date": scheduled, "time": window, "remarks": remarks}
    return _save(
        order,
        {"fulfillment": stored},
        f"Administrator scheduled the pickup for {scheduled}" + (f" {window}" if window else "") + "." + (f" {remarks}" if remarks else ""),
        payload.get("expected_version"),
        admin=True,
        audit=_audit("pickup.scheduled", ADMIN, actor, date=scheduled, time=window),
        notifications=[notification_services.build(
            order, "pickup_scheduled", f"pickup-schedule:{scheduled}:{window}", "Pickup schedule",
            f"Pickup of order {order['reference']} is scheduled for {scheduled}" + (f", {window}" if window else "") + ".",
        )],
    )


def confirm_pickup(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Release a fully paid order to a verified claimant, once."""
    order = _admin_order(reference)
    _require_status(order, "Ready for Pickup")
    order_services.ensure_no_pending_cancellation(order)
    _require_paid(order, "releasing the order")
    stored = order_services.fulfillment(order)
    if stored.get("pickup_confirmation"):
        raise HTTPException(status_code=409, detail="This order was already claimed.")
    claimant = _text(payload.get("claimant_name"), "Claimant name", 120, True)
    claimant_type = payload.get("claimant_type")
    if claimant_type not in CLAIMANT_TYPES:
        raise HTTPException(status_code=422, detail="Choose whether the claimant is the customer or an authorized representative.")
    authorization = _text(payload.get("authorization_details", "") or "", "Authorization details", 300, required=claimant_type == "authorized")
    code = _text(payload.get("verification_code"), "Pickup code", 40, True).replace(" ", "").upper()
    expected = str(stored.get("pickup_code") or order["reference"]).replace(" ", "").upper()
    if not secrets.compare_digest(code, expected):
        raise HTTPException(status_code=422, detail="The pickup code does not match this order. Do not release the order.")
    if payload.get("acknowledged") is not True:
        raise HTTPException(status_code=422, detail="Confirm that the claimant received the order in good condition.")
    remarks = _text(payload.get("remarks", "") or "", "Remarks", 500)
    now = now_iso()
    stored["pickup_confirmation"] = {
        "claimant_name": claimant, "claimant_type": claimant_type, "claimant_type_label": CLAIMANT_TYPES[claimant_type],
        "authorization_details": authorization, "verified_with": "pickup code" if stored.get("pickup_code") else "order reference",
        "claimed_at": now, "acknowledged": True, "remarks": remarks, "by": actor,
    }
    return _save(
        order,
        {"status": "Order Claimed", "fulfillment": stored, **_finish_reprint(order, now, actor)},
        f"Order claimed by {claimant} ({CLAIMANT_TYPES[claimant_type].lower()}), verified with the "
        f"{stored['pickup_confirmation']['verified_with']}." + (f" Authorization: {authorization}." if authorization else "")
        + (f" Remarks: {remarks}" if remarks else ""),
        payload.get("expected_version"),
        admin=True,
        audit=_audit("pickup.claimed", ADMIN, actor, claimant=claimant, claimant_type=claimant_type),
        notifications=[notification_services.build(
            order, "order_claimed", "claimed", "Order claimed",
            f"Your order {order['reference']} was claimed by {claimant} on {now[:10]}.",
        )],
    )


# ================================================================== delivery issues


def report_issue(reference: str, payload: Mapping[str, Any], customer_id: str | None) -> dict[str, Any]:
    order = order_services.customer_owned_order(reference, payload.get("email"), customer_id)
    if not can_report_issue(order):
        raise HTTPException(status_code=409, detail="Delivery issues can be reported once a delivery order is out for delivery.")
    kind = payload.get("type")
    if kind not in ISSUE_TYPES:
        raise HTTPException(status_code=422, detail="Choose the kind of delivery issue.")
    details = _text(payload.get("details"), "Issue details", 1000, True)
    stored = order_services.fulfillment(order)
    if any(item.get("status") == "open" for item in stored.get("issues", [])):
        raise HTTPException(status_code=409, detail="You already reported an issue that the shop is reviewing.")
    issue = {"id": f"DI-{secrets.token_hex(4).upper()}", "type": kind, "details": details, "reported_at": now_iso(), "status": "open"}
    stored["issues"] = stored.get("issues", []) + [issue]
    return _save(
        order,
        {"fulfillment": stored},
        f"Customer reported a delivery issue ({ISSUE_TYPES[kind]}): {details}",
        audit=_audit("delivery.issue_reported", CUSTOMER, customer_id, issue=issue["id"], type=kind),
    )


def resolve_issue(reference: str, issue_id: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    order = _admin_order(reference)
    stored = order_services.fulfillment(order)
    issue = next((item for item in stored.get("issues", []) if item.get("id") == issue_id), None)
    if issue is None:
        raise HTTPException(status_code=404, detail="Delivery issue not found.")
    if issue.get("status") != "open":
        raise HTTPException(status_code=409, detail="This issue was already resolved.")
    resolution = _text(payload.get("resolution"), "Resolution", 1000, True)
    issue.update({"status": "resolved", "resolution": resolution, "resolved_at": now_iso(), "resolved_by": actor})
    return _save(
        order,
        {"fulfillment": stored},
        f"Administrator resolved delivery issue {issue_id}: {resolution}",
        payload.get("expected_version"),
        admin=True,
        audit=_audit("delivery.issue_resolved", ADMIN, actor, issue=issue_id, resolution=resolution),
        notifications=[notification_services.build(
            order, "issue_resolved", f"issue:{issue_id}", "Delivery issue update",
            f"The shop reviewed the delivery issue you reported for order {order['reference']}: {resolution}",
        )],
    )


# ================================================================== admin lists


PRODUCTION_TABS = {
    "drafts": ("Confirmed", "Draft Preparation", "Awaiting Customer Approval"),
    "printing": ("Approved for Printing", "In Production"),
    "quality": ("Quality Checking",),
    "packaging": ("Packaging", "Awaiting Full Payment"),
}
FULFILLMENT_STATUSES = (
    "In Production", "Quality Checking", "Packaging", "Awaiting Full Payment", "Ready for Pickup", "Ready for Dispatch",
    "Out for Delivery", "Delivery Failed", "Delivered", "Order Claimed", "Completed",
)
PICKUP_STATUSES = ("Packaging", "Awaiting Full Payment", "Ready for Pickup", "Order Claimed")
DELIVERY_STATUSES = ("Packaging", "Awaiting Full Payment", "Ready for Dispatch", "Out for Delivery", "Delivered")


def _row(order: OrderRecord, services: Any) -> dict[str, Any]:
    from backend.app.services import report_services

    row = report_services.order_row(order, services)
    record = order_services.production(order)
    stored = order_services.fulfillment(order)
    shipping = order_services.shipping_view(order)
    row.update({
        "version": order_repository.order_version(order),
        "progress": record.get("progress", 0),
        "estimated_completion": record.get("estimated_completion"),
        "personnel": record.get("personnel", ""),
        "printed_quantity": record.get("printed_quantity"),
        "quality_passed": (record.get("quality_check") or {}).get("passed"),
        "packed_at": (record.get("packaging") or {}).get("completed_at"),
        "courier": stored.get("courier", ""),
        "tracking_number": stored.get("tracking_number", ""),
        "tracking_url": stored.get("tracking_url", ""),
        "scheduled_date": stored.get("scheduled_date"),
        "area_label": stored.get("area_label", ""),
        "delivery_fee": order_services.delivery_fee(order),
        "delivery_province": shipping["delivery_province"],
        "destination_region": shipping["destination_region"],
        "estimated_delivery": shipping["estimated_delivery"],
        "recipient": stored.get("recipient_name", "") or order["customer"].get("name", ""),
        "address": ", ".join(str(stored.get(key)) for key in ("street", "barangay", "city", "province") if stored.get(key)),
        "pickup_location": (stored.get("pickup_location") or {}).get("name", ""),
        "preferred_pickup_date": stored.get("preferred_pickup_date"),
        "pickup_schedule": stored.get("pickup_schedule"),
        "claimed_by": (stored.get("pickup_confirmation") or {}).get("claimant_name", ""),
        "attempts": len(stored.get("attempts", [])),
        "last_failure": next((FAILURE_REASONS.get(item.get("failure_reason", ""), "") for item in reversed(stored.get("attempts", [])) if item.get("status") == "failed"), ""),
        "open_issues": sum(1 for item in stored.get("issues", []) if item.get("status") == "open"),
        "cancellation_pending": bool(order_services.pending_cancellation(order)),
    })
    return row


def _search(rows: list[dict[str, Any]], params: Mapping[str, Any]) -> list[dict[str, Any]]:
    from backend.app.services import report_services

    needle = report_services.param_text(params, "search")
    if not needle:
        return rows
    return [row for row in rows if report_services.matches(needle, row["reference"], row["customer_name"], row["product"], row.get("tracking_number"), row.get("recipient"))]


def admin_production(params: Mapping[str, Any]) -> dict[str, Any]:
    from backend.app.services import report_services

    tab = params.get("tab") or "printing"
    if tab not in PRODUCTION_TABS:
        raise HTTPException(status_code=422, detail="Choose a valid production tab.")
    orders, services = report_services._load()
    counts = {key: sum(1 for order in orders if order["status"] in statuses) for key, statuses in PRODUCTION_TABS.items()}
    rows = [_row(order, services) for order in orders if order["status"] in PRODUCTION_TABS[tab]]
    rows = sorted(_search(rows, params), key=lambda row: row["updated_at"], reverse=True)
    return {"tab": tab, "counts": counts, **report_services.paginate(rows, params)}


def admin_fulfillment(params: Mapping[str, Any]) -> dict[str, Any]:
    """Fulfillment Management: overview counts, pickup orders, delivery orders, delivery issues, and history."""
    from backend.app.services import report_services

    tab = params.get("tab") or "overview"
    if tab not in {"overview", "pickup", "delivery", "issues", "history"}:
        raise HTTPException(status_code=422, detail="Choose a valid fulfillment tab.")
    orders, services = report_services._load()
    counts = {status: sum(1 for order in orders if order["status"] == status) for status in FULFILLMENT_STATUSES}
    issues = [order for order in orders if order["status"] == "Delivery Failed" or any(
        item.get("status") == "open" for item in order_services.fulfillment(order).get("issues", []))]
    summary = {
        "counts": counts,
        "pickup_waiting": sum(1 for order in orders if order["status"] == "Ready for Pickup"),
        "dispatch_waiting": sum(1 for order in orders if order["status"] == "Ready for Dispatch"),
        "issues": len(issues),
        "awaiting_full_payment": counts["Awaiting Full Payment"],
    }
    if tab == "overview":
        active = [order for order in orders if order["status"] in FULFILLMENT_STATUSES and order["status"] != "Completed"]
        rows = [_row(order, services) for order in active]
    elif tab == "pickup":
        rows = [_row(order, services) for order in orders if order_services.fulfillment_method(order) == "pickup" and order["status"] in PICKUP_STATUSES]
    elif tab == "delivery":
        rows = [_row(order, services) for order in orders if order_services.fulfillment_method(order) == "delivery" and order["status"] in DELIVERY_STATUSES]
    elif tab == "issues":
        rows = [_row(order, services) for order in issues]
    else:
        rows = []
        for order in orders:
            stored = order_services.fulfillment(order)
            base = {"reference": order["reference"], "customer_name": order["customer"].get("name", ""), "product": (order.get("product") or {}).get("name", ""),
                    "status": order["status"], "method": FULFILLMENT_METHODS[order_services.fulfillment_method(order)]}
            for attempt in stored.get("attempts", []):
                outcome = {"delivered": "Delivered", "failed": "Delivery failed", "out_for_delivery": "Out for delivery"}.get(attempt.get("status", ""), attempt.get("status", ""))
                rows.append({**base, "event": f"Delivery attempt {attempt.get('number')}: {outcome}",
                             "detail": " · ".join(str(value) for value in (attempt.get("courier"), attempt.get("tracking_number"), FAILURE_REASONS.get(attempt.get("failure_reason", ""), ""), attempt.get("received_by")) if value),
                             "at": attempt.get("delivered_at") or attempt.get("failed_at") or attempt.get("dispatched_at") or "",
                             "customer_name_search": base["customer_name"]})
            pickup = stored.get("pickup_confirmation")
            if pickup:
                rows.append({**base, "event": "Order claimed", "detail": f"{pickup.get('claimant_name')} ({pickup.get('claimant_type_label', '')})", "at": pickup.get("claimed_at", "")})
            if order["status"] == "Completed" and order.get("completed_at"):
                rows.append({**base, "event": "Completed", "detail": "", "at": order["completed_at"]})
        needle = report_services.param_text(params, "search")
        if needle:
            rows = [row for row in rows if report_services.matches(needle, row["reference"], row["customer_name"], row["product"], row["detail"])]
        rows.sort(key=lambda row: row["at"] or "", reverse=True)
        return {"tab": tab, "summary": summary, **report_services.paginate(rows, params)}
    rows = sorted(_search(rows, params), key=lambda row: row["updated_at"], reverse=True)
    return {"tab": tab, "summary": summary, **report_services.paginate(rows, params)}


# ================================================================== reprints

# Fulfillment details of one release cycle. A reprint archives them (nothing is deleted) and starts a new cycle.
CYCLE_FULFILLMENT_KEYS = (
    "released_at", "pickup_code", "pickup_confirmation", "attempts", "courier", "courier_id", "courier_service",
    "tracking_number", "tracking_url", "shipment_date", "scheduled_date", "delivery_personnel", "dispatched_at",
    "delivered_at", "pickup_schedule", "reschedules",
)
RESTART_STAGES = {"In Production": "In Production (same approved artwork)", "Draft Preparation": "Draft Preparation (artwork must change)"}


def _finish_reprint(order: Mapping[str, Any], now: str, actor: str) -> dict[str, Any]:
    """Mark the active reprint completed when the reprinted order is delivered or claimed."""
    reprint = order_services.active_reprint(order)
    if reprint is None:
        return {}
    record = order_services.production(order)
    finished = {**record["reprint"], "status": "completed", "completed_at": now, "completed_by": actor}
    record["reprint"] = finished
    record["reprints"] = [finished if item.get("id") == finished["id"] else item for item in record.get("reprints", [])]
    return {"production": record}


def reprint_updates(order: Mapping[str, Any], payload: Mapping[str, Any], actor: str, refund_id: str | None = None) -> tuple[dict[str, Any], str, dict[str, Any], dict[str, Any]]:
    """Validate a reprint and build its changes: (order updates, history text, audit details, customer notification).

    The original order, payments, production record, and delivery/pickup history are kept; the current production
    and fulfillment cycle are archived and a new linked production cycle starts. Quality checking and packaging
    are required again. A free reprint adds no charge; a chargeable one needs the customer's agreement.
    """
    if order["status"] not in order_services.REPRINTABLE_STATUSES:
        raise HTTPException(status_code=409, detail="A reprint can be started only after the order was delivered, claimed, or completed.")
    order_services.ensure_no_pending_cancellation(order)
    if order_services.active_reprint(order):
        raise HTTPException(status_code=409, detail="A reprint of this order is already in progress.")
    if payload.get("authorized") is not True:
        raise HTTPException(status_code=422, detail="Confirm that you authorize this reprint.")
    reason = _text(payload.get("reason"), "Reprint reason", 1000, True)
    ordered = _ordered_quantity(order)
    quantity = payload.get("quantity")
    quantity = ordered if quantity in (None, "") else _quantity(quantity, "Reprint quantity", ordered)
    if not quantity:
        raise HTTPException(status_code=422, detail="Enter the number of pieces to reprint.")
    stage = payload.get("restart_stage") or "In Production"
    if stage not in RESTART_STAGES:
        raise HTTPException(status_code=422, detail="Restart the reprint at In Production or Draft Preparation.")
    if stage == "In Production" and not order.get("draft_attachment"):
        raise HTTPException(status_code=409, detail="This order has no approved draft on file: restart the reprint at Draft Preparation.")
    chargeable = payload.get("chargeable") is True
    charge = 0.0
    if chargeable:
        charge_value = payload.get("charge_amount")
        if not order_services._valid_amount(charge_value) or charge_value > 1_000_000:
            raise HTTPException(status_code=422, detail="Enter the reprint charge the customer agreed to.")
        if payload.get("customer_agreed") is not True:
            raise HTTPException(status_code=422, detail="A chargeable reprint needs the customer's agreement to the charge.")
        charge = round(float(charge_value), 2)
    now = now_iso()
    record = order_services.production(order)
    reprints = list(record.get("reprints", []))
    reprint = {
        "id": f"RP-{now[2:10].replace('-', '')}-{secrets.token_hex(3).upper()}",
        "number": len(reprints) + 1,
        "reason": reason,
        "refund_id": refund_id,
        "quantity": quantity,
        "restart_stage": stage,
        "chargeable": chargeable,
        "charge_amount": charge,
        "customer_agreed": chargeable,
        "authorized_by": actor,
        "authorized_at": now,
        "original_status": order["status"],
        "original_total": order["total_amount"],
        "original_completed_at": order.get("completed_at"),
        "status": "in_progress",
        "remarks": _text(payload.get("remarks", "") or "", "Remarks", 1000),
    }
    archived = {key: value for key, value in record.items() if key not in {"reprint", "reprints", "previous_cycles"}}
    archived.setdefault("cycle", record.get("cycle", 1))
    archived["archived_at"] = now
    new_record: dict[str, Any] = {
        "cycle": archived["cycle"] + 1,
        "reprint_id": reprint["id"],
        "ordered_quantity": quantity,
        "progress": 0,
        "updates": [],
        "photos": [],
        "reprint": reprint,
        "reprints": reprints + [reprint],
        "previous_cycles": list(record.get("previous_cycles", [])) + [archived],
    }
    if stage == "In Production":
        new_record["started_at"] = now
    stored = order_services.fulfillment(order)
    cycle = {key: stored.pop(key) for key in CYCLE_FULFILLMENT_KEYS if key in stored}
    if cycle:
        cycle.update({"cycle": archived["cycle"], "archived_at": now})
        stored["previous_cycles"] = stored.get("previous_cycles", []) + [cycle]
    updates: dict[str, Any] = {"production": new_record, "fulfillment": stored, "status": stage}
    if charge:
        updates["total_amount"] = round(float(order["total_amount"] or 0) + charge, 2)
    event = (
        f"Administrator authorized reprint {reprint['id']} of {quantity:,} pc(s){f' for refund request {refund_id}' if refund_id else ''}: {reason}. "
        f"Production restarts at {stage}; quality checking and packaging are required again. "
        + (f"Chargeable reprint: PHP {charge:,.2f} added with the customer's agreement." if charge else "Free reprint: no additional payment is required.")
        + " The original order, payments, and delivery/pickup history are kept."
    )
    audit = {"reprint": reprint["id"], "refund": refund_id, "quantity": quantity, "restart_stage": stage, "chargeable": chargeable, "charge": charge, "reason": reason}
    notice = notification_services.build(
        order, "reprint_started", f"reprint:{reprint['id']}", "Reprint started",
        f"The shop is reprinting {quantity:,} pc(s) of order {order['reference']}: {reason}. "
        + (f"An agreed reprint charge of PHP {charge:,.2f} was added to your balance." if charge else "There is no additional charge."),
    )
    return updates, event, audit, notice


def tracking_archive_ops(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Mark the current cycle's tracking numbers as archived when a reprint restarts production.

    Redelivery within the same cycle may reuse a tracking number; a number from a cycle that has been
    archived by a reprint may not, because it belongs to a parcel that was already handed to the courier.
    """
    stored = order_services.fulfillment(order)
    cycle = str(order_services.production(order).get("cycle") or 1)
    numbers = [stored.get("tracking_number")]
    numbers.extend(item.get("tracking_number") for item in stored.get("attempts", []))
    operations: list[dict[str, Any]] = []
    for number in numbers:
        key = str(number or "").replace(" ", "").casefold()
        if len(key) >= 6:
            operations.append({"op": "archive", "kind": "tracking", "key": key, "context": f"archived:{cycle}"})
    return operations


def start_reprint(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    order = _admin_order(reference)
    updates, event, details, notice = reprint_updates(order, payload, actor)
    return _save(
        order, updates, event, payload.get("expected_version"), admin=True,
        audit=_audit("reprint.started", ADMIN, actor, **details),
        notifications=[notice],
        claim_ops=tracking_archive_ops(order),
    )

