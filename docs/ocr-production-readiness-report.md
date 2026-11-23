# OCR-Assisted Payment Verification — Production Readiness Report

Date: 2026-11-23  
Scope: existing FastAPI + SQLite/Turso application, additive OCR-assisted receipt review. The OCR implementation was **not** rewritten or replaced.

Latest addendum: [ocr-live-deployment-checklist.md](ocr-live-deployment-checklist.md) records the final PASS / FAIL / NOT TESTED status. The live-preview procedure is in [vercel-preview-deployment-runbook.md](vercel-preview-deployment-runbook.md), and the real-receipt procedure is in [authorized-receipt-test-procedure.md](authorized-receipt-test-procedure.md).

## Verdict

The feature is safe to deploy in its intended mode: **OCR is advisory; money moves only after an explicit administrator confirmation or an independent provider confirmation.** The normal admin-review queue remains the fallback when OCR is unavailable, unreadable, or unsupported.

It is **not yet ready to claim real-world receipt-accuracy or unattended auto-confirmation**, because:

1. No authorized/redacted real GCash, Maya, or bank receipts were available in the repository.
2. Provider confirmation was tested only against a local mock, not a live GCash/Maya/bank API.
3. Live Turso was not touched; compatibility was checked with local SQLite and a local Hrana-compatible emulator.

The measured 100% accuracy below applies **only** to three synthetic, high-resolution, clearly watermarked receipts. It is OCR-engine validation, not proof of real payment accuracy.

## Environment verified

| Component | Result |
| --- | --- |
| Tesseract | `5.4.0.20240606`, installed at `C:\Program Files\Tesseract-OCR\tesseract.exe`, languages `eng`, `osd` |
| Pillow | `13.0.0` |
| pytesseract | `0.3.13` |
| pypdf | `5.9.0` |
| OCR provider availability | `True` with local Tesseract configured |
| Receipt sources | Synthetic files only; no real customer/payment data used |

All audit work used isolated temporary databases and upload directories. No repository, production, or customer database received test transactions. Synthetic receipts are watermarked `SYNTHETIC TEST RECEIPT - NOT A PAYMENT`.

## Synthetic OCR accuracy

Harness: `C:\Users\janel\AppData\Local\Temp\opencode\ocr_audit.py`  
Results: `C:\Users\janel\AppData\Local\Temp\opencode\ocr-audit\ocr-audit-results.json`

Three clearly labeled synthetic receipts were rendered for GCash, Maya, and Bank Transfer. The shop was configured the way an administrator would configure it: Maya activated and a Bank Transfer method added for the test.

| Field | Correct / total | Accuracy |
| --- | ---: | ---: |
| Reference | 3 / 3 | 100% |
| Amount | 3 / 3 | 100% |
| Date/time | 3 / 3 | 100% |
| Sender | 3 / 3 | 100% |
| Recipient | 3 / 3 | 100% |
| Payment method | 3 / 3 | 100% |
| **Overall** | **18 / 18** | **100%** |

All three extracted at `confidence: 1.0` with `engine: tesseract`, no flags, and masked account/wallet details such as `0917****4821`.

**Interpretation:** the parser recognizes the intended synthetic layouts. Real receipts vary in fonts, compression, language, glare, cropping, overlays, and reference formats, so real-world accuracy remains unknown until real samples are supplied.

## Edge cases

| Case | Observed result |
| --- | --- |
| Blurry image | `flags: ["unreadable"]`, confidence `0`, note “No text could be read from the receipt.” |
| Unreadable image | `flags: ["unreadable"]`, confidence `0` |
| Unsupported file type | `flags: ["unsupported_type"]`, confidence `0`, shop review notice |
| PDF without a text layer | `flags: ["unreadable"]`, confidence `0`, explicit “No text layer was found in this PDF; the shop will review it.” |
| PDF with embedded text | `pypdf` extracted 241 characters and OCR parsed reference `GCASH-482719` |
| Cropped receipt | All fields were still read and no flag was raised because all critical text remained in frame |

Scanned PDFs are intentionally **not** OCR-rasterized; they go to admin review. The cropped case is not evidence of general crop robustness — it only shows that legibility, not physical completeness, drove the result.

## Money safety

### Admin confirmation path

