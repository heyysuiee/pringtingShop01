# OCR payment verification — live deployment readiness checklist

Date: 2026-11-23  
Scope: final readiness review after the OCR Production Readiness Report. A Vercel preview against a copied Turso database was deployed and verified. No production deployment was performed.

## Overall verdict

**Conditional readiness: PASS for manual-verification mode in the live preview. NOT READY for production deployment.**

- Live preview verification against a copied Turso database completed with **7/7 scripted checks PASS**.
- OCR never records money. Only explicit administrator verification or an independent provider confirmation can do so.
- Manual administrator verification remains the default. No real GCash/Maya/bank provider API is integrated.
- Real GCash, Maya, and bank receipt accuracy is **NOT TESTED** because no authorized/redacted samples were supplied.
- One platform constraint is a **FAIL** for the stated “5 MB inline receipt” capability under Vercel: base64 overhead makes the effective inline file ceiling about 3.3 MB.

## Live preview evidence

| Item | Result | Actual evidence |
| --- | --- | --- |
| Preview deployment | PASS | `https://printing-shop-system-4zrgfssmj-heyysuiee.vercel.app`, deployment `dpl_2eXp2RJso1Y3TCt5UkGVE8MbUATy`, environment `preview`. No `--prod`. |
| Copied preview database | PASS | `printing-shop-preview-20261123-105229`, branched from `printing-shop`. Baseline 11 orders; 14 orders after synthetic tests. |
| Production database untouched | PASS | `printing-shop` still has 11 orders; latest reference remains `IPS-11-23-26-2`. All tests used only the preview URL/token. |
| Admin login on preview | PASS | `POST /api/admin/session` → HTTP 200, `{"mode":"authenticated"}`. |
| Manual verification default on preview | PASS | GCash and Maya both reported `verification_method: manual` and `gateway.configured: false`. |
| OCR fallback in Vercel runtime | PASS | Order `IPS-11-23-26-4`: `ocr.engine=tesseract`, `ocr.flags=["engine_unavailable"]`, `verification.status=admin_review`. |
| Scan/submit money safety | PASS | Before scan, after scan, and after submit: status `Awaiting Down Payment`, total `1000.0`, recorded payments `0`, balance `1000.0`. |
| Receipt persistence and access | PASS | `PRINTSHOP_FILE_STORAGE=database`. Owner HTTP 200 (160 bytes), admin HTTP 200, anonymous HTTP 401. |
| Manual admin verification | PASS | After verification: total `1000.0`, status `Confirmed`, payments `1`, verified amount `500.0`, remaining balance `500.0`, receipt still HTTP 200. |
| Real Vercel request-body cap | PASS | 4,199,953-byte body → HTTP 200. 4,799,952-byte body → HTTP 413 `FUNCTION_PAYLOAD_TOO_LARGE` before app code. |
| Live secret exposure | PASS | Public/admin payment-method responses and customer/admin order responses were HTTP 200 and contained no `TURSO_AUTH_TOKEN`, `PRINTSHOP_SECRET_KEY`, SMTP password, preview admin password, or preview DB token. |
| Vercel protection-bypass secret | PASS | The automation bypass secret added by `vercel curl` was revoked. Final check: `REMAINING_BYPASS_SECRETS=0`. |

## PASS

