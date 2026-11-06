"use strict";

const loginForm = document.querySelector("#admin-login-form");
const passwordInput = document.querySelector("#password");
const passwordToggle = document.querySelector(".password-toggle");
const loginError = document.querySelector("#login-error");

passwordToggle.addEventListener("click", () => {
    const showPassword = passwordInput.type === "password";
    passwordInput.type = showPassword ? "text" : "password";
    passwordToggle.textContent = showPassword ? "Hide" : "Show";
    passwordToggle.setAttribute("aria-label", showPassword ? "Hide password" : "Show password");
    passwordToggle.setAttribute("aria-pressed", String(showPassword));
});

loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    loginError.hidden = true;

    if (!loginForm.reportValidity()) return;

    const formData = new FormData(loginForm);
    const email = String(formData.get("email") || "").trim();
    const password = String(formData.get("password") || "");
    if (!email || !password) {
        loginError.textContent = "Enter your admin username and password to continue.";
        loginError.hidden = false;
        return;
    }

    const submitButton = loginForm.querySelector('[type="submit"]');
    submitButton.disabled = true;
    try {
        const response = await fetch("/api/admin/session", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email, password }),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
            // FastAPI reports validation problems (such as an invalid email address) as a list of {msg} objects.
            const detail = Array.isArray(result.detail) ? result.detail.map((item) => item.msg).join(" ") : result.detail;
            throw new Error(detail || "Administrator sign in failed.");
        }
        window.location.assign("/admin/dashboard");
    } catch (error) {
        loginError.textContent = error instanceof Error ? error.message : "Administrator sign in failed.";
        loginError.hidden = false;
    } finally {
        submitButton.disabled = false;
    }
});
