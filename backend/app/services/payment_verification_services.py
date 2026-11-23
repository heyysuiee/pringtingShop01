"""Compare OCR results with the order and decide how a receipt should be treated.

This module performs *matching*, not verification of money. Its ``verified`` label is only produced when an
independent payment provider has positively confirmed the transaction. Without that confirmation the receipt
stays pending or goes to an administrator, no matter how convincing the screenshot looks.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from backend.app.core.config import settings
from backend.app.models.order import DOWN_PAYMENT_RATE


# Internal workflow states are kept for backwards compatibility with older submissions and reports. The
# ``result_*`` codes below are the customer-facing verification outcomes shown in the admin payment table.
# Nothing here moves money: only the existing admin confirmation or an independent provider confirmation
# can create a payment record.
VERIFIED = "verified"
PENDING = "pending"
ADMIN_REVIEW = "admin_review"
REJECTED = "rejected"
DUPLICATE = "possible_duplicate"

RESULT_VERIFIED = "verified"
RESULT_OCR_PENDING = "ocr_checked_pending"
RESULT_INSUFFICIENT = "insufficient_payment"
RESULT_REFERENCE_MISMATCH = "reference_mismatch"
RESULT_DUPLICATE = "duplicate_reference"
RESULT_MANUAL_REVIEW = "needs_manual_review"
RESULT_REJECTED = "rejected"

STATUS_LABELS = {
    VERIFIED: "Verified",
    PENDING: "Pending Verification",
    ADMIN_REVIEW: "Needs Manual Review",
    REJECTED: "Rejected",
    DUPLICATE: "Duplicate Reference Number",
}
RESULT_LABELS = {
    RESULT_VERIFIED: "Verified",
    RESULT_OCR_PENDING: "OCR-checked · pending manual verification",
    RESULT_INSUFFICIENT: "Insufficient Payment",
    RESULT_REFERENCE_MISMATCH: "Reference Number Mismatch",
    RESULT_DUPLICATE: "Duplicate Reference Number",
    RESULT_MANUAL_REVIEW: "Needs Manual Review",
    RESULT_REJECTED: "Rejected",
}
CHECK_STATES = {"match", "missing", "inconsistent", "pending"}


def _key(value: Any) -> str:
    return re.sub(r"[\s\-_]+", "", str(value or "")).casefold()


def _number(value: Any) -> float | None:
    try:
        return round(float(str(value).replace(",", "")), 2)
    except (TypeError, ValueError):
        return None


def down_payment_calculation(
    order: Mapping[str, Any],
    stage_required: float,
    extracted_amount: float | None,
    already_paid: float = 0.0,
) -> dict[str, Any]:
    """The money arithmetic the admin no longer has to do by hand.

    ``stage_required`` is the server-calculated amount due for this submission (the rest of the down payment,
    or the remaining balance). ``extracted_amount`` is the actual amount OCR read from the receipt. Earlier
    verified payments are included so multiple partial payments accumulate correctly: a new payment is added
    to, never substituted for, the confirmed total. Every figure is recomputed from the order, never from a
    value supplied by the customer's browser.

    The status describes what would happen *if the administrator confirms this amount*. OCR alone never
    credits the payment.
    """
    from backend.app.services import order_services

    total = _number(order.get("total_amount")) or 0.0
    stored = _number(order.get("required_down_payment"))
    required_down = stored if stored is not None and 0 < stored <= total + 0.005 else round(total * DOWN_PAYMENT_RATE, 2)
    paid_before = round(max(0.0, _number(already_paid) or 0.0), 2)
    actual = _number(extracted_amount)
    paid_after = round(paid_before + (actual or 0.0), 2)
    due = round(max(0.0, _number(stage_required) or 0.0), 2)
    shortfall = round(max(0.0, due - actual), 2) if actual is not None and due > actual + 0.005 else 0.0
    stage_excess = round(actual - due, 2) if actual is not None and actual > due + 0.005 else 0.0
    remaining_down = round(max(0.0, required_down - paid_after), 2)
    # Excess is money above the whole order, not merely above one stage. Stage excess is kept separately so
    # the admin can still see that a receipt paid more than the current instalment.
    excess = round(max(0.0, paid_after - total), 2)
    return {
        "total_order_amount": round(total, 2),
        "required_down_payment": round(required_down, 2),
        "stage_amount_due": due,
        "actual_amount_paid": actual,
        "previously_paid": paid_before,
        "cumulative_paid": paid_after,
        "remaining_down_payment": remaining_down,
        "remaining_balance": round(max(0.0, total - paid_after), 2),
        "remaining_balance_exact": round(total - paid_after, 2),
        "excess_amount": excess,
        "stage_excess_amount": stage_excess,
        "down_payment_shortfall": remaining_down,
        "additional_needed_for_down_payment": remaining_down,
        "insufficient_by": shortfall,
        "payment_status": order_services.payment_status_from_amounts(
            round(total, 2), round(required_down, 2), paid_after
        ),
        "payment_percentage": round((paid_after / total) * 100, 1) if total > 0 else 0.0,
        "down_payment_percentage": round((min(paid_after, required_down) / required_down) * 100, 1) if required_down > 0 else 0.0,
        "down_payment_met": bool(total > 0 and paid_after >= required_down - 0.005),
    }


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def parse_date(value: str) -> datetime | None:
    """Best-effort parsing of the common receipt date formats (naive values are treated as local time)."""
    text = str(value or "").strip()
    if not text:
        return None
    month_name = (
        r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
    )
    named_patterns = (
        (rf"\b({month_name})\.?\s+(\d{{1,2}}),?\s+(20\d{{2}})\b", "mdy"),
        (rf"\b(\d{{1,2}})\s+({month_name})\.?,?\s+(20\d{{2}})\b", "dmy"),
    )
    for pattern, order in (
        (r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", "ymd"),
        (r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", "dmy"),
        *named_patterns,
    ):
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        try:
            groups = match.groups()
            if order == "ymd":
                year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
            elif order == "mdy":
                month = _MONTHS.get(str(groups[0]).casefold()[:4], _MONTHS.get(str(groups[0]).casefold(), 0))
                day, year = int(groups[1]), int(groups[2])
            elif order == "dmy" and isinstance(groups[1], str) and not groups[1].isdigit():
                day = int(groups[0])
                month = _MONTHS.get(str(groups[1]).casefold()[:4], _MONTHS.get(str(groups[1]).casefold(), 0))
                year = int(groups[2])
            else:
                day, month, year = int(groups[0]), int(groups[1]), int(groups[2])
            if not month:
                continue
            time_match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?", text)
            hour = minute = second_value = 0
            if time_match:
                hour, minute = int(time_match.group(1)), int(time_match.group(2))
                second_value = int(time_match.group(3) or 0)
                period = (time_match.group(4) or "").casefold()
                if period == "pm" and hour < 12:
                    hour += 12
                elif period == "am" and hour == 12:
                    hour = 0
            return datetime(year, month, day, hour, minute, second_value)
        except ValueError:
            continue
    return None


def _ip_is_local(address: str) -> bool:
    """True for addresses that must never be contacted as a payment provider."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True  # not a literal: the caller resolves host names separately
    return (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def _literal_host(host: str) -> str:
    """Normalise decimal/hex-encoded IPv4 hosts so they cannot bypass the literal check."""
    try:
        if host.lower().startswith("0x"):
            return str(ipaddress.IPv4Address(int(host, 16)))
        if host.isdigit():
            return str(ipaddress.IPv4Address(int(host, 10)))
    except (ValueError, ipaddress.AddressValueError):
        pass
    return host


def endpoint_problem(endpoint: Any) -> str:
    """Return a short reason why a gateway endpoint is unsafe, or "" when it is acceptable.

    The admin settings form validates the URL when it is saved; this check runs again at request time
    because a stored value may come from an older version, a direct database edit, or a new deployment.
    Host names are resolved here and every returned address must be public. Unresolvable names are
    allowed through only so the request itself fails closed (no connection can be made to them).
    """
    text = str(endpoint or "").strip()
    if not text:
        return "no endpoint is configured"
    try:
        parsed = urlparse(text)
    except ValueError:
        return "the endpoint URL is malformed"
    if parsed.scheme not in {"https", "http"}:
        return "the endpoint must use HTTPS"
    if parsed.username or parsed.password:
        return "the endpoint must not contain credentials"
    if parsed.fragment:
        return "the endpoint must not contain a URL fragment"
    host = parsed.hostname
    if not host:
        return "the endpoint has no host"
    host = _literal_host(host)
    # Local HTTP is kept only for administrators testing a gateway on their own machine.
    local_dev = parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and not local_dev:
        return "HTTPS is required for a remote payment provider"
    allowed = tuple(value.strip().casefold() for value in settings.gateway_allowed_hosts if value.strip())
    if allowed and not any(host.casefold() == value or host.casefold().endswith("." + value) for value in allowed):
        return "the endpoint host is not in the configured allow-list"
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal:
        if _ip_is_local(host) and not local_dev:
            return "the endpoint points at a private or local network address"
        return ""
    if host.casefold() == "localhost" or host.casefold().endswith(".localhost"):
        return "local host names are not accepted as payment providers"
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, ValueError):
        infos = []  # the connection itself will fail; nothing is approved by a failed lookup
    for info in infos:
        if _ip_is_local(str(info[4][0])):
            return "the endpoint host resolves to a private or local network address"
    return ""


