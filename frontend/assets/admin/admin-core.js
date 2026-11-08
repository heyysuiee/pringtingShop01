"use strict";

/**
 * Shared admin UI building blocks: element helper, formatting, API calls, toasts, confirmation
 * dialogs, the data table (search/filter/sort/pagination with clickable rows), the image viewer
 * (zoom, pan, fullscreen), lightweight charts, and report downloads.
 * Exposed as window.AdminUI and used by dashboard.js and catalog-admin.js.
 */
(() => {
    const SHOP_TIME_ZONE = "Asia/Manila";

    // ================================================================ DOM helper

    function h(tag, attributes = {}, ...children) {
        const element = document.createElement(tag);
        for (const [name, value] of Object.entries(attributes || {})) {
            if (value === undefined || value === null || value === false) continue;
            if (name === "className") element.className = value;
            else if (name === "text") element.textContent = value;
            else if (name === "dataset") Object.assign(element.dataset, value);
            else if (name === "value") element.value = value;
            else if (name === "checked") element.checked = Boolean(value);
            else if (name.startsWith("on") && typeof value === "function") element.addEventListener(name.slice(2), value);
            else if (value === true) element.setAttribute(name, "");
            else element.setAttribute(name, String(value));
        }
        for (const child of children.flat()) {
            if (child === null || child === undefined || child === false) continue;
            element.append(child instanceof Node ? child : String(child));
        }
        return element;
    }

    // ================================================================ formatting

    const moneyFormat = new Intl.NumberFormat("en-PH", { style: "currency", currency: "PHP", minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const compactFormat = new Intl.NumberFormat("en-PH", { notation: "compact", maximumFractionDigits: 1 });
    const money = (value) => (value === null || value === undefined || value === "" ? "—" : moneyFormat.format(Number(value)));
    const compactMoney = (value) => (Math.abs(value) >= 10000 ? `₱${compactFormat.format(value)}` : moneyFormat.format(value).replace(/\.00$/, ""));
    const number = (value) => (value === null || value === undefined ? "—" : new Intl.NumberFormat("en-PH").format(value));

    /** "2026-11-08" → "Nov 8, 2026" (a calendar date, so no time-zone shift). */
    function date(value) {
        if (!value) return "—";
        const [year, month, day] = String(value).slice(0, 10).split("-").map(Number);
        if (!year || !month || !day) return "—";
        return new Date(Date.UTC(year, month - 1, day)).toLocaleDateString("en-PH", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
    }

    /** A stored UTC timestamp shown in shop time. */
    function dateTime(value) {
        if (!value) return "—";
        const parsed = new Date(value);
        if (Number.isNaN(parsed.getTime())) return "—";
        return parsed.toLocaleString("en-PH", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZone: SHOP_TIME_ZONE });
    }

    function days(value) {
        if (value === null || value === undefined) return "—";
        return `${Number(value).toLocaleString("en-PH", { maximumFractionDigits: 1 })} day${Number(value) === 1 ? "" : "s"}`;
    }

    function relativeTime(value) {
        const parsed = new Date(value);
        const seconds = Math.round((Date.now() - parsed.getTime()) / 1000);
        if (Number.isNaN(seconds)) return "";
        if (seconds < 60) return "Just now";
        if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
        if (seconds < 86400) return `${Math.floor(seconds / 3600)} hr ago`;
        if (seconds < 7 * 86400) return `${Math.floor(seconds / 86400)} day${seconds < 2 * 86400 ? "" : "s"} ago`;
        return dateTime(value);
    }

    const slug = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    const fileSize = (bytes) => (bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`);

    function badge(text, extraClass = "") {
        return h("span", { className: `status-badge ${slug(text)} ${extraClass}`.trim(), text });
    }

    // ================================================================ API

    async function api(url, { method = "GET", body } = {}) {
        let response;
        try {
            response = await fetch(url, {
                method,
                credentials: "same-origin",
                headers: body === undefined ? { Accept: "application/json" } : { "Content-Type": "application/json", Accept: "application/json" },
                body: body === undefined ? undefined : JSON.stringify(body),
            });
        } catch {
            throw new Error("The server could not be reached. Check the connection and try again.");
        }
        if (response.status === 401) {
            window.location.assign("/admin/login");
            throw new Error("Your administrator session has ended. Please sign in again.");
        }
        if (response.status === 204) return null;
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
            const detail = Array.isArray(result.detail) ? result.detail.map((item) => item.msg).join(" ") : result.detail;
            const error = new Error(detail || `The request failed (HTTP ${response.status}).`);
            error.status = response.status;
            throw error;
        }
        return result;
    }

    function query(params) {
        const search = new URLSearchParams();
        for (const [key, value] of Object.entries(params || {})) {
            if (value === undefined || value === null || value === "") continue;
            search.set(key, String(value));
        }
        const text = search.toString();
        return text ? `?${text}` : "";
    }

    // ================================================================ toasts

    function toast(message, tone = "info") {
        const region = document.querySelector("#toast-region");
        if (!region) return;
        const element = h("div", { className: `toast toast-${tone}`, role: tone === "error" ? "alert" : "status" },
            h("span", { className: "toast-icon", "aria-hidden": "true", text: tone === "error" ? "!" : tone === "success" ? "✓" : "i" }),
            h("span", { text: message }),
        );
        region.append(element);
        window.setTimeout(() => element.remove(), tone === "error" ? 6000 : 3600);
    }

    // ================================================================ modal & confirm

    const mainDialog = () => document.querySelector("#admin-dialog");

    function openModal(node, { wide = false, size = "" } = {}) {
        const dialog = mainDialog();
        const content = document.querySelector("#dialog-content");
        content.replaceChildren(node);
        dialog.classList.toggle("admin-dialog-wide", wide);
        dialog.classList.toggle("admin-dialog-xl", size === "xl");
        if (!dialog.open) dialog.showModal();
        dialog.querySelector(".dialog-frame")?.scrollTo({ top: 0 });
        return dialog;
    }

    function closeModal() {
        const dialog = mainDialog();
        if (dialog?.open) dialog.close();
    }

    function stackedDialog(className) {
        const dialog = h("dialog", { className });
        document.body.append(dialog);
        dialog.addEventListener("close", () => dialog.remove());
        return dialog;
    }

    /**
     * In-page confirmation. Resolves to false when cancelled, or { value } when confirmed.
     * With `input`, a text box is shown (for example a rejection reason).
     */
    function confirm({ title, message, confirmLabel = "Confirm", tone = "primary", input = null, details = [] }) {
        return new Promise((resolve) => {
            const dialog = stackedDialog("admin-dialog confirm-dialog");
            let field = null;
            if (input) {
                field = h(input.multiline === false ? "input" : "textarea", {
                    id: "confirm-input",
                    maxlength: input.maxlength || 500,
                    placeholder: input.placeholder || "",
                    required: input.required || false,
                });
            }
            const confirmButton = h("button", { className: `button ${tone === "danger" ? "button-danger-strong" : "button-primary"}`, type: "submit", text: confirmLabel });
            const form = h("form", { method: "dialog", className: "confirm-form" },
                h("h2", { className: "dialog-heading", text: title }),
                message ? h("p", { className: "confirm-message", text: message }) : null,
                details.length ? h("dl", { className: "confirm-details" }, ...details.flatMap(([label, value]) => [h("dt", { text: label }), h("dd", { text: value })])) : null,
                field ? h("div", { className: "form-field" }, h("label", { for: "confirm-input", text: input.label }), field) : null,
                h("div", { className: "dialog-actions" },
                    h("button", { className: "button", type: "button", value: "cancel", text: "Cancel", onclick: () => dialog.close("cancel") }),
                    confirmButton,
                ),
            );
            let settled = false;
            form.addEventListener("submit", (event) => {
                event.preventDefault();
                if (field && !form.reportValidity()) return;
                settled = true;
                resolve({ value: field ? field.value.trim() : "" });
                dialog.close("confirm");
            });
            dialog.addEventListener("close", () => {
                if (!settled) resolve(false);
            });
            dialog.append(form);
            dialog.showModal();
            (field || confirmButton).focus();
        });
    }

    /** Disable a button while an async task runs, so actions cannot be submitted twice. */
    async function busy(button, task, label = "Saving…") {
        if (button?.disabled) return undefined;
        const original = button?.textContent;
        if (button) {
            button.disabled = true;
            button.setAttribute("aria-busy", "true");
            button.textContent = label;
        }
        try {
            return await task();
        } finally {
            if (button?.isConnected) {
                button.disabled = false;
                button.removeAttribute("aria-busy");
                button.textContent = original;
            }
        }
    }

    // ================================================================ data table

    /**
     * A table with sorting, pagination, loading/empty/error states, and clickable rows.
     *
     * Server mode: options.fetch(query) returns { items, total, page, pages, page_size }.
     * Client mode: options.rows() returns every row; options.filter(row, query) narrows them and
     * columns' `value(row)` is used for sorting.
     */
    class DataTable {
        constructor(container, options) {
            this.container = container;
            this.options = { pageSize: 25, emptyMessage: "No records found.", ...options };
            this.query = { ...(options.initialQuery || {}) };
            this.sort = { ...(options.defaultSort || {}) };
            this.page = 1;
            this.pageSize = this.options.pageSize;
            this.requestId = 0;
            this.result = null;
            this.loading = false;
            this.error = "";
            this.build();
        }

        build() {
            this.thead = h("thead");
            this.tbody = h("tbody");
            this.table = h("table", { className: `data-table ${this.options.className || ""}`.trim() }, this.thead, this.tbody);
            if (this.options.caption) this.table.prepend(h("caption", { className: "visually-hidden", text: this.options.caption }));
            this.wrap = h("div", { className: "table-wrap data-table-wrap" }, this.table);
            this.footer = h("div", { className: "table-footer" });
            this.container.replaceChildren(this.wrap, this.footer);
            this.renderHead();
            this.tbody.addEventListener("click", (event) => {
                if (event.target.closest("a, button, input, select, label")) return;
                const row = event.target.closest("tr[data-row-index]");
                if (row) this.activate(row);
            });
            this.tbody.addEventListener("keydown", (event) => {
                if (event.key !== "Enter" && event.key !== " ") return;
                const row = event.target.closest("tr[data-row-index]");
                if (!row || event.target !== row) return;
                event.preventDefault();
                this.activate(row);
            });
        }

        activate(row) {
            const item = this.visibleRows[Number(row.dataset.rowIndex)];
            if (item && this.options.onRowClick) this.options.onRowClick(item, row);
        }

        renderHead() {
            const cells = this.options.columns.map((column) => {
                const th = h("th", { scope: "col", className: column.align === "right" ? "align-right" : "" });
                if (column.sortKey) {
                    const active = this.sort.key === column.sortKey;
                    th.setAttribute("aria-sort", active ? (this.sort.dir === "asc" ? "ascending" : "descending") : "none");
                    th.append(h("button", {
                        className: `sort-button${active ? " active" : ""}`,
                        type: "button",
                        "aria-label": `Sort by ${column.label}`,
                        onclick: () => this.toggleSort(column),
                    }, column.label, h("span", { className: "sort-indicator", "aria-hidden": "true", text: active ? (this.sort.dir === "asc" ? "▲" : "▼") : "↕" })));
                } else {
                    th.textContent = column.label;
                }
                return th;
            });
            this.thead.replaceChildren(h("tr", {}, ...cells));
        }

        toggleSort(column) {
            if (this.sort.key === column.sortKey) {
                this.sort.dir = this.sort.dir === "asc" ? "desc" : "asc";
            } else {
                this.sort = { key: column.sortKey, dir: column.defaultDir || (column.align === "right" || column.type === "date" ? "desc" : "asc") };
            }
            this.page = 1;
            this.renderHead();
            this.load();
        }

        setQuery(values, { replace = false } = {}) {
            this.query = replace ? { ...values } : { ...this.query, ...values };
            this.page = 1;
            return this.load();
        }

        params() {
            return { ...this.query, sort: this.sort.key, dir: this.sort.dir, page: this.page, page_size: this.pageSize };
        }

        async load() {
            const id = ++this.requestId;
            this.loading = true;
            this.error = "";
            this.renderBody();
            try {
                const result = this.options.fetch ? await this.options.fetch(this.params()) : this.clientResult();
                if (id !== this.requestId) return;
                if (!result || !Array.isArray(result.items) || typeof result.total !== "number") {
                    // Happens when the browser has new admin pages but the server still runs older code.
                    throw new Error("The server sent an unexpected response. Restart the server so it runs the latest code, then reload this page.");
                }
                this.result = result;
                this.page = result.page;
                this.options.onLoad?.(result);
            } catch (error) {
                if (id !== this.requestId) return;
                this.error = error instanceof Error ? error.message : "The records could not be loaded.";
            } finally {
                if (id === this.requestId) {
                    this.loading = false;
                    this.renderBody();
                }
            }
        }

        reload() {
            return this.load();
        }

        clientResult() {
            const all = this.options.rows() || [];
            const filtered = this.options.filter ? all.filter((row) => this.options.filter(row, this.query)) : all;
            const column = this.options.columns.find((item) => item.sortKey && item.sortKey === this.sort.key);
            if (column) {
                const value = column.value || ((row) => row[column.sortKey]);
                const direction = this.sort.dir === "asc" ? 1 : -1;
                filtered.sort((a, b) => {
                    const left = value(a);
                    const right = value(b);
                    if (left === right) return 0;
                    if (left === null || left === undefined || left === "") return 1;
                    if (right === null || right === undefined || right === "") return -1;
                    if (typeof left === "number" && typeof right === "number") return (left - right) * direction;
                    return String(left).localeCompare(String(right), undefined, { numeric: true, sensitivity: "base" }) * direction;
                });
            }
            const pages = Math.max(1, Math.ceil(filtered.length / this.pageSize));
            const page = Math.min(this.page, pages);
            return {
                items: filtered.slice((page - 1) * this.pageSize, page * this.pageSize),
                total: filtered.length,
                page,
                pages,
                page_size: this.pageSize,
            };
        }

        renderBody() {
            const columns = this.options.columns.length;
            this.wrap.classList.toggle("is-loading", this.loading && Boolean(this.result));
            if (this.error) {
                this.visibleRows = [];
                this.tbody.replaceChildren(h("tr", {}, h("td", { className: "empty-state table-error", colspan: columns },
                    h("strong", { text: "Could not load the records. " }), this.error, " ",
                    h("button", { className: "button button-small", type: "button", text: "Try again", onclick: () => this.load() }),
                )));
                this.footer.replaceChildren();
                return;
            }
            if (!this.result) {
                this.visibleRows = [];
                this.tbody.replaceChildren(h("tr", {}, h("td", { className: "empty-state", colspan: columns },
                    h("span", { className: "spinner", "aria-hidden": "true" }), " Loading records…")));
                return;
            }
            this.visibleRows = this.result.items;
            if (!this.result.items.length) {
                const filtered = Object.values(this.query).some((value) => value !== "" && value !== undefined && value !== null);
                this.tbody.replaceChildren(h("tr", {}, h("td", { className: "empty-state", colspan: columns },
                    filtered ? (this.options.filteredEmptyMessage || "No records match the selected filters.") : this.options.emptyMessage)));
            } else {
                this.tbody.replaceChildren(...this.result.items.map((item, index) => {
                    const row = h("tr", { dataset: { rowIndex: String(index) } });
                    if (this.options.onRowClick) {
                        row.className = "clickable-row";
                        row.tabIndex = 0;
                        row.title = this.options.rowTitle ? this.options.rowTitle(item) : "Open details";
                    }
                    for (const column of this.options.columns) {
                        const content = column.render ? column.render(item) : item[column.key];
                        const cell = h("td", { className: [column.align === "right" ? "align-right" : "", column.className || ""].join(" ").trim() || null, "data-label": column.label });
                        if (content instanceof Node) cell.append(content);
                        else cell.textContent = content === null || content === undefined || content === "" ? "—" : String(content);
                        row.append(cell);
                    }
                    return row;
                }));
            }
            this.renderFooter();
        }

        renderFooter() {
            const { total, page, pages, page_size: size } = this.result;
            const first = total ? (page - 1) * size + 1 : 0;
            const last = Math.min(total, page * size);
            const sizeSelect = h("select", { "aria-label": "Rows per page" });
            [10, 25, 50, 100].forEach((value) => sizeSelect.add(new Option(`${value} per page`, String(value))));
            sizeSelect.value = String(this.pageSize);
            sizeSelect.addEventListener("change", () => {
                this.pageSize = Number(sizeSelect.value);
                this.page = 1;
                this.load();
            });
            const go = (target) => {
                this.page = target;
                this.load();
                this.wrap.scrollIntoView({ block: "nearest" });
            };
            const pager = h("nav", { className: "pager", "aria-label": "Pagination" },
                h("button", { className: "button button-small", type: "button", disabled: page <= 1, "aria-label": "Previous page", text: "‹ Prev", onclick: () => go(page - 1) }),
                ...pageNumbers(page, pages).map((value) => value === "…"
                    ? h("span", { className: "pager-gap", text: "…" })
                    : h("button", {
                        className: `button button-small pager-number${value === page ? " active" : ""}`,
                        type: "button",
                        "aria-current": value === page ? "page" : null,
                        "aria-label": `Page ${value}`,
                        text: String(value),
                        onclick: () => go(value),
                    })),
                h("button", { className: "button button-small", type: "button", disabled: page >= pages, "aria-label": "Next page", text: "Next ›", onclick: () => go(page + 1) }),
            );
            this.footer.replaceChildren(
                h("span", { className: "table-count", text: total ? `Showing ${number(first)}–${number(last)} of ${number(total)}` : "No records" }),
                h("div", { className: "table-footer-controls" }, sizeSelect, pages > 1 ? pager : null),
            );
        }
    }

    function pageNumbers(page, pages) {
        const values = new Set([1, pages, page - 1, page, page + 1].filter((value) => value >= 1 && value <= pages));
        const sorted = [...values].sort((a, b) => a - b);
        const result = [];
        sorted.forEach((value, index) => {
            if (index && value - sorted[index - 1] > 1) result.push("…");
            result.push(value);
        });
        return result;
    }

    // ================================================================ image viewer

    /**
     * Full-screen-capable viewer for order images. images: [{ src, name, caption, original }].
     * Zoom with the buttons, mouse wheel, double-click, or + / - keys; drag to pan; arrow keys or the
     * thumbnails switch images; F toggles fullscreen; Esc closes and returns to the order details.
     */
    function openViewer(images, startIndex = 0) {
        if (!images.length) return;
        const MIN = 1;
        const MAX = 8;
        const state = { index: Math.min(Math.max(startIndex, 0), images.length - 1), zoom: 1, x: 0, y: 0, natural: null };
        const dialog = stackedDialog("image-viewer");
        dialog.setAttribute("aria-label", "Image preview");

        const img = h("img", { className: "viewer-image", alt: "", draggable: "false", decoding: "async" });
        const status = h("div", { className: "viewer-status", role: "status" });
        const stage = h("div", { className: "viewer-stage", tabindex: "0" }, img, status);
        const title = h("strong", { className: "viewer-title" });
        const counter = h("span", { className: "viewer-counter" });
        const zoomLabel = h("span", { className: "viewer-zoom", "aria-live": "polite" });
        const tool = (label, symbol, handler, extra = {}) => h("button", { className: "viewer-button", type: "button", "aria-label": label, title: label, text: symbol, onclick: handler, ...extra });
        const fullscreenButton = tool("Fullscreen (F)", "⛶", () => toggleFullscreen());
        const original = h("a", { className: "viewer-button viewer-link", target: "_blank", rel: "noopener", title: "Open original in a new tab", text: "Original ↗" });
        const prev = tool("Previous image (←)", "‹", () => show(state.index - 1), { className: "viewer-button viewer-nav viewer-prev" });
        const next = tool("Next image (→)", "›", () => show(state.index + 1), { className: "viewer-button viewer-nav viewer-next" });
        const thumbs = h("div", { className: "viewer-thumbs" }, ...images.map((image, index) => h("button", {
            className: "viewer-thumb", type: "button", "aria-label": `Show ${image.name || `image ${index + 1}`}`, onclick: () => show(index),
        }, h("img", { src: image.src, alt: "", loading: "lazy", decoding: "async" }))));
        const toolbar = h("div", { className: "viewer-toolbar" },
            h("div", { className: "viewer-heading" }, title, counter),
            h("div", { className: "viewer-tools" },
                tool("Zoom out (−)", "−", () => zoomBy(1 / 1.4)),
                zoomLabel,
                tool("Zoom in (+)", "+", () => zoomBy(1.4)),
                tool("Fit to screen (0)", "Fit", () => reset()),
                tool("Actual size (1)", "1:1", () => actualSize()),
                fullscreenButton,
                original,
                tool("Close preview (Esc)", "×", () => dialog.close(), { className: "viewer-button viewer-close" }),
            ),
        );
        const caption = h("p", { className: "viewer-caption" });
        const frame = h("div", { className: "viewer-frame" }, toolbar, h("div", { className: "viewer-body" }, prev, stage, next), caption, images.length > 1 ? thumbs : null);
        dialog.append(frame);
        prev.hidden = next.hidden = images.length < 2;

        function fitScale() {
            if (!state.natural) return 1;
            const rect = stage.getBoundingClientRect();
            return Math.min(rect.width / state.natural.width, rect.height / state.natural.height, 1);
        }

        function clamp() {
            if (!state.natural) return;
            const rect = stage.getBoundingClientRect();
            const scale = fitScale() * state.zoom;
            const maxX = Math.max(0, (state.natural.width * scale - rect.width) / 2);
            const maxY = Math.max(0, (state.natural.height * scale - rect.height) / 2);
            state.x = Math.min(maxX, Math.max(-maxX, state.x));
            state.y = Math.min(maxY, Math.max(-maxY, state.y));
        }

        function apply() {
            if (!state.natural) return;
            clamp();
            const scale = fitScale() * state.zoom;
            const width = state.natural.width * scale;
            const height = state.natural.height * scale;
            img.style.width = `${width}px`;
            img.style.height = `${height}px`;
            img.style.transform = `translate(calc(-50% + ${state.x}px), calc(-50% + ${state.y}px))`;
            zoomLabel.textContent = `${Math.round(scale * 100)}%`;
            stage.classList.toggle("can-pan", state.zoom > 1.001);
        }

        function zoomTo(target, point) {
            const zoom = Math.min(MAX, Math.max(MIN, target));
            if (point) {
                const rect = stage.getBoundingClientRect();
                const qx = point.x - (rect.left + rect.width / 2);
                const qy = point.y - (rect.top + rect.height / 2);
                state.x = qx - (qx - state.x) * (zoom / state.zoom);
                state.y = qy - (qy - state.y) * (zoom / state.zoom);
            } else {
                state.x *= zoom / state.zoom;
                state.y *= zoom / state.zoom;
            }
            state.zoom = zoom;
            apply();
        }

        const zoomBy = (factor, point) => zoomTo(state.zoom * factor, point);

        function reset() {
            state.zoom = 1;
            state.x = 0;
            state.y = 0;
            apply();
        }

        function actualSize() {
            const fit = fitScale();
            zoomTo(fit ? 1 / fit : 1);
        }

        function show(index) {
            state.index = (index + images.length) % images.length;
            const image = images[state.index];
            state.natural = null;
            state.zoom = 1;
            state.x = 0;
            state.y = 0;
            img.hidden = true;
            status.hidden = false;
            status.replaceChildren(h("span", { className: "spinner", "aria-hidden": "true" }), " Loading image…");
            title.textContent = image.name || "Image";
            counter.textContent = images.length > 1 ? `${state.index + 1} / ${images.length}` : "";
            caption.textContent = image.caption || "";
            caption.hidden = !image.caption;
            original.href = image.original || image.src;
            img.alt = image.name || "Order image";
            thumbs.querySelectorAll(".viewer-thumb").forEach((thumb, position) => thumb.classList.toggle("active", position === state.index));
            img.onload = () => {
                state.natural = { width: img.naturalWidth || 1, height: img.naturalHeight || 1 };
                img.hidden = false;
                status.hidden = true;
                apply();
            };
            img.onerror = () => {
                status.textContent = "This image could not be loaded. It may have been removed, or your session may have expired.";
            };
            img.src = image.src;
        }

        function toggleFullscreen() {
            if (document.fullscreenElement) {
                document.exitFullscreen?.();
            } else if (frame.requestFullscreen) {
                frame.requestFullscreen().catch(() => toast("Fullscreen is not available in this browser.", "error"));
            }
        }

        const onFullscreen = () => {
            fullscreenButton.textContent = document.fullscreenElement ? "⤢" : "⛶";
            fullscreenButton.setAttribute("aria-label", document.fullscreenElement ? "Exit fullscreen (F)" : "Fullscreen (F)");
            window.requestAnimationFrame(apply);
        };
        document.addEventListener("fullscreenchange", onFullscreen);
        const onResize = () => apply();
        window.addEventListener("resize", onResize);

        stage.addEventListener("wheel", (event) => {
            event.preventDefault();
            zoomBy(event.deltaY < 0 ? 1.15 : 1 / 1.15, { x: event.clientX, y: event.clientY });
        }, { passive: false });
        stage.addEventListener("dblclick", (event) => {
            if (state.zoom > 1.001) reset();
            else zoomTo(2.5, { x: event.clientX, y: event.clientY });
        });
        let drag = null;
        stage.addEventListener("pointerdown", (event) => {
            if (state.zoom <= 1.001) return;
            drag = { id: event.pointerId, x: event.clientX - state.x, y: event.clientY - state.y };
            stage.setPointerCapture(event.pointerId);
            stage.classList.add("is-panning");
        });
        stage.addEventListener("pointermove", (event) => {
            if (!drag || drag.id !== event.pointerId) return;
            state.x = event.clientX - drag.x;
            state.y = event.clientY - drag.y;
            apply();
        });
        const endDrag = () => {
            drag = null;
            stage.classList.remove("is-panning");
        };
        stage.addEventListener("pointerup", endDrag);
        stage.addEventListener("pointercancel", endDrag);
        dialog.addEventListener("keydown", (event) => {
            if (event.target.closest("a, input")) return;
            const keys = {
                "+": () => zoomBy(1.4), "=": () => zoomBy(1.4), "-": () => zoomBy(1 / 1.4), "_": () => zoomBy(1 / 1.4),
                0: reset, 1: actualSize, f: toggleFullscreen, F: toggleFullscreen,
                ArrowLeft: () => images.length > 1 && show(state.index - 1),
                ArrowRight: () => images.length > 1 && show(state.index + 1),
            };
            if (keys[event.key]) {
                event.preventDefault();
                keys[event.key]();
            }
        });
        dialog.addEventListener("close", () => {
            document.removeEventListener("fullscreenchange", onFullscreen);
            window.removeEventListener("resize", onResize);
            if (document.fullscreenElement) document.exitFullscreen?.();
        });
        dialog.showModal();
        stage.focus();
        show(state.index);
    }

    // ================================================================ charts

    function niceMax(value) {
        if (value <= 0) return 1;
        const exponent = 10 ** Math.floor(Math.log10(value));
        const fraction = value / exponent;
        const step = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
        return step * exponent;
    }

    let tooltipElement = null;
    function tooltip(target, lines) {
        const show = () => {
            if (!tooltipElement) {
                tooltipElement = h("div", { className: "chart-tooltip", role: "tooltip" });
                document.body.append(tooltipElement);
            }
            tooltipElement.replaceChildren(...lines.map((line, index) => h(index ? "span" : "strong", { text: line })));
            tooltipElement.hidden = false;
            const rect = target.getBoundingClientRect();
            const tip = tooltipElement.getBoundingClientRect();
            const left = Math.min(window.innerWidth - tip.width - 8, Math.max(8, rect.left + rect.width / 2 - tip.width / 2));
            const top = rect.top - tip.height - 8 < 8 ? rect.bottom + 8 : rect.top - tip.height - 8;
            tooltipElement.style.left = `${left}px`;
            tooltipElement.style.top = `${top}px`;
        };
        const hide = () => {
            if (tooltipElement) tooltipElement.hidden = true;
        };
        target.addEventListener("mouseenter", show);
        target.addEventListener("focus", show);
        target.addEventListener("mouseleave", hide);
        target.addEventListener("blur", hide);
        target.setAttribute("aria-label", lines.join(", "));
    }

    /** Horizontal bars, one series. items: [{ label, value, detail }]. */
    function barChart(container, items, { format = number, emptyMessage = "No data yet.", onSelect = null, detail = null } = {}) {
        if (!items.length || items.every((item) => !item.value)) {
            container.replaceChildren(h("p", { className: "empty-state chart-empty", text: emptyMessage }));
            return;
        }
        const max = Math.max(...items.map((item) => item.value), 0) || 1;
        container.replaceChildren(h("div", { className: "hbar-chart" }, ...items.map((item) => {
            const percent = Math.max(item.value > 0 ? 1.5 : 0, (item.value / max) * 100);
            const row = h(onSelect ? "button" : "div", { className: "hbar-row", type: onSelect ? "button" : null, tabindex: onSelect ? null : "0" },
                h("span", { className: "hbar-label", text: item.label }),
                h("span", { className: "hbar-track" }, h("span", { className: "hbar-fill", style: `width:${percent}%` })),
                h("span", { className: "hbar-value", text: format(item.value) }),
            );
            if (onSelect) row.addEventListener("click", () => onSelect(item));
            tooltip(row, [item.label, format(item.value), ...(detail ? [detail(item)] : [])].filter(Boolean));
            return row;
        })));
    }

    /** Vertical columns over time, one series. points: [{ label, short, value, detail }]. */
    function columnChart(container, points, { format = number, axisFormat = format, emptyMessage = "No data yet.", detail = null } = {}) {
        if (!points.length) {
            container.replaceChildren(h("p", { className: "empty-state chart-empty", text: emptyMessage }));
            return;
        }
        const top = niceMax(Math.max(...points.map((point) => point.value), 0));
        const ticks = [0, 0.25, 0.5, 0.75, 1].map((ratio) => top * ratio);
        const labelEvery = Math.max(1, Math.ceil(points.length / 12));
        const grid = h("div", { className: "vchart-grid", "aria-hidden": "true" }, ...ticks.map((tick) => h("div", {
            className: "vchart-gridline", style: `bottom:${(tick / top) * 100}%`,
        }, h("span", { text: axisFormat(tick) }))));
        const columns = h("div", { className: "vchart-columns" }, ...points.map((point, index) => {
            const height = point.value > 0 ? Math.max(1, (point.value / top) * 100) : 0;
            const column = h("div", { className: "vchart-column", tabindex: "0" },
                h("span", { className: "vchart-bar", style: `height:${height}%` }),
            );
            tooltip(column, [point.label, format(point.value), ...(detail ? [detail(point)] : [])].filter(Boolean));
            column.dataset.index = String(index);
            return column;
        }));
        const labels = h("div", { className: "vchart-labels", "aria-hidden": "true" }, ...points.map((point, index) => h("span", {
            text: index % labelEvery === 0 || index === points.length - 1 ? (point.short || point.label) : "",
        })));
        const allZero = points.every((point) => !point.value);
        container.replaceChildren(h("div", { className: "vchart" },
            h("div", { className: "vchart-plot" }, grid, columns),
            labels,
            allZero ? h("p", { className: "chart-note", text: emptyMessage }) : null,
        ));
    }

    // ================================================================ downloads

    async function download(kind, params, format, button) {
        return busy(button, async () => {
            const url = `/api/admin/exports/${encodeURIComponent(kind)}${query({ ...params, format })}`;
            let response;
            try {
                response = await fetch(url, { credentials: "same-origin" });
            } catch {
                toast("The export could not be downloaded. Check the connection and try again.", "error");
                return;
            }
            if (!response.ok) {
                const result = await response.json().catch(() => ({}));
                toast(typeof result.detail === "string" ? result.detail : `The export failed (HTTP ${response.status}).`, "error");
                return;
            }
            const disposition = response.headers.get("Content-Disposition") || "";
            const name = /filename="([^"]+)"/.exec(disposition)?.[1] || `report.${format}`;
            const blob = await response.blob();
            const link = h("a", { href: URL.createObjectURL(blob), download: name });
            document.body.append(link);
            link.click();
            link.remove();
            window.setTimeout(() => URL.revokeObjectURL(link.href), 4000);
            toast(`${format === "pdf" ? "PDF" : "Excel"} report downloaded.`, "success");
        }, "Preparing…");
    }

    function exportButtons(kind, getParams, { label = "Export" } = {}) {
        const pdf = h("button", { className: "button button-small", type: "button", text: `${label} PDF` });
        const xlsx = h("button", { className: "button button-small", type: "button", text: `${label} Excel` });
        pdf.addEventListener("click", () => download(kind, getParams(), "pdf", pdf));
        xlsx.addEventListener("click", () => download(kind, getParams(), "xlsx", xlsx));
        return h("div", { className: "export-buttons" }, pdf, xlsx);
    }

    // ================================================================ misc

    function debounce(fn, wait = 250) {
        let timer;
        return (...args) => {
            window.clearTimeout(timer);
            timer = window.setTimeout(() => fn(...args), wait);
        };
    }

    function todayLocal() {
        const parts = new Intl.DateTimeFormat("en-CA", { timeZone: SHOP_TIME_ZONE, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
        return parts; // en-CA formats as YYYY-MM-DD
    }

    window.AdminUI = {
        h, money, compactMoney, number, date, dateTime, days, relativeTime, slug, fileSize, badge,
        api, query, toast, openModal, closeModal, confirm, busy, DataTable, openViewer,
        barChart, columnChart, download, exportButtons, debounce, todayLocal,
    };
})();
