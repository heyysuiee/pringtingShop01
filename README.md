# ImPRESSion PrintShop

FastAPI serves the existing HTML/CSS/JavaScript storefront and admin pages from one origin. The current order workflow defaults to local SQLite and disk uploads for self-hosting; Vercel uses Turso with database-backed files. Passwords for customer accounts are PBKDF2-SHA256 hashes and sessions use signed, HttpOnly cookies.

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

## Order, quotation, and payment workflow

**Every order requires a 50% down payment** (package and customized orders alike). The down payment and the remaining 50% balance are calculated by the server from the agreed total and returned as `payment.down_payment_required`, `payment.remaining_after_down_payment`, and `payment.next_payment` (the amount the customer pays next). Cash on Delivery is no longer offered; orders placed while it was are still shown and finished, and the administrator can switch them to the down payment.

**Fixed-price package orders.** Package prices and inclusions are edited in Admin → Services & Products (Packages tab, or the product's editor) and appear on the order page immediately. The order total is the package's published price plus its add-on rate, calculated by the server, and the package's name, price, and inclusions are saved with the order (`customization.package`), so later package edits never change existing orders. The administrator approves the order and shares the GCash instructions (`POST /api/admin/orders/{ref}/confirm`); the customer is notified and the order waits for the down payment.

**Packages for every service.** Any product (invitations, souvenirs, stickers, Sintra boards, party needs, tarpaulins and large format) can have packages, managed in Admin → Services & Products, on the Packages tab or from the product's editor. Each package has its own base price, minimum order quantity (`included_quantity`, also returned as `minimum_quantity`), optional additional price per piece (`add_on_price`; blank means a fixed quantity), and inclusions (`inclusion_details`: name + optional description; `inclusions` lists the names). The server calculates `total = base price + (quantity − minimum) × additional price` for the live estimate and again when the order is saved, refusing quantities below the package minimum. No packages are created for these services automatically; the shop adds its own.

**Shop-made designs.** "Customize Order" opens the shop's designs for every product that has any: in the **Customize Design** step the customer chooses a design from the gallery ("Choose your design", with a larger preview), and the details form opens right below it, asking only for what that design needs (celebrant's name, age, event date, time, location, and so on). The print specifications follow in the next step; a shop-design order takes no files (the server refuses design orders with uploads, `422`). The last card of the gallery, **Use my own design**, opens the product's regular customize form with its design-file and reference uploads, with a link back to the shop's designs. Products with no designs yet go straight to that form. Design orders are priced like customized orders (catalog estimate, then the administrator's quotation) and are offered only for products that accept customized orders. Designs are managed in Admin → Services & Products → Designs (sidebar: Design Management, `/admin/services#designs`): upload a design image (JPG/PNG/WebP, stored in the database so it survives on Vercel), set its name, description, style, service category, products, the personalization details customers fill in, and whether it is active. The gallery starts with 24 **TEMPORARY SAMPLE** designs (SVG previews in `frontend/assets/image/designs`, seeded once from `backend/app/database/design_seed.py`), labelled "TEMPORARY SAMPLE" in the admin and "Sample design" for customers. Replace a sample with the shop's real design (Replace button), or remove all samples at once. Each order keeps a snapshot of its design (`customization.design`: name, image, the customer's details), and an image used by an order is never deleted, so editing, replacing, or deleting designs never changes existing orders.

**Invitation standard sizes.** Each invitation type has its own standard size, preselected on the order form and shown on the product and in the services list: Birthday and Christening 4 × 6 in, Wedding 5 × 7 in (the default option of the product's "Invitation size" field, editable in the field editor). The chosen size is saved in the order's specifications and shown in the customer portal and the admin order details. A package without its own size records the invitation type's standard size. Other sizes stay available; "Custom size" is quoted.

**Tarpaulin size price list.** Tarpaulin / large-format products use the pricing method "Standard size price list": the order form shows the category's active sizes with their prices (2 × 3 ft ₱200, 2 × 4 ft ₱250, 3 × 4 ft ₱300, 3 × 5 ft ₱350, 4 × 5 ft ₱400, 4 × 6 ft ₱500, 4 × 7 ft ₱550, 4 × 8 ft ₱600) and the server prices the order as **size price × quantity** (plus any priced options). Such an order is a fixed-price order like a package: the administrator approves it (no quotation) and the customer pays the 50% down payment. The size and price are saved with the order (`customization.price_list`), so later price changes never change it. "Custom size (request a quote)" asks for the width and height and goes through the normal quotation workflow. Sizes live in the `standard_sizes` table and are managed in Admin → Services & Products → **Size prices** (add, edit, deactivate, delete; `GET/POST /api/admin/catalog/sizes`, `PATCH/DELETE /api/admin/catalog/sizes/{id}`).

**T-Shirt Printing.** A seventh service category with two separate services and order forms: **Heat Press Printing** (shirt type, color, size, print placement Front/Back/Front and Back, print size) and **Sublimation Printing** (polyester shirt type, size, full or partial sublimation; placement only for partial). Both take the customer's design upload, an optional reference image, text/names to print, and special requests; "Mixed sizes" or kids' sizes ask for a size breakdown. Each service has its own base price and option prices (no prices are seeded: they stay on "Request a quote" until the shop enters them), so Heat Press and Sublimation prices never mix. Design templates are shop designs of the T-Shirt Printing category (Designs tab); customers may pick one or upload their own.

**Large uploads.** Order files are uploaded in pieces of at most 2 MiB (`POST /api/uploads`, `PUT /api/uploads/{id}/chunks/{n}`, `POST /api/uploads/{id}/complete`) before the order is submitted, so every request stays under hosting limits (Vercel: 4.5 MB). Limits are per file and per product: `PRINTSHOP_LARGE_FORMAT_MAX_UPLOAD_MB` (default 25) for the categories in `PRINTSHOP_LARGE_UPLOAD_CATEGORIES` (default `large-format`), `PRINTSHOP_MAX_UPLOAD_MB` (default 5) otherwise; an order's files together may be up to three full-size files (at least 30 MB). JPG/JPEG, PNG, WebP, and PDF only; extension, declared type, and file signature must agree; files are stored under random names. Only the customer who uploaded a file can attach it, once, to an order of the same product and field. `PRINTSHOP_FILE_STORAGE=database` (the default when Turso is configured) keeps order files, payment proofs, drafts, and product photos in the database, so they survive on Vercel; `disk` uses `PRINTSHOP_UPLOAD_DIR`. Files are served only to the order's owner or an administrator.

**Customized order availability.** Each product has a "Customized orders" setting (Admin → Services & Products → product editor; `allow_customize`, enabled by default and for every product that existed before the setting). When disabled, the order page hides "Customize Order" for that product and the server refuses customized orders and estimates for it (`422`); package orders, prices, and existing orders and quotations are not affected. A product with customized orders disabled and no active package cannot be ordered, which the admin product table flags.

**Customized orders** (and packages whose options need a quote):

1. The customer submits specifications and reference images. No price is assigned; the catalog estimate is shown to the administrator only as a reference.
2. The administrator enters the customization price by hand (`/quotation`) and can revise it until money is reported. Every quotation is kept in `quotation_history` (revision, amount, previous amount, note, sent time, sender, the customer's response, and when it was superseded). The customer is notified of each new or revised quotation.
3. The customer reviews the latest quotation and clicks **Accept Quotation** or **Decline Quotation** (`/api/orders/{ref}/quotation/accept|decline`, with the `quotation_revision` they saw). An outdated revision is refused. Accepting moves the order to Awaiting Down Payment; a declined quotation can never be paid, and the administrator may send a revised one.

**Payment and confirmation (both order types).**

1. The customer pays through GCash and submits the GCash reference number with the screenshot or receipt (`/api/orders/{ref}/payments`). The amount is set by the server; a different amount is refused. The submission is saved as **Pending Verification** and never counts as paid.
2. The administrator verifies or rejects it (`/payment-submissions/{id}/verify|reject`). When verified payments cover the 50% down payment, the order moves automatically to **Confirmed** (an active order) and the customer is notified once that the down payment is verified and the order is confirmed. Nothing else, including an uploaded screenshot or a manual status change, can confirm an order.
3. Confirmed → Draft Preparation → customer approval or revision requests → **In Production** (needs an approved draft and the verified down payment) → **Ready for Pickup** or **Out for Delivery** (from the order's fulfillment choice) → **Completed** (only when fully paid; the balance is paid the same way or recorded by the administrator).

**Delivery or pickup** is chosen at checkout, independent of payment. Delivery requires the recipient name, contact number, house number/street, barangay, city/municipality, and province (landmark optional). Orders placed before this existed count as store pickup.

**Order security.**

- Only the signed-in account that owns an order can accept or decline a quotation, submit a payment, approve a draft, or cancel. An email address and reference number are enough to view an order, never to change it.
- Prices and payment amounts come from the server; values sent by the customer are ignored or refused. Only an administrator session can verify, reject, or record payments.
- A submission can be verified once and becomes exactly one payment record; each action carries the order version, so a repeated or out-of-date action is refused with HTTP 409. Only one payment can wait for verification at a time, a GCash reference number can be used once across all orders, and payments cannot exceed the balance.
- Customer notifications (`customer_notifications` table, `GET /api/account/notifications`, `POST /api/account/notifications/read`) have a unique key per event, so the same event never notifies twice. When SMTP is configured, each new notification is also emailed once.
- Every important order, quotation, and payment change is written to the append-only `order_audit_log` table in the same transaction as the change (action, admin or customer, time, and details such as status and price before/after). Administrators see it under **Audit history** in the order details (`GET /api/admin/orders/{ref}/audit`).
- Outstanding balances and customer transaction totals count only agreed prices: an unaccepted quotation or an unapproved package order is not money owed. Orders with reported or recorded payments cannot be cancelled.

## Production, fulfillment, payments, cancellations, and refunds

After the customer approves the draft: **In Production → Quality Checking → Packaging → (Awaiting Full Payment) → Ready for Pickup → Order Claimed** or **Ready for Dispatch → Out for Delivery → Delivered (or Delivery Failed → rescheduled)** → **Completed**. Nothing is released before the order is fully paid. Customers pay with any active e-wallet configured in Admin → Settings → Payment methods (GCash active, Maya inactive until its account is added); delivery fees, areas, estimated courier periods, couriers (J&T Express default), and pickup locations are in Admin → Settings → Delivery, couriers & pickup, and the fee is calculated by the server. Paid orders are cancelled through a reviewed cancellation request; refunds are separate records (Pending Review → Approved → Processing → Refunded). Net collections = verified collections − completed refunds. **Reprints** (Delivered, Order Claimed, or Completed orders; from the order details or by resolving a damaged/incorrect-product refund request) need the administrator's authorization, record the reason, quantity, and whether they are free or chargeable (a charge needs the customer's agreement; a free reprint adds no payment), archive the previous production and delivery/pickup cycle, and restart at In Production or Draft Preparation, so quality checking and packaging happen again. Only one reprint can be in progress; reports keep the original completion date and revenue. If a cancellation is approved while a reprint is in progress, the reprint is closed as cancelled in the same action and the order stops counting as completed in reports; the refund still never exceeds verified payments (an unpaid reprint charge is not refundable). Details: [docs/implementation-report.md](docs/implementation-report.md).

## Admin side

Every admin page reads from the database; there is no sample data. Tables (orders, customers, payments, reports, catalog) support search, filters, sorting, and pagination, and a row opens its details. Order details show the catalog sample and every uploaded image (JPG, PNG, WebP) in a gallery with a large viewer (zoom, pan, fullscreen); the files are served only to an administrator session. Available actions depend on the order's status, ask for confirmation, and are refused with HTTP 409 if the order changed since it was opened.

Reporting rules, used by the dashboard, reports, customer totals, and exports alike:

- **Confirmed revenue** counts an order only when it is Completed *and* fully paid, once, for its confirmed total, on its completion date. Pending, partially paid, unpaid, cancelled, and rejected orders never count. A customer's **amount spent** uses the same rule.
- An order can be marked Completed only after its full amount is recorded; the completion time is stored in `orders.completed_at` (older completed orders are backfilled from their history on start-up).
- **Completion duration** = completion date − request date in calendar days (weekends included), on Philippine time.

Sales & Reports has a business summary (today/week/month/year/custom, grouped daily/weekly/monthly/yearly, per service), a completed-transactions report, and per-service reports. Each exports to PDF and Excel (`/api/admin/exports/{completed|service|summary|revenue|customers|customer|orders|payments}?format=pdf|xlsx`) with the on-screen filters; a combined service export has one worksheet per service.

**Navigation.** The sidebar has one entry per module (Dashboard, Orders, Customers, Payments & Transactions, Sales & Reports, Services & Products, Package Management, Design Management, Notifications, Settings). Package Management and Design Management open the Packages and Designs tabs of Services & Products (`/admin/services#packages`, `/admin/services#designs`); they are not separate pages. The header bell opens a dropdown of recent notifications with a link to the Notifications page; the profile menu holds **Administrator profile** and **Log out** (the only logout).

**Payment references.** Every payment has a reference: GCash, Maya, bank transfer, and other providers require their transaction number (`422` without one); cash takes the official receipt number or, when blank, the system issues a unique `CASH-YYYYMMDD-XXXXXX` receipt number. Each payment stores who verified it and when (`payments[].verification`). Payments & Transactions lists recorded payments (**Verified**) together with customer submissions (**Pending Verification** / **Rejected**, never counted as paid) and flags older payments saved without a reference (**Reference Missing**); the administrator can add the missing number once (`POST /api/admin/orders/{ref}/payments/{n}/reference`). Payment status (Unpaid / Partially Paid / Fully Paid) is always calculated from verified payments, never from the order status.

**Customers.** Customer Management has All customers, Paid records (every agreed order fully paid), and Unpaid records (at least one agreed order with a balance) views (`?view=all|paid|unpaid`); a customer appears in at most one of Paid/Unpaid. **Print report** prints the current view and filters (A4, Letter, or Legal; optionally with each customer's order and payment history) from `/api/admin/customers-report`; a customer's profile has its own Print report.

**Notifications** are customer activity (orders, payments, quotation and draft responses, registrations). The read state is stored in the database (`app_meta`, keys `admin.notifications_*`), so it is the same on every device. `GET /api/admin/notifications` (search, source, category, read status, dates, pages), `GET /api/admin/notifications/recent` (header bell), `POST /api/admin/notifications/read` (`ids`, `all`, or `before`).

**Database backup and restore** (Settings). **Backup database** downloads a version 2 JSON file with every application table (customers, catalog, packages, orders with payments and history, audit log, customer notifications, app settings, and the database-backed upload tables `stored_files` / `file_chunks`), read from one consistent snapshot through the normal database connection, so it works the same with the local SQLite file and with Turso; no credentials are exposed. Files in `PRINTSHOP_UPLOAD_DIR` are added as base64 entries in the same file, so order images, receipts, drafts, and catalog photos are restored too. Administrator credentials, password-reset codes, and sign-in throttling records are never included or overwritten. Each file has a SHA-256 checksum and an HMAC signature made with `PRINTSHOP_BACKUP_SIGNING_KEY` (falling back to `PRINTSHOP_SECRET_KEY`); only unmodified backups from this installation can be restored. Set a dedicated, persistent backup key and list retired keys in `PRINTSHOP_BACKUP_PREVIOUS_SIGNING_KEYS` when rotating, so older backups remain restorable. Restore validates the whole file first (including file paths and base64 data), requires typing `RESTORE`, stages disk files, then replaces the tables in one transaction; if the database write fails, the disk files are rolled back too. The audit log is never cleared: every current entry is kept, the backup's entries are merged in without duplicates (even for orders the restore removes), and the restore itself is recorded as an audit entry, so a rollback cannot erase the evidence of later actions. Backup and restore events are listed on the Settings page (`app_meta` key `backup.history`). Version 1 backups (without files) are still accepted and leave the upload directory untouched. The local/API limit is 100 MB; on Vercel, the platform's smaller request-body limit still caps what can be uploaded for a restore. The `standard_sizes` table is included; older backups without it are still accepted (the current size list is kept). Production refuses `PRINTSHOP_FILE_STORAGE=disk` on Vercel/Turso or when `PRINTSHOP_UPLOAD_DIR` is a temporary directory; use a persistent volume or `PRINTSHOP_FILE_STORAGE=database`. On startup, uploads abandoned before the 24-hour grace window are cleaned up.

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
- The product catalog (categories, products, each product's order-form fields and options, and prices) is stored in the database and managed from Admin → Services & Products (base price and pricing method in each product's details; option prices in its order-form fields or the “Option prices” screen). It is seeded once on first start with the seven service categories (T-Shirt Printing included) and no prices except the published package prices and the Tarpaulin size price list; every product stays on "Request a quote" until an administrator enters a price. Order estimates are calculated on the server from those prices; the administrator still confirms each order's final amount. Online card/e-wallet checkout is not implemented.
- `PRINTSHOP_CORS_ORIGINS` is for explicitly allowed cross-origin API clients; the current frontend uses relative same-origin API URLs and should be served through FastAPI or a same-origin reverse proxy.

This project is not certified production-ready until real environment values, TLS, persistence, backups, and the deployed host have been configured and tested.
