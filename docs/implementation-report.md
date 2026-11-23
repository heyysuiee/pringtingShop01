# ImPRESSion PrintShop — Enhancement Implementation Report

Date: 2026-11-21 · Scope: e-wallet payments, delivery fees, production monitoring, quality checking, packaging, store pickup, courier delivery, cancellations, refunds, reports, and the matching customer and admin screens.

## 1. Executive summary

The existing FastAPI + SQLite/Turso application was extended in place; nothing was rebuilt, no data was reset, and every pre-existing test still passes. New workflows are enforced on the server and exposed through new admin pages (Production Monitoring, Fulfillment Management, Cancellation & Refunds), new Settings panels (payment methods, delivery/couriers/pickup), and an expanded customer portal (tracking timeline with dates, e-wallet payment cards, pickup code, courier tracking, cancellation/refund/issue requests, printable receipt).

Verification for the original enhancement pass included a **25-step browser end-to-end run** (Chrome, headless) against an isolated server with a throwaway database. Automated test counts changed during later QA passes; the current consolidated count is recorded in `docs/qa-remediation-report.md`. The system is **not certified production-ready**: real merchant accounts, delivery fees, pickup addresses, and SMTP must still be configured, and the work has not been tested against the live Turso database.

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

Ready for a **staging** deployment. Not yet production-ready until: (1) the shop's real GCash/Maya accounts and QR codes, delivery fees, and pickup address are entered in Settings; (2) the cancellation policy is approved by the shop owner; (3) the deployment is checked on a Vercel preview with a copy of the Turso data (back up first); (4) SMTP is configured, or email verification is explicitly disabled outside production, because production registration requires one of those two options.

## Outstanding issues (not fixed)

- No live GCash/Maya/J&T APIs (manual verification and manual tracking numbers, by design).
- Small text in the admin sidebar (9–10 px brand caption and badge counts) and 18 px-high table sort buttons are part of the existing design and were left unchanged.
- No favicon on customer pages (404 in the browser console).

---

# Third audit — UI/UX & validation implementation (2026-11-23)

This pass followed `docs/ui-ux-improvement-plan.md`, which was written before implementation. It was deliberately surgical: working workflows, business rules, database relationships, authentication, APIs, and order/payment/cancellation/refund/delivery behavior were preserved. No migration, dummy data, duplicate feature, or broad rewrite was introduced.

## 1. Problems identified

- **Customer delivery address was unverifiable.** Three free-text inputs allowed mismatched province/municipality/region combinations. The server assumed an unknown province was Luzon, which could then fail the delivery-area check later in checkout.
- **Checkout payment copy was static.** GCash wording lived in `order.html`; available methods, instructions, account details, proof limits, and reference rules were only partly dynamic in the portal, and the proof-size label could drift from the server setting.
- **Portal actions were buried.** Draft approval/revision and payment actions sat below status, timeline, quotation, and request panels; re-rendering could also scroll the customer back to the top after an action.
- **Order-page density was inconsistent.** The design gallery was tall, “Use my own design” was last, personalization appeared after all gallery cards, and quantity/estimate/notes appeared in more than one place with inconsistent step numbers.
- **Customer input guards were weak.** No frontend PH phone pattern or trimmed required-text checks; proof validation checked extension/size but not MIME; double-clicks could queue duplicate POSTs; field-level backend errors were not mapped back to inputs.
- **Admin hierarchy hid urgent work.** The dashboard led with 16 equal-weight cards; order and customer tables were click-only; payment verification was not the first-class queue; reports combined many unrelated sections on one tab.
- **Admin consistency issues.** Catalog dialogs leaked the order modal’s XL class; several dialogs used weaker local copies of shared helpers; numeric parsing could produce `NaN`; cancellation/refund forms had no client-side cap after deductions; some forms had no `reportValidity()` or busy guard.
- **Validation gaps on both sides.** Order and admin action schemas were almost entirely optional and ignored unknown fields; cancellation/rejection required no reason; phone validation accepted almost any digit string; inline JSON proof/draft uploads lacked the streaming body cap; event dates had no business-date rule; admin login throttling was only in-process.

## 2. UI/UX improvements

- **Hierarchy first:** each main screen now leads with the decision the user must make — “Needs your action” on the portal, “Needs attention” on the dashboard, pending submissions in Payments, and explicit Open/Verify actions instead of click-only rows.
- **Denser but calmer layout:** scoped CSS compacts section padding, table rows, modal sections, and gallery cards without changing shared components globally. Secondary portal panels use the existing `detail-toggle` behavior.
- **Consistent interaction patterns:** shared `AdminUI.DataTable`, `detail-toggle`, `wallet-card`, `field-error`, `PrintConfirm`, and `withBusy` patterns are reused instead of local copies.
- **Better scanning:** admin order tables use a focused column set with explicit actions; order-detail modals have section anchors and a scroll spy; status chips jump to their section; combined KPIs route to the correct fulfillment view.
- **Accessible and resilient forms:** labels, focus anchors, validity reporting, busy/disabled states, and distinct alert styling for field errors were added. Loading, empty, and error states are preserved and made more visible.
- **Responsive intent:** existing responsive breakpoints were retained and scoped overrides avoid reintroducing the horizontal overflow fixed in the previous audit.

