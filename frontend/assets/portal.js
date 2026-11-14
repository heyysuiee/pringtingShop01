"use strict";

(() => {
    const form = document.querySelector("#orders-lookup-form");
    const emailInput = document.querySelector("#lookup-email");
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
    const PAYMENT_METHODS = ["GCash", "Maya", "Bank transfer", "Other"];
    const MAX_PROOF_BYTES = 5 * 1024 * 1024;
    let activeEmail = "";
    let accessReference = "";
    let currentOrder = null;
    let detailSource = "lookup";

    const make = (tag, className, value) => {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (value !== undefined) element.textContent = value;
        return element;
    };

    function money(value) {
        return new Intl.NumberFormat("en-PH", { style: "currency", currency: "PHP" }).format(value);
    }

    function paymentSummary(order) {
        const chosen = order.payment?.payment_option;
        if (!chosen) {
            if (order.status === "Quotation Sent") return "Quotation ready · accept it to choose how to pay";
            return "Payment option chosen after the quotation";
        }
        const option = chosen === "cod" ? "COD" : "50% down payment";
        if (order.total_amount === null) return `${option} · not due yet, waiting for quotation`;
        const received = order.payments.reduce((total, payment) => total + payment.amount, 0);
        const state = received >= order.total_amount ? "Paid in full" : received > 0 ? `Partial · ${money(received)} received` : "Not received";
        const pending = order.payment?.pending_verification ? ` · ${money(order.payment.pending_verification)} being verified` : "";
        return `${option} · ${state}${pending}`;
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

    function renderOrders(orders) {
        list.replaceChildren();
        detail.hidden = true;
        list.hidden = false;
        if (!orders.length) {
            const empty = make("div", "card portal-empty");
            empty.append(make("span", "confirm-badge confirm-badge-muted", "i"), make("h2", "", "No orders found"), make("p", "", "No requests were found for that email address. Check the email and try again, or place a new order."));
            list.append(empty);
            return;
        }
        const heading = make("div", "orders-list-heading");
        heading.append(make("div", "", `${orders.length} ${orders.length === 1 ? "order" : "orders"} found`));
        list.append(heading);
        orders.forEach((order) => {
            const card = make("article", "card order-history-card");
            const main = make("div", "order-history-main");
            const kind = order.order_kind === "package"
                ? (order.package?.name ? `Package: ${order.package.name}` : "Ready-made package")
                : order.order_kind === "customize" ? "Customized order" : "";
            main.append(make("span", "service-tag", order.product.category), make("h2", "", order.product.name), make("p", "field-hint", [order.reference, `Submitted ${formatDate(order.created_at)}`, kind].filter(Boolean).join(" · ")));
            const meta = make("div", "order-history-meta");
            meta.append(make("span", "order-status-pill", order.status), make("span", "payment-status", paymentSummary(order)));
            if (order.total_amount !== null) {
                const received = order.payments.reduce((total, payment) => total + payment.amount, 0);
                meta.append(make("span", "field-hint", `Total ${money(order.total_amount)} · Balance ${money(Math.max(0, order.total_amount - received))}`));
            } else if (estimateOf(order) !== null) {
                meta.append(make("span", "field-hint", `Estimate ${money(estimateOf(order))} · awaiting confirmation`));
            } else {
                meta.append(make("span", "field-hint", "Waiting for Admin Quotation"));
            }
            const button = make("button", "btn btn-dark btn-small", "View details");
            button.type = "button";
            button.addEventListener("click", () => loadOrder(order.reference));
            card.append(main, meta, button);
            list.append(card);
        });
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

    /** The steps this order goes through: its pricing (package or quotation), payment option, and fulfillment. */
    function orderPath(order) {
        const option = order.payment?.payment_option;
        // Before the quotation is accepted the payment step is not known yet (down payment or, if allowed, COD).
        const payment = option === "cod"
            ? (order.fixed_price ? [] : ["Awaiting COD Confirmation"])
            : option ? ["Awaiting Down Payment"] : ["Payment Arrangement"];
        return [
            "Pending Confirmation",
            "Under Review",
            ...(order.fixed_price ? [] : ["Quotation Sent"]),
            ...payment,
            "Draft Preparation",
            "Awaiting Customer Approval",
            "Approved for Printing",
            "In Production",
            order.fulfillment?.release_status || "Ready for Pickup",
            "Completed",
        ];
    }

    function renderTimeline(order) {
        const timeline = document.querySelector("#detail-timeline");
        timeline.replaceChildren();
        const terminal = ["Rejected", "Cancelled"].includes(order.status);
        const ordered = terminal ? ["Pending Confirmation", order.status] : orderPath(order);
        // Side steps are shown in place of the step they belong to.
        const shownAs = { "Awaiting Quotation": "Under Review", "Quotation Declined": "Quotation Sent" }[order.status];
        if (shownAs && ordered.includes(shownAs)) ordered[ordered.indexOf(shownAs)] = order.status;
        const currentIndex = ordered.indexOf(order.status);
        ordered.forEach((status, index) => {
            const item = make("li", "timeline-step");
            if (index < currentIndex) item.classList.add("is-done");
            if (index === currentIndex) {
                item.classList.add("is-current");
                item.setAttribute("aria-current", "step");
            }
            const dot = make("span", "timeline-dot", index < currentIndex ? "✓" : String(index + 1));
            const label = make("span", "timeline-label", status);
            item.append(dot, label);
            timeline.append(item);
        });
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

    /** Quotation waiting for the customer: accept it (choosing how to pay) or decline it. */
    function renderQuotationPanel(order) {
        quotationPanel.replaceChildren();
        const quote = order.quotation;
        const open = order.status === "Quotation Sent" && quote?.status === "sent";
        const declined = order.status === "Quotation Declined";
        quotationPanel.hidden = !open && !declined;
        if (declined) {
            quotationPanel.append(
                make("h3", "", "You declined the quotation"),
                make("p", "field-hint", `${quote?.decline_reason ? `Your reason: “${quote.decline_reason}”. ` : ""}The shop may send a revised quotation; it will appear here.`),
            );
            return;
        }
        if (!open) return;
        const total = order.total_amount;
        const down = Math.round(total * 50) / 100;
        quotationPanel.append(
            make("h3", "", "Your quotation is ready"),
            make("p", "field-hint", "Review the price from the shop. Accept it and choose how to pay, or decline it. Nothing is charged when you accept."),
            definitionList([
                ["Quoted total", money(total)],
                ["50% down payment", `${money(down)} (if you choose the down payment)`],
                ["Balance after down payment", money(total - down)],
                ["Cash on Delivery", quote.cod_allowed ? "Allowed for this order" : "Not available for this order"],
                ["Message from the shop", quote.note],
                ["Payment instructions", order.payment_instructions],
            ]),
        );
        const options = make("fieldset", "payment-options");
        options.append(make("legend", "field-label", "How will you pay?"));
        const grid = make("div", "payment-option-grid");
        [
            ["down_payment", "50% Down Payment", `Pay ${money(down)} now; the draft is prepared once the shop verifies it. Pay ${money(total - down)} before release.`, true],
            ["cod", "Cash on Delivery (COD)", quote.cod_allowed ? `No down payment. Pay ${money(total)} on delivery or pickup, after the shop confirms the arrangement.` : "The shop has not allowed COD for this order.", quote.cod_allowed],
        ].forEach(([value, title, description, enabled], index) => {
            const card = make("label", `payment-option-card${enabled ? "" : " is-disabled"}`);
            const input = make("input");
            input.type = "radio";
            input.name = "quote-payment-option";
            input.value = value;
            input.disabled = !enabled;
            input.checked = index === 0;
            const body = make("span", "payment-option-body");
            body.append(make("strong", "", title), make("small", "", description));
            card.append(input, body);
            grid.append(card);
        });
        options.append(grid);
        const reason = make("textarea", "input");
        reason.rows = 2;
        reason.maxLength = 500;
        reason.placeholder = "Optional: tell the shop why (for example, over budget)";
        reason.setAttribute("aria-label", "Reason for declining (optional)");
        const accept = make("button", "btn btn-dark", "Accept quotation");
        accept.type = "button";
        const decline = make("button", "btn btn-ghost-dark", "Decline quotation");
        decline.type = "button";
        accept.addEventListener("click", () => {
            const option = quotationPanel.querySelector('input[name="quote-payment-option"]:checked')?.value || "down_payment";
            const label = option === "cod" ? "Cash on Delivery" : "the 50% down payment";
            if (!window.confirm(`Accept the quotation of ${money(total)} and pay with ${label}?`)) return;
            void orderAction("quotation/accept", { payment_option: option, quotation_revision: quote.revision }, "The quotation could not be accepted.", accept);
        });
        decline.addEventListener("click", () => {
            if (!window.confirm("Decline this quotation? The shop may send a revised one.")) return;
            void orderAction("quotation/decline", { reason: reason.value.trim(), quotation_revision: quote.revision }, "The quotation could not be declined.", decline);
        });
        const actions = make("div", "draft-actions");
        actions.append(accept, decline);
        quotationPanel.append(options, reason, actions);
    }

    /** Payment status, instructions, and (for down-payment orders) the form to report a payment with proof. */
    function renderPaymentPanel(order) {
        paymentPanel.replaceChildren();
        const payment = order.payment || {};
        const closed = ["Rejected", "Cancelled"].includes(order.status);
        const submissionsList = order.payment_submissions || [];
        const show = !closed && order.total_amount !== null && payment.payment_option_chosen;
        paymentPanel.hidden = !show && !submissionsList.length;
        if (paymentPanel.hidden) return;
        const cod = payment.payment_option === "cod";
        paymentPanel.append(make("h3", "", "Payment"));
        paymentPanel.append(definitionList([
            [order.fixed_price ? "Package total" : "Accepted quotation", money(order.total_amount)],
            ["Payment option", payment.payment_option_label],
            cod ? ["Down payment", "Not required (Cash on Delivery)"] : ["50% down payment", `${money(payment.down_payment_required)} · ${payment.down_payment_status}`],
            ["Verified payments", money(payment.amount_paid)],
            payment.pending_verification ? ["Waiting for verification", money(payment.pending_verification)] : ["", ""],
            [cod ? "Due on delivery / pickup" : "Remaining balance", money(payment.balance)],
        ]));
        if (order.payment_instructions) {
            const instructions = make("div", "payment-instructions");
            instructions.append(make("strong", "", "Payment instructions from the shop"), make("p", "", order.payment_instructions));
            paymentPanel.append(instructions);
        }
        const awaitingConfirmation = ["Pending Confirmation", "Under Review"].includes(order.status);
        const pending = submissionsList.find((item) => item.status === "pending");
        const payable = ["Awaiting Down Payment", "Draft Preparation", "Awaiting Customer Approval", "Approved for Printing", "In Production", "Ready for Pickup", "Out for Delivery"].includes(order.status);
        if (closed) {
            // Only the submission history below.
        } else if (cod) {
            paymentPanel.append(make("p", "notice", order.status === "Awaiting COD Confirmation"
                ? "You chose Cash on Delivery. The shop will confirm the arrangement before preparing your draft."
                : `Cash on Delivery: pay ${money(payment.balance)} when your order is ${order.fulfillment?.method === "delivery" ? "delivered" : "picked up"}. No down-payment receipt is needed.`));
        } else if (awaitingConfirmation) {
            paymentPanel.append(make("p", "notice", "Payment opens after the shop confirms your order and shares the payment instructions."));
        } else if (pending) {
            paymentPanel.append(make("p", "notice", `Your payment of ${money(pending.amount)} (reference ${pending.reference}) is waiting for the shop to verify it.`));
        } else if (payable && payment.balance > 0.005) {
            paymentPanel.append(paymentForm(order));
        }
        if (submissionsList.length) {
            const list = make("ul", "payment-submissions");
            submissionsList.slice().reverse().forEach((item) => {
                const entry = make("li", `submission-${item.status}`);
                const label = { pending: "Waiting for verification", verified: "Verified", rejected: "Not verified" }[item.status] || item.status;
                entry.append(make("strong", "", `${money(item.amount)} · ${item.method} · ${item.reference}`), make("span", "field-hint", `${label} · submitted ${formatDate(item.submitted_at)}`));
                if (item.review_note) entry.append(make("span", "field-hint", `Shop note: ${item.review_note}`));
                if (item.proof) {
                    const link = make("a", "", `Proof: ${item.proof.name}`);
                    link.href = item.proof.url;
                    link.target = "_blank";
                    link.rel = "noopener";
                    entry.append(link);
                }
                list.append(entry);
            });
            paymentPanel.append(make("h4", "", "Payments you submitted"), list);
        }
    }

    function paymentForm(order) {
        const payment = order.payment;
        const suggested = payment.down_payment_remaining > 0.005 ? payment.down_payment_remaining : payment.balance;
        const formElement = make("form", "payment-submit-form");
        formElement.noValidate = false;
        const field = (labelText, control) => {
            const label = make("label", "", labelText);
            label.append(control);
            return label;
        };
        const amount = make("input", "input");
        Object.assign(amount, { type: "number", min: "0.01", step: "0.01", max: String(payment.balance), required: true, value: suggested.toFixed(2) });
        const method = make("select", "input");
        PAYMENT_METHODS.forEach((value) => method.append(new Option(value, value)));
        const reference = make("input", "input");
        Object.assign(reference, { maxLength: 120, required: true, placeholder: "e.g. GCash reference number" });
        const proof = make("input", "input");
        Object.assign(proof, { type: "file", accept: ".jpg,.jpeg,.png,.webp,.pdf,image/jpeg,image/png,image/webp,application/pdf" });
        const submit = make("button", "btn btn-dark", "Submit payment for verification");
        submit.type = "submit";
        formElement.append(
            make("h4", "", payment.down_payment_remaining > 0.005 ? "Report your down payment" : "Report a balance payment"),
            make("p", "field-hint", `Send the payment using the shop's instructions, then enter its details. The shop verifies it before it counts as paid. Suggested amount: ${money(suggested)}.`),
            field("Amount paid (PHP)", amount),
            field("Payment method", method),
            field("Payment reference number", reference),
            field("Proof of payment (optional: screenshot or receipt, JPG/PNG/WebP/PDF, max 5 MB)", proof),
            submit,
        );
        formElement.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!formElement.reportValidity()) return;
            const file = proof.files?.[0];
            if (file && (file.size > MAX_PROOF_BYTES || !/\.(jpe?g|png|webp|pdf)$/i.test(file.name))) {
                detailError.textContent = "The proof must be a JPG, PNG, WebP, or PDF file no larger than 5 MB.";
                return;
            }
            const value = Number(amount.value);
            if (!window.confirm(`Submit a payment of ${money(value)} (reference ${reference.value.trim()}) for verification?`)) return;
            let proofPayload = null;
            try {
                if (file) proofPayload = { name: file.name, dataUrl: await fileAsDataUrl(file) };
            } catch (issue) {
                detailError.textContent = issue instanceof Error ? issue.message : "The proof file could not be read.";
                return;
            }
            await orderAction("payments", { amount: value, method: method.value, reference: reference.value.trim(), proof: proofPayload }, "The payment could not be submitted.", submit);
        });
        return formElement;
    }

    function renderDetail(order) {
        currentOrder = order;
        detailError.textContent = "";
        list.hidden = true;
        detail.hidden = false;
        document.querySelector("#detail-service").textContent = order.product.category;
        document.querySelector("#detail-item").textContent = order.product.name;
        document.querySelector("#detail-meta").textContent = `Submitted ${formatDate(order.created_at)} · Quantity ${order.customization.quantity}`;
        document.querySelector("#detail-ref").textContent = order.reference;
        document.querySelector("#detail-status").textContent = order.status;
        renderTimeline(order);

        const summary = document.querySelector("#detail-summary");
        summary.replaceChildren();
        if (order.order_kind) appendSummaryRow(summary, "Order type", order.order_kind === "package" ? `Package${order.package?.name ? `: ${order.package.name}` : ""}` : "Customized order");
        appendSummaryRow(summary, "Quantity", `${order.customization.quantity} pcs`);
        if (typeof order.pricing?.unit_price === "number") appendSummaryRow(summary, "Price per piece", `${money(order.pricing.unit_price)} (estimate)`);
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
        appendSummaryRow(summary, order.fixed_price ? "Package total" : "Confirmed total", order.total_amount === null
            ? "Waiting for Admin Quotation"
            : `${money(order.total_amount)}${quoteWaiting ? " (quotation, not yet accepted)" : ""}`);
        const payment = order.payment || {};
        const cod = payment.payment_option === "cod";
        appendSummaryRow(summary, "Payment option", payment.payment_option_label || "50% Down Payment");
        if (payment.payment_option_chosen) {
            if (cod) {
                appendSummaryRow(summary, "Down payment", "Not required (Cash on Delivery)");
            } else if (payment.down_payment_required !== null && payment.down_payment_required !== undefined) {
                appendSummaryRow(summary, "50% down payment", `${money(payment.down_payment_required)} · ${payment.down_payment_status}`);
            }
        }
        if (order.total_amount !== null && payment.payment_option_chosen) {
            appendSummaryRow(summary, "Payment received (verified)", money(payment.amount_paid));
            if (payment.pending_verification) appendSummaryRow(summary, "Waiting for verification", money(payment.pending_verification));
            appendSummaryRow(summary, cod ? "Due on delivery / pickup" : "Remaining balance", money(payment.balance));
        }
        const fulfillment = order.fulfillment || {};
        appendSummaryRow(summary, "Delivery or pickup", fulfillment.label || "Store Pickup");
        (fulfillment.address || []).forEach((row) => appendSummaryRow(summary, row.label, row.value));
        if (order.mockup && window.InvitationMockup) {
            const wrapper = make("div", "summary-row");
            const holder = make("dd", "portal-mockup");
            holder.append(window.InvitationMockup.render(order.mockup));
            wrapper.append(make("dt", "", `Invitation mockup · ${order.mockup.template_name}`), holder);
            summary.append(wrapper);
        }
        renderAttachments(summary, order.attachments, "Uploaded files");

        const draft = document.querySelector("#customer-draft");
        const awaitingApproval = order.status === "Awaiting Customer Approval" && order.draft_attachment;
        draft.hidden = !awaitingApproval;
        if (awaitingApproval) {
            const download = document.querySelector("#draft-download");
            download.href = order.draft_attachment.url;
            download.textContent = `Open draft: ${order.draft_attachment.name}`;
        }
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
        const hasPendingPayment = (order.payment_submissions || []).some((item) => item.status === "pending");
        const canCancel = pendingStatuses.includes(order.status) && order.payments.length === 0 && !hasPendingPayment;
        const cancelButton = document.querySelector("#cancel-order");
        cancelButton.hidden = !canCancel;
        cancelButton.disabled = false;
        detail.scrollIntoView({ block: "start", behavior: "instant" });
    }

    async function loadOrder(reference, source = "lookup") {
        error.textContent = "";
        try {
            // Signed-in customers are verified by their session; lookup visitors prove access with email + reference.
            const url = source === "account"
                ? `/api/account/orders/${encodeURIComponent(reference)}`
                : `/api/orders/${encodeURIComponent(reference)}?email=${encodeURIComponent(activeEmail)}`;
            const order = await request(url);
            if (source === "account") activeEmail = order.customer.email;
            detailSource = source;
            if (source === "account") {
                accountSection.hidden = true;
                lookupPanel.hidden = true;
            }
            renderDetail(order);
        } catch (issue) {
            if (source === "account" && issue.status === 401) {
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
        void loadOrder(reference, "account");
    });

    async function loadOrders(email, reference) {
        error.textContent = "";
        detailSource = "lookup";
        activeEmail = email.trim().toLowerCase();
        accessReference = reference.trim();
        const result = await request(`/api/orders?email=${encodeURIComponent(activeEmail)}&reference=${encodeURIComponent(accessReference)}`);
        renderOrders(result.orders);
        return result.orders;
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!form.reportValidity()) return;
        try {
            const reference = referenceInput.value.trim();
            const orders = await loadOrders(emailInput.value, reference);
            if (orders.some((order) => order.reference === reference)) await loadOrder(reference);
            else list.scrollIntoView({ block: "start", behavior: "instant" });
        } catch (issue) {
            error.textContent = issue instanceof Error ? issue.message : "Your order history could not be loaded.";
        }
    });

    document.querySelector("#back-to-orders").addEventListener("click", async () => {
        if (detailSource === "account") {
            detail.hidden = true;
            accountSection.hidden = false;
            accountPanel.hidden = false;
            lookupPanel.hidden = false;
            list.hidden = true;
            error.textContent = "";
            // The order may have changed while it was open (approved, cancelled): reload the account's list.
            document.dispatchEvent(new CustomEvent("customer-orders-changed"));
            accountPanel.scrollIntoView({ block: "start", behavior: "instant" });
            return;
        }
        try {
            await loadOrders(activeEmail, accessReference);
            list.scrollIntoView({ block: "start", behavior: "instant" });
        } catch (issue) {
            error.textContent = issue instanceof Error ? issue.message : "Your orders could not be loaded.";
        }
    });

    document.querySelector("#approve-draft").addEventListener("click", (event) => {
        if (!currentOrder || !window.confirm("Approve this draft? The administrator may then move the order to printing.")) return;
        void orderAction("approve", {}, "The draft approval could not be recorded.", event.currentTarget);
    });

    document.querySelector("#request-draft-changes").addEventListener("click", (event) => {
        if (!currentOrder || !window.confirm("Ask the shop to revise this draft?")) return;
        void orderAction("request-changes", { note: "Customer requested changes to the draft layout." }, "The change request could not be recorded.", event.currentTarget);
    });

    document.querySelector("#cancel-order").addEventListener("click", (event) => {
        if (!currentOrder || !window.confirm("Cancel this request? This cannot be undone.")) return;
        void orderAction("cancel", {}, "The request could not be cancelled.", event.currentTarget);
    });

    const parameters = new URLSearchParams(window.location.search);
    if (parameters.has("email")) {
        emailInput.value = parameters.get("email");
        referenceInput.value = parameters.get("ref") || "";
        form.requestSubmit();
    }
})();
