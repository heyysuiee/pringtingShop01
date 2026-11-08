"use strict";

/**
 * ImPRESSion PrintShop admin application.
 * Every figure, table, and action uses the backend API; nothing on these pages is sample data.
 * Shared building blocks (tables, charts, image viewer, dialogs) live in admin-core.js.
 */
(() => {
    const UI = window.AdminUI;
    const { h, api, query, money, date, dateTime, days, number, badge, toast } = UI;

    fetch("/api/admin/session", { credentials: "same-origin" })
        .then((response) => {
            if (!response.ok) window.location.replace("/admin/login");
            return response.ok ? response.json() : null;
        })
        .then((session) => {
            adminEmail = session?.email || "";
            document.querySelectorAll("[data-admin-email]").forEach((element) => { element.textContent = adminEmail || "Administrator"; });
        })
        .catch(() => window.location.replace("/admin/login"));

    // ================================================================ constants & state

    const LABELS = {
        dashboard: "Dashboard",
        orders: "Order Management",
        customers: "Customer Management",
        services: "Services & Products",
        pricing: "Pricing Management",
        payments: "Payments & Transactions",
        reports: "Sales & Reports",
        notifications: "Notifications",
        settings: "Settings",
    };
    const STATUSES = [
        "Pending Confirmation", "Under Review", "Awaiting Quotation", "Awaiting Down Payment",
        "Draft Preparation", "Awaiting Customer Approval", "Approved for Printing",
        "In Production", "Ready for Pickup", "Completed", "Rejected", "Cancelled",
    ];
    const STATUS_GROUPS = [
        ["", "All orders", STATUSES],
        ["pending", "Pending", ["Pending Confirmation", "Under Review", "Awaiting Quotation", "Awaiting Down Payment"]],
        ["processing", "In progress", ["Draft Preparation", "Awaiting Customer Approval", "Approved for Printing", "In Production", "Ready for Pickup"]],
        ["completed", "Completed", ["Completed"]],
        ["cancelled", "Cancelled / rejected", ["Rejected", "Cancelled"]],
    ];
    const PAYMENT_STATUSES = ["Fully Paid", "Partially Paid", "Unpaid", "Not Quoted"];
    const ACTION_LABELS = {
        "Under Review": "Approve request",
        "Awaiting Quotation": "Mark as awaiting quotation",
        "Rejected": "Reject request",
        "Cancelled": "Cancel order",
        "In Production": "Start production",
        "Ready for Pickup": "Mark ready for pickup",
        "Completed": "Mark as completed",
    };
    const ACTION_HELP = {
        "Under Review": "Accept the request and start reviewing its details.",
        "Awaiting Quotation": "Use when the price needs more time to work out.",
        "Rejected": "The customer sees the request as rejected.",
        "Cancelled": "Stops the order. Orders with recorded payments cannot be cancelled here.",
        "In Production": "The customer approved the draft; printing starts.",
        "Ready for Pickup": "Printing is done and the order can be collected.",
        "Completed": "Records today as the completion date. The order must be fully paid.",
    };
    const NOTIFICATION_SEEN_KEY = "printshop.admin.notificationsSeenAt";
    const COMPACT_KEY = "printshop.admin.compactTables";

    const viewRoot = document.querySelector("#view-root");
    const dialog = document.querySelector("#admin-dialog");
    let activeView = document.body.dataset.adminView || "dashboard";
    let adminEmail = "";
    let services = [];

    const filters = {
        orders: { search: "", status: "", service: "", payment_status: "", date_from: "", date_to: "" },
        customers: { search: "", activity: "", registered_from: "", registered_to: "", ordered_from: "", ordered_to: "" },
        payments: { search: "", method: "", service: "", payment_status: "", date_from: "", date_to: "" },
        reports: {
            tab: "summary",
            summary: { preset: "month", date_from: "", date_to: "", group_by: "", service: "" },
            completed: { service: "", customer: "", request_from: "", request_to: "", completed_from: "", completed_to: "", payment_status: "", min_days: "", max_days: "" },
            service: { service: "", completed_from: "", completed_to: "", request_from: "", request_to: "" },
        },
        notifications: { actor: "", limit: 30 },
    };
    // Called after an order action so the visible table/report shows the saved change.
    let refreshActiveView = () => {};

    // ================================================================ small builders

    function heading(title, description, ...actions) {
        return h("header", { className: "page-heading" },
            h("div", {}, h("h1", { text: title }), description ? h("p", { text: description }) : null),
            actions.length ? h("div", { className: "page-heading-actions" }, ...actions) : null,
        );
    }

    function panel(title, subtitle, content, ...actions) {
        return h("section", { className: "panel" },
            h("div", { className: "panel-heading" },
                h("div", {}, h("h2", {}, ...[].concat(title)), subtitle ? h("p", { text: subtitle }) : null),
                actions.length ? h("div", { className: "panel-actions" }, ...actions) : null,
            ),
            content,
        );
    }

    function statCard({ label, value, foot, icon, onClick, tone }) {
        const card = h(onClick ? "button" : "article", { className: `stat-card${onClick ? " stat-card-link" : ""}${tone ? ` stat-${tone}` : ""}`, type: onClick ? "button" : null },
            h("span", { className: "stat-top" }, h("span", { className: "stat-label", text: label }), h("span", { className: "stat-icon", "aria-hidden": "true", text: icon || "" })),
            h("span", { className: "stat-value", text: value }),
            foot ? h("span", { className: "stat-foot", text: foot }) : null,
        );
        if (onClick) card.addEventListener("click", onClick);
        return card;
    }

    function loadingBlock(message = "Loading…") {
        return h("div", { className: "loading-block", role: "status" }, h("span", { className: "spinner", "aria-hidden": "true" }), ` ${message}`);
    }

    function errorBlock(message, retry) {
        return h("div", { className: "notice-card notice-error", role: "alert" },
            h("strong", { text: "Something went wrong. " }), message, " ",
            retry ? h("button", { className: "button button-small", type: "button", text: "Try again", onclick: retry }) : null,
        );
    }

    function field(label, control, className = "") {
        const id = control.id || `f-${Math.random().toString(36).slice(2, 9)}`;
        control.id = id;
        return h("div", { className: `filter-field ${className}`.trim() }, h("label", { for: id, text: label }), control);
    }

    function searchInput(placeholder, value, onChange) {
        const input = h("input", { type: "search", placeholder, value, autocomplete: "off" });
        input.addEventListener("input", UI.debounce(() => onChange(input.value.trim()), 300));
        return h("label", { className: "search-control filter-search" }, h("span", { "aria-hidden": "true", text: "⌕" }), input);
    }

    function selectInput(options, value, onChange) {
        const select = h("select");
        options.forEach(([optionValue, text]) => select.add(new Option(text, optionValue)));
        select.value = value;
        select.addEventListener("change", () => onChange(select.value));
        return select;
    }

    function dateInput(value, onChange) {
        const input = h("input", { type: "date", value });
        input.addEventListener("change", () => onChange(input.value));
        return input;
    }

    function numberInput(value, onChange, placeholder = "") {
        const input = h("input", { type: "number", min: 0, step: 1, value, placeholder, inputmode: "numeric" });
        input.addEventListener("input", UI.debounce(() => onChange(input.value), 400));
        return input;
    }

    function serviceOptions(list = services, allLabel = "All services") {
        return [["", allLabel], ...list.map((item) => [item.id, item.label])];
    }

    function rememberServices(list) {
        if (Array.isArray(list) && list.length) services = list;
    }

    function cellPair(primary, secondary) {
        return h("span", { className: "cell-pair" }, h("span", { className: "table-primary", text: primary || "—" }), secondary ? h("span", { className: "table-secondary", text: secondary }) : null);
    }

    function totalText(row) {
        if (row.total_amount !== null && row.total_amount !== undefined) return money(row.total_amount);
        if (row.status === "Rejected" || row.status === "Cancelled") return "—";
        return row.estimated_total !== null && row.estimated_total !== undefined ? `Est. ${money(row.estimated_total)}` : "Awaiting quote";
    }

    // ================================================================ navigation

    function viewFromLocation() {
        const route = window.location.pathname.split("/").filter(Boolean).pop();
        return LABELS[route] ? route : "dashboard";
    }

    async function navigate(view, addHistory = true) {
        const nextView = LABELS[view] ? view : "dashboard";
        const path = nextView === "dashboard" ? "/admin/dashboard" : `/admin/${nextView}`;
        document.querySelector("#admin-sidebar")?.classList.remove("open");
        if (nextView === activeView && window.location.pathname === path) {
            renderView();
            return;
        }
        try {
            const response = await fetch(path, { credentials: "same-origin" });
            if (!response.ok) throw new Error(`The admin page could not be loaded (HTTP ${response.status}).`);
            const pageDocument = new DOMParser().parseFromString(await response.text(), "text/html");
            const incomingView = pageDocument.querySelector("#view-root");
            if (!incomingView || pageDocument.body.dataset.adminView !== nextView) {
                window.location.assign(response.url || "/admin/login");
                return;
            }
            viewRoot.replaceChildren(...Array.from(incomingView.childNodes, (node) => document.importNode(node, true)));
            document.title = pageDocument.title;
            activeView = nextView;
            if (addHistory) window.history.pushState({ view: nextView }, "", path);
            renderView();
            viewRoot.focus({ preventScroll: true });
            window.scrollTo({ top: 0 });
        } catch (error) {
            toast(error instanceof Error ? error.message : "The admin page could not be loaded.", "error");
        }
    }

    function renderView() {
        document.querySelector("#current-section").textContent = LABELS[activeView];
        document.querySelectorAll("[data-view]").forEach((link) => {
            const active = link.dataset.view === activeView;
            link.classList.toggle("active", active);
            if (link.matches(".sidebar-nav a, .sidebar-bottom > a")) {
                if (active) link.setAttribute("aria-current", "page");
                else link.removeAttribute("aria-current");
            }
        });
        refreshActiveView = () => {};
        const renderers = {
            dashboard: renderDashboard,
            orders: renderOrders,
            customers: renderCustomers,
            services: () => window.PrintShopCatalogAdmin?.renderServices(),
            pricing: () => window.PrintShopCatalogAdmin?.renderPricing(),
            payments: renderPayments,
            reports: renderReports,
            notifications: renderNotifications,
            settings: renderSettings,
        };
        renderers[activeView]?.();
    }

    function viewBody() {
        return viewRoot.querySelector("[data-view-body]");
    }

    async function refreshBadges() {
        try {
            const [orders, activity] = await Promise.all([
                api(`/api/admin/orders${query({ page_size: 10 })}`),
                api(`/api/admin/activity${query({ actor: "customer", limit: 200 })}`),
            ]);
            rememberServices(orders.services);
            const pending = orders.status_counts?.["Pending Confirmation"] || 0;
            const pendingBadge = document.querySelector("#pending-count");
            pendingBadge.textContent = pending ? String(pending) : "";
            pendingBadge.title = pending ? `${pending} new request${pending === 1 ? "" : "s"} waiting for review` : "";
            const seen = localStorage.getItem(NOTIFICATION_SEEN_KEY) || "";
            const unread = activity.events.filter((event) => (event.at || "") > seen).length;
            document.querySelector("#notification-count").textContent = unread ? String(unread) : "";
            document.querySelectorAll(".notification-dot").forEach((dot) => { dot.hidden = !unread; });
        } catch {
            // Badges are a convenience; the pages themselves report loading errors.
        }
    }

    // ================================================================ dashboard

    async function renderDashboard() {
        const body = viewBody();
        if (!body) return;
        body.replaceChildren(loadingBlock("Loading live figures…"));
        let data;
        try {
            data = await api("/api/admin/dashboard");
        } catch (error) {
            body.replaceChildren(errorBlock(error.message, renderDashboard));
            return;
        }
        if (activeView !== "dashboard") return;
        const goOrders = (status) => () => {
            filters.orders = { ...filters.orders, status };
            navigate("orders");
        };
        const cards = h("section", { className: "stats-grid stats-grid-8", "aria-label": "Business summary" },
            statCard({ label: "Total customers", value: number(data.total_customers), foot: "Registered customer accounts", icon: "👥", onClick: () => navigate("customers") }),
            statCard({ label: "Total orders", value: number(data.total_orders), foot: "All order requests", icon: "▤", onClick: goOrders("") }),
            statCard({ label: "Pending orders", value: number(data.pending_orders), foot: "Awaiting review, quotation, or down payment", icon: "◷", onClick: goOrders("pending") }),
            statCard({ label: "In progress", value: number(data.processing_orders), foot: "Drafting, approval, production, pickup", icon: "⚒", onClick: goOrders("processing") }),
            statCard({ label: "Completed orders", value: number(data.completed_orders), foot: "Marked completed", icon: "✓", onClick: goOrders("completed") }),
            statCard({ label: "Cancelled orders", value: number(data.cancelled_orders), foot: "Cancelled or rejected", icon: "✕", onClick: goOrders("cancelled") }),
            statCard({ label: "Fully paid orders", value: number(data.fully_paid_orders), foot: "Payments cover the confirmed total", icon: "₱", onClick: () => { filters.orders = { ...filters.orders, status: "", payment_status: "Fully Paid" }; navigate("orders"); } }),
            statCard({ label: "Total confirmed revenue", value: money(data.total_revenue), foot: "Completed and fully paid orders only", icon: "₱", tone: "highlight", onClick: () => { filters.reports.tab = "summary"; filters.reports.summary.preset = "all"; navigate("reports"); } }),
        );

        const trend = h("div", { className: "chart-box" });
        UI.columnChart(trend, data.revenue_trend.map((bucket) => ({
            label: bucket.label,
            short: shortBucket(bucket.key, "month"),
            value: bucket.revenue,
            completed: bucket.completed_orders,
        })), {
            format: money,
            axisFormat: UI.compactMoney,
            detail: (point) => `${point.completed} completed order${point.completed === 1 ? "" : "s"}`,
            emptyMessage: "No confirmed revenue in the last 12 months yet.",
        });

        const statusChart = h("div", { className: "chart-box" });
        UI.barChart(statusChart, STATUSES.map((status) => ({ label: status, value: data.status_counts[status] || 0 })).filter((item) => item.value), {
            emptyMessage: "No orders have been submitted yet.",
            detail: (item) => `${Math.round((item.value / Math.max(1, data.total_orders)) * 100)}% of all orders`,
            onSelect: (item) => { filters.orders = { ...filters.orders, status: item.label }; navigate("orders"); },
        });
        const ordersChart = h("div", { className: "chart-box" });
        UI.barChart(ordersChart, data.by_service.map((item) => ({ label: item.service, value: item.orders })), { emptyMessage: "No orders yet." });
        const completedChart = h("div", { className: "chart-box" });
        UI.barChart(completedChart, data.by_service.map((item) => ({ label: item.service, value: item.completed_orders })), { emptyMessage: "No completed orders yet." });
        const revenueChart = h("div", { className: "chart-box" });
        UI.barChart(revenueChart, data.by_service.map((item) => ({ label: item.service, value: item.revenue })), {
            format: money,
            emptyMessage: "No confirmed revenue yet.",
            detail: (item) => (data.total_revenue ? `${((item.value / data.total_revenue) * 100).toFixed(1)}% of revenue` : ""),
        });

        const recentTable = h("div");
        new UI.DataTable(recentTable, {
            columns: [
                { label: "Order ID", className: "ref-cell", render: (row) => cellPair(row.reference, row.request_date ? date(row.request_date) : "") },
                { label: "Customer", render: (row) => cellPair(row.customer_name, row.product) },
                { label: "Total", align: "right", render: (row) => totalText(row) },
                { label: "Status", render: (row) => badge(row.status) },
            ],
            rows: () => data.recent_orders,
            pageSize: 10,
            emptyMessage: "No orders have been submitted yet.",
            onRowClick: (row) => openOrder(row.reference),
            rowTitle: (row) => `Open order ${row.reference}`,
        }).load();
        const completedTable = h("div");
        new UI.DataTable(completedTable, {
            columns: [
                { label: "Transaction", className: "ref-cell", render: (row) => cellPair(row.reference, row.customer_name) },
                { label: "Service", render: (row) => cellPair(row.service, row.product) },
                { label: "Completed", render: (row) => cellPair(date(row.completion_date), days(row.completion_days)) },
                { label: "Amount", align: "right", render: (row) => money(row.total_amount) },
                { label: "Payment", render: (row) => badge(row.payment_status) },
            ],
            rows: () => data.recent_completed,
            pageSize: 10,
            emptyMessage: "No completed transactions yet.",
            onRowClick: (row) => openOrder(row.reference),
        }).load();

        body.replaceChildren(
            cards,
            h("div", { className: "dashboard-grid" },
                panel("Revenue trend", "Confirmed revenue per month, last 12 months", trend),
                panel("Order status distribution", `${number(data.total_orders)} orders · select a status to open it`, statusChart),
                panel("Orders per service", "All order requests by printing service", ordersChart),
                panel("Completed orders per service", "Completed transactions by printing service", completedChart),
            ),
            panel("Revenue by service", "Confirmed revenue (completed and fully paid)", revenueChart,
                h("button", { className: "button button-small", type: "button", text: "Open reports", onclick: () => navigate("reports") })),
            panel("Recent orders", "Newest requests · select a row for details", recentTable,
                h("button", { className: "button button-small", type: "button", text: "All orders", onclick: () => navigate("orders") })),
            panel("Recently completed transactions", "Select a row to view the transaction", completedTable),
        );
        refreshActiveView = renderDashboard;
    }

    function shortBucket(key, group) {
        const [year, month, day] = key.split("-").map(Number);
        const value = new Date(Date.UTC(year, month - 1, day));
        const options = { timeZone: "UTC" };
        if (group === "day" || group === "week") return value.toLocaleDateString("en-PH", { ...options, month: "short", day: "numeric" });
        if (group === "month") return value.toLocaleDateString("en-PH", { ...options, month: "short", year: "2-digit" });
        return String(year);
    }

    // ================================================================ orders

    function renderOrders() {
        const body = viewBody();
        if (!body) return;
        const f = filters.orders;
        const tabs = h("div", { className: "segmented", role: "tablist", "aria-label": "Order status groups" });
        const countLabel = h("span", { className: "table-secondary" });
        const tableHost = h("div");
        const serviceSelect = selectInput(serviceOptions(), f.service, (value) => apply({ service: value }));
        const statusSelect = selectInput([["", "All statuses"], ...STATUS_GROUPS.slice(1).map(([value, label]) => [value, `${label} (group)`]), ...STATUSES.map((status) => [status, status])], f.status, (value) => apply({ status: value }));
        const paymentSelect = selectInput([["", "All payment statuses"], ...PAYMENT_STATUSES.map((status) => [status, status])], f.payment_status, (value) => apply({ payment_status: value }));
        const fromInput = dateInput(f.date_from, (value) => apply({ date_from: value }));
        const toInput = dateInput(f.date_to, (value) => apply({ date_to: value }));
        const search = searchInput("Search order ID, customer, email, product…", f.search, (value) => apply({ search: value }));

        function renderTabs(counts = {}) {
            tabs.replaceChildren(...STATUS_GROUPS.map(([value, label, statuses]) => {
                const count = statuses.reduce((sum, status) => sum + (counts[status] || 0), 0);
                return h("button", {
                    className: `segmented-button${f.status === value ? " active" : ""}`,
                    type: "button",
                    role: "tab",
                    "aria-selected": String(f.status === value),
                    onclick: () => { statusSelect.value = value; apply({ status: value }); },
                }, label, h("span", { className: "segmented-count", text: number(count) }));
            }));
        }

        function apply(values) {
            Object.assign(f, values);
            renderTabs(lastCounts);
            table.setQuery({ ...f });
        }

        let lastCounts = {};
        const table = new UI.DataTable(tableHost, {
            columns: [
                { label: "Order ID", sortKey: "reference", className: "ref-cell", render: (row) => h("span", { className: "cell-pair" },
                    h("span", { className: "table-primary", text: row.reference }),
                    row.image_count ? h("span", { className: "table-chip", text: `🖼 ${row.image_count} image${row.image_count === 1 ? "" : "s"}` }) : null) },
                { label: "Customer", sortKey: "customer", render: (row) => cellPair(row.customer_name, row.customer_email) },
                { label: "Service / product", sortKey: "service", render: (row) => cellPair(row.product, row.service) },
                { label: "Request date", sortKey: "request_date", type: "date", render: (row) => date(row.request_date) },
                { label: "Total", sortKey: "total", align: "right", render: (row) => totalText(row) },
                { label: "Paid", sortKey: "paid", align: "right", render: (row) => money(row.amount_paid) },
                { label: "Payment", sortKey: "payment_status", render: (row) => badge(row.payment_status) },
                { label: "Status", sortKey: "status", render: (row) => badge(row.status) },
            ],
            defaultSort: { key: "request_date", dir: "desc" },
            initialQuery: { ...f },
            fetch: (params) => api(`/api/admin/orders${query(params)}`),
            onLoad: (result) => {
                lastCounts = result.status_counts || {};
                renderTabs(lastCounts);
                if (result.services?.length && serviceSelect.options.length <= 1) {
                    rememberServices(result.services);
                    serviceSelect.replaceChildren(...serviceOptions().map(([value, text]) => new Option(text, value)));
                    serviceSelect.value = f.service;
                }
                countLabel.textContent = `${number(result.total)} order${result.total === 1 ? "" : "s"}`;
            },
            emptyMessage: "No customer orders have been submitted yet.",
            onRowClick: (row) => openOrder(row.reference),
            rowTitle: (row) => `Open order ${row.reference}`,
            caption: "Orders",
        });
        renderTabs();
        const clear = h("button", { className: "button button-small", type: "button", text: "Clear filters", onclick: () => {
            Object.assign(f, { search: "", status: "", service: "", payment_status: "", date_from: "", date_to: "" });
            renderOrders();
        } });
        body.replaceChildren(
            tabs,
            h("div", { className: "filter-bar" },
                search,
                field("Service", serviceSelect),
                field("Status", statusSelect),
                field("Payment", paymentSelect),
                field("Requested from", fromInput),
                field("Requested to", toInput),
                clear,
            ),
            panel(["Orders ", countLabel], "Select a row to review the order, its uploaded samples, and available actions.", tableHost,
                UI.exportButtons("orders", () => ({ ...f, sort: table.sort.key, dir: table.sort.dir }))),
        );
        table.load();
        refreshActiveView = () => table.reload();
    }

    // ================================================================ order details modal

    async function openOrder(reference, { back = null, focus = "" } = {}) {
        const container = h("div", { className: "order-modal" }, loadingBlock("Loading order details…"));
        UI.openModal(container, { wide: true, size: "xl" });
        let order;
        try {
            order = await api(`/api/admin/orders/${encodeURIComponent(reference)}`);
        } catch (error) {
            container.replaceChildren(errorBlock(error.message, () => openOrder(reference, { back, focus })));
            return;
        }
        if (!dialog.open || !container.isConnected) return;
        container.replaceChildren(...orderModalContent(order, { back, focus }));
        if (focus) container.querySelector(`[data-focus="${focus}"]`)?.scrollIntoView({ block: "start" });
    }

    function orderImages(order) {
        const images = [];
        if (order.product?.image) {
            images.push({ src: order.product.image, name: `${order.product.name} — catalog sample`, caption: "Product sample the customer selected from the catalog", kind: "Catalog sample" });
        }
        order.attachments.filter((file) => file.mime_type.startsWith("image/")).forEach((file) => {
            images.push({ src: file.url, name: file.name, caption: `${file.field_label || "Reference file"} · uploaded by the customer · ${UI.fileSize(file.size)}`, kind: file.field_label || "Customer upload" });
        });
        if (order.draft_attachment?.mime_type?.startsWith("image/")) {
            images.push({ src: order.draft_attachment.url, name: order.draft_attachment.name, caption: "Draft layout sent to the customer for approval", kind: "Draft layout" });
        }
        return images;
    }

    function gallery(order) {
        const images = orderImages(order);
        const documents = order.attachments.filter((file) => !file.mime_type.startsWith("image/"));
        if (order.draft_attachment && !order.draft_attachment.mime_type?.startsWith("image/")) documents.push({ ...order.draft_attachment, field_label: "Draft layout" });
        const section = h("section", { className: "order-gallery", "aria-label": "Product samples and uploads" });
        if (!images.length) {
            section.append(h("div", { className: "gallery-empty" }, h("span", { "aria-hidden": "true", text: "🖼" }), h("p", { text: "No product sample or uploaded images for this order." })));
        } else {
            let current = 0;
            const mainImage = h("img", { alt: "", decoding: "async" });
            const kind = h("span", { className: "gallery-kind" });
            const main = h("button", { className: "gallery-main", type: "button", title: "Open large preview with zoom and fullscreen" },
                mainImage, kind, h("span", { className: "gallery-zoom-hint", "aria-hidden": "true", text: "🔍 Zoom · Fullscreen" }));
            const name = h("p", { className: "gallery-name" });
            const thumbs = h("div", { className: "gallery-thumbs" });
            const select = (index) => {
                current = index;
                const image = images[index];
                mainImage.src = image.src;
                mainImage.alt = image.name;
                kind.textContent = image.kind;
                name.textContent = `${image.name} — ${image.caption}`;
                main.setAttribute("aria-label", `Open ${image.name} in the large preview`);
                thumbs.querySelectorAll("button").forEach((thumb, position) => thumb.classList.toggle("active", position === index));
            };
            mainImage.addEventListener("error", () => { kind.textContent = "Image unavailable"; });
            images.forEach((image, index) => thumbs.append(h("button", {
                className: "gallery-thumb", type: "button", "aria-label": `Show ${image.name}`, title: image.name, onclick: () => select(index),
            }, h("img", { src: image.src, alt: "", loading: "lazy", decoding: "async" }))));
            main.addEventListener("click", () => UI.openViewer(images, current));
            section.append(main, name, images.length > 1 ? thumbs : null,
                h("button", { className: "button button-small gallery-open", type: "button", text: `Open large preview (${images.length} image${images.length === 1 ? "" : "s"})`, onclick: () => UI.openViewer(images, current) }));
            select(0);
        }
        if (documents.length) {
            section.append(h("div", { className: "gallery-docs" }, h("strong", { text: "Other files" }),
                h("ul", {}, ...documents.map((file) => h("li", {},
                    h("a", { href: file.url, target: "_blank", rel: "noopener", text: `📄 ${file.name}` }),
                    h("span", { className: "table-secondary", text: ` ${file.field_label || "Reference file"} · ${UI.fileSize(file.size || 0)}` }))))));
        }
        return section;
    }

    function detailList(items) {
        return h("dl", { className: "detail-list" }, ...items.filter(Boolean).flatMap(([label, value]) => [
            h("dt", { text: label }),
            h("dd", {}, value instanceof Node ? value : (value === null || value === undefined || value === "" ? "—" : String(value))),
        ]));
    }

    function specifications(order) {
        const customization = order.customization || {};
        if (Array.isArray(customization.specifications)) return customization.specifications.map((spec) => [spec.label, spec.value]);
        return [["Size", customization.size], ["Material / variant", customization.material], ["Customization", customization.details]].filter(([, value]) => value);
    }

    function orderModalContent(order, { back, focus }) {
        const summary = order.summary;
        const reload = () => openOrder(order.reference, { back, focus });
        const header = h("header", { className: "order-modal-header" },
            back ? h("button", { className: "button button-small back-button", type: "button", text: "← Back", onclick: back }) : null,
            h("div", {},
                h("p", { className: "eyebrow", text: `${summary.service} · ${summary.product}` }),
                h("h2", { className: "dialog-heading", text: `Order ${order.reference}` }),
                h("div", { className: "badge-row" }, badge(order.status), badge(summary.payment_status), h("span", { className: "table-secondary", text: `Requested ${dateTime(order.created_at)}` })),
            ),
        );
        const customerLink = order.customer_id
            ? h("button", { className: "table-link", type: "button", text: "View customer profile →", onclick: () => openCustomer(order.customer_id, { back: () => openOrder(order.reference, { back }) }) })
            : null;
        const facts = h("div", { className: "order-facts" },
            h("section", { className: "fact-card" }, h("h3", { text: "Customer" }), detailList([
                ["Name", order.customer.name],
                ["Email", h("a", { href: `mailto:${order.customer.email}`, text: order.customer.email })],
                ["Contact number", order.customer.phone],
                order.customer.messenger ? ["Messenger", order.customer.messenger] : null,
            ]), customerLink),
            h("section", { className: "fact-card" }, h("h3", { text: "Order" }), detailList([
                ["Order ID", order.reference],
                ["Service", summary.service],
                ["Product", summary.product],
                ["Quantity", number(order.customization?.quantity)],
                ["Request date", dateTime(order.created_at)],
                ["Completion date", summary.completed_at ? dateTime(summary.completed_at) : "Not completed"],
                summary.completion_days !== null ? ["Completion duration", `${days(summary.completion_days)} (calendar)`] : null,
            ])),
            h("section", { className: "fact-card", dataset: { focus: "payments" } }, h("h3", { text: "Payment" }), detailList([
                ["Confirmed total", order.total_amount === null ? "Waiting for quotation" : money(order.total_amount)],
                ["Catalog estimate", summary.estimated_total === null ? "Needs a custom quotation" : money(summary.estimated_total)],
                ["Amount paid", money(summary.amount_paid)],
                ["Balance", summary.balance === null ? "—" : money(summary.balance)],
                ["Payment status", badge(summary.payment_status)],
            ])),
        );

        const specs = specifications(order);
        const details = h("div", { className: "order-detail-sections" },
            specs.length ? h("section", { className: "fact-card" }, h("h3", { text: "Customer specifications" }), detailList(specs)) : null,
            h("section", { className: "fact-card" }, h("h3", { text: "Special instructions" }),
                h("p", { className: "prewrap", text: order.customization?.notes || order.customization?.instructions || "None provided." })),
            order.has_event ? h("section", { className: "fact-card" }, h("h3", { text: "Event details" }), detailList([
                ["Celebrant", order.event_details.celebrant],
                ["Event type", order.event_details.event_type],
                ["Event date", order.event_details.event_date ? date(order.event_details.event_date) : ""],
                ["Event time", order.event_details.event_time],
                ["Location", order.event_details.event_location],
            ])) : null,
            pricingCard(order.pricing),
            order.admin_notes ? h("section", { className: "fact-card" }, h("h3", { text: "Latest note" }), h("p", { className: "prewrap", text: order.admin_notes })) : null,
        );

        const payments = h("section", { className: "fact-card" }, h("h3", { text: `Payment history (${order.payments.length})` }),
            order.payments.length ? h("div", { className: "table-wrap" }, h("table", { className: "data-table compact-table" },
                h("thead", {}, h("tr", {}, ...["Received", "Method", "Reference", "Amount"].map((text, index) => h("th", { scope: "col", className: index === 3 ? "align-right" : "", text })))),
                h("tbody", {}, ...order.payments.map((payment) => h("tr", {},
                    h("td", { text: dateTime(payment.received_at) }), h("td", { text: payment.method }),
                    h("td", { text: payment.reference || "—" }), h("td", { className: "align-right", text: money(payment.amount) })))),
            )) : h("p", { className: "field-note", text: "No payments recorded yet." }),
            order.payment_instructions ? h("p", { className: "field-note" }, h("strong", { text: "Payment instructions shared: " }), order.payment_instructions) : null,
        );

        const history = h("section", { className: "fact-card" }, h("h3", { text: "Order activity" }),
            h("ol", { className: "timeline" }, ...[...order.history].reverse().map((entry) => h("li", {},
                h("span", { text: entry.text }), h("time", { text: dateTime(entry.at) })))));

        return [
            header,
            h("div", { className: "order-top" }, gallery(order), h("div", { className: "order-top-side" }, facts, actionsPanel(order, reload))),
            details,
            payments,
            history,
        ];
    }

    function pricingCard(pricing) {
        if (!pricing || !Object.keys(pricing).length) return null;
        const items = pricing.requires_quote
            ? (pricing.quote_reasons || []).map((reason) => h("li", { text: reason }))
            : [
                ...(pricing.lines || []).map((line) => h("li", { text: `${line.label}: ${money(line.amount)} (${line.detail})` })),
                pricing.area_sqft ? h("li", { text: `Print area: ${pricing.area_sqft} sq ft per piece` }) : null,
                h("li", { className: "strong", text: pricing.estimated_total === null ? "No total calculated." : `Estimated total: ${money(pricing.estimated_total)}` }),
            ];
        return h("section", { className: "fact-card" }, h("h3", { text: "Price estimate from catalog prices" }), h("ul", { className: "plain-list" }, ...items));
    }

    function actionsPanel(order, reload) {
        const summary = order.summary;
        const actions = order.actions;
        const section = h("section", { className: "actions-card", dataset: { focus: "actions" } }, h("h3", { text: "Available actions" }));
        const buttons = h("div", { className: "action-buttons" });

        order.allowed_statuses.forEach((status) => {
            const danger = status === "Rejected" || status === "Cancelled";
            const blocked = (danger && actions.cancel_blocked_by_payments) || (status === "Completed" && actions.complete_requires_payment);
            const button = h("button", {
                className: `button ${danger ? "button-danger-strong" : "button-primary"}`,
                type: "button",
                text: ACTION_LABELS[status] || status,
                disabled: blocked,
                title: ACTION_HELP[status] || "",
            });
            button.addEventListener("click", async () => {
                const answer = await UI.confirm({
                    title: `${ACTION_LABELS[status] || status}?`,
                    message: `${ACTION_HELP[status] || ""} Order ${order.reference} will move from “${order.status}” to “${status}”.`,
                    confirmLabel: ACTION_LABELS[status] || "Confirm",
                    tone: danger ? "danger" : "primary",
                    input: danger ? { label: status === "Rejected" ? "Reason for rejection (recorded in the order history)" : "Reason (optional)", required: status === "Rejected", placeholder: "e.g. Artwork cannot be reproduced at this size" } : null,
                    details: status === "Completed" ? [["Confirmed total", money(order.total_amount)], ["Amount paid", money(summary.amount_paid)], ["Completion date", "Today"]] : [],
                });
                if (!answer) return;
                await submitAction(button, order, "status", { status, note: answer.value || "" }, reload, `Order marked as ${status}.`);
            });
            buttons.append(button);
        });
        if (buttons.childElementCount) section.append(buttons);
        if (actions.complete_requires_payment) {
            section.append(h("p", { className: "notice-card notice-warning", text: `Record the remaining balance of ${money(summary.balance)} before marking this order as completed. Only fully paid orders count as completed revenue.` }));
        }
        if (actions.cancel_blocked_by_payments && order.allowed_statuses.some((status) => status === "Rejected" || status === "Cancelled")) {
            section.append(h("p", { className: "field-note", text: "This order has recorded payments, so it cannot be rejected or cancelled from here." }));
        }
        if (order.status === "Awaiting Customer Approval") {
            section.append(h("p", { className: "notice-card", text: "Waiting for the customer to approve the draft in their portal. The administrator cannot approve printing for the customer." }));
        }
        if (actions.quote) section.append(quoteForm(order, reload));
        if (actions.record_payment) section.append(paymentForm(order, reload));
        if (actions.send_draft) section.append(draftForm(order, reload));
        if (section.childElementCount === 1) {
            const final = { Completed: "This order is completed and fully paid. No further actions are needed.", Rejected: "This request was rejected.", Cancelled: "This order was cancelled." }[order.status];
            section.append(h("p", { className: "field-note", text: final || "No administrator action is available at this stage." }));
        }
        return section;
    }

    async function submitAction(button, order, action, payload, reload, successMessage) {
        await UI.busy(button, async () => {
            try {
                await api(`/api/admin/orders/${encodeURIComponent(order.reference)}/${action}`, {
                    method: "POST",
                    body: { ...payload, expected_version: order.version },
                });
                toast(successMessage, "success");
                await reload();
                refreshActiveView();
                refreshBadges();
            } catch (error) {
                toast(error.message, "error");
                if (error.status === 409) await reload();
            }
        });
    }

    function quoteForm(order, reload) {
        const estimate = order.summary.estimated_total;
        const amount = h("input", { id: "quote-amount", name: "total_amount", type: "number", min: "0.01", step: "0.01", required: true, value: order.total_amount ?? estimate ?? "" });
        const instructions = h("textarea", { id: "quote-instructions", name: "payment_instructions", maxlength: 1000, placeholder: "The shop's approved payment instructions (GCash number, bank details, etc.)" });
        instructions.value = order.payment_instructions || "";
        const submit = h("button", { className: "button button-primary", type: "submit", text: "Send quotation" });
        const form = h("form", { className: "action-form" },
            h("h4", { text: "Confirm quotation" }),
            h("p", { className: "field-note", text: estimate === null ? "No catalog estimate: enter a custom quotation." : `Prefilled with the catalog estimate of ${money(estimate)}. Adjust it if needed.` }),
            h("div", { className: "form-grid" },
                h("div", { className: "form-field" }, h("label", { for: "quote-amount", text: "Total order amount (PHP)" }), amount),
                h("div", { className: "form-field full" }, h("label", { for: "quote-instructions", text: "Payment instructions" }), instructions)),
            h("div", { className: "dialog-actions" }, submit),
        );
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            const total = Number(amount.value);
            const answer = await UI.confirm({ title: "Send this quotation?", message: "The customer sees the quotation and payment instructions in their portal. The order moves to Awaiting Down Payment.", confirmLabel: "Send quotation", details: [["Total amount", money(total)]] });
            if (!answer) return;
            await submitAction(submit, order, "quotation", { total_amount: total, payment_instructions: instructions.value.trim() }, reload, "Quotation saved and shared with the customer.");
        });
        return form;
    }

    function paymentForm(order, reload) {
        const summary = order.summary;
        const amount = h("input", { id: "payment-amount", type: "number", min: "0.01", step: "0.01", max: String(summary.balance), required: true });
        const method = selectInput(["GCash", "Maya", "Bank transfer", "Cash", "Other"].map((value) => [value, value]), "GCash", () => {});
        method.id = "payment-method";
        const reference = h("input", { id: "payment-reference", maxlength: 120, placeholder: "Receipt or transaction number" });
        const received = h("input", { id: "payment-received", type: "checkbox", required: true });
        const submit = h("button", { className: "button button-primary", type: "submit", text: "Confirm payment" });
        const down = order.total_amount * 0.5;
        const fill = (value) => h("button", { className: "button button-small", type: "button", text: value === summary.balance ? `Full balance ${money(value)}` : `50% down ${money(value)}`, onclick: () => { amount.value = value.toFixed(2); } });
        const form = h("form", { className: "action-form", dataset: { focus: "payment" } },
            h("h4", { text: "Confirm a received payment" }),
            h("p", { className: "field-note", text: `Confirmed total ${money(order.total_amount)} · Paid ${money(summary.amount_paid)} · Balance ${money(summary.balance)}. Record only money the shop has actually received.` }),
            h("div", { className: "quick-fill" }, summary.amount_paid < down - 0.005 && down < summary.balance ? fill(Math.round((down - summary.amount_paid) * 100) / 100) : null, fill(summary.balance)),
            h("div", { className: "form-grid" },
                h("div", { className: "form-field" }, h("label", { for: "payment-amount", text: "Amount received (PHP)" }), amount),
                h("div", { className: "form-field" }, h("label", { for: "payment-method", text: "Payment method" }), method),
                h("div", { className: "form-field full" }, h("label", { for: "payment-reference", text: "Payment reference (optional)" }), reference),
                h("label", { className: "check-field full", for: "payment-received" }, received, " I confirm this payment has actually been received.")),
            h("div", { className: "dialog-actions" }, submit),
        );
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            const value = Number(amount.value);
            const remaining = Math.round((summary.balance - value) * 100) / 100;
            const answer = await UI.confirm({
                title: "Record this payment?",
                message: "Payments cannot be edited after they are saved.",
                confirmLabel: "Record payment",
                details: [["Amount", money(value)], ["Method", method.value], ["Balance after payment", money(Math.max(0, remaining))]],
            });
            if (!answer) return;
            await submitAction(submit, order, "payments", { amount: value, method: method.value, reference: reference.value.trim(), received: true }, reload,
                remaining <= 0 ? "Payment recorded. The order is now fully paid." : "Payment recorded.");
        });
        return form;
    }

    function draftForm(order, reload) {
        const file = h("input", { id: "draft-file", type: "file", accept: ".jpg,.jpeg,.png,.webp,.pdf,image/jpeg,image/png,image/webp,application/pdf", required: true });
        const submit = h("button", { className: "button button-primary", type: "submit", text: order.draft_attachment ? "Replace draft" : "Send draft for approval" });
        const form = h("form", { className: "action-form" },
            h("h4", { text: order.draft_attachment ? "Send a revised draft" : "Send a draft layout for customer approval" }),
            h("p", { className: "field-note", text: "The draft appears in the customer's My Orders page. Printing starts only after the customer approves it." }),
            h("div", { className: "form-field" }, h("label", { for: "draft-file", text: "Draft file (JPG, PNG, WebP, or PDF · max 5 MB)" }), file),
            h("div", { className: "dialog-actions" }, submit),
        );
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const chosen = file.files?.[0];
            if (!chosen || !/\.(jpe?g|png|webp|pdf)$/i.test(chosen.name)) {
                toast("Choose a JPG, PNG, WebP, or PDF draft file.", "error");
                return;
            }
            if (chosen.size > 5 * 1024 * 1024) {
                toast("Draft files must be no larger than 5 MB.", "error");
                return;
            }
            const answer = await UI.confirm({ title: "Send this draft to the customer?", message: `${chosen.name} will be shared for approval.`, confirmLabel: "Send draft" });
            if (!answer) return;
            const dataUrl = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.addEventListener("load", () => resolve(reader.result));
                reader.addEventListener("error", () => reject(new Error(`Could not read ${chosen.name}.`)));
                reader.readAsDataURL(chosen);
            });
            await submitAction(submit, order, "draft", { file: { name: chosen.name, dataUrl } }, reload, "Draft sent for customer approval.");
        });
        return form;
    }

    // ================================================================ customers

    const CUSTOMER_ACTIVITY = [
        ["", "Any order activity"],
        ["with_orders", "Has orders"],
        ["no_orders", "No orders yet"],
        ["with_pending", "Has pending orders"],
        ["with_completed", "Has completed orders"],
        ["repeat", "Repeat customers (2+ orders)"],
        ["with_spend", "Has confirmed spending"],
        ["no_spend", "No confirmed spending"],
    ];

    function renderCustomers() {
        const body = viewBody();
        if (!body) return;
        const f = filters.customers;
        const tableHost = h("div");
        const countLabel = h("span", { className: "table-secondary" });
        const summaryLine = h("p", { className: "field-note" });
        const table = new UI.DataTable(tableHost, {
            columns: [
                { label: "Customer ID", render: (row) => h("span", { className: "mono", text: row.display_id }) },
                { label: "Customer name", sortKey: "name", render: (row) => cellPair(row.name, row.last_order_date ? `Last order ${date(row.last_order_date)}` : "No orders yet") },
                { label: "Email address", sortKey: "email", render: (row) => row.email },
                { label: "Contact number", render: (row) => row.phone || "—" },
                { label: "Registered", sortKey: "registered", type: "date", render: (row) => date(row.registered_date) },
                { label: "Total orders", sortKey: "orders", align: "right", render: (row) => number(row.total_orders) },
                { label: "Completed", sortKey: "completed", align: "right", render: (row) => number(row.completed_orders) },
                { label: "Pending", sortKey: "pending", align: "right", render: (row) => number(row.pending_orders) },
                { label: "Amount spent", sortKey: "spent", align: "right", render: (row) => h("strong", { text: money(row.total_spent) }) },
            ],
            defaultSort: { key: "registered", dir: "desc" },
            initialQuery: { ...f },
            fetch: (params) => api(`/api/admin/customers${query(params)}`),
            onLoad: (result) => {
                countLabel.textContent = `${number(result.total)} customer${result.total === 1 ? "" : "s"}`;
                summaryLine.textContent = `${number(result.summary.with_orders)} with orders · ${money(result.summary.total_spent)} total spent by these customers (completed and fully paid orders only).`;
            },
            emptyMessage: "No customer accounts have been registered yet.",
            onRowClick: (row) => openCustomer(row.id),
            rowTitle: (row) => `Open ${row.name}'s profile`,
        });
        const apply = (values) => {
            Object.assign(f, values);
            table.setQuery({ ...f });
        };
        body.replaceChildren(
            h("div", { className: "filter-bar" },
                searchInput("Search name, email, phone, or customer ID…", f.search, (value) => apply({ search: value })),
                field("Order activity", selectInput(CUSTOMER_ACTIVITY, f.activity, (value) => apply({ activity: value }))),
                field("Registered from", dateInput(f.registered_from, (value) => apply({ registered_from: value }))),
                field("Registered to", dateInput(f.registered_to, (value) => apply({ registered_to: value }))),
                field("Ordered from", dateInput(f.ordered_from, (value) => apply({ ordered_from: value }))),
                field("Ordered to", dateInput(f.ordered_to, (value) => apply({ ordered_to: value }))),
                h("button", { className: "button button-small", type: "button", text: "Clear filters", onclick: () => {
                    Object.keys(f).forEach((key) => { f[key] = ""; });
                    renderCustomers();
                } }),
            ),
            panel(["Customers ", countLabel], "Amount spent counts only completed and fully paid orders. Select a row for the full history.", h("div", {}, summaryLine, tableHost),
                UI.exportButtons("customers", () => ({ ...f, sort: table.sort.key, dir: table.sort.dir }))),
        );
        table.load();
        refreshActiveView = () => table.reload();
    }

    async function openCustomer(customerId, { back = null } = {}) {
        const container = h("div", { className: "customer-modal" }, loadingBlock("Loading customer…"));
        UI.openModal(container, { wide: true, size: "xl" });
        let data;
        try {
            data = await api(`/api/admin/customers/${encodeURIComponent(customerId)}`);
        } catch (error) {
            container.replaceChildren(errorBlock(error.message, () => openCustomer(customerId, { back })));
            return;
        }
        if (!container.isConnected) return;
        const customer = data.customer;
        const self = () => openCustomer(customerId, { back });
        const ordersHost = h("div");
        const paymentsHost = h("div");
        const servicesHost = h("div");
        new UI.DataTable(ordersHost, {
            columns: [
                { label: "Order ID", sortKey: "reference", render: (row) => row.reference },
                { label: "Service / product", sortKey: "service", value: (row) => row.service, render: (row) => cellPair(row.product, row.service) },
                { label: "Requested", sortKey: "created_at", type: "date", render: (row) => date(row.request_date) },
                { label: "Total", sortKey: "total_amount", align: "right", render: (row) => totalText(row) },
                { label: "Paid", sortKey: "amount_paid", align: "right", render: (row) => money(row.amount_paid) },
                { label: "Payment", sortKey: "payment_status", render: (row) => badge(row.payment_status) },
                { label: "Status", sortKey: "status", render: (row) => badge(row.status) },
            ],
            rows: () => [...data.orders],
            defaultSort: { key: "created_at", dir: "desc" },
            pageSize: 10,
            emptyMessage: "This customer has not placed any orders.",
            onRowClick: (row) => openOrder(row.reference, { back: self }),
        }).load();
        new UI.DataTable(paymentsHost, {
            columns: [
                { label: "Received", sortKey: "received_at", type: "date", render: (row) => dateTime(row.received_at) },
                { label: "Order ID", sortKey: "reference", render: (row) => cellPair(row.reference, row.product) },
                { label: "Method", sortKey: "method", render: (row) => row.method },
                { label: "Reference", render: (row) => row.payment_reference || "—" },
                { label: "Amount", sortKey: "amount", align: "right", render: (row) => money(row.amount) },
            ],
            rows: () => [...data.payments],
            defaultSort: { key: "received_at", dir: "desc" },
            pageSize: 10,
            emptyMessage: "No payments recorded for this customer.",
            onRowClick: (row) => openOrder(row.reference, { back: self, focus: "payments" }),
        }).load();
        new UI.DataTable(servicesHost, {
            columns: [
                { label: "Service", sortKey: "service", render: (row) => row.service },
                { label: "Orders", sortKey: "orders", align: "right", render: (row) => number(row.orders) },
                { label: "Completed", sortKey: "completed", align: "right", render: (row) => number(row.completed) },
                { label: "Amount spent", sortKey: "spent", align: "right", render: (row) => money(row.spent) },
                { label: "Last order", sortKey: "last_order_date", type: "date", render: (row) => date(row.last_order_date) },
            ],
            rows: () => [...data.service_history],
            defaultSort: { key: "orders", dir: "desc" },
            pageSize: 10,
            emptyMessage: "No service history yet.",
        }).load();
        const tabs = tabSet([
            ["orders", `Order history (${data.orders.length})`, ordersHost],
            ["payments", `Payment history (${data.payments.length})`, paymentsHost],
            ["services", `Service history (${data.service_history.length})`, servicesHost],
        ]);
        container.replaceChildren(
            h("header", { className: "order-modal-header" },
                back ? h("button", { className: "button button-small back-button", type: "button", text: "← Back", onclick: back }) : null,
                h("div", {},
                    h("p", { className: "eyebrow", text: `Customer ${customer.display_id}` }),
                    h("h2", { className: "dialog-heading", text: customer.name }),
                    h("p", { className: "table-secondary", text: `Registered ${dateTime(customer.created_at)}` })),
                h("div", { className: "header-actions" }, UI.exportButtons("customer", () => ({ customer_id: customer.id }), { label: "Report" })),
            ),
            h("div", { className: "mini-stats" },
                miniStat("Total orders", number(customer.total_orders)),
                miniStat("Completed", number(customer.completed_orders)),
                miniStat("Pending", number(customer.pending_orders)),
                miniStat("Cancelled", number(customer.cancelled_orders)),
                miniStat("Amount spent", money(customer.total_spent), "Completed and fully paid orders"),
                miniStat("Total payments received", money(customer.total_paid), "Includes payments on open orders"),
            ),
            h("section", { className: "fact-card" }, h("h3", { text: "Customer information" }), detailList([
                ["Customer ID", customer.display_id],
                ["Name", customer.name],
                ["Email address", h("a", { href: `mailto:${customer.email}`, text: customer.email })],
                ["Contact number", customer.phone],
                customer.messenger ? ["Messenger", customer.messenger] : null,
                ["Registration date", date(customer.registered_date)],
                ["Last order", customer.last_order_date ? date(customer.last_order_date) : "No orders yet"],
            ])),
            tabs,
        );
    }

    function miniStat(label, value, note = "") {
        return h("div", { className: "mini-stat", title: note || null }, h("span", { text: label }), h("strong", { text: value }), note ? h("small", { text: note }) : null);
    }

    function tabSet(entries, active = entries[0][0], onChange = null) {
        const list = h("div", { className: "tab-list", role: "tablist" });
        const panels = h("div", { className: "tab-panels" });
        const select = (key) => {
            list.querySelectorAll("button").forEach((button) => {
                const on = button.dataset.tab === key;
                button.classList.toggle("active", on);
                button.setAttribute("aria-selected", String(on));
                button.tabIndex = on ? 0 : -1;
            });
            panels.querySelectorAll("[role=tabpanel]").forEach((element) => { element.hidden = element.dataset.tab !== key; });
            onChange?.(key);
        };
        entries.forEach(([key, label, content]) => {
            const id = `tab-${key}-${Math.random().toString(36).slice(2, 7)}`;
            list.append(h("button", { className: "tab-button", type: "button", role: "tab", id, dataset: { tab: key }, onclick: () => select(key) }, label));
            panels.append(h("div", { role: "tabpanel", "aria-labelledby": id, dataset: { tab: key } }, content));
        });
        list.addEventListener("keydown", (event) => {
            if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
            const buttons = [...list.querySelectorAll("button")];
            const index = buttons.indexOf(document.activeElement);
            const next = buttons[(index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length];
            next.focus();
            next.click();
        });
        select(active);
        return h("div", { className: "tabs" }, list, panels);
    }

    // ================================================================ payments

    function renderPayments() {
        const body = viewBody();
        if (!body) return;
        const f = filters.payments;
        const cards = h("section", { className: "stats-grid", "aria-label": "Payment summary" }, loadingBlock());
        const tableHost = h("div");
        const countLabel = h("span", { className: "table-secondary" });
        const methodSelect = selectInput([["", "All methods"]], f.method, (value) => apply({ method: value }));
        const serviceSelect = selectInput(serviceOptions(), f.service, (value) => apply({ service: value }));
        let balances = [];
        const table = new UI.DataTable(tableHost, {
            columns: [
                { label: "Date received", sortKey: "received", type: "date", render: (row) => dateTime(row.received_at) },
                { label: "Order ID", sortKey: "reference", className: "ref-cell", render: (row) => cellPair(row.reference, `Payment #${row.payment_number}`) },
                { label: "Customer", sortKey: "customer", render: (row) => cellPair(row.customer_name, row.customer_email) },
                { label: "Service / product", sortKey: "service", render: (row) => cellPair(row.product, row.service) },
                { label: "Method", sortKey: "method", render: (row) => row.method },
                { label: "Reference", render: (row) => row.payment_reference || "—" },
                { label: "Amount", sortKey: "amount", align: "right", render: (row) => h("strong", { text: money(row.amount) }) },
                { label: "Order payment", sortKey: "payment_status", render: (row) => badge(row.order_payment_status) },
            ],
            defaultSort: { key: "received", dir: "desc" },
            initialQuery: { ...f },
            fetch: (params) => api(`/api/admin/payments${query(params)}`),
            onLoad: (result) => {
                const s = result.summary;
                balances = s.orders_with_balance;
                cards.replaceChildren(
                    statCard({ label: "Total payments received", value: money(s.total_received), foot: "All recorded payments", icon: "₱" }),
                    statCard({ label: "Outstanding balance", value: money(s.outstanding_balance), foot: `${s.orders_with_balance.length} active order${s.orders_with_balance.length === 1 ? "" : "s"} with a balance`, icon: "◷" }),
                    statCard({ label: "Fully paid orders", value: number(s.fully_paid_orders), foot: "Payments cover the confirmed total", icon: "✓" }),
                    statCard({ label: "Awaiting quotation", value: number(s.awaiting_quotation), foot: "No confirmed total yet", icon: "⌕" }),
                );
                countLabel.textContent = `${number(s.filtered_count)} payment${s.filtered_count === 1 ? "" : "s"} · ${money(s.filtered_amount)}`;
                if (methodSelect.options.length <= 1 && result.methods.length) {
                    result.methods.forEach((method) => methodSelect.add(new Option(method, method)));
                    methodSelect.value = f.method;
                }
                if (result.services?.length && serviceSelect.options.length <= 1) {
                    rememberServices(result.services);
                    serviceSelect.replaceChildren(...serviceOptions().map(([value, text]) => new Option(text, value)));
                    serviceSelect.value = f.service;
                }
            },
            emptyMessage: "No payments have been recorded yet.",
            onRowClick: (row) => openOrder(row.reference, { focus: "payments" }),
            rowTitle: (row) => `View transaction details for ${row.reference}`,
        });
        const apply = (values) => {
            Object.assign(f, values);
            table.setQuery({ ...f });
        };
        const recordButton = h("button", { className: "button button-primary", type: "button", text: "＋ Record a payment", onclick: () => openBalancePicker(balances) });
        body.replaceChildren(
            h("div", { className: "page-actions-row" }, recordButton),
            cards,
            h("div", { className: "filter-bar" },
                searchInput("Search order ID, customer, payment reference…", f.search, (value) => apply({ search: value })),
                field("Method", methodSelect),
                field("Service", serviceSelect),
                field("Order payment status", selectInput([["", "All"], ...PAYMENT_STATUSES.slice(0, 3).map((value) => [value, value])], f.payment_status, (value) => apply({ payment_status: value }))),
                field("Received from", dateInput(f.date_from, (value) => apply({ date_from: value }))),
                field("Received to", dateInput(f.date_to, (value) => apply({ date_to: value }))),
                h("button", { className: "button button-small", type: "button", text: "Clear filters", onclick: () => {
                    Object.keys(f).forEach((key) => { f[key] = ""; });
                    renderPayments();
                } }),
            ),
            panel(["Payment transactions ", countLabel], "Payments are recorded from an order's details. Select a row to view the transaction and its order.", tableHost,
                UI.exportButtons("payments", () => ({ ...f, sort: table.sort.key, dir: table.sort.dir }))),
        );
        table.load();
        refreshActiveView = () => table.reload();
    }

    function openBalancePicker(rows) {
        const host = h("div");
        UI.openModal(h("div", {},
            h("h2", { className: "dialog-heading", text: "Record a payment" }),
            h("p", { className: "dialog-subtitle", text: "Choose the order the payment is for. Its details open with the payment form." }),
            host,
        ), { wide: true });
        new UI.DataTable(host, {
            columns: [
                { label: "Order ID", sortKey: "reference", render: (row) => row.reference },
                { label: "Customer", sortKey: "customer_name", render: (row) => cellPair(row.customer_name, row.product) },
                { label: "Status", sortKey: "status", render: (row) => badge(row.status) },
                { label: "Balance", sortKey: "balance", align: "right", render: (row) => h("strong", { text: money(row.balance) }) },
            ],
            rows: () => [...rows],
            defaultSort: { key: "balance", dir: "desc" },
            pageSize: 10,
            emptyMessage: "No quoted orders have an outstanding balance.",
            onRowClick: (row) => openOrder(row.reference, { focus: "payment", back: () => openBalancePicker(rows) }),
        }).load();
    }

    // ================================================================ reports

    function renderReports() {
        const body = viewBody();
        if (!body) return;
        const requested = new URLSearchParams(window.location.search).get("tab");
        if (requested && ["summary", "completed", "services"].includes(requested)) filters.reports.tab = requested;
        const summaryHost = h("div");
        const completedHost = h("div");
        const servicesHost = h("div");
        const rendered = new Set();
        const show = (key) => {
            filters.reports.tab = key;
            if (rendered.has(key)) return;
            rendered.add(key);
            ({ summary: renderSummaryTab, completed: renderCompletedTab, services: renderServiceTab })[key](
                { summary: summaryHost, completed: completedHost, services: servicesHost }[key],
            );
        };
        body.replaceChildren(tabSet([
            ["summary", "Business summary & revenue", summaryHost],
            ["completed", "Completed transactions", completedHost],
            ["services", "Service reports", servicesHost],
        ], filters.reports.tab, show));
        refreshActiveView = () => {
            rendered.clear();
            renderReports();
        };
    }

    function renderSummaryTab(host) {
        const f = filters.reports.summary;
        const content = h("div", {}, loadingBlock("Calculating…"));
        const fromInput = dateInput(f.date_from, (value) => { f.date_from = value; if (f.date_to) load(); });
        const toInput = dateInput(f.date_to, (value) => { f.date_to = value; if (f.date_from) load(); });
        const customFields = h("div", { className: "filter-inline" }, field("From", fromInput), field("To", toInput));
        const serviceSelect = selectInput(serviceOptions(), f.service, (value) => { f.service = value; load(); });
        const presetSelect = selectInput([
            ["all", "All time"], ["today", "Today"], ["week", "This week"], ["month", "This month"], ["year", "This year"], ["custom", "Custom date range"],
        ], f.preset, (value) => {
            f.preset = value;
            f.group_by = "";
            groupSelect.value = "";
            customFields.hidden = value !== "custom";
            if (value !== "custom" || (f.date_from && f.date_to)) load();
        });
        const groupSelect = selectInput([["", "Automatic"], ["day", "Daily"], ["week", "Weekly"], ["month", "Monthly"], ["year", "Yearly"]], f.group_by, (value) => { f.group_by = value; load(); });
        customFields.hidden = f.preset !== "custom";
        const params = () => ({ ...f });
        host.replaceChildren(
            h("div", { className: "filter-bar report-filters" },
                field("Report period", presetSelect), customFields,
                field("Revenue grouping", groupSelect),
                field("Service", serviceSelect),
                h("div", { className: "export-group" },
                    h("span", { className: "export-label", text: "Business summary" }), UI.exportButtons("summary", params, { label: "" })),
                h("div", { className: "export-group" },
                    h("span", { className: "export-label", text: "Revenue report" }), UI.exportButtons("revenue", params, { label: "" })),
            ),
            content,
        );
        host.querySelectorAll(".export-buttons button").forEach((button) => { button.textContent = button.textContent.trim(); });

        let requestId = 0;
        async function load() {
            const id = ++requestId;
            content.classList.add("is-refreshing");
            if (!content.childElementCount) content.append(loadingBlock("Calculating…"));
            let data;
            try {
                data = await api(`/api/admin/reports/summary${query(params())}`);
            } catch (error) {
                if (id === requestId) content.replaceChildren(errorBlock(error.message, load));
                return;
            } finally {
                if (id === requestId) content.classList.remove("is-refreshing");
            }
            if (id !== requestId) return;
            rememberServices(data.services);
            if (serviceSelect.options.length <= 1) {
                serviceSelect.replaceChildren(...serviceOptions().map(([value, text]) => new Option(text, value)));
                serviceSelect.value = f.service;
            }
            groupSelect.value = f.group_by;
            content.replaceChildren(...summaryContent(data));
        }
        load();
    }

    function summaryContent(data) {
        const t = data.totals;
        const group = data.period.group_by;
        const groupName = { day: "Daily", week: "Weekly", month: "Monthly", year: "Yearly" }[group];
        const cards = h("section", { className: "stats-grid stats-grid-8" },
            statCard({ label: "Total customers", value: number(t.total_customers), foot: `${number(t.new_customers)} registered in this period`, icon: "👥" }),
            statCard({ label: "Total orders", value: number(t.total_orders), foot: "Requested in this period", icon: "▤" }),
            statCard({ label: "Completed orders", value: number(t.completed_orders), foot: "Completed in this period", icon: "✓" }),
            statCard({ label: "Pending orders", value: number(t.pending_orders), foot: `${number(t.processing_orders)} more in progress`, icon: "◷" }),
            statCard({ label: "Cancelled orders", value: number(t.cancelled_orders), foot: "Cancelled or rejected", icon: "✕" }),
            statCard({ label: "Fully paid orders", value: number(t.fully_paid_orders), foot: "Requested in this period", icon: "₱" }),
            statCard({ label: "Total confirmed revenue", value: money(t.confirmed_revenue), foot: "Completed and fully paid", icon: "₱", tone: "highlight" }),
            statCard({ label: "Avg. completion time", value: t.average_completion_days === null ? "—" : days(t.average_completion_days), foot: "Calendar days, request to completion", icon: "⏱" }),
        );
        const trendChart = h("div", { className: "chart-box" });
        UI.columnChart(trendChart, data.trend.map((bucket) => ({
            label: bucket.label, short: shortBucket(bucket.key, group), value: bucket.revenue, completed: bucket.completed_orders,
        })), {
            format: money, axisFormat: UI.compactMoney, emptyMessage: "No confirmed revenue in this period.",
            detail: (point) => `${point.completed} completed order${point.completed === 1 ? "" : "s"}`,
        });
        const revenueChart = h("div", { className: "chart-box" });
        UI.barChart(revenueChart, data.by_service.map((item) => ({ label: item.service, value: item.revenue, share: item.revenue_share })), {
            format: money, emptyMessage: "No confirmed revenue in this period.", detail: (item) => `${item.share}% of revenue`,
        });
        const completedChart = h("div", { className: "chart-box" });
        UI.barChart(completedChart, data.by_service.map((item) => ({ label: item.service, value: item.completed_orders })), { emptyMessage: "No orders were completed in this period." });

        const comparisonHost = h("div");
        new UI.DataTable(comparisonHost, {
            columns: [
                { label: "Service", sortKey: "service", render: (row) => h("strong", { text: row.service }) },
                { label: "Orders requested", sortKey: "total_orders", align: "right", render: (row) => number(row.total_orders) },
                { label: "Completed", sortKey: "completed_orders", align: "right", render: (row) => number(row.completed_orders) },
                { label: "Completed & fully paid", sortKey: "fully_paid_completed", align: "right", render: (row) => number(row.fully_paid_completed) },
                { label: "Confirmed revenue", sortKey: "revenue", align: "right", render: (row) => h("strong", { text: money(row.revenue) }) },
                { label: "Share", sortKey: "revenue_share", align: "right", render: (row) => `${row.revenue_share.toFixed(1)}%` },
                { label: "Avg. completion", sortKey: "average_completion_days", align: "right", render: (row) => days(row.average_completion_days) },
            ],
            rows: () => [...data.by_service],
            defaultSort: { key: "revenue", dir: "desc" },
            pageSize: 25,
            emptyMessage: "No services in the catalog.",
            onRowClick: (row) => {
                filters.reports.service.service = row.service_id;
                filters.reports.tab = "services";
                renderReports();
            },
            rowTitle: (row) => `Open the ${row.service} service report`,
        }).load();
        const trendHost = h("div");
        new UI.DataTable(trendHost, {
            columns: [
                { label: "Period", sortKey: "key", render: (row) => row.label },
                { label: "Completed orders", sortKey: "completed_orders", align: "right", render: (row) => number(row.completed_orders) },
                { label: "Completed & fully paid", sortKey: "fully_paid_completed", align: "right", render: (row) => number(row.fully_paid_completed) },
                { label: "Confirmed revenue", sortKey: "revenue", align: "right", render: (row) => money(row.revenue) },
            ],
            rows: () => [...data.trend],
            defaultSort: { key: "key", dir: "desc" },
            pageSize: 10,
            emptyMessage: "No periods to show.",
        }).load();
        const transactionsHost = h("div");
        new UI.DataTable(transactionsHost, {
            columns: [
                { label: "Completed", sortKey: "completed_at", type: "date", render: (row) => date(row.completion_date) },
                { label: "Transaction ID", sortKey: "reference", className: "ref-cell", render: (row) => row.reference },
                { label: "Customer", sortKey: "customer_name", render: (row) => row.customer_name },
                { label: "Service / product", sortKey: "service", render: (row) => cellPair(row.product, row.service) },
                { label: "Revenue", sortKey: "confirmed_revenue", align: "right", render: (row) => money(row.confirmed_revenue) },
            ],
            rows: () => [...data.revenue_transactions],
            defaultSort: { key: "completed_at", dir: "desc" },
            pageSize: 10,
            emptyMessage: "No completed and fully paid transactions in this period.",
            onRowClick: (row) => openOrder(row.reference),
        }).load();

        return [
            h("p", { className: "report-period-note" }, h("strong", { text: data.period.label }),
                data.service ? ` · ${data.service.label}` : " · All services",
                " · Orders are counted by request date; completed orders and revenue by completion date. Revenue includes only completed and fully paid orders, each counted once."),
            cards,
            h("div", { className: "dashboard-grid" },
                panel(`Revenue trend (${groupName.toLowerCase()})`, data.period.label, trendChart),
                panel("Revenue per service", "Confirmed revenue in this period", revenueChart),
                panel("Completed orders per service", "Orders completed in this period", completedChart),
                panel(`Revenue by period (${groupName.toLowerCase()})`, "Table view of the revenue trend", trendHost),
            ),
            panel("Service performance comparison", "Select a service to open its report", comparisonHost),
            panel("Revenue transactions", "Completed and fully paid orders in this period · select a row for details", transactionsHost),
        ];
    }

    function completedColumns() {
        return [
            { label: "Transaction ID", sortKey: "reference", className: "ref-cell", render: (row) => h("span", { className: "table-primary", text: row.reference }) },
            { label: "Customer", sortKey: "customer", render: (row) => row.customer_name },
            { label: "Service", sortKey: "service", render: (row) => row.service },
            { label: "Product / description", sortKey: "description", className: "wrap-cell", render: (row) => row.description },
            { label: "Total amount", sortKey: "total", align: "right", render: (row) => money(row.total_amount) },
            { label: "Amount paid", sortKey: "paid", align: "right", render: (row) => money(row.amount_paid) },
            { label: "Request date", sortKey: "request_date", type: "date", render: (row) => date(row.request_date) },
            { label: "Completion date", sortKey: "completion_date", type: "date", render: (row) => date(row.completion_date) },
            { label: "Duration", sortKey: "days", align: "right", render: (row) => days(row.completion_days) },
            { label: "Payment status", sortKey: "payment_status", render: (row) => badge(row.payment_status) },
        ];
    }

    function completedSummaryCards(host, s) {
        host.replaceChildren(
            statCard({ label: "Completed transactions", value: number(s.completed_orders), foot: `${number(s.fully_paid_orders)} fully paid`, icon: "✓" }),
            statCard({ label: "Total order amount", value: money(s.total_order_amount), foot: `Paid ${money(s.total_amount_paid)}`, icon: "▤" }),
            statCard({ label: "Confirmed revenue", value: money(s.confirmed_revenue), foot: "Completed and fully paid only", icon: "₱", tone: "highlight" }),
            statCard({ label: "Avg. completion time", value: s.average_completion_days === null ? "—" : days(s.average_completion_days), foot: s.fastest_completion_days === null ? "Calendar days" : `Fastest ${days(s.fastest_completion_days)} · slowest ${days(s.slowest_completion_days)}`, icon: "⏱" }),
        );
    }

    function renderCompletedTab(host) {
        const f = filters.reports.completed;
        const cards = h("section", { className: "stats-grid" }, loadingBlock());
        const tableHost = h("div");
        const countLabel = h("span", { className: "table-secondary" });
        const serviceSelect = selectInput(serviceOptions(), f.service, (value) => apply({ service: value }));
        const table = new UI.DataTable(tableHost, {
            columns: completedColumns(),
            defaultSort: { key: "completion_date", dir: "desc" },
            initialQuery: { ...f },
            fetch: (params) => api(`/api/admin/reports/completed${query(params)}`),
            onLoad: (result) => {
                completedSummaryCards(cards, result.summary);
                countLabel.textContent = `${number(result.total)} transaction${result.total === 1 ? "" : "s"}`;
                if (result.services?.length && serviceSelect.options.length <= 1) {
                    rememberServices(result.services);
                    serviceSelect.replaceChildren(...serviceOptions().map(([value, text]) => new Option(text, value)));
                    serviceSelect.value = f.service;
                }
            },
            className: "report-table",
            emptyMessage: "No orders have been completed yet.",
            onRowClick: (row) => openOrder(row.reference),
            rowTitle: (row) => `Open transaction ${row.reference}`,
        });
        const apply = (values) => {
            Object.assign(f, values);
            table.setQuery({ ...f });
        };
        host.replaceChildren(
            h("div", { className: "filter-bar report-filters" },
                field("Service", serviceSelect),
                field("Customer", (() => { const input = h("input", { type: "search", placeholder: "Name or email", value: f.customer }); input.addEventListener("input", UI.debounce(() => apply({ customer: input.value.trim() }), 300)); return input; })()),
                field("Payment status", selectInput([["", "All"], ...PAYMENT_STATUSES.slice(0, 3).map((value) => [value, value])], f.payment_status, (value) => apply({ payment_status: value }))),
                field("Requested from", dateInput(f.request_from, (value) => apply({ request_from: value }))),
                field("Requested to", dateInput(f.request_to, (value) => apply({ request_to: value }))),
                field("Completed from", dateInput(f.completed_from, (value) => apply({ completed_from: value }))),
                field("Completed to", dateInput(f.completed_to, (value) => apply({ completed_to: value }))),
                field("Min. days", numberInput(f.min_days, (value) => apply({ min_days: value }), "0"), "narrow"),
                field("Max. days", numberInput(f.max_days, (value) => apply({ max_days: value }), "Any"), "narrow"),
                h("button", { className: "button button-small", type: "button", text: "Clear filters", onclick: () => {
                    Object.keys(f).forEach((key) => { f[key] = ""; });
                    renderCompletedTab(host);
                } }),
            ),
            cards,
            panel(["Completed transactions ", countLabel], "Duration = completion date − request date, in calendar days (weekends included). Select a row for the full transaction.", tableHost,
                UI.exportButtons("completed", () => ({ ...f, sort: table.sort.key, dir: table.sort.dir }))),
        );
        table.load();
    }

    function renderServiceTab(host) {
        const f = filters.reports.service;
        const cardsHost = h("div", { className: "service-cards" }, loadingBlock());
        const detailHost = h("div");
        let table = null;
        const dateParams = () => ({ completed_from: f.completed_from, completed_to: f.completed_to, request_from: f.request_from, request_to: f.request_to });
        async function loadCards() {
            let data;
            try {
                data = await api(`/api/admin/reports/services${query(dateParams())}`);
            } catch (error) {
                cardsHost.replaceChildren(errorBlock(error.message, loadCards));
                return;
            }
            rememberServices(data.services);
            if (!f.service && data.summaries.length) f.service = data.summaries[0].service_id;
            cardsHost.replaceChildren(...data.summaries.map((item) => h("button", {
                className: `service-card${item.service_id === f.service ? " active" : ""}`,
                type: "button",
                "aria-pressed": String(item.service_id === f.service),
                onclick: () => { f.service = item.service_id; loadCards(); },
            },
            h("strong", { text: item.service }),
            h("span", { className: "service-card-revenue", text: money(item.confirmed_revenue) }),
            h("span", { className: "table-secondary", text: `${number(item.completed_orders)} completed · ${number(item.fully_paid_orders)} fully paid` }),
            h("span", { className: "table-secondary", text: `Avg. ${days(item.average_completion_days)}` }))));
            renderDetail(data.summaries.find((item) => item.service_id === f.service));
        }
        function renderDetail(summary) {
            if (!summary) {
                detailHost.replaceChildren(h("p", { className: "empty-state", text: "Select a service to see its report." }));
                return;
            }
            const cards = h("section", { className: "stats-grid" });
            const tableHost = h("div");
            cards.append(
                statCard({ label: "Total completed orders", value: number(summary.completed_orders), foot: `${number(summary.total_orders)} orders in total for this service`, icon: "✓" }),
                statCard({ label: "Total fully paid orders", value: number(summary.fully_paid_orders), foot: "Among completed orders", icon: "₱" }),
                statCard({ label: "Total revenue", value: money(summary.confirmed_revenue), foot: "Completed and fully paid", icon: "₱", tone: "highlight" }),
                statCard({ label: "Avg. completion duration", value: days(summary.average_completion_days), foot: "Calendar days", icon: "⏱" }),
            );
            table = new UI.DataTable(tableHost, {
                columns: completedColumns().filter((column) => column.sortKey !== "service"),
                defaultSort: { key: "completion_date", dir: "desc" },
                initialQuery: { ...dateParams(), service: summary.service_id },
                fetch: (params) => api(`/api/admin/reports/services${query(params)}`),
                className: "report-table",
                emptyMessage: `No completed ${summary.service} transactions yet.`,
                filteredEmptyMessage: `No completed ${summary.service} transactions in the selected dates.`,
                onRowClick: (row) => openOrder(row.reference),
            });
            detailHost.replaceChildren(
                h("h2", { className: "section-title", text: `${summary.service} report` }),
                cards,
                panel(`${summary.service} — completed transactions`, "Only transactions for this service are listed.", tableHost,
                    UI.exportButtons("service", () => ({ ...dateParams(), service: summary.service_id, sort: table.sort.key, dir: table.sort.dir }))),
            );
            table.load();
        }
        const apply = (values) => {
            Object.assign(f, values);
            loadCards();
        };
        host.replaceChildren(
            h("div", { className: "filter-bar report-filters" },
                field("Completed from", dateInput(f.completed_from, (value) => apply({ completed_from: value }))),
                field("Completed to", dateInput(f.completed_to, (value) => apply({ completed_to: value }))),
                field("Requested from", dateInput(f.request_from, (value) => apply({ request_from: value }))),
                field("Requested to", dateInput(f.request_to, (value) => apply({ request_to: value }))),
                h("div", { className: "export-group" }, h("span", { className: "export-label", text: "All services (one sheet each)" }),
                    UI.exportButtons("service", () => ({ ...dateParams(), service: "all" }), { label: "" })),
            ),
            h("p", { className: "field-note", text: "Select a service. Each report lists only that service's completed transactions." }),
            cardsHost,
            detailHost,
        );
        host.querySelectorAll(".export-buttons button").forEach((button) => { button.textContent = button.textContent.trim(); });
        loadCards();
    }

    // ================================================================ notifications

    function renderNotifications() {
        const body = viewBody();
        if (!body) return;
        const f = filters.notifications;
        const list = h("section", { className: "notification-list", "aria-label": "Activity notifications" }, loadingBlock());
        const description = viewRoot.querySelector("[data-notifications-description]");
        const seenAt = localStorage.getItem(NOTIFICATION_SEEN_KEY) || "";
        const chips = h("div", { className: "segmented" }, ...[["", "All activity"], ["customer", "Customer activity"], ["admin", "Admin actions"]].map(([value, label]) => h("button", {
            className: `segmented-button${f.actor === value ? " active" : ""}`, type: "button", onclick: () => { f.actor = value; f.limit = 30; renderNotifications(); },
        }, label)));
        const more = h("button", { className: "button", type: "button", text: "Show more", hidden: true, onclick: () => { f.limit += 30; load(); } });
        const markAll = h("button", { className: "button", type: "button", text: "Mark all as read", onclick: () => {
            localStorage.setItem(NOTIFICATION_SEEN_KEY, new Date().toISOString().replace(/\.\d+Z$/, "+00:00"));
            list.querySelectorAll(".notification-item.unread").forEach((item) => item.classList.remove("unread"));
            refreshBadges();
            toast("All notifications marked as read.", "success");
        } });
        body.replaceChildren(h("div", { className: "notifications-toolbar" }, chips, markAll), list, h("div", { className: "center-row" }, more));
        async function load() {
            let data;
            try {
                data = await api(`/api/admin/activity${query({ actor: f.actor, limit: f.limit })}`);
            } catch (error) {
                list.replaceChildren(errorBlock(error.message, load));
                return;
            }
            const unread = data.events.filter((event) => event.actor === "customer" && (event.at || "") > seenAt).length;
            if (description) description.textContent = `${number(data.total)} events from real orders and registrations · ${number(unread)} new customer update${unread === 1 ? "" : "s"} since you last marked them read.`;
            if (!data.events.length) {
                list.replaceChildren(h("p", { className: "empty-state", text: "No activity yet." }));
            } else {
                list.replaceChildren(...data.events.map((event) => {
                    const symbol = event.kind === "customer" ? "♙" : event.title.includes("Payment") ? "₱" : event.actor === "customer" ? "✉" : "▤";
                    const item = h("button", {
                        className: `notification-item${event.actor === "customer" && (event.at || "") > seenAt ? " unread" : ""}`,
                        type: "button",
                        onclick: () => (event.reference ? openOrder(event.reference) : event.customer_id ? openCustomer(event.customer_id) : null),
                    },
                    h("span", { className: "notification-symbol", "aria-hidden": "true", text: symbol }),
                    h("div", {},
                        h("h2", { text: event.title }),
                        h("p", { text: event.body }),
                        h("p", { className: "table-secondary", text: [event.reference, event.customer_name].filter(Boolean).join(" · ") })),
                    h("time", { datetime: event.at, title: dateTime(event.at), text: UI.relativeTime(event.at) }));
                    return item;
                }));
            }
            more.hidden = data.events.length >= data.total;
        }
        load();
        refreshActiveView = load;
    }

    // ================================================================ settings

    function renderSettings() {
        const body = viewBody();
        if (!body) return;
        const compact = localStorage.getItem(COMPACT_KEY) === "true";
        const toggle = h("button", { className: "settings-toggle", type: "button", "aria-pressed": String(compact), "aria-label": "Compact tables" });
        toggle.addEventListener("click", () => {
            const next = toggle.getAttribute("aria-pressed") !== "true";
            toggle.setAttribute("aria-pressed", String(next));
            localStorage.setItem(COMPACT_KEY, String(next));
            document.body.classList.toggle("compact-tables", next);
            toast(next ? "Compact tables turned on." : "Compact tables turned off.", "success");
        });
        const activity = h("div", { className: "activity-list" }, loadingBlock());
        body.replaceChildren(
            h("section", { className: "split-grid" },
                panel("Administrator account", "Signed-in administrator", h("div", { className: "settings-list" },
                    h("div", { className: "settings-row" }, h("div", {}, h("strong", { text: "Email" }), h("p", { "data-admin-email": "", text: adminEmail || "Loading…" }))),
                    h("div", { className: "settings-row" }, h("div", {}, h("strong", { text: "Session" }), h("p", { text: "Sessions end after 12 hours or when you sign out." })),
                        h("button", { className: "button button-danger-strong", type: "button", dataset: { action: "logout" }, text: "Sign out" })),
                )),
                panel("Display preferences", "Saved in this browser", h("div", { className: "settings-list" },
                    h("div", { className: "settings-row" }, h("div", {}, h("strong", { text: "Compact tables" }), h("p", { text: "Use tighter row spacing in management tables." })), toggle),
                )),
            ),
            panel("How figures are calculated", "Rules used by the dashboard, reports, customer totals, and exports", h("ul", { className: "rules-list" },
                h("li", { text: "Confirmed revenue includes only orders that are Completed and fully paid. Each order counts once, for its confirmed total, on the date it was completed." }),
                h("li", { text: "Pending payments, partially paid, unpaid, cancelled, and rejected orders are never counted as revenue." }),
                h("li", { text: "A customer's amount spent uses the same rule, so customer totals add up to revenue." }),
                h("li", { text: "Completion duration = completion date − request date, in calendar days (weekends included), using the stored timestamps on Philippine time." }),
                h("li", { text: "An order can be marked Completed only after its full amount is recorded as paid." }),
            )),
            panel("Recent administrator activity", "Actions recorded in order histories", activity),
        );
        api(`/api/admin/activity${query({ actor: "admin", limit: 15 })}`).then((data) => {
            activity.replaceChildren(...(data.events.length ? data.events.map((event) => h("div", { className: "activity-item" },
                h("span", { className: "activity-point" }),
                h("div", {}, h("p", { text: `${event.reference}: ${event.body}` }), h("time", { text: dateTime(event.at) })))) : [h("p", { className: "empty-state", text: "No administrator actions recorded yet." })]));
        }).catch((error) => activity.replaceChildren(errorBlock(error.message)));
    }

    // ================================================================ global events

    document.addEventListener("click", async (event) => {
        const viewLink = event.target.closest("[data-view]");
        if (viewLink) {
            event.preventDefault();
            await navigate(viewLink.dataset.view);
            return;
        }
        const action = event.target.closest("[data-action]");
        if (!action) return;
        switch (action.dataset.action) {
            case "menu": {
                const sidebar = document.querySelector("#admin-sidebar");
                const open = !sidebar.classList.contains("open");
                sidebar.classList.toggle("open", open);
                action.setAttribute("aria-expanded", String(open));
                action.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
                break;
            }
            case "logout": {
                const answer = await UI.confirm({ title: "Sign out?", message: "You will need to sign in again to use the admin dashboard.", confirmLabel: "Sign out" });
                if (!answer) return;
                try {
                    const response = await fetch("/api/admin/session", { method: "DELETE", credentials: "same-origin" });
                    if (!response.ok) throw new Error("The administrator session could not be cleared.");
                } catch (error) {
                    toast(error.message, "error");
                    return;
                }
                window.location.assign("/admin/login");
                break;
            }
            case "close-dialog":
                UI.closeModal();
                break;
            case "refresh-view":
                renderView();
                refreshBadges();
                toast("Figures refreshed from the database.", "success");
                break;
            default:
                break;
        }
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") document.querySelector("#admin-sidebar")?.classList.remove("open");
    });
    dialog.addEventListener("close", () => {
        dialog.classList.remove("admin-dialog-wide", "admin-dialog-xl");
        document.querySelector("#dialog-content").replaceChildren();
    });
    // Clicking the dimmed backdrop closes the details modal.
    dialog.addEventListener("click", (event) => {
        if (event.target === dialog) UI.closeModal();
    });
    window.addEventListener("popstate", () => navigate(viewFromLocation(), false));

    document.body.classList.toggle("compact-tables", localStorage.getItem(COMPACT_KEY) === "true");
    document.querySelector("#today-date").textContent = new Date().toLocaleDateString("en-PH", { weekday: "short", month: "short", day: "numeric", timeZone: "Asia/Manila" });
    activeView = document.body.dataset.adminView || viewFromLocation();
    renderView();
    refreshBadges();
    window.setInterval(refreshBadges, 60000);

    // Deep links: /admin/orders?order=IPS-11-08-26 or /admin/customers?customer=<id>
    const params = new URLSearchParams(window.location.search);
    if (params.get("order")) openOrder(params.get("order"));
    else if (params.get("customer")) openCustomer(params.get("customer"));

    window.PrintShopAdmin = { openOrder, openCustomer, navigate };
})();
