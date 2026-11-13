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
    const ordersContainer = document.querySelector("#account-orders");
    // Only the customer portal has an account panel; on other pages this script does nothing.
    if (!panel) return;

    let activeCustomer = null;

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

    // The "Orders will appear here" card belongs to the guest lookup; signed-in customers see their orders above.
    const lookupEmptyCard = () => document.querySelector("#portal-empty");

    function showSignedOut() {
        activeCustomer = null;
        panel.hidden = true;
        if (signedOut) signedOut.hidden = false;
        if (lookupEmptyCard()) lookupEmptyCard().hidden = false;
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
        if (lookupEmptyCard()) lookupEmptyCard().hidden = true;
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
        void loadOrders();
    }

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
                details.textContent = `${order.reference} · ${order.status} · ${order.customization.quantity} item(s) · ${paying}`;
                main.append(category, title, details);
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
        if (activeCustomer) void loadOrders();
    });
    document.addEventListener("customer-session-ended", sessionEnded);

    profileForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
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
})();
