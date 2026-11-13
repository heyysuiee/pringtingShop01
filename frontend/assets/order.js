"use strict";

/**
 * Order wizard. The catalog, each product's questions, its packages, and all prices come from /api/catalog,
 * so the form always reflects what the administrator configured. Estimates are calculated by the server
 * (/api/catalog/quote) with the same rules it applies when the order is saved, for custom orders and
 * package orders alike.
 *
 * Every product starts with an ordering option: Select Package or Customize Order.
 *   Invitations:  Product → Ordering Option → Configure → Event & Files → Mockup → Contact & Payment → Review
 *   Other items:  Product → Ordering Option → Details → Contact & Payment → Review
 */
(() => {
    const DynamicForm = window.PrintShopDynamicForm;
    const Mockup = window.InvitationMockup;
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

    const packageSection = $("#package-section");
    const customizeSection = $("#customize-section");
    const packageList = $("#package-list");
    const packageError = $("#package-error");
    const modeError = $("#order-mode-error");
    const configureLead = $("#configure-lead");
    const selectedPackageSummary = $("#selected-package-summary");
    const generalPackageSummary = $("#general-package-summary");

    const generalFormContainer = $("#general-dynamic-fields");
    const generalPackageContainer = $("#general-package-fields");
    const basicsContainer = $("#invitation-basics-fields");
    const optionsContainer = $("#invitation-options-fields");
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
    const paymentBreakdown = $("#payment-breakdown");

    const generalEstimate = { total: $("#estimate-total"), status: $("#estimate-status"), lines: $("#estimate-lines") };
    const invitationEstimate = { total: $("#invitation-estimate-total"), status: $("#invitation-estimate-status"), lines: $("#invitation-estimate-lines") };

    const MAX_REFERENCE_FILES = 3;
    const MAX_ORDER_FILES = 15;
    const DOWN_PAYMENT_RATE = 0.5;
    const money = DynamicForm.money;
    const parameters = new URLSearchParams(window.location.search);

    const SEQUENCES = {
        general: [
            { step: 1, label: "Product" },
            { step: 2, label: "Ordering Option" },
            { step: 8, label: "Details" },
            { step: 5, label: "Contact & Payment" },
            { step: 6, label: "Review" },
        ],
        invitation: [
            { step: 1, label: "Product" },
            { step: 2, label: "Ordering Option" },
            { step: 3, label: "Configure" },
            { step: 4, label: "Event & Files" },
            { step: 7, label: "Mockup" },
            { step: 5, label: "Contact & Payment" },
            { step: 6, label: "Review" },
        ],
    };
    const REVIEW_STEP = 6;

    const state = {
        categories: [],
        product: null,
        invitation: false,
        kind: null, // mockup kind: birthday | wedding | christening
        mode: "", // "package" | "customize"
        packages: [],
        package: null,
        forms: {},
        referenceFiles: [],
        quote: null,
        quoteTimer: 0,
        quoteSequence: 0,
        sequence: SEQUENCES.general,
        activeStep: 1,
        idempotencyKey: newIdempotencyKey(),
        mockup: { template: "", palette: 0, skip: false },
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

    function perPiece(pkg) {
        return pkg.included_quantity ? Math.round((pkg.price / pkg.included_quantity) * 100) / 100 : null;
    }

    /** "₱12.00 per piece", "₱20.00 per sq ft", packages "from ₱15.00 per piece", or "Request a quote". */
    function priceSummary(product) {
        const packages = product.packages || [];
        const cheapest = packages.length ? Math.min(...packages.map((pkg) => perPiece(pkg) ?? Infinity)) : Infinity;
        if (product.pricing_mode !== "quote" && product.base_price !== null) {
            return product.pricing_mode === "area"
                ? `${money(product.base_price)} per sq ft`
                : `${money(product.base_price)} per piece`;
        }
        if (Number.isFinite(cheapest)) return `Packages from ${money(cheapest)} per piece`;
        return "Request a quote";
    }

    function productImages(product) {
        return (product.images && product.images.length ? product.images : (product.image ? [product.image] : []));
    }

    function productImage(product, className) {
        const source = productImages(product)[0];
        if (source) {
            const image = el("img", className);
            image.src = source;
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

    // ---------------------------------------------------------------- lightbox (product and package photos, mockup)

    const lightbox = $("#customer-lightbox");
    const lightboxState = { items: [], index: 0 };

    function openLightbox(items, index = 0) {
        if (!lightbox || !items.length) return;
        lightboxState.items = items;
        showLightbox(index);
        if (!lightbox.open) lightbox.showModal();
    }

    function showLightbox(index) {
        const items = lightboxState.items;
        lightboxState.index = (index + items.length) % items.length;
        const item = items[lightboxState.index];
        const stage = $("#lightbox-stage");
        stage.replaceChildren();
        if (item.node) {
            stage.append(item.node());
        } else {
            const image = el("img");
            image.src = item.src;
            image.alt = item.alt || "";
            stage.append(image);
        }
        $("#lightbox-caption").textContent = item.caption || "";
        $("#lightbox-count").textContent = items.length > 1 ? `${lightboxState.index + 1} of ${items.length}` : "";
        $("#lightbox-prev").hidden = items.length < 2;
        $("#lightbox-next").hidden = items.length < 2;
    }

    if (lightbox) {
        $("#lightbox-prev").addEventListener("click", () => showLightbox(lightboxState.index - 1));
        $("#lightbox-next").addEventListener("click", () => showLightbox(lightboxState.index + 1));
        lightbox.querySelector("[data-close-lightbox]").addEventListener("click", () => lightbox.close());
        lightbox.addEventListener("click", (event) => {
            if (event.target === lightbox) lightbox.close();
        });
        lightbox.addEventListener("keydown", (event) => {
            if (event.key === "ArrowLeft") showLightbox(lightboxState.index - 1);
            if (event.key === "ArrowRight") showLightbox(lightboxState.index + 1);
        });
    }

    /** A main photo with thumbnails; selecting the photo opens the lightbox. */
    function gallery(images, name, className = "") {
        const wrapper = el("div", `photo-gallery ${className}`.trim());
        if (!images.length) {
            const placeholder = el("span", "photo-gallery-main product-image-placeholder", name.slice(0, 1));
            placeholder.setAttribute("aria-hidden", "true");
            wrapper.append(placeholder);
            return wrapper;
        }
        let current = 0;
        const items = images.map((src, index) => ({ src, alt: `${name} — photo ${index + 1}`, caption: `${name} · photo ${index + 1} of ${images.length}` }));
        const main = el("button", "photo-gallery-main");
        main.type = "button";
        main.setAttribute("aria-label", `View larger photos of ${name}`);
        const mainImage = el("img");
        mainImage.alt = items[0].alt;
        mainImage.src = images[0];
        main.append(mainImage);
        if (images.length > 1) main.append(el("span", "photo-gallery-count", `${images.length} photos`));
        main.addEventListener("click", () => openLightbox(items, current));
        wrapper.append(main);
        if (images.length > 1) {
            const thumbs = el("div", "photo-gallery-thumbs");
            images.forEach((src, index) => {
                const thumb = el("button", `photo-gallery-thumb${index === 0 ? " active" : ""}`);
                thumb.type = "button";
                thumb.setAttribute("aria-label", `Show photo ${index + 1} of ${name}`);
                const image = el("img");
                image.src = src;
                image.alt = "";
                image.loading = "lazy";
                thumb.append(image);
                thumb.addEventListener("click", () => {
                    current = index;
                    mainImage.src = src;
                    mainImage.alt = items[index].alt;
                    thumbs.querySelectorAll(".photo-gallery-thumb").forEach((item, position) => item.classList.toggle("active", position === index));
                });
                thumbs.append(thumb);
            });
            wrapper.append(thumbs);
        }
        return wrapper;
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
        scheduleQuote();
        if (state.activeStep === 7) renderMockup();
        if (state.activeStep === REVIEW_STEP) renderSummary();
    }

    function createForms(product) {
        destroyForms();
        if (state.invitation) {
            state.forms = {
                basics: DynamicForm.create(basicsContainer, product, { includeSections: [""], onChange: onFormChange, idPrefix: "inv-basics" }),
                options: DynamicForm.create(optionsContainer, product, { includeSections: ["options"], onChange: onFormChange, idPrefix: "inv-options" }),
                design: DynamicForm.create(designContainer, product, { includeSections: ["design"], onChange: onFormChange, idPrefix: "inv-design" }),
                event: DynamicForm.create(eventContainer, product, { includeSections: ["event"], onChange: onFormChange, idPrefix: "inv-event" }),
                upload: DynamicForm.create(uploadContainer, product, { includeSections: ["upload"], onChange: onFormChange, idPrefix: "inv-upload" }),
            };
            $("#invitation-options-block").hidden = !state.forms.options.fieldCount;
        } else {
            const packageKeys = new Set(product.package_field_keys || []);
            state.forms = {
                general: DynamicForm.create(generalFormContainer, product, { onChange: onFormChange, idPrefix: "opt" }),
                generalPackage: DynamicForm.create(generalPackageContainer, product, {
                    onChange: onFormChange,
                    idPrefix: "pkg",
                    fieldFilter: (field) => packageKeys.has(field.field_key),
                }),
            };
        }
    }

    function activeForms() {
        const forms = state.forms;
        if (!state.invitation) {
            return [state.mode === "package" ? forms.generalPackage : forms.general].filter(Boolean);
        }
        if (state.mode === "package") return [forms.options, forms.event, forms.upload].filter(Boolean);
        return [forms.basics, forms.options, forms.design, forms.event, forms.upload].filter(Boolean);
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

    // ---------------------------------------------------------------- product selection

    function renderSelectedProduct(product) {
        selectedProduct.hidden = false;
        selectedProduct.replaceChildren();
        const copy = el("div", "selected-product-copy");
        copy.append(
            el("span", "order-step-kicker", product.category_label),
            el("h3", "", product.name),
            el("p", "", product.description || ""),
            el("strong", "product-price-note", priceSummary(product)),
        );
        const packages = product.packages || [];
        copy.append(el("p", "field-hint", packages.length
            ? `${packages.length} package${packages.length === 1 ? "" : "s"} available, or customize your own order.`
            : "Customize your own order; the shop has not published packages for this item yet."));
        if (product.min_quantity > 1) copy.append(el("span", "min-badge", `Minimum order: ${product.min_quantity.toLocaleString()} pcs`));
        selectedProduct.append(gallery(productImages(product), product.name, "selected-product-gallery"), copy);
    }

    function selectProduct(productId) {
        const product = currentCategory()?.products.find((item) => item.id === productId);
        if (!product || product === state.product) return;
        state.product = product;
        state.invitation = isInvitationProduct(product);
        state.kind = Mockup ? Mockup.kindOf(product) : null;
        state.packages = Array.isArray(product.packages) ? product.packages : [];
        state.package = null;
        state.quote = null;
        state.quoteSequence += 1;
        window.clearTimeout(state.quoteTimer);
        packageError.textContent = "";
        modeError.textContent = "";
        $("#product-error").textContent = "";
        setError("");

        renderSelectedProduct(product);
        const note = pricingNote.querySelector("div");
        const priced = product.pricing_mode !== "quote" && product.base_price !== null;
        note.replaceChildren(
            el("strong", "", state.packages.length ? "Packages or a customized order" : (priced ? "Prices set by the shop" : "Request a quote")),
            el("p", "", state.packages.length
                ? "Choose a published package with a fixed price, or customize your order. The total is calculated from the quantity and the shop confirms the final amount before payment."
                : priced
                    ? "You will see the price per piece and a live total as you choose options. The administrator confirms the final amount before payment is requested."
                    : "This product does not have a published price yet. Submit your options and the administrator will send a quotation."),
        );

        $("#step2-product").textContent = product.name;
        $("#step2-general-product").textContent = product.name;
        const largeFormat = product.category_id === "large-format" || product.pricing_mode === "area";
        $("#customize-mode-title").textContent = state.invitation ? "Customize Your Own Invitation" : "Customize Order";
        $("#customize-mode-note").textContent = state.invitation
            ? "Set the printing, finish, envelope, quantity, design direction, and event details yourself."
            : largeFormat
                ? "Enter your exact size, material, and requirements. Priced per square foot or by quotation."
                : "Choose your own size, material, and details. Priced from the shop's rates or by quotation.";
        $("#package-mode-note").textContent = state.packages.length
            ? `${state.packages.length} package${state.packages.length === 1 ? "" : "s"} with fixed prices, inclusions, and photos.`
            : "No packages are published for this item yet.";

        createForms(product);
        state.referenceFiles = [];
        renderReferenceFiles();
        resetMockup();
        renderPackages();
        // Without packages, the customized order is the only option.
        setMode(state.packages.length ? "" : "customize", { silent: true });
        configureLegacyEvent();
        setSequence(state.invitation ? "invitation" : "general");

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
        const pkg = state.mode === "package" ? state.package : null;

        if (pkg) {
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
        } else if (state.mode === "package") {
            hint.textContent = "Select a package to see its included quantity.";
        } else {
            hint.textContent = product.min_quantity > 1
                ? `Minimum order: ${product.min_quantity.toLocaleString()} pieces · maximum ${product.max_quantity.toLocaleString()}. The total updates with the quantity.`
                : `Up to ${product.max_quantity.toLocaleString()} pieces. The total updates with the quantity.`;
        }
        const badge = state.invitation ? $("#invitation-min-badge") : $("#quantity-min-badge");
        badge.hidden = min <= 1;
        badge.textContent = min === max ? `Fixed: ${min.toLocaleString()} pcs` : `Minimum ${min.toLocaleString()} pcs`;

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
        input.closest(".quantity-field")?.querySelectorAll(".quantity-step").forEach((button) => {
            button.disabled = input.disabled;
        });
    }

    function onQuantityChange() {
        scheduleQuote();
        if (state.activeStep === REVIEW_STEP) renderSummary();
    }

    // ---------------------------------------------------------------- ordering option (package or customize)

    function updateModeButtons() {
        document.querySelectorAll("[data-order-mode]").forEach((button) => {
            const active = button.dataset.orderMode === state.mode;
            button.classList.toggle("selected", active);
            button.setAttribute("aria-pressed", String(active));
        });
        const packageButton = $('[data-order-mode="package"]');
        packageButton.disabled = !state.packages.length;
        packageButton.setAttribute("aria-disabled", String(!state.packages.length));
    }

    /** The Next button stays locked on the ordering step while a package is required but not chosen. */
    function syncNextButton() {
        const next = $("#btn-next");
        if (!next || !state.product) return;
        const needsPackage = state.activeStep === 2 && state.mode === "package" && !state.package;
        next.disabled = needsPackage;
        next.title = needsPackage ? "Select a package to continue." : "";
    }

    function setMode(mode, { silent = false } = {}) {
        if (mode === "package" && !state.packages.length) mode = "customize";
        state.mode = mode;
        if (mode !== "package") state.package = null;
        if (!silent) {
            modeError.textContent = "";
            if (mode === "package") packageError.textContent = "";
        }
        updateModeButtons();
        packageSection.hidden = mode !== "package";
        renderPackages();
        customizeSection.hidden = mode !== "customize";
        generalFormContainer.hidden = mode === "package";
        const packageFormCount = state.forms.generalPackage?.fieldCount || 0;
        generalPackageContainer.hidden = mode !== "package" || !packageFormCount;
        $("#general-step-title").textContent = mode === "package" ? "Package details for" : "Customize your";
        $("#general-step-lead").textContent = mode === "package"
            ? (packageFormCount
                ? "Your package fixes the specifications. Add the personalization details and the quantity you need."
                : "Your package fixes the specifications. Set the quantity and add any special instructions.")
            : "Only the options for this product are shown. Fields marked “optional” can be left blank.";
        configureLead.textContent = mode === "package"
            ? (state.package
                ? "Choose the paper finish and envelope, then set the total pieces. The package price is fixed; extra pieces use the package's add-on rate."
                : "Choose a package to open the package order form.")
            : "Set the printing, finish, envelope, quantity, and design direction. Required fields are marked; the shop confirms the final amount before payment.";
        renderSelectedPackageSummary();
        state.quote = null;
        configureQuantity({ reset: true });
        scheduleQuote();
        syncNextButton();
        if (state.activeStep === REVIEW_STEP) renderSummary();
    }

    function packageSummaryNode(pkg) {
        const fragment = document.createDocumentFragment();
        const head = el("div", "selected-package-head");
        head.append(el("span", "order-step-kicker", "Selected package"), el("strong", "selected-package-name", pkg.name));
        const unit = perPiece(pkg);
        const meta = [money(pkg.price), unit !== null ? `≈ ${money(unit)} per piece` : "", pkg.size, pkg.material].filter(Boolean).join(" · ");
        fragment.append(head, el("p", "selected-package-meta", meta));
        if (pkg.description) fragment.append(el("p", "field-hint", pkg.description));
        if (pkg.inclusions?.length) {
            const list = el("ul", "package-inclusions");
            pkg.inclusions.forEach((item) => list.append(el("li", "", item)));
            fragment.append(list);
        }
        const change = el("button", "btn btn-ghost-dark btn-small package-change", "Change package");
        change.type = "button";
        change.addEventListener("click", () => {
            showStep(2);
            packageSection.scrollIntoView({ behavior: "smooth", block: "center" });
            packageList.querySelector(".package-card.selected .package-select")?.focus();
        });
        fragment.append(change);
        return fragment;
    }

    function renderSelectedPackageSummary() {
        const pkg = state.mode === "package" ? state.package : null;
        for (const target of [selectedPackageSummary, generalPackageSummary]) {
            target.replaceChildren();
            target.hidden = !pkg;
            if (pkg) target.append(packageSummaryNode(pkg));
        }
    }

    function renderPackages() {
        packageList.replaceChildren();
        if (!state.packages.length) {
            packageList.append(el("p", "package-empty", "No packages are available for this item right now. Choose “Customize Order” to continue."));
            return;
        }
        for (const pkg of state.packages) {
            const selected = state.package?.id === pkg.id;
            const card = el("article", `package-card${selected ? " selected" : ""}`);
            const media = gallery(pkg.images?.length ? pkg.images : (pkg.image ? [pkg.image] : productImages(state.product)), pkg.name, "package-card-gallery");
            const body = el("div", "package-card-body");
            body.append(el("h3", "package-name", pkg.name));
            const price = el("p", "package-price", money(pkg.price));
            const unit = perPiece(pkg);
            if (unit !== null) price.append(el("span", "package-unit-price", ` ≈ ${money(unit)} per piece`));
            body.append(price);
            if (pkg.description) body.append(el("p", "package-description", pkg.description));
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
            body.append(el("p", "package-addon", pkg.add_on_price === null
                ? `Ordered at exactly ${pkg.included_quantity.toLocaleString()} pieces.`
                : `${pkg.add_on_label || "Additional pieces"}: ${money(pkg.add_on_price)} per additional piece.`));
            const select = el("button", "btn btn-dark package-select", selected ? "Selected ✓" : "Select Package");
            select.type = "button";
            select.setAttribute("aria-pressed", String(selected));
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
        renderSelectedPackageSummary();
        state.quote = null;
        scheduleQuote();
        syncNextButton();
        if (state.activeStep === REVIEW_STEP) renderSummary();
    }

    // ---------------------------------------------------------------- general products: optional event details

    function configureLegacyEvent() {
        // Invitation event details are catalog fields in the "event" section.
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
        const reference = estimateReference();
        if (!state.mode || (state.mode === "package" && !state.package)) {
            state.quote = null;
            reference.total.textContent = "—";
            reference.lines.replaceChildren();
            reference.status.textContent = state.mode === "package" ? "Select a package to see its total." : "Choose how you would like to order.";
            renderPaymentBreakdown();
            return;
        }
        reference.status.textContent = "Updating estimate…";
        state.quoteTimer = window.setTimeout(refreshQuote, 250);
    }

    async function refreshQuote() {
        if (!state.product || !state.mode) return;
        const sequence = ++state.quoteSequence;
        const body = {
            product_id: state.product.id,
            quantity: quantityValue() || state.product.min_quantity,
            options: collectValues(),
        };
        if (state.mode === "package" && state.package) body.package_id = state.package.id;
        try {
            const quote = await requestJson("/api/catalog/quote", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            if (sequence !== state.quoteSequence) return; // a newer request is in flight
            state.quote = quote;
        } catch (error) {
            if (sequence !== state.quoteSequence) return;
            state.quote = null;
            estimateReference().status.textContent = error instanceof Error ? error.message : "The estimate could not be calculated.";
            renderPaymentBreakdown();
            return;
        }
        renderEstimate();
        renderPaymentBreakdown();
        if (state.activeStep === REVIEW_STEP) renderSummary();
    }

    function estimateText(quote) {
        if (!quote) return "—";
        if (quote.estimated_total !== null) return money(quote.estimated_total);
        return quote.requires_quote ? "Quotation" : "—";
    }

    function renderEstimate() {
        const reference = estimateReference();
        const quote = state.quote;
        reference.total.textContent = estimateText(quote);
        reference.lines.replaceChildren();
        if (!quote) return;
        let status;
        if (quote.requires_quote) status = quote.quote_reasons.join(" ");
        else if (quote.incomplete.length) status = quote.incomplete.join(" ");
        else if (quote.unit_price !== null) {
            status = quote.pricing_mode === "package"
                ? `≈ ${money(quote.unit_price)} per piece × ${quote.quantity.toLocaleString()} pcs · package price, confirmed by the shop`
                : `${money(quote.unit_price)} per piece × ${quote.quantity.toLocaleString()} pcs · estimate, confirmed by the shop`;
        } else status = "";
        reference.status.textContent = status;
        if (quote.requires_quote) return;
        quote.lines.forEach((line) => {
            const item = el("li");
            const label = el("span");
            label.append(el("strong", "", line.label), el("small", "", line.detail));
            item.append(label, el("span", "estimate-amount", quote.estimated_total === null && quote.pricing_mode === "area" ? "—" : money(line.amount)));
            reference.lines.append(item);
        });
        if (quote.estimated_total !== null) {
            const total = el("li", "estimate-total-line");
            total.append(el("strong", "", "Total"), el("strong", "estimate-amount", money(quote.estimated_total)));
            reference.lines.append(total);
        }
    }

    // ---------------------------------------------------------------- payment option & breakdown

    function paymentOption() {
        return form.querySelector('input[name="payment-option"]:checked')?.value || "down_payment";
    }

    function paymentRows() {
        const cod = paymentOption() === "cod";
        const total = state.quote?.estimated_total ?? null;
        const rows = [["Payment option", cod ? "Cash on Delivery (COD)" : "50% Down Payment"]];
        if (total === null) {
            rows.push(["Total", "Confirmed by the shop's quotation"]);
            rows.push(cod ? ["Down payment", "Not required"] : ["Down payment (50%)", "Half of the confirmed total"]);
            rows.push([cod ? "Due on delivery / pickup" : "Balance before release", cod ? "The full confirmed total" : "The other half"]);
            return rows;
        }
        const down = cod ? 0 : Math.round(total * DOWN_PAYMENT_RATE * 100) / 100;
        rows.push(["Estimated total", money(total)]);
        rows.push(cod ? ["Down payment", "Not required (COD)"] : ["Down payment (50%)", money(down)]);
        rows.push([cod ? "Due on delivery / pickup" : "Balance before release", money(Math.round((total - down) * 100) / 100)]);
        return rows;
    }

    function renderPaymentBreakdown() {
        if (!paymentBreakdown) return;
        paymentBreakdown.replaceChildren();
        paymentRows().forEach(([label, value]) => {
            const row = el("div", "payment-breakdown-row");
            row.append(el("dt", "", label), el("dd", "", value));
            paymentBreakdown.append(row);
        });
        if (state.quote?.estimated_total !== null && state.quote?.estimated_total !== undefined) {
            paymentBreakdown.append(el("p", "field-hint", "Estimated from the shop's published prices; the administrator confirms the final amount."));
        }
    }

    form.querySelectorAll('input[name="payment-option"]').forEach((input) => input.addEventListener("change", () => {
        renderPaymentBreakdown();
        if (state.activeStep === REVIEW_STEP) renderSummary();
    }));

    // ---------------------------------------------------------------- invitation mockup designer

    function resetMockup() {
        const designerStep = steps.find((section) => section.dataset.step === "7");
        if (!state.kind || !Mockup) {
            state.mockup = { template: "", palette: 0, skip: true };
            if (designerStep) designerStep.dataset.available = "false";
            return;
        }
        const first = Mockup.templates(state.kind)[0];
        const defaults = Mockup.defaults(state.kind);
        state.mockup = { template: first.id, palette: 0, skip: false };
        $("#mockup-headline").value = defaults.headline || "";
        $("#mockup-message").value = defaults.message || "";
        $("#mockup-skip").checked = false;
        if (designerStep) designerStep.dataset.available = "true";
    }

    function mockupDetails() {
        const values = state.forms.event ? state.forms.event.values() : {};
        const names = state.kind === "wedding"
            ? [values.bride_name, values.groom_name].filter(Boolean).join(" & ")
            : values.celebrant_name || "";
        return {
            names,
            age: values.celebrant_age !== undefined ? String(values.celebrant_age) : "",
            date: values.event_date || "",
            time: values.event_time || "",
            location: values.event_location || "",
            rsvp: values.rsvp_contact || "",
        };
    }

    function mockupData(template = state.mockup.template, palette = state.mockup.palette) {
        return {
            kind: state.kind,
            template,
            palette,
            headline: $("#mockup-headline").value.trim(),
            message: $("#mockup-message").value.trim(),
            details: mockupDetails(),
        };
    }

    function renderMockup() {
        if (!state.kind || !Mockup) return;
        const templates = Mockup.templates(state.kind);
        const designer = $("#mockup-designer");
        designer.classList.toggle("is-disabled", state.mockup.skip);
        designer.querySelectorAll("input, textarea, button").forEach((control) => {
            if (control.id !== "mockup-edit-details") control.disabled = state.mockup.skip;
        });

        const templateHost = $("#mockup-templates");
        templateHost.replaceChildren(...templates.map((template) => {
            const selected = template.id === state.mockup.template;
            const button = el("button", `mockup-template${selected ? " selected" : ""}`);
            button.type = "button";
            button.setAttribute("role", "radio");
            button.setAttribute("aria-checked", String(selected));
            button.disabled = state.mockup.skip;
            const thumb = el("span", "mockup-template-thumb");
            thumb.append(Mockup.render(mockupData(template.id, selected ? state.mockup.palette : 0)));
            button.append(thumb, el("span", "mockup-template-name", template.name));
            button.addEventListener("click", () => {
                state.mockup.template = template.id;
                state.mockup.palette = 0;
                renderMockup();
            });
            return button;
        }));

        const current = templates.find((template) => template.id === state.mockup.template) || templates[0];
        const paletteHost = $("#mockup-palettes");
        paletteHost.replaceChildren(...current.palettes.map((palette, index) => {
            const selected = index === state.mockup.palette;
            const swatch = el("button", `mockup-swatch${selected ? " selected" : ""}`);
            swatch.type = "button";
            swatch.setAttribute("role", "radio");
            swatch.setAttribute("aria-checked", String(selected));
            swatch.setAttribute("aria-label", `Colour option ${index + 1}`);
            swatch.disabled = state.mockup.skip;
            swatch.style.background = `linear-gradient(135deg, ${palette.bg} 0 50%, ${palette.accent} 50% 100%)`;
            swatch.addEventListener("click", () => {
                state.mockup.palette = index;
                renderMockup();
            });
            return swatch;
        }));

        const preview = $("#mockup-preview");
        preview.replaceChildren(Mockup.render(mockupData()));
        const details = mockupDetails();
        const missing = [["the celebrant's name", details.names], ["the event date", details.date], ["the event location", details.location]]
            .filter(([, value]) => !value).map(([label]) => label);
        $("#mockup-caption").textContent = state.mockup.skip
            ? "Mockup skipped. The shop will design from your details and uploads."
            : `${current.name} · colour option ${state.mockup.palette + 1}. ${missing.length ? `Add ${missing.join(", ")} in Event & Files to complete the card.` : "Select the card to view it larger."}`;
    }

    function mockupPayload() {
        if (!state.kind || state.mockup.skip || !state.mockup.template) return null;
        return {
            template: state.mockup.template,
            palette: state.mockup.palette,
            headline: $("#mockup-headline").value.trim(),
            message: $("#mockup-message").value.trim(),
        };
    }

    $("#mockup-skip")?.addEventListener("change", (event) => {
        state.mockup.skip = event.target.checked;
        renderMockup();
    });
    ["#mockup-headline", "#mockup-message"].forEach((selector) => $(selector)?.addEventListener("input", renderMockup));
    $("#mockup-preview")?.addEventListener("click", () => {
        if (!state.kind) return;
        openLightbox([{ node: () => Mockup.render(mockupData()), caption: "Invitation mockup preview — the shop sends a final draft for your approval." }]);
    });
    $("#mockup-edit-details")?.addEventListener("click", () => showStep(4));

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

    function appendGroup(title, rows, extra = null) {
        const filled = rows.filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== "");
        if (!filled.length && !extra) return;
        const section = el("section", "summary-group");
        section.append(el("h3", "summary-group-title", title));
        if (filled.length) {
            const list = el("dl", "summary order-summary");
            filled.forEach(([label, value]) => {
                const row = el("div", "summary-row");
                row.append(el("dt", "", label), el("dd", "", String(value)));
                list.append(row);
            });
            section.append(list);
        }
        if (extra) section.append(extra);
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
        const kind = state.mode === "package" ? "Package" : (state.invitation ? "Customized invitation" : "Customized order");
        appendGroup("Product", [
            ["Category", product.category_label],
            ["Product", product.name],
            ["Ordering option", kind],
            ["Quantity", `${quantity.toLocaleString()} pcs`],
        ]);

        if (state.mode === "package" && state.package) {
            const pkg = state.package;
            const unit = perPiece(pkg);
            appendGroup("Package", [
                ["Package", pkg.name],
                ["Package price", `${money(pkg.price)}${unit !== null ? ` (≈ ${money(unit)} per piece)` : ""}`],
                ["Size", pkg.size],
                ["Material", pkg.material],
                ["Inclusions", pkg.inclusions.join("; ")],
                ["Included quantity", `${pkg.included_quantity.toLocaleString()} pcs`],
                ["Add-on", pkg.add_on_price === null
                    ? "Not available — this package has a fixed quantity"
                    : `${pkg.add_on_label || "Additional pieces"}: ${money(pkg.add_on_price)} each`],
            ]);
        }

        if (state.invitation) {
            if (state.mode === "customize") {
                appendGroup("Basic specifications", (state.forms.basics?.summary() || []).map((row) => [row.label, row.value]));
                appendGroup("Design customization", (state.forms.design?.summary() || []).map((row) => [row.label, row.value]));
            }
            appendGroup("Paper finish & envelope", (state.forms.options?.summary() || []).map((row) => [row.label, row.value]));
            appendGroup("Event details", (state.forms.event?.summary() || []).map((row) => [row.label, row.value]));
            appendGroup("Photos & references", (state.forms.upload?.summary() || []).map((row) => [row.label, row.value]));
            const mockup = mockupPayload();
            if (mockup) {
                const template = Mockup.templates(state.kind).find((item) => item.id === mockup.template);
                const preview = el("div", "summary-mockup");
                preview.append(Mockup.render(mockupData()));
                appendGroup("Invitation mockup", [["Template", `${template?.name || mockup.template} · colour option ${mockup.palette + 1}`]], preview);
            } else if (state.kind) {
                appendGroup("Invitation mockup", [["Mockup", "Skipped — the shop will design from your details"]]);
            }
        } else {
            const dynamicForm = state.mode === "package" ? state.forms.generalPackage : state.forms.general;
            const specs = dynamicForm?.summary() || [];
            if (state.quote?.area_sqft) specs.push({ label: "Print area", value: `${state.quote.area_sqft} sq ft per piece` });
            appendGroup(state.mode === "package" ? "Personalization" : "Your options", specs.map((row) => [row.label, row.value]));
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
        const quote = state.quote;
        if (!quote) {
            pricing.push(["Estimate", state.mode === "package" && !state.package ? "Select a package" : "Calculating…"]);
        } else if (quote.requires_quote) {
            pricing.push(["Estimated total", "Quotation from the shop"], ["Why", quote.quote_reasons.join(" ")]);
        } else if (quote.estimated_total === null) {
            pricing.push(["Estimated total", quote.incomplete.join(" ") || "—"]);
        } else {
            quote.lines.forEach((line) => pricing.push([line.label, `${money(line.amount)} (${line.detail})`]));
            if (quote.unit_price !== null) pricing.push(["Price per piece", `${money(quote.unit_price)} × ${quote.quantity.toLocaleString()} pcs`]);
            pricing.push(["Estimated total", money(quote.estimated_total)]);
        }
        pricing.push(["Final amount", "Confirmed by the administrator before payment is requested"]);
        appendGroup("Pricing", pricing);
        appendGroup("Payment", paymentRows());
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
            if (!state.mode) {
                modeError.textContent = "Choose “Select Package” or “Customize Order” to continue.";
                return false;
            }
            if (state.mode === "package" && !state.package) {
                packageError.textContent = "Select a package to continue.";
                return false;
            }
            return true;
        }
        if (stepNumber === 8) {
            if (!generalQuantity.checkValidity()) {
                generalQuantity.reportValidity();
                return false;
            }
            const dynamicForm = state.mode === "package" ? state.forms.generalPackage : state.forms.general;
            if (!dynamicForm?.validate()) return false;
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
            if (!state.forms.options?.validate()) return false;
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
        if (stepNumber === 7) return true;
        const section = steps.find((item) => Number(item.dataset.step) === stepNumber);
        const invalid = [...section.querySelectorAll("input, select, textarea")].filter((control) => !control.disabled && !control.checkValidity());
        if (invalid.length) {
            invalid[0].reportValidity();
            return false;
        }
        if (stepNumber === REVIEW_STEP && !$("#acknowledge").checked) {
            $("#acknowledgement-error").textContent = "Please acknowledge the order instructions before submitting.";
            $("#acknowledge").focus();
            return false;
        }
        return true;
    }

    function setSequence(kind) {
        // The mockup designer exists only for Birthday, Wedding, and Christening invitations.
        state.sequence = SEQUENCES[kind].filter((item) => item.step !== 7 || state.kind);
        stepper.classList.add("stepper-dynamic");
        stepper.style.setProperty("--steps", String(state.sequence.length));
        stepper.replaceChildren(...state.sequence.map((item, index) => {
            const indicator = el("li", "stepper-item");
            indicator.dataset.stepIndicator = String(item.step);
            indicator.append(el("span", "stepper-num", String(index + 1)), el("span", "stepper-label", item.label));
            return indicator;
        }));
        // Number each step heading after its position in this product's flow.
        state.sequence.forEach((item, index) => {
            const section = steps.find((element) => Number(element.dataset.step) === item.step);
            const kicker = section?.querySelector(".order-step-kicker");
            if (kicker) kicker.textContent = kicker.textContent.replace(/^\d+\s·\s/, `${String(index + 1).padStart(2, "0")} · `);
        });
    }

    function showStep(stepNumber, scroll = true) {
        // Never open the package order form before a package has been chosen.
        if (state.mode === "package" && !state.package && stepNumber > 2 && stepNumber !== 1) {
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
        if (stepNumber === 7) renderMockup();
        if (stepNumber === 5) renderPaymentBreakdown();
        if (stepNumber === REVIEW_STEP) renderSummary();
        if (scroll) {
            const section = steps.find((item) => Number(item.dataset.step) === stepNumber);
            section?.scrollIntoView({ behavior: "smooth", block: "start" });
        }
    }

    function moveStep(direction) {
        const index = state.sequence.findIndex((item) => item.step === state.activeStep);
        const target = state.sequence[index + direction];
        if (!target) return;
        if (direction > 0 && state.mode === "package" && !state.package && target.step > 2) {
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
        if (!validateStep(REVIEW_STEP)) return;
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
            if (state.mode === "package" && state.package) customization.package_id = state.package.id;
            const mockup = mockupPayload();
            if (mockup) customization.mockup = mockup;
            const payload = {
                customer: {
                    name: $("#name").value.trim(),
                    email: $("#email").value.trim(),
                    phone: $("#phone").value.trim(),
                    messenger: $("#messenger").value.trim(),
                },
                product: { id: state.product.id },
                customization,
                payment_option: paymentOption(),
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
        const payment = order.payment || {};
        const details = $("#confirm-details");
        const packageName = order.order_kind === "package" ? order.package?.name : "";
        const lines = [
            el("p", "", `${order.product.name}${packageName ? ` · ${packageName}` : ""} · ${order.customization.quantity.toLocaleString()} pc(s) · ${order.status}`),
            el("p", "", estimate !== null && estimate !== undefined
                ? `Estimated total: ${money(estimate)}${order.pricing.unit_price ? ` (${money(order.pricing.unit_price)} per piece)` : ""} — the administrator will confirm the final amount.`
                : "The administrator will send a quotation for this request."),
            el("p", "", `Payment option: ${payment.payment_option_label || "50% Down Payment"}${payment.down_payment_required ? ` · down payment ≈ ${money(payment.down_payment_required)} after confirmation` : payment.payment_option === "cod" ? " · no down payment; pay on delivery or pickup" : ""}`),
        ];
        details.replaceChildren(...lines);
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
    $("#edit-order").addEventListener("click", () => showStep(state.invitation ? 3 : 8));
    $("#btn-cancel").addEventListener("click", () => {
        if (window.confirm("Cancel this order request and return to the product catalog?")) window.location.assign("/services");
    });
    form.addEventListener("input", () => {
        if (state.activeStep === REVIEW_STEP) renderSummary();
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
        renderPaymentBreakdown();
    }

    setSequence("general");
    void start();
})();
