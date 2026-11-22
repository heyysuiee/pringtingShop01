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
    const MAX_PROOF_BYTES = 5 * 1024 * 1024;
    let activeEmail = "";
    let currentOrder = null;
    // Every order is opened through the signed-in account (the server checks that the account owns it).
    let detailSource = "account";
    // Changing an order (accepting a quotation, paying, approving a draft) needs the signed-in account.
    let signedIn = false;
    fetch("/api/customer/session", { credentials: "same-origin" })
        .then((response) => { signedIn = response.ok; })
        .catch(() => { signedIn = false; });

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

    /** POST a customer action (approve / request-changes / cancel) for the open order and show the result. */
    async function orderAction(action, body, failure, button) {
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
        const waitForBalance = order.status === "Awaiting Full Payment" || reached.has("Awaiting Full Payment") || (!packed && (order.payment?.balance || 0) > 0.005);
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
            make("p", "field-hint", "Review the latest quotation from the shop. If you accept it, the next step is the 50% down payment through one of the shop's e-wallets. If you decline it, nothing is paid and the shop may send a revised quotation."),
            definitionList([
                quote.delivery_fee ? ["Printing", money(quote.amount)] : ["", ""],
                quote.delivery_fee ? ["Delivery fee", money(quote.delivery_fee)] : ["", ""],
                ["Quoted total", money(total)],
                revised && typeof quote.previous_amount === "number" ? ["Previous quotation", money(quote.previous_amount)] : ["", ""],
                ["50% down payment", money(payment.down_payment_required)],
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
        accept.addEventListener("click", async () => {
            if (!(await window.PrintConfirm(`Accept the quotation of ${money(total)}? You will then pay the 50% down payment of ${money(payment.down_payment_required)} through the shop's e-wallet.`, { title: "Accept quotation?", confirmLabel: "Accept quotation" }))) return;
            // The revision you reviewed is sent along: if the shop revised the quotation meanwhile, acceptance is refused.
            void orderAction("quotation/accept", { quotation_revision: quote.revision }, "The quotation could not be accepted.", accept);
        });
        decline.addEventListener("click", async () => {
            if (!(await window.PrintConfirm("Decline this quotation? Nothing will be charged; the shop may send a revised one.", { title: "Decline quotation?", confirmLabel: "Decline quotation", tone: "danger" }))) return;
            void orderAction("quotation/decline", { reason: reason.value.trim(), quotation_revision: quote.revision }, "The quotation could not be declined.", decline);
        });
        const actions = make("div", "draft-actions");
        actions.append(accept, decline);
        quotationPanel.append(reason, actions);
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
        paymentPanel.append(make("h3", "", "Payment"));
        paymentPanel.append(definitionList([
            payment.delivery_fee ? ["Printing / package subtotal", money(payment.subtotal)] : ["", ""],
            payment.delivery_fee ? ["Delivery fee", money(payment.delivery_fee)] : ["", ""],
            [order.fixed_price ? (payment.delivery_fee ? "Order total" : (order.fixed_price_label || "Package total")) : "Accepted quotation", money(order.total_amount)],
            cod ? ["Down payment", "Not required (Cash on Delivery, arranged before the 50% down payment rule)"] : ["50% down payment", `${money(payment.down_payment_required)} · ${payment.down_payment_status}`],
            ["Verified payments", money(payment.amount_paid)],
            payment.pending_verification ? ["Pending verification", money(payment.pending_verification)] : ["", ""],
            [cod ? "Due on delivery / pickup" : "Remaining balance", money(payment.balance)],
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
        const pending = submissionsList.find((item) => item.status === "pending");
        const due = payment.next_payment;
        if (closed) {
            // Only the submission history below.
        } else if (cod) {
            paymentPanel.append(make("p", "notice", `Cash on Delivery: pay ${money(payment.balance)} when your order is ${order.fulfillment?.method === "delivery" ? "delivered" : "picked up"}.`));
        } else if (!agreed) {
            paymentPanel.append(make("p", "notice", "Payment opens after the shop approves your order and shares the payment instructions."));
        } else if (pending) {
            paymentPanel.append(make("p", "notice", `Your ${pending.method} payment of ${money(pending.amount)} (reference ${pending.reference}) is pending verification. ${order.status === "Awaiting Down Payment" ? "Your order is confirmed only after the shop verifies it." : "The shop will update your balance once it is verified."}`));
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
                if (item.review_note) entry.append(make("span", "field-hint", `Shop note: ${item.review_note}`));
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
    function paymentMethods() {
        methodsRequest ||= request("/api/payment-methods").then((result) => result.methods || []).catch(() => []);
        return methodsRequest;
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

    /** E-wallet payment form: choose one of the shop's active methods, pay outside the site, then report the
     *  reference number and screenshot. The amount comes from the server and cannot be edited. */
    function paymentForm(order, due) {
        const formElement = make("form", "payment-submit-form");
        const cards = make("div", "wallet-cards");
        cards.setAttribute("role", "radiogroup");
        cards.setAttribute("aria-label", "Payment method");
        const details = make("div", "wallet-details");
        let chosen = null;
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
            row("Exact amount due", money(due.amount), due.amount.toFixed(2));
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
            proofLabel.firstChild.textContent = `${method.provider} screenshot or receipt (JPG, PNG, WebP, or PDF · max 5 MB)`;
        };
        paymentMethods().then((methods) => {
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
                card.append(make("strong", "", method.provider), make("span", "field-hint", method.account_name || "E-wallet"));
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
        Object.assign(amount, { type: "text", readOnly: true, value: money(due.amount) });
        amount.setAttribute("aria-readonly", "true");
        const reference = make("input", "input");
        Object.assign(reference, { maxLength: 120, minLength: 4, required: true, placeholder: "Reference number", autocomplete: "off" });
        const proof = make("input", "input");
        Object.assign(proof, { type: "file", required: true, accept: ".jpg,.jpeg,.png,.webp,.pdf,image/jpeg,image/png,image/webp,application/pdf" });
        const submit = make("button", "btn btn-dark", "Submit payment for verification");
        submit.type = "submit";
        const down = due.stage === "down_payment";
        const referenceLabel = field("Reference number", reference);
        const proofLabel = field("Screenshot or receipt (JPG, PNG, WebP, or PDF · max 5 MB)", proof);
        formElement.append(
            make("h4", "", down ? "Pay your 50% down payment" : "Pay the remaining balance"),
            make("p", "field-hint", `Choose an e-wallet, send exactly ${money(due.amount)} to the shop's account, then enter the reference number and upload the screenshot or receipt. The amount is set by the shop. Your payment stays Pending Verification until the shop checks the money in its account${down ? "; your order is confirmed once it is verified" : ""}. The shop never asks for your PIN or OTP.`),
            cards,
            details,
            field(`Amount to pay (${due.label})`, amount),
            referenceLabel,
            proofLabel,
            submit,
        );
        formElement.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!formElement.reportValidity()) return;
            const file = proof.files?.[0];
            if (!file || file.size > MAX_PROOF_BYTES || !/\.(jpe?g|png|webp|pdf)$/i.test(file.name)) {
                detailError.textContent = "Upload the payment screenshot or receipt as a JPG, PNG, WebP, or PDF file no larger than 5 MB.";
                return;
            }
            if (!chosen) {
                detailError.textContent = "Choose the e-wallet you paid with.";
                return;
            }
            if (!(await window.PrintConfirm(`Submit your ${chosen.provider} payment of ${money(due.amount)} (reference ${reference.value.trim()}) for verification?`, { title: "Submit payment?", confirmLabel: "Submit payment" }))) return;
            let proofPayload;
            try {
                proofPayload = { name: file.name, dataUrl: await fileAsDataUrl(file) };
            } catch (issue) {
                detailError.textContent = issue instanceof Error ? issue.message : "The receipt could not be read.";
                return;
            }
            // The amount is sent only so a stale page is refused; the server decides the amount due.
            await orderAction("payments", { amount: due.amount, method: chosen.provider, reference: reference.value.trim(), proof: proofPayload }, "The payment could not be submitted.", submit);
        });
        return formElement;
    }

    function renderDetail(order) {
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
            appendSummaryRow(summary, "50% down payment", `${money(payment.down_payment_required)} · ${payment.down_payment_status}`);
        }
        if (order.total_amount !== null && payment.payment_option_chosen && !quoteWaiting) {
            appendSummaryRow(summary, "Payment received (verified)", money(payment.amount_paid));
            if (payment.pending_verification) appendSummaryRow(summary, "Waiting for verification", money(payment.pending_verification));
            appendSummaryRow(summary, cod ? "Due on delivery / pickup" : "Remaining balance", money(payment.balance));
        }
        const fulfillment = order.fulfillment || {};
        appendSummaryRow(summary, "Delivery or pickup", fulfillment.label || "Store Pickup");
        (fulfillment.address || []).forEach((row) => appendSummaryRow(summary, row.label, row.value));
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
        renderPaymentPanel(order);
        const pendingStatuses = ["Pending Confirmation", "Under Review", "Awaiting Quotation", "Quotation Sent", "Quotation Declined", "Awaiting COD Confirmation", "Awaiting Down Payment"];
        // Confirmed orders have a verified payment, so they are never cancellable here.
        const hasPendingPayment = (order.payment_submissions || []).some((item) => item.status === "pending");
        const canCancel = pendingStatuses.includes(order.status) && order.payments.length === 0 && !hasPendingPayment;
        const cancelButton = document.querySelector("#cancel-order");
        cancelButton.hidden = !canCancel;
        cancelButton.disabled = false;
        // Tracking (production, pickup, courier), cancellation and refund requests, and the receipt.
        window.PortalTracking?.render(order, {
            orderAction, money, formatDate, make, definitionList, fileAsDataUrl, paymentMethods, copyButton, detailError,
            canAct: detailSource === "account" || signedIn,
        });
        detail.scrollIntoView({ block: "start", behavior: "instant" });
    }

    async function loadOrder(reference) {
        error.textContent = "";
        try {
            // Verified by the session: the server only returns orders owned by the signed-in account.
            const order = await request(`/api/account/orders/${encodeURIComponent(reference)}`);
            activeEmail = order.customer.email;
            detailSource = "account";
            accountSection.hidden = true;
            lookupPanel.hidden = true;
            renderDetail(order);
        } catch (issue) {
            if (issue.status === 401) {
                // The sign-in ended (expired, or the password was reset on another device).
                document.dispatchEvent(new CustomEvent("customer-session-ended"));
                return;
            }
            error.textContent = issue instanceof Error ? issue.message : "The order could not be loaded.";
        }
    }

    document.addEventListener("customer-order-select", (event) => {
        const { reference } = event.detail || {};
        if (typeof reference !== "string" || !reference) return;
        void loadOrder(reference);
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!form.reportValidity()) return;
        await loadOrder(referenceInput.value.trim());
    });

    document.querySelector("#back-to-orders").addEventListener("click", () => {
        detail.hidden = true;
        accountSection.hidden = false;
        accountPanel.hidden = false;
        lookupPanel.hidden = false;
        list.hidden = true;
        error.textContent = "";
        // The order may have changed while it was open (approved, cancelled): reload the account's list.
        document.dispatchEvent(new CustomEvent("customer-orders-changed"));
        accountPanel.scrollIntoView({ block: "start", behavior: "instant" });
    });

    document.querySelector("#approve-draft").addEventListener("click", async (event) => {
        const button = event.currentTarget;
        if (!currentOrder || !(await window.PrintConfirm("Approve this draft? The administrator may then move the order to printing.", { title: "Approve draft?", confirmLabel: "Approve draft" }))) return;
        void orderAction("approve", {}, "The draft approval could not be recorded.", button);
    });

    document.querySelector("#request-draft-changes").addEventListener("click", async (event) => {
        const button = event.currentTarget;
        const noteInput = document.querySelector("#draft-revision-note");
        const note = noteInput.value.trim();
        if (!note) {
            detailError.textContent = "Describe the changes you want in the revision instructions.";
            noteInput.focus();
            return;
        }
        if (!currentOrder || !(await window.PrintConfirm("Ask the shop to revise this draft with your instructions?", { title: "Request revision?", confirmLabel: "Request revision" }))) return;
        void orderAction("request-changes", { note }, "The revision request could not be recorded.", button);
    });

    document.querySelector("#cancel-order").addEventListener("click", async (event) => {
        const button = event.currentTarget;
        if (!currentOrder || !(await window.PrintConfirm("Cancel this request? This cannot be undone.", { title: "Cancel request?", confirmLabel: "Cancel request", tone: "danger" }))) return;
        void orderAction("cancel", {}, "The request could not be cancelled.", button);
    });

    // Old links (/customer-portal?email=…&ref=…) only prefill the reference; opening it still needs sign-in.
    const parameters = new URLSearchParams(window.location.search);
    if (parameters.get("ref")) referenceInput.value = parameters.get("ref");
})();
