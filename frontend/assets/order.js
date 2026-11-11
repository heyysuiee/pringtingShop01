"use strict";

/**
 * Order wizard. The catalog, each product's questions, and all prices come from /api/catalog, so the
 * form always reflects what the administrator configured. Price estimates are calculated by the server
 * (/api/catalog/quote) with the same rules it applies when the order is saved.
 *
 * Invitation products use a six-step flow: Product → Ordering Option → Configure → Event & Files →
 * Contact → Review. Packages come from /api/catalog/products/{id}/packages and are priced from the
 * package's published price and add-on charge. Every other product keeps the original four-step flow.
 */
(() => {
    const DynamicForm = window.PrintShopDynamicForm;
    const form = document.querySelector("#order-form");
    if (!form || !DynamicForm) return;

    const $ = (selector, root = document) => root.querySelector(selector);
    const steps = [...form.querySelectorAll(".wizard-step")];
    const categorySelect = $("#category");
    const productPicker = $("#product-picker");
    const selectedProduct = $("#selected-product");
    const pricingNote = $("#pricing-note");
    const orderError = $("#order-error");
    const summary = $("#summary");
    const eventToggle = $("#event-toggle");
    const hasEvent = $("#has-event");
    const eventFields = $("#event-fields");
    const stepper = $("#stepper");

    const invitationOption = $("#invitation-option");
    const generalCustomize = $("#general-customize");
    const packageSection = $("#package-section");
    const customizeSection = $("#customize-section");
    const packageList = $("#package-list");
    const packageError = $("#package-error");
    const modeError = $("#order-mode-error");
    const configureLead = $("#configure-lead");
    const selectedPackageSummary = $("#selected-package-summary");

    const generalFormContainer = $("#general-dynamic-fields");
    const basicsContainer = $("#invitation-basics-fields");
    const designContainer = $("#invitation-design-fields");
    const eventContainer = $("#invitation-event-fields");
    const uploadContainer = $("#invitation-upload-fields");

    const generalQuantity = $("#quantity");
    const invitationQuantity = $("#invitation-quantity");
    const quantityHint = $("#quantity-hint");
    const invitationQuantityHint = $("#invitation-quantity-hint");
    const notesInput = $("#notes");
    const invitationNotes = $("#invitation-notes");
    const uploadInput = $("#artwork");
    const uploadPreviews = $("#upload-previews");

    const generalEstimate = {
        total: $("#estimate-total"),
        status: $("#estimate-status"),
        lines: $("#estimate-lines"),
    };
    const invitationEstimate = {
        total: $("#invitation-estimate-total"),
        status: $("#invitation-estimate-status"),
        lines: $("#invitation-estimate-lines"),
    };

    const MAX_REFERENCE_FILES = 3;
    const MAX_ORDER_FILES = 15;
    const money = DynamicForm.money;
    const parameters = new URLSearchParams(window.location.search);

    const SEQUENCES = {
        general: [
            { step: 1, label: "Product" },
            { step: 2, label: "Customize" },
            { step: 5, label: "Contact" },
            { step: 6, label: "Review" },
        ],
        invitation: [
            { step: 1, label: "Product" },
            { step: 2, label: "Ordering Option" },
            { step: 3, label: "Configure" },
            { step: 4, label: "Event & Files" },
            { step: 5, label: "Contact" },
            { step: 6, label: "Review" },
        ],
    };

    const state = {
        categories: [],
        product: null,
        invitation: false,
        mode: "", // "package" | "customize" for invitation products
        packages: [],
        packagesLoading: false,
        package: null,
        forms: {}, // general, or basics/design/event/upload for invitations
        referenceFiles: [],
        quote: null,
        quoteTimer: 0,
        quoteSequence: 0,
        sequence: SEQUENCES.general,
        activeStep: 1,
        idempotencyKey: newIdempotencyKey(),
    };

    function newIdempotencyKey() {
        if (typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
        return [...window.crypto.getRandomValues(new Uint8Array(16))].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    }

    function el(tag, className, text) {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (text !== undefined) element.textContent = text;
        return element;
    }

    function setError(message) {
        orderError.textContent = message;
        orderError.hidden = !message;
        if (message) orderError.scrollIntoView({ behavior: "smooth", block: "center" });
    }

    async function requestJson(url, options = {}) {
        const response = await fetch(url, { credentials: "same-origin", ...options });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
            const detail = Array.isArray(result.detail) ? result.detail.map((item) => item.msg).join(" ") : result.detail;
            throw new Error(detail || `The request failed (HTTP ${response.status}).`);
        }
        return result;
    }

    /** "From ₱12.00 each", "₱20.00 per sq ft", or "Request a quote" — only administrator prices are shown. */
    function priceSummary(product) {
        if (product.pricing_mode === "quote" || product.base_price === null) return "Request a quote";
        return product.pricing_mode === "area"
            ? `${money(product.base_price)} per sq ft`
            : `From ${money(product.base_price)} each`;
    }

    function productImage(product, className) {
        if (product.image) {
            const image = el("img", className);
            image.src = product.image;
            image.alt = `Sample image for ${product.name}`;
            image.loading = "lazy";
            return image;
        }
        const placeholder = el("span", `${className} product-image-placeholder`, product.name.slice(0, 1));
        placeholder.setAttribute("aria-hidden", "true");
        return placeholder;
    }

    function isInvitationProduct(product) {
        return Boolean(product && (product.fields || []).some((field) => field.section === "event"));
    }

    // ---------------------------------------------------------------- step 1: category & product

    function currentCategory() {
        return state.categories.find((category) => category.id === categorySelect.value) || null;
    }

    function populateCategories() {
        categorySelect.replaceChildren(...state.categories.map((category) => new Option(category.label, category.id)));
        const requested = state.categories.find((category) => category.id === parameters.get("service"))
            || state.categories.find((category) => category.products.some((product) => product.id === parameters.get("design")));
        categorySelect.value = (requested || state.categories[0])?.id || "";
        populateProducts(parameters.get("design"));
    }

    function populateProducts(preferredId) {
        const category = currentCategory();
        productPicker.replaceChildren();
        if (!category) return;
        category.products.forEach((product) => {
            const option = el("label", "product-option");
            const input = el("input");
            input.type = "radio";
            input.name = "product";
            input.value = product.id;
            input.required = true;
            input.addEventListener("change", () => selectProduct(product.id));
            const body = el("span", "product-option-body");
            body.append(productImage(product, "product-option-image"), el("strong", "", product.name), el("small", "", priceSummary(product)));
            option.append(input, body);
            productPicker.append(option);
        });
        const preferred = category.products.find((product) => product.id === preferredId) || category.products[0];
        if (preferred) {
            productPicker.querySelector(`input[value="${CSS.escape(preferred.id)}"]`).checked = true;
            selectProduct(preferred.id);
        }
    }

    // ---------------------------------------------------------------- form construction

    function destroyForms() {
        for (const dynamicForm of Object.values(state.forms)) dynamicForm?.destroy();
        state.forms = {};
    }

    function onFormChange() {
        if (state.invitation) {
            if (state.mode === "package") renderPackageEstimate();
            else if (state.mode === "customize") scheduleQuote();
        } else {
            scheduleQuote();
        }
        if (state.activeStep === 6) renderSummary();
    }

    function createForms(product) {
        destroyForms();
        if (state.invitation) {
            state.forms = {
                basics: DynamicForm.create(basicsContainer, product, { includeSections: [""], onChange: onFormChange, idPrefix: "inv-basics" }),
                design: DynamicForm.create(designContainer, product, { includeSections: ["design"], onChange: onFormChange, idPrefix: "inv-design" }),
                event: DynamicForm.create(eventContainer, product, { includeSections: ["event"], onChange: onFormChange, idPrefix: "inv-event" }),
                upload: DynamicForm.create(uploadContainer, product, { includeSections: ["upload"], onChange: onFormChange, idPrefix: "inv-upload" }),
            };
        } else {
            state.forms = {
                general: DynamicForm.create(generalFormContainer, product, { onChange: onFormChange, idPrefix: "opt" }),
            };
        }
    }

    function activeForms() {
        if (!state.invitation) return state.forms.general ? [state.forms.general] : [];
        if (state.mode === "package") return [state.forms.event, state.forms.upload].filter(Boolean);
        return [state.forms.basics, state.forms.design, state.forms.event, state.forms.upload].filter(Boolean);
    }

    function collectValues() {
        const result = {};
        for (const dynamicForm of activeForms()) Object.assign(result, dynamicForm.values());
        return result;
    }

    function fileCount() {
        return activeForms().reduce((total, dynamicForm) => total + dynamicForm.fileCount(), 0)
            + (state.invitation ? 0 : state.referenceFiles.length);
    }

    // ---------------------------------------------------------------- step 2 + configure: product selection

    function selectProduct(productId) {
        const product = currentCategory()?.products.find((item) => item.id === productId);
        if (!product || product === state.product) return;
        state.product = product;
        state.invitation = isInvitationProduct(product);
        state.mode = "";
        state.packages = [];
        state.packagesLoading = false;
        state.package = null;
        state.quote = null;
        state.quoteSequence += 1;
        window.clearTimeout(state.quoteTimer);
        packageError.textContent = "";
        modeError.textContent = "";
        $("#product-error").textContent = "";
        setError("");

        selectedProduct.hidden = false;
        selectedProduct.replaceChildren();
        const copy = el("div", "selected-product-copy");
        copy.append(
            el("span", "order-step-kicker", product.category_label),
            el("h3", "", product.name),
            el("p", "", product.description || ""),
            el("strong", "product-price-note", priceSummary(product)),
        );
        selectedProduct.append(productImage(product, "selected-product-image"), copy);

        const note = pricingNote.querySelector("div");
        if (state.invitation) {
            note.replaceChildren(
                el("strong", "", "Packages or a custom quotation"),
                el("p", "", "Choose a published package with a fixed price, or customize your invitation and let the administrator send a quotation. The final amount is always confirmed before payment."),
            );
        } else {
            const priced = product.pricing_mode !== "quote" && product.base_price !== null;
            note.replaceChildren(
                el("strong", "", priced ? "Prices set by the shop" : "Request a quote"),
                el("p", "", priced
                    ? "You will see a live estimate as you choose options. The administrator confirms the final amount before payment is requested."
                    : "This product does not have a published price yet. Submit your options and the administrator will send a quotation."),
            );
        }

        $("#step2-product").textContent = product.name;
        $("#step2-general-product").textContent = product.name;
        invitationOption.hidden = !state.invitation;
        generalCustomize.hidden = state.invitation;
        packageSection.hidden = true;
        customizeSection.hidden = true;
        selectedPackageSummary.hidden = true;
        selectedPackageSummary.replaceChildren();
        configureLead.textContent = "";

        createForms(product);
        updateModeButtons();
        if (state.invitation) {
            state.referenceFiles = [];
            renderReferenceFiles();
            configureQuantity({ reset: true });
            loadPackages(product.id);
        } else {
            configureQuantity({ reset: true });
        }
        configureLegacyEvent();
        setSequence(state.invitation ? "invitation" : "general");
        if (state.invitation) renderPackageEstimate();
        else scheduleQuote();

        // A later step from the previous product may not exist in the new sequence.
        if (!state.sequence.some((item) => item.step === state.activeStep)) showStep(1, false);
        else syncNextButton();

        const url = new URL(window.location.href);
        url.searchParams.set("service", product.category_id);
        url.searchParams.set("design", product.id);
        window.history.replaceState(null, "", url);
    }

    // ---------------------------------------------------------------- quantity

    function quantityField() {
        return state.invitation ? invitationQuantity : generalQuantity;
    }

    function quantityHintElement() {
        return state.invitation ? invitationQuantityHint : quantityHint;
    }

    function quantityValue() {
        const value = Number(quantityField().value);
        return Number.isInteger(value) ? value : 0;
    }

    function configureQuantity({ reset = false } = {}) {
        const input = quantityField();
        const product = state.product;
        if (!product) return;
        const hint = quantityHintElement();
        input.disabled = false;
        let min = product.min_quantity;
        let max = product.max_quantity;
        let preferred;

        if (state.invitation && state.mode === "package" && state.package) {
            const pkg = state.package;
            if (pkg.add_on_price === null) {
                min = pkg.included_quantity;
                max = pkg.included_quantity;
                preferred = min;
                input.disabled = true;
                hint.textContent = `This package is ordered at exactly ${min.toLocaleString()} pieces.`;
            } else {
                min = Math.max(product.min_quantity, pkg.included_quantity);
                preferred = min;
                hint.textContent = `Includes ${pkg.included_quantity.toLocaleString()} pcs. Each additional piece is ${money(pkg.add_on_price)}${pkg.add_on_label ? ` (${pkg.add_on_label.toLowerCase()})` : ""}.`;
            }
        } else if (state.invitation && state.mode === "package") {
            hint.textContent = "Select a package to see its included quantity.";
        } else if (state.invitation) {
            hint.textContent = `Minimum order: ${product.min_quantity.toLocaleString()} pieces · maximum ${product.max_quantity.toLocaleString()}. The shop confirms the final quotation.`;
        } else {
            hint.textContent = product.min_quantity > 1
                ? `Minimum order: ${product.min_quantity.toLocaleString()} pieces · maximum ${product.max_quantity.toLocaleString()}.`
                : `Up to ${product.max_quantity.toLocaleString()} pieces.`;
        }

        input.min = String(min);
        input.max = String(max);
        const current = Number(input.value);
        if (reset) {
            input.value = String(preferred !== undefined ? preferred : min);
        } else if (preferred !== undefined) {
            input.value = String(preferred);
        } else if (!Number.isInteger(current) || current < min || current > max) {
            input.value = String(min);
        }

        const field = input.closest(".quantity-field");
        field?.querySelectorAll(".quantity-step").forEach((button) => {
            button.disabled = input.disabled;
        });
    }

    function onQuantityChange() {
        if (state.invitation) {
            if (state.mode === "package") renderPackageEstimate();
            else if (state.mode === "customize") scheduleQuote();
        } else {
            scheduleQuote();
        }
        if (state.activeStep === 6) renderSummary();
    }

    // ---------------------------------------------------------------- invitation ordering option

    function updateModeButtons() {
        document.querySelectorAll("[data-order-mode]").forEach((button) => {
            const active = button.dataset.orderMode === state.mode;
            button.classList.toggle("selected", active);
            button.setAttribute("aria-pressed", String(active));
        });
    }

    function syncPackageAvailability() {
        const button = $('[data-order-mode="package"]');
        if (!button) return;
        const unavailable = !state.invitation || state.packagesLoading || !state.packages.length;
        button.disabled = Boolean(unavailable);
        button.setAttribute("aria-disabled", String(Boolean(unavailable)));
        if (unavailable && state.mode === "package") setMode("customize", { silent: true });
    }

    /** The step-2 Next button stays locked while a package is required but not chosen. */
    function syncNextButton() {
        const next = $("#btn-next");
        if (!next || !state.product) return;
        const needsPackage = state.invitation && state.activeStep === 2
            && state.mode === "package" && !state.package;
        next.disabled = needsPackage;
        next.title = needsPackage ? "Select a package to continue." : "";
    }

    function setMode(mode, { silent = false } = {}) {
        state.mode = mode;
        if (mode !== "package") state.package = null;
        if (!silent) {
            modeError.textContent = "";
            if (mode === "package") packageError.textContent = "";
        }
        updateModeButtons();
        packageSection.hidden = mode !== "package";
        customizeSection.hidden = mode !== "customize";
        configureLead.textContent = mode === "package"
            ? (state.package
                ? "Set the total pieces you need. The package price is fixed; any pieces beyond the included quantity use the set's add-on rate."
                : "Choose a package above to open the package order form.")
            : "Set the size, paper, quantity, and design direction you want. Required fields are marked; the shop will send a quotation before payment.";
        renderSelectedPackageSummary();

        if (mode === "package") {
            state.quote = null;
            configureQuantity({ reset: true });
            renderPackageEstimate();
        } else {
            configureQuantity({ reset: true });
            scheduleQuote();
        }
        syncNextButton();
        if (state.activeStep === 6) renderSummary();
    }

    function renderSelectedPackageSummary() {
        if (!selectedPackageSummary) return;
        const pkg = state.package;
        const visible = state.invitation && state.mode === "package" && Boolean(pkg);
        selectedPackageSummary.hidden = !visible;
        selectedPackageSummary.replaceChildren();
        if (!visible) return;

        const head = el("div", "selected-package-head");
        head.append(
            el("span", "order-step-kicker", "Selected package"),
            el("strong", "selected-package-name", pkg.name),
        );
        const meta = [money(pkg.price), pkg.size, pkg.material].filter(Boolean).join(" · ");
        selectedPackageSummary.append(head, el("p", "selected-package-meta", meta));
        if (pkg.inclusions?.length) {
            const list = el("ul", "package-inclusions");
            pkg.inclusions.forEach((item) => list.append(el("li", "", item)));
            selectedPackageSummary.append(list);
        }
        const change = el("button", "btn btn-ghost-dark btn-small package-change", "Change package");
        change.type = "button";
        change.addEventListener("click", () => {
            showStep(2);
            packageSection.scrollIntoView({ behavior: "smooth", block: "center" });
            packageList.querySelector(".package-card.selected .package-select")?.focus();
        });
        selectedPackageSummary.append(change);
    }

    async function loadPackages(productId) {
        state.packagesLoading = true;
        state.packages = [];
        updateModeButtons();
        syncPackageAvailability();
        renderPackages();
        let failed = "";
        try {
            const result = await requestJson(`/api/catalog/products/${encodeURIComponent(productId)}/packages`, { cache: "no-store" });
            if (state.product?.id !== productId) return;
            state.packages = Array.isArray(result.packages) ? result.packages : [];
        } catch (error) {
            if (state.product?.id !== productId) return;
            failed = error instanceof Error ? error.message : "The packages could not be loaded.";
        }
        if (state.product?.id !== productId) return;
        state.packagesLoading = false;
        if (failed) packageError.textContent = `${failed} You can still customize your invitation.`;
        renderPackages();
        syncPackageAvailability();
        if (state.mode === "package") {
            configureQuantity({ reset: true });
            renderPackageEstimate();
        }
    }

    function renderPackages() {
        packageList.replaceChildren();
        if (state.packagesLoading) {
            packageList.append(el("p", "package-empty", "Loading packages…"));
            return;
        }
        if (!state.packages.length) {
            packageList.append(el("p", "package-empty", "No packages are available for this invitation right now. Choose “Customize Your Own Invitation” to continue."));
            return;
        }
        for (const pkg of state.packages) {
            const card = el("article", "package-card");
            if (state.package?.id === pkg.id) card.classList.add("selected");
            const media = pkg.image
                ? productImage({ ...pkg, image: pkg.image, name: pkg.name }, "package-card-image")
                : el("span", "package-card-image product-image-placeholder", pkg.name.slice(0, 1));
            const body = el("div", "package-card-body");
            body.append(el("h3", "package-name", pkg.name));
            body.append(el("p", "package-price", money(pkg.price)));

            const meta = [];
            if (pkg.size) meta.push(pkg.size);
            if (pkg.material) meta.push(pkg.material);
            if (pkg.included_quantity) meta.push(`${pkg.included_quantity.toLocaleString()} pcs included`);
            if (meta.length) body.append(el("p", "package-meta", meta.join(" · ")));

            if (pkg.inclusions?.length) {
                const list = el("ul", "package-inclusions");
                pkg.inclusions.forEach((item) => list.append(el("li", "", item)));
                body.append(list);
            }
            body.append(el(
                "p",
                "package-addon",
                pkg.add_on_price === null
                    ? `Ordered at exactly ${pkg.included_quantity.toLocaleString()} pieces.`
                    : `${pkg.add_on_label || "Additional pieces"}: ${money(pkg.add_on_price)} per additional piece.`,
            ));

            const select = el("button", "btn btn-dark package-select", state.package?.id === pkg.id ? "Selected" : "Select Package");
            select.type = "button";
            select.setAttribute("aria-pressed", String(state.package?.id === pkg.id));
            select.addEventListener("click", () => selectPackage(pkg));
            body.append(select);
            card.append(media, body);
            packageList.append(card);
        }
    }

    function selectPackage(pkg) {
        state.package = pkg;
        packageError.textContent = "";
        renderPackages();
        configureQuantity({ reset: true });
        renderPackageEstimate();
        renderSelectedPackageSummary();
        syncNextButton();
        if (state.activeStep === 6) renderSummary();
    }

    function packageTotal(pkg, quantity) {
        if (!pkg) return null;
        const extra = pkg.add_on_price === null ? 0 : Math.max(0, quantity - pkg.included_quantity);
        return Math.round((pkg.price + extra * (pkg.add_on_price || 0)) * 100) / 100;
    }

    function renderPackageEstimate() {
        if (!state.invitation) return;
        const pkg = state.package;
        const quantity = quantityValue();
        invitationEstimate.total.textContent = pkg ? money(packageTotal(pkg, quantity)) : "—";
        invitationEstimate.lines.replaceChildren();
        if (!pkg) {
            invitationEstimate.status.textContent = "Select a package to see its total.";
            return;
        }
        invitationEstimate.status.textContent = pkg.add_on_price === null
            ? `This set is ordered at exactly ${pkg.included_quantity.toLocaleString()} pieces.`
            : `Includes ${pkg.included_quantity.toLocaleString()} pcs · ${money(pkg.add_on_price)} per additional piece.`;

        const base = el("li");
        const baseLabel = el("span");
        baseLabel.append(el("strong", "", pkg.name), el("small", "", `${pkg.included_quantity.toLocaleString()} pcs included`));
        base.append(baseLabel, el("span", "estimate-amount", money(pkg.price)));
        invitationEstimate.lines.append(base);

        const extra = pkg.add_on_price === null ? 0 : Math.max(0, quantity - pkg.included_quantity);
        if (extra > 0) {
            const line = el("li");
            const label = el("span");
            label.append(el("strong", "", pkg.add_on_label || "Additional pieces"), el("small", "", `${extra.toLocaleString()} additional pcs × ${money(pkg.add_on_price)}`));
            line.append(label, el("span", "estimate-amount", money(extra * pkg.add_on_price)));
            invitationEstimate.lines.append(line);
        }
        const total = el("li");
        total.append(el("strong", "", "Package total"), el("strong", "estimate-amount", money(packageTotal(pkg, quantity))));
        invitationEstimate.lines.append(total);
    }

    // ---------------------------------------------------------------- step 5: legacy event details

    function configureLegacyEvent() {
        // Invitation event details are catalog fields in the "event" section (step 4).
        const mode = state.invitation ? "none" : (state.product?.event_mode || "none");
        eventToggle.hidden = mode !== "optional";
        if (mode !== "optional") hasEvent.checked = false;
        const show = mode === "required" || (mode === "optional" && hasEvent.checked);
        eventFields.hidden = !show;
        $("#event-hint").textContent = mode === "required"
            ? `Event details are required for a ${state.product.name}.`
            : "Share the details that should appear on, or guide, your design.";
        for (const id of ["celebrant", "event-type", "event-date"]) {
            const input = $(`#${id}`);
            input.required = show;
            input.disabled = !show;
        }
        for (const id of ["event-time", "event-location"]) $(`#${id}`).disabled = !show;
        $("#event-date").min = new Date().toISOString().slice(0, 10);
    }

    function eventData() {
        return {
            celebrant: $("#celebrant").value.trim(),
            event_type: $("#event-type").value.trim(),
            event_date: $("#event-date").value.trim(),
            event_time: $("#event-time").value.trim(),
            event_location: $("#event-location").value.trim(),
        };
    }

    // ---------------------------------------------------------------- live estimate (server-calculated)

    function estimateReference() {
        return state.invitation ? invitationEstimate : generalEstimate;
    }

    function scheduleQuote() {
        window.clearTimeout(state.quoteTimer);
        if (!state.product) return;
        if (state.invitation && state.mode !== "customize") return;
        estimateReference().status.textContent = "Updating estimate…";
        state.quoteTimer = window.setTimeout(refreshQuote, 250);
    }

    async function refreshQuote() {
        if (!state.product) return;
        if (state.invitation && state.mode !== "customize") return;
        const sequence = ++state.quoteSequence;
        try {
            const quote = await requestJson("/api/catalog/quote", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    product_id: state.product.id,
                    quantity: quantityValue() || state.product.min_quantity,
                    options: collectValues(),
                }),
            });
            if (sequence !== state.quoteSequence) return; // a newer request is in flight
            state.quote = quote;
        } catch (error) {
            if (sequence !== state.quoteSequence) return;
            state.quote = null;
            estimateReference().status.textContent = error instanceof Error ? error.message : "The estimate could not be calculated.";
            return;
        }
        renderEstimate();
        if (state.activeStep === 6) renderSummary();
    }

    function estimateText(quote) {
        if (!quote) return "—";
        if (quote.estimated_total !== null) return money(quote.estimated_total);
        return quote.requires_quote ? "Quotation" : "—";
    }

    function renderEstimate() {
        if (state.invitation && state.mode !== "customize") return;
        const reference = estimateReference();
        const quote = state.quote;
        reference.total.textContent = estimateText(quote);
        reference.lines.replaceChildren();
        if (!quote) return;
        let status;
        if (quote.requires_quote) status = quote.quote_reasons.join(" ");
        else if (quote.incomplete.length) status = quote.incomplete.join(" ");
        else status = quote.unit_price !== null
            ? `${money(quote.unit_price)} per piece × ${quote.quantity.toLocaleString()} · estimate, confirmed by the shop`
            : "";
        reference.status.textContent = status;
        if (quote.requires_quote) return;
        quote.lines.forEach((line) => {
            const item = el("li");
            const label = el("span");
            label.append(el("strong", "", line.label), el("small", "", line.detail));
            item.append(label, el("span", "estimate-amount", quote.estimated_total === null && quote.pricing_mode === "area" ? "—" : money(line.amount)));
            reference.lines.append(item);
        });
    }

    // ---------------------------------------------------------------- general reference files (non-invitation)

    function formatBytes(bytes) {
        return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    }

    function renderReferenceFiles() {
        uploadPreviews.replaceChildren();
        state.referenceFiles.forEach((file, index) => {
            const item = el("article", "upload-preview");
            if (file.type.startsWith("image/")) {
                const image = el("img");
                image.src = URL.createObjectURL(file);
                image.alt = `Preview of ${file.name}`;
                image.addEventListener("load", () => URL.revokeObjectURL(image.src), { once: true });
                item.append(image);
            } else {
                item.append(el("span", "pdf-preview", "PDF"));
            }
            const info = el("span", "upload-preview-info");
            info.append(el("strong", "", file.name), el("small", "", formatBytes(file.size)));
            const remove = el("button", "text-button", "Remove");
            remove.type = "button";
            remove.setAttribute("aria-label", `Remove ${file.name}`);
            remove.addEventListener("click", () => {
                state.referenceFiles.splice(index, 1);
                renderReferenceFiles();
            });
            item.append(info, remove);
            uploadPreviews.append(item);
        });
    }

    uploadInput.addEventListener("change", () => {
        setError("");
        const chosen = [...uploadInput.files];
        uploadInput.value = "";
        const allowed = new Set(["image/jpeg", "image/png", "image/webp", "application/pdf"]);
        const invalid = chosen.find((file) => !allowed.has(file.type) || !/\.(jpe?g|png|webp|pdf)$/i.test(file.name));
        if (invalid) return setError(`${invalid.name}: choose a JPG, JPEG, PNG, WebP, or PDF file.`);
        const oversized = chosen.find((file) => file.size > 5 * 1024 * 1024);
        if (oversized) return setError(`${oversized.name}: each file must be no larger than 5 MB.`);
        if (state.referenceFiles.length + chosen.length > MAX_REFERENCE_FILES) return setError(`Attach up to ${MAX_REFERENCE_FILES} reference files.`);
        state.referenceFiles.push(...chosen);
        renderReferenceFiles();
    });

    // ---------------------------------------------------------------- review summary

    function appendGroup(title, rows) {
        const filled = rows.filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== "");
        if (!filled.length) return;
        const section = el("section", "summary-group");
        section.append(el("h3", "summary-group-title", title));
        const list = el("dl", "summary order-summary");
        filled.forEach(([label, value]) => {
            const row = el("div", "summary-row");
            row.append(el("dt", "", label), el("dd", "", String(value)));
            list.append(row);
        });
        section.append(list);
        summary.append(section);
    }

    function activeNotes() {
        return state.invitation ? invitationNotes : notesInput;
    }

    function renderSummary() {
        summary.replaceChildren();
        const product = state.product;
        if (!product) return;
        const quantity = quantityValue();
        const kind = state.invitation
            ? (state.mode === "package" ? "Ready-made package" : "Customized invitation")
            : "Customized order";
        appendGroup("Product", [
            ["Category", product.category_label],
            ["Product", product.name],
            ["Ordering option", kind],
            ["Quantity", quantity.toLocaleString()],
        ]);

        if (state.invitation && state.mode === "package" && state.package) {
            const pkg = state.package;
            appendGroup("Package", [
                ["Package", pkg.name],
                ["Package price", money(pkg.price)],
                ["Size", pkg.size],
                ["Material", pkg.material],
                ["Inclusions", pkg.inclusions.join("; ")],
                ["Included quantity", `${pkg.included_quantity.toLocaleString()} pcs`],
                ["Add-on", pkg.add_on_price === null
                    ? "Not available — this set has a fixed quantity"
                    : `${pkg.add_on_label || "Additional pieces"}: ${money(pkg.add_on_price)} each`],
            ]);
        }

        if (state.invitation && state.mode === "customize") {
            appendGroup("Basic specifications", (state.forms.basics?.summary() || []).map((row) => [row.label, row.value]));
            appendGroup("Design customization", (state.forms.design?.summary() || []).map((row) => [row.label, row.value]));
        } else if (!state.invitation) {
            const specs = state.forms.general?.summary() || [];
            if (state.quote?.area_sqft) specs.push({ label: "Print area", value: `${state.quote.area_sqft} sq ft per piece` });
            appendGroup("Your options", specs.map((row) => [row.label, row.value]));
            if (!eventFields.hidden) {
                const event = eventData();
                appendGroup("Event details", [
                    ["Celebrant / honoree", event.celebrant],
                    ["Event type", event.event_type],
                    ["Event date", event.event_date],
                    ["Event time", event.event_time],
                    ["Venue", event.event_location],
                ]);
            }
        }

        if (state.invitation) {
            appendGroup("Event details", (state.forms.event?.summary() || []).map((row) => [row.label, row.value]));
            appendGroup("Photos & references", (state.forms.upload?.summary() || []).map((row) => [row.label, row.value]));
        }

        appendGroup("Special instructions", [["Notes", activeNotes().value.trim()]]);
        appendGroup("Contact", [
            ["Name", $("#name").value.trim()],
            ["Email", $("#email").value.trim()],
            ["Contact number", $("#phone").value.trim()],
            ["Messenger", $("#messenger").value.trim()],
        ]);
        if (!state.invitation) {
            appendGroup("Other reference files", [["Files", state.referenceFiles.map((file) => file.name).join(", ") || "None attached"]]);
        }

        const pricing = [];
        if (state.invitation && state.mode === "package") {
            const pkg = state.package;
            if (!pkg) {
                pricing.push(["Estimated total", "Select a package"]);
            } else {
                const extra = pkg.add_on_price === null ? 0 : Math.max(0, quantity - pkg.included_quantity);
                pricing.push([pkg.name, `${money(pkg.price)} · ${pkg.included_quantity.toLocaleString()} pcs included`]);
                if (extra > 0) {
                    pricing.push([
                        pkg.add_on_label || "Additional pieces",
                        `${extra.toLocaleString()} pcs × ${money(pkg.add_on_price)} = ${money(extra * pkg.add_on_price)}`,
                    ]);
                }
                const total = packageTotal(pkg, quantity);
                pricing.push(["Estimated total", money(total)]);
                pricing.push(["50% down payment (estimate)", money(total * 0.5)]);
            }
        } else {
            const quote = state.quote;
            if (!quote) {
                pricing.push(["Estimate", "Calculating…"]);
            } else if (quote.requires_quote) {
                pricing.push(["Estimated total", "Quotation from the shop"], ["Why", quote.quote_reasons.join(" ")]);
            } else if (quote.estimated_total === null) {
                pricing.push(["Estimated total", quote.incomplete.join(" ") || "—"]);
            } else {
                quote.lines.forEach((line) => pricing.push([line.label, `${money(line.amount)} (${line.detail})`]));
                pricing.push(["Estimated total", money(quote.estimated_total)]);
                pricing.push(["50% down payment (estimate)", money(quote.estimated_total * 0.5)]);
            }
        }
        pricing.push(["Final amount", "Confirmed by the administrator before payment is requested"]);
        appendGroup("Pricing", pricing);
    }

    // ---------------------------------------------------------------- navigation & validation

    function validateStep(stepNumber) {
        setError("");
        if (stepNumber === 1) {
            if (!state.product) {
                $("#product-error").textContent = "Choose a product to continue.";
                return false;
            }
            return true;
        }
        if (stepNumber === 2) {
            if (state.invitation) {
                if (!state.mode) {
                    modeError.textContent = "Choose how you would like to order this invitation.";
                    return false;
                }
                if (state.mode === "package" && !state.package) {
                    packageError.textContent = "Select a package to continue.";
                    return false;
                }
                return true;
            }
            const input = generalQuantity;
            if (!input.checkValidity()) {
                input.reportValidity();
                return false;
            }
            if (!state.forms.general?.validate()) return false;
            if (fileCount() > MAX_ORDER_FILES) {
                setError(`Attach up to ${MAX_ORDER_FILES} files in total. Email any extra originals to the shop.`);
                return false;
            }
            return true;
        }
        if (stepNumber === 3) {
            if (!invitationQuantity.checkValidity()) {
                invitationQuantity.reportValidity();
                return false;
            }
            if (state.mode === "customize") {
                if (!state.forms.basics?.validate()) return false;
                if (!state.forms.design?.validate()) return false;
            }
            return true;
        }
        if (stepNumber === 4) {
            if (!state.forms.event?.validate()) return false;
            if (!state.forms.upload?.validate()) return false;
            if (fileCount() > MAX_ORDER_FILES) {
                setError(`Attach up to ${MAX_ORDER_FILES} files in total. Email any extra originals to the shop.`);
                return false;
            }
            return true;
        }
        const section = steps.find((item) => Number(item.dataset.step) === stepNumber);
        const invalid = [...section.querySelectorAll("input, select, textarea")].filter((control) => !control.disabled && !control.checkValidity());
        if (invalid.length) {
            invalid[0].reportValidity();
            return false;
        }
        if (stepNumber === 6 && !$("#acknowledge").checked) {
            $("#acknowledgement-error").textContent = "Please acknowledge the order instructions before submitting.";
            $("#acknowledge").focus();
            return false;
        }
        return true;
    }

    function setSequence(kind) {
        state.sequence = SEQUENCES[kind];
        stepper.classList.toggle("stepper-six", state.sequence.length > 4);
        stepper.replaceChildren(...state.sequence.map((item, index) => {
            const indicator = el("li", "stepper-item");
            indicator.dataset.stepIndicator = String(item.step);
            indicator.append(el("span", "stepper-num", String(index + 1)), el("span", "stepper-label", item.label));
            return indicator;
        }));
    }

    function showStep(stepNumber, scroll = true) {
        // Never reveal the package order form before a package has been chosen.
        if (state.invitation && state.mode === "package" && !state.package && stepNumber > 2) {
            stepNumber = 2;
            packageError.textContent = "Select a package to continue.";
        }
        const index = state.sequence.findIndex((item) => item.step === stepNumber);
        if (index === -1) return;
        state.activeStep = stepNumber;
        steps.forEach((section) => section.classList.toggle("active", Number(section.dataset.step) === stepNumber));
        [...stepper.children].forEach((indicator, indicatorIndex) => {
            indicator.classList.toggle("active", indicatorIndex === index);
            indicator.classList.toggle("done", indicatorIndex < index);
            if (indicatorIndex === index) indicator.setAttribute("aria-current", "step");
            else indicator.removeAttribute("aria-current");
        });
        const last = state.sequence.length - 1;
        $("#btn-back").hidden = index === 0;
        $("#btn-cancel").hidden = index !== last;
        $("#btn-next").hidden = index === last;
        $("#btn-submit").hidden = index !== last;
        syncNextButton();
        if (stepNumber === 6) renderSummary();
        if (scroll) {
            const section = steps.find((item) => Number(item.dataset.step) === stepNumber);
            section?.scrollIntoView({ behavior: "smooth", block: "start" });
        }
    }

    function moveStep(direction) {
        const index = state.sequence.findIndex((item) => item.step === state.activeStep);
        const target = state.sequence[index + direction];
        if (!target) return;
        if (direction > 0 && state.invitation && state.mode === "package" && !state.package && target.step > 2) {
            packageError.textContent = "Select a package to continue.";
            showStep(2);
            return;
        }
        showStep(target.step);
    }

    // ---------------------------------------------------------------- submission

    function fileAsDataUrl(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.addEventListener("load", () => resolve(reader.result));
            reader.addEventListener("error", () => reject(new Error(`Could not read ${file.name}.`)));
            reader.readAsDataURL(file);
        });
    }

    async function encodeFiles(files) {
        return Promise.all(files.map(async (file) => ({ name: file.name, dataUrl: await fileAsDataUrl(file) })));
    }

    async function submitOrder(event) {
        event.preventDefault();
        $("#acknowledgement-error").textContent = "";
        for (const item of state.sequence.slice(0, -1)) {
            if (!validateStep(item.step)) {
                showStep(item.step);
                return;
            }
        }
        if (!validateStep(6)) return;
        const button = $("#btn-submit");
        button.disabled = true;
        button.textContent = "Submitting…";
        try {
            const fieldFiles = {};
            for (const dynamicForm of activeForms()) {
                for (const [key, files] of Object.entries(dynamicForm.files())) fieldFiles[key] = await encodeFiles(files);
            }
            const customization = {
                quantity: quantityValue(),
                options: collectValues(),
                notes: activeNotes().value.trim(),
            };
            if (state.invitation && state.mode === "package" && state.package) customization.package_id = state.package.id;
            const payload = {
                customer: {
                    name: $("#name").value.trim(),
                    email: $("#email").value.trim(),
                    phone: $("#phone").value.trim(),
                    messenger: $("#messenger").value.trim(),
                },
                product: { id: state.product.id },
                customization,
                has_event: state.invitation ? true : !eventFields.hidden,
                event: state.invitation || eventFields.hidden ? {} : eventData(),
                acknowledged: $("#acknowledge").checked,
                files: state.invitation ? [] : await encodeFiles(state.referenceFiles),
                field_files: fieldFiles,
            };
            const result = await requestJson("/api/orders", {
                method: "POST",
                headers: { "Content-Type": "application/json", "Idempotency-Key": state.idempotencyKey },
                body: JSON.stringify(payload),
            });
            showConfirmation(result);
            state.idempotencyKey = newIdempotencyKey();
        } catch (error) {
            setError(error instanceof Error ? error.message : "The order request could not be submitted.");
        } finally {
            button.disabled = false;
            button.textContent = "Submit order request";
        }
    }

    function showConfirmation(order) {
        form.hidden = true;
        stepper.hidden = true;
        $("#confirm-ref").textContent = order.reference;
        const estimate = order.pricing?.estimated_total;
        const details = $("#confirm-details");
        const packageName = order.order_kind === "package" ? order.package?.name : "";
        details.replaceChildren(
            el("p", "", `${order.product.name}${packageName ? ` · ${packageName}` : ""} · ${order.customization.quantity.toLocaleString()} pc(s) · ${order.status}`),
            el("p", "", estimate !== null && estimate !== undefined
                ? `Estimated total: ${money(estimate)} — the administrator will confirm the final amount.`
                : "The administrator will send a quotation for this request."),
        );
        $("#confirm-track").href = "/customer-portal";
        const confirmation = $("#confirmation");
        confirmation.hidden = false;
        confirmation.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    // ---------------------------------------------------------------- wiring

    categorySelect.addEventListener("change", () => {
        state.product = null;
        populateProducts("");
    });
    [generalQuantity, invitationQuantity].forEach((input) => input.addEventListener("input", onQuantityChange));
    form.querySelectorAll("[data-quantity-step]").forEach((button) => {
        button.addEventListener("click", () => {
            const input = button.closest(".quantity-control")?.querySelector("input");
            if (!input || input.disabled) return;
            const step = Number(button.dataset.quantityStep);
            const min = Number(input.min) || 1;
            const max = Number(input.max) || 50000;
            const current = Number(input.value) || min;
            input.value = String(Math.min(max, Math.max(min, current + step)));
            input.dispatchEvent(new Event("input", { bubbles: true }));
        });
    });
    document.querySelectorAll("[data-order-mode]").forEach((button) => {
        button.addEventListener("click", () => {
            if (button.disabled) return;
            setMode(button.dataset.orderMode);
        });
    });
    hasEvent.addEventListener("change", configureLegacyEvent);
    $("#btn-next").addEventListener("click", () => {
        if (validateStep(state.activeStep)) moveStep(1);
    });
    $("#btn-back").addEventListener("click", () => moveStep(-1));
    $("#edit-order").addEventListener("click", () => showStep(state.invitation ? 3 : 2));
    $("#btn-cancel").addEventListener("click", () => {
        if (window.confirm("Cancel this order request and return to the product catalog?")) window.location.assign("/services");
    });
    form.addEventListener("input", () => {
        if (state.activeStep === 6) renderSummary();
    });
    form.addEventListener("submit", submitOrder);

    // ---------------------------------------------------------------- start: sign-in gate, then catalog

    const gate = $("#order-gate");

    function showGate() {
        // Return to this exact page (with the chosen product) after signing in or creating an account.
        const here = window.location.pathname + window.location.search;
        gate?.querySelectorAll("a[data-auth-return]").forEach((link) => {
            const url = new URL(link.getAttribute("href"), window.location.origin);
            url.searchParams.set("next", here);
            link.href = url.pathname + url.search;
        });
        if (gate) gate.hidden = false;
        stepper.hidden = true;
        form.hidden = true;
    }

    function showOrderForm(customer) {
        if (gate) gate.hidden = true;
        stepper.hidden = false;
        form.hidden = false;
        $("#name").value = customer.name || "";
        $("#email").value = customer.email || "";
        $("#phone").value = customer.phone || "";
        $("#messenger").value = customer.messenger || "";
        // The account is the source of truth for the email, and for the phone once it is set.
        $("#email").readOnly = true;
        $("#phone").readOnly = Boolean(customer.phone);
    }

    async function start() {
        let customer;
        try {
            const response = await fetch("/api/customer/session", { credentials: "same-origin" });
            if (response.status === 401) {
                showGate();
                return;
            }
            if (!response.ok) throw new Error("Your saved account details could not be loaded.");
            customer = (await response.json()).customer;
        } catch (error) {
            showGate();
            setError(error instanceof Error ? error.message : "Your account details could not be loaded.");
            return;
        }
        try {
            state.categories = (await requestJson("/api/catalog", { cache: "no-store" })).categories;
        } catch (error) {
            showOrderForm(customer);
            setError(`The product catalog could not be loaded. ${error instanceof Error ? error.message : ""}`);
            $("#btn-next").disabled = true;
            return;
        }
        showOrderForm(customer);
        if (!state.categories.length) {
            setError("No products are available for online ordering right now. Please contact the shop.");
            $("#btn-next").disabled = true;
            return;
        }
        setSequence("general");
        populateCategories();
        showStep(1, false);
    }

    setSequence("general");
    void start();
})();