## 3. Customer changes

- **Dependent location selects:** province → municipality/city dropdowns are populated from a bundled read-only PSGC snapshot via public `GET /api/locations`. Selecting a province resets the municipality and derives the region; barangay remains a required text field. The matching delivery area is retried after either locations or fulfillment options load, so the displayed fee is correct regardless of which request finishes first. The dataset contains 82 provinces and 1,633 cities/municipalities, including Metro Manila (NCR).
- **Server-driven payment presentation:** `/api/payment-methods` now also exposes configured state, instructions, account details, proof requirements, `max_proof_bytes`, and `allowed_proof_types`. Order and portal copy no longer hardcodes GCash or invents a proof-size limit.
- **Portal action strip:** pending draft approval/revision, payment, and other customer actions are moved to the top of order detail. `loadOrder()` is the only action path that requests a scroll, so routine actions no longer jump the page to the top.
- **Order page cleanup:** “Use my own design” comes first, the gallery collapses after selection, duplicate quantity/estimate/notes blocks were consolidated, and step numbering was made consistent.
- **Input and submit guards:** PH phone patterns, trimmed required fields, MIME checks for proof files, field-error mapping, and duplicate-submit guards were added.
- **Scoped CSS and copy:** order/portal spacing overrides are scoped to their pages; field alerts have a distinct style; shop email/GCash wording was replaced with configured/server copy.

## 4. Admin changes

- **Dashboard:** four primary KPI cards, a “Needs attention” action panel, revenue, and recent orders now precede secondary metrics. The combined KPI routes to production or fulfillment based on where release work is blocked.
- **Orders and customers:** compact, explicit-action tables; order modal section navigation with anchors and scroll spy; focused customer table columns; smaller modal sections without cross-module class leakage.
- **Payments:** pending submissions are a queue-first view with explicit Verify action; down-payment/additional/full-payment labels are clear; manual amount entry is capped to the remaining balance.
- **Catalog:** compact product table, shared helpers restored, finite-number validation, modal-class leak fixed, consistent form busy states, and zero-cost additional pieces remain editable to match the backend price rule.
- **Operations:** cancellation/refund amounts are capped against `max_refundable` after existing deductions; deductions reduce the displayed refund cap; status reasons are enforced for Cancelled/Rejected changes, and the admin confirm dialog now requires a reason for both Rejected and Cancelled.
- **Reports and settings:** executive summary plus drill-down tabs replace the single overloaded view; payment-method and fulfillment settings remain operator-configured with no invented account, fee, or address values. The “Payments to verify” attention tile now applies the pending filter before navigating.
- **Shared core:** table row-click handling now ignores `summary, details`, preventing `<details>` expansion in the product table from opening an unrelated dialog.

## 5. Validation improvements

- **Location:** `/api/locations` is public and read-only; province/municipality combinations are validated client-side and server-side before fee/area calculation. No database schema change was needed.
- **Address:** service-layer checks require nonblank province, municipality/city, and barangay and reject unmatched combinations instead of silently defaulting to Luzon.
- **Phone:** PH mobile formats (`09XX XXX XXXX`, `+63 9XX XXX XXXX`, `639XXXXXXXXX`) and landlines are accepted; arbitrary digit strings are rejected. The order page mirrors this rule for both the contact and delivery-contact fields.
- **Actions and reasons:** Cancelled and Rejected status changes require a nonblank reason/note; admin action bodies are field-bounded so oversized or malformed text is rejected early.
- **Schemas:** typed, bounded validators were added for order submission, quoting, status changes, and admin actions. Trimming and length bounds are applied, while service rules remain authoritative for business decisions. Field caps were aligned with existing service limits (draft notes 1000, quotation instructions/notes 1000, payment-reference note 300) so valid text is not rejected before the service runs.
- **Uploads:** inline JSON proof/draft POSTs are rejected by `Content-Length` before parsing when they exceed the derived inline limit. Chunked uploads keep their existing streaming cap and signature checks.
- **Auth:** admin login throttling is database-backed in production; local/test retains the in-process limiter so the full suite does not throttle itself.
- **Payment metadata:** additive `max_proof_bytes` and `allowed_proof_types` let the client display the server’s real requirements rather than a guessed limit. Existing API response shapes were otherwise preserved.

## 6. Files modified

