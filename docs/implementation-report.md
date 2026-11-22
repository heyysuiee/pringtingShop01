# ImPRESSion PrintShop — Enhancement Implementation Report

Date: 2026-11-21 · Scope: e-wallet payments, delivery fees, production monitoring, quality checking, packaging, store pickup, courier delivery, cancellations, refunds, reports, and the matching customer and admin screens.

## 1. Executive summary

The existing FastAPI + SQLite/Turso application was extended in place; nothing was rebuilt, no data was reset, and every pre-existing test still passes. New workflows are enforced on the server and exposed through new admin pages (Production Monitoring, Fulfillment Management, Cancellation & Refunds), new Settings panels (payment methods, delivery/couriers/pickup), and an expanded customer portal (tracking timeline with dates, e-wallet payment cards, pickup code, courier tracking, cancellation/refund/issue requests, printable receipt).

Verification: **276 automated tests pass** (251 existing + 25 new) and a **25-step browser end-to-end run** (Chrome, headless) passed against an isolated server with a throwaway database. The system is **not certified production-ready**: real merchant accounts, delivery fees, pickup addresses, and SMTP must still be configured, and the work has not been tested against the live Turso database.

## 2. Initial audit findings

Already implemented and working (kept): customer accounts and sessions; fixed-price package/standard-size orders and quotation-based customized orders with revision history; 50% down payment computed by the server; customer payment submission (GCash only) with admin verify/reject, duplicate-reference blocking and optimistic versioning; draft upload/approval; pickup/delivery choice with address; customer notifications (deduplicated, optional email); append-only audit log; reports with revenue recognized only for completed + fully paid orders; PDF/Excel exports; backups; chatbot.

Gaps found: payment method hard-coded to GCash; no delivery fees or areas; no quality-checking or packaging stages; orders released before full payment; no courier/tracking, delivery attempts, failures, or pickup verification; no draft history (only the latest draft); no reviewed cancellation for paid orders; no refund module; reports had no refund/net-collection figures; the portal timeline had no dates and no failed states.

Database: the app uses SQLite locally and Turso (hosted SQLite) on Vercel through the same SQL. No MySQL. Orders store nested data in JSON columns; this pattern was reused.

## 3. Features implemented

- **Configurable e-wallets** — `payment_methods` table; provider, account name/number, QR image, instructions, active flag, display order. Customers see only active methods; submissions must name an active method. GCash seeded active, Maya seeded **inactive** (no account invented).
- **Delivery fees & estimates** — areas (Luzon/Visayas/Mindanao by default, custom areas by province list), fee and estimated courier days per area, delivery on/off, couriers (J&T Express default), pickup locations. The fee is calculated on the server from the province; a chosen area must match the address. Fixed-price totals and quotations include the fee; the 50% split uses the total (₱2,000 + ₱150 → ₱1,075 / ₱1,075, covered by a test).
- **Draft history** — every draft is kept with remarks, status, and the customer's revision instructions (no longer a fixed text).
- **Production** — progress, personnel, printed/completed quantities, estimated completion, remarks, photos, update log.
- **Quality checking** — five-item checklist; pass → Packaging, fail → back to In Production (defects and corrective action required).
- **Packaging** — packed quantity, remarks, proof. Fully paid → released; otherwise **Awaiting Full Payment**, released automatically when the balance is verified.
- **Pickup** — one-time pickup code shown only to the customer; release needs claimant name, customer/authorized type (authorization details required), the code, and acknowledgment; claimable once.
- **Delivery** — dispatch with courier, service, tracking number (unique across orders) or own rider, dates; Delivered needs proof; Delivery Failed needs a reason; reschedule creates a new attempt; all attempts kept. Customers can report delivery issues; admins resolve them.
- **Cancellation requests** (paid orders) — reason, remarks, policy acknowledgment, refund recipient; puts production/payment/dispatch on hold; admin approves (with documented deductions + justification; order Cancelled; refund created) or rejects (order continues). Pending payment submissions must be resolved first. The proposed policy table is shown as guidance only.
- **Refunds** — separate records: Pending Review → Approved → Processing → Refunded, plus Rejected, Not Eligible, Failed (retry requires confirming the failed transfer was not received), and Resolved by Reprint for damaged/incorrect products. Maximum = verified payments − completed refunds − refunds already approved/in progress − deductions. Completion requires method, unique transaction reference, proof, verified recipient, and confirmation.
- **Reports** — verified collections, completed refunds, refunds in progress, net collections, delivery-fee share of collections; new filters (fulfillment, status, customer, payment method). Revenue rule unchanged.
- **Dashboard & badges** — status counts, quality checking, packaging, ready for pickup/dispatch, out for delivery, pending cancellations/refunds, net collections; sidebar badges for the new modules.
- **Notifications** — production started, quality check completed, packaging completed, balance due, ready for pickup/dispatch, out for delivery, delivery failed/rescheduled, delivered, claimed, draft approved, revision received, cancellation received/approved/rejected, refund requested/approved/declined/failed/completed, issue resolved. Each has a dedupe key.

