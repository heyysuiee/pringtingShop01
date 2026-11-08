# Customer accounts, password recovery, and orders

Written for the shop administrator setting up the site and for developers maintaining it.

## What is implemented

| Area | Status |
| --- | --- |
| Sign In page (`/sign-in`) with show/hide password, Remember me (30 days), and links to Forgot password and Create an Account | Implemented |
| Separate registration page (`/create-account`) | Implemented |
| Sign-in required before submitting an order; order page shows the sign-in gate otherwise | Implemented |
| Pre-filled customer details for signed-in customers; profile editing | Implemented |
| Forgot password (`/forgot-password`): emailed 6-digit code, 5-minute expiry, attempt limit, 60-second resend cooldown, new password + confirmation | Implemented; **needs SMTP** to send codes |
| Automatic order reference numbers (`XXX-MM-DD-YY`) | Implemented |
| Customer portal: profile, order history, order details, status | Implemented |
| Sign in with Google | **Not implemented.** There is no Google sign-in code in this project. |

Nothing here is mocked in production code. The automated tests replace the SMTP sender with a stub, so they check this application's logic but not your mail server's behaviour. Run the check in the SMTP setup below with real credentials before going live.

## Pages

| Page | Purpose |
| --- | --- |
| `/sign-in` | Customer sign in. Accepts `?next=/path` to return to a page afterwards (only same-site paths). |
| `/create-account` | Registration. |
| `/forgot-password` | Email → code → new password → success, then back to Sign In. |
| `/customer-portal` | Profile and orders when signed in; a Sign In prompt and guest order lookup otherwise. |

The order page links to Sign In with the selected product kept in `next`.

## Configuration

All secrets are read from environment variables. Never commit them or place them in frontend files.

For local development you can put them in a `.env` file in the project root (not `backend/app/.env`, which is not read; one `KEY=value` per line; it is git-ignored). Variables already set in the shell take priority. Keep `PRINTSHOP_ENVIRONMENT=development` in a local `.env`; `production` requires every production setting.

| Variable | Purpose |
| --- | --- |
| `PRINTSHOP_SECRET_KEY` | Signs session cookies and keys the stored reset-code hashes. Set a long random value (`python -c "import secrets; print(secrets.token_urlsafe(48))"`) and keep it the same between restarts. Without it, a temporary key is generated at each start (a warning is logged), so every restart signs all customers out and cancels resets in progress. Changing it deliberately signs everyone out. |
| `PRINTSHOP_SMTP_HOST`, `PRINTSHOP_SMTP_PORT` | SMTP server (port 587 with STARTTLS by default; port 465 uses implicit TLS) |
| `PRINTSHOP_SMTP_USERNAME`, `PRINTSHOP_SMTP_PASSWORD` | SMTP login, if the server requires one |
| `PRINTSHOP_SMTP_FROM` | Sender address shown to customers |
| `PRINTSHOP_SMTP_STARTTLS` | `true` (default) or `false` only for a trusted local relay |

If `PRINTSHOP_SMTP_HOST` or `PRINTSHOP_SMTP_FROM` is blank, password-reset requests return `503` and no code is claimed to have been sent.

### SMTP setup

