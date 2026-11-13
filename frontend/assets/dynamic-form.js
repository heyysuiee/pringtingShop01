"use strict";

/**
 * PrintShopDynamicForm — renders the order-form questions configured for one catalog product.
 *
 * Usage:
 *   const form = PrintShopDynamicForm.create(container, product, { onChange, includeSections });
 *   form.values()    → { field_key: value } for visible fields (numbers as numbers, checkboxes as arrays)
 *   form.files()     → { field_key: [File, ...] } for visible upload fields
 *   form.validate()  → true when every visible required field is valid (shows messages otherwise)
 *   form.summary()   → [{ key, label, value }] readable rows for the review step
 *
 * ``includeSections`` limits the form to fields whose catalog section is in the given array (for example
 * [""] for basic specifications, or ["event", "upload"] for a package order). Omit it to render every field.
 * ``fieldFilter(field)`` can narrow the fields further (a package order of a non-invitation product shows
 * only the field keys listed in ``product.package_field_keys``).
 *
 * Fields and options come from /api/catalog, so administrator changes appear here without code changes.
 */
(() => {
    const ALLOWED_UPLOADS = new Set(["image/jpeg", "image/png", "image/webp", "application/pdf"]);
    const MAX_UPLOAD_BYTES = 5 * 1024 * 1024;
    const OPTION_TYPES = new Set(["select", "radio", "checkboxes"]);
    const CHARGE_LABELS = { per_unit: "each", per_order: "per order", per_sqft: "per sq ft" };

    const money = (value) => new Intl.NumberFormat("en-PH", { style: "currency", currency: "PHP" }).format(value);

    function el(tag, attributes = {}, ...children) {
        const element = document.createElement(tag);
        for (const [name, value] of Object.entries(attributes)) {
            if (value === undefined || value === null || value === false) continue;
            if (name === "className") element.className = value;
            else if (name === "text") element.textContent = value;
            else if (name === "dataset") Object.assign(element.dataset, value);
            else if (value === true) element.setAttribute(name, "");
            else element.setAttribute(name, String(value));
        }
        element.append(...children.filter((child) => child !== null && child !== undefined && child !== false));
        return element;
    }

    function formatBytes(bytes) {
        return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    }

    /** Short price hint for an option, shown only when the product has an administrator price. */
    function optionPriceHint(product, option) {
        if (option.requires_quote) return "quote required";
        const priced = product.pricing_mode !== "quote" && product.base_price !== null;
        if (!priced || !option.price_adjustment) return "";
        const sign = option.price_adjustment > 0 ? "+" : "−";
        const charge = option.price_type === "per_sqft" && product.pricing_mode !== "area" ? "each" : CHARGE_LABELS[option.price_type] || "each";
        return `${sign}${money(Math.abs(option.price_adjustment))} ${charge}`;
    }

    function create(container, product, { onChange = () => {}, idPrefix = "opt", includeSections = null, fieldFilter = null } = {}) {
        const sections = Array.isArray(includeSections) ? new Set(includeSections) : null;
        const fields = (product.fields || []).filter((field) => (!sections || sections.has(field.section || ""))
            && (typeof fieldFilter !== "function" || fieldFilter(field)));
        const byKey = new Map(fields.map((field) => [field.field_key, field]));
        const wrappers = new Map();
        const uploads = new Map(); // field_key -> File[]
        let destroyed = false;

        const fieldId = (field, suffix = "") => `${idPrefix}-${field.field_key}${suffix}`;

        function rawValue(field) {
            const wrapper = wrappers.get(field.field_key);
            if (!wrapper) return "";
            if (field.field_type === "radio") {
                return wrapper.querySelector("input[type=radio]:checked")?.value || "";
            }
            if (field.field_type === "checkboxes") {
                return [...wrapper.querySelectorAll("input[type=checkbox]:checked")].map((input) => input.value);
            }
            if (field.field_type === "file") return uploads.get(field.field_key) || [];
            return wrapper.querySelector("input, select, textarea")?.value.trim() || "";
        }

        // Same rule as the server: a field shows when its parent is visible and has one of the trigger values.
        function isVisible(field, trail = new Set()) {
            if (!field.depends_on) return true;
            const parent = byKey.get(field.depends_on);
            if (!parent || trail.has(parent.field_key)) return false;
            const value = rawValue(parent);
            const chosen = Array.isArray(value) ? value : [value];
            return isVisible(parent, new Set([...trail, field.field_key])) && chosen.some((item) => field.depends_values.includes(item));
        }

        function setError(field, message) {
            const wrapper = wrappers.get(field.field_key);
            const error = wrapper?.querySelector(".dyn-error");
            if (!error) return;
            error.textContent = message || "";
            wrapper.classList.toggle("has-error", Boolean(message));
        }

        function refreshVisibility() {
            for (const field of fields) {
                const wrapper = wrappers.get(field.field_key);
                const visible = isVisible(field);
                wrapper.hidden = !visible;
                wrapper.querySelectorAll("input, select, textarea").forEach((control) => {
                    control.disabled = !visible;
                });
                if (!visible) setError(field, "");
            }
        }

        function changed(field) {
            if (field) setError(field, "");
            refreshVisibility();
            if (!destroyed) onChange();
        }

        function labelFor(field, forId) {
            const label = el("label", { className: "field-label", for: forId }, field.label);
            if (!field.required) label.append(" ", el("span", { className: "optional", text: "(optional)" }));
            return label;
        }

        function renderSelect(field) {
            const id = fieldId(field);
            const select = el("select", { className: "input", id, name: field.field_key, required: field.required });
            select.append(el("option", { value: "", text: field.required ? `Choose ${field.label.toLowerCase()}…` : "No preference" }));
            for (const option of field.options) {
                const hint = optionPriceHint(product, option);
                select.append(el("option", { value: option.value, text: hint ? `${option.label} · ${hint}` : option.label }));
            }
            if (field.default_value && field.options.some((option) => option.value === field.default_value)) {
                select.value = field.default_value;
            }
            select.addEventListener("change", () => changed(field));
            return [labelFor(field, id), select];
        }

        function renderChoiceGroup(field) {
            const multiple = field.field_type === "checkboxes";
            const group = el("div", { className: "choice-chips", role: multiple ? "group" : "radiogroup" });
            field.options.forEach((option, index) => {
                const id = fieldId(field, `-${index}`);
                const input = el("input", {
                    type: multiple ? "checkbox" : "radio",
                    id,
                    name: field.field_key,
                    value: option.value,
                    required: !multiple && field.required && index === 0,
                });
                if (!multiple && option.value === field.default_value) input.checked = true;
                input.addEventListener("change", () => changed(field));
                const hint = optionPriceHint(product, option);
                const body = el("span", { className: "choice-chip-body" }, el("span", { text: option.label }));
                if (hint) body.append(el("small", { text: hint }));
                group.append(el("label", { className: "choice-chip", for: id }, input, body));
            });
            const legend = el("legend", { className: "field-label" }, field.label);
            if (!field.required) legend.append(" ", el("span", { className: "optional", text: "(optional)" }));
            return [legend, group];
        }

        function renderInput(field) {
            const id = fieldId(field);
            let control;
            if (field.field_type === "textarea") {
                control = el("textarea", { className: "input", id, name: field.field_key, rows: 4 });
            } else {
                const types = { number: "number", date: "date", time: "time" };
                control = el("input", { className: "input", id, name: field.field_key, type: types[field.field_type] || "text" });
                if (field.field_type === "number") {
                    control.step = "any";
                    control.inputMode = "decimal";
                    if (field.min_value !== null) control.min = String(field.min_value);
                    if (field.max_value !== null) control.max = String(field.max_value);
                }
                if (field.field_type === "date") control.min = new Date().toISOString().slice(0, 10);
            }
            if (field.required) control.required = true;
            if (field.placeholder) control.placeholder = field.placeholder;
            if (field.max_length && ["text", "textarea"].includes(field.field_type)) control.maxLength = field.max_length;
            if (field.default_value && !OPTION_TYPES.has(field.field_type)) control.value = field.default_value;
            control.addEventListener("input", () => changed(field));
            return [labelFor(field, id), control];
        }

        function renderFile(field) {
            const id = fieldId(field);
            const files = [];
            let replaceIndex = -1;
            uploads.set(field.field_key, files);
            const input = el("input", {
                id,
                className: "visually-hidden",
                type: "file",
                accept: ".jpg,.jpeg,.png,.webp,.pdf,image/jpeg,image/png,image/webp,application/pdf",
                multiple: field.max_files > 1,
            });
            const zone = el(
                "label",
                { className: "upload-zone upload-zone-compact", for: id },
                el("span", { className: "upload-icon", "aria-hidden": "true", text: "↥" }),
                el("strong", { text: field.max_files > 1 ? `Choose up to ${field.max_files} files` : "Choose a file" }),
                el("small", { text: "JPG, PNG, WebP, or PDF · 5 MB maximum each" }),
            );
            // Opening the normal picker should add files, not replace the last one the customer chose to replace.
            zone.addEventListener("click", () => {
                replaceIndex = -1;
            });
            const previews = el("div", { className: "upload-previews" });

            function previewProblem(chosen) {
                const invalid = chosen.find((file) => !ALLOWED_UPLOADS.has(file.type) || !/\.(jpe?g|png|webp|pdf)$/i.test(file.name));
                if (invalid) return `${invalid.name}: choose a JPG, PNG, WebP, or PDF file.`;
                const oversized = chosen.find((file) => file.size > MAX_UPLOAD_BYTES);
                if (oversized) return `${oversized.name}: each file must be 5 MB or smaller.`;
                return "";
            }

            function renderPreviews() {
                previews.replaceChildren();
                files.forEach((file, index) => {
                    const media = file.type.startsWith("image/")
                        ? el("img", { alt: `Preview of ${file.name}` })
                        : el("span", { className: "pdf-preview", text: "PDF" });
                    if (media.tagName === "IMG") {
                        media.src = URL.createObjectURL(file);
                        media.addEventListener("load", () => URL.revokeObjectURL(media.src), { once: true });
                    }
                    const replace = el("button", { type: "button", className: "text-button", "aria-label": `Replace ${file.name}`, text: "Replace" });
                    replace.addEventListener("click", () => {
                        replaceIndex = index;
                        input.click();
                    });
                    const remove = el("button", { type: "button", className: "text-button", "aria-label": `Remove ${file.name}`, text: "Remove" });
                    remove.addEventListener("click", () => {
                        files.splice(index, 1);
                        renderPreviews();
                        changed(field);
                    });
                    previews.append(el(
                        "article",
                        { className: "upload-preview" },
                        media,
                        el("span", { className: "upload-preview-info" }, el("strong", { text: file.name }), el("small", { text: formatBytes(file.size) })),
                        el("span", { className: "upload-preview-actions" }, replace, remove),
                    ));
                });
                zone.hidden = files.length >= field.max_files;
            }

            input.addEventListener("change", () => {
                const chosen = [...input.files];
                input.value = "";
                if (!chosen.length) {
                    replaceIndex = -1;
                    return;
                }
                const problem = previewProblem(chosen);
                if (problem) {
                    replaceIndex = -1;
                    return setError(field, problem);
                }
                if (replaceIndex >= 0) {
                    // Replace the chosen preview in place, keeping the position in the list.
                    if (replaceIndex < files.length) files[replaceIndex] = chosen[0];
                    replaceIndex = -1;
                    renderPreviews();
                    changed(field);
                    return;
                }
                if (files.length + chosen.length > field.max_files) {
                    return setError(field, `Attach up to ${field.max_files} file${field.max_files === 1 ? "" : "s"} here.`);
                }
                files.push(...chosen);
                renderPreviews();
                changed(field);
            });
            const label = el("span", { className: "field-label" }, field.label);
            if (!field.required) label.append(" ", el("span", { className: "optional", text: "(optional)" }));
            return [label, zone, input, previews];
        }

        function renderField(field) {
            const kind = field.field_type;
            const choiceGroup = kind === "radio" || kind === "checkboxes";
            const wide = choiceGroup || kind === "textarea" || kind === "file";
            const wrapper = el(choiceGroup ? "fieldset" : "div", {
                className: `field dyn-field dyn-${kind}${wide ? " dyn-field-wide" : ""}`,
                dataset: { fieldKey: field.field_key },
            });
            let parts;
            if (kind === "select") parts = renderSelect(field);
            else if (choiceGroup) parts = renderChoiceGroup(field);
            else if (kind === "file") parts = renderFile(field);
            else parts = renderInput(field);
            wrapper.append(...parts);
            if (field.help_text) wrapper.append(el("p", { className: "field-hint", text: field.help_text }));
            wrapper.append(el("p", { className: "field-error dyn-error", role: "alert" }));
            wrappers.set(field.field_key, wrapper);
            return wrapper;
        }

        container.replaceChildren();
        const grid = el("div", { className: "dynamic-grid" });
        fields.forEach((field) => grid.append(renderField(field)));
        container.append(grid);
        refreshVisibility();

        function visibleFields() {
            return fields.filter((field) => isVisible(field));
        }

        function validateField(field) {
            const wrapper = wrappers.get(field.field_key);
            const value = rawValue(field);
            if (field.field_type === "file") {
                if (field.required && !value.length) return "Attach at least one file.";
                return "";
            }
            if (field.field_type === "checkboxes") {
                return field.required && !value.length ? "Choose at least one option." : "";
            }
            if (field.field_type === "radio") {
                return field.required && !value ? "Choose one option." : "";
            }
            const control = wrapper.querySelector("input, select, textarea");
            if (control && !control.checkValidity()) return control.validationMessage;
            return "";
        }

        return {
            product,
            fieldCount: fields.length,
            values() {
                const result = {};
                for (const field of visibleFields()) {
                    if (field.field_type === "file") continue;
                    const value = rawValue(field);
                    if (Array.isArray(value) ? !value.length : value === "") continue;
                    result[field.field_key] = field.field_type === "number" ? Number(value) : value;
                }
                return result;
            },
            files() {
                const result = {};
                for (const field of visibleFields()) {
                    const files = uploads.get(field.field_key);
                    if (field.field_type === "file" && files?.length) result[field.field_key] = [...files];
                }
                return result;
            },
            fileCount() {
                return visibleFields().reduce((total, field) => total + (uploads.get(field.field_key)?.length || 0), 0);
            },
            validate() {
                let first = null;
                for (const field of visibleFields()) {
                    const message = validateField(field);
                    setError(field, message);
                    if (message && !first) first = field;
                }
                if (first) {
                    const wrapper = wrappers.get(first.field_key);
                    wrapper.scrollIntoView({ behavior: "smooth", block: "center" });
                    wrapper.querySelector("input:not([type=file]), select, textarea, .upload-zone")?.focus({ preventScroll: true });
                    return false;
                }
                return true;
            },
            summary() {
                const rows = [];
                for (const field of visibleFields()) {
                    const value = rawValue(field);
                    if (field.field_type === "file") {
                        if (value.length) rows.push({ key: field.field_key, label: field.label, value: value.map((file) => file.name).join(", ") });
                        continue;
                    }
                    if (Array.isArray(value) ? !value.length : value === "") continue;
                    if (OPTION_TYPES.has(field.field_type)) {
                        const labels = (Array.isArray(value) ? value : [value])
                            .map((item) => field.options.find((option) => option.value === item)?.label || item);
                        rows.push({ key: field.field_key, label: field.label, value: labels.join(", ") });
                    } else if (field.field_type === "number" && ["width", "height"].includes(field.field_key)) {
                        const unit = rawValue(byKey.get("measurement_unit") || {}) || "";
                        rows.push({ key: field.field_key, label: field.label, value: `${value} ${unit}`.trim() });
                    } else {
                        rows.push({ key: field.field_key, label: field.label, value });
                    }
                }
                return rows;
            },
            destroy() {
                destroyed = true;
                container.replaceChildren();
            },
        };
    }

    window.PrintShopDynamicForm = { create, optionPriceHint, money };
})();
