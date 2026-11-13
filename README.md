# ImPRESSion PrintShop

FastAPI serves the existing HTML/CSS/JavaScript storefront and admin pages from one origin. The current order workflow uses SQLite and local file storage; passwords for customer accounts are PBKDF2-SHA256 hashes and sessions use signed, HttpOnly cookies.

## Local development

1. Use Python 3.12 or newer.
2. Install dependencies:

   ```powershell
   python -m pip install -r backend/app/requirements.txt
   ```

3. Configure an administrator. The application does not accept a demo password:

   ```powershell
   python -m backend.app.core.generate_admin_hash
   ```

   Copy the printed hash into `PRINTSHOP_ADMIN_PASSWORD_HASH`, and set `PRINTSHOP_ADMIN_EMAIL`. For local development, set these in the shell before starting the server. Once SMTP is configured, the admin sign-in page's **Forgot password?** link can email a verification code and save a new password in the database; that saved password then takes precedence over `PRINTSHOP_ADMIN_PASSWORD_HASH`.

4. Start the website from the project root:

   ```powershell
   $env:PRINTSHOP_ADMIN_EMAIL = "your-admin-email"
   $env:PRINTSHOP_ADMIN_PASSWORD_HASH = "paste-the-generated-hash"
   python -m uvicorn main:app --reload
   ```

   The default SQLite database and uploads are under `backend/data/`. Override them with `PRINTSHOP_DATABASE_PATH` and `PRINTSHOP_UPLOAD_DIR` when desired. Local development uses a process-generated signing key unless `PRINTSHOP_SECRET_KEY` is set; sessions become invalid after a restart if the key changes.

## Project structure

```text
main.py                     Entry point for `uvicorn main:app` (re-exports backend.app.main:app)
backend/
  app/
    main.py                 FastAPI app: middleware, static files, page routes, router registration
    core/                   Settings, password hashing and sessions, rate limiting, admin dependency
    database/               SQLite connection helper and the one-time catalog seed data
    models/                 Typed shapes of stored orders and catalog records
    schemas/                Request bodies validated by FastAPI (Pydantic)
    repositories/           SQL for customers, orders, and the catalog
    services/               Business rules: accounts, orders, catalog pricing, email
    routes/                 HTTP endpoints (auth, catalog, orders)
    utils/                  Clock and order-reference helpers
    requirements.txt
  tests/                    unittest suite (uses a temporary database)
  data/                     Local SQLite database and uploads (git-ignored)
frontend/
  customer/                 Storefront pages
  admin/                    Admin pages (served only with an admin session)
  assets/                   CSS, JavaScript, images; assets/admin holds admin-only files
docs/                       Setup and API notes
```

## Customer accounts

Forgot-password codes, the customer portal, and the order reference format are documented in [docs/customer-accounts.md](docs/customer-accounts.md), including the environment variables and the SMTP setup steps.

## Admin side

Every admin page reads from the database; there is no sample data. Tables (orders, customers, payments, reports, catalog) support search, filters, sorting, and pagination, and a row opens its details. Order details show the catalog sample and every uploaded image (JPG, PNG, WebP) in a gallery with a large viewer (zoom, pan, fullscreen); the files are served only to an administrator session. Available actions depend on the order's status, ask for confirmation, and are refused with HTTP 409 if the order changed since it was opened.

Reporting rules, used by the dashboard, reports, customer totals, and exports alike:

- **Confirmed revenue** counts an order only when it is Completed *and* fully paid, once, for its confirmed total, on its completion date. Pending, partially paid, unpaid, cancelled, and rejected orders never count. A customer's **amount spent** uses the same rule.
- An order can be marked Completed only after its full amount is recorded; the completion time is stored in `orders.completed_at` (older completed orders are backfilled from their history on start-up).
- **Completion duration** = completion date − request date in calendar days (weekends included), on Philippine time.

Sales & Reports has a business summary (today/week/month/year/custom, grouped daily/weekly/monthly/yearly, per service), a completed-transactions report, and per-service reports. Each exports to PDF and Excel (`/api/admin/exports/{completed|service|summary|revenue|customers|customer|orders|payments}?format=pdf|xlsx`) with the on-screen filters; a combined service export has one worksheet per service.

## Tests

Run the backend unit tests:

```powershell
python -m unittest discover -s backend/tests -v
```

The tests use a temporary SQLite database. For an HTTP smoke test, start Uvicorn with temporary `PRINTSHOP_DATABASE_PATH` and `PRINTSHOP_UPLOAD_DIR`, then check `/api/health`, customer registration/profile/order flows, and admin login/order transitions.

## Production container

The included Docker image serves the frontend and API on the same origin. It uses SQLite and local uploads on `/data`, so deploy it with a persistent writable volume and a single application instance. Do not mount that volume read-only or run multiple replicas against the same SQLite database.

1. Copy `.env.example` to `.env`.
2. Generate an administrator password hash with `python -m backend.app.core.generate_admin_hash`.
3. Set a unique random `PRINTSHOP_SECRET_KEY` (for example, `python -c "import secrets; print(secrets.token_urlsafe(48))"`), administrator email and hash, persistent paths, and the real public host in `PRINTSHOP_ALLOWED_HOSTS`.
4. Keep TLS termination in a trusted reverse proxy and expose the application only through that proxy:

   ```powershell
   docker compose up --build -d
   ```

`PRINTSHOP_ENVIRONMENT=production` makes startup fail when required secrets, administrator credentials, persistent paths, or explicit allowed hosts are missing. `PRINTSHOP_CORS_ORIGINS` should remain empty for same-origin hosting; add only explicit trusted origins if a separate frontend is configured.

## Current operational limitations

- Production credentials, hostnames, TLS/reverse-proxy setup, persistent storage, backups, and hosting are operator-provided and have not been verified here.
- SQLite is suitable for a single-instance installation with a persistent volume, not horizontally scaled deployments. A PostgreSQL migration and deployment-specific storage adapter are not implemented.
- Customer accounts support email/password sign-in and emailed password-reset codes. Email only works once SMTP credentials are configured (see [docs/customer-accounts.md](docs/customer-accounts.md)); the unit tests use a stub, and the full flow has been checked end to end against a local SMTP server, but not against a real mail provider. Google sign-in is not implemented. Outbound chat notifications and online payment processing are not configured. No payment is recorded automatically.
- The product catalog (categories, products, each product's order-form fields and options, and prices) is stored in the database and managed from Admin → Services & Products and Pricing Management. It is seeded once on first start with the six service categories and no prices; every product stays on "Request a quote" until an administrator enters a price. Order estimates are calculated on the server from those prices; the administrator still confirms each order's final amount. Online card/e-wallet checkout is not implemented.
- `PRINTSHOP_CORS_ORIGINS` is for explicitly allowed cross-origin API clients; the current frontend uses relative same-origin API URLs and should be served through FastAPI or a same-origin reverse proxy.

This project is not certified production-ready until real environment values, TLS, persistence, backups, and the deployed host have been configured and tested.