def gateway_view(config: Any) -> dict[str, Any]:
    """A safe view of gateway settings: configuration status only, never a key or secret."""
    data = config if isinstance(config, dict) else {}
    return {
        "provider": str(data.get("provider") or "").strip()[:60],
        "endpoint": str(data.get("endpoint") or "").strip()[:200],
        "configured": bool(str(data.get("api_key") or "").strip()),
        "timeout_seconds": data.get("timeout_seconds", 10),
    }


def confirm_with_provider(
    method: str,
    verification_method: str,
    gateway_config: Any,
    reference: str,
    amount: float,
) -> dict[str, Any]:
    """Ask an authorized provider whether the transaction really happened.

    Any missing configuration, network error, HTTP error, or mismatch returns ``confirmed: False`` with a
    reason; it never raises and never treats silence as approval.
    """
    result = {"attempted": False, "confirmed": False, "detail": "", "provider": ""}
    if verification_method != "api":
        result["detail"] = "Manual confirmation: no provider API is configured for this payment method."
        return result
    config = gateway_config if isinstance(gateway_config, dict) else {}
    endpoint = str(config.get("endpoint") or "").strip()
    api_key = str(config.get("api_key") or "").strip()
    if not endpoint or not api_key:
        result["detail"] = "Provider confirmation is enabled but not configured; the shop must confirm manually."
        return result
    problem = endpoint_problem(endpoint)
    if problem:
        # Reject an unsafe stored endpoint before any request is made. `attempted` stays False, so this
        # can never produce an auto-confirmed payment.
        result["detail"] = f"Provider endpoint was rejected ({problem}); manual confirmation is required."
        return result
    result.update({"attempted": True, "provider": str(config.get("provider") or "payment provider")[:60]})
    try:
        import httpx  # imported only when a gateway is actually configured

        timeout = max(1, min(30, int(config.get("timeout_seconds", 10) or 10)))
        response = httpx.post(
            endpoint,
            json={"method": method, "reference": reference, "amount": amount, "currency": "PHP"},
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
        )
        if response.status_code != 200:
            result["detail"] = f"The provider returned HTTP {response.status_code}; confirmation was not received."
            return result
        data = response.json()
        if not isinstance(data, dict):
            result["detail"] = "The provider returned an unexpected response; confirmation was not received."
            return result
        confirmed = data.get("confirmed") is True or str(data.get("status") or "").casefold() in {"success", "paid", "completed"}
        returned_reference = str(data.get("reference") or data.get("transaction_id") or "")
        returned_amount = _number(data.get("amount"))
        if not confirmed:
            result["detail"] = str(data.get("detail") or "The provider did not confirm this transaction.")[:300]
            return result
        if returned_reference and _key(returned_reference) != _key(reference):
            result["detail"] = "The provider confirmed a different reference number."
            return result
        if returned_amount is not None and abs(returned_amount - amount) > 0.005:
            result["detail"] = "The provider confirmed a different amount."
            return result
        result["confirmed"] = True
        result["detail"] = "Independently confirmed by the payment provider."
    except Exception as error:
        result["detail"] = f"Provider confirmation was unavailable: {type(error).__name__}. Manual confirmation is required."
    return result


