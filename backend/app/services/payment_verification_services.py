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


VERIFIED = "verified"
PENDING = "pending"
ADMIN_REVIEW = "admin_review"
REJECTED = "rejected"
DUPLICATE = "possible_duplicate"

STATUS_LABELS = {
    VERIFIED: "Verified",
    PENDING: "Pending Verification",
    ADMIN_REVIEW: "For Admin Review",
    REJECTED: "Rejected",
    DUPLICATE: "Possible Duplicate",
}
CHECK_STATES = {"match", "missing", "inconsistent", "pending"}


def _key(value: Any) -> str:
    return re.sub(r"[\s\-_]+", "", str(value or "")).casefold()


def _number(value: Any) -> float | None:
    try:
        return round(float(str(value).replace(",", "")), 2)
    except (TypeError, ValueError):
        return None


def parse_date(value: str) -> datetime | None:
    """Best-effort parsing of the common receipt date formats (naive values are treated as local time)."""
    text = str(value or "").strip()
    if not text:
        return None
    for pattern, order in (
        (r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", "ymd"),
        (r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", "dmy"),
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        first, second, third = (int(item) for item in match.groups())
        year, month, day = (first, second, third) if order == "ymd" else (third, second, first)
        try:
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
            return None
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
    if ocr_amount is None:
        check("amount", "Transaction amount", "missing", f"PHP {required_amount:,.2f}", "", "OCR could not read an amount from the receipt.")
    elif abs(ocr_amount - required_amount) <= 0.005:
        check("amount", "Transaction amount", "match", f"PHP {required_amount:,.2f}", f"PHP {ocr_amount:,.2f}", "The extracted amount matches the amount due for this stage.")
    else:
        check("amount", "Transaction amount", "inconsistent", f"PHP {required_amount:,.2f}", f"PHP {ocr_amount:,.2f}", "The extracted amount does not match the amount due.")

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

    auto_confirm = status == VERIFIED and not duplicates and provider["attempted"]
    detail = {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "checks": checks,
        "confidence": round(confidence, 2),
        "engine": engine,
        "flags": flags,
        "notes": notes,
        "receipt_hash": str(submission.get("receipt_hash") or ""),
        "verification_method": "provider_api" if provider["attempted"] else "manual",
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
        "confidence": verification.get("confidence", 0.0),
    }
