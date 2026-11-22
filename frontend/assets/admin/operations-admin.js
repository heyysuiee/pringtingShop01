"use strict";

/**
 * Production Monitoring, Fulfillment Management, Cancellation & Refunds, the matching order-detail
 * sections and action forms, and the payment-method / delivery settings.
 *
 * Every figure and rule comes from the server (fulfillment_services, refund_services,
 * shop_settings_services); these screens only collect input and show the saved records.
 * Exposed as window.PrintShopOperationsAdmin and called by dashboard.js with its shared builders.
 */
(() => {
    const UI = window.AdminUI;
    const { h, api, query, money, date, dateTime, number, badge, toast } = UI;
    const MAX_FILE_BYTES = 5 * 1024 * 1024;
    const FILE_ACCEPT = ".jpg,.jpeg,.png,.webp,.pdf,image/jpeg,image/png,image/webp,application/pdf";
    const IMAGE_ACCEPT = ".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp";

    // ================================================================ small helpers

    const plural = (count, word) => `${number(count)} ${word}${count === 1 ? "" : "s"}`;

    function readFile(file) {
        return new Promise((resolve, reject) => {
            if (file.size > MAX_FILE_BYTES) {
                reject(new Error(`${file.name} is larger than 5 MB.`));
                return;
            }
            const reader = new FileReader();
            reader.addEventListener("load", () => resolve({ name: file.name, dataUrl: reader.result }));
            reader.addEventListener("error", () => reject(new Error(`Could not read ${file.name}.`)));
            reader.readAsDataURL(file);
        });
    }

    async function readFiles(input, limit = 4) {
        const files = Array.from(input?.files || []);
        if (files.length > limit) throw new Error(`Attach up to ${limit} files.`);
        return Promise.all(files.map(readFile));
    }

    function fileInput(id, { multiple = true, accept = FILE_ACCEPT, required = false } = {}) {
        return h("input", { id, type: "file", accept, multiple, required });
    }

    function formField(label, control, full = false) {
        const id = control.id || `op-${Math.random().toString(36).slice(2, 8)}`;
        control.id = id;
        return h("div", { className: `form-field${full ? " full" : ""}` }, h("label", { for: id, text: label }), control);
    }

    function textInput(attrs = {}) {
        return h("input", { type: "text", maxlength: 200, ...attrs });
    }

    function textArea(attrs = {}) {
        return h("textarea", { maxlength: 1000, rows: 2, ...attrs });
    }

    function intValue(input) {
        const value = input.value.trim();
        return value === "" ? null : Number.parseInt(value, 10);
    }

    function fileLinks(files) {
        const list = (files || []).filter(Boolean);
        if (!list.length) return null;
        return h("div", { className: "op-files" }, ...list.map((file) => h("a", { href: file.url, target: "_blank", rel: "noopener", text: `📎 ${file.name}` })));
    }

    function actionForm(title, note, ...content) {
        return h("form", { className: "action-form op-form" }, h("h4", { text: title }), note ? h("p", { className: "field-note", text: note }) : null, ...content);
    }

    function progressBar(value) {
        const percent = Math.max(0, Math.min(100, Number(value) || 0));
        return h("span", { className: "op-progress", title: `${percent}%` }, h("span", { className: "op-progress-bar", style: `width:${percent}%` }), h("span", { className: "op-progress-label", text: `${percent}%` }));
    }

    /** POST an order action from a form: confirmation, busy state, toast, and reload of the order. */
    function bindSubmit(form, ctx, order, action, build, { confirm, success, reload }) {
        const submit = form.querySelector("button[type=submit]");
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            let payload;
            try {
                payload = await build();
            } catch (error) {
                toast(error.message, "error");
                return;
            }
            if (confirm) {
                const answer = await UI.confirm(typeof confirm === "function" ? confirm(payload) : confirm);
                if (!answer) return;
                if (answer.value && payload.__confirmInto) payload[payload.__confirmInto] = answer.value;
            }
            delete payload.__confirmInto;
            await ctx.submitAction(submit, order, action, payload, reload, success);
        });
        return form;
    }

    // ================================================================ order detail sections

    function productionCard(order, ctx) {
        const p = order.production || {};
        const started = p.started_at || p.updates?.length || p.quality_check || p.packaging;
        if (!started && !["Approved for Printing", "In Production"].includes(order.status)) return null;
        const quality = p.quality_check;
        const checks = (order.quality_checks || []).map((item) => [item.label, quality?.checks ? (quality.checks[item.key] ? "✓ Passed" : "✗ Failed") : "—"]);
        const reprint = p.reprint;
        return h("section", { className: "fact-card", dataset: { focus: "production" } },
            h("h3", { text: p.cycle > 1 ? `Production monitoring · cycle ${p.cycle}` : "Production monitoring" }),
            reprint ? h("p", { className: `notice-card ${reprint.status === "in_progress" ? "notice-warning" : "notice-success"}`, text: `Reprint ${reprint.id} (${reprint.status === "in_progress" ? "in progress" : "completed"}): ${number(reprint.quantity)} pc(s) · ${reprint.reason} · ${reprint.chargeable ? `chargeable ${money(reprint.charge_amount)} (customer agreed)` : "free"} · authorized by ${reprint.authorized_by || "the administrator"} on ${dateTime(reprint.authorized_at)}${reprint.refund_id ? ` · from refund request ${reprint.refund_id}` : ""}` }) : null,
            ctx.detailList([
                ["Progress", progressBar(p.progress)],
                ["Production started", p.started_at ? dateTime(p.started_at) : "Not started"],
                ["Estimated completion", p.estimated_completion ? date(p.estimated_completion) : "—"],
                p.personnel ? ["Personnel", p.personnel] : null,
                ["Ordered / printed / completed", `${number(p.ordered_quantity)} / ${p.printed_quantity ?? "—"} / ${p.completed_quantity ?? "—"}`],
                p.remarks ? ["Latest remarks", p.remarks] : null,
            ]),
            fileLinks(p.photos),
            quality ? h("div", { className: "op-subsection" },
                h("h4", { text: `Quality check · ${quality.passed ? "Passed" : "Failed"} · ${dateTime(quality.at)}` }),
                ctx.detailList([
                    ...checks,
                    quality.defects ? ["Defects", quality.defects] : null,
                    quality.corrective_actions ? ["Corrective action", quality.corrective_actions] : null,
                    quality.remarks ? ["Remarks", quality.remarks] : null,
                ]),
                fileLinks(quality.photos),
                p.quality_history?.length ? h("p", { className: "field-note", text: `${plural(p.quality_history.length, "earlier inspection")} kept in the history.` }) : null,
            ) : null,
            p.packaging ? h("div", { className: "op-subsection" },
                h("h4", { text: "Packaging" }),
                ctx.detailList([
                    ["Started", p.packaging.started_at ? dateTime(p.packaging.started_at) : "—"],
                    ["Completed", p.packaging.completed_at ? dateTime(p.packaging.completed_at) : "In progress"],
                    p.packaging.completed_quantity ? ["Packed quantity", number(p.packaging.completed_quantity)] : null,
                    p.packaging.remarks ? ["Remarks", p.packaging.remarks] : null,
                ]),
                fileLinks(p.packaging.proof),
            ) : null,
            p.previous_cycles?.length ? h("details", { className: "op-details" }, h("summary", { text: `Earlier production cycles (${p.previous_cycles.length})` }),
                h("ol", { className: "op-list" }, ...p.previous_cycles.map((cycle) => h("li", {}, h("strong", { text: `Cycle ${cycle.cycle}` }),
                    h("span", { className: "table-secondary", text: [cycle.started_at ? `started ${dateTime(cycle.started_at)}` : "", cycle.quality_passed ? "quality check passed" : "", cycle.packed_at ? `packed ${dateTime(cycle.packed_at)}` : ""].filter(Boolean).join(" · ") }))))) : null,
            p.updates?.length ? h("details", { className: "op-details" }, h("summary", { text: `Production updates (${p.updates.length})` }),
                h("ol", { className: "timeline" }, ...[...p.updates].reverse().map((item) => h("li", {},
                    h("span", { text: Object.entries(item).filter(([key]) => !["at", "by", "photo_count"].includes(key)).map(([key, value]) => `${key.replace(/_/g, " ")}: ${value}`).join(" · ") || "Photos added" }),
                    h("time", { text: `${dateTime(item.at)}${item.by ? ` · ${item.by}` : ""}` }))))) : null,
        );
    }

    function draftsCard(order) {
        const drafts = order.drafts || [];
        if (!drafts.length) return null;
        return h("section", { className: "fact-card", dataset: { focus: "drafts" } },
            h("h3", { text: `Draft history (${drafts.length})` }),
            h("ol", { className: "op-list" }, ...[...drafts].reverse().map((item) => h("li", {},
                h("div", { className: "op-list-head" }, h("strong", { text: `Draft ${item.number}` }), badge(item.status_label || item.status)),
                item.file ? h("a", { href: item.file.url, target: "_blank", rel: "noopener", text: item.file.name }) : null,
                h("span", { className: "table-secondary", text: [item.sent_at ? `Sent ${dateTime(item.sent_at)}` : "", item.sent_by ? `by ${item.sent_by}` : "", item.responded_at ? `· answered ${dateTime(item.responded_at)}` : ""].filter(Boolean).join(" ") }),
                item.remarks ? h("p", { className: "field-note", text: `Remarks: ${item.remarks}` }) : null,
                item.revision_note ? h("p", { className: "field-note", text: `Customer's revision request: ${item.revision_note}` }) : null,
            ))),
        );
    }

    function deliveryCard(order, ctx, reload) {
        const f = order.fulfillment || {};
        const attempts = f.attempts || [];
        const issues = f.issues || [];
        const cycles = f.previous_cycles || [];
        const hasData = f.courier || attempts.length || f.pickup_confirmation || issues.length || f.pickup_schedule || f.released_at || cycles.length;
        if (!hasData) return null;
        return h("section", { className: "fact-card", dataset: { focus: "fulfillment" } },
            h("h3", { text: f.method === "delivery" ? "Delivery tracking" : "Pickup" }),
            ctx.detailList([
                f.released_at ? ["Released", dateTime(f.released_at)] : null,
                f.courier ? ["Courier", `${f.courier}${f.courier_service ? ` · ${f.courier_service}` : ""}`] : null,
                f.tracking_number ? ["Tracking number", f.tracking_url ? h("a", { href: f.tracking_url, target: "_blank", rel: "noopener", text: f.tracking_number }) : f.tracking_number] : null,
                f.delivery_personnel ? ["Delivery personnel", f.delivery_personnel] : null,
                f.shipment_date ? ["Shipment date", date(f.shipment_date)] : null,
                f.scheduled_date ? ["Scheduled delivery", date(f.scheduled_date)] : null,
                f.delivered_at ? ["Delivered", dateTime(f.delivered_at)] : null,
                f.pickup_schedule ? ["Pickup schedule", `${date(f.pickup_schedule.date)} ${f.pickup_schedule.time || ""}`.trim()] : null,
                f.method === "pickup" ? ["Pickup code", f.pickup_code_set ? "Issued to the customer (ask the claimant for it)" : "Issued when the order is ready"] : null,
                f.pickup_confirmation ? ["Claimed by", `${f.pickup_confirmation.claimant_name} (${f.pickup_confirmation.claimant_type_label}) · ${dateTime(f.pickup_confirmation.claimed_at)}`] : null,
                f.pickup_confirmation?.authorization_details ? ["Authorization", f.pickup_confirmation.authorization_details] : null,
            ]),
            attempts.length ? h("div", { className: "op-subsection" }, h("h4", { text: `Delivery attempts (${attempts.length})` }),
                h("ol", { className: "op-list" }, ...attempts.map((item) => h("li", {},
                    h("div", { className: "op-list-head" }, h("strong", { text: `Attempt ${item.number}` }), badge({ delivered: "Delivered", failed: "Delivery Failed", out_for_delivery: "Out for Delivery" }[item.status] || item.status)),
                    h("span", { className: "table-secondary", text: [item.courier, item.tracking_number, item.personnel, item.dispatched_at ? `dispatched ${dateTime(item.dispatched_at)}` : ""].filter(Boolean).join(" · ") }),
                    item.failure_label ? h("p", { className: "field-note", text: `Failed: ${item.failure_label}${item.failure_remarks ? ` — ${item.failure_remarks}` : ""}` }) : null,
                    item.received_by ? h("p", { className: "field-note", text: `Received by ${item.received_by}` }) : null,
                    fileLinks(item.proof),
                )))) : null,
            cycles.length ? h("details", { className: "op-details" }, h("summary", { text: `Earlier releases before a reprint (${cycles.length})` }),
                h("ol", { className: "op-list" }, ...cycles.map((cycle) => h("li", {},
                    h("strong", { text: `Cycle ${cycle.cycle}${cycle.delivered_at ? ` · delivered ${dateTime(cycle.delivered_at)}` : ""}${cycle.pickup_confirmation ? ` · claimed by ${cycle.pickup_confirmation.claimant_name}` : ""}` }),
                    ...cycle.attempts.map((item) => h("span", { className: "table-secondary", text: `Attempt ${item.number}: ${[item.courier, item.tracking_number, item.status].filter(Boolean).join(" · ")}` })),
                    ...cycle.attempts.map((item) => fileLinks(item.proof)))))) : null,
            issues.length ? h("div", { className: "op-subsection" }, h("h4", { text: `Delivery issues (${issues.length})` }),
                h("ol", { className: "op-list" }, ...issues.map((item) => h("li", {},
                    h("div", { className: "op-list-head" }, h("strong", { text: `${item.id} · ${item.type_label}` }), badge(item.status === "open" ? "Open" : "Resolved")),
                    h("p", { className: "field-note", text: item.details }),
                    item.resolution ? h("p", { className: "field-note", text: `Resolution: ${item.resolution}` }) : null,
                    item.status === "open" ? resolveIssueForm(order, item, ctx, reload) : null,
                )))) : null,
        );
    }

    function resolveIssueForm(order, issue, ctx, reload) {
        const resolution = textArea({ required: true, placeholder: "What the shop did (e.g. courier confirmed redelivery on Friday)" });
        const form = actionForm("Resolve this issue", "", formField("Resolution (shown to the customer)", resolution, true),
            h("div", { className: "dialog-actions" }, h("button", { className: "button button-primary button-small", type: "submit", text: "Mark resolved" })));
        return bindSubmit(form, ctx, order, `issues/${encodeURIComponent(issue.id)}/resolve`, async () => ({ resolution: resolution.value.trim() }), { success: "Delivery issue resolved.", reload });
    }

    const CANCEL_LABEL = { pending_review: "Pending Review", approved: "Approved", rejected: "Rejected" };

    function cancellationCard(order, ctx, reload) {
        const requests = order.cancellation_requests || [];
        if (!requests.length) return null;
        return h("section", { className: "fact-card", dataset: { focus: "cancellation" } },
            h("h3", { text: `Cancellation requests (${requests.length})` }),
            h("ol", { className: "op-list" }, ...[...requests].reverse().map((item) => h("li", {},
                h("div", { className: "op-list-head" }, h("strong", { text: `${item.id} · ${item.reason_label}` }), badge(CANCEL_LABEL[item.status] || item.status)),
                ctx.detailList([
                    ["Requested", dateTime(item.requested_at)],
                    ["Order status when requested", item.order_status],
                    ["Verified payments", money(item.verified_paid)],
                    item.remarks ? ["Customer remarks", item.remarks] : null,
                    item.refund_recipient?.name ? ["Refund to", `${item.refund_recipient.provider}: ${item.refund_recipient.name} · ${item.refund_recipient.number}`] : null,
                    item.decided_at ? ["Decided", `${dateTime(item.decided_at)}${item.decided_by ? ` by ${item.decided_by}` : ""}`] : null,
                    item.total_deductions ? ["Deductions", `${money(item.total_deductions)} — ${item.deduction_justification}`] : null,
                    item.approved_refund !== undefined ? ["Approved refund", money(item.approved_refund)] : null,
                    item.remarks_admin ? ["Administrator remarks", item.remarks_admin] : null,
                ]),
                item.status === "pending_review" ? decideCancellation(order, item, ctx, reload) : null,
            ))),
        );
    }

    function deductionRows() {
        const host = h("div", { className: "op-deductions" });
        const add = () => {
            const label = textInput({ placeholder: "e.g. Design work completed", maxlength: 160 });
            const amount = h("input", { type: "number", min: "0.01", step: "0.01", placeholder: "Amount" });
            const remove = h("button", { className: "button button-small", type: "button", text: "Remove", onclick: () => row.remove() });
            const row = h("div", { className: "op-deduction-row" }, label, amount, remove);
            host.append(row);
        };
        const addButton = h("button", { className: "button button-small", type: "button", text: "+ Add documented deduction", onclick: add });
        return {
            element: h("div", {}, host, addButton),
            values: () => [...host.querySelectorAll(".op-deduction-row")].map((row) => {
                const [label, amount] = row.querySelectorAll("input");
                return { label: label.value.trim(), amount: Number(amount.value) };
            }).filter((item) => item.label || item.amount),
        };
    }

    function decideCancellation(order, request, ctx, reload) {
        const deductions = deductionRows();
        const justification = textArea({ placeholder: "Required when there are deductions: the documented costs and the approved policy" });
        const refundAmount = h("input", { type: "number", min: "0", step: "0.01", placeholder: `Default: ${money(order.max_refundable)} minus deductions` });
        const work = textArea({ placeholder: "Work completed and documented expenses (draft, printing, materials)" });
        const remarks = textArea({ placeholder: "Remarks for the customer (optional)" });
        const approve = h("button", { className: "button button-primary", type: "submit", text: "Approve cancellation" });
        const reject = h("button", { className: "button button-danger-strong", type: "button", text: "Reject request" });
        const policy = order.cancellation_policy || {};
        const pending = (order.payment_submissions || []).some((item) => item.status === "pending");
        const form = actionForm("Review this cancellation", `Stage: ${policy.stage}. ${policy.guidance} This is the shop's proposed policy (guidance only): enter any deduction yourself, with its justification. Verified payments ${money(order.summary.amount_paid)}; refundable now ${money(order.max_refundable)}.`,
            pending ? h("p", { className: "notice-card notice-warning", text: "A payment is waiting for verification. Verify or reject it first so the refund is calculated from verified money only." }) : null,
            h("div", { className: "form-grid" },
                formField("Completed work / documented expenses", work, true),
                h("div", { className: "form-field full" }, h("span", { className: "form-label", text: "Deductions" }), deductions.element),
                formField("Deduction justification", justification, true),
                formField("Refund amount (PHP)", refundAmount),
                formField("Remarks", remarks, true)),
            h("div", { className: "dialog-actions" }, reject, approve));
        reject.addEventListener("click", async () => {
            const answer = await UI.confirm({ title: "Reject the cancellation request?", message: "The order continues. The reason is shown to the customer.", confirmLabel: "Reject request", tone: "danger", input: { label: "Reason (required)", required: true } });
            if (!answer) return;
            await ctx.submitAction(reject, order, `cancellations/${encodeURIComponent(request.id)}/reject`, { remarks: answer.value }, reload, "Cancellation request rejected.");
        });
        return bindSubmit(form, ctx, order, `cancellations/${encodeURIComponent(request.id)}/approve`, async () => {
            const list = deductions.values();
            if (list.some((item) => !item.label || !(item.amount > 0))) throw new Error("Each deduction needs a description and an amount.");
            const body = { deductions: list, deduction_justification: justification.value.trim(), completed_work: work.value.trim(), remarks: remarks.value.trim() };
            if (refundAmount.value.trim() !== "") body.refund_amount = Number(refundAmount.value);
            return body;
        }, {
            confirm: (body) => {
                const deducted = body.deductions.reduce((sum, item) => sum + item.amount, 0);
                const refund = body.refund_amount ?? Math.max(0, order.max_refundable - deducted);
                return { title: "Approve the cancellation?", message: "The order is cancelled and production, payment, and dispatch stop. A refund record is created when money is due back.", confirmLabel: "Approve cancellation", tone: "danger", details: [["Verified payments", money(order.summary.amount_paid)], ["Deductions", money(deducted)], ["Refund to approve", money(refund)]] };
            },
            success: "Cancellation approved.",
            reload,
        });
    }

    function refundsCard(order, ctx, reload) {
        const refunds = order.refunds || [];
        const canOpen = order.actions?.create_refund;
        if (!refunds.length && !canOpen) return null;
        return h("section", { className: "fact-card", dataset: { focus: "refunds" } },
            h("h3", { text: `Refunds (${refunds.length})` }),
            h("p", { className: "field-note", text: `Verified payments ${money(order.summary.amount_paid)} · refunded ${money(order.payment?.refunded || 0)} · still refundable ${money(order.max_refundable)}` }),
            refunds.length ? h("ol", { className: "op-list" }, ...[...refunds].reverse().map((item) => h("li", {},
                h("div", { className: "op-list-head" }, h("strong", { text: `${item.id} · ${item.reason_label}` }), badge(item.status_label)),
                ctx.detailList([
                    ["Requested", `${dateTime(item.requested_at)} by ${item.requested_by === "shop" ? "the shop" : "the customer"}`],
                    item.details ? ["Details", item.details] : null,
                    item.requested_amount ? ["Requested amount", money(item.requested_amount)] : null,
                    item.amount !== null && item.amount !== undefined ? ["Approved amount", money(item.amount)] : null,
                    item.total_deductions ? ["Deductions", `${money(item.total_deductions)} — ${item.deduction_justification}`] : null,
                    item.preferred_resolution === "reprint" ? ["Customer prefers", "Reprint"] : null,
                    item.recipient?.name ? ["Refund to", `${item.recipient.provider}: ${item.recipient.name} · ${item.recipient.number}`] : null,
                    item.original_payments?.length ? ["Original payments", item.original_payments.map((pay) => `${pay.method} ${pay.reference} (${money(pay.amount)})`).join("; ")] : null,
                    item.transaction_reference ? ["Refund transaction", `${item.method} · ${item.transaction_reference} · ${dateTime(item.refunded_at)}`] : null,
                    item.failure_reason ? ["Last failure", item.failure_reason] : null,
                    item.remarks ? ["Remarks", item.remarks] : null,
                ]),
                fileLinks(item.evidence),
                fileLinks(item.proof),
                refundActions(order, item, ctx, reload),
            ))) : null,
            canOpen ? openRefundForm(order, ctx, reload) : null,
        );
    }

    function refundActions(order, refund, ctx, reload) {
        const base = `refunds/${encodeURIComponent(refund.id)}`;
        const wrap = h("div", { className: "op-refund-actions" });
        if (refund.status === "pending_review") {
            const amount = h("input", { type: "number", min: "0.01", step: "0.01", value: refund.requested_amount ?? "", required: true });
            const deductions = deductionRows();
            const justification = textArea({ placeholder: "Required when there are deductions" });
            const remarks = textArea({ placeholder: "Remarks (optional; required for a reprint)" });
            const approve = h("button", { className: "button button-primary", type: "submit", text: "Approve refund" });
            const reprint = ["incorrect_product", "damaged_product"].includes(refund.reason)
                ? h("button", { className: "button", type: "button", text: "Resolve by reprint" }) : null;
            const reject = h("button", { className: "button button-danger-strong", type: "button", text: "Reject" });
            const notEligible = h("button", { className: "button", type: "button", text: "Not eligible" });
            const reprintFields = reprint ? reprintOptions(order, { requireReason: false }) : null;
            const form = actionForm("Review refund request", `Maximum for this refund: ${money(order.max_refundable)} (verified payments minus completed and approved refunds). ${["incorrect_product", "damaged_product"].includes(refund.reason) ? "Wrong or damaged products: no change-of-mind deductions apply." : ""}`,
                h("div", { className: "form-grid" },
                    formField("Approved amount (PHP)", amount),
                    h("div", { className: "form-field full" }, h("span", { className: "form-label", text: "Deductions" }), deductions.element),
                    formField("Deduction justification", justification, true),
                    formField("Remarks", remarks, true)),
                reprintFields ? h("details", { className: "op-details" }, h("summary", { text: "Reprint options (for “Resolve by reprint”)" }), reprintFields.element) : null,
                h("div", { className: "dialog-actions" }, reject, notEligible, reprint, approve));
            const decline = (status, button, label) => button.addEventListener("click", async () => {
                const answer = await UI.confirm({ title: `${label}?`, message: "The reason is shown to the customer.", confirmLabel: label, tone: "danger", input: { label: "Reason (required)", required: true } });
                if (!answer) return;
                await ctx.submitAction(button, order, `${base}/${status}`, { remarks: answer.value }, reload, `Refund marked ${label.toLowerCase()}.`);
            });
            decline("reject", reject, "Reject refund");
            decline("not-eligible", notEligible, "Not eligible");
            reprint?.addEventListener("click", async () => {
                if (!remarks.value.trim()) {
                    toast("Describe the reprint in Remarks first.", "error");
                    remarks.focus();
                    return;
                }
                const values = reprintFields.values();
                if (values.chargeable && !(values.charge_amount > 0 && values.customer_agreed)) {
                    toast("A chargeable reprint needs the agreed charge and the customer's agreement.", "error");
                    return;
                }
                const answer = await UI.confirm({ title: "Resolve by reprint?", message: "No money is refunded. Production restarts as a linked reprint (quality checking and packaging again); the original order, payments, and delivery history are kept.", confirmLabel: "Authorize reprint", details: [["Pieces", String(values.quantity || "all")], ["Restart at", values.restart_stage], ["Charge", values.chargeable ? money(values.charge_amount) : "Free"]] });
                if (!answer) return;
                await ctx.submitAction(reprint, order, `${base}/approve`, { resolution: "reprint", remarks: remarks.value.trim(), ...values, authorized: true }, reload, "Reprint authorized and production restarted.");
            });
            wrap.append(bindSubmit(form, ctx, order, `${base}/approve`, async () => {
                const list = deductions.values();
                if (list.some((item) => !item.label || !(item.amount > 0))) throw new Error("Each deduction needs a description and an amount.");
                return { resolution: "refund", amount: Number(amount.value), deductions: list, deduction_justification: justification.value.trim(), remarks: remarks.value.trim() };
            }, { confirm: (body) => ({ title: "Approve this refund?", message: "The amount is reserved for this refund. Mark it Refunded only after the transfer is confirmed.", confirmLabel: "Approve refund", details: [["Approved amount", money(body.amount)]] }), success: "Refund approved.", reload }));
        }
        if (refund.status === "approved" || refund.status === "failed") {
            const start = h("button", { className: "button button-primary button-small", type: "button", text: refund.status === "failed" ? "Retry transfer" : "Start processing" });
            start.addEventListener("click", async () => {
                const retry = refund.status === "failed";
                const answer = await UI.confirm({
                    title: retry ? "Retry this refund?" : "Start processing?",
                    message: retry ? "Confirm that the failed transfer was NOT received by the customer, so the refund is never sent twice." : "Send the refund through the e-wallet, then record its transaction reference and receipt.",
                    confirmLabel: retry ? "Yes, retry" : "Start processing",
                });
                if (!answer) return;
                await ctx.submitAction(start, order, `${base}/processing`, retry ? { confirmed_not_sent: true } : {}, reload, "Refund is processing.");
            });
            wrap.append(start);
            if (refund.status === "approved") {
                const cancel = h("button", { className: "button button-small", type: "button", text: "Reject" });
                cancel.addEventListener("click", async () => {
                    const answer = await UI.confirm({ title: "Reject this approved refund?", confirmLabel: "Reject refund", tone: "danger", input: { label: "Reason (required)", required: true } });
                    if (answer) await ctx.submitAction(cancel, order, `${base}/reject`, { remarks: answer.value }, reload, "Refund rejected.");
                });
                wrap.append(cancel);
            }
        }
        if (refund.status === "processing") {
            const method = h("select", {}, ...(order.payment_methods || ["GCash", "Maya"]).filter((item) => !["Cash on Delivery"].includes(item)).map((item) => new Option(item, item)));
            const recipientName = textInput({ required: true, value: refund.recipient?.name || "", maxlength: 120 });
            const recipientNumber = textInput({ required: true, value: refund.recipient?.number || "", maxlength: 40 });
            const transaction = textInput({ required: true, maxlength: 120, placeholder: "E-wallet transaction / reference number" });
            const proof = fileInput("refund-proof", { required: true });
            const verified = h("input", { type: "checkbox", required: true });
            const confirmed = h("input", { type: "checkbox", required: true });
            const submit = h("button", { className: "button button-primary", type: "submit", text: "Mark as refunded" });
            const fail = h("button", { className: "button button-danger-strong", type: "button", text: "Transfer failed" });
            const form = actionForm("Record the completed transfer", `Refund ${money(refund.amount)}. Never ask the customer for a password, PIN, or OTP.`,
                h("div", { className: "form-grid" },
                    formField("Refund method", method),
                    formField("Transaction reference", transaction),
                    formField("Recipient name", recipientName),
                    formField("Recipient e-wallet number", recipientNumber),
                    formField("Transfer receipt (JPG, PNG, WebP, PDF)", proof, true)),
                h("label", { className: "check-row" }, verified, " The recipient account was verified as the customer's"),
                h("label", { className: "check-row" }, confirmed, " The transfer was completed (checked in the e-wallet account)"),
                h("div", { className: "dialog-actions" }, fail, submit));
            fail.addEventListener("click", async () => {
                const answer = await UI.confirm({ title: "Record a failed transfer?", message: "No money is recorded as refunded. You can retry later.", confirmLabel: "Record failure", tone: "danger", input: { label: "What happened (required)", required: true } });
                if (answer) await ctx.submitAction(fail, order, `${base}/fail`, { reason: answer.value }, reload, "Failed transfer recorded.");
            });
            wrap.append(bindSubmit(form, ctx, order, `${base}/complete`, async () => ({
                method: method.value,
                transaction_reference: transaction.value.trim(),
                recipient_name: recipientName.value.trim(),
                recipient_number: recipientNumber.value.trim(),
                recipient_verified: verified.checked,
                confirmed: confirmed.checked,
                proof: await readFiles(proof, 3),
            }), { confirm: { title: "Mark this refund as refunded?", message: "Only after the money actually reached the customer's account.", confirmLabel: "Mark as refunded" }, success: "Refund completed.", reload }));
        }
        return wrap.childElementCount ? wrap : null;
    }

    function openRefundForm(order, ctx, reload) {
        const reason = h("select", {}, ...[["shop_unable", "Shop unable to fulfill the order"], ["duplicate_payment", "Duplicate payment"], ["excess_payment", "Excess payment"], ["incorrect_product", "Incorrect product"], ["damaged_product", "Damaged product"], ["other", "Other verified reason"]].map(([value, text]) => new Option(text, value)));
        const details = textArea({ required: true, placeholder: "What happened and how it was verified" });
        const form = actionForm("Open a refund", "Use for a duplicate or excess payment (the order is not cancelled) or when the shop cannot fulfill the order. It starts in Pending Review.",
            h("div", { className: "form-grid" }, formField("Reason", reason), formField("Details", details, true)),
            h("div", { className: "dialog-actions" }, h("button", { className: "button button-small", type: "submit", text: "Open refund" })));
        const wrapper = h("details", { className: "op-details" }, h("summary", { text: "Open a refund for this order" }), form);
        bindSubmit(form, ctx, order, "refunds", async () => ({ reason: reason.value, details: details.value.trim() }), { success: "Refund opened.", reload });
        return wrapper;
    }

    /** Extra sections for the order details dialog. */
    function orderSections(order, reload, ctx) {
        return [draftsCard(order), productionCard(order, ctx), deliveryCard(order, ctx, reload), cancellationCard(order, ctx, reload), refundsCard(order, ctx, reload)].filter(Boolean);
    }

    // ================================================================ order action forms

    function productionForm(order, ctx, reload) {
        const p = order.production || {};
        const estimated = h("input", { type: "date", value: p.estimated_completion || "" });
        const personnel = textInput({ value: p.personnel || "", maxlength: 120 });
        const printed = h("input", { type: "number", min: "0", step: "1", value: p.printed_quantity ?? "" });
        const completed = h("input", { type: "number", min: "0", step: "1", max: String(p.ordered_quantity || ""), value: p.completed_quantity ?? "" });
        const progress = h("input", { type: "number", min: "0", max: "100", step: "1", value: p.progress ?? "" });
        const remarks = textArea({ placeholder: "Production remarks (optional)" });
        const photos = fileInput("production-photos", { accept: FILE_ACCEPT });
        const form = actionForm("Update production", `Ordered quantity: ${number(p.ordered_quantity)} pcs.`,
            h("div", { className: "form-grid" },
                formField("Estimated completion", estimated),
                formField("Personnel", personnel),
                formField("Printed quantity", printed),
                formField("Completed quantity", completed),
                formField("Progress (%)", progress),
                formField("Remarks", remarks, true),
                formField("Photos (optional, up to 4)", photos, true)),
            h("div", { className: "dialog-actions" }, h("button", { className: "button button-primary button-small", type: "submit", text: "Save progress" })));
        const details = h("details", { className: "op-details", open: order.status === "In Production" }, h("summary", { text: "Production progress" }), form);
        bindSubmit(form, ctx, order, "production", async () => {
            const body = { remarks: remarks.value.trim(), photos: await readFiles(photos) };
            if (estimated.value) body.estimated_completion = estimated.value;
            if (personnel.value.trim() && personnel.value.trim() !== (p.personnel || "")) body.personnel = personnel.value.trim();
            for (const [key, input] of [["printed_quantity", printed], ["completed_quantity", completed], ["progress", progress]]) {
                const value = intValue(input);
                if (value !== null) body[key] = value;
            }
            return body;
        }, { success: "Production updated.", reload });
        return details;
    }

    function qualityForm(order, ctx, reload) {
        const boxes = (order.quality_checks || []).map((item) => ({ ...item, input: h("input", { type: "checkbox" }) }));
        const defects = textArea({ placeholder: "Defects or issues found (required if any check fails)" });
        const corrective = textArea({ placeholder: "Corrective action (required if any check fails)" });
        const remarks = textArea({ placeholder: "Inspection remarks" });
        const inspected = h("input", { type: "number", min: "0", step: "1" });
        const photos = fileInput("quality-photos");
        const form = actionForm("Quality checklist", "Tick each item that passed. All passed: the order moves to Packaging. Any failed: it returns to In Production for rework.",
            h("div", { className: "op-checklist" }, ...boxes.map((item) => h("label", { className: "check-row" }, item.input, ` ${item.label}`))),
            h("div", { className: "form-grid" },
                formField("Inspected quantity", inspected),
                formField("Defects", defects, true),
                formField("Corrective action", corrective, true),
                formField("Remarks", remarks, true),
                formField("Photos (optional)", photos, true)),
            h("div", { className: "dialog-actions" }, h("button", { className: "button button-primary", type: "submit", text: "Save quality check" })));
        return bindSubmit(form, ctx, order, "quality-check", async () => {
            const body = { checks: Object.fromEntries(boxes.map((item) => [item.key, item.input.checked])), defects: defects.value.trim(), corrective_actions: corrective.value.trim(), remarks: remarks.value.trim(), photos: await readFiles(photos) };
            const value = intValue(inspected);
            if (value !== null) body.inspected_quantity = value;
            return body;
        }, {
            confirm: (body) => {
                const passed = Object.values(body.checks).every(Boolean);
                return { title: passed ? "Record a passed quality check?" : "Record a failed quality check?", message: passed ? "The order moves to Packaging." : "The order returns to In Production for rework.", confirmLabel: "Save", tone: passed ? "primary" : "danger" };
            },
            success: "Quality check recorded.",
            reload,
        });
    }

    function packagingForm(order, ctx, reload) {
        const quantity = h("input", { type: "number", min: "1", step: "1", required: true, max: String(order.production?.ordered_quantity || ""), value: order.production?.completed_quantity ?? order.customization?.quantity ?? "" });
        const remarks = textArea({ placeholder: "Packaging remarks" });
        const proof = fileInput("packaging-proof");
        const paid = order.summary.payment_status === "Fully Paid";
        const next = paid ? (order.fulfillment?.release_status || "Ready for Pickup") : "Awaiting Full Payment";
        const form = actionForm("Complete packaging", paid ? `The order is fully paid: it moves to ${next} and the customer is notified.` : `A balance of ${money(order.summary.balance)} is due: the order waits in Awaiting Full Payment and the customer is asked to pay it before release.`,
            h("div", { className: "form-grid" }, formField("Packed quantity", quantity), formField("Remarks", remarks, true), formField("Packaging proof (optional photos)", proof, true)),
            h("div", { className: "dialog-actions" }, h("button", { className: "button button-primary", type: "submit", text: "Packaging completed" })));
        return bindSubmit(form, ctx, order, "packaging", async () => ({ completed_quantity: intValue(quantity), remarks: remarks.value.trim(), proof: await readFiles(proof) }), {
            confirm: { title: "Packaging completed?", message: `The order moves to ${next}.`, confirmLabel: "Confirm" }, success: "Packaging completed.", reload,
        });
    }

    function dispatchForm(order, ctx, reload) {
        const courier = h("select", {}, ...(order.couriers || []).map((item) => new Option(item.name, item.id)));
        courier.value = order.default_courier || courier.value;
        const service = textInput({ maxlength: 80, placeholder: "e.g. Standard / Express", value: (order.couriers || []).find((item) => item.id === courier.value)?.service || "" });
        const trackingNumber = textInput({ maxlength: 60, placeholder: "Courier tracking number" });
        const personnel = textInput({ maxlength: 120, placeholder: "For the shop's own rider (instead of a tracking number)" });
        const shipment = h("input", { type: "date", value: UI.todayLocal() });
        const scheduled = h("input", { type: "date", value: order.fulfillment?.scheduled_date || "" });
        const remarks = textArea({ placeholder: "Delivery remarks" });
        const f = order.fulfillment || {};
        const form = actionForm("Dispatch the order", `${f.area_label ? `${f.area_label} · ` : ""}${f.estimate_text || ""}. No live courier tracking is connected: enter the tracking number from the courier's waybill.`,
            h("div", { className: "form-grid" },
                formField("Courier", courier), formField("Service", service),
                formField("Tracking number", trackingNumber), formField("Delivery personnel", personnel),
                formField("Shipment date", shipment), formField("Scheduled delivery", scheduled),
                formField("Remarks", remarks, true)),
            h("div", { className: "dialog-actions" }, h("button", { className: "button button-primary", type: "submit", text: "Mark out for delivery" })));
        return bindSubmit(form, ctx, order, "dispatch", async () => ({
            courier_id: courier.value, service: service.value.trim(), tracking_number: trackingNumber.value.trim(), delivery_personnel: personnel.value.trim(),
            shipment_date: shipment.value, scheduled_date: scheduled.value, remarks: remarks.value.trim(),
        }), { confirm: { title: "Dispatch this order?", message: "The customer is notified with the tracking details.", confirmLabel: "Dispatch" }, success: "Order dispatched.", reload });
    }

    function deliveredForm(order, ctx, reload) {
        const receivedBy = textInput({ maxlength: 120, placeholder: "Name of the person who received it" });
        const remarks = textArea({ placeholder: "Remarks" });
        const proof = fileInput("delivery-proof", { required: true });
        const deliveredForm = actionForm("Confirm delivery", "Attach the proof of delivery (courier POD screenshot or photo). An estimated date never marks an order delivered.",
            h("div", { className: "form-grid" }, formField("Received by", receivedBy), formField("Proof of delivery", proof, true), formField("Remarks", remarks, true)),
            h("div", { className: "dialog-actions" }, h("button", { className: "button button-primary", type: "submit", text: "Confirm delivered" })));
        bindSubmit(deliveredForm, ctx, order, "delivery/delivered", async () => ({ received_by: receivedBy.value.trim(), remarks: remarks.value.trim(), proof: await readFiles(proof) }), { success: "Delivery confirmed.", reload });
        const reason = h("select", { required: true }, new Option("Choose a reason", ""), ...Object.entries(order.failure_reasons || {}).map(([value, text]) => new Option(text, value)));
        const failRemarks = textArea({ placeholder: "Details (required for Other)" });
        const failForm = actionForm("Delivery failed", "The order is not cancelled or refunded; you can reschedule it.",
            h("div", { className: "form-grid" }, formField("Reason", reason), formField("Remarks", failRemarks, true)),
            h("div", { className: "dialog-actions" }, h("button", { className: "button button-danger-strong", type: "submit", text: "Record failed delivery" })));
        bindSubmit(failForm, ctx, order, "delivery/failed", async () => ({ reason: reason.value, remarks: failRemarks.value.trim() }), { success: "Failed delivery recorded.", reload });
        return [deliveredForm, h("details", { className: "op-details" }, h("summary", { text: "Delivery failed?" }), failForm)];
    }

    function rescheduleForm(order, ctx, reload) {
        const scheduled = h("input", { type: "date", required: true });
        const remarks = textArea({ placeholder: "What was arranged with the customer" });
        const form = actionForm("Reschedule delivery", "The order returns to Ready for Dispatch; the next dispatch is a new attempt.",
            h("div", { className: "form-grid" }, formField("New delivery date", scheduled), formField("Remarks", remarks, true)),
            h("div", { className: "dialog-actions" }, h("button", { className: "button button-primary", type: "submit", text: "Reschedule" })));
        return bindSubmit(form, ctx, order, "delivery/reschedule", async () => ({ scheduled_date: scheduled.value, remarks: remarks.value.trim() }), { success: "Delivery rescheduled.", reload });
    }

    function pickupScheduleForm(order, ctx, reload) {
        const schedule = order.fulfillment?.pickup_schedule || {};
        const pickupDate = h("input", { type: "date", required: true, value: schedule.date || order.fulfillment?.preferred_pickup_date || "" });
        const time = textInput({ maxlength: 60, placeholder: "e.g. 1:00–5:00 PM", value: schedule.time || "" });
        const remarks = textInput({ maxlength: 500, value: schedule.remarks || "" });
        const form = actionForm("Pickup schedule", order.fulfillment?.preferred_pickup_date ? `Customer's preferred date: ${date(order.fulfillment.preferred_pickup_date)}.` : "",
            h("div", { className: "form-grid" }, formField("Pickup date", pickupDate), formField("Time", time), formField("Remarks", remarks, true)),
            h("div", { className: "dialog-actions" }, h("button", { className: "button button-small", type: "submit", text: "Save schedule" })));
        bindSubmit(form, ctx, order, "pickup/schedule", async () => ({ pickup_date: pickupDate.value, pickup_time: time.value.trim(), remarks: remarks.value.trim() }), { success: "Pickup schedule saved.", reload });
        return h("details", { className: "op-details" }, h("summary", { text: "Schedule pickup" }), form);
    }

    function pickupConfirmForm(order, ctx, reload) {
        const name = textInput({ required: true, maxlength: 120, placeholder: "Full name of the claimant" });
        const type = h("select", {}, new Option("The customer", "customer"), new Option("Authorized representative", "authorized"));
        const authorization = textInput({ maxlength: 300, placeholder: "e.g. Authorization letter + valid ID (required for a representative)" });
        const code = textInput({ required: true, maxlength: 40, placeholder: "Code shown by the claimant", autocomplete: "off" });
        const acknowledged = h("input", { type: "checkbox", required: true });
        const remarks = textInput({ maxlength: 500 });
        const form = actionForm("Release to claimant", "Ask the claimant for the pickup code from the customer's portal or notification. Never release an order without it.",
            h("div", { className: "form-grid" }, formField("Claimant name", name), formField("Claimant", type), formField("Authorization details", authorization, true), formField("Pickup code", code), formField("Remarks", remarks)),
            h("label", { className: "check-row" }, acknowledged, " The claimant received the order and acknowledged its condition"),
            h("div", { className: "dialog-actions" }, h("button", { className: "button button-primary", type: "submit", text: "Confirm order claimed" })));
        return bindSubmit(form, ctx, order, "pickup/confirm", async () => ({
            claimant_name: name.value.trim(), claimant_type: type.value, authorization_details: authorization.value.trim(),
            verification_code: code.value.trim(), acknowledged: acknowledged.checked, remarks: remarks.value.trim(),
        }), { success: "Order claimed.", reload });
    }

    /** Quantity, restart stage, and charge of a reprint (shared by the reprint form and refund resolution). */
    function reprintOptions(order, { requireReason = true } = {}) {
        const ordered = order.customization?.quantity || 1;
        const reason = textArea({ required: requireReason, placeholder: "Why the order is reprinted (e.g. 5 pcs damaged in transit)" });
        const quantity = h("input", { type: "number", min: "1", max: String(ordered), step: "1", value: ordered, required: true });
        const stage = h("select", {}, new Option("In Production (same approved artwork)", "In Production"), new Option("Draft Preparation (artwork must change)", "Draft Preparation"));
        const chargeable = h("input", { type: "checkbox" });
        const charge = h("input", { type: "number", min: "0.01", step: "0.01", placeholder: "Agreed charge (PHP)", disabled: true });
        const agreed = h("input", { type: "checkbox", disabled: true });
        chargeable.addEventListener("change", () => { charge.disabled = !chargeable.checked; agreed.disabled = !chargeable.checked; charge.required = chargeable.checked; });
        const element = h("div", {},
            h("div", { className: "form-grid" },
                requireReason ? formField("Reason for reprinting", reason, true) : null,
                formField(`Pieces to reprint (of ${number(ordered)})`, quantity),
                formField("Restart at", stage),
                formField("Reprint charge", charge)),
            h("label", { className: "check-row" }, chargeable, " Chargeable reprint (otherwise free: no new payment is required)"),
            h("label", { className: "check-row" }, agreed, " The customer agreed to the charge"));
        return {
            element,
            values: () => ({
                ...(requireReason ? { reason: reason.value.trim() } : {}),
                quantity: Number.parseInt(quantity.value, 10),
                restart_stage: stage.value,
                chargeable: chargeable.checked,
                ...(chargeable.checked ? { charge_amount: Number(charge.value), customer_agreed: agreed.checked } : {}),
            }),
        };
    }

    function reprintForm(order, ctx, reload) {
        const options = reprintOptions(order);
        const authorized = h("input", { type: "checkbox", required: true });
        const remarks = textArea({ placeholder: "Remarks (optional)" });
        const form = actionForm("Start a reprint", "Keeps the original order, payments, and delivery/pickup history. The reprint goes through quality checking and packaging again. Only one reprint can be in progress.",
            options.element,
            formField("Remarks", remarks, true),
            h("label", { className: "check-row" }, authorized, " I authorize this reprint"),
            h("div", { className: "dialog-actions" }, h("button", { className: "button button-primary", type: "submit", text: "Start reprint" })));
        bindSubmit(form, ctx, order, "reprint", async () => {
            const values = options.values();
            if (values.chargeable && !(values.charge_amount > 0 && values.customer_agreed)) throw new Error("A chargeable reprint needs the agreed charge and the customer's agreement.");
            return { ...values, remarks: remarks.value.trim(), authorized: authorized.checked };
        }, {
            confirm: (body) => ({ title: "Start the reprint?", message: `Production restarts at ${body.restart_stage}. The customer is notified.`, confirmLabel: "Start reprint", details: [["Pieces", String(body.quantity)], ["Charge", body.chargeable ? money(body.charge_amount) : "Free (no new payment)"]] }),
            success: "Reprint started.",
            reload,
        });
        return h("details", { className: "op-details" }, h("summary", { text: "Reprint this order" }), form);
    }

    /** Action forms added to the order dialog's "Available actions" card. */
    function orderActions(order, reload, ctx) {
        const a = order.actions || {};
        const items = [];
        if (a.cancellation_pending) items.push(h("p", { className: "notice-card notice-warning", text: "A cancellation request is waiting for review (below). Production, payment recording, and dispatch are on hold until it is approved or rejected." }));
        if (a.update_production) items.push(productionForm(order, ctx, reload));
        if (a.quality_check) items.push(qualityForm(order, ctx, reload));
        if (a.complete_packaging) items.push(packagingForm(order, ctx, reload));
        if (a.awaiting_full_payment) items.push(h("p", { className: "notice-card", text: `Packed. Waiting for the remaining balance of ${money(order.summary.balance)}; the order is released automatically when the payment is verified.` }));
        if (a.schedule_pickup) items.push(pickupScheduleForm(order, ctx, reload));
        if (a.confirm_pickup) items.push(pickupConfirmForm(order, ctx, reload));
        if (a.dispatch) items.push(dispatchForm(order, ctx, reload));
        if (a.confirm_delivery) items.push(...deliveredForm(order, ctx, reload));
        if (a.reschedule_delivery) items.push(rescheduleForm(order, ctx, reload));
        if (a.start_reprint) items.push(reprintForm(order, ctx, reload));
        return items;
    }

    // ================================================================ list pages

    function tabs(entries, active, onSelect) {
        return h("div", { className: "segmented", role: "tablist" }, ...entries.map(([key, label, count]) => h("button", {
            className: `segmented-button${key === active ? " active" : ""}`, type: "button", role: "tab", "aria-selected": String(key === active),
            onclick: () => onSelect(key),
        }, label, count !== undefined ? h("span", { className: "segmented-count", text: number(count) }) : null)));
    }

    const state = {
        production: { tab: "printing", search: "" },
        fulfillment: { tab: "overview", search: "" },
        refunds: { tab: "cancellations", search: "", status: "" },
    };

    function orderCell(row) {
        return h("span", { className: "cell-pair" }, h("span", { className: "table-primary", text: row.reference }), h("span", { className: "table-secondary", text: row.customer_name }));
    }

    function renderProduction(body, ctx) {
        const s = state.production;
        const tabHost = h("div");
        const tableHost = h("div");
        const labels = { drafts: "Drafts & approval", printing: "Printing", quality: "Quality checking", packaging: "Packaging" };
        let counts = {};
        const drawTabs = () => tabHost.replaceChildren(tabs(Object.entries(labels).map(([key, label]) => [key, label, counts[key]]), s.tab, (key) => { s.tab = key; drawTabs(); table.setQuery({ ...s }); }));
        const table = new UI.DataTable(tableHost, {
            columns: [
                { label: "Order", className: "ref-cell", render: orderCell },
                { label: "Product", render: (row) => ctx.cellPair(row.product, `${number(row.quantity)} pcs · ${row.fulfillment_label}`) },
                { label: "Status", render: (row) => h("span", { className: "cell-pair" }, badge(row.status), row.cancellation_pending ? h("span", { className: "table-chip", text: "⚑ Cancellation requested" }) : null) },
                { label: "Progress", render: (row) => progressBar(row.progress) },
                { label: "Est. completion", render: (row) => (row.estimated_completion ? date(row.estimated_completion) : "—") },
                { label: "Quality", render: (row) => (row.quality_passed === true ? badge("Passed") : row.quality_passed === false ? badge("Failed") : "—") },
                { label: "Payment", render: (row) => h("span", { className: "cell-pair" }, badge(row.payment_status), h("span", { className: "table-secondary", text: row.balance ? `Balance ${money(row.balance)}` : "No balance" })) },
            ],
            initialQuery: { ...s },
            fetch: (params) => api(`/api/admin/production${query(params)}`),
            onLoad: (result) => { counts = result.counts || {}; drawTabs(); },
            emptyMessage: "No orders at this stage.",
            onRowClick: (row) => ctx.openOrder(row.reference, { focus: s.tab === "drafts" ? "drafts" : "production" }),
            rowTitle: (row) => `Open order ${row.reference}`,
        });
        drawTabs();
        body.replaceChildren(
            tabHost,
            h("div", { className: "filter-bar" }, ctx.searchInput("Search order, customer, product…", s.search, (value) => { s.search = value; table.setQuery({ ...s }); })),
            ctx.panel("Orders in production", "Select an order to update progress, record the quality check, or complete packaging.", tableHost),
        );
        table.load();
        ctx.setRefresh(() => table.reload());
    }

    const FULFILLMENT_CARDS = [
        ["In Production", "⚒"], ["Packaging", "▦"], ["Awaiting Full Payment", "₱"], ["Ready for Pickup", "⌂"],
        ["Ready for Dispatch", "⛟"], ["Out for Delivery", "➜"], ["Delivery Failed", "⚠"], ["Completed", "✓"],
    ];

    function renderFulfillment(body, ctx) {
        const s = state.fulfillment;
        const tabHost = h("div");
        const cards = h("section", { className: "stats-grid stats-grid-4 op-stats", "aria-label": "Fulfillment stages" });
        const tableHost = h("div");
        const labels = { overview: "Overview", pickup: "Pickup Orders", delivery: "Delivery Orders", issues: "Delivery Issues", history: "Fulfillment History" };
        let summary = null;
        const drawTabs = () => tabHost.replaceChildren(tabs(Object.entries(labels).map(([key, label]) => [key, label, key === "issues" ? summary?.issues : undefined]), s.tab, (key) => { s.tab = key; renderFulfillment(body, ctx); }));
        const columns = {
            overview: [
                { label: "Order", className: "ref-cell", render: orderCell },
                { label: "Product", render: (row) => ctx.cellPair(row.product, `${number(row.quantity)} pcs`) },
                { label: "Method", render: (row) => row.fulfillment_label },
                { label: "Status", render: (row) => badge(row.status) },
                { label: "Payment", render: (row) => badge(row.payment_status) },
                { label: "Courier / pickup", render: (row) => (row.fulfillment_method === "delivery" ? ctx.cellPair(row.courier || "—", row.tracking_number || "") : ctx.cellPair(row.pickup_location || "Shop", row.pickup_schedule ? date(row.pickup_schedule.date) : "")) },
            ],
            pickup: [
                { label: "Order", className: "ref-cell", render: orderCell },
                { label: "Status", render: (row) => badge(row.status) },
                { label: "Pickup location", render: (row) => row.pickup_location || "The shop" },
                { label: "Schedule", render: (row) => (row.pickup_schedule ? `${date(row.pickup_schedule.date)} ${row.pickup_schedule.time || ""}` : row.preferred_pickup_date ? `Preferred ${date(row.preferred_pickup_date)}` : "—") },
                { label: "Payment", render: (row) => h("span", { className: "cell-pair" }, badge(row.payment_status), row.balance ? h("span", { className: "table-secondary", text: `Balance ${money(row.balance)}` }) : null) },
                { label: "Claimed by", render: (row) => row.claimed_by || "—" },
            ],
            delivery: [
                { label: "Order", className: "ref-cell", render: orderCell },
                { label: "Recipient / address", render: (row) => ctx.cellPair(row.recipient, row.address) },
                { label: "Status", render: (row) => badge(row.status) },
                { label: "Courier / tracking", render: (row) => ctx.cellPair(row.courier || "—", row.tracking_number || (row.scheduled_date ? `Scheduled ${date(row.scheduled_date)}` : "")) },
                { label: "Area / fee", render: (row) => ctx.cellPair(row.area_label || "—", money(row.delivery_fee)) },
                { label: "Attempts", align: "right", render: (row) => number(row.attempts) },
            ],
            issues: [
                { label: "Order", className: "ref-cell", render: orderCell },
                { label: "Status", render: (row) => badge(row.status) },
                { label: "Last failure", render: (row) => row.last_failure || "—" },
                { label: "Open customer issues", align: "right", render: (row) => number(row.open_issues) },
                { label: "Courier / tracking", render: (row) => ctx.cellPair(row.courier || "—", row.tracking_number || "") },
            ],
            history: [
                { label: "When", render: (row) => dateTime(row.at) },
                { label: "Order", className: "ref-cell", render: orderCell },
                { label: "Event", render: (row) => ctx.cellPair(row.event, row.method) },
                { label: "Details", render: (row) => row.detail || "—" },
            ],
        };
        const table = new UI.DataTable(tableHost, {
            columns: columns[s.tab],
            initialQuery: { ...s },
            fetch: (params) => api(`/api/admin/fulfillment${query(params)}`),
            onLoad: (result) => {
                summary = result.summary;
                drawTabs();
                const c = summary.counts || {};
                cards.replaceChildren(...FULFILLMENT_CARDS.map(([status, icon]) => ctx.statCard({
                    label: status, value: number(c[status] || 0), icon, tone: status === "Delivery Failed" && c[status] ? "highlight" : "",
                    foot: { "Awaiting Full Payment": "Packed · balance due before release", "Delivery Failed": "Reschedule or contact the customer", "Ready for Pickup": "Fully paid · waiting for the claimant", "Ready for Dispatch": "Fully paid · hand to the courier" }[status] || "",
                    onClick: () => ctx.showOrders(status),
                })));
            },
            emptyMessage: { overview: "No orders in production or fulfillment.", pickup: "No pickup orders at these stages.", delivery: "No delivery orders at these stages.", issues: "No failed deliveries or open delivery issues.", history: "No fulfillment events yet." }[s.tab],
            onRowClick: (row) => ctx.openOrder(row.reference, { focus: "fulfillment" }),
            rowTitle: (row) => `Open order ${row.reference}`,
        });
        drawTabs();
        body.replaceChildren(
            cards,
            tabHost,
            h("div", { className: "filter-bar" }, ctx.searchInput("Search order, customer, tracking number…", s.search, (value) => { s.search = value; table.setQuery({ ...s }); })),
            ctx.panel(labels[s.tab], "Select an order to release it, dispatch it with the J&T Express (or other courier) tracking number, record delivery or failure, or verify the claimant.", tableHost),
        );
        table.load();
        ctx.setRefresh(() => table.reload());
    }

    const CANCEL_STATUSES = [["pending_review", "Pending Review"], ["approved", "Approved"], ["rejected", "Rejected"], ["all", "All"]];
    const REFUND_REQUEST_STATUSES = [["", "All open"], ["pending_review", "Pending Review"], ["approved", "Approved"], ["processing", "Processing"], ["failed", "Failed"]];
    const REFUND_HISTORY_STATUSES = [["", "All"], ["refunded", "Refunded"], ["rejected", "Rejected"], ["not_eligible", "Not Eligible"], ["reprint", "Resolved by Reprint"]];

    function renderRefunds(body, ctx) {
        const s = state.refunds;
        const tableHost = h("div");
        const totals = h("div", { className: "revenue-figures" });
        const tabHost = tabs([["cancellations", "Cancellation Requests"], ["requests", "Refund Requests"], ["history", "Refund History"]], s.tab, (key) => { s.tab = key; s.status = key === "cancellations" ? "pending_review" : ""; renderRefunds(body, ctx); });
        if (s.tab === "cancellations" && !s.status) s.status = "pending_review";
        const statusOptions = s.tab === "cancellations" ? CANCEL_STATUSES : s.tab === "requests" ? REFUND_REQUEST_STATUSES : REFUND_HISTORY_STATUSES;
        const statusSelect = ctx.selectInput(statusOptions, s.status, (value) => { s.status = value; table.setQuery(params()); });
        const params = () => (s.tab === "cancellations" ? { status: s.status, search: s.search } : { view: s.tab, status: s.status, search: s.search });
        const columns = s.tab === "cancellations" ? [
            { label: "Request", className: "ref-cell", render: (row) => ctx.cellPair(row.id, dateTime(row.requested_at)) },
            { label: "Order", render: orderCell },
            { label: "Reason", render: (row) => ctx.cellPair(row.reason_label, row.remarks || "") },
            { label: "Stage", render: (row) => h("span", { className: "cell-pair" }, badge(row.order_status), h("span", { className: "table-secondary", text: row.policy?.stage || "" })) },
            { label: "Verified paid", align: "right", render: (row) => money(row.verified_paid) },
            { label: "Decision", render: (row) => h("span", { className: "cell-pair" }, badge(row.status_label), row.approved_refund !== undefined ? h("span", { className: "table-secondary", text: `Refund ${money(row.approved_refund)}` }) : null) },
        ] : [
            { label: "Refund", className: "ref-cell", render: (row) => ctx.cellPair(row.id, dateTime(row.refunded_at || row.requested_at)) },
            { label: "Order", render: orderCell },
            { label: "Reason", render: (row) => ctx.cellPair(row.reason_label, row.details || "") },
            { label: "Amount", align: "right", render: (row) => (row.amount !== null && row.amount !== undefined ? money(row.amount) : row.requested_amount ? `${money(row.requested_amount)} requested` : "To review") },
            { label: "Paid / max", align: "right", render: (row) => ctx.cellPair(money(row.verified_paid), `max ${money(row.max_for_this)}`) },
            { label: "Status", render: (row) => h("span", { className: "cell-pair" }, badge(row.status_label), row.transaction_reference ? h("span", { className: "table-secondary", text: `${row.method} · ${row.transaction_reference}` }) : null) },
        ];
        const table = new UI.DataTable(tableHost, {
            columns,
            initialQuery: params(),
            fetch: (values) => api(`${s.tab === "cancellations" ? "/api/admin/cancellations" : "/api/admin/refunds"}${query(values)}`),
            onLoad: (result) => {
                if (result.totals) {
                    totals.replaceChildren(
                        h("div", { className: "mini-stat" }, h("span", { text: "Completed refunds (all time)" }), h("strong", { text: money(result.totals.refunded_total) })),
                        h("div", { className: "mini-stat" }, h("span", { text: "Approved / in progress" }), h("strong", { text: money(result.totals.reserved_total) })),
                        h("div", { className: "mini-stat" }, h("span", { text: "Pending review" }), h("strong", { text: number(result.counts?.pending_review || 0) })),
                        h("div", { className: "mini-stat" }, h("span", { text: "Failed transfers" }), h("strong", { text: number(result.counts?.failed || 0) })),
                    );
                } else {
                    totals.replaceChildren(...CANCEL_STATUSES.slice(0, 3).map(([key, label]) => h("div", { className: "mini-stat" }, h("span", { text: label }), h("strong", { text: number(result.counts?.[key] || 0) }))));
                }
            },
            emptyMessage: s.tab === "cancellations" ? "No cancellation requests with this status." : "No refunds here.",
            onRowClick: (row) => ctx.openOrder(row.reference, { focus: s.tab === "cancellations" ? "cancellation" : "refunds" }),
            rowTitle: (row) => `Open order ${row.reference}`,
        });
        body.replaceChildren(
            tabHost,
            totals,
            h("div", { className: "filter-bar" }, ctx.searchInput("Search order, customer, request or refund ID…", s.search, (value) => { s.search = value; table.setQuery(params()); }), ctx.field("Status", statusSelect)),
            ctx.panel({ cancellations: "Cancellation requests", requests: "Refund requests", history: "Refund history" }[s.tab],
                s.tab === "cancellations" ? "Approve or reject in the order details. Payments waiting for verification must be resolved first." : "Approve, process, and complete refunds in the order details. Only completed refunds reduce net collections.",
                tableHost),
        );
        table.load();
        ctx.setRefresh(() => table.reload());
    }

    function render(view, body, ctx) {
        ({ production: renderProduction, fulfillment: renderFulfillment, refunds: renderRefunds })[view]?.(body, ctx);
    }

    // ================================================================ settings

    function paymentMethodsPanel(ctx) {
        const host = h("div", { className: "op-settings" }, ctx.loadingBlock("Loading payment methods…"));
        const load = async () => {
            let data;
            try {
                data = await api("/api/admin/settings/payment-methods");
            } catch (error) {
                host.replaceChildren(ctx.errorBlock(error.message, load));
                return;
            }
            host.replaceChildren(
                h("p", { className: "field-note", text: "Customers see only active methods, with these account details and QR codes. Payments stay Pending Verification until you check the money in the shop's account; no payment is confirmed automatically." }),
                ...data.methods.map((method) => methodEditor(method, load)),
                h("details", { className: "op-details" }, h("summary", { text: "Add another e-wallet" }), methodEditor(null, load)),
            );
        };
        load();
        return ctx.panel("Payment methods", "GCash, Maya, and other e-wallets the shop accepts", host);
    }

    function methodEditor(method, reload) {
        const isNew = !method;
        const provider = textInput({ required: true, maxlength: 60, value: method?.provider || "", placeholder: "e.g. ShopeePay" });
        const accountName = textInput({ maxlength: 120, value: method?.account_name || "", placeholder: "Account / merchant name" });
        const accountNumber = textInput({ maxlength: 60, value: method?.account_number || "", placeholder: "e.g. 0917 123 4567" });
        const instructions = textArea({ value: method?.instructions || "", placeholder: "Payment instructions shown to customers" });
        instructions.value = method?.instructions || "";
        const order = h("input", { type: "number", min: "0", max: "999", step: "1", value: method?.display_order ?? 10 });
        const active = h("input", { type: "checkbox", checked: method ? method.active : true });
        const qr = fileInput(`qr-${method?.id || "new"}`, { multiple: false, accept: IMAGE_ACCEPT });
        const removeQr = method?.qr_url ? h("input", { type: "checkbox" }) : null;
        const save = h("button", { className: "button button-primary button-small", type: "submit", text: isNew ? "Add payment method" : "Save" });
        const remove = isNew ? null : h("button", { className: "button button-small button-danger-strong", type: "button", text: "Delete" });
        const form = h("form", { className: "op-method" },
            h("div", { className: "op-method-head" },
                method?.qr_url ? h("img", { className: "op-qr", src: method.qr_url, alt: `${method.provider} QR code` }) : h("span", { className: "op-qr op-qr-empty", text: "No QR" }),
                h("div", {}, h("strong", { text: method?.provider || "New e-wallet" }), method ? badge(method.active ? "Active" : "Inactive") : null,
                    method && !method.configured ? h("p", { className: "field-note", text: "Account number or QR code not set yet." }) : null)),
            h("div", { className: "form-grid" },
                formField("Provider", provider), formField("Account / merchant name", accountName), formField("Account number", accountNumber),
                formField("Display order", order), formField("Instructions", instructions, true), formField("QR code image (PNG, JPG, WebP)", qr, true)),
            h("label", { className: "check-row" }, active, " Active (shown to customers)"),
            removeQr ? h("label", { className: "check-row" }, removeQr, " Remove the current QR code") : null,
            h("div", { className: "dialog-actions" }, remove, save));
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            await UI.busy(save, async () => {
                try {
                    const body = {
                        provider: provider.value.trim(), account_name: accountName.value.trim(), account_number: accountNumber.value.trim(),
                        instructions: instructions.value.trim(), display_order: Number.parseInt(order.value || "0", 10), active: active.checked,
                    };
                    const [file] = await readFiles(qr, 1);
                    if (file) body.qr = file;
                    if (removeQr?.checked) body.remove_qr = true;
                    await api(isNew ? "/api/admin/settings/payment-methods" : `/api/admin/settings/payment-methods/${method.id}`, { method: isNew ? "POST" : "PATCH", body });
                    toast(isNew ? "Payment method added." : "Payment method saved.", "success");
                    reload();
                } catch (error) {
                    toast(error.message, "error");
                }
            });
        });
        remove?.addEventListener("click", async () => {
            const answer = await UI.confirm({ title: `Delete ${method.provider}?`, message: "Recorded payments keep their method name. Deactivating hides it without deleting.", confirmLabel: "Delete", tone: "danger" });
            if (!answer) return;
            try {
                await api(`/api/admin/settings/payment-methods/${method.id}`, { method: "DELETE" });
                toast("Payment method deleted.", "success");
                reload();
            } catch (error) {
                toast(error.message, "error");
            }
        });
        return form;
    }

    function fulfillmentSettingsPanel(ctx) {
        const host = h("div", { className: "op-settings" }, ctx.loadingBlock("Loading delivery settings…"));
        const load = async () => {
            let settings;
            try {
                settings = await api("/api/admin/settings/fulfillment");
            } catch (error) {
                host.replaceChildren(ctx.errorBlock(error.message, load));
                return;
            }
            const enabled = h("input", { type: "checkbox", checked: settings.delivery_enabled });
            const areaRows = h("div", { className: "op-rows" });
            const addArea = (area = { id: "", label: "", region: "luzon", provinces: [], fee: 0, estimate_min_days: null, estimate_max_days: null, active: true }) => {
                const label = textInput({ required: true, maxlength: 80, value: area.label });
                const region = h("select", {}, new Option("Luzon", "luzon"), new Option("Visayas", "visayas"), new Option("Mindanao", "mindanao"));
                region.value = area.region;
                const provinces = textInput({ maxlength: 2000, value: (area.provinces || []).join(", "), placeholder: "Optional: provinces (comma-separated) for a specific area" });
                const fee = h("input", { type: "number", min: "0", step: "0.01", required: true, value: area.fee });
                const low = h("input", { type: "number", min: "0", max: "120", step: "1", value: area.estimate_min_days ?? "" });
                const high = h("input", { type: "number", min: "0", max: "120", step: "1", value: area.estimate_max_days ?? "" });
                const active = h("input", { type: "checkbox", checked: area.active });
                const row = h("div", { className: "op-row" },
                    h("div", { className: "form-grid" }, formField("Area name", label), formField("Island group", region), formField("Delivery fee (PHP)", fee),
                        formField("Estimate: min days", low), formField("Estimate: max days", high), formField("Provinces", provinces, true)),
                    h("div", { className: "op-row-actions" }, h("label", { className: "check-row" }, active, " Delivery available"), h("button", { className: "button button-small", type: "button", text: "Remove area", onclick: () => row.remove() })));
                row.read = () => ({
                    id: area.id, label: label.value.trim(), region: region.value, fee: Number(fee.value || 0),
                    provinces: provinces.value.split(",").map((item) => item.trim()).filter(Boolean),
                    estimate_min_days: low.value === "" ? null : Number.parseInt(low.value, 10), estimate_max_days: high.value === "" ? null : Number.parseInt(high.value, 10), active: active.checked,
                });
                areaRows.append(row);
            };
            settings.areas.forEach(addArea);
            const courierRows = h("div", { className: "op-rows" });
            const addCourier = (courier = { id: "", name: "", service: "", tracking_url: "", active: true }) => {
                const name = textInput({ required: true, maxlength: 80, value: courier.name });
                const service = textInput({ maxlength: 80, value: courier.service });
                const url = textInput({ maxlength: 300, value: courier.tracking_url, placeholder: "https://…{tracking}… (optional tracking page)" });
                const active = h("input", { type: "checkbox", checked: courier.active });
                const isDefault = h("input", { type: "radio", name: "default-courier", checked: courier.id && courier.id === settings.default_courier });
                const row = h("div", { className: "op-row" },
                    h("div", { className: "form-grid" }, formField("Courier", name), formField("Service", service), formField("Tracking page address", url, true)),
                    h("div", { className: "op-row-actions" }, h("label", { className: "check-row" }, active, " Active"), h("label", { className: "check-row" }, isDefault, " Default courier"), h("button", { className: "button button-small", type: "button", text: "Remove", onclick: () => row.remove() })));
                row.read = () => ({ id: courier.id, name: name.value.trim(), service: service.value.trim(), tracking_url: url.value.trim(), active: active.checked, isDefault: isDefault.checked });
                courierRows.append(row);
            };
            settings.couriers.forEach(addCourier);
            const locationRows = h("div", { className: "op-rows" });
            const addLocation = (location = { id: "", name: "", address: "", hours: "", instructions: "", active: true }) => {
                const name = textInput({ required: true, maxlength: 120, value: location.name });
                const address = textInput({ required: true, maxlength: 300, value: location.address });
                const hours = textInput({ maxlength: 160, value: location.hours, placeholder: "e.g. Mon–Sat 9 AM–6 PM" });
                const instructions = textInput({ maxlength: 500, value: location.instructions });
                const active = h("input", { type: "checkbox", checked: location.active });
                const row = h("div", { className: "op-row" },
                    h("div", { className: "form-grid" }, formField("Location name", name), formField("Hours", hours), formField("Address", address, true), formField("Instructions", instructions, true)),
                    h("div", { className: "op-row-actions" }, h("label", { className: "check-row" }, active, " Active"), h("button", { className: "button button-small", type: "button", text: "Remove", onclick: () => row.remove() })));
                row.read = () => ({ id: location.id, name: name.value.trim(), address: address.value.trim(), hours: hours.value.trim(), instructions: instructions.value.trim(), active: active.checked });
                locationRows.append(row);
            };
            settings.pickup_locations.forEach(addLocation);
            const pickupInstructions = textArea({ placeholder: "Shown to pickup customers (bring your pickup code, valid ID, …)" });
            pickupInstructions.value = settings.pickup_instructions || "";
            const deliveryInstructions = textArea({ placeholder: "Shown to delivery customers" });
            deliveryInstructions.value = settings.delivery_instructions || "";
            const disclaimer = textArea({ maxlength: 600 });
            disclaimer.value = settings.delivery_disclaimer || "";
            const save = h("button", { className: "button button-primary", type: "submit", text: "Save delivery & pickup settings" });
            const form = h("form", { className: "op-settings-form" },
                h("label", { className: "check-row" }, enabled, " Offer delivery at checkout"),
                h("h4", { text: "Delivery areas, fees, and estimated courier delivery periods" }),
                h("p", { className: "field-note", text: "The delivery fee is calculated on the server from the customer's province (Luzon, Visayas, or Mindanao, or a specific area listing the province). Estimates are not guaranteed arrival dates." }),
                areaRows, h("button", { className: "button button-small", type: "button", text: "+ Add delivery area", onclick: () => addArea() }),
                h("h4", { text: "Couriers" }),
                h("p", { className: "field-note", text: "J&T Express is the default courier. No live tracking API is connected: tracking numbers are entered when dispatching. A tracking page address with {tracking} shows customers a tracking link." }),
                courierRows, h("button", { className: "button button-small", type: "button", text: "+ Add courier", onclick: () => addCourier() }),
                h("h4", { text: "Pickup locations" }),
                locationRows, h("button", { className: "button button-small", type: "button", text: "+ Add pickup location", onclick: () => addLocation() }),
                h("div", { className: "form-grid" }, formField("Pickup instructions", pickupInstructions, true), formField("Delivery instructions", deliveryInstructions, true), formField("Delivery estimate disclaimer", disclaimer, true)),
                h("div", { className: "dialog-actions" }, save));
            form.addEventListener("submit", async (event) => {
                event.preventDefault();
                if (!form.reportValidity()) return;
                const couriers = [...courierRows.children].map((row) => row.read());
                const defaultCourier = couriers.find((item) => item.isDefault);
                const body = {
                    delivery_enabled: enabled.checked,
                    areas: [...areaRows.children].map((row) => row.read()),
                    couriers: couriers.map(({ isDefault, ...item }) => item),
                    default_courier: defaultCourier?.id || settings.default_courier,
                    pickup_locations: [...locationRows.children].map((row) => row.read()),
                    pickup_instructions: pickupInstructions.value.trim(),
                    delivery_instructions: deliveryInstructions.value.trim(),
                    delivery_disclaimer: disclaimer.value.trim(),
                };
                await UI.busy(save, async () => {
                    try {
                        await api("/api/admin/settings/fulfillment", { method: "POST", body });
                        toast("Delivery and pickup settings saved.", "success");
                        load();
                    } catch (error) {
                        toast(error.message, "error");
                    }
                });
            });
            host.replaceChildren(form);
        };
        load();
        return ctx.panel("Delivery, couriers & pickup", "Delivery areas and fees, estimated delivery periods, couriers, and pickup locations", host);
    }

    function settingsPanels(ctx) {
        return [paymentMethodsPanel(ctx), fulfillmentSettingsPanel(ctx)];
    }

    window.PrintShopOperationsAdmin = { render, orderSections, orderActions, settingsPanels };
})();