| Stage | GCash | Maya | Bank Transfer |
| --- | ---: | ---: | ---: |
| Money after scan | 0 | 0 | 0 |
| Money after submission | 0 | 0 | 0 |
| Money after explicit admin verify | 1,250.00 | 875.50 | 2,400.00 |
| Order status after submission | Awaiting Down Payment | Awaiting Down Payment | Awaiting Down Payment |
| Order status after verify | Confirmed | Confirmed | Confirmed |

Scanning and submitting a receipt never counted as money. Only the explicit admin verification wrote a payment record and confirmed the order.

### OCR failure path

A blurry submission produced:

- `verification_status: admin_review`
- `flags: ["unreadable"]`
- recorded money `0 → 0`
- balance `1,000.00 → 1,000.00`
- order status unchanged (`Awaiting Down Payment`)

### Independent provider confirmation path

A local mock provider returned a positive response with matching reference and amount. The application then used its existing verification service:

- `provider_confirmed: true`
- `verification_status: verified`
- `submission_status: verified`
- recorded money: `500.00`
- order status: `Confirmed`
- stored `auto_confirm: false` because the flag is consumed by `verify_payment_submission`

This was a **mock**, not a real provider. The test proves the internal guard path works; it does not prove GCash, Maya, or bank APIs are integrated. No API keys are exposed to browsers; admin views expose only whether a gateway is configured.

An audit-wording defect was fixed during this review: provider-confirmed payments previously wrote “Administrator verified…” in history. History now names the actual actor, for example “was verified by Local Audit Provider”.

## Customer and admin UI

Browser automation used installed Chrome at `C:\Program Files\Google\Chrome\Application\chrome.exe` with Playwright. Viewports: 390 px, 820 px, and 1440 px.

Screenshots:

| View | 390 px | 820 px | 1440 px |
| --- | --- | --- | --- |
| Customer scan feedback | [customer-ocr-feedback-390.png](ocr-screenshots/customer-ocr-feedback-390.png) | [customer-ocr-feedback-820.png](ocr-screenshots/customer-ocr-feedback-820.png) | [customer-ocr-feedback-1440.png](ocr-screenshots/customer-ocr-feedback-1440.png) |
| Admin verification form | [admin-verification-form-390.png](ocr-screenshots/admin-verification-form-390.png) | [admin-verification-form-820.png](ocr-screenshots/admin-verification-form-820.png) | [admin-verification-form-1440.png](ocr-screenshots/admin-verification-form-1440.png) |
| Admin OCR review modal | [admin-ocr-review-390.png](ocr-screenshots/admin-ocr-review-390.png) | [admin-ocr-review-820.png](ocr-screenshots/admin-ocr-review-820.png) | [admin-ocr-review-1440.png](ocr-screenshots/admin-ocr-review-1440.png) |

Customer checks at all three widths:

- Status: `For Admin Review · 100% confidence`
- Detected chips: reference, amount, recipient, status
- Explicit notice: “Scanning reads the receipt only. Payment is confirmed after the shop or provider verifies it.”
- `payment_records_after_scan: 0`
- Balance unchanged at `2,500`

Admin checks at all three widths:

- Review, request clearer receipt, add note, reject, and verify actions visible
- 8 comparison rows and 9 validation checks rendered
- Receipt preview loaded
- Notice: “Receipt reading: For Admin Review · 100% confidence. OCR is not proof of payment; confirm the money in the shop's account before approving.”
- No console errors in admin captures

One customer capture at 390 px logged a `404` for a missing favicon; no application API errors were observed.

## Deployment configuration

| Concern | Status |
| --- | --- |
| Docker image | `Dockerfile` installs `tesseract-ocr` and `tesseract-ocr-eng`; OCR remains optional |
| Python dependencies | `Pillow>=10,<15`, `pytesseract>=0.3,<1`, `pypdf>=4,<6` in both `requirements.txt` and `backend/app/requirements.txt` |
| OCR defaults | `PRINTSHOP_OCR_ENGINE=auto`, `PRINTSHOP_OCR_LANGUAGE=eng`, `PRINTSHOP_OCR_MAX_MB=5`, `PRINTSHOP_OCR_TIMEOUT_SECONDS=20` |
| Upload limit | `PRINTSHOP_MAX_UPLOAD_MB=5`; inline body cap allows base64 overhead plus 256 KB |
| Vercel consideration | Vercel’s ~4.5 MB request body cap still applies to inline JSON data URLs. Keep receipt files below about 3.3 MB base64-encoded, or use a separate object store for larger files |
| PDF handling | Embedded text only via `pypdf`; scanned PDFs go to admin review |
| Provider gateway | Host allow-list and HTTPS enforcement are available; keys stay server-side; views expose configuration status only |
| Turso | `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` are now explicit in `.env.example`; `PRINTSHOP_FILE_STORAGE=database` is the default when Turso is configured |
| Proxy settings | `PRINTSHOP_PUBLIC_SCHEME` and `PRINTSHOP_TRUSTED_PROXY_IPS` are documented in `.env.example` |

