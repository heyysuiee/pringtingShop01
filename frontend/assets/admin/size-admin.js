"use strict";

/**
 * Admin Size prices — the "Size prices" tab of Services & Products.
 *
 * The standard size price list of a category (the Tarpaulin sizes: 2 × 3 ft ₱200, 2 × 4 ft ₱250, …). Products
 * priced by "Standard size price list" offer the category's active sizes on the order form; the server prices
 * each order as size price × quantity. Sizes can be added, edited (name, width × height, unit, price, order),
 * deactivated, or removed without code changes. Orders keep the size and price they were placed with.
 *
 * Saved through /api/admin/catalog/sizes. catalog-admin.js calls render() whenever the page renders.
 */
(() => {
    const catalog = window.PrintShopCatalogAdmin;
    if (!catalog?.helpers) return;
    const { h, api, toast, openDialog, closeDialog, formField, selectOf, guarded, statusBadge, bindControl, setTabCount } = catalog.helpers;
    const UI = () => window.AdminUI;
    const money = (value) => new Intl.NumberFormat("en-PH", { style: "currency", currency: "PHP" }).format(value);
    const DEFAULT_CATEGORY = "large-format";

    const state = { data: null, loading: null, error: "", table: null, limits: null };
    const filter = { category: "", status: "" };

    async function load(force = false) {
        if (state.data && !force) return state.data;
        if (!state.loading) {
            state.loading = api("GET", "/api/admin/catalog/sizes")
                .then((data) => {
                    state.data = data;
                    state.error = "";
                    return data;
                })
                .catch((error) => {
                    state.error = error instanceof Error ? error.message : "The size price list could not be loaded.";
                    throw error;
                })
                .finally(() => { state.loading = null; });
        }
        return state.loading;
    }

    /** Keep a save's response (the whole list), redraw, and refresh the product pricing text. */
    function store(data) {
        state.data = { ...state.data, ...data };
        render();
        void catalog.reloadCatalog?.();
    }

    const categories = () => state.data?.categories || [];
    const categoryLabel = (id) => categories().find((item) => item.id === id)?.label || id;
    const sizes = () => state.data?.sizes || [];

    function productsText(categoryId) {
        const products = categories().find((item) => item.id === categoryId)?.size_priced_products || [];
        if (!products.length) return "No product uses this price list yet (set a product's pricing method to “Standard size price list”).";
        return `Used by ${products.length} product${products.length === 1 ? "" : "s"}: ${products.map((item) => item.name).join(", ")}.`;
    }

    function dimensionText(size) {
        const value = (number) => (Number.isInteger(number) ? String(number) : number.toFixed(2).replace(/0+$/, "").replace(/\.$/, ""));
        return `${value(size.width)} × ${value(size.height)} ${size.unit}`;
    }

    function actions(size) {
        const toggle = h("button", { className: "button button-small", type: "button", text: size.active ? "Deactivate" : "Activate" });
        toggle.addEventListener("click", (event) => {
            event.stopPropagation();
            void guarded(toggle, async () => {
                store(await api("PATCH", `/api/admin/catalog/sizes/${encodeURIComponent(size.id)}`, { active: !size.active }));
                toast(size.active ? `${size.label} is hidden from the order form. Existing orders are unchanged.` : `${size.label} is available again.`);
            });
        });
        const edit = h("button", { className: "button button-small", type: "button", text: "Edit" });
        edit.addEventListener("click", (event) => {
            event.stopPropagation();
            openEditor(size);
        });
        return h("div", { className: "catalog-row-actions" }, edit, toggle);
    }

    function openEditor(size) {
        const creating = !size;
        const form = h("form", { className: "catalog-form", novalidate: true });
        const categoryChoices = categories().map((item) => [item.id, item.label]);
        const category = selectOf("category_id", categoryChoices, size?.category_id || filter.category || DEFAULT_CATEGORY);
        if (!categoryChoices.some(([id]) => id === category.value) && categoryChoices.length) category.value = categoryChoices[0][0];
        const width = h("input", { name: "width", type: "number", min: "0.1", max: "1000", step: "0.01", required: true, value: size?.width ?? "" });
        const height = h("input", { name: "height", type: "number", min: "0.1", max: "1000", step: "0.01", required: true, value: size?.height ?? "" });
        const unit = selectOf("unit", (state.data?.units || ["ft", "in", "cm", "m"]).map((item) => [item, item]), size?.unit || "ft");
        const price = h("input", { name: "price", type: "number", min: "0.01", step: "0.01", required: true, value: size?.price ?? "", placeholder: "e.g. 350" });
        const label = h("input", { name: "label", maxlength: 60, value: size?.label || "", placeholder: "Leave blank to use the dimensions (e.g. 3 × 5 ft)" });
        const order = h("input", { name: "sort_order", type: "number", min: 0, max: 10000, step: 1, value: size?.sort_order ?? "" , placeholder: "Automatic" });
        const active = selectOf("active", [["true", "Active — customers can choose it"], ["false", "Inactive — hidden from the order form"]], String(size ? size.active : true));
        const usage = h("p", { className: "field-note full" });
        const syncUsage = () => { usage.textContent = productsText(category.value); };
        category.addEventListener("change", syncUsage);
        syncUsage();

        form.append(h("div", { className: "form-grid" },
            formField("Category", category),
            formField("Status", active),
            formField("Width", width),
            formField("Height", height),
            formField("Unit", unit),
            formField("Price per piece (PHP)", price, { note: "The order total is this price × the quantity." }),
            formField("Display name", label, { full: true }),
            formField("Display order", order, { note: "Lower numbers are listed first." }),
            usage,
        ));
        const save = h("button", { className: "button button-primary", type: "submit", text: creating ? "Add size" : "Save size" });
        const buttons = h("div", { className: "dialog-actions" });
        if (!creating) {
            const remove = h("button", { className: "button button-danger", type: "button", text: "Delete size" });
            remove.addEventListener("click", async () => {
                const confirmed = await UI().confirm({
                    title: "Delete this size?",
                    message: `Delete ${size.label} (${money(size.price)}) from the price list? Orders already placed keep this size and price. To offer it again later, deactivate it instead.`,
                    confirmLabel: "Delete size",
                    tone: "danger",
                });
                if (!confirmed) return;
                void guarded(remove, async () => {
                    store(await api("DELETE", `/api/admin/catalog/sizes/${encodeURIComponent(size.id)}`));
                    closeDialog();
                    toast("Size deleted. Existing orders are unchanged.");
                });
            });
            buttons.append(remove);
        }
        buttons.append(h("button", { className: "button", type: "button", dataset: { action: "close-dialog" }, text: "Close" }), save);
        form.append(buttons);

        form.addEventListener("submit", (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            const payload = {
                category_id: category.value,
                width: Number(width.value),
                height: Number(height.value),
                unit: unit.value,
                price: Number(price.value),
                label: label.value.trim(),
                active: active.value === "true",
            };
            if (order.value !== "") payload.sort_order = Number(order.value);
            void guarded(save, async () => {
                const result = creating
                    ? await api("POST", "/api/admin/catalog/sizes", payload)
                    : await api("PATCH", `/api/admin/catalog/sizes/${encodeURIComponent(size.id)}`, payload);
                store(result);
                closeDialog();
                toast(creating ? `${result.size.label} added at ${money(result.size.price)}.` : `${result.size.label} saved. New orders use ${money(result.size.price)}; existing orders keep their price.`);
            });
        });

        openDialog(h("div", { className: "catalog-editor" },
            h("h2", { className: "dialog-heading", text: creating ? "Add a standard size" : `Edit ${size.label}` }),
            h("p", { className: "dialog-subtitle", text: "Prices come from this list only; the server recalculates every order from it. Changes apply to new orders immediately." }),
            form,
        ), false);
    }

    function renderLimits() {
        const host = document.querySelector("[data-upload-limits]");
        if (!host) return;
        const limits = state.limits;
        host.textContent = limits
            ? `Upload limits: ${limits.large_format_mb} MB per file for ${limits.large_format_categories.map(categoryLabel).join(", ") || "large-format"} orders, ${limits.standard_mb} MB for other services (server settings PRINTSHOP_LARGE_FORMAT_MAX_UPLOAD_MB and PRINTSHOP_MAX_UPLOAD_MB).`
            : "";
    }

    function render() {
        const host = document.querySelector("[data-size-table]");
        if (!host) return;
        bindActions();
        if (!state.data) {
            host.replaceChildren(h("p", { className: "empty-state", text: state.error ? `The size price list could not be loaded. ${state.error} Select Reload to try again.` : "Loading the size price list…" }));
            state.table = null;
            if (!state.error && !state.loading) load().then(render, render);
            return;
        }
        if (!state.limits) {
            api("GET", "/api/admin/catalog").then((data) => { state.limits = data.upload_limits || null; renderLimits(); }, () => {});
        }
        renderLimits();
        setTabCount("sizes", sizes().length);

        const categorySelect = document.querySelector("#size-category-filter");
        if (categorySelect) {
            categorySelect.replaceChildren(new Option("All categories", ""), ...categories().map((item) => new Option(item.label, item.id)));
            categorySelect.value = categories().some((item) => item.id === filter.category) ? filter.category : "";
            filter.category = categorySelect.value;
        }

        if (!state.table || state.table.container !== host || !host.isConnected) {
            state.table = new (UI().DataTable)(host, {
                columns: [
                    { label: "Size", sortKey: "area", value: (size) => size.width * size.height, render: (size) => h("span", {},
                        h("span", { className: "table-primary", text: size.label }),
                        size.label !== dimensionText(size) ? h("span", { className: "table-secondary", text: dimensionText(size) }) : null) },
                    { label: "Category", sortKey: "category", value: (size) => categoryLabel(size.category_id), render: (size) => categoryLabel(size.category_id) },
                    { label: "Price per piece", sortKey: "price", align: "right", value: (size) => size.price, render: (size) => h("strong", { text: money(size.price) }) },
                    { label: "Order", sortKey: "sort_order", value: (size) => size.sort_order, render: (size) => String(size.sort_order) },
                    { label: "Status", sortKey: "active", value: (size) => (size.active ? 0 : 1), render: (size) => statusBadge(size.active, "Active", "Inactive") },
                    { label: "", render: actions },
                ],
                rows: () => sizes(),
                filter: (size, query) => (!query.category || size.category_id === query.category)
                    && (!query.status || (query.status === "active") === size.active),
                initialQuery: { ...filter },
                defaultSort: { key: "area", dir: "asc" },
                emptyMessage: "No standard sizes yet. Select “Add size” to start the price list.",
                filteredEmptyMessage: "No sizes match these filters.",
                onRowClick: (size) => openEditor(size),
                rowTitle: (size) => `Edit ${size.label}`,
                onLoad: (result) => {
                    const count = document.querySelector("[data-size-count]");
                    const active = sizes().filter((size) => size.active).length;
                    if (count) count.textContent = `${result.total} shown · ${active} active`;
                },
            });
        }
        const apply = (values) => {
            Object.assign(filter, values);
            state.table.setQuery({ ...filter });
        };
        bindControl("#size-category-filter", "change", (event) => apply({ category: event.target.value }));
        const status = bindControl("#size-status-filter", "change", (event) => apply({ status: event.target.value }));
        if (status) status.value = filter.status;
        state.table.setQuery({ ...filter });
    }

    function bindActions() {
        const add = document.querySelector('[data-size-action="add-size"]');
        if (!add || add.dataset.bound) return;
        add.dataset.bound = "true";
        add.addEventListener("click", async () => {
            try {
                await load();
            } catch {
                toast(state.error || "The size price list could not be loaded.");
                return;
            }
            openEditor(null);
        });
        document.querySelector('[data-catalog-action="reload-catalog"]')?.addEventListener("click", () => {
            state.data = null;
            state.error = "";
            render();
        });
    }

    window.PrintShopSizeAdmin = { render, reload: () => load(true).then(render, render) };
})();