## 4. Existing features preserved

Order reference format and generation, idempotent submission, quotation history, payment verification rules, audit log, notifications, reports/exports, backups (new columns and table included), chatbot (status explanations updated for new stages), catalog/packages/designs, legacy COD records. Orders released before this change (Ready for Pickup / Out for Delivery without a packaging record) can still be completed directly once fully paid.

## 5–6. Files

Modified: `backend/app/main.py`, `models/order.py`, `schemas/order.py`, `repositories/order_repository.py`, `services/order_services.py`, `services/report_services.py`, `services/backup_services.py`, `services/chatbot_services.py`, `tests/test_order_workflow.py` (3 tests updated to the new release rule), every admin page's sidebar, `frontend/admin/Settings.html`, `assets/admin/dashboard.js`, `customer/order.html`, `assets/order.js`, `customer/customer-portal.html`, `assets/portal.js`.

New: `repositories/payment_method_repository.py`, `services/shop_settings_services.py`, `services/fulfillment_services.py`, `services/refund_services.py`, `routes/operations.py`, `tests/test_operations.py`, `frontend/admin/ProductionMonitoring.html`, `FulfillmentManagement.html`, `CancellationsRefunds.html`, `assets/admin/operations-admin.js`, `assets/admin/operations-admin.css`, `assets/portal-tracking.js`, `assets/portal-tracking.css`, this report.

## 7. Database changes (additive only)

- `orders`: new JSON columns `production`, `drafts`, `cancellation_requests`, `refunds` (defaults `{}` / `[]`), added with `ALTER TABLE ... ADD COLUMN` on startup. Delivery/pickup details extend the existing `fulfillment` JSON.
- New table `payment_methods`, seeded once (marker `payment_methods.seeded` in `app_meta`).
- Fulfillment settings stored in `app_meta` key `shop.fulfillment_settings`.
- Rollback: the new columns and table are ignored by the previous code version, so redeploying the previous version works without a schema change. Take a backup (Settings → Backup database) before deploying.

## 8. API changes

Public: `GET /api/payment-methods`, `GET /api/payment-methods/{id}/qr`, `GET /api/fulfillment-options`.
Customer: `POST /api/orders/{ref}/cancellation-request`, `/refund-request`, `/delivery-issue`; `/payments` now accepts any active `method`; `/request-changes` stores the customer's note in the draft history.
Admin: `GET /api/admin/production|fulfillment|cancellations|refunds`; `POST /api/admin/orders/{ref}/production|quality-check|packaging|dispatch|delivery/delivered|delivery/failed|delivery/reschedule|pickup/schedule|pickup/confirm|refunds`, `/issues/{id}/resolve`, `/cancellations/{id}/approve|reject`, `/refunds/{id}/approve|reject|not-eligible|processing|complete|fail`; `GET/POST/PATCH/DELETE /api/admin/settings/payment-methods`, `GET/POST /api/admin/settings/fulfillment`. Admin order detail includes new `actions` flags, couriers, `max_refundable`, and policy guidance. Admin pages `/admin/production`, `/admin/fulfillment`, `/admin/refunds`.

## 9–10. Workflows

Customer: order (delivery fee shown at checkout) → quotation/approval → pay down payment with a chosen e-wallet card → draft approve/revise → follow progress on a dated timeline → pay balance when packed → pickup code or courier tracking → report issue / request cancellation or refund → print receipt.

Administrator: review/quote → verify payment → drafts → Production Monitoring (progress, QC, packaging) → Fulfillment Management (pickup release, dispatch, delivered/failed/reschedule, issues, history) → Cancellation & Refunds → reports.

## 11–12. Configuration required before go-live

