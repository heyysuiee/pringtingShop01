"use strict";

(() => {
    const forms = document.querySelector("#account-forms");
    const panel = document.querySelector("#account-panel");
    const message = document.querySelector("#account-message");
    const loginForm = document.querySelector("#customer-login-form");
    const registerForm = document.querySelector("#customer-register-form");
    const profileForm = document.querySelector("#customer-profile-form");
    const ordersContainer = document.querySelector("#account-orders");
    const forgotCard = document.querySelector("#forgot-password");
    const isOrderGate = Boolean(document.querySelector("#order-form"));
    const RESEND_SECONDS = 60;

    let activeCustomer = null;
    let resetEmail = "";
    let resetToken = "";
    let resendAvailableAt = 0;
    let resendTimer = null;

    function setNote(element, text, isError = false) {
        if (!element) return;
        element.textContent = text;
        element.hidden = !text;
        element.classList.toggle("field-error", isError);
        element.classList.toggle("field-hint", Boolean(text) && !isError);
    }

    const showMessage = (text, isError = false) => setNote(message, text, isError);

    async function request(url, options = {}) {
        const response = await fetch(url, { credentials: "same-origin", ...options });
        if (response.status === 204) return null;
        let result = null;
        try {
            result = await response.json();
        } catch {
            result = null;
        }
        if (!response.ok) {
            const detail = result?.detail;
            // FastAPI reports validation problems as a list of {msg} objects.
            const text = Array.isArray(detail) ? detail.map((item) => item.msg).join(" ") : detail;
            throw new Error(typeof text === "string" && text ? text : "The request could not be completed.");
        }
        return result;
    }

    function jsonOptions(method, payload) {
        return {
            method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        };
    }

    // ---- Signed-in state (customer portal only) -------------------------------------

    function renderCustomer(customer) {
        activeCustomer = customer;
        if (!panel) return;
        forms.hidden = true;
        forgotCard.hidden = true;
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

    function afterSignIn(customer) {
        // On the order page the gate is driven by the session, so reloading shows the order form.
        if (isOrderGate) {
            window.location.reload();
            return;
        }
        renderCustomer(customer);
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
                details.textContent = `${order.reference} · ${order.status} · ${order.customization.quantity} item(s)`;
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
            showMessage(error instanceof Error ? error.message : "Orders could not be loaded.", true);
        } finally {
            ordersContainer.removeAttribute("aria-busy");
        }
    }

    // ---- Sign in / register ----------------------------------------------------------

    async function submitAuth(form, url, payload) {
        showMessage("");
        const button = form.querySelector('[type="submit"]');
        button.disabled = true;
        button.classList.add("is-loading");
        try {
            const result = await request(url, jsonOptions("POST", payload));
            afterSignIn(result.customer);
        } catch (error) {
            showMessage(error instanceof Error ? error.message : "The request could not be completed.", true);
        } finally {
            button.disabled = false;
            button.classList.remove("is-loading");
        }
    }

    loginForm?.addEventListener("submit", (event) => {
        event.preventDefault();
        if (!loginForm.reportValidity()) return;
        const values = new FormData(loginForm);
        void submitAuth(loginForm, "/api/customer/session", {
            email: String(values.get("email") || "").trim(),
            password: String(values.get("password") || ""),
        });
    });

    registerForm?.addEventListener("submit", (event) => {
        event.preventDefault();
        if (!registerForm.reportValidity()) return;
        const values = new FormData(registerForm);
        if (String(values.get("password")) !== String(values.get("confirm_password"))) {
            showMessage("The password and confirmation do not match.", true);
            registerForm.elements.namedItem("confirm_password").focus();
            return;
        }
        void submitAuth(registerForm, "/api/customer/register", {
            name: String(values.get("name") || "").trim(),
            email: String(values.get("email") || "").trim(),
            phone: String(values.get("phone") || "").trim(),
            messenger: String(values.get("messenger") || "").trim(),
            password: String(values.get("password") || ""),
        });
    });

    // ---- Forgot password (email OTP) -------------------------------------------------

    const forgotSteps = {
        email: document.querySelector('[data-forgot-step="email"]'),
        code: document.querySelector('[data-forgot-step="code"]'),
        password: document.querySelector('[data-forgot-step="password"]'),
    };
    const forgotMessage = document.querySelector("#forgot-message");
    const resendButton = document.querySelector("#forgot-resend");

    function showForgotStep(name) {
        for (const [key, element] of Object.entries(forgotSteps)) {
            if (element) element.hidden = key !== name;
        }
        setNote(forgotMessage, "");
    }

    function openForgotPassword() {
        if (!forgotCard) return;
        forms.hidden = true;
        forgotCard.hidden = false;
        const loginEmail = document.querySelector("#login-email");
        const resetEmailInput = document.querySelector("#forgot-email");
        if (resetEmailInput && loginEmail?.value) resetEmailInput.value = loginEmail.value;
        showForgotStep("email");
        resetEmailInput?.focus();
    }

    function closeForgotPassword(notice = "") {
        if (!forgotCard) return;
        forgotCard.hidden = true;
        forms.hidden = false;
        resetEmail = "";
        resetToken = "";
        clearInterval(resendTimer);
        showMessage(notice);
    }

    function updateResendButton() {
        if (!resendButton) return;
        const remaining = Math.ceil((resendAvailableAt - Date.now()) / 1000);
        if (remaining > 0) {
            resendButton.disabled = true;
            resendButton.textContent = `Resend code in ${remaining}s`;
        } else {
            clearInterval(resendTimer);
            resendButton.disabled = false;
            resendButton.textContent = "Resend code";
        }
    }

    function startResendCooldown(seconds) {
        resendAvailableAt = Date.now() + seconds * 1000;
        clearInterval(resendTimer);
        updateResendButton();
        resendTimer = setInterval(updateResendButton, 1000);
    }

    async function requestResetCode(email, button) {
        if (button) button.disabled = true;
        try {
            const result = await request("/api/customer/password-reset/request", jsonOptions("POST", { email }));
            resetEmail = email;
            setNote(forgotMessage, result.message);
            document.querySelector("#forgot-sent-to").textContent = email;
            document.querySelector("#forgot-code").value = "";
            showForgotStep("code");
            startResendCooldown(result.resend_after || RESEND_SECONDS);
            document.querySelector("#forgot-code").focus();
        } catch (error) {
            setNote(forgotMessage, error instanceof Error ? error.message : "The verification code could not be sent.", true);
        } finally {
            // The resend button stays disabled while its cooldown runs; only the other buttons are re-enabled here.
            if (button && button !== resendButton) button.disabled = false;
            else updateResendButton();
        }
    }

    document.querySelector("#open-forgot-password")?.addEventListener("click", openForgotPassword);
    document.querySelector("#back-to-sign-in")?.addEventListener("click", () => closeForgotPassword());
    document.querySelector("#forgot-back")?.addEventListener("click", () => {
        clearInterval(resendTimer);
        showForgotStep("email");
    });

    document.querySelector("#forgot-email-form")?.addEventListener("submit", (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        if (!form.reportValidity()) return;
        const email = String(new FormData(form).get("email") || "").trim();
        void requestResetCode(email, form.querySelector('[type="submit"]'));
    });

    resendButton?.addEventListener("click", () => {
        if (resetEmail) void requestResetCode(resetEmail, resendButton);
    });

    document.querySelector("#forgot-code-form")?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        if (!form.reportValidity()) return;
        const button = form.querySelector('[type="submit"]');
        button.disabled = true;
        try {
            const code = String(new FormData(form).get("code") || "").trim();
            const result = await request("/api/customer/password-reset/verify", jsonOptions("POST", { email: resetEmail, code }));
            resetToken = result.reset_token;
            showForgotStep("password");
            document.querySelector("#forgot-new-password").focus();
        } catch (error) {
            setNote(forgotMessage, error instanceof Error ? error.message : "The code could not be verified.", true);
        } finally {
            button.disabled = false;
        }
    });

    document.querySelector("#forgot-password-form")?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        if (!form.reportValidity()) return;
        const values = new FormData(form);
        const password = String(values.get("new_password") || "");
        if (password !== String(values.get("confirm_password") || "")) {
            setNote(forgotMessage, "The new password and confirmation do not match.", true);
            return;
        }
        const button = form.querySelector('[type="submit"]');
        button.disabled = true;
        try {
            const result = await request("/api/customer/password-reset/confirm", jsonOptions("POST", {
                email: resetEmail,
                reset_token: resetToken,
                new_password: password,
            }));
            const loginEmail = document.querySelector("#login-email");
            if (loginEmail) loginEmail.value = resetEmail;
            form.reset();
            closeForgotPassword(result.message);
        } catch (error) {
            setNote(forgotMessage, error instanceof Error ? error.message : "The password could not be changed.", true);
        } finally {
            button.disabled = false;
        }
    });

    // ---- Profile and sign out (customer portal) ------------------------------------

    profileForm?.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!profileForm.reportValidity() || !activeCustomer) return;
        showMessage("");
        const button = profileForm.querySelector('[type="submit"]');
        button.disabled = true;
        try {
            const values = new FormData(profileForm);
            const result = await request("/api/account/profile", jsonOptions("PATCH", {
                name: String(values.get("name") || "").trim(),
                phone: String(values.get("phone") || "").trim(),
                messenger: String(values.get("messenger") || "").trim(),
            }));
            renderCustomer(result.customer);
            showMessage("Your profile was saved.");
        } catch (error) {
            showMessage(error instanceof Error ? error.message : "Your profile could not be saved.", true);
        } finally {
            button.disabled = false;
        }
    });

    document.querySelector("#customer-logout")?.addEventListener("click", async (event) => {
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

    if (panel) {
        request("/api/customer/session")
            .then((result) => renderCustomer(result.customer))
            .catch(() => {
                if (forms) forms.hidden = false;
                panel.hidden = true;
            });
    }
})();
