"""Optional OCR for payment receipts.

OCR only *recognizes text*. It never verifies money, changes an order total, or counts as proof of payment.
The matching service decides what the recognized text means; an administrator or an independent provider
confirmation decides whether money was actually received.

The engine is deliberately lazy and optional. If Tesseract/Pillow is missing, a PDF cannot be read, or the
receipt is unreadable, ``extract_receipt`` returns an empty structured result instead of raising: the normal
admin review queue continues to work unchanged.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from backend.app.core.config import settings


logger = logging.getLogger("printshop.ocr")

OCR_FIELDS = ("reference", "date_time", "sender", "recipient", "method", "amount", "status", "account_info")
RECEIPT_MIME_TYPES = {"image/jpeg", "image/png", "application/pdf"}


@dataclass
class OCRResult:
    """Structured, display-safe result of scanning one receipt."""

    fields: dict[str, str] = field(default_factory=lambda: {key: "" for key in OCR_FIELDS})
    confidence: float = 0.0
    engine: str = "none"
    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    raw_text: str = ""

    def view(self) -> dict[str, Any]:
        """The only shape returned to clients. Account details are already masked by the parser."""
        return {
            "fields": dict(self.fields),
            "confidence": round(float(self.confidence), 2),
            "engine": self.engine,
            "notes": list(self.notes),
            "flags": list(self.flags),
            "text_preview": self.raw_text[:400],
        }


class OCRProvider:
    name = "none"

    def available(self) -> bool:
        return False

    def extract_text(self, contents: bytes, mime_type: str) -> str:  # pragma: no cover - interface
        return ""


class NoEngineProvider(OCRProvider):
    name = "none"


class TesseractProvider(OCRProvider):
    """Lazy Tesseract provider; importing and loading the engine happens only when a receipt is scanned."""

    name = "tesseract"

    def __init__(self) -> None:
        self._modules: tuple[Any, Any] | None = None
        self._checked = False

    def available(self) -> bool:
        if settings.ocr_engine not in {"auto", "tesseract"}:
            return False
        if not self._checked:
            self._checked = True
            try:
                import pytesseract  # type: ignore
                from PIL import Image  # type: ignore

                pytesseract.get_tesseract_version()
                self._modules = (pytesseract, Image)
            except Exception as error:  # missing package or missing system binary
                logger.info("OCR engine unavailable; receipts will use admin review: %s", error)
                self._modules = None
        return self._modules is not None

    def extract_text(self, contents: bytes, mime_type: str) -> str:
        if not self.available() or self._modules is None:
            return ""
        if mime_type == "application/pdf":
            # Tesseract reads images. PDFs may contain a text layer; read that when a reader is installed.
            try:
                from pypdf import PdfReader  # type: ignore

                reader = PdfReader(io.BytesIO(contents))
                return "\n".join(page.extract_text() or "" for page in reader.pages[:3])
            except Exception as error:
                logger.info("PDF receipt text could not be read: %s", error)
                return ""
        pytesseract, image_module = self._modules
        image = image_module.open(io.BytesIO(contents))
        image = image.convert("RGB")
        # Large screenshots slow OCR down; the stored original is untouched.
        if max(image.size) > 2400:
            image.thumbnail((2400, 2400))
        return str(pytesseract.image_to_string(image, lang=settings.ocr_language, timeout=settings.ocr_timeout_seconds) or "")


_provider: OCRProvider = TesseractProvider()


def set_provider(provider: OCRProvider | None) -> None:
    """Replace the provider (used by tests and by a future hosted OCR integration)."""
    global _provider
    _provider = provider or NoEngineProvider()


_AMOUNT_RE = re.compile(
    r"(?:(?:[₱PHPp]\s*)|(?:amount|total|paid|value)\s*[:=]?\s*)([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})|[0-9]+(?:\.[0-9]{2}))",
    re.IGNORECASE,
)
_REFERENCE_RES = (
    # A label with a delimiter ("Reference: ABC123", "Txn No. ABC123").
    re.compile(r"\b(?:reference|ref|transaction|txn|receipt)\b[ \t]*(?:no\.?|number|id|#)?[ \t]*[:#=.\-][ \t]*([A-Z0-9][A-Z0-9\-]{5,30})\b", re.I),
    # A label followed by a space ("Reference ABC123"); [ \t] never crosses the newline into the next label.
    re.compile(r"\b(?:reference|ref|transaction|txn|receipt)\b[ \t]+(?:no\.?|number|id|#)?[ \t]+([A-Z0-9][A-Z0-9\-]{5,30})\b", re.I),
    re.compile(r"(?:^|\s)#([A-Z0-9][A-Z0-9\-]{5,30})", re.I | re.M),
)
_DATE_RE = re.compile(
    r"((?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]20\d{2})"
    r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)?)"
)
_SENDER_RE = re.compile(r"(?:sender|from|name|account holder|customer)\s*[:#]?\s*([A-Z][A-Za-z0-9 .,'\-]{2,49})", re.I)
_RECIPIENT_RE = re.compile(
    r"\b(?:recipient|received by|beneficiary|payee|merchant|paid to|to)\b[ \t]*[:#]?[ \t]*([A-Z][A-Za-z0-9 .,'\-]{2,69})", re.I
)
_ACCOUNT_RE = re.compile(
    r"(?:(?:account|acct|wallet|mobile|phone|number|no\.?)\s*[:#]?\s*)"
    r"((?:\+?\d[\d\s().\-]{5,}\d)|(?:\*{2,}\d{2,})|(?:\d{3,}\*{2,}\d{2,}))",
    re.I,
)
_METHOD_PATTERNS = (
    (re.compile(r"\bgcash\b|\bg-cash\b", re.I), "GCash"),
    (re.compile(r"\bmaya\b", re.I), "Maya"),
    (re.compile(r"\bbank transfer\b|\bfund transfer\b|\binstapay\b|\bpesonet\b", re.I), "Bank Transfer"),
    (re.compile(r"\b(bdo|bpi|unionbank|landbank|metrobank|security bank|rcbc|chinabank)\b", re.I), "Bank Transfer"),
)
_SUCCESS_RE = re.compile(r"\b(success|successful|completed|paid|received|approved)\b", re.I)
_PENDING_RE = re.compile(r"\b(pending|processing|in progress)\b", re.I)
_FAILED_RE = re.compile(r"\b(failed|cancelled|canceled|declined|reversed|expired)\b", re.I)
_WEIGHTS = {"reference": 0.25, "amount": 0.25, "date_time": 0.15, "sender": 0.10, "recipient": 0.10, "method": 0.10, "status": 0.05}


def mask_account(value: str) -> str:
    """Keep at most the first and last four characters, never a complete wallet or bank number."""
    text = str(value or "").strip()
    if not text:
        return ""
    digits = [character for character in text if character.isdigit()]
    if len(digits) <= 4:
        return "*" * max(0, len(text) - 4) + text[-4:]
    prefix = "".join(character for character in text[:4] if character.isdigit())
    suffix = "".join(character for character in text[-4:] if character.isdigit())
    return f"{prefix}****{suffix}"


def _clean_line(value: str, limit: int = 80) -> str:
    return re.sub(r"\s+", " ", value).strip(" :#=-")[:limit]


def parse_text(text: str, engine: str = "parse") -> OCRResult:
    """Turn arbitrary receipt text into structured fields. Never raises."""
    result = OCRResult(engine=engine, raw_text=(text or "")[:2000])
    clean = re.sub(r"[ \t]+", " ", str(text or ""))
    if not clean.strip():
        result.notes.append("No text could be read from the receipt.")
        result.flags.append("unreadable")
        return result

    for patterns in _REFERENCE_RES:
        match = patterns.search(clean)
        if match:
            result.fields["reference"] = _clean_line(match.group(1), 40).upper()
            break

    amounts: list[float] = []
    for match in _AMOUNT_RE.finditer(clean):
        try:
            amounts.append(float(match.group(1).replace(",", "")))
        except ValueError:
            continue
    if amounts:
        result.fields["amount"] = f"{amounts[0]:.2f}"
        if any(value != amounts[0] for value in amounts):
            result.flags.append("multiple_amounts")

    match = _DATE_RE.search(clean)
    if match:
        result.fields["date_time"] = _clean_line(match.group(1), 40)

    match = _SENDER_RE.search(clean)
    if match:
        result.fields["sender"] = _clean_line(match.group(1))
    match = _RECIPIENT_RE.search(clean)
    if match:
        result.fields["recipient"] = _clean_line(match.group(1))

    for pattern, label in _METHOD_PATTERNS:
        if pattern.search(clean):
            result.fields["method"] = label
            break

    if _FAILED_RE.search(clean):
        result.fields["status"] = "Failed"
        result.flags.append("failed_status")
    elif _PENDING_RE.search(clean):
        result.fields["status"] = "Pending"
    elif _SUCCESS_RE.search(clean):
        result.fields["status"] = "Success"
    if len(_SUCCESS_RE.findall(clean)) > 1 and _FAILED_RE.search(clean):
        result.flags.append("conflicting_status")

    accounts = [_clean_line(item.group(1), 40) for item in _ACCOUNT_RE.finditer(clean)]
    if accounts:
        result.fields["account_info"] = mask_account(accounts[0])

    score = sum(weight for key, weight in _WEIGHTS.items() if result.fields[key])
    result.confidence = round(min(1.0, score), 2)
    if result.confidence < 0.5:
        result.flags.append("low_confidence")
    return result


def receipt_hash(contents: bytes) -> str:
    """A stable fingerprint used only to flag the same file submitted again."""
    return hashlib.sha256(contents).hexdigest()


def extract_receipt(contents: bytes, mime_type: str, *, name: str = "") -> OCRResult:
    """Scan one receipt without ever interrupting the payment flow."""
    result = OCRResult(engine=getattr(_provider, "name", "none"))
    if not contents or len(contents) > settings.ocr_max_bytes:
        result.notes.append("The receipt is too large to scan automatically; the shop will review it.")
        result.flags.append("unreadable")
        return result
    if mime_type not in RECEIPT_MIME_TYPES:
        result.notes.append("This file type cannot be scanned automatically; the shop will review it.")
        result.flags.append("unsupported_type")
        return result
    try:
        if not _provider.available():
            result.notes.append("Automatic scanning is not available; the shop will review this receipt.")
            result.flags.append("engine_unavailable")
            return result
        text = _provider.extract_text(contents, mime_type)
    except Exception as error:  # OCR must never crash a payment submission
        logger.warning("Receipt OCR failed for %s: %s", name or "upload", error)
        result.notes.append("The receipt could not be read automatically; the shop will review it.")
        result.flags.append("ocr_error")
        return result
    parsed = parse_text(text, engine=result.engine)
    if mime_type == "application/pdf" and not text.strip():
        parsed.notes.append("No text layer was found in this PDF; the shop will review it.")
    return parsed
