"use strict";

/**
 * ImPRESS Assistant: the floating customer support chat on every storefront page.
 *
 * The browser only sends the customer's text; the server answers (Gemini + live database data), keeps
 * the conversation, and decides which orders the signed-in customer may see. No API key or prompt is
 * here. Replies are rendered with text nodes (a small **bold** / bullet-list formatter), never as HTML,
 * and links come from the server's fixed list of site pages.
 *
 * sessionStorage keeps the conversation id and whether the window is open, so the chat continues when
 * the customer moves to another page in the same tab.
 */
(() => {
    if (window.ImpressionChat) return;

    const STORE_CONVERSATION = "impression.chat.conversation";
    const STORE_OPEN = "impression.chat.open";
    const ICONS = {
        chat: '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/><path d="M8 12h.01"/><path d="M12 12h.01"/><path d="M16 12h.01"/>',
        close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
        send: '<path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/>',
        restart: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
        arrow: '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
        minimize: '<path d="M5 12h14"/>',
        sparkles: '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/>',
    };
    // Shown in the window header (the server's assistant name is used for message labels).
    const HEADER_TITLE = "ImPRESS AI Assistant";
    const CLOSE_ANIMATION_MS = 200;
    const QUICK_ACTIONS = [
        ["View Services", "What services do you offer?"],
        ["Browse Packages", "What packages do you have?"],
        ["Check Prices", "How much are your packages and prices?"],
        ["Track My Order", "Track my order"],
        ["How to Order", "How do I place an order?"],
        ["Contact Administrator", null],
    ];

    const state = {
        config: null,
        conversationId: storage("get", STORE_CONVERSATION) || "",
        historyLoaded: false,
        sending: false,
        lastFocus: null,
    };
    let root, panel, launcher, messages, input, sendButton, inquiry, actions, composer, footnote;

    // ------------------------------------------------------------ helpers

    function storage(action, key, value) {
        try {
            if (action === "get") return window.sessionStorage.getItem(key);
            if (action === "set") window.sessionStorage.setItem(key, value);
            if (action === "remove") window.sessionStorage.removeItem(key);
        } catch {
            // Private mode or blocked storage: the chat still works, it just won't follow page changes.
        }
        return null;
    }

    function el(tag, attributes = {}, ...children) {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(attributes)) {
            if (value === null || value === undefined || value === false) continue;
            if (key === "className") node.className = value;
            else if (key === "text") node.textContent = value;
            else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
            else node.setAttribute(key, value === true ? "" : value);
        }
        for (const child of children) {
            if (child !== null && child !== undefined && child !== false) node.append(child);
        }
        return node;
    }

    function icon(name) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("fill", "none");
        svg.setAttribute("stroke", "currentColor");
        svg.setAttribute("stroke-width", "2");
        svg.setAttribute("stroke-linecap", "round");
        svg.setAttribute("stroke-linejoin", "round");
        svg.setAttribute("aria-hidden", "true");
        svg.innerHTML = ICONS[name]; // fixed markup from this file only
        return svg;
    }

    function time(value) {
        const date = value ? new Date(value) : new Date();
        if (Number.isNaN(date.getTime())) return "";
        return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    }

    async function api(url, { method = "GET", body } = {}) {
        const response = await fetch(url, {
            method,
            credentials: "same-origin",
            headers: body ? { "Content-Type": "application/json" } : {},
            body: body ? JSON.stringify(body) : undefined,
        });
        let data = null;
        try {
            data = await response.json();
        } catch {
            data = null;
        }
        if (!response.ok) {
            const detail = data && data.detail;
            const message = typeof detail === "string" ? detail
                : Array.isArray(detail) ? "Please check what you typed and try again."
                    : "Something went wrong. Please try again.";
            const error = new Error(message);
            error.status = response.status;
            throw error;
        }
        return data;
    }

    /** Text with **bold**, "- " bullets, and "1." lists as DOM nodes (no HTML is ever parsed). */
    function rich(text) {
        const fragment = document.createDocumentFragment();
        let list = null;
        let listType = "";
        for (const raw of String(text || "").split("\n")) {
            const line = raw.replace(/\s+$/, "");
            const bullet = /^\s*[-*•]\s+(.*)$/.exec(line);
            const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
            if (bullet || numbered) {
                const type = bullet ? "ul" : "ol";
                if (!list || listType !== type) {
                    list = el(type);
                    listType = type;
                    fragment.append(list);
                }
                const item = el("li");
                inline(item, (bullet || numbered)[1]);
                list.append(item);
                continue;
            }
            if (list && /^\s{2,}\S/.test(raw) && list.lastElementChild) {
                list.lastElementChild.append(el("br"));
                inline(list.lastElementChild, line.trim());
                continue;
            }
            list = null;
            if (!line.trim()) continue;
            const paragraph = el("p");
            inline(paragraph, line.trim());
            fragment.append(paragraph);
        }
        return fragment;
    }

    function inline(parent, text) {
        const parts = text.split(/\*\*(.+?)\*\*/g);
        parts.forEach((part, index) => {
            if (!part) return;
            parent.append(index % 2 ? el("strong", { text: part }) : document.createTextNode(part));
        });
    }

    function safeUrl(url) {
        // Only same-site paths from the server's link list.
        return typeof url === "string" && /^\/(?!\/)[A-Za-z0-9/_\-?=&#.%]*$/.test(url) ? url : null;
    }

    // ------------------------------------------------------------ rendering

    function scrollDown() {
        messages.scrollTop = messages.scrollHeight;
    }

    function clearSuggestions() {
        messages.querySelectorAll(".ipc-suggestions").forEach((node) => node.remove());
    }

    function addMessage(message, { suggestions = [], error = false } = {}) {
        const isUser = message.role === "user";
        const row = el("div", { className: `ipc-row ${isUser ? "ipc-user" : "ipc-assistant"}${error ? " ipc-error" : ""}` });
        const bubble = el("div", { className: "ipc-bubble" });
        if (isUser) bubble.textContent = message.content;
        else bubble.append(rich(message.content));
        row.append(bubble);
        const label = isUser ? "You" : state.config.assistant_name;
        row.append(el("div", { className: "ipc-meta", text: `${label} · ${time(message.created_at)}` }));
        const links = (message.links || []).map((link) => ({ ...link, url: safeUrl(link.url) })).filter((link) => link.url);
        if (links.length) {
            row.append(el("div", { className: "ipc-links" }, ...links.map((link) =>
                el("a", { className: "ipc-link", href: link.url }, link.label, icon("arrow")))));
        }
        const chips = [];
        if (message.needs_admin) {
            chips.push(el("button", { className: "ipc-chip ipc-contact-chip", type: "button", text: "Contact administrator", onclick: openInquiry }));
        }
        for (const suggestion of suggestions) {
            chips.push(el("button", { className: "ipc-chip", type: "button", text: suggestion, onclick: () => send(suggestion) }));
        }
        if (chips.length) {
            clearSuggestions();
            row.append(el("div", { className: "ipc-suggestions" }, ...chips));
        }
        messages.append(row);
        scrollDown();
        return row;
    }

    function welcome() {
        const config = state.config;
        const greeting = config.signed_in && config.customer_name
            ? `Hi ${config.customer_name}! ${config.welcome_message.replace(/^(hi|hello)[!,.]?\s*/i, "")}`
            : config.welcome_message;
        addMessage({ role: "assistant", content: greeting, created_at: new Date().toISOString() }, { suggestions: config.suggestions || [] });
    }

    function typing(show) {
        messages.querySelector(".ipc-typing")?.remove();
        if (!show) return;
        messages.append(el("div", { className: "ipc-row ipc-assistant ipc-typing", "aria-label": `${state.config.assistant_name} is typing` },
            el("div", { className: "ipc-bubble" }, el("span"), el("span"), el("span"))));
        scrollDown();
    }

    // ------------------------------------------------------------ behaviour

    async function loadHistory() {
        if (state.historyLoaded) return;
        state.historyLoaded = true;
        welcome();
        if (!state.conversationId) return;
        try {
            const data = await api(`/api/chatbot/conversations/${encodeURIComponent(state.conversationId)}`);
            if (data.messages.length) clearSuggestions();
            data.messages.forEach((message) => addMessage(message));
        } catch {
            // Expired, or it belongs to a different sign-in: start a fresh conversation.
            state.conversationId = "";
            storage("remove", STORE_CONVERSATION);
        }
    }

    async function send(text) {
        const message = (text ?? input.value).trim();
        if (!message || state.sending) return;
        state.sending = true;
        sendButton.disabled = true;
        clearSuggestions();
        if (text === undefined) input.value = "";
        resizeInput();
        const pending = addMessage({ role: "user", content: message, created_at: new Date().toISOString() });
        typing(true);
        try {
            const data = await api("/api/chatbot/messages", {
                method: "POST",
                body: { conversation_id: state.conversationId || null, message, page: window.location.pathname },
            });
            state.conversationId = data.conversation_id;
            storage("set", STORE_CONVERSATION, data.conversation_id);
            typing(false);
            pending.querySelector(".ipc-meta").textContent = `You · ${time(data.question.created_at)}`;
            addMessage(data.reply, { suggestions: data.reply.suggestions || [] });
        } catch (error) {
            typing(false);
            addMessage({ role: "assistant", content: error.message, created_at: new Date().toISOString(), needs_admin: error.status !== 429 }, { error: true });
        } finally {
            state.sending = false;
            sendButton.disabled = !input.value.trim();
            input.focus();
        }
    }

    function resizeInput() {
        input.style.height = "auto";
        input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
        sendButton.disabled = state.sending || !input.value.trim();
    }

    function open({ focus = true } = {}) {
        window.clearTimeout(state.closeTimer);
        panel.classList.remove("ipc-closing");
        state.lastFocus = document.activeElement;
        root.classList.add("ipc-open");
        panel.hidden = false;
        launcher.setAttribute("aria-expanded", "true");
        root.querySelector(".ipc-launcher-dot")?.remove();
        storage("set", STORE_OPEN, "1");
        loadHistory();
        if (focus) window.setTimeout(() => input.focus(), 30);
    }

    /**
     * Hide the window. Minimize keeps an unfinished "Contact administrator" form; close discards it.
     * Either way the conversation is kept and continues when the chat is opened again.
     */
    function close({ minimize = false } = {}) {
        if (panel.hidden || panel.classList.contains("ipc-closing")) return;
        if (!minimize) closeInquiry();
        launcher.setAttribute("aria-expanded", "false");
        storage("remove", STORE_OPEN);
        const finish = () => {
            panel.classList.remove("ipc-closing");
            root.classList.remove("ipc-open");
            panel.hidden = true;
            (state.lastFocus && document.contains(state.lastFocus) ? state.lastFocus : launcher).focus();
        };
        if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
            finish();
            return;
        }
        panel.classList.add("ipc-closing");
        state.closeTimer = window.setTimeout(finish, CLOSE_ANIMATION_MS);
    }

    function restart() {
        state.conversationId = "";
        storage("remove", STORE_CONVERSATION);
        closeInquiry();
        messages.replaceChildren();
        state.historyLoaded = false;
        loadHistory();
        input.focus();
    }

    // ------------------------------------------------------------ contact the administrator

    function openInquiry() {
        const config = state.config;
        const recentQuestion = [...messages.querySelectorAll(".ipc-user .ipc-bubble")].pop()?.textContent || "";
        const error = el("p", { className: "ipc-form-error", role: "alert", hidden: true });
        const nameInput = el("input", { name: "name", autocomplete: "name", maxlength: "120", required: true });
        const emailInput = el("input", { name: "email", type: "email", autocomplete: "email", maxlength: "254", required: true });
        const phoneInput = el("input", { name: "phone", type: "tel", autocomplete: "tel", maxlength: "40" });
        const messageInput = el("textarea", { name: "message", maxlength: "2000", required: true });
        messageInput.value = recentQuestion;
        if (config.signed_in) emailInput.value = config.customer_email || "";
        const submit = el("button", { className: "ipc-button", type: "submit", text: "Send to administrator" });
        const form = el("form", { className: "ipc-inquiry-form", novalidate: true },
            el("h3", { text: "Contact the shop administrator" }),
            el("p", { text: config.signed_in
                ? "Your message is saved with your account. The shop will follow up with you."
                : "Leave your details and message. The shop will follow up using these contact details." }),
            config.signed_in ? null : el("label", {}, "Name", nameInput),
            config.signed_in ? null : el("label", {}, "Email", emailInput),
            config.signed_in ? null : el("label", {}, "Phone (optional)", phoneInput),
            el("label", {}, "Your message", messageInput),
            error,
            el("div", { className: "ipc-inquiry-actions" },
                el("button", { className: "ipc-button ipc-ghost", type: "button", text: "Cancel", onclick: closeInquiry }),
                submit));
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            error.hidden = true;
            if (!config.signed_in && (!nameInput.value.trim() || !emailInput.value.trim())) {
                error.textContent = "Please enter your name and email.";
                error.hidden = false;
                return;
            }
            if (messageInput.value.trim().length < 5) {
                error.textContent = "Please write a short message.";
                error.hidden = false;
                return;
            }
            submit.disabled = true;
            submit.textContent = "Sending…";
            try {
                const data = await api("/api/chatbot/inquiries", {
                    method: "POST",
                    body: {
                        conversation_id: state.conversationId || null,
                        name: nameInput.value, email: emailInput.value, phone: phoneInput.value, message: messageInput.value,
                    },
                });
                closeInquiry();
                addMessage({ role: "assistant", content: data.message, created_at: data.inquiry.created_at });
            } catch (failure) {
                error.textContent = failure.message;
                error.hidden = false;
                submit.disabled = false;
                submit.textContent = "Send to administrator";
            }
        });
        inquiry.replaceChildren(form);
        inquiry.hidden = false;
        actions.hidden = true;
        composer.hidden = true;
        footnote.hidden = true;
        (config.signed_in ? messageInput : nameInput).focus();
    }

    function closeInquiry() {
        if (!inquiry || inquiry.hidden) return;
        inquiry.hidden = true;
        inquiry.replaceChildren();
        actions.hidden = false;
        composer.hidden = false;
        footnote.hidden = false;
    }

    // ------------------------------------------------------------ build

    function build() {
        const name = state.config.assistant_name;
        launcher = el("button", {
            className: "ipc-launcher", type: "button", "aria-expanded": "false", "aria-controls": "ipc-panel",
            "aria-label": `Chat with ${name}`, onclick: () => open(),
        }, el("span", { className: "ipc-launcher-icon" }, icon("chat")), el("span", { className: "ipc-launcher-label", text: "Ask us" }), el("span", { className: "ipc-launcher-dot", "aria-hidden": "true" }));

        messages = el("div", { className: "ipc-messages", role: "log", "aria-live": "polite", "aria-label": "Conversation" });
        input = el("textarea", {
            rows: "1", maxlength: String(state.config.max_message_chars || 1000), placeholder: "Type your question…",
            "aria-label": "Type your question", enterkeyhint: "send",
        });
        sendButton = el("button", { className: "ipc-send", type: "submit", "aria-label": "Send", disabled: true }, icon("send"));
        composer = el("form", { className: "ipc-composer", onsubmit: (event) => { event.preventDefault(); send(); } }, input, sendButton);
        input.addEventListener("input", resizeInput);
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
                event.preventDefault();
                send();
            }
        });
        actions = el("div", { className: "ipc-actions", role: "group", "aria-label": "Suggested actions" },
            ...QUICK_ACTIONS.map(([label, question]) => el("button", {
                className: `ipc-chip${question ? "" : " ipc-contact-chip"}`, type: "button", text: label,
                onclick: () => (question ? send(question) : openInquiry()),
            })));
        inquiry = el("div", { className: "ipc-inquiry", hidden: true });
        footnote = el("p", { className: "ipc-footnote", text: "AI answers can make mistakes. Prices come from our live catalog." });

        panel = el("section", { className: "ipc-panel", id: "ipc-panel", role: "dialog", "aria-label": `${name} chat`, hidden: true },
            el("header", { className: "ipc-header" },
                el("span", { className: "ipc-avatar", "aria-hidden": "true" }, icon("sparkles")),
                el("div", { className: "ipc-title" }, el("strong", { text: HEADER_TITLE }), el("small", { text: "Online" })),
                el("button", { className: "ipc-icon-button", type: "button", title: "Start a new chat", "aria-label": "Start a new chat", onclick: restart }, icon("restart")),
                el("button", { className: "ipc-icon-button", type: "button", title: "Minimize", "aria-label": "Minimize chat", onclick: () => close({ minimize: true }) }, icon("minimize")),
                el("button", { className: "ipc-icon-button", type: "button", title: "Close chat", "aria-label": "Close chat", onclick: () => close() }, icon("close"))),
            messages, inquiry, actions, composer, footnote);
        panel.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.stopPropagation();
                if (!inquiry.hidden) closeInquiry();
                else close();
            }
        });

        root = el("div", { className: "ipc-root", id: "ipc-root" }, panel, launcher);
        document.body.append(root);
        if (storage("get", STORE_OPEN)) open({ focus: false });
    }

    async function start() {
        try {
            state.config = await api("/api/chatbot/config");
        } catch {
            return; // The site works normally without the chat.
        }
        if (!state.config || !state.config.enabled) return;
        build();
    }

    window.ImpressionChat = { open: () => root && open() };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
})();