def _duplicate_details(
    reference: str,
    receipt_hash: str,
    orders: list[Mapping[str, Any]],
    ignore_id: str | None,
    order_reference: str | None = None,
) -> list[str]:
    details: list[str] = []
    key = _key(reference)
    hash_key = _key(receipt_hash)
    for order in orders:
        if not isinstance(order, Mapping):
            continue
        reference_label = str(order.get("reference") or "")
        same_order = order_reference is not None and reference_label == str(order_reference)
        for payment in order.get("payments") or []:
            if key and _key(payment.get("reference")) == key:
                details.append(f"Reference is already recorded on order {reference_label}.")
        for item in order.get("payment_submissions") or []:
            if item.get("id") == ignore_id:
                continue
            superseded = item.get("status") == "receipt_requested" and same_order
            if key and not superseded and _key(item.get("reference")) == key:
                # Replacing a receipt with the same transaction reference is expected after a clearer-receipt
                # request on this order. A superseded receipt on another order still claims the reference.
                details.append(f"Reference was already submitted on order {reference_label}.")
            if hash_key and _key(item.get("receipt_hash")) == hash_key:
                # The exact same file is never a clearer receipt, even when its old reference was released.
                details.append(f"The same receipt file was already submitted on order {reference_label}.")
    return list(dict.fromkeys(details))


