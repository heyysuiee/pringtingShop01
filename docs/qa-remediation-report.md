# ImPRESS Print Works — QA remediation report

Date: 2026-11-23

## Scope and method

This report records the staged P0 → P1 → P2 remediation of the audit findings. The original P0/P1 issue IDs and decisions were retained in the working context. The original P2 wording was not retained; the user approved deriving the P2 findings from a fresh code review, so P2 items below use derived IDs (`P2-D01`, …). QA-036–039 were described in the retained context as business limitations and are preserved rather than changed.

No reset, clean, checkout, or discard was performed. All pre-existing uncommitted work was preserved. No real payment, production customer record, live SMTP/gateway/OCR provider, or production deployment was used. A live Vercel preview against a copied Turso database was verified separately and is recorded in `docs/ocr-live-deployment-checklist.md`; the production database was not touched.

## Baselines and verification summary

- Baseline before remediation: **323 tests OK**.
- After P0: 70 affected tests OK; 334 full tests OK; 20 JS files syntax OK.
- After P1 batch 1 (QA-006/007): 90 affected tests OK.
- After P1 batch 2 (QA-008/009/010): 117 affected tests OK.
- After P1 batch 3 (QA-016/005): 49 targeted, 83 auth, 353 full tests OK.
- After P1 batch 4: 12 new targeted tests, 83 affected tests, **365 full tests OK**.
- After P2: 27 new P2 regression tests OK; 118 affected auth/security/UI/workflow tests OK; **403 full tests OK**.
- Final structural checks: project SQLite database `PRAGMA integrity_check` = `ok`; `PRAGMA foreign_key_check` = 0 rows; 21/21 JavaScript files pass `node --check`; `git diff --check` reports no whitespace errors; API/frontend smoke returned 63/64 PASS, with the single pre-existing static-reference miss in the unmodified `frontend/customer/index.html` fallback images.

## P0 findings

| ID | Finding | Classification | Evidence |
| --- | --- | --- | --- |
| QA-001 | Logout token replay | FIXED | Exact token revoked by HMAC; only the logging-out device is signed out. |
| QA-002 | XLSX formula injection | FIXED | Formula-leading strings are prefixed with `'` before export. |
| QA-003 | Forwarded-IP spoofing / untrusted proxy headers | FIXED | Forwarded headers trusted only on Vercel or an explicit trusted proxy; untrusted proxies skip only the IP throttle bucket. |
| QA-004 | In-memory customer auth throttle | FIXED | Production throttles use a shared DB-backed store. |
| QA-017 | `size-admin.js` missing from an admin shell | FIXED | Loaded in every admin shell that loads `dashboard.js`. |

## P1 findings

| ID | Finding | Classification | Evidence |
| --- | --- | --- | --- |
| QA-005 | Email ownership not verified | FIXED | Opt-in verification; defaults on in production, off in dev/tests; production requires SMTP. |
| QA-006 | OTP compare/increment/lockout race | FIXED | Compare, increment, lockout, and reset-token issue run in one `BEGIN IMMEDIATE` transaction. |
| QA-007 | Payment gateway endpoint SSRF | FIXED | Request-time `endpoint_problem()` validation plus optional host allow-list; loopback HTTP remains available for local testing. |
| QA-008 | Duplicate payment reference | FIXED | `reference_claims` PK `(kind, claim_key)` enforces uniqueness. |
| QA-009 | Duplicate refund transaction reference | FIXED | Same claim store, `refund` / `refund_id` kinds. |
| QA-010 | Duplicate tracking number | FIXED | Same claim store, `tracking` kind; same order/cycle reuse and reprint archiving preserved. |
| QA-016 | Province alias mismatch | FIXED | Aliases canonicalized through PSGC keys. |
| QA-020 | Backups read inconsistent snapshots | FIXED | One `BEGIN IMMEDIATE` snapshot covers every table. |
| QA-021 | Uploaded files missing from backups | FIXED | v2 backups include DB-backed files and base64 disk bytes; v1 remains accepted. |
| QA-022 | Orphaned upload files after failed writes | FIXED | Tracked saves + compensating cleanup + startup cleanup with a 24-hour grace period. |
| QA-023 | Concurrent startup seeding/migration race | FIXED | `exclusive_write()` wraps all initializers and the full startup sequence; nested calls are reentrant. |
| QA-027 | Ephemeral disk storage in production | FIXED | Production refuses `disk` storage on Vercel/Turso or under temp directories. |
| QA-028 | Backup signature key coupling/rotation | FIXED | Dedicated `PRINTSHOP_BACKUP_SIGNING_KEY`, fallback to `PRINTSHOP_SECRET_KEY`, and previous-key rotation list. |

## P2 findings

Original P2 wording was unavailable, so these IDs were derived from a fresh frontend, backend, and configuration review. All derived code and documentation findings are FIXED; items that require external services or are deliberate business limits remain in the preserved-limitations table.