- Settings → Payment methods: enter the shop's GCash account name, number, and QR; activate Maya only after adding its account.
- Settings → Delivery: set the real fee per area (all start at ₱0), confirm/adjust estimates (Mindanao has none), add pickup location(s) with the real address, optionally add a J&T tracking page address containing `{tracking}`.
- Approve or adjust the proposed cancellation policy (it is displayed as guidance; nothing is enforced automatically).

## 13. Security

Ownership checks on every customer action; admin-only settings and actions; amounts, fees, balances and refund limits computed on the server; delivery area must match the address; unique payment references, tracking numbers, and refund transaction references; refund completion requires proof and confirmations; file uploads keep the existing type/signature/size checks; pickup codes hidden from the admin view; all changes audited and versioned (stale actions refused with 409); no passwords/PINs/OTPs requested or stored.

## 14–15. Test results

- Automated: `python -m unittest discover -s backend/tests` → **276 tests, OK**. New coverage (`test_operations.py`): GCash/Maya/custom methods, inactive/unknown methods, QR serving, last-method protection, proof validation, delivery fee and 50/50 split, area mismatch, delivery disabled, settings validation, draft history and revisions, production gating and validation, QC pass/fail, packaging, pickup code/claimant/duplicate claim, unpaid release blocked, legacy completion, dispatch/duplicate tracking/failure/reschedule/proof/history, delivery issues, fulfillment and production lists, unpaid vs paid cancellation, hold during review, deductions, rejection, pending-verification ordering, full/partial/duplicate/excess refunds, failure and retry, reprint, duplicate transfer reference, authorization, and report figures.
- Browser (Chrome headless, isolated server and database): 25/25 checks passed — portal quotation with fee, e-wallet card payment, draft revision, admin dashboard, production/QC/dispatch/cancellation forms, customer tracking timeline (future steps pending), settings panels, checkout areas. No JavaScript errors; only a pre-existing missing favicon (404).

## 16–18. Known limitations and outstanding integrations

- No live GCash/Maya API or webhooks: payments and refunds remain manually verified (by design, as required).
- No live J&T Express API: tracking numbers are entered manually; a tracking link appears only if a tracking page address is configured.
- Delivery fees, merchant accounts, and pickup addresses are not set (no real values were invented).
- "Reprint" resolves the refund request but does not create a new production run automatically; the shop handles the reprint and can record it in the order history.
- Duplicate/excess payments refunded through the refund module are not added to an order's paid amount (the system still refuses recording payments above the balance), so the refund is capped by verified payments.
- Not tested against the live Turso database or on Vercel; the payment-method QR and settings use the same storage paths as existing files.
- Responsive layout was checked at 1280 px, 1440 px and a 390 px phone width for the order page; not every admin page was reviewed on a phone.

## 19. Deployment

No new environment variables or dependencies. Deploy as before; the schema migrates on startup. Back up first, then configure Settings as listed in section 11.

## 20. Recommended next steps

1. Enter real payment, delivery, and pickup settings, then run one real order end to end on the deployed site.
2. Have the shop owner approve the cancellation policy text.
3. Consider a reprint action that reopens production for affected quantities.
4. Add a favicon to the customer pages.

---

# Second audit (2026-11-21)

## Verified features

Every feature listed below was exercised through the HTTP API in automated tests and, where marked (UI), in a headless Chrome browser against an isolated server with a throwaway database:
GCash/Maya/custom e-wallet settings and QR codes (UI) · e-wallet payment submission and manual verification (UI) · 50% down payment, multiple partial payments, remaining balance, overpayment refusal · delivery fees by area and the area/address match · production progress (UI) · draft revision history (UI) · quality checking pass/fail (UI) · packaging and Awaiting Full Payment · pickup code, claimant verification, single claim · J&T dispatch with tracking (UI), duplicate tracking refusal, delivery failure, rescheduling, proof of delivery · customer delivery issues · paid-order cancellation requests (UI) with deductions · refund approval, processing, failure/retry, completion · net collections and historical reports · customer tracking timeline with dates (UI) · printable receipt (UI) · reprints (UI).

## Bugs found and fixed in this audit

