# Safe procedure for GCash, Maya, and bank receipt accuracy testing

Status: **NOT EXECUTED**. No authorized or redacted real receipts were available. Real-world accuracy is `NOT TESTED`.

The existing 100% result applies only to three synthetic, high-resolution, clearly watermarked receipts. Do not present it as real-world accuracy.

## Before starting

- Obtain explicit permission from the account owner and the shop for every receipt used.
- Use copies only. Never move originals into the repository.
- Store samples in a temporary folder outside version control, for example `%TEMP%\ocr-receipt-samples`.
- Do not use real customer orders, real money, or live payment references in the test database.
- Use the copied preview Turso database, never the production database.

## Redaction checklist

Keep the fields OCR must read:

- Reference / transaction number
- Amount
- Date and time
- Sender label and recipient / merchant name
- Payment method branding or name
- Status text such as Success / Completed

Redact or replace:

- Customer name, phone, email, and address — replace consistently with an alias such as `Test Customer`.
- Full wallet, mobile, card, or bank numbers — keep only the last four digits, for example `0917****4821`.
- QR codes, barcodes, signatures, and one-time codes.
- Other transactions or unrelated personal data visible on the same screenshot.
- Any production order reference that is not part of the synthetic test order.

The receipt must still look like a real receipt after redaction. Over-redaction is acceptable to record as `NOT READABLE`; it is not an OCR failure.

## Per-provider test set

Recommended minimum: 10 authorized/redacted samples per provider.

| Provider | Samples | Include |
| --- | --- | --- |
| GCash | 10 | app screenshot, receipt page, different reference formats, compressed image |
| Maya | 10 | app screenshot, receipt page, different reference formats, compressed image |
| Bank Transfer | 10 | bank confirmation, fund transfer, InstaPay/Pesonet-style reference |

Also include 2–3 deliberately difficult samples per provider: low light, glare, partial crop, old/low-resolution screenshot, and scanned PDF without a text layer. Record these separately from normal samples.

## Execution

1. Start the app against the copied preview database with `PRINTSHOP_FILE_STORAGE=database`.
2. Create one synthetic customer and order per test with an amount that matches the receipt amount.
3. Upload each redacted receipt through the customer scan endpoint.
4. Record the structured OCR result and verification checks.
5. Submit the receipt only for tests that specifically exercise persistence/admin review.
6. Explicitly verify as administrator only when testing the admin verification path. OCR confidence alone must never record money.
7. After every test, record:
   - order total
   - amount recorded as money
   - remaining balance
   - order status
8. Delete temporary receipts and synthetic orders after recording results.

## Accuracy recording template

Record field-level results, not just an overall impression.

| Provider | Sample | Reference | Amount | Date/time | Sender | Recipient | Method | Status | Confidence | Flags | Correct fields / 8 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GCash | 01 | correct/incorrect/not read | ... | ... | ... | ... | ... | ... | 0.00 | ... | 0 / 8 |

Provider summary:

| Provider | Correct / total fields | Accuracy | Notes |
| --- | ---: | ---: | --- |
| GCash | 0 / 0 | NOT TESTED | |
| Maya | 0 / 0 | NOT TESTED | |
| Bank Transfer | 0 / 0 | NOT TESTED | |

## Interpretation rules

- A field is correct only if it exactly matches the visible redacted value, ignoring case and whitespace.
- A masked account number is correct only if the visible masked form is preserved.
- A wrong amount or reference is a failure, even if confidence is high.
- `admin_review`, `unreadable`, `engine_unavailable`, `unsupported_type`, and `possible_duplicate` are safety outcomes, not payment confirmations.
- Do not enable provider auto-confirmation for this test. Manual administrator verification remains the default until a real provider API is integrated.

## Completion criteria

Real-world OCR accuracy can be reported only after all three provider rows contain actual sample counts. Until then, report **real-world OCR accuracy: NOT TESTED**.
