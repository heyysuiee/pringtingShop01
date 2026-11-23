# Vercel preview deployment runbook — copied Turso database

Status: **EXECUTED for preview only on 2026-11-23**. Production deployment remains unauthorized. Do not use this runbook against the production database. Do not run `vercel --prod`.

This runbook prepares a preview that uses a **separate Turso database copied from production**. Production data may be used only as the source of the copy; all tests run against the copy.

## Execution record (preview only)

- Production source DB (name only): `printing-shop`.
- Copied preview DB: `printing-shop-preview-20261123-105229`. Its URL and token were stored only in a temp folder and configured as Vercel **Preview** variables; no production credentials were used by the preview.
- Final preview URL: `https://printing-shop-system-4zrgfssmj-heyysuiee.vercel.app`.
- Final deployment ID: `dpl_2eXp2RJso1Y3TCt5UkGVE8MbUATy`; target/environment reported by Vercel: `preview` (no `--prod`).
- `GET /api/health` on that preview returned `{"status":"ok"}` using `vercel curl` because Deployment Protection is enabled.
- Preview admin login returned `{"mode":"authenticated"}` with preview-only credentials.
- `PRINTSHOP_ALLOWED_HOSTS=*.vercel.app` is intentional. Vercel generated a different hostname on each preview deploy; pinning an exact hostname caused `Invalid host header` on the next deploy.
- `vercel curl` added a project-level Protection Bypass for Automation secret. It was revoked after verification; final check reported `REMAINING_BYPASS_SECRETS=0`.
- Live verification completed with 7/7 scripted checks PASS against the copied database. Results were written to a temporary JSON file outside the repository.
- The preview DB now contains 14 orders (11 baseline + 3 synthetic). It was not destroyed; cleanup awaits an explicit decision.
- Production DB `printing-shop` was rechecked and remains at 11 orders with latest reference `IPS-11-23-26-2`; no production writes occurred.
- No production deployment, commit, push, or pull request was made as part of this runbook execution.

### Final outcome and remaining issues

- PASS: preview deployment, copied DB connectivity, receipt persistence/access control (owner/admin 200, anonymous 401), OCR engine-unavailable fallback to `admin_review`, manual admin verification with correct totals/balance, live Vercel 413 body rejection, and API secret-exposure audit.
- FAIL: a 5 MB inline receipt cannot pass Vercel's ~4.5 MB body cap; effective inline ceiling is about 3.3 MB.
- NOT TESTED: real GCash/Maya/bank receipt accuracy, real provider confirmation APIs, production deployment.
- The preview database and Preview environment variables are still present. Delete them only after the user confirms the preview is no longer needed.

## Current environment

- Vercel CLI authentication: available as `heyysuiee`.
- Project link: `.vercel/project.json` points to `printing-shop-system` (`prj_hi83APxecblds14oy8X7nbG64Nfr`).
- Turso CLI: available in WSL at `/home/janel/.turso/turso`; authenticated as `heyysuiee`.
- Vercel environment variables: existing variables were Production-only; separate **Preview** variables were added for the copied DB, a preview-only secret key, and preview-only admin credentials.

## 1. Authenticate Turso

On Windows, install and use the Turso CLI inside WSL:

```bash
curl -sSfL https://get.tur.so/install.sh | bash
# open a new shell
turso auth login --headless
turso auth whoami
turso db list
```

Do not paste or commit Turso credentials into the repository or chat.

## 2. Identify the production source database name without revealing the token

```powershell
$envFile = Join-Path $env:TEMP 'printshop-prod.env'
npx vercel env pull $envFile --environment=production

$line = Get-Content $envFile | Where-Object { $_ -like 'TURSO_DATABASE_URL=*' } | Select-Object -First 1
$url = $line.Substring('TURSO_DATABASE_URL='.Length)
$sourceDb = [regex]::Match($url, '^(?:libsql|https)://([A-Za-z0-9-]+)').Groups[1].Value
Write-Output "SOURCE_DB_NAME=$sourceDb"

Remove-Item $envFile -Force
```

Only the database name should be displayed. Never display `TURSO_AUTH_TOKEN`.

## 3. Create the preview database copy

```bash
PREVIEW_DB="printshop-preview-$(date +%Y%m%d-%H%M%S)"
turso db create "$PREVIEW_DB" --from-db "$SOURCE_DB"
turso db list
turso db show "$PREVIEW_DB" --url > "$TEMP/preview-url.txt"
turso db tokens create "$PREVIEW_DB" --expiration 7d > "$TEMP/preview-token.txt"
```

A branch created with `--from-db` is a separate database. Confirm that the preview URL/token belong to `$PREVIEW_DB`, not the source database.