1. **Reprint did not restart production** (known limitation) — replaced by a full reprint workflow (below).
2. **Reprint quantity ignored by production/packaging checks** — packed and completed quantities were validated against the original order quantity, so a 1-piece reprint could be recorded as 2 packed pieces. Checks now use the current cycle's quantity.
3. **Archived waybill reuse** — after a reprint, the first parcel's tracking number could be reused for the new parcel. Tracking numbers from earlier cycles are now refused (redelivery of the same parcel within a cycle may still reuse its number).
4. **Re-completion would rewrite history** — completing an order again after a reprint would have moved its completion date and dropped its revenue while the reprint ran. The first completion date is now kept, and the original revenue stays recognized during a reprint.
5. **Mobile dashboard overflow** (existed before the enhancement) — the 12-month revenue chart made the dashboard 522 px wide on a 390 px phone. The chart now scrolls inside its box.
6. **Mobile tables broke words** — order references and customer names wrapped letter by letter; reference cells now keep a minimum width and the table scrolls sideways.
7. **Mobile layout** — production/fulfillment stat cards stacked one per row (now two columns); the 19-step tracking timeline was a clipped sideways-scrolling grid (now a vertical list on phones); small checkboxes enlarged to 18 px.

## Reprint workflow

`POST /api/admin/orders/{ref}/reprint` (or resolving a damaged/incorrect-product refund request with "reprint") for Delivered, Order Claimed, or Completed orders. Requires the administrator's explicit authorization and a reason; records quantity, free or chargeable (a charge needs the customer's agreement and is added to the balance; a free reprint adds no payment), restart stage (In Production with the approved artwork, or Draft Preparation when the artwork must change), who authorized it, and the linked refund request. The current production record and the delivery/pickup cycle (attempts, proofs, tracking, pickup confirmation, pickup code) are archived, never deleted; a new linked production cycle starts and must pass quality checking and packaging again before release. Only one reprint can be in progress. The reprint is marked completed when the order is delivered or claimed again.

## Tests executed

- Local SQLite: `python -m unittest discover -s backend/tests` → **292 tests, OK** (251 original + 25 from the first round + 16 in `test_audit_followup.py`: reprints, financial accuracy, migration, backup/restore, concurrency, transaction integrity, security).
- Turso: see "Database compatibility" below.
- Browser: original 25-step flow re-run (25/25) and a reprint/receipt flow (7/7); no JavaScript errors (only a missing favicon 404, pre-existing).
- Responsive: every customer page (8) and admin page (13, plus the order dialog) at 390 px, 820 px, and 1440 px — 66 page views checked automatically for horizontal overflow, with screenshots reviewed for the key screens. After the fixes, **no page overflows at any width**; the mobile sidebar opens from the menu button and its links are reachable.

## Database compatibility

- **SQLite**: an orders table in the pre-enhancement layout, with a record, was migrated on startup: the record (status, payments, delivery address) is unchanged; the new columns and the `payment_methods` table are added.
- **Turso**: the live Turso database was **not** touched. The application's Turso adapter was tested against a local emulator of Turso's HTTP protocol (Hrana `/v2/pipeline`, backed by SQLite): the full test suite ran through the adapter with database file storage (as on Vercel), and the same old-schema migration succeeded through it. The only failures are two tests that check local-file behavior by design (one patches the local SQLite file path; one asserts that an upload is a file on disk, while Turso mode stores files in the database). An emulator is not Turso itself: verify on a Vercel preview deployment with a copy of the database before production.
- Backup and restore round-trip includes the new columns and the payment-method table; concurrent actions with the same version apply exactly once (others get 409); a refused action writes nothing.

## Security audit results

Verified by tests: other customers get 404 for orders, files, payment, cancellation, refund, and issue endpoints; signed-out requests get 401; customers get 401/403 on every admin endpoint (payment verification, production, quality check, dispatch, pickup, reprint, refunds, settings, lists) and cannot verify their own payments; cross-site requests are refused (403); the status endpoint cannot skip workflow steps; uploads with mismatched signatures, SVGs, executables, and PDF QR codes are refused; inactive/unknown QR codes are not served.

## Deployment readiness

Ready for a **staging** deployment. Not yet production-ready until: (1) the shop's real GCash/Maya accounts and QR codes, delivery fees, and pickup address are entered in Settings; (2) the cancellation policy is approved by the shop owner; (3) the deployment is checked on a Vercel preview with a copy of the Turso data (back up first); (4) SMTP is confirmed if emailed notifications are wanted.

## Outstanding issues (not fixed)

- No live GCash/Maya/J&T APIs (manual verification and manual tracking numbers, by design).
- Small text in the admin sidebar (9–10 px brand caption and badge counts) and 18 px-high table sort buttons are part of the existing design and were left unchanged.
- No favicon on customer pages (404 in the browser console).