| ID | Finding | Classification | Evidence |
| --- | --- | --- | --- |
| P2-D01 | City/municipality select never populated from PSGC data | FIXED | `order.js` now rebuilds the dependent city list from `province.cities` and keeps the server as the authoritative fee checker. |
| P2-D02 | Changing province kept a stale delivery area and fee | FIXED | `syncDeliveryArea()` clears the prior selection before matching the new province; an unmatched province shows an explicit warning. |
| P2-D03 | Date minima used UTC, wrong before 8 a.m. PHT | FIXED | `DynamicForm.manilaToday()` uses `Asia/Manila`; order dates use that shared helper. |
| P2-D04 | Backup restore treated file metadata as verified and raced file selection | FIXED | Metadata is labelled as file claims, a selection token discards stale async reads, and the confirmation text says the server verifies contents. |
| P2-D05 | Client-side phone rules differed across customer forms | FIXED | Shared `frontend/assets/phone.js` mirrors the server rule and is loaded by order, registration, and portal pages; local fallbacks remain. |
| P2-D06 | Payment-method load failures looked like an empty shop setup | FIXED | Portal tracks load errors, blocks payment/request submission, and offers a retry in refund recipient fields. |
| P2-D07 | GCash copy hardcoded into configurable payment flows | FIXED | Customer and admin copy now refers to the shop's configured e-wallet(s) or uses the fetched method list. |
| P2-D08 | Confirmation dialogs could stack and reuse DOM IDs | FIXED | Both dialog helpers guard concurrent calls and generate unique heading/input IDs. |
| P2-D09 | Stale receipt-toggle read could contradict the current order | FIXED | The toggle is computed from the current order by `PortalTracking.render()`; the old read was removed. |
| P2-D10 | Backup could combine a DB snapshot with inconsistent disk bytes | FIXED | `create_backup()` takes `BEGIN IMMEDIATE` before scanning disk and holds it through the table snapshot; disk saves publish by atomic rename. |
| P2-D11 | Password-reset token could be consumed twice concurrently | FIXED | Customer and admin confirmation now validate the token, check the current password, update credentials, and delete the token in one write transaction. |
| P2-D12 | CORS omitted `PUT`, breaking cross-origin chunked uploads | FIXED | `PUT` added to allowed methods with a static regression check. |
| P2-D13 | Inline body cap trusted `Content-Length` and missed file routes | FIXED | The middleware counts real streamed bytes, covers refund/delivery-issue/refund-completion routes, and replays the bounded body to FastAPI. The new test caught and fixed an initial middleware receive-handle bug. |
| P2-D14 | 24-hour upload-session expiry documented but not enforced on use | FIXED | `_owned_session()` and completed-session adoption reject and delete expired sessions. |
| P2-D15 | Root `requirements.txt` omitted runtime `httpx` | FIXED | Both requirement files now pin `httpx>=0.27,<1`; a parity test covers the gateway import. |
| P2-D16 | Vercel preview runbook created a deployment that could not start | FIXED | Preview-only `PRINTSHOP_REQUIRE_EMAIL_VERIFICATION=false` added with an explicit production warning. |
| P2-D17 | Render free-plan blueprint allowed guaranteed data loss | FIXED | Startup refuses Render without `TURSO_DATABASE_URL`; blueprint wording and a negative config test document the guard. |
| P2-D18 | Stale throttle, password-length, SMTP, README, and test-count docs | FIXED | Docs now describe DB-backed production throttles, customer/admin password lengths, mandatory production SMTP or explicit opt-out, current architecture, and the consolidated test report. |
| P2-D19 | `.env.example` and account docs omitted supported variables | FIXED | Added `PRINTSHOP_ENV_FILE`, `PRINTSHOP_ORDER_DB`, `GOOGLE_API_KEY`, admin credential guidance, and backup key documentation. |
| P2-D20 | Docker enabled uvicorn proxy-header parsing | FIXED | Removed `--proxy-headers`; static check aligns Docker with the Render proxy policy. |
| P2-D21 | `.dockerignore` matched only exact `.env` filenames | FIXED | Patterns now cover `.env*`, `backend/app/.env*`, and `.vercel`. |
| P2-D22 | Render blueprint lacked backup signing key settings | FIXED | Added dedicated backup signing key and previous-key variables. |
| P2-D23 | Docker image ships English-only Tesseract data | FIXED | `.env.example` now states that other languages fall back to admin review unless language packs are added at build time. |
| P2-D24 | Production config validation had no negative tests | FIXED | Added table-driven tests for missing/short secrets, wildcard hosts, bad admin hash, missing SMTP, Render/Turso, and short previous backup keys. |

## Preserved limitations

| Item | Classification | Note |
| --- | --- | --- |
| QA-036–039 | UNRESOLVED (by design) | Business limitations preserved as required; no payment automation, no production customer records, and no live provider verification were introduced. |
| Live SMTP, payment gateway, OCR/Tesseract accuracy, browser/responsive, sustained load, production deployment | BLOCKED | No external services, credentials, or production deployment were available in this environment. |
| Live Vercel preview against copied Turso | FIXED | Verified with 7/7 scripted checks in `docs/ocr-live-deployment-checklist.md`; the production database remained untouched, and production deployment remains BLOCKED. |
| Six missing fallback image references in unmodified `frontend/customer/index.html` | UNRESOLVED (pre-existing) | The 64-check smoke run has 63 PASS; the single FAIL is unchanged by this remediation and outside the confirmed scope. |
| Vercel inline base64 receipt ceiling near 3.3 MB | UNRESOLVED (platform limit) | Chunked upload remains the safe path for larger receipts; no platform limit was changed. |