- **Plan/report:** `docs/ui-ux-improvement-plan.md` (new), `docs/implementation-report.md` (this section).
- **Backend modified:** `backend/app/main.py`, `backend/app/routes/auth.py`, `backend/app/routes/operations.py`, `backend/app/schemas/order.py`, `backend/app/schemas/user.py`, `backend/app/services/order_services.py`, `backend/app/services/shop_settings_services.py`.
- **Backend new:** `backend/app/data/philippine_locations.json`, `backend/app/services/location_services.py`, `backend/tests/test_ui_ux_validation.py`.
- **Customer frontend:** `frontend/customer/order.html`, `frontend/customer/customer-portal.html`, `frontend/customer/sign-in.html`, `frontend/customer/create-account.html`, `frontend/customer/forgot-password.html`, `frontend/assets/order.js`, `frontend/assets/portal.js`, `frontend/assets/style.css`.
- **Admin frontend:** `frontend/assets/admin/dashboard.js`, `frontend/assets/admin/catalog-admin.js`, `frontend/assets/admin/operations-admin.js`, `frontend/assets/admin/chatbot-admin.js`, `frontend/assets/admin/admin-core.js`, `frontend/assets/admin/theme-admin.css`.

## 7. Tests/results

- **Baseline before changes:** `python -m unittest discover -s backend/tests` → **292 tests, OK**.
- **New validation tests:** `python -m unittest backend.tests.test_ui_ux_validation -v` → **8 tests, OK** (locations endpoint and combo validation, address validation, phone validation, payment metadata, required cancel/reject reason, schema/service limit alignment, inline body cap).
- **Full suite after all changes:** `python -m unittest discover -s backend/tests` → **300 tests in 113.650s, OK**.
- **Final diff review:** a read-only review found six actionable issues (admin cancellation reason, async delivery-area selection, schema/service limit mismatches, client phone validation, pending-payment tile filter, zero-cost add-on pricing). All six were fixed and every check in this section was re-run.
- **JavaScript syntax:** `node --check` passed for `order.js`, `portal.js`, `dashboard.js`, `catalog-admin.js`, `operations-admin.js`, `chatbot-admin.js`, and `admin-core.js`.
- **Page/static smoke:** with `TestClient(app)` lifespan initialized, 8 customer pages, 12 admin pages, and 9 changed assets were requested — **29/29 returned HTTP 200**.
- **Reference scan:** 292 local `src`/`href`, CSS `url()`, and JS asset-string references were resolved. No missing reference was found in any changed file. Six misses were reported only in the unmodified `frontend/customer/index.html` `onerror` fallbacks; these are pre-existing and were not introduced by this pass.

## 8. Remaining issues

No actionable findings remain from the final read-only diff review. The following are known limitations rather than unresolved review items:

- Manual browser and responsive checks were **not** re-run for this pass; no browser automation was available in this session. The previous audit’s 390/820/1440 px results still stand for the pre-restructure layout, so the new dense layouts should be visually confirmed at those widths.
- Event dates are still only format-checked; past dates are not blocked by the server.
- Inline payment proof remains a JSON data URL. The new cap scales with `max_upload_mb`, but large proofs can still approach the Vercel 4.5 MB request cap; the chunked upload endpoint remains the safer path for large files.
- Typed schemas intentionally remain permissive in places because service rules are authoritative; the new bounds are defense in depth, not a replacement for workflow checks.
- No database migration was added by design. The PSGC dataset is a static read-only snapshot, so future PSGC changes require refreshing that JSON file.
- Real payment-account values, delivery fees, and pickup addresses remain shop-configured; no values were invented.

---

# Fourth pass — OCR-assisted payment receipt verification (2026-11-23)

This pass followed `docs/ocr-payment-verification-plan.md`. It is additive: the SQLite/Turso database, the nested `payments` and `payment_submissions` records, the existing `status` workflow authority, authentication, audit trail, notifications, one-pending-submission rule, and the 50% down payment/balance calculations are unchanged. OCR reads text only; it never proves that money moved.

## 1. Configuration and graceful fallback

- `PRINTSHOP_OCR_ENGINE`, `PRINTSHOP_OCR_LANGUAGE`, `PRINTSHOP_OCR_MAX_MB`, and `PRINTSHOP_OCR_TIMEOUT_SECONDS` were added to `backend/app/core/config.py` and `.env.example`. Optional `Pillow`/`pytesseract` and `pypdf` dependencies were added to both requirements files; the Dockerfile installs the Tesseract system binary.
- The Tesseract provider is loaded lazily on the first scan. A missing package/binary, an engine error, an oversized or unsupported file, an unreadable image, or a PDF without a text layer returns a structured empty/advisory result instead of raising. In those cases the receipt goes to `admin_review` with an explanatory flag; the existing manual verification path continues to work.
- PDFs are read from their embedded text layer only when `pypdf` is available; scanned PDFs need a clear image upload or an administrator. Extracted account/wallet numbers are masked to at most the first and last four digits before anything is stored or returned.

