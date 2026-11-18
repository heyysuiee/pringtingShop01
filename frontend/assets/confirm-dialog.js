"use strict";

/**
 * Themed confirmation dialog for the storefront and customer portal, replacing the browser's
 * plain window.confirm(). Resolves to true when confirmed and false when cancelled or dismissed.
 *
 *   if (!(await PrintConfirm("Cancel this request?", { title: "Cancel request", confirmLabel: "Cancel request", tone: "danger" }))) return;
 */
(() => {
    function printConfirm(message, { title = "Please confirm", confirmLabel = "Confirm", cancelLabel = "Go back", tone = "primary" } = {}) {
        if (typeof HTMLDialogElement !== "function") return Promise.resolve(window.confirm(message));
        return new Promise((resolve) => {
            const dialog = document.createElement("dialog");
            dialog.className = "theme-confirm";
            dialog.setAttribute("aria-labelledby", "theme-confirm-title");

            const heading = document.createElement("h2");
            heading.id = "theme-confirm-title";
            heading.textContent = title;
            const text = document.createElement("p");
            text.textContent = message;

            const cancel = document.createElement("button");
            cancel.type = "button";
            cancel.className = "btn btn-ghost-dark";
            cancel.textContent = cancelLabel;
            const confirm = document.createElement("button");
            confirm.type = "button";
            confirm.className = tone === "danger" ? "btn btn-danger" : "btn";
            confirm.textContent = confirmLabel;

            const actions = document.createElement("div");
            actions.className = "theme-confirm-actions";
            actions.append(cancel, confirm);
            dialog.append(heading, text, actions);

            let answer = false;
            cancel.addEventListener("click", () => dialog.close());
            confirm.addEventListener("click", () => {
                answer = true;
                dialog.close();
            });
            dialog.addEventListener("click", (event) => {
                if (event.target === dialog) dialog.close(); // backdrop
            });
            dialog.addEventListener("close", () => {
                dialog.remove();
                resolve(answer);
            });
            document.body.append(dialog);
            dialog.showModal();
            confirm.focus();
        });
    }

    window.PrintConfirm = printConfirm;
})();
