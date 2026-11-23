"""Cancellation requests and refunds.

Cancellation and refund are kept apart: a cancellation decides whether an order stops; a refund decides how
much verified money goes back. A duplicate or excess payment is refunded without cancelling the order.

Cancellation (paid orders; unpaid orders keep the direct cancel in order_services)::

    Customer requests → Pending Review → administrator approves (order Cancelled, refund created when money
    is due back) or rejects (the order continues)

While a request is pending, production, payment recording, and dispatch actions are on hold. Payments that
wait for verification must be verified or rejected before the request is approved.

Refunds::

    Pending Review → Approved → Processing → Refunded        (also Rejected, Not Eligible, Failed → retried)

``Maximum refund = verified payments − completed refunds − refunds already approved or in progress − deductions``.
A refund is marked Refunded only with the e-wallet transfer reference, proof, a verified recipient, and the
administrator's confirmation that the transfer went through. The shop never asks for passwords, PINs, or OTPs.

The cancellation policy table (``POLICY``) is a *proposal* shown to the administrator as guidance. It is
never applied automatically: the administrator enters every deduction with its justification.
"""
from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

from backend.app.models.order import OrderRecord
from backend.app.repositories import order_repository
from backend.app.services import file_services, notification_services, order_services, shop_settings_services
from backend.app.utils.clock import now_iso


ADMIN = order_services.ADMIN
CUSTOMER = order_services.CUSTOMER
_text = order_services._text
_audit = order_services._audit
_save = order_services._save
_admin_order = order_services._admin_order

CANCELLATION_REASONS = {
    "change_of_mind": "Changed my mind",
    "delayed": "Order is taking too long",
    "wrong_details": "Wrong order details",
    "duplicate_order": "Ordered twice by mistake",
    "financial": "Financial reasons",
    "other": "Other",
}
CANCELLATION_LABELS = {"pending_review": "Pending Review", "approved": "Approved", "rejected": "Rejected"}
REFUND_REASONS = {
    "cancellation": "Approved order cancellation",
    "duplicate_payment": "Duplicate payment",
    "excess_payment": "Excess payment",
    "incorrect_product": "Incorrect product",
    "damaged_product": "Damaged product",
    "shop_unable": "Shop unable to fulfill the order",
    "other": "Other verified reason",
}
CUSTOMER_REFUND_REASONS = ("duplicate_payment", "excess_payment", "incorrect_product", "damaged_product", "other")
PRODUCT_PROBLEMS = {"incorrect_product", "damaged_product"}
REFUND_LABELS = {
    "pending_review": "Pending Review",
    "approved": "Approved",
    "processing": "Processing",
    "refunded": "Refunded",
    "rejected": "Rejected",
    "failed": "Failed",
    "not_eligible": "Not Eligible",
    "reprint": "Resolved by Reprint",
}
# Money set aside for these refunds counts against the maximum refundable amount.
RESERVED_STATUSES = {"approved", "processing", "failed"}
OPEN_REFUND_STATUSES = {"pending_review", "approved", "processing", "failed"}
FINISHED_STATUSES = {"Completed", "Cancelled", "Rejected", "Delivered", "Order Claimed"}
DELIVERED_STATUSES = {"Delivered", "Order Claimed", "Completed"}
# Proposed treatment by order stage (needs the shop owner's approval; guidance only, never enforced).
POLICY = (
    ({"Pending Confirmation", "Under Review", "Awaiting Quotation", "Quotation Sent", "Quotation Declined", "Awaiting Down Payment"},
     "Paid, work not started", "Proposed: full refund."),
    ({"Confirmed"}, "Paid, draft not started", "Proposed: full refund."),
    ({"Draft Preparation", "Awaiting Customer Approval"}, "Draft preparation",
     "Proposed: full or partial refund, minus documented design costs."),
    ({"Approved for Printing", "In Production", "Quality Checking"}, "Printing / production",
     "Proposed: partial or no refund, according to the approved policy and documented material costs."),
    ({"Packaging", "Awaiting Full Payment", "Ready for Pickup", "Ready for Dispatch", "Delivery Failed"}, "Packaging",
     "Proposed: normally no change-of-mind cancellation."),
    ({"Out for Delivery"}, "Out for delivery", "Proposed: normally no change-of-mind cancellation."),
)


# ================================================================== amounts


