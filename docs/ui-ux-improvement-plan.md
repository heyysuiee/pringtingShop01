# ImPRESSion PrintShop — UI/UX & Validation Improvement Plan

Date: 2026-11-22 · Baseline: `python -m unittest discover -s backend/tests` → **292 tests, OK**. No production data changed; no unrelated feature will be rebuilt.

## 1. Audit method

- Read-only exploration of all 8 customer pages, 14 admin pages, the shared CSS/JS assets, and the FastAPI routes/schemas/services.
- Baseline full backend test suite (292 passing).
- Prioritization rule: fix hierarchy, pending-action visibility, validation correctness, and scrolling first; keep working business rules and APIs intact.

## 2. Problems identified (highest impact first)

### Customer
1. Delivery address uses three free-text inputs (`order.html:381-394`). No province → municipality dependency, no derived region, no combination validation; the server silently assumes an unknown province is Luzon (`shop_settings_services.py:394-400`) and can reject the later area check (`425-426`).
2. Order page hardcodes GCash text (`order.html:425-462`); dynamic active payment methods exist only in the portal (`portal.js:389-521`) and include unconfigured providers (`448`). Proof-size label can drift from the server setting.
3. Portal order detail puts "Your Draft" and other customer actions far below status, timeline, quotation, payment, and request panels (`customer-portal.html:122-139`; `portal.js:523-673`), and re-render scrolls the user back to the top (`672`).
4. Design gallery cards are tall, "Use my own design" is last, and personalization stays below all cards (`order.js:970-1005`; `style.css:5864-5904`).
5. Duplicate order sections (quantity, estimate, notes, package/design summaries) and inconsistent step numbers (`order.html:158-309`).
6. Phone/address inputs have no frontend pattern/trim validation (`order.js:1734-1739`); proof upload checks extension/size but not MIME (`portal.js:500-504`); double-clicks can queue duplicate POSTs (`portal.js:55-84`).
7. Large vertical padding on order/portal pages (`style.css:1465-1506,1556-1562,3283-3309`); field errors have no distinct alert styling (`:1608-1615`).

### Admin
8. Dashboard shows 16 equal-weight cards before chart/recent orders; urgent actions are buried (`dashboard.js:612-700`).
9. Order table is click-only with 8+ columns; detail modal stacks all sections in one long dialog (`dashboard.js:713-823,827-1100`).
10. Payment view shows six summary cards and a wide table; verification is not the first-class queue (`dashboard.js:2040-2136`); payment entry form can create an over-balance state (`:1514-1579`).
11. Catalog dialogs leak XL modal sizing from order modal (`catalog-admin.js:483-494` vs `dashboard.js:829`); local copies of shared helpers are weaker (`catalog-admin.js:33-68`); numeric parsing can yield `NaN` (`:509-512`).
12. Customer table has 11 columns and click-only details (`dashboard.js:1661-1725`); reports put 12 stats, several tables and 3 charts on one tab (`:2162-2475`).
13. Cancellation/refund forms lack a client-side cap against `max_refundable` (`operations-admin.js:266-385`); some admin forms lack `reportValidity()` and busy guards (`chatbot-admin.js:449-466`).

### Validation (both sides)
14. Order/admin request schemas use `Any = None` and `extra="ignore"`, so Pydantic catches almost nothing (`schemas/order.py:8-141`); money-moving endpoints lack idempotency/atomic reference uniqueness (`order_services.py:1270-1287,1327-1377`).
15. Inline proof/draft endpoints lack the streaming body cap used by chunked uploads (`routes/order.py:31-39,269-276`; `routes/admin.py:99-109`); phone validation allows any 7+ digits (`schemas/user.py:47-51`); event dates have no past-date rule (`order_services.py:832-836`); rejection status needs no note (`2125`); unknown payment providers are accepted if active (`shop_settings_services.py:141-158`).

## 3. Implementation plan (surgical, in phases)

### Phase A — Customer delivery location (explicit requirement)
- Add a complete PSGC-based province/municipality dataset as a new static JSON file (no DB migration) plus a public read-only API `GET /api/locations` (provinces with municipalities and region).
- Replace free-text province/city with dependent selects + auto-derived region; reset municipality on province change; reject mismatched province/municipality client-side and server-side before area/fee calculation.
- Keep barangay as text (PSGC barangay-level data is not distributed in the same lightweight dataset); validate required + nonblank.

### Phase B — Customer ordering, payment, and tracking
- Compact order/design/portal spacing with scoped overrides; put "Use my own design" first and collapse gallery after selection.
- Replace static GCash copy with dynamic method names from `/api/payment-methods`; show method-specific instructions, account details, reference requirements, and proof requirements in the portal; expose `max_proof_bytes` and `configured` from the API.
- Add an "Needs your action" strip at the top of portal order details, move draft/approval/payment actions there, collapse secondary panels with existing `detail-toggle`, and preserve scroll position after actions.
- Add frontend phone pattern, trimmed required text, proof MIME check, and duplicate-submit guards; map backend field errors to inputs.

### Phase C — Admin modules
- Dashboard: 4 KPI cards + "Needs attention" action panel + revenue + recent orders; demote remaining metrics.
- Orders: scannable table with the requested columns, explicit Open button, one visible filter bar, section navigation in the detail modal, compact modal sections.
- Payments: pending-submission queue first, explicit Verify action, `amount <= balance` in the entry form, clear down/additional/full labels.
- Catalog: compact product table, shared helpers, finite-number validation, fix leaked XL modal class.
- Customers: trimmed table with explicit actions; Reports: executive summary + drill-down tabs.
- Operations: refund/cancellation amount caps; consistent form validation/busy states.

### Phase D — Backend validation (defense in depth)
- Typed schemas with field validators for order submission, quote, status changes, and admin action bodies; keep service checks.
- PH phone/email consistency; address province/municipality/region validation; business-date validation; required note for rejected/cancelled; unconfigured payment provider rejection.
- Streaming body caps for inline proof/draft endpoints; DB-backed throttling for admin auth; atomic payment reference uniqueness where safe without a migration (pre-check + version conflict already exists; add normalized check).

## 4. Preservation rules

- No business-rule, pricing, status-transition, authentication, or database-relationship changes.
- No migrations; new read-only dataset/API only. Existing APIs keep their shapes; new fields are additive.
- Reuse existing components (`AdminUI.DataTable`, `detail-toggle`, `wallet-card`, `field-error`, `PrintConfirm`, `withBusy`).
- No dummy/fake records; settings remain operator-configured.

## 5. Verification plan

- `python -m unittest discover -s backend/tests` (expect 292 + new tests).
- New tests for `/api/locations`, address validation, phone/date/reference/provider checks, and inline body caps.
- Static checks for JS syntax (`node --check`) and HTML references.
- Manual/browser checks (where a browser is available): customer address selection, quote/payment/draft flows, admin dashboard/order/payment/catalog/reports, and 390/820/1440 px layouts.

## 6. Deliverable

A final implementation report appended to `docs/implementation-report.md` covering problems, changes by side, validation changes, modified files, tests, and remaining issues.
