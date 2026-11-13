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
    const SECTION_LABELS = { "": "Basic specifications", design: "Design customization", event: "Event details", upload: "Uploads" };

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
        const failed = /could not|must|invalid|choose|add at least|every option|add a category/i.test(message);
        window.AdminUI?.toast(message, failed ? "error" : "success");
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
        if (document.querySelector("[data-service-table]")) renderServices();
        if (document.querySelector("[data-pricing-table]")) renderPricing();
    }

    function emptyRow(tbody, columns, message) {
        tbody.replaceChildren(h("tr", {}, h("td", { className: "empty-state", colspan: columns, text: message })));
    }

    function statusBadge(active, onText = "Available", offText = "Unavailable") {
        return h("span", { className: `status-badge ${active ? "available" : "unavailable"}`, text: active ? onText : offText });
    }

    function imagesOf(item) {
        return item.images && item.images.length ? item.images : (item.image ? [item.image] : []);
    }

    /** Open product or package photos in the large viewer (zoom, pan, fullscreen). */
    function viewImages(images, name, index = 0) {
        UI().openViewer(images.map((src, position) => ({
            src,
            name: `${name} — photo ${position + 1}`,
            caption: `${name} · photo ${position + 1} of ${images.length}${position === 0 ? " · primary photo" : ""}`,
            original: src,
        })), index);
    }

    /** A large thumbnail that opens the viewer. Clicking it does not open the row's editor. */
    function thumb(item) {
        const images = imagesOf(item);
        if (!images.length) return h("span", { className: "product-thumb product-thumb-empty", "aria-hidden": "true", text: item.name.slice(0, 1) });
        const button = h("button", { className: "product-thumb-button", type: "button", title: "View photos (zoom and fullscreen)", "aria-label": `View photos of ${item.name}` },
            h("img", { className: "product-thumb", src: images[0], alt: "", loading: "lazy", decoding: "async" }),
            images.length > 1 ? h("span", { className: "thumb-count", text: `${images.length}` }) : null,
        );
        button.addEventListener("click", (event) => {
            event.stopPropagation();
            viewImages(images, item.name);
        });
        return button;
    }

    /**
     * Photo manager for a product or package: large previews (aspect ratio kept), view with zoom and
     * fullscreen, reorder, remove, add uploads, or add a bundled image by its site path.
     * value() → { images: [...saved paths in order], image_uploads: [{ name, dataUrl }] }.
     */
    function imageManager(initial, { name = "Photo", maxImages = 12 } = {}) {
        const kept = [...initial];
        const uploads = [];
        const grid = h("div", { className: "image-manager-grid" });
        const fileInput = h("input", { type: "file", accept: "image/jpeg,image/png,image/webp", multiple: true });
        const pathInput = h("input", { type: "text", maxlength: 300, placeholder: "/frontend/image/…" });
        const addPath = h("button", { className: "button button-small", type: "button", text: "Add path" });
        const count = h("span", { className: "field-note" });
        const all = () => [...kept, ...uploads.map((upload) => upload.dataUrl)];

        function render() {
            const sources = all();
            count.textContent = `${sources.length} of ${maxImages} photos. The first photo is the main image customers see first.${uploads.length ? " New photos are added after the saved ones; reorder them after saving." : ""}`;
            grid.replaceChildren(...sources.map((src, index) => {
                const saved = index < kept.length;
                const view = h("button", { className: "image-tile-preview", type: "button", title: "View larger (zoom and fullscreen)", "aria-label": `View photo ${index + 1}` },
                    h("img", { src, alt: "", loading: "lazy", decoding: "async" }));
                view.addEventListener("click", () => viewImages(all(), name, index));
                const left = h("button", { className: "button button-small", type: "button", text: "◀", "aria-label": "Move photo earlier", disabled: !saved || index === 0 });
                const right = h("button", { className: "button button-small", type: "button", text: "▶", "aria-label": "Move photo later", disabled: !saved || index >= kept.length - 1 });
                const remove = h("button", { className: "button button-small button-danger", type: "button", text: "Remove" });
                left.addEventListener("click", () => { kept.splice(index - 1, 0, kept.splice(index, 1)[0]); render(); });
                right.addEventListener("click", () => { kept.splice(index + 1, 0, kept.splice(index, 1)[0]); render(); });
                remove.addEventListener("click", () => {
                    if (saved) kept.splice(index, 1);
                    else uploads.splice(index - kept.length, 1);
                    render();
                });
                return h("figure", { className: "image-tile" },
                    view,
                    h("figcaption", {},
                        h("span", { className: `catalog-chip${index === 0 ? "" : " catalog-chip-muted"}`, text: index === 0 ? "Main photo" : saved ? `Photo ${index + 1}` : "New — not saved yet" }),
                        h("span", { className: "catalog-row-actions" }, left, right, remove)),
                );
            }));
            if (!sources.length) grid.append(h("p", { className: "empty-state", text: "No photos yet. Upload one or more photos." }));
        }

        fileInput.addEventListener("change", async () => {
            const files = [...(fileInput.files || [])];
            fileInput.value = "";
            for (const file of files) {
                if (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > 2 * 1024 * 1024) {
                    toast(`${file.name}: choose a JPG, PNG, or WebP image of 2 MB or less.`);
                    continue;
                }
                if (all().length >= maxImages) {
                    toast(`A product or package can have at most ${maxImages} photos.`);
                    break;
                }
                uploads.push({ name: file.name, dataUrl: await readImage(file) });
            }
            render();
        });
        addPath.addEventListener("click", () => {
            const path = pathInput.value.trim();
            if (!path.startsWith("/")) {
                toast("Image paths must start with “/”, for example /frontend/image/souvenir/totebag.jpg.");
                return;
            }
            if (!kept.includes(path) && all().length < maxImages) kept.push(path);
            pathInput.value = "";
            render();
        });
        render();
        const element = h("div", { className: "image-manager" },
            grid,
            count,
            h("div", { className: "image-manager-actions" },
                h("label", { className: "button button-small image-upload-button" }, "＋ Upload photos", fileInput),
                h("span", { className: "image-path-field" }, pathInput, addPath)),
        );
        return {
            element,
            value: () => ({ images: [...kept], image_uploads: uploads.map((upload) => ({ name: upload.name, dataUrl: upload.dataUrl })) }),
        };
    }

    // ================================================================ Services & Products view

    const UI = () => window.AdminUI;
    const views = { products: null, pricing: null, packages: null };
    const serviceFilter = { search: "", category: "", availability: "" };
    const pricingFilter = { search: "", category: "", state: "" };

    /** Bind a toolbar control once per page render (the view's DOM is replaced on navigation). */
    function bindControl(selector, event, handler) {
        const control = document.querySelector(selector);
        if (!control || control.dataset.bound) return control;
        control.dataset.bound = "true";
        control.addEventListener(event, handler);
        return control;
    }

    function fillCategorySelect(select, value) {
        if (!select) return;
        select.replaceChildren(new Option("All categories", ""), ...categories().map((category) => new Option(category.label, category.id)));
        select.value = categories().some((category) => category.id === value) ? value : "";
    }

    function productTable(host) {
        if (views.products && views.products.container === host && host.isConnected) return views.products;
        views.products = new (UI().DataTable)(host, {
            columns: [
                { label: "Product", sortKey: "name", render: (product) => h("div", { className: "product-cell" }, thumb(product), h("span", {},
                    h("span", { className: "table-primary", text: product.name }),
                    h("span", { className: "table-secondary", text: product.description || "No description" }),
                )) },
                { label: "Category", sortKey: "category_label", render: (product) => product.category_label },
                { label: "Pricing", sortKey: "base_price", value: (product) => (product.pricing_mode === "quote" ? null : product.base_price), render: (product) => priceText(product) },
                { label: "Order form", sortKey: "fields", value: (product) => product.fields.length, render: (product) => {
                    const optionCount = product.fields.reduce((total, field) => total + field.options.length, 0);
                    return h("span", {}, h("span", { className: "table-primary", text: `${product.fields.length} fields` }), h("span", { className: "table-secondary", text: `${optionCount} options` }));
                } },
                { label: "Availability", sortKey: "active", value: (product) => (product.active ? 0 : 1), render: (product) => statusBadge(product.active) },
                { label: "Order form preview", render: (product) => h("a", { className: "table-link", href: orderPreviewUrl(product), target: "_blank", rel: "noopener", text: "Preview ↗" }) },
            ],
            rows: () => allProducts(),
            filter: (product, filter) => {
                const needle = (filter.search || "").toLowerCase();
                return (!filter.category || product.category_id === filter.category) &&
                    (!filter.availability || (filter.availability === "available") === product.active) &&
                    (!needle || `${product.name} ${product.category_label} ${product.description}`.toLowerCase().includes(needle));
            },
            initialQuery: { ...serviceFilter },
            defaultSort: { key: "category_label", dir: "asc" },
            emptyMessage: "No products yet. Add a product to start the catalog.",
            onRowClick: (product) => openProductEditor(product.id),
            rowTitle: (product) => `Configure ${product.name}`,
            onLoad: (result) => {
                const count = document.querySelector("[data-service-count]");
                if (count) count.textContent = `${result.total} of ${allProducts().length} products`;
            },
        });
        return views.products;
    }

    function renderServices() {
        const host = document.querySelector("[data-service-table]");
        const categoryRows = document.querySelector("[data-category-rows]");
        if (!host) return;
        if (!state.catalog) {
            host.replaceChildren(h("p", { className: "empty-state", text: state.error ? `The catalog could not be loaded. ${state.error}` : "Loading product catalog…" }));
            if (categoryRows) emptyRow(categoryRows, 3, state.error ? "—" : "Loading categories…");
            if (!state.error) load().then(rerenderActiveView, rerenderActiveView);
            return;
        }

        const table = productTable(host);
        const apply = (values) => {
            Object.assign(serviceFilter, values);
            table.setQuery({ ...serviceFilter });
        };
        const search = bindControl("#service-search", "input", (event) => apply({ search: event.target.value.trim() }));
        if (search && !search.value) search.value = serviceFilter.search;
        bindControl("#service-category-filter", "change", (event) => apply({ category: event.target.value }));
        fillCategorySelect(document.querySelector("#service-category-filter"), serviceFilter.category);
        const availability = bindControl("#service-availability-filter", "change", (event) => apply({ availability: event.target.value }));
        if (availability) availability.value = serviceFilter.availability;

        if (categoryRows) {
            categoryRows.replaceChildren(...categories().map((category) => h(
                "tr",
                { className: "clickable-row", tabindex: "0", title: `Edit ${category.label}`, dataset: { catalogAction: "edit-category", id: category.id } },
                h("td", {}, h("span", { className: "table-primary", text: category.label }), h("span", { className: "table-secondary", text: category.description || "—" })),
                h("td", { text: `${category.products.length} product${category.products.length === 1 ? "" : "s"}` }),
                h("td", {}, statusBadge(category.active, "Shown", "Hidden")),
            )));
            if (!categories().length) emptyRow(categoryRows, 3, "No categories yet. Add one to start your catalog.");
        }
        table.setQuery({ ...serviceFilter });
    }

    function orderPreviewUrl(product) {
        return `/order?${new URLSearchParams({ service: product.category_id, design: product.id })}`;
    }

    // ================================================================ Pricing view

    function renderPricing() {
        const host = document.querySelector("[data-pricing-table]");
        if (!host) return;
        if (!state.catalog) {
            host.replaceChildren(h("p", { className: "empty-state", text: state.error ? `The catalog could not be loaded. ${state.error}` : "Loading administrator-defined prices…" }));
            if (!state.error) load().then(rerenderActiveView, rerenderActiveView);
            return;
        }
        if (!views.pricing || views.pricing.container !== host || !host.isConnected) {
            views.pricing = new (UI().DataTable)(host, {
                columns: [
                    { label: "Product", sortKey: "name", render: (product) => h("span", {}, h("span", { className: "table-primary", text: product.name }), h("span", { className: "table-secondary", text: product.active ? "Available" : "Unavailable" })) },
                    { label: "Category", sortKey: "category_label", render: (product) => product.category_label },
                    { label: "Method", sortKey: "pricing_mode", render: (product) => PRICING_LABELS[product.pricing_mode] },
                    { label: "Base price", sortKey: "base_price", value: (product) => (product.pricing_mode === "quote" ? null : product.base_price), render: (product) => {
                        const ready = product.pricing_mode !== "quote" && product.base_price !== null;
                        return h("span", { className: `status-badge ${ready ? "available" : "pending"}`, text: priceText(product) });
                    } },
                    { label: "Option prices", render: (product) => {
                        const options = product.fields.flatMap((field) => field.options);
                        return `${options.filter((option) => option.price_adjustment).length} priced · ${options.filter((option) => option.requires_quote).length} quote-only · ${options.length} total`;
                    } },
                ],
                rows: () => allProducts(),
                filter: (product, filter) => {
                    const needle = (filter.search || "").toLowerCase();
                    const priced = product.pricing_mode !== "quote" && product.base_price !== null;
                    return (!filter.category || product.category_id === filter.category) &&
                        (!filter.state || (filter.state === "priced") === priced) &&
                        (!needle || `${product.name} ${product.category_label}`.toLowerCase().includes(needle));
                },
                initialQuery: { ...pricingFilter },
                defaultSort: { key: "category_label", dir: "asc" },
                emptyMessage: "No products in the catalog yet.",
                onRowClick: (product) => openPricingEditor(product.id),
                rowTitle: (product) => `Set prices for ${product.name}`,
                onLoad: () => {
                    const count = document.querySelector("[data-pricing-count]");
                    const products = allProducts();
                    if (count) count.textContent = `${products.length} products · ${products.filter((product) => product.base_price !== null && product.pricing_mode !== "quote").length} with a published price`;
                },
            });
        }
        const apply = (values) => {
            Object.assign(pricingFilter, values);
            views.pricing.setQuery({ ...pricingFilter });
        };
        bindControl("#pricing-search", "input", (event) => apply({ search: event.target.value.trim() }));
        bindControl("#pricing-category-filter", "change", (event) => apply({ category: event.target.value }));
        bindControl("#pricing-state-filter", "change", (event) => apply({ state: event.target.value }));
        fillCategorySelect(document.querySelector("#pricing-category-filter"), pricingFilter.category);
        views.pricing.setQuery({ ...pricingFilter });
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
        const form = h("form", { className: "catalog-form", novalidate: true });
        const photos = imageManager(product ? imagesOf(product) : [], { name: product?.name || "New product" });
        const name = h("input", { name: "name", required: true, maxlength: 120, value: product?.name || "" });
        const category = selectOf("category_id", categories().map((item) => [item.id, item.label]), product?.category_id || serviceFilter.category || categories()[0].id);
        const description = h("textarea", { name: "description", maxlength: 600 });
        description.value = product?.description || "";
        const active = selectOf("active", [["true", "Available to order"], ["false", "Unavailable"]], product ? String(product.active) : "true");
        const eventMode = selectOf("event_mode", Object.entries(EVENT_LABELS), product?.event_mode || "none");
        const minQuantity = h("input", { name: "min_quantity", type: "number", min: 1, max: 50000, step: 1, required: true, value: product?.min_quantity ?? 1 });
        const maxQuantity = h("input", { name: "max_quantity", type: "number", min: 1, max: 50000, step: 1, required: true, value: product?.max_quantity ?? 10000 });
        const pricingMode = selectOf("pricing_mode", Object.entries(PRICING_LABELS), product?.pricing_mode || "unit");
        const basePrice = h("input", { name: "base_price", type: "number", min: 0, step: "0.01", placeholder: "Not set — customers request a quote", value: product?.base_price ?? "" });
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
        ), h("div", { className: "form-field full" },
            h("span", { className: "form-label-strong", text: "Product photos" }),
            h("span", { className: "field-note", text: "Upload several photos (JPG, PNG, or WebP · 2 MB each). Customers see all of them on the services and order pages. Select a photo to view it larger with zoom and fullscreen." }),
            photos.element,
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
                ...photos.value(),
            };
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
        if (product) {
            body.append(packageListSection(product));
            body.append(fieldListSection(product));
        }
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
                h("span", { className: "catalog-chip", text: SECTION_LABELS[field.section] || field.section || SECTION_LABELS[""] }),
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

    // ================================================================ packages (every product)

    function perPieceText(pkg) {
        return pkg.included_quantity ? `${money(pkg.price / pkg.included_quantity)} per piece` : "";
    }

    function packageListSection(product) {
        const listHost = h("div");
        const add = h("button", { className: "button button-primary button-small", type: "button", text: "＋ Add package" });
        add.addEventListener("click", () => openPackageEditor(product, null, { back: () => openProductEditor(product.id) }));
        const largeFormat = product.category_id === "large-format" || product.pricing_mode === "area";
        const section = h("section", { className: "catalog-packages" },
            h("div", { className: "catalog-section-head" }, h("h3", { className: "catalog-section-title", text: "Packages" }), add),
            h("p", { className: "field-note", text: largeFormat
                ? "Optional ready-made packages. Customers can always choose “Customize Order” to give their own size and requirements."
                : "Ready-made packages customers can select on the order page, with their photos, inclusions, and price. Customers can also customize their own order." }),
            listHost,
        );
        const reload = async () => {
            listHost.replaceChildren(h("p", { className: "empty-state", text: "Loading packages…" }));
            try {
                const result = await api("GET", `/api/admin/catalog/products/${encodeURIComponent(product.id)}/packages`);
                const packages = result.packages || [];
                if (!packages.length) {
                    listHost.replaceChildren(h("p", { className: "empty-state", text: "No packages yet. Add the ready-made packages customers can order." }));
                    return;
                }
                listHost.replaceChildren(h("div", { className: "table-wrap" }, h("table", { className: "data-table compact-table" },
                    h("thead", {}, h("tr", {}, ...["", "Package", "Price", "Included", "Add-on", "Availability", ""].map((text) => h("th", { scope: "col", text })))),
                    h("tbody", {}, ...packages.map((pkg) => packageRow(product, pkg, reload))),
                )));
            } catch (error) {
                listHost.replaceChildren(h("p", { className: "empty-state", text: error instanceof Error ? error.message : "Packages could not be loaded." }));
            }
        };
        void reload();
        return section;
    }

    function packageActions(pkg, reload) {
        const toggle = h("button", { className: "button button-small", type: "button", text: pkg.active ? "Deactivate" : "Activate", title: pkg.active ? "Stop offering this package" : "Offer this package again" });
        toggle.addEventListener("click", () => {
            void guarded(toggle, async () => {
                await api("PATCH", `/api/admin/catalog/packages/${encodeURIComponent(pkg.id)}`, { active: !pkg.active });
                toast(pkg.active ? "Package deactivated. Customers no longer see it." : "Package activated. Customers can select it again.");
                await reload();
            });
        });
        const remove = h("button", { className: "button button-small button-danger", type: "button", text: "Delete" });
        remove.addEventListener("click", () => {
            if (!window.confirm(`Delete the package “${pkg.name}”? Existing orders keep their saved package details. To keep it for later, deactivate it instead.`)) return;
            void guarded(remove, async () => {
                await api("DELETE", `/api/admin/catalog/packages/${encodeURIComponent(pkg.id)}`);
                toast("Package deleted.");
                await reload();
            });
        });
        return { toggle, remove };
    }

    function packageRow(product, pkg, reload) {
        const { toggle, remove } = packageActions(pkg, reload);
        const edit = h("button", { className: "button button-small", type: "button", text: "Edit" });
        edit.addEventListener("click", () => openPackageEditor(product, pkg, { back: () => openProductEditor(product.id) }));
        const addOn = pkg.add_on_price === null || pkg.add_on_price === undefined
            ? "—"
            : `${pkg.add_on_label || "Add-on"} ${money(pkg.add_on_price)}`;
        return h("tr", {},
            h("td", {}, thumb(pkg)),
            h("td", {}, h("span", { className: "table-primary", text: pkg.name }), h("span", { className: "table-secondary", text: `${pkg.inclusions.length} inclusion${pkg.inclusions.length === 1 ? "" : "s"} · ${imagesOf(pkg).length} photo${imagesOf(pkg).length === 1 ? "" : "s"}${pkg.size ? ` · ${pkg.size}` : ""}` })),
            h("td", {}, h("span", { className: "table-primary", text: money(pkg.price) }), h("span", { className: "table-secondary", text: perPieceText(pkg) })),
            h("td", { text: `${pkg.included_quantity} pcs` }),
            h("td", { text: addOn }),
            h("td", {}, statusBadge(pkg.active, "Active", "Inactive")),
            h("td", {}, h("div", { className: "catalog-row-actions" }, toggle, edit, remove)),
        );
    }

    /**
     * Add or edit a package. ``back`` reopens the screen the editor came from; ``onSaved`` runs after saving.
     */
    function openPackageEditor(product, pkg, { back = null, onSaved = null } = {}) {
        const form = h("form", { className: "catalog-form", novalidate: true });
        const name = h("input", { name: "name", required: true, maxlength: 120, value: pkg?.name || "" });
        const description = h("textarea", { name: "description", maxlength: 600, rows: 3, placeholder: "What makes this package a good choice" });
        description.value = pkg?.description || "";
        const price = h("input", { name: "price", type: "number", min: "0.01", step: "0.01", required: true, value: pkg?.price ?? "" });
        const size = h("input", { name: "size", maxlength: 80, value: pkg?.size || "", placeholder: 'e.g. 5" × 7"' });
        const material = h("input", { name: "material", maxlength: 120, value: pkg?.material || "", placeholder: "e.g. Canvas, premium photo paper" });
        const included = h("input", { name: "included_quantity", type: "number", min: 1, max: 50000, step: 1, required: true, value: pkg?.included_quantity ?? Math.max(1, product.min_quantity || 1) });
        const addOnLabel = h("input", { name: "add_on_label", maxlength: 120, value: pkg?.add_on_label || "", placeholder: "e.g. Additional piece" });
        const addOnPrice = h("input", { name: "add_on_price", type: "number", min: 0, step: "0.01", value: pkg?.add_on_price ?? "", placeholder: "Leave blank for a fixed quantity" });
        const inclusions = h("textarea", { name: "inclusions", required: true, maxlength: 10000, rows: 6, placeholder: "One inclusion per line" });
        inclusions.value = (pkg?.inclusions || []).join("\n");
        const active = selectOf("active", [["true", "Active — customers can select it"], ["false", "Inactive — hidden from customers"]], pkg ? String(pkg.active) : "true");
        const sortOrder = h("input", { name: "sort_order", type: "number", min: 0, max: 10000, step: 1, value: pkg?.sort_order ?? "", placeholder: "Automatic" });
        const photos = imageManager(pkg ? imagesOf(pkg) : [], { name: pkg?.name || "New package" });
        const perPiece = h("span", { className: "field-note" });
        const refreshPerPiece = () => {
            const value = Number(price.value);
            const quantity = Number(included.value);
            perPiece.textContent = value > 0 && quantity > 0 ? `≈ ${money(value / quantity)} per piece for the included ${quantity.toLocaleString()} pcs.` : "";
        };
        price.addEventListener("input", refreshPerPiece);
        included.addEventListener("input", refreshPerPiece);
        refreshPerPiece();

        form.append(h("div", { className: "form-grid" },
            formField("Package name", name),
            formField("Availability", active),
            formField("Description", description, { full: true }),
            formField("Package price (PHP)", price),
            h("div", { className: "form-field" }, h("label", { for: included.id = `pkg-qty-${Math.random().toString(36).slice(2, 7)}`, text: "Included quantity (pcs)" }), included, perPiece),
            formField("Size", size),
            formField("Material", material),
            formField("Add-on label", addOnLabel, { note: "Shown when customers order more than the included quantity." }),
            formField("Add-on price per extra piece (PHP)", addOnPrice, { note: "Leave blank to lock the order at exactly the included quantity." }),
            formField("Sort order", sortOrder, { note: "Lower numbers appear first. Leave blank to add the package at the end." }),
            formField("Inclusions", inclusions, { full: true, note: "One inclusion per line. Customers see exactly these lines on the package card." }),
        ), h("div", { className: "form-field full" },
            h("span", { className: "form-label-strong", text: "Package photos" }),
            h("span", { className: "field-note", text: "Upload several photos of the package. The first one is shown on the package card; customers can browse all of them." }),
            photos.element,
        ));

        const actions = h("div", { className: "dialog-actions" });
        if (back) {
            const backButton = h("button", { className: "button", type: "button", text: "← Back" });
            backButton.addEventListener("click", back);
            actions.append(backButton);
        } else {
            actions.append(h("button", { className: "button", type: "button", dataset: { action: "close-dialog" }, text: "Close" }));
        }
        if (pkg) {
            const remove = h("button", { className: "button button-danger", type: "button", text: "Delete package" });
            remove.addEventListener("click", () => {
                if (!window.confirm(`Delete the package “${pkg.name}”? Existing orders keep their saved package details. To keep it for later, make it inactive instead.`)) return;
                void guarded(remove, async () => {
                    await api("DELETE", `/api/admin/catalog/packages/${encodeURIComponent(pkg.id)}`);
                    toast("Package deleted.");
                    if (onSaved) await onSaved();
                    if (back) back(); else closeDialog();
                });
            });
            actions.append(remove);
        }
        const save = h("button", { className: "button button-primary", type: "submit", text: pkg ? "Save package" : "Add package" });
        actions.append(save);
        form.append(actions);

        form.addEventListener("submit", (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            const lines = inclusions.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
            if (!lines.length) {
                toast("Add at least one inclusion for this package.");
                return;
            }
            const priceValue = Number(price.value);
            if (!(priceValue > 0)) {
                toast("Package price must be greater than zero.");
                return;
            }
            const payload = {
                name: name.value.trim(),
                description: description.value.trim(),
                price: priceValue,
                size: size.value.trim(),
                material: material.value.trim(),
                included_quantity: Number(included.value),
                add_on_label: addOnLabel.value.trim(),
                add_on_price: addOnPrice.value.trim() === "" ? null : Number(addOnPrice.value),
                inclusions: lines,
                active: active.value === "true",
                ...photos.value(),
            };
            if (sortOrder.value.trim() !== "") payload.sort_order = Number(sortOrder.value);
            void guarded(save, async () => {
                if (pkg) {
                    await api("PATCH", `/api/admin/catalog/packages/${encodeURIComponent(pkg.id)}`, payload);
                } else {
                    await api("POST", `/api/admin/catalog/products/${encodeURIComponent(product.id)}/packages`, payload);
                }
                toast(pkg ? "Package saved. The customer order page shows the changes now." : "Package added. Customers can select it on the order page.");
                if (onSaved) await onSaved();
                if (back) back(); else closeDialog();
            });
        });

        openDialog(h("div", { className: "catalog-editor" },
            h("p", { className: "catalog-breadcrumb", text: `${product.category_label} / ${product.name}` }),
            h("h2", { className: "dialog-heading", text: pkg ? `Edit “${pkg.name}”` : "Add a package" }),
            h("p", { className: "dialog-subtitle", text: "Customers can select this package instead of customizing. Use only prices and inclusions approved by the shop." }),
            form,
        ));
    }

    // ================================================================ Package Management view

    const packageState = { data: null, loading: null, error: "" };
    const packageFilter = { search: "", service: "", product: "", status: "" };

    async function loadPackages(force = false) {
        if (packageState.data && !force) return packageState.data;
        if (!packageState.loading) {
            packageState.loading = api("GET", "/api/admin/catalog/packages")
                .then((data) => {
                    packageState.data = data;
                    packageState.error = "";
                    return data;
                })
                .catch((error) => {
                    packageState.error = error instanceof Error ? error.message : "The packages could not be loaded.";
                    throw error;
                })
                .finally(() => { packageState.loading = null; });
        }
        return packageState.loading;
    }

    async function refreshPackagesView() {
        await Promise.all([loadPackages(true), load(true)]);
        if (document.querySelector("[data-package-table]")) renderPackages();
    }

    function productForPackage(productId) {
        const fromCatalog = findProduct(productId);
        if (fromCatalog) return fromCatalog;
        const listed = packageState.data?.products.find((item) => item.id === productId);
        return listed ? { ...listed, fields: [] } : null;
    }

    function openPackageProductPicker() {
        const products = packageState.data?.products || [];
        if (!products.length) {
            toast("Add a product before adding packages.");
            return;
        }
        const select = h("select", { required: true });
        const groups = new Map();
        products.forEach((product) => {
            if (!groups.has(product.category_label)) groups.set(product.category_label, h("optgroup", { label: product.category_label }));
            groups.get(product.category_label).append(new Option(`${product.name}${product.active ? "" : " (unavailable)"} · ${product.package_count} package${product.package_count === 1 ? "" : "s"}`, product.id));
        });
        select.append(...groups.values());
        if (packageFilter.product) select.value = packageFilter.product;
        const next = h("button", { className: "button button-primary", type: "submit", text: "Continue" });
        const form = h("form", { className: "catalog-form" },
            h("div", { className: "form-grid" }, formField("Product", select, { full: true, note: "Every product can have packages, including tote bags, stickers, souvenirs, Sintra boards, party needs, invitations, and tarpaulins." })),
            h("div", { className: "dialog-actions" }, h("button", { className: "button", type: "button", dataset: { action: "close-dialog" }, text: "Cancel" }), next));
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            const product = productForPackage(select.value);
            if (product) openPackageEditor(product, null, { onSaved: refreshPackagesView });
        });
        openDialog(h("div", {}, h("h2", { className: "dialog-heading", text: "Add a package" }), h("p", { className: "dialog-subtitle", text: "Choose the product this package belongs to." }), form), false);
    }

    function renderPackages() {
        const host = document.querySelector("[data-package-table]");
        if (!host) return;
        if (!packageState.data || !state.catalog) {
            host.replaceChildren(h("p", { className: "empty-state", text: packageState.error ? `The packages could not be loaded. ${packageState.error}` : "Loading packages…" }));
            if (!packageState.error) Promise.all([loadPackages(), load()]).then(renderPackages, renderPackages);
            return;
        }
        const data = packageState.data;
        const packages = data.packages;
        const stats = document.querySelector("[data-package-stats]");
        if (stats) {
            const withPackages = data.products.filter((product) => product.package_count > 0).length;
            const card = (label, value, foot) => h("article", { className: "stat-card" },
                h("span", { className: "stat-top" }, h("span", { className: "stat-label", text: label })),
                h("span", { className: "stat-value", text: String(value) }),
                h("span", { className: "stat-foot", text: foot }));
            stats.replaceChildren(
                card("Total packages", packages.length, "Across every service"),
                card("Active packages", packages.filter((pkg) => pkg.active).length, "Customers can select these"),
                card("Products with packages", withPackages, `of ${data.products.length} products`),
                card("Products without packages", data.products.length - withPackages, "Ordered by customization only"),
            );
        }

        const serviceSelect = document.querySelector("#package-service-filter");
        const productSelect = document.querySelector("#package-product-filter");
        if (serviceSelect && !serviceSelect.dataset.bound) {
            serviceSelect.replaceChildren(new Option("All services", ""), ...data.services.map((service) => new Option(service.label, service.id)));
            serviceSelect.value = packageFilter.service;
        }
        const fillProducts = () => {
            if (!productSelect) return;
            const options = data.products.filter((product) => !packageFilter.service || product.category_id === packageFilter.service);
            productSelect.replaceChildren(new Option("All products", ""), ...options.map((product) => new Option(product.name, product.id)));
            productSelect.value = options.some((product) => product.id === packageFilter.product) ? packageFilter.product : "";
            packageFilter.product = productSelect.value;
        };
        fillProducts();

        if (!views.packages || views.packages.container !== host || !host.isConnected) {
            views.packages = new (UI().DataTable)(host, {
                columns: [
                    { label: "Package", sortKey: "name", render: (pkg) => h("div", { className: "product-cell" }, thumb(pkg), h("span", {},
                        h("span", { className: "table-primary", text: pkg.name }),
                        h("span", { className: "table-secondary", text: pkg.description || pkg.inclusions.slice(0, 2).join(" · ") }),
                    )) },
                    { label: "Product / service", sortKey: "product_name", render: (pkg) => h("span", {}, h("span", { className: "table-primary", text: pkg.product_name }), h("span", { className: "table-secondary", text: pkg.category_label })) },
                    { label: "Price", sortKey: "price", align: "right", render: (pkg) => h("span", {}, h("span", { className: "table-primary", text: money(pkg.price) }), h("span", { className: "table-secondary", text: perPieceText(pkg) })) },
                    { label: "Included", sortKey: "included_quantity", align: "right", render: (pkg) => `${pkg.included_quantity.toLocaleString()} pcs` },
                    { label: "Add-on", render: (pkg) => (pkg.add_on_price === null ? "Fixed quantity" : `${money(pkg.add_on_price)} / extra pc`) },
                    { label: "Photos", value: (pkg) => imagesOf(pkg).length, sortKey: "photos", align: "right", render: (pkg) => String(imagesOf(pkg).length) },
                    { label: "Status", sortKey: "active", value: (pkg) => (pkg.active ? 0 : 1), render: (pkg) => statusBadge(pkg.active, "Active", "Inactive") },
                    { label: "", render: (pkg) => {
                        const { toggle, remove } = packageActions(pkg, refreshPackagesView);
                        return h("div", { className: "catalog-row-actions" }, toggle, remove);
                    } },
                ],
                rows: () => packageState.data?.packages || [],
                filter: (pkg, filter) => {
                    const needle = (filter.search || "").toLowerCase();
                    return (!filter.service || pkg.category_id === filter.service)
                        && (!filter.product || pkg.product_id === filter.product)
                        && (!filter.status || (filter.status === "active") === pkg.active)
                        && (!needle || `${pkg.name} ${pkg.description} ${pkg.product_name} ${pkg.category_label} ${pkg.inclusions.join(" ")}`.toLowerCase().includes(needle));
                },
                initialQuery: { ...packageFilter },
                defaultSort: { key: "product_name", dir: "asc" },
                emptyMessage: "No packages yet. Select “Add package” to create the first one.",
                filteredEmptyMessage: "No packages match these filters.",
                onRowClick: (pkg) => {
                    const product = productForPackage(pkg.product_id);
                    if (product) openPackageEditor(product, pkg, { onSaved: refreshPackagesView });
                },
                rowTitle: (pkg) => `Edit ${pkg.name}`,
                onLoad: (result) => {
                    const count = document.querySelector("[data-package-count]");
                    if (count) count.textContent = `${result.total} of ${packages.length}`;
                },
            });
        }
        const apply = (values) => {
            Object.assign(packageFilter, values);
            views.packages.setQuery({ ...packageFilter });
        };
        const search = bindControl("#package-search", "input", (event) => apply({ search: event.target.value.trim() }));
        if (search && !search.value) search.value = packageFilter.search;
        bindControl("#package-service-filter", "change", (event) => {
            packageFilter.service = event.target.value;
            fillProducts();
            apply({});
        });
        bindControl("#package-product-filter", "change", (event) => apply({ product: event.target.value }));
        const status = bindControl("#package-status-filter", "change", (event) => apply({ status: event.target.value }));
        if (status) status.value = packageFilter.status;
        views.packages.setQuery({ ...packageFilter });

        const productsHost = document.querySelector("[data-package-products]");
        if (productsHost) {
            const rows = [...data.products].sort((a, b) => a.category_label.localeCompare(b.category_label) || a.name.localeCompare(b.name));
            productsHost.replaceChildren(h("div", { className: "table-wrap" }, h("table", { className: "data-table compact-table" },
                h("thead", {}, h("tr", {}, ...["Product", "Service", "Packages", ""].map((text) => h("th", { scope: "col", text })))),
                h("tbody", {}, ...rows.map((product) => {
                    const add = h("button", { className: "button button-small", type: "button", text: "＋ Add package" });
                    add.addEventListener("click", () => {
                        const full = productForPackage(product.id);
                        if (full) openPackageEditor(full, null, { onSaved: refreshPackagesView });
                    });
                    const filterButton = h("button", { className: "button button-small", type: "button", text: "Show", disabled: !product.package_count });
                    filterButton.addEventListener("click", () => {
                        packageFilter.service = product.category_id;
                        if (serviceSelect) serviceSelect.value = product.category_id;
                        fillProducts();
                        packageFilter.product = product.id;
                        if (productSelect) productSelect.value = product.id;
                        apply({});
                        host.scrollIntoView({ behavior: "smooth", block: "start" });
                    });
                    return h("tr", {},
                        h("td", {}, h("span", { className: "table-primary", text: product.name }), product.active ? null : h("span", { className: "table-secondary", text: "Unavailable to order" })),
                        h("td", { text: product.category_label }),
                        h("td", {}, product.package_count ? h("span", { className: "status-badge available", text: `${product.package_count} package${product.package_count === 1 ? "" : "s"}` }) : h("span", { className: "status-badge pending", text: "No packages" })),
                        h("td", {}, h("div", { className: "catalog-row-actions" }, filterButton, add)),
                    );
                })),
            )));
        }
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
        const section = selectOf("section", Object.entries(SECTION_LABELS), field?.section || "");
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
                formField("Form section", section, { note: "Invitation orders group fields into basic specs, design, event, and uploads. Other products show all fields together." }),
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
                section: section.value,
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

    function handleTrigger(event) {
        const trigger = event.target.closest("[data-catalog-action]");
        if (!trigger) return;
        // Links and buttons inside a clickable row keep their own behavior.
        const inner = event.target.closest("a, button, input, select");
        if (inner && inner !== trigger && trigger.contains(inner)) return;
        if (event.type === "keydown" && ((event.key !== "Enter" && event.key !== " ") || event.target !== trigger)) return;
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
                case "add-package":
                    try {
                        await loadPackages();
                    } catch {
                        toast(packageState.error || "The packages could not be loaded.");
                        return;
                    }
                    openPackageProductPicker();
                    break;
                case "reload-packages":
                    await refreshPackagesView();
                    toast("Packages reloaded.");
                    break;
                case "reload-catalog":
                    await load(true);
                    rerenderActiveView();
                    toast("Catalog reloaded.");
                    break;
                default: break;
            }
        };
        void run();
    }
    document.addEventListener("click", handleTrigger);
    document.addEventListener("keydown", handleTrigger);

    // The script loads at the end of <body>, so the shared dialog already exists.
    dialog()?.addEventListener("close", () => dialog().classList.remove("admin-dialog-wide"));

    window.PrintShopCatalogAdmin = { renderServices, renderPricing, renderPackages, reload: () => load(true) };
})();