## 4. Set Vercel preview environment variables

Add these as **Preview** environment variables with `npx vercel env add <NAME> preview`. Paste values at the prompt; do not put secrets in shell history.

```text
PRINTSHOP_ENVIRONMENT=production
PRINTSHOP_SECRET_KEY=<preview-only stable 32+ character secret>
PRINTSHOP_ADMIN_EMAIL=<preview admin email>
PRINTSHOP_ADMIN_PASSWORD_HASH=<preview admin PBKDF2 hash>
PRINTSHOP_DATABASE_PATH=/tmp/orders.sqlite3
PRINTSHOP_UPLOAD_DIR=/tmp/order_uploads
PRINTSHOP_ALLOWED_HOSTS=*.vercel.app
PRINTSHOP_PUBLIC_SCHEME=https
PRINTSHOP_FILE_STORAGE=database
PRINTSHOP_MAX_UPLOAD_MB=5
PRINTSHOP_OCR_ENGINE=auto
PRINTSHOP_OCR_LANGUAGE=eng
PRINTSHOP_OCR_MAX_MB=5
PRINTSHOP_OCR_TIMEOUT_SECONDS=20
# Preview-only: no SMTP secrets are configured, so registration must stay usable.
PRINTSHOP_REQUIRE_EMAIL_VERIFICATION=false
TURSO_DATABASE_URL=<URL from preview-url.txt>
TURSO_AUTH_TOKEN=<token from preview-token.txt>
```

Notes:

- Use a **preview-only** secret key so production sessions are invalid on the preview.
- Use preview-only admin credentials where possible.
- `*.vercel.app` is acceptable for the first preview deploy because the exact hostname is not known yet. After Vercel prints the preview URL, replace it with the exact hostname and redeploy the preview:
  `npx vercel env rm PRINTSHOP_ALLOWED_HOSTS preview` then add the exact host.
- Do not copy SMTP/Gemini/gateway secrets unless the preview specifically needs them. Manual verification does not require a gateway key. `PRINTSHOP_REQUIRE_EMAIL_VERIFICATION=false` above is preview-only; production registers customers only after email verification.

## 5. Deploy the preview only

```powershell
npx vercel --yes
```

Do **not** add `--prod`. Record the printed preview URL.

## 6. Verify the preview against the copied database

1. `GET <preview-url>/api/health` → `{"status":"ok"}`.
2. Confirm the app is connected to the copied database:
   - In the preview, create one synthetic customer and one synthetic order.
   - In Turso, run `turso db shell "$PREVIEW_DB" "SELECT reference FROM orders ORDER BY created_at DESC LIMIT 1;"` and confirm the new reference exists.
   - Do not run this check against the source database.
3. Verify payment persistence:
   - Scan a small synthetic receipt; confirm no payment record is created.
   - Submit it; confirm a pending submission and a retrievable receipt URL.
   - Confirm anonymous and other-customer requests to the receipt URL are denied.
   - Explicitly verify as admin; confirm one payment record, correct amount, correct balance, and correct order status.
4. Verify OCR:
   - With Tesseract available: record extracted fields per provider.
   - Without Tesseract (expected on a default Vercel image): confirm `engine_unavailable`, `admin_review`, and unchanged money state.
5. Verify upload boundary:
   - A body below ~4.5 MB should succeed.
   - A body above Vercel's limit should be rejected by Vercel before reaching the app.
   - A 5 MB inline image cannot pass through a base64 data URL on Vercel; keep receipts below ~3.3 MB or use chunked/object storage.
6. Verify manual default:
   - Admin payment-method settings show `verification_method: manual` and `gateway.configured: false` unless a gateway was deliberately configured.
7. Verify exposure:
   - Public and admin API responses contain no API key, SMTP password, `TURSO_AUTH_TOKEN`, or `PRINTSHOP_SECRET_KEY`.
   - Customer order responses contain no `comparison`, `recorded_by`, or admin-note actor fields.

## 7. Cleanup

Status: **not executed**. The preview DB and Preview env vars remain in place pending an explicit user decision.

After the preview is no longer needed:

```bash
turso db tokens invalidate "$PREVIEW_DB" -y
turso db destroy "$PREVIEW_DB"
npx vercel env rm TURSO_DATABASE_URL preview
npx vercel env rm TURSO_AUTH_TOKEN preview
```

Remove temporary env/token files. Never delete or rotate the production database as part of preview cleanup.

## Safety rules

- Never set preview `TURSO_DATABASE_URL` to the production URL.
- Never use a production admin password in logs or screenshots.
- Never mark a screenshot as a verified payment.
- Never promote the preview to production without explicit authorization.
