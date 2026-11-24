"use strict";

/**
 * ImPRESS Print Works admin application.
 * Every figure, table, and action uses the backend API; nothing on these pages is sample data.
 * Shared building blocks (tables, charts, image viewer, dialogs) live in admin-core.js.
 */
(() => {
    const UI = window.AdminUI;
    const { h, api, query, money, date, dateTime, days, number, badge, slug, toast } = UI;

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
        production: "Production Monitoring",
        fulfillment: "Fulfillment Management",
        refunds: "Cancellation & Refunds",
        customers: "Customer Management",
        services: "Services & Products",
        payments: "Payments & Transactions",
        reports: "Sales & Reports",
        notifications: "Notifications",
        chatbot: "AI Chatbot",
        settings: "Settings",
    };
    const STATUSES = [
        "Pending Confirmation", "Under Review", "Awaiting Quotation", "Quotation Sent", "Quotation Declined",
        "Awaiting COD Confirmation", "Awaiting Down Payment", "Confirmed",
        "Draft Preparation", "Awaiting Customer Approval", "Approved for Printing",
        "In Progress", "Ready for Customer Review", "Revision Requested", "Approved by Customer", "Awaiting Final Payment",
        "In Production", "Quality Checking", "Packaging", "Awaiting Full Payment",
        "Ready for Pickup", "Ready for Dispatch", "Out for Delivery", "Delivery Failed", "Delivered", "Order Claimed",
        "Completed", "Rejected", "Cancelled",
    ];
    const STATUS_GROUPS = [
        ["", "All orders", STATUSES],
        ["pending", "Pending", ["Pending Confirmation", "Under Review", "Awaiting Quotation", "Quotation Sent", "Quotation Declined", "Awaiting COD Confirmation", "Awaiting Down Payment"]],
        ["processing", "Active (confirmed)", ["Confirmed", "Draft Preparation", "Awaiting Customer Approval", "Approved for Printing", "In Progress", "Ready for Customer Review", "Revision Requested", "Approved by Customer", "Awaiting Final Payment", "In Production", "Quality Checking", "Packaging", "Awaiting Full Payment", "Ready for Pickup", "Ready for Dispatch", "Out for Delivery", "Delivery Failed", "Delivered", "Order Claimed"]],
        ["completed", "Completed", ["Completed"]],
        ["cancelled", "Cancelled / rejected", ["Rejected", "Cancelled"]],
    ];
    // Workflow stages used by the dashboard cards (server: report_services.DASHBOARD_STAGES).
    const WORKFLOW_STAGES = [
        ["to_review", "Orders to review (stage)"],
        ["awaiting_customer", "Waiting for the customer (stage)"],
        ["production", "In production (stage)"],
        ["release", "Ready for pickup / delivery (stage)"],
    ];
    const PAYMENT_STATUSES = ["Not Quoted", "No Payment", "Insufficient Down Payment", "Down Payment Paid", "Partially Paid", "Fully Paid", "Excess Payment"];
    const VERIFICATION_FILTERS = [
        ["", "All verification statuses"],
        ["verified", "Verified"],
        ["ocr_checked_pending", "OCR-checked · pending manual verification"],
        ["insufficient_payment", "Insufficient Payment"],
        ["reference_mismatch", "Reference Number Mismatch"],
        ["duplicate_reference", "Duplicate Reference Number"],
        ["needs_manual_review", "Needs Manual Review"],
        ["pending", "Pending Verification"],
        ["rejected", "Rejected"],
        ["missing", "Reference Missing"],
    ];
    const PAYMENT_OPTIONS = [["down_payment", "50% Down Payment"], ["cod", "Cash on Delivery (COD)"]];
    const CUSTOMER_PAYMENT_STATUSES = ["With balance", "Fully paid", "Awaiting quotation", "No transactions"];
    const ACTION_LABELS = {
        "Under Review": "Approve request",
        "Awaiting Quotation": "Mark as awaiting quotation",
        "Draft Preparation": "Start draft preparation",
        "Rejected": "Reject request",
        "Cancelled": "Cancel order",
        "In Progress": "Mark as in progress",
        "Ready for Customer Review": "Send work for review",
        "Revision Requested": "Mark revision requested",
        "Approved by Customer": "Mark approved by customer",
        "Awaiting Final Payment": "Await final payment",
        "In Production": "Start production",
        "Quality Checking": "Send to quality checking",
        "Ready for Pickup": "Mark ready for pickup",
        "Out for Delivery": "Mark out for delivery",
        "Completed": "Mark as completed",
    };
    const ACTION_HELP = {
        "Under Review": "Accept the request and start reviewing its details.",
        "Awaiting Quotation": "Use when the price needs more time to work out.",
        "Draft Preparation": "The down payment is verified; start preparing the design draft.",
        "Rejected": "The customer sees the request as rejected.",
        "Cancelled": "Stops the order. Orders with recorded payments cannot be cancelled here.",
        "In Progress": "The confirmed work is being prepared. Send the finished work for review when it is ready.",
        "Ready for Customer Review": "The customer can approve the finished work or request a revision.",
        "Revision Requested": "The customer asked for changes; send an updated preview to continue.",
        "Approved by Customer": "The customer approved the work, so the remaining balance can be paid.",
        "Awaiting Final Payment": "The finished work is approved; collect the remaining balance before release.",
        "In Production": "The customer approved the draft; printing starts.",
        "Quality Checking": "Printing is finished; inspect the products before packaging.",
        "Ready for Pickup": "Printing is done and the order can be collected at the shop.",
        "Out for Delivery": "Printing is done and the order is on its way to the customer's delivery address.",
        "Completed": "Records today as the completion date. The order must be fully paid.",
    };
    const OPS = () => window.PrintShopOperationsAdmin;
    /** Shared builders passed to operations-admin.js (Production, Fulfillment, Cancellation & Refunds, settings). */
    function opsContext() {
        return {
            panel, statCard, loadingBlock, errorBlock, field, searchInput, selectInput, dateInput, detailList, cellPair,
            openOrder, submitAction, refreshBadges, showOrders,
            setRefresh: (fn) => { refreshActiveView = fn; },
        };
    }

    function renderOperations(view) {
        const body = viewBody();
        if (!body) return;
        if (!OPS()) {
            body.replaceChildren(errorBlock("The operations script did not load. Reload the page."));
            return;
        }
        OPS().render(view, body, opsContext());
    }
    // Read state used to be kept in this browser; it is now stored on the server (moved once on load).
    const LEGACY_NOTIFICATION_SEEN_KEY = "printshop.admin.notificationsSeenAt";
    const COMPACT_KEY = "printshop.admin.compactTables";

    const viewRoot = document.querySelector("#view-root");
    const dialog = document.querySelector("#admin-dialog");
    let activeView = document.body.dataset.adminView || "dashboard";
    let adminEmail = "";
    let services = [];
    let products = []; // every printing service (catalog product) with its category, for the order filter

    const filters = {
        orders: { search: "", status: "", service: "", product: "", payment_status: "", payment_option: "", kind: "", date_from: "", date_to: "" },
        customers: { view: "all", search: "", activity: "", payment_status: "", registered_from: "", registered_to: "", ordered_from: "", ordered_to: "" },
        payments: { search: "", verification: "", method: "", service: "", payment_status: "", payment_option: "", date_from: "", date_to: "" },
        reports: {
            tab: "summary",
            summary: { preset: "month", date_from: "", date_to: "", group_by: "", service: "" },
            completed: { service: "", customer: "", request_from: "", request_to: "", completed_from: "", completed_to: "", payment_status: "", min_days: "", max_days: "" },
            service: { service: "", completed_from: "", completed_to: "", request_from: "", request_to: "" },
        },
        notifications: { actor: "", status: "", category: "", search: "", date_from: "", date_to: "" },
    };
    // Called after an order action so the visible table/report shows the saved change.
    let refreshActiveView = () => {};
    let reportSummaryState = null;
    // One watcher for the order modal; replaced when another order is opened, released when it closes.
    let orderWatch = null;

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

    // Presentation only: each stat glyph gets a Lucide icon and an accent colour (see theme-admin.css).
    const STAT_ICONS = {
        "👥": ["users", "purple"], "▤": ["clipboard-list", "cyan"], "◷": ["clock", "orange"], "⚒": ["loader-circle", "purple"],
        "✓": ["circle-check", "green"], "✕": ["circle-x", "red"], "₱": ["philippine-peso", "pink"], "⚑": ["flag", "orange"],
        "½": ["percent", "cyan"], "⛟": ["truck", "cyan"], "⌕": ["search", "purple"], "⏱": ["timer", "cyan"],
    };

    function statIcon(icon) {
        const [name] = STAT_ICONS[icon] || [];
        const svg = name && window.PrintIcons ? window.PrintIcons.svg(name, { size: 18 }) : null;
        return h("span", { className: "stat-icon", "aria-hidden": "true" }, svg || icon || "");
    }

    function statCard({ label, value, foot, icon, onClick, tone }) {
        const accent = (STAT_ICONS[icon] || [])[1] || "pink";
        const card = h(onClick ? "button" : "article", { className: `stat-card stat-accent-${accent}${onClick ? " stat-card-link" : ""}${tone ? ` stat-${tone}` : ""}`, type: onClick ? "button" : null },
            h("span", { className: "stat-top" }, h("span", { className: "stat-label", text: label }), statIcon(icon)),
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

    const MONEY_TOLERANCE = 0.005;
    const finite = (value, fallback = null) => typeof value === "number" && Number.isFinite(value) ? value : fallback;

    /** The same cumulative money view the server exposes, with legacy fields only as a fallback. */
    function adminPayment(order) {
        const summary = order.summary || {};
        const payment = order.payment || {};
        const calc = payment.payment_calculation || summary.payment_calculation || {};
        const total = finite(calc.total_order_amount, finite(summary.total_amount, finite(order.total_amount)));
        const paid = finite(calc.confirmed_payments, finite(summary.amount_paid, 0));
        return {
            total,
            paid,
            balanceDue: finite(calc.balance_due, finite(summary.balance, total === null ? null : Math.max(0, total - paid))),
            exact: finite(calc.remaining_balance_exact, finite(calc.remaining_balance, total === null ? null : total - paid)),
            requiredDown: finite(calc.required_down_payment, finite(payment.down_payment_required, finite(summary.down_payment_required))),
            shortfall: finite(calc.additional_needed_for_down_payment, finite(summary.additional_needed_for_down_payment, 0)),
            excess: finite(calc.excess_amount, finite(summary.excess_amount, 0)),
            status: calc.payment_status || summary.order_payment_status || order.payment_status || payment.payment_status || summary.payment_status || "—",
        };
    }

    /** Client-side preview only; the server recomputes and stores this when the payment is confirmed. */
    function paymentStatusAfter(total, requiredDown, paidAfter, cod = false) {
        if (total === null) return "No Payment";
        if (!cod && paidAfter > total + MONEY_TOLERANCE) return "Excess Payment";
        if (paidAfter >= total - MONEY_TOLERANCE) return "Fully Paid";
        if (paidAfter <= MONEY_TOLERANCE) return "No Payment";
        const required = cod ? 0 : requiredDown || 0;
        if (required > 0 && paidAfter < required - MONEY_TOLERANCE) return "Insufficient Down Payment";
        if (required > 0 && paidAfter <= required + MONEY_TOLERANCE) return "Down Payment Paid";
        return "Partially Paid";
    }

    // ================================================================ navigation

    // Former admin pages now part of another view (Package and Pricing Management → Services & Products).
    const VIEW_ALIASES = { packages: "services", pricing: "services", designs: "services" };
    /**
     * Views whose tabs have their own sidebar entry (the Catalog Management and AI Chatbot groups).
     * A sidebar link names its tab with data-catalog-target / data-chatbot-target; the open tab is kept in
     * the URL hash (#packages), so a reload or a shared link reopens it. `sidebar` maps a tab without its own
     * entry to the entry that is highlighted for it.
     */
    const SECTION_TABS = {
        services: {
            attribute: "catalogTarget",
            fallback: "products",
            tabs: ["products", "categories", "packages", "designs", "sizes"],
            open: (tab) => window.PrintShopCatalogAdmin?.openTab(tab),
        },
        chatbot: {
            attribute: "chatbotTarget",
            fallback: "overview",
            tabs: ["overview", "conversations", "unanswered", "inquiries", "faqs", "settings"],
            sidebar: { unanswered: "conversations" },
            open: (tab) => window.PrintShopChatbotAdmin?.openTab(tab),
        },
    };

    // Per-order deep links are served by the Order Management shell. Without this check the final path
    // segment ("payments") would be mistaken for the Payment Monitoring view on Back/Forward.
    const ADMIN_ORDER_PATH = /^\/admin\/orders\/([^/]+)(\/payments)?\/?$/;
    function adminOrderRoute() {
        const match = ADMIN_ORDER_PATH.exec(window.location.pathname);
        if (!match) return null;
        try { return { reference: decodeURIComponent(match[1]), focus: match[2] ? "payments" : "" }; }
        catch { return null; }
    }

    function viewFromLocation() {
        // history.state and the path describe the view being revisited on Back/Forward; the server-provided
        // body view is the fallback for a fresh load of a per-order deep link such as /admin/orders/<ref>.
        const stateView = window.history.state?.view;
        if (LABELS[stateView]) return stateView;
        if (ADMIN_ORDER_PATH.test(window.location.pathname)) return "orders";
        const segment = window.location.pathname.split("/").filter(Boolean).pop();
        const route = VIEW_ALIASES[segment] || segment;
        if (LABELS[route]) return route;
        const bodyView = document.body.dataset.adminView;
        return LABELS[bodyView] ? bodyView : "dashboard";
    }

    /** The open tab of a SECTION_TABS view, read from the URL hash ("" for other views). */
    function sectionFromLocation(view = activeView) {
        const config = SECTION_TABS[view];
        if (!config) return "";
        const tab = window.location.hash.slice(1);
        return config.tabs.includes(tab) ? tab : config.fallback;
    }

    function linkSection(link) {
        const config = SECTION_TABS[link.dataset.view];
        return config ? link.dataset[config.attribute] || "" : "";
    }

    function closeSidebar() {
        const sidebar = document.querySelector("#admin-sidebar");
        if (!sidebar?.classList.contains("open")) return;
        sidebar.classList.remove("open");
        const button = document.querySelector('[data-action="menu"]');
        button?.setAttribute("aria-expanded", "false");
        button?.setAttribute("aria-label", "Open navigation");
    }

    /**
     * Open an admin view. `tab` opens one tab of a view listed in SECTION_TABS: the sidebar's
     * "Packages" entry is the Packages tab of Services & Products, not a separate page.
     */
    async function navigate(view, addHistory = true, { tab = "" } = {}) {
        const nextView = LABELS[view] ? view : "dashboard";
        const path = nextView === "dashboard" ? "/admin/dashboard" : `/admin/${nextView}`;
        const config = SECTION_TABS[nextView];
        const hashTab = config?.tabs.includes(window.location.hash.slice(1)) ? window.location.hash.slice(1) : "";
        const section = config ? (config.tabs.includes(tab) ? tab : hashTab) : "";
        const hash = section && section !== config.fallback ? `#${section}` : "";
        closeSidebar();
        closeNotificationPanel();
        if (nextView === activeView && window.location.pathname === path) {
            if (config) {
                const target = section || config.fallback;
                if (addHistory) window.history.replaceState({ view: nextView }, "", path + hash);
                config.open(target);
                markActiveNavigation();
                return;
            }
            renderView();
            return;
        }
        try {
            const response = await fetch(path, { credentials: "same-origin" });
            if (response.status === 401 || response.status === 403 || (response.redirected && /\/admin\/login(?:[?#]|$)/.test(response.url))) {
                window.location.assign("/admin/login");
                return;
            }
            if (!response.ok) throw new Error(`The admin page could not be loaded (HTTP ${response.status}).`);
            const pageDocument = new DOMParser().parseFromString(await response.text(), "text/html");
            const incomingView = pageDocument.querySelector("#view-root");
            if (!incomingView || pageDocument.body.dataset.adminView !== nextView) {
                throw new Error("The admin page could not be loaded. Please refresh and try again.");
            }
            viewRoot.replaceChildren(...Array.from(incomingView.childNodes, (node) => document.importNode(node, true)));
            document.title = pageDocument.title;
            activeView = nextView;
            document.body.dataset.adminView = nextView;
            if (addHistory) window.history.pushState({ view: nextView }, "", path + hash);
            else window.history.replaceState({ view: nextView }, "", path + hash);
            // The view opens the tab named in the URL hash (set just above).
            renderView();
            viewRoot.focus({ preventScroll: true });
            window.scrollTo({ top: 0 });
        } catch (error) {
            toast(error instanceof Error ? error.message : "The admin page could not be loaded.", "error");
        }
    }

    // ---------------------------------------------------------------- sidebar groups

    const NAV_GROUPS_KEY = "printshop.admin.navGroups";
    const navGroups = () => document.querySelectorAll(".sidebar-nav .nav-group");

    function readNavGroups() {
        try {
            const value = JSON.parse(sessionStorage.getItem(NAV_GROUPS_KEY) || "[]");
            return Array.isArray(value) ? value.filter((id) => typeof id === "string") : [];
        } catch {
            return [];
        }
    }

    function writeNavGroups(ids) {
        try { sessionStorage.setItem(NAV_GROUPS_KEY, JSON.stringify([...new Set(ids)])); } catch { /* ignore */ }
    }

    function setNavGroupOpen(group, open) {
        group.querySelector(".nav-group-toggle")?.setAttribute("aria-expanded", String(open));
        const submenu = group.querySelector(".nav-submenu");
        if (submenu) submenu.hidden = !open;
    }

    function syncNavGroupState() {
        const expanded = new Set(readNavGroups());
        navGroups().forEach((group) => {
            const id = group.dataset.navGroup;
            const open = id ? expanded.has(id) : group.querySelector(".nav-group-toggle")?.getAttribute("aria-expanded") === "true";
            setNavGroupOpen(group, open);
        });
    }

    /** Groups open one at a time (accordion), so the sidebar stays short. */
    function toggleNavGroup(button) {
        const group = button.closest(".nav-group");
        const open = button.getAttribute("aria-expanded") !== "true";
        navGroups().forEach((other) => setNavGroupOpen(other, other === group ? open : false));
        const id = group?.dataset.navGroup;
        writeNavGroups(open && id ? [id] : []);
    }

    let lastActiveGroup;

    /**
     * Highlight the sidebar entry of the open view (and, on views with tabs in the sidebar, of the open
     * tab), mark its group, and show "Admin / Group / Page" in the top bar.
     */
    function markActiveNavigation() {
        const config = SECTION_TABS[activeView];
        const current = sectionFromLocation();
        const section = config?.sidebar?.[current] || current;
        let activeLink = null;
        document.querySelectorAll("[data-view]").forEach((link) => {
            const target = linkSection(link);
            const active = link.dataset.view === activeView && (!target || target === section);
            link.classList.toggle("active", active);
            if (link.matches(".sidebar-nav a")) {
                if (active) {
                    link.setAttribute("aria-current", "page");
                    activeLink = activeLink || link;
                } else {
                    link.removeAttribute("aria-current");
                }
            }
        });
        const activeGroup = activeLink?.closest(".nav-group") || null;
        navGroups().forEach((group) => group.classList.toggle("has-active", group === activeGroup));
        // Open the group of the active page; preserve the user's expanded section across refresh/navigation.
        if (activeGroup !== lastActiveGroup) {
            const expanded = new Set(readNavGroups());
            const activeId = activeGroup?.dataset.navGroup;
            if (activeId) expanded.add(activeId);
            navGroups().forEach((group) => {
                const id = group.dataset.navGroup;
                const open = id ? expanded.has(id) : group === activeGroup;
                setNavGroupOpen(group, open);
            });
            writeNavGroups([...navGroups()].filter((group) => group.querySelector(".nav-group-toggle")?.getAttribute("aria-expanded") === "true").map((group) => group.dataset.navGroup).filter(Boolean));
            lastActiveGroup = activeGroup;
        } else if (activeGroup) {
            // Keep active group open even if storage was cleared.
            setNavGroupOpen(activeGroup, true);
        }
        // Notifications has no sidebar entry: it is opened from the bell, which shows as current instead.
        document.querySelector('[data-action="toggle-notifications"]')?.classList.toggle("is-active", activeView === "notifications");
        const groupLabel = activeGroup?.querySelector(".nav-group-toggle .nav-label")?.textContent || "";
        const groupCrumb = document.querySelector("#current-group");
        if (groupCrumb) {
            groupCrumb.textContent = groupLabel;
            groupCrumb.hidden = !groupLabel;
            if (groupCrumb.nextElementSibling) groupCrumb.nextElementSibling.hidden = !groupLabel;
        }
        document.querySelector("#current-section").textContent = activeLink?.querySelector(".nav-label")?.textContent || LABELS[activeView];
    }

    function renderView() {
        window.PrintIcons?.hydrate(viewRoot);
        markActiveNavigation();
        refreshActiveView = () => {};
        const renderers = {
            dashboard: renderDashboard,
            orders: renderOrders,
            production: () => renderOperations("production"),
            fulfillment: () => renderOperations("fulfillment"),
            refunds: () => renderOperations("refunds"),
            customers: renderCustomers,
            services: () => window.PrintShopCatalogAdmin?.renderServices(),
            payments: renderPayments,
            reports: renderReports,
            notifications: renderNotifications,
            chatbot: renderChatbot,
            settings: renderSettings,
        };
        renderers[activeView]?.();
    }

    function viewBody() {
        return viewRoot.querySelector("[data-view-body]");
    }

    /** The AI Chatbot page lives in chatbot-admin.js and is built with this file's shared builders. */
    function renderChatbot() {
        const body = viewBody();
        if (!body) return;
        if (!window.PrintShopChatbotAdmin) {
            body.replaceChildren(errorBlock("The chatbot admin script did not load. Reload the page."));
            return;
        }
        window.PrintShopChatbotAdmin.render(body, {
            panel, statCard, loadingBlock, errorBlock, field, searchInput, selectInput, detailList, refreshBadges,
            setRefresh: (fn) => { refreshActiveView = fn; },
        });
    }

    let lastSignature = "";
    let lastUnread = null;

    function setBadge(selector, value, title) {
        const badge = document.querySelector(selector);
        if (!badge) return;
        const text = value ? String(value) : "";
        if (badge.textContent !== text && value) {
            badge.classList.remove("is-new");
            void badge.offsetWidth; // restart the pulse animation
            badge.classList.add("is-new");
        }
        badge.textContent = text;
        badge.title = value ? title : "";
        badge.setAttribute("aria-label", value ? title : "");
    }

    /** A collapsed sidebar group shows the total of its pages' badges (hidden while the group is open). */
    function updateGroupBadges() {
        navGroups().forEach((group) => {
            const badge = group.querySelector("[data-group-count]");
            if (!badge) return;
            const total = [...group.querySelectorAll(".nav-submenu .nav-count")].reduce((sum, item) => sum + (Number(item.textContent) || 0), 0);
            const label = group.querySelector(".nav-group-toggle .nav-label")?.textContent || "";
            const text = total ? String(total) : "";
            badge.textContent = text;
            badge.title = total ? `${total} item${total === 1 ? "" : "s"} to act on in ${label} (expand to see each page)` : "";
            badge.setAttribute("aria-label", badge.title);
        });
    }

    /**
     * Sidebar badges and the header bell: requests to review, payments to collect, new customers, unread
     * notifications (read state is stored on the server). Polled every 30 seconds and after every action;
     * when any record changed, the open page reloads its data. A newly received customer notification is
     * announced once with a short toast; the Notifications page and the bell list them.
     */
    async function refreshBadges() {
        try {
            const counts = await api("/api/admin/counts");
            const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
            setBadge("#pending-count", counts.orders, `${plural(counts.new_requests, "new request")} and ${plural(counts.to_review, "request")} to review or quote`);
            setBadge("#payment-count", counts.payments, `${plural(counts.payments_to_verify, "payment")} to verify, ${plural(counts.collect_before_release, "order")} ready for release with a balance`);
            setBadge("#customer-count", counts.customers, `${plural(counts.customers, "new customer")} in the last 7 days`);
            setBadge("#chatbot-count", counts.chatbot_inquiries || 0, `${plural(counts.chatbot_inquiries || 0, "chat inquiry")} to follow up`);
            setBadge("#production-count", counts.production || 0, `${plural(counts.production || 0, "order")} printing, in quality checking, or packaging`);
            setBadge("#fulfillment-count", counts.fulfillment || 0, `${plural(counts.ready_for_pickup || 0, "order")} ready for pickup, ${plural(counts.ready_for_dispatch || 0, "order")} ready for dispatch, ${plural(counts.delivery_failed || 0, "failed delivery", )}, ${plural(counts.open_delivery_issues || 0, "open delivery issue")}`);
            setBadge("#refund-count", counts.refunds || 0, `${plural(counts.pending_cancellations || 0, "cancellation request")} and ${plural(counts.pending_refunds || 0, "open refund")}`);
            updateGroupBadges();
            const unread = counts.unread_notifications || 0;
            setBadge("#notification-count", unread, plural(unread, "unread notification"));
            document.querySelectorAll('[data-action="toggle-notifications"]').forEach((button) => {
                button.setAttribute("aria-label", unread ? `Notifications (${plural(unread, "unread notification")})` : "Notifications");
            });
            if (lastUnread !== null && unread > lastUnread) announceNewNotifications();
            lastUnread = unread;
            // Records changed elsewhere (a new order, a customer action): refresh the open table, unless a dialog is open.
            if (lastSignature && counts.signature !== lastSignature && !dialog.open && activeView !== "services") {
                refreshActiveView();
            }
            if (lastSignature && counts.signature !== lastSignature && notificationPanelOpen()) loadNotificationPanel();
            lastSignature = counts.signature;
        } catch {
            // Badges are a convenience; the pages themselves report loading errors.
        }
    }

    /** One short toast for notifications that arrived since the last check (not for every item). */
    async function announceNewNotifications() {
        try {
            const data = await api("/api/admin/notifications/recent");
            const newest = data.items.find((item) => !item.read);
            if (!newest) return;
            const more = data.unread_count > 1 ? ` (+${data.unread_count - 1} more unread)` : "";
            toast(`New notification: ${newest.title}${newest.reference ? ` · ${newest.reference}` : ""}${more}`, "info");
        } catch {
            // The badge already shows the unread count.
        }
    }

    // ================================================================ header notification dropdown

    const notificationPanel = () => document.querySelector("#notification-panel");
    const notificationPanelOpen = () => Boolean(notificationPanel() && !notificationPanel().hidden);

    function closeNotificationPanel() {
        const panelElement = notificationPanel();
        if (!panelElement || panelElement.hidden) return;
        panelElement.hidden = true;
        document.querySelector('[data-action="toggle-notifications"]')?.setAttribute("aria-expanded", "false");
    }

    function toggleNotificationPanel(button) {
        const panelElement = notificationPanel();
        if (!panelElement) return;
        if (!panelElement.hidden) {
            closeNotificationPanel();
            return;
        }
        document.querySelectorAll(".profile-menu[open]").forEach((menu) => menu.removeAttribute("open"));
        panelElement.hidden = false;
        button.setAttribute("aria-expanded", "true");
        loadNotificationPanel();
    }

    async function markNotificationsRead(body) {
        const result = await api("/api/admin/notifications/read", { method: "POST", body });
        refreshBadges();
        return result;
    }

    /** Open what a notification is about (its order or customer) and mark it read. */
    function openNotification(event) {
        closeNotificationPanel();
        if (!event.read) markNotificationsRead({ ids: [event.id] }).then(() => refreshActiveView()).catch(() => {});
        if (event.reference) openOrder(event.reference);
        else if (event.customer_id) openCustomer(event.customer_id);
    }

    function notificationSymbol(event) {
        return event.kind === "customer" ? "♙" : event.title.includes("ayment") ? "₱" : event.actor === "customer" ? "✉" : "▤";
    }

    async function loadNotificationPanel() {
        const panelElement = notificationPanel();
        if (!panelElement) return;
        if (!panelElement.childElementCount) panelElement.replaceChildren(loadingBlock("Loading notifications…"));
        let data;
        try {
            data = await api("/api/admin/notifications/recent");
        } catch (error) {
            panelElement.replaceChildren(h("p", { className: "notification-panel-empty", text: error.message }));
            return;
        }
        const markAll = h("button", { className: "table-link", type: "button", text: "Mark all as read", disabled: !data.unread_count });
        markAll.addEventListener("click", async () => {
            try {
                await markNotificationsRead({ all: true });
                loadNotificationPanel();
                refreshActiveView();
            } catch (error) {
                toast(error.message, "error");
            }
        });
        panelElement.replaceChildren(
            h("div", { className: "notification-panel-head" },
                h("strong", { text: "Notifications" }),
                h("span", { className: "table-secondary", text: data.unread_count ? `${number(data.unread_count)} unread` : "All caught up" }),
                markAll),
            data.items.length
                ? h("ul", { className: "notification-panel-list" }, ...data.items.map((event) => h("li", {},
                    h("button", { className: `notification-panel-item${event.read ? "" : " unread"}`, type: "button", onclick: () => openNotification(event) },
                        h("span", { className: "notification-symbol", "aria-hidden": "true", text: notificationSymbol(event) }),
                        h("span", { className: "notification-panel-text" },
                            h("strong", { text: event.title }),
                            h("span", { text: [event.reference, event.customer_name].filter(Boolean).join(" · ") })),
                        h("time", { datetime: event.at, title: dateTime(event.at), text: UI.relativeTime(event.at) }),
                        event.read ? null : h("span", { className: "visually-hidden", text: " (unread)" })))))
                : h("p", { className: "notification-panel-empty", text: "No customer notifications yet." }),
            h("a", { className: "notification-panel-all", href: "/admin/notifications", "data-view": "notifications", text: "View all notifications →" }),
        );
    }

    // ================================================================ dashboard

    /** Open the orders page filtered to one status or workflow stage. */
    function showOrders(status, extra = {}) {
        filters.orders = { ...filters.orders, status, payment_status: "", ...extra };
        navigate("orders");
    }

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
        const st = data.stages;
        const sc = data.status_counts || {};
        const plural = (count, word) => `${number(count)} ${word}${count === 1 ? "" : "s"}`;
        const goTo = (view, apply) => { apply?.(); navigate(view); };
        const releaseDue = Number(sc["Ready for Pickup"] || 0) + Number(sc["Ready for Dispatch"] || 0)
            + Number(sc["Out for Delivery"] || 0) + Number(sc["Delivery Failed"] || 0);

        // Only the four queues that need an administrator first are shown at full weight.
        const priorityCards = h("section", { className: "stats-grid stats-grid-4 dashboard-kpis", "aria-label": "Priority metrics" },
            statCard({ label: "Open / pending orders", value: number(data.pending_orders), foot: `${plural(st.to_review, "order")} to review · ${plural(st.awaiting_customer, "order")} waiting on the customer`, icon: "⌕", tone: data.pending_orders ? "highlight" : "", onClick: () => showOrders("pending") }),
            statCard({ label: "Payment verification", value: number(data.payments_to_verify), foot: data.payments_to_verify ? `${money(data.payments_to_verify_amount)} submitted by customers` : "No submissions waiting", icon: "₱", tone: data.payments_to_verify ? "highlight" : "", onClick: () => goTo("payments", () => { filters.payments = { ...filters.payments, verification: "pending" }; }) }),
            statCard({ label: "Production / fulfillment due", value: number(st.production + st.release), foot: `${plural(st.production, "order")} in production · ${plural(releaseDue, "order")} to release`, icon: "⚒", tone: releaseDue ? "highlight" : "", onClick: () => navigate(st.production >= releaseDue ? "production" : "fulfillment") }),
            statCard({ label: "Refunds pending", value: number((data.pending_cancellations || 0) + (data.pending_refunds || 0)), foot: `${plural(data.pending_cancellations || 0, "cancellation")} · ${plural(data.pending_refunds || 0, "refund")}`, icon: "↺", tone: (data.pending_cancellations || data.pending_refunds) ? "highlight" : "", onClick: () => navigate("refunds") }),
        );

        // The four priority cards above are the "needs attention" summary. A second
        // tile grid repeated the same counts, so the dashboard now shows each figure once.

        // Order workflow: every order that is not cancelled or rejected is counted in exactly one card.
        const orderCards = h("section", { className: "stats-grid stats-grid-4 dashboard-stages", "aria-label": "Orders by stage" },
            statCard({ label: "New orders", value: number(st.new), foot: "Just submitted · not yet opened", icon: "⚑", tone: st.new ? "highlight" : "", onClick: () => showOrders("Pending Confirmation") }),
            statCard({ label: "Orders to review", value: number(st.to_review), foot: "Review, quote, or re-quote", icon: "⌕", tone: st.to_review ? "highlight" : "", onClick: () => showOrders("to_review") }),
            statCard({ label: "Pending orders", value: number(st.awaiting_customer), foot: "Waiting for the customer's acceptance or down payment", icon: "◷", onClick: () => showOrders("awaiting_customer") }),
            statCard({ label: "Payments to verify", value: number(data.payments_to_verify), foot: data.payments_to_verify ? `${money(data.payments_to_verify_amount)} submitted by customers` : "No customer payments waiting", icon: "₱", tone: data.payments_to_verify ? "highlight" : "", onClick: () => { filters.payments = { ...filters.payments, verification: "pending" }; navigate("payments"); } }),
            statCard({ label: "In production", value: number(st.production), foot: "Confirmed · drafting, approval, and printing", icon: "⚒", onClick: () => showOrders("production") }),
            statCard({ label: "Ready for pickup / delivery", value: number(st.release), foot: "Printed and waiting to be released", icon: "⛟", onClick: () => showOrders("release") }),
            statCard({ label: "Completed orders", value: number(st.completed), foot: "Completed and fully paid", icon: "✓", onClick: () => showOrders("completed") }),
            statCard({ label: "All orders", value: number(data.total_orders), foot: `${plural(data.cancelled_orders, "cancelled or rejected order")}`, icon: "▤", onClick: () => showOrders("") }),
        );

        const operationCards = h("section", { className: "stats-grid stats-grid-4 dashboard-stages op-stats", "aria-label": "Production, fulfillment, cancellations and refunds" },
            statCard({ label: "Confirmed orders", value: number(sc["Confirmed"] || 0), foot: "Down payment verified · start the draft", icon: "✔", onClick: () => showOrders("Confirmed") }),
            statCard({ label: "Quality checking", value: number(sc["Quality Checking"] || 0), foot: "Printed · inspect before packing", icon: "⌕", onClick: () => navigate("production") }),
            statCard({ label: "Packaging", value: number((sc["Packaging"] || 0) + (sc["Awaiting Full Payment"] || 0)), foot: `${number(sc["Awaiting Full Payment"] || 0)} packed and waiting for the balance`, icon: "▦", onClick: () => navigate("production") }),
            statCard({ label: "Ready for pickup", value: number(sc["Ready for Pickup"] || 0), foot: "Fully paid · waiting for the claimant", icon: "⌂", onClick: () => navigate("fulfillment") }),
            statCard({ label: "Ready for dispatch", value: number(sc["Ready for Dispatch"] || 0), foot: "Fully paid · hand to J&T Express", icon: "⛟", onClick: () => navigate("fulfillment") }),
            statCard({ label: "Out for delivery", value: number(sc["Out for Delivery"] || 0), foot: `${number(sc["Delivery Failed"] || 0)} failed deliveries to reschedule`, icon: "➜", tone: sc["Delivery Failed"] ? "highlight" : "", onClick: () => navigate("fulfillment") }),
            statCard({ label: "Pending cancellations", value: number(data.pending_cancellations || 0), foot: "Paid orders waiting for review", icon: "⨯", tone: data.pending_cancellations ? "highlight" : "", onClick: () => navigate("refunds") }),
            statCard({ label: "Open refunds", value: number(data.pending_refunds || 0), foot: data.collections ? `Net collections ${money(data.collections.net)}` : "", icon: "↺", tone: data.pending_refunds ? "highlight" : "", onClick: () => navigate("refunds") }),
        );

        const r = data.revenue;
        const revenueFigures = h("div", { className: "revenue-figures" },
            miniStat("Total confirmed revenue", money(r.total), "Completed and fully paid orders, each counted once"),
            miniStat(`Revenue · ${r.month_label}`, money(r.this_month), "Orders completed and fully paid this month"),
            miniStat("Payments received this month", money(r.received_this_month), `${plural(r.payments_this_month, "verified payment")} (cash flow, not revenue)`),
            miniStat("Outstanding balance", money(r.outstanding_balance), "Still to be paid on agreed, active orders"),
            data.collections ? miniStat("Net collections (all time)", money(data.collections.net), `Verified ${money(data.collections.verified)} − completed refunds ${money(data.collections.refunds_completed)}`) : null,
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

        const recentTable = h("div");
        new UI.DataTable(recentTable, {
            columns: [
                { label: "Order ID", className: "ref-cell", render: (row) => cellPair(row.reference, row.request_date ? date(row.request_date) : "") },
                { label: "Customer", render: (row) => cellPair(row.customer_name, row.product) },
                { label: "Total", align: "right", render: (row) => totalText(row) },
                { label: "Payment", render: (row) => badge(row.payment_status) },
                { label: "Status", render: (row) => badge(row.status) },
            ],
            rows: () => data.recent_orders,
            pageSize: 10,
            emptyMessage: "No orders have been submitted yet.",
            onRowClick: (row) => openOrder(row.reference),
            rowTitle: (row) => `Open order ${row.reference}`,
        }).load();

        // The full metric set stays available, but collapsed so it no longer buries the work queues.
        const secondaryMetrics = h("details", { className: "dashboard-secondary" },
            h("summary", {}, h("strong", { text: "All order-stage & operations metrics" }), h("span", { className: "table-secondary", text: " · 16 detailed counters" })),
            orderCards,
            operationCards,
        );
        body.replaceChildren(
            priorityCards,
            data.payments_missing_reference ? h("p", { className: "notice-card notice-warning dashboard-notice" },
                `${plural(data.payments_missing_reference, "older payment")} ${data.payments_missing_reference === 1 ? "has" : "have"} no reference number. `,
                h("button", { className: "table-link", type: "button", text: "Review in Payments & Verification →", onclick: () => { filters.payments = { ...filters.payments, verification: "missing" }; navigate("payments"); } })) : null,
            panel("Revenue summary", "Revenue counts only completed and fully paid orders", h("div", { className: "revenue-summary" }, revenueFigures, trend),
                h("button", { className: "button button-small", type: "button", text: "Open Sales & Reports", onclick: () => navigate("reports") })),
            panel("Recent orders", "Newest requests · select a row for details", recentTable,
                h("button", { className: "button button-small", type: "button", text: "All orders", onclick: () => showOrders("") })),
            secondaryMetrics,
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

    /** The next step for an order, derived only from fields already present in the orders row. */
    function requiredAction(row) {
        if (row.payments_to_verify) return { text: "Verify payment", tone: "verify" };
        if (row.work_status === "Revision Requested") return { text: "Resend finished work", tone: "action" };
        if (row.work_status === "Ready for Customer Review") return { text: "Await work approval", tone: "" };
        if (row.work_status === "Approved by Customer" || row.status === "Awaiting Final Payment") return { text: "Collect remaining balance", tone: "action" };
        if (ACTION_LABELS[row.status]) return { text: ACTION_LABELS[row.status], tone: "action" };
        if (row.status === "Awaiting Down Payment") return { text: "Await down payment", tone: "" };
        if (row.status === "Awaiting Full Payment") return { text: "Collect remaining balance", tone: "action" };
        if (["Pending Confirmation", "Under Review", "Awaiting Quotation", "Quotation Sent"].includes(row.status)) return { text: "Review request", tone: "action" };
        if (["Ready for Pickup", "Ready for Dispatch", "Out for Delivery", "Delivery Failed"].includes(row.status)) return { text: "Release / deliver", tone: "action" };
        return { text: "View details", tone: "" };
    }

    function renderOrders() {
        const body = viewBody();
        if (!body) return;
        const f = filters.orders;
        const tabs = h("div", { className: "segmented", role: "tablist", "aria-label": "Order status groups" });
        const countLabel = h("span", { className: "table-secondary" });
        const tableHost = h("div");
        const productChoices = () => [["", "All printing services"], ...products.filter((item) => !f.service || item.service_id === f.service).map((item) => [item.id, item.label])];
        const productSelect = selectInput(productChoices(), f.product, (value) => apply({ product: value }));
        const serviceSelect = selectInput(serviceOptions(), f.service, (value) => {
            // A printing service belongs to one category: clear it when the category changes.
            const keep = products.some((item) => item.id === f.product && (!value || item.service_id === value));
            apply({ service: value, product: keep ? f.product : "" });
            productSelect.replaceChildren(...productChoices().map(([id, text]) => new Option(text, id)));
            productSelect.value = f.product;
        });
        const statusSelect = selectInput([["", "All statuses"], ...STATUS_GROUPS.slice(1).map(([value, label]) => [value, `${label} (group)`]), ...WORKFLOW_STAGES, ...STATUSES.map((status) => [status, status])], f.status, (value) => apply({ status: value }));
        const paymentSelect = selectInput([["", "All payment statuses"], ...PAYMENT_STATUSES.map((status) => [status, status])], f.payment_status, (value) => apply({ payment_status: value }));
        const kindSelect = selectInput([["", "All order types"], ["package", "Package"], ["design", "Customized · shop design"], ["customize", "Customized order"]], f.kind, (value) => apply({ kind: value }));
        const optionSelect = selectInput([["", "All payment options"], ...PAYMENT_OPTIONS], f.payment_option, (value) => apply({ payment_option: value }));
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
                {
                    label: "Order Reference", sortKey: "reference", className: "ref-cell", render: (row) => h("span", { className: "cell-pair" },
                        h("span", { className: "table-primary", text: row.reference }),
                        row.image_count ? h("span", { className: "table-chip", text: `🖼 ${row.image_count} image${row.image_count === 1 ? "" : "s"}` }) : null),
                },
                { label: "Customer", sortKey: "customer", render: (row) => cellPair(row.customer_name, row.customer_email) },
                { label: "Service", sortKey: "service", render: (row) => h("span", { className: "cell-pair" },
                    h("span", { className: "table-primary", text: row.product }),
                    h("span", { className: "table-secondary", text: row.service }),
                    row.order_kind ? h("span", { className: "table-chip", text: orderKindChip(row) }) : null) },
                { label: "Order Date", sortKey: "request_date", type: "date", render: (row) => date(row.request_date) },
                { label: "Order Status", sortKey: "status", render: (row) => h("span", { className: "cell-pair" }, badge(row.status),
                    row.work_status && row.work_status !== row.status ? h("span", { className: "table-chip", text: row.work_status }) : null,
                    h("span", { className: "table-secondary", text: row.delivery_province
                        ? `${row.fulfillment_label} · ${row.delivery_province} (${row.destination_region})`
                        : row.fulfillment_label })) },
                { label: "Payment Status", sortKey: "payment_status", render: (row) => h("span", { className: "cell-pair" }, badge(row.order_payment_status || row.payment_status),
                    h("span", { className: "table-secondary", text: row.order_payment_status && row.order_payment_status !== row.payment_status ? `Legacy: ${row.payment_status}` : "" }),
                    h("span", { className: "table-secondary", text: row.payment_option === "cod" ? "COD" : row.payment_option ? `${row.admin_set_down_payment ? "Custom DP" : "50% DP"} · ${row.down_payment_status}` : "Option after quotation" }),
                    row.payments_to_verify ? h("span", { className: "table-chip", text: "⚑ Payment to verify" }) : null) },
                { label: "Total Amount", sortKey: "total", align: "right", render: (row) => totalText(row) },
                { label: "Amount Paid", sortKey: "paid", align: "right", render: (row) => money(row.amount_paid) },
                { label: "Remaining Balance", align: "right", render: (row) => row.balance === null || row.balance === undefined ? "—" : money(row.balance) },
                { label: "Required Action", render: (row) => {
                    const action = requiredAction(row);
                    return h("span", { className: `required-action${action.tone ? ` action-${action.tone}` : ""}`, text: action.text });
                } },
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
                if (Array.isArray(result.products) && productSelect.options.length <= 1) {
                    products = result.products;
                    productSelect.replaceChildren(...productChoices().map(([value, text]) => new Option(text, value)));
                    productSelect.value = f.product;
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
            Object.assign(f, { search: "", status: "", service: "", product: "", payment_status: "", payment_option: "", kind: "", date_from: "", date_to: "" });
            renderOrders();
        } });
        // Keep the everyday filters visible; uncommon filters live behind one disclosure.
        const moreOrderFilters = h("details", { className: "filter-more" },
            h("summary", { text: "More filters" }),
            h("div", { className: "filter-more-grid" },
                field("Exact status", statusSelect),
                field("Payment option", optionSelect),
                field("Order type", kindSelect),
                field("Requested from", fromInput),
                field("Requested to", toInput),
                clear,
            ));
        body.replaceChildren(
            tabs,
            h("div", { className: "filter-bar orders-filter-bar", role: "search", "aria-label": "Order search and filters" },
                h("span", { className: "filter-bar-label", text: "Search & filters" }),
                search,
                field("Category", serviceSelect),
                field("Printing service", productSelect),
                field("Payment", paymentSelect),
                moreOrderFilters,
            ),
            panel(["Customer requests ", countLabel], "Reference, customer, service, date, statuses, amounts, balance, and the next required action. Select a row for the complete transaction details.", tableHost,
                UI.exportButtons("orders", () => ({ ...f, sort: table.sort.key, dir: table.sort.dir }))),
        );
        table.load();
        refreshActiveView = () => table.reload();
    }

    // ================================================================ order details modal

    /** Redraw an open order modal from the database, but never while the admin is filling in a form. */
    function refreshOpenOrder(reference, container, options) {
        if (!dialog.open || !container.isConnected || container.dataset.dirty === "true") return;
        void api(`/api/admin/orders/${encodeURIComponent(reference)}`).then((order) => {
            if (!dialog.open || !container.isConnected || container.dataset.dirty === "true") return;
            container.replaceChildren(...orderModalContent(order, options));
            const nav = container.querySelector(".order-section-nav");
            if (nav) watchOrderSections(container, nav);
        }).catch(() => { /* the next version change retries; the current view stays usable */ });
    }

    /** Refresh/back-safe URL for the order modal; the step only controls the initial scroll/focus. */
    function syncOrderUrl(reference, focus = "") {
        const suffix = ["payments", "payment", "verify"].includes(focus) ? "/payments" : "";
        const url = `/admin/orders/${encodeURIComponent(reference)}${suffix}`;
        if (window.location.pathname !== url) {
            window.history.pushState({ view: "orders", reference, focus }, "", url);
        }
    }

    async function openOrder(reference, { back = null, focus = "" } = {}) {
        orderWatch?.();
        orderWatch = null;
        syncOrderUrl(reference, focus);
        const container = h("div", { className: "order-modal" }, loadingBlock("Loading order details…"));
        // Track in-progress edits so a live update cannot discard an admin's typing or chosen file.
        const markDirty = (event) => {
            if (event.target.matches("textarea, input[type=text], input[type=number], input[type=file], select")) {
                container.dataset.dirty = "true";
            }
        };
        container.addEventListener("input", markDirty, true);
        container.addEventListener("change", markDirty, true);
        UI.openModal(container, { wide: true, size: "xl" });
        let order;
        try {
            order = await api(`/api/admin/orders/${encodeURIComponent(reference)}`);
        } catch (error) {
            if (error?.status === 404) {
                // A hand-written or deleted order link must not leave a stale route behind.
                window.history.replaceState({ view: "orders" }, "", "/admin/orders");
            }
            container.replaceChildren(errorBlock(error.message, () => openOrder(reference, { back, focus })));
            return;
        }
        if (!dialog.open || !container.isConnected) return;
        container.replaceChildren(...orderModalContent(order, { back, focus }));
        const nav = container.querySelector(".order-section-nav");
        if (nav) watchOrderSections(container, nav);
        if (window.PrintShopWorkflowSync) {
            orderWatch = window.PrintShopWorkflowSync.watch({
                reference,
                role: "admin",
                version: order.version,
                onChange: () => refreshOpenOrder(reference, container, { back, focus }),
            });
        }
        if (focus) {
            const target = revealOrderTarget(container.querySelector(`[data-focus="${focus}"]`) || orderSectionTarget(container, focus));
            target?.scrollIntoView({ block: "start" });
            const section = ORDER_SECTIONS.some(([key]) => key === focus)
                ? focus
                : (target?.closest("[data-order-section]")?.dataset.orderSection || "overview");
            nav?.setActive?.(section);
        }
    }

    function orderKindChip(row) {
        if (row.order_kind === "package") return `📦 ${row.package_name || "Package"}`;
        if (row.order_kind === "design") return `🎨 ${row.design_name || "Shop design"}${row.standard_size ? ` · ${row.standard_size}` : ""}`;
        if (row.price_basis === "standard_size") return `📐 ${row.standard_size || "Standard size"} · price list`;
        return "✎ Customized";
    }

    function orderKindLabel(order) {
        const base = { package: "Package", design: "Customized order (shop design)" }[order.order_kind] || "Customized order";
        return order.price_basis === "standard_size" ? `${base} · standard size from the price list` : base;
    }

    function orderImages(order) {
        const images = [];
        if (order.design?.image) {
            images.push({
                src: order.design.image,
                name: `${order.design.name} — shop design`,
                caption: `Shop design the customer selected${order.design.is_sample ? " (temporary sample)" : ""}`,
                kind: "Selected design",
            });
        }
        if (order.product?.image) {
            images.push({ src: order.product.image, name: `${order.product.name} — catalog sample`, caption: "Product sample the customer selected from the catalog", kind: "Catalog sample" });
        }
        const packageImages = order.package ? (order.package.images?.length ? order.package.images : (order.package.image ? [order.package.image] : [])) : [];
        packageImages.filter((src) => src !== order.product?.image).forEach((src, index) => {
            images.push({ src, name: `${order.package.name} — package photo ${index + 1}`, caption: "Photo of the package the customer selected", kind: "Package photo" });
        });
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
            section.append(main, name, ...(images.length > 1 ? [thumbs] : []),
                h("button", { className: "button button-small gallery-open", type: "button", text: `View all images (${images.length}) · fullscreen`, onclick: () => UI.openViewer(images, current) }));
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
        if (Array.isArray(customization.specifications)) {
            // Package details are shown in their own card; keep only the remaining rows here.
            const rows = order.package
                ? customization.specifications.filter((spec) => !String(spec.key || "").startsWith("package"))
                : customization.specifications;
            return rows.map((spec) => [spec.label, spec.value]);
        }
        return [["Size", customization.size], ["Material / variant", customization.material], ["Customization", customization.details]].filter(([, value]) => value);
    }

    /** Event rows from the catalog fields, with a fallback for older orders that stored a fixed object. */
    function eventRows(order) {
        if (Array.isArray(order.event_fields) && order.event_fields.length) {
            return order.event_fields.map((row) => [row.label, row.value]);
        }
        const legacy = order.event_details && !Array.isArray(order.event_details) ? order.event_details : {};
        return [
            ["Celebrant", legacy.celebrant],
            ["Event type", legacy.event_type],
            ["Event date", legacy.event_date ? date(legacy.event_date) : ""],
            ["Event time", legacy.event_time],
            ["Event location", legacy.event_location],
        ].filter(([, value]) => value);
    }

    function packageCard(order) {
        const pkg = order.package;
        if (!pkg) return null;
        const hasAddOn = pkg.add_on_price !== null && pkg.add_on_price !== undefined;
        const minimum = pkg.minimum_quantity ?? pkg.included_quantity;
        const details = pkg.inclusion_details?.length ? pkg.inclusion_details : (pkg.inclusions || []).map((name) => ({ name, description: "" }));
        const quantity = pkg.quantity ?? order.customization?.quantity;
        return h("section", { className: "fact-card", dataset: { focus: "package" } },
            h("h3", { text: "Selected package (as ordered)" }),
            detailList([
                ["Package", pkg.name],
                ["Base package price", money(pkg.price)],
                ["Size", pkg.size],
                ["Material", pkg.material],
                ["Minimum order", `${number(minimum)} pcs`],
                ["Additional piece", hasAddOn
                    ? `${money(pkg.add_on_price)} each${pkg.add_on_label ? ` (${pkg.add_on_label})` : ""}`
                    : `Fixed set of ${number(minimum)} pcs (no additional pieces)`],
                ["Quantity ordered", `${number(quantity)} pcs`],
                hasAddOn && typeof pkg.additional_cost === "number"
                    ? ["Computation", `${money(pkg.price)} + (${number(quantity)} − ${number(minimum)}) × ${money(pkg.add_on_price)} = ${money(pkg.price + pkg.additional_cost)}`]
                    : null,
            ]),
            details.length
                ? h("div", { className: "package-inclusions-detail" }, h("strong", { text: "Inclusions" }), h("ul", { className: "plain-list" },
                    ...details.map((item) => h("li", { text: item.description ? `${item.name} — ${item.description}` : item.name }))))
                : null,
            h("p", { className: "field-note", text: "Saved when the order was placed. Later package edits do not change this order." }),
        );
    }

    /** The shop design the customer chose and the details they gave to personalize it (saved as ordered). */
    function designCard(order) {
        const design = order.design;
        if (!design) return null;
        const images = [{ src: design.image, name: design.name, caption: `${design.name} · shop design selected for ${order.reference}`, kind: "Selected design" }];
        const preview = h("button", { className: "design-order-preview", type: "button", title: "Open large preview with zoom and fullscreen", "aria-label": `Open ${design.name} in the large preview`, onclick: () => UI.openViewer(images, 0) },
            h("img", { src: design.image, alt: `${design.name} design preview`, loading: "lazy", decoding: "async" }));
        const details = Array.isArray(design.details) ? design.details : [];
        return h("section", { className: "fact-card design-order-card", dataset: { focus: "design" } },
            h("h3", { text: "Selected shop design (as ordered)" }),
            h("div", { className: "design-order-layout" },
                preview,
                h("div", {},
                    design.is_sample ? h("p", { className: "design-sample-flag", text: "TEMPORARY SAMPLE design" }) : null,
                    detailList([
                        ["Design", design.name],
                        ["Design ID", design.id],
                        ["Service", design.category_label],
                        design.style ? ["Style", design.style] : null,
                        design.description ? ["Description", design.description] : null,
                    ]))),
            h("h4", { className: "design-order-subtitle", text: "Customer personalization" }),
            details.length ? detailList(details.map((row) => [row.label, row.value])) : h("p", { className: "field-note", text: "No details provided." }),
            h("p", { className: "field-note", text: "Saved when the order was placed. Editing, replacing, or deleting the design later does not change this order." }),
        );
    }

    const ORDER_SECTIONS = [["overview", "Overview"], ["payments", "Payment"], ["operations", "Processing"], ["history", "Activity"]];

    /** Navigation anchors can live inside the two-column layout or inside a collapsed panel. */
    function orderSectionTarget(container, key) {
        return container?.querySelector?.(`[data-order-section="${key}"]`) || null;
    }

    /** Open every collapsed panel around a target before scrolling to it (deep links keep working). */
    function revealOrderTarget(node) {
        if (!node) return null;
        if (node instanceof HTMLDetailsElement) node.open = true;
        let parent = node.parentElement;
        while (parent) {
            if (parent instanceof HTMLDetailsElement) parent.open = true;
            parent = parent.parentElement;
        }
        return node;
    }

    /** Small, consistent heading used by the compact order cards. */
    function odPanelHead(title, meta) {
        return h("div", { className: "od-panel-head" },
            h("h3", { text: title }),
            meta ? h("span", { className: "od-panel-meta", text: meta }) : null);
    }

    /** Quiet dot + text status. Used instead of stacking several loud badges. */
    function odStatus(text) {
        const value = String(text || "—");
        const key = slug(value);
        const bad = /declined|rejected|cancelled|unpaid|not-quoted|failed/.test(key);
        const warn = /partial|pending|waiting|verification|review|due|after/.test(key);
        const good = !bad && !warn && /paid|verified|accepted|completed|available|shown|delivered|claimed/.test(key);
        const tone = bad ? "muted" : good ? "good" : warn ? "warn" : "muted";
        return h("span", { className: `od-status od-status-${tone}` }, h("i", { "aria-hidden": "true" }), h("span", { text: value }));
    }

    /** Native disclosure used for every secondary panel on the order detail screen. */
    function odDisclosure(label, { focus = "", hint = "", body = [] } = {}) {
        return h("details", { className: "od-disclosure", ...(focus ? { dataset: { focus } } : {}) },
            h("summary", {}, h("span", { text: label }), hint ? h("span", { className: "od-disclosure-hint", text: hint }) : null),
            h("div", { className: "od-disclosure-body" }, ...body.filter(Boolean)));
    }

    /** Sticky chip bar: jumps to the existing sections without changing any of their content. */
    function orderSectionNav() {
        const nav = h("nav", { className: "order-section-nav", "aria-label": "Order detail sections" });
        nav.setActive = (key) => {
            nav.querySelectorAll(".order-section-chip").forEach((chip) => {
                const active = chip.dataset.section === key;
                chip.classList.toggle("active", active);
                chip.setAttribute("aria-pressed", String(active));
            });
        };
        ORDER_SECTIONS.forEach(([key, label], index) => nav.append(h("button", {
            className: `order-section-chip${index === 0 ? " active" : ""}`,
            type: "button",
            dataset: { section: key },
            "aria-pressed": String(index === 0),
            text: label,
            onclick: () => {
                nav.setActive(key);
                revealOrderTarget(orderSectionTarget(nav.closest(".order-modal"), key))
                    ?.scrollIntoView({ behavior: "smooth", block: "start" });
            },
        })));
        return nav;
    }

    /** Keep the active chip in step with manual scrolling inside the modal frame. */
    function watchOrderSections(container, nav) {
        const frame = container.closest(".dialog-frame");
        if (!frame) return;
        const handler = () => {
            const line = frame.getBoundingClientRect().top + 120;
            let active = ORDER_SECTIONS[0][0];
            ORDER_SECTIONS.forEach(([key]) => {
                if ((orderSectionTarget(container, key)?.getBoundingClientRect().top ?? Infinity) <= line) active = key;
            });
            nav.setActive(active);
        };
        frame.addEventListener("scroll", handler, { passive: true });
        container.closest("dialog")?.addEventListener("close", () => frame.removeEventListener("scroll", handler), { once: true });
    }

    /** Reorganized order detail: compact header, two focus columns, then one collapsed processing panel. */
    function orderModalContent(order, { back, focus }) {
        const summary = order.summary;
        const reload = () => openOrder(order.reference, { back, focus });
        const actions = actionsPanel(order, reload);
        const opsSections = OPS()?.orderSections(order, reload, opsContext()) || [];
        const deliveryOps = opsSections.filter((node) => node?.dataset?.focus === "fulfillment");
        const otherOps = opsSections.filter((node) => node?.dataset?.focus !== "fulfillment");

        const header = h("header", { className: "od-header", dataset: { orderSection: "overview" } },
            back ? h("button", { className: "button button-small back-button", type: "button", text: "← Back", onclick: back }) : null,
            h("div", { className: "od-header-main" },
                h("div", { className: "od-title-row" },
                    h("p", { className: "eyebrow", text: `${summary.service} · ${summary.product}` }),
                    h("h2", { className: "dialog-heading", text: `Order ${order.reference}` })),
                h("div", { className: "od-meta" },
                    badge(order.status),
                    h("span", { className: "od-meta-item", text: `Requested ${dateTime(order.created_at)}` }),
                    h("span", { className: "od-meta-item", text: `${number(order.customization?.quantity)} pcs` }),
                    summary.completed_at ? h("span", { className: "od-meta-item", text: `Completed ${dateTime(summary.completed_at)}` }) : null,
                    summary.completion_days !== null ? h("span", { className: "od-meta-item", text: `${days(summary.completion_days)} to complete` }) : null)),
            h("div", { className: "od-header-actions" },
                h("button", {
                    className: "button button-primary", type: "button", text: "Update status",
                    disabled: typeof actions.triggerPrimary !== "function",
                    onclick: () => actions.triggerPrimary?.(),
                }),
                h("button", { className: "button", type: "button", text: "More actions", onclick: () => actions.openMore?.() })));

        const main = h("div", { className: "od-grid", dataset: { orderSection: "overview" } },
            h("div", { className: "od-column" },
                previewCard(order),
                pricingNoticeCard(order),
                customerCard(order, back),
                specificationsCard(order),
                deliverySummaryCard(order, deliveryOps)),
            h("div", { className: "od-column" },
                paymentSummaryCard(order),
                workReviewCard(order),
                quotationCard(order),
                paymentHistoryCard(order, reload),
                actions,
                activityCard(order)));

        const operations = h("section", { className: "od-panel od-operations", dataset: { focus: "operations", orderSection: "operations" } },
            odPanelHead("Processing & fulfillment", order.status),
            otherOps.length
                ? odDisclosure("View processing details", { focus: "operations-details", hint: `${otherOps.length} record${otherOps.length === 1 ? "" : "s"}`, body: otherOps })
                : h("p", { className: "field-note", text: "No production, draft, cancellation, or refund records for this order yet." }));

        return [header, orderSectionNav(), main, operations].filter(Boolean);
    }

    function previewCard(order) {
        const summary = order.summary;
        return h("section", { className: "od-panel od-preview", dataset: { focus: "preview" } },
            odPanelHead("Product & design"),
            h("div", { className: "od-product-info" },
                h("div", {},
                    h("span", { className: "od-eyebrow", text: "Product" }),
                    h("strong", { text: summary.product || "Custom order" })),
                h("div", {},
                    h("span", { className: "od-eyebrow", text: "Category" }),
                    h("strong", { text: summary.service || "—" })),
                order.design?.name ? h("div", {},
                    h("span", { className: "od-eyebrow", text: "Design" }),
                    h("strong", { text: order.design.name })) : null),
            gallery(order));
    }

    /** Compact identity + contact. The full address lives here, not in the delivery card. */
    function customerCard(order, back) {
        const customer = order.customer || {};
        const f = order.fulfillment || {};
        const address = (f.address || []).filter((row) => row.value);
        const profileLink = order.customer_id
            ? h("button", {
                className: "table-link", type: "button", text: "View customer profile →",
                onclick: () => openCustomer(order.customer_id, { back: () => openOrder(order.reference, { back }) }),
            })
            : null;
        const initials = String(customer.name || "?").trim().slice(0, 2).toUpperCase();
        const body = [
            address.length
                ? h("div", {}, h("strong", { text: "Complete delivery address" }), detailList(address.map((row) => [row.label, row.value])))
                : h("p", { className: "field-note", text: "No delivery address on file — this order is set for store pickup." }),
            f.method === "delivery" ? h("div", {}, detailList([["Delivery area", f.area_label || "—"]])) : null,
            detailList([
                ["Email", customer.email ? h("a", { href: `mailto:${customer.email}`, text: customer.email }) : "—"],
                customer.messenger ? ["Messenger", customer.messenger] : null,
                ["Account", order.customer_id ? "Registered customer" : "Guest / not linked"],
            ]),
            profileLink,
        ];
        return h("section", { className: "od-panel od-customer", dataset: { focus: "customer" } },
            odPanelHead("Customer"),
            h("div", { className: "od-customer-summary" },
                h("span", { className: "od-avatar", "aria-hidden": "true", text: initials }),
                h("div", { className: "od-customer-identity" },
                    h("strong", { text: customer.name || "—" }),
                    h("span", { text: customer.phone || "No contact number" }))),
            odDisclosure("View customer details", { focus: "customer-details", body }));
    }

    /** Only the fields relevant to this service are shown; deeper customization is one disclosure. */
    function specificationsCard(order) {
        const summary = order.summary;
        const specs = specifications(order);
        const events = eventRows(order);
        const notes = order.customization?.notes || order.customization?.instructions || "";
        const count = specs.length + events.length + (order.package ? 1 : 0) + (order.design ? 1 : 0) + (notes ? 1 : 0);
        const body = [
            specs.length ? detailList(specs) : h("p", { className: "field-note", text: "No extra customization fields for this service." }),
            events.length ? h("div", {}, h("strong", { text: "Event details" }), detailList(events)) : null,
            packageCard(order),
            designCard(order),
            h("div", {}, h("strong", { text: "Special instructions" }), h("p", { className: "prewrap", text: notes || "None provided." })),
            order.admin_notes ? h("div", {}, h("strong", { text: "Latest internal note" }), h("p", { className: "prewrap", text: order.admin_notes })) : null,
        ];
        return h("section", { className: "od-panel od-specs", dataset: { focus: "specifications" } },
            odPanelHead("Order specifications", `${number(order.customization?.quantity)} pcs`),
            detailList([
                ["Service", summary.service],
                ["Product", summary.product],
                order.order_kind ? ["Order type", orderKindLabel(order)] : null,
                ["Quantity", number(order.customization?.quantity)],
                order.package?.name ? ["Selected package", order.package.name] : null,
                order.design?.name ? ["Selected design", order.design.name] : null,
            ]),
            odDisclosure("View customization details", {
                focus: "customization",
                hint: count ? `${count} item${count === 1 ? "" : "s"}` : "",
                body,
            }));
    }

    /** Fulfillment at a glance; the address is intentionally omitted (it lives in Customer). */
    function deliverySummaryCard(order, deliveryOps = []) {
        const f = order.fulfillment || {};
        const shipping = order.shipping || {};
        const tracking = f.tracking_number
            ? (f.tracking_url
                ? h("a", { href: f.tracking_url, target: "_blank", rel: "noopener", text: f.tracking_number })
                : f.tracking_number)
            : null;
        const packed = Boolean(f.released_at || order.production?.packaging?.completed_at);
        const nextRelease = f.next_release_status || f.release_status;
        return h("section", { className: "od-panel od-delivery", dataset: { focus: "fulfillment-summary" } },
            odPanelHead("Delivery & pickup", f.method === "delivery" ? "Delivery" : "Pickup"),
            detailList([
                ["Method", h("strong", { text: f.label || "Store Pickup" })],
                f.courier ? ["Courier", `${f.courier}${f.courier_service ? ` · ${f.courier_service}` : ""}`] : null,
                tracking ? ["Tracking number", tracking] : null,
                shipping.delivery_province ? ["Delivery province", shipping.delivery_province] : null,
                shipping.destination_region ? ["Destination region", shipping.destination_region] : null,
                shipping.shipping_fee ? ["Shipping fee", money(shipping.shipping_fee)] : f.delivery_fee ? ["Delivery fee", money(f.delivery_fee)] : null,
                shipping.estimated_delivery || f.estimate_text ? ["Estimated delivery", shipping.estimated_delivery || f.estimate_text] : null,
                f.scheduled_date ? ["Scheduled delivery", date(f.scheduled_date)] : null,
                f.pickup_schedule ? ["Pickup schedule", `${date(f.pickup_schedule.date)} ${f.pickup_schedule.time || ""}`.trim()] : null,
                f.preferred_pickup_date ? ["Preferred pickup date", date(f.preferred_pickup_date)] : null,
                f.pickup_location ? ["Pickup location", `${f.pickup_location.name} · ${f.pickup_location.address}`] : null,
                f.pickup_notes ? ["Pickup notes", f.pickup_notes] : null,
                ["Fulfillment status", badge(f.fulfillment_status || "Not started")],
                packed && nextRelease ? ["Release destination", nextRelease] : null,
                f.recorded ? null : ["Note", "Placed before delivery options existed: store pickup."],
            ]),
            deliveryOps.length
                ? odDisclosure("View delivery history", { focus: "delivery-history", hint: `${deliveryOps.length} record${deliveryOps.length === 1 ? "" : "s"}`, body: deliveryOps })
                : null);
    }

    /** Primary financial snapshot. Every figure is the server-calculated value from the order view. */
    function paymentSummaryCard(order) {
        const summary = order.summary;
        const payment = order.payment || {};
        const cod = payment.payment_option === "cod";
        const moneyNow = adminPayment(order);
        const downValue = cod
            ? (moneyNow.requiredDown === 0 ? "Not required (COD)" : money(moneyNow.requiredDown))
            : (moneyNow.requiredDown === null
                ? "Half of the quotation"
                : money(moneyNow.requiredDown));
        const shipping = order.shipping || {};
        const metrics = [
            ...(payment.order_subtotal === null || payment.order_subtotal === undefined ? [] : [
                h("div", { className: "od-money" },
                    h("span", { text: "Order subtotal" }),
                    h("strong", { text: money(payment.order_subtotal) })),
            ]),
            ...(shipping.delivery_province ? [
                h("div", { className: "od-money" },
                    h("span", { text: `Shipping fee · ${shipping.destination_region}` }),
                    h("strong", { text: money(payment.shipping_fee ?? shipping.shipping_fee ?? 0) })),
            ] : []),
            h("div", { className: "od-money" },
                h("span", { text: cod ? "Down payment" : `Required down payment${payment.admin_set_down_payment ? "" : " (50%)"}` }),
                h("strong", { text: downValue })),
            h("div", { className: "od-money" },
                h("span", { text: "Confirmed payments" }),
                h("strong", { text: money(moneyNow.paid) })),
            h("div", { className: "od-money" },
                h("span", { text: cod ? "Due on delivery" : "Remaining balance" }),
                h("strong", { className: moneyNow.balanceDue > 0 ? "od-balance-due" : "", text: moneyNow.balanceDue === null ? "—" : money(moneyNow.balanceDue) })),
            h("div", { className: "od-money" },
                h("span", { text: "Down payment status" }),
                h("strong", {}, odStatus(payment.down_payment_status || "—"))),
            h("div", { className: "od-money" },
                h("span", { text: "Payment status" }),
                h("strong", {}, odStatus(moneyNow.status))),
            h("div", { className: "od-money" },
                h("span", { text: "Work status" }),
                h("strong", {}, odStatus(order.work_status || payment.work_status || "—"))),
        ];
        return h("section", { className: "od-panel od-payment", dataset: { focus: "payments", orderSection: "payments" } },
            odPanelHead("Payment summary"),
            h("div", { className: "od-total" },
                h("div", {}, h("span", { text: "Confirmed total" }),
                    h("strong", { text: summary.total_amount === null || summary.total_amount === undefined ? "Awaiting quotation" : summary.total_amount })),
                odStatus(summary.payment_status || "—")),
            h("p", { className: "od-section-label", text: "Payment breakdown" }),
            h("div", { className: "od-money-grid" }, ...metrics),
            payment.pending_verification
                ? h("p", { className: "od-inline-note" }, h("strong", { text: `${money(payment.pending_verification)} waiting for verification.` }), " It is not counted as paid yet.")
                : null,
            payment.final_payment_locked
                ? h("p", { className: "od-inline-note" }, h("strong", { text: "Final payment locked: " }), payment.final_payment_lock || "Waiting for the customer's approval of the finished work.")
                : null,
            !cod && moneyNow.shortfall > 0.005
                ? h("p", { className: "od-inline-note", text: `${money(moneyNow.shortfall)} still due on the down payment.` })
                : null,
            moneyNow.excess > 0.005
                ? h("p", { className: "od-inline-note", text: `${money(moneyNow.excess)} excess was confirmed and is flagged for admin review.` })
                : null);
    }

    /** The finished-work review record, including the files the customer is approving. */
    function workReviewCard(order) {
        const review = order.work_approval || {};
        if (!review.status && !(review.preview_files || []).length) return null;
        const previews = (review.preview_files || []).map((file) => h("a", {
            className: "button button-small", href: file.url, target: "_blank", rel: "noopener",
            text: `Open ${file.name}`,
        }));
        return h("section", { className: "od-panel od-work-review", dataset: { focus: "work-review" } },
            odPanelHead("Finished-work review", review.status_label || review.status || ""),
            detailList([
                ["Work status", odStatus(review.work_status || review.status_label || "—")],
                ["Sent", review.sent_at ? dateTime(review.sent_at) : "—"],
                ["Customer responded", review.responded_at ? dateTime(review.responded_at) : "—"],
                ["Revision requests", number(review.revision_count || 0)],
                ["Shop remarks", review.remarks || "—"],
                ["Customer note", review.approval_note || review.revision_note || "—"],
            ]),
            previews.length ? h("div", { className: "dialog-actions" }, ...previews) : null);
    }

    /** Compact payment ledger. The header carries the record count and the whole section collapses. */
    function paymentHistoryCard(order, reload) {
        const count = order.payments.length;
        const moneyNow = adminPayment(order);
        let running = 0;
        const table = count
            ? h("div", { className: "table-wrap" }, h("table", { className: "data-table compact-table od-pay-table" },
                h("thead", {}, h("tr", {}, ...["Date received", "Payment type", "Payment method", "Amount", "Cumulative paid", "Balance after", "Verification status"].map((text, index) => h("th", {
                    scope: "col", className: index >= 3 && index <= 5 ? "align-right" : "", text,
                })))),
                h("tbody", {}, ...order.payments.map((payment) => {
                    const check = payment.verification_view || {};
                    running = Math.round((running + Number(payment.amount || 0)) * 100) / 100;
                    const balanceAfter = moneyNow.total === null ? null : Math.max(0, Math.round((moneyNow.total - running) * 100) / 100);
                    const reference = payment.reference
                        ? h("span", { className: "od-cell-sub", text: `Ref ${payment.reference}${check.reference_type_label ? ` · ${check.reference_type_label}` : ""}` })
                        : addReferenceButton(order, payment, reload);
                    return h("tr", {},
                        h("td", { text: dateTime(payment.received_at) }),
                        h("td", {},
                            h("span", { text: payment.kind || "Payment" }),
                            payment.source === "customer submission" ? h("span", { className: "od-cell-sub", text: "Verified submission" }) : null),
                        h("td", { text: payment.method || "—" }),
                        h("td", { className: "align-right od-amount", text: money(payment.amount) }),
                        h("td", { className: "align-right", text: money(running) }),
                        h("td", { className: "align-right", text: balanceAfter === null ? "—" : money(balanceAfter) }),
                        h("td", {},
                            odStatus(check.status || "—"),
                            check.verified_by ? h("span", { className: "od-cell-sub", text: `by ${check.verified_by}` }) : null,
                            reference));
                }))))
            : h("p", { className: "field-note", text: "No payments recorded yet." });
        return h("details", { className: "od-panel od-history", dataset: { focus: "payment-history" } },
            h("summary", { className: "od-panel-summary" },
                h("span", {}, h("h3", { text: "Payment history" }), h("small", { text: `${count} record${count === 1 ? "" : "s"}` }))),
            h("div", { className: "od-history-body" },
                table,
                submissionHistory(order),
                order.payment_instructions ? h("p", { className: "od-note" }, h("strong", { text: "Payment instructions shared: " }), order.payment_instructions) : null));
    }

    /** Compact chronological activity; the audit trail loads on demand below the visible events. */
    function activityCard(order) {
        const entries = [...(order.history || [])].reverse();
        return h("section", { className: "od-panel od-activity", dataset: { focus: "activity", orderSection: "history" } },
            odPanelHead("Activity", `${entries.length} event${entries.length === 1 ? "" : "s"}`),
            entries.length
                ? h("ol", { className: "timeline od-timeline" }, ...entries.map((entry) => h("li", {},
                    h("span", { text: entry.text }),
                    h("time", { text: [dateTime(entry.at), entry.by || entry.actor].filter(Boolean).join(" · ") }))))
                : h("p", { className: "field-note", text: "No activity recorded yet." }),
            auditCard(order));
    }

    /** Accepted / Pending / Rejected at a glance, mapped from the stored quotation status. */
    function quoteResponseLabel(status) {
        return { accepted: "Accepted", sent: "Pending", declined: "Rejected", superseded: "Superseded" }[status]
            || (status ? String(status).replace(/^./, (letter) => letter.toUpperCase()) : "Pending");
    }

    /** Separate, collapsed quotation section. It reuses the figures already calculated by the API. */
    function quotationCard(order) {
        const quote = order.quotation;
        if (!quote) return null;
        const response = quoteResponseLabel(quote.status);
        const body = h("div", { className: "od-quote-body" },
            detailList([
                ["Revision number", number(quote.revision)],
                ["Printing amount", money(quote.amount)],
                ["Quoted total", money(quote.total ?? quote.amount)],
                ["Required down payment", money(quote.down_payment)],
                ["Date sent", dateTime(quote.sent_at)],
                ["Date responded", quote.responded_at ? dateTime(quote.responded_at) : "—"],
            ]),
            h("div", { className: "od-quote-response" }, h("span", { text: "Customer response" }), odStatus(response)),
            quote.note ? h("p", { className: "od-note" }, h("strong", { text: "Message sent: " }), quote.note) : null,
            quote.decline_reason ? h("p", { className: "od-note" }, h("strong", { text: "Customer reason: " }), quote.decline_reason) : null,
            order.quotation_history?.length
                ? odDisclosure("View all revisions", { focus: "quotation-history", hint: `${order.quotation_history.length} total`, body: [quotationHistoryCard(order)] })
                : null);
        return h("details", { className: "od-panel od-quotation", dataset: { focus: "quotation" } },
            h("summary", { className: "od-panel-summary" },
                h("span", {}, h("h3", { text: "Quotation details" }), h("small", { text: `Revision ${number(quote.revision)}` })),
                odStatus(response)),
            body);
    }

    const QUOTE_STATES = { sent: "Waiting for the customer", accepted: "Accepted by the customer", declined: "Declined by the customer", superseded: "Superseded" };

    /** Every quotation sent for this order: previous and revised prices, timestamps, notes, and responses. */
    function quotationHistoryCard(order) {
        const items = order.quotation_history || [];
        if (!items.length) return null;
        return h("div", { className: "table-wrap" }, h("table", { className: "data-table compact-table od-history-table" },
            h("thead", {}, h("tr", {}, ...["Rev.", "Sent", "Previous", "Amount", "Status", "Responded", "Note / reason", "By"].map((text) => h("th", { scope: "col", text })))),
            h("tbody", {}, ...items.slice().reverse().map((item) => h("tr", {},
                h("td", { text: String(item.revision) }),
                h("td", { text: dateTime(item.sent_at) }),
                h("td", { text: typeof item.previous_amount === "number" ? money(item.previous_amount) : "—" }),
                h("td", { text: money(item.amount) }),
                h("td", {}, badge(QUOTE_STATES[item.status] || item.status)),
                h("td", { text: item.responded_at ? dateTime(item.responded_at) : (item.superseded_at ? `Superseded ${dateTime(item.superseded_at)}` : "—") }),
                h("td", { className: "prewrap", text: [item.note, item.decline_reason ? `Customer: ${item.decline_reason}` : ""].filter(Boolean).join(" · ") || "—" }),
                h("td", { text: item.sent_by || "—" }))))));
    }

    const AUDIT_LABELS = {
        "order.created": "Order placed",
        "order.approved": "Package order approved (down payment requested)",
        "order.status_changed": "Status changed",
        "order.cancelled": "Cancelled by customer",
        "quotation.sent": "Quotation sent",
        "quotation.revised": "Quotation revised",
        "quotation.accepted": "Quotation accepted",
        "quotation.declined": "Quotation declined",
        "payment.submitted": "Payment submitted (pending verification)",
        "payment.verified": "Payment verified",
        "payment.rejected": "Payment rejected",
        "payment.recorded": "Payment recorded by admin",
        "payment.terms_set": "Payment terms set/revised",
        "payment.option_changed": "Payment option changed",
        "draft.sent": "Draft sent",
        "draft.approved": "Draft approved by customer",
        "draft.changes_requested": "Draft changes requested",
    };

    function auditDetails(entry) {
        const d = entry.details || {};
        const parts = [];
        if (d.status_from || d.status_to) parts.push(`${d.status_from || "—"} → ${d.status_to || "—"}`);
        if (typeof d.total_from === "number" || typeof d.total_to === "number") parts.push(`Total ${typeof d.total_from === "number" ? money(d.total_from) : "—"} → ${typeof d.total_to === "number" ? money(d.total_to) : "—"}`);
        if (typeof d.down_payment === "number") parts.push(`Down ${money(d.down_payment)}${d.down_payment_source === "custom" ? " (custom)" : d.down_payment_source === "default_50_percent" ? " (50%)" : ""}`);
        if (typeof d.remaining_after_down_payment === "number") parts.push(`Balance after down ${money(d.remaining_after_down_payment)}`);
        if (typeof d.amount === "number") parts.push(`Amount ${money(d.amount)}`);
        if (typeof d.reported_amount === "number" && Math.abs(d.reported_amount - (d.amount || 0)) > 0.005) parts.push(`reported ${money(d.reported_amount)}`);
        if (d.reference) parts.push(`Ref ${d.reference}`);
        if (d.revision) parts.push(`Revision ${d.revision}`);
        if (d.note || d.reason) parts.push(d.note || d.reason);
        return parts.join(" · ") || "—";
    }

    /** The audit trail (who did what, when) and the notifications sent to the customer; loaded on demand. */
    function auditCard(order) {
        const body = h("div", {}, h("p", { className: "field-note", text: "Open to load the audit trail." }));
        const card = h("details", { className: "fact-card audit-card", dataset: { focus: "audit" } },
            h("summary", {}, h("h3", { text: "Audit history & customer notifications" })), body);
        let loaded = false;
        card.addEventListener("toggle", async () => {
            if (!card.open || loaded) return;
            loaded = true;
            body.replaceChildren(loadingBlock("Loading audit history…"));
            try {
                const data = await api(`/api/admin/orders/${encodeURIComponent(order.reference)}/audit`);
                body.replaceChildren(
                    h("div", { className: "table-wrap" }, h("table", { className: "data-table compact-table" },
                        h("thead", {}, h("tr", {}, ...["When", "Action", "By", "Details"].map((text) => h("th", { scope: "col", text })))),
                        h("tbody", {}, ...data.audit.slice().reverse().map((entry) => h("tr", {},
                            h("td", { text: dateTime(entry.at) }),
                            h("td", { text: AUDIT_LABELS[entry.action] || entry.action }),
                            h("td", { text: entry.actor_type === "admin" ? `Admin${entry.actor_id ? ` (${entry.actor_id})` : ""}` : entry.actor_type === "customer" ? "Customer" : "System" }),
                            h("td", { text: auditDetails(entry) })))))),
                    h("h4", { text: `Notifications sent to the customer (${data.notifications.length})` }),
                    data.notifications.length
                        ? h("ul", { className: "plain-list" }, ...data.notifications.map((item) => h("li", { text: `${dateTime(item.created_at)} · ${item.title}${item.read ? " · read" : ""}` })))
                        : h("p", { className: "field-note", text: "No notifications sent yet." }),
                );
            } catch (error) {
                loaded = false;
                body.replaceChildren(h("p", { className: "field-note", text: error.message }));
            }
        });
        return card;
    }

    function submissionHistory(order) {
        const items = order.payment_submissions || [];
        if (!items.length) return null;
        const label = { pending: "Pending verification", verified: "Verified", rejected: "Rejected" };
        return h("div", { className: "submission-history" },
            h("h4", { text: `Customer payment submissions (${items.length})` }),
            h("div", { className: "table-wrap" }, h("table", { className: "data-table compact-table" },
                h("thead", {}, h("tr", {}, ...["Submitted", "Method", "Reference", "Proof", "Status", "Amount"].map((text, index) => h("th", { scope: "col", className: index === 5 ? "align-right" : "", text })))),
                h("tbody", {}, ...items.slice().reverse().map((item) => h("tr", {},
                    h("td", { text: dateTime(item.submitted_at) }),
                    h("td", { text: item.method }),
                    h("td", { text: item.reference }),
                    h("td", {}, item.proof ? h("a", { href: item.proof.url, target: "_blank", rel: "noopener", text: item.proof.name }) : "—"),
                    h("td", {}, badge(label[item.status] || item.status), item.review_note ? h("span", { className: "table-secondary", text: ` ${item.review_note}` }) : null),
                    h("td", { className: "align-right", text: money(item.amount) })))))));
    }

    function paymentOptionForm(order, reload) {
        const button = h("button", { className: "button button-primary", type: "button", text: "Switch to 50% Down Payment" });
        button.addEventListener("click", async () => {
            const answer = await UI.confirm({
                title: "Switch to the 50% down payment?",
                message: "Cash on Delivery is no longer offered. The customer must pay the 50% down payment; the order is confirmed once it is verified.",
                confirmLabel: "Switch payment option",
                input: { label: "Reason (recorded in the order history)", required: false, placeholder: "e.g. COD is no longer offered" },
            });
            if (!answer) return;
            await submitAction(button, order, "payment-option", { payment_option: "down_payment", note: answer.value || "" }, reload, "Payment option changed to the 50% down payment.");
        });
        return h("div", { className: "action-form" },
            h("h4", { text: "Older Cash on Delivery order" }),
            h("p", { className: "field-note", text: "This order was placed while Cash on Delivery was offered. Every order now requires a 50% down payment." }),
            h("div", { className: "dialog-actions" }, button));
    }

    /** Small, quiet note explaining the source of the price; the figures stay in Payment summary. */
    function pricingNoticeCard(order) {
        const pricing = order.pricing || {};
        const quoteBased = order.price_basis === "quotation" && !order.fixed_price;
        const message = quoteBased
            ? order.quotation?.status === "accepted"
                ? "This product has no published catalog price. The confirmed total was set through the accepted quotation."
                : "This product has no published catalog price. The confirmed total is set through a quotation."
            : order.fixed_price
                ? order.price_basis === "standard_size"
                    ? "Total saved from the published size price list when the order was placed. Later price-list changes do not affect it."
                    : "Total saved from the published package price when the order was placed. Later package changes do not affect it."
                : "Catalog prices are an estimate only. The confirmed total is shown in Payment summary.";
        const detail = pricingCard(pricing, order);
        return h("section", { className: "od-price-note", dataset: { focus: "pricing" } },
            h("span", { className: "od-price-icon", "aria-hidden": "true", text: "i" }),
            h("div", { className: "od-price-copy" },
                h("p", { text: message }),
                detail ? odDisclosure("View price breakdown", { focus: "pricing-details", body: [detail] }) : null));
    }

    function pricingCard(pricing, order = null) {
        if (!pricing || !Object.keys(pricing).length) return null;
        const packageOrder = order?.order_kind === "package" || pricing.pricing_mode === "package";
        const priceListOrder = !packageOrder && pricing.pricing_mode === "size";
        const items = pricing.requires_quote
            ? (pricing.quote_reasons || []).map((reason) => h("li", { text: reason }))
            : [
                ...(pricing.lines || []).map((line) => h("li", { text: `${line.label}: ${money(line.amount)} (${line.detail})` })),
                pricing.area_sqft ? h("li", { text: `Print area: ${pricing.area_sqft} sq ft per piece` }) : null,
                h("li", { className: "strong", text: pricing.estimated_total === null ? "No total calculated." : `Estimated total: ${money(pricing.estimated_total)}` }),
            ];
        return h("div", { className: "od-price-detail" },
            h("h4", { text: packageOrder ? "Package price" : priceListOrder && order?.fixed_price ? "Price from the standard size price list" : "Price estimate from catalog prices" }),
            order?.price_list ? detailList([
                ["Selected size", order.price_list.size],
                ["Unit price (as ordered)", typeof order.price_list.unit_price === "number" ? money(order.price_list.unit_price) : "—"],
                ["Quantity", number(order.price_list.quantity)],
                ["Subtotal", typeof order.price_list.computed_total === "number" ? money(order.price_list.computed_total) : "—"],
            ]) : null,
            h("ul", { className: "plain-list" }, ...items),
            priceListOrder ? h("p", { className: "field-note", text: order?.fixed_price
                ? "Saved when the order was placed: later price-list changes do not change this order."
                : "A custom size (or an option that needs a quote) is priced by your quotation below." }) : null,
            packageOrder ? h("p", { className: "field-note", text: order?.fixed_price
                ? "Calculated from the shop's published package price and add-on charge. This is the order's fixed total."
                : "Calculated from the shop's published package price and add-on charge. Some options need a quotation, so confirm the price below." }) : null,
        );
    }

    function actionsPanel(order, reload) {
        const summary = order.summary;
        const actions = order.actions;
        const section = h("section", { className: "actions-card", dataset: { focus: "actions" } }, odPanelHead("Available actions", "Next steps"));
        const buttons = h("div", { className: "action-buttons" });
        let primary = null;

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
            if (!primary && !danger && !blocked) {
                primary = button;
                button.classList.add("od-primary-status");
            }
            button.addEventListener("click", async () => {
                const answer = await UI.confirm({
                    title: `${ACTION_LABELS[status] || status}?`,
                    message: `${ACTION_HELP[status] || ""} Order ${order.reference} will move from “${order.status}” to “${status}”.`,
                    confirmLabel: ACTION_LABELS[status] || "Confirm",
                    tone: danger ? "danger" : "primary",
                    input: danger ? { label: status === "Rejected" ? "Reason for rejection (recorded in the order history)" : "Reason for cancellation (recorded in the order history)", required: true, placeholder: "e.g. Artwork cannot be reproduced at this size" } : null,
                    details: status === "Completed" ? [["Confirmed total", money(order.total_amount)], ["Amount paid", money(summary.amount_paid)], ["Completion date", "Today"]] : [],
                });
                if (!answer) return;
                const note = (answer.value || "").trim();
                // UI.confirm blocks an empty required input; keep this guard for unexpected confirm implementations.
                if (danger && !note) return;
                await submitAction(button, order, "status", { status, note }, reload, `Order marked as ${status}.`);
            });
            buttons.append(button);
        });
        if (buttons.childElementCount) section.append(buttons);
        if (actions.complete_requires_payment) {
            section.append(h("p", { className: "notice-card notice-warning", text: `Resolve any pending payment review or record the remaining balance of ${money(summary.balance)} before marking this order as completed. Only fully paid orders count as completed revenue.` }));
        }
        if (actions.cancel_blocked_by_payments && order.allowed_statuses.some((status) => status === "Rejected" || status === "Cancelled")) {
            section.append(h("p", { className: "field-note", text: "This order has recorded or submitted payments, so it cannot be rejected or cancelled from here. Paid orders are cancelled through a reviewed cancellation request, with any refund recorded separately." }));
        }
        if (order.status === "Awaiting Customer Approval") {
            section.append(h("p", { className: "notice-card", text: "Waiting for the customer to approve the draft in their portal. The administrator cannot approve printing for the customer." }));
        }
        if (order.status === "Quotation Sent") {
            section.append(h("p", { className: "notice-card", text: "Waiting for the customer to accept or decline the quotation in their portal. No payment can be recorded until they accept it." }));
        }
        if (order.status === "Quotation Declined") {
            section.append(h("p", { className: "notice-card", text: "The customer declined this quotation, so it cannot be paid. Send a revised quotation or close the request." }));
        }
        if (order.status === "Awaiting Down Payment" && !actions.verify_payments) {
            section.append(h("p", { className: "notice-card", text: `Waiting for the customer's ${order.payment?.admin_set_down_payment ? "required down payment" : "50% down payment"} of ${money(order.payment?.down_payment_required)} through the shop's e-wallet. The order is confirmed automatically when you verify it.` }));
        }
        if (order.status === "Confirmed") {
            section.append(h("p", { className: "notice-card notice-success", text: "The down payment is verified and the order is confirmed. Start draft preparation or send the finished work for review." }));
        }
        const review = order.work_approval || {};
        if (review.status === "pending") {
            section.append(h("p", { className: "notice-card", text: "Waiting for the customer to approve the finished work. The remaining balance stays locked until they approve it." }));
        } else if (review.status === "revision_requested") {
            section.append(h("p", { className: "notice-card notice-warning", text: `The customer requested a revision${review.revision_note ? `: “${review.revision_note}”` : ""}. Send an updated finished-work preview to continue.` }));
        } else if (review.status === "approved") {
            section.append(h("p", { className: "notice-card notice-success", text: "The customer approved the finished work. The remaining balance can now be paid." }));
        }
        if (order.payment?.final_payment_locked && review.status !== "pending") {
            section.append(h("p", { className: "notice-card notice-warning", text: order.payment.final_payment_lock || "The remaining balance is locked until the customer approves the finished work." }));
        }
        if (actions.start_production_blocked) {
            section.append(h("p", { className: "notice-card notice-warning", text: "Production cannot start until the required down payment is verified." }));
        }

        if (section.childElementCount === 1) {
            const final = { Completed: "This order is completed and fully paid. No further actions are needed.", Rejected: "This request was rejected.", Cancelled: "This order was cancelled. Any refund is handled in Processing & fulfillment below." }[order.status];
            section.append(h("p", { className: "field-note", text: final || "No administrator action is available at this stage." }));
        }

        // Forms are secondary: one disclosure keeps the initial view short without removing any capability.
        const advanced = [];
        if (actions.confirm_package) advanced.push(confirmPackageForm(order, reload));
        if (actions.set_payment_terms) advanced.push(paymentTermsForm(order, reload));
        const pendingPayments = (order.payment_submissions || []).filter((item) => item.status === "pending");
        if (actions.verify_payments) advanced.push(...pendingPayments.map((item) => paymentForm(order, reload, item)));
        if (actions.quote) advanced.push(quoteForm(order, reload));
        // A pending customer receipt must be verified or rejected first; the direct form would be refused.
        if (actions.record_payment && !pendingPayments.length) advanced.push(paymentForm(order, reload));
        if (actions.send_draft) advanced.push(draftForm(order, reload));
        if (actions.send_work_preview) advanced.push(workPreviewForm(order, reload));
        if (actions.change_payment_option) advanced.push(paymentOptionForm(order, reload));
        advanced.push(...(OPS()?.orderActions(order, reload, opsContext()) || []));
        const more = advanced.length
            ? odDisclosure("More actions", { focus: "more-actions", hint: `${advanced.length} form${advanced.length === 1 ? "" : "s"}`, body: advanced })
            : null;
        if (more) section.append(more);

        section.triggerPrimary = () => {
            if (primary && !primary.disabled) primary.click();
            else revealOrderTarget(section).scrollIntoView({ behavior: "smooth", block: "start" });
        };
        section.openMore = () => {
            if (more) {
                more.open = true;
                revealOrderTarget(more).scrollIntoView({ behavior: "smooth", block: "start" });
                return;
            }
            revealOrderTarget(section).scrollIntoView({ behavior: "smooth", block: "start" });
        };
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
        const quote = order.quotation;
        const revising = Boolean(quote);
        // The amount is entered by the administrator; the catalog estimate is shown only as a reference.
        const fee = order.payment?.delivery_fee || 0;
        // Base the optional down-payment helper on the value the admin is about to edit,
        // and keep it in sync while they type. It is only guidance; the server recalculates it.
        const openingTotal = Number(order.payment?.subtotal ?? order.total_amount ?? 0) || 0;
        const amount = h("input", { id: "quote-amount", name: "total_amount", type: "number", min: "0.01", step: "0.01", required: true, value: order.payment?.subtotal ?? order.total_amount ?? "" });
        const down = h("input", { id: "quote-down-payment", name: "down_payment_amount", type: "number", min: "0.01", max: String(openingTotal + fee), step: "0.01", placeholder: `50% of ${money(openingTotal + fee)}` });
        amount.addEventListener("input", () => {
            const current = Number(amount.value) || 0;
            down.max = String(current + fee);
            down.placeholder = `50% of ${money(current + fee)}`;
        });
        down.value = order.quotation?.down_payment_amount ?? "";
        const instructions = h("textarea", { id: "quote-instructions", name: "payment_instructions", maxlength: 1000, placeholder: "The shop's approved payment instructions (configured e-wallet number, bank details, etc.)" });
        instructions.value = order.payment_instructions || "";
        const note = h("textarea", { id: "quote-note", name: "note", maxlength: 1000, placeholder: "Optional message shown with the quotation (what it includes, lead time, …)" });
        note.value = quote?.note || "";
        const submit = h("button", { className: "button button-primary", type: "submit", text: revising ? "Send revised quotation" : "Send quotation" });
        const form = h("form", { className: "action-form", dataset: { focus: "quotation" } },
            h("h4", { text: revising ? `Revise quotation (currently revision ${quote.revision}, ${quote.status})` : "Send quotation" }),
            h("p", { className: "field-note", text: `${estimate === null ? "No catalog estimate for this request." : `Catalog estimate for reference only: ${money(estimate)}.`} Enter the customization price yourself. The customer is notified, must accept it, and then pays the down payment.${revising ? " The earlier revision is kept in the quotation history and can no longer be accepted." : ""}` }),
            h("div", { className: "form-grid" },
                h("div", { className: "form-field" }, h("label", { for: "quote-amount", text: fee ? `Printing amount (PHP) · + ${money(fee)} delivery fee` : "Quoted total (PHP)" }), amount),
                h("div", { className: "form-field" }, h("label", { for: "quote-down-payment", text: "Down payment (PHP, optional)" }), down, h("p", { className: "field-note", text: "Leave blank to use 50% of the quoted total including delivery." })),
                h("div", { className: "form-field full" }, h("label", { for: "quote-instructions", text: "Payment instructions" }), instructions),
                h("div", { className: "form-field full" }, h("label", { for: "quote-note", text: "Message to the customer (optional)" }), note)),
            h("div", { className: "dialog-actions" }, submit),
        );
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            const total = Number(amount.value);
            const answer = await UI.confirm({
                title: revising ? "Send the revised quotation?" : "Send this quotation?",
                message: `The customer is notified and accepts or declines the quotation in their portal. After accepting, they pay the down payment.${revising ? " Any earlier acceptance is cleared." : ""}`,
                confirmLabel: revising ? "Send revised quotation" : "Send quotation",
                details: [
                    revising ? ["Previous quotation", money(quote.total ?? quote.amount)] : null,
                    fee ? ["Printing amount", money(total)] : null,
                    fee ? ["Delivery fee (from checkout)", money(fee)] : null,
                    ["Quoted total", money(total + fee)],
                    down.value ? ["Required down payment", money(Number(down.value))] : ["50% down payment (approx.)", money(Math.round((total + fee) * 50) / 100)],
                ].filter(Boolean),
            });
            if (!answer) return;
            await submitAction(submit, order, "quotation", {
                total_amount: total,
                down_payment_amount: down.value === "" ? null : Number(down.value),
                payment_instructions: instructions.value.trim(),
                note: note.value.trim(),
            }, reload, revising ? "Revised quotation sent to the customer." : "Quotation sent to the customer.");
        });
        return form;
    }

    function confirmPackageForm(order, reload) {
        const cod = order.payment?.payment_option === "cod";
        const instructions = h("textarea", { id: "confirm-instructions", maxlength: 1000, placeholder: "The shop's configured e-wallet number and account name" });
        instructions.value = order.payment_instructions || "";
        const down = h("input", { id: "confirm-down-payment", type: "number", min: "0.01", max: String(order.total_amount || 100000000), step: "0.01", placeholder: `50% of ${money(order.total_amount)}` });
        down.value = order.required_down_payment ?? "";
        const submit = h("button", { className: "button button-primary", type: "submit", text: "Approve & request down payment" });
        const form = h("form", { className: "action-form" },
            h("h4", { text: order.price_basis === "standard_size" ? "Approve the standard-size order" : "Approve the package order" }),
            h("p", { className: "field-note", text: `Fixed ${order.price_basis === "standard_size" ? "price-list" : "package"} total ${money(order.total_amount)}. The customer then pays the down payment of ${money(order.payment?.down_payment_required)} through the shop's configured e-wallet(s); the order is confirmed when you verify it.${cod ? " This order was placed with Cash on Delivery, which is no longer offered: approving switches it to the down payment." : ""}` }),
            h("div", { className: "form-field" }, h("label", { for: "confirm-down-payment", text: "Down payment (PHP, optional)" }), down, h("p", { className: "field-note", text: "Leave blank to use the default 50% down payment." })),
            h("div", { className: "form-field full" }, h("label", { for: "confirm-instructions", text: "Payment instructions" }), instructions),
            h("div", { className: "dialog-actions" }, submit),
        );
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            const answer = await UI.confirm({ title: order.price_basis === "standard_size" ? "Approve this order?" : "Approve this package order?", message: "The customer is notified to pay the down payment and sees the payment instructions in their portal.", confirmLabel: "Approve order" });
            if (!answer) return;
            await submitAction(submit, order, "confirm", {
                down_payment_amount: down.value === "" ? null : Number(down.value),
                payment_instructions: instructions.value.trim(),
            }, reload, "Order approved. Down payment requested.");
        });
        return form;
    }

    /** Correct the agreed total or set a custom down payment while no money has been recorded yet. */
    function paymentTermsForm(order, reload) {
        const currentTotal = order.total_amount ?? order.payment?.confirmed_total ?? "";
        const storedTotal = currentTotal === "" ? null : Number(currentTotal);
        const storedDown = order.required_down_payment === null || order.required_down_payment === undefined
            ? null : Number(order.required_down_payment);
        const storedInstructions = String(order.payment_instructions || "");
        const fee = Math.round((Number(order.payment?.delivery_fee ?? order.shipping?.shipping_fee ?? 0) || 0) * 100) / 100;
        const roundMoney = (value) => Math.round((Number(value) + Number.EPSILON) * 100) / 100;
        const total = h("input", { id: "payment-terms-total", type: "number", min: "0.01", max: "100000000", step: "0.01", required: true, value: currentTotal });
        const down = h("input", { id: "payment-terms-down", type: "number", min: "0.01", max: String(currentTotal || 100000000), step: "0.01" });
        down.value = order.required_down_payment ?? "";
        const instructions = h("textarea", { id: "payment-terms-instructions", maxlength: 1000, placeholder: "The shop's configured e-wallet number and account name" });
        instructions.value = storedInstructions;
        const noteLabel = h("label", { for: "payment-terms-note", text: "Reason (optional)" });
        const note = h("textarea", { id: "payment-terms-note", maxlength: 1000, placeholder: "Why the terms are being changed (recorded in the order history)" });
        const breakdownView = h("div", { className: "od-money-grid", dataset: { focus: "payment-terms-preview" } });
        const submit = h("button", { className: "button button-primary", type: "submit", text: "Save payment terms" });

        const readMoney = (input) => input.value.trim() === "" ? null : Number(input.value);
        const snapshot = () => {
            const value = readMoney(total);
            const downValue = readMoney(down);
            const validTotal = Number.isFinite(value) && value > 0;
            const effectiveDown = downValue === null && validTotal ? roundMoney(value * 0.5) : downValue;
            const validDown = Number.isFinite(effectiveDown) && effectiveDown > 0 && (!validTotal || effectiveDown <= value + MONEY_TOLERANCE);
            return {
                value, downValue, validTotal, effectiveDown, validDown,
                subtotal: validTotal ? roundMoney(value - fee) : null,
                fee,
                remaining: validTotal && validDown ? roundMoney(value - effectiveDown) : null,
            };
        };
        const isChanged = (state) => {
            const totalChanged = storedTotal === null ? state.validTotal : Math.abs(storedTotal - (state.value ?? 0)) > MONEY_TOLERANCE;
            const downChanged = Number.isFinite(storedDown)
                ? Math.abs(storedDown - (state.effectiveDown ?? 0)) > MONEY_TOLERANCE
                : state.effectiveDown !== null;
            return totalChanged || downChanged || instructions.value.trim() !== storedInstructions;
        };
        const breakdownRows = (state) => {
            const rows = [
                ["Printing subtotal", money(state.subtotal)],
                ["Delivery fee", money(state.fee)],
                ["Confirmed total", money(state.value)],
                state.downValue === null
                    ? ["Required down payment", `${money(state.effectiveDown)} (50% default)`]
                    : ["Required down payment", state.validDown ? `${money(state.effectiveDown)} (custom)` : "—"],
                ["Remaining after down payment", state.remaining === null ? "—" : money(state.remaining)],
                ["Amount due next", state.validDown ? money(state.effectiveDown) : "—"],
            ];
            return rows;
        };
        const refresh = () => {
            const state = snapshot();
            const limit = state.validTotal ? state.value : 100000000;
            down.max = String(limit);
            down.placeholder = state.validTotal
                ? `50% of ${money(roundMoney(state.value * 0.5))} — leave blank to use this`
                : "50% of the total";
            breakdownView.replaceChildren(...breakdownRows(state).map(([label, value]) =>
                h("div", { className: "od-money" }, h("span", { text: label }), h("strong", { text: value }))));
            const changed = isChanged(state);
            noteLabel.text = changed ? "Reason for this change (required)" : "Reason (optional)";
            note.required = changed;
        };
        total.addEventListener("input", refresh);
        down.addEventListener("input", refresh);
        instructions.addEventListener("input", refresh);
        refresh();

        const form = h("form", { className: "action-form", dataset: { focus: "payment-terms" } },
            h("h4", { text: "Set payment terms" }),
            h("p", { className: "field-note", text: "Available only before a payment is submitted or recorded. Leave the down payment blank to use the default 50%. Review the calculated breakdown before saving." }),
            h("div", { className: "form-grid" },
                h("div", { className: "form-field" }, h("label", { for: "payment-terms-total", text: "Confirmed total (PHP, including delivery fee)" }), total),
                h("div", { className: "form-field" }, h("label", { for: "payment-terms-down", text: "Required down payment (PHP, optional)" }), down),
                h("div", { className: "form-field full" }, h("label", { for: "payment-terms-instructions", text: "Payment instructions" }), instructions),
                h("div", { className: "form-field full" }, noteLabel, note)),
            h("h4", { text: "Payment breakdown preview" }),
            breakdownView,
            h("p", { className: "field-note", text: "The customer is notified with this breakdown and the payment instructions. The reason is kept in the order history and audit log." }),
            h("div", { className: "dialog-actions" }, submit));
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            const state = snapshot();
            if (!state.validTotal) {
                toast("Enter a valid confirmed total.", "error");
                total.focus();
                return;
            }
            if (state.downValue !== null && (!Number.isFinite(state.downValue) || state.downValue <= 0)) {
                toast("The down payment must be greater than zero.", "error");
                down.focus();
                return;
            }
            if (state.downValue !== null && state.downValue > state.value + MONEY_TOLERANCE) {
                toast("The down payment cannot exceed the total.", "error");
                down.focus();
                return;
            }
            if (isChanged(state) && !note.value.trim()) {
                toast("Enter the reason for this change.", "error");
                note.focus();
                return;
            }
            const details = breakdownRows(state).filter(([, value]) => value !== "—");
            if (note.value.trim()) details.push(["Reason", note.value.trim()]);
            const answer = await UI.confirm({
                title: "Save these payment terms?",
                message: "The customer is notified with this breakdown and sees the updated amounts in their portal.",
                confirmLabel: "Save terms",
                details,
            });
            if (!answer) return;
            await submitAction(submit, order, "payment-terms", {
                total_amount: state.value,
                down_payment_amount: state.downValue,
                payment_instructions: instructions.value.trim(),
                note: note.value.trim(),
            }, reload, "Payment terms saved and the customer was notified.");
        });
        return form;
    }

    /** Upload the finished work so the customer can approve it before paying the balance. */
    function workPreviewForm(order, reload) {
        const file = h("input", { id: "work-preview-file", type: "file", accept: ".jpg,.jpeg,.png,.pdf,image/jpeg,image/png,application/pdf", required: true });
        const remarks = h("textarea", { id: "work-preview-remarks", maxlength: 1000, placeholder: "Optional notes for the customer about the finished work" });
        remarks.value = order.work_approval?.remarks || "";
        const submit = h("button", { className: "button button-primary", type: "submit", text: order.work_approval?.sent_at ? "Send updated work for review" : "Send finished work for review" });
        const form = h("form", { className: "action-form", dataset: { focus: "work-preview" } },
            h("h4", { text: order.work_approval?.sent_at ? "Resend finished work for review" : "Send finished work for review" }),
            h("p", { className: "field-note", text: "The customer reviews this file in their portal. The remaining balance stays locked until they approve it." }),
            h("div", { className: "form-grid" },
                h("div", { className: "form-field full" }, h("label", { for: "work-preview-file", text: "Finished-work preview (JPG, PNG, or PDF · max 5 MB)" }), file),
                h("div", { className: "form-field full" }, h("label", { for: "work-preview-remarks", text: "Remarks for the customer (optional)" }), remarks)),
            h("div", { className: "dialog-actions" }, submit));
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            const chosen = file.files?.[0];
            if (!chosen || !/\.(jpe?g|png|pdf)$/i.test(chosen.name) || !["image/jpeg", "image/png", "application/pdf"].includes(chosen.type)) {
                toast("Choose a JPG, PNG, or PDF finished-work preview.", "error");
                return;
            }
            if (chosen.size > 5 * 1024 * 1024) {
                toast("Finished-work previews must be no larger than 5 MB.", "error");
                return;
            }
            const answer = await UI.confirm({ title: "Send this work for customer review?", message: `${chosen.name} will be shared with the customer. Final payment stays locked until they approve it.`, confirmLabel: "Send for review" });
            if (!answer) return;
            const dataUrl = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.addEventListener("load", () => resolve(reader.result));
                reader.addEventListener("error", () => reject(new Error(`Could not read ${chosen.name}.`)));
                reader.readAsDataURL(chosen);
            });
            await submitAction(submit, order, "work-preview", { file: { name: chosen.name, dataUrl }, remarks: remarks.value.trim() }, reload, "Finished work sent for customer review.");
        });
        return form;
    }

    function fmtAmount(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? money(parsed) : "—";
    }

    /** The customer's original receipt, with the shared zoom/fullscreen viewer (no separate page). */
    function receiptPreview(item) {
        const proof = item.proof;
        if (!proof) return h("p", { className: "field-note", text: "No receipt attached (older submission)." });
        if (!String(proof.mime_type || "").startsWith("image/")) {
            return h("a", { href: proof.url, target: "_blank", rel: "noopener", className: "button button-small", text: `📄 Open ${proof.name}` });
        }
        const open = () => UI.openViewer([{
            src: proof.url, name: proof.name,
            caption: `Customer-uploaded receipt · ${proof.name}`,
            original: proof.url,
        }]);
        return h("div", { className: "receipt-preview" },
            h("button", { className: "receipt-preview-image", type: "button", title: "Zoom and fullscreen preview", onclick: open },
                h("img", { src: proof.url, alt: `Customer-uploaded receipt ${proof.name}`, loading: "lazy" })),
            h("div", { className: "receipt-preview-actions" },
                h("button", { className: "button button-small", type: "button", text: "Zoom / fullscreen", onclick: open }),
                h("a", { href: proof.url, target: "_blank", rel: "noopener", className: "button button-small", text: "Open original ↗" })));
    }

    function verificationStatusChips(confirmation) {
        const statuses = (confirmation && confirmation.statuses) || [];
        return statuses.length
            ? h("div", { className: "verify-statuses" }, ...statuses.map((status) =>
                h("span", { className: `verify-status verify-${status.tone || "info"}`, text: status.label })))
            : null;
    }

    /** Section C: read-only OCR extraction and the comparison with the shop's records. */
    function ocrReviewPanel(order, item) {
        const comparison = item.comparison || { rows: [] };
        const verification = item.verification || {};
        const confirmation = item.confirmation || {};
        const rows = (comparison.rows || []).map((row) => h("tr", { className: `ocr-row ocr-${row.state}` },
            h("th", { scope: "row", text: row.label }),
            h("td", { text: row.receipt_value }),
            h("td", { text: row.system_value || "—" }),
            h("td", { className: "ocr-state", text: { match: "Match", missing: "Missing", inconsistent: "Inconsistent", pending: "Not checked" }[row.state] || row.state })));
        const checks = (verification.checks || []).map((check) => h("li", { className: `ocr-check ocr-${check.state}` },
            h("strong", { text: `${check.label}: ` }), h("span", { text: check.detail })));
        return h("div", { className: "ocr-review" },
            verificationStatusChips(confirmation),
            h("p", { className: "notice-card", text: confirmation.notice || "OCR reads text only. It does not prove that money was received; check the shop's account before recording this payment." }),
            // Only the raw extraction lives here. Expected amounts and match results are shown once,
            // in the comparison table below, so the same facts are not repeated twice in the modal.
            detailList([
                ["Amount read", confirmation.extracted_amount !== null && confirmation.extracted_amount !== undefined ? fmtAmount(confirmation.extracted_amount) : "Not read"],
                ["Reference read", confirmation.extracted_reference || "Not read"],
                ["Method on receipt", confirmation.extracted_method || item.method || "Not read"],
                ["Date / time", confirmation.extracted_date_time || "Not read"],
                ["Sender", confirmation.extracted_sender || "Not read"],
                ["Recipient / account", confirmation.extracted_recipient || "Not read"],
                ["Scan confidence", Number.isFinite(Number(item.ocr?.confidence)) ? `${Math.round(Number(item.ocr.confidence) * 100)}%` : "—"],
            ]),
            rows.length ? h("table", { className: "ocr-table" },
                h("thead", {}, h("tr", {}, h("th", { text: "Field" }), h("th", { text: "Receipt" }), h("th", { text: "System" }), h("th", { text: "Result" }))),
                h("tbody", {}, ...rows)) : h("p", { className: "field-note", text: "No receipt information was extracted." }),
            checks.length ? h("details", { className: "od-disclosure" }, h("summary", {}, h("span", { text: "Validation results" })), h("ul", { className: "ocr-checks" }, ...checks)) : null,
            (item.admin_notes || []).length ? h("p", { className: "field-note", text: `Verification notes: ${item.admin_notes.map((note) => `${note.by}: ${note.note}`).join(" · ")}` }) : null,
        );
    }

    // Customer submissions and manually recorded payments now share one Confirm Received Payment form.
    // The submission is passed in when OCR has read the customer's receipt; everything is prefilled from it.

    const CASH_METHODS = ["Cash", "Cash on Delivery"];

    function paymentForm(order, reload, submission = null) {
        const summary = order.summary;
        const confirmation = submission?.confirmation || null;
        const ocrFields = (submission?.ocr?.fields) || {};
        const cod = order.payment?.payment_option === "cod";
        const moneyNow = adminPayment(order);
        const balanceNow = moneyNow.balanceDue ?? 0;
        const methods = order.payment_methods || ["GCash", "Maya", "Bank transfer", "Cash", "Cash on Delivery", "Other"];
        const suffix = submission ? submission.id : "new";
        const down = cod ? 0 : (moneyNow.requiredDown ?? (moneyNow.total ?? 0) * 0.5);
        const downShortfall = cod ? 0 : Math.max(0, Math.round((down - moneyNow.paid) * 100) / 100);

        // OCR values are the default. Everything the admin changes here is sent back as an audited correction.
        const extractedAmount = Number(ocrFields.amount);
        const initial = submission ? {
            amount: Number.isFinite(extractedAmount) && extractedAmount > 0 ? extractedAmount : Number(submission.amount),
            method: methods.includes(String(ocrFields.method || "")) ? String(ocrFields.method) : (submission.method || (cod ? "Cash on Delivery" : "GCash")),
            reference: String(ocrFields.reference || submission.reference || ""),
        } : {
            amount: !cod && downShortfall > 0.005 && downShortfall < balanceNow
                ? downShortfall : balanceNow,
            method: cod ? "Cash on Delivery" : "GCash",
            reference: "",
        };
        const hasOcr = Boolean(submission && (ocrFields.amount || ocrFields.reference || ocrFields.date_time));

        // There is deliberately no maximum: a real overpayment is recorded and flagged for review, never
        // silently clipped by the browser or rejected as if the shop could not receive it.
        const amount = h("input", {
            id: `payment-amount-${suffix}`, type: "number", min: "0.01", step: "0.01",
            required: true,
        });
        amount.value = Number.isFinite(initial.amount) ? initial.amount.toFixed(2) : "";
        const method = selectInput(methods.map((value) => [value, value]), initial.method, () => updateReferenceField());
        method.id = `payment-method-${suffix}`;
        const reference = h("input", { id: `payment-reference-${suffix}`, maxlength: 120, autocomplete: "off" });
        reference.value = initial.reference;
        const referenceLabel = h("label", { for: reference.id });
        const referenceNote = h("p", { className: "field-note" });
        const note = h("input", { id: `payment-note-${suffix}`, maxlength: 300, placeholder: "e.g. Checked the transaction in the shop's GCash account" });
        const received = h("input", { id: `payment-received-${suffix}`, type: "checkbox", required: true });
        const correct = h("button", { className: "button button-small", type: "button", text: "Correct extracted details" });
        const correctionHint = h("p", { className: "field-note", text: "Corrections to OCR-extracted values are recorded in the order's audit log." });
        const submit = h("button", { className: "button button-primary", type: "submit", text: submission ? "Confirm received payment" : "Confirm payment" });
        const reject = h("button", { className: "button button-danger-strong", type: "button", text: "Reject" });
        const request = h("button", { className: "button button-small", type: "button", text: "Request clearer receipt" });
        const addNote = h("button", { className: "button button-small", type: "button", text: "Add verification note" });
        const fill = (value, label) => h("button", {
            className: "button button-small", type: "button",
            text: label || (Math.abs(value - balanceNow) < 0.005 ? `Full balance ${money(value)}` : `Down payment ${money(value)}`),
            onclick: () => { amount.value = value.toFixed(2); updateAmountPreview(); },
        });
        const amountPreview = h("p", { className: "notice-card" });
        function updateAmountPreview() {
            const value = Number(amount.value);
            if (!Number.isFinite(value) || value <= 0) {
                amountPreview.textContent = "Enter the amount actually received to preview the cumulative total and status.";
                amountPreview.className = "notice-card";
                return;
            }
            const paidAfter = Math.round((moneyNow.paid + value) * 100) / 100;
            const exact = moneyNow.total === null ? null : Math.round((moneyNow.total - paidAfter) * 100) / 100;
            const statusAfter = paymentStatusAfter(moneyNow.total, down, paidAfter, cod);
            const review = exact !== null && exact < -MONEY_TOLERANCE;
            amountPreview.className = `notice-card${review ? " notice-warning" : ""}`;
            amountPreview.textContent = `After this payment: cumulative confirmed ${money(paidAfter)}, balance ${money(Math.max(0, exact ?? 0))}, status ${statusAfter}.`
                + (review ? ` Excess of ${money(-exact)} over the order total will be flagged for admin review.` : "")
                + (!cod && moneyNow.total !== null && paidAfter < down - MONEY_TOLERANCE
                    ? ` Down payment still short by ${money(Math.round((down - paidAfter) * 100) / 100)}.` : "");
        }
        amount.addEventListener("input", updateAmountPreview);
        updateAmountPreview();

        /** E-wallet and bank payments need their reference; cash may use an official receipt. */
        function updateReferenceField() {
            const cash = CASH_METHODS.includes(method.value);
            reference.required = !cash;
            reference.placeholder = cash ? "Official receipt number (optional)" : `${method.value} reference / transaction number`;
            referenceLabel.textContent = cash ? "Official receipt number (optional)" : `${method.value} reference number (required)`;
            referenceNote.textContent = cash
                ? "No receipt number? Leave it blank: the system issues a unique cash receipt number, and your confirmation below is recorded as the verification."
                : "Required so the payment can be traced and verified. Each reference can be recorded only once.";
        }
        updateReferenceField();

        // Read-only by default: an authorized admin unlocks the extracted values only to correct a bad read.
        function setLocked(locked) {
            amount.readOnly = locked;
            reference.readOnly = locked;
            method.disabled = locked;
            correct.textContent = locked ? "Correct extracted details" : "Restore extracted values";
            correctionHint.hidden = locked;
            form.classList.toggle("ocr-locked", locked);
        }

        correct.addEventListener("click", () => {
            const locked = !amount.readOnly;
            if (!locked) {
                amount.value = Number.isFinite(initial.amount) ? initial.amount.toFixed(2) : "";
                method.value = initial.method;
                reference.value = initial.reference;
                updateReferenceField();
                updateAmountPreview();
            }
            setLocked(locked);
            (locked ? correct : amount).focus();
        });

        const stage = submission?.stage === "balance" ? " (balance)"
            : submission?.stage === "down_payment" ? ` (${order.payment?.admin_set_down_payment ? "down payment" : "50% down payment"})` : "";
        const title = submission ? `Confirm Received Payment${stage}` : (cod ? "Confirm a Cash on Delivery payment" : "Confirm a received payment");
        const form = h("form", { className: "action-form confirm-payment-form", dataset: { focus: submission ? "verify" : "payment" } },
            h("h4", { text: title }),
            h("div", { className: "confirm-sections" },
                h("section", { className: "confirm-section" },
                    h("h5", { text: "A. Payment summary" }),
                    detailList([
                        ["Order Total", fmtAmount(moneyNow.total)],
                        ["Required Down Payment", cod ? "Cash on Delivery" : fmtAmount(moneyNow.requiredDown)],
                        ["Previously Received", fmtAmount(moneyNow.paid)],
                        ["Remaining Balance", fmtAmount(moneyNow.balanceDue)],
                        ["Down-payment Shortfall", cod ? "Not required" : fmtAmount(moneyNow.shortfall)],
                        ["Payment Status", moneyNow.status],
                    ]),
                    !submission ? h("div", { className: "quick-fill" },
                        !cod && downShortfall > 0.005 && downShortfall < balanceNow
                            ? fill(downShortfall, `Down-payment shortfall ${money(downShortfall)}`) : null,
                        fill(balanceNow)) : null),
                submission ? h("section", { className: "confirm-section" },
                    h("h5", { text: "B. Customer receipt" }),
                    h("p", { className: "field-note", text: `Uploaded ${dateTime(submission.submitted_at)} · ${submission.method} · ${submission.reference || "no reference entered"}` }),
                    receiptPreview(submission)) : null,
                submission ? h("section", { className: "confirm-section" },
                    h("h5", { text: "C. Receipt scan (assist only)" }),
                    ocrReviewPanel(order, submission)) : null,
                h("section", { className: "confirm-section" },
                    h("h5", { text: "D. Admin confirmation" }),
                    h("p", { className: "notice-card", text: submission
                        ? "The values below were filled from the receipt. OCR is preliminary evidence only: check the shop's account and confirm the money was actually received before recording it."
                        : "Record only money the shop has actually received. Payments cannot be edited after they are saved." }),
                    amountPreview,
                    hasOcr ? correctionHint : null,
                    h("div", { className: "form-grid" },
                        h("div", { className: "form-field" }, h("label", { for: amount.id, text: "Amount Received (PHP)" }), amount),
                        h("div", { className: "form-field" }, h("label", { for: method.id, text: "Payment Method" }), method, hasOcr ? correct : null),
                        h("div", { className: "form-field full" }, referenceLabel, reference, referenceNote),
                        h("div", { className: "form-field full" }, h("label", { for: note.id, text: "Verification Note" }), note),
                        h("label", { className: "check-field full", for: received.id }, received, " I confirm this payment has actually been received and checked against the reference.")))),
            h("div", { className: "dialog-actions" }, ...(submission ? [reject, request, addNote] : []), submit),
        );
        if (hasOcr) setLocked(true);

        request.addEventListener("click", async () => {
            const answer = await UI.confirm({
                title: "Request a clearer receipt?",
                message: "The customer is notified and can upload a new receipt. The earlier submission stays in the audit history and no payment is recorded.",
                confirmLabel: "Request new receipt",
                input: { label: "What should the customer show clearly?", required: false, placeholder: "e.g. the reference number and recipient name" },
            });
            if (!answer) return;
            await submitAction(request, order, `payment-submissions/${encodeURIComponent(submission.id)}/request-receipt`, { note: answer.value || "" }, reload, "The customer was asked for a clearer receipt.");
        });
        addNote.addEventListener("click", async () => {
            const answer = await UI.confirm({
                title: "Add a verification note?",
                message: "The note is kept with the submission's audit history and shown to the customer.",
                confirmLabel: "Add note",
                input: { label: "Verification note", required: true, placeholder: "e.g. Reference matched the shop's account after callback" },
            });
            if (!answer) return;
            await submitAction(addNote, order, `payment-submissions/${encodeURIComponent(submission.id)}/notes`, { note: answer.value }, reload, "Verification note added.");
        });
        reject.addEventListener("click", async () => {
            const answer = await UI.confirm({
                title: "Reject this payment submission?",
                message: "The customer sees the reason and can submit the payment again.",
                confirmLabel: "Reject submission",
                tone: "danger",
                input: { label: "Reason (shown to the customer)", required: true, placeholder: "e.g. Reference number not found in our e-wallet account" },
            });
            if (!answer) return;
            await submitAction(reject, order, `payment-submissions/${encodeURIComponent(submission.id)}/reject`, { note: answer.value }, reload, "Payment submission rejected.");
        });

        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            const value = Number(amount.value);
            const cash = CASH_METHODS.includes(method.value);
            if (!Number.isFinite(value) || value <= 0) {
                toast("Enter a payment amount greater than zero.", "error");
                amount.focus();
                return;
            }
            if (!cash && !reference.value.trim()) {
                toast(`Enter the ${method.value} reference number before recording this payment.`, "error");
                reference.focus();
                return;
            }
            const paidAfter = Math.round((moneyNow.paid + value) * 100) / 100;
            const remainingExact = moneyNow.total === null ? null : Math.round((moneyNow.total - paidAfter) * 100) / 100;
            const remaining = Math.max(0, remainingExact ?? 0);
            const statusAfter = paymentStatusAfter(moneyNow.total, down, paidAfter, cod);
            const adminReview = remainingExact !== null && remainingExact < -MONEY_TOLERANCE;
            const changes = [];
            if (Math.abs(value - initial.amount) > 0.005) changes.push(["Amount", `${fmtAmount(initial.amount)} → ${fmtAmount(value)}`]);
            if (method.value !== initial.method) changes.push(["Method", `${initial.method} → ${method.value}`]);
            if (reference.value.trim() !== initial.reference.trim()) changes.push(["Reference", `${initial.reference || "—"} → ${reference.value.trim() || "—"}`]);
            const answer = await UI.confirm({
                title: submission ? "Confirm receipt of this payment?" : "Record this payment?",
                message: submission
                    ? "The payment is recorded once in the history and cannot be edited. Only confirm after checking that the shop actually received the money."
                    : "Payments cannot be edited after they are saved.",
                confirmLabel: submission ? "Confirm received payment" : "Record payment",
                input: changes.length ? { label: "Reason for correcting the extracted details (audit log)", required: true, placeholder: "e.g. OCR read the last digit of the reference incorrectly" } : null,
                details: [
                    ["Amount", money(value)],
                    ["Method", method.value],
                    ["Reference", reference.value.trim() || (cash ? "Cash receipt number issued by the system" : "—")],
                    ...(confirmation ? [["Amount match", confirmation.amount_match ? "YES" : "NO"], ["Reference", confirmation.reference_matched ? "MATCHED" : "Needs review"]] : []),
                    ...changes,
                    ["Previously received", money(moneyNow.paid)],
                    ["Cumulative after payment", money(paidAfter)],
                    ["Remaining balance after", money(remaining)],
                    ["Payment status after", statusAfter],
                    ...(adminReview ? [["Admin review", `Excess of ${money(-remainingExact)} over the order total`]] : []),
                ],
            });
            if (!answer) return;
            const payload = {
                amount: value, method: method.value, reference: reference.value.trim(),
                verification_note: note.value.trim(), received: true,
            };
            if (submission) {
                payload.correction_note = changes.length ? (answer.value || "") : "";
                await submitAction(submit, order, `payment-submissions/${encodeURIComponent(submission.id)}/verify`, payload, reload,
                    adminReview
                        ? "Payment verified and flagged for admin review because it exceeds the order total."
                        : "Payment verified and recorded.");
                return;
            }
            await submitAction(submit, order, "payments", payload, reload,
                adminReview
                    ? "Payment recorded and flagged for admin review because it exceeds the order total."
                    : statusAfter === "Fully Paid" ? "Payment recorded. The order is now fully paid." : "Payment recorded.");
        });
        return form;
    }

    /** Older payments saved without a reference: let the administrator add the receipt or transaction number. */
    function addReferenceButton(order, payment, reload) {
        const button = h("button", { className: "button button-small", type: "button", text: "Add reference" });
        button.addEventListener("click", async () => {
            const cash = CASH_METHODS.includes(payment.method);
            const answer = await UI.confirm({
                title: `Add the reference for payment #${payment.number}`,
                message: cash
                    ? "Enter the official receipt number, or leave it blank to have the system issue a cash receipt number. Existing references cannot be changed later."
                    : `Enter the ${payment.method || "payment"} reference / transaction number exactly as shown in the shop's records. It cannot be changed later.`,
                confirmLabel: "Save reference",
                input: { label: cash ? "Official receipt number (optional)" : "Reference number", required: !cash, multiline: false, maxlength: 120, placeholder: cash ? "Leave blank to issue a cash receipt number" : "e.g. 1234 567 890123" },
                details: [["Amount", money(payment.amount)], ["Method", payment.method || "—"], ["Received", dateTime(payment.received_at)]],
            });
            if (!answer) return;
            await UI.busy(button, async () => {
                try {
                    await api(`/api/admin/orders/${encodeURIComponent(order.reference)}/payments/${payment.number}/reference`, {
                        method: "POST",
                        body: { reference: answer.value, confirmed: true, expected_version: order.version },
                    });
                    toast("Payment reference saved.", "success");
                    await reload();
                    refreshActiveView();
                    refreshBadges();
                } catch (error) {
                    toast(error.message, "error");
                    if (error.status === 409) await reload();
                }
            });
        });
        return h("span", { className: "cell-pair" }, h("span", { className: "table-secondary", text: "No reference recorded" }), button);
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

    const CUSTOMER_VIEWS = [
        ["all", "All customers", "Every registered customer account."],
        ["paid", "Paid records", "Customers whose agreed orders are all fully paid (verified payments cover every confirmed total)."],
        ["unpaid", "Unpaid records", "Customers with at least one agreed order that still has a balance (unpaid or partially paid)."],
    ];

    function renderCustomers() {
        const body = viewBody();
        if (!body) return;
        const f = filters.customers;
        if (!CUSTOMER_VIEWS.some(([value]) => value === f.view)) f.view = "all";
        const tableHost = h("div");
        const countLabel = h("span", { className: "table-secondary" });
        const summaryLine = h("p", { className: "field-note" });
        const viewNote = h("p", { className: "field-note view-note" });
        const tabs = h("div", { className: "segmented customer-views", role: "tablist", "aria-label": "Customer payment views" });
        let viewCounts = {};
        const paymentFilter = field("Payment status", selectInput([["", "Any payment status"], ...CUSTOMER_PAYMENT_STATUSES.map((value) => [value, value])], f.payment_status, (value) => apply({ payment_status: value })));

        function renderTabs() {
            tabs.replaceChildren(...CUSTOMER_VIEWS.map(([value, label]) => h("button", {
                className: `segmented-button${f.view === value ? " active" : ""}`,
                type: "button",
                role: "tab",
                "aria-selected": String(f.view === value),
                onclick: () => {
                    if (f.view === value) return;
                    // One view at a time: a customer is listed once, under the view that matches their payments.
                    apply({ view: value, payment_status: "" });
                },
            }, label, h("span", { className: "segmented-count", text: viewCounts[value] === undefined ? "" : number(viewCounts[value]) }))));
            viewNote.textContent = CUSTOMER_VIEWS.find(([value]) => value === f.view)[2];
            // The payment status filter only applies to All customers; the other views are defined by payment status.
            paymentFilter.hidden = f.view !== "all";
        }

        const table = new UI.DataTable(tableHost, {
            columns: [
                { label: "Customer name", sortKey: "name", render: (row) => h("span", { title: `Registered ${date(row.registered_date)}` },
                    cellPair(row.name, `${row.display_id}${row.last_order_date ? ` · Last order ${date(row.last_order_date)}` : " · No orders yet"}`)) },
                { label: "Contact", render: (row) => cellPair(row.phone || "—", row.email) },
                { label: "Total orders", sortKey: "orders", align: "right", render: (row) => number(row.total_orders) },
                { label: "Completed orders", sortKey: "completed", align: "right", render: (row) => number(row.completed_orders) },
                { label: "Outstanding balance", sortKey: "balance", align: "right", render: (row) => h("span", {
                    title: `Transactions ${money(row.total_transaction_amount)} · Paid ${money(row.total_paid)} · Spent ${money(row.total_spent)}`,
                    text: money(row.outstanding_balance),
                }) },
                { label: "Account status", sortKey: "payment_status", render: (row) => badge(row.payment_status) },
                { label: "Actions", render: (row) => h("span", { className: "row-actions" },
                    h("button", { className: "button button-small", type: "button", text: "View", title: `Open ${row.name}'s profile`, onclick: () => openCustomer(row.id) }),
                    h("button", { className: "button button-small", type: "button", text: "Print report", title: `Print ${row.name}'s report`, onclick: (event) => printCustomer(row.id, event.currentTarget) })) },
            ],
            defaultSort: { key: "registered", dir: "desc" },
            initialQuery: { ...f },
            fetch: (params) => api(`/api/admin/customers${query(params)}`),
            onLoad: (result) => {
                viewCounts = result.view_counts || {};
                renderTabs();
                countLabel.textContent = `${number(result.total)} customer${result.total === 1 ? "" : "s"}`;
                const s = result.summary;
                summaryLine.textContent = `${number(s.with_orders)} with orders · ${money(s.total_transaction_amount)} in transactions · ${money(s.total_paid)} paid · ${money(s.outstanding_balance)} outstanding · ${money(s.total_spent)} spent (completed and fully paid orders only).`;
            },
            emptyMessage: "No customer accounts have been registered yet.",
            filteredEmptyMessage: "No customers match this view and the selected filters.",
            onRowClick: (row) => openCustomer(row.id),
            rowTitle: (row) => `Open ${row.name}'s profile`,
        });
        function apply(values) {
            Object.assign(f, values);
            renderTabs();
            table.setQuery({ ...f });
        }
        const printButton = h("button", { className: "button button-small", type: "button" },
            h("span", { "aria-hidden": "true", "data-icon": "printer", "data-icon-size": "15", text: "⎙" }), " Print report");
        printButton.addEventListener("click", () => openPrintOptions({ ...f, sort: table.sort.key, dir: table.sort.dir }));
        renderTabs();
        body.replaceChildren(
            tabs,
            h("div", { className: "filter-bar" },
                searchInput("Search name, email, phone, or customer ID…", f.search, (value) => apply({ search: value })),
                field("Order activity", selectInput(CUSTOMER_ACTIVITY, f.activity, (value) => apply({ activity: value }))),
                paymentFilter,
                field("Registered from", dateInput(f.registered_from, (value) => apply({ registered_from: value }))),
                field("Registered to", dateInput(f.registered_to, (value) => apply({ registered_to: value }))),
                field("Ordered from", dateInput(f.ordered_from, (value) => apply({ ordered_from: value }))),
                field("Ordered to", dateInput(f.ordered_to, (value) => apply({ ordered_to: value }))),
                h("button", { className: "button button-small", type: "button", text: "Clear filters", onclick: () => {
                    Object.keys(f).forEach((key) => { f[key] = key === "view" ? f.view : ""; });
                    renderCustomers();
                } }),
            ),
            panel(["Customers ", countLabel], "Priority columns with direct View and Print report actions; select a row for the full transaction history. Balance tooltips show transactions, paid, and spent figures.", h("div", {}, viewNote, summaryLine, tableHost),
                printButton,
                UI.exportButtons("customers", () => ({ ...f, sort: table.sort.key, dir: table.sort.dir }))),
        );
        window.PrintIcons?.hydrate(body);
        table.load();
        refreshActiveView = () => table.reload();
    }

    // ================================================================ printable customer report

    const PAPER_SIZES = [["A4", "A4 (210 × 297 mm)"], ["letter", "Letter (8.5 × 11 in)"], ["legal", "Legal (8.5 × 14 in)"]];

    /** Print options (paper size, orientation, history), then the printable report of the current view. */
    function openPrintOptions(params) {
        const paper = selectInput(PAPER_SIZES, localStorage.getItem("printshop.admin.paper") || "A4", () => {});
        paper.id = "print-paper";
        const orientation = selectInput([["portrait", "Portrait"], ["landscape", "Landscape"]], "landscape", () => {});
        orientation.id = "print-orientation";
        const history = h("input", { id: "print-history", type: "checkbox" });
        const submit = h("button", { className: "button button-primary", type: "submit", text: "Print report" });
        const viewLabel = CUSTOMER_VIEWS.find(([value]) => value === params.view)?.[1] || "All customers";
        const form = h("form", { className: "print-options" },
            h("h2", { className: "dialog-heading", text: "Print customer report" }),
            h("p", { className: "dialog-subtitle", text: `${viewLabel}, with the search and filters currently applied, in the current sort order.` }),
            h("div", { className: "form-grid" },
                h("div", { className: "form-field" }, h("label", { for: "print-paper", text: "Paper size" }), paper),
                h("div", { className: "form-field" }, h("label", { for: "print-orientation", text: "Orientation" }), orientation),
                h("label", { className: "check-field full", for: "print-history" }, history, " Include each customer's order history and payments (longer report)")),
            h("div", { className: "dialog-actions" }, h("button", { className: "button", type: "button", text: "Cancel", onclick: () => UI.closeModal() }), submit),
        );
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            localStorage.setItem("printshop.admin.paper", paper.value);
            await UI.busy(submit, async () => {
                let data;
                try {
                    data = await api(`/api/admin/customers-report${query({ ...params, page_size: "all", include_history: history.checked ? "1" : "" })}`);
                } catch (error) {
                    toast(error.message, "error");
                    return;
                }
                printDocument(customerReportHtml(data, { viewLabel, paper: paper.value, orientation: orientation.value, params }));
                UI.closeModal();
            }, "Preparing…");
        });
        UI.openModal(form);
    }

    /** Print one customer's report from their profile (keeps the profile open; paper size from the last print). */
    async function printCustomer(customerId, button) {
        await UI.busy(button, async () => {
            try {
                const data = await api(`/api/admin/customers-report${query({ customer_id: customerId })}`);
                printDocument(customerReportHtml(data, { viewLabel: "This customer", paper: localStorage.getItem("printshop.admin.paper") || "A4", orientation: "portrait", params: {} }));
            } catch (error) {
                toast(error.message, "error");
            }
        }, "Preparing…");
    }

    function escapeHtml(value) {
        return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
    }

    /** Print an HTML document from a hidden frame, so only the report (no admin navigation) is printed. */
    function printDocument(html) {
        document.querySelector("#print-frame")?.remove();
        const frame = h("iframe", { id: "print-frame", title: "Printable report", "aria-hidden": "true", tabindex: "-1" });
        frame.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden";
        document.body.append(frame);
        const target = frame.contentWindow;
        target.document.open();
        target.document.write(html);
        target.document.close();
        const run = () => {
            target.focus();
            target.print();
            window.setTimeout(() => frame.remove(), 60000);
        };
        if (target.document.readyState === "complete") window.setTimeout(run, 50);
        else frame.addEventListener("load", run, { once: true });
    }

    function customerReportHtml(data, { viewLabel, paper, orientation, params }) {
        const e = escapeHtml;
        const filtersUsed = [
            params.search ? `Search “${params.search}”` : "",
            params.activity ? CUSTOMER_ACTIVITY.find(([value]) => value === params.activity)?.[1] : "",
            params.payment_status ? `Payment status: ${params.payment_status}` : "",
            params.registered_from || params.registered_to ? `Registered ${params.registered_from || "…"} to ${params.registered_to || "…"}` : "",
            params.ordered_from || params.ordered_to ? `Ordered ${params.ordered_from || "…"} to ${params.ordered_to || "…"}` : "",
        ].filter(Boolean);
        const sm = data.summary;
        const single = data.view === "single";
        const customerBlock = (c) => {
            const orders = c.orders ? `
                <h3>Order history (${c.orders.length})</h3>
                ${c.orders.length ? `<table><thead><tr><th>Order ID</th><th>Service / product</th><th>Requested</th><th>Status</th><th class="num">Total</th><th class="num">Paid</th><th class="num">Balance</th><th>Payment</th></tr></thead><tbody>
                ${c.orders.map((o) => `<tr><td>${e(o.reference)}</td><td>${e(o.product)}<br><small>${e(o.service)}</small></td><td>${e(date(o.request_date))}</td><td>${e(o.status)}</td><td class="num">${o.total_amount === null ? "—" : e(money(o.total_amount))}</td><td class="num">${e(money(o.amount_paid))}</td><td class="num">${o.balance === null ? "—" : e(money(o.balance))}</td><td>${e(o.payment_status)}</td></tr>`).join("")}
                </tbody></table>` : "<p class=\"muted\">No orders.</p>"}
                <h3>Payments (${c.payments.length})</h3>
                ${c.payments.length ? `<table><thead><tr><th>Date</th><th>Order ID</th><th>Method</th><th>Reference no.</th><th>Verification</th><th class="num">Amount</th></tr></thead><tbody>
                ${c.payments.map((pay) => `<tr><td>${e(dateTime(pay.received_at))}</td><td>${e(pay.reference)}</td><td>${e(pay.method)}</td><td>${e(pay.payment_reference || "—")}<br><small>${e(pay.reference_type_label)}</small></td><td>${e(pay.verification_status)}</td><td class="num">${e(money(pay.amount))}</td></tr>`).join("")}
                </tbody></table><p class="muted">Pending and rejected submissions are listed for reference and are not counted as paid.</p>` : "<p class=\"muted\">No payments.</p>"}` : "";
            return `<section class="customer">
                <h2>${e(c.name)} <small>${e(c.display_id)}</small></h2>
                <dl>
                    <dt>Email</dt><dd>${e(c.email)}</dd><dt>Contact number</dt><dd>${e(c.phone || "—")}</dd>
                    <dt>Registered</dt><dd>${e(date(c.registered_date))}</dd><dt>Last order</dt><dd>${e(c.last_order_date ? date(c.last_order_date) : "No orders yet")}</dd>
                    <dt>Orders</dt><dd>${e(number(c.total_orders))} (${e(number(c.completed_orders))} completed, ${e(number(c.pending_orders))} open, ${e(number(c.cancelled_orders))} cancelled)</dd>
                    <dt>Payment status</dt><dd>${e(c.payment_status)}</dd>
                    <dt>Total transactions</dt><dd>${e(money(c.total_transaction_amount))}</dd><dt>Total paid</dt><dd>${e(money(c.total_paid))}</dd>
                    <dt>Outstanding balance</dt><dd>${e(money(c.outstanding_balance))}</dd><dt>Amount spent</dt><dd>${e(money(c.total_spent))}</dd>
                </dl>${orders}</section>`;
        };
        const listTable = `<table class="customers"><thead><tr><th>Customer ID</th><th>Name</th><th>Email</th><th>Contact</th><th>Registered</th><th class="num">Orders</th><th class="num">Transactions</th><th class="num">Paid</th><th class="num">Balance</th><th class="num">Spent</th><th>Payment status</th></tr></thead><tbody>
            ${data.customers.map((c) => `<tr><td>${e(c.display_id)}</td><td>${e(c.name)}</td><td>${e(c.email)}</td><td>${e(c.phone || "—")}</td><td>${e(date(c.registered_date))}</td><td class="num">${e(number(c.total_orders))}</td><td class="num">${e(money(c.total_transaction_amount))}</td><td class="num">${e(money(c.total_paid))}</td><td class="num">${e(money(c.outstanding_balance))}</td><td class="num">${e(money(c.total_spent))}</td><td>${e(c.payment_status)}</td></tr>`).join("") || `<tr><td colspan="11" class="muted">No customers match this view and filters.</td></tr>`}
            </tbody><tfoot><tr><td colspan="5">Total (${e(number(sm.customers))} customers)</td><td class="num">${e(number(sm.total_orders))}</td><td class="num">${e(money(sm.total_transaction_amount))}</td><td class="num">${e(money(sm.total_paid))}</td><td class="num">${e(money(sm.outstanding_balance))}</td><td class="num">${e(money(sm.total_spent))}</td><td></td></tr></tfoot></table>`;
        const title = single ? `Customer Report — ${data.customers[0]?.name || ""}` : `Customer Report — ${viewLabel}`;
        return `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>${e(title)}</title><style>
            @page { size: ${paper} ${orientation}; margin: 12mm; }
            * { box-sizing: border-box; }
            body { margin: 0; color: #111; font: 10.5pt/1.4 "Segoe UI", Arial, sans-serif; }
            header { display: flex; justify-content: space-between; gap: 12px; align-items: flex-end; border-bottom: 2px solid #111; padding-bottom: 6px; margin-bottom: 10px; }
            header h1 { margin: 0; font-size: 16pt; }
            header p { margin: 2px 0 0; color: #444; font-size: 9pt; }
            .meta { text-align: right; }
            .totals { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 0 0 10px; font-size: 9.5pt; }
            .totals strong { display: block; font-size: 11pt; }
            table { width: 100%; border-collapse: collapse; margin: 4px 0 10px; font-size: 9pt; page-break-inside: auto; }
            thead { display: table-header-group; }
            tr { page-break-inside: avoid; }
            th, td { padding: 4px 6px; border: 1px solid #bbb; text-align: left; vertical-align: top; }
            th { background: #eee; font-weight: 700; }
            tfoot td { font-weight: 700; background: #f5f5f5; }
            .num { text-align: right; white-space: nowrap; }
            small, .muted { color: #555; }
            .customer { margin: 0 0 14px; page-break-inside: avoid; }
            .customer + .customer { border-top: 1px solid #999; padding-top: 10px; }
            .customer h2 { margin: 0 0 6px; font-size: 12.5pt; }
            .customer h3 { margin: 8px 0 2px; font-size: 10.5pt; }
            dl { display: grid; grid-template-columns: max-content 1fr max-content 1fr; gap: 2px 10px; margin: 0; font-size: 9.5pt; }
            dt { color: #555; } dd { margin: 0; }
            footer { margin-top: 10px; color: #555; font-size: 8.5pt; }
        </style></head><body>
            <header><div><h1>ImPRESS Print Works — ${e(title)}</h1><p>${e(filtersUsed.length ? filtersUsed.join(" · ") : single ? "Complete customer record" : "No filters applied")}</p></div>
            <div class="meta"><p>Generated ${e(dateTime(data.generated_at))}</p><p>Prepared by ${e(adminEmail || "Administrator")}</p></div></header>
            ${single ? "" : `<div class="totals"><span>Customers<strong>${e(number(sm.customers))}</strong></span><span>Total transactions<strong>${e(money(sm.total_transaction_amount))}</strong></span><span>Total paid<strong>${e(money(sm.total_paid))}</strong></span><span>Outstanding<strong>${e(money(sm.outstanding_balance))}</strong></span><span>Amount spent<strong>${e(money(sm.total_spent))}</strong></span></div>`}
            ${single || data.include_history ? data.customers.map(customerBlock).join("") : listTable}
            <footer>Total transactions = confirmed totals of orders that are not cancelled. Paid = verified payments only. Amount spent counts only completed and fully paid orders.</footer>
        </body></html>`;
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
                { label: "Payment", sortKey: "payment_status", render: (row) => h("span", { className: "cell-pair" }, badge(row.payment_status), h("span", { className: "table-secondary", text: row.payment_option_label })) },
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
                { label: "Reference no.", render: (row) => cellPair(row.payment_reference || "No reference recorded", row.reference_type_label) },
                { label: "Verification", sortKey: "verification_status", render: (row) => badge(row.verification_status) },
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
                h("div", { className: "header-actions" },
                    h("button", { className: "button button-small", type: "button", text: "Print report", onclick: (event) => printCustomer(customer.id, event.currentTarget) }),
                    UI.exportButtons("customer", () => ({ customer_id: customer.id }), { label: "Report" })),
            ),
            h("div", { className: "mini-stats" },
                miniStat("Total orders", number(customer.total_orders)),
                miniStat("Completed", number(customer.completed_orders)),
                miniStat("Pending", number(customer.pending_orders)),
                miniStat("Cancelled", number(customer.cancelled_orders)),
                miniStat("Total transactions", money(customer.total_transaction_amount), "Confirmed totals, excluding cancelled orders"),
                miniStat("Total payments received", money(customer.total_paid), "Includes payments on open orders"),
                miniStat("Outstanding balance", money(customer.outstanding_balance), "Still to be paid on open orders"),
                miniStat("Amount spent", money(customer.total_spent), "Completed and fully paid orders"),
                miniStat("Payment status", customer.payment_status),
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

    /** The customer's original receipt in the dedicated verification modal (images zoom, PDFs open). */
    function verificationReceipt(url, name, mimeType) {
        if (!url) return h("p", { className: "field-note", text: "No receipt is attached to this record." });
        if (!String(mimeType || "").startsWith("image/")) {
            return h("div", { className: "receipt-preview" },
                h("a", { href: url, target: "_blank", rel: "noopener", className: "button button-small", text: `📄 Open ${name || "receipt"}` }));
        }
        const open = () => UI.openViewer([{
            src: url, name: name || "Payment receipt",
            caption: `Customer-uploaded receipt · ${name || "payment receipt"}`, original: url,
        }]);
        return h("div", { className: "receipt-preview" },
            h("button", { className: "receipt-preview-image", type: "button", title: "Zoom and fullscreen preview", onclick: open },
                h("img", { src: url, alt: `Customer-uploaded receipt ${name || ""}`, loading: "lazy" })),
            h("div", { className: "receipt-preview-actions" },
                h("button", { className: "button button-small", type: "button", text: "Zoom / fullscreen", onclick: open }),
                h("a", { href: url, target: "_blank", rel: "noopener", className: "button button-small", text: "Open original ↗" })));
    }

    const RESULT_TONES = {
        verified: "success", ocr_checked_pending: "info", insufficient_payment: "danger",
        reference_mismatch: "danger", duplicate_reference: "danger", needs_manual_review: "warning",
        rejected: "danger",
    };

    /**
     * Clicking any payment row opens the verification record itself: the untouched receipt on the left and
     * everything OCR extracted, the money arithmetic, and the validation checks on the right.
     */
    async function openPaymentVerification(row, reload) {
        const host = h("div", { className: "payment-verification-modal" }, loadingBlock("Loading verification details…"));
        UI.openModal(host, { wide: true, size: "xl" });
        let order;
        try {
            order = await api(`/api/admin/orders/${encodeURIComponent(row.reference)}`);
        } catch (error) {
            host.replaceChildren(errorBlock(error.message, () => openPaymentVerification(row, reload)));
            return;
        }
        if (!dialog.open || !host.isConnected) return;
        const submission = row.submission_id
            ? (order.payment_submissions || []).find((item) => item.id === row.submission_id) : null;
        const payment = !submission && row.payment_number
            ? (order.payments || []).find((item) => item.number === row.payment_number) : null;
        const record = submission || payment || {};
        const confirmation = submission?.confirmation || {};
        const verification = submission?.verification || payment?.extraction_verification || {};
        const comparison = submission?.comparison || payment?.extraction_comparison || {};
        const ocrFields = record.ocr?.fields || {};
        const receiptUrl = row.receipt_url || record.proof?.url || record.receipt?.url || "";
        const receiptName = record.proof?.name || record.receipt?.name || "Payment receipt";
        const receiptMime = record.proof?.mime_type || record.receipt?.mime_type || "";
        const num = (...values) => {
            const found = values.find((value) => value !== null && value !== undefined && value !== "");
            const parsed = Number(found);
            return Number.isFinite(parsed) ? parsed : null;
        };
        const total = num(confirmation.total_order_amount, row.total_order_amount, order.total_amount);
        const required = num(confirmation.required_down_payment, row.required_down_payment);
        // A recorded payment uses the amount the administrator actually received (which may be a correction);
        // a pending submission still shows the amount OCR read from the receipt.
        const actual = num(row.entry_type === "payment" ? row.amount : null,
            confirmation.actual_amount_paid, row.actual_amount_paid, row.ocr_amount, row.amount);
        const remainingDown = num(confirmation.remaining_down_payment, row.remaining_down_payment);
        const remainingBalance = num(confirmation.remaining_balance_after, row.order_balance);
        const percentage = num(confirmation.payment_percentage, row.payment_percentage);
        const insufficient = num(confirmation.insufficient_by, row.insufficient_by);
        const excess = num(confirmation.excess_amount, row.excess_amount);
        const result = row.verification_result || confirmation.verification_result || verification.result || "";
        const statusLabel = row.verification_result_label || confirmation.verification_result_label || verification.result_label || row.verification_status || "—";
        const verificationDate = row.verification_date || confirmation.verification_date || verification.reviewed_at || record.reviewed_at || record.verified_at;
        const basis = verification.verification_basis_label || row.verification_basis_label || "";
        const checks = (verification.checks || []).map((check) => h("li", { className: `ocr-check ocr-${check.state}` },
            h("strong", { text: `${check.label}: ` }), h("span", { text: check.detail })));
        const pending = row.entry_type === "submission";

        host.replaceChildren(
            h("h2", { className: "dialog-heading", text: "Payment verification details" }),
            h("p", { className: "dialog-subtitle", text: `${row.reference} · ${row.customer_name} · ${row.method || "—"}` }),
            h("div", { className: "verify-statuses" },
                h("span", { className: `verify-status verify-${RESULT_TONES[result] || "info"}`, text: statusLabel }),
                basis ? h("span", { className: "verify-status verify-info", text: basis }) : null),
            h("div", { className: "verification-modal-grid" },
                h("section", { className: "verify-pane" },
                    h("h3", { text: "Original receipt" }),
                    verificationReceipt(receiptUrl, receiptName, receiptMime),
                    h("p", { className: "field-note", text: row.submitted_at ? `Submitted ${dateTime(row.submitted_at)}` : `Received ${dateTime(row.received_at)}` })),
                h("section", { className: "verify-pane" },
                    h("h3", { text: "Extracted payment information" }),
                    detailList([
                        ["Extracted reference", ocrFields.reference || row.ocr_reference || "Not read"],
                        ["Actual amount paid", actual === null ? "Not read" : fmtAmount(actual)],
                        ["Transaction date / time", ocrFields.date_time || row.ocr_date_time || "Not read"],
                        ["Payment sender", ocrFields.sender || row.ocr_sender || "Not read"],
                        ["Payment provider", ocrFields.method || row.ocr_method || record.method || "Not read"],
                        ["Recipient / account", ocrFields.recipient || ocrFields.account_info || row.ocr_account_info || "Not read"],
                        ["Receipt status", ocrFields.status || "Not read"],
                        ["OCR confidence", Number.isFinite(Number(record.ocr?.confidence)) ? `${Math.round(Number(record.ocr.confidence) * 100)}%` : "—"],
                        ["Verification status", statusLabel],
                        ["Verification date", verificationDate ? dateTime(verificationDate) : "—"],
                        ["Verification method", verification.verification_method || row.verification_method || "—"],
                    ]))),
            h("section", { className: "verify-pane verify-calculation" },
                h("h3", { text: "Automatic down payment calculation" }),
                detailList([
                    ["Total order amount", total === null ? "—" : fmtAmount(total)],
                    ["Required down payment", required === null ? "—" : fmtAmount(required)],
                    ["Actual amount paid", actual === null ? "—" : fmtAmount(actual)],
                    ["Remaining down payment", remainingDown === null ? "—" : fmtAmount(remainingDown)],
                    ["Remaining balance", remainingBalance === null ? "—" : fmtAmount(remainingBalance)],
                    ["Payment %", percentage === null ? "—" : `${percentage}%`],
                    insufficient > 0 ? ["Insufficient by", fmtAmount(insufficient)] : null,
                    excess > 0 ? ["Excess (admin review)", fmtAmount(excess)] : null,
                ]),
                h("p", { className: "field-note", text: "Figures are calculated on the server from the confirmed order total and earlier verified payments. Partial payments accumulate; confirmed payments are recorded only once." })),
            checks.length ? h("details", { className: "od-disclosure" },
                h("summary", {}, h("span", { text: "Validation checks" })),
                h("ul", { className: "ocr-checks" }, ...checks)) : null,
            (comparison.rows || []).length ? h("table", { className: "ocr-table" },
                h("thead", {}, h("tr", {}, h("th", { text: "Field" }), h("th", { text: "Receipt" }), h("th", { text: "System" }), h("th", { text: "Result" }))),
                h("tbody", {}, ...comparison.rows.map((item) => h("tr", { className: `ocr-row ocr-${item.state}` },
                    h("th", { scope: "row", text: item.label }),
                    h("td", { text: item.receipt_value }),
                    h("td", { text: item.system_value || "—" }),
                    h("td", { className: "ocr-state", text: { match: "Match", missing: "Missing", inconsistent: "Inconsistent" }[item.state] || item.state }))))) : null,
            h("div", { className: "dialog-actions" },
                !pending && excess > 0 && (payment?.excess_review_status || row.excess_review_status) !== "resolved"
                    ? h("button", {
                        className: "button button-small", type: "button", text: "Mark excess returned",
                        onclick: async () => {
                            const answer = await UI.confirm({
                                title: "Resolve the flagged excess?",
                                message: `PHP ${fmtAmount(excess)} was received above the order total and is not counted as paid. Confirm that it was returned to the customer.`,
                                confirmLabel: "Mark returned",
                                input: { label: "What happened to the excess? (recorded in the order history)", required: true, placeholder: "e.g. Returned via the original payment channel, ref 123456" },
                                details: [["Received", fmtAmount(num(row.received_amount, payment?.amount) || 0)], ["Credited", fmtAmount(num(row.applied_amount, payment?.applied_amount) || 0)]],
                            });
                            if (!answer) return;
                            try {
                                await api(`/api/admin/orders/${encodeURIComponent(row.reference)}/payments/${row.payment_number}/excess`, {
                                    method: "POST",
                                    body: { resolution: "returned", note: answer.value, expected_version: order.version },
                                });
                                UI.closeModal();
                                toast("The excess was marked returned. It was never counted as paid.", "success");
                                if (reload) await reload();
                            } catch (error) {
                                toast(error.message, "error");
                            }
                        },
                    })
                    : null,
                h("button", { className: "button button-small", type: "button", text: "Close", onclick: () => UI.closeModal() }),
                h("button", {
                    className: "button button-primary", type: "button",
                    text: pending ? "Review & verify in order" : "Open payment history",
                    onclick: () => openOrder(row.reference, { focus: pending ? "verify" : "payments" }),
                })),
        );
    }

    /** Keep the verification filter in the URL so a refresh of Payment Verification stays there. */
    function syncVerificationParam(value) {
        const url = new URL(window.location.href);
        if (value) url.searchParams.set("verification", value);
        else url.searchParams.delete("verification");
        if (url.pathname + url.search + url.hash !== window.location.pathname + window.location.search + window.location.hash) {
            window.history.replaceState(window.history.state, "", url);
        }
    }

    function renderPayments() {
        const body = viewBody();
        if (!body) return;
        const f = filters.payments;
        f.verification = new URLSearchParams(window.location.search).get("verification") || f.verification || "";
        syncVerificationParam(f.verification);
        const cards = h("section", { className: "stats-grid stats-grid-6", "aria-label": "Payment summary" }, loadingBlock());
        const tableHost = h("div");
        const countLabel = h("span", { className: "table-secondary" });
        const awaiting = h("p", { className: "notice-card notice-warning payment-awaiting", role: "status" }, loadingBlock("Checking pending submissions…"));
        const methodSelect = selectInput([["", "All methods"]], f.method, (value) => apply({ method: value }));
        const serviceSelect = selectInput(serviceOptions(), f.service, (value) => apply({ service: value }));
        let balances = [];
        /** Every row opens the verification record: receipt, extracted details, calculations, and checks. */
        const verificationAction = (row) => {
            const pending = row.entry_type === "submission" && Boolean(row.submission_id) && row.verification_status === "Pending Verification";
            return h("button", {
                className: `button button-small${pending ? " button-primary" : ""}`,
                type: "button",
                text: pending ? "Verify" : "Review",
                title: pending ? `Verify the payment submission for ${row.reference}` : `Open verification details for ${row.reference}`,
                onclick: () => openPaymentVerification(row, () => refreshActiveView()),
            });
        };
        const table = new UI.DataTable(tableHost, {
            // Constrained to the facts needed to decide whether a payment can be verified.
            // The full comparison (expected amount, prior payments, discrepancies, OCR)
            // stays in the verification panel opened from the row.
            columns: [
                { label: "Action", render: verificationAction },
                { label: "Date", sortKey: "received", type: "date", render: (row) => cellPair(dateTime(row.received_at), row.entry_type === "submission" ? "Submitted by customer" : "Received") },
                { label: "Order ID", sortKey: "reference", className: "ref-cell", render: (row) => cellPair(row.reference, row.entry_type === "payment" ? `Payment #${row.payment_number}` : "Submission") },
                { label: "Customer", sortKey: "customer", render: (row) => cellPair(row.customer_name, row.customer_email) },
                { label: "Method", sortKey: "method", render: (row) => cellPair(row.method, row.product) },
                { label: "Reference no.", sortKey: "payment_reference", render: (row) => row.payment_reference
                    ? cellPair(row.payment_reference, row.reference_type_label)
                    : h("span", { className: "cell-pair" }, h("span", { className: "table-primary missing-reference", text: "No reference" }), h("span", { className: "table-secondary", text: "Open to add it" })) },
                { label: "OCR (assist only)", sortKey: "ocr_amount", align: "right", render: (row) => h("span", { className: "cell-pair" },
                    h("strong", { text: row.ocr_amount === null || row.ocr_amount === undefined ? "Not read" : money(row.ocr_amount) }),
                    h("span", { className: "table-secondary", text: [
                        row.ocr_reference ? `Ref ${row.ocr_reference}` : "",
                        Number.isFinite(row.ocr_confidence) ? `${Math.round(row.ocr_confidence * 100)}% confidence` : "Reference not read",
                    ].filter(Boolean).join(" · ") })) },
                { label: "Amount counted", sortKey: "amount", align: "right", render: (row) => h("span", { className: "cell-pair" },
                    h(row.counts_as_paid ? "strong" : "span", { className: row.counts_as_paid ? "" : "not-counted", text: money(row.amount) }),
                    h("span", { className: "table-secondary", text: row.counts_as_paid ? "Recorded" : "Not counted until verified" })) },
                { label: "Verification", sortKey: "verification_result", render: (row) => h("span", { className: "cell-pair", title: row.verification_detail },
                    badge(row.verification_result_label || row.verification_status),
                    h("span", { className: "table-secondary", text: [
                        row.verification_basis_label || {
                            "Reference Missing": "Older record · add its reference",
                            "Pending Verification": "Not counted until verified",
                            "Rejected": row.verified_by ? `Rejected by ${row.verified_by}` : "Not counted",
                        }[row.verification_status] || (row.verified_by ? `By ${row.verified_by}` : ""),
                        row.insufficient_by > 0 ? `Short by ${money(row.insufficient_by)}` : "",
                        row.excess_review_status === "pending" && row.excess_amount > 0 ? `Excess ${money(row.excess_amount)} held for review` : "",
                        row.duplicate_reference_status === "Duplicate detected" ? "Duplicate ref" : "",
                    ].filter(Boolean).join(" · ") })) },
                { label: "Order balance", sortKey: "payment_status", render: (row) => h("span", { className: "cell-pair" }, badge(row.order_payment_status),
                    h("span", { className: "table-secondary", title: row.order_total === null ? "" : `Paid ${money(row.order_paid)} of ${money(row.order_total)}`, text: row.order_total === null ? "Not quoted" : `Balance ${money(row.order_balance)}` })) },
            ],
            // "Pending Verification" sorts before the other statuses, so submissions needing review come first.
            defaultSort: { key: "verification", dir: "asc" },
            initialQuery: { ...f },
            fetch: (params) => api(`/api/admin/payments${query(params)}`),
            onLoad: (result) => {
                const s = result.summary;
                balances = s.orders_with_balance;
                const pendingAmount = s.payments_to_verify.reduce((sum, item) => sum + item.amount, 0);
                cards.replaceChildren(
                    statCard({ label: "Payments to verify", value: number(s.payments_to_verify.length), foot: s.payments_to_verify.length ? `${money(pendingAmount)} submitted · Pending Verification` : "No submissions waiting", icon: "⚑", tone: s.payments_to_verify.length ? "highlight" : "", onClick: () => setVerification("pending") }),
                    statCard({ label: "Verified payments received", value: money(s.total_received), foot: "Only verified payments count as paid", icon: "₱", onClick: () => setVerification("verified") }),
                    statCard({ label: "Outstanding balance", value: money(s.outstanding_balance), foot: `${s.orders_with_balance.length} active order${s.orders_with_balance.length === 1 ? "" : "s"} with a balance`, icon: "◷" }),
                    statCard({ label: "Partially paid orders", value: number(s.partially_paid_orders), foot: `${number(s.awaiting_down_payment)} awaiting the 50% down payment (${money(s.down_payments_due)})`, icon: "½", onClick: () => { filters.orders = { ...filters.orders, status: "", payment_status: "Partially Paid" }; navigate("orders"); } }),
                    statCard({ label: "Fully paid orders", value: number(s.fully_paid_orders), foot: "Verified payments cover the confirmed total", icon: "✓", onClick: () => { filters.orders = { ...filters.orders, status: "", payment_status: "Fully Paid" }; navigate("orders"); } }),
                    statCard({ label: "Payments missing a reference", value: number(s.missing_reference), foot: s.missing_reference ? "Older records · open each one to add its reference" : "Every payment has a reference", icon: "✕", tone: s.missing_reference ? "highlight" : "", onClick: () => setVerification("missing") }),
                );
                awaiting.replaceChildren(
                    h("strong", { text: `${number(s.payments_to_verify.length)} awaiting verification` }),
                    s.payments_to_verify.length ? ` · ${money(pendingAmount)} submitted by customers. ` : " · Nothing waiting. ",
                    h("button", { className: "table-link", type: "button", text: s.payments_to_verify.length ? "Review pending →" : "View all payments →", onclick: () => setVerification(s.payments_to_verify.length ? "pending" : "") }),
                );
                const extra = s.filtered_entries - s.filtered_count;
                countLabel.textContent = `${number(s.filtered_count)} verified payment${s.filtered_count === 1 ? "" : "s"} · ${money(s.filtered_amount)}${extra ? ` · ${number(extra)} submission${extra === 1 ? "" : "s"} not counted` : ""} · ${number(s.payments_to_verify.length)} awaiting verification`;
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
            onRowClick: (row) => openPaymentVerification(row, () => table.reload()),
            rowTitle: (row) => `View verification details for ${row.reference}`,
            filteredEmptyMessage: "No payments match the selected filters.",
        });
        const apply = (values) => {
            Object.assign(f, values);
            if ("verification" in values) syncVerificationParam(values.verification);
            table.setQuery({ ...f });
        };
        const verificationSelect = selectInput(VERIFICATION_FILTERS, f.verification, (value) => apply({ verification: value }));
        function setVerification(value) {
            verificationSelect.value = value;
            apply({ verification: value });
        }
        const recordButton = h("button", { className: "button button-primary", type: "button", text: "＋ Record a payment", onclick: () => openBalancePicker(balances) });
        body.replaceChildren(
            h("div", { className: "page-actions-row" }, recordButton),
            cards,
            awaiting,
            h("div", { className: "filter-bar" },
                searchInput("Search order ID, customer, payment reference…", f.search, (value) => apply({ search: value })),
                field("Verification", verificationSelect),
                field("Method", methodSelect),
                field("Service", serviceSelect),
                field("Order payment status", selectInput([["", "All"], ...PAYMENT_STATUSES.map((value) => [value, value])], f.payment_status, (value) => apply({ payment_status: value }))),
                field("Payment option", selectInput([["", "All"], ...PAYMENT_OPTIONS], f.payment_option, (value) => apply({ payment_option: value }))),
                field("Received from", dateInput(f.date_from, (value) => apply({ date_from: value }))),
                field("Received to", dateInput(f.date_to, (value) => apply({ date_to: value }))),
                h("button", { className: "button button-small", type: "button", text: "Clear filters", onclick: () => {
                    Object.keys(f).forEach((key) => { f[key] = ""; });
                    syncVerificationParam("");
                    renderPayments();
                } }),
            ),
            panel(["Payment transactions ", countLabel], "Recorded payments (Verified) and customer submissions (Pending Verification or Rejected). Only verified payments count toward an order's paid amount. OCR readings are an assistant only — compare the receipt and reference yourself before verifying. Select a row to review.", tableHost,
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
                { label: "Status", sortKey: "status", render: (row) => cellPair(row.status, row.payment_option_label) },
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

    const REPORT_TABS = ["summary", "payments", "completed", "services"];
    const SUMMARY_PRESETS = new Set(["all", "today", "week", "month", "year", "custom"]);
    const SUMMARY_GROUPS = new Set(["", "day", "week", "month", "year"]);

    function cleanReportValue(value, max = 80) {
        return typeof value === "string" ? value.trim().slice(0, max) : "";
    }

    function readReportFiltersFromUrl() {
        const params = new URLSearchParams(window.location.search);
        const tab = cleanReportValue(params.get("tab") || params.get("rtab"), 20);
        if (REPORT_TABS.includes(tab)) filters.reports.tab = tab;

        const summary = filters.reports.summary;
        const preset = cleanReportValue(params.get("spreset"), 20);
        if (SUMMARY_PRESETS.has(preset)) summary.preset = preset;
        summary.date_from = cleanReportValue(params.get("sfrom"), 20);
        summary.date_to = cleanReportValue(params.get("sto"), 20);
        const groupBy = cleanReportValue(params.get("sgroup"), 20);
        summary.group_by = SUMMARY_GROUPS.has(groupBy) ? groupBy : "";
        summary.service = cleanReportValue(params.get("sservice"), 80);
        if (summary.preset === "custom" && !(summary.date_from && summary.date_to)) {
            // Keep custom selected but empty dates show the existing validation message on load.
        }

        const completed = filters.reports.completed;
        completed.service = cleanReportValue(params.get("cservice"), 80);
        completed.customer = cleanReportValue(params.get("ccustomer"), 120);
        completed.request_from = cleanReportValue(params.get("creqfrom"), 20);
        completed.request_to = cleanReportValue(params.get("creqto"), 20);
        completed.completed_from = cleanReportValue(params.get("ccompfrom"), 20);
        completed.completed_to = cleanReportValue(params.get("ccompto"), 20);
        const payStatus = cleanReportValue(params.get("cpay"), 40);
        completed.payment_status = PAYMENT_STATUSES.includes(payStatus) ? payStatus : "";
        completed.min_days = cleanReportValue(params.get("cmindays"), 10);
        completed.max_days = cleanReportValue(params.get("cmaxdays"), 10);

        const service = filters.reports.service;
        service.service = cleanReportValue(params.get("svservice"), 80);
        service.completed_from = cleanReportValue(params.get("svcompfrom"), 20);
        service.completed_to = cleanReportValue(params.get("svcompto"), 20);
        service.request_from = cleanReportValue(params.get("svreqfrom"), 20);
        service.request_to = cleanReportValue(params.get("svreqto"), 20);
    }

    function syncReportUrl() {
        const url = new URL(window.location.href);
        const params = url.searchParams;
        ["tab", "rtab", "spreset", "sfrom", "sto", "sgroup", "sservice", "cservice", "ccustomer", "creqfrom", "creqto", "ccompfrom", "ccompto", "cpay", "cmindays", "cmaxdays", "svservice", "svcompfrom", "svcompto", "svreqfrom", "svreqto"].forEach((key) => params.delete(key));
        const r = filters.reports;
        params.set("tab", r.tab);
        const s = r.summary;
        if (s.preset && s.preset !== "month") params.set("spreset", s.preset);
        if (s.date_from) params.set("sfrom", s.date_from);
        if (s.date_to) params.set("sto", s.date_to);
        if (s.group_by) params.set("sgroup", s.group_by);
        if (s.service) params.set("sservice", s.service);
        const c = r.completed;
        if (c.service) params.set("cservice", c.service);
        if (c.customer) params.set("ccustomer", c.customer);
        if (c.request_from) params.set("creqfrom", c.request_from);
        if (c.request_to) params.set("creqto", c.request_to);
        if (c.completed_from) params.set("ccompfrom", c.completed_from);
        if (c.completed_to) params.set("ccompto", c.completed_to);
        if (c.payment_status) params.set("cpay", c.payment_status);
        if (c.min_days) params.set("cmindays", c.min_days);
        if (c.max_days) params.set("cmaxdays", c.max_days);
        const sv = r.service;
        if (sv.service) params.set("svservice", sv.service);
        if (sv.completed_from) params.set("svcompfrom", sv.completed_from);
        if (sv.completed_to) params.set("svcompto", sv.completed_to);
        if (sv.request_from) params.set("svreqfrom", sv.request_from);
        if (sv.request_to) params.set("svreqto", sv.request_to);
        const next = `${url.pathname}${params.toString() ? `?${params.toString()}` : ""}${url.hash}`;
        const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
        if (next !== current) history.replaceState(history.state, "", next);
    }

    function renderReports() {
        const body = viewBody();
        if (!body) return;
        readReportFiltersFromUrl();
        syncReportUrl();
        const hosts = {
            summary: h("div"),
            payments: h("div"),
            completed: h("div"),
            services: h("div"),
        };
        const rendered = new Set();
        // One summary request feeds the executive and payments tabs; both update when its filters change.
        const summaryState = {
            data: null,
            error: null,
            loading: false,
            requestId: 0,
            listeners: new Set(),
            subscribe(listener) {
                this.listeners.add(listener);
                if (this.data) listener(this.data, null, false);
                else if (this.error) listener(null, this.error, false);
                return () => this.listeners.delete(listener);
            },
            notify(data, error, loading) {
                this.listeners.forEach((listener) => listener(data, error, loading));
            },
            async load() {
                const id = ++this.requestId;
                this.loading = true;
                this.notify(null, null, true);
                let data;
                try {
                    data = await api(`/api/admin/reports/summary${query({ ...filters.reports.summary })}`);
                } catch (error) {
                    if (id !== this.requestId) return;
                    this.loading = false;
                    this.data = null;
                    this.error = error;
                    this.notify(null, error, false);
                    return;
                }
                if (id !== this.requestId) return;
                this.loading = false;
                this.data = data;
                this.error = null;
                rememberServices(data.services);
                this.notify(data, null, false);
            },
        };
        reportSummaryState = summaryState;
        const loadSummaryIfNeeded = () => {
            if (!summaryState.data && !summaryState.error && !summaryState.loading) summaryState.load();
        };
        const show = (key) => {
            filters.reports.tab = key;
            syncReportUrl();
            if (!rendered.has(key)) {
                rendered.add(key);
                ({
                    summary: renderSummaryTab,
                    payments: renderPaymentReportTab,
                    completed: renderCompletedTab,
                    services: renderServiceTab,
                })[key](hosts[key], summaryState);
            }
            if (key === "summary" || key === "payments") loadSummaryIfNeeded();
        };
        body.replaceChildren(tabSet([
            ["summary", "Executive summary", hosts.summary],
            ["payments", "Payments", hosts.payments],
            ["completed", "Completed transactions", hosts.completed],
            ["services", "Service reports", hosts.services],
        ], filters.reports.tab, show));
        refreshActiveView = () => {
            rendered.clear();
            renderReports();
        };
    }

    /** Collapsed section that builds its tables/charts only the first time it is opened. */
    function lazyDetails(title, note, build) {
        const body = h("div", { className: "report-details-body" });
        const details = h("details", { className: "report-details" },
            h("summary", {}, h("strong", { text: title }), h("span", { className: "table-secondary", text: ` · ${note}` })),
            body,
        );
        let built = false;
        details.addEventListener("toggle", () => {
            if (!details.open || built) return;
            built = true;
            body.replaceChildren(loadingBlock("Preparing tables…"));
            try {
                body.replaceChildren(...build());
            } catch (error) {
                body.replaceChildren(errorBlock(error.message));
            }
        });
        return details;
    }

    function renderSummaryTab(host, state) {
        const f = filters.reports.summary;
        const content = h("div", {}, loadingBlock("Calculating…"));
        const loadCustom = () => {
            syncReportUrl();
            if (f.preset !== "custom") { state.load(); return; }
            if (f.date_from && f.date_to) { state.load(); return; }
            state.loading = false;
            state.data = null;
            state.error = new Error("Choose both a start and an end date for a custom period.");
            state.notify(null, state.error, false);
        };
        const fromInput = dateInput(f.date_from, (value) => { f.date_from = value; loadCustom(); });
        const toInput = dateInput(f.date_to, (value) => { f.date_to = value; loadCustom(); });
        const customFields = h("div", { className: "filter-inline" }, field("From", fromInput), field("To", toInput));
        const serviceSelect = selectInput(serviceOptions(), f.service, (value) => { f.service = value; loadCustom(); });
        const groupSelect = selectInput([["", "Automatic"], ["day", "Daily"], ["week", "Weekly"], ["month", "Monthly"], ["year", "Yearly"]], f.group_by, (value) => { f.group_by = value; loadCustom(); });
        const presetSelect = selectInput([
            ["all", "All time"], ["today", "Today"], ["week", "This week"], ["month", "This month"], ["year", "This year"], ["custom", "Custom date range"],
        ], f.preset, (value) => {
            f.preset = value;
            f.group_by = "";
            groupSelect.value = "";
            customFields.hidden = value !== "custom";
            loadCustom();
        });
        customFields.hidden = f.preset !== "custom";
        const params = () => ({ ...f });
        const shortcut = (label, preset, group) => {
            const button = h("button", {
                className: `segmented-button${f.preset === preset && (f.group_by || "day") === group ? " active" : ""}`,
                type: "button",
                text: label,
                onclick: () => {
                    f.preset = preset;
                    f.group_by = group;
                    presetSelect.value = preset;
                    groupSelect.value = group;
                    customFields.hidden = true;
                    shortcuts.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
                    state.load();
                },
            });
            return button;
        };
        const shortcuts = h("div", { className: "segmented sales-shortcuts", role: "group", "aria-label": "Sales report" },
            shortcut("Daily sales (today)", "today", "day"),
            shortcut("Weekly sales (this week)", "week", "day"),
            shortcut("Monthly sales (this month)", "month", "day"),
            shortcut("Year by month", "year", "month"));
        host.replaceChildren(
            shortcuts,
            h("div", { className: "filter-bar report-filters", role: "search", "aria-label": "Report period and service" },
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

        state.subscribe((data, error, loading) => {
            content.classList.toggle("is-refreshing", Boolean(loading));
            if (loading) return;
            if (error) {
                content.replaceChildren(errorBlock(error.message, () => state.load()));
                return;
            }
            if (!data) return;
            if (serviceSelect.options.length <= 1) {
                serviceSelect.replaceChildren(...serviceOptions().map(([value, text]) => new Option(text, value)));
                serviceSelect.value = f.service;
            }
            groupSelect.value = f.group_by;
            content.replaceChildren(...summaryContent(data));
        });
    }

    function summaryContent(data) {
        const t = data.totals;
        const p = data.payments;
        const group = data.period.group_by;
        const groupName = { day: "Daily", week: "Weekly", month: "Monthly", year: "Yearly" }[group];
        const executiveCards = h("section", { className: "stats-grid stats-grid-4 report-kpis", "aria-label": "Executive figures" },
            statCard({ label: "Confirmed sales", value: money(t.confirmed_revenue), foot: "Completed and fully paid orders", icon: "₱", tone: "highlight" }),
            statCard({ label: "Orders requested", value: number(t.total_orders), foot: `${number(t.new_customers)} new customers in this period`, icon: "▤" }),
            statCard({ label: "Completed orders", value: number(t.completed_orders), foot: `${number(t.pending_orders)} pending · ${number(t.processing_orders)} in progress`, icon: "✓" }),
            statCard({ label: "Outstanding balance", value: money(p.outstanding_balance), foot: `${number(p.awaiting_down_payment)} awaiting down payment`, icon: "◷" }),
        );
        const trendChart = h("div", { className: "chart-box" });
        UI.columnChart(trendChart, data.trend.map((bucket) => ({
            label: bucket.label, short: shortBucket(bucket.key, group), value: bucket.revenue, completed: bucket.completed_orders,
        })), {
            format: money, axisFormat: UI.compactMoney, emptyMessage: "No confirmed revenue in this period.",
            detail: (point) => `${point.completed} completed order${point.completed === 1 ? "" : "s"}`,
        });

        const details = lazyDetails("Detailed revenue, service & period figures", "Expand for the full tables and charts", () => {
            const allTotals = h("section", { className: "stats-grid stats-grid-8", "aria-label": "All summary totals" },
                statCard({ label: "Total customers", value: number(t.total_customers), foot: `${number(t.new_customers)} registered in this period`, icon: "👥" }),
                statCard({ label: "Total orders", value: number(t.total_orders), foot: "Requested in this period", icon: "▤" }),
                statCard({ label: "Completed orders", value: number(t.completed_orders), foot: "Completed in this period", icon: "✓" }),
                statCard({ label: "Pending orders", value: number(t.pending_orders), foot: `${number(t.processing_orders)} more in progress`, icon: "◷" }),
                statCard({ label: "Cancelled orders", value: number(t.cancelled_orders), foot: "Cancelled or rejected", icon: "✕" }),
                statCard({ label: "Fully paid orders", value: number(t.fully_paid_orders), foot: "Requested in this period", icon: "₱" }),
                statCard({ label: "Total sales", value: money(t.confirmed_revenue), foot: "Confirmed revenue: completed and fully paid", icon: "₱", tone: "highlight" }),
                statCard({ label: "Avg. completion time", value: t.average_completion_days === null ? "—" : days(t.average_completion_days), foot: "Calendar days, request to completion", icon: "⏱" }),
            );
            const revenueSummaryHost = h("div");
            new UI.DataTable(revenueSummaryHost, {
                columns: [
                    { label: "Service name", sortKey: "service", render: (row) => h("strong", { text: row.service }) },
                    { label: "Total completed transactions", sortKey: "completed_orders", align: "right", render: (row) => number(row.completed_orders) },
                    { label: "Total revenue", sortKey: "revenue", align: "right", render: (row) => h("strong", { text: money(row.revenue) }) },
                    { label: "Date range", render: () => data.period.label },
                ],
                rows: () => [...data.by_service],
                defaultSort: { key: "revenue", dir: "desc" },
                pageSize: 25,
                emptyMessage: "No services in the catalog.",
            }).load();
            const revenueChart = h("div", { className: "chart-box" });
            UI.barChart(revenueChart, data.by_service.map((item) => ({ label: item.service, value: item.revenue, share: item.revenue_share })), {
                format: money, emptyMessage: "No confirmed revenue in this period.", detail: (item) => `${item.share}% of revenue`,
            });
            const completedChart = h("div", { className: "chart-box" });
            UI.barChart(completedChart, data.by_service.map((item) => ({ label: item.service, value: item.completed_orders })), { emptyMessage: "No orders were completed in this period." });
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
                allTotals,
                panel("Revenue summary", `Completed transactions and revenue per service · ${data.period.label}`, revenueSummaryHost),
                h("div", { className: "dashboard-grid" },
                    panel("Revenue per service", "Confirmed revenue in this period", revenueChart),
                    panel("Completed orders per service", "Orders completed in this period", completedChart),
                    panel(`Revenue by period (${groupName.toLowerCase()})`, "Table view of the revenue trend", trendHost),
                ),
                panel("Service performance comparison", "Select a service to open its report", comparisonHost),
                panel("Revenue transactions", "Completed and fully paid orders in this period · select a row for details", transactionsHost),
            ];
        });

        return [
            h("p", { className: "report-period-note" }, h("strong", { text: data.period.label }),
                data.service ? ` · ${data.service.label}` : " · All services",
                " · Orders are counted by request date; completed orders and revenue by completion date. Revenue includes only completed and fully paid orders, each counted once."),
            executiveCards,
            panel(`Revenue trend (${groupName.toLowerCase()})`, data.period.label, trendChart),
            details,
        ];
    }

    /** Payments report reads the same summary response as the executive tab, so its figures cannot drift. */
    function renderPaymentReportTab(host, state) {
        const content = h("div", { className: "payment-report" }, loadingBlock("Calculating…"));
        host.replaceChildren(
            h("p", { className: "report-period-note", text: "Payments are cash flow by payment date; confirmed sales on the executive tab count only completed and fully paid orders. Change the reporting period and service there." }),
            content,
        );
        state.subscribe((data, error, loading) => {
            content.classList.toggle("is-refreshing", Boolean(loading));
            if (loading) return;
            if (error) {
                content.replaceChildren(errorBlock(error.message, () => state.load()));
                return;
            }
            if (!data) return;
            content.replaceChildren(...paymentReportContent(data));
        });
    }

    function paymentReportContent(data) {
        const p = data.payments;
        const cards = h("section", { className: "stats-grid stats-grid-4", "aria-label": "Payment figures" },
            statCard({ label: "Payments received", value: money(p.received_total), foot: `${number(p.payment_count)} payment${p.payment_count === 1 ? "" : "s"} received in this period`, icon: "₱" }),
            statCard({ label: "Down payments (50%)", value: money(p.down_payments), foot: `Balance payments ${money(p.balance_payments)}`, icon: "½" }),
            statCard({ label: "Cash on Delivery collected", value: money(p.cod_collections), foot: `${number(p.orders_cod)} COD · ${number(p.orders_down_payment)} down-payment orders requested`, icon: "⛟" }),
            statCard({ label: "Outstanding balance", value: money(p.outstanding_balance), foot: `${number(p.awaiting_down_payment)} awaiting down payment`, icon: "◷" }),
        );
        const methodHost = h("div");
        new UI.DataTable(methodHost, {
            columns: [
                { label: "Payment method", sortKey: "method", render: (row) => h("strong", { text: row.method }) },
                { label: "Payments", sortKey: "count", align: "right", render: (row) => number(row.count) },
                { label: "Amount received", sortKey: "amount", align: "right", render: (row) => money(row.amount) },
            ],
            rows: () => [...p.by_method],
            defaultSort: { key: "amount", dir: "desc" },
            pageSize: 10,
            emptyMessage: "No payments were received in this period.",
        }).load();
        return [
            h("p", { className: "report-period-note" }, h("strong", { text: data.period.label }),
                data.service ? ` · ${data.service.label}` : " · All services",
                " · Verified collections minus completed refunds", ` ${money(p.net_collections)}`, ` · Refunds completed ${number(p.refund_count)} (${money(p.refunds_completed)})`),
            cards,
            panel("Payment methods", `Money received in ${data.period.label} by payment date`, methodHost,
                UI.exportButtons("payment_summary", () => ({ ...filters.reports.summary }), { label: "Payment summary" })),
        ];
    }

    function completedColumns() {
        return [
            { label: "Transaction ID", sortKey: "reference", className: "ref-cell", render: (row) => h("span", { className: "table-primary", text: row.reference }) },
            { label: "Customer", sortKey: "customer", render: (row) => row.customer_name },
            { label: "Service", sortKey: "service", render: (row) => row.service },
            { label: "Product / description", sortKey: "description", className: "wrap-cell", render: (row) => row.description },
            { label: "Destination / shipping", render: (row) => row.delivery_province
                ? cellPair(`${row.delivery_province} · ${row.destination_region}`, `Subtotal ${money(row.order_subtotal ?? row.total_amount)} · fee ${money(row.shipping_fee || 0)}`)
                : "—" },
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
            syncReportUrl();
            table.setQuery({ ...f });
        };
        host.replaceChildren(
            h("div", { className: "filter-bar report-filters" },
                field("Service", serviceSelect),
                field("Customer", (() => { const input = h("input", { type: "search", placeholder: "Name or email", value: f.customer }); input.addEventListener("input", UI.debounce(() => apply({ customer: input.value.trim() }), 300)); return input; })()),
                field("Payment status", selectInput([["", "All"], ...PAYMENT_STATUSES.map((value) => [value, value])], f.payment_status, (value) => apply({ payment_status: value }))),
                field("Requested from", dateInput(f.request_from, (value) => apply({ request_from: value }))),
                field("Requested to", dateInput(f.request_to, (value) => apply({ request_to: value }))),
                field("Completed from", dateInput(f.completed_from, (value) => apply({ completed_from: value }))),
                field("Completed to", dateInput(f.completed_to, (value) => apply({ completed_to: value }))),
                field("Min. days", numberInput(f.min_days, (value) => apply({ min_days: value }), "0"), "narrow"),
                field("Max. days", numberInput(f.max_days, (value) => apply({ max_days: value }), "Any"), "narrow"),
                h("button", { className: "button button-small", type: "button", text: "Clear filters", onclick: () => {
                    Object.keys(f).forEach((key) => { f[key] = ""; });
                    syncReportUrl();
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
                onclick: () => { f.service = item.service_id; syncReportUrl(); loadCards(); },
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
            syncReportUrl();
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

    const NOTIFICATION_STATUS = [["", "All notifications"], ["unread", "Unread"], ["read", "Read"]];
    const NOTIFICATION_SOURCES = [["", "Customer and admin activity"], ["customer", "Customer activity"], ["admin", "Admin actions"]];
    const NOTIFICATION_CATEGORY_OPTIONS = [["", "All categories"], ["orders", "Orders"], ["payments", "Payments"], ["quotations", "Quotations & drafts"], ["customers", "Customer registrations"]];

    /**
     * The one notifications page: search and filters with server-side pagination. Filtering shows the
     * matching notifications in the list (never as toasts). Opening a notification marks it read.
     */
    function renderNotifications() {
        const body = viewBody();
        if (!body) return;
        const f = filters.notifications;
        const description = viewRoot.querySelector("[data-notifications-description]");
        const tableHost = h("div", { className: "notification-table" });
        const countLabel = h("span", { className: "table-secondary" });
        const statusTabs = h("div", { className: "segmented", role: "tablist", "aria-label": "Read status" });
        let unreadTotal = 0;

        function renderStatusTabs() {
            statusTabs.replaceChildren(...NOTIFICATION_STATUS.map(([value, label]) => h("button", {
                className: `segmented-button${f.status === value ? " active" : ""}`,
                type: "button",
                role: "tab",
                "aria-selected": String(f.status === value),
                onclick: () => apply({ status: value }),
            }, label, value === "unread" ? h("span", { className: "segmented-count", text: number(unreadTotal) }) : null)));
        }

        const table = new UI.DataTable(tableHost, {
            columns: [
                { label: "Notification", render: (row) => h("span", { className: `notification-row${row.read ? "" : " unread"}` },
                    h("span", { className: "notification-symbol", "aria-hidden": "true", text: notificationSymbol(row) }),
                    h("span", { className: "cell-pair" },
                        h("span", { className: "table-primary" }, row.read ? null : h("span", { className: "unread-dot", title: "Unread" }), row.title, row.read ? null : h("span", { className: "visually-hidden", text: " (unread)" })),
                        h("span", { className: "table-secondary notification-body", text: row.body }))) },
                { label: "Order / customer", render: (row) => cellPair(row.reference || "—", row.customer_name) },
                { label: "Source", render: (row) => badge(row.actor === "customer" ? "Customer" : "Admin") },
                { label: "When", render: (row) => h("time", { datetime: row.at, title: dateTime(row.at), text: UI.relativeTime(row.at) }) },
                { label: "", render: (row) => row.read ? h("span", { className: "table-secondary", text: "Read" }) : h("button", {
                    className: "table-link", type: "button", text: "Mark read",
                    onclick: async () => {
                        try {
                            await markNotificationsRead({ ids: [row.id] });
                            table.reload();
                        } catch (error) {
                            toast(error.message, "error");
                        }
                    },
                }) },
            ],
            initialQuery: { ...f },
            pageSize: 25,
            fetch: (params) => api(`/api/admin/notifications${query(params)}`),
            onLoad: (result) => {
                unreadTotal = result.unread_count;
                renderStatusTabs();
                markAll.disabled = !unreadTotal;
                countLabel.textContent = `${number(result.total)} notification${result.total === 1 ? "" : "s"}`;
                if (description) description.textContent = `Customer activity from real orders and registrations · ${number(unreadTotal)} unread. Administrator actions are listed for reference.`;
            },
            emptyMessage: "No notifications yet.",
            filteredEmptyMessage: "No notifications match the selected filters.",
            onRowClick: (row) => {
                if (!row.read) markNotificationsRead({ ids: [row.id] }).then(() => table.reload()).catch(() => {});
                if (row.reference) openOrder(row.reference);
                else if (row.customer_id) openCustomer(row.customer_id);
            },
            rowTitle: (row) => (row.reference ? `Open order ${row.reference}` : "Open customer profile"),
            caption: "Notifications",
        });
        function apply(values) {
            Object.assign(f, values);
            renderStatusTabs();
            table.setQuery({ ...f });
        }
        const markAll = h("button", { className: "button", type: "button", text: "Mark all as read" });
        markAll.addEventListener("click", () => UI.busy(markAll, async () => {
            try {
                await markNotificationsRead({ all: true });
                toast("All notifications marked as read.", "success");
                table.reload();
            } catch (error) {
                toast(error.message, "error");
            }
        }, "Saving…"));
        renderStatusTabs();
        body.replaceChildren(
            h("div", { className: "notifications-toolbar" }, statusTabs, markAll),
            h("div", { className: "filter-bar" },
                searchInput("Search title, details, order ID, or customer…", f.search, (value) => apply({ search: value })),
                field("Source", selectInput(NOTIFICATION_SOURCES, f.actor, (value) => apply({ actor: value }))),
                field("Category", selectInput(NOTIFICATION_CATEGORY_OPTIONS, f.category, (value) => apply({ category: value }))),
                field("From", dateInput(f.date_from, (value) => apply({ date_from: value }))),
                field("To", dateInput(f.date_to, (value) => apply({ date_to: value }))),
                h("button", { className: "button button-small", type: "button", text: "Clear filters", onclick: () => {
                    Object.keys(f).forEach((key) => { f[key] = ""; });
                    renderNotifications();
                } }),
            ),
            panel(["All notifications ", countLabel], "Newest first · select a notification to open its order or customer", tableHost),
        );
        table.load();
        refreshActiveView = () => table.reload();
    }

    // ================================================================ administrator profile

    function openProfile() {
        document.querySelectorAll(".profile-menu[open]").forEach((menu) => menu.removeAttribute("open"));
        UI.openModal(h("div", { className: "profile-modal" },
            h("header", { className: "order-modal-header" },
                h("div", {},
                    h("p", { className: "eyebrow", text: "Signed-in account" }),
                    h("h2", { className: "dialog-heading", text: "Administrator profile" }))),
            detailList([
                ["Name", "Administrator"],
                ["Email", adminEmail || "—"],
                ["Role", "Shop administrator (full access to orders, customers, payments, reports, catalog, and settings)"],
                ["Session", "Sessions end after 12 hours or when you log out."],
                ["Password", h("span", {}, "Change it with ", h("a", { href: "/admin/forgot-password", text: "Forgot password" }), " (a verification code is emailed to this address). Changing it signs out every session.")],
            ]),
        ));
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
        const backupHost = h("div", { className: "backup-panel" }, loadingBlock("Loading backup status…"));
        body.replaceChildren(
            ...(OPS()?.settingsPanels(opsContext()) || []),
            panel("Database backup & restore", "Download a copy of the shop's data, or restore one downloaded from this admin panel", backupHost),
            h("section", { className: "split-grid" },
                panel("Display preferences", "Saved in this browser", h("div", { className: "settings-list" },
                    h("div", { className: "settings-row" }, h("div", {}, h("strong", { text: "Compact tables" }), h("p", { text: "Use tighter row spacing in management tables." })), toggle),
                )),
                panel("How figures are calculated", "Rules used by the dashboard, reports, customer totals, and exports", h("ul", { className: "rules-list" },
                    h("li", { text: "Confirmed revenue includes only orders that are Completed and fully paid. Each order counts once, for its confirmed total, on the date it was completed." }),
                    h("li", { text: "Only verified payments count as paid. Customer submissions stay Pending Verification until the administrator verifies them; rejected submissions never count." }),
                    h("li", { text: "Every payment has a reference: the configured e-wallet or bank transaction number, the official receipt number, or a cash receipt number issued by the system." }),
                    h("li", { text: "A customer's amount spent uses the revenue rule, so customer totals add up to revenue." }),
                    h("li", { text: "Completion duration = completion date − request date, in calendar days (weekends included), on Philippine time." }),
                    h("li", { text: "An order can be marked Completed only after its full amount is recorded as paid." }),
                    h("li", { text: "Net collections = verified collections − completed refunds. Pending, rejected, and failed refunds are never deducted." }),
                    h("li", { text: "Packed orders are released for pickup or dispatch only when fully paid." }),
                )),
            ),
            panel("Recent administrator activity", "Actions recorded in order histories", activity),
        );
        renderBackupPanel(backupHost);
        api(`/api/admin/activity${query({ actor: "admin", limit: 15 })}`).then((data) => {
            activity.replaceChildren(...(data.events.length ? data.events.map((event) => h("div", { className: "activity-item" },
                h("span", { className: "activity-point" }),
                h("div", {}, h("p", { text: `${event.reference}: ${event.body}` }), h("time", { text: dateTime(event.at) })))) : [h("p", { className: "empty-state", text: "No administrator actions recorded yet." })]));
        }).catch((error) => activity.replaceChildren(errorBlock(error.message)));
    }

    const fileSizeText = (bytes) => (bytes ? UI.fileSize(bytes) : "—");

    async function renderBackupPanel(host) {
        let info;
        try {
            info = await api("/api/admin/backup");
        } catch (error) {
            host.replaceChildren(errorBlock(error.message, () => renderBackupPanel(host)));
            return;
        }
        const backupButton = h("button", { className: "button button-primary", type: "button" },
            h("span", { "aria-hidden": "true", text: "⭳" }), " Backup database");
        const statusLine = h("p", { className: "backup-status", role: "status", "aria-live": "polite" });
        const fileInput = h("input", { id: "restore-file", type: "file", accept: ".json,application/json" });
        const restoreButton = h("button", { className: "button button-danger-strong", type: "button", text: "Restore from backup…", disabled: true });
        const fileNote = h("p", { className: "field-note", text: `Choose a .json backup file downloaded from this admin panel (up to ${info.max_upload_mb} MB). Version 2 files also restore uploaded images, receipts, and database-backed files.` });
        let chosen = null;
        let selectionToken = 0;

        const last = info.last_backup;
        const lastRestore = info.last_restore;
        statusLine.textContent = last
            ? `Last backup: ${dateTime(last.at)} by ${last.by} · ${number(last.rows)} records${last.files ? ` · ${number(last.files)} files` : ""} · ${fileSizeText(last.size)}`
            : "No backup has been created yet.";

        backupButton.addEventListener("click", () => UI.busy(backupButton, async () => {
            statusLine.textContent = "Creating the backup…";
            let response;
            try {
                response = await fetch("/api/admin/backup", { method: "POST", credentials: "same-origin" });
            } catch {
                statusLine.textContent = "Backup failed: the server could not be reached.";
                toast("The backup could not be created. Check the connection and try again.", "error");
                return;
            }
            if (response.status === 401) {
                window.location.assign("/admin/login");
                return;
            }
            if (!response.ok) {
                const result = await response.json().catch(() => ({}));
                statusLine.textContent = `Backup failed: ${result.detail || `HTTP ${response.status}`}`;
                toast(result.detail || "The backup could not be created.", "error");
                renderHistory();
                return;
            }
            const name = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "")?.[1] || "printshop-backup.json";
            const blob = await response.blob();
            const link = h("a", { href: URL.createObjectURL(blob), download: name });
            document.body.append(link);
            link.click();
            link.remove();
            window.setTimeout(() => URL.revokeObjectURL(link.href), 4000);
            toast(`Backup created and downloaded (${name}).`, "success");
            renderBackupPanel(host);
        }, "Creating backup…"));

        fileInput.addEventListener("change", async () => {
            // A slower read of an earlier file must not replace a newer selection.
            const token = ++selectionToken;
            chosen = null;
            restoreButton.disabled = true;
            const file = fileInput.files?.[0];
            if (!file) {
                fileNote.textContent = `Choose a .json backup file downloaded from this admin panel (up to ${info.max_upload_mb} MB). Version 2 files also restore uploaded images, receipts, and database-backed files.`;
                return;
            }
            const invalid = (message) => {
                fileNote.textContent = message;
                fileNote.classList.add("field-error");
            };
            fileNote.classList.remove("field-error");
            if (!/\.json$/i.test(file.name)) return invalid("Choose a .json backup file.");
            if (file.size > info.max_upload_mb * 1024 * 1024) return invalid(`This file is larger than ${info.max_upload_mb} MB.`);
            let data;
            let text;
            try {
                text = await file.text();
                data = JSON.parse(text);
            } catch {
                if (token === selectionToken) invalid("This file is not a valid backup (it is not JSON).");
                return;
            }
            if (token !== selectionToken) return;
            if (data?.format !== "impression-printshop-backup" || typeof data.tables !== "object" || !data.counts) {
                return invalid("This file is not an ImPRESS Print Works backup.");
            }
            chosen = { file, data, text };
            const rows = Object.values(data.counts).reduce((sum, value) => sum + Number(value || 0), 0);
            fileNote.textContent = `${file.name} · file claims: created ${dateTime(data.created_at)}${data.created_by ? ` by ${data.created_by}` : ""} · ${number(rows)} records${data.file_count ? ` · ${number(data.file_count)} files` : ""}. These details are not verified until the server checks the signature and contents.`;
            restoreButton.disabled = false;
        });

        restoreButton.addEventListener("click", async () => {
            if (!chosen) return;
            const counts = chosen.data.counts || {};
            const answer = await UI.confirm({
                title: "Restore this backup?",
                message: "Restoring replaces ALL current customers, orders, payments, catalog, packages, and notifications with the data in this backup. Anything added or changed after the backup was created will be lost. The audit trail is never erased: every existing audit entry is kept, the backup's entries are merged in, and the restore itself is recorded. Your administrator sign-in is not changed. Download a backup of the current data first if you may need it. The details below are claims made by the selected file; the server verifies its signature and contents before applying anything. Type RESTORE to continue.",
                confirmLabel: "Restore backup",
                tone: "danger",
                input: { label: "Type RESTORE to confirm", required: true, multiline: false, maxlength: 20, placeholder: "RESTORE" },
                details: [
                    ["Claimed creation time", dateTime(chosen.data.created_at)],
                    ["Claimed customers", number(counts.customer_accounts || 0)],
                    ["Claimed orders", number(counts.orders || 0)],
                    ["Claimed catalog products", number(counts.catalog_products || 0)],
                    ["Claimed attached files", number(chosen.data.file_count || 0)],
                ],
            });
            if (!answer) return;
            if (answer.value !== "RESTORE") {
                toast("The restore was cancelled: type RESTORE exactly to confirm.", "error");
                return;
            }
            await UI.busy(restoreButton, async () => {
                statusLine.textContent = "Validating and restoring the backup… Do not close this page.";
                try {
                    const result = await api("/api/admin/backup/restore", { method: "POST", body: { confirmation: "RESTORE", backup_text: chosen.text } });
                    const total = Object.values(result.restored).reduce((sum, value) => sum + value, 0);
                    toast(`Backup restored: ${number(total)} records${result.files_restored ? ` and ${number(result.files_restored)} files` : ""}.`, "success");
                    refreshBadges();
                    renderBackupPanel(host);
                } catch (error) {
                    statusLine.textContent = `Restore failed: ${error.message}`;
                    toast(error.message, "error");
                    renderHistory();
                }
            }, "Restoring…");
        });

        const historyHost = h("div");
        async function renderHistory() {
            let items = info.history;
            try {
                items = (await api("/api/admin/backup")).history;
            } catch {
                // Keep the history already shown.
            }
            historyHost.replaceChildren(items.length
                ? h("div", { className: "table-wrap" }, h("table", { className: "data-table compact-table" },
                    h("thead", {}, h("tr", {}, ...["When", "Action", "Result", "By", "Details"].map((text) => h("th", { scope: "col", text })))),
                    h("tbody", {}, ...items.map((item) => h("tr", {},
                        h("td", { text: dateTime(item.at) }),
                        h("td", { text: item.type === "backup" ? "Backup" : "Restore" }),
                        h("td", {}, badge(item.status === "success" ? "Success" : "Failed")),
                        h("td", { text: item.by || "—" }),
                        h("td", { className: "wrap-cell", text: item.status !== "success" ? (item.error || "—")
                            : item.type === "backup" ? `${item.file} · ${number(item.rows)} records · ${item.files ? `${number(item.files)} files · ` : ""}${fileSizeText(item.size)}`
                                : `${number(item.rows)} records${item.files ? ` · ${number(item.files)} files` : ""} from the backup of ${dateTime(item.backup_created_at)}` }))))))
                : h("p", { className: "field-note", text: "No backups or restores yet." }));
        }
        renderHistory();

        host.replaceChildren(
            h("div", { className: "backup-grid" },
                h("section", { className: "backup-card" },
                    h("h3", { text: "Backup" }),
                    h("p", { className: "field-note", text: `Saves customers, orders with payments and history, the catalog and packages, audit log, notifications, and uploaded files (${number(info.database_files)} in the database, ${number(info.disk_files)} on disk) from the ${info.database} as a signed JSON file (version ${info.version}, up to ${info.max_upload_mb} MB). Administrator credentials are never included.` }),
                    backupButton,
                    statusLine,
                    info.signing_key_persistent ? null : h("p", { className: "notice-card notice-warning", text: "No persistent backup signing key is configured, so backups are signed with a temporary key and cannot be restored after the server restarts. Set PRINTSHOP_BACKUP_SIGNING_KEY or PRINTSHOP_SECRET_KEY in the server settings." }),
                    h("p", { className: "field-note", text: `Backup signing key: ${info.signing_key_source}. Rotate it with PRINTSHOP_BACKUP_PREVIOUS_SIGNING_KEYS so older backups remain restorable.` })),
                h("section", { className: "backup-card" },
                    h("h3", { text: "Restore" }),
                    h("p", { className: "field-note", text: lastRestore ? `Last restore: ${dateTime(lastRestore.at)} by ${lastRestore.by}. The audit trail was kept.` : "Replaces the current data with a backup. Everything is checked first; if anything fails, nothing is changed. The audit trail is never erased." }),
                    h("div", { className: "form-field" }, h("label", { for: "restore-file", text: "Backup file" }), fileInput),
                    fileNote,
                    restoreButton)),
            h("h3", { className: "backup-history-title", text: "Backup & restore history" }),
            historyHost,
        );
    }

    // ================================================================ global events

    document.addEventListener("click", async (event) => {
        // The top-bar profile menu closes on any click outside it or on one of its items.
        document.querySelectorAll(".profile-menu[open]").forEach((menu) => {
            if (!menu.contains(event.target) || event.target.closest(".profile-menu-list")) menu.removeAttribute("open");
        });
        // The notification dropdown closes on any click outside it.
        if (!event.target.closest("[data-notification-menu]")) closeNotificationPanel();
        const viewLink = event.target.closest("[data-view]");
        if (viewLink) {
            event.preventDefault();
            await navigate(viewLink.dataset.view, true, { tab: linkSection(viewLink) });
            return;
        }
        const action = event.target.closest("[data-action]");
        if (!action) return;
        switch (action.dataset.action) {
            case "toggle-nav-group":
                toggleNavGroup(action);
                break;
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
            case "toggle-notifications":
                toggleNotificationPanel(action);
                break;
            case "profile":
                openProfile();
                break;
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
        if (event.key !== "Escape") return;
        closeSidebar();
        document.querySelectorAll(".profile-menu[open]").forEach((menu) => menu.removeAttribute("open"));
        if (notificationPanelOpen()) {
            closeNotificationPanel();
            document.querySelector('[data-action="toggle-notifications"]')?.focus();
        }
    });
    // In-page tabs that have a sidebar entry (Catalog Management, AI Chatbot): follow the open tab.
    // Both scripts update the URL hash before dispatching the event.
    document.addEventListener("catalog-tab-change", () => {
        if (activeView === "services") markActiveNavigation();
    });
    document.addEventListener("chatbot-tab-change", () => {
        if (activeView === "chatbot") markActiveNavigation();
    });
    dialog.addEventListener("close", () => {
        // The close event is dispatched asynchronously: if another editor was opened in the meantime, keep it.
        if (dialog.open) return;
        orderWatch?.();
        orderWatch = null;
        // Closing an order returns the address bar to the ordinary Order Management route.
        if (adminOrderRoute()) window.history.pushState({ view: "orders" }, "", "/admin/orders");
        dialog.classList.remove("admin-dialog-wide", "admin-dialog-xl");
        document.querySelector("#dialog-content").replaceChildren();
    });
    // Clicking the dimmed backdrop closes the details modal.
    dialog.addEventListener("click", (event) => {
        if (event.target === dialog) UI.closeModal();
    });
    window.addEventListener("popstate", () => {
        const route = adminOrderRoute();
        if (route) {
            // Back/Forward into an order link rebuilds the modal from the database, not from a cached view.
            void navigate("orders", false).then(() => openOrder(route.reference, { focus: route.focus }));
            return;
        }
        const view = viewFromLocation();
        void navigate(view, false, { tab: sectionFromLocation(view) });
        if (dialog.open) UI.closeModal();
    });

    document.body.classList.toggle("compact-tables", localStorage.getItem(COMPACT_KEY) === "true");
    activeView = document.body.dataset.adminView || viewFromLocation();
    renderView();
    // Read state used to live in this browser's storage; move it to the server once.
    const legacySeen = localStorage.getItem(LEGACY_NOTIFICATION_SEEN_KEY);
    if (legacySeen) {
        api("/api/admin/notifications/read", { method: "POST", body: { before: legacySeen } })
            .then(() => localStorage.removeItem(LEGACY_NOTIFICATION_SEEN_KEY))
            .catch(() => {})
            .finally(refreshBadges);
    } else {
        refreshBadges();
    }
    // Badge/view freshness is handled by refreshVisibleTable below (30s + visibility/focus/online).

    // Keep the order, payments, and reports views current without a manual refresh. A dialog is left alone so an
    // administrator never loses a half-finished verification form.
    const refreshVisibleTable = () => {
        if (document.visibilityState !== "visible") return;
        refreshBadges();
        if (dialog.open) return;
        if (activeView === "orders" || activeView === "payments" || activeView === "production" || activeView === "fulfillment" || activeView === "refunds") {
            refreshActiveView();
            return;
        }
        if (activeView === "reports") {
            const tab = filters.reports.tab;
            if ((tab === "summary" || tab === "payments") && reportSummaryState && !reportSummaryState.loading) {
                reportSummaryState.load();
            } else if (tab === "completed" || tab === "services") {
                refreshActiveView();
            }
        }
    };
    window.setInterval(refreshVisibleTable, 30000);
    document.addEventListener("visibilitychange", refreshVisibleTable);
    window.addEventListener("focus", refreshVisibleTable);
    window.addEventListener("online", refreshVisibleTable);

    // Deep links: /admin/orders/<ref>[/payments], the older ?order=<ref>, or /admin/customers?customer=<id>.
    const params = new URLSearchParams(window.location.search);
    const orderPath = /^\/admin\/orders\/([^/]+)(?:\/(payments))?\/?$/.exec(window.location.pathname);
    let deepReference = params.get("order");
    let deepFocus = "";
    if (orderPath) {
        try { deepReference = decodeURIComponent(orderPath[1]); } catch { /* malformed links are ignored */ }
        deepFocus = orderPath[2] ? "payments" : "";
    }
    if (deepReference) openOrder(deepReference, { focus: deepFocus });
    else if (params.get("customer")) openCustomer(params.get("customer"));

    window.PrintShopAdmin = { openOrder, openCustomer, navigate };
})();
