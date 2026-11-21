"use strict";

/**
 * Admin Design Management — the Designs tab of Services & Products (sidebar: "Design Management").
 *
 * Shop-made designs are what customers pick with "Choose a Shop Design" on the order page; they only type
 * their personalization details (names, date, venue, …) and never upload a design file. The gallery starts
 * with TEMPORARY SAMPLE designs. Here the administrator uploads the shop's real designs, edits names,
 * descriptions, services, products, and personalization questions, activates or deactivates designs, replaces
 * a sample with a real design, and removes the samples. Orders keep their own snapshot of the design.
 *
 * Saved through /api/admin/designs. catalog-admin.js calls render() whenever the Services & Products page renders.
 */
(() => {
    const catalog = window.PrintShopCatalogAdmin;
    if (!catalog?.helpers) return;
    const { h, api, toast, openDialog, closeDialog, formField, selectOf, guarded, readImage, statusBadge, bindControl, setTabCount } = catalog.helpers;
    const UI = () => window.AdminUI;
    const MAX_IMAGE_BYTES = 4 * 1024 * 1024;

    const state = { data: null, loading: null, error: "", table: null };
    const filter = { search: "", service: "", type: "", status: "" };

    async function load(force = false) {
        if (state.data && !force) return state.data;
        if (!state.loading) {
            state.loading = api("GET", "/api/admin/designs")
                .then((data) => {
                    state.data = data;
                    state.error = "";
                    return data;
                })
                .catch((error) => {
                    state.error = error instanceof Error ? error.message : "The designs could not be loaded.";
                    throw error;
                })
                .finally(() => { state.loading = null; });
        }
        return state.loading;
    }

    async function reload() {
        try {
            await load(true);
        } catch {
            // The Designs tab shows the loading error.
        }
        render();
    }

    /** Keep the response of a save (it returns the whole list) and redraw. */
    function store(data) {
        state.data = data;
        render();
    }

    const services = () => state.data?.services || [];
    const service = (id) => services().find((item) => item.id === id) || null;
    const library = () => state.data?.field_library || [];

    function sampleBadge(design) {
        return design.is_sample
            ? h("span", { className: "design-sample-flag", title: "Placeholder design: replace it with the shop's real design.", text: "TEMPORARY SAMPLE" })
            : h("span", { className: "status-badge available", text: "Shop design" });
    }

    function viewDesign(design) {
        UI().openViewer([{ src: design.image, name: design.name, caption: `${design.name}${design.is_sample ? " · TEMPORARY SAMPLE" : ""}`, original: design.image }], 0);
    }

    function thumb(design) {
        const button = h("button", { className: "product-thumb-button", type: "button", title: "View larger (zoom and fullscreen)", "aria-label": `View ${design.name}` },
            h("img", { className: "product-thumb design-admin-thumb", src: design.image, alt: "", loading: "lazy", decoding: "async" }));
        button.addEventListener("click", (event) => {
            event.stopPropagation();
            viewDesign(design);
        });
        return button;
    }

    function productText(design) {
        return design.product_names.length ? design.product_names.join(", ") : "Every product in this service";
    }

    // ---------------------------------------------------------------- row actions

    function actions(design) {
        const toggle = h("button", { className: "button button-small", type: "button", text: design.active ? "Deactivate" : "Activate", title: design.active ? "Hide this design from customers" : "Offer this design again" });
        toggle.addEventListener("click", () => void guarded(toggle, async () => {
            store(await api("PATCH", `/api/admin/designs/${encodeURIComponent(design.id)}`, { active: !design.active }));
            toast(design.active ? "Design deactivated. Customers no longer see it." : "Design activated. Customers can choose it again.");
        }));
        const replace = design.is_sample ? h("button", { className: "button button-small button-primary", type: "button", text: "Replace", title: "Replace this sample with the shop's real design" }) : null;
        replace?.addEventListener("click", () => openEditor(design, { replace: true }));
        const remove = h("button", { className: "button button-small button-danger", type: "button", text: "Delete" });
        remove.addEventListener("click", () => void deleteDesign(design, remove));
        return h("div", { className: "catalog-row-actions" }, toggle, replace, remove);
    }

    async function deleteDesign(design, button, after = null) {
        const confirmed = await UI().confirm({
            title: "Delete design?",
            message: `Delete “${design.name}”? Customers will no longer see it. Orders already placed with it keep their saved design and picture. To keep it for later, deactivate it instead.`,
            confirmLabel: "Delete design",
            tone: "danger",
        });
        if (!confirmed) return;
        await guarded(button, async () => {
            store(await api("DELETE", `/api/admin/designs/${encodeURIComponent(design.id)}`));
            toast("Design deleted. Existing orders are unchanged.");
            after?.();
        });
    }

    async function removeSamples(button) {
        const count = state.data?.sample_count || 0;
        if (!count) return;
        const confirmed = await UI().confirm({
            title: "Remove all temporary samples?",
            message: `Delete all ${count} TEMPORARY SAMPLE design${count === 1 ? "" : "s"}? Upload the shop's real designs first so customers still have designs to choose from. Orders already placed with a sample keep their saved design.`,
            confirmLabel: "Remove samples",
            tone: "danger",
        });
        if (!confirmed) return;
        await guarded(button, async () => {
            const result = await api("DELETE", "/api/admin/designs/samples");
            store(result);
            toast(`${result.deleted} sample design${result.deleted === 1 ? "" : "s"} removed. Existing orders are unchanged.`);
        });
    }

    // ---------------------------------------------------------------- editor

    /** Personalization questions: include each library detail, and mark which ones are required. */
    function fieldPicker(initial) {
        const rows = new Map();
        const list = h("div", { className: "design-field-picker" });
        const chosen = new Map(initial.map((item) => [item.key, Boolean(item.required)]));
        for (const field of library()) {
            const include = h("input", { type: "checkbox", checked: chosen.has(field.key), "aria-label": `Ask for ${field.label}` });
            const required = h("input", { type: "checkbox", checked: Boolean(chosen.get(field.key)), disabled: !chosen.has(field.key), "aria-label": `${field.label} is required` });
            include.addEventListener("change", () => {
                required.disabled = !include.checked;
                if (!include.checked) required.checked = false;
            });
            const kind = { text: "Short text", textarea: "Long text", number: "Number", date: "Date", time: "Time" }[field.field_type] || field.field_type;
            list.append(h("div", { className: "design-field-row" },
                h("label", { className: "design-field-include" }, include, h("span", {}, h("strong", { text: field.label }), h("small", { text: kind }))),
                h("label", { className: "design-field-required" }, required, h("span", { text: "Required" }))));
            rows.set(field.key, { include, required });
        }
        return {
            element: list,
            set(values) {
                const next = new Map(values.map((item) => [item.key, Boolean(item.required)]));
                rows.forEach(({ include, required }, key) => {
                    include.checked = next.has(key);
                    required.disabled = !include.checked;
                    required.checked = Boolean(next.get(key));
                });
            },
            // Library order keeps the customer form consistent; included details only.
            value: () => [...rows.entries()].filter(([, row]) => row.include.checked).map(([key, row]) => ({ key, required: row.required.checked })),
        };
    }

    function productPicker() {
        const host = h("div", { className: "design-product-picker" });
        let boxes = [];
        return {
            element: host,
            render(serviceId, selected) {
                const products = service(serviceId)?.products || [];
                boxes = products.map((product) => ({ id: product.id, input: h("input", { type: "checkbox", checked: selected.includes(product.id) }) }));
                host.replaceChildren(...products.map((product, index) => h("label", { className: "design-product-option" },
                    boxes[index].input,
                    h("span", { text: product.name }),
                    product.allow_customize === false ? h("small", { className: "field-note", text: " (packages only: designs are not shown)" }) : null)));
                if (!products.length) host.append(h("p", { className: "field-note", text: "This service has no products yet." }));
            },
            value: () => boxes.filter((box) => box.input.checked).map((box) => box.id),
        };
    }

    function imagePicker(design, { required }) {
        let upload = null;
        const preview = h("button", { className: "design-image-preview", type: "button", title: "View larger" });
        const caption = h("p", { className: "field-note" });
        const input = h("input", { type: "file", accept: "image/jpeg,image/png,image/webp" });
        const show = (src, text) => {
            preview.replaceChildren(src ? h("img", { src, alt: "" }) : h("span", { className: "field-note", text: "No image yet" }));
            preview.disabled = !src;
            caption.textContent = text;
        };
        preview.addEventListener("click", () => {
            const src = upload?.dataUrl || design?.image;
            if (src) UI().openViewer([{ src, name: "Design image", caption: upload ? "New image (not saved yet)" : "Current image", original: src }], 0);
        });
        input.addEventListener("change", async () => {
            const file = input.files?.[0];
            input.value = "";
            if (!file) return;
            if (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > MAX_IMAGE_BYTES) {
                toast(`${file.name}: choose a JPG, PNG, or WebP image of 4 MB or less.`);
                return;
            }
            upload = { name: file.name, dataUrl: await readImage(file) };
            show(upload.dataUrl, `New image: ${file.name} — saved when you select Save.`);
        });
        show(design?.image || "", design
            ? (required ? "Current sample image. Upload the shop's design to replace it." : "Current image. Upload a new file to replace it.")
            : "Upload the design customers will choose from.");
        return {
            element: h("div", { className: "design-image-field" }, preview, h("div", {},
                caption,
                h("label", { className: "button button-small image-upload-button" }, design ? "Upload replacement image" : "＋ Upload design image", input),
                h("p", { className: "field-note", text: "JPG, PNG, or WebP, up to 4 MB. Use a clear preview of the full design (portrait or landscape)." }))),
            value: () => upload,
        };
    }

    /**
     * Add, edit, or replace a design. ``replace`` (for a TEMPORARY SAMPLE) requires a new image and turns the
     * design into a shop design; the sample's service, products, and questions are kept as a starting point.
     */
    function openEditor(design, { replace = false } = {}) {
        if (!state.data) return;
        const creating = !design;
        const form = h("form", { className: "catalog-form", novalidate: true });
        const name = h("input", { name: "name", required: true, maxlength: 120, value: replace ? "" : (design?.name || ""), placeholder: replace ? design.name : "e.g. Blush Garden" });
        const style = h("input", { name: "style", maxlength: 60, value: design?.style || "", placeholder: "e.g. Floral, Minimalist, Playful", list: "design-style-options" });
        const styles = h("datalist", { id: "design-style-options" }, ...[...new Set((state.data.designs || []).map((item) => item.style).filter(Boolean))].map((value) => h("option", { value })));
        const description = h("textarea", { name: "description", maxlength: 600, rows: 3, placeholder: "What the design looks like and what it suits" });
        description.value = replace ? "" : (design?.description || "");
        const firstService = design?.category_id || filter.service || services()[0]?.id || "";
        const serviceSelect = selectOf("category_id", services().map((item) => [item.id, `${item.label}${item.active ? "" : " (hidden)"}`]), firstService);
        const products = productPicker();
        products.render(firstService, design?.product_ids || []);
        const fields = fieldPicker(design ? design.fields : (service(firstService)?.default_fields || []));
        serviceSelect.addEventListener("change", () => {
            products.render(serviceSelect.value, []);
            // A new design starts with the questions usually asked for that service.
            if (creating) fields.set(service(serviceSelect.value)?.default_fields || []);
        });
        const active = selectOf("active", [["true", "Active — customers can choose it"], ["false", "Inactive — hidden from customers"]], design ? String(design.active) : "true");
        const sample = h("input", { type: "checkbox", checked: Boolean(design?.is_sample) && !replace });
        const sortOrder = h("input", { name: "sort_order", type: "number", min: 0, max: 10000, step: 1, value: design?.sort_order ?? "", placeholder: "Automatic" });
        const image = imagePicker(design, { required: replace });

        form.append(
            replace ? h("div", { className: "notice-card" }, h("strong", { text: "Replacing a temporary sample. " }),
                "Upload the shop's real design and give it its own name. The service, products, and personalization details below are kept from the sample; change them if needed. Orders already placed with the sample keep the sample.") : null,
            h("div", { className: "form-grid" },
                formField("Design name", name),
                formField("Availability", active),
                formField("Style", style, { note: "Shown as a tag and used for the gallery's style filter." }),
                formField("Sort order", sortOrder, { note: "Lower numbers appear first." }),
                formField("Description", description, { full: true }),
                formField("Service category", serviceSelect, { full: true }),
                h("div", { className: "form-field full" },
                    h("span", { className: "form-label-strong", text: "Products" }),
                    h("span", { className: "field-note", text: "Select the products this design is offered for. Leave all unchecked to offer it for every product in the service." }),
                    products.element),
                h("div", { className: "form-field full" },
                    h("span", { className: "form-label-strong", text: "Personalization details customers fill in" }),
                    h("span", { className: "field-note", text: "Only these details are asked after a customer selects the design. Customers never upload a design file for a shop design." }),
                    fields.element),
                h("div", { className: "form-field full" },
                    h("span", { className: "form-label-strong", text: "Design image" }),
                    image.element),
                replace ? null : h("label", { className: "form-field full design-sample-toggle" }, sample,
                    h("span", {}, h("strong", { text: "Temporary sample" }), h("small", { className: "field-note", text: " Marked “TEMPORARY SAMPLE” here and “Sample design” for customers. Clear it once this is the shop's real design." }))),
            ),
            styles,
        );

        const actionsBar = h("div", { className: "dialog-actions" }, h("button", { className: "button", type: "button", dataset: { action: "close-dialog" }, text: "Close" }));
        if (design) {
            const remove = h("button", { className: "button button-danger", type: "button", text: "Delete design" });
            remove.addEventListener("click", () => void deleteDesign(design, remove, closeDialog));
            actionsBar.append(remove);
        }
        const save = h("button", { className: "button button-primary", type: "submit", text: creating ? "Add design" : replace ? "Replace with this design" : "Save design" });
        actionsBar.append(save);
        form.append(actionsBar);

        form.addEventListener("submit", (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            const chosenFields = fields.value();
            if (!chosenFields.length) {
                toast("Choose at least one detail the customer fills in.");
                return;
            }
            const upload = image.value();
            if ((creating || replace) && !upload) {
                toast(replace ? "Upload the shop's design image to replace the sample." : "Upload the design image customers will choose from.");
                return;
            }
            const payload = {
                name: name.value.trim(),
                style: style.value.trim(),
                description: description.value.trim(),
                category_id: serviceSelect.value,
                product_ids: products.value(),
                fields: chosenFields,
                active: active.value === "true",
                is_sample: replace ? false : sample.checked,
            };
            if (sortOrder.value.trim() !== "") payload.sort_order = Number(sortOrder.value);
            if (upload) payload.image_upload = upload;
            void guarded(save, async () => {
                const result = creating
                    ? await api("POST", "/api/admin/designs", payload)
                    : await api("PATCH", `/api/admin/designs/${encodeURIComponent(design.id)}`, payload);
                store(result);
                toast(creating ? "Design added. Customers can choose it on the order page now."
                    : replace ? "Sample replaced with the shop's design. Existing orders keep the sample they were placed with."
                        : "Design saved. The order page shows the changes now.");
                closeDialog();
            });
        });

        openDialog(h("div", { className: "catalog-editor" },
            h("p", { className: "catalog-breadcrumb", text: design ? `${design.category_label} / ${design.name}` : "Shop designs" }),
            h("h2", { className: "dialog-heading", text: creating ? "Upload a shop design" : replace ? `Replace “${design.name}”` : `Edit “${design.name}”` }),
            h("p", { className: "dialog-subtitle", text: "Customers choose this design and type only the details below. Orders keep the design as it was when they were placed." }),
            form,
        ));
    }

    // ---------------------------------------------------------------- tab rendering

    function renderNotice() {
        const host = document.querySelector("[data-design-notice]");
        if (!host) return;
        const count = state.data?.sample_count || 0;
        if (!count) {
            host.replaceChildren();
            return;
        }
        const remove = h("button", { className: "button button-small button-danger", type: "button", text: "Remove all samples" });
        remove.addEventListener("click", () => void removeSamples(remove));
        host.replaceChildren(h("div", { className: "notice-card design-sample-notice" },
            h("div", {},
                h("strong", { text: `${count} TEMPORARY SAMPLE design${count === 1 ? "" : "s"} in the gallery. ` }),
                "These placeholders show customers how designs work until the shop owner provides the real designs; customers see them labelled “Sample design”. Upload the real designs, then use Replace on a sample or remove all samples. Existing orders are never changed."),
            remove));
    }

    function renderStats() {
        const host = document.querySelector("[data-design-stats]");
        if (!host || !state.data) return;
        const designs = state.data.designs;
        const card = (label, value, foot) => h("article", { className: "stat-card" },
            h("span", { className: "stat-top" }, h("span", { className: "stat-label", text: label })),
            h("span", { className: "stat-value", text: String(value) }),
            h("span", { className: "stat-foot", text: foot }));
        host.replaceChildren(
            card("Total designs", designs.length, "Across every service"),
            card("Active designs", designs.filter((design) => design.active).length, "Customers can choose these"),
            card("Temporary samples", state.data.sample_count, "Replace with the shop's designs"),
            card("Services with designs", new Set(designs.filter((design) => design.active).map((design) => design.category_id)).size, `of ${services().length} services`),
        );
    }

    function render() {
        const host = document.querySelector("[data-design-table]");
        if (!host) return;
        bindActions();
        if (!state.data) {
            host.replaceChildren(h("p", { className: "empty-state", text: state.error ? `The designs could not be loaded. ${state.error} Select Reload to try again.` : "Loading designs…" }));
            state.table = null;
            if (!state.error && !state.loading) load().then(render, render);
            return;
        }
        setTabCount("designs", state.data.designs.length);
        renderNotice();
        renderStats();

        const serviceFilter = document.querySelector("#design-service-filter");
        if (serviceFilter) {
            serviceFilter.replaceChildren(new Option("All categories", ""), ...services().map((item) => new Option(item.label, item.id)));
            serviceFilter.value = services().some((item) => item.id === filter.service) ? filter.service : "";
            filter.service = serviceFilter.value;
        }

        if (!state.table || state.table.container !== host || !host.isConnected) {
            state.table = new (UI().DataTable)(host, {
                columns: [
                    { label: "Design", sortKey: "name", render: (design) => h("div", { className: "product-cell" }, thumb(design), h("span", {},
                        h("span", { className: "table-primary", text: design.name }),
                        h("span", { className: "table-secondary design-admin-description", text: [design.style, design.description].filter(Boolean).join(" · ") || "No description" }))) },
                    { label: "Service / products", sortKey: "category_label", render: (design) => h("span", {},
                        h("span", { className: "table-primary", text: design.category_label }),
                        h("span", { className: "table-secondary", text: productText(design) })) },
                    { label: "Customer details", value: (design) => design.fields.length, sortKey: "field_count", render: (design) => h("span", { title: design.fields.map((field) => `${field.label}${field.required ? " (required)" : ""}`).join("\n") },
                        h("span", { className: "table-primary", text: `${design.fields.length} detail${design.fields.length === 1 ? "" : "s"}` }),
                        h("span", { className: "table-secondary", text: design.fields.slice(0, 3).map((field) => field.label).join(", ") + (design.fields.length > 3 ? "…" : "") })) },
                    { label: "Type / status", sortKey: "is_sample", value: (design) => (design.is_sample ? 0 : 1), render: (design) => h("span", { className: "design-admin-status" },
                        sampleBadge(design), statusBadge(design.active, "Active", "Inactive")) },
                    { label: "", render: actions },
                ],
                rows: () => state.data?.designs || [],
                filter: (design, query) => {
                    const needle = (query.search || "").toLowerCase();
                    return (!query.service || design.category_id === query.service)
                        && (!query.type || (query.type === "sample") === design.is_sample)
                        && (!query.status || (query.status === "active") === design.active)
                        && (!needle || `${design.name} ${design.description} ${design.style} ${design.category_label} ${design.product_names.join(" ")}`.toLowerCase().includes(needle));
                },
                initialQuery: { ...filter },
                defaultSort: { key: "category_label", dir: "asc" },
                emptyMessage: "No designs yet. Select “Upload design” to add the shop's first design.",
                filteredEmptyMessage: "No designs match these filters.",
                onRowClick: (design) => openEditor(design),
                rowTitle: (design) => `Edit ${design.name}`,
                onLoad: (result) => {
                    const count = document.querySelector("[data-design-count]");
                    if (count) count.textContent = `${result.total} of ${(state.data?.designs || []).length}`;
                },
            });
        }
        const apply = (values) => {
            Object.assign(filter, values);
            state.table.setQuery({ ...filter });
        };
        const search = bindControl("#design-search", "input", (event) => apply({ search: event.target.value.trim() }));
        if (search && !search.value) search.value = filter.search;
        bindControl("#design-service-filter", "change", (event) => apply({ service: event.target.value }));
        const type = bindControl("#design-type-filter", "change", (event) => apply({ type: event.target.value }));
        if (type) type.value = filter.type;
        const status = bindControl("#design-status-filter", "change", (event) => apply({ status: event.target.value }));
        if (status) status.value = filter.status;
        state.table.setQuery({ ...filter });
    }

    function bindActions() {
        const add = document.querySelector('[data-design-action="add-design"]');
        if (!add || add.dataset.bound) return;
        add.dataset.bound = "true";
        add.addEventListener("click", async () => {
            try {
                await load();
            } catch {
                toast(state.error || "The designs could not be loaded.");
                return;
            }
            openEditor(null);
        });
    }

    window.PrintShopDesignAdmin = { render, reload };
})();