For Vercel/Turso deployment, set at minimum:

```text
PRINTSHOP_ENVIRONMENT=production
PRINTSHOP_SECRET_KEY=<stable 32+ character secret>
PRINTSHOP_ADMIN_EMAIL=<admin email>
PRINTSHOP_ADMIN_PASSWORD_HASH=<pbkdf2_sha256 hash>
PRINTSHOP_ALLOWED_HOSTS=<exact public host>
TURSO_DATABASE_URL=<libsql:// or https:// Turso URL>
TURSO_AUTH_TOKEN=<Turso token>
PRINTSHOP_FILE_STORAGE=database
PRINTSHOP_OCR_ENGINE=auto
PRINTSHOP_OCR_MAX_MB=5
PRINTSHOP_OCR_TIMEOUT_SECONDS=20
PRINTSHOP_PUBLIC_SCHEME=https
PRINTSHOP_TRUSTED_PROXY_IPS=<known proxy IPs>
```

For Docker/local deployment, keep the SQLite file and upload directory on a persistent volume and leave the Turso variables blank.

## SQLite and Turso verification

| Run | Result |
| --- | --- |
| Local SQLite full suite, final | **346 tests, OK** |
| Local SQLite full suite, baseline before concurrent test additions | 323 tests, OK |
| Local SQLite OCR suite | **23 tests, OK** |
| Hrana emulator OCR suite | **23 tests, OK** |
| Hrana emulator full suite | 334 tests: 333 passed, 1 failure, 1 error |

The two emulator-only failures were test-harness limitations, not OCR defects:

1. A legacy SQLite migration test patches `DATABASE_PATH`, but that path is ignored when `TURSO_DATABASE_URL` is set; it therefore hit the shared emulator database and found an existing table. It passes in the local SQLite suite.
2. An upload test asserts a file exists on disk, which is false by design under `PRINTSHOP_FILE_STORAGE=database`; the file is served correctly from the database.

A follow-up emulator run with disk file storage exceeded the time budget and is not used as evidence. **Live Turso was not contacted.** Treat Turso compatibility as emulator/static verification only until a Vercel preview with copied data is exercised.

## Code and validation checks

| Check | Result |
| --- | --- |
| `python -m unittest discover -s backend/tests` | 346 tests, OK |
| `python -m unittest discover -s backend/tests -p 'test_ocr_payment_verification.py'` | 23 tests, OK |
| `node --check` on all frontend JS | 20 files, OK |
| `ocr_smoke.py` | 35/35 HTTP 200; 21 pages; 13 changed assets; 0 missing static references |
| `git diff --check` | No errors; only pre-existing LF/CRLF warnings |

Three minimal fixes were made during this audit:

1. `backend/app/core/config.py`: moved defaulted `public_scheme` / `trusted_proxy_ips` after required SMTP fields so the `Settings` dataclass can be imported.
2. `backend/app/services/order_services.py`: corrected provider-confirmation history wording to name the real actor.
3. `.env.example`: documented `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN`.

## Remaining work before claiming real-world readiness

1. **Provide authorized or redacted real receipts** for GCash, Maya, and bank transfer. A folder path is needed; the same accuracy matrix will be rerun against them.
2. Integrate real provider confirmation APIs if automatic confirmation is desired. Until then, keep `verification_method=manual` and admin review as the default.
3. Run a Vercel preview against a copied Turso database to confirm live Hrana behavior, file storage, body limits, and proxy headers.
4. Add larger/realistic Vercel upload handling if receipts above the platform request cap must be accepted.
5. Expand edge-case samples to real glare, low-resolution screenshots, partial crops, and provider-specific reference formats.

No dummy transactions were added to the project, no screenshot is marked as a verified payment, and no commit, push, or pull request was made.
