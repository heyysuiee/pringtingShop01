"use strict";

/**
 * Administrator "Forgot password?" page.
 * Talks to /api/admin/password-reset/* : a 6-digit code is emailed to the configured admin
 * address, verified, and then a new password is saved (the password reset also signs out
 * every older administrator session).
 */
(() => {
    const steps = [...document.querySelectorAll("[data-reset-step]")];
    const progress = document.querySelector("#reset-steps");
    const backLink = document.querySelector("#reset-back");
    const alertBox = document.querySelector("#reset-alert");
    const emailForm = document.querySelector("#reset-email-form");
    const codeForm = document.querySelector("#reset-code-form");
    const passwordForm = document.querySelector("#reset-password-form");
    const codeInput = document.querySelector("#reset-code");
    const resendButton = document.querySelector("#reset-resend");
    const expiryLabel = document.querySelector("#reset-expiry");
    const PASSWORD_MIN_LENGTH = 12;
    const RESET_EMAIL_KEY = "printshop.adminResetEmail";

    // Kept in memory only: the reset token is never written to storage or the URL.
    const state = { email: "", token: "", expiresAt: null, resendAt: 0, timer: null };

    // ================================================================ helpers

    function showAlert(text, tone = "info") {
        alertBox.textContent = text;
        alertBox.className = `reset-alert${tone === "error" ? " is-error" : tone === "success" ? " is-success" : ""}`;
        alertBox.setAttribute("role", tone === "error" ? "alert" : "status");
        alertBox.hidden = !text;
    }

    function fieldError(input, message) {
        const error = document.getElementById(`${input.id}-error`);
        if (error) error.textContent = message;
        if (message) input.setAttribute("aria-invalid", "true");
        else input.removeAttribute("aria-invalid");
    }

    function clearErrors(form) {
        form.querySelectorAll("input").forEach((input) => fieldError(input, ""));
        showAlert("");
    }

    function validate(form, extraErrors = {}) {
        let firstInvalid = null;
        form.querySelectorAll("input").forEach((input) => {
            let message = extraErrors[input.id] || "";
            const value = input.value.trim();
            if (!message && input.required && !value) message = "Fill in this field.";
            else if (!message && input.type === "email" && value && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value)) {
                message = "Enter a valid email address, like name@example.com.";
            }
            fieldError(input, message);
            if (message && !firstInvalid) firstInvalid = input;
        });
        firstInvalid?.focus();
        return !firstInvalid;
    }

    async function postJson(url, payload) {
        let response;
        try {
            response = await fetch(url, {
                method: "POST",
                credentials: "same-origin",
                headers: { "Content-Type": "application/json", Accept: "application/json" },
                body: JSON.stringify(payload),
            });
        } catch {
            throw new Error("We couldn't reach the server. Check your internet connection and try again.");
        }
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
            const detail = Array.isArray(result.detail)
                ? result.detail.map((item) => item.msg.replace(/^Value error, /, "")).join(" ")
                : result.detail;
            const error = new Error(typeof detail === "string" && detail ? detail : `Something went wrong (error ${response.status}). Please try again.`);
            error.status = response.status;
            throw error;
        }
        return result;
    }

    async function withBusy(button, busyText, task) {
        if (button.disabled) return;
        const label = button.textContent;
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
        button.textContent = busyText;
        try {
            await task();
        } finally {
            if (button.isConnected) {
                button.disabled = false;
                button.removeAttribute("aria-busy");
                button.textContent = label;
            }
        }
    }

    // ================================================================ steps

    function showStep(name) {
        const order = ["email", "code", "password"];
        steps.forEach((step) => { step.hidden = step.dataset.resetStep !== name; });
        progress.hidden = name === "done";
        backLink.hidden = name === "done";
        progress.querySelectorAll("li").forEach((item) => {
            const position = order.indexOf(item.dataset.step);
            const current = order.indexOf(name);
            item.classList.toggle("done", position < current);
            item.classList.toggle("current", position === current);
            if (position === current) item.setAttribute("aria-current", "step");
            else item.removeAttribute("aria-current");
        });
        const active = steps.find((step) => step.dataset.resetStep === name);
        (active.querySelector("input") || active.querySelector("h3") || active.querySelector("h1"))?.focus();
    }

    const formatSeconds = (seconds) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;

    function tick() {
        const now = Date.now();
        const expiresIn = state.expiresAt === null ? null : Math.max(0, Math.ceil((state.expiresAt - now) / 1000));
        const resendIn = Math.max(0, Math.ceil((state.resendAt - now) / 1000));
        if (expiresIn === null) {
            expiryLabel.textContent = "Enter the code from the most recent email. Codes last 5 minutes.";
            expiryLabel.classList.remove("expired");
        } else if (expiresIn > 0) {
            expiryLabel.innerHTML = "";
            expiryLabel.append("Code expires in ", Object.assign(document.createElement("strong"), { textContent: formatSeconds(expiresIn) }));
            expiryLabel.classList.remove("expired");
        } else {
            expiryLabel.textContent = "This code has expired. Request a new one.";
            expiryLabel.classList.add("expired");
        }
        resendButton.disabled = resendIn > 0;
        resendButton.textContent = resendIn > 0 ? `Resend code in ${resendIn}s` : "Resend code";
    }

    function startTimers(expiresIn, resendAfter) {
        state.expiresAt = expiresIn ? Date.now() + expiresIn * 1000 : null;
        state.resendAt = Date.now() + resendAfter * 1000;
        window.clearInterval(state.timer);
        tick();
        state.timer = window.setInterval(tick, 1000);
    }

    async function sendCode(email, button, busyText, emailField) {
        await withBusy(button, busyText, async () => {
            try {
                const result = await postJson("/api/admin/password-reset/request", { email });
                const keptExpiry = result.cooldown && email === state.email && state.expiresAt
                    ? Math.max(0, Math.ceil((state.expiresAt - Date.now()) / 1000))
                    : 0;
                state.email = email;
                document.querySelector("#reset-sent-to").textContent = email;
                codeInput.value = "";
                fieldError(codeInput, "");
                showStep("code");
                startTimers(result.cooldown ? keptExpiry : result.expires_in, result.resend_after || 60);
                showAlert(result.cooldown ? result.message : "Verification code sent. It can be used once and expires in 5 minutes.", result.cooldown ? "info" : "success");
            } catch (error) {
                if (error.status === 404 && emailField) {
                    // This address does not belong to the administrator account.
                    fieldError(emailField, error.message);
                    emailField.focus();
                    emailField.select();
                    return;
                }
                showAlert(error.status === 503
                    ? "Password reset emails are not available right now. Contact the shop owner."
                    : error.message, "error");
            }
        });
        if (button === resendButton) tick();
    }

    // ================================================================ step 1: email

    const emailInput = emailForm.elements.email;
    emailInput.addEventListener("input", () => fieldError(emailInput, ""));

    emailForm.addEventListener("submit", (event) => {
        event.preventDefault();
        clearErrors(emailForm);
        if (!validate(emailForm)) return;
        sendCode(emailInput.value.trim(), emailForm.querySelector('[type="submit"]'), "Sending code…", emailInput);
    });

    // ================================================================ step 2: code

    codeInput.addEventListener("input", () => {
        codeInput.value = codeInput.value.replace(/\D/g, "").slice(0, 6);
        fieldError(codeInput, "");
    });

    resendButton.addEventListener("click", () => sendCode(state.email, resendButton, "Sending…"));

    document.querySelector("#reset-change-email").addEventListener("click", () => {
        window.clearInterval(state.timer);
        showAlert("");
        showStep("email");
    });

    codeForm.addEventListener("submit", (event) => {
        event.preventDefault();
        showAlert("");
        if (!/^\d{6}$/.test(codeInput.value)) {
            fieldError(codeInput, "Enter the 6-digit code from the email.");
            codeInput.focus();
            return;
        }
        if (state.expiresAt !== null && Date.now() >= state.expiresAt) {
            fieldError(codeInput, "This code has expired. Use \"Resend code\" to get a new one.");
            return;
        }
        withBusy(codeForm.querySelector('[type="submit"]'), "Verifying…", async () => {
            try {
                const result = await postJson("/api/admin/password-reset/verify", { email: state.email, code: codeInput.value });
                state.token = result.reset_token;
                window.clearInterval(state.timer);
                showAlert("");
                showStep("password");
            } catch (error) {
                if (error.status === 429) {
                    state.expiresAt = 0;
                    tick();
                    showAlert(`${error.message} Use "Resend code" when it becomes available.`, "error");
                } else {
                    fieldError(codeInput, error.message);
                }
                codeInput.select();
            }
        });
    });

    // ================================================================ step 3: new password

    const rules = document.querySelector("#reset-rules");
    const passwordInput = passwordForm.elements.new_password;
    const confirmInput = passwordForm.elements.confirm_password;

    function updateRules() {
        const length = passwordInput.value.length >= PASSWORD_MIN_LENGTH;
        const match = passwordInput.value.length > 0 && passwordInput.value === confirmInput.value;
        rules.querySelector('[data-rule="length"]').classList.toggle("met", length);
        rules.querySelector('[data-rule="match"]').classList.toggle("met", match);
        return { length, match };
    }

    passwordInput.addEventListener("input", updateRules);
    confirmInput.addEventListener("input", updateRules);
    updateRules();

    passwordForm.addEventListener("submit", (event) => {
        event.preventDefault();
        showAlert("");
        fieldError(passwordInput, "");
        fieldError(confirmInput, "");
        let valid = true;
        if (passwordInput.value.length < PASSWORD_MIN_LENGTH) {
            fieldError(passwordInput, `Use at least ${PASSWORD_MIN_LENGTH} characters.`);
            passwordInput.focus();
            valid = false;
        } else if (passwordInput.value !== confirmInput.value) {
            fieldError(confirmInput, "The passwords do not match.");
            confirmInput.focus();
            valid = false;
        }
        if (!valid) return;
        withBusy(passwordForm.querySelector('[type="submit"]'), "Saving…", async () => {
            try {
                await postJson("/api/admin/password-reset/confirm", {
                    email: state.email,
                    reset_token: state.token,
                    new_password: passwordInput.value,
                });
                state.token = "";
                passwordForm.reset();
                updateRules();
                sessionStorage.setItem(RESET_EMAIL_KEY, state.email);
                showStep("done");
                redirectToSignIn();
            } catch (error) {
                if (/expired|start again/i.test(error.message)) {
                    showAlert("This reset session has expired. Enter your email to get a new code.", "error");
                    showStep("email");
                } else {
                    fieldError(passwordInput, error.message);
                    passwordInput.focus();
                }
            }
        });
    });

    function redirectToSignIn() {
        const note = document.querySelector("#reset-redirect-note");
        let seconds = 5;
        const update = () => { note.textContent = `Taking you to admin sign in in ${seconds} second${seconds === 1 ? "" : "s"}…`; };
        update();
        const timer = window.setInterval(() => {
            seconds -= 1;
            if (seconds <= 0) {
                window.clearInterval(timer);
                window.location.assign("/admin/login");
            } else {
                update();
            }
        }, 1000);
    }

    // ================================================================ password toggles

    document.querySelectorAll("[data-toggle-password]").forEach((button) => {
        button.addEventListener("click", () => {
            const input = document.getElementById(button.dataset.togglePassword);
            const show = input.type === "password";
            input.type = show ? "text" : "password";
            button.textContent = show ? "Hide" : "Show";
            button.setAttribute("aria-pressed", String(show));
            button.setAttribute("aria-label", show ? "Hide password" : "Show password");
            input.focus();
        });
    });

    // Prefill the address when the visitor came from the admin sign-in page.
    const loginEmail = sessionStorage.getItem(RESET_EMAIL_KEY);
    sessionStorage.removeItem(RESET_EMAIL_KEY);
    if (loginEmail) emailForm.elements.email.value = loginEmail;

    showStep("email");
})();
