"use strict";

(() => {
    const catalog = window.PrintShopCatalog;
    const getSamples = window.PrintShopGetSamples;
    const form = document.querySelector("#order-form");
    if (!catalog || !form) return;

    const steps = [...form.querySelectorAll(".wizard-step")];
    const indicators = [...document.querySelectorAll("[data-step-indicator]")];
    const categorySelect = document.querySelector("#category");
    const itemSelect = document.querySelector("#item");
    const selectedProduct = document.querySelector("#selected-product");
    const uploadInput = document.querySelector("#artwork");
    const uploadPreviews = document.querySelector("#upload-previews");
    const eventFields = document.querySelector("#event-fields");
    const summary = document.querySelector("#summary");
    const orderError = document.querySelector("#order-error");
    const files = [];
    let activeStep = 1;
    let currentProduct = null;
    const parameters = new URLSearchParams(window.location.search);
    const initialService = parameters.get("service");
    const initialDesign = parameters.get("design");
    const createIdempotencyKey = () => {
        if (typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
        const bytes = window.crypto.getRandomValues(new Uint8Array(16));
        return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    };
    let idempotencyKey = createIdempotencyKey();

    const text = (selector) => document.querySelector(selector).value.trim();
    const safeText = (value) => String(value || "").trim();

    function setError(message) {
        orderError.textContent = message;
        orderError.hidden = !message;
    }

    function productFor(categoryId, productName) {
        const category = catalog[categoryId];
        if (!category) return null;
        const sample = getSamples(categoryId).find((entry) => entry.name === productName);
        return sample ? { ...sample, categoryId, category: category.label, description: sample.description || category.description } : null;
    }

    function renderProduct() {
        currentProduct = productFor(categorySelect.value, itemSelect.value);
        selectedProduct.replaceChildren();
        if (!currentProduct) return;

        const image = document.createElement("img");
        image.src = currentProduct.image;
        image.alt = `Sample image for ${currentProduct.name}`;
        image.className = "selected-product-image";
        const copy = document.createElement("div");
        copy.className = "selected-product-copy";
        const category = document.createElement("span");
        category.className = "order-step-kicker";
        category.textContent = currentProduct.category;
        const title = document.createElement("h3");
        title.textContent = currentProduct.name;
        const description = document.createElement("p");
        description.textContent = currentProduct.description;
        const quote = document.createElement("strong");
        quote.className = "product-price-note";
        quote.textContent = "Price confirmed after administrator review";
        copy.append(category, title, description, quote);
        selectedProduct.append(image, copy);

        const isEvent = categorySelect.value === "invitation" || categorySelect.value === "party-needs" ||
            /birthday|wedding|celebrat|event|welcome|graduat|christening|party/i.test(currentProduct.name);
        eventFields.hidden = !isEvent;
        eventFields.querySelectorAll("input").forEach((input) => {
            input.required = isEvent && ["celebrant", "event-type"].includes(input.id);
        });
    }

    function populateCategories() {
        categorySelect.replaceChildren();
        Object.entries(catalog).forEach(([id, category]) => {
            const option = document.createElement("option");
            option.value = id;
            option.textContent = category.label;
            categorySelect.append(option);
        });
        const selectedCategory = catalog[initialService] ? initialService : Object.keys(catalog)[0];
        categorySelect.value = selectedCategory;
        populateProducts(initialDesign);
    }

    function populateProducts(designId) {
        const selectedCategory = catalog[categorySelect.value];
        const samples = getSamples(categorySelect.value);
        itemSelect.replaceChildren();
        samples.forEach((sample) => {
            const option = document.createElement("option");
            option.value = sample.name;
            option.textContent = sample.name;
            option.dataset.designId = sample.id;
            itemSelect.append(option);
        });
        const matchingDesign = [...itemSelect.options].find((option) => option.dataset.designId === designId);
        itemSelect.value = matchingDesign?.value || samples[0]?.name || "";
        selectedProduct.setAttribute("aria-label", `Selected ${selectedCategory.label} product`);
        renderProduct();
    }

    function appendSummaryRow(label, value, target = summary) {
        const row = document.createElement("div");
        row.className = "summary-row";
        const term = document.createElement("dt");
        term.textContent = label;
        const detail = document.createElement("dd");
        detail.textContent = safeText(value) || "—";
        row.append(term, detail);
        target.append(row);
    }

    function customerData() {
        return {
            name: text("#name"),
            email: text("#email"),
            phone: text("#phone"),
            messenger: text("#messenger"),
        };
    }

    function eventData() {
        return {
            celebrant: text("#celebrant"),
            event_type: text("#event-type"),
            event_date: text("#event-date"),
            event_time: text("#event-time"),
            event_location: text("#event-location"),
        };
    }

    function renderSummary() {
        summary.replaceChildren();
        appendSummaryRow("Product", currentProduct?.name);
        appendSummaryRow("Category", currentProduct?.category);
        appendSummaryRow("Size / dimensions", text("#size"));
        appendSummaryRow("Quantity", text("#quantity"));
        appendSummaryRow("Material / variant", text("#material"));
        appendSummaryRow("Customization", text("#customization"));
        appendSummaryRow("Special instructions", text("#instructions"));
        if (!eventFields.hidden) {
            Object.entries({
                "Celebrant": eventData().celebrant,
                "Event type": eventData().event_type,
                "Event date": eventData().event_date,
                "Event time": eventData().event_time,
                "Event location": eventData().event_location,
            }).forEach(([label, value]) => appendSummaryRow(label, value));
        }
        appendSummaryRow("Customer", customerData().name);
        appendSummaryRow("Email", customerData().email);
        appendSummaryRow("Contact number", customerData().phone);
        appendSummaryRow("Messenger name", customerData().messenger);
        appendSummaryRow("Reference files", files.map((file) => file.name).join(", ") || "None attached");
        appendSummaryRow("Unit price", "Request a Quote");
        appendSummaryRow("Estimated total", "Waiting for Admin Quotation");
        appendSummaryRow("Down payment / balance", "Shown after the administrator confirms the final amount");
    }

    function validateStep(stepNumber) {
        const section = steps.find((item) => Number(item.dataset.step) === stepNumber);
        const invalid = [...section.querySelectorAll("input, select, textarea")]
            .filter((control) => !control.disabled && !control.checkValidity());
        if (invalid.length) {
            invalid[0].reportValidity();
            return false;
        }
        if (stepNumber === 4 && !document.querySelector("#acknowledge").checked) {
            document.querySelector("#acknowledgement-error").textContent = "Please acknowledge the order instructions before submitting.";
            document.querySelector("#acknowledge").focus();
            return false;
        }
        return true;
    }

    function showStep(stepNumber) {
        activeStep = stepNumber;
        steps.forEach((section) => section.classList.toggle("active", Number(section.dataset.step) === activeStep));
        indicators.forEach((indicator) => {
            const number = Number(indicator.dataset.stepIndicator);
            indicator.classList.toggle("active", number === activeStep);
            indicator.classList.toggle("done", number < activeStep);
        });
        document.querySelector("#btn-back").hidden = activeStep === 1;
        document.querySelector("#btn-cancel").hidden = activeStep !== 4;
        document.querySelector("#btn-next").hidden = activeStep === steps.length;
        document.querySelector("#btn-submit").hidden = activeStep !== steps.length;
        if (activeStep === 4) renderSummary();
        document.querySelector(`#step${activeStep}-title`).scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function formatBytes(bytes) {
        return bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(0)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    }

    function renderFiles() {
        uploadPreviews.replaceChildren();
        files.forEach((file, index) => {
            const item = document.createElement("article");
            item.className = "upload-preview";
            if (file.type.startsWith("image/")) {
                const image = document.createElement("img");
                image.src = URL.createObjectURL(file);
                image.alt = `Preview of ${file.name}`;
                image.onload = () => URL.revokeObjectURL(image.src);
                item.append(image);
            } else {
                const pdf = document.createElement("span");
                pdf.className = "pdf-preview";
                pdf.textContent = "PDF";
                item.append(pdf);
            }
            const info = document.createElement("span");
            info.className = "upload-preview-info";
            const name = document.createElement("strong");
            name.textContent = file.name;
            const size = document.createElement("small");
            size.textContent = formatBytes(file.size);
            info.append(name, size);
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "text-button";
            remove.textContent = "Remove";
            remove.setAttribute("aria-label", `Remove ${file.name}`);
            remove.addEventListener("click", () => {
                files.splice(index, 1);
                renderFiles();
            });
            item.append(info, remove);
            uploadPreviews.append(item);
        });
    }

    function fileAsDataUrl(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.addEventListener("load", () => resolve(reader.result));
            reader.addEventListener("error", () => reject(new Error(`Could not read ${file.name}.`)));
            reader.readAsDataURL(file);
        });
    }

    async function submitOrder(event) {
        event.preventDefault();
        setError("");
        document.querySelector("#acknowledgement-error").textContent = "";
        if (!validateStep(4)) return;
        const button = document.querySelector("#btn-submit");
        button.disabled = true;
        button.textContent = "Submitting…";
        try {
            const payload = {
                customer: customerData(),
                product: {
                    name: currentProduct.name,
                    category: currentProduct.category,
                    description: currentProduct.description,
                    image: currentProduct.image,
                },
                customization: {
                    size: text("#size"),
                    quantity: Number(text("#quantity")),
                    material: text("#material"),
                    details: text("#customization"),
                    instructions: text("#instructions"),
                },
                has_event: !eventFields.hidden,
                event: eventData(),
                acknowledged: document.querySelector("#acknowledge").checked,
                files: await Promise.all(files.map(async (file) => ({
                    name: file.name,
                    dataUrl: await fileAsDataUrl(file),
                }))),
            };
            const response = await fetch("/api/orders", {
                method: "POST",
                headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
                body: JSON.stringify(payload),
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.detail || "The order request could not be submitted.");
            form.hidden = true;
            document.querySelector("#stepper").hidden = true;
            const confirmation = document.querySelector("#confirmation");
            document.querySelector("#confirm-ref").textContent = result.reference;
            document.querySelector("#confirm-details").textContent = `${result.product.name} · ${result.customization.quantity} × · ${result.status}`;
            document.querySelector("#confirm-track").href = `/customer-portal?email=${encodeURIComponent(result.customer.email)}&ref=${encodeURIComponent(result.reference)}`;
            confirmation.hidden = false;
            idempotencyKey = createIdempotencyKey();
            confirmation.scrollIntoView({ behavior: "smooth", block: "start" });
        } catch (error) {
            setError(error instanceof Error ? error.message : "The order request could not be submitted.");
        } finally {
            button.disabled = false;
            button.textContent = "Submit order request";
        }
    }

    categorySelect.addEventListener("change", () => populateProducts(""));
    itemSelect.addEventListener("change", renderProduct);
    document.querySelector("#btn-next").addEventListener("click", () => {
        if (validateStep(activeStep)) showStep(Math.min(steps.length, activeStep + 1));
    });
    document.querySelector("#btn-back").addEventListener("click", () => showStep(Math.max(1, activeStep - 1)));
    document.querySelector("#edit-order").addEventListener("click", () => showStep(1));
    document.querySelector("#btn-cancel").addEventListener("click", () => {
        if (window.confirm("Cancel this order request and return to the product catalog?")) window.location.assign("/services");
    });
    uploadInput.addEventListener("change", () => {
        setError("");
        const allowed = new Set(["image/jpeg", "image/png", "application/pdf"]);
        const chosen = [...uploadInput.files];
        const invalid = chosen.find((file) => !allowed.has(file.type) || !/\.(jpe?g|png|pdf)$/i.test(file.name));
        if (invalid) {
            setError(`${invalid.name}: choose a JPG, JPEG, PNG, or PDF file.`);
            uploadInput.value = "";
            return;
        }
        const oversized = chosen.find((file) => file.size > 5 * 1024 * 1024);
        if (oversized) {
            setError(`${oversized.name}: each file must be no larger than 5 MB.`);
            uploadInput.value = "";
            return;
        }
        if (files.length + chosen.length > 3) {
            setError("Attach up to 3 reference files.");
            uploadInput.value = "";
            return;
        }
        files.push(...chosen);
        renderFiles();
        uploadInput.value = "";
    });
    form.addEventListener("input", () => {
        if (activeStep === 4) renderSummary();
    });
    form.addEventListener("submit", submitOrder);

    populateCategories();
    showStep(1);
    fetch("/api/customer/session")
        .then(async (response) => {
            if (response.status === 401) return;
            if (!response.ok) throw new Error("Your saved account details could not be loaded.");
            const result = await response.json();
            for (const [selector, value] of [
                ["#name", result.customer.name],
                ["#email", result.customer.email],
                ["#phone", result.customer.phone],
                ["#messenger", result.customer.messenger],
            ]) {
                document.querySelector(selector).value = value || "";
            }
            document.querySelector("#email").readOnly = true;
        })
        .catch((error) => {
            document.querySelector("#email").readOnly = false;
            setError(error instanceof Error ? error.message : "Your account details could not be loaded.");
        });
})();
