"use strict";

(() => {
    const form = document.querySelector("#orders-lookup-form");
    const emailInput = document.querySelector("#lookup-email");
    const referenceInput = document.querySelector("#lookup-ref");
    const error = document.querySelector("#lookup-error");
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
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "The order information could not be loaded.");
        return result;
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
            main.append(make("span", "service-tag", order.product.category), make("h2", "", order.product.name), make("p", "field-hint", `${order.reference} · Submitted ${formatDate(order.created_at)}`));
            const meta = make("div", "order-history-meta");
            meta.append(make("span", "order-status-pill", order.status), make("span", "payment-status", paymentSummary(order)));
            if (order.total_amount !== null) {
                const received = order.payments.reduce((total, payment) => total + payment.amount, 0);
                meta.append(make("span", "field-hint", `Total ${money(order.total_amount)} · Balance ${money(Math.max(0, order.total_amount - received))}`));
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
            const link = make("a", "", `${file.name} · ${(file.size / 1024).toFixed(0)} KB`);
            link.href = file.url;
            link.target = "_blank";
            link.rel = "noopener";
            container.append(link);
        });
        target.append(container);
    }

    function renderDetail(order) {
        currentOrder = order;
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
        appendSummaryRow(summary, "Size / dimensions", order.customization.size);
        appendSummaryRow(summary, "Quantity", String(order.customization.quantity));
        appendSummaryRow(summary, "Material / variant", order.customization.material);
        appendSummaryRow(summary, "Customization", order.customization.details);
        appendSummaryRow(summary, "Special instructions", order.customization.instructions);
        appendSummaryRow(summary, "Customer", order.customer.name);
        appendSummaryRow(summary, "Email", order.customer.email);
        appendSummaryRow(summary, "Contact number", order.customer.phone);
        appendSummaryRow(summary, "Messenger name", order.customer.messenger);
        if (order.has_event) {
            appendSummaryRow(summary, "Celebrant", order.event_details.celebrant);
            appendSummaryRow(summary, "Event type", order.event_details.event_type);
            appendSummaryRow(summary, "Event date", order.event_details.event_date);
            appendSummaryRow(summary, "Event time", order.event_details.event_time);
            appendSummaryRow(summary, "Event location", order.event_details.event_location);
        }
        appendSummaryRow(summary, "Confirmed total", order.total_amount === null ? "Waiting for Admin Quotation" : money(order.total_amount));
        if (order.total_amount !== null) {
            const received = order.payments.reduce((total, payment) => total + payment.amount, 0);
            appendSummaryRow(summary, "50% down payment", money(order.total_amount * 0.5));
            appendSummaryRow(summary, "Payment received", money(received));
            appendSummaryRow(summary, "Remaining balance", money(Math.max(0, order.total_amount - received)));
        }
        if (order.payment_instructions) appendSummaryRow(summary, "Payment instructions", order.payment_instructions);
        renderAttachments(summary, order.attachments, "Reference files");

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
        document.querySelector("#cancel-order").hidden = !canCancel;
        detail.scrollIntoView({ block: "start", behavior: "instant" });
    }

    async function loadOrder(reference, source = "lookup") {
        error.textContent = "";
        try {
            const order = await request(`/api/orders/${encodeURIComponent(reference)}?email=${encodeURIComponent(activeEmail)}`);
            detailSource = source;
            if (source === "account") {
                accountSection.hidden = true;
                lookupPanel.hidden = true;
            }
            renderDetail(order);
        } catch (issue) {
            error.textContent = issue instanceof Error ? issue.message : "The order could not be loaded.";
        }
    }

    document.addEventListener("customer-order-select", (event) => {
        const { email, reference } = event.detail || {};
        if (typeof email !== "string" || typeof reference !== "string" || !email || !reference) return;
        activeEmail = email;
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
            accountPanel.scrollIntoView({ block: "start", behavior: "instant" });
            return;
        }
        try {
            renderOrders((await request(`/api/orders?email=${encodeURIComponent(activeEmail)}&reference=${encodeURIComponent(accessReference)}`)).orders);
            list.scrollIntoView({ block: "start", behavior: "instant" });
        } catch (issue) {
            error.textContent = issue instanceof Error ? issue.message : "Your orders could not be loaded.";
        }
    });

    document.querySelector("#approve-draft").addEventListener("click", async () => {
        if (!currentOrder || !window.confirm("Approve this draft? The administrator may then move the order to printing.")) return;
        try {
            const order = await request(`/api/orders/${encodeURIComponent(currentOrder.reference)}/approve`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ email: activeEmail }),
            });
            renderDetail(order);
        } catch (issue) {
            error.textContent = issue instanceof Error ? issue.message : "The draft approval could not be recorded.";
        }
    });

    document.querySelector("#request-draft-changes").addEventListener("click", async () => {
        if (!currentOrder || !window.confirm("Ask the shop to revise this draft?")) return;
        try {
            const order = await request(`/api/orders/${encodeURIComponent(currentOrder.reference)}/request-changes`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ email: activeEmail, note: "Customer requested changes to the draft layout." }),
            });
            renderDetail(order);
        } catch (issue) {
            error.textContent = issue instanceof Error ? issue.message : "The change request could not be recorded.";
        }
    });

    document.querySelector("#cancel-order").addEventListener("click", async () => {
        if (!currentOrder || !window.confirm("Cancel this request? This cannot be undone.")) return;
        try {
            const order = await request(`/api/orders/${encodeURIComponent(currentOrder.reference)}/cancel`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ email: activeEmail }),
            });
            renderDetail(order);
        } catch (issue) {
            error.textContent = issue instanceof Error ? issue.message : "The request could not be cancelled.";
        }
    });

    const parameters = new URLSearchParams(window.location.search);
    if (parameters.has("email")) {
        emailInput.value = parameters.get("email");
        referenceInput.value = parameters.get("ref") || "";
        form.requestSubmit();
    }
})();
