"use strict";

/**
 * Admin catalog management: categories, products, each product's order-form fields and options, and prices.
 * Everything is saved through /api/admin/catalog and is what the customer order form renders.
 * dashboard.js calls renderServices() / renderPricing() when those views are shown.
 */
(() => {
    const FIELD_TYPE_LABELS = {
        select: "Dropdown",
        radio: "Choice buttons",
        checkboxes: "Checkboxes (multi-select)",
        text: "Short text",
        textarea: "Long text",
        number: "Number",
        date: "Date",
        time: "Time",
        file: "File upload",
    };
    const OPTION_TYPES = new Set(["select", "radio", "checkboxes"]);
    const PRICING_LABELS = { unit: "Per piece", area: "Per square foot (by size)", quote: "Always by quotation" };
    const PRICE_TYPE_LABELS = { per_unit: "per piece", per_order: "per order", per_sqft: "per sq ft" };
    const EVENT_LABELS = { none: "Not collected", optional: "Optional", required: "Required" };

    const state = { catalog: null, loading: null, error: "" };

    const money = (value) => new Intl.NumberFormat("en-PH", { style: "currency", currency: "PHP" }).format(value);
    const dialog = () => document.querySelector("#admin-dialog");
    const dialogContent = () => document.querySelector("#dialog-content");

    function h(tag, attributes = {}, ...children) {
        const element = document.createElement(tag);
        for (const [name, value] of Object.entries(attributes)) {
            if (value === undefined || value === null || value === false) continue;
            if (name === "className") element.className = value;
            else if (name === "text") element.textContent = value;
            else if (name === "dataset") Object.assign(element.dataset, value);
            else if (name === "value") element.value = value;
            else if (name === "checked") element.checked = Boolean(value);
            else if (value === true) element.setAttribute(name, "");
            else element.setAttribute(name, String(value));
        }
        element.append(...children.filter((child) => child !== null && child !== undefined && child !== false));
        return element;
    }

    function toast(message) {
        const region = document.querySelector("#toast-region");
        if (!region) return;
        const element = h("div", { className: "toast", text: message });
        region.append(element);
        window.setTimeout(() => element.remove(), 3600);
    }

    async function api(method, url, body) {
        const response = await fetch(url, {
            method,
            credentials: "same-origin",
            headers: body === undefined ? {} : { "Content-Type": "application/json" },
            body: body === undefined ? undefined : JSON.stringify(body),
        });
        if (response.status === 204) return null;
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
            const detail = Array.isArray(result.detail) ? result.detail.map((item) => item.msg).join(" ") : result.detail;
            throw new Error(detail || `The catalog change could not be saved (HTTP ${response.status}).`);
        }
        return result;
    }

    async function load(force = false) {
        if (state.catalog && !force) return state.catalog;
        if (!state.loading) {
            state.loading = api("GET", "/api/admin/catalog")
                .then((catalog) => {
                    state.catalog = catalog;
                    state.error = "";
                    return catalog;
                })
                .catch((error) => {
                    state.error = error instanceof Error ? error.message : "The catalog could not be loaded.";
                    throw error;
                })
                .finally(() => {
                    state.loading = null;
                });
        }
        return state.loading;
    }

    const categories = () => state.catalog?.categories || [];
    const allProducts = () => categories().flatMap((category) => category.products);
    const findProduct = (id) => allProducts().find((product) => product.id === id) || null;

    /** Put a product returned by the API back into the cached catalog (moving it if its category changed). */
    function storeProduct(product) {
        for (const category of categories()) {
            category.products = category.products.filter((item) => item.id !== product.id);
        }
        const target = categories().find((category) => category.id === product.category_id);
        if (target) {
            target.products.push(product);
            target.products.sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name));
        }
    }

    function removeProduct(productId) {
        for (const category of categories()) category.products = category.products.filter((item) => item.id !== productId);
    }

    function priceText(product) {
        if (product.pricing_mode === "quote") return "Always quoted";
        if (product.base_price === null) return "Request a quote (no price set)";
        return product.pricing_mode === "area" ? `${money(product.base_price)} per sq ft` : `${money(product.base_price)} per piece`;
    }

    function rerenderActiveView() {
        if (document.querySelector("[data-catalog-services]")) renderServices(lastServiceFilter);
        if (document.querySelector("[data-catalog-pricing]")) renderPricing();
    }

    function emptyRow(tbody, columns, message) {
        tbody.replaceChildren(h("tr", {}, h("td", { className: "empty-state", colspan: columns, text: message })));
    }

    function statusBadge(active, onText = "Available", offText = "Unavailable") {
        return h("span", { className: `status-badge ${active ? "available" : "unavailable"}`, text: active ? onText : offText });
    }

    function thumb(product) {
        return product.image
            ? h("img", { className: "product-thumb", src: product.image, alt: "" })
            : h("span", { className: "product-thumb product-thumb-empty", "aria-hidden": "true", text: product.name.slice(0, 1) });
    }

    // ================================================================ Services & Products view

    let lastServiceFilter = { search: "", category: "" };

    function renderServices(filter = lastServiceFilter) {
        lastServiceFilter = filter;
        const productRows = document.querySelector("[data-service-rows]");
        const categoryRows = document.querySelector("[data-category-rows]");
        if (!productRows) return;
        if (!state.catalog) {
            emptyRow(productRows, 6, state.error ? `The catalog could not be loaded. ${state.error}` : "Loading product catalog…");
            if (categoryRows) emptyRow(categoryRows, 4, state.error ? "—" : "Loading categories…");
            if (!state.error) load().then(rerenderActiveView, rerenderActiveView);
            return;
        }

        const categoryFilter = document.querySelector("#service-category-filter");
        if (categoryFilter) {
            categoryFilter.replaceChildren(new Option("All categories", ""), ...categories().map((category) => new Option(category.label, category.id)));
            categoryFilter.value = categories().some((category) => category.id === filter.category) ? filter.category : "";
        }

        if (categoryRows) {
            categoryRows.replaceChildren(...categories().map((category) => h(
                "tr",
                {},
                h("td", {}, h("span", { className: "table-primary", text: category.label }), h("span", { className: "table-secondary", text: category.description || "—" })),
                h("td", { text: `${category.products.length} product${category.products.length === 1 ? "" : "s"}` }),
                h("td", {}, statusBadge(category.active, "Shown", "Hidden")),
                h("td", {}, h("button", { className: "table-link", type: "button", dataset: { catalogAction: "edit-category", id: category.id }, text: "Edit" })),
            )));
            if (!categories().length) emptyRow(categoryRows, 4, "No categories yet. Add one to start your catalog.");
        }

        const needle = (filter.search || "").toLowerCase();
        const products = allProducts().filter((product) =>
            (!filter.category || product.category_id === filter.category) &&
            (!needle || `${product.name} ${product.category_label} ${product.description}`.toLowerCase().includes(needle))
        );
        productRows.replaceChildren(...products.map((product) => {
            const optionCount = product.fields.reduce((total, field) => total + field.options.length, 0);
            return h(
                "tr",
                {},
                h("td", {}, h("div", { className: "product-cell" }, thumb(product), h("span", {},
                    h("span", { className: "table-primary", text: product.name }),
                    h("span", { className: "table-secondary", text: product.description || "No description" }),
                ))),
                h("td", { text: product.category_label }),
                h("td", { text: priceText(product) }),
                h("td", {}, h("span", { className: "table-primary", text: `${product.fields.length} fields` }), h("span", { className: "table-secondary", text: `${optionCount} options` })),
                h("td", {}, statusBadge(product.active)),
                h("td", {}, h("div", { className: "table-actions" },
                    h("button", { className: "table-link", type: "button", dataset: { catalogAction: "edit-product", id: product.id }, text: "Configure" }),
                    h("a", { className: "table-link", href: orderPreviewUrl(product), target: "_blank", rel: "noopener", text: "Preview" }),
                )),
            );
        }));
        const count = document.querySelector("[data-service-count]");
        if (count) count.textContent = `${products.length} of ${allProducts().length} products`;
        if (!products.length) emptyRow(productRows, 6, allProducts().length ? "No products match these filters." : "No products yet.");
    }

    function orderPreviewUrl(product) {
        return `/order?${new URLSearchParams({ service: product.category_id, design: product.id })}`;
    }

    // ================================================================ Pricing view

    function renderPricing() {
        const rows = document.querySelector("[data-pricing-rows]");
        if (!rows) return;
        if (!state.catalog) {
            emptyRow(rows, 6, state.error ? `The catalog could not be loaded. ${state.error}` : "Loading administrator-defined prices…");
            if (!state.error) load().then(rerenderActiveView, rerenderActiveView);
            return;
        }
        const products = allProducts();
        rows.replaceChildren(...products.map((product) => {
            const options = product.fields.flatMap((field) => field.options);
            const priced = options.filter((option) => option.price_adjustment).length;
            const quoted = options.filter((option) => option.requires_quote).length;
            const ready = product.pricing_mode !== "quote" && product.base_price !== null;
            return h(
                "tr",
                {},
                h("td", {}, h("span", { className: "table-primary", text: product.name }), h("span", { className: "table-secondary", text: product.active ? "Available" : "Unavailable" })),
                h("td", { text: product.category_label }),
                h("td", { text: PRICING_LABELS[product.pricing_mode] }),
                h("td", {}, h("span", { className: `status-badge ${ready ? "available" : "pending"}`, text: priceText(product) })),
                h("td", { text: `${priced} priced · ${quoted} quote-only · ${options.length} total` }),
                h("td", {}, h("button", { className: "table-link", type: "button", dataset: { catalogAction: "edit-pricing", id: product.id }, text: "Set prices" })),
            );
        }));
        const count = document.querySelector("[data-pricing-count]");
        if (count) count.textContent = `${products.length} products · ${products.filter((product) => product.base_price !== null && product.pricing_mode !== "quote").length} with a published price`;
        if (!products.length) emptyRow(rows, 6, "No products in the catalog yet.");
    }

    // ================================================================ dialog helpers

    function openDialog(node, wide = true) {
        const element = dialog();
        dialogContent().replaceChildren(node);
        element.classList.toggle("admin-dialog-wide", wide);
        if (!element.open) element.showModal();
        element.querySelector(".dialog-frame")?.scrollTo({ top: 0 });
        node.querySelector("input:not([type=hidden]), select, textarea")?.focus({ preventScroll: true });
    }

    function closeDialog() {
        dialog().close();
    }

    function formField(label, control, { full = false, note = "" } = {}) {
        const id = control.id || `catalog-${Math.random().toString(36).slice(2, 9)}`;
        control.id = id;
        return h("div", { className: `form-field${full ? " full" : ""}` }, h("label", { for: id, text: label }), control, note ? h("span", { className: "field-note", text: note }) : null);
    }

    function selectOf(name, entries, value) {
        const select = h("select", { name });
        for (const [optionValue, label] of entries) select.add(new Option(label, optionValue));
        if (value !== undefined && value !== null) select.value = String(value);
        return select;
    }

    function numberOrNull(value) {
        const text = String(value ?? "").trim();
        return text === "" ? null : Number(text);
    }

    async function guarded(button, task) {
        const label = button?.textContent;
        if (button) {
            button.disabled = true;
            button.textContent = "Saving…";
        }
        try {
            await task();
        } catch (error) {
            toast(error instanceof Error ? error.message : "The change could not be saved.");
        } finally {
            if (button?.isConnected) {
                button.disabled = false;
                button.textContent = label;
            }
        }
    }

    function readImage(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.addEventListener("load", () => resolve(reader.result));
            reader.addEventListener("error", () => reject(new Error(`Could not read ${file.name}.`)));
            reader.readAsDataURL(file);
        });
    }

    // ================================================================ category form

    function openCategoryForm(category) {
        const form = h("form", { className: "catalog-form", novalidate: true });
        const label = h("input", { name: "label", required: true, maxlength: 80, value: category?.label || "" });
        const description = h("textarea", { name: "description", maxlength: 300 });
        description.value = category?.description || "";
        const active = selectOf("active", [["true", "Shown to customers"], ["false", "Hidden"]], category ? String(category.active) : "true");
        form.append(
            h("div", { className: "form-grid" },
                formField("Category name", label),
                formField("Visibility", active),
                formField("Description", description, { full: true }),
            ),
        );
        const save = h("button", { className: "button button-primary", type: "submit", text: category ? "Save category" : "Add category" });
        const actions = h("div", { className: "dialog-actions" });
        if (category) {
            const remove = h("button", { className: "button button-danger", type: "button", text: "Delete category" });
            remove.addEventListener("click", () => {
                if (!window.confirm(`Delete the category “${category.label}”? Only empty categories can be deleted.`)) return;
                void guarded(remove, async () => {
                    state.catalog = { ...state.catalog, ...(await api("DELETE", `/api/admin/catalog/categories/${encodeURIComponent(category.id)}`)) };
                    closeDialog();
                    rerenderActiveView();
                    toast("Category deleted.");
                });
            });
            actions.append(remove);
        }
        actions.append(h("button", { className: "button", type: "button", dataset: { action: "close-dialog" }, text: "Cancel" }), save);
        form.append(actions);
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            const payload = { label: label.value.trim(), description: description.value.trim(), active: active.value === "true" };
            void guarded(save, async () => {
                const result = category
                    ? await api("PATCH", `/api/admin/catalog/categories/${encodeURIComponent(category.id)}`, payload)
                    : await api("POST", "/api/admin/catalog/categories", payload);
                state.catalog = { ...state.catalog, ...result };
                closeDialog();
                rerenderActiveView();
                toast(category ? "Category saved." : "Category added.");
            });
        });
        openDialog(h("div", {},
            h("h2", { className: "dialog-heading", text: category ? `Edit category` : "Add a category" }),
            h("p", { className: "dialog-subtitle", text: "Hidden categories and their products are not offered on the customer order form." }),
            form,
        ), false);
    }

    // ================================================================ product editor

    function openProductEditor(productId) {
        const product = productId ? findProduct(productId) : null;
        if (productId && !product) return;
        if (!categories().length) {
            toast("Add a category before adding products.");
            return;
        }
        let imageUpload = null;

        const form = h("form", { className: "catalog-form", novalidate: true });
        const name = h("input", { name: "name", required: true, maxlength: 120, value: product?.name || "" });
        const category = selectOf("category_id", categories().map((item) => [item.id, item.label]), product?.category_id || lastServiceFilter.category || categories()[0].id);
        const description = h("textarea", { name: "description", maxlength: 600 });
        description.value = product?.description || "";
        const active = selectOf("active", [["true", "Available to order"], ["false", "Unavailable"]], product ? String(product.active) : "true");
        const eventMode = selectOf("event_mode", Object.entries(EVENT_LABELS), product?.event_mode || "none");
        const minQuantity = h("input", { name: "min_quantity", type: "number", min: 1, max: 50000, step: 1, required: true, value: product?.min_quantity ?? 1 });
        const maxQuantity = h("input", { name: "max_quantity", type: "number", min: 1, max: 50000, step: 1, required: true, value: product?.max_quantity ?? 10000 });
        const pricingMode = selectOf("pricing_mode", Object.entries(PRICING_LABELS), product?.pricing_mode || "unit");
        const basePrice = h("input", { name: "base_price", type: "number", min: 0, step: "0.01", placeholder: "Not set — customers request a quote", value: product?.base_price ?? "" });
        const imagePath = h("input", { name: "image", maxlength: 300, placeholder: "/frontend/image/…", value: product?.image || "" });
        const imageFile = h("input", { type: "file", accept: "image/jpeg,image/png,image/webp" });
        const preview = h("img", { className: "service-image-preview", alt: "Product image preview" });
        preview.hidden = !product?.image;
        if (product?.image) preview.src = product.image;
        imageFile.addEventListener("change", async () => {
            const file = imageFile.files?.[0];
            if (!file) return;
            if (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > 2 * 1024 * 1024) {
                imageFile.value = "";
                toast("Choose a JPG, PNG, or WebP image of 2 MB or less.");
                return;
            }
            imageUpload = { name: file.name, dataUrl: await readImage(file) };
            preview.src = imageUpload.dataUrl;
            preview.hidden = false;
        });
        const priceLabel = () => (pricingMode.value === "area" ? "Base price per sq ft (PHP)" : "Base price per piece (PHP)");
        const basePriceField = formField(priceLabel(), basePrice, { note: "Leave blank until the shop approves a price. Blank means customers get a quotation." });
        pricingMode.addEventListener("change", () => {
            basePriceField.querySelector("label").textContent = priceLabel();
            basePrice.disabled = pricingMode.value === "quote";
        });
        basePrice.disabled = pricingMode.value === "quote";

        form.append(h("div", { className: "form-grid" },
            formField("Product name", name),
            formField("Category", category),
            formField("Description", description, { full: true }),
            formField("Availability", active),
            formField("Event details on the order form", eventMode, { note: "Celebrant, event type, date, and venue." }),
            formField("Minimum quantity", minQuantity),
            formField("Maximum quantity", maxQuantity),
            formField("Pricing method", pricingMode, { note: "“Per square foot” uses the product's width, height, and measurement unit fields." }),
            basePriceField,
            formField("Upload product image", imageFile, { note: "JPG, PNG, or WebP · 2 MB maximum. Saved to the server." }),
            formField("Or image path", imagePath),
            h("div", { className: "form-field full" }, preview),
        ));

        const save = h("button", { className: "button button-primary", type: "submit", text: product ? "Save product" : "Create product" });
        const actions = h("div", { className: "dialog-actions" });
        if (product) {
            actions.append(h("a", { className: "button", href: orderPreviewUrl(product), target: "_blank", rel: "noopener", text: "Preview order form ↗" }));
            const remove = h("button", { className: "button button-danger", type: "button", text: "Delete product" });
            remove.addEventListener("click", () => {
                if (!window.confirm(`Delete “${product.name}” and its order-form fields? Existing orders keep their saved details. To keep it for later, mark it Unavailable instead.`)) return;
                void guarded(remove, async () => {
                    await api("DELETE", `/api/admin/catalog/products/${encodeURIComponent(product.id)}`);
                    removeProduct(product.id);
                    closeDialog();
                    rerenderActiveView();
                    toast("Product deleted.");
                });
            });
            actions.append(remove);
        }
        actions.append(h("button", { className: "button", type: "button", dataset: { action: "close-dialog" }, text: "Close" }), save);
        form.append(actions);

        form.addEventListener("submit", (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            const payload = {
                name: name.value.trim(),
                category_id: category.value,
                description: description.value.trim(),
                active: active.value === "true",
                event_mode: eventMode.value,
                min_quantity: Number(minQuantity.value),
                max_quantity: Number(maxQuantity.value),
                pricing_mode: pricingMode.value,
                base_price: pricingMode.value === "quote" ? null : numberOrNull(basePrice.value),
                image: imagePath.value.trim(),
            };
            if (imageUpload) payload.image_upload = imageUpload;
            void guarded(save, async () => {
                const result = product
                    ? await api("PATCH", `/api/admin/catalog/products/${encodeURIComponent(product.id)}`, payload)
                    : await api("POST", "/api/admin/catalog/products", payload);
                storeProduct(result.product);
                rerenderActiveView();
                toast(product ? "Product saved. The order form now shows these changes." : "Product created. Add its order-form fields next.");
                openProductEditor(result.product.id);
            });
        });

        const body = h("div", { className: "catalog-editor" },
            h("h2", { className: "dialog-heading", text: product ? product.name : "Add a product" }),
            h("p", { className: "dialog-subtitle", text: "Changes are saved to the database and appear on the customer order form right away." }),
            h("h3", { className: "catalog-section-title", text: "Product details" }),
            form,
        );
        if (product) body.append(fieldListSection(product));
        openDialog(body);
    }

    function describeCondition(product, field) {
        if (!field.depends_on) return "";
        const parent = product.fields.find((item) => item.field_key === field.depends_on);
        if (!parent) return `Shows when “${field.depends_on}” matches (field missing)`;
        const labels = field.depends_values.map((value) => parent.options.find((option) => option.value === value)?.label || value);
        return `Shows when ${parent.label} is ${labels.join(" or ")}`;
    }

    function fieldListSection(product) {
        const list = h("ol", { className: "catalog-field-list" });
        product.fields.forEach((field, index) => {
            const optionText = OPTION_TYPES.has(field.field_type)
                ? `Options: ${field.options.slice(0, 5).map((option) => option.label).join(", ")}${field.options.length > 5 ? ` +${field.options.length - 5} more` : ""}`
                : field.field_type === "file" ? `Up to ${field.max_files} file(s)` : "";
            const meta = h("div", { className: "catalog-field-meta" },
                h("strong", { text: field.label }),
                h("span", { className: "catalog-chip", text: FIELD_TYPE_LABELS[field.field_type] || field.field_type }),
                h("span", { className: "catalog-chip", text: field.required ? "Required" : "Optional" }),
                field.active ? null : h("span", { className: "catalog-chip catalog-chip-muted", text: "Hidden" }),
                optionText ? h("small", { text: optionText }) : null,
                field.depends_on ? h("small", { text: describeCondition(product, field) }) : null,
            );
            const up = h("button", { className: "button button-small", type: "button", "aria-label": `Move ${field.label} up`, text: "↑", disabled: index === 0 });
            const down = h("button", { className: "button button-small", type: "button", "aria-label": `Move ${field.label} down`, text: "↓", disabled: index === product.fields.length - 1 });
            const edit = h("button", { className: "button button-small", type: "button", text: "Edit" });
            const remove = h("button", { className: "button button-small button-danger", type: "button", text: "Delete" });
            const move = (offset) => {
                const ids = product.fields.map((item) => item.id);
                ids.splice(index + offset, 0, ids.splice(index, 1)[0]);
                void guarded(null, async () => {
                    const result = await api("POST", `/api/admin/catalog/products/${encodeURIComponent(product.id)}/field-order`, { field_ids: ids });
                    storeProduct(result.product);
                    openProductEditor(product.id);
                });
            };
            up.addEventListener("click", () => move(-1));
            down.addEventListener("click", () => move(1));
            edit.addEventListener("click", () => openFieldEditor(product.id, field.id));
            remove.addEventListener("click", () => {
                if (!window.confirm(`Delete the “${field.label}” question from ${product.name}?`)) return;
                void guarded(remove, async () => {
                    const result = await api("DELETE", `/api/admin/catalog/fields/${encodeURIComponent(field.id)}`);
                    storeProduct(result.product);
                    rerenderActiveView();
                    openProductEditor(product.id);
                    toast("Field deleted.");
                });
            });
            list.append(h("li", { className: "catalog-field-item" }, meta, h("div", { className: "catalog-row-actions" }, up, down, edit, remove)));
        });
        const add = h("button", { className: "button button-primary button-small", type: "button", text: "＋ Add field" });
        add.addEventListener("click", () => openFieldEditor(product.id, null));
        return h("section", { className: "catalog-fields" },
            h("div", { className: "catalog-section-head" }, h("h3", { className: "catalog-section-title", text: `Order form fields (${product.fields.length})` }), add),
            h("p", { className: "field-note", text: "Customers see these questions, in this order, after choosing this product. Quantity and contact details are always asked." }),
            product.fields.length ? list : h("p", { className: "empty-state", text: "No fields yet. Add the sizes, materials, and other options customers should choose." }),
        );
    }

    // ================================================================ field editor

    function optionRow(option, priceTypeDefault) {
        const label = h("input", { "aria-label": "Option label", required: true, maxlength: 120, value: option.label || "" });
        const price = h("input", { "aria-label": "Price adjustment in PHP", type: "number", step: "0.01", value: option.price_adjustment ?? 0 });
        const priceType = selectOf("price_type", Object.entries(PRICE_TYPE_LABELS), option.price_type || priceTypeDefault);
        priceType.setAttribute("aria-label", "Charged");
        const quote = h("input", { type: "checkbox", "aria-label": "Requires a quotation", checked: option.requires_quote });
        const active = h("input", { type: "checkbox", "aria-label": "Shown to customers", checked: option.active !== false });
        const up = h("button", { className: "button button-small", type: "button", "aria-label": "Move option up", text: "↑" });
        const down = h("button", { className: "button button-small", type: "button", "aria-label": "Move option down", text: "↓" });
        const remove = h("button", { className: "button button-small button-danger", type: "button", "aria-label": "Remove option", text: "✕" });
        const row = h("tr", { dataset: { optionId: option.id || "", value: option.value || "" } },
            h("td", {}, label), h("td", {}, price), h("td", {}, priceType), h("td", { className: "center" }, quote), h("td", { className: "center" }, active),
            h("td", {}, h("div", { className: "catalog-row-actions" }, up, down, remove)),
        );
        up.addEventListener("click", () => row.previousElementSibling?.before(row));
        down.addEventListener("click", () => row.nextElementSibling?.after(row));
        remove.addEventListener("click", () => row.remove());
        row.read = () => ({
            id: row.dataset.optionId || undefined,
            value: row.dataset.value || undefined,
            label: label.value.trim(),
            price_adjustment: Number(price.value || 0),
            price_type: priceType.value,
            requires_quote: quote.checked,
            active: active.checked,
        });
        return row;
    }

    function openFieldEditor(productId, fieldId) {
        const product = findProduct(productId);
        if (!product) return;
        const field = fieldId ? product.fields.find((item) => item.id === fieldId) : null;
        const form = h("form", { className: "catalog-form", novalidate: true });

        const label = h("input", { name: "label", required: true, maxlength: 80, value: field?.label || "" });
        const key = h("input", { name: "field_key", maxlength: 40, pattern: "[a-z][a-z0-9_]{0,39}", placeholder: "Generated from the label", value: field?.field_key || "" });
        const type = selectOf("field_type", Object.entries(FIELD_TYPE_LABELS), field?.field_type || "select");
        const required = selectOf("required", [["true", "Required"], ["false", "Optional"]], field ? String(field.required) : "true");
        const active = selectOf("active", [["true", "Shown"], ["false", "Hidden"]], field ? String(field.active) : "true");
        const placeholder = h("input", { name: "placeholder", maxlength: 160, value: field?.placeholder || "" });
        const help = h("input", { name: "help_text", maxlength: 300, value: field?.help_text || "" });
        const minValue = h("input", { name: "min_value", type: "number", step: "any", value: field?.min_value ?? "" });
        const maxValue = h("input", { name: "max_value", type: "number", step: "any", value: field?.max_value ?? "" });
        const maxLength = h("input", { name: "max_length", type: "number", min: 1, max: 5000, step: 1, value: field?.max_length ?? "" });
        const maxFiles = h("input", { name: "max_files", type: "number", min: 1, max: 10, step: 1, value: field?.max_files ?? 3 });
        const defaultValue = h("select", { name: "default_value" });
        const textDefault = h("input", { name: "default_text", maxlength: 160, value: field && !OPTION_TYPES.has(field.field_type) ? field.default_value : "" });

        const parents = product.fields.filter((item) => OPTION_TYPES.has(item.field_type) && item.id !== field?.id);
        const dependsOn = selectOf("depends_on", [["", "Always show this field"], ...parents.map((item) => [item.field_key, `Only when “${item.label}” is…`])], field?.depends_on || "");
        const dependsValues = h("div", { className: "catalog-check-list" });
        function renderDependsValues() {
            const parent = parents.find((item) => item.field_key === dependsOn.value);
            dependsValues.replaceChildren();
            dependsValues.hidden = !parent;
            if (!parent) return;
            parent.options.forEach((option) => {
                const id = `dep-${option.id}`;
                dependsValues.append(h("label", { for: id },
                    h("input", { id, type: "checkbox", value: option.value, checked: field?.depends_values?.includes(option.value) }),
                    ` ${option.label}`,
                ));
            });
        }
        dependsOn.addEventListener("change", renderDependsValues);
        renderDependsValues();

        const optionBody = h("tbody");
        (field?.options || []).forEach((option) => optionBody.append(optionRow(option, "per_unit")));
        if (!field) ["Option 1", "Option 2"].forEach((labelText) => optionBody.append(optionRow({ label: labelText }, "per_unit")));
        const addOption = h("button", { className: "button button-small", type: "button", text: "＋ Add option" });
        addOption.addEventListener("click", () => {
            const row = optionRow({ label: "" }, "per_unit");
            optionBody.append(row);
            row.querySelector("input").focus();
            refreshDefaults();
        });
        optionBody.addEventListener("input", () => refreshDefaults());
        function refreshDefaults() {
            const current = defaultValue.value || field?.default_value || "";
            defaultValue.replaceChildren(new Option("No default (customer chooses)", ""));
            [...optionBody.rows].forEach((row) => {
                const value = row.dataset.value;
                if (value) defaultValue.add(new Option(row.read().label || value, value));
            });
            defaultValue.value = [...defaultValue.options].some((option) => option.value === current) ? current : "";
        }
        refreshDefaults();

        const optionsSection = h("section", { className: "catalog-options" },
            h("div", { className: "catalog-section-head" }, h("h3", { className: "catalog-section-title", text: "Options" }), addOption),
            h("p", { className: "field-note", text: "Price adjustments are added to the base price. Leave 0 when an option costs nothing extra. Prices only apply once the product has a base price." }),
            h("div", { className: "table-wrap" }, h("table", { className: "data-table catalog-options-table" },
                h("thead", {}, h("tr", {}, ...["Option label", "Price adj. (PHP)", "Charged", "Quote only", "Shown", ""].map((text) => h("th", { scope: "col", text })))),
                optionBody,
            )),
        );

        const groups = {
            number: h("div", { className: "form-grid" }, formField("Minimum value", minValue), formField("Maximum value", maxValue)),
            text: formField("Maximum length (characters)", maxLength),
            file: formField("Maximum files", maxFiles, { note: "JPG, PNG, or PDF · 5 MB each. Up to 10." }),
            defaultOption: formField("Default choice", defaultValue, { note: "New options get a default value after you save." }),
            defaultText: formField("Default value", textDefault),
            placeholder: formField("Placeholder", placeholder),
        };
        function refreshType() {
            const kind = type.value;
            const isOption = OPTION_TYPES.has(kind);
            groups.number.hidden = kind !== "number";
            groups.text.hidden = !["text", "textarea"].includes(kind);
            groups.file.hidden = kind !== "file";
            groups.defaultOption.hidden = !isOption;
            groups.defaultText.hidden = isOption || kind === "file";
            groups.placeholder.hidden = !["text", "textarea", "number"].includes(kind);
            optionsSection.hidden = !isOption;
            optionBody.querySelectorAll("input, select").forEach((control) => { control.disabled = !isOption; });
        }
        type.addEventListener("change", refreshType);

        form.append(
            h("div", { className: "form-grid" },
                formField("Question label", label),
                formField("Field key", key, { note: "Stored with orders. Lowercase letters, numbers, and underscores." }),
                formField("Field type", type),
                formField("Required?", required),
                formField("Help text", help, { full: true }),
                formField("Visibility", active),
                groups.placeholder,
                groups.defaultOption,
                groups.defaultText,
                groups.text,
                groups.file,
                formField("Show this field", dependsOn, { full: true, note: "Use this to show a question only for certain choices, e.g. Envelope color only for a colored envelope." }),
                h("div", { className: "form-field full" }, dependsValues),
            ),
            groups.number,
            optionsSection,
        );
        refreshType();

        const back = h("button", { className: "button", type: "button", text: "← Back to product" });
        back.addEventListener("click", () => openProductEditor(product.id));
        const save = h("button", { className: "button button-primary", type: "submit", text: field ? "Save field" : "Add field" });
        form.append(h("div", { className: "dialog-actions" }, back, save));

        form.addEventListener("submit", (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            const kind = type.value;
            const payload = {
                label: label.value.trim(),
                field_type: kind,
                required: required.value === "true",
                active: active.value === "true",
                help_text: help.value.trim(),
                placeholder: ["text", "textarea", "number"].includes(kind) ? placeholder.value.trim() : "",
                min_value: kind === "number" ? numberOrNull(minValue.value) : null,
                max_value: kind === "number" ? numberOrNull(maxValue.value) : null,
                max_length: ["text", "textarea"].includes(kind) ? numberOrNull(maxLength.value) : null,
                max_files: kind === "file" ? Number(maxFiles.value || 1) : 1,
                depends_on: dependsOn.value,
                depends_values: [...dependsValues.querySelectorAll("input:checked")].map((input) => input.value),
                default_value: OPTION_TYPES.has(kind) ? defaultValue.value : kind === "file" ? "" : textDefault.value.trim(),
            };
            if (key.value.trim()) payload.field_key = key.value.trim();
            if (OPTION_TYPES.has(kind)) {
                payload.options = [...optionBody.rows].map((row) => row.read());
                if (!payload.options.length) {
                    toast("Add at least one option.");
                    return;
                }
                if (payload.options.some((option) => !option.label)) {
                    toast("Every option needs a label.");
                    return;
                }
            }
            if (payload.depends_on && !payload.depends_values.length) {
                toast("Choose which options make this field appear, or set it to always show.");
                return;
            }
            void guarded(save, async () => {
                const result = field
                    ? await api("PATCH", `/api/admin/catalog/fields/${encodeURIComponent(field.id)}`, payload)
                    : await api("POST", `/api/admin/catalog/products/${encodeURIComponent(product.id)}/fields`, payload);
                storeProduct(result.product);
                rerenderActiveView();
                toast(field ? "Field saved." : "Field added to the order form.");
                openProductEditor(product.id);
            });
        });

        openDialog(h("div", { className: "catalog-editor" },
            h("p", { className: "catalog-breadcrumb", text: `${product.category_label} / ${product.name}` }),
            h("h2", { className: "dialog-heading", text: field ? `Edit “${field.label}”` : "Add a field" }),
            h("p", { className: "dialog-subtitle", text: "This question appears only on this product's order form." }),
            form,
        ));
    }

    // ================================================================ pricing editor

    function openPricingEditor(productId) {
        const product = findProduct(productId);
        if (!product) return;
        const form = h("form", { className: "catalog-form", novalidate: true });
        const mode = selectOf("pricing_mode", Object.entries(PRICING_LABELS), product.pricing_mode);
        const base = h("input", { name: "base_price", type: "number", min: 0, step: "0.01", placeholder: "Not set — customers request a quote", value: product.base_price ?? "" });
        const baseField = formField("", base, { note: "Leave blank to keep this product on “Request a quote”." });
        const refreshBase = () => {
            baseField.querySelector("label").textContent = mode.value === "area" ? "Base price per square foot (PHP)" : "Base price per piece (PHP)";
            base.disabled = mode.value === "quote";
        };
        mode.addEventListener("change", refreshBase);
        refreshBase();
        form.append(h("div", { className: "form-grid" }, formField("Pricing method", mode), baseField));

        const rows = [];
        const optionFields = product.fields.filter((field) => OPTION_TYPES.has(field.field_type) && field.options.length);
        optionFields.forEach((field) => {
            const body = h("tbody");
            field.options.forEach((option) => {
                const price = h("input", { type: "number", step: "0.01", "aria-label": `${option.label} price adjustment`, value: option.price_adjustment });
                const charge = selectOf("price_type", Object.entries(PRICE_TYPE_LABELS), option.price_type);
                charge.setAttribute("aria-label", `${option.label} charged`);
                const quote = h("input", { type: "checkbox", "aria-label": `${option.label} requires quotation`, checked: option.requires_quote });
                rows.push(() => ({ id: option.id, price_adjustment: Number(price.value || 0), price_type: charge.value, requires_quote: quote.checked }));
                body.append(h("tr", {}, h("td", { text: option.label + (option.active ? "" : " (hidden)") }), h("td", {}, price), h("td", {}, charge), h("td", { className: "center" }, quote)));
            });
            form.append(h("fieldset", { className: "catalog-price-group" },
                h("legend", { text: field.label }),
                h("div", { className: "table-wrap" }, h("table", { className: "data-table catalog-options-table" },
                    h("thead", {}, h("tr", {}, ...["Option", "Adds (PHP)", "Charged", "Quote only"].map((text) => h("th", { scope: "col", text })))),
                    body,
                )),
            ));
        });
        if (!optionFields.length) form.append(h("p", { className: "field-note", text: "This product has no choice fields with options yet." }));

        const save = h("button", { className: "button button-primary", type: "submit", text: "Save prices" });
        form.append(h("div", { className: "dialog-actions" },
            h("button", { className: "button", type: "button", dataset: { action: "close-dialog" }, text: "Cancel" }),
            save,
        ));
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            void guarded(save, async () => {
                const result = await api("POST", `/api/admin/catalog/products/${encodeURIComponent(product.id)}/pricing`, {
                    pricing_mode: mode.value,
                    base_price: mode.value === "quote" ? null : numberOrNull(base.value),
                    options: rows.map((read) => read()),
                });
                storeProduct(result.product);
                closeDialog();
                rerenderActiveView();
                toast(`Prices saved for ${product.name}. New estimates use them immediately.`);
            });
        });

        openDialog(h("div", { className: "catalog-editor" },
            h("p", { className: "catalog-breadcrumb", text: `${product.category_label} / ${product.name}` }),
            h("h2", { className: "dialog-heading", text: "Set prices" }),
            h("p", { className: "dialog-subtitle", text: "Enter only prices approved by the shop. Estimate = (base price + per-piece options) × quantity + per-order options. For size-based products, the base and per-sq-ft options are multiplied by the area first. Options marked “Quote only” switch the order to a quotation." }),
            form,
        ));
    }

    // ================================================================ events

    document.addEventListener("click", (event) => {
        const trigger = event.target.closest("[data-catalog-action]");
        if (!trigger) return;
        event.preventDefault();
        const id = trigger.dataset.id;
        const run = async () => {
            try {
                await load();
            } catch {
                toast(state.error || "The catalog could not be loaded.");
                return;
            }
            switch (trigger.dataset.catalogAction) {
                case "add-category": openCategoryForm(null); break;
                case "edit-category": openCategoryForm(categories().find((category) => category.id === id) || null); break;
                case "add-product": openProductEditor(null); break;
                case "edit-product": openProductEditor(id); break;
                case "edit-pricing": openPricingEditor(id); break;
                case "reload-catalog":
                    await load(true);
                    rerenderActiveView();
                    toast("Catalog reloaded.");
                    break;
                default: break;
            }
        };
        void run();
    });

    // The script loads at the end of <body>, so the shared dialog already exists.
    dialog()?.addEventListener("close", () => dialog().classList.remove("admin-dialog-wide"));

    window.PrintShopCatalogAdmin = { renderServices, renderPricing, reload: () => load(true) };
})();
