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
    const statuses = [
        "Pending Confirmation",
        "Under Review",
        "Awaiting Quotation",
        "Awaiting Down Payment",
        "Draft Preparation",
        "Awaiting Customer Approval",
        "Approved for Printing",
        "In Production",
        "Ready for Pickup",
        "Completed",
        "Rejected",
        "Cancelled",
    ];
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
        if (order.total_amount === null) return "Payment: Not due · Waiting for quotation";
        const received = order.payments.reduce((total, payment) => total + payment.amount, 0);
        return received >= order.total_amount ? "Paid in full" : received > 0 ? `Partial · ${money(received)} received` : "Not received";
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
                : order.order_kind === "customize" ? "Customized invitation" : "";
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

    function renderTimeline(order) {
        const timeline = document.querySelector("#detail-timeline");
        timeline.replaceChildren();
        const terminal = ["Rejected", "Cancelled"].includes(order.status);
        const ordered = terminal ? ["Pending Confirmation", order.status] : statuses.slice(0, 10);
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
        if (order.order_kind) appendSummaryRow(summary, "Order type", order.order_kind === "package" ? "Ready-made package" : "Customized invitation");
        appendSummaryRow(summary, "Quantity", String(order.customization.quantity));
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
        appendSummaryRow(summary, "Confirmed total", order.total_amount === null ? "Waiting for Admin Quotation" : money(order.total_amount));
        if (order.total_amount !== null) {
            const received = order.payments.reduce((total, payment) => total + payment.amount, 0);
            appendSummaryRow(summary, "50% down payment", money(order.total_amount * 0.5));
            appendSummaryRow(summary, "Payment received", money(received));
            appendSummaryRow(summary, "Remaining balance", money(Math.max(0, order.total_amount - received)));
        }
        if (order.payment_instructions) appendSummaryRow(summary, "Payment instructions", order.payment_instructions);
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
        const canCancel = ["Pending Confirmation", "Under Review", "Awaiting Quotation", "Awaiting Down Payment"].includes(order.status) && order.payments.length === 0;
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
