"use strict";

/**
 * Customer portal: order tracking (production, store pickup, courier delivery), cancellation requests,
 * refund requests, delivery issue reports, and the printable receipt.
 *
 * Only the actions the server lists in order.customer_actions are offered; the server enforces the same
 * rules when a request is sent. Called by portal.js (window.PortalTracking.render) for the open order.
 */
(() => {
    const tracking = document.querySelector("#customer-tracking");
    const requests = document.querySelector("#customer-requests");
    const receiptButton = document.querySelector("#download-receipt");
    const MAX_FILE_BYTES = 5 * 1024 * 1024;
    let current = null;
    let context = null;

    const CANCEL_REASONS = [
        ["change_of_mind", "Changed my mind"],
        ["delayed", "Order is taking too long"],
        ["wrong_details", "Wrong order details"],
        ["duplicate_order", "Ordered twice by mistake"],
        ["financial", "Financial reasons"],
        ["other", "Other"],
    ];
    const REFUND_REASONS = [
        ["duplicate_payment", "I paid twice (duplicate payment)"],
        ["excess_payment", "I paid more than the amount due"],
        ["incorrect_product", "I received the wrong product", true],
        ["damaged_product", "The product arrived damaged", true],
        ["other", "Other"],
    ];
    const ISSUE_TYPES = [["not_received", "Order not received"], ["damaged", "Damaged on arrival"], ["wrong_item", "Wrong or incomplete item"], ["late", "Late delivery"], ["other", "Other"]];
    const CANCEL_STATUS = { pending_review: "Pending review", approved: "Approved", rejected: "Not approved" };
    const RECEIVED = ["Delivered", "Order Claimed", "Completed"];

    function el(tag, className, text) {
        return context.make(tag, className, text);
    }

    function link(text, href) {
        const anchor = el("a", "", text);
        anchor.href = href;
        anchor.target = "_blank";
        anchor.rel = "noopener";
        return anchor;
    }

    function select(options, name) {
        const element = el("select", "input");
        element.name = name;
        element.required = true;
        element.append(new Option("Choose…", ""), ...options.map(([value, text]) => new Option(text, value)));
        return element;
    }

    function labelled(text, control) {
        const label = el("label", "", text);
        label.append(control);
        return label;
    }

    function textarea(name, placeholder, required = false) {
        const element = el("textarea", "input");
        Object.assign(element, { name, rows: 3, maxLength: 1000, placeholder, required });
        return element;
    }

    function input(name, attrs = {}) {
        const element = el("input", "input");
        Object.assign(element, { name, ...attrs });
        return element;
    }

    async function files(inputElement, limit) {
        const list = Array.from(inputElement?.files || []);
        if (list.length > limit) throw new Error(`Attach up to ${limit} photos.`);
        return Promise.all(list.map(async (file) => {
            if (file.size > MAX_FILE_BYTES || !/\.(jpe?g|png|webp|pdf)$/i.test(file.name)) {
                throw new Error(`${file.name}: use a JPG, PNG, WebP, or PDF file no larger than 5 MB.`);
            }
            return { name: file.name, dataUrl: await context.fileAsDataUrl(file) };
        }));
    }

    /** Where a refund should be sent: an e-wallet the shop supports, never a password, PIN, or OTP. */
    function recipientFields(required) {
        const provider = el("select", "input");
        provider.name = "refund_provider";
        provider.required = required;
        provider.append(new Option("Same e-wallet I paid with", "original"));
        const notice = el("p", "field-hint");
        notice.hidden = true;
        const retry = el("button", "btn btn-ghost-dark btn-small", "Retry payment methods");
        retry.type = "button";
        retry.hidden = true;
        let methodOptions = [];

        const showProblem = (problem) => {
            notice.textContent = problem;
            notice.hidden = false;
            retry.hidden = false;
            provider.dataset.methodsError = "1";
            provider.setAttribute("aria-invalid", "true");
        };
        const load = async (options = {}) => {
            retry.disabled = true;
            provider.setAttribute("aria-busy", "true");
            try {
                const result = context.paymentMethodsStatus
                    ? await context.paymentMethodsStatus(options)
                    : { methods: await context.paymentMethods(options), error: "" };
                methodOptions.forEach((option) => option.remove());
                methodOptions = (result.methods || []).map((method) => new Option(method.provider, method.provider));
                provider.append(...methodOptions);
                if (result.error) showProblem(`Payment methods could not be loaded (${result.error}). Retry before sending this request.`);
                else if (!methodOptions.length) showProblem("No e-wallets are configured in the shop right now. Contact the shop before sending this request.");
                else {
                    notice.hidden = true;
                    retry.hidden = true;
                    delete provider.dataset.methodsError;
                    provider.removeAttribute("aria-invalid");
                }
            } catch (error) {
                showProblem(`Payment methods could not be loaded (${error instanceof Error ? error.message : "unknown error"}). Retry before sending this request.`);
            } finally {
                retry.disabled = false;
                provider.removeAttribute("aria-busy");
            }
        };
        retry.addEventListener("click", () => { void load({ refresh: true }); });
        void load();

        const name = input("recipient_name", { maxLength: 120, required, placeholder: "Account name" });
        const number = input("recipient_number", { maxLength: 40, required, placeholder: "e.g. 0917 123 4567", inputMode: "tel" });
        const wrap = el("div", "request-grid");
        const refundLabel = labelled("Refund to", provider);
        refundLabel.append(notice, retry);
        wrap.append(refundLabel, labelled("E-wallet account name", name), labelled("E-wallet number", number));
        return { element: wrap, values: () => ({ refund_provider: provider.value, recipient_name: name.value.trim(), recipient_number: number.value.trim() }) };
    }

    function submitForm(form, action, build, confirmText) {
        const button = form.querySelector("button[type=submit]");
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const pendingMethods = form.querySelector('select[data-methods-error="1"]');
            if (pendingMethods && !pendingMethods.closest("[hidden]")) {
                context.detailError.textContent = "Payment methods could not be loaded. Use Retry payment methods before sending.";
                pendingMethods.focus();
                return;
            }
            if (!form.reportValidity()) return;
            let body;
            try {
                body = await build();
            } catch (issue) {
                context.detailError.textContent = issue instanceof Error ? issue.message : "Check the form.";
                return;
            }
            const ask = context.confirmAction || window.PrintConfirm;
            if (!(await ask(confirmText, { title: "Send request?", confirmLabel: "Send request" }))) return;
            await context.orderAction(action, body, "The request could not be sent.", button);
        });
        return form;
    }

    // ================================================================ tracking

    function renderTracking(order) {
        tracking.replaceChildren();
        const f = order.fulfillment || {};
        const p = order.production || {};
        const rows = [];
        if (p.started_at || p.estimated_completion) {
            rows.push(["Production", `${p.progress || 0}% complete${p.started_at ? ` · started ${context.formatDate(p.started_at)}` : ""}`]);
            if (p.estimated_completion) rows.push(["Estimated production completion", context.formatDate(p.estimated_completion)]);
        }
        if (p.quality_check?.passed) rows.push(["Quality check", `Passed · ${context.formatDate(p.quality_check.at)}`]);
        if (p.packaging?.completed_at) rows.push(["Packed", `${context.formatDate(p.packaging.completed_at)}${p.packaging.completed_quantity ? ` · ${p.packaging.completed_quantity} pcs` : ""}`]);
        if (f.method === "delivery") {
            rows.push(["Delivery area", f.area_label || "—"], ["Delivery fee", context.money(f.delivery_fee || 0)]);
            if (f.estimate_text) rows.push(["Estimated courier delivery", f.estimate_text]);
            if (f.courier) rows.push(["Courier", `${f.courier}${f.courier_service ? ` · ${f.courier_service}` : ""}`]);
            if (f.delivery_personnel) rows.push(["Delivered by", f.delivery_personnel]);
            if (f.shipment_date) rows.push(["Shipped", context.formatDate(f.shipment_date)]);
            if (f.scheduled_date) rows.push(["Scheduled delivery", context.formatDate(f.scheduled_date)]);
            if (f.delivered_at) rows.push(["Delivered", context.formatDate(f.delivered_at)]);
        } else {
            const location = f.pickup_location;
            rows.push(["Pickup location", location ? `${location.name}, ${location.address}${location.hours ? ` (${location.hours})` : ""}` : "The shop"]);
            if (f.pickup_schedule) rows.push(["Pickup schedule", `${context.formatDate(f.pickup_schedule.date)} ${f.pickup_schedule.time || ""}`.trim()]);
            else if (f.preferred_pickup_date) rows.push(["Your preferred pickup date", context.formatDate(f.preferred_pickup_date)]);
            if (f.pickup_confirmation) rows.push(["Claimed", `${f.pickup_confirmation.claimant_name} (${f.pickup_confirmation.claimant_type_label}) · ${context.formatDate(f.pickup_confirmation.claimed_at)}`]);
        }
        const reprint = p.reprint;
        if (reprint) rows.unshift(["Reprint", `${reprint.status === "in_progress" ? "In progress" : "Completed"} · ${reprint.quantity} pc(s) · ${reprint.chargeable ? `agreed charge ${context.money(reprint.charge_amount)}` : "no additional charge"}`]);
        const show = rows.length > 2 || reprint || f.courier || f.pickup_code || ["Ready for Pickup", "Order Claimed", "Ready for Dispatch", "Out for Delivery", "Delivery Failed", "Delivered"].includes(order.status) || p.started_at;
        tracking.hidden = !show;
        if (!show) return;
        tracking.append(el("h3", "", f.method === "delivery" ? "Delivery tracking" : "Pickup details"));
        if (f.pickup_code) {
            const code = el("div", "pickup-code");
            code.append(el("span", "", "Your pickup code"), el("strong", "", f.pickup_code), el("small", "", "Show this code at the shop. The order is released only to the customer or an authorized representative with this code."));
            tracking.append(code);
        }
        if (reprint?.status === "in_progress") {
            tracking.append(el("p", "notice", `The shop is reprinting ${reprint.quantity} pc(s) of your order: ${reprint.reason}. It goes through quality checking and packaging again before release.`));
        }
        if (order.status === "Awaiting Full Payment") {
            tracking.append(el("p", "notice", `Your order is packed. Pay the remaining balance of ${context.money(order.payment?.balance || 0)} below so the shop can release it for ${f.method === "delivery" ? "dispatch" : "pickup"}.`));
        }
        if (order.status === "Delivery Failed") {
            const last = (f.attempts || []).filter((item) => item.status === "failed").pop();
            tracking.append(el("p", "notice notice-error", `The last delivery attempt failed${last?.failure_label ? ` (${last.failure_label})` : ""}. The shop will contact you to reschedule; your order is not cancelled.`));
        }
        tracking.append(context.definitionList(rows));
        if (f.tracking_number) {
            const line = el("p", "tracking-number");
            line.append(el("span", "", `Tracking number: ${f.tracking_number} `), context.copyButton(f.tracking_number, "tracking number"));
            if (f.tracking_url) line.append(" ", link(`Track on the ${f.courier || "courier"} website`, f.tracking_url));
            else line.append(el("span", "field-hint", " Use this number on the courier's website or app to track the parcel."));
            tracking.append(line);
        }
        if (f.method === "delivery" && f.delivery_disclaimer) tracking.append(el("p", "field-hint", f.delivery_disclaimer));
        if (f.method === "pickup" && f.pickup_instructions) tracking.append(el("p", "field-hint", f.pickup_instructions));
        const attempts = f.attempts || [];
        if (attempts.length > 1) {
            const list = el("ol", "payment-submissions");
            attempts.forEach((item) => {
                const entry = el("li", `attempt-${item.status}`);
                entry.append(el("strong", "", `Attempt ${item.number}: ${{ delivered: "Delivered", failed: "Failed", out_for_delivery: "Out for delivery" }[item.status] || item.status}`));
                entry.append(el("span", "field-hint", [item.courier, item.tracking_number, item.failure_label].filter(Boolean).join(" · ")));
                list.append(entry);
            });
            tracking.append(el("h4", "", "Delivery attempts"), list);
        }
        const proofs = attempts.flatMap((item) => item.proof || []).filter(Boolean);
        proofs.forEach((proof) => tracking.append(link(`Proof of delivery: ${proof.name}`, proof.url)));
    }

    // ================================================================ requests

    function requestHistory(order) {
        const items = [];
        (order.cancellation_requests || []).forEach((item) => {
            const entry = el("li", `request-${item.status}`);
            entry.append(el("strong", "", `Cancellation ${item.id} · ${CANCEL_STATUS[item.status] || item.status}`));
            entry.append(el("span", "field-hint", `${item.reason_label} · requested ${context.formatDate(item.requested_at)}`));
            if (item.remarks_admin) entry.append(el("span", "field-hint", `Shop: ${item.remarks_admin}`));
            if (item.total_deductions) entry.append(el("span", "field-hint", `Deductions: ${context.money(item.total_deductions)} — ${item.deduction_justification}`));
            if (item.approved_refund !== undefined) entry.append(el("span", "field-hint", `Approved refund: ${context.money(item.approved_refund)}`));
            items.push(entry);
        });
        (order.refunds || []).forEach((item) => {
            const entry = el("li", `refund-${item.status}`);
            entry.append(el("strong", "", `Refund ${item.id} · ${item.status_label}`));
            entry.append(el("span", "field-hint", `${item.reason_label}${item.amount !== null && item.amount !== undefined ? ` · ${context.money(item.amount)}` : item.requested_amount ? ` · ${context.money(item.requested_amount)} requested` : ""}`));
            if (item.transaction_reference) entry.append(el("span", "field-hint", `Sent via ${item.method} · transaction ${item.transaction_reference} · ${context.formatDate(item.refunded_at)}`));
            if (item.remarks) entry.append(el("span", "field-hint", `Shop: ${item.remarks}`));
            if (item.failure_reason && item.status === "failed") entry.append(el("span", "field-hint", `Transfer failed: ${item.failure_reason}. The shop will retry or contact you.`));
            (item.proof || []).filter(Boolean).forEach((proof) => entry.append(link(`Refund receipt: ${proof.name}`, proof.url)));
            items.push(entry);
        });
        ((order.fulfillment || {}).issues || []).forEach((item) => {
            const entry = el("li", `issue-${item.status}`);
            entry.append(el("strong", "", `Delivery issue ${item.id} · ${item.status === "open" ? "Being reviewed" : "Resolved"}`));
            entry.append(el("span", "field-hint", `${item.type_label}: ${item.details}`));
            if (item.resolution) entry.append(el("span", "field-hint", `Shop: ${item.resolution}`));
            items.push(entry);
        });
        if (!items.length) return null;
        const list = el("ul", "payment-submissions");
        list.append(...items);
        return list;
    }

    function cancellationForm(order) {
        const form = el("form", "request-form");
        const reason = select(CANCEL_REASONS, "reason");
        const remarks = textarea("remarks", "Anything the shop should know (required for Other)");
        const paid = (order.payment?.amount_paid || 0) > 0;
        const hasPendingPayment = (order.payment_submissions || []).some((item) => item.status === "pending");
        const recipientRequired = paid || hasPendingPayment || (order.payments || []).length > 0;
        const recipient = recipientFields(recipientRequired);
        const acknowledged = input("acknowledged", { type: "checkbox", required: true });
        const ack = el("label", "check-row");
        ack.append(acknowledged, " I understand that the shop reviews this request against the work already done. A refund, if any, is decided in that review; deductions for documented costs may apply, and packed or dispatched orders are normally not cancelled for a change of mind.");
        const submit = el("button", "btn btn-dark", "Send cancellation request");
        submit.type = "submit";
        form.append(
            el("p", "field-hint", `Order ${order.reference} · ${order.status} · total ${context.money(order.total_amount)} · verified payments ${context.money(order.payment?.amount_paid || 0)}${order.payments?.length ? ` via ${[...new Set(order.payments.map((item) => item.method))].join(", ")}` : ""}.`),
            labelled("Reason", reason),
            labelled("Additional remarks", remarks),
            recipientRequired ? el("p", "field-hint", "Where should a refund be sent if one is approved? Provide your e-wallet details now so the shop can process any approved refund without delay.") : null,
            recipientRequired ? recipient.element : null,
            ack,
            submit,
        );
        reason.addEventListener("change", () => { remarks.required = reason.value === "other"; });
        return submitForm(form, "cancellation-request", async () => ({
            reason: reason.value, remarks: remarks.value.trim(), acknowledged: acknowledged.checked, ...(recipientRequired ? recipient.values() : {}),
        }), "Send this cancellation request? Your order is put on hold while the shop reviews it.");
    }

    function refundForm(order) {
        const received = RECEIVED.includes(order.status);
        const form = el("form", "request-form");
        const reason = select(REFUND_REASONS.filter(([, , needsReceipt]) => !needsReceipt || received), "reason");
        const details = textarea("details", "What happened? Include the duplicate or excess reference number, or describe the problem", true);
        const amount = input("amount", { type: "number", min: "0.01", step: "0.01", placeholder: "Optional" });
        const resolution = el("select", "input");
        resolution.append(new Option("Refund", "refund"), new Option("Reprint the order", "reprint"));
        const resolutionLabel = labelled("Preferred resolution", resolution);
        resolutionLabel.hidden = true;
        const recipient = recipientFields(true);
        const evidence = input("evidence", { type: "file", multiple: true, accept: ".jpg,.jpeg,.png,.webp,.pdf,image/*,application/pdf" });
        const submit = el("button", "btn btn-dark", "Send refund request");
        submit.type = "submit";
        const product = () => ["incorrect_product", "damaged_product"].includes(reason.value);
        const sync = () => {
            resolutionLabel.hidden = !product();
            if (!product()) resolution.value = "refund";
            const needsRecipient = resolution.value === "refund";
            recipient.element.hidden = !needsRecipient;
            recipient.element.querySelectorAll("input").forEach((field) => { field.required = needsRecipient; });
        };
        reason.addEventListener("change", sync);
        resolution.addEventListener("change", sync);
        form.append(
            el("p", "field-hint", "Refunds never exceed your verified payments. The shop reviews each request; for a wrong or damaged product you may ask for a reprint instead."),
            labelled("Reason", reason),
            labelled("Details", details),
            labelled("Amount to refund (PHP)", amount),
            resolutionLabel,
            recipient.element,
            labelled("Photos (optional, up to 4)", evidence),
            submit,
        );
        sync();
        return submitForm(form, "refund-request", async () => {
            const body = { reason: reason.value, details: details.value.trim(), preferred_resolution: resolution.value, evidence: await files(evidence, 4) };
            if (amount.value) body.amount = Number(amount.value);
            if (resolution.value === "refund") Object.assign(body, recipient.values());
            return body;
        }, "Send this refund request to the shop?");
    }

    function issueForm() {
        const form = el("form", "request-form");
        const type = select(ISSUE_TYPES, "type");
        const details = textarea("details", "Describe the problem", true);
        const submit = el("button", "btn btn-dark", "Report issue");
        submit.type = "submit";
        form.append(labelled("Issue", type), labelled("Details", details), submit);
        return submitForm(form, "delivery-issue", async () => ({ type: type.value, details: details.value.trim() }), "Report this delivery issue to the shop?");
    }

    function toggle(title, content) {
        const details = el("details", "request-toggle");
        details.append(el("summary", "", title), content);
        return details;
    }

    function renderRequests(order) {
        requests.replaceChildren();
        const actions = order.customer_actions || {};
        const history = requestHistory(order);
        const forms = [];
        if (context.canAct) {
            if (actions.request_cancellation) forms.push(toggle("Request cancellation", cancellationForm(order)));
            if (actions.request_refund) forms.push(toggle("Request a refund", refundForm(order)));
            if (actions.report_delivery_issue) forms.push(toggle("Report a delivery issue", issueForm()));
        }
        const pendingCancellation = (order.cancellation_requests || []).some((item) => item.status === "pending_review");
        const refundBlockedByCancellation = pendingCancellation && !actions.request_refund;
        const hasContent = Boolean(history) || forms.length > 0 || refundBlockedByCancellation;
        requests.hidden = !hasContent;
        const empty = document.querySelector("#requests-empty");
        if (empty) empty.hidden = hasContent;
        const requestsToggle = document.querySelector("#requests-toggle");
        if (requestsToggle) requestsToggle.classList.toggle("is-empty", !hasContent);
        if (!hasContent) return;
        if (history) requests.append(history);
        if (refundBlockedByCancellation) requests.append(el("p", "field-hint", "A cancellation request is pending review. Refund requests are unavailable until the cancellation request is decided."));
        requests.append(...forms);
    }

    // ================================================================ receipt

    function escapeHtml(value) {
        return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
    }

    function printReceipt() {
        const order = current;
        if (!order) return;
        const money = context.money;
        const payment = order.payment || {};
        const rows = (order.payments || []).map((item) => `<tr><td>${escapeHtml(context.formatDate(item.received_at))}</td><td>${escapeHtml(item.kind || "Payment")}</td><td>${escapeHtml(item.method)}</td><td>${escapeHtml(item.reference)}</td><td class="r">${escapeHtml(money(item.amount))}</td></tr>`).join("");
        const refunds = (order.refunds || []).filter((item) => item.status === "refunded").map((item) => `<tr><td>${escapeHtml(context.formatDate(item.refunded_at))}</td><td>Refund ${escapeHtml(item.id)}</td><td>${escapeHtml(item.method)}</td><td>${escapeHtml(item.transaction_reference)}</td><td class="r">−${escapeHtml(money(item.amount))}</td></tr>`).join("");
        const html = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Receipt ${escapeHtml(order.reference)}</title>
<style>body{font-family:system-ui,sans-serif;margin:32px;color:#111}h1{font-size:20px;margin:0}table{width:100%;border-collapse:collapse;margin-top:16px}th,td{border-bottom:1px solid #ddd;padding:6px 4px;text-align:left;font-size:13px}.r{text-align:right}.muted{color:#555;font-size:12px}dl{display:grid;grid-template-columns:200px 1fr;gap:4px 12px;font-size:13px}dt{color:#555}</style></head><body>
<h1>ImPRESSion PrintShop — Payment receipt</h1><p class="muted">Order ${escapeHtml(order.reference)} · printed ${escapeHtml(new Date().toLocaleString("en-PH"))}</p>
<dl><dt>Customer</dt><dd>${escapeHtml(order.customer?.name)}</dd><dt>Item</dt><dd>${escapeHtml(order.product?.name)} × ${escapeHtml(order.customization?.quantity)}</dd>
<dt>Fulfillment</dt><dd>${escapeHtml(order.fulfillment?.label)}</dd>${payment.delivery_fee ? `<dt>Delivery fee</dt><dd>${escapeHtml(money(payment.delivery_fee))}</dd>` : ""}
<dt>Order total</dt><dd>${escapeHtml(money(order.total_amount))}</dd><dt>Verified payments</dt><dd>${escapeHtml(money(payment.amount_paid))}</dd>
<dt>Remaining balance</dt><dd>${escapeHtml(money(payment.balance))}</dd><dt>Status</dt><dd>${escapeHtml(order.status)}</dd></dl>
<table><thead><tr><th>Date</th><th>Type</th><th>Method</th><th>Reference</th><th class="r">Amount</th></tr></thead><tbody>${rows}${refunds}</tbody></table>
<p class="muted">Only payments verified by the shop are listed. This receipt is generated from the shop's records.</p></body></html>`;
        const frame = document.createElement("iframe");
        frame.style.cssText = "position:fixed;width:0;height:0;border:0;right:0;bottom:0";
        document.body.append(frame);
        frame.contentDocument.open();
        frame.contentDocument.write(html);
        frame.contentDocument.close();
        frame.contentWindow.focus();
        frame.contentWindow.print();
        setTimeout(() => frame.remove(), 60000);
    }

    receiptButton?.addEventListener("click", printReceipt);

    function render(order, ctx) {
        current = order;
        context = ctx;
        renderTracking(order);
        renderRequests(order);
        const canReceipt = Boolean(order.customer_actions?.download_receipt);
        if (receiptButton) receiptButton.hidden = !canReceipt;
        const receiptHint = document.querySelector("#receipt-hint");
        if (receiptHint) {
            receiptHint.textContent = canReceipt
                ? "Download a record of verified payments for this order."
                : "A receipt is available once verified payments have been recorded for this order.";
        }
        const receiptToggle = document.querySelector("#receipt-toggle");
        if (receiptToggle) {
            const cancelVisible = !document.querySelector("#cancel-order")?.hidden;
            receiptToggle.classList.toggle("has-action", canReceipt || cancelVisible);
        }
    }

    window.PortalTracking = { render };
})();
