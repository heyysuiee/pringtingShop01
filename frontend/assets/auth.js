"use strict";

/**
 * Customer authentication pages: Sign In, Create an Account, and Forgot Password.
 * Every form talks to the FastAPI endpoints under /api/customer/; nothing is simulated.
 *
 * A "next" query parameter (for example /sign-in?next=/order?service=souvenir) sends the customer
 * back to the page they came from after signing in. Only same-site paths are accepted.
 */
(() => {
    const page = document.body.dataset.authPage;
    const alertBox = document.querySelector("#auth-alert");
    const PASSWORD_MIN_LENGTH = 12;
    const SIGN_IN_EMAIL_KEY = "printshop.signInEmail";
    const SIGN_IN_NOTICE_KEY = "printshop.signInNotice";
    const RESET_EMAIL_KEY = "printshop.resetEmail";

    // ================================================================ helpers

    /** The page to open after signing in. Rejects other sites ("//evil.example", "https://…"). */
    function nextPath() {
        const value = new URLSearchParams(window.location.search).get("next") || "";
        if (value.startsWith("/") && !value.startsWith("//") && !value.startsWith("/\\")) return value;
        return "/customer-portal";
    }

    // Links marked data-keep-next carry the "next" destination between the auth pages.
    const next = new URLSearchParams(window.location.search).get("next");
    if (next) {
        document.querySelectorAll("a[data-keep-next]").forEach((link) => {
            const url = new URL(link.getAttribute("href"), window.location.origin);
            url.searchParams.set("next", nextPath());
            link.href = url.pathname + url.search;
        });
    }

    function showAlert(text, tone = "info") {
        if (!alertBox) return;
        alertBox.textContent = text;
        alertBox.className = `auth-alert${tone === "error" ? " auth-alert-error" : tone === "success" ? " auth-alert-success" : ""}`;
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
        form.querySelectorAll(".auth-input").forEach((input) => fieldError(input, ""));
        showAlert("");
    }

    /** Validate required fields and email format; show messages next to the fields. Returns true when valid. */
    function validate(form) {
        let firstInvalid = null;
        form.querySelectorAll(".auth-input").forEach((input) => {
            let message = "";
            const value = input.value.trim();
            const label = form.querySelector(`label[for="${input.id}"]`)?.childNodes[0]?.textContent.trim() || "This field";
            if (input.required && !value) message = `Enter your ${label.toLowerCase()}.`;
            else if (input.type === "email" && value && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value)) message = "Enter a valid email address, like name@example.com.";
            else if (input.type === "tel" && value && value.replace(/\D/g, "").length < 7) message = "Enter a contact number with at least 7 digits.";
            else if (input.minLength > 0 && value && input.value.length < input.minLength) message = `Use at least ${input.minLength} characters.`;
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
            const detail = Array.isArray(result.detail) ? result.detail.map((item) => item.msg.replace(/^Value error, /, "")).join(" ") : result.detail;
            const error = new Error(typeof detail === "string" && detail ? detail : `Something went wrong (error ${response.status}). Please try again.`);
            error.status = response.status;
            throw error;
        }
        return result;
    }

    /** Disable the button and show a spinner while the request runs (prevents double submits). */
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

    // Show/hide password buttons
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

    /** Live checklist for a new password and its confirmation. Returns a function that reports validity. */
    function passwordRules(passwordInput, confirmInput, list) {
        const update = () => {
            const length = passwordInput.value.length >= PASSWORD_MIN_LENGTH;
            const match = passwordInput.value.length > 0 && passwordInput.value === confirmInput.value;
            list.querySelector('[data-rule="length"]').classList.toggle("met", length);
            list.querySelector('[data-rule="match"]').classList.toggle("met", match);
            return { length, match };
        };
        passwordInput.addEventListener("input", update);
        confirmInput.addEventListener("input", update);
        update();
        return () => {
            const { length, match } = update();
            if (!length) fieldError(passwordInput, `Use at least ${PASSWORD_MIN_LENGTH} characters.`);
            else if (!match) fieldError(confirmInput, "The passwords do not match.");
            if (!length) passwordInput.focus();
            else if (!match) confirmInput.focus();
            return length && match;
        };
    }

    // ================================================================ Sign In

    function setupSignIn() {
        const form = document.querySelector("#sign-in-form");
        const email = form.elements.email;
        const password = form.elements.password;

        // Messages handed over from the reset or registration pages.
        const savedEmail = sessionStorage.getItem(SIGN_IN_EMAIL_KEY);
        const notice = sessionStorage.getItem(SIGN_IN_NOTICE_KEY);
        sessionStorage.removeItem(SIGN_IN_EMAIL_KEY);
        sessionStorage.removeItem(SIGN_IN_NOTICE_KEY);
        if (savedEmail) email.value = savedEmail;
        if (notice) showAlert(notice, "success");
        (email.value ? password : email).focus();

        // Carry the typed email address over to the Forgot Password page (kept out of the URL).
        document.querySelector('a[href^="/forgot-password"]')?.addEventListener("click", () => {
            if (email.value.trim()) sessionStorage.setItem(RESET_EMAIL_KEY, email.value.trim());
        });

        // Already signed in: go straight on.
        fetch("/api/customer/session", { credentials: "same-origin" })
            .then((response) => { if (response.ok) window.location.replace(nextPath()); })
            .catch(() => {});

        form.addEventListener("submit", (event) => {
            event.preventDefault();
            clearErrors(form);
            if (!validate(form)) return;
            const button = form.querySelector('[type="submit"]');
            withBusy(button, "Signing in…", async () => {
                try {
                    await postJson("/api/customer/session", {
                        email: email.value.trim(),
                        password: password.value,
                        remember: form.elements.remember.checked,
                    });
                    window.location.assign(nextPath());
                } catch (error) {
                    showAlert(error.message, "error");
                    if (error.status === 401) {
                        password.value = "";
                        password.focus();
                    }
                }
            });
        });
    }

    // ================================================================ Create an Account

    function setupCreateAccount() {
        const form = document.querySelector("#create-account-form");
        const checkPasswords = passwordRules(form.elements.password, form.elements.confirm_password, document.querySelector("#register-rules"));
        form.elements.name.focus();

        form.addEventListener("submit", (event) => {
            event.preventDefault();
            clearErrors(form);
            if (!validate(form) || !checkPasswords()) return;
            const button = form.querySelector('[type="submit"]');
            withBusy(button, "Creating account…", async () => {
                try {
                    await postJson("/api/customer/register", {
                        name: form.elements.name.value.trim(),
                        email: form.elements.email.value.trim(),
                        phone: form.elements.phone.value.trim(),
                        messenger: form.elements.messenger.value.trim(),
                        password: form.elements.password.value,
                    });
                    window.location.assign(nextPath());
                } catch (error) {
                    if (error.status === 409) {
                        fieldError(form.elements.email, "An account already exists for this email address.");
                        showAlert("This email address already has an account. Sign in, or use Forgot password on the Sign In page.", "error");
                        form.elements.email.focus();
                    } else {
                        showAlert(error.message, "error");
                    }
                }
            });
        });
    }

    // ================================================================ Forgot Password

    function setupForgotPassword() {
        const steps = [...document.querySelectorAll("[data-reset-step]")];
        const progress = document.querySelector("#reset-steps");
        const backLink = document.querySelector("#reset-back");
        const emailForm = document.querySelector("#reset-email-form");
        const codeForm = document.querySelector("#reset-code-form");
        const passwordForm = document.querySelector("#reset-password-form");
        const codeInput = document.querySelector("#reset-code");
        const resendButton = document.querySelector("#reset-resend");
        const expiryLabel = document.querySelector("#reset-expiry");
        const checkPasswords = passwordRules(passwordForm.elements.new_password, passwordForm.elements.confirm_password, document.querySelector("#reset-rules"));

        // Kept in memory only: the reset token is never written to storage or the URL.
        // expiresAt is null when the expiry is unknown (a cooldown answer for a code sent before this page loaded).
        const state = { email: "", token: "", expiresAt: null, resendAt: 0, timer: null };

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
            (active.querySelector(".auth-input") || active.querySelector("h1")).focus();
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

        async function sendCode(email, button, busyText) {
            await withBusy(button, busyText, async () => {
                try {
                    const result = await postJson("/api/customer/password-reset/request", { email });
                    // A cooldown answer means no new code: keep the known expiry of the earlier code for this
                    // address, or treat it as unknown (the server still checks it) if that code predates this page.
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
                    showAlert(error.status === 503
                        ? "We couldn't send the verification email right now. Please try again later or contact the shop."
                        : error.message, "error");
                }
            });
            // withBusy re-enables the button; the resend button must respect its cooldown instead.
            if (button === resendButton) tick();
        }

        // Step 1: email
        emailForm.addEventListener("submit", (event) => {
            event.preventDefault();
            clearErrors(emailForm);
            if (!validate(emailForm)) return;
            sendCode(emailForm.elements.email.value.trim(), emailForm.querySelector('[type="submit"]'), "Sending code…");
        });
        const loginEmail = sessionStorage.getItem(RESET_EMAIL_KEY);
        sessionStorage.removeItem(RESET_EMAIL_KEY);
        if (loginEmail) emailForm.elements.email.value = loginEmail;

        // Step 2: code
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
                fieldError(codeInput, "This code has expired. Use “Resend code” to get a new one.");
                return;
            }
            withBusy(codeForm.querySelector('[type="submit"]'), "Verifying…", async () => {
                try {
                    const result = await postJson("/api/customer/password-reset/verify", { email: state.email, code: codeInput.value });
                    state.token = result.reset_token;
                    window.clearInterval(state.timer);
                    document.querySelector("#reset-account-email").textContent = state.email;
                    showAlert("");
                    showStep("password");
                } catch (error) {
                    if (error.status === 429) {
                        // Too many wrong codes: the code was cancelled on the server.
                        state.expiresAt = 0;
                        tick();
                        showAlert(`${error.message} Use “Resend code” when it becomes available.`, "error");
                    } else {
                        fieldError(codeInput, error.message);
                    }
                    codeInput.select();
                }
            });
        });

        // Step 3: new password
        passwordForm.addEventListener("submit", (event) => {
            event.preventDefault();
            clearErrors(passwordForm);
            if (!checkPasswords()) return;
            withBusy(passwordForm.querySelector('[type="submit"]'), "Saving…", async () => {
                try {
                    await postJson("/api/customer/password-reset/confirm", {
                        email: state.email,
                        reset_token: state.token,
                        new_password: passwordForm.elements.new_password.value,
                    });
                    state.token = "";
                    passwordForm.reset();
                    showAlert("");
                    showStep("done");
                    // The Sign In page shows this message and fills in the email address.
                    sessionStorage.setItem(SIGN_IN_EMAIL_KEY, state.email);
                    sessionStorage.setItem(SIGN_IN_NOTICE_KEY, "Your password was changed. Sign in with your new password.");
                    redirectToSignIn();
                } catch (error) {
                    if (/expired|start again/i.test(error.message)) {
                        showAlert("This reset session has expired. Enter your email to get a new code.", "error");
                        showStep("email");
                    } else {
                        fieldError(passwordForm.elements.new_password, error.message);
                        passwordForm.elements.new_password.focus();
                    }
                }
            });
        });

        function redirectToSignIn() {
            const note = document.querySelector("#reset-redirect-note");
            const target = document.querySelector('[data-reset-step="done"] a').getAttribute("href");
            let seconds = 5;
            const update = () => { note.textContent = `Taking you to Sign In in ${seconds} second${seconds === 1 ? "" : "s"}…`; };
            update();
            const timer = window.setInterval(() => {
                seconds -= 1;
                if (seconds <= 0) {
                    window.clearInterval(timer);
                    window.location.assign(target);
                } else {
                    update();
                }
            }, 1000);
        }

        showStep("email");
    }

    if (page === "sign-in") setupSignIn();
    else if (page === "create-account") setupCreateAccount();
    else if (page === "forgot-password") setupForgotPassword();
})();
