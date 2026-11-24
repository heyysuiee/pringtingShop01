/* ==========================================================================
   ImPRESS Print Works — theme mode toggle.
   Sets data-theme on <html>, remembers the preference in localStorage, and
   injects the same accessible button into customer and admin pages.
   Loaded in <head> (without defer) so the saved theme is applied before paint.
   ========================================================================== */
(function () {
    "use strict";

    var STORAGE_KEY = "impress-theme";
    var root = document.documentElement;
    /* Live logos are crops of the preserved original artwork. The artwork keeps its
       black background in every theme, so there are no light/dark file variants. */
    var BRAND_LOGOS = {
        horizontal: "/assets/image/brand/logo-horizontal.jpg",
        compact: "/assets/image/brand/logo-horizontal.jpg",
        icon: "/assets/image/brand/logo-icon.jpg"
    };

    function readTheme() {
        try {
            return localStorage.getItem(STORAGE_KEY) === "dark" ? "dark" : "light";
        } catch (error) {
            return "light";
        }
    }

    function updateMeta(theme) {
        var meta = document.querySelector('meta[name="theme-color"]');
        if (meta) {
            meta.setAttribute("content", theme === "dark" ? "#08090D" : "#FAF8F4");
        }
    }

    function syncBrandLogos() {
        document.querySelectorAll("img[data-brand-logo]").forEach(function (image) {
            var next = BRAND_LOGOS[image.getAttribute("data-brand-logo")];
            if (!next) return;
            if (image.getAttribute("src") !== next) image.setAttribute("src", next);
        });
    }

    function syncButton(theme) {
        var button = document.querySelector("[data-theme-toggle]");
        if (!button) return;

        var isDark = theme === "dark";
        var action = isDark ? "Switch to light mode" : "Switch to dark mode";
        button.setAttribute("aria-pressed", isDark ? "true" : "false");
        button.setAttribute("aria-label", action);
        button.setAttribute("title", action);

        var label = button.querySelector(".theme-toggle-label");
        if (label) label.textContent = isDark ? "Dark" : "Light";
    }

    function applyTheme(theme, persist) {
        root.setAttribute("data-theme", theme);
        updateMeta(theme);
        syncButton(theme);
        syncBrandLogos();

        if (persist) {
            try {
                localStorage.setItem(STORAGE_KEY, theme);
            } catch (error) {
                /* Storage can be unavailable in private browsing; the theme still applies. */
            }
        }

        if (typeof window.CustomEvent === "function") {
            window.dispatchEvent(new CustomEvent("impress-theme-change", { detail: { theme: theme } }));
        }
    }

    function buildButton() {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "theme-toggle";
        button.setAttribute("data-theme-toggle", "");
        button.innerHTML =
            '<span class="theme-toggle-icon" aria-hidden="true">' +
                '<svg class="theme-icon theme-icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
                    '<circle cx="12" cy="12" r="4"></circle>' +
                    '<path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.65 17.65l1.42 1.42M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.65 6.35l1.42-1.42"></path>' +
                '</svg>' +
                '<svg class="theme-icon theme-icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
                    '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>' +
                '</svg>' +
            '</span>' +
            '<span class="theme-toggle-label">Light</span>';

        button.addEventListener("click", function () {
            applyTheme(root.getAttribute("data-theme") === "dark" ? "light" : "dark", true);
        });

        return button;
    }

    function mountButton() {
        if (document.querySelector("[data-theme-toggle]")) {
            syncButton(root.getAttribute("data-theme"));
            return;
        }

        var button = buildButton();
        var mount = document.querySelector(".topbar-actions");

        if (mount) {
            mount.insertBefore(button, mount.firstChild);
        } else {
            var nav = document.querySelector(".nav");
            if (nav) {
                /* Customer pages: group the toggle with the mobile menu button
                   in a right-aligned action area instead of beside the brand.
                   The portal already owns a .nav-actions area for its bell. */
                var actions = nav.querySelector(".nav-actions");
                if (!actions) {
                    actions = document.createElement("div");
                    actions.className = "nav-actions";
                    nav.appendChild(actions);
                }

                var menuToggle = nav.querySelector(".nav-toggle");
                if (menuToggle && menuToggle.parentElement !== actions) actions.appendChild(menuToggle);

                actions.appendChild(button);
            } else {
                var card = document.querySelector(".login-card") ||
                    document.querySelector(".auth-card") ||
                    document.body;
                card.insertBefore(button, card.firstChild);
            }
        }

        syncButton(root.getAttribute("data-theme"));
    }

    applyTheme(readTheme(), false);

    function mountAll() {
        mountButton();
        syncBrandLogos();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", mountAll);
    } else {
        mountAll();
    }

    window.addEventListener("storage", function (event) {
        if (event.key === STORAGE_KEY) {
            applyTheme(event.newValue === "dark" ? "dark" : "light", false);
        }
    });
})();
