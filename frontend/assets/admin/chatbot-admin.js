"use strict";

/**
 * Admin "AI Chatbot" page: activity overview, customer conversations, unanswered questions, inquiries
 * needing follow-up, FAQ management, and chatbot settings (on/off, welcome message, suggestions).
 *
 * Exposed as window.PrintShopChatbotAdmin.render(body, helpers); dashboard.js calls it with its shared
 * builders (panel, statCard, field, ...) so this page looks and behaves like the other admin views.
 * Every request goes to /api/admin/chatbot/* and requires the administrator session.
 */
(() => {
    const UI = window.AdminUI;
    const { h, api, query, toast, number, dateTime, badge } = UI;
    const TABS = [
        ["overview", "Overview"],
        ["conversations", "Conversations"],
        ["unanswered", "Unanswered"],
        ["inquiries", "Inquiries"],
        ["faqs", "FAQs"],
        ["settings", "Settings"],
    ];
    const KIND_OPTIONS = [["", "All conversations"], ["customer", "Signed-in customers"], ["guest", "Guests"], ["unanswered", "With unanswered questions"]];
    const state = { tab: "overview", conversations: { search: "", kind: "" }, inquiries: { status: "open" }, counts: {} };
    let helpers = null;
    let root = null;

    // ------------------------------------------------------------ helpers

    function when(value) {
        return value ? h("time", { datetime: value, title: dateTime(value), text: UI.relativeTime(value) }) : "—";
    }

    function excerpt(text, length = 110) {
        const value = String(text || "").replace(/\s+/g, " ").trim();
        return value.length > length ? `${value.slice(0, length - 1)}…` : value;
    }

    function visitor(row) {
        const name = row.customer_name || row.name;
        const email = row.customer_email || row.email;
        return h("span", { className: "cell-pair" },
            h("span", { className: "table-primary", text: name || "Guest visitor" }),
            h("span", { className: "table-secondary", text: email || (row.customer_id || row.is_customer ? "Customer account" : "Not signed in") }));
    }

    function paged(result, key) {
        return { items: result[key], total: result.total, page: result.page, pages: result.pages, page_size: result.page_size };
    }

    function chatMarkup(text) {
        // Same light formatting as the storefront chat, built from text nodes only.
        const box = h("div", { className: "cb-text" });
        String(text || "").split("\n").forEach((line) => {
            if (!line.trim()) return;
            const paragraph = h("p");
            line.split(/\*\*(.+?)\*\*/g).forEach((part, index) => {
                if (part) paragraph.append(index % 2 ? h("strong", { text: part }) : document.createTextNode(part));
            });
            box.append(paragraph);
        });
        return box;
    }

    // ------------------------------------------------------------ shell

    function render(body, shared) {
        helpers = shared;
        root = body;
        const requested = window.location.hash.replace("#", "");
        if (TABS.some(([value]) => value === requested)) state.tab = requested;
        draw();
        helpers.setRefresh(() => draw());
    }

    function tabs() {
        return h("div", { className: "segmented cb-tabs", role: "tablist", "aria-label": "Chatbot sections" }, ...TABS.map(([value, label]) => {
            const count = value === "unanswered" ? state.counts.unanswered : value === "inquiries" ? state.counts.inquiries : 0;
            return h("button", {
                className: `segmented-button${state.tab === value ? " active" : ""}`,
                type: "button", role: "tab", "aria-selected": String(state.tab === value),
                onclick: () => openTab(value),
            }, label, count ? h("span", { className: "segmented-count", text: number(count) }) : null);
        }));
    }

    function openTab(tab) {
        state.tab = tab;
        window.history.replaceState(window.history.state, "", `${window.location.pathname}${tab === "overview" ? "" : `#${tab}`}`);
        draw();
    }

    function draw() {
        const host = h("div", { className: "cb-host" }, helpers.loadingBlock());
        root.replaceChildren(tabs(), host);
        ({
            overview: renderOverview,
            conversations: renderConversations,
            unanswered: renderUnanswered,
            inquiries: renderInquiries,
            faqs: renderFaqs,
            settings: renderSettings,
        })[state.tab](host);
    }

    function updateCounts(counts) {
        const changed = counts.unanswered !== state.counts.unanswered || counts.inquiries !== state.counts.inquiries;
        state.counts = { ...state.counts, ...counts };
        if (changed && root) root.querySelector(".cb-tabs")?.replaceWith(tabs());
    }

    // ------------------------------------------------------------ overview

    async function renderOverview(host) {
        let data;
        try {
            data = await api("/api/admin/chatbot/overview");
        } catch (error) {
            host.replaceChildren(helpers.errorBlock(error.message, () => renderOverview(host)));
            return;
        }
        updateCounts({ unanswered: data.unanswered_open, inquiries: data.open_inquiries });
        const ai = data.settings.ai;
        const aiSources = data.sources.ai || { replies: 0 };
        const dbSources = data.sources.database || { replies: 0 };
        const cards = h("section", { className: "stats-grid stats-grid-6", "aria-label": "Chatbot activity" },
            helpers.statCard({ label: "Conversations", value: number(data.conversations), foot: `Last 30 days · ${number(data.total_conversations)} in total`, icon: "👥" }),
            helpers.statCard({ label: "Questions asked", value: number(data.questions), foot: `${number(data.signed_in_conversations)} chats by signed-in customers`, icon: "▤" }),
            helpers.statCard({ label: "Answered confidently", value: data.answer_rate === null ? "—" : `${Math.round(data.answer_rate * 100)}%`, foot: "Share of replies in the last 30 days", icon: "✓" }),
            helpers.statCard({ label: "Unanswered to review", value: number(data.unanswered_open), foot: "Open the list", icon: "⚑", onClick: () => openTab("unanswered") }),
            helpers.statCard({ label: "Inquiries to follow up", value: number(data.open_inquiries), foot: "Messages sent to the administrator", icon: "◷", onClick: () => openTab("inquiries") }),
            helpers.statCard({ label: "AI replies", value: number(aiSources.replies), foot: `${number(dbSources.replies)} answered from the database without AI`, icon: "⌕" }),
        );
        const notices = [];
        if (!data.settings.enabled) {
            notices.push(h("div", { className: "notice-card cb-notice" }, h("strong", { text: "The chatbot is turned off. " }), "Customers don't see the chat button. Turn it on under Settings."));
        }
        if (!ai.configured) {
            notices.push(h("div", { className: "notice-card cb-notice" },
                h("strong", { text: "Gemini AI is not configured. " }),
                "The chatbot answers from the database only (services, packages, prices, FAQs, and the customer's own orders). Set GEMINI_API_KEY in the server environment to enable AI answers."));
        } else if (ai.last_status && ai.last_status.ok === false) {
            notices.push(h("div", { className: "notice-card notice-error cb-notice" },
                h("strong", { text: "The last Gemini request failed. " }),
                `${ai.last_status.error || ""} (${dateTime(ai.last_status.at)}). Customers were answered from the database instead.`));
        }
        const chart = h("div", { className: "cb-chart" });
        const recentHost = h("div", {}, helpers.loadingBlock());
        host.replaceChildren(
            ...notices,
            cards,
            h("section", { className: "split-grid" },
                helpers.panel("Questions per day", "Customer messages in the last 30 days (Philippine time)", chart),
                helpers.panel("Latest conversations", "Select one to read the full conversation", recentHost,
                    h("button", { className: "button button-small", type: "button", text: "View all", onclick: () => openTab("conversations") }))),
        );
        UI.columnChart(chart, data.daily_questions.map((day) => ({
            label: new Date(`${day.date}T00:00:00`).toLocaleDateString([], { month: "short", day: "numeric" }),
            short: day.date.slice(8),
            value: day.count,
        })), { emptyMessage: "No questions in the last 30 days yet." });
        try {
            const recent = await api(`/api/admin/chatbot/conversations${query({ page_size: 6 })}`);
            recentHost.replaceChildren(recent.conversations.length
                ? h("div", { className: "cb-recent" }, ...recent.conversations.map((row) => h("button", {
                    className: "cb-recent-item", type: "button", onclick: () => openConversation(row.id),
                }, visitor(row), h("span", { className: "cb-recent-question", text: excerpt(row.first_question, 90) }),
                h("span", { className: "table-secondary" }, when(row.updated_at), ` · ${number(row.message_count)} messages`))))
                : h("p", { className: "empty-state", text: "No conversations yet." }));
        } catch (error) {
            recentHost.replaceChildren(helpers.errorBlock(error.message));
        }
    }

    // ------------------------------------------------------------ conversations

    function renderConversations(host) {
        const f = state.conversations;
        const tableHost = h("div");
        const table = new UI.DataTable(tableHost, {
            columns: [
                { label: "Visitor", render: visitor },
                { label: "First question", render: (row) => h("span", { className: "cb-question", text: excerpt(row.first_question) }) },
                { label: "Messages", align: "right", render: (row) => number(row.message_count) },
                { label: "Unanswered", render: (row) => row.unanswered_count ? badge(`${row.unanswered_count} unanswered`, "cb-badge-warn") : h("span", { className: "table-secondary", text: "—" }) },
                { label: "Last activity", render: (row) => when(row.updated_at) },
            ],
            initialQuery: { ...f },
            pageSize: 20,
            fetch: async (params) => paged(await api(`/api/admin/chatbot/conversations${query(params)}`), "conversations"),
            emptyMessage: "No conversations yet. They appear here when customers use the chat.",
            onRowClick: (row) => openConversation(row.id, () => table.reload()),
            rowTitle: () => "Read this conversation",
            caption: "Chatbot conversations",
        });
        host.replaceChildren(
            h("div", { className: "filter-bar" },
                helpers.searchInput("Search messages, customer name, or email…", f.search, (value) => { f.search = value; table.setQuery({ ...f }); }),
                helpers.field("Show", helpers.selectInput(KIND_OPTIONS, f.kind, (value) => { f.kind = value; table.setQuery({ ...f }); }))),
            helpers.panel("Conversations", "Most recent activity first", tableHost),
        );
        table.load();
    }

    async function openConversation(id, onChange = () => {}) {
        UI.openModal(h("div", {}, helpers.loadingBlock("Loading conversation…")), { wide: true });
        let data;
        try {
            data = await api(`/api/admin/chatbot/conversations/${encodeURIComponent(id)}`);
        } catch (error) {
            UI.openModal(helpers.errorBlock(error.message), { wide: true });
            return;
        }
        const conversation = data.conversation;
        const transcript = h("div", { className: "cb-transcript" }, ...data.messages.map((message) => {
            const assistant = message.role === "assistant";
            const tags = assistant ? [
                badge(message.source === "ai" ? "AI (Gemini)" : "Database answer", "cb-badge-source"),
                message.answered === false ? badge("Not answered", "cb-badge-warn") : null,
                message.needs_admin ? badge("Offered admin contact", "cb-badge-info") : null,
                message.reviewed_at ? badge("Reviewed", "cb-badge-ok") : null,
            ] : [];
            return h("div", { className: `cb-message ${assistant ? "cb-assistant" : "cb-user"}` },
                h("div", { className: "cb-message-head" },
                    h("strong", { text: assistant ? "ImPRESSion Assistant" : (conversation.customer_name || "Visitor") }),
                    h("span", { className: "table-secondary", text: dateTime(message.created_at) }), ...tags),
                chatMarkup(message.content),
                message.links && message.links.length
                    ? h("p", { className: "table-secondary", text: `Links shown: ${message.links.map((link) => `${link.label} (${link.url})`).join(", ")}` })
                    : null);
        }));
        const inquiries = data.inquiries.length ? helpers.panel("Inquiries from this conversation", "", h("div", { className: "cb-recent" },
            ...data.inquiries.map((item) => h("button", { className: "cb-recent-item", type: "button", onclick: () => openInquiry(item) },
                h("span", { className: "table-primary", text: excerpt(item.message, 90) }),
                h("span", { className: "table-secondary" }, badge(item.status === "open" ? "Open" : "Resolved"), " ", when(item.created_at)))))) : null;
        const remove = h("button", { className: "button button-danger-strong", type: "button", text: "Delete conversation" });
        remove.addEventListener("click", async () => {
            const confirmed = await UI.confirm({
                title: "Delete this conversation?",
                message: "The messages are removed permanently. Inquiries sent from it are kept.",
                confirmLabel: "Delete", tone: "danger",
            });
            if (!confirmed) return;
            try {
                await api(`/api/admin/chatbot/conversations/${encodeURIComponent(id)}`, { method: "DELETE" });
                UI.closeModal();
                toast("Conversation deleted.", "success");
                onChange();
            } catch (error) {
                toast(error.message, "error");
            }
        });
        UI.openModal(h("div", { className: "cb-conversation" },
            h("header", { className: "order-modal-header" }, h("div", {},
                h("p", { className: "eyebrow", text: "Chatbot conversation" }),
                h("h2", { className: "dialog-heading", text: conversation.customer_name || "Guest visitor" }),
                h("p", { className: "dialog-subtitle", text: [
                    conversation.customer_email || "Not signed in",
                    `Started ${dateTime(conversation.started_at)}`,
                    conversation.page ? `on ${conversation.page}` : "",
                    `${number(conversation.message_count)} messages`,
                ].filter(Boolean).join(" · ") }))),
            transcript,
            inquiries,
            h("div", { className: "dialog-actions" }, remove)), { wide: true });
    }

    // ------------------------------------------------------------ unanswered

    function renderUnanswered(host) {
        const tableHost = h("div");
        const table = new UI.DataTable(tableHost, {
            columns: [
                { label: "Customer question", render: (row) => h("span", { className: "cell-pair" },
                    h("span", { className: "table-primary cb-question", text: excerpt(row.question, 160) }),
                    h("span", { className: "table-secondary", text: `Reply: ${excerpt(row.reply, 120)}` })) },
                { label: "Visitor", render: visitor },
                { label: "Asked", render: (row) => when(row.created_at) },
                { label: "", render: (row) => h("span", { className: "cb-row-actions" },
                    h("button", { className: "table-link", type: "button", text: "Add FAQ", onclick: () => editFaq({ question: row.question, answer: "" }, () => {}) }),
                    h("button", { className: "table-link", type: "button", text: "Mark reviewed", onclick: async () => {
                        try {
                            await api(`/api/admin/chatbot/unanswered/${encodeURIComponent(row.id)}/review`, { method: "POST" });
                            toast("Marked as reviewed.", "success");
                            updateCounts({ unanswered: Math.max(0, (state.counts.unanswered || 1) - 1) });
                            helpers.refreshBadges();
                            table.reload();
                        } catch (error) {
                            toast(error.message, "error");
                        }
                    } })) },
            ],
            pageSize: 20,
            fetch: async (params) => {
                const result = await api(`/api/admin/chatbot/unanswered${query(params)}`);
                updateCounts({ unanswered: result.total });
                return paged(result, "items");
            },
            emptyMessage: "No unanswered questions. Every customer question was answered.",
            onRowClick: (row) => openConversation(row.conversation_id, () => table.reload()),
            rowTitle: () => "Read the conversation",
            caption: "Unanswered questions",
        });
        host.replaceChildren(
            h("div", { className: "notice-card cb-notice" },
                "Questions the assistant could not answer confidently from the shop's data. Add an FAQ (or update the catalog) so it can answer next time, then mark the question reviewed."),
            helpers.panel("Unanswered questions", "Newest first · select a row to read the conversation", tableHost),
        );
        table.load();
    }

    // ------------------------------------------------------------ inquiries

    function renderInquiries(host) {
        const f = state.inquiries;
        const tableHost = h("div");
        const statusTabs = h("div", { className: "segmented", role: "tablist", "aria-label": "Inquiry status" }, ...[["open", "Needs follow-up"], ["resolved", "Resolved"], ["", "All"]].map(([value, label]) => h("button", {
            className: `segmented-button${f.status === value ? " active" : ""}`, type: "button", role: "tab", "aria-selected": String(f.status === value),
            onclick: () => { f.status = value; renderInquiries(host); },
        }, label)));
        const table = new UI.DataTable(tableHost, {
            columns: [
                { label: "From", render: (row) => h("span", { className: "cell-pair" },
                    h("span", { className: "table-primary", text: row.name }),
                    h("span", { className: "table-secondary", text: [row.email, row.phone].filter(Boolean).join(" · ") })) },
                { label: "Message", render: (row) => h("span", { className: "cb-question", text: excerpt(row.message, 140) }) },
                { label: "Account", render: (row) => row.customer_id ? badge("Customer", "cb-badge-info") : h("span", { className: "table-secondary", text: "Guest" }) },
                { label: "Status", render: (row) => badge(row.status === "open" ? "Needs follow-up" : "Resolved", row.status === "open" ? "cb-badge-warn" : "cb-badge-ok") },
                { label: "Received", render: (row) => when(row.created_at) },
            ],
            initialQuery: { status: f.status },
            pageSize: 20,
            fetch: async (params) => {
                const result = await api(`/api/admin/chatbot/inquiries${query(params)}`);
                updateCounts({ inquiries: result.open });
                return paged(result, "inquiries");
            },
            emptyMessage: f.status === "open" ? "No inquiries waiting for follow-up." : "No inquiries yet.",
            onRowClick: (row) => openInquiry(row, () => { table.reload(); helpers.refreshBadges(); }),
            rowTitle: () => "Open this inquiry",
            caption: "Customer inquiries",
        });
        host.replaceChildren(h("div", { className: "notifications-toolbar" }, statusTabs), helpers.panel("Customer inquiries", "Messages customers sent to the administrator from the chat window", tableHost));
        table.load();
    }

    function openInquiry(item, onChange = () => {}) {
        const note = h("textarea", { rows: 3, maxlength: 1000, placeholder: "Optional note, e.g. how you followed up" });
        note.value = item.admin_note || "";
        const resolved = item.status === "resolved";
        const action = h("button", { className: "button button-primary", type: "button", text: resolved ? "Reopen" : "Mark as resolved" });
        action.addEventListener("click", () => UI.busy(action, async () => {
            try {
                await api(`/api/admin/chatbot/inquiries/${encodeURIComponent(item.id)}/status`, {
                    method: "POST", body: { status: resolved ? "open" : "resolved", note: note.value },
                });
                UI.closeModal();
                toast(resolved ? "Inquiry reopened." : "Inquiry marked as resolved.", "success");
                onChange();
                if (state.tab === "inquiries") draw();
            } catch (error) {
                toast(error.message, "error");
            }
        }));
        const subject = encodeURIComponent("Your inquiry to ImPRESSion PrintShop");
        UI.openModal(h("div", {},
            h("header", { className: "order-modal-header" }, h("div", {},
                h("p", { className: "eyebrow", text: "Chatbot inquiry" }),
                h("h2", { className: "dialog-heading", text: item.name }),
                h("p", { className: "dialog-subtitle", text: `Received ${dateTime(item.created_at)}` }))),
            helpers.detailList([
                ["Email", h("a", { href: `mailto:${item.email}?subject=${subject}`, text: item.email })],
                ["Phone", item.phone || "—"],
                ["Account", item.customer_id ? "Signed-in customer" : "Guest (not signed in)"],
                ["Status", resolved ? `Resolved ${dateTime(item.resolved_at)}${item.resolved_by ? ` by ${item.resolved_by}` : ""}` : "Needs follow-up"],
            ]),
            h("div", { className: "cb-inquiry-message" }, chatMarkup(item.message)),
            h("div", { className: "form-field full" }, h("label", { text: "Administrator note" }), note),
            h("div", { className: "dialog-actions" },
                item.conversation_id ? h("button", { className: "button", type: "button", text: "View conversation", onclick: () => openConversation(item.conversation_id) }) : null,
                action)));
    }

    // ------------------------------------------------------------ FAQs

    function renderFaqs(host) {
        const list = h("div", { className: "cb-faq-list" }, helpers.loadingBlock());
        const add = h("button", { className: "button button-primary", type: "button", text: "Add FAQ", onclick: () => editFaq(null, load) });
        host.replaceChildren(
            h("div", { className: "notice-card cb-notice" },
                "The assistant uses active FAQs to answer common questions, together with the live catalog (prices and packages come from Services & Products, never from FAQs). Hidden FAQs are not used."),
            helpers.panel("Frequently asked questions", "Shown to the assistant in this order", list, add),
        );
        async function load() {
            let data;
            try {
                data = await api("/api/admin/chatbot/faqs");
            } catch (error) {
                list.replaceChildren(helpers.errorBlock(error.message, load));
                return;
            }
            list.replaceChildren(...(data.faqs.length ? data.faqs.map((faq) => h("button", {
                className: `cb-faq${faq.active ? "" : " cb-faq-hidden"}`, type: "button", onclick: () => editFaq(faq, load),
            },
            h("span", { className: "cb-faq-head" }, h("strong", { text: faq.question }), faq.active ? null : badge("Hidden", "cb-badge-muted")),
            h("span", { className: "table-secondary", text: excerpt(faq.answer, 220) }))) : [h("p", { className: "empty-state", text: "No FAQs yet." })]));
        }
        load();
    }

    function editFaq(faq, onSaved) {
        const creating = !faq || !faq.id;
        const question = h("input", { maxlength: 300, required: true, value: faq?.question || "" });
        const answer = h("textarea", { rows: 6, maxlength: 3000, required: true });
        answer.value = faq?.answer || "";
        const keywords = h("input", { maxlength: 300, value: faq?.keywords || "", placeholder: "e.g. magkano price presyo" });
        const order = h("input", { type: "number", min: 0, max: 10000, step: 1, value: faq?.sort_order ?? "" , placeholder: "Last" });
        const active = h("input", { type: "checkbox" });
        active.checked = faq ? faq.active !== false : true;
        const save = h("button", { className: "button button-primary", type: "submit", text: creating ? "Add FAQ" : "Save changes" });
        const form = h("form", { className: "cb-faq-form" },
            h("header", { className: "order-modal-header" }, h("div", {},
                h("p", { className: "eyebrow", text: "Chatbot FAQ" }),
                h("h2", { className: "dialog-heading", text: creating ? "Add FAQ" : "Edit FAQ" }))),
            h("div", { className: "form-grid" },
                h("div", { className: "form-field full" }, h("label", { text: "Question" }), question),
                h("div", { className: "form-field full" }, h("label", { text: "Answer" }), answer,
                    h("span", { className: "field-note", text: "Do not type prices here: the assistant reads prices and packages from the catalog so they stay current." })),
                h("div", { className: "form-field" }, h("label", { text: "Extra keywords (optional, English or Filipino)" }), keywords),
                h("div", { className: "form-field" }, h("label", { text: "Position" }), order),
                h("label", { className: "form-field full cb-check" }, active, " Active (used by the assistant)")),
            h("div", { className: "dialog-actions" },
                creating ? null : h("button", { className: "button button-danger-strong", type: "button", text: "Delete", onclick: async () => {
                    const confirmed = await UI.confirm({ title: "Delete this FAQ?", message: faq.question, confirmLabel: "Delete", tone: "danger" });
                    if (!confirmed) return;
                    try {
                        await api(`/api/admin/chatbot/faqs/${encodeURIComponent(faq.id)}`, { method: "DELETE" });
                        UI.closeModal();
                        toast("FAQ deleted.", "success");
                        onSaved();
                    } catch (error) {
                        toast(error.message, "error");
                    }
                } }),
                save));
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            UI.busy(save, async () => {
                const body = {
                    question: question.value.trim(), answer: answer.value.trim(), keywords: keywords.value.trim(), active: active.checked,
                    ...(order.value === "" ? (creating ? {} : {}) : { sort_order: Number(order.value) }),
                };
                try {
                    if (creating) await api("/api/admin/chatbot/faqs", { method: "POST", body });
                    else await api(`/api/admin/chatbot/faqs/${encodeURIComponent(faq.id)}`, { method: "PATCH", body });
                    UI.closeModal();
                    toast(creating ? "FAQ added. The assistant uses it from the next question." : "FAQ saved.", "success");
                    onSaved();
                } catch (error) {
                    toast(error.message, "error");
                }
            });
        });
        UI.openModal(form);
        question.focus();
    }

    // ------------------------------------------------------------ settings

    async function renderSettings(host) {
        let data;
        try {
            data = await api("/api/admin/chatbot/settings");
        } catch (error) {
            host.replaceChildren(helpers.errorBlock(error.message, () => renderSettings(host)));
            return;
        }
        const toggle = h("button", { className: "settings-toggle", type: "button", "aria-pressed": String(data.enabled), "aria-label": "Chatbot enabled" });
        toggle.addEventListener("click", () => toggle.setAttribute("aria-pressed", String(toggle.getAttribute("aria-pressed") !== "true")));
        const welcome = h("textarea", { rows: 4, maxlength: 600 });
        welcome.value = data.welcome_message;
        const suggestions = h("textarea", { rows: 6 });
        suggestions.value = data.suggestions.join("\n");
        const save = h("button", { className: "button button-primary", type: "submit", text: "Save settings" });
        const form = h("form", { className: "cb-settings" },
            h("div", { className: "cb-toggle-row" }, toggle, h("div", {},
                h("strong", { text: "Show the chat on the website" }),
                h("p", { className: "table-secondary", text: "When off, customers don't see the chat button and the chat API refuses messages." }))),
            h("div", { className: "form-grid" },
                h("div", { className: "form-field full" }, h("label", { text: "Welcome message" }), welcome,
                    h("span", { className: "field-note", text: "The first message customers see. Leave it empty to use the default." })),
                h("div", { className: "form-field full" }, h("label", { text: "Suggested questions (one per line, up to 8)" }), suggestions)),
            h("div", { className: "dialog-actions" },
                h("button", { className: "button", type: "button", text: "Use default welcome", onclick: () => { welcome.value = data.default_welcome_message; } }),
                save));
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            UI.busy(save, async () => {
                try {
                    data = await api("/api/admin/chatbot/settings", {
                        method: "PATCH",
                        body: {
                            enabled: toggle.getAttribute("aria-pressed") === "true",
                            welcome_message: welcome.value,
                            suggestions: suggestions.value.split("\n").map((line) => line.trim()).filter(Boolean).slice(0, 8),
                        },
                    });
                    toast(data.enabled ? "Chatbot settings saved." : "Saved. The chatbot is now hidden from customers.", "success");
                } catch (error) {
                    toast(error.message, "error");
                }
            });
        });
        const ai = data.ai;
        const status = ai.last_status || {};
        host.replaceChildren(
            h("section", { className: "split-grid" },
                helpers.panel("Chatbot settings", "What customers see", form),
                helpers.panel("AI provider", "Configured on the server; the API key is never shown or sent to browsers", helpers.detailList([
                    ["Provider", ai.provider],
                    ["Model", ai.model],
                    ["API key", ai.configured ? "Configured (GEMINI_API_KEY)" : "Not set: answers come from the database only"],
                    ["Last AI status", status.at ? `${status.ok ? "Working" : `Failed: ${status.error || "unknown error"}`} (${dateTime(status.at)})` : "No AI requests yet"],
                    ["Data used", "Live services, products, packages, option charges, active FAQs, and only the signed-in customer's own orders"],
                ]))),
        );
    }

    window.PrintShopChatbotAdmin = { render };
})();
