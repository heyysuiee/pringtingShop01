"use strict";

(() => {
    // Signed-in customers can open one of their own orders by its reference; there is no guest lookup.
    const form = document.querySelector("#orders-lookup-form");
    const referenceInput = document.querySelector("#lookup-ref");
    const error = document.querySelector("#lookup-error");
    // Errors while an order is open go here: the lookup form (and its error line) is hidden for account orders.
    const detailError = document.querySelector("#detail-error");
    const list = document.querySelector("#orders-list");
    const detail = document.querySelector("#order-detail");
    const accountSection = document.querySelector("#customer-account");
    const accountPanel = document.querySelector("#account-panel");
    const lookupPanel = document.querySelector("#order-lookup");
    const quotationPanel = document.querySelector("#customer-quotation");
    const paymentPanel = document.querySelector("#customer-payment");
    const workReviewPanel = document.querySelector("#customer-work-review");
    const actionStrip = document.querySelector("#customer-actions");
    const MAX_PROOF_BYTES = 5 * 1024 * 1024;
    // Replaced by the server value when /api/payment-methods is loaded; never invented in the browser.
    let proofLimitBytes = MAX_PROOF_BYTES;
    let allowedProofTypes = ["image/jpeg", "image/png", "application/pdf"];
    const proofLimitLabel = () => `${Math.max(1, Math.round(proofLimitBytes / 1024 / 1024))} MB`;
    let activeEmail = "";
    let currentOrder = null;
    // Every order is opened through the signed-in account (the server checks that the account owns it).
    let detailSource = "account";
    // Changing an order (accepting a quotation, paying, approving a draft) needs the signed-in account.
    let signedIn = false;
    // account.js restores the session on every page load; wait for it before opening an order from the URL.
    const sessionReady = window.PrintShopAccount?.ready || Promise.resolve(null);
    sessionReady.then((customer) => { signedIn = Boolean(customer); }).catch(() => { signedIn = false; });

    // Stable order routes: /customer/orders/<ref>[/payment|/approval]. The reference always comes from the
    // address bar on a refresh, never from a temporary navigation state.
    const ORDER_ROUTE = /^\/customer\/orders\/([^/]+)(?:\/(payment|approval))?\/?$/;
    const STEP_TARGETS = { payment: () => paymentPanel, approval: () => workReviewPanel };
    let currentStep = "";
    let unwatch = null;

    function parseOrderRoute() {
        const match = ORDER_ROUTE.exec(window.location.pathname);
        if (!match) return null;
        try { return { reference: decodeURIComponent(match[1]), step: match[2] || "" }; }
        catch { return null; }
    }

    function orderUrl(reference, step = "") {
        return `/customer/orders/${encodeURIComponent(reference)}${step ? `/${step}` : ""}`;
    }

    function syncOrderUrl(reference, step = currentStep, replace = false) {
        const url = orderUrl(reference, step);
        if (window.location.pathname === url) return;
        window.history[replace ? "replaceState" : "pushState"]({ reference, step }, "", url);
    }

    function focusStep(step) {
        const target = STEP_TARGETS[step]?.();
        if (target && !target.hidden) target.scrollIntoView({ block: "start", behavior: "instant" });
    }

    const PENDING_STATUSES = ["Pending Confirmation", "Under Review", "Awaiting Quotation", "Quotation Sent", "Quotation Declined", "Awaiting COD Confirmation", "Awaiting Down Payment"];
    function canCancelOrder(order) {
        return PENDING_STATUSES.includes(order.status) && order.payments.length === 0
            && !(order.payment_submissions || []).some((item) => item.status === "pending");
    }

    const make = (tag, className, value) => {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (value !== undefined) element.textContent = value;
        return element;
    };

    // "Pending Confirmation" → "pending-confirmation": the class that colours the status badge.
    const statusSlug = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

    function money(value) {
        return new Intl.NumberFormat("en-PH", { style: "currency", currency: "PHP" }).format(value);
    }

    // The shop may set a custom down payment. Keep the familiar 50% wording only when it still applies.
    function downPaymentLabel(payment) {
        return payment?.admin_set_down_payment ? "Required down payment" : "50% down payment";
    }

    const fieldsAmount = (value) => {
        const cleaned = String(value || "").replace(/[^\d.]/g, "");
        if (!cleaned) return null;
        const parsed = Number(cleaned);
        return Number.isFinite(parsed) && parsed > 0 ? Math.round((parsed + Number.EPSILON) * 100) / 100 : null;
    };

    /**
     * One source of money truth: the server's canonical calculation. Older cached responses may only have
     * the legacy amount_paid/balance fields, so those remain fallbacks; no amount is ever added up here.
     */
    function paymentMoney(order) {
        const payment = order.payment || {};
        const calc = payment.payment_calculation || {};
        const number = (value, fallback = null) => typeof value === "number" && Number.isFinite(value) ? value : fallback;
        const total = number(calc.total_order_amount, number(payment.total_amount, number(order.total_amount)));
        const paid = number(calc.confirmed_payments, number(payment.amount_paid, 0));
        const balanceDue = number(calc.balance_due, number(payment.balance, total === null ? null : Math.max(0, total - paid)));
        const remainingExact = number(calc.remaining_balance_exact, number(calc.remaining_balance, total === null ? null : total - paid));
        return {
            total,
            paid,
            balanceDue,
            remainingExact,
            requiredDown: number(calc.required_down_payment, number(payment.down_payment_required)),
            shortfall: number(calc.additional_needed_for_down_payment, number(payment.down_payment_remaining, 0)),
            excess: number(calc.excess_amount, number(payment.excess_amount, 0)),
            status: calc.payment_status || order.payment_status || payment.payment_status || "",
            percentage: number(calc.payment_percentage, null),
        };
    }

    async function request(url, options = {}) {
        const response = await fetch(url, options);
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
            const detail = Array.isArray(result.detail) ? result.detail.map((item) => item.msg).join(" ") : result.detail;
            const failure = new Error(detail || "The order information could not be loaded.");
            failure.status = response.status;
            throw failure;
        }
        return result;
    }

    // One confirmation at a time: a double-click cannot open two dialogs or queue two mutations.
    let confirmingAction = false;
    async function confirmAction(message, options) {
        if (confirmingAction) return false;
        confirmingAction = true;
        try {
            return await window.PrintConfirm(message, options);
        } finally {
            confirmingAction = false;
        }
    }

    /** POST a customer action (approve / request-changes / cancel) for the open order and show the result. */
    async function orderAction(action, body, failure, button) {
        // A second click while the first request is in flight must never queue a duplicate POST.
        if (!button || button.disabled) return;
        detailError.textContent = "";
        button.disabled = true;
        try {
            const order = await request(`/api/orders/${encodeURIComponent(currentOrder.reference)}/${action}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ email: activeEmail, ...body }),
            });
            renderDetail(order);
        } catch (issue) {
            const message = issue instanceof Error ? issue.message : failure;
            if (issue.status === 401) {
                // The session ended while the order was open: changing an order needs the owner's signed-in account.
                detailError.textContent = "Sign in to your customer account to respond to this order.";
                button.disabled = false;
                detailError.scrollIntoView({ block: "center", behavior: "instant" });
                return;
            }
            if (issue.status === 409) {
                // The order changed meanwhile (for example in another tab): show its current state, then the reason.
                // renderDetail sets which actions are still available.
                await loadOrder(currentOrder.reference, detailSource);
            } else {
                button.disabled = false;  // nothing changed on the server: let the customer try again
            }
            detailError.textContent = message;
            detailError.scrollIntoView({ block: "center", behavior: "instant" });
        }
    }

    function appendSummaryRow(target, label, value) {
        const row = make("div", "summary-row");
        row.append(make("dt", "", label), make("dd", "", value || "—"));
        target.append(row);
    }

    function formatDate(value) {
        if (!value) return "—";
        const parsed = new Date(value);
        return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString("en-PH", { year: "numeric", month: "short", day: "numeric" });
    }

    function estimateOf(order) {
        const estimate = order.pricing?.estimated_total;
        return typeof estimate === "number" ? estimate : null;
    }

    /** Product options saved with the order. Older orders used fixed size/material fields. */
    function specificationRows(order) {
        const customization = order.customization || {};
        if (Array.isArray(customization.specifications)) {
            return customization.specifications.map((spec) => [spec.label, spec.value]);
        }
        return [
            ["Size / dimensions", customization.size],
            ["Material / variant", customization.material],
            ["Customization", customization.details],
        ];
    }

    /** Event rows from the invitation order form, with a fallback for older fixed event details. */
    function eventRows(order) {
        if (Array.isArray(order.event_fields) && order.event_fields.length) {
            return order.event_fields.map((row) => [row.label, row.value]);
        }
        const legacy = order.event_details && !Array.isArray(order.event_details) ? order.event_details : {};
        return [
            ["Celebrant", legacy.celebrant],
            ["Event type", legacy.event_type],
            ["Event date", legacy.event_date],
            ["Event time", legacy.event_time],
            ["Event location", legacy.event_location],
        ].filter(([, value]) => value);
    }

    /** The steps this order goes through: its pricing (package or quotation), the down payment, production, and fulfillment. */
    function orderPath(order) {
        const cod = order.payment?.payment_option === "cod";  // only orders placed while COD was offered
        const payment = cod ? (order.fixed_price ? [] : ["Awaiting COD Confirmation"]) : ["Awaiting Down Payment", "Confirmed"];
        const delivery = order.fulfillment?.method === "delivery";
        const reached = new Set((order.status_timeline || []).map((item) => item.status));
        // Packed orders that were already fully paid skip the "Awaiting Full Payment" step.
        const packed = ["Ready for Pickup", "Ready for Dispatch", "Out for Delivery", "Delivery Failed", "Delivered", "Order Claimed", "Completed"].includes(order.status);
        const waitForBalance = order.status === "Awaiting Full Payment" || reached.has("Awaiting Full Payment") || (!packed && (paymentMoney(order).balanceDue || 0) > 0.005);
        // Orders released before Quality Checking and Packaging existed went straight from printing to release.
        const legacy = packed && !order.production?.packaging?.completed_at && !reached.has("Packaging");
        return [
            "Pending Confirmation",
            "Under Review",
            ...(order.fixed_price ? [] : ["Quotation Sent"]),
            ...payment,
            "Draft Preparation",
            "Awaiting Customer Approval",
            "Approved for Printing",
            "In Production",
            ...(legacy ? [] : ["Quality Checking", "Packaging"]),
            ...(waitForBalance && !legacy ? ["Awaiting Full Payment"] : []),
            ...(delivery ? [...(legacy ? [] : ["Ready for Dispatch"]), "Out for Delivery", ...(legacy ? [] : ["Delivered"])] : ["Ready for Pickup", ...(legacy ? [] : ["Order Claimed"])]),
            "Completed",
        ];
    }

    const STEP_LABELS = {
        "Pending Confirmation": "Order Submitted",
        "Confirmed": "Down Payment Verified · Order Confirmed",
        "Ready for Dispatch": "Ready for Dispatch",
    };

    function renderTimeline(order) {
        const timeline = document.querySelector("#detail-timeline");
        timeline.replaceChildren();
        const terminal = ["Rejected", "Cancelled"].includes(order.status);
        const ordered = terminal ? ["Pending Confirmation", order.status] : orderPath(order);
        // Side steps are shown in place of the step they belong to.
        const shownAs = { "Awaiting Quotation": "Under Review", "Quotation Declined": "Quotation Sent", "Delivery Failed": "Out for Delivery" }[order.status];
        if (shownAs && ordered.includes(shownAs)) ordered[ordered.indexOf(shownAs)] = order.status;
        const currentIndex = ordered.indexOf(order.status);
        // When each step was reached (latest time), from the server's status history.
        const times = {};
        (order.status_timeline || []).forEach((item) => { times[item.status] = item.at; });
        ordered.forEach((status, index) => {
            const item = make("li", "timeline-step");
            const failed = ["Rejected", "Cancelled", "Delivery Failed", "Quotation Declined"].includes(status) && index === currentIndex;
            if (index < currentIndex) item.classList.add("is-done");
            if (index === currentIndex) {
                item.classList.add(failed ? "is-failed" : "is-current");
                item.setAttribute("aria-current", "step");
            }
            if (index > currentIndex) item.classList.add("is-pending");
            const dot = make("span", "timeline-dot", failed ? "!" : index < currentIndex ? "✓" : String(index + 1));
            const label = make("span", "timeline-label", STEP_LABELS[status] || status);
            item.append(dot, label);
            if (index <= currentIndex && times[status]) item.append(make("time", "timeline-time", formatDateTime(times[status])));
            timeline.append(item);
        });
    }

    function formatDateTime(value) {
        const parsed = new Date(value);
        return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleString("en-PH", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    }

    function renderAttachments(target, attachments, label) {
        if (!attachments?.length) return;
        const container = make("div", "order-attachments");
        container.append(make("strong", "", label));
        attachments.forEach((file) => {
            const prefix = file.field_label && file.field_label !== "Reference file" ? `${file.field_label}: ` : "";
            const link = make("a", "", `${prefix}${file.name} · ${(file.size / 1024).toFixed(0)} KB`);
            link.href = file.url;
            link.target = "_blank";
            link.rel = "noopener";
            container.append(link);
        });
        target.append(container);
    }

    function definitionList(rows) {
        const list = make("dl", "summary");
        rows.filter(([, value]) => value !== null && value !== undefined && value !== "").forEach(([label, value]) => appendSummaryRow(list, label, value));
        return list;
    }

    function fileAsDataUrl(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.addEventListener("load", () => resolve(reader.result));
            reader.addEventListener("error", () => reject(new Error(`Could not read ${file.name}.`)));
            reader.readAsDataURL(file);
        });
    }

    const QUOTE_STATES = { sent: "Waiting for your response", accepted: "Accepted", declined: "Declined", superseded: "Replaced by a newer quotation" };

    /** Every quotation the shop sent, newest first: price, previous price, message, and your response. */
    function quotationHistory(order) {
        const items = order.quotation_history || [];
        if (items.length < 2) return null;
        const wrapper = make("details", "quotation-history");
        wrapper.append(make("summary", "", `Quotation history (${items.length})`));
        const listElement = make("ol", "payment-submissions");
        items.slice().reverse().forEach((item) => {
            const entry = make("li", `quote-${item.status}`);
            const change = typeof item.previous_amount === "number" ? ` (was ${money(item.previous_amount)})` : "";
            entry.append(make("strong", "", `Revision ${item.revision}: ${money(item.amount)}${change}`));
            entry.append(make("span", "field-hint", `${QUOTE_STATES[item.status] || item.status} · sent ${formatDate(item.sent_at)}${item.responded_at ? ` · answered ${formatDate(item.responded_at)}` : ""}`));
            if (item.note) entry.append(make("span", "field-hint", `Shop note: ${item.note}`));
            if (item.decline_reason) entry.append(make("span", "field-hint", `Your reason: ${item.decline_reason}`));
            listElement.append(entry);
        });
        wrapper.append(listElement);
        return wrapper;
    }

    const quotationIsOpen = (order) => order.status === "Quotation Sent" && order.quotation?.status === "sent";

    // Shared by the full panel and the action strip at the top: the same guards and the same server call.
    async function acceptQuotation(order, button) {
        if (!quotationIsOpen(order)) return;
        const quote = order.quotation;
        const due = order.payment?.down_payment_required;
        const label = downPaymentLabel(order.payment);
        if (!(await confirmAction(`Accept the quotation of ${money(order.total_amount)}? You will then pay the ${label.toLowerCase()} of ${money(due)} through the shop's e-wallet.`, { title: "Accept quotation?", confirmLabel: "Accept quotation" }))) return;
        // The revision you reviewed is sent along: a revised quotation elsewhere is refused with 409.
        await orderAction("quotation/accept", { quotation_revision: quote.revision }, "The quotation could not be accepted.", button);
    }

    async function declineQuotation(order, button, reasonText = "") {
        if (!quotationIsOpen(order)) return;
        if (!(await confirmAction("Decline this quotation? Nothing will be charged; the shop may send a revised one.", { title: "Decline quotation?", confirmLabel: "Decline quotation", tone: "danger" }))) return;
        await orderAction("quotation/decline", { reason: reasonText.trim(), quotation_revision: order.quotation.revision }, "The quotation could not be declined.", button);
    }

    async function approveDraft(button) {
        if (!currentOrder) return;
        if (!(await confirmAction("Approve this draft? The administrator may then move the order to printing.", { title: "Approve draft?", confirmLabel: "Approve draft" }))) return;
        await orderAction("approve", {}, "The draft approval could not be recorded.", button);
    }

    async function requestDraftChanges(button) {
        if (!currentOrder) return;
        const noteInput = document.querySelector("#draft-revision-note");
        const note = noteInput.value.trim();
        if (!note) {
            detailError.textContent = "Describe the changes you want in the revision instructions.";
            noteInput.focus();
            return;
        }
        if (!(await confirmAction("Ask the shop to revise this draft with your instructions?", { title: "Request revision?", confirmLabel: "Request revision" }))) return;
        await orderAction("request-changes", { note }, "The revision request could not be recorded.", button);
    }

    async function approveFinishedWork(button) {
        if (!currentOrder) return;
        const note = document.querySelector("#work-review-note").value.trim();
        if (!(await confirmAction("Approve the finished work? The remaining balance will unlock after this.", { title: "Approve finished work?", confirmLabel: "Approve work" }))) return;
        await orderAction("work/approve", { note }, "The work approval could not be recorded.", button);
    }

    async function requestFinishedWorkRevision(button) {
        if (!currentOrder) return;
        const noteInput = document.querySelector("#work-review-note");
        const note = noteInput.value.trim();
        if (!note) {
            document.querySelector("#work-review-error").textContent = "Describe the changes you want before requesting a revision.";
            noteInput.focus();
            return;
        }
        if (!(await confirmAction("Ask the shop to revise the finished work? The remaining balance stays locked until you approve the updated work.", { title: "Request revision?", confirmLabel: "Request revision" }))) return;
        await orderAction("work/revision", { note }, "The revision request could not be recorded.", button);
    }

    /** The latest quotation: review it, then accept (proceed to the 50% down payment) or decline it. */
    function renderQuotationPanel(order) {
        quotationPanel.replaceChildren();
        const quote = order.quotation;
        const open = order.status === "Quotation Sent" && quote?.status === "sent";
        const declined = order.status === "Quotation Declined";
        const history = quotationHistory(order);
        quotationPanel.hidden = !open && !declined && !history;
        if (declined) {
            quotationPanel.append(
                make("h3", "", "You declined the quotation"),
                make("p", "field-hint", `${quote?.decline_reason ? `Your reason: “${quote.decline_reason}”. ` : ""}No payment is due. The shop may send a revised quotation; you will be notified and it will appear here.`),
            );
            if (history) quotationPanel.append(history);
            return;
        }
        if (!open) {
            if (history) quotationPanel.append(make("h3", "", "Quotation"), history);
            return;
        }
        const total = order.total_amount;
        const payment = order.payment || {};
        const revised = quote.revision > 1;
        quotationPanel.append(
            make("h3", "", revised ? `Revised quotation (revision ${quote.revision})` : "Your quotation is ready"),
            make("p", "field-hint", `Review the latest quotation from the shop. If you accept it, the next step is the ${downPaymentLabel(order.payment).toLowerCase()} through one of the shop's e-wallets. If you decline it, nothing is paid and the shop may send a revised quotation.`),
            definitionList([
                quote.delivery_province ? ["Delivery province", quote.delivery_province] : ["", ""],
                quote.destination_region ? ["Destination region", quote.destination_region] : ["", ""],
                quote.delivery_fee ? ["Order subtotal", money(quote.amount)] : ["", ""],
                quote.delivery_fee ? ["Shipping fee", money(quote.shipping_fee ?? quote.delivery_fee)] : ["", ""],
                ["Quoted total", money(total)],
                quote.estimated_delivery ? ["Estimated delivery", quote.estimated_delivery] : ["", ""],
                revised && typeof quote.previous_amount === "number" ? ["Previous quotation", money(quote.previous_amount)] : ["", ""],
                [downPaymentLabel(payment), money(payment.down_payment_required)],
                ["Remaining balance", `${money(payment.remaining_after_down_payment)} (paid before release)`],
                ["Sent", formatDate(quote.sent_at)],
                ["Message from the shop", quote.note],
                ["Payment instructions", order.payment_instructions],
            ]),
        );
        if (history) quotationPanel.append(history);
        if (detailSource !== "account" && !signedIn) {
            quotationPanel.append(make("p", "notice", "Sign in to your customer account to accept or decline this quotation."));
            return;
        }
        const reason = make("textarea", "input");
        reason.rows = 2;
        reason.maxLength = 500;
        reason.placeholder = "Optional: tell the shop why (for example, over budget)";
        reason.setAttribute("aria-label", "Reason for declining (optional)");
        const accept = make("button", "btn btn-dark", "Accept Quotation");
        accept.type = "button";
        const decline = make("button", "btn btn-ghost-dark", "Decline Quotation");
        decline.type = "button";
        accept.addEventListener("click", () => { void acceptQuotation(order, accept); });
        decline.addEventListener("click", () => { void declineQuotation(order, decline, reason.value); });
        const actions = make("div", "draft-actions");
        actions.append(accept, decline);
        quotationPanel.append(reason, actions);
    }

    /** Finished-work review: the customer sees the uploaded preview and approves it or asks for changes. */
    function renderWorkReviewPanel(order) {
        const review = order.work_approval || {};
        const actions = order.customer_actions || {};
        const pending = review.status === "pending";
        const canAct = detailSource === "account" || signedIn;
        workReviewPanel.hidden = !pending;
        if (!pending) return;
        const remarks = document.querySelector("#work-review-remarks");
        const noteParts = [];
        if (review.remarks) noteParts.push(`Shop notes: ${review.remarks}`);
        if (review.revision_count) noteParts.push(`Revision request ${review.revision_count}`);
        remarks.hidden = !noteParts.length;
        remarks.textContent = noteParts.join(" · ");
        const files = document.querySelector("#work-preview-files");
        files.replaceChildren(...(review.preview_files || []).map((file) => {
            const row = make("div", "work-preview-file");
            const link = make("a", "btn btn-ghost-dark btn-compact", `Open finished work: ${file.name}`);
            link.href = file.url;
            link.target = "_blank";
            link.rel = "noopener";
            row.append(link);
            if (file.size) row.append(make("span", "field-hint", `${Math.max(1, Math.round(file.size / 1024))} KB`));
            return row;
        }));
        document.querySelector("#work-review-note").value = "";
        document.querySelector("#work-review-error").textContent = "";
        document.querySelector("#approve-work").disabled = !canAct || actions.approve_work === false;
        document.querySelector("#request-work-revision").disabled = !canAct || actions.request_work_revision === false;
    }

    const SUBMISSION_LABELS = { pending: "Pending Verification", verified: "Verified", rejected: "Not verified" };

    /** Payment status, instructions, and the GCash payment form (amount set by the shop). */
    function renderPaymentPanel(order) {
        paymentPanel.replaceChildren();
        const payment = order.payment || {};
        const closed = ["Rejected", "Cancelled", "Quotation Declined"].includes(order.status);
        const submissionsList = order.payment_submissions || [];
        const agreed = order.total_amount !== null && !["Pending Confirmation", "Under Review", "Awaiting Quotation", "Quotation Sent", "Quotation Declined"].includes(order.status);
        const show = !closed && order.total_amount !== null && (agreed || order.fixed_price);
        paymentPanel.hidden = !show && !submissionsList.length;
        if (paymentPanel.hidden) return;
        const cod = payment.payment_option === "cod";
        const moneyNow = paymentMoney(order);
        const shipping = order.shipping || {};
        paymentPanel.append(make("h3", "", "Payment"));
        // The order overview already shows total, payment status, verified paid, remaining balance and work status.
        // Keep only payment details that are not repeated there.
        paymentPanel.append(definitionList([
            shipping.delivery_province ? ["Delivery province", shipping.delivery_province] : ["", ""],
            shipping.destination_region ? ["Destination region", shipping.destination_region] : ["", ""],
            typeof payment.order_subtotal === "number" ? ["Order subtotal", money(payment.order_subtotal)] : ["", ""],
            shipping.delivery_province ? ["Shipping fee", money(payment.shipping_fee ?? payment.delivery_fee ?? 0)] : ["", ""],
            shipping.estimated_delivery ? ["Estimated delivery", shipping.estimated_delivery] : ["", ""],
            cod ? ["Down payment", "Not required (Cash on Delivery, arranged before the down payment rule)"] : [downPaymentLabel(payment), `${money(moneyNow.requiredDown)} · ${moneyNow.status}`],
            cod ? ["", ""] : moneyNow.shortfall > 0.005 ? ["Still due on the down payment", money(moneyNow.shortfall)] : ["", ""],
            payment.pending_verification ? ["Pending verification", money(payment.pending_verification)] : ["", ""],
            moneyNow.excess > 0.005 ? ["Excess — admin review", money(moneyNow.excess)] : ["", ""],
            payment.refunded ? ["Refunded to you", money(payment.refunded)] : ["", ""],
        ]));
        if (order.status === "Confirmed") {
            paymentPanel.append(make("p", "notice notice-success", "Your down payment is verified and your order is confirmed. The shop is preparing your draft."));
        }
        if (order.payment_instructions) {
            const instructions = make("div", "payment-instructions");
            instructions.append(make("strong", "", "Payment instructions from the shop"), make("p", "", order.payment_instructions));
            paymentPanel.append(instructions);
        }
        const work = order.work_approval || {};
        if (!closed && !cod && work.status === "pending") {
            paymentPanel.append(make("p", "notice", "Your finished work is ready for review. Approve it in the work review section to unlock the remaining balance."));
        } else if (!closed && !cod && work.status === "revision_requested") {
            paymentPanel.append(make("p", "notice", `You requested a revision${work.revision_note ? `: “${work.revision_note}”` : ""}. The shop will send updated work; the remaining balance stays locked until you approve it.`));
        } else if (!closed && !cod && work.status === "approved") {
            paymentPanel.append(make("p", "notice notice-success", "You approved the finished work. The remaining balance is unlocked when it is due."));
        }
        const pending = submissionsList.find((item) => item.status === "pending");
        const due = payment.next_payment;
        if (closed) {
            // Only the submission history below.
        } else if (cod) {
            paymentPanel.append(make("p", "notice", `Cash on Delivery: pay ${money(moneyNow.balanceDue)} when your order is ${order.fulfillment?.method === "delivery" ? "delivered" : "picked up"}.`));
        } else if (!agreed) {
            paymentPanel.append(make("p", "notice", "Payment opens after the shop approves your order and shares the payment instructions."));
        } else if (pending) {
            const status = pending.verification_status_label || "Pending Verification";
            paymentPanel.append(make("p", "notice", `Your ${pending.method} payment of ${money(pending.amount)} (reference ${pending.reference}) is ${status}. ${order.status === "Awaiting Down Payment" ? "Your order is confirmed only after the shop verifies it." : "The shop will update your balance once it is verified."} A receipt scan is not proof that funds were received.`));
        } else if (due && due.locked) {
            paymentPanel.append(make("p", "notice", due.reason || payment.final_payment_lock || "The remaining balance is locked."));
        } else if (due && order.customer_actions && !order.customer_actions.pay) {
            paymentPanel.append(make("p", "notice", "Payment is on hold while your cancellation request is reviewed."));
        } else if (due) {
            if (detailSource !== "account" && !signedIn) {
                paymentPanel.append(make("p", "notice", "Sign in to your customer account to submit your payment."));
            } else {
                paymentPanel.append(paymentForm(order, due));
            }
        }
        if (submissionsList.length) {
            const listElement = make("ul", "payment-submissions");
            submissionsList.slice().reverse().forEach((item) => {
                const entry = make("li", `submission-${item.status}`);
                const label = item.status_label || SUBMISSION_LABELS[item.status] || item.status;
                const amount = typeof item.verified_amount === "number" && Math.abs(item.verified_amount - item.amount) > 0.005
                    ? `${money(item.amount)} (verified ${money(item.verified_amount)})` : money(item.amount);
                entry.append(make("strong", "", `${amount} · ${item.method} · ${item.reference}`), make("span", "field-hint", `${label} · submitted ${formatDate(item.submitted_at)}`));
                if (item.verification_status_label && item.verification_status_label !== label) {
                    entry.append(make("span", "status-badge", item.verification_status_label));
                }
                if (item.requires_admin_review || item.excess_amount > 0.005) {
                    entry.append(make("span", "status-badge", `Needs admin review · excess ${money(item.excess_amount || 0)}`));
                }
                if (item.ocr?.fields?.reference || item.ocr?.fields?.amount) {
                    const detected = [];
                    if (item.ocr.fields.reference) detected.push(`reference ${item.ocr.fields.reference}`);
                    if (item.ocr.fields.amount) detected.push(`amount ${money(Number(item.ocr.fields.amount))}`);
                    if (item.ocr.fields.date_time) detected.push(`date ${item.ocr.fields.date_time}`);
                    entry.append(make("span", "field-hint", `Receipt scan detected: ${detected.join(" · ")}. Scanning is not proof of payment.`));
                }
                if (item.review_note) entry.append(make("span", "field-hint", `Shop note: ${item.review_note}`));
                (item.receipt_requests || []).forEach((request) => entry.append(make("span", "field-hint", `Clearer receipt requested ${formatDate(request.at)}: ${request.note}`)));
                (item.admin_notes || []).forEach((note) => entry.append(make("span", "field-hint", `Verification note (${note.at ? formatDate(note.at) : "shop"}): ${note.note}`)));
                if (item.proof) {
                    const link = make("a", "", `Receipt: ${item.proof.name}`);
                    link.href = item.proof.url;
                    link.target = "_blank";
                    link.rel = "noopener";
                    entry.append(link);
                }
                listElement.append(entry);
            });
            paymentPanel.append(make("h4", "", "Payments you submitted"), listElement);
        }
    }

    let methodsRequest = null;
    /** Payment metadata plus its load error, so callers never confuse an outage with an empty shop setup. */
    function paymentMethodsStatus(options = {}) {
        const refresh = options === true || options.refresh === true;
        if (refresh || !methodsRequest) {
            methodsRequest = request("/api/payment-methods").then((result) => {
                if (Number.isFinite(result.max_proof_bytes) && result.max_proof_bytes > 0) proofLimitBytes = result.max_proof_bytes;
                if (Array.isArray(result.allowed_proof_types) && result.allowed_proof_types.length) allowedProofTypes = result.allowed_proof_types;
                return { methods: result.methods || [], error: "" };
            }).catch((error) => ({
                methods: [],
                error: error instanceof Error && error.message ? error.message : "the shop's payment list could not be reached",
            }));
        }
        return methodsRequest;
    }

    function paymentMethods(options = {}) {
        return paymentMethodsStatus(options).then((result) => result.methods);
    }

    function copyButton(text, label) {
        const button = make("button", "btn btn-ghost-dark btn-copy", "Copy");
        button.type = "button";
        button.setAttribute("aria-label", `Copy ${label}`);
        button.addEventListener("click", async () => {
            try {
                await navigator.clipboard.writeText(text);
                button.textContent = "Copied";
            } catch {
                button.textContent = "Copy failed";
            }
            setTimeout(() => { button.textContent = "Copy"; }, 1600);
        });
        return button;
    }

    /** E-wallet payment form: choose an active method, upload a receipt, see OCR feedback, then submit.
     *  The customer may correct the amount actually paid; it stays pending until an administrator confirms it. */
    function paymentForm(order, due) {
        const payment = order.payment || {};
        const moneyNow = paymentMoney(order);
        const formElement = make("form", "payment-submit-form");
        const cards = make("div", "wallet-cards");
        cards.setAttribute("role", "radiogroup");
        cards.setAttribute("aria-label", "Payment method");
        const details = make("div", "wallet-details");
        let chosen = null;
        let receiptDataUrl = "";
        const showMethod = (method) => {
            chosen = method;
            cards.querySelectorAll(".wallet-card").forEach((card) => {
                const on = card.dataset.method === method.provider;
                card.classList.toggle("is-selected", on);
                card.setAttribute("aria-checked", String(on));
            });
            details.replaceChildren();
            const rows = make("dl", "summary");
            const row = (label, value, copyValue) => {
                const wrap = make("div", "summary-row");
                const dd = make("dd", "", value || "—");
                if (copyValue) dd.append(" ", copyButton(copyValue, label));
                wrap.append(make("dt", "", label), dd);
                rows.append(wrap);
            };
            row("Pay to", method.account_name || "Ask the shop for the account name");
            row(`${method.provider} number`, method.account_number || "Not set yet — use the shop's payment instructions", method.account_number);
            row("Payment purpose", `${due.label} for order ${order.reference}`);
            row("Reference to enter", "The transaction / reference number shown on your receipt (at least 4 characters)");
            row(`Server-suggested ${due.label}`, money(due.amount), due.amount.toFixed(2));
            row("Confirmed paid so far", money(moneyNow.paid));
            row("Remaining balance", money(moneyNow.balanceDue));
            if (moneyNow.shortfall > 0.005) row(`Still due on ${downPaymentLabel(payment).toLowerCase()}`, money(moneyNow.shortfall));
            details.append(rows);
            if (method.qr_url) {
                const qr = make("img", "wallet-qr");
                qr.src = method.qr_url;
                qr.alt = `${method.provider} QR code of the shop`;
                qr.loading = "lazy";
                details.append(qr);
            }
            if (method.instructions) details.append(make("p", "field-hint", method.instructions));
            if (!method.configured) details.append(make("p", "notice", `The shop has not added its ${method.provider} account details yet. Follow the payment instructions from the shop or contact the shop before paying.`));
            reference.placeholder = `${method.provider} reference number`;
            referenceLabel.firstChild.textContent = `${method.provider} reference number`;
            proofLabel.firstChild.textContent = `${method.provider} screenshot or receipt (JPG, PNG, or PDF · max ${proofLimitLabel()})`;
            if (receiptDataUrl) scanReceipt();
        };
        paymentMethodsStatus({ refresh: true }).then(({ methods, error }) => {
            if (error) {
                cards.replaceChildren(make("p", "notice notice-error", `Payment methods could not be loaded (${error}). Refresh the page or contact the shop before paying.`));
                submit.disabled = true;
                return;
            }
            if (!methods.length) {
                cards.replaceChildren(make("p", "notice", "No payment method is available right now. Contact the shop."));
                submit.disabled = true;
                return;
            }
            methods.forEach((method) => {
                const card = make("button", "wallet-card");
                card.type = "button";
                card.dataset.method = method.provider;
                card.setAttribute("role", "radio");
                const logo = make("span", `method-logo method-logo-${statusSlug(method.provider)}`, (method.provider || "?").trim().charAt(0).toUpperCase());
                logo.setAttribute("aria-hidden", "true");
                card.append(logo, make("strong", "", method.provider), make("span", "field-hint", method.account_name || "E-wallet"));
                card.addEventListener("click", () => showMethod(method));
                cards.append(card);
            });
            showMethod(methods[0]);
        });
        const field = (labelText, control) => {
            const label = make("label", "", labelText);
            label.append(control);
            return label;
        };
        const amount = make("input", "input");
        Object.assign(amount, {
            type: "number", min: "0.01", step: "0.01", inputMode: "decimal",
            placeholder: `Server suggestion: ${due.amount.toFixed(2)}`,
        });
        const amountHint = make("p", "field-hint", `Enter the amount you actually paid. Leave blank to use the amount detected on the receipt; if OCR cannot read it, the server will use ${money(due.amount)}.`);
        const reference = make("input", "input");
        Object.assign(reference, { maxLength: 120, minLength: 4, required: true, placeholder: "Reference number", autocomplete: "off" });
        const proof = make("input", "input");
        Object.assign(proof, { type: "file", required: true, accept: ".jpg,.jpeg,.png,.pdf,image/jpeg,image/png,application/pdf" });
        const submit = make("button", "btn btn-dark", "Submit payment for verification");
        submit.type = "submit";
        const down = due.stage === "down_payment";
        const referenceStatusHint = make("p", "field-hint", "Enter the reference number from your receipt if OCR cannot detect it.");
        const referenceLabel = field("Reference Number", reference);
        referenceLabel.append(referenceStatusHint);
        const detectedAmountPaid = make("input", "input");
        Object.assign(detectedAmountPaid, { type: "text", placeholder: "Auto-filled if detected", autocomplete: "off" });
        const detectedAmountHint = make("p", "field-hint", "Detected from receipt (for review). Enter corrections in the amount field above.");
        const detectedAmountLabel = field("Amount Detected on Receipt", detectedAmountPaid);
        detectedAmountLabel.append(detectedAmountHint);
        const detectedDate = make("input", "input");
        Object.assign(detectedDate, { type: "text", placeholder: "Auto-filled if detected", autocomplete: "off" });
        const detectedDateHint = make("p", "field-hint", "Detected from receipt (for review).");
        const detectedDateLabel = field("Transaction Date", detectedDate);
        detectedDateLabel.append(detectedDateHint);
        const proofLabel = field(`Upload Payment Screenshot (JPG, PNG, or PDF · max ${proofLimitLabel()})`, proof);
        const ocrStatus = make("p", "notice", "OCR Status: Waiting for receipt upload.");
        const preview = make("div", "receipt-preview");
        preview.hidden = true;
        const previewActions = make("div", "receipt-preview-actions");
        const replace = make("button", "btn btn-ghost-dark btn-small", "Replace receipt");
        replace.type = "button";
        replace.addEventListener("click", () => proof.click());
        const remove = make("button", "btn btn-ghost-dark btn-small", "Remove receipt");
        remove.type = "button";
        let detectedReferenceValue = "";
        let duplicateDetected = false;
        let scanning = false;
        const syncReferenceHint = () => {
            const value = reference.value.trim();
            if (!value) {
                referenceStatusHint.textContent = "Enter the reference number from your receipt if OCR cannot detect it.";
                return;
            }
            if (detectedReferenceValue && value === detectedReferenceValue) {
                referenceStatusHint.textContent = "Auto-detected from receipt. You can correct it if needed.";
            } else {
                referenceStatusHint.textContent = "Manually entered/edited. Please make sure it matches the receipt.";
            }
        };
        reference.addEventListener("input", () => {
            if (duplicateDetected && reference.value.trim() !== detectedReferenceValue) {
                duplicateDetected = false;
                submit.disabled = false;
            }
            syncReferenceHint();
        });
        detectedAmountPaid.addEventListener("input", () => {
            detectedAmountPaid.dataset.manual = "true";
            detectedAmountHint.textContent = "Manually edited (for review). Enter corrections in the amount field above.";
        });
        detectedDate.addEventListener("input", () => {
            detectedDate.dataset.manual = "true";
            detectedDateHint.textContent = "Manually entered/edited (for review).";
        });
        const resetDetectedFields = () => {
            detectedReferenceValue = "";
            duplicateDetected = false;
            submit.disabled = false;
            detectedAmountPaid.value = "";
            detectedAmountPaid.dataset.manual = "";
            detectedAmountHint.textContent = "Detected from receipt (for review). Enter corrections in the amount field above.";
            detectedDate.value = "";
            detectedDate.dataset.manual = "";
            detectedDateHint.textContent = "Detected from receipt (for review).";
            referenceStatusHint.textContent = "Enter the reference number from your receipt if OCR cannot detect it.";
            ocrStatus.textContent = "OCR Status: Waiting for receipt upload.";
            ocrStatus.className = "notice";
        };
        remove.addEventListener("click", () => {
            proof.value = "";
            receiptDataUrl = "";
            preview.replaceChildren();
            preview.hidden = true;
            scanResult.replaceChildren();
            scanResult.hidden = true;
            resetDetectedFields();
            detailError.textContent = "";
        });
        previewActions.append(replace, remove);
        const scanResult = make("div", "ocr-scan-result");
        scanResult.hidden = true;
        const scanAgain = make("button", "btn btn-ghost-dark btn-small", "Scan receipt again");
        scanAgain.type = "button";
        scanAgain.addEventListener("click", scanReceipt);

        const fileOk = (file) => /\.(jpe?g|png|pdf)$/i.test(file?.name || "")
            && (!file?.type || allowedProofTypes.includes(file.type))
            && file.size <= proofLimitBytes;

        async function scanReceipt() {
            const file = proof.files?.[0];
            if (scanning || !receiptDataUrl || !file || !chosen) return;
            scanning = true;
            scanAgain.disabled = true;
            submit.disabled = false;
            duplicateDetected = false;
            scanResult.hidden = false;
            ocrStatus.textContent = "OCR Status: Scanning...";
            ocrStatus.className = "notice";
            scanResult.replaceChildren(make("p", "field-hint", "Scanning your payment receipt. Please wait..."));
            try {
                const result = await request(`/api/orders/${encodeURIComponent(order.reference)}/payments/scan`, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        ...(activeEmail ? { email: activeEmail } : {}),
                        method: chosen.provider,
                        reference: reference.value.trim(),
                        proof: { name: file.name, dataUrl: receiptDataUrl },
                    }),
                });
                const ocr = result.ocr || {};
                const fields = ocr.fields || {};
                const verification = result.verification || {};
                const status = result.ocr_status || (fields.reference || fields.amount ? "Detected" : "Failed");
                ocrStatus.textContent = `OCR Status: ${status}`;
                ocrStatus.className = status === "Failed" ? "notice" : status === "Detected" ? "notice notice-success" : "notice";
                scanResult.replaceChildren();
                const detectedAny = Boolean(fields.reference || fields.amount || fields.date_time);
                const detectedAll = Boolean(fields.reference && fields.amount && fields.date_time);
                const statusMessage = status === "Failed"
                    ? "Unable to read this receipt. Please upload a clearer screenshot or enter the details manually."
                    : (detectedAny && !detectedAll)
                        ? "Some payment details were detected. Please complete or correct the missing information."
                        : "Receipt scanned successfully. Please review the detected payment details.";
                scanResult.append(make("strong", "", statusMessage));
                if (status !== "Failed" && Number.isFinite(verification.confidence) && verification.confidence > 0) {
                    scanResult.append(make("p", "field-hint", `OCR confidence: ${Math.round(verification.confidence * 100)}%`));
                }
                const detected = make("div", "ocr-detected");
                [["Reference", fields.reference], ["Amount", fields.amount ? money(Number(fields.amount)) : ""], ["Date", fields.date_time], ["Method", fields.method], ["Status", fields.status]]
                    .filter(([, value]) => value)
                    .forEach(([label, value]) => detected.append(make("span", "ocr-chip", `${label}: ${value}`)));
                if (detected.children.length) scanResult.append(detected);

                const candidates = Array.isArray(ocr.reference_candidates) ? ocr.reference_candidates : [];
                const candidateValues = candidates.map((candidate) => String(candidate.value || "")).filter(Boolean);
                if (fields.reference) {
                    detectedReferenceValue = String(fields.reference);
                    const currentRef = reference.value.trim();
                    if (!currentRef || currentRef === detectedReferenceValue || !candidateValues.includes(currentRef)) {
                        reference.value = detectedReferenceValue;
                    }
                }
                if (fields.amount && !detectedAmountPaid.dataset.manual) {
                    detectedAmountPaid.value = money(Number(fields.amount));
                }
                if (fields.date_time && !detectedDate.dataset.manual) {
                    detectedDate.value = String(fields.date_time);
                }
                syncReferenceHint();

                if (candidates.length > 1) {
                    const candidateBox = make("div", "notice");
                    candidateBox.append(make("strong", "", "Multiple possible reference numbers detected"));
                    candidateBox.append(make("p", "field-hint", "Please confirm the correct reference number before submitting."));
                    candidates.slice(0, 5).forEach((candidate, index) => {
                        const value = String(candidate.value || "");
                        if (!value) return;
                        const optionLabel = make("label", "check-field");
                        const radio = make("input");
                        radio.type = "radio";
                        radio.name = `ref-candidate-${order.reference}`;
                        radio.value = value;
                        radio.checked = value === reference.value.trim() || (index === 0 && !reference.value.trim());
                        radio.addEventListener("change", () => {
                            if (radio.checked) {
                                reference.value = value;
                                detectedReferenceValue = value;
                                syncReferenceHint();
                                scanReceipt();
                            }
                        });
                        optionLabel.append(radio, document.createTextNode(` ${value}`));
                        candidateBox.append(optionLabel);
                    });
                    scanResult.append(candidateBox);
                }

                if (result.reference_message) {
                    scanResult.append(make("p", result.duplicate_reference ? "notice" : "notice notice-success", result.reference_message));
                }
                const previewMoney = result.down_payment || {};
                const previewExcess = Number(previewMoney.excess_amount || 0);
                const previewShortfall = Number(previewMoney.additional_needed_for_down_payment || 0);
                if (result.amount_match) {
                    scanResult.append(make("p", "notice notice-success", "Amount Matched: The detected payment amount matches the amount due for this stage."));
                } else if (fields.amount) {
                    scanResult.append(make("p", "notice", result.amount_message || "The detected amount differs from the server-calculated amount due. Check the receipt before submitting."));
                }
                if (previewExcess > 0.005) {
                    scanResult.append(make("p", "notice", `Excess of ${money(previewExcess)} over the whole order. It will be flagged for admin review and will not be treated as a confirmed payment until the shop checks it.`));
                } else if (previewShortfall > 0.005) {
                    scanResult.append(make("p", "notice", `The down payment is still short by ${money(previewShortfall)}. A partial payment is accepted, but the order cannot be confirmed until the minimum down payment is verified.`));
                }
                if (previewMoney.payment_status) {
                    scanResult.append(make("p", "field-hint", `Status if confirmed: ${previewMoney.payment_status}. Confirmed paid so far: ${money(previewMoney.cumulative_paid || 0)}; balance after: ${money(previewMoney.remaining_balance || 0)}.`));
                }
                duplicateDetected = Boolean(result.duplicate_reference);
                if (duplicateDetected) submit.disabled = true;
                (ocr.notes || []).forEach((note) => scanResult.append(make("p", "field-hint", note)));
                if (status === "Failed") {
                    scanResult.append(make("p", "notice", "Unable to detect the reference number. Please upload a clearer payment screenshot or enter the reference number manually."));
                }
                scanResult.append(make("p", "field-hint", "Scanning reads the receipt only. Payment is confirmed after the shop or provider verifies it."));
            } catch (issue) {
                ocrStatus.textContent = "OCR Status: Failed";
                ocrStatus.className = "notice";
                scanResult.replaceChildren(make("p", "notice", issue instanceof Error ? issue.message : "The receipt could not be scanned. The shop will review it."));
            } finally {
                scanning = false;
                scanAgain.disabled = !receiptDataUrl;
            }
        }

        proof.addEventListener("change", async () => {
            const file = proof.files?.[0];
            receiptDataUrl = "";
            preview.replaceChildren();
            scanResult.replaceChildren();
            scanResult.hidden = true;
            if (!file) {
                preview.hidden = true;
                return;
            }
            if (!fileOk(file)) {
                preview.hidden = true;
                detailError.textContent = `Upload the payment receipt as a JPG, PNG, or PDF file no larger than ${proofLimitLabel()}.`;
                proof.value = "";
                return;
            }
            detailError.textContent = "";
            try {
                receiptDataUrl = await fileAsDataUrl(file);
            } catch (issue) {
                detailError.textContent = issue instanceof Error ? issue.message : "The receipt could not be read.";
                return;
            }
            preview.replaceChildren();
            if (file.type.startsWith("image/")) {
                const image = make("img", "receipt-thumbnail");
                image.src = receiptDataUrl;
                image.alt = `Preview of ${file.name}`;
                preview.append(image);
            } else {
                preview.append(make("span", "receipt-file-badge", `${file.name} · PDF receipt`));
            }
            preview.append(make("span", "field-hint", `${file.name} · ${(file.size / 1024).toFixed(0)} KB`), previewActions);
            preview.hidden = false;
            await scanReceipt();
        });

        formElement.append(
            make("h4", "", down ? `Pay your ${due.label.toLowerCase()}` : "Pay the remaining balance"),
            make("p", "field-hint", `Choose an e-wallet, pay the amount you actually sent to the shop's account, then upload the payment screenshot. The shop suggests ${money(due.amount)} for this stage; you can correct the amount below if you paid a different amount. The system scans the receipt only to assist review. Your payment stays Pending Verification until the shop checks the money in its account${down ? "; your order is confirmed once the minimum down payment is verified" : ""}. The shop never asks for your PIN or OTP.`),
            cards,
            details,
            (() => { const label = field(`Amount you paid (${due.label}, PHP)`, amount); label.append(amountHint); return label; })(),
            proofLabel,
            preview,
            ocrStatus,
            scanResult,
            scanAgain,
            referenceLabel,
            detectedAmountLabel,
            detectedDateLabel,
            submit,
        );
        formElement.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!formElement.reportValidity()) return;
            const file = proof.files?.[0];
            if (!file || !fileOk(file)) {
                detailError.textContent = `Upload the payment screenshot or receipt as a JPG, PNG, or PDF file no larger than ${proofLimitLabel()}. The file type and size must match the file you selected.`;
                return;
            }
            if (!chosen) {
                detailError.textContent = "Choose the e-wallet you paid with.";
                return;
            }
            const referenceValue = reference.value.trim();
            if (referenceValue.length < 4) {
                detailError.textContent = "Enter the complete reference number from your receipt before submitting.";
                reference.focus();
                return;
            }
            if (duplicateDetected && referenceValue === detectedReferenceValue) {
                detailError.textContent = "This reference number has already been submitted. Please check your payment details or upload a different transaction receipt.";
                return;
            }
            let declaredAmount = null;
            if (amount.value.trim()) {
                declaredAmount = Number(amount.value);
                if (!Number.isFinite(declaredAmount) || declaredAmount <= 0) {
                    detailError.textContent = "Enter the positive amount you actually paid, or leave it blank to use the amount on the receipt.";
                    amount.focus();
                    return;
                }
                declaredAmount = Math.round((declaredAmount + Number.EPSILON) * 100) / 100;
            }
            const reportedAmount = declaredAmount ?? (fieldsAmount(detectedAmountPaid.value) ?? due.amount);
            if (!(await confirmAction(`Submit your ${chosen.provider} payment of ${money(reportedAmount)} (reference ${referenceValue}) for verification? Only the shop's confirmation will count it as paid.`, { title: "Submit payment?", confirmLabel: "Submit payment" }))) return;
            let proofPayload;
            try {
                proofPayload = { name: file.name, dataUrl: receiptDataUrl || await fileAsDataUrl(file) };
            } catch (issue) {
                detailError.textContent = issue instanceof Error ? issue.message : "The receipt could not be read.";
                return;
            }
            // An explicit amount wins; otherwise the server uses OCR and finally its own suggested amount.
            await orderAction("payments", {
                ...(declaredAmount === null ? {} : { amount: declaredAmount }),
                method: chosen.provider,
                reference: referenceValue,
                proof: proofPayload,
            }, "The payment could not be submitted.", submit);
        });
        return formElement;
    }

    function renderPaymentSnapshot(order) {
        const payment = order.payment || {};
        const submissions = order.payment_submissions || [];
        const pendingCount = submissions.filter((item) => item.status === "pending").length;
        const verifiedCount = submissions.filter((item) => item.status === "verified").length;
        const rejectedCount = submissions.filter((item) => item.status === "rejected").length;
        const moneyNow = paymentMoney(order);
        const total = moneyNow.total === null ? "Quotation pending" : money(moneyNow.total);
        const paid = moneyNow.total === null ? "—" : money(moneyNow.paid);
        const balance = moneyNow.balanceDue === null ? "—" : money(moneyNow.balanceDue);
        const submittedParts = [];
        submittedParts.push(`${submissions.length} total`);
        if (verifiedCount) submittedParts.push(`${verifiedCount} verified`);
        if (pendingCount) submittedParts.push(`${pendingCount} pending`);
        if (rejectedCount) submittedParts.push(`${rejectedCount} not verified`);
        const submitted = submissions.length ? submittedParts.join(" · ") : "None yet";

        // The server owns payment and work progress; the older client-side fallbacks cover older records only.
        let paymentStatus = moneyNow.status;
        if (["Rejected", "Cancelled", "Quotation Declined"].includes(order.status)) {
            paymentStatus = order.status;
        } else if (order.status === "Quotation Sent" && !paymentStatus) {
            paymentStatus = "Quotation pending acceptance";
        } else if (!paymentStatus) {
            if (order.status === "Awaiting Down Payment") {
                paymentStatus = pendingCount ? "Down payment submitted" : "Awaiting down payment";
            } else if (moneyNow.balanceDue !== null && moneyNow.balanceDue <= 0.005 && moneyNow.paid > 0) {
                paymentStatus = "Fully Paid";
            } else if (payment.down_payment_status) {
                paymentStatus = payment.down_payment_status;
            } else if (order.total_amount === null) {
                paymentStatus = "Awaiting quotation";
            } else {
                paymentStatus = "Not available";
            }
        }
        const workStatus = order.work_status || payment.work_status || "Not available";
        // The server derives this from order.status; never show a release destination as current progress.
        const fulfillmentStatus = order.fulfillment?.fulfillment_status || "Not started";

        const setText = (id, value) => {
            const node = document.querySelector(id);
            if (node) node.textContent = value;
        };
        setText("#snapshot-total", total);
        setText("#snapshot-payment-status", paymentStatus);
        setText("#snapshot-paid", paid);
        setText("#snapshot-balance", balance);
        setText("#snapshot-submitted", submitted);
        setText("#snapshot-work-status", workStatus);
        setText("#snapshot-fulfillment-status", fulfillmentStatus);

        const colourStatus = (selector, value) => {
            const node = document.querySelector(selector);
            if (!node) return;
            node.classList.remove("is-good", "is-warn", "is-bad", "is-muted");
            const good = /verified|fully paid|^paid$|approved/i.test(value);
            const bad = /rejected|cancelled|declined|not verified|revision/i.test(value);
            const warn = /pending|awaiting|submitted|review/i.test(value);
            node.classList.add(bad ? "is-bad" : good ? "is-good" : warn ? "is-warn" : "is-muted");
        };
        colourStatus("#snapshot-payment-status", paymentStatus);
        colourStatus("#snapshot-work-status", workStatus);
        colourStatus("#snapshot-fulfillment-status", fulfillmentStatus);
    }

    /** Compact, always-visible summary of money and whatever the customer must do next. */
    function renderActionStrip(order) {
        actionStrip.replaceChildren();
        actionStrip.hidden = false;
        const payment = order.payment || {};
        const due = payment.next_payment;
        const pending = (order.payment_submissions || []).find((item) => item.status === "pending");
        const awaitingDraft = order.status === "Awaiting Customer Approval" && Boolean(order.draft_attachment);
        const work = order.work_approval || {};
        const workPending = work.status === "pending" && (order.customer_actions?.approve_work || order.customer_actions?.request_work_revision);
        const quoteOpen = quotationIsOpen(order);
        const canAct = detailSource === "account" || signedIn;
        const actions = [];
        const heading = make("div", "action-strip-heading");
        heading.append(make("strong", "", "Needs your attention"));
        const balance = paymentMoney(order).balanceDue;
        heading.append(make("span", "field-hint", `Remaining balance: ${balance === null ? "—" : money(balance)}`));
        actionStrip.append(heading);
        const row = make("div", "action-strip-items");
        // The strip directs customers to the one place where the decision is made.
        // Duplicating accept/decline buttons here and in the panel made the same
        // action appear twice with two different handlers.
        if (quoteOpen) {
            const item = make("div", "action-strip-item is-urgent");
            item.append(make("span", "action-badge", "Quotation"));
            item.append(make("span", "", `Review ${money(order.total_amount)} and accept or decline it in the quotation section.`));
            const review = make("button", "btn btn-dark btn-small", "Review quotation");
            review.type = "button";
            review.addEventListener("click", () => quotationPanel.scrollIntoView({ block: "start", behavior: "smooth" }));
            item.append(review);
            row.append(item);
            actions.push("quotation");
        }
        if (awaitingDraft) {
            const item = make("div", "action-strip-item is-urgent");
            item.append(make("span", "action-badge", "Draft"));
            item.append(make("span", "", "Approve the draft to start printing, or request a revision."));
            const review = make("button", "btn btn-dark btn-small", "Review draft");
            review.type = "button";
            review.addEventListener("click", () => document.querySelector("#customer-draft")
                .scrollIntoView({ block: "start", behavior: "smooth" }));
            item.append(review);
            row.append(item);
            actions.push("draft");
        }
        if (workPending) {
            const item = make("div", "action-strip-item is-urgent");
            item.append(make("span", "action-badge", "Finished work"));
            item.append(make("span", "", "Review the finished work, then approve it or request a revision. Final payment stays locked until you approve."));
            const review = make("button", "btn btn-dark btn-small", "Review work");
            review.type = "button";
            review.disabled = !canAct;
            review.addEventListener("click", () => {
                currentStep = "approval";
                if (currentOrder) syncOrderUrl(currentOrder.reference, currentStep);
                workReviewPanel.scrollIntoView({ block: "start", behavior: "smooth" });
            });
            item.append(review);
            row.append(item);
            actions.push("work");
        }
        if (due && due.locked && !workPending && canAct) {
            const item = make("div", "action-strip-item");
            item.append(make("span", "action-badge", "Payment locked"));
            item.append(make("span", "", due.reason || payment.final_payment_lock || "The remaining balance is locked."));
            row.append(item);
            actions.push("payment-lock");
        }
        if (due && !due.locked && canAct && !(order.customer_actions && order.customer_actions.pay === false)) {
            const item = make("div", "action-strip-item is-urgent");
            item.append(make("span", "action-badge", "Payment"));
            item.append(make("span", "", `${due.label}: send ${money(due.amount)} and submit your receipt.`));
            const pay = make("button", "btn btn-dark btn-small", "Go to payment");
            pay.type = "button";
            pay.addEventListener("click", () => {
                currentStep = "payment";
                if (currentOrder) syncOrderUrl(currentOrder.reference, currentStep);
                paymentPanel.scrollIntoView({ block: "start", behavior: "smooth" });
            });
            item.append(pay);
            row.append(item);
            actions.push("payment");
        }
        if (pending) {
            const item = make("div", "action-strip-item");
            item.append(make("span", "action-badge", "Submitted"));
            item.append(make("span", "", `${pending.method} payment of ${money(pending.amount)} is ${pending.verification_status_label || "waiting for verification"}.`));
            row.append(item);
        }
        if (!actions.length && !pending) {
            actionStrip.hidden = true;
            return;
        }
        if (!actions.length) {
            row.append(make("p", "field-hint", "Nothing else is due from you right now."));
        }
        actionStrip.append(row);
    }

    function renderDetail(order, options = {}) {
        // Collapse the toggle sections when a different order is opened; keep them as they are on a refresh.
        if (!currentOrder || currentOrder.reference !== order.reference) {
            detail.querySelectorAll(".detail-toggle").forEach((section) => { section.open = false; });
        }
        currentOrder = order;
        detailError.textContent = "";
        list.hidden = true;
        detail.hidden = false;
        document.querySelector("#detail-service").textContent = order.product.category;
        document.querySelector("#detail-item").textContent = order.product.name;
        document.querySelector("#detail-meta").textContent = `Submitted ${formatDate(order.created_at)} · Quantity ${order.customization.quantity}`;
        document.querySelector("#detail-ref").textContent = order.reference;
        const statusBanner = document.querySelector("#detail-status");
        statusBanner.textContent = order.status;
        statusBanner.className = `status-banner ${statusSlug(order.status)}`;
        renderTimeline(order);

        const summary = document.querySelector("#detail-summary");
        summary.replaceChildren();
        const orderType = { package: `Package${order.package?.name ? `: ${order.package.name}` : ""}`, design: "Customized order (shop design)" }[order.order_kind] || "Customized order";
        if (order.order_kind) appendSummaryRow(summary, "Order type", orderType);
        // The shop design chosen for the order (saved as ordered) and the details the customer gave for it.
        if (order.design) {
            const design = order.design;
            const row = make("div", "summary-row");
            const holder = make("dd", "portal-design");
            const image = make("img");
            image.src = design.image;
            image.alt = `${design.name} design preview`;
            image.loading = "lazy";
            const copy = make("div");
            copy.append(make("strong", "", design.name));
            if (design.style) copy.append(make("p", "field-hint", design.style));
            if (design.is_sample) copy.append(make("p", "field-hint", "Sample design shown for illustration; the shop confirms the final artwork in your draft."));
            holder.append(image, copy);
            row.append(make("dt", "", "Selected design"), holder);
            summary.append(row);
            (design.details || []).forEach((item) => appendSummaryRow(summary, item.label, item.value));
        }
        // The package price and inclusions saved when the order was placed (later package edits do not change them).
        if (order.package) {
            const pkg = order.package;
            const minimum = pkg.minimum_quantity ?? pkg.included_quantity;
            appendSummaryRow(summary, "Package price", `${money(pkg.price)} for ${minimum} pcs (minimum order)`);
            if (pkg.add_on_price !== null && pkg.add_on_price !== undefined) {
                appendSummaryRow(summary, "Additional piece", `${money(pkg.add_on_price)} each`);
                if (pkg.additional_quantity) {
                    appendSummaryRow(summary, "Additional pieces", `${pkg.additional_quantity} × ${money(pkg.add_on_price)} = ${money(pkg.additional_cost)}`);
                }
            }
            const details = pkg.inclusion_details?.length ? pkg.inclusion_details : (pkg.inclusions || []).map((name) => ({ name, description: "" }));
            if (details.length) appendSummaryRow(summary, "Package inclusions", details.map((item) => (item.description ? `${item.name} (${item.description})` : item.name)).join("; "));
        }
        appendSummaryRow(summary, "Quantity", `${order.customization.quantity} pcs`);
        // A standard size from the price list (Tarpaulin): the size and its price, as saved when the order was placed.
        if (order.price_list) {
            appendSummaryRow(summary, "Selected size", order.price_list.size);
            appendSummaryRow(summary, "Unit price", typeof order.price_list.unit_price === "number" ? `${money(order.price_list.unit_price)} per piece (price list)` : "");
        }
        if (typeof order.pricing?.unit_price === "number") appendSummaryRow(summary, "Price per piece", `${money(order.pricing.unit_price)}${order.fixed_price ? "" : " (estimate)"}`);
        specificationRows(order).forEach(([label, value]) => appendSummaryRow(summary, label, value));
        appendSummaryRow(summary, "Special instructions", order.customization.notes ?? order.customization.instructions);
        appendSummaryRow(summary, "Customer", order.customer.name);
        appendSummaryRow(summary, "Email", order.customer.email);
        appendSummaryRow(summary, "Contact number", order.customer.phone);
        appendSummaryRow(summary, "Messenger name", order.customer.messenger);
        if (order.has_event) {
            eventRows(order).forEach(([label, value]) => appendSummaryRow(summary, label, value));
        }
        if (order.total_amount === null && estimateOf(order) !== null) {
            appendSummaryRow(summary, "Estimated total", `${money(estimateOf(order))} (from shop prices; not yet confirmed)`);
        }
        const quoteWaiting = order.status === "Quotation Sent" || order.status === "Quotation Declined";
        appendSummaryRow(summary, order.fixed_price ? (order.fixed_price_label || "Package total") : "Confirmed total", order.total_amount === null
            ? "Waiting for Admin Quotation"
            : `${money(order.total_amount)}${quoteWaiting ? " (quotation, not yet accepted)" : ""}`);
        const payment = order.payment || {};
        const cod = payment.payment_option === "cod";
        appendSummaryRow(summary, "Payment", payment.payment_option_label || "50% Down Payment");
        if (cod) {
            appendSummaryRow(summary, "Down payment", "Not required (Cash on Delivery)");
        } else if (payment.down_payment_required !== null && payment.down_payment_required !== undefined) {
            appendSummaryRow(summary, downPaymentLabel(payment), `${money(payment.down_payment_required)} · ${payment.down_payment_status}`);
        }
        if (order.total_amount !== null && payment.payment_option_chosen && !quoteWaiting) {
            const moneyNow = paymentMoney(order);
            appendSummaryRow(summary, "Payment received (verified)", money(moneyNow.paid));
            if (payment.pending_verification) appendSummaryRow(summary, "Waiting for verification", money(payment.pending_verification));
            appendSummaryRow(summary, cod ? "Due on delivery / pickup" : "Remaining balance", money(moneyNow.balanceDue));
            appendSummaryRow(summary, "Payment status", moneyNow.status || "—");
            if (moneyNow.shortfall > 0.005) appendSummaryRow(summary, `Still due on ${downPaymentLabel(payment).toLowerCase()}`, money(moneyNow.shortfall));
            if (moneyNow.excess > 0.005) appendSummaryRow(summary, "Excess (admin review)", money(moneyNow.excess));
        }
        const fulfillment = order.fulfillment || {};
        appendSummaryRow(summary, "Delivery or pickup", fulfillment.label || "Store Pickup");
        (fulfillment.address || []).forEach((row) => appendSummaryRow(summary, row.label, row.value));
        const shipping = order.shipping || {};
        if (shipping.delivery_province) {
            appendSummaryRow(summary, "Delivery province", shipping.delivery_province);
            appendSummaryRow(summary, "Destination region", shipping.destination_region);
            appendSummaryRow(summary, "Shipping fee", money(payment.shipping_fee ?? shipping.shipping_fee ?? 0));
            if (shipping.estimated_delivery) appendSummaryRow(summary, "Estimated delivery", shipping.estimated_delivery);
            if (typeof payment.order_subtotal === "number") appendSummaryRow(summary, "Order subtotal", money(payment.order_subtotal));
            if (order.total_amount !== null) appendSummaryRow(summary, "Total amount", money(order.total_amount));
        }
        renderAttachments(summary, order.attachments, "Uploaded files");

        const draft = document.querySelector("#customer-draft");
        const awaitingApproval = order.status === "Awaiting Customer Approval" && order.draft_attachment;
        draft.hidden = !awaitingApproval;
        const drafts = order.drafts || [];
        if (awaitingApproval) {
            const download = document.querySelector("#draft-download");
            download.href = order.draft_attachment.url;
            download.textContent = `Open draft: ${order.draft_attachment.name}`;
            const latest = drafts[drafts.length - 1];
            const remarks = document.querySelector("#draft-remarks");
            remarks.hidden = !latest?.remarks;
            remarks.textContent = latest?.remarks ? `Notes from the shop: ${latest.remarks}` : "";
            document.querySelector("#draft-revision-note").value = "";
        }
        const draftHistory = document.querySelector("#draft-history");
        document.querySelector("#draft-history-toggle").hidden = drafts.length < 1;
        draftHistory.replaceChildren(...drafts.slice().reverse().map((item) => {
            const entry = make("li", `draft-${item.status}`);
            entry.append(make("strong", "", `Draft ${item.number} · ${item.status_label}`));
            if (item.file) {
                const link = make("a", "", item.file.name);
                link.href = item.file.url;
                link.target = "_blank";
                link.rel = "noopener";
                entry.append(link);
            }
            if (item.sent_at) entry.append(make("span", "field-hint", `Sent ${formatDate(item.sent_at)}${item.responded_at ? ` · answered ${formatDate(item.responded_at)}` : ""}`));
            if (item.remarks) entry.append(make("span", "field-hint", `Shop notes: ${item.remarks}`));
            if (item.revision_note) entry.append(make("span", "field-hint", `Your revision request: ${item.revision_note}`));
            return entry;
        }));
        const history = document.querySelector("#detail-history");
        history.replaceChildren();
        order.history.forEach((entry) => {
            const item = make("li");
            item.append(make("span", "", entry.text), make("time", "", formatDate(entry.at)));
            history.append(item);
        });
        document.querySelector("#approve-draft").disabled = !awaitingApproval;
        document.querySelector("#request-draft-changes").disabled = !awaitingApproval;
        renderQuotationPanel(order);
        renderWorkReviewPanel(order);
        renderPaymentPanel(order);
        renderPaymentSnapshot(order);
        // Confirmed orders have a verified payment, so they are never cancellable here.
        const canCancel = canCancelOrder(order);
        const cancelButton = document.querySelector("#cancel-order");
        cancelButton.hidden = !canCancel;
        cancelButton.disabled = false;
        // Tracking (production, pickup, courier), cancellation and refund requests, and the receipt.
        // PortalTracking.render() computes the receipt toggle from this order's current action flags.
        window.PortalTracking?.render(order, {
            orderAction, confirmAction, money, formatDate, make, definitionList, fileAsDataUrl,
            paymentMethods, paymentMethodsStatus, copyButton, detailError,
            canAct: detailSource === "account" || signedIn,
        });
        renderActionStrip(order);
        // Only an explicit open scrolls to the top; refreshing after an action keeps the customer in place.
        if (options.scroll) detail.scrollIntoView({ block: "start", behavior: "instant" });
    }

    function showOrderList() {
        unwatch?.();
        unwatch = null;
        currentStep = "";
        detail.hidden = true;
        accountSection.hidden = false;
        accountPanel.hidden = false;
        lookupPanel.hidden = false;
        list.hidden = true;
        error.textContent = "";
        document.dispatchEvent(new CustomEvent("customer-orders-changed"));
    }

    /** True while a form has typed values or a chosen receipt: an update must not rebuild that form. */
    function formBusy() {
        if (confirmingAction) return true;
        const fields = document.querySelectorAll(
            "#customer-payment input:not([type=hidden]), #customer-payment textarea, "
            + "#customer-work-review textarea, #customer-requests input:not([type=hidden]), #customer-requests textarea"
        );
        return Array.from(fields).some((field) => {
            if (field.type === "file") return Boolean(field.files?.length);
            // Radio/checkbox values are always present; only a selected one is real input.
            if (field.type === "radio" || field.type === "checkbox") return field.checked;
            return Boolean(field.value);
        });
    }

    /** Status-only update for the case where an in-progress form must stay untouched. */
    function applyLiveStatus(order) {
        currentOrder = order;
        const banner = document.querySelector("#detail-status");
        banner.textContent = order.status;
        banner.className = `status-banner ${statusSlug(order.status)}`;
        renderTimeline(order);
        renderPaymentSnapshot(order);
        renderActionStrip(order);
        const payment = order.payment || {};
        const pending = (order.payment_submissions || []).some((item) => item.status === "pending");
        const due = payment.next_payment;
        const submit = paymentPanel.querySelector('button[type="submit"]');
        if (submit) submit.disabled = !(due && !due.locked && !pending && order.customer_actions?.pay !== false && (detailSource === "account" || signedIn));
        const work = order.work_approval || {};
        const workPending = work.status === "pending";
        document.querySelector("#approve-work").disabled = !workPending || !order.customer_actions?.approve_work;
        document.querySelector("#request-work-revision").disabled = !workPending || !order.customer_actions?.request_work_revision;
        document.querySelector("#cancel-order").hidden = !canCancelOrder(order);
    }

    let refreshing = false;
    /** Called when the server's version moves: fetch the authoritative order and redraw without reloading. */
    async function quietRefresh(reference) {
        if (!currentOrder || currentOrder.reference !== reference || refreshing) return;
        refreshing = true;
        try {
            const order = await request(`/api/account/orders/${encodeURIComponent(reference)}`);
            if (formBusy()) {
                // Keep the customer's typing and receipt; apply only the status and button changes now.
                applyLiveStatus(order);
            } else {
                renderDetail(order);
            }
        } catch (issue) {
            if (issue.status === 401) document.dispatchEvent(new CustomEvent("customer-session-ended"));
            // A transient network error leaves the current view intact; the next poll retries.
        } finally {
            refreshing = false;
        }
    }

    function subscribe(reference, version) {
        unwatch?.();
        unwatch = null;
        if (!window.PrintShopWorkflowSync) return;
        unwatch = window.PrintShopWorkflowSync.watch({
            reference,
            role: "account",
            version,
            onChange: () => { void quietRefresh(reference); },
        });
    }

    async function loadOrder(reference, options = {}) {
        error.textContent = "";
        detailError.textContent = "";
        try {
            // Verified by the session: the server only returns orders owned by the signed-in account.
            const order = await request(`/api/account/orders/${encodeURIComponent(reference)}`);
            activeEmail = order.customer.email;
            detailSource = "account";
            accountSection.hidden = true;
            lookupPanel.hidden = true;
            list.hidden = true;
            detail.hidden = false;
            currentStep = options.step || currentStep;
            renderDetail(order, { scroll: true });
            syncOrderUrl(order.reference, currentStep, Boolean(options.fromRoute));
            subscribe(order.reference, order.version);
            if (currentStep) focusStep(currentStep);
        } catch (issue) {
            if (issue.status === 401) {
                // The sign-in ended (expired, or the password was reset on another device).
                document.dispatchEvent(new CustomEvent("customer-session-ended"));
                return;
            }
            if (issue.status === 404 && options.fromRoute) {
                // A deleted order or hand-written URL: leave the stale route and explain why.
                showOrderList();
                error.textContent = "This order is no longer available.";
                window.history.replaceState(null, "", "/customer-portal");
                return;
            }
            error.textContent = issue instanceof Error ? issue.message : "The order could not be loaded.";
        }
    }

    document.addEventListener("customer-order-select", (event) => {
        const { reference } = event.detail || {};
        if (typeof reference !== "string" || !reference) return;
        currentStep = "";
        void loadOrder(reference);
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!form.reportValidity()) return;
        currentStep = "";
        await loadOrder(referenceInput.value.trim());
    });

    document.querySelector("#back-to-orders").addEventListener("click", () => {
        showOrderList();
        window.history.pushState(null, "", "/customer-portal");
        accountPanel.scrollIntoView({ block: "start", behavior: "instant" });
    });

    document.querySelector("#approve-draft").addEventListener("click", (event) => { void approveDraft(event.currentTarget); });
    document.querySelector("#request-draft-changes").addEventListener("click", (event) => { void requestDraftChanges(event.currentTarget); });
    document.querySelector("#approve-work").addEventListener("click", (event) => { void approveFinishedWork(event.currentTarget); });
    document.querySelector("#request-work-revision").addEventListener("click", (event) => { void requestFinishedWorkRevision(event.currentTarget); });

    document.querySelector("#cancel-order").addEventListener("click", async (event) => {
        const button = event.currentTarget;
        if (!currentOrder || !(await confirmAction("Cancel this request? This cannot be done.", { title: "Cancel request?", confirmLabel: "Cancel request", tone: "danger" }))) return;
        void orderAction("cancel", {}, "The request could not be cancelled.", button);
    });

    // Back/Forward re-reads the route and the database; it never relies on the previous in-memory order.
    window.addEventListener("popstate", () => {
        const route = parseOrderRoute();
        if (!route) {
            showOrderList();
            return;
        }
        currentStep = route.step || "";
        void loadOrder(route.reference, { fromRoute: true, step: currentStep });
    });
    document.addEventListener("customer-session-ended", () => {
        unwatch?.();
        unwatch = null;
        // A session can expire while an order is open. Return to this exact order after signing in again.
        if (!parseOrderRoute()) return;
        const target = window.location.pathname + window.location.search;
        window.location.replace(`/sign-in?${new URLSearchParams({ next: target })}`);
    });

    // Old links (/customer-portal?email=…&ref=…) only prefill the reference; opening it still needs sign-in.
    const parameters = new URLSearchParams(window.location.search);
    if (parameters.get("ref")) referenceInput.value = parameters.get("ref");
    // Restore the exact workflow from the URL only after the session has been restored.
    void sessionReady.then((customer) => {
        const route = parseOrderRoute();
        if (!route) return;
        if (!customer) {
            const target = window.location.pathname + window.location.search;
            window.location.replace(`/sign-in?${new URLSearchParams({ next: target })}`);
            return;
        }
        currentStep = route.step || "";
        void loadOrder(route.reference, { fromRoute: true, step: currentStep });
    });
})();
