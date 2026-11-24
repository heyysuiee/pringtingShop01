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
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
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
    reference_candidates: list[dict[str, Any]] = field(default_factory=list)

    def view(self) -> dict[str, Any]:
        """The only shape returned to clients. Account details are already masked by the parser."""
        return {
            "fields": dict(self.fields),
            "confidence": round(float(self.confidence), 2),
            "engine": self.engine,
            "notes": list(self.notes),
            "flags": list(self.flags),
            "text_preview": self.raw_text[:400],
            "reference_candidates": [dict(item) for item in self.reference_candidates[:5]],
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

    def _resolve_tesseract_cmd(self, pytesseract: Any) -> None:
        configured = os.environ.get("PRINTSHOP_TESSERACT_CMD", "").strip()
        candidates = [configured] if configured else []
        which = shutil.which("tesseract")
        if which:
            candidates.append(which)
        candidates.extend([
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            "/usr/bin/tesseract",
            "/usr/local/bin/tesseract",
        ])
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                pytesseract.pytesseract.tesseract_cmd = candidate
                return

    def available(self) -> bool:
        if settings.ocr_engine not in {"auto", "tesseract"}:
            return False
        if not self._checked:
            self._checked = True
            try:
                import pytesseract  # type: ignore
                from PIL import Image  # type: ignore

                self._resolve_tesseract_cmd(pytesseract)
                pytesseract.get_tesseract_version()
                self._modules = (pytesseract, Image)
            except Exception as error:  # missing package or missing system binary
                logger.info("OCR engine unavailable; receipts will use admin review: %s", error)
                self._modules = None
        return self._modules is not None

    def _preprocess(self, image: Any) -> Any:
        from PIL import ImageEnhance, ImageOps  # type: ignore

        gray = ImageOps.grayscale(image)
        gray = ImageOps.autocontrast(gray, cutoff=1)
        width, height = gray.size
        if max(width, height) < 1200:
            scale = 2
            gray = gray.resize((width * scale, height * scale))
        gray = ImageEnhance.Sharpness(gray).enhance(1.5)
        gray = ImageEnhance.Contrast(gray).enhance(1.3)
        return gray

    def _ocr_image(self, pytesseract: Any, image: Any, config: str = "--oem 3") -> str:
        # Auto page segmentation (the default) preserves the column layout of e-wallet receipts. Forcing a
        # single text block (PSM 6) used to merge or drop the amount line on some receipts, so it is now only
        # a fallback when auto segmentation cannot find the payment fields.
        return str(pytesseract.image_to_string(image, lang=settings.ocr_language, timeout=settings.ocr_timeout_seconds, config=config) or "")

    @staticmethod
    def _text_score(text: str) -> tuple[int, int]:
        """Rank OCR readings by the payment fields they contain, then by amount of readable text."""
        parsed = parse_text(text, engine="tesseract")
        fields = parsed.fields
        useful = sum(1 for key in ("reference", "amount", "date_time", "method", "status") if fields.get(key))
        return useful, len(re.sub(r"\s+", "", text))

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
        try:
            preprocessed = self._preprocess(image)
            readings = [self._ocr_image(pytesseract, preprocessed)]
            best = max(readings, key=self._text_score)
            # Preprocessing can blur a faint amount, so compare it with the untouched screenshot.
            if self._text_score(best)[0] < 2:
                readings.append(self._ocr_image(pytesseract, image))
                best = max(readings, key=self._text_score)
            # PSM 6 is useful for very simple receipts, but it is no longer the default because it can drop
            # the amount line on multi-section e-wallet receipts. Try it only when the safer passes failed.
            if self._text_score(best)[0] < 2:
                readings.append(self._ocr_image(pytesseract, preprocessed, config="--oem 3 --psm 6"))
                best = max(readings, key=self._text_score)
            return best
        except Exception:
            return self._ocr_image(pytesseract, image)


_provider: OCRProvider = TesseractProvider()


def set_provider(provider: OCRProvider | None) -> None:
    """Replace the provider (used by tests and by a future hosted OCR integration)."""
    global _provider
    _provider = provider or NoEngineProvider()


# Receipts do not always print cents ("Amount 500", "₱1,000"). Accept whole amounts as well as one or two
# decimals, and match the full PHP token/standalone P without capturing unrelated letter sequences.
_AMOUNT_NUMBER = r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)"
_AMOUNT_LABEL = (
    r"(?:payment\s+amount|amount\s+paid|amount\s+due|amount\s+to\s+pay|total\s+amount|"
    r"amount|total|paid|value)\b[ \t]*[:=]?[ \t]*"
)
_AMOUNT_CURRENCY = r"(?:₱[ \t]*|PHP[ \t]*|(?<![A-Za-z])P(?=[ \t]*\d)[ \t]*)"
_AMOUNT_RE = re.compile(rf"(?:{_AMOUNT_CURRENCY}|{_AMOUNT_LABEL}){_AMOUNT_NUMBER}", re.IGNORECASE)
_REFERENCE_LABEL = r"(?:reference|ref(?:erence)?\s*(?:no|number|num|id|#)?|transaction|txn|receipt|payment|field)\s*(?:no\.?|number|num|id|#)?"
_REFERENCE_VALUE = r"([A-Z0-9][A-Z0-9\-/]{3,40})"
_REFERENCE_RES = (
    # A label with a delimiter ("Reference: ABC123", "Txn No. ABC123", "Field: 12345").
    re.compile(rf"\b({_REFERENCE_LABEL})[ \t]*[:#=.\-][ \t]*{_REFERENCE_VALUE}\b", re.I),
    # A label followed by a space ("Reference ABC123"); [ \t] never crosses the newline into the next label.
    re.compile(rf"\b({_REFERENCE_LABEL})[ \t]+{_REFERENCE_VALUE}\b", re.I),
    # GCash/Maya receipts often use only a short label ("Ref: 1234", "Txn 1234") or put the label below the value.
    re.compile(rf"\b({_REFERENCE_LABEL})[ \t]*\n[ \t]*{_REFERENCE_VALUE}\b", re.I),
    # A label with OCR-confused delimiter ("Ref 0 1234", "Reference - 1234").
    re.compile(rf"\b({_REFERENCE_LABEL})[ \t]+(?:0|O|o|\-)[ \t]*{_REFERENCE_VALUE}\b", re.I),
    re.compile(rf"(?:^|\s)#({_REFERENCE_VALUE})", re.I | re.M),
    # Some wallet receipts print only the reference number without a label.
    re.compile(rf"(?:^|\n)[ \t]*({_REFERENCE_VALUE})[ \t]*(?:\n|$)", re.M),
)
_NON_REFERENCE_CONTEXT = re.compile(
    r"\b(?:mobile|phone|contact|account|acct|wallet|card|number\s*to|send\s*to|amount|total|balance|due|fee|php|₱)\b",
    re.I,
)
_PHONE_LIKE = re.compile(r"^(?:\+?63|0)?9\d{9}$")
_MONTH_NAME = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_DATE_RE = re.compile(
    r"((?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]20\d{2}"
    rf"|{_MONTH_NAME}\.?\s+\d{{1,2}},?\s+20\d{{2}}"
    rf"|\d{{1,2}}\s+{_MONTH_NAME}\.?,?\s+20\d{{2}})"
    r"(?:[ ,T]\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)?)",
    re.I,
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
    (re.compile(r"\bmaribank\b|\bmari\s*bank\b", re.I), "MariBank"),
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


def _normalize_reference(value: str) -> str:
    cleaned = re.sub(r"\s+", "", str(value or "").strip().upper())
    cleaned = cleaned.strip("-/:#.=*")
    return cleaned[:40]


def _reference_candidates(text: str, expected_method: str = "") -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected = str(expected_method or "").strip().casefold()
    for pattern in _REFERENCE_RES:
        for match in pattern.finditer(text):
            groups = match.groups()
            if len(groups) == 2:
                label, raw_value = groups[0], groups[1]
            else:
                label, raw_value = "reference", groups[0]
            value = _normalize_reference(raw_value)
            if len(value) < 4:
                continue
            if _PHONE_LIKE.match(value):
                continue
            if value.replace(".", "", 1).isdigit() and len(value) <= 6:
                # likely an amount/short number, not a transaction reference
                continue
            context_start = max(0, match.start() - 35)
            context_end = min(len(text), match.end() + 35)
            context = text[context_start:context_end]
            if _NON_REFERENCE_CONTEXT.search(label) and not re.search(r"\bref|transaction|txn|receipt\b", str(label), re.I):
                continue
            if _NON_REFERENCE_CONTEXT.search(context) and re.search(r"\bmobile|phone|contact|account|acct|wallet|card|send\s*to\b", context, re.I):
                # Skip obvious account/mobile numbers captured by loose OCR around labels.
                if value.isdigit() and len(value) >= 10:
                    continue
            score = 0
            label_text = str(label or "").casefold()
            if "reference" in label_text or "ref" in label_text:
                score += 4
            if "transaction" in label_text or "txn" in label_text:
                score += 3
            if "receipt" in label_text:
                score += 2
            if any(ch.isalpha() for ch in value):
                score += 1
            if 8 <= len(value) <= 30:
                score += 1
            if expected and expected in context.casefold():
                score += 2
            if value in seen:
                continue
            seen.add(value)
            candidates.append({
                "value": value,
                "label": _clean_line(str(label or "Reference"), 40),
                "score": score,
            })
    candidates.sort(key=lambda item: (-int(item["score"]), -len(str(item["value"]))))
    return candidates


def parse_text(text: str, engine: str = "parse", expected_method: str = "") -> OCRResult:
    """Turn arbitrary receipt text into structured fields. Never raises."""
    result = OCRResult(engine=engine, raw_text=(text or "")[:2000])
    clean = re.sub(r"[ \t]+", " ", str(text or ""))
    if not clean.strip():
        result.notes.append("No text could be read from the receipt.")
        result.flags.append("unreadable")
        return result

    candidates = _reference_candidates(clean, expected_method=expected_method)
    result.reference_candidates = candidates
    if candidates:
        result.fields["reference"] = str(candidates[0]["value"])
        if len(candidates) > 1:
            result.flags.append("multiple_reference_candidates")
            result.notes.append("Multiple possible reference numbers were detected. Please confirm the correct one.")

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


def extract_receipt(contents: bytes, mime_type: str, *, name: str = "", expected_method: str = "") -> OCRResult:
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
    parsed = parse_text(text, engine=result.engine, expected_method=expected_method)
    if mime_type == "application/pdf" and not text.strip():
        parsed.notes.append("No text layer was found in this PDF; the shop will review it.")
    return parsed
