"use strict";

(() => {
    const toggle = document.querySelector(".inner-page .nav-toggle");
    const menu = document.querySelector(".inner-page .nav-menu");
    if (!toggle || !menu) return;
    toggle.addEventListener("click", () => {
        const open = toggle.getAttribute("aria-expanded") !== "true";
        toggle.setAttribute("aria-expanded", String(open));
        toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
        menu.classList.toggle("open", open);
    });
    menu.addEventListener("click", (event) => {
        if (event.target.closest("a")) {
            menu.classList.remove("open");
            toggle.setAttribute("aria-expanded", "false");
            toggle.setAttribute("aria-label", "Open menu");
        }
    });
})();