## 2. Customer payment and upload experience

- Public `/api/payment-methods` still drives the portal: provider name/logo treatment, active state, instructions, account details, QR URL, proof types, and maximum proof size all come from the server. Admin Settings can enable/disable methods, edit account details/QR/instructions, choose `manual` or `api` verification, and configure an optional gateway. Gateway keys remain server-side; public and admin views expose only `gateway.configured`, provider, endpoint, and timeout status. Remote gateway URLs must use HTTPS so the key is not sent in cleartext; loopback HTTP is allowed for local testing.
- The portal shows the server-calculated amount due, the 50% down-payment or remaining-balance stage, the payment purpose, and the selected method. Receipt upload accepts JPG/JPEG/PNG/PDF with preview, replace, and remove; existing signature, MIME, and size checks are unchanged.
- `POST /api/orders/{reference}/payments/scan` gives an advisory pre-submission result (extracted fields, confidence, flags, checks, and status). The authoritative scan runs again on the existing `POST /api/orders/{reference}/payments`, so a preview cannot be replayed as proof.
- Each stored submission now carries `ocr`, `receipt_hash`, `verification`, `verification_status`, `verification_method`, `admin_notes`, and `receipt_requests` additively. The receipt hash flags the same file reused with a new reference; duplicate references are still refused on submission. A requested clearer receipt frees the pending slot while preserving the full history: the same transaction reference may be reused with a new file, but the exact same file is still flagged as a duplicate. Customers see note text and time in their history, not the admin actor's account details.

## 3. Matching, classification, and the money rule

- `payment_verification_services.py` compares amount, recipient, method, reference, date, sender/order association, completeness, status, duplicate references, and duplicate files. Results are `verified`, `pending`, `admin_review`, `rejected`, or `possible_duplicate`, with a per-field match/missing/inconsistent/pending view for the administrator.
- A convincing screenshot alone stays `pending` or goes to `admin_review`; an explicit failed status is `rejected`; duplicates become `possible_duplicate`. Only an administrator using the existing verify action, or an independent provider confirmation (`auto_confirm`) with a matching reference and amount and no duplicates, may record money. Provider confirmation is opt-in per method, generic, and fails closed when the gateway is offline or returns a mismatch.
- New admin endpoints add `POST /api/admin/orders/{reference}/payment-submissions/{submission_id}/notes` and `POST /api/admin/orders/{reference}/payment-submissions/{submission_id}/request-receipt`. The dashboard has OCR columns, a side-by-side receipt/extracted-info review panel, validation highlighting, confidence badges, and approve/reject/request-receipt/note actions. New notification kinds and audit actions are additive. The admin payment report includes OCR reference, amount, confidence, status, required amount, submitted date, receipt URL, and verification method.

## 4. Verification

- **OCR payment suite:** `python -m unittest backend.tests.test_ocr_payment_verification -v` → **23 tests, OK** (GCash/Maya/bank receipts, down payment/balance/full-payment guards, amount/recipient/reference/file mismatches, unreadable and failed OCR, PDF text-only fallback, duplicate reference/file, clearer-receipt resubmission including the cross-order guard, unknown methods, provider unavailable/mismatch/positive confirmation, HTTPS gateway validation, and secret/customer-view scoping).
- **Full suite (historical OCR pass):** `python -m unittest discover -s backend/tests` → **323 tests, OK**. The current consolidated result is recorded in `docs/qa-remediation-report.md`.
- **JavaScript syntax:** `node --check` passed for `portal.js`, `portal-tracking.js`, `admin/dashboard.js`, and `admin/operations-admin.js`.
- **Page/static smoke:** with the app lifespan initialized, 21 customer/admin pages, 13 changed assets, and `/api/health` returned **35/35 HTTP 200**.
- **Reference scan:** 20 local `src`/`href`, CSS `url()`, and JS path references across the 18 changed frontend files resolved; **0 missing**.

## 5. Known limitations

- No Tesseract binary is installed in this development environment, so local scans intentionally use the no-engine fallback. Install Tesseract plus the optional Python packages to exercise real image OCR; the tests stub the provider and are deterministic.
- OCR is a regex/heuristic reader, not a payment API. Unusual layouts, glare, or low-resolution screenshots can lower confidence or be read incorrectly; the admin review path is the safety net.
- Provider confirmation uses a generic authenticated JSON POST contract. It does not add native GCash/Maya/J&T APIs; the shop must configure a provider that can return a matching reference and amount.
- Browser and responsive checks were not re-run for this pass; the prior audit's 390/820/1440 px results still stand for the pre-OCR layout. The new review modal and OCR chips should be visually confirmed at those widths.
- Inline receipts remain JSON data URLs and inherit the existing body cap; the chunked upload endpoint remains the safer path for very large files.
