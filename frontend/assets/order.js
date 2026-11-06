"use strict";

/**
 * Order wizard. The catalog, each product's questions, and all prices come from /api/catalog, so the
 * form always reflects what the administrator configured. Price estimates are calculated by the server
 * (/api/catalog/quote) with the same rules it applies when the order is saved.
 */
(() => {
    const DynamicForm = window.PrintShopDynamicForm;
    const form = document.querySelector("#order-form");
    if (!form || !DynamicForm) return;

    const $ = (selector) => document.querySelector(selector);
    const steps = [...form.querySelectorAll(".wizard-step")];
    const indicators = [...document.querySelectorAll("[data-step-indicator]")];
    const categorySelect = $("#category");
    const productPicker = $("#product-picker");
    const selectedProduct = $("#selected-product");
    const pricingNote = $("#pricing-note");
    const quantityInput = $("#quantity");
    const dynamicContainer = $("#dynamic-fields");
    const uploadInput = $("#artwork");
    const uploadPreviews = $("#upload-previews");
    const eventToggle = $("#event-toggle");
    const hasEvent = $("#has-event");
    const eventFields = $("#event-fields");
    const summary = $("#summary");
    const orderError = $("#order-error");
    const MAX_REFERENCE_FILES = 3;
    const MAX_ORDER_FILES = 15;
    const money = DynamicForm.money;
    const parameters = new URLSearchParams(window.location.search);

    const state = {
        categories: [],
        product: null,
        dynamicForm: null,
        referenceFiles: [],
        quote: null,
        quoteTimer: 0,
        quoteSequence: 0,
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

    function selectProduct(productId) {
        const product = currentCategory()?.products.find((item) => item.id === productId);
        if (!product || product === state.product) return;
        state.product = product;
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

        const priced = product.pricing_mode !== "quote" && product.base_price !== null;
        const note = pricingNote.querySelector("div");
        note.replaceChildren(
            el("strong", "", priced ? "Prices set by the shop" : "Request a quote"),
            el("p", "", priced
                ? "You will see a live estimate as you choose options. The administrator confirms the final amount before payment is requested."
                : "This product does not have a published price yet. Submit your options and the administrator will send a quotation."),
        );

        // Build this product's own form. Nothing from the previous product is kept.
        $("#step2-product").textContent = product.name;
        quantityInput.min = String(product.min_quantity);
        quantityInput.max = String(product.max_quantity);
        quantityInput.value = String(product.min_quantity);
        $("#quantity-hint").textContent = product.min_quantity > 1
            ? `Minimum order: ${product.min_quantity.toLocaleString()} pieces · maximum ${product.max_quantity.toLocaleString()}.`
            : `Up to ${product.max_quantity.toLocaleString()} pieces.`;
        state.dynamicForm?.destroy();
        state.dynamicForm = DynamicForm.create(dynamicContainer, product, { onChange: scheduleQuote, idPrefix: "opt" });
        configureEvent();
        state.quote = null;
        scheduleQuote();

        const url = new URL(window.location.href);
        url.searchParams.set("service", product.category_id);
        url.searchParams.set("design", product.id);
        window.history.replaceState(null, "", url);
    }

    // ---------------------------------------------------------------- step 3: event details per product

    function configureEvent() {
        const mode = state.product?.event_mode || "none";
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

    function quantityValue() {
        const value = Number(quantityInput.value);
        return Number.isInteger(value) ? value : 0;
    }

    function scheduleQuote() {
        window.clearTimeout(state.quoteTimer);
        $("#estimate-status").textContent = "Updating estimate…";
        state.quoteTimer = window.setTimeout(refreshQuote, 250);
    }

    async function refreshQuote() {
        if (!state.product) return;
        const sequence = ++state.quoteSequence;
        try {
            const quote = await requestJson("/api/catalog/quote", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    product_id: state.product.id,
                    quantity: quantityValue() || state.product.min_quantity,
                    options: state.dynamicForm.values(),
                }),
            });
            if (sequence !== state.quoteSequence) return; // a newer request is in flight
            state.quote = quote;
        } catch (error) {
            if (sequence !== state.quoteSequence) return;
            state.quote = null;
            $("#estimate-status").textContent = error instanceof Error ? error.message : "The estimate could not be calculated.";
            return;
        }
        renderEstimate();
        if (state.activeStep === 4) renderSummary();
    }

    function estimateText(quote) {
        if (!quote) return "—";
        if (quote.estimated_total !== null) return money(quote.estimated_total);
        return quote.requires_quote ? "Quotation" : "—";
    }

    function renderEstimate() {
        const quote = state.quote;
        $("#estimate-total").textContent = estimateText(quote);
        const lines = $("#estimate-lines");
        lines.replaceChildren();
        if (!quote) return;
        let status;
        if (quote.requires_quote) status = quote.quote_reasons.join(" ");
        else if (quote.incomplete.length) status = quote.incomplete.join(" ");
        else status = quote.unit_price !== null
            ? `${money(quote.unit_price)} per piece × ${quote.quantity.toLocaleString()} · estimate, confirmed by the shop`
            : "";
        $("#estimate-status").textContent = status;
        if (quote.requires_quote) return;
        quote.lines.forEach((line) => {
            const item = el("li");
            const label = el("span");
            label.append(el("strong", "", line.label), el("small", "", line.detail));
            item.append(label, el("span", "estimate-amount", quote.estimated_total === null && quote.pricing_mode === "area" ? "—" : money(line.amount)));
            lines.append(item);
        });
    }

    // ---------------------------------------------------------------- general reference files

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
        const allowed = new Set(["image/jpeg", "image/png", "application/pdf"]);
        const invalid = chosen.find((file) => !allowed.has(file.type) || !/\.(jpe?g|png|pdf)$/i.test(file.name));
        if (invalid) return setError(`${invalid.name}: choose a JPG, JPEG, PNG, or PDF file.`);
        const oversized = chosen.find((file) => file.size > 5 * 1024 * 1024);
        if (oversized) return setError(`${oversized.name}: each file must be no larger than 5 MB.`);
        if (state.referenceFiles.length + chosen.length > MAX_REFERENCE_FILES) return setError(`Attach up to ${MAX_REFERENCE_FILES} reference files.`);
        state.referenceFiles.push(...chosen);
        renderReferenceFiles();
    });

    // ---------------------------------------------------------------- step 4: review summary

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

    function renderSummary() {
        summary.replaceChildren();
        const product = state.product;
        if (!product) return;
        appendGroup("Product", [
            ["Category", product.category_label],
            ["Product", product.name],
            ["Quantity", quantityValue().toLocaleString()],
        ]);
        const specs = state.dynamicForm.summary();
        if (state.quote?.area_sqft) specs.push({ label: "Print area", value: `${state.quote.area_sqft} sq ft per piece` });
        appendGroup("Your options", specs.map((row) => [row.label, row.value]));
        appendGroup("Special instructions", [["Notes", $("#notes").value.trim()]]);
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
        appendGroup("Contact", [
            ["Name", $("#name").value.trim()],
            ["Email", $("#email").value.trim()],
            ["Contact number", $("#phone").value.trim()],
            ["Messenger", $("#messenger").value.trim()],
        ]);
        appendGroup("Other reference files", [["Files", state.referenceFiles.map((file) => file.name).join(", ") || "None attached"]]);

        const quote = state.quote;
        const pricing = [];
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
            if (!quantityInput.checkValidity()) {
                quantityInput.reportValidity();
                return false;
            }
            if (!state.dynamicForm.validate()) return false;
            const total = state.referenceFiles.length + state.dynamicForm.fileCount();
            if (total > MAX_ORDER_FILES) {
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
        if (stepNumber === 4 && !$("#acknowledge").checked) {
            $("#acknowledgement-error").textContent = "Please acknowledge the order instructions before submitting.";
            $("#acknowledge").focus();
            return false;
        }
        return true;
    }

    function showStep(stepNumber, scroll = true) {
        state.activeStep = stepNumber;
        steps.forEach((section) => section.classList.toggle("active", Number(section.dataset.step) === stepNumber));
        indicators.forEach((indicator) => {
            const number = Number(indicator.dataset.stepIndicator);
            indicator.classList.toggle("active", number === stepNumber);
            indicator.classList.toggle("done", number < stepNumber);
            if (number === stepNumber) indicator.setAttribute("aria-current", "step");
            else indicator.removeAttribute("aria-current");
        });
        $("#btn-back").hidden = stepNumber === 1;
        $("#btn-cancel").hidden = stepNumber !== 4;
        $("#btn-next").hidden = stepNumber === steps.length;
        $("#btn-submit").hidden = stepNumber !== steps.length;
        if (stepNumber === 4) renderSummary();
        if (scroll) $(`#step${stepNumber}-title`).scrollIntoView({ behavior: "smooth", block: "start" });
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
        for (const step of [1, 2, 3]) {
            if (!validateStep(step)) {
                showStep(step);
                return;
            }
        }
        if (!validateStep(4)) return;
        const button = $("#btn-submit");
        button.disabled = true;
        button.textContent = "Submitting…";
        try {
            const fieldFiles = {};
            for (const [key, files] of Object.entries(state.dynamicForm.files())) fieldFiles[key] = await encodeFiles(files);
            const payload = {
                customer: {
                    name: $("#name").value.trim(),
                    email: $("#email").value.trim(),
                    phone: $("#phone").value.trim(),
                    messenger: $("#messenger").value.trim(),
                },
                product: { id: state.product.id },
                customization: {
                    quantity: quantityValue(),
                    options: state.dynamicForm.values(),
                    notes: $("#notes").value.trim(),
                },
                has_event: !eventFields.hidden,
                event: eventFields.hidden ? {} : eventData(),
                acknowledged: $("#acknowledge").checked,
                files: await encodeFiles(state.referenceFiles),
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
        $("#stepper").hidden = true;
        $("#confirm-ref").textContent = order.reference;
        const estimate = order.pricing?.estimated_total;
        const details = $("#confirm-details");
        details.replaceChildren(
            el("p", "", `${order.product.name} · ${order.customization.quantity.toLocaleString()} pc(s) · ${order.status}`),
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
    quantityInput.addEventListener("input", scheduleQuote);
    form.querySelectorAll("[data-quantity-step]").forEach((button) => {
        button.addEventListener("click", () => {
            const step = Number(button.dataset.quantityStep);
            const min = Number(quantityInput.min) || 1;
            const max = Number(quantityInput.max) || 50000;
            quantityInput.value = String(Math.min(max, Math.max(min, (quantityValue() || min) + step)));
            scheduleQuote();
        });
    });
    hasEvent.addEventListener("change", configureEvent);
    $("#btn-next").addEventListener("click", () => {
        if (validateStep(state.activeStep)) showStep(Math.min(steps.length, state.activeStep + 1));
    });
    $("#btn-back").addEventListener("click", () => showStep(Math.max(1, state.activeStep - 1)));
    $("#edit-order").addEventListener("click", () => showStep(2));
    $("#btn-cancel").addEventListener("click", () => {
        if (window.confirm("Cancel this order request and return to the product catalog?")) window.location.assign("/services");
    });
    form.addEventListener("input", () => {
        if (state.activeStep === 4) renderSummary();
    });
    form.addEventListener("submit", submitOrder);

    // ---------------------------------------------------------------- start: sign-in gate, then catalog

    const gate = $("#order-gate");
    const stepper = $("#stepper");

    function showGate() {
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
        populateCategories();
        showStep(1, false);
    }

    void start();
})();
