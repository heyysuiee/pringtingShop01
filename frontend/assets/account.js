"use strict";

(() => {
    const forms = document.querySelector("#account-forms");
    const panel = document.querySelector("#account-panel");
    const message = document.querySelector("#account-message");
    const loginForm = document.querySelector("#customer-login-form");
    const registerForm = document.querySelector("#customer-register-form");
    const profileForm = document.querySelector("#customer-profile-form");
    const ordersContainer = document.querySelector("#account-orders");
    let activeCustomer = null;

    function showMessage(text, isError = false) {
        message.textContent = text;
        message.hidden = !text;
        message.classList.toggle("field-error", isError);
        message.classList.toggle("field-hint", Boolean(text) && !isError);
    }

    async function request(url, options = {}) {
        const response = await fetch(url, { credentials: "same-origin", ...options });
        const result = response.status === 204 ? null : await response.json();
        if (!response.ok) {
            const detail = result?.detail;
            throw new Error(typeof detail === "string" ? detail : "The request could not be completed.");
        }
        return result;
    }

    function renderCustomer(customer) {
        activeCustomer = customer;
        forms.hidden = true;
        panel.hidden = false;
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
                details.textContent = `${order.reference} · ${order.status} · ${order.customization.quantity} item(s)`;
                main.append(category, title, details);
                const viewOrder = document.createElement("button");
                viewOrder.className = "btn btn-dark btn-small";
                viewOrder.type = "button";
                viewOrder.textContent = "View order progress";
                viewOrder.addEventListener("click", () => {
                    document.dispatchEvent(new CustomEvent("customer-order-select", {
                        detail: { email: activeCustomer.email, reference: order.reference },
                    }));
                });
                card.append(main, viewOrder);
                ordersContainer.append(card);
            }
        } catch (error) {
            showMessage(error instanceof Error ? error.message : "Orders could not be loaded.", true);
        } finally {
            ordersContainer.removeAttribute("aria-busy");
        }
    }

    async function submitForm(form, url, payload) {
        showMessage("");
        const button = form.querySelector('[type="submit"]');
        button.disabled = true;
        try {
            const result = await request(url, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            renderCustomer(result.customer);
        } catch (error) {
            showMessage(error instanceof Error ? error.message : "The request could not be completed.", true);
        } finally {
            button.disabled = false;
        }
    }

    loginForm.addEventListener("submit", (event) => {
        event.preventDefault();
        if (!loginForm.reportValidity()) return;
        const values = new FormData(loginForm);
        void submitForm(loginForm, "/api/customer/session", {
            email: String(values.get("email") || "").trim(),
            password: String(values.get("password") || ""),
        });
    });

    registerForm.addEventListener("submit", (event) => {
        event.preventDefault();
        if (!registerForm.reportValidity()) return;
        const values = new FormData(registerForm);
        void submitForm(registerForm, "/api/customer/register", {
            name: String(values.get("name") || "").trim(),
            email: String(values.get("email") || "").trim(),
            phone: String(values.get("phone") || "").trim(),
            messenger: String(values.get("messenger") || "").trim(),
            password: String(values.get("password") || ""),
        });
    });

    profileForm.addEventListener("submit", async (event) => {
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
            showMessage(error instanceof Error ? error.message : "Your profile could not be saved.", true);
        } finally {
            button.disabled = false;
        }
    });

    document.querySelector("#customer-logout").addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            await request("/api/customer/session", { method: "DELETE" });
            activeCustomer = null;
            panel.hidden = true;
            forms.hidden = false;
            showMessage("You have signed out.");
        } catch (error) {
            showMessage(error instanceof Error ? error.message : "Sign out failed.", true);
        } finally {
            button.disabled = false;
        }
    });

    request("/api/customer/session")
        .then((result) => renderCustomer(result.customer))
        .catch(() => {
            forms.hidden = false;
            panel.hidden = true;
        });
})();
