"use strict";

(() => {
    fetch("/api/admin/session")
        .then((response) => {
            if (!response.ok) window.location.replace("/admin/login");
        })
        .catch(() => window.location.replace("/admin/login"));

    const serviceCatalog = {
        "Invitation": [
            "Birthday Invitation", "Wedding Invitation", "Christening Invitation"
        ],
        "Souvenir": [
            "Ref Magnet", "Acrylic Keychain", "Personalized Nail Cutter", "White Mug",
            "Glass Mug", "Nordic Mug", "Personalized Tote Bag", "Coloring Book"
        ],
        "Sticker": [
            "Business Logo Stickers", "Product Label Stickers", "Name Stickers", "Wall Stickers/Decals"
        ],
        "Sintra Board": [
            "Birthday/Welcome Signs", "Wedding Signs", "Menu Board", "Photo Mounting Boards",
            "Customized Photo Displays", "Photo Collages"
        ],
        "Party Needs": [
            "Party Hats", "Gable Boxes", "Gift/Thank You Tags", "Personalized Candle"
        ],
        "Tarpaulins & Large Format Printing": [
            "Birthdays", "Weddings", "Graduations", "Announcements", "Events", "Store Banners",
            "Promos", "Product Advertisements", "Stage Backdrops", "Welcome Banners",
            "Program Banners", "School Announcements", "Schedules", "Public Notices",
            "Entrance Signs", "Directional Banners", "Temporary Signs"
        ]
    };

    const categoryImages = {
        "Invitation": "/frontend/image/invitation/weddinginvitationj.png",
        "Souvenir": "/frontend/image/souvenir/keychain.jpg",
        "Sticker": "/frontend/image/Sticker/sticker.png",
        "Sintra Board": "/frontend/image/SintraBoard/welcomeBoard.jpg",
        "Party Needs": "/frontend/image/partyNeeds/partyNeeds.jpg",
        "Tarpaulins & Large Format Printing": "/frontend/image/tarpaulin/event.png"
    };
    const services = Object.entries(serviceCatalog).flatMap(([category, products]) =>
        products.map((name, index) => ({
            id: `${category.toLowerCase().replace(/[^a-z0-9]+/g, "-")}-${index + 1}`,
            category,
            name,
            description: `${name} printing and customization.`,
            image: categoryImages[category],
            pricingMode: "quote",
            price: null,
            available: true
        }))
    );

    const customers = [
        { id: "C-1001", name: "Mia Santos", email: "mia.santos@example.com", phone: "+63 917 555 0142", joined: "2026-08-12" },
        { id: "C-1002", name: "Joshua Reyes", email: "joshua.reyes@example.com", phone: "+63 918 555 0168", joined: "2026-09-04" },
        { id: "C-1003", name: "Angela Cruz", email: "angela.cruz@example.com", phone: "+63 905 555 0135", joined: "2026-09-18" },
        { id: "C-1004", name: "Marco Lim", email: "marco.lim@example.com", phone: "+63 917 555 0191", joined: "2026-10-02" },
        { id: "C-1005", name: "Bea Garcia", email: "bea.garcia@example.com", phone: "+63 916 555 0119", joined: "2026-10-20" }
    ];

    const orders = [
        { id: "PS-261102-031", customerId: "C-1001", product: "Wedding Invitation", category: "Invitation", date: "2026-11-02", status: "Pending Confirmation", agreedAmount: null, quantity: 100, specs: "Cream stock, sage-green floral details, folded card.", remarks: "", referenceImage: "/frontend/image/invitation/weddinginvitationj.png" },
        { id: "PS-261101-027", customerId: "C-1002", product: "Birthday / Welcome Signs", category: "Sintra Board", date: "2026-11-01", status: "Under Review", agreedAmount: 4200, quantity: 2, specs: "Welcome signage, 2 x 3 ft, photo and name personalization.", remarks: "Confirm final dimensions before production.", referenceImage: "/frontend/image/SintraBoard/welcomeBoard.jpg" },
        { id: "PS-261031-024", customerId: "C-1003", product: "Acrylic Keychain", category: "Souvenir", date: "2026-10-31", status: "Awaiting Down Payment", agreedAmount: 3600, quantity: 60, specs: "Round acrylic, printed name and event date.", remarks: "", referenceImage: "" },
        { id: "PS-261030-021", customerId: "C-1004", product: "Event Banners", category: "Tarpaulins & Large Format Printing", date: "2026-10-30", status: "In Production", agreedAmount: 5400, quantity: 3, specs: "Three event banners; final layout approved.", remarks: "", referenceImage: "/frontend/image/tarpaulin/event.png" },
        { id: "PS-261029-019", customerId: "C-1005", product: "Business Logo Stickers", category: "Sticker", date: "2026-10-29", status: "Ready for Pickup", agreedAmount: 1800, quantity: 200, specs: "Matte finish, 2-inch round labels.", remarks: "Customer prefers afternoon pickup.", referenceImage: "" },
        { id: "PS-261027-015", customerId: "C-1001", product: "Personalized Tote Bag", category: "Souvenir", date: "2026-10-27", status: "Completed", agreedAmount: 2500, quantity: 10, specs: "Natural canvas with one-color print.", remarks: "", referenceImage: "" },
        { id: "PS-261025-012", customerId: "C-1002", product: "Party Hats", category: "Party Needs", date: "2026-10-25", status: "Rejected", agreedAmount: null, quantity: 30, specs: "Custom character artwork requested.", remarks: "Artwork could not be reproduced as submitted.", referenceImage: "" },
        { id: "PS-261024-010", customerId: "C-1003", product: "Menu Board", category: "Sintra Board", date: "2026-10-24", status: "Cancelled", agreedAmount: 2100, quantity: 1, specs: "A3 menu board, matte surface.", remarks: "Customer cancelled before production.", referenceImage: "" }
    ];
    let liveOrders = [];
    let liveOrdersError = "";
    let liveOrdersLoaded = false;

    const payments = [
        { id: "TX-9021", orderId: "PS-261101-027", date: "2026-11-01", amount: 2100, method: "GCash", reference: "DEMO-4821", remarks: "Down payment recorded by admin." },
        { id: "TX-9018", orderId: "PS-261030-021", date: "2026-10-31", amount: 5400, method: "Bank transfer", reference: "DEMO-7194", remarks: "Full amount recorded by admin." },
        { id: "TX-9012", orderId: "PS-261029-019", date: "2026-10-30", amount: 900, method: "Cash", reference: "DEMO-3008", remarks: "Down payment recorded by admin." },
        { id: "TX-9006", orderId: "PS-261027-015", date: "2026-10-28", amount: 2500, method: "Maya", reference: "DEMO-1680", remarks: "Full amount recorded by admin." }
    ];

    const notifications = [
        { id: "N-1", title: "New printing request", body: "Mia Santos submitted a wedding invitation request.", time: "Today · 9:14 AM", read: false },
        { id: "N-2", title: "Payment recorded", body: "A down payment was recorded for order PS-261101-027.", time: "Yesterday · 4:32 PM", read: false },
        { id: "N-3", title: "Order ready for pickup", body: "Order PS-261029-019 is marked ready for pickup.", time: "Oct 30 · 2:10 PM", read: false },
        { id: "N-4", title: "New customer registration", body: "Bea Garcia created a customer profile.", time: "Oct 20 · 10:21 AM", read: true }
    ];

    const activity = [
        { text: "Demo order PS-261029-019 marked ready for pickup.", time: "Today · 8:45 AM" },
        { text: "Demo down payment recorded for PS-261101-027.", time: "Yesterday · 4:32 PM" },
        { text: "Service catalog reviewed by Shop Admin.", time: "Oct 30 · 11:10 AM" }
    ];

    const labels = {
        dashboard: "Dashboard",
        orders: "Order Management",
        customers: "Customer Management",
        services: "Services & Products",
        pricing: "Pricing Management",
        payments: "Payments & Transactions",
        reports: "Sales & Reports",
        notifications: "Notifications",
        settings: "Settings"
    };
    const statusOptions = [
        "Pending Confirmation", "Under Review", "Awaiting Quotation", "Awaiting Down Payment",
        "Draft Preparation", "Awaiting Customer Approval", "Approved for Printing",
        "In Production", "Ready for Pickup", "Completed", "Rejected", "Cancelled"
    ];
    const viewRoot = document.querySelector("#view-root");
    const dialog = document.querySelector("#admin-dialog");
    const dialogContent = document.querySelector("#dialog-content");
    const toastRegion = document.querySelector("#toast-region");
    const filters = {
        orders: { search: "", category: "", status: "", from: "", to: "" },
        customers: { search: "" },
        services: { search: "", category: "" },
        payments: { search: "" },
        reports: { from: "", to: "", category: "", period: "" }
    };
    let activeView = "dashboard";
    let chartPeriod = "week";
    let filterTimer;
    let serviceImageDraftUrl = "";
    let bannerDismissed = false;
    let dialogTemplatesPromise;
    const dialogTemplates = new Map();

    const money = (value) => new Intl.NumberFormat("en-PH", {
        style: "currency", currency: "PHP", maximumFractionDigits: 0
    }).format(Number(value || 0));
    const dateLabel = (value) => value
        ? new Date(`${value}T00:00:00`).toLocaleDateString("en-PH", { month: "short", day: "numeric", year: "numeric" })
        : "—";
    const localDate = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
    const slug = (value) => String(value).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    const customerFor = (order) => customers.find((customer) => customer.id === order.customerId);
    const orderFor = (id) => orders.find((order) => order.id === id);
    const paidFor = (orderId) => payments
        .filter((payment) => payment.orderId === orderId)
        .reduce((total, payment) => total + payment.amount, 0);
    const balanceFor = (order) => Math.max(0, Number(order.agreedAmount || 0) - paidFor(order.id));
    const paymentStatus = (order) => {
        const total = Number(order.agreedAmount || 0);
        const paid = paidFor(order.id);
        if (!total) return "Unpaid";
        if (paid >= total) return "Fully Paid";
        if (paid > 0) return "Partially Paid";
        return "Awaiting Down Payment";
    };
    const setStatusBadge = (element, status) => {
        element.className = `status-badge ${slug(status)}`;
        element.textContent = status;
    };

    function showTableEmpty(tableBody, columnCount, message) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");
        cell.className = "empty-state";
        cell.colSpan = columnCount;
        cell.textContent = message;
        row.append(cell);
        tableBody.replaceChildren(row);
    }

    function appendTextPair(container, primary, secondary) {
        const main = document.createElement("span");
        main.className = "table-primary";
        main.textContent = primary;
        const sub = document.createElement("span");
        sub.className = "table-secondary";
        sub.textContent = secondary;
        container.replaceChildren(main, sub);
    }

    function setTextPair(primaryElement, secondaryElement, primary, secondary) {
        primaryElement.textContent = primary;
        secondaryElement.textContent = secondary;
    }

    function toast(message) {
        const element = document.createElement("div");
        element.className = "toast";
        element.textContent = message;
        toastRegion.append(element);
        window.setTimeout(() => element.remove(), 3200);
    }

    function logActivity(message) {
        activity.unshift({ text: message, time: "Just now" });
        activity.splice(8);
    }

    function revokeUnusedImage(url) {
        if (url?.startsWith("blob:") && !services.some((service) => service.image === url)) {
            URL.revokeObjectURL(url);
        }
    }

    function viewFromLocation() {
        const route = window.location.pathname.split("/").filter(Boolean).pop();
        const queryView = new URLSearchParams(window.location.search).get("view");
        const requested = queryView || route;
        return labels[requested] ? requested : "dashboard";
    }

    async function navigate(view, addHistory = true) {
        const nextView = labels[view] ? view : "dashboard";
        const path = nextView === "dashboard" ? "/admin/dashboard" : `/admin/${nextView}`;
        if (nextView === activeView && window.location.pathname === path) return;

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
            if (!bannerDismissed) {
                const currentBanner = document.querySelector(".prototype-banner");
                const incomingBanner = pageDocument.querySelector(".prototype-banner");
                if (currentBanner && incomingBanner) {
                    currentBanner.replaceWith(document.importNode(incomingBanner, true));
                }
            }
            activeView = nextView;
            if (addHistory && window.location.pathname !== path) window.history.pushState({ view: nextView }, "", path);
            updateViewStylesheet();
            renderView();
            viewRoot.focus({ preventScroll: true });
        } catch (error) {
            console.error(`Could not navigate to admin ${nextView}.`, error);
            toast(error instanceof Error ? error.message : "The admin page could not be loaded.");
        }
    }

    function updateViewStylesheet() {
        const files = { reports: "reports.css", notifications: "notifications.css" };
        let stylesheet = document.querySelector("#admin-view-stylesheet");
        const fileName = files[activeView];
        if (!fileName) {
            stylesheet?.remove();
            return;
        }
        if (!stylesheet) {
            stylesheet = document.createElement("link");
            stylesheet.id = "admin-view-stylesheet";
            stylesheet.rel = "stylesheet";
            document.head.append(stylesheet);
        }
        stylesheet.href = `/assets/admin/${fileName}`;
    }

    async function loadDialogTemplates() {
        const response = await fetch("/assets/admin/dialogs.html");
        if (!response.ok) {
            throw new Error(`Admin dialog templates could not be loaded (HTTP ${response.status}).`);
        }
        const documentText = await response.text();
        const templateDocument = new DOMParser().parseFromString(documentText, "text/html");
        const templates = templateDocument.querySelectorAll("template[id^='dialog-']");
        if (!templates.length) {
            throw new Error("The admin dialog templates file does not contain any templates.");
        }
        templates.forEach((template) => dialogTemplates.set(template.id, template.content));
    }

    async function showDialog(templateId) {
        if (!dialogTemplatesPromise) dialogTemplatesPromise = loadDialogTemplates();
        try {
            await dialogTemplatesPromise;
        } catch (error) {
            console.error("Could not load admin dialog templates.", error);
            toast("Admin dialog forms could not be loaded. Please reload this page.");
            return null;
        }
        const template = dialogTemplates.get(templateId);
        if (!template) throw new Error(`The admin dialog template "${templateId}" is missing.`);
        dialogContent.replaceChildren(template.cloneNode(true));
        if (!dialog.open) dialog.showModal();
        return dialogContent.firstElementChild;
    }

    function appendDetailItem(container, label, value) {
        const item = dialogTemplates.get("dialog-detail-item");
        if (!item) throw new Error("The shared admin dialog detail template is missing.");
        const element = item.firstElementChild.cloneNode(true);
        element.querySelector("[data-detail-label]").textContent = label;
        element.querySelector("[data-detail-value]").textContent = String(value || "—");
        container.append(element);
    }

    function renderDashboard() {
        const pending = orders.filter((order) => order.status === "Pending Confirmation").length;
        const underReview = orders.filter((order) => order.status === "Under Review").length;
        const awaitingPayment = orders.filter((order) => order.status === "Awaiting Down Payment").length;
        const production = orders.filter((order) => order.status === "In Production").length;
        const ready = orders.filter((order) => order.status === "Ready for Pickup").length;
        const completed = orders.filter((order) => order.status === "Completed").length;
        const sales = payments.reduce((sum, payment) => sum + payment.amount, 0);
        const stats = {
            "total-orders": orders.length,
            "pending-orders": pending,
            "under-review-note": underReview,
            "awaiting-payment": awaitingPayment,
            "ready-orders": ready,
            "completed-note": completed,
            "review-orders": underReview,
            "production-orders": production,
            "completed-orders": completed,
            "recorded-payments": money(sales)
        };
        Object.entries(stats).forEach(([name, value]) => {
            document.querySelector(`[data-stat="${name}"]`).textContent = String(value);
        });

        const tableBody = document.querySelector("[data-dashboard-order-rows]");
        tableBody.replaceChildren();
        [...orders].slice(0, 5).forEach((order) => {
            const customer = customerFor(order);
            const row = document.querySelector("#dashboard-order-row-template").content.firstElementChild.cloneNode(true);
            row.querySelector('[data-field="id"]').textContent = order.id;
            setTextPair(
                row.querySelector('[data-field="customer"]'),
                row.querySelector('[data-field="email"]'),
                customer?.name || "Unknown",
                customer?.email || ""
            );
            row.querySelector('[data-field="product"]').textContent = order.product;
            row.querySelector('[data-field="date"]').textContent = dateLabel(order.date);
            row.querySelector('[data-field="amount"]').textContent = order.agreedAmount ? money(order.agreedAmount) : "Quote";
            setStatusBadge(row.querySelector('[data-field="status"]'), order.status);
            row.querySelector("[data-action='order-detail']").dataset.id = order.id;
            tableBody.append(row);
        });

        const statusRows = [
            ["Pending confirmation", pending, ""],
            ["Under review", underReview, "blue"],
            ["Awaiting down payment", awaitingPayment, "pink"],
            ["In production", production, "pink"],
            ["Ready for pickup", ready, "green"],
            ["Completed", completed, "gray"]
        ];
        const statusBreakdown = document.querySelector("[data-status-breakdown]");
        statusBreakdown.replaceChildren();
        statusRows.forEach(([label, count, color]) => {
            const row = document.querySelector("#dashboard-status-row-template").content.firstElementChild.cloneNode(true);
            row.querySelector('[data-field="label"]').textContent = label;
            row.querySelector('[data-field="count"]').textContent = String(count);
            if (color) row.querySelector('[data-field="dot"]').classList.add(color);
            statusBreakdown.append(row);
        });
        document.querySelector("[data-status-order-count]").textContent = String(orders.length);

        const configs = {
            day: { labels: ["8a", "10a", "12p", "2p", "4p", "6p"], values: [18, 30, 45, 28, 62, 37] },
            week: { labels: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], values: [30, 55, 42, 72, 54, 88, 63] },
            month: { labels: ["Wk 1", "Wk 2", "Wk 3", "Wk 4"], values: [54, 70, 46, 90] }
        };
        const chart = configs[chartPeriod];
        document.querySelector("#chart-period").value = chartPeriod;
        const chartRoot = document.querySelector("[data-sales-chart]");
        chartRoot.replaceChildren();
        chart.values.forEach((value, index) => {
            const column = document.querySelector("#dashboard-chart-column-template").content.firstElementChild.cloneNode(true);
            const bar = column.querySelector('[data-field="bar"]');
            bar.value = value;
            column.querySelector('[data-field="label"]').textContent = chart.labels[index];
            chartRoot.append(column);
        });

        const popular = Object.keys(serviceCatalog).map((category) => ({
            category,
            count: orders.filter((order) => order.category === category).length
        })).sort((a, b) => b.count - a.count).slice(0, 4);
        const max = Math.max(1, ...popular.map((item) => item.count));
        const serviceList = document.querySelector("[data-popular-services]");
        serviceList.replaceChildren();
        popular.forEach((item) => {
            const row = document.querySelector("#dashboard-service-row-template").content.firstElementChild.cloneNode(true);
            row.querySelector('[data-field="label"]').textContent = item.category;
            row.querySelector('[data-field="count"]').textContent = String(item.count);
            const bar = row.querySelector('[data-field="bar"]');
            bar.max = max;
            bar.value = item.count;
            serviceList.append(row);
        });

        const activityList = document.querySelector("[data-dashboard-activity]");
        activityList.replaceChildren();
        activity.slice(0, 4).forEach((item) => {
            const row = document.querySelector("#dashboard-activity-row-template").content.firstElementChild.cloneNode(true);
            row.querySelector('[data-field="text"]').textContent = item.text;
            row.querySelector('[data-field="time"]').textContent = item.time;
            activityList.append(row);
        });
        renderLiveOrdersTable();
    }

    function renderLiveOrdersTable() {
        const tableBody = document.querySelector("[data-live-order-rows]");
        if (!tableBody) return;
        tableBody.replaceChildren();
        liveOrders.forEach((order) => {
            const row = document.querySelector("#live-order-row-template").content.firstElementChild.cloneNode(true);
            row.querySelector('[data-field="reference"]').textContent = order.reference;
            setTextPair(row.querySelector('[data-field="customer"]'), row.querySelector('[data-field="email"]'), order.customer.name, order.customer.email);
            setTextPair(row.querySelector('[data-field="product"]'), row.querySelector('[data-field="category"]'), order.product.name, order.product.category);
            row.querySelector('[data-field="submitted"]').textContent = dateLabel(order.created_at.slice(0, 10));
            row.querySelector('[data-field="total"]').textContent =
                order.total_amount === null ? "Waiting for quotation" : money(order.total_amount);
            setStatusBadge(row.querySelector('[data-field="status"]'), order.status);
            row.querySelector("[data-action='live-order-detail']").dataset.id = order.reference;
            tableBody.append(row);
        });
        const errorNotice = document.querySelector("[data-live-order-error]");
        if (errorNotice) {
            errorNotice.hidden = !liveOrdersError;
            errorNotice.textContent = liveOrdersError ? `Customer requests could not be loaded. ${liveOrdersError}` : "";
        }
        document.querySelectorAll("[data-live-order-count]").forEach((count) => {
            count.textContent = `${liveOrders.length} saved requests`;
        });
        if (!tableBody.childElementCount) {
            const message = liveOrdersError
                ? "Customer requests could not be loaded."
                : liveOrdersLoaded
                    ? "No customer requests have been submitted yet."
                    : "Loading customer requests…";
            showTableEmpty(tableBody, 7, message);
        }
    }

    function liveOrderFor(reference) {
        return liveOrders.find((order) => order.reference === reference);
    }

    function livePaymentTotal(order) {
        return order.payments.reduce((total, payment) => total + payment.amount, 0);
    }

    function appendLiveStatusActions(container, order) {
        const options = {
            "Pending Confirmation": ["Under Review", "Rejected", "Cancelled"],
            "Under Review": ["Awaiting Quotation", "Rejected", "Cancelled"],
            "Awaiting Quotation": ["Rejected", "Cancelled"],
            "Awaiting Down Payment": ["Rejected", "Cancelled"],
            "Approved for Printing": ["In Production"],
            "In Production": ["Ready for Pickup"],
            "Ready for Pickup": ["Completed"],
        };
        container.replaceChildren();
        (options[order.status] || []).forEach((status) => {
            const button = document.createElement("button");
            button.className = `button ${status === "Rejected" || status === "Cancelled" ? "button-danger" : "button-primary"}`;
            button.type = "button";
            button.dataset.action = "live-status";
            button.dataset.id = order.reference;
            button.dataset.status = status;
            button.textContent = status;
            container.append(button);
        });
    }

    async function openLiveOrder(reference) {
        const order = liveOrderFor(reference);
        if (!order) return;
        const paid = livePaymentTotal(order);
        const balance = order.total_amount === null ? null : Math.max(0, order.total_amount - paid);
        const canSendDraft = order.total_amount !== null && paid >= order.total_amount * 0.5 &&
            ["Draft Preparation", "Awaiting Customer Approval"].includes(order.status);
        const content = await showDialog("dialog-live-order");
        if (!content) return;
        content.querySelector("[data-live-title]").textContent = `Customer request ${reference}`;
        const details = content.querySelector("[data-live-details]");
        [
            ["Customer", order.customer.name],
            ["Email", order.customer.email],
            ["Contact number", order.customer.phone],
            ["Messenger", order.customer.messenger],
            ["Product", order.product.name],
            ["Category", order.product.category],
            ["Size", order.customization.size],
            ["Quantity", String(order.customization.quantity)],
            ["Material / variant", order.customization.material],
            ["Confirmed total", order.total_amount === null ? "Waiting for Admin Quotation" : money(order.total_amount)],
            ["Payment received", money(paid)],
            ["Order status", order.status],
        ].forEach(([label, value]) => appendDetailItem(details, label, value));
        if (order.has_event) {
            [
                ["Celebrant", order.event_details.celebrant],
                ["Event type", order.event_details.event_type],
                ["Event date", order.event_details.event_date],
                ["Event time", order.event_details.event_time],
                ["Event location", order.event_details.event_location],
            ].forEach(([label, value]) => appendDetailItem(details, label, value));
        }
        content.querySelector("[data-live-customization]").textContent = order.customization.details || "Not specified";
        content.querySelector("[data-live-instructions]").textContent = order.customization.instructions || "None";
        const productImage = content.querySelector("[data-live-product-image]");
        if (order.product.image) {
            productImage.hidden = false;
            const image = content.querySelector("[data-live-product-image-source]");
            image.src = order.product.image;
            image.alt = `Selected product sample for ${order.product.name}`;
        } else {
            productImage.hidden = true;
        }

        const attachments = content.querySelector("[data-live-attachments]");
        const attachmentList = content.querySelector("[data-live-attachment-list]");
        attachmentList.replaceChildren();
        order.attachments.forEach((file) => {
            const item = document.createElement("li");
            const link = document.createElement("a");
            link.href = file.url;
            link.target = "_blank";
            link.rel = "noopener";
            link.textContent = `${file.name} · ${(file.size / 1024).toFixed(0)} KB`;
            item.append(link);
            attachmentList.append(item);
        });
        attachments.hidden = order.attachments.length === 0;
        content.querySelector("[data-live-no-attachments]").hidden = order.attachments.length > 0;

        const paymentInstructions = content.querySelector("[data-live-payment-instructions]");
        paymentInstructions.hidden = !order.payment_instructions;
        paymentInstructions.querySelector("span").textContent = order.payment_instructions || "";
        const paymentList = content.querySelector("[data-live-payment-list]");
        paymentList.replaceChildren();
        order.payments.forEach((payment) => {
            const item = document.createElement("li");
            item.textContent = `${money(payment.amount)} · ${payment.method} · ${new Date(payment.received_at).toLocaleString()}`;
            paymentList.append(item);
        });
        content.querySelector("[data-live-payments]").hidden = order.payments.length === 0;

        const draftNotice = content.querySelector("[data-live-draft-notice]");
        draftNotice.hidden = !order.draft_attachment;
        if (order.draft_attachment) {
            const strong = document.createElement("strong");
            strong.textContent = "Draft attached: ";
            const link = document.createElement("a");
            link.href = order.draft_attachment.url;
            link.target = "_blank";
            link.rel = "noopener";
            link.textContent = order.draft_attachment.name;
            draftNotice.replaceChildren(strong, link);
        }
        content.querySelector("[data-live-approval-notice]").hidden = order.status !== "Awaiting Customer Approval";
        appendLiveStatusActions(content.querySelector("[data-live-actions]"), order);

        const quoteForm = content.querySelector("[data-live-quote-form]");
        quoteForm.hidden = !["Under Review", "Awaiting Quotation"].includes(order.status);
        quoteForm.dataset.id = reference;
        quoteForm.elements.total_amount.value = order.total_amount ?? "";
        quoteForm.elements.payment_instructions.value = order.payment_instructions || "";

        const paymentForm = content.querySelector("[data-live-payment-form]");
        paymentForm.hidden = order.total_amount === null || balance <= 0 ||
            ["Rejected", "Cancelled", "Completed"].includes(order.status);
        paymentForm.dataset.id = reference;
        paymentForm.elements.amount.max = String(balance ?? "");
        content.querySelector("[data-live-payment-summary]").textContent =
            order.total_amount === null ? "" :
                `50% down payment required: ${money(order.total_amount * 0.5)} · Received: ${money(paid)} · Balance: ${money(balance)}`;

        const draftForm = content.querySelector("[data-live-draft-form]");
        draftForm.hidden = !canSendDraft;
        draftForm.dataset.id = reference;

        const history = content.querySelector("[data-live-history]");
        history.replaceChildren();
        order.history.forEach((event) => {
            const item = document.createElement("li");
            const description = document.createElement("span");
            description.textContent = event.text;
            const time = document.createElement("time");
            time.textContent = new Date(event.at).toLocaleString();
            item.append(description, time);
            history.append(item);
        });
    }

    async function refreshLiveOrders() {
        try {
            const response = await fetch("/api/admin/orders");
            const result = await response.json();
            if (!response.ok) throw new Error(result.detail || "Could not load customer orders.");
            liveOrders = result.orders;
            liveOrdersError = "";
            liveOrdersLoaded = true;
        } catch (error) {
            liveOrdersError = error instanceof Error ? error.message : "Could not load customer orders.";
            liveOrdersLoaded = true;
        }
        if (!dialog.open) renderView();
    }

    function renderOrders() {
        const filter = filters.orders;
        const needle = filter.search.toLowerCase();
        const filtered = orders.filter((order) => {
            const customer = customerFor(order);
            const matchesSearch = !needle || [order.id, customer?.name, customer?.email, order.product].some((value) => String(value || "").toLowerCase().includes(needle));
            const matchesCategory = !filter.category || order.category === filter.category;
            const matchesStatus = !filter.status || order.status === filter.status;
            const matchesFrom = !filter.from || order.date >= filter.from;
            const matchesTo = !filter.to || order.date <= filter.to;
            return matchesSearch && matchesCategory && matchesStatus && matchesFrom && matchesTo;
        });
        const categoryFilter = document.querySelector("#order-category-filter");
        categoryFilter.replaceChildren(new Option("All service categories", ""));
        Object.keys(serviceCatalog).forEach((category) => categoryFilter.add(new Option(category, category)));
        categoryFilter.value = filter.category;
        const statusFilter = document.querySelector("#order-status-filter");
        statusFilter.replaceChildren(new Option("All statuses", ""));
        statusOptions.forEach((status) => statusFilter.add(new Option(status, status)));
        statusFilter.value = filter.status;
        document.querySelector("#order-search").value = filter.search;
        document.querySelector("#order-date-from").value = filter.from;
        document.querySelector("#order-date-to").value = filter.to;

        const liveRows = document.querySelector("[data-live-order-rows]");
        liveRows.replaceChildren();
        liveOrders.forEach((order) => {
            const row = document.querySelector("#live-order-row-template").content.firstElementChild.cloneNode(true);
            row.querySelector('[data-field="reference"]').textContent = order.reference;
            setTextPair(row.querySelector('[data-field="customer"]'), row.querySelector('[data-field="email"]'), order.customer.name, order.customer.email);
            setTextPair(row.querySelector('[data-field="product"]'), row.querySelector('[data-field="category"]'), order.product.name, order.product.category);
            row.querySelector('[data-field="submitted"]').textContent = dateLabel(order.created_at.slice(0, 10));
            row.querySelector('[data-field="total"]').textContent =
                order.total_amount === null ? "Waiting for quotation" : money(order.total_amount);
            setStatusBadge(row.querySelector('[data-field="status"]'), order.status);
            row.querySelector("[data-action='live-order-detail']").dataset.id = order.reference;
            liveRows.append(row);
        });
        const liveOrderError = document.querySelector("[data-live-order-error]");
        liveOrderError.hidden = !liveOrdersError;
        liveOrderError.textContent = liveOrdersError ? `Customer requests could not be loaded. ${liveOrdersError}` : "";
        document.querySelector("[data-live-order-count]").textContent = `${liveOrders.length} saved requests`;
        if (!liveRows.childElementCount) {
            const message = liveOrdersError
                ? "Customer requests could not be loaded."
                : liveOrdersLoaded
                    ? "No customer requests have been submitted yet."
                    : "Loading customer requests…";
            showTableEmpty(liveRows, 7, message);
        }

        const sampleRows = document.querySelector("[data-sample-order-rows]");
        sampleRows.replaceChildren();
        filtered.forEach((order) => {
            const customer = customerFor(order);
            const row = document.querySelector("#sample-order-row-template").content.firstElementChild.cloneNode(true);
            row.querySelector('[data-field="id"]').textContent = order.id;
            setTextPair(row.querySelector('[data-field="customer"]'), row.querySelector('[data-field="email"]'), customer?.name || "Unknown", customer?.email || "");
            setTextPair(row.querySelector('[data-field="product"]'), row.querySelector('[data-field="category"]'), order.product, order.category);
            row.querySelector('[data-field="date"]').textContent = dateLabel(order.date);
            row.querySelector('[data-field="amount"]').textContent =
                order.agreedAmount ? money(order.agreedAmount) : "Quote required";
            setStatusBadge(row.querySelector('[data-field="status"]'), order.status);
            row.querySelector("[data-action='order-detail']").dataset.id = order.id;
            sampleRows.append(row);
        });
        document.querySelector("[data-sample-order-count]").textContent = `${filtered.length} illustrative records`;
        if (!sampleRows.childElementCount) showTableEmpty(sampleRows, 7, "No orders match these filters.");
    }

    function renderCustomers() {
        const needle = filters.customers.search.toLowerCase();
        const filtered = customers.filter((customer) =>
            !needle || `${customer.name} ${customer.email} ${customer.id}`.toLowerCase().includes(needle)
        );
        const tableBody = document.querySelector("[data-customer-rows]");
        tableBody.replaceChildren();
        filtered.forEach((customer) => {
            const customerOrders = orders.filter((order) => order.customerId === customer.id);
            const completed = customerOrders.filter((order) => order.status === "Completed").length;
            const row = document.querySelector("#customer-row-template").content.firstElementChild.cloneNode(true);
            setTextPair(row.querySelector('[data-field="name"]'), row.querySelector('[data-field="id"]'), customer.name, customer.id);
            row.querySelector('[data-field="email"]').textContent = customer.email;
            row.querySelector('[data-field="phone"]').textContent = customer.phone;
            row.querySelector('[data-field="joined"]').textContent = dateLabel(customer.joined);
            row.querySelector('[data-field="orders"]').textContent = String(customerOrders.length);
            row.querySelector('[data-field="completed"]').textContent = String(completed);
            row.querySelector("[data-action='customer-detail']").dataset.id = customer.id;
            tableBody.append(row);
        });
        document.querySelector("[data-customer-count]").textContent = `${filtered.length} profiles · demo data`;
        if (!filtered.length) showTableEmpty(tableBody, 7, "No customers match this search.");
    }

    function renderServices() {
        const filter = filters.services;
        const needle = filter.search.toLowerCase();
        const filtered = services.filter((service) =>
            (!needle || `${service.name} ${service.category}`.toLowerCase().includes(needle)) &&
            (!filter.category || service.category === filter.category)
        );
        const categoryFilter = document.querySelector("#service-category-filter");
        categoryFilter.replaceChildren(new Option("All categories", ""));
        Object.keys(serviceCatalog).forEach((category) => categoryFilter.add(new Option(category, category)));
        categoryFilter.value = filter.category;
        const tableBody = document.querySelector("[data-service-rows]");
        tableBody.replaceChildren();
        filtered.forEach((service) => {
            const row = document.querySelector("#service-row-template").content.firstElementChild.cloneNode(true);
            const image = row.querySelector('[data-field="image"]');
            image.src = service.image;
            image.alt = "";
            appendTextPair(row.querySelector(".product-cell > span"), service.name, service.description);
            row.querySelector('[data-field="category"]').textContent = service.category;
            row.querySelector('[data-field="pricing"]').textContent =
                service.pricingMode === "fixed" ? money(service.price) : "Request a quote";
            setStatusBadge(row.querySelector('[data-field="availability"]'), service.available ? "Available" : "Unavailable");
            row.querySelector("[data-action='edit-service']").dataset.id = service.id;
            tableBody.append(row);
        });
        document.querySelector("[data-service-count]").textContent = `${filtered.length} items`;
        if (!filtered.length) showTableEmpty(tableBody, 5, "No products match these filters.");
    }

    function renderPricing() {
        const tableBody = document.querySelector("[data-pricing-rows]");
        tableBody.replaceChildren();
        services.forEach((service) => {
            const row = document.querySelector("#pricing-row-template").content.firstElementChild.cloneNode(true);
            row.querySelector('[data-field="name"]').textContent = service.name;
            row.querySelector('[data-field="category"]').textContent = service.category;
            row.querySelector('[data-field="price"]').textContent =
                service.pricingMode === "fixed" ? money(service.price) : "Request a quote";
            setStatusBadge(row.querySelector('[data-field="mode"]'), service.pricingMode === "fixed" ? "Fixed price" : "Quote");
            row.querySelector("[data-action='edit-price']").dataset.id = service.id;
            tableBody.append(row);
        });
        document.querySelector("[data-pricing-count]").textContent = `${services.length} catalog items`;
    }

    function renderPayments() {
        const needle = filters.payments.search.toLowerCase();
        const filtered = [...payments].filter((payment) => {
            const order = orderFor(payment.orderId);
            const customer = order ? customerFor(order) : null;
            return !needle || `${payment.id} ${payment.orderId} ${customer?.name || ""} ${payment.reference}`.toLowerCase().includes(needle);
        }).sort((a, b) => b.date.localeCompare(a.date));
        document.querySelector("#payment-search").value = filters.payments.search;
        const received = payments.reduce((sum, payment) => sum + payment.amount, 0);
        const outstanding = orders.filter((order) => order.agreedAmount && !["Rejected", "Cancelled"].includes(order.status))
            .reduce((sum, order) => sum + balanceFor(order), 0);
        document.querySelector("[data-payment-received]").textContent = money(received);
        document.querySelector("[data-payment-outstanding]").textContent = money(outstanding);
        document.querySelector("[data-payment-count]").textContent = String(payments.length);
        document.querySelector("[data-payment-unpriced]").textContent =
            String(orders.filter((order) => !order.agreedAmount).length);

        const customerPaymentRows = document.querySelector("[data-customer-payment-rows]");
        customerPaymentRows.replaceChildren();
        liveOrders.forEach((order) => {
            order.payments.forEach((payment) => {
                const row = document.querySelector("#customer-payment-row-template").content.firstElementChild.cloneNode(true);
                setTextPair(row.querySelector('[data-field="reference"]'), row.querySelector('[data-field="receipt"]'), order.reference, payment.reference || "No payment reference");
                setTextPair(row.querySelector('[data-field="customer"]'), row.querySelector('[data-field="product"]'), order.customer.name, order.product.name);
                row.querySelector('[data-field="date"]').textContent = dateLabel(payment.received_at.slice(0, 10));
                row.querySelector('[data-field="method"]').textContent = payment.method;
                row.querySelector('[data-field="amount"]').textContent = money(payment.amount);
                const paymentState = order.total_amount === null
                    ? "Awaiting quotation"
                    : livePaymentTotal(order) >= order.total_amount ? "Paid in full" : "Partial payment";
                setStatusBadge(row.querySelector('[data-field="status"]'), paymentState);
                customerPaymentRows.append(row);
            });
        });
        const customerPaymentTotal = liveOrders.reduce((sum, order) => sum + livePaymentTotal(order), 0);
        document.querySelector("[data-customer-payment-total]").textContent = `${money(customerPaymentTotal)} recorded`;
        if (!customerPaymentRows.childElementCount) {
            showTableEmpty(customerPaymentRows, 6, "No payments have been recorded for customer requests.");
        }

        const historyRows = document.querySelector("[data-payment-history-rows]");
        historyRows.replaceChildren();
        filtered.forEach((payment) => {
            const order = orderFor(payment.orderId);
            const customer = order ? customerFor(order) : null;
            const row = document.querySelector("#payment-history-row-template").content.firstElementChild.cloneNode(true);
            setTextPair(row.querySelector('[data-field="transaction"]'), row.querySelector('[data-field="reference"]'), payment.id, payment.reference);
            setTextPair(row.querySelector('[data-field="order"]'), row.querySelector('[data-field="customer"]'), payment.orderId, customer?.name || "Unknown customer");
            row.querySelector('[data-field="date"]').textContent = dateLabel(payment.date);
            row.querySelector('[data-field="method"]').textContent = payment.method;
            row.querySelector('[data-field="amount"]').textContent = money(payment.amount);
            setStatusBadge(row.querySelector('[data-field="status"]'), order ? paymentStatus(order) : "Recorded");
            row.querySelector('[data-field="remarks"]').textContent = payment.remarks;
            historyRows.append(row);
        });
        document.querySelector("[data-payment-history-count]").textContent = `${filtered.length} records · demo data`;
        if (!historyRows.childElementCount) showTableEmpty(historyRows, 7, "No payments match this search.");
    }

    function renderReports() {
        const filter = filters.reports;
        const records = payments.filter((payment) => {
            const order = orderFor(payment.orderId);
            return (!filter.from || payment.date >= filter.from) &&
                (!filter.to || payment.date <= filter.to) &&
                (!filter.category || order?.category === filter.category);
        });
        const total = records.reduce((sum, payment) => sum + payment.amount, 0);
        const categoryTotals = Object.keys(serviceCatalog).map((category) => ({
            category,
            amount: records.filter((payment) => orderFor(payment.orderId)?.category === category)
                .reduce((sum, payment) => sum + payment.amount, 0)
        })).sort((a, b) => b.amount - a.amount);
        const max = Math.max(1, ...categoryTotals.map((item) => item.amount));
        const categorySelect = document.querySelector("#report-category");
        categorySelect.replaceChildren(new Option("All categories", ""));
        Object.keys(serviceCatalog).forEach((category) => {
            categorySelect.add(new Option(category, category));
        });
        categorySelect.value = filter.category;
        document.querySelector("#report-period").value = filter.period;
        document.querySelector("#report-from").value = filter.from;
        document.querySelector("#report-to").value = filter.to;

        document.querySelector("[data-report-total]").textContent = money(total);
        document.querySelector("[data-report-transactions]").textContent = String(records.length);
        document.querySelector("[data-report-orders]").textContent = String(new Set(records.map((payment) => payment.orderId)).size);
        document.querySelector("[data-report-completed]").textContent = String(orders.filter((order) => order.status === "Completed").length);
        document.querySelector("[data-report-record-count]").textContent = `${records.length} transactions`;

        const categoryList = document.querySelector("[data-report-category-totals]");
        categoryList.replaceChildren();
        const categoryTemplate = document.querySelector("#report-category-template");
        categoryTotals.filter((item) => item.amount > 0).forEach((item) => {
            const fragment = categoryTemplate.content.cloneNode(true);
            fragment.querySelector('[data-field="category"]').textContent = item.category;
            const bar = fragment.querySelector('[data-field="bar"]');
            bar.max = max;
            bar.value = item.amount;
            bar.setAttribute("aria-label", `${item.category}: ${money(item.amount)}`);
            fragment.querySelector('[data-field="amount"]').textContent = money(item.amount);
            categoryList.append(fragment);
        });
        if (!categoryList.childElementCount) {
            const empty = document.createElement("p");
            empty.className = "empty-state";
            empty.textContent = "No payments match these filters.";
            categoryList.append(empty);
        }

        const tableBody = document.querySelector("[data-report-rows]");
        tableBody.replaceChildren();
        const rowTemplate = document.querySelector("#report-row-template");
        records.forEach((payment) => {
            const order = orderFor(payment.orderId);
            const row = rowTemplate.content.firstElementChild.cloneNode(true);
            row.querySelector('[data-field="date"]').textContent = dateLabel(payment.date);
            row.querySelector('[data-field="order"]').textContent = payment.orderId;
            row.querySelector('[data-field="category"]').textContent = order?.category || "—";
            row.querySelector('[data-field="product"]').textContent = order?.product || "—";
            row.querySelector('[data-field="amount"]').textContent = money(payment.amount);
            tableBody.append(row);
        });
        if (!tableBody.childElementCount) {
            const row = document.createElement("tr");
            const cell = document.createElement("td");
            cell.className = "empty-state";
            cell.colSpan = 5;
            cell.textContent = "No payments match these filters.";
            row.append(cell);
            tableBody.append(row);
        }
    }

    function renderNotifications() {
        const unread = notifications.filter((notification) => !notification.read).length;
        document.querySelector("[data-notifications-description]").textContent =
            `${unread} unread sample notifications. Prototype notices are not connected to email or a live order system.`;
        const list = document.querySelector("[data-notification-list]");
        list.replaceChildren();
        const itemTemplate = document.querySelector("#notification-item-template");
        notifications.forEach((notification) => {
            const item = itemTemplate.content.firstElementChild.cloneNode(true);
            item.classList.toggle("unread", !notification.read);
            item.querySelector("[data-notification-symbol]").textContent =
                notification.title.includes("Payment") ? "₱" :
                    notification.title.includes("registration") ? "♙" : "▤";
            item.querySelector("[data-notification-title]").textContent = notification.title;
            item.querySelector("[data-notification-body]").textContent = notification.body;
            item.querySelector("[data-notification-time]").textContent = notification.time;
            list.append(item);
        });
    }

    function renderSettings() {
        const activityList = document.querySelector("[data-settings-activity]");
        activityList.replaceChildren();
        const rowTemplate = document.querySelector("#activity-row-template");
        activity.forEach((item) => {
            const row = rowTemplate.content.firstElementChild.cloneNode(true);
            row.querySelector('[data-field="text"]').textContent = item.text;
            row.querySelector('[data-field="time"]').textContent = item.time;
            activityList.append(row);
        });
    }

    function renderView() {
        const renderers = {
            dashboard: renderDashboard, orders: renderOrders, customers: renderCustomers,
            services: renderServices, pricing: renderPricing, payments: renderPayments,
            reports: renderReports, notifications: renderNotifications, settings: renderSettings
        };
        document.querySelector("#current-section").textContent = labels[activeView];
        document.querySelectorAll("[data-view]").forEach((link) => {
            link.classList.toggle("active", link.dataset.view === activeView);
            if (link.matches(".sidebar-nav a, .sidebar-bottom > a")) {
                if (link.dataset.view === activeView) link.setAttribute("aria-current", "page");
                else link.removeAttribute("aria-current");
            }
        });
        document.querySelector("#pending-count").textContent = String(
            orders.filter((order) => order.status === "Pending Confirmation").length +
            liveOrders.filter((order) => order.status === "Pending Confirmation").length
        );
        document.querySelector("#notification-count").textContent = String(notifications.filter((notification) => !notification.read).length);
        renderers[activeView]();
        document.querySelector("#today-date").textContent = new Date().toLocaleDateString("en-PH", {
            weekday: "short", month: "short", day: "numeric"
        });
    }

    async function openOrder(orderId) {
        const order = orderFor(orderId);
        if (!order) return;
        const customer = customerFor(order);
        const content = await showDialog("dialog-sample-order");
        if (!content) return;
        content.querySelector("[data-sample-title]").textContent = `Order ${order.id}`;
        const details = content.querySelector("[data-sample-details]");
        [
            ["Customer", customer?.name],
            ["Email", customer?.email],
            ["Phone", customer?.phone],
            ["Requested date", dateLabel(order.date)],
            ["Service / product", order.product],
            ["Category", order.category],
            ["Quantity", String(order.quantity)],
            ["Agreed amount", order.agreedAmount ? money(order.agreedAmount) : "Quote not agreed"],
            ["Payment received", money(paidFor(order.id))],
            ["Balance", order.agreedAmount ? money(balanceFor(order)) : "Set agreed amount first"],
        ].forEach(([label, value]) => appendDetailItem(details, label, value));
        content.querySelector("[data-sample-specs]").textContent = order.specs;
        const imagePanel = content.querySelector("[data-sample-image]");
        imagePanel.hidden = !order.referenceImage;
        if (order.referenceImage) {
            const image = content.querySelector("[data-sample-image-source]");
            image.src = order.referenceImage;
            image.alt = `Sample artwork attached to ${order.id}`;
        }
        const form = content.querySelector('[data-form="order-update"]');
        form.dataset.id = order.id;
        const status = form.elements.status;
        statusOptions.filter((item) => item !== "Approved for Printing")
            .forEach((item) => status.add(new Option(item, item)));
        status.value = order.status;
        form.elements.amount.value = order.agreedAmount ?? "";
        form.elements.remarks.value = order.remarks;
        content.querySelectorAll("[data-sample-decision]").forEach((button) => {
            button.dataset.id = order.id;
            button.hidden = !["Pending Confirmation", "Under Review"].includes(order.status);
        });
    }

    async function openCustomer(customerId) {
        const customer = customers.find((item) => item.id === customerId);
        if (!customer) return;
        const content = await showDialog("dialog-customer");
        if (!content) return;
        content.querySelector("[data-customer-title]").textContent = customer.name;
        const details = content.querySelector("[data-customer-details]");
        [
            ["Customer ID", customer.id],
            ["Email address", customer.email],
            ["Contact number", customer.phone],
            ["Joined", dateLabel(customer.joined)],
        ].forEach(([label, value]) => appendDetailItem(details, label, value));
        const tableBody = content.querySelector("[data-customer-history-rows]");
        const rowTemplate = dialogTemplates.get("dialog-customer-order-row");
        orders.filter((order) => order.customerId === customer.id).forEach((order) => {
            const row = rowTemplate.firstElementChild.cloneNode(true);
            row.querySelector('[data-field="order"]').textContent = order.id;
            row.querySelector('[data-field="product"]').textContent = order.product;
            row.querySelector('[data-field="date"]').textContent = dateLabel(order.date);
            setStatusBadge(row.querySelector('[data-field="status"]'), order.status);
            tableBody.append(row);
        });
        if (!tableBody.childElementCount) showTableEmpty(tableBody, 4, "No orders found for this customer.");
    }

    async function openServiceForm(service) {
        const editing = Boolean(service);
        revokeUnusedImage(serviceImageDraftUrl);
        serviceImageDraftUrl = service?.image || "";
        const content = await showDialog("dialog-service-form");
        if (!content) return;
        content.querySelector("[data-service-form-title]").textContent = editing ? "Edit service" : "Add a service";
        const form = content.querySelector('[data-form="service-save"]');
        form.dataset.id = service?.id || "";
        form.elements.name.value = service?.name || "";
        form.elements.description.value = service?.description || "";
        form.elements.pricingMode.value = service?.pricingMode || "quote";
        form.elements.price.value = service?.price ?? "";
        form.elements.available.value = service?.available === false ? "false" : "true";
        const category = form.elements.category;
        Object.keys(serviceCatalog).forEach((item) => category.add(new Option(item, item)));
        category.value = service?.category || Object.keys(serviceCatalog)[0];
        const preview = content.querySelector("#service-image-preview");
        preview.hidden = !serviceImageDraftUrl;
        if (serviceImageDraftUrl) preview.src = serviceImageDraftUrl;
    }

    async function openPriceForm(service) {
        if (!service) return;
        const content = await showDialog("dialog-price-form");
        if (!content) return;
        content.querySelector("[data-price-description]").textContent =
            `${service.name} · ${service.category}. Enter only a price approved by the shop administrator.`;
        const form = content.querySelector('[data-form="price-save"]');
        form.dataset.id = service.id;
        form.elements.pricingMode.value = service.pricingMode;
        form.elements.price.value = service.price ?? "";
    }

    async function openCalculator() {
        const content = await showDialog("dialog-calculator");
        if (!content) return;
        const select = content.querySelector("#estimate-service");
        services.forEach((service) => {
            const price = service.pricingMode === "fixed" ? ` · ${money(service.price)}` : " · Quote";
            select.add(new Option(`${service.name} · ${service.category}${price}`, service.id));
        });
    }

    async function openPaymentForm() {
        const eligible = orders.filter((order) => order.agreedAmount && balanceFor(order) > 0 && !["Rejected", "Cancelled"].includes(order.status));
        if (!eligible.length) {
            toast("No agreed orders with an outstanding balance are available.");
            return;
        }
        const content = await showDialog("dialog-payment-form");
        if (!content) return;
        const select = content.querySelector("#payment-order");
        eligible.forEach((order) => {
            const customer = customerFor(order);
            select.add(new Option(`${order.id} · ${customer?.name || ""} · balance ${money(balanceFor(order))}`, order.id));
        });
        content.querySelector("#payment-date").value = new Date().toISOString().slice(0, 10);
    }

    function confirmAndUpdateStatus(order, status) {
        if (!window.confirm(`Change ${order.id} from "${order.status}" to "${status}"? This is a temporary prototype update.`)) return;
        order.status = status;
        logActivity(`Order ${order.id} status changed to ${status}.`);
        dialog.close();
        renderView();
        toast(`Order status updated to ${status}.`);
    }

    function exportCsv(filename, rows) {
        const csv = rows.map((row) => row.map((cell) =>
            `"${String(cell ?? "").replace(/"/g, "\"\"")}"`
        ).join(",")).join("\r\n");
        const link = document.createElement("a");
        link.href = URL.createObjectURL(new Blob([`\uFEFF${csv}`], { type: "text/csv;charset=utf-8" }));
        link.download = filename;
        link.click();
        URL.revokeObjectURL(link.href);
    }

    function preserveRender() {
        const active = document.activeElement;
        const id = active?.id;
        const selectionStart = active?.selectionStart;
        renderView();
        if (id) {
            const replacement = document.getElementById(id);
            replacement?.focus();
            if (Number.isInteger(selectionStart) && replacement?.setSelectionRange) {
                replacement.setSelectionRange(selectionStart, selectionStart);
            }
        }
    }

    async function readUploadAsDataUrl(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.addEventListener("load", () => resolve(reader.result));
            reader.addEventListener("error", () => reject(new Error(`Could not read ${file.name}.`)));
            reader.readAsDataURL(file);
        });
    }

    async function saveLiveOrder(reference, action, payload) {
        try {
            const response = await fetch(`/api/admin/orders/${encodeURIComponent(reference)}/${action}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.detail || "The order update could not be saved.");
            const index = liveOrders.findIndex((order) => order.reference === reference);
            if (index >= 0) liveOrders[index] = result;
            dialog.close();
            renderView();
            await openLiveOrder(reference);
            toast("Order action recorded.");
        } catch (error) {
            toast(error instanceof Error ? error.message : "The order update could not be saved.");
        }
    }

    async function handleLiveAdminForm(form) {
        if (!form.reportValidity()) return;
        const reference = form.dataset.id;
        const formData = new FormData(form);
        const submitButton = form.querySelector('[type="submit"]');
        submitButton.disabled = true;
        try {
            if (form.dataset.form === "live-quote") {
                await saveLiveOrder(reference, "quotation", {
                    total_amount: Number(formData.get("total_amount")),
                    payment_instructions: String(formData.get("payment_instructions") || "").trim(),
                });
            } else if (form.dataset.form === "live-payment") {
                if (!formData.get("received") || !window.confirm("Record this amount as money the shop has actually received?")) return;
                await saveLiveOrder(reference, "payments", {
                    amount: Number(formData.get("amount")),
                    method: String(formData.get("method")),
                    reference: String(formData.get("reference") || "").trim(),
                    received: true,
                });
            } else if (form.dataset.form === "live-draft") {
                const file = form.querySelector('input[name="file"]').files[0];
                const acceptedTypes = new Set(["image/jpeg", "image/png", "application/pdf"]);
                if (!file || !acceptedTypes.has(file.type) || !/\.(jpe?g|png|pdf)$/i.test(file.name)) {
                    toast("Choose a JPG, PNG, or PDF draft file.");
                    return;
                }
                if (file.size > 5 * 1024 * 1024) {
                    toast("Draft files must be no larger than 5 MB.");
                    return;
                }
                const dataUrl = await readUploadAsDataUrl(file);
                await saveLiveOrder(reference, "draft", { file: { name: file.name, dataUrl } });
            }
        } catch (error) {
            toast(error instanceof Error ? error.message : "The order action could not be recorded.");
        } finally {
            submitButton.disabled = false;
        }
    }

    function handleFormSubmit(event) {
        const form = event.target.closest("form[data-form]");
        if (!form) return;
        event.preventDefault();
        const formData = new FormData(form);
        const type = form.dataset.form;
        if (type.startsWith("live-")) {
            void handleLiveAdminForm(form);
            return;
        }

        if (type === "order-update") {
            const order = orderFor(form.dataset.id);
            if (!order) return;
            const nextStatus = String(formData.get("status"));
            const amountValue = String(formData.get("amount") || "").trim();
            const newAmount = amountValue ? Number(amountValue) : null;
            if (nextStatus === "Approved for Printing") {
                toast("Only the customer can approve a submitted draft. Use a submitted customer request for the live workflow.");
                return;
            }
            if (nextStatus === "In Production" && order.status !== "In Production") {
                toast("A sample order cannot enter production without a recorded customer draft approval. Use a submitted customer request for the live workflow.");
                return;
            }
            if (newAmount !== null && (!Number.isFinite(newAmount) || newAmount < 0)) {
                toast("Enter a valid agreed amount.");
                return;
            }
            if (newAmount !== null && newAmount < paidFor(order.id)) {
                toast("Agreed amount cannot be less than payments already recorded.");
                return;
            }
            const statusChanged = nextStatus !== order.status;
            if (statusChanged && !window.confirm(`Save changes and update status to "${nextStatus}"?`)) return;
            order.status = nextStatus;
            order.agreedAmount = newAmount;
            order.remarks = String(formData.get("remarks") || "").trim();
            logActivity(`Order ${order.id} details updated${statusChanged ? `; status set to ${nextStatus}` : ""}.`);
            dialog.close();
            renderView();
            toast("Order details updated for this demo session.");
        }

        if (type === "service-save") {
            const id = form.dataset.id;
            const service = id ? services.find((item) => item.id === id) : null;
            const pricingMode = String(formData.get("pricingMode"));
            const priceText = String(formData.get("price") || "").trim();
            const price = priceText ? Number(priceText) : null;
            if (pricingMode === "fixed" && (price === null || !Number.isFinite(price) || price < 0)) {
                toast("Enter an administrator-defined price or select Request a quote.");
                return;
            }
            const item = {
                id: service?.id || `custom-${Date.now()}`,
                name: String(formData.get("name")).trim(),
                category: String(formData.get("category")),
                description: String(formData.get("description")).trim(),
                image: serviceImageDraftUrl || service?.image || categoryImages[String(formData.get("category"))],
                pricingMode,
                price: pricingMode === "fixed" ? price : null,
                available: formData.get("available") === "true"
            };
            if (!item.name || !item.description) {
                toast("Enter a product name and description.");
                return;
            }
            const previousImage = service?.image;
            if (service) Object.assign(service, item);
            else services.push(item);
            if (previousImage !== item.image) revokeUnusedImage(previousImage);
            serviceImageDraftUrl = "";
            logActivity(`${service ? "Service updated" : "Service added"}: ${item.name}.`);
            dialog.close();
            renderView();
            toast("Service catalog updated for this demo session.");
        }

        if (type === "price-save") {
            const service = services.find((item) => item.id === form.dataset.id);
            if (!service) return;
            const pricingMode = String(formData.get("pricingMode"));
            const priceText = String(formData.get("price") || "").trim();
            const price = priceText ? Number(priceText) : null;
            if (pricingMode === "fixed" && (price === null || !Number.isFinite(price) || price < 0)) {
                toast("A fixed-price rule needs a valid administrator-defined price.");
                return;
            }
            service.pricingMode = pricingMode;
            service.price = pricingMode === "fixed" ? price : null;
            logActivity(`Pricing rule updated for ${service.name}.`);
            dialog.close();
            renderView();
            toast("Pricing rule updated for this demo session.");
        }

        if (type === "payment-save") {
            const order = orderFor(String(formData.get("orderId")));
            const amount = Number(formData.get("amount"));
            if (!order || !Number.isFinite(amount) || amount <= 0) {
                toast("Select an order and enter a valid received amount.");
                return;
            }
            if (amount > balanceFor(order)) {
                toast(`Amount exceeds the remaining balance of ${money(balanceFor(order))}.`);
                return;
            }
            if (!window.confirm(`Record ${money(amount)} as received for ${order.id}? Only confirm money actually received.`)) return;
            payments.unshift({
                id: `TX-${Date.now().toString().slice(-6)}`,
                orderId: order.id,
                date: String(formData.get("date")),
                amount,
                method: String(formData.get("method")),
                reference: String(formData.get("reference")).trim(),
                remarks: String(formData.get("remarks") || "").trim()
            });
            logActivity(`Payment of ${money(amount)} recorded for ${order.id}.`);
            dialog.close();
            renderView();
            toast("Payment recorded in demo data.");
        }
    }

    document.addEventListener("click", async (event) => {
        const viewLink = event.target.closest("[data-view]");
        if (viewLink) {
            event.preventDefault();
            await navigate(viewLink.dataset.view);
            return;
        }
        const action = event.target.closest("[data-action]");
        if (!action) return;
        const id = action.dataset.id;
        switch (action.dataset.action) {
            case "menu": {
                const sidebar = document.querySelector("#admin-sidebar");
                const open = !sidebar.classList.contains("open");
                sidebar.classList.toggle("open", open);
                action.setAttribute("aria-expanded", String(open));
                action.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
                break;
            }
            case "dismiss-banner":
                bannerDismissed = true;
                action.closest(".prototype-banner").remove();
                break;
            case "logout":
                if (window.confirm("Sign out of the admin dashboard?")) {
                    try {
                        const response = await fetch("/api/admin/session", { method: "DELETE" });
                        if (!response.ok) throw new Error("The administrator session could not be cleared.");
                    } catch (error) {
                        toast(error instanceof Error ? error.message : "The administrator session could not be cleared.");
                        return;
                    }
                    window.location.assign("/admin/login");
                }
                break;
            case "close-dialog":
                dialog.close();
                break;
            case "order-detail":
                await openOrder(id);
                break;
            case "live-order-detail":
                await openLiveOrder(id);
                break;
            case "refresh-live-orders":
                liveOrdersLoaded = false;
                liveOrdersError = "";
                renderView();
                void refreshLiveOrders();
                break;
            case "live-status": {
                const order = liveOrderFor(id);
                const status = action.dataset.status;
                if (order && window.confirm(`Record ${status} for ${id}?`)) {
                    void saveLiveOrder(id, "status", { status });
                }
                break;
            }
            case "customer-detail":
                await openCustomer(id);
                break;
            case "accept-order": {
                const order = orderFor(id);
                if (order) confirmAndUpdateStatus(order, "Under Review");
                break;
            }
            case "reject-order": {
                const order = orderFor(id);
                if (order) confirmAndUpdateStatus(order, "Rejected");
                break;
            }
            case "add-service":
                await openServiceForm(null);
                break;
            case "edit-service":
                await openServiceForm(services.find((service) => service.id === id));
                break;
            case "edit-price":
                await openPriceForm(services.find((service) => service.id === id));
                break;
            case "show-calculator":
                await openCalculator();
                break;
            case "calculate-estimate": {
                const form = action.closest("form");
                const service = services.find((item) => item.id === form.elements.serviceId.value);
                const quantity = Number(form.elements.quantity.value);
                const output = form.querySelector("#estimate-total");
                if (!service || !Number.isInteger(quantity) || quantity < 1) {
                    output.textContent = "Enter a valid quantity.";
                } else if (service.pricingMode !== "fixed" || service.price === null) {
                    output.textContent = "Request a quote";
                } else {
                    output.textContent = money(service.price * quantity);
                }
                break;
            }
            case "record-payment":
                await openPaymentForm();
                break;
            case "mark-all-read":
                notifications.forEach((notification) => { notification.read = true; });
                renderView();
                toast("All sample notifications marked as read.");
                break;
            case "toggle-setting":
                action.setAttribute("aria-pressed", String(action.getAttribute("aria-pressed") !== "true"));
                toast("Preference changed for this session.");
                break;
            case "clear-report-filters":
                filters.reports = { from: "", to: "", category: "", period: "" };
                renderView();
                break;
            case "print-report":
                window.print();
                break;
            case "export-orders":
                exportCsv("impressions-demo-orders.csv", [
                    ["Order number", "Customer", "Email", "Product", "Category", "Date", "Agreed amount", "Status"],
                    ...orders.map((order) => [order.id, customerFor(order)?.name, customerFor(order)?.email, order.product, order.category, order.date, order.agreedAmount ?? "Quote", order.status])
                ]);
                break;
            case "export-report": {
                const reportRecords = payments.filter((payment) => {
                    const order = orderFor(payment.orderId);
                    const filter = filters.reports;
                    return (!filter.from || payment.date >= filter.from) && (!filter.to || payment.date <= filter.to) && (!filter.category || order?.category === filter.category);
                });
                exportCsv("impressions-demo-payments-report.csv", [
                    ["Date", "Transaction", "Order", "Category", "Product", "Payment received PHP"],
                    ...reportRecords.map((payment) => {
                        const order = orderFor(payment.orderId);
                        return [payment.date, payment.id, payment.orderId, order?.category, order?.product, payment.amount];
                    })
                ]);
                break;
            }
            default:
                break;
        }
    });

    document.addEventListener("submit", handleFormSubmit);
    document.addEventListener("input", (event) => {
        const target = event.target;
        if (target.id === "order-search") filters.orders.search = target.value;
        else if (target.id === "customer-search") filters.customers.search = target.value;
        else if (target.id === "service-search") filters.services.search = target.value;
        else if (target.id === "payment-search") filters.payments.search = target.value;
        else return;
        window.clearTimeout(filterTimer);
        filterTimer = window.setTimeout(preserveRender, 100);
    });

    document.addEventListener("change", (event) => {
        if (event.target.id !== "service-image") return;
        const file = event.target.files?.[0];
        if (!file) return;
        if (!file.type.startsWith("image/")) {
            event.target.value = "";
            toast("Choose an image file for the product preview.");
            return;
        }
        revokeUnusedImage(serviceImageDraftUrl);
        serviceImageDraftUrl = URL.createObjectURL(file);
        const preview = document.querySelector("#service-image-preview");
        preview.src = serviceImageDraftUrl;
        preview.hidden = false;
    });

    document.addEventListener("change", (event) => {
        const target = event.target;
        if (target.id === "chart-period") {
            chartPeriod = target.value;
            renderView();
        } else if (target.id === "order-category-filter") {
            filters.orders.category = target.value;
            renderView();
        } else if (target.id === "order-status-filter") {
            filters.orders.status = target.value;
            renderView();
        } else if (target.id === "order-date-from") {
            filters.orders.from = target.value;
            renderView();
        } else if (target.id === "order-date-to") {
            filters.orders.to = target.value;
            renderView();
        } else if (target.id === "service-category-filter") {
            filters.services.category = target.value;
            renderView();
        } else if (target.id === "report-from") {
            filters.reports.from = target.value;
            filters.reports.period = "";
            renderView();
        } else if (target.id === "report-to") {
            filters.reports.to = target.value;
            filters.reports.period = "";
            renderView();
        } else if (target.id === "report-category") {
            filters.reports.category = target.value;
            renderView();
        } else if (target.id === "report-period") {
            const today = new Date();
            const start = new Date(today);
            if (target.value === "day") {
                start.setHours(0, 0, 0, 0);
            } else if (target.value === "week") {
                start.setDate(today.getDate() - ((today.getDay() + 6) % 7));
                start.setHours(0, 0, 0, 0);
            } else if (target.value === "month") {
                start.setDate(1);
                start.setHours(0, 0, 0, 0);
            } else if (target.value === "year") {
                start.setMonth(0, 1);
                start.setHours(0, 0, 0, 0);
            }
            filters.reports.period = target.value;
            filters.reports.from = target.value ? localDate(start) : "";
            filters.reports.to = target.value ? localDate(today) : "";
            renderView();
        } else if (target.id === "payment-order") {
            const form = target.closest("form");
            const order = orderFor(target.value);
            const amount = form?.querySelector("#payment-amount");
            if (order && amount) amount.max = String(balanceFor(order));
        } else if (target.id === "estimate-service" || target.id === "estimate-quantity") {
            const form = target.closest("form");
            const service = services.find((item) => item.id === form?.elements.serviceId.value);
            const quantity = Number(form?.elements.quantity.value);
            const output = form?.querySelector("#estimate-total");
            if (output && service) {
                output.textContent = service.pricingMode === "fixed" && service.price !== null && quantity > 0
                    ? money(service.price * quantity)
                    : "Request a quote";
            }
        }
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") document.querySelector("#admin-sidebar").classList.remove("open");
    });
    window.addEventListener("popstate", () => {
        void navigate(viewFromLocation(), false);
    });
    activeView = document.body.dataset.adminView || viewFromLocation();
    updateViewStylesheet();
    renderView();
    void refreshLiveOrders();
})();