| Item | Classification | Actual evidence |
| --- | --- | --- |
| Existing test suite preserved | PASS | Final run: `python -m unittest discover -s backend/tests` → **403 tests, OK**. Earlier baseline was 353 tests; the increase is from concurrent test files in the working tree. |
| OCR/payment safety tests | PASS | Previous run: OCR suite 23 tests OK locally and through the local Hrana-compatible emulator. |
| Full emulator suite | PASS with limits | Previous run: 334 tests, 333 passed, 1 failure, 1 error; both non-OCR failures were harness/environment limitations, not payment defects. |
| Database/payment persistence in Vercel-style storage | PASS | Local audit used `PRINTSHOP_FILE_STORAGE=database` in an isolated SQLite database. Submitted receipt was persisted and served to owner/admin; anonymous request returned 401; another customer returned 404. |
| Receipt storage survives admin verification | PASS | Local and live preview receipt URLs returned HTTP 200 after verification. |
| OCR fallback when Tesseract is missing | PASS | Simulated locally and confirmed live on Vercel: `engine_unavailable`, `admin_review`, unchanged money state. |
| Manual administrator verification | PASS | Local and live: explicit admin verification recorded exactly one `500.00` payment. Total stayed `1000.00`, status became `Confirmed`, remaining balance became `500.00`. |
| Scan/submit never records money | PASS | Local and live: before scan, after scan, and after submit total `1000.00`, money recorded `0`, payments `0`, balance `1000.00`, status unchanged. |
| Upload cap — below Vercel body limit | PASS | Local 4,200,047-byte body → HTTP 200; live 4,199,953-byte body → HTTP 200. |
| Upload cap — above application cap | PASS | Local 7,500,047-byte body → HTTP 413 with expected “too large” detail. |
| Upload cap — above Vercel limit | PASS | Live 4,799,952-byte body → HTTP 413 `FUNCTION_PAYLOAD_TOO_LARGE`. |
| Manual verification is default | PASS | Local and live payment-method views use `verification_method: manual` and empty gateway config; provider confirmation is opt-in per method. |
| Gateway/customer data exposure | PASS | Configured a secret gateway API key in a temporary DB. It did not appear in admin payment-method views, public payment-method views, customer order views, or admin order views. |
| Customer admin-only field filtering | PASS | Customer order view did not expose `comparison`, `recorded_by`, or admin-note actor fields. |
| Static secret scan | PASS | Frontend files contain no `TURSO_AUTH_TOKEN`, `TURSO_DATABASE_URL`, `PRINTSHOP_SECRET_KEY`, SMTP password, or Gemini key values. Only non-secret configuration names appear in admin notices. |
| Frontend syntax | PASS | Prior check: `node --check` on 20 JS files → OK. |
| HTTP smoke | PASS | Prior check: 35/35 HTTP 200, 21 pages, 13 changed assets, 0 missing static references. |
| Diff hygiene | PASS | `git diff --check` → no errors; only LF/CRLF warnings. |
| No production changes | PASS | No commit, push, pull request, production deployment, or production database write was made. |
| Vercel preview deployment | PASS | Deployed with `npx vercel --yes`; health check `{"status":"ok"}`. |
| Copied Turso database creation | PASS | Separate branch created and used by the preview only. |
| Live Turso connectivity from Vercel | PASS | Preview API reads/writes succeeded against the copied database. |
| Live receipt persistence on Turso | PASS | Receipt file stored in DB and retrievable by owner/admin after verification. |
| OCR behavior in the Vercel runtime | PASS | Tesseract is configured but unavailable in the runtime; app fell back to `admin_review`. |
| Actual Vercel request-body rejection | PASS | Platform returned 413 before app code, and money state was unchanged. |
| Live API secret exposure | PASS | No sensitive markers in public/admin API responses. |
| Protection-bypass cleanup | PASS | Bypass secret revoked; final remaining count is 0. |

## FAIL

| Item | Classification | Finding |
| --- | --- | --- |
| 5 MB inline receipt under Vercel's request-body cap | FAIL | `PRINTSHOP_MAX_UPLOAD_MB=5` allows a 5 MB file, but a base64 JSON data URL is roughly 6.7 MB, above Vercel's ~4.5 MB body ceiling. Live preview confirmed HTTP 413 at 4,799,952 bytes. The application-level cap is 7,252,650 bytes, so it does not protect against Vercel's lower cap. Effective inline file ceiling is about 3.3 MB. Use a smaller receipt image or a chunked/object-storage path for larger files. This is a platform constraint, not a payment-logic defect. |

## NOT TESTED

| Item | Classification | Reason |
| --- | --- | --- |
| Real GCash OCR accuracy | NOT TESTED | No authorized/redacted GCash receipts were supplied. |
| Real Maya OCR accuracy | NOT TESTED | No authorized/redacted Maya receipts were supplied. |
| Real bank-transfer OCR accuracy | NOT TESTED | No authorized/redacted bank receipts were supplied. |
| Real provider confirmation APIs | NOT TESTED | No live GCash/Maya/bank API integration exists; only a local mock path was exercised. |
| Live static-asset secret scan on preview host | NOT TESTED | Local static scan passed; preview static assets were not downloaded and rescanned after the bypass secret was revoked. |
| Public access with Deployment Protection disabled | NOT TESTED | Preview intentionally uses Vercel Authentication; `vercel curl` bypassed it only for verification. |
| Production deployment | NOT TESTED | Explicitly unauthorized. Do not run `vercel --prod`. |

## Blocked inputs

1. **Authorized or redacted real receipts.** The user confirmed no samples are available. See [authorized-receipt-test-procedure.md](authorized-receipt-test-procedure.md).
2. **Real provider API integration.** Provider confirmation is opt-in and no live API exists.
3. **Production deployment authorization.** No production deploy, commit, push, or PR is authorized.

## Required before calling production verified

1. Run the authorized receipt procedure per provider and fill in the accuracy matrix with real counts.
2. Decide whether to accept the ~3.3 MB inline receipt ceiling or implement a larger-file path.
3. Keep manual administrator verification as the default until a real provider API is integrated.
4. Obtain explicit authorization for any production deployment.

No receipt screenshot was treated as proof of payment, and no dummy transaction was added to the project database.
