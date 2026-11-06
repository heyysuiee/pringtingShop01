# Customer accounts, password recovery, and orders

Written for the shop administrator setting up the site and for developers maintaining it.

## What is implemented

| Area | Status |
| --- | --- |
| Email and password registration and sign-in | Implemented |
| Sign-in required before submitting an order; order page shows the sign-in gate otherwise | Implemented |
| Pre-filled customer details for signed-in customers; profile editing | Implemented |
| Forgot password: emailed 6-digit code, expiry, attempt limit, cooldown, reset | Implemented; **needs SMTP** to send codes |
| Automatic order reference numbers (`XXX-MM-DD-YY`) | Implemented |
| Customer portal: profile, order history, order details, status | Implemented |
| Sign in with Google | **Not implemented.** There is no Google sign-in code in this project. |

Nothing here is mocked in production code. The automated tests replace the SMTP sender with a stub, so they check this application's logic but not your mail server's behaviour. Run the check in the SMTP setup below with real credentials before going live.

## Configuration

All secrets are read from environment variables. Never commit them or place them in frontend files.

| Variable | Purpose |
| --- | --- |
| `PRINTSHOP_SMTP_HOST`, `PRINTSHOP_SMTP_PORT` | SMTP server (port 587 with STARTTLS by default) |
| `PRINTSHOP_SMTP_USERNAME`, `PRINTSHOP_SMTP_PASSWORD` | SMTP login, if the server requires one |
| `PRINTSHOP_SMTP_FROM` | Sender address shown to customers |
| `PRINTSHOP_SMTP_STARTTLS` | `true` (default) or `false` only for a trusted local relay |

If `PRINTSHOP_SMTP_HOST` or `PRINTSHOP_SMTP_FROM` is blank, password-reset requests return `503` and no code is claimed to have been sent.

### SMTP setup

Use any provider that gives SMTP credentials (for example your domain's mail host, a transactional email service, or Gmail with an app password). Set the variables above, then test by requesting a reset for an account you control. The code email is sent from `PRINTSHOP_SMTP_FROM`. Check the spam folder if it does not arrive.

## Forgot password behaviour

1. `POST /api/customer/password-reset/request` with the email. The response is the same whether or not an account exists (no email enumeration).
2. The code is 6 digits, valid for 10 minutes, and stored only as a keyed HMAC.
3. `POST /api/customer/password-reset/verify` checks the code. After 5 wrong attempts the code is discarded and a new one must be requested.
4. A correct code returns a short-lived reset token (10 minutes). `POST /api/customer/password-reset/confirm` sets the new password (at least 12 characters, hashed with PBKDF2-SHA256) and deletes the code and token, so both are single use.
5. Resending is blocked for 60 seconds per email address. Requests are rate-limited per client address and per email.

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
| `POST` | `/api/customer/session` | `email`, `password` | Sign in. Rate limited. |
| `GET` | `/api/customer/session` | | Current customer, or `401`. |
| `DELETE` | `/api/customer/session` | | Sign out. |
| `GET` | `/api/account/profile` | | Signed-in profile. |
| `PATCH` | `/api/account/profile` | `name`, `phone`, `messenger` | Update own details. Email is fixed. |

### Forgot password

| Method | Path | Body | Response |
| --- | --- | --- | --- |
| `POST` | `/api/customer/password-reset/request` | `email` | `{message, resend_after}` |
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

`customer_accounts` stores customers; `customer_password_resets` holds at most one active reset record per customer. Both are created automatically at startup by `backend/app/repositories/customer_repository.py`. The database is SQLite (`PRINTSHOP_DATABASE_PATH`).

## Running the tests

```powershell
python -m unittest discover -s backend/tests -v
```

## Known limitations

- Rate limits and reset cooldowns are kept in process memory. They reset on restart and are not shared between instances, which matches the documented single-instance deployment.
- Changing a password does not sign out existing sessions; they expire after 12 hours.
- Behind a reverse proxy, all visitors share one client address unless the proxy's forwarded address is trusted. Per-email limits still apply.
- Email delivery has not been exercised against a live mail server in this environment. Verify it with your real credentials.