def refunds(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = order.get("refunds")
    return order_services.json_copy(value) if isinstance(value, list) else []


def refunded(order: Mapping[str, Any]) -> float:
    return order_services.refunded_amount(order)


def reserved(order: Mapping[str, Any], exclude_id: str | None = None) -> float:
    return round(sum(float(item.get("amount") or 0) for item in refunds(order)
                     if item.get("status") in RESERVED_STATUSES and item.get("id") != exclude_id), 2)


def max_refundable(order: Mapping[str, Any], exclude_id: str | None = None) -> float:
    """Verified payments minus completed refunds and refunds already approved or in progress."""
    return round(max(0.0, order_services.paid_amount(order) - refunded(order) - reserved(order, exclude_id)), 2)


def _amount(value: Any, label: str) -> float:
    if isinstance(value, bool) or not (order_services._valid_amount(value) or value == 0):
        raise HTTPException(status_code=422, detail=f"Enter a valid {label.lower()}.")
    return round(float(value), 2)


def _deductions(value: Any, justification: Any) -> tuple[list[dict[str, Any]], float, str]:
    """Documented deductions: each has a description and an amount; a justification is required for any deduction."""
    if value in (None, ""):
        value = []
    if not isinstance(value, list) or len(value) > 10:
        raise HTTPException(status_code=422, detail="List up to 10 deductions.")
    items = []
    for number, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail=f"Deduction {number} is invalid.")
        label = _text(item.get("label"), f"Deduction {number} description", 160, True)
        amount = item.get("amount")
        if not order_services._valid_amount(amount):
            raise HTTPException(status_code=422, detail=f"Deduction {number}: enter a valid amount.")
        items.append({"label": label, "amount": round(float(amount), 2)})
    total = round(sum(item["amount"] for item in items), 2)
    reason = _text(justification or "", "Deduction justification", 1000, required=bool(items))
    return items, total, reason


def _recipient(payload: Mapping[str, Any], required: bool) -> dict[str, str]:
    """Where the customer wants the refund sent (an e-wallet account). Never a password, PIN, or OTP."""
    provider = payload.get("refund_provider")
    name = _text(payload.get("recipient_name", "") or "", "Refund recipient name", 120, required)
    number = _text(payload.get("recipient_number", "") or "", "Refund e-wallet number", 40, required)
    if number and not number.replace(" ", "").replace("-", "").replace("+", "").isdigit():
        raise HTTPException(status_code=422, detail="Enter the e-wallet mobile or account number using digits only.")
    if provider in (None, "", "original"):
        provider_name = "Original payment method"
    else:
        provider_name = shop_settings_services.active_provider(provider)
    return {"provider": provider_name, "name": name, "number": number} if (name or number) else {}


def _payment_sources(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"method": item.get("method", ""), "reference": item.get("reference", ""), "amount": item.get("amount")}
        for item in order["payments"]
    ]


def policy_guidance(order: Mapping[str, Any]) -> dict[str, str]:
    status = order["status"]
    for statuses, stage, text in POLICY:
        if status in statuses:
            return {"stage": stage, "guidance": text}
    return {"stage": status, "guidance": "Review the order before deciding."}


# ================================================================== views


def _file(item: dict[str, Any] | None, url) -> dict[str, Any] | None:
    if not item:
        return None
    return {"id": item["id"], "name": item["name"], "mime_type": item["mime_type"], "size": item["size"], "url": url(item["id"])}


def cancellation_view(order: Mapping[str, Any], url, admin: bool) -> list[dict[str, Any]]:
    hidden = set() if admin else {"decided_by"}
    return [
        {**{key: value for key, value in item.items() if key not in hidden}, "status_label": CANCELLATION_LABELS.get(item.get("status", ""), "")}
        for item in order_services.cancellation_requests(order)
    ]


def refunds_view(order: Mapping[str, Any], url, admin: bool) -> list[dict[str, Any]]:
    hidden = {"proof", "evidence"} | (set() if admin else {"approved_by", "processed_by", "completed_by", "rejected_by"})
    views = []
    for item in refunds(order):
        view = {key: value for key, value in item.items() if key not in hidden}
        view["status_label"] = REFUND_LABELS.get(item.get("status", ""), item.get("status", ""))
        view["reason_label"] = REFUND_REASONS.get(item.get("reason", ""), item.get("reason", ""))
        view["proof"] = [_file(entry, url) for entry in item.get("proof", [])]
        view["evidence"] = [_file(entry, url) for entry in item.get("evidence", [])]
        if not admin:
            view["attempts"] = [{key: value for key, value in attempt.items() if key != "by"} for attempt in item.get("attempts", [])]
        views.append(view)
    return views


