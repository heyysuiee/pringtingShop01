"use strict";

/**
 * Customer portal account section: shows the signed-in customer's profile and orders, or a
 * "Sign in" prompt. Signing in, creating an account, and resetting a password happen on the
 * dedicated pages (/sign-in, /create-account, /forgot-password) handled by auth.js.
 */
(() => {
    const panel = document.querySelector("#account-panel");
    const signedOut = document.querySelector("#account-signed-out");
    const message = document.querySelector("#account-message");
    const profileForm = document.querySelector("#customer-profile-form");
    const addressForm = document.querySelector("#customer-address-form");
    const addressProvince = document.querySelector("#address-province");
    const addressCity = document.querySelector("#address-city");
    const addressRegion = document.querySelector("#address-region");
    const ordersContainer = document.querySelector("#account-orders");
    const notificationsList = document.querySelector("#account-notifications");
    const notificationCount = document.querySelector("#notification-count");
    const readAllButton = document.querySelector("#notifications-read-all");
    // The bell in the navigation bar and the dropdown it opens.
    const notifyMenu = document.querySelector("#nav-notifications");
    const notifyToggle = document.querySelector("#notifications-toggle");
    const notifyPanel = document.querySelector("#notifications-panel");
    // Only the customer portal has an account panel; on other pages this script does nothing.
    if (!panel) return;

    let activeCustomer = null;
    let locationData = { provinces: [] };
    let pendingSavedAddress = null;

    function setNotificationsOpen(open) {
        if (!notifyPanel || !notifyToggle) return;
        notifyPanel.hidden = !open;
        notifyToggle.setAttribute("aria-expanded", String(open));
    }

    notifyToggle?.addEventListener("click", () => {
        const opening = notifyPanel.hidden;
        setNotificationsOpen(opening);
        if (opening) void loadNotifications();
    });
    // Close the dropdown on a click outside it or on Escape.
    document.addEventListener("click", (event) => {
        if (notifyPanel && !notifyPanel.hidden && !notifyMenu.contains(event.target)) setNotificationsOpen(false);
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && notifyPanel && !notifyPanel.hidden) {
            setNotificationsOpen(false);
            notifyToggle.focus();
        }
    });

    // "Pending Confirmation" → "pending-confirmation": the class that colours the status badge.
    const statusSlug = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

    /** Same PH mobile/landline rule as registration and the server. */
    function validPhone(value) {
        if (window.PrintShopPhone?.valid) return window.PrintShopPhone.valid(value);
        const digits = String(value || "").replace(/\D/g, "");
        return /^(?:0|63)9\d{9}$/.test(digits) || /^0\d{7,9}$/.test(digits);
    }

    // PSGC names can appear with a "(Province)" suffix; match on the normalized key used by the server.
    const locationKey = (value) => String(value || "").toLowerCase().replace(/\s*\([^)]*\)/g, "")
        .replace(/^province of\s+/, "").replace(/\s+province$/, "").trim();
    const selectedAddressProvince = () => locationData.provinces.find(
        (item) => locationKey(item.name) === locationKey(addressProvince?.value),
    ) || null;

    /** Keep the city list dependent on the province; the server repeats this check before saving. */
    function syncAddressCities(preferredCity = "") {
        if (!addressCity) return;
        const province = selectedAddressProvince();
        addressCity.replaceChildren(
            new Option(province ? "Select city / municipality" : "Select province first", ""),
            ...(province?.cities || []).map((name) => new Option(name, name)),
        );
        if (preferredCity) {
            const match = (province?.cities || []).find((name) => locationKey(name) === locationKey(preferredCity));
            if (match) addressCity.value = match;
        }
        if (addressRegion) {
            addressRegion.textContent = province
                ? `${province.region} · ${province.island_group.charAt(0).toUpperCase()}${province.island_group.slice(1)}`
                : "";
        }
    }

    /** Load the PSGC dropdown data, then fill any saved address that arrived before it. */
    async function loadLocationOptions() {
        if (!addressProvince) return;
        try {
            locationData = await request("/api/locations");
            addressProvince.replaceChildren(
                new Option("Select province", ""),
                ...locationData.provinces.map((item) => new Option(item.name, item.name)),
            );
            syncAddressCities();
            const saved = pendingSavedAddress || activeCustomer?.saved_address;
            if (saved) applySavedAddress(saved);
        } catch (error) {
            if (addressRegion) addressRegion.textContent = "The address list is unavailable. Refresh the page to try again.";
            handleFailure(error, "The address list could not be loaded.");
        }
    }

    /** Populate the saved-address form from the account; checkout prefills from this same record. */
    function applySavedAddress(saved) {
        if (!saved || !addressForm || !locationData.provinces.length) {
            pendingSavedAddress = saved || null;
            return;
        }
        for (const [name, value] of Object.entries(saved)) {
            const input = addressForm.elements.namedItem(name);
            if (input) input.value = value || "";
        }
        syncAddressCities(saved.city || "");
        pendingSavedAddress = null;
    }

    addressProvince?.addEventListener("change", () => syncAddressCities());

    function showMessage(text, isError = false) {
        if (!message) return;
        message.textContent = text;
        message.hidden = !text;
        message.classList.toggle("field-error", isError);
        message.classList.toggle("field-hint", Boolean(text) && !isError);
    }

    async function request(url, options = {}) {
        const response = await fetch(url, { credentials: "same-origin", ...options });
        if (response.status === 204) return null;
        const result = await response.json().catch(() => null);
        if (!response.ok) {
            const detail = result?.detail;
            // FastAPI reports validation problems as a list of {msg} objects.
            const text = Array.isArray(detail) ? detail.map((item) => item.msg).join(" ") : detail;
            const error = new Error(typeof text === "string" && text ? text : "The request could not be completed.");
            error.status = response.status;
            throw error;
        }
        return result;
    }

    // The "Sign in to see your orders" card is for guests; signed-in customers see their orders above.
    const lookupEmptyCard = () => document.querySelector("#portal-empty");
    // Finding an order by reference is for signed-in customers only (the server checks ownership).
    const lookupPanel = document.querySelector("#order-lookup");
    const orderDetail = document.querySelector("#order-detail");

    function showSignedOut() {
        activeCustomer = null;
        panel.hidden = true;
        setNotificationsOpen(false);
        if (notifyMenu) notifyMenu.hidden = true;
        if (signedOut) signedOut.hidden = false;
        if (lookupEmptyCard()) lookupEmptyCard().hidden = false;
        if (lookupPanel) lookupPanel.hidden = true;
        // An order that was open stays private once the session has ended.
        if (orderDetail) orderDetail.hidden = true;
        const accountSection = document.querySelector("#customer-account");
        if (accountSection) accountSection.hidden = false;
        const ordersList = document.querySelector("#orders-list");
        if (ordersList) ordersList.hidden = false;
    }

    /** The session expired or was ended elsewhere (for example a password reset): show the sign-in prompt. */
    function sessionEnded() {
        showSignedOut();
        showMessage("Your session has ended. Please sign in again.", true);
    }

    /** Handle a failed account request: a 401 means the session is gone; anything else is shown as an error. */
    function handleFailure(error, fallback) {
        if (error?.status === 401) {
            sessionEnded();
            return;
        }
        showMessage(error instanceof Error ? error.message : fallback, true);
    }

    function renderCustomer(customer) {
        activeCustomer = customer;
        if (signedOut) signedOut.hidden = true;
        panel.hidden = false;
        if (notifyMenu) notifyMenu.hidden = false;
        if (lookupEmptyCard()) lookupEmptyCard().hidden = true;
        if (lookupPanel) lookupPanel.hidden = false;
        showMessage(`Signed in as ${customer.email}.`);
        for (const [name, value] of Object.entries({
            name: customer.name,
            email: customer.email,
            phone: customer.phone,
            messenger: customer.messenger,
        })) {
            const input = profileForm.elements.namedItem(name);
            if (input) input.value = value || "";
        }
        applySavedAddress(customer.saved_address);
        void loadOrders();
        void loadNotifications();
    }

    function formatWhen(value) {
        const parsed = new Date(value);
        return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleString("en-PH", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    }

    /** Quotation, payment, and order updates from the shop. Opening one shows its order and marks it read. */
    function renderNotifications(result) {
        if (!notificationsList) return;
        notificationsList.replaceChildren();
        const unread = result.unread || 0;
        if (notificationCount) {
            notificationCount.hidden = unread === 0;
            notificationCount.textContent = unread > 9 ? "9+" : String(unread || "");
        }
        notifyToggle?.setAttribute("aria-label", unread ? `Notifications, ${unread} unread` : "Notifications");
        if (readAllButton) readAllButton.hidden = unread === 0;
        if (!result.notifications.length) {
            const empty = document.createElement("li");
            empty.className = "field-hint";
            empty.textContent = "No notifications yet. You will be notified here when the shop sends a quotation or verifies a payment.";
            notificationsList.append(empty);
            return;
        }
        for (const item of result.notifications.slice(0, 10)) {
            const entry = document.createElement("li");
            entry.className = `notification-item${item.read ? "" : " is-unread"}`;
            const button = document.createElement("button");
            button.type = "button";
            button.className = "notification-button";
            const title = document.createElement("strong");
            title.textContent = item.title;
            const message = document.createElement("span");
            message.textContent = item.message;
            const meta = document.createElement("small");
            meta.textContent = `${item.order_reference} · ${formatWhen(item.created_at)}`;
            button.append(title, message, meta);
            button.addEventListener("click", async () => {
                if (!item.read) {
                    try {
                        renderNotifications(await request("/api/account/notifications/read", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ ids: [item.id] }),
                        }));
                    } catch (error) {
                        handleFailure(error, "The notification could not be updated.");
                    }
                }
                setNotificationsOpen(false);
                document.dispatchEvent(new CustomEvent("customer-order-select", { detail: { reference: item.order_reference, signedIn: true } }));
            });
            entry.append(button);
            notificationsList.append(entry);
        }
    }

    async function loadNotifications() {
        if (!notificationsList) return;
        try {
            renderNotifications(await request("/api/account/notifications"));
        } catch (error) {
            handleFailure(error, "Notifications could not be loaded.");
        }
    }

    readAllButton?.addEventListener("click", async () => {
        readAllButton.disabled = true;
        try {
            renderNotifications(await request("/api/account/notifications/read", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({}),
            }));
        } catch (error) {
            handleFailure(error, "Notifications could not be updated.");
        } finally {
            readAllButton.disabled = false;
        }
    });

    async function loadOrders() {
        if (!ordersContainer) return;
        ordersContainer.setAttribute("aria-busy", "true");
        try {
            const result = await request("/api/account/orders");
            ordersContainer.replaceChildren();
            if (!result.orders.length) {
                const empty = document.createElement("p");
                empty.className = "field-hint";
                empty.textContent = "No orders have been placed while signed in to this account.";
                ordersContainer.append(empty);
                return;
            }
            for (const order of result.orders) {
                const card = document.createElement("article");
                card.className = "card order-history-card";
                const main = document.createElement("div");
                main.className = "order-history-main";
                const category = document.createElement("span");
                category.className = "service-tag";
                category.textContent = order.product.category;
                const title = document.createElement("h3");
                title.textContent = order.product.name;
                const details = document.createElement("p");
                details.className = "field-hint";
                const paying = order.payment?.payment_option === "cod" ? "Cash on Delivery" : "50% down payment";
                details.textContent = `${order.reference} · ${order.customization.quantity} item(s) · ${paying}`;
                const status = document.createElement("span");
                status.className = `order-status-pill ${statusSlug(order.status)}`;
                status.textContent = order.status;
                main.append(category, title, status, details);
                const viewOrder = document.createElement("button");
                viewOrder.className = "btn btn-dark btn-small";
                viewOrder.type = "button";
                viewOrder.textContent = "View order progress";
                viewOrder.addEventListener("click", () => {
                    document.dispatchEvent(new CustomEvent("customer-order-select", {
                        detail: { reference: order.reference, signedIn: true },
                    }));
                });
                card.append(main, viewOrder);
                ordersContainer.append(card);
            }
        } catch (error) {
            handleFailure(error, "Orders could not be loaded.");
        } finally {
            ordersContainer.removeAttribute("aria-busy");
        }
    }

    // Sent by portal.js: after viewing an order (it may have been approved or cancelled), and when its session ends.
    document.addEventListener("customer-orders-changed", () => {
        if (!activeCustomer) return;
        void loadOrders();
        void loadNotifications();
    });
    document.addEventListener("customer-session-ended", sessionEnded);

    profileForm?.elements.phone?.addEventListener("input", () => profileForm.elements.phone.setCustomValidity(""));
    addressForm?.elements.namedItem("contact_number")?.addEventListener("input", (event) => event.currentTarget.setCustomValidity(""));
    addressForm?.elements.namedItem("city")?.addEventListener("input", (event) => event.currentTarget.setCustomValidity(""));

    profileForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const phoneField = profileForm.elements.phone;
        if (phoneField) {
            phoneField.setCustomValidity("");
            if (!validPhone(phoneField.value)) {
                phoneField.setCustomValidity("Enter a valid Philippine mobile or landline number.");
                phoneField.reportValidity();
                return;
            }
        }
        if (!profileForm.reportValidity() || !activeCustomer) return;
        showMessage("");
        const button = profileForm.querySelector('[type="submit"]');
        button.disabled = true;
        try {
            const values = new FormData(profileForm);
            const result = await request("/api/account/profile", {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    name: String(values.get("name") || "").trim(),
                    phone: String(values.get("phone") || "").trim(),
                    messenger: String(values.get("messenger") || "").trim(),
                }),
            });
            renderCustomer(result.customer);
            showMessage("Your profile was saved.");
        } catch (error) {
            handleFailure(error, "Your profile could not be saved.");
        } finally {
            button.disabled = false;
        }
    });

    addressForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const contactField = addressForm.elements.namedItem("contact_number");
        const cityField = addressForm.elements.namedItem("city");
        contactField.setCustomValidity("");
        cityField.setCustomValidity("");
        if (!validPhone(contactField.value)) {
            contactField.setCustomValidity("Enter a valid Philippine mobile or landline number.");
            contactField.reportValidity();
            return;
        }
        const province = selectedAddressProvince();
        const city = (province?.cities || []).find((name) => locationKey(name) === locationKey(cityField.value));
        if (!province || !city) {
            cityField.setCustomValidity("Choose a city or municipality in the selected province.");
            cityField.reportValidity();
            return;
        }
        if (!addressForm.reportValidity() || !activeCustomer) return;
        showMessage("");
        const button = addressForm.querySelector('[type="submit"]');
        button.disabled = true;
        const values = new FormData(addressForm);
        try {
            const result = await request("/api/account/delivery-address", {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    recipient_name: String(values.get("recipient_name") || "").trim(),
                    contact_number: String(values.get("contact_number") || "").trim(),
                    street: String(values.get("street") || "").trim(),
                    barangay: String(values.get("barangay") || "").trim(),
                    province: province.name,
                    city,
                    landmark: String(values.get("landmark") || "").trim(),
                    delivery_instructions: String(values.get("delivery_instructions") || "").trim(),
                }),
            });
            activeCustomer = result.customer;
            applySavedAddress(result.customer.saved_address);
            showMessage("Your delivery address was saved. It will be filled in at checkout.");
        } catch (error) {
            handleFailure(error, "Your delivery address could not be saved.");
        } finally {
            button.disabled = false;
        }
    });

    document.querySelector("#customer-logout")?.addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            await request("/api/customer/session", { method: "DELETE" });
            showSignedOut();
            showMessage("You have signed out.");
        } catch (error) {
            handleFailure(error, "Sign out failed.");
        } finally {
            button.disabled = false;
        }
    });

    request("/api/customer/session")
        .then((result) => renderCustomer(result.customer))
        .catch(showSignedOut);
    void loadLocationOptions();
})();