def build_verification(
    order: Mapping[str, Any],
    submission: Mapping[str, Any],
    ocr: Any,
    *,
    method: str,
    required_amount: float,
    customer_name: str = "",
    recipient_expected: str = "",
    ocr_reference: str = "",
    verification_method: str = "manual",
    gateway_config: Any = None,
    orders: list[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return an additive verification record for one submission.

    ``status`` is advisory workflow metadata. Only ``auto_confirm`` (set when a provider independently
    confirms a clean match) can advance money without an administrator, and the caller still uses the existing
    verified-payment service which enforces all amount/balance/receipt rules.
    """
    fields = (ocr.get("fields") if isinstance(ocr, Mapping) else getattr(ocr, "fields", {})) or {}
    confidence = float((ocr.get("confidence") if isinstance(ocr, Mapping) else getattr(ocr, "confidence", 0.0)) or 0.0)
    engine = (ocr.get("engine") if isinstance(ocr, Mapping) else getattr(ocr, "engine", "none")) or "none"
    flags = list((ocr.get("flags") if isinstance(ocr, Mapping) else getattr(ocr, "flags", [])) or [])
    notes = list((ocr.get("notes") if isinstance(ocr, Mapping) else getattr(ocr, "notes", [])) or [])
    checks: list[dict[str, Any]] = []

    def check(key: str, label: str, state: str, expected: Any, actual: Any, detail: str) -> None:
        checks.append({
            "key": key, "label": label, "state": state if state in CHECK_STATES else "pending",
            "expected": str(expected or ""), "actual": str(actual or ""), "detail": detail,
        })

    order_reference = str(order.get("reference") or "")
    duplicates = _duplicate_details(
        str(submission.get("reference") or ""), str(submission.get("receipt_hash") or ""), orders or [], str(submission.get("id") or ""),
        order_reference=order_reference,
    )
    if ocr_reference and _key(ocr_reference) != _key(submission.get("reference")):
        duplicates += _duplicate_details(
            ocr_reference, "", orders or [], str(submission.get("id") or ""), order_reference=order_reference
        )
    duplicates = list(dict.fromkeys(duplicates))
    if duplicates:
        check("duplicate", "Duplicate reference or receipt", "inconsistent", "Unique reference and receipt", "Already used", "; ".join(duplicates))
    else:
        check("duplicate", "Duplicate reference or receipt", "match", "Unique reference and receipt", "Not seen before", "No earlier payment or submission uses this reference or receipt.")

    ocr_amount = _number(fields.get("amount"))
    already_paid = round(sum(
        max(0.0, _number(payment.get("amount")) or 0.0)
        for payment in (order.get("payments") or []) if isinstance(payment, Mapping)
    ), 2)
    calculation = down_payment_calculation(order, required_amount, ocr_amount, already_paid)
    if ocr_amount is None:
        check("amount", "Transaction amount", "missing", f"PHP {required_amount:,.2f}", "", "OCR could not read an amount from the receipt.")
    elif abs(ocr_amount - required_amount) <= 0.005:
        check("amount", "Transaction amount", "match", f"PHP {required_amount:,.2f}", f"PHP {ocr_amount:,.2f}", "The extracted amount matches the amount due for this stage.")
    elif ocr_amount < required_amount - 0.005:
        check("amount", "Transaction amount", "inconsistent", f"PHP {required_amount:,.2f}", f"PHP {ocr_amount:,.2f}",
              f"Insufficient payment: the receipt is short by PHP {calculation['insufficient_by']:,.2f}.")
    else:
        detail = (
            f"PHP {calculation['stage_excess_amount']:,.2f} more than the amount due for this stage; "
            "the full amount will still be credited when the shop confirms it."
        )
        if calculation["excess_amount"] > 0.005:
            detail += (
                f" PHP {calculation['excess_amount']:,.2f} is above the whole order and needs admin review."
            )
        check("amount", "Transaction amount", "inconsistent", f"PHP {required_amount:,.2f}", f"PHP {ocr_amount:,.2f}", detail)

    # Cross-check the receipt against the customer's submitted record as well as the server-calculated amount.
    submitted_amount = _number(submission.get("amount"))
    if ocr_amount is None or submitted_amount is None:
        check("receipt_vs_submission", "Receipt vs submitted amount", "missing", "Both amounts readable", "", "One of the amounts could not be read.")
    elif abs(ocr_amount - submitted_amount) <= 0.005:
        check("receipt_vs_submission", "Receipt vs submitted amount", "match", f"PHP {submitted_amount:,.2f}", f"PHP {ocr_amount:,.2f}",
              "The receipt amount agrees with the amount on the customer's payment record.")
    else:
        check("receipt_vs_submission", "Receipt vs submitted amount", "inconsistent", f"PHP {submitted_amount:,.2f}", f"PHP {ocr_amount:,.2f}",
              "The receipt amount differs from the amount on the customer's payment record.")

    # The 50% down payment itself (not just this stage) is checked with earlier verified payments included.
    if ocr_amount is None:
        check("down_payment", "Required down payment", "missing", f"PHP {calculation['required_down_payment']:,.2f}", "",
              "OCR could not read the amount, so the down payment progress cannot be calculated.")
    elif calculation["remaining_down_payment"] <= 0.005:
        check("down_payment", "Required down payment", "match", f"PHP {calculation['required_down_payment']:,.2f}",
              f"PHP {calculation['cumulative_paid']:,.2f} paid in total", "Verified payments cover the required down payment.")
    else:
        check("down_payment", "Required down payment", "inconsistent",
              f"PHP {calculation['required_down_payment']:,.2f}", f"PHP {calculation['cumulative_paid']:,.2f} paid in total",
              f"PHP {calculation['remaining_down_payment']:,.2f} of the down payment is still due.")

    extracted_recipient = str(fields.get("recipient") or "").strip()
    if not recipient_expected:
        check("recipient", "Recipient account", "missing", "Shop account configured", extracted_recipient or "not read", "The shop has not configured an account name for this payment method.")
    elif not extracted_recipient:
        check("recipient", "Recipient account", "missing", recipient_expected, "", "OCR could not read who received the payment.")
    elif _key(extracted_recipient) == _key(recipient_expected) or _key(recipient_expected) in _key(extracted_recipient):
        check("recipient", "Recipient account", "match", recipient_expected, extracted_recipient, "The extracted recipient matches the shop's configured account.")
    else:
        check("recipient", "Recipient account", "inconsistent", recipient_expected, extracted_recipient, "The extracted recipient does not match the shop's configured account.")

    extracted_method = str(fields.get("method") or "").strip()
    if not extracted_method:
        check("method", "Payment method on receipt", "missing", method, "", "OCR could not identify the payment method; the customer's selection is used.")
    elif _key(extracted_method) == _key(method):
        check("method", "Payment method on receipt", "match", method, extracted_method, "The method on the receipt matches the selected method.")
    else:
        check("method", "Payment method on receipt", "inconsistent", method, extracted_method, "The method on the receipt does not match the selected method.")

    date_value = parse_date(str(fields.get("date_time") or ""))
    if date_value is None:
        check("date", "Transaction date", "missing", "A valid recent date", fields.get("date_time", ""), "OCR could not read a valid transaction date.")
    else:
        today = now or datetime.now(timezone.utc).replace(tzinfo=None)
        if date_value > today + timedelta(days=1):
            check("date", "Transaction date", "inconsistent", "Not in the future", fields.get("date_time", ""), "The receipt date is in the future.")
        elif date_value < today - timedelta(days=60):
            check("date", "Transaction date", "inconsistent", "Within the last 60 days", fields.get("date_time", ""), "The receipt is too old for this payment.")
        else:
            check("date", "Transaction date", "match", "Recent valid date", fields.get("date_time", ""), "The receipt date is valid.")

    reference_text = str(fields.get("reference") or "").strip()
    if len(_key(reference_text)) < 4:
        check("reference", "Reference on receipt", "missing", "At least 4 characters", reference_text, "OCR could not read a complete reference number.")
    elif _key(reference_text) == _key(submission.get("reference")):
        check("reference", "Reference on receipt", "match", submission.get("reference"), reference_text, "The receipt reference matches the reference entered by the customer.")
    else:
        check("reference", "Reference on receipt", "inconsistent", submission.get("reference"), reference_text, "The receipt reference differs from the reference entered by the customer.")

    sender = str(fields.get("sender") or "").strip()
    raw_text = str((ocr.get("text_preview") if isinstance(ocr, Mapping) else getattr(ocr, "raw_text", "")) or "")
    order_ref = str(order.get("reference") or "")
    if sender and customer_name and _key(sender) != _key(customer_name):
        check("association", "Customer and order", "inconsistent", customer_name, sender, "The sender on the receipt is not the customer who owns this order.")
    elif not customer_name or (sender or order_ref and _key(order_ref) in _key(raw_text)):
        check("association", "Customer and order", "match", customer_name or "Order owner", sender or "order reference present", "No conflicting customer or order information was found.")
    else:
        check("association", "Customer and order", "missing", customer_name or "Order owner", sender or "not read", "OCR could not link the receipt to this customer or order.")

    present = sum(1 for key in ("reference", "amount", "date_time", "sender", "recipient", "method") if str(fields.get(key) or "").strip())
    if present >= 5 and confidence >= 0.5:
        check("completeness", "Receipt information", "match", "Key transaction details present", f"{present} of 6 fields", "Enough information was extracted to compare with the order.")
    else:
        check("completeness", "Receipt information", "missing", "Key transaction details present", f"{present} of 6 fields", "The receipt is incomplete or unreadable; an administrator must review it.")

    ocr_status = str(fields.get("status") or "")
    invalid_status = ocr_status.casefold() == "failed" or "failed_status" in flags
    if invalid_status:
        check("status", "Receipt status", "inconsistent", "Successful transaction", ocr_status or "failed", "The receipt itself says the transaction failed or was reversed.")
    elif ocr_status:
        check("status", "Receipt status", "match", "Successful transaction", ocr_status, "The receipt reports a successful transaction (not proof of received funds).")
    else:
        check("status", "Receipt status", "missing", "Successful transaction", "", "OCR could not read the transaction status.")

    provider = confirm_with_provider(method, verification_method, gateway_config, str(submission.get("reference") or ""), float(required_amount))
    states = [item["state"] for item in checks if item["key"] != "duplicate"]
    if duplicates:
        status = DUPLICATE
    elif invalid_status or (provider["attempted"] and provider["detail"].startswith("The provider did not confirm")):
        status = REJECTED
    elif "inconsistent" in states or "missing" in states or "engine_unavailable" in flags or "unreadable" in flags:
        status = ADMIN_REVIEW
    elif provider["confirmed"]:
        status = VERIFIED
    else:
        status = PENDING

    check_states = {item["key"]: item["state"] for item in checks}
    issues: list[str] = []
    if duplicates:
        issues.append(RESULT_DUPLICATE)
    if invalid_status or (provider["attempted"] and provider["detail"].startswith("The provider did not confirm")):
        issues.append(RESULT_REJECTED)
    if check_states.get("reference") == "inconsistent":
        issues.append(RESULT_REFERENCE_MISMATCH)
    if calculation["insufficient_by"] > 0 and check_states.get("amount") == "inconsistent":
        issues.append(RESULT_INSUFFICIENT)
    if (
        "inconsistent" in states or "missing" in states
        or "engine_unavailable" in flags or "unreadable" in flags or "ocr_error" in flags
    ):
        issues.append(RESULT_MANUAL_REVIEW)
    result = next(
        (code for code in (RESULT_DUPLICATE, RESULT_REJECTED, RESULT_REFERENCE_MISMATCH, RESULT_INSUFFICIENT, RESULT_MANUAL_REVIEW) if code in issues),
        RESULT_VERIFIED if provider["confirmed"] and status == VERIFIED else RESULT_OCR_PENDING,
    )
    basis = "provider_api" if provider["attempted"] else "ocr_only"
    basis_label = (
        "Independently confirmed by the payment provider"
        if result == RESULT_VERIFIED and provider["confirmed"]
        else "OCR-checked · pending manual verification"
    )

    auto_confirm = status == VERIFIED and not duplicates and provider["attempted"]
    detail = {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "result": result,
        "result_label": RESULT_LABELS[result],
        "issues": list(dict.fromkeys(issues)),
        "checks": checks,
        "confidence": round(confidence, 2),
        "engine": engine,
        "flags": flags,
        "notes": notes,
        "receipt_hash": str(submission.get("receipt_hash") or ""),
        "verification_method": "provider_api" if provider["attempted"] else "manual",
        "verification_basis": basis,
        "verification_basis_label": basis_label,
        "down_payment": calculation,
        "provider": provider,
        "auto_confirm": bool(auto_confirm),
        "requires_admin": status in {ADMIN_REVIEW, DUPLICATE, REJECTED} or (status == PENDING and not provider["attempted"]),
        "reviewed_at": None,
        "reviewed_by": "",
    }
    return detail


def comparison_view(order: Mapping[str, Any], submission: Mapping[str, Any], verification: Mapping[str, Any]) -> dict[str, Any]:
    """Rows for the admin side-by-side comparison: what the receipt says vs what the system expects."""
    ocr = submission.get("ocr") if isinstance(submission.get("ocr"), Mapping) else {}
    fields = ocr.get("fields", {}) if isinstance(ocr, Mapping) else {}
    expected = {
        "reference": str(submission.get("reference") or ""),
        "amount": f"PHP {float(submission.get('amount') or 0):,.2f}",
        "date_time": "Recent transaction date",
        "sender": str((order.get("customer") or {}).get("name") or ""),
        "recipient": next((item["expected"] for item in verification.get("checks", []) if item["key"] == "recipient"), ""),
        "method": str(submission.get("method") or ""),
        "status": "Successful transaction",
        "account_info": "Masked for privacy",
    }
    states = {item["key"]: item["state"] for item in verification.get("checks", [])}
    labels = {
        "reference": "Reference number", "amount": "Amount", "date_time": "Date and time", "sender": "Sender",
        "recipient": "Recipient", "method": "Payment method", "status": "Status", "account_info": "Account / wallet",
    }
    rows = []
    for key, label in labels.items():
        actual = str(fields.get(key) or "") or "Not read"
        state = states.get(key, "pending" if key == "account_info" else "missing")
        rows.append({"key": key, "label": label, "receipt_value": actual, "system_value": expected.get(key, ""), "state": state})
    return {
        "rows": rows,
        "receipt_url": str((submission.get("proof") or {}).get("url") or "") if isinstance(submission.get("proof"), Mapping) else "",
        "verification_status": verification.get("status", ""),
        "verification_label": verification.get("status_label", ""),
        "verification_result": verification.get("result", ""),
        "verification_result_label": verification.get("result_label", verification.get("status_label", "")),
        "verification_basis_label": verification.get("verification_basis_label", ""),
        "reviewed_at": verification.get("reviewed_at"),
        "reviewed_by": str(verification.get("reviewed_by") or ""),
        "down_payment": verification.get("down_payment", {}),
        "confidence": verification.get("confidence", 0.0),
    }