def files(order: Mapping[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in refunds(order):
        items += item.get("proof", []) + item.get("evidence", [])
    return items


def can_request_cancellation(order: Mapping[str, Any]) -> bool:
    """Paid orders (verified or reported payments) that are not finished and have no request under review."""
    if order["status"] in FINISHED_STATUSES or order_services.pending_cancellation(order):
        return False
    return bool(order["payments"]) or bool(order_services.pending_submissions(order))


def can_request_refund(order: Mapping[str, Any]) -> bool:
    if order_services.pending_cancellation(order):
        return False
    if order_services.paid_amount(order) <= 0 or max_refundable(order) <= 0:
        return False
    return not any(item.get("status") == "pending_review" for item in refunds(order))


# ================================================================== cancellation


def _new_id(prefix: str) -> str:
    return f"{prefix}-{now_iso()[2:10].replace('-', '')}-{secrets.token_hex(3).upper()}"


def _refund_claim(refund_id: str) -> dict[str, Any]:
    """Reserve the refund's own reference number in the order transaction."""
    return {
        "op": "acquire", "kind": "refund_id", "key": str(refund_id).casefold(),
        "detail": "This refund reference is already recorded.",
    }


def request_cancellation(reference: str, payload: Mapping[str, Any], customer_id: str | None) -> dict[str, Any]:
    order = order_services.customer_owned_order(reference, payload.get("email"), customer_id)
    if order["status"] in order_services.PENDING_STATUSES and not order["payments"] and not order_services.pending_submissions(order):
        raise HTTPException(status_code=409, detail="This order has no payment yet: cancel it directly from your customer portal.")
    if not can_request_cancellation(order):
        if order_services.pending_cancellation(order):
            raise HTTPException(status_code=409, detail="A cancellation request for this order is already under review.")
        raise HTTPException(status_code=409, detail="This order can no longer be cancelled. If something is wrong with it, request a refund instead.")
    reason = payload.get("reason")
    if reason not in CANCELLATION_REASONS:
        raise HTTPException(status_code=422, detail="Choose the reason for the cancellation.")
    remarks = _text(payload.get("remarks", "") or "", "Additional remarks", 1000, required=reason == "other")
    if payload.get("acknowledged") is not True:
        raise HTTPException(status_code=422, detail="Read and acknowledge the cancellation policy before sending the request.")
    paid = order_services.paid_amount(order)
    # Collect recipient details when a refund may become due now or after pending payments are verified.
    recipient_required = paid > 0 or bool(order["payments"]) or bool(order_services.pending_submissions(order))
    recipient = _recipient(payload, required=recipient_required)
    now = now_iso()
    request = {
        "id": _new_id("CR"),
        "reason": reason,
        "reason_label": CANCELLATION_REASONS[reason],
        "remarks": remarks,
        "status": "pending_review",
        "requested_at": now,
        "order_status": order["status"],
        "verified_paid": paid,
        "pending_verification": order_services.pending_submission_amount(order),
        "payment_methods": sorted({item.get("method", "") for item in order["payments"] if item.get("method")}),
        "refund_recipient": recipient,
        "acknowledged": True,
    }
    return _save(
        order,
        {"cancellation_requests": order_services.cancellation_requests(order) + [request]},
        f"Customer requested cancellation ({request['reason_label']})" + (f": {remarks}" if remarks else "")
        + f". Verified payments PHP {paid:,.2f}. Pending administrator review; no refund has been promised.",
        audit=_audit("cancellation.requested", CUSTOMER, customer_id, request=request["id"], reason=reason, verified_paid=paid),
        notifications=[notification_services.build(
            order, "cancellation_requested", f"cancellation:{request['id']}", "Cancellation request received",
            f"We received your cancellation request for order {order['reference']}. The shop will review the order's "
            "progress and payments; any refund is decided in that review.",
        )],
    )


def _find(items: list[dict[str, Any]], item_id: str, what: str) -> dict[str, Any]:
    found = next((item for item in items if item.get("id") == item_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail=f"{what} not found.")
    return found


def approve_cancellation(reference: str, request_id: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    order = _admin_order(reference)
    requests = order_services.cancellation_requests(order)
    request = _find(requests, request_id, "Cancellation request")
    if request.get("status") != "pending_review":
        raise HTTPException(status_code=409, detail=f"This cancellation request was already {request.get('status', '').replace('_', ' ')}.")
    if order_services.pending_submissions(order):
        raise HTTPException(status_code=409, detail="Verify or reject the payment waiting for verification before deciding on the cancellation.")
    deductions, total_deductions, justification = _deductions(payload.get("deductions"), payload.get("deduction_justification"))
    limit = max_refundable(order)
    if total_deductions > limit + 0.005:
        raise HTTPException(status_code=422, detail=f"Deductions cannot be more than the refundable PHP {limit:,.2f}.")
    proposed = payload.get("refund_amount")
    refund_amount = round(limit - total_deductions, 2) if proposed in (None, "") else _amount(proposed, "Refund amount")
    if refund_amount > round(limit - total_deductions, 2) + 0.005:
        raise HTTPException(status_code=422, detail=f"The refund cannot be more than PHP {limit - total_deductions:,.2f} (verified payments minus completed refunds and deductions).")
    remarks = _text(payload.get("remarks", "") or "", "Administrator remarks", 1000)
    work = _text(payload.get("completed_work", "") or "", "Completed work and documented expenses", 1000)
    recipient = dict(request.get("refund_recipient") or {})
    if any(payload.get(key) not in (None, "") for key in ("refund_provider", "recipient_name", "recipient_number")):
        recipient = _recipient(payload, required=refund_amount > 0)
    if refund_amount > 0 and not (recipient.get("name") and recipient.get("number")):
        raise HTTPException(
            status_code=422,
            detail="Enter the refund recipient's e-wallet name and number before approving a cancellation that includes a refund.",
        )
    now = now_iso()
    request.update({
        "status": "approved", "decided_at": now, "decided_by": actor, "remarks_admin": remarks, "completed_work": work,
        "verified_paid_at_decision": order_services.paid_amount(order), "deductions": deductions,
        "total_deductions": total_deductions, "deduction_justification": justification, "approved_refund": refund_amount,
        "stage_at_decision": order["status"], "refund_recipient": recipient,
    })
    all_refunds = refunds(order)
    notifications = [notification_services.build(
        order, "cancellation_approved", f"cancellation-approved:{request_id}", "Cancellation approved",
        f"Your cancellation request for order {order['reference']} was approved and the order is cancelled."
        + (f" Refund approved: PHP {refund_amount:,.2f}." if refund_amount > 0 else " No refund is due.")
        + (f" Deductions: PHP {total_deductions:,.2f} ({justification})." if total_deductions else ""),
    )]
    if refund_amount > 0:
        refund = {
            "id": _new_id("RF"), "reason": "cancellation", "details": f"Cancellation {request_id}: {request.get('reason_label', '')}",
            "status": "approved", "requested_at": request["requested_at"], "requested_by": "customer", "cancellation_id": request_id,
            "amount": refund_amount, "deductions": deductions, "total_deductions": total_deductions,
            "deduction_justification": justification, "approved_at": now, "approved_by": actor,
            "recipient": recipient, "original_payments": _payment_sources(order), "attempts": [],
        }
        all_refunds.append(refund)
        request["refund_id"] = refund["id"]
    return _save(
        order,
        {"cancellation_requests": requests, "refunds": all_refunds, "status": "Cancelled", "admin_notes": remarks or order["admin_notes"]},
        f"Administrator approved cancellation request {request_id} at stage {order['status']}. The order is cancelled."
        + (f" Deductions PHP {total_deductions:,.2f} ({justification})." if total_deductions else "")
        + (f" Refund approved: PHP {refund_amount:,.2f} ({request.get('refund_id')})." if refund_amount > 0 else " No refund is due.")
        + (f" Remarks: {remarks}" if remarks else ""),
        payload.get("expected_version"),
        admin=True,
        audit=_audit("cancellation.approved", ADMIN, actor, request=request_id, refund=refund_amount, deductions=total_deductions),
        notifications=notifications,
        claim_ops=[_refund_claim(refund["id"])] if refund_amount > 0 else [],
    )


def reject_cancellation(reference: str, request_id: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    order = _admin_order(reference)
    requests = order_services.cancellation_requests(order)
    request = _find(requests, request_id, "Cancellation request")
    if request.get("status") != "pending_review":
        raise HTTPException(status_code=409, detail="This cancellation request was already decided.")
    remarks = _text(payload.get("remarks"), "Reason for rejecting", 1000, True)
    request.update({"status": "rejected", "decided_at": now_iso(), "decided_by": actor, "remarks_admin": remarks, "stage_at_decision": order["status"]})
    return _save(
        order,
        {"cancellation_requests": requests},
        f"Administrator rejected cancellation request {request_id}: {remarks} The order continues.",
        payload.get("expected_version"),
        admin=True,
        audit=_audit("cancellation.rejected", ADMIN, actor, request=request_id, remarks=remarks),
        notifications=[notification_services.build(
            order, "cancellation_rejected", f"cancellation-rejected:{request_id}", "Cancellation request declined",
            f"Your cancellation request for order {order['reference']} was not approved: {remarks} Your order continues.",
        )],
    )


# ================================================================== refunds


@file_services.compensating_files
def request_refund(reference: str, payload: Mapping[str, Any], customer_id: str | None) -> dict[str, Any]:
    order = order_services.customer_owned_order(reference, payload.get("email"), customer_id)
    if order_services.pending_cancellation(order):
        raise HTTPException(status_code=409, detail="A cancellation request for this order is pending review. Wait for the cancellation decision before requesting a refund.")
    reason = payload.get("reason")
    if reason not in CUSTOMER_REFUND_REASONS:
        raise HTTPException(status_code=422, detail="Choose the reason for the refund request.")
    if reason in PRODUCT_PROBLEMS and order["status"] not in DELIVERED_STATUSES:
        raise HTTPException(status_code=409, detail="Report an incorrect or damaged product after you receive the order.")
    if reason in PRODUCT_PROBLEMS and order_services.active_reprint(order):
        raise HTTPException(status_code=409, detail="A reprint of this order is already in progress.")
    if not can_request_refund(order):
        raise HTTPException(status_code=409, detail="There is no refundable verified payment on this order, or a refund request is already under review.")
    details = _text(payload.get("details"), "Details", 1000, True)
    limit = max_refundable(order)
    requested = payload.get("amount")
    amount = None
    if requested not in (None, ""):
        amount = _amount(requested, "Requested amount")
        if amount > limit + 0.005:
            raise HTTPException(status_code=422, detail=f"The refundable amount on this order is at most PHP {limit:,.2f}.")
    resolution = payload.get("preferred_resolution") or "refund"
    if resolution not in {"refund", "reprint"} or (resolution == "reprint" and reason not in PRODUCT_PROBLEMS):
        raise HTTPException(status_code=422, detail="A reprint can only be requested for an incorrect or damaged product.")
    recipient = _recipient(payload, required=resolution == "refund")
    evidence_items = payload.get("evidence") or []
    if not isinstance(evidence_items, list) or len(evidence_items) > 4:
        raise HTTPException(status_code=422, detail="Attach up to 4 photos.")
    decoded = [order_services._decode_upload(item) for item in evidence_items]
    evidence = []
    for metadata, contents in decoded:
        order_services.file_services.save(metadata, contents)  # type: ignore[arg-type]
        evidence.append(metadata)
    refund = {
        "id": _new_id("RF"), "reason": reason, "details": details, "status": "pending_review", "requested_at": now_iso(),
        "requested_by": "customer", "requested_amount": amount, "amount": None, "preferred_resolution": resolution,
        "recipient": recipient, "evidence": evidence, "original_payments": _payment_sources(order), "attempts": [],
    }
    try:
        return _save(
            order,
            {"refunds": refunds(order) + [refund]},
            f"Customer requested a refund ({REFUND_REASONS[reason]}" + (f", PHP {amount:,.2f}" if amount else "") + f"): {details}"
            + (" Preferred resolution: reprint." if resolution == "reprint" else ""),
            audit=_audit("refund.requested", CUSTOMER, customer_id, refund=refund["id"], reason=reason, amount=amount),
            notifications=[notification_services.build(
                order, "refund_requested", f"refund:{refund['id']}", "Refund request received",
                f"We received your refund request {refund['id']} for order {order['reference']}. The shop will review it.",
            )],
            claim_ops=[_refund_claim(refund["id"])],
        )
    except HTTPException:
        for item in evidence:
            order_services.file_services.delete(item["filename"])
        raise


def create_refund(reference: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """The administrator opens a refund (for example the shop cannot fulfill the order, or a duplicate payment)."""
    order = _admin_order(reference)
    reason = payload.get("reason")
    if reason not in REFUND_REASONS or reason == "cancellation":
        raise HTTPException(status_code=422, detail="Choose the refund reason. Cancellation refunds are created when a cancellation is approved.")
    details = _text(payload.get("details"), "Details", 1000, True)
    pending_review = next((item for item in refunds(order) if item.get("status") == "pending_review"), None)
    if pending_review is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Refund {pending_review.get('id', '')} is already pending review for this order. Review or resolve it before opening another refund.",
        )
    if max_refundable(order) <= 0:
        raise HTTPException(status_code=409, detail="This order has no verified payment left to refund.")
    refund = {
        "id": _new_id("RF"), "reason": reason, "details": details, "status": "pending_review", "requested_at": now_iso(),
        "requested_by": "shop", "opened_by": actor, "requested_amount": None, "amount": None, "preferred_resolution": "refund",
        "recipient": _recipient(payload, required=False), "evidence": [], "original_payments": _payment_sources(order), "attempts": [],
    }
    return _save(
        order,
        {"refunds": refunds(order) + [refund]},
        f"Administrator opened refund {refund['id']} ({REFUND_REASONS[reason]}): {details}",
        payload.get("expected_version"),
        admin=True,
        audit=_audit("refund.opened", ADMIN, actor, refund=refund["id"], reason=reason),
        claim_ops=[_refund_claim(refund["id"])],
    )


def _refund_action(reference: str, refund_id: str) -> tuple[OrderRecord, list[dict[str, Any]], dict[str, Any]]:
    order = _admin_order(reference)
    items = refunds(order)
    return order, items, _find(items, refund_id, "Refund")


def _require(refund: Mapping[str, Any], *statuses: str) -> None:
    if refund.get("status") not in statuses:
        label = REFUND_LABELS.get(refund.get("status", ""), refund.get("status", ""))
        raise HTTPException(status_code=409, detail=f"This refund is {label}; the action is not available.")


def approve_refund(reference: str, refund_id: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    order, items, refund = _refund_action(reference, refund_id)
    _require(refund, "pending_review")
    resolution = payload.get("resolution") or "refund"
    now = now_iso()
    if resolution == "reprint":
        if refund.get("reason") not in PRODUCT_PROBLEMS:
            raise HTTPException(status_code=422, detail="A reprint resolves only an incorrect or damaged product.")
        from backend.app.services import fulfillment_services

        remarks = _text(payload.get("remarks"), "Reprint details", 1000, True)
        reason = f"{REFUND_REASONS[refund['reason']]} ({refund_id}): {refund.get('details', '')}".strip()
        updates, event, details, notice = fulfillment_services.reprint_updates(
            order, {**payload, "reason": payload.get("reason") or reason[:1000], "remarks": remarks}, actor, refund_id,
        )
        refund.update({
            "status": "reprint", "amount": 0.0, "approved_at": now, "approved_by": actor, "remarks": remarks,
            "reprint_id": updates["production"]["reprint"]["id"],
        })
        updates["refunds"] = items
        return _save(
            order, updates,
            f"Administrator resolved refund request {refund_id} with a reprint. {event}",
            payload.get("expected_version"), admin=True,
            audit=_audit("refund.reprint", ADMIN, actor, refund=refund_id, remarks=remarks, **{key: value for key, value in details.items() if key != "refund"}),
            notifications=[notice],
            claim_ops=fulfillment_services.tracking_archive_ops(order),
        )
    if resolution != "refund":
        raise HTTPException(status_code=422, detail="Choose refund or reprint.")
    deductions, total_deductions, justification = _deductions(payload.get("deductions"), payload.get("deduction_justification"))
    limit = round(max_refundable(order, refund_id) - total_deductions, 2)
    amount = _amount(payload.get("amount"), "Approved refund amount")
    if amount <= 0:
        raise HTTPException(status_code=422, detail="Enter the approved refund amount, or mark the request not eligible.")
    if amount > limit + 0.005:
        raise HTTPException(status_code=422, detail=f"The refund cannot be more than PHP {max(0.0, limit):,.2f} (verified payments minus completed or approved refunds and deductions).")
    remarks = _text(payload.get("remarks", "") or "", "Remarks", 1000)
    refund.update({
        "status": "approved", "amount": amount, "deductions": deductions, "total_deductions": total_deductions,
        "deduction_justification": justification, "approved_at": now, "approved_by": actor, "remarks": remarks,
    })
    return _save(
        order, {"refunds": items},
        f"Administrator approved refund {refund_id} of PHP {amount:,.2f}" + (f" after deductions of PHP {total_deductions:,.2f} ({justification})" if total_deductions else "") + "."
        + (f" Remarks: {remarks}" if remarks else ""),
        payload.get("expected_version"), admin=True,
        audit=_audit("refund.approved", ADMIN, actor, refund=refund_id, amount=amount, deductions=total_deductions),
        notifications=[notification_services.build(
            order, "refund_approved", f"refund-approved:{refund_id}", "Refund approved",
            f"Your refund {refund_id} of PHP {amount:,.2f} for order {order['reference']} was approved. You will be notified when the transfer is completed.",
        )],
    )


def decline_refund(reference: str, refund_id: str, payload: Mapping[str, Any], status: str, actor: str = ADMIN) -> dict[str, Any]:
    """Reject a refund request, or mark it not eligible. The reason is shown to the customer."""
    if status not in {"rejected", "not_eligible"}:
        raise HTTPException(status_code=422, detail="Invalid refund decision.")
    order, items, refund = _refund_action(reference, refund_id)
    _require(refund, "pending_review", "approved")
    remarks = _text(payload.get("remarks"), "Reason", 1000, True)
    refund.update({"status": status, "decided_at": now_iso(), "rejected_by": actor, "remarks": remarks})
    label = REFUND_LABELS[status]
    return _save(
        order, {"refunds": items},
        f"Administrator marked refund {refund_id} as {label}: {remarks}",
        payload.get("expected_version"), admin=True,
        audit=_audit(f"refund.{status}", ADMIN, actor, refund=refund_id, remarks=remarks),
        notifications=[notification_services.build(
            order, "refund_declined", f"refund-{status}:{refund_id}", f"Refund {label.lower()}",
            f"Your refund request {refund_id} for order {order['reference']} was marked {label.lower()}: {remarks}",
        )],
    )


def start_processing(reference: str, refund_id: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    order, items, refund = _refund_action(reference, refund_id)
    _require(refund, "approved", "failed")
    retry = refund["status"] == "failed"
    if retry and payload.get("confirmed_not_sent") is not True:
        raise HTTPException(status_code=422, detail="Confirm that the failed transfer was not received before retrying, so the refund is never paid twice.")
    if float(refund.get("amount") or 0) > max_refundable(order, refund_id) + 0.005:
        raise HTTPException(status_code=409, detail="The refund is more than the refundable amount left on this order.")
    refund.update({"status": "processing", "processing_at": now_iso(), "processed_by": actor})
    return _save(
        order, {"refunds": items},
        f"Administrator {'retried' if retry else 'started processing'} refund {refund_id} (PHP {float(refund['amount']):,.2f}).",
        payload.get("expected_version"), admin=True,
        audit=_audit("refund.processing", ADMIN, actor, refund=refund_id, retry=retry),
    )


def _transfer_unused(transaction: str) -> None:
    key = order_services._payment_reference_key(transaction)
    for other in order_repository.list_orders():
        for item in refunds(other):
            if order_services._payment_reference_key(str(item.get("transaction_reference") or "")) == key:
                raise HTTPException(status_code=409, detail="This refund transaction reference is already recorded.")
            for attempt in item.get("attempts", []):
                if order_services._payment_reference_key(str(attempt.get("transaction_reference") or "")) == key:
                    raise HTTPException(status_code=409, detail="This refund transaction reference is already recorded.")


@file_services.compensating_files
def complete_refund(reference: str, refund_id: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    """Mark a refund Refunded, only after the actual transfer is confirmed (reference + proof + verified recipient)."""
    order, items, refund = _refund_action(reference, refund_id)
    _require(refund, "processing")
    if payload.get("confirmed") is not True:
        raise HTTPException(status_code=422, detail="Confirm that the transfer was actually completed.")
    if payload.get("recipient_verified") is not True:
        raise HTTPException(status_code=422, detail="Confirm that the recipient account was verified as the customer's.")
    method = _text(payload.get("method"), "Refund method", 60, True)
    transaction = _text(payload.get("transaction_reference"), "Refund transaction reference", 120, True)
    if len(order_services._payment_reference_key(transaction)) < 4:
        raise HTTPException(status_code=422, detail="Enter the complete refund transaction reference.")
    _transfer_unused(transaction)
    recipient_name = _text(payload.get("recipient_name", "") or (refund.get("recipient") or {}).get("name", ""), "Recipient name", 120, True)
    recipient_number = _text(payload.get("recipient_number", "") or (refund.get("recipient") or {}).get("number", ""), "Recipient account number", 40, True)
    proof_items = payload.get("proof")
    if not isinstance(proof_items, list) or not proof_items or len(proof_items) > 3:
        raise HTTPException(status_code=422, detail="Attach the refund transfer receipt (up to 3 files).")
    decoded = [order_services._decode_upload(item) for item in proof_items]
    proof = []
    for metadata, contents in decoded:
        order_services.file_services.save(metadata, contents)  # type: ignore[arg-type]
        proof.append(metadata)
    if float(refund.get("amount") or 0) > max_refundable(order, refund_id) + 0.005:
        for item in proof:
            order_services.file_services.delete(item["filename"])
        raise HTTPException(status_code=409, detail="The refund is more than the refundable amount left on this order.")
    now = now_iso()
    refund.update({
        "status": "refunded", "method": method, "transaction_reference": transaction, "refunded_at": now,
        "completed_by": actor, "proof": proof, "verified_recipient": {"name": recipient_name, "number": recipient_number},
    })
    try:
        return _save(
            order, {"refunds": items},
            f"Administrator completed refund {refund_id}: PHP {float(refund['amount']):,.2f} sent via {method} to {recipient_name} "
            f"(transaction {transaction}).",
            payload.get("expected_version"), admin=True,
            audit=_audit("refund.completed", ADMIN, actor, refund=refund_id, amount=refund["amount"], method=method, transaction=transaction),
            notifications=[notification_services.build(
                order, "refund_completed", f"refund-completed:{refund_id}", "Refund completed",
                f"Your refund {refund_id} of PHP {float(refund['amount']):,.2f} was sent via {method} (transaction {transaction}).",
            )],
            claim_ops=[{
                "op": "acquire", "kind": "refund", "key": order_services._payment_reference_key(transaction),
                "context": refund_id, "detail": "This refund transaction reference is already recorded.",
            }],
        )
    except HTTPException:
        for item in proof:
            order_services.file_services.delete(item["filename"])
        raise


def fail_refund(reference: str, refund_id: str, payload: Mapping[str, Any], actor: str = ADMIN) -> dict[str, Any]:
    order, items, refund = _refund_action(reference, refund_id)
    _require(refund, "processing")
    reason = _text(payload.get("reason"), "Failure reason", 500, True)
    transaction = _text(payload.get("transaction_reference", "") or "", "Failed transaction reference", 120)
    now = now_iso()
    refund.setdefault("attempts", []).append({"at": now, "by": actor, "result": "failed", "reason": reason, "transaction_reference": transaction})
    refund.update({"status": "failed", "failed_at": now, "failure_reason": reason})
    return _save(
        order, {"refunds": items},
        f"Refund {refund_id} transfer failed: {reason}. No money was recorded as refunded.",
        payload.get("expected_version"), admin=True,
        audit=_audit("refund.failed", ADMIN, actor, refund=refund_id, reason=reason),
        notifications=[notification_services.build(
            order, "refund_failed", f"refund-failed:{refund_id}:{len(refund['attempts'])}", "Refund transfer failed",
            f"The transfer for refund {refund_id} did not go through ({reason}). The shop will retry or contact you.",
        )],
        claim_ops=[{
            "op": "acquire", "kind": "refund", "key": order_services._payment_reference_key(transaction),
            "context": f"{refund_id}:failed:{now}", "detail": "This refund transaction reference is already recorded.",
        }] if order_services._payment_reference_key(transaction) else [],
    )


# ================================================================== admin lists


def _base(order: OrderRecord) -> dict[str, Any]:
    return {
        "reference": order["reference"],
        "customer_name": order["customer"].get("name", ""),
        "customer_email": order["customer"].get("email", ""),
        "product": (order.get("product") or {}).get("name", ""),
        "order_status": order["status"],
        "total_amount": order["total_amount"],
        "verified_paid": order_services.paid_amount(order),
        "refunded": refunded(order),
        "max_refundable": max_refundable(order),
        "pending_verification": order_services.pending_submission_amount(order),
        "payment_methods": sorted({item.get("method", "") for item in order["payments"] if item.get("method")}),
        "version": order_repository.order_version(order),
    }


def admin_cancellations(params: Mapping[str, Any]) -> dict[str, Any]:
    from backend.app.services import report_services

    status = params.get("status") or "pending_review"
    if status not in {*CANCELLATION_LABELS, "all"}:
        raise HTTPException(status_code=422, detail="Choose a valid cancellation status.")
    orders = order_repository.list_orders()
    rows, counts = [], {key: 0 for key in CANCELLATION_LABELS}
    for order in orders:
        for item in order_services.cancellation_requests(order):
            counts[item.get("status", "pending_review")] = counts.get(item.get("status", "pending_review"), 0) + 1
            if status != "all" and item.get("status") != status:
                continue
            rows.append({**_base(order), **item, "status_label": CANCELLATION_LABELS.get(item.get("status", ""), ""), **{"policy": policy_guidance(order)}})
    needle = report_services.param_text(params, "search")
    if needle:
        rows = [row for row in rows if report_services.matches(needle, row["reference"], row["customer_name"], row["id"], row["product"])]
    rows.sort(key=lambda row: row.get("requested_at") or "", reverse=True)
    return {"status": status, "counts": counts, **report_services.paginate(rows, params)}


def admin_refunds(params: Mapping[str, Any]) -> dict[str, Any]:
    from backend.app.services import report_services

    view = params.get("view") or "requests"
    if view not in {"requests", "history"}:
        raise HTTPException(status_code=422, detail="Choose refund requests or refund history.")
    status = params.get("status") or ""
    if status and status not in REFUND_LABELS:
        raise HTTPException(status_code=422, detail="Choose a valid refund status.")
    orders = order_repository.list_orders()
    rows, counts = [], {key: 0 for key in REFUND_LABELS}
    for order in orders:
        for item in refunds(order):
            counts[item.get("status", "")] = counts.get(item.get("status", ""), 0) + 1
            in_view = item.get("status") in OPEN_REFUND_STATUSES if view == "requests" else item.get("status") not in OPEN_REFUND_STATUSES
            if not in_view or (status and item.get("status") != status):
                continue
            rows.append({
                **_base(order),
                **{key: value for key, value in item.items() if key not in {"proof", "evidence"}},
                "status_label": REFUND_LABELS.get(item.get("status", ""), ""),
                "reason_label": REFUND_REASONS.get(item.get("reason", ""), item.get("reason", "")),
                "max_for_this": max_refundable(order, item.get("id")),
                "proof_count": len(item.get("proof", [])),
                "evidence_count": len(item.get("evidence", [])),
            })
    needle = report_services.param_text(params, "search")
    if needle:
        rows = [row for row in rows if report_services.matches(needle, row["reference"], row["customer_name"], row["id"], row.get("transaction_reference"))]
    rows.sort(key=lambda row: row.get("refunded_at") or row.get("approved_at") or row.get("requested_at") or "", reverse=True)
    totals = {
        "refunded_total": round(sum(refunded(order) for order in orders), 2),
        "reserved_total": round(sum(reserved(order) for order in orders), 2),
    }
    return {"view": view, "status": status, "counts": counts, "totals": totals, **report_services.paginate(rows, params)}