Use any provider that gives SMTP credentials (for example your domain's mail host, a transactional email service, or Gmail with an app password). Gmail example:

```text
PRINTSHOP_SMTP_HOST=smtp.gmail.com
PRINTSHOP_SMTP_PORT=587
PRINTSHOP_SMTP_USERNAME=yourshop@gmail.com
PRINTSHOP_SMTP_PASSWORD=your-16-character-app-password
PRINTSHOP_SMTP_FROM=ImPRESSion PrintShop <yourshop@gmail.com>
```

Then check the settings by sending a test email:

```powershell
python -m backend.app.core.send_test_email you@example.com
```

Finally request a reset for an account you control. The code email is sent from `PRINTSHOP_SMTP_FROM`. Check the spam folder if it does not arrive.

## Forgot password behaviour

1. `POST /api/customer/password-reset/request` with the email. The response is the same whether or not an account exists (no email enumeration).
2. The code is 6 digits, valid for 5 minutes, and stored only as a keyed HMAC. It is never returned by the API or written to logs.
3. `POST /api/customer/password-reset/verify` checks the code. After 5 wrong attempts the code is discarded and a new one must be requested.
4. A correct code returns a short-lived reset token (10 minutes), kept only in the page's memory. `POST /api/customer/password-reset/confirm` sets the new password (at least 12 characters, different from the current one, hashed with PBKDF2-SHA256) and deletes the code and token in the same transaction, so both are single use. The token only works for the account it was issued to.
5. Resending is blocked for 60 seconds per email address (the response then says no new code was sent). Requests are rate-limited per client address and per email. If sending fails, the cooldown is released so the customer can retry immediately.
6. When SMTP is not configured, every request gets the same `503` (checked before the account lookup, so it reveals nothing) and the server log names the missing variables. Submitting an already-used code is rejected without cancelling the verified reset session.
7. On success the page shows a confirmation and returns to Sign In, where the email is filled in.

## Orders and reference numbers

Every accepted order gets a server-generated reference in the form `XXX-MM-DD-YY`:

- `XXX` is the shop prefix `IPS`.
- `MM-DD-YY` is the order's creation date on the shop's calendar (Philippine time, UTC+8).
- Example: `IPS-11-04-26`.

The first order of a day uses the bare reference. Any further order on the same day receives a numeric suffix, for example `IPS-11-04-26-2`, because the format alone cannot hold two orders on one date. References are chosen inside an immediate SQLite write transaction and have a unique primary key, so concurrent submissions cannot receive the same reference. Customers cannot choose or change a reference.

Orders created before this change keep their earlier `IPS-yymmdd-<hex>` references, which still work for lookup.

## API endpoints

All `POST`, `PATCH`, and `DELETE` requests are checked for same-origin use. Session cookies are signed, HttpOnly, and `SameSite=Strict`.

### Customer account

| Method | Path | Body or query | Notes |
| --- | --- | --- | --- |
| `POST` | `/api/customer/register` | `name`, `email`, `phone`, `messenger`, `password` | Creates the account and signs in. `409` if the email exists. |
| `POST` | `/api/customer/session` | `email`, `password`, `remember` | Sign in. Rate limited. `remember: true` keeps the session 30 days; otherwise it ends when the browser closes (12 hours at most). |
| `GET` | `/api/customer/session` | | Current customer, or `401`. |
| `DELETE` | `/api/customer/session` | | Sign out. |
| `GET` | `/api/account/profile` | | Signed-in profile. |
| `PATCH` | `/api/account/profile` | `name`, `phone`, `messenger` | Update own details. Email is fixed. |

### Forgot password

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `POST` | `/api/customer/password-reset/request` | `email` | `{message, expires_in, resend_after}`, or `{message, resend_after, cooldown: true}` during the cooldown |
| `POST` | `/api/customer/password-reset/verify` | `email`, `code` | `{reset_token, message}` |
| `POST` | `/api/customer/password-reset/confirm` | `email`, `reset_token`, `new_password` | `{message}` |

### Orders

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| `POST` | `/api/orders` | Signed-in customer | Requires an `Idempotency-Key` header. Email must match the account. |
| `GET` | `/api/account/orders` | Signed-in customer | The customer's own orders. |
| `GET` | `/api/account/orders/{reference}` | Signed-in customer | One order; other customers' references return `404`. |
| `GET` | `/api/orders?email=&reference=` | Email plus existing reference | Guest lookup for orders placed before sign-in was required. |
| `GET` | `/api/orders/{reference}?email=` | Email | Order details for lookup visitors. |

Administrator endpoints live under `/api/admin/` and require an administrator session.

## Database

The database is SQLite (`PRINTSHOP_DATABASE_PATH`). Tables are created or updated automatically at startup; existing rows are kept.

- `customer_accounts` stores customers. Its `session_version` column is copied into each customer's session cookie; a password reset increments it, so every session issued before the reset (on any device) is rejected. Cookies issued before this column existed count as version 0.
- `customer_password_resets` holds at most one active reset per customer: the hashed code, its expiry time, the wrong-attempt count, and the hashed reset token.
- `auth_throttle_events` (in `auth_throttle_repository.py`) records forgot-password requests for the 60-second resend cooldown and the per-email / per-address limits. Keys are stored as HMACs, not plain email addresses. Because all three tables live in the database, OTP expiry, attempt limits, and cooldowns keep working after a restart.

## Running the tests

```powershell
python -m unittest discover -s backend/tests -v
```

## Known limitations

- Forgot-password limits are stored in the database. The sign-in and registration rate limits are still kept in process memory, so they reset on restart.
- Signing out (`DELETE /api/customer/session`) clears the cookie on that browser only; other devices stay signed in until a password reset or until their session expires.
- Behind a reverse proxy, all visitors share one client address unless the proxy's forwarded address is trusted. Per-email limits still apply.
- Email delivery has not been exercised against a live mail server in this environment. Verify it with your real credentials.
