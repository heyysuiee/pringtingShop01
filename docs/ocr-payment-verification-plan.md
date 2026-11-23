# OCR-Assisted Payment Verification — Implementation Plan

Date: 2026-11-23 · Baseline: existing FastAPI + SQLite/Turso application, 300 tests passing.

## Guiding rule

OCR recognizes text. Matching compares extracted text with the order and shop settings. Only an independent payment-provider confirmation or an administrator's explicit confirmation may move money into the verified payment totals. A screenshot, OCR confidence, or a matching amount is never proof of receipt.

The repository already runs on SQLite locally and Turso in production, not MySQL. This plan keeps that storage layer and the existing nested `payments` / `payment_submissions` order records; no MySQL migration or separate payment database is introduced.

## 1. Backend: OCR and matching engine

- Add `backend/app/services/ocr_services.py` with a provider interface, a lazy Tesseract/Pillow provider, a no-engine fallback, byte/page caps, and structured receipt parsing for GCash, Maya, and bank receipts.
- Extract reference, date/time, sender, recipient, method, amount, status, and masked account/wallet information. Use layout-tolerant regular expressions and return a confidence score plus flags; never throw on unreadable input.
- Add `backend/app/services/payment_verification_services.py` to compare OCR output with the server-computed due amount, active payment method, configured recipient, existing references, receipt hash, date, customer name, and order reference. Produce a stable classification: `Verified`, `Pending Verification`, `For Admin Review`, `Rejected`, or `Possible Duplicate`.
- Add an optional generic provider-confirmation client. Only a positive independent API response may auto-confirm; absence, error, or mismatch leaves the submission pending for an admin.

## 2. Backend: data and APIs

- Extend each `payment_submissions` record (no new top-level order migration) with OCR result, receipt hash, verification checks, verification status/method, provider confirmation, and admin notes/history. Historical submissions remain append-only.
- Add customer scan endpoint `POST /api/orders/{reference}/payments/scan` for pre-submission OCR feedback. Add the inline body cap for this route.
- Keep `POST /api/orders/{reference}/payments` as the authoritative submit path; enrich its response/order view with OCR and verification fields.
- Add admin endpoints to request a clearer receipt, append verification notes, and view the OCR comparison. Keep existing verify/reject paths and their `expected_version` checks.
- Extend the admin payments listing with extracted reference/amount, required amount, OCR confidence, verification status, and submission date.

## 3. Payment-method settings

- Add additive `payment_methods` columns for preferred verification method and optional gateway configuration (provider, endpoint, key reference). Keep secrets out of public responses.
- Expose only active methods and non-secret metadata to customers; keep account names/numbers/instructions and QR behavior unchanged.
- Add admin settings controls for the new fields without changing provider uniqueness or the at-least-one-active-method rule.

## 4. Customer UI

- In the customer portal, render active methods with names/QR, instructions, account details, amount due, down payment, balance, and payment purpose.
- Add a receipt preview with Replace/Remove, derived `accept` types from server metadata, and an OCR scan status region. Prefill the reference when confidently detected, but never prefill or override the server-calculated amount.
- Show pending/scan/review/duplicate statuses and notifications in the payment panel, action strip, tracking, and notification center. Use wording that distinguishes OCR from confirmed payment.

## 5. Admin UI

- Add OCR columns to the payment queue and an OCR review action inside the existing order modal verification form.
- Use a stacked dialog for a side-by-side receipt / extracted-data comparison with match, missing, and inconsistent highlighting.
- Add `Request clearer receipt`, `Add verification note`, `Approve`, and `Reject` actions. Continue using the shared `submitAction()` path for optimistic versioning, audit, notifications, and refresh.
- Add focused CSS for the OCR review layout and status badges.

## 6. Notifications, audit, and security

- Add customer notifications for upload received, processing, verified, additional verification required, receipt rejected, and clearer receipt requested. Use dedupe keys.
- Audit every new admin action and OCR-related state change with the existing append-only order audit log.
- Validate file type/signature/size using existing upload code; store receipts only through existing secure storage; resolve files only through the owning order; mask account/wallet details in API responses; hash receipts to flag duplicates.

## 7. Verification

- New `backend/tests/test_ocr_payment_verification.py` covering all 14 required scenarios with OCR and provider calls stubbed, including failed OCR and provider-unavailable cases.
- `backend/tests/test_payment_verification_results.py` covers the customer-facing result codes, the server-calculated money figures, the admin payment table fields/filters, the verification modal source, successful/insufficient/duplicate/mismatched/unreadable receipts, overpayment, and accumulating partial payments.
- Re-run the full backend suite, `node --check` on changed JavaScript, page/static smoke checks, and static reference scan.
- Document the OCR engine configuration and remaining limitations in the implementation report.

## 8. Result statuses and the payment verification record

The internal `status` codes (`verified`, `pending`, `admin_review`, `possible_duplicate`, `rejected`) are preserved for older submissions and reports. New submissions also carry a first-class `result` code and label:

| Result code | Label | Meaning |
| --- | --- | --- |
| `verified` | Verified | An independent provider confirmed the transaction (or an admin explicitly recorded the money). |
| `ocr_checked_pending` | OCR-checked · pending manual verification | OCR read the receipt cleanly; no money has been credited. |
| `insufficient_payment` | Insufficient Payment | The extracted amount is below the server-calculated amount due. |
| `reference_mismatch` | Reference Number Mismatch | The reference on the receipt differs from the customer's submitted reference. |
| `duplicate_reference` | Duplicate Reference Number | The reference or receipt file was already used on another payment/submission. |
| `needs_manual_review` | Needs Manual Review | OCR was unreadable, incomplete, inconsistent, or the engine failed. |
| `rejected` | Rejected | The receipt reports a failed transaction or the provider explicitly denied it. |

`build_verification()` stores a `down_payment` calculation with every new OCR result. The figures are recomputed from the confirmed order total and earlier verified payments (never from browser input): total order amount, required down payment (50% or the admin-set amount), stage amount due, actual amount paid, previously paid, cumulative paid, remaining down payment, remaining balance, insufficient by, excess, payment percentage, down payment percentage, and whether the down payment has been met. Older records are recomputed when read.

Every row in the admin payments table opens a dedicated verification modal. It shows the original receipt beside the extracted reference, amount, date/time, sender, provider, recipient, status and confidence, followed by the automatic down payment calculation, validation checks, and the receipt-vs-system comparison. Only verified payments have `counts_as_paid = true`; partial admin confirmations accumulate, and a submission can never be credited twice. OCR alone never sets `auto_confirm`.
