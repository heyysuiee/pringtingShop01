"use strict";

/**
 * Order wizard. The catalog, each product's questions, its packages, and all prices come from /api/catalog,
 * so the form always reflects what the administrator configured. Estimates are calculated by the server
 * (/api/catalog/quote) with the same rules it applies when the order is saved, for custom orders and
 * package orders alike.
 *
 * Every product starts with an ordering option: Select Package or Customize Order. Customize Order opens the
 * shop's design gallery when the product has designs (internally the "design" mode: choose a design and add
 * the details, no upload). The gallery also offers "Use my own design", which opens the product's own
 * customize form with its uploads (the "customize" mode, also used by products without designs).
 *   Invitations:  Product → Ordering Option → Configure → Event & Files → Contact & Payment → Review
 *   Other items:  Product → Ordering Option → Details → Contact & Payment → Review
 * A shop design order replaces the artwork steps (uploads, design direction) with Personalize:
 *   Invitations:  Product → Ordering Option (design gallery) → Personalize → Configure → Contact & Payment → Review
 *   Other items:  Product → Ordering Option (design gallery) → Personalize → Print Details → Contact & Payment → Review
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
    const generalDesignContainer = $("#general-design-fields");
    const designSection = $("#design-section");
    const designList = $("#design-list");
    const designFilter = $("#design-filter");
    const designError = $("#design-error");
    const designPreview = $("#design-preview");
    const personalizeContainer = $("#design-personalize-fields");
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
            { step: 5, label: "Contact & Payment" },
            { step: 6, label: "Review" },
        ],
        designGeneral: [
            { step: 1, label: "Product" },
            { step: 2, label: "Ordering Option" },
            { step: 9, label: "Customize Design" },
            { step: 8, label: "Print Details" },
            { step: 5, label: "Contact & Payment" },
            { step: 6, label: "Review" },
        ],
        designInvitation: [
            { step: 1, label: "Product" },
            { step: 2, label: "Ordering Option" },
            { step: 9, label: "Customize Design" },
            { step: 3, label: "Configure" },
            { step: 5, label: "Contact & Payment" },
            { step: 6, label: "Review" },
        ],
    };
    const REVIEW_STEP = 6;

    const state = {
        categories: [],
        product: null,
        invitation: false,
        mode: "", // "package" | "design" | "customize"
        packages: [],
        package: null,
        designs: [], // the shop's designs offered for this product
        ownDesign: false, // Customize Order with the customer's own design instead of a shop design
        design: null,
        designStyle: "", // gallery filter
        personalizeForm: null, // the chosen design's personalization questions
        forms: {},
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

    function perPiece(pkg) {
        return pkg.included_quantity ? Math.round((pkg.price / pkg.included_quantity) * 100) / 100 : null;
    }

    /** The cheapest standard size of a size-priced product (Tarpaulin price list), or null. */
    function cheapestSize(product) {
        const sizes = product.pricing_mode === "size" ? (product.standard_sizes || []) : [];
        return sizes.length ? sizes.reduce((low, size) => (size.price < low.price ? size : low)) : null;
    }

    /** "₱12.00 per piece", "₱20.00 per sq ft", "from ₱200.00 (2 × 3 ft)", packages "from ₱15.00 per piece", or "Request a quote". */
    function priceSummary(product) {
        const packages = product.packages || [];
        const cheapest = packages.length ? Math.min(...packages.map((pkg) => perPiece(pkg) ?? Infinity)) : Infinity;
        if (product.pricing_mode === "size") {
            const size = cheapestSize(product);
            return size ? `From ${money(size.price)} (${size.label}) · custom sizes quoted` : "Request a quote";
        }
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

    // ---------------------------------------------------------------- lightbox (product and package photos)

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
        setLightboxZoom(false);
        const zoomButton = $("#lightbox-zoom");
        if (zoomButton) zoomButton.hidden = Boolean(item.node); // zoom applies to photos, not the live preview
    }

    // Zoom: the photo scales up and follows the pointer, so any part of it can be inspected.
    function setLightboxZoom(zoomed) {
        const stage = $("#lightbox-stage");
        const button = $("#lightbox-zoom");
        if (!stage) return;
        stage.classList.toggle("is-zoomed", zoomed);
        const image = stage.querySelector("img");
        if (image && !zoomed) image.style.transformOrigin = "";
        if (button) {
            button.setAttribute("aria-pressed", String(zoomed));
            button.textContent = zoomed ? "Zoom out" : "Zoom in";
        }
    }

    function panLightbox(event) {
        const stage = $("#lightbox-stage");
        const image = stage?.querySelector("img");
        if (!image || !stage.classList.contains("is-zoomed")) return;
        const rect = image.getBoundingClientRect();
        const x = Math.min(100, Math.max(0, ((event.clientX - rect.left) / rect.width) * 100));
        const y = Math.min(100, Math.max(0, ((event.clientY - rect.top) / rect.height) * 100));
        image.style.transformOrigin = `${x}% ${y}%`;
    }

    // Full screen: the native API when the browser allows it, otherwise a viewport-filling layout.
    function setLightboxFullscreen(on) {
        lightbox.classList.toggle("is-fullscreen", on);
        const button = $("#lightbox-fullscreen");
        if (button) {
            button.setAttribute("aria-pressed", String(on));
            button.textContent = on ? "Exit full screen" : "Full screen";
        }
    }

    if (lightbox) {
        const stage = $("#lightbox-stage");
        stage.addEventListener("click", (event) => {
            if (!event.target.closest("img")) return;
            const zoomed = !stage.classList.contains("is-zoomed");
            setLightboxZoom(zoomed);
            if (zoomed) panLightbox(event);
        });
        stage.addEventListener("pointermove", panLightbox);
        $("#lightbox-zoom")?.addEventListener("click", () => setLightboxZoom(!stage.classList.contains("is-zoomed")));
        $("#lightbox-fullscreen")?.addEventListener("click", async () => {
            const on = !lightbox.classList.contains("is-fullscreen");
            setLightboxFullscreen(on);
            try {
                if (on && lightbox.requestFullscreen && !document.fullscreenElement) await lightbox.requestFullscreen();
                if (!on && document.fullscreenElement) await document.exitFullscreen();
            } catch {
                // The CSS layout already fills the viewport when native full screen is unavailable.
            }
        });
        document.addEventListener("fullscreenchange", () => {
            if (!document.fullscreenElement && lightbox.classList.contains("is-fullscreen")) setLightboxFullscreen(false);
        });
        lightbox.addEventListener("close", () => {
            setLightboxZoom(false);
            setLightboxFullscreen(false);
            if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
        });
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
            const designKeys = new Set(product.design_field_keys || []);
            state.forms = {
                general: DynamicForm.create(generalFormContainer, product, { onChange: onFormChange, idPrefix: "opt" }),
                generalPackage: DynamicForm.create(generalPackageContainer, product, {
                    onChange: onFormChange,
                    idPrefix: "pkg",
                    fieldFilter: (field) => packageKeys.has(field.field_key),
                }),
                // A shop design order keeps only the print specifications (no artwork questions or uploads).
                generalDesign: DynamicForm.create(generalDesignContainer, product, {
                    onChange: onFormChange,
                    idPrefix: "dsgopt",
                    fieldFilter: (field) => designKeys.has(field.field_key),
                }),
            };
        }
    }

    function activeForms() {
        const forms = state.forms;
        if (!state.invitation) {
            const chosen = { package: forms.generalPackage, design: forms.generalDesign }[state.mode] || forms.general;
            return [chosen].filter(Boolean);
        }
        if (state.mode === "package") return [forms.options, forms.event, forms.upload].filter(Boolean);
        if (state.mode === "design") return [forms.basics, forms.options].filter(Boolean);
        return [forms.basics, forms.options, forms.design, forms.event, forms.upload].filter(Boolean);
    }

    function collectValues() {
        const result = {};
        for (const dynamicForm of activeForms()) Object.assign(result, dynamicForm.values());
        return result;
    }

    function fileCount() {
        return activeForms().reduce((total, dynamicForm) => total + dynamicForm.fileCount(), 0)
            + (state.invitation || state.mode === "design" ? 0 : state.referenceFiles.length);
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
        const customizable = customizeAllowed(product);
        const designCount = customizable ? (product.designs || []).length : 0;
        const packageText = `${packages.length} package${packages.length === 1 ? "" : "s"} available`;
        copy.append(el("p", "field-hint", packages.length
            ? (customizable ? `${packageText}, or customize your own order.` : `${packageText}. This item is ordered through its packages only.`)
            : (customizable
                ? "Customize your own order; the shop has not published packages for this item yet."
                : "This item is not available to order right now. Please choose another product or contact the shop.")));
        if (designCount) copy.append(el("p", "field-hint", `${designCount} shop design${designCount === 1 ? "" : "s"} to choose from — just add your details.`));
        if (product.standard_size_label) copy.append(el("p", "standard-size-note", `Standard size: ${product.standard_size_label}`));
        const priceList = sizePriceTable(product);
        if (priceList) copy.append(priceList);
        if (product.min_quantity > 1) copy.append(el("span", "min-badge", `Minimum order: ${product.min_quantity.toLocaleString()} pcs`));
        selectedProduct.append(gallery(productImages(product), product.name, "selected-product-gallery"), copy);
    }

    /** The standard sizes and their prices per piece (Tarpaulin), as a small table. Null for other products. */
    function sizePriceTable(product, { caption = "Standard sizes & prices" } = {}) {
        const sizes = product.pricing_mode === "size" ? (product.standard_sizes || []) : [];
        if (!sizes.length) return null;
        const table = el("table", "size-price-table");
        const head = el("caption", "", caption);
        const headRow = el("tr");
        [["Size", "col"], ["Price per piece", "col"]].forEach(([text]) => {
            const cell = el("th", "", text);
            cell.scope = "col";
            headRow.append(cell);
        });
        const thead = el("thead");
        thead.append(headRow);
        const body = el("tbody");
        sizes.forEach((size) => {
            const row = el("tr");
            const name = el("th", "", size.label);
            name.scope = "row";
            row.append(name, el("td", "", money(size.price)));
            body.append(row);
        });
        const custom = el("tr", "size-price-custom");
        const customName = el("th", "", "Custom size");
        customName.scope = "row";
        custom.append(customName, el("td", "", "Request a quote"));
        body.append(custom);
        table.append(head, thead, body);
        return table;
    }

    /** Whether the shop accepts customized orders (quotation requests) for a product. Older data: yes. */
    function customizeAllowed(product = state.product) {
        return product?.allow_customize !== false;
    }

    /** A product can be ordered when it offers packages or accepts customized orders. */
    function productOrderable(product = state.product) {
        return Boolean(product) && (customizeAllowed(product) || (product.packages || []).length > 0);
    }

    function selectProduct(productId) {
        const product = currentCategory()?.products.find((item) => item.id === productId);
        if (!product || product === state.product) return;
        state.product = product;
        state.invitation = isInvitationProduct(product);
        state.packages = Array.isArray(product.packages) ? product.packages : [];
        state.package = null;
        state.designs = customizeAllowed(product) && Array.isArray(product.designs) ? product.designs : [];
        state.design = null;
        state.designStyle = "";
        state.ownDesign = false;
        designError.textContent = "";
        state.quote = null;
        state.quoteSequence += 1;
        window.clearTimeout(state.quoteTimer);
        packageError.textContent = "";
        modeError.textContent = "";
        $("#product-error").textContent = "";
        setError("");

        renderSelectedProduct(product);
        syncUploadLimit(product);
        const note = pricingNote.querySelector("div");
        const priced = product.pricing_mode === "size" ? Boolean((product.standard_sizes || []).length) : product.pricing_mode !== "quote" && product.base_price !== null;
        note.replaceChildren(
            el("strong", "", state.packages.length ? (customizeAllowed(product) ? "Packages or a customized order" : "Packages") : (priced ? "Prices set by the shop" : "Request a quote")),
            el("p", "", state.packages.length
                ? (customizeAllowed(product)
                    ? "Choose a published package with a fixed price, or customize your order. The total is calculated from the quantity and the shop confirms the final amount before payment."
                    : "Choose a published package with a fixed price. The total is calculated from the quantity and the shop confirms the final amount before payment.")
                : priced && product.pricing_mode === "size"
                    ? "Choose a standard size: its published price × the quantity is your total. Custom sizes are quoted by the shop."
                : priced
                    ? "You will see the price per piece and a live total as you choose options. The administrator confirms the final amount before payment is requested."
                    : "This product does not have a published price yet. Submit your options and the administrator will send a quotation."),
        );

        $("#step2-product").textContent = product.name;
        $("#step2-general-product").textContent = product.name;
        const largeFormat = product.category_id === "large-format" || product.pricing_mode === "area";
        $("#customize-mode-title").textContent = state.invitation ? "Customize Your Own Invitation" : "Customize Order";
        $("#order-mode-lead").textContent = customizeAllowed(product)
            ? "Choose a ready-made package with a fixed price and inclusions, or customize the order yourself. The shop confirms the final amount before payment is requested."
            : "Choose one of the ready-made packages with a fixed price and inclusions. The shop confirms the final amount before payment is requested.";
        $("#customize-mode-note").textContent = !customizeAllowed(product)
            ? "Not available for this item. Please choose one of its packages."
            : state.designs.length
            ? `Choose one of the shop's ${state.designs.length} design${state.designs.length === 1 ? "" : "s"} and add your details, or send your own design.`
            : state.invitation
            ? "Set the printing, finish, envelope, quantity, design direction, and event details yourself."
            : product.pricing_mode === "size"
                ? "Choose a standard size from the price list, or a custom size for a quotation, then upload your design."
            : largeFormat
                ? "Enter your exact size, material, and requirements. Priced per square foot or by quotation."
                : "Choose your own size, material, and details. Priced from the shop's rates or by quotation.";
        $("#package-mode-note").textContent = state.packages.length
            ? `${state.packages.length} package${state.packages.length === 1 ? "" : "s"} with fixed prices, inclusions, and photos.`
            : "No packages are published for this item yet.";

        createForms(product);
        destroyPersonalizeForm();
        state.referenceFiles = [];
        renderReferenceFiles();
        renderPackages();
        renderDesigns();
        // With only one way to order, choose it for the customer.
        const choices = [state.packages.length ? "package" : "", customizeAllowed(product) ? "customize" : ""].filter(Boolean);
        const initialMode = choices.length === 1 ? choices[0] : "";
        setMode(initialMode, { silent: true });
        configureLegacyEvent();
        setSequence(sequenceKind());

        if (!state.sequence.some((item) => item.step === state.activeStep)) showStep(1, false);
        else syncNextButton();

        syncSelectionUrl();
    }

    /** Keep the chosen service, product, and package in the address bar (refresh or sign-in keeps them). */
    function syncSelectionUrl() {
        if (!state.product) return;
        const url = new URL(window.location.href);
        url.searchParams.set("service", state.product.category_id);
        url.searchParams.set("design", state.product.id);
        if (state.mode === "package" && state.package) url.searchParams.set("package", state.package.id);
        else url.searchParams.delete("package");
        if (state.mode === "design" && state.design) url.searchParams.set("shop_design", state.design.id);
        else url.searchParams.delete("shop_design");
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
            // Each package has its own minimum order (the pieces its base price covers).
            const minimum = packageMinimum(pkg);
            if (pkg.add_on_price === null) {
                min = minimum;
                max = minimum;
                preferred = min;
                input.disabled = true;
                hint.textContent = `This package is ordered at exactly ${min.toLocaleString()} pieces for ${money(pkg.price)}.`;
            } else {
                min = minimum;
                max = Math.max(minimum, product.max_quantity);
                preferred = min;
                hint.textContent = `Minimum order: ${min.toLocaleString()} pcs for ${money(pkg.price)}. Each additional piece adds ${money(pkg.add_on_price)}${pkg.add_on_label ? ` (${pkg.add_on_label.toLowerCase()})` : ""}.`;
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

    /** A typed quantity outside the allowed range snaps back to it (never below the minimum order). */
    function clampQuantity(event) {
        const input = event.currentTarget;
        const min = Number(input.min) || 1;
        const max = Number(input.max) || 50000;
        const value = Math.round(Number(input.value));
        const clamped = Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : min;
        if (String(clamped) !== input.value) {
            input.value = String(clamped);
            onQuantityChange();
        }
    }

    function packageMinimum(pkg) {
        return pkg.minimum_quantity ?? pkg.included_quantity;
    }

    /** Inclusions with their descriptions (older packages have names only). */
    function inclusionList(pkg) {
        const details = Array.isArray(pkg.inclusion_details) && pkg.inclusion_details.length
            ? pkg.inclusion_details
            : (pkg.inclusions || []).map((name) => ({ name, description: "" }));
        if (!details.length) return null;
        const list = el("ul", "package-inclusions");
        details.forEach((item) => {
            const entry = el("li", "", item.name);
            if (item.description) entry.append(el("span", "package-inclusion-description", ` — ${item.description}`));
            list.append(entry);
        });
        return list;
    }

    /** Minimum order, base package price, and additional piece price, as published by the shop. */
    function packageTerms(pkg) {
        const terms = el("dl", "package-terms");
        const add = (label, value) => {
            const row = el("div", "package-term");
            row.append(el("dt", "", label), el("dd", "", value));
            terms.append(row);
        };
        add("Minimum order", `${packageMinimum(pkg).toLocaleString()} pcs`);
        add("Package price", money(pkg.price));
        add(pkg.add_on_label || "Additional piece", pkg.add_on_price === null ? "Not available (fixed quantity)" : `${money(pkg.add_on_price)} each`);
        return terms;
    }

    // ---------------------------------------------------------------- ordering option (package or customize)

    function updateModeButtons() {
        document.querySelectorAll("[data-order-mode]").forEach((button) => {
            // The shop design flow is Customize Order for products that have designs.
            const active = button.dataset.orderMode === (state.mode === "design" ? "customize" : state.mode);
            button.classList.toggle("selected", active);
            button.setAttribute("aria-pressed", String(active));
        });
        const packageButton = $('[data-order-mode="package"]');
        packageButton.disabled = !state.packages.length;
        packageButton.setAttribute("aria-disabled", String(!state.packages.length));
        const customizeButton = $('[data-order-mode="customize"]');
        const customizeOff = !customizeAllowed();
        customizeButton.disabled = customizeOff;
        customizeButton.setAttribute("aria-disabled", String(customizeOff));
        customizeButton.hidden = customizeOff;
    }

    /** The Next button stays locked on the ordering step while a package is required but not chosen. */
    function syncNextButton() {
        const next = $("#btn-next");
        if (!next || !state.product) return;
        const needsPackage = state.activeStep === 2 && state.mode === "package" && !state.package;
        const needsDesign = state.activeStep === 9 && state.mode === "design" && !state.design;
        next.disabled = needsPackage || needsDesign;
        next.title = needsPackage ? "Select a package to continue." : needsDesign ? "Select a design to continue." : "";
    }

    /** Which step sequence the chosen product and ordering option use. */
    function sequenceKind() {
        if (state.mode === "design") return state.invitation ? "designInvitation" : "designGeneral";
        return state.invitation ? "invitation" : "general";
    }

    function setMode(mode, { silent = false } = {}) {
        if (mode === "package" && !state.packages.length) mode = customizeAllowed() ? "customize" : "";
        if (mode === "design" && !state.designs.length) mode = customizeAllowed() ? "customize" : (state.packages.length ? "package" : "");
        if (mode === "customize" && !customizeAllowed()) mode = state.packages.length ? "package" : "";
        // Customize Order starts with the shop's designs whenever the product has any, unless the customer
        // chose to send their own design.
        if (mode === "customize" && state.designs.length && !state.ownDesign) mode = "design";
        state.mode = mode;
        if (mode !== "package") state.package = null;
        if (mode !== "design") {
            state.design = null;
            destroyPersonalizeForm();
        }
        if (!silent) {
            modeError.textContent = "";
            if (mode === "package") packageError.textContent = "";
            if (mode === "design") designError.textContent = "";
        }
        const design = mode === "design";
        updateModeButtons();
        packageSection.hidden = mode !== "package";
        renderPackages();
        renderDesigns();
        customizeSection.hidden = mode !== "customize" && !design;
        $("#invitation-design-block").hidden = design;
        generalFormContainer.hidden = mode === "package" || design;
        const packageFormCount = state.forms.generalPackage?.fieldCount || 0;
        generalPackageContainer.hidden = mode !== "package" || !packageFormCount;
        generalDesignContainer.hidden = !design || !(state.forms.generalDesign?.fieldCount);
        // A shop design order never takes design or reference files.
        $("#general-artwork-field").hidden = design;
        uploadInput.disabled = design;
        $("#general-step-title").textContent = mode === "package" ? "Package details for" : design ? "Print details for" : "Customize your";
        $("#general-step-lead").textContent = mode === "package"
            ? (packageFormCount
                ? "Your package fixes the specifications. Add the personalization details and the quantity you need."
                : "Your package fixes the specifications. Set the quantity and add any special instructions.")
            : design
                ? "Your design is chosen. Choose the print specifications and the quantity; the shop confirms the final amount with a quotation."
                : "Only the options for this product are shown. Fields marked “optional” can be left blank.";
        configureLead.textContent = mode === "package"
            ? (state.package
                ? "Choose the paper finish and envelope, then set the total pieces. The package price is fixed; extra pieces use the package's add-on rate."
                : "Choose a package to open the package order form.")
            : design
                ? "Your design and details are set. Choose the printing, paper finish, envelope, and total pieces; the shop confirms the final amount with a quotation."
                : "Set the printing, finish, envelope, and quantity, and upload your design. Required fields are marked; the shop confirms the final amount before payment.";
        configureLegacyEvent();
        renderSelectedPackageSummary();
        renderSelectedDesign();
        renderOwnDesignNotes();
        setSequence(sequenceKind());
        if (!silent && state.product) showStep(state.sequence.some((item) => item.step === state.activeStep) ? state.activeStep : 2, false);
        state.quote = null;
        configureQuantity({ reset: true });
        scheduleQuote();
        syncNextButton();
        syncSelectionUrl();
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
        fragment.append(packageTerms(pkg));
        const inclusions = inclusionList(pkg);
        if (inclusions) fragment.append(inclusions);
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
            body.append(packageTerms(pkg));
            const meta = [];
            if (pkg.size) meta.push(pkg.size);
            if (pkg.material) meta.push(pkg.material);
            if (meta.length) body.append(el("p", "package-meta", meta.join(" · ")));
            const inclusions = inclusionList(pkg);
            if (inclusions) body.append(inclusions);
            body.append(el("p", "package-addon", pkg.add_on_price === null
                ? `Ordered at exactly ${packageMinimum(pkg).toLocaleString()} pieces.`
                : `You can order ${packageMinimum(pkg).toLocaleString()} pieces or more; each piece above ${packageMinimum(pkg).toLocaleString()} adds ${money(pkg.add_on_price)}.`));
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
        syncSelectionUrl();
        if (state.activeStep === REVIEW_STEP) renderSummary();
    }

    // ---------------------------------------------------------------- shop designs (gallery, selection, personalization)

    function designItems(designs) {
        return designs.map((design) => ({
            src: design.image,
            alt: `${design.name} — design preview`,
            caption: `${design.name}${design.style ? ` · ${design.style}` : ""}${design.is_sample ? " · Sample design for illustration" : ""}`,
        }));
    }

    function designBadges(design) {
        const badges = el("span", "design-badges");
        if (design.style) badges.append(el("span", "design-tag", design.style));
        if (design.is_sample) {
            const sample = el("span", "design-tag design-tag-sample", "Sample design");
            sample.title = "A temporary sample shown until the shop adds its own designs.";
            badges.append(sample);
        }
        return badges;
    }

    function visibleDesigns() {
        return state.designs.filter((design) => !state.designStyle || design.style === state.designStyle);
    }

    function renderDesignFilter() {
        const styles = [...new Set(state.designs.map((design) => design.style).filter(Boolean))];
        designFilter.hidden = styles.length < 2;
        if (styles.length < 2) {
            designFilter.replaceChildren();
            return;
        }
        designFilter.replaceChildren(...["", ...styles].map((style) => {
            const button = el("button", `design-filter-chip${state.designStyle === style ? " selected" : ""}`, style || "All designs");
            button.type = "button";
            button.setAttribute("aria-pressed", String(state.designStyle === style));
            button.addEventListener("click", () => {
                state.designStyle = style;
                renderDesigns();
            });
            return button;
        }));
    }

    function renderDesigns() {
        designList.replaceChildren();
        renderDesignFilter();
        if (!state.designs.length) {
            designList.append(el("p", "package-empty", "The shop has not published designs for this item yet. Choose another ordering option."));
            return;
        }
        const designs = visibleDesigns();
        designs.forEach((design, index) => {
            const selected = state.design?.id === design.id;
            const card = el("article", `design-card${selected ? " selected" : ""}`);
            const view = el("button", "design-card-media");
            view.type = "button";
            view.setAttribute("aria-label", `View a larger preview of ${design.name}`);
            const image = el("img");
            image.src = design.image;
            image.alt = `${design.name} design preview`;
            image.loading = "lazy";
            image.decoding = "async";
            view.append(image, el("span", "design-card-zoom", "View larger"));
            if (selected) view.append(el("span", "design-card-check", "✓ Selected"));
            view.addEventListener("click", () => openLightbox(designItems(designs), index));
            const body = el("div", "design-card-body");
            body.append(el("h4", "design-card-name", design.name), designBadges(design));
            if (design.description) body.append(el("p", "design-card-description", design.description));
            const choose = el("button", `btn ${selected ? "btn-dark" : "btn-ghost-dark"} btn-small design-select`, selected ? "Selected ✓" : "Select design");
            choose.type = "button";
            choose.setAttribute("aria-pressed", String(selected));
            choose.addEventListener("click", () => selectDesign(design));
            body.append(choose);
            card.append(view, body);
            designList.append(card);
        });
        // Last in the gallery: the customer can still send their own design.
        designList.append(ownDesignCard());
    }

    /** The gallery's "Use my own design" choice: opens the product's customize form with its uploads. */
    function ownDesignCard() {
        const card = el("article", "design-card design-card-own");
        const icon = el("div", "design-card-media design-own-media");
        icon.append(el("span", "design-own-icon", "↥"), el("strong", "", "Your own design"));
        const body = el("div", "design-card-body");
        body.append(
            el("h4", "design-card-name", "Use my own design"),
            el("p", "design-card-description", "Upload your own design file instead of choosing one of the shop's designs."),
        );
        const choose = el("button", "btn btn-ghost-dark btn-small design-select", "Use my own design");
        choose.type = "button";
        choose.addEventListener("click", useOwnDesign);
        body.append(choose);
        card.append(icon, body);
        return card;
    }

    function useOwnDesign() {
        state.ownDesign = true;
        setMode("customize", { silent: true });
        // Open the first step of the product's customize form (design direction and uploads).
        showStep(state.invitation ? 3 : 8);
    }

    function useShopDesigns() {
        state.ownDesign = false;
        setMode("design", { silent: true });
        showStep(9);
    }

    /** In the own-design form: a way back to the shop's designs. */
    function renderOwnDesignNotes() {
        const show = state.mode === "customize" && state.ownDesign && state.designs.length > 0;
        for (const host of [$("#general-own-design-note"), $("#invitation-own-design-note")]) {
            if (!host) continue;
            host.hidden = !show;
            host.replaceChildren();
            if (!show) continue;
            const back = el("button", "text-button", `Choose one of the shop's ${state.designs.length} designs instead`);
            back.type = "button";
            back.addEventListener("click", useShopDesigns);
            host.append(el("span", "", "You are sending your own design. "), back);
        }
    }

    function destroyPersonalizeForm() {
        state.personalizeForm?.destroy();
        state.personalizeForm = null;
    }

    function selectDesign(design, { scroll = true } = {}) {
        const changed = state.design?.id !== design.id;
        state.design = design;
        designError.textContent = "";
        if (changed || !state.personalizeForm) {
            // Keep what the customer already typed for questions the new design also asks.
            const previous = state.personalizeForm?.values() || {};
            destroyPersonalizeForm();
            const fields = design.fields.map((field) => ({
                ...field,
                default_value: previous[field.field_key] !== undefined ? String(previous[field.field_key]) : field.default_value,
            }));
            state.personalizeForm = DynamicForm.create(personalizeContainer, { id: design.id, name: design.name, pricing_mode: "quote", base_price: null, fields }, {
                onChange: onPersonalizeChange,
                idPrefix: "dsg",
            });
        }
        renderDesigns();
        renderSelectedDesign();
        state.quote = null;
        scheduleQuote();
        syncNextButton();
        syncSelectionUrl();
        if (state.activeStep === REVIEW_STEP) renderSummary();
        // The details form opens right below the gallery.
        if (scroll && changed && state.activeStep === 9) $("#design-details-title").scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function onPersonalizeChange() {
        renderDesignDetails();
        if (state.activeStep === REVIEW_STEP) renderSummary();
    }

    function designDetailRows() {
        return state.personalizeForm ? state.personalizeForm.summary() : [];
    }

    /** The chosen design (select the picture to see it larger), with a Change design button. */
    function designPreviewNode(design, { compact = false } = {}) {
        const wrapper = el("div", `design-preview-card${compact ? " compact" : ""}`);
        const view = el("button", "design-preview-media");
        view.type = "button";
        view.setAttribute("aria-label", `View a larger preview of ${design.name}`);
        const image = el("img");
        image.src = design.image;
        image.alt = `${design.name} design preview`;
        view.append(image);
        view.addEventListener("click", () => openLightbox(designItems([design])));
        const copy = el("div", "design-preview-copy");
        copy.append(el("span", "order-step-kicker", "Selected design"), el("strong", "design-preview-name", design.name), designBadges(design));
        if (!compact && design.description) copy.append(el("p", "field-hint", design.description));
        const change = el("button", "btn btn-ghost-dark btn-small", "Change design");
        change.type = "button";
        change.addEventListener("click", () => {
            showStep(9, false);
            designSection.scrollIntoView({ behavior: "smooth", block: "start" });
        });
        copy.append(change);
        wrapper.append(view, copy);
        return wrapper;
    }

    /** The customer's details listed next to the design: the preview of what goes on it. */
    function renderDesignDetails() {
        const host = designPreview.querySelector(".design-details-preview");
        if (!host) return;
        const rows = designDetailRows();
        host.replaceChildren(el("strong", "", "Your details on this design"));
        if (!rows.length) {
            host.append(el("p", "field-hint", "Your details appear here as you type."));
            return;
        }
        const list = el("dl", "design-details-list");
        rows.forEach((row) => {
            const item = el("div");
            item.append(el("dt", "", row.label), el("dd", "", row.value));
            list.append(item);
        });
        host.append(list, el("p", "field-hint", "The shop places these details on the design and sends a draft for your approval."));
    }

    function renderSelectedDesign() {
        const design = state.mode === "design" ? state.design : null;
        // The details form opens below the gallery once a design is chosen.
        $("#design-personalize").hidden = !design;
        $("#design-details-hint").hidden = Boolean(design);
        designPreview.replaceChildren();
        if (design) {
            designPreview.append(designPreviewNode(design), el("div", "design-details-preview"));
            renderDesignDetails();
        }
        for (const target of [$("#general-design-summary"), $("#invitation-design-summary")]) {
            target.replaceChildren();
            target.hidden = !design;
            if (design) target.append(designPreviewNode(design, { compact: true }));
        }
    }

    // ---------------------------------------------------------------- general products: optional event details

    function configureLegacyEvent() {
        // Invitation event details are catalog fields in the "event" section.
        // A shop design order collects its details (names, date, venue) in the Personalize step.
        const mode = state.invitation || state.mode === "design" ? "none" : (state.product?.event_mode || "none");
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
        if (!state.mode || (state.mode === "package" && !state.package) || (state.mode === "design" && !state.design)) {
            state.quote = null;
            reference.total.textContent = "—";
            reference.lines.replaceChildren();
            reference.status.textContent = state.mode === "package" ? "Select a package to see its total."
                : state.mode === "design" ? "Select a design to see the estimate." : "Choose how you would like to order.";
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
        if (state.mode === "design" && state.design) body.design_id = state.design.id;
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

    /** "₱300 + (25 − 20) × ₱15 = ₱375", from the server's calculation. */
    function packageFormula(quote) {
        const total = money(quote.estimated_total);
        if (quote.add_on_price === null || !quote.additional_quantity) {
            return `${quote.quantity.toLocaleString()} pcs · base package price ${money(quote.package_price)}${quote.estimated_total !== quote.package_price ? ` · total ${total}` : ""}`;
        }
        const formula = `${money(quote.package_price)} + (${quote.quantity.toLocaleString()} − ${quote.minimum_quantity.toLocaleString()}) × ${money(quote.add_on_price)} = ${money(quote.package_price + quote.additional_cost)}`;
        return quote.estimated_total !== Math.round((quote.package_price + quote.additional_cost) * 100) / 100 ? `${formula} · with options ${total}` : formula;
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
                ? packageFormula(quote)
                : quote.price_basis === "standard_size"
                    ? `${quote.standard_size?.label || "Standard size"}: ${money(quote.unit_price)} per piece × ${quote.quantity.toLocaleString()} pcs · published price`
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

    /**
     * A package with a complete published total is a fixed-price order: the customer picks the payment option now.
     * Customized orders (and packages whose options need a quote) are priced by the shop's quotation, and the
     * payment option is chosen in the customer portal after the quotation is accepted.
     */
    function isFixedPrice() {
        const quote = state.quote;
        // A standard size from the published price list (Tarpaulin): size price × quantity is the order total.
        if (state.mode !== "package") return Boolean(quote && quote.price_basis === "standard_size" && quote.estimated_total > 0);
        if (!state.package) return false;
        if (!quote) return true; // the package total is still loading
        return quote.estimated_total !== null && quote.estimated_total > 0 && !quote.requires_quote;
    }

    function fixedTotalLabel() {
        return state.mode === "package" ? "Package total" : "Price-list total";
    }

    function syncPaymentSection() {
        const fixed = isFixedPrice();
        $("#payment-options").hidden = !fixed;
        $("#payment-after-quote").hidden = fixed;
    }

    /** The 50% down payment and remaining balance, as calculated by the server's price quote. */
    function paymentRows() {
        if (!isFixedPrice()) {
            return [
                ["Payment", "50% Down Payment"],
                ["Total", `Set by the shop's quotation (you accept or decline it first)${fulfillmentMethod() === "delivery" && selectedArea() ? ` plus the ${money(deliveryFee())} delivery fee` : ""}`],
                ["Down payment (50%)", "Half of the accepted quotation, verified by the shop"],
            ];
        }
        const quote = state.quote || {};
        const rows = [["Payment", "50% Down Payment"]];
        if (typeof quote.down_payment !== "number" || typeof quote.estimated_total !== "number") {
            rows.push([fixedTotalLabel(), "Calculating…"]);
            return rows;
        }
        const fee = deliveryFee();
        const total = Math.round((quote.estimated_total + fee) * 100) / 100;
        const down = Math.round(total * 50) / 100;
        rows.push([fixedTotalLabel(), money(quote.estimated_total)]);
        if (fulfillmentMethod() === "delivery") rows.push(["Delivery fee", selectedArea() ? money(fee) : "Choose your delivery area"]);
        rows.push(["Order total", money(total)]);
        rows.push(["Down payment (50%)", money(down)]);
        rows.push(["Remaining balance (50%)", money(Math.round((total - down) * 100) / 100)]);
        return rows;
    }

    function renderPaymentBreakdown() {
        if (!paymentBreakdown) return;
        syncPaymentSection();
        paymentBreakdown.replaceChildren();
        if (!isFixedPrice()) return;
        paymentRows().forEach(([label, value]) => {
            const row = el("div", "payment-breakdown-row");
            row.append(el("dt", "", label), el("dd", "", value));
            paymentBreakdown.append(row);
        });
        if (state.quote?.estimated_total !== null && state.quote?.estimated_total !== undefined) {
            paymentBreakdown.append(el("p", "field-hint", `${state.mode === "package" ? "From the package's published price and add-on rate." : "From the shop's published size price list (price per piece × quantity)."} Pay the down payment after the shop approves your order; it is confirmed once the shop verifies your e-wallet payment.`));
        }
    }


    // ---------------------------------------------------------------- delivery or store pickup

    const deliveryFields = $("#delivery-fields");
    const deliveryInputs = [...deliveryFields.querySelectorAll("[data-delivery-field]")];
    const pickupFields = $("#pickup-fields");
    const areaSelect = $("#delivery-area");
    const pickupSelect = $("#pickup-location");
    // Delivery areas (fee and estimated courier period) and pickup locations configured by the shop. The
    // server calculates the delivery fee again when the order is submitted.
    let fulfillmentOptions = { delivery_enabled: true, areas: [], pickup_locations: [], delivery_disclaimer: "" };
    fetch("/api/fulfillment-options", { credentials: "same-origin" })
        .then((response) => (response.ok ? response.json() : null))
        .then((options) => {
            if (!options) return;
            fulfillmentOptions = options;
            areaSelect.replaceChildren(new Option("Choose your area", ""), ...options.areas.map((area) => new Option(`${area.label} — ${money(area.fee)} delivery fee`, area.id)));
            const deliveryRadio = form.querySelector('input[name="fulfillment-method"][value="delivery"]');
            if (!options.delivery_enabled && deliveryRadio) {
                deliveryRadio.disabled = true;
                deliveryRadio.closest("label").title = "Delivery is not available right now.";
            }
            $("#pickup-location-field").hidden = options.pickup_locations.length < 2;
            pickupSelect.replaceChildren(...options.pickup_locations.map((item) => new Option(`${item.name} — ${item.address}`, item.id)));
            $("#pickup-date").min = new Date().toISOString().slice(0, 10);
            // Without configured areas the server works the area out from the province.
            if (!options.areas.length) {
                areaSelect.dataset.optional = "true";
                areaSelect.closest(".field").hidden = true;
            }
            syncFulfillment();
        })
        .catch(() => {
            areaSelect.dataset.optional = "true";
            areaSelect.closest(".field").hidden = true;
            syncFulfillment();
        });

    function selectedArea() {
        return fulfillmentOptions.areas.find((area) => area.id === areaSelect.value) || null;
    }

    /** The delivery fee shown before submitting (the server recalculates it from the address). */
    function deliveryFee() {
        return fulfillmentMethod() === "delivery" ? Number(selectedArea()?.fee || 0) : 0;
    }

    function estimateText(area) {
        if (!area) return "";
        const low = area.estimate_min_days;
        const high = area.estimate_max_days;
        if (low === null && high === null) return "Estimated delivery period confirmed by the shop";
        if (low === null || high === null || low === high) return `About ${high ?? low} days (estimate)`;
        return `${low}–${high} days (estimate)`;
    }

    function renderDeliveryQuote() {
        const area = selectedArea();
        $("#delivery-quote").textContent = area
            ? `Delivery fee ${money(area.fee)} · ${estimateText(area)}. ${fulfillmentOptions.delivery_disclaimer || ""}`
            : "Choose the area of the delivery address to see the delivery fee and estimated delivery period.";
        const location = fulfillmentOptions.pickup_locations.find((item) => item.id === pickupSelect.value) || fulfillmentOptions.pickup_locations[0];
        $("#pickup-info").textContent = [location ? `Pickup at ${location.name}, ${location.address}${location.hours ? ` (${location.hours})` : ""}.` : "Pickup at the shop.", location?.instructions, fulfillmentOptions.pickup_instructions, "Orders are released only when fully paid; bring the pickup code shown in your customer portal."].filter(Boolean).join(" ");
    }

    function fulfillmentMethod() {
        return form.querySelector('input[name="fulfillment-method"]:checked')?.value || "pickup";
    }

    /** Address fields are shown, enabled, and required only for delivery; pickup needs no address. */
    function syncFulfillment() {
        const delivery = fulfillmentMethod() === "delivery";
        deliveryFields.hidden = !delivery;
        pickupFields.hidden = delivery;
        renderDeliveryQuote();
        deliveryInputs.forEach((input) => {
            input.disabled = !delivery;
            input.required = delivery && input.dataset.optional !== "true";
        });
        if (delivery) {
            // Start from the contact details; the recipient can be someone else.
            if (!$("#delivery-name").value.trim()) $("#delivery-name").value = $("#name").value.trim();
            if (!$("#delivery-phone").value.trim()) $("#delivery-phone").value = $("#phone").value.trim();
        }
    }

    function fulfillmentPayload() {
        if (fulfillmentMethod() !== "delivery") {
            const pickup = { method: "pickup" };
            if (pickupSelect.value) pickup.pickup_location_id = pickupSelect.value;
            if ($("#pickup-date").value) pickup.preferred_pickup_date = $("#pickup-date").value;
            if ($("#pickup-notes").value.trim()) pickup.pickup_notes = $("#pickup-notes").value.trim();
            return pickup;
        }
        const result = { method: "delivery" };
        deliveryInputs.forEach((input) => { result[input.dataset.deliveryField] = input.value.trim(); });
        return result;
    }

    function fulfillmentRows() {
        if (fulfillmentMethod() !== "delivery") {
            const location = fulfillmentOptions.pickup_locations.find((item) => item.id === pickupSelect.value);
            return [
                ["Method", "Store Pickup — no delivery address needed"],
                location ? ["Pickup location", `${location.name}, ${location.address}`] : ["Pickup location", "The shop"],
                ["Preferred pickup date", $("#pickup-date").value || "Not specified"],
            ];
        }
        const area = selectedArea();
        return [
            ["Method", "Delivery"],
            ...deliveryInputs.filter((input) => input !== areaSelect).map((input) => [input.closest(".field").querySelector("label").textContent.replace("(optional)", "").trim(), input.value.trim()]),
            ["Delivery area", area ? area.label : "Not chosen"],
            ["Delivery fee", area ? money(area.fee) : "—"],
            ["Estimated delivery", area ? `${estimateText(area)} — not a guaranteed arrival date` : "—"],
        ];
    }

    form.querySelectorAll('input[name="fulfillment-method"]').forEach((input) => input.addEventListener("change", () => {
        syncFulfillment();
        renderPaymentBreakdown();
        if (state.activeStep === REVIEW_STEP) renderSummary();
    }));
    [areaSelect, pickupSelect].forEach((select) => select.addEventListener("change", () => {
        renderDeliveryQuote();
        renderPaymentBreakdown();
        if (state.activeStep === REVIEW_STEP) renderSummary();
    }));
    syncFulfillment();

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
        const limit = DynamicForm.uploadLimit(state.product);
        const oversized = chosen.find((file) => file.size > limit);
        if (oversized) return setError(`${oversized.name} is ${DynamicForm.formatBytes(oversized.size)}. Each file must be no larger than ${DynamicForm.limitText(limit)}.`);
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
        const kind = state.mode === "package" ? "Package" : state.mode === "design" ? "Customize Order (shop design)" : (state.invitation ? "Customized invitation" : "Customized order");
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
                ["Inclusions", (pkg.inclusion_details?.length ? pkg.inclusion_details : pkg.inclusions.map((name) => ({ name, description: "" })))
                    .map((item) => (item.description ? `${item.name} (${item.description})` : item.name)).join("; ")],
                ["Minimum order", `${packageMinimum(pkg).toLocaleString()} pcs`],
                ["Additional piece", pkg.add_on_price === null
                    ? "Not available — this package has a fixed quantity"
                    : `${pkg.add_on_label || "Additional piece"}: ${money(pkg.add_on_price)} each`],
            ]);
        }

        if (state.mode === "design" && state.design) {
            const design = state.design;
            const preview = el("div", "summary-design");
            preview.append(designPreviewNode(design));
            appendGroup("Selected design", [
                ["Design", design.name],
                ["Style", design.style],
                ["Note", design.is_sample ? "Sample design shown for illustration; the shop confirms the final artwork in your draft." : ""],
            ], preview);
            appendGroup("Your design details", designDetailRows().map((row) => [row.label, row.value]));
        }

        if (state.invitation && state.mode === "design") {
            appendGroup("Basic specifications", (state.forms.basics?.summary() || []).map((row) => [row.label, row.value]));
            appendGroup("Paper finish & envelope", (state.forms.options?.summary() || []).map((row) => [row.label, row.value]));
        } else if (state.invitation) {
            if (state.mode === "customize") {
                appendGroup("Basic specifications", (state.forms.basics?.summary() || []).map((row) => [row.label, row.value]));
                appendGroup("Design customization", (state.forms.design?.summary() || []).map((row) => [row.label, row.value]));
            }
            appendGroup("Paper finish & envelope", (state.forms.options?.summary() || []).map((row) => [row.label, row.value]));
            appendGroup("Event details", (state.forms.event?.summary() || []).map((row) => [row.label, row.value]));
            appendGroup("Photos & references", (state.forms.upload?.summary() || []).map((row) => [row.label, row.value]));
        } else {
            const dynamicForm = activeForms()[0];
            const specs = dynamicForm?.summary() || [];
            if (state.quote?.area_sqft) specs.push({ label: "Print area", value: `${state.quote.area_sqft} sq ft per piece` });
            appendGroup(state.mode === "package" ? "Personalization" : state.mode === "design" ? "Print specifications" : "Your options", specs.map((row) => [row.label, row.value]));
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
        if (!state.invitation && state.mode !== "design") {
            appendGroup("Other reference files", [["Files", state.referenceFiles.map((file) => file.name).join(", ") || "None attached"]]);
        }

        const pricing = [];
        const quote = state.quote;
        if (!quote) {
            pricing.push(["Estimate", state.mode === "package" && !state.package ? "Select a package" : state.mode === "design" && !state.design ? "Select a design" : "Calculating…"]);
        } else if (quote.requires_quote) {
            pricing.push(["Estimated total", "Quotation from the shop"], ["Why", quote.quote_reasons.join(" ")]);
        } else if (quote.estimated_total === null) {
            pricing.push(["Estimated total", quote.incomplete.join(" ") || "—"]);
        } else {
            quote.lines.forEach((line) => pricing.push([line.label, `${money(line.amount)} (${line.detail})`]));
            if (quote.unit_price !== null) pricing.push(["Price per piece", `${money(quote.unit_price)} × ${quote.quantity.toLocaleString()} pcs`]);
            pricing.push(["Estimated total", money(quote.estimated_total)]);
        }
        if (quote?.standard_size) {
            pricing.unshift(["Selected size", quote.standard_size.label], ["Unit price", money(quote.standard_size.unit_price)], ["Quantity", `${quote.quantity.toLocaleString()} pcs`]);
        }
        pricing.push(["Final amount", isFixedPrice()
            ? (state.mode === "package"
                ? "The package's published price; the shop confirms the order before payment"
                : "The published size price × quantity; the shop confirms the order before payment")
            : "Set by the shop's quotation, which you accept or decline before any payment"]);
        appendGroup("Pricing", pricing);
        appendGroup("Delivery or pickup", fulfillmentRows());
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
            if (!productOrderable()) {
                $("#product-error").textContent = `${state.product.name} is not available to order right now. Please choose another product.`;
                return false;
            }
            return true;
        }
        if (stepNumber === 2) {
            if (!state.mode) {
                modeError.textContent = customizeAllowed() ? "Choose “Select Package” or “Customize Order” to continue." : "Select a package to continue.";
                return false;
            }
            if (state.mode === "package" && !state.package) {
                packageError.textContent = "Select a package to continue.";
                return false;
            }
            return true;
        }
        if (stepNumber === 9) {
            if (!state.design) {
                designError.textContent = "Choose a design to continue.";
                designSection.scrollIntoView({ behavior: "smooth", block: "start" });
                return false;
            }
            return Boolean(state.personalizeForm?.validate());
        }
        if (stepNumber === 8) {
            if (!generalQuantity.checkValidity()) {
                generalQuantity.reportValidity();
                return false;
            }
            const dynamicForm = activeForms()[0];
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
            if (state.mode === "customize" || state.mode === "design") {
                if (!state.forms.basics?.validate()) return false;
            }
            if (state.mode === "customize" && !state.forms.design?.validate()) return false;
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
        state.sequence = SEQUENCES[kind];
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
        // Steps after Customize Design need a chosen design.
        if (state.mode === "design" && !state.design && stepNumber !== 9 && state.sequence.findIndex((item) => item.step === stepNumber) > state.sequence.findIndex((item) => item.step === 9)) {
            stepNumber = 9;
            designError.textContent = "Choose a design to continue.";
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

    /** Show the per-product upload limit next to the general reference-file and invitation upload hints. */
    function syncUploadLimit(product) {
        const text = DynamicForm.limitText(DynamicForm.uploadLimit(product));
        const artwork = $("#artwork-limit");
        if (artwork) artwork.textContent = `${text} maximum per file.`;
        document.querySelectorAll("[data-upload-limit]").forEach((node) => { node.textContent = text; });
    }

    // ------------------------------------------------------------- chunked uploads (large Tarpaulin files)
    //
    // Files are sent in pieces (at most 2 MB each) before the order itself, so every request stays below the
    // hosting request limit and large print files can be uploaded. A file that was uploaded once is reused if
    // the customer submits again (for example after fixing a form error).

    const uploadProgress = $("#upload-progress");
    const uploadedFiles = new WeakMap(); // File -> { key, upload_id }

    function showUploadProgress(done, total, name) {
        if (!uploadProgress) return;
        uploadProgress.hidden = false;
        const percent = total ? Math.min(100, Math.round((done / total) * 100)) : 100;
        $("#upload-progress-label").textContent = name ? `Uploading ${name}… ${percent}%` : `Uploading files… ${percent}%`;
        $("#upload-progress-bar").style.width = `${percent}%`;
    }

    function hideUploadProgress() {
        if (uploadProgress) uploadProgress.hidden = true;
    }

    async function uploadFile(file, fieldKey, progress) {
        const key = `${state.product.id}:${fieldKey}`;
        const known = uploadedFiles.get(file);
        if (known && known.key === key) {
            progress.done += file.size;
            showUploadProgress(progress.done, progress.total, file.name);
            return { upload_id: known.upload_id };
        }
        const started = await requestJson("/api/uploads", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ product_id: state.product.id, field_key: fieldKey, name: file.name, size: file.size, mime_type: file.type }),
        });
        const chunk = started.chunk_size;
        for (let index = 0, offset = 0; offset < file.size; index += 1, offset += chunk) {
            const piece = file.slice(offset, Math.min(file.size, offset + chunk));
            let attempt = 0;
            for (;;) {
                try {
                    await requestJson(`/api/uploads/${encodeURIComponent(started.upload_id)}/chunks/${index}`, {
                        method: "PUT",
                        headers: { "Content-Type": "application/octet-stream" },
                        body: piece,
                    });
                    break;
                } catch (error) {
                    attempt += 1;
                    if (attempt >= 3) throw new Error(`${file.name} could not be uploaded. ${error instanceof Error ? error.message : ""}`.trim());
                }
            }
            progress.done += piece.size;
            showUploadProgress(progress.done, progress.total, file.name);
        }
        await requestJson(`/api/uploads/${encodeURIComponent(started.upload_id)}/complete`, { method: "POST" });
        uploadedFiles.set(file, { key, upload_id: started.upload_id });
        return { upload_id: started.upload_id };
    }

    /** Upload every file of the order; returns {fieldFiles: {key: [{upload_id}]}, files: [{upload_id}]}. */
    async function uploadOrderFiles(fieldFileMap, referenceFiles) {
        const all = [...Object.values(fieldFileMap).flat(), ...referenceFiles];
        const progress = { done: 0, total: all.reduce((sum, file) => sum + file.size, 0) };
        if (all.length) showUploadProgress(0, progress.total, "");
        const fieldFiles = {};
        for (const [key, files] of Object.entries(fieldFileMap)) {
            fieldFiles[key] = [];
            for (const file of files) fieldFiles[key].push(await uploadFile(file, key, progress));
        }
        const files = [];
        for (const file of referenceFiles) files.push(await uploadFile(file, "", progress));
        return { fieldFiles, files };
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
            const designOrderFiles = state.mode === "design" && Boolean(state.design);
            const fieldFileMap = {};
            if (!designOrderFiles) {
                for (const dynamicForm of activeForms()) Object.assign(fieldFileMap, dynamicForm.files());
            }
            const referenceFiles = designOrderFiles || state.invitation ? [] : state.referenceFiles;
            button.textContent = "Uploading files…";
            const uploads = await uploadOrderFiles(fieldFileMap, referenceFiles);
            button.textContent = "Submitting…";
            const customization = {
                quantity: quantityValue(),
                options: collectValues(),
                notes: activeNotes().value.trim(),
            };
            if (state.mode === "package" && state.package) customization.package_id = state.package.id;
            const designOrder = state.mode === "design" && Boolean(state.design);
            if (designOrder) {
                customization.design_id = state.design.id;
                customization.design_details = state.personalizeForm ? state.personalizeForm.values() : {};
            }
            const payload = {
                customer: {
                    name: $("#name").value.trim(),
                    email: $("#email").value.trim(),
                    phone: $("#phone").value.trim(),
                    messenger: $("#messenger").value.trim(),
                },
                product: { id: state.product.id },
                customization,
                // Customized orders choose the payment option after accepting the quotation.
                payment_option: "down_payment",
                fulfillment: fulfillmentPayload(),
                has_event: designOrder ? false : (state.invitation ? true : !eventFields.hidden),
                event: designOrder || state.invitation || eventFields.hidden ? {} : eventData(),
                acknowledged: $("#acknowledge").checked,
                // A shop design order never sends design or reference files. Files were uploaded in pieces above.
                files: uploads.files,
                field_files: uploads.fieldFiles,
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
            hideUploadProgress();
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
        const packageName = order.order_kind === "package" ? order.package?.name : (order.order_kind === "design" ? `${order.design?.name || "Shop"} design` : "");
        const fulfillment = order.fulfillment || {};
        const lines = [
            el("p", "", `${order.product.name}${packageName ? ` · ${packageName}` : ""} · ${order.customization.quantity.toLocaleString()} pc(s) · ${order.status}`),
        ];
        if (order.fixed_price) {
            const sizeText = order.price_list?.size ? ` · ${order.price_list.size}` : "";
            lines.push(
                el("p", "", `${order.fixed_price_label || "Package total"}: ${money(order.total_amount)}${order.pricing?.unit_price ? ` (${money(order.pricing.unit_price)} per piece${sizeText})` : ""}.`),
                el("p", "", `50% down payment: ${money(payment.down_payment_required)} · remaining balance ${money(payment.remaining_after_down_payment)}. `
                    + "Pay the down payment through the shop's e-wallet (GCash, Maya, or another listed option) after the shop approves your order; your order is confirmed once the shop verifies it."),
            );
        } else {
            lines.push(
                el("p", "", estimate !== null && estimate !== undefined
                    ? `Estimated total: ${money(estimate)} — the shop will send a quotation for you to accept or decline.`
                    : "The shop will send a quotation for you to accept or decline."),
                el("p", "", "You will be notified in the customer portal when the quotation is ready. After you accept it, pay the 50% down payment through the shop's e-wallet; your order is confirmed once the shop verifies it."),
            );
        }
        lines.push(el("p", "", `Fulfillment: ${fulfillment.label || "Store Pickup"}${fulfillment.method === "delivery"
            ? ` to ${fulfillment.address.filter((row) => ["street", "barangay", "city", "province"].includes(row.key)).map((row) => row.value).join(", ")} · delivery fee ${money(fulfillment.delivery_fee || 0)}${fulfillment.estimate_text ? ` · ${fulfillment.estimate_text}` : ""}`
            : fulfillment.pickup_location ? ` at ${fulfillment.pickup_location.name}` : ""}.`));
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
    [generalQuantity, invitationQuantity].forEach((input) => {
        input.addEventListener("input", onQuantityChange);
        input.addEventListener("change", clampQuantity);
    });
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
            // Customize Order opens the shop's designs first; "Use my own design" is chosen in the gallery.
            if (button.dataset.orderMode === "customize") state.ownDesign = false;
            setMode(button.dataset.orderMode);
        });
    });
    hasEvent.addEventListener("change", configureLegacyEvent);
    $("#btn-next").addEventListener("click", () => {
        if (validateStep(state.activeStep)) moveStep(1);
    });
    $("#btn-back").addEventListener("click", () => moveStep(-1));
    $("#edit-order").addEventListener("click", () => showStep(state.invitation ? 3 : 8));
    $("#btn-cancel").addEventListener("click", async () => {
        if (await window.PrintConfirm("Cancel this order request and return to the services list?", { title: "Cancel this order?", confirmLabel: "Cancel order", cancelLabel: "Keep ordering", tone: "danger" })) window.location.assign("/order");
    });
    form.addEventListener("input", () => {
        if (state.activeStep === REVIEW_STEP) renderSummary();
    });
    form.addEventListener("submit", submitOrder);

    // ---------------------------------------------------------------- services selection (/order with no service)

    const serviceSelect = $("#service-select");
    const serviceGrid = $("#service-select-grid");

    /** The order form for a service, product, or package: /order?service=…&design=…&package=… */
    function orderPath(categoryId, productId, packageId) {
        const query = new URLSearchParams({ service: categoryId });
        if (productId) query.set("design", productId);
        if (packageId) query.set("package", packageId);
        return `/order?${query}`;
    }

    /** Guests go to Sign In first, which returns them to exactly this selection afterwards. */
    function signInPath(next) {
        return `/sign-in?${new URLSearchParams({ next })}`;
    }

    function serviceLink(signedIn, className, text, ...ids) {
        const link = el("a", className, text);
        link.href = signedIn ? orderPath(...ids) : signInPath(orderPath(...ids));
        return link;
    }

    /** The lowest published price for a product: its base rate, else its cheapest package, else none (quotation). */
    function startingPrice(product) {
        if (product.pricing_mode === "size") {
            const size = cheapestSize(product);
            if (size) return { amount: money(size.price), unit: `${size.label} · per piece` };
        }
        if (product.pricing_mode !== "quote" && product.base_price !== null && product.pricing_mode !== "size") {
            return { amount: money(product.base_price), unit: product.pricing_mode === "area" ? "per sq ft" : "per piece" };
        }
        const packages = product.packages || [];
        if (!packages.length) return null;
        const cheapest = packages.reduce((low, pkg) => (pkg.price < low.price ? pkg : low));
        return { amount: money(cheapest.price), unit: `package of ${packageMinimum(cheapest).toLocaleString()} pcs` };
    }

    /** Small preview image; when the product has photos, selecting it opens them in the lightbox. */
    function serviceRowMedia(product) {
        const images = productImages(product);
        if (!images.length) {
            const placeholder = el("span", "service-row-thumb service-row-thumb-empty", product.name.slice(0, 1));
            placeholder.setAttribute("aria-hidden", "true");
            return placeholder;
        }
        const button = el("button", "service-row-thumb");
        button.type = "button";
        button.setAttribute("aria-label", `View photos of ${product.name}`);
        const image = el("img");
        image.src = images[0];
        image.alt = "";
        image.loading = "lazy";
        image.decoding = "async";
        button.append(image);
        button.addEventListener("click", () => openLightbox(images.map((src, index) => ({
            src,
            alt: `${product.name} — photo ${index + 1}`,
            caption: `${product.name} · photo ${index + 1} of ${images.length}`,
        }))));
        return button;
    }

    /** One compact row: preview · name, description, and tags · starting price · Avail Service (+ its packages). */
    function serviceRow(category, product, signedIn) {
        const row = el("li", "service-row");
        row.id = `service-row-${product.id}`;
        const packages = product.packages || [];

        const info = el("div", "service-row-info");
        const name = el("h4", "service-row-name", product.name);
        name.id = `service-row-${product.id}-name`;
        info.append(name);
        if (product.description) info.append(el("p", "service-row-description", product.description));
        // Only facts worth a glance; the package count and "customize" option are shown under the row.
        const tags = [];
        if (!customizeAllowed(product)) tags.push("Packages only");
        const designCount = customizeAllowed(product) ? (product.designs || []).length : 0;
        if (designCount) tags.push(`${designCount} shop design${designCount === 1 ? "" : "s"}`);
        if (product.min_quantity > 1) tags.push(`Min. ${product.min_quantity.toLocaleString()} pcs`);
        if (product.standard_size_label) tags.push(`Standard size ${product.standard_size_label}`);
        if ((product.standard_sizes || []).length && product.pricing_mode === "size") tags.push(`${product.standard_sizes.length} standard sizes`);
        if ((product.upload_limit_bytes || 0) > 5 * 1024 * 1024) tags.push(`Files up to ${DynamicForm.limitText(product.upload_limit_bytes)}`);
        if (tags.length) {
            const tagList = el("p", "service-row-tags");
            tags.forEach((tag) => tagList.append(el("span", "service-row-tag", tag)));
            info.append(tagList);
        }

        const price = el("div", "service-row-price");
        const starting = startingPrice(product);
        if (starting) {
            price.append(el("small", "", "Starts at"), el("strong", "", starting.amount), el("small", "", starting.unit));
        } else {
            price.classList.add("is-quote");
            price.append(el("small", "", "Pricing"), el("strong", "", "By quotation"));
        }

        const actions = el("div", "service-row-actions");
        const avail = serviceLink(signedIn, "btn btn-dark btn-small service-row-avail", "Avail Service", category.id, product.id);
        avail.setAttribute("aria-describedby", name.id);
        if (!signedIn) avail.title = "Sign in first; you return to this service right after.";
        actions.append(avail);

        row.append(serviceRowMedia(product), info, price, actions);

        // Size-priced services (Tarpaulin) show their price list.
        const priceTable = sizePriceTable(product, { caption: `${product.name}: standard sizes & prices` });
        if (priceTable) {
            const sizes = el("details", "service-row-packages service-row-sizes");
            sizes.append(el("summary", "", `View price list (${product.standard_sizes.length} sizes · custom sizes by quotation)`), priceTable);
            row.append(sizes);
        }

        // Packages open the order form with that package already chosen.
        if (packages.length) {
            const details = el("details", "service-row-packages");
            details.append(el("summary", "", `View ${packages.length} package${packages.length === 1 ? "" : "s"}${customizeAllowed(product) ? " · or customize your own" : ""}`));
            const list = el("ul", "service-package-list");
            for (const pkg of packages) {
                const entry = el("li");
                const choose = serviceLink(signedIn, "service-package-link", "", category.id, product.id, pkg.id);
                const copy = el("span", "service-package-copy");
                copy.append(el("strong", "", pkg.name));
                const inclusions = (pkg.inclusion_details?.length ? pkg.inclusion_details.map((inc) => inc.name) : pkg.inclusions || []).filter(Boolean);
                const terms = [`${packageMinimum(pkg).toLocaleString()} pcs`, ...inclusions.slice(0, 3)];
                if (inclusions.length > 3) terms.push(`+${inclusions.length - 3} more`);
                copy.append(el("small", "", terms.join(" · ")));
                choose.append(copy, el("span", "service-package-price", money(pkg.price)), el("span", "service-package-cta", "Choose"));
                choose.setAttribute("aria-label", `Choose the ${pkg.name} package for ${product.name}, ${money(pkg.price)}`);
                entry.append(choose);
                list.append(entry);
            }
            details.append(list);
            row.append(details);
        }
        return row;
    }

    /** One section per service category, with its services listed as rows. */
    function categorySection(category, signedIn) {
        const section = el("section", "service-category-block");
        section.id = `service-category-${category.id}`;
        const head = el("header", "service-category-block-head");
        const titles = el("div", "service-category-block-titles");
        const title = el("h3", "service-category-block-title", category.label);
        title.id = `${section.id}-title`;
        section.setAttribute("aria-labelledby", title.id);
        titles.append(title);
        if (category.description) titles.append(el("p", "service-category-block-description", category.description));
        const count = category.products.length;
        head.append(titles, el("span", "service-category-block-count", `${count} service${count === 1 ? "" : "s"}`));
        const rows = el("ul", "service-rows");
        rows.setAttribute("aria-label", `${category.label} services`);
        rows.append(...category.products.map((product) => serviceRow(category, product, signedIn)));
        section.append(head, rows);
        return section;
    }

    function renderCategoryJump(categories) {
        const nav = $("#service-select-jump");
        if (!nav) return;
        nav.replaceChildren(...categories.map((category) => {
            const link = el("a", "service-jump-link", category.label);
            link.href = `#service-category-${category.id}`;
            return link;
        }));
        nav.hidden = categories.length < 2;
    }

    function showServiceError(message) {
        const box = $("#service-select-error");
        box.textContent = message;
        box.hidden = !message;
    }

    const serviceView = { signature: "", signedIn: false, loadedAt: 0, notice: "", jumped: false };

    /** Only active products that can be ordered, grouped under their (active) category, in the shop's order. */
    function orderableCategories(categories) {
        return categories
            .map((category) => ({ ...category, products: category.products.filter((product) => productOrderable(product)) }))
            .filter((category) => category.products.length);
    }

    function renderServiceList(categories) {
        const orderable = orderableCategories(categories);
        const signature = JSON.stringify([serviceView.signedIn, orderable]);
        if (signature === serviceView.signature) return; // unchanged: keep open package lists as they are
        serviceView.signature = signature;
        renderCategoryJump(orderable);
        if (!orderable.length) {
            serviceGrid.replaceChildren(el("p", "service-select-status", "No services are open for online ordering right now. Please contact the shop."));
            return;
        }
        serviceGrid.replaceChildren(...orderable.map((category) => categorySection(category, serviceView.signedIn)));
        // A link such as /order#service-category-souvenir opens at that category once the list exists.
        if (!serviceView.jumped && window.location.hash.startsWith("#service-category-")) {
            serviceView.jumped = true;
            document.getElementById(window.location.hash.slice(1))?.scrollIntoView({ block: "start" });
        }
    }

    /** The public services list. Choosing a service checks the sign-in status before any order form opens. */
    async function startServiceSelection(notice = "") {
        serviceSelect.hidden = false;
        if (gate) gate.hidden = true;
        stepper.hidden = true;
        form.hidden = true;
        serviceView.notice = notice;
        showServiceError(notice);
        const [catalog, session] = await Promise.allSettled([
            requestJson("/api/catalog", { cache: "no-store" }),
            fetch("/api/customer/session", { credentials: "same-origin", cache: "no-store" }).then((response) => (response.ok ? response.json() : null)),
        ]);
        const customer = session.status === "fulfilled" ? session.value?.customer : null;
        serviceView.signedIn = Boolean(customer);
        serviceView.loadedAt = Date.now();
        const authNote = $("#service-select-auth");
        authNote.hidden = false;
        authNote.textContent = customer
            ? `Signed in as ${customer.name || customer.email}. Choose Avail Service to open its order form.`
            : "You will be asked to sign in first. Your chosen service and package are kept, and the order form opens right after.";
        serviceGrid.removeAttribute("aria-busy");
        if (catalog.status === "rejected") {
            serviceView.signature = "";
            serviceGrid.replaceChildren(el("p", "service-select-status", "Services could not be loaded right now. Refresh the page or contact the shop."));
            showServiceError(`The service list could not be loaded. ${catalog.reason instanceof Error ? catalog.reason.message : ""}`.trim());
            return;
        }
        state.categories = catalog.value.categories || [];
        renderServiceList(state.categories);
    }

    // Returning to the tab reloads the list, so services the administrator added, changed, or hid show up.
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState !== "visible" || serviceSelect.hidden) return;
        if (Date.now() - serviceView.loadedAt < 15000) return;
        void startServiceSelection(serviceView.notice);
    });

    /** After sign-in the customer lands on the product (and package) they chose; no need to choose again. */
    function applyRequestedSelection() {
        const productId = parameters.get("design");
        if (!productId || state.product?.id !== productId) return;
        const pkg = state.packages.find((item) => item.id === parameters.get("package"));
        const design = state.designs.find((item) => item.id === parameters.get("shop_design"));
        if (pkg) {
            setMode("package", { silent: true });
            selectPackage(pkg);
        } else if (design) {
            setMode("design", { silent: true });
            selectDesign(design);
        }
        // The product is already chosen, so open its ordering options.
        showStep(2, false);
    }

    // ---------------------------------------------------------------- start: sign-in check, then catalog

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
        // /order on its own is the services selection. A service in the URL means "order this".
        if (!parameters.get("service") && !parameters.get("design")) {
            await startServiceSelection();
            return;
        }
        let customer;
        try {
            const response = await fetch("/api/customer/session", { credentials: "same-origin", cache: "no-store" });
            if (response.status === 401) {
                // Guests never see the order form: sign in first, then come back to this exact selection.
                window.location.replace(signInPath(window.location.pathname + window.location.search));
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
        const requested = state.categories.some((category) => category.id === parameters.get("service")
            || category.products.some((product) => product.id === parameters.get("design")));
        if (!requested) {
            // A removed or hidden service: go back to the list instead of opening another service's form.
            window.history.replaceState(null, "", "/order");
            await startServiceSelection("The service you selected is no longer available. Please choose another service.");
            return;
        }
        setSequence("general");
        populateCategories();
        showStep(1, false);
        applyRequestedSelection();
        renderPaymentBreakdown();
    }

    setSequence("general");
    void start();
})();
