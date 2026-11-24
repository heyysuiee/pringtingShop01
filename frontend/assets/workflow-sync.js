"use strict";

/**
 * Workflow change detection shared by the customer portal and the admin workspace.
 *
 * The project has no WebSocket or broadcast server, so this is the documented polling fallback. It is
 * deliberately small: one interval for every open order on the page, no polling while the tab is hidden,
 * an immediate check when the tab becomes visible or the network comes back, and a cross-tab signal so a
 * second tab does not have to wait for its own interval. Every response is read-only and contains only a
 * version and a few status fields; the page then fetches the authoritative order if the version moved.
 */
(() => {
    const POLL_MS = 15000;
    const CHANNEL_NAME = "impress-workflow";
    const STORAGE_PREFIX = "impress.workflow.version:";
    const watchers = new Map();
    let timer = null;
    let listening = false;
    let channel = null;

    function stateUrl(role, reference) {
        const base = role === "admin" ? "/api/admin/orders" : "/api/account/orders";
        return `${base}/${encodeURIComponent(reference)}/state`;
    }

    function publish(state) {
        const message = { role: state.role, reference: state.reference, version: state.version };
        try { channel?.postMessage(message); } catch { /* a closed channel must not break polling */ }
        try { localStorage.setItem(STORAGE_PREFIX + message.reference, JSON.stringify(message)); } catch { /* storage may be disabled */ }
    }

    function signalOtherTabs(data) {
        if (!data || !data.reference) return;
        watchers.forEach((watcher) => {
            const sameOrder = watcher.reference === data.reference;
            const sameRole = !data.role || watcher.role === data.role;
            if (sameOrder && sameRole && Number(data.version || 0) > watcher.version) void poll(watcher);
        });
    }

    function onMessage(event) { signalOtherTabs(event?.data); }

    function onStorage(event) {
        if (!event.key || !event.key.startsWith(STORAGE_PREFIX)) return;
        try { signalOtherTabs(JSON.parse(event.newValue || "{}")); } catch { /* ignore malformed signals */ }
    }

    function onVisible() {
        if (document.visibilityState === "visible") void tick();
    }

    function attach() {
        if (listening) return;
        listening = true;
        document.addEventListener("visibilitychange", onVisible);
        window.addEventListener("focus", onVisible);
        window.addEventListener("online", onVisible);
        window.addEventListener("pagehide", stop);
        if (typeof BroadcastChannel === "function") {
            try {
                channel = new BroadcastChannel(CHANNEL_NAME);
                channel.addEventListener("message", onMessage);
            } catch { channel = null; }
        }
        window.addEventListener("storage", onStorage);
    }

    function detach() {
        if (!listening) return;
        listening = false;
        document.removeEventListener("visibilitychange", onVisible);
        window.removeEventListener("focus", onVisible);
        window.removeEventListener("online", onVisible);
        window.removeEventListener("pagehide", stop);
        window.removeEventListener("storage", onStorage);
        if (channel) {
            channel.removeEventListener("message", onMessage);
            try { channel.close(); } catch { /* already closed */ }
            channel = null;
        }
    }

    async function poll(watcher) {
        if (watcher.inFlight || document.visibilityState !== "visible") return;
        watcher.inFlight = true;
        const controller = new AbortController();
        watcher.controller = controller;
        try {
            const response = await fetch(stateUrl(watcher.role, watcher.reference), {
                credentials: "same-origin",
                cache: "no-store",
                headers: { Accept: "application/json" },
                signal: controller.signal,
            });
            // 401/404 are handled by the page that loaded the order; do not replace its message here.
            if (!response.ok) return;
            const state = await response.json();
            const version = Number(state.version || 0);
            if (version === watcher.version) return;
            watcher.version = version;
            watcher.callbacks.forEach((callback) => callback({ ...state, role: watcher.role }));
            publish({ ...state, role: watcher.role });
        } catch (error) {
            if (error?.name === "AbortError") return;
            watcher.errors.forEach((callback) => callback(error));
        } finally {
            watcher.inFlight = false;
            watcher.controller = null;
        }
    }

    function tick() {
        watchers.forEach((watcher) => { void poll(watcher); });
    }

    function start() {
        attach();
        if (!timer) timer = window.setInterval(tick, POLL_MS);
    }

    function stop() {
        if (timer) {
            window.clearInterval(timer);
            timer = null;
        }
        watchers.forEach((watcher) => watcher.controller?.abort());
        detach();
    }

    /**
     * Watch one order. `version` is the version already displayed by the caller, so the first poll never
     * causes a redundant reload. Returns an unsubscribe function; listeners and the interval are removed
     * automatically when the last watcher is gone.
     */
    function watch({ reference, role = "account", version = 0, onChange, onError }) {
        if (typeof reference !== "string" || !reference || typeof onChange !== "function") return () => {};
        const key = `${role}:${reference}`;
        let watcher = watchers.get(key);
        if (!watcher) {
            watcher = { reference, role, version: Number(version) || 0, callbacks: new Set(), errors: new Set(), inFlight: false, controller: null };
            watchers.set(key, watcher);
        }
        watcher.version = Number(version) || watcher.version;
        watcher.callbacks.add(onChange);
        if (typeof onError === "function") watcher.errors.add(onError);
        start();
        return () => {
            watcher.callbacks.delete(onChange);
            watcher.errors.delete(onError);
            if (watcher.callbacks.size) return;
            watcher.controller?.abort();
            watchers.delete(key);
            if (!watchers.size) stop();
        };
    }

    window.PrintShopWorkflowSync = { watch, tick, stop, POLL_MS };
})();
