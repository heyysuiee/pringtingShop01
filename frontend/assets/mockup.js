"use strict";

/**
 * InvitationMockup — draws an invitation preview (5 × 7 card) as SVG.
 *
 * Used by the order page (designer), the customer portal, and the admin order details, so everyone sees
 * the same card. Only Birthday, Wedding, and Christening invitations have templates.
 *
 *   InvitationMockup.templates("wedding")          → [{ id, name, palettes: [...] }]
 *   InvitationMockup.kindOf(product)               → "birthday" | "wedding" | "christening" | null
 *   InvitationMockup.render({ kind, template, palette, headline, message, details }) → <svg>
 *
 * ``details`` holds { names, age, date, time, location, rsvp } from the order form.
 * Keep the template ids in sync with backend/app/services/mockup_services.py.
 */
(() => {
    const NS = "http://www.w3.org/2000/svg";
    const W = 500;
    const H = 700;
    const SERIF = "Georgia, 'Times New Roman', serif";
    const SCRIPT = "'Brush Script MT', 'Segoe Script', 'Lucida Handwriting', cursive";
    const SANS = "'Segoe UI', Arial, sans-serif";

    // bg, ink (main text), accent, soft (secondary shapes)
    const TEMPLATES = {
        birthday: [
            { id: "confetti", name: "Confetti Pop", palettes: [
                { bg: "#fff8ec", ink: "#3b2a4a", accent: "#ff6f91", soft: "#ffc75f" },
                { bg: "#eef8ff", ink: "#1f3b57", accent: "#2e9cca", soft: "#ffb347" },
                { bg: "#f3fff0", ink: "#24452b", accent: "#5bbf6a", soft: "#ff8c69" },
            ] },
            { id: "balloons", name: "Balloon Party", palettes: [
                { bg: "#fdf2f8", ink: "#5a2445", accent: "#e64980", soft: "#74c0fc" },
                { bg: "#f1f3ff", ink: "#29305e", accent: "#5c7cfa", soft: "#ffd43b" },
                { bg: "#fff9db", ink: "#5c4400", accent: "#f08c00", soft: "#63e6be" },
            ] },
            { id: "starry", name: "Starry Night", palettes: [
                { bg: "#1b1f3b", ink: "#fdf6e3", accent: "#f2c94c", soft: "#8c9eff" },
                { bg: "#24112f", ink: "#fbeaff", accent: "#ff9ff3", soft: "#c8b6ff" },
                { bg: "#0f2a2a", ink: "#e9fff9", accent: "#7ee8c4", soft: "#ffd166" },
            ] },
        ],
        wedding: [
            { id: "classic", name: "Classic Elegance", palettes: [
                { bg: "#fbf7ef", ink: "#3d3428", accent: "#b08d57", soft: "#e8dcc4" },
                { bg: "#ffffff", ink: "#2b2b2b", accent: "#8a8a8a", soft: "#e6e6e6" },
                { bg: "#f6f0f3", ink: "#4a2f3c", accent: "#b5838d", soft: "#ead7dd" },
            ] },
            { id: "botanical", name: "Botanical Greenery", palettes: [
                { bg: "#f7f8f2", ink: "#2f3e2c", accent: "#6b8f5e", soft: "#a3b899" },
                { bg: "#fbf8f3", ink: "#3c3a2e", accent: "#9c8a5b", soft: "#c9c19f" },
                { bg: "#f2f6f7", ink: "#24383d", accent: "#4f7c82", soft: "#9fbfc4" },
            ] },
            { id: "monogram", name: "Modern Monogram", palettes: [
                { bg: "#f4f1ec", ink: "#1e1e1e", accent: "#1e1e1e", soft: "#d8d2c7" },
                { bg: "#0f1d2b", ink: "#f4efe6", accent: "#d4af6a", soft: "#2a3d50" },
                { bg: "#f0ebe3", ink: "#5b3a29", accent: "#a0522d", soft: "#e2d3c3" },
            ] },
        ],
        christening: [
            { id: "clouds", name: "Soft Clouds", palettes: [
                { bg: "#eaf4ff", ink: "#294a6b", accent: "#6aa6de", soft: "#ffffff" },
                { bg: "#fdeef4", ink: "#6b2f4a", accent: "#e58fb1", soft: "#ffffff" },
                { bg: "#f0f7ee", ink: "#33503a", accent: "#88b494", soft: "#ffffff" },
            ] },
            { id: "dove", name: "Peace Dove", palettes: [
                { bg: "#fbfaf6", ink: "#3a4a5c", accent: "#c8a96a", soft: "#dfe7ef" },
                { bg: "#f6fafd", ink: "#2c4258", accent: "#7fa7c9", soft: "#e3eef6" },
                { bg: "#fdf8fa", ink: "#563a4a", accent: "#d39fb5", soft: "#f3e3ea" },
            ] },
            { id: "floral", name: "Pastel Florals", palettes: [
                { bg: "#fffaf5", ink: "#4d3b33", accent: "#f2a7b8", soft: "#bcdff1" },
                { bg: "#f8fbff", ink: "#2f4560", accent: "#9cc5ea", soft: "#f6d5a8" },
                { bg: "#fbfff8", ink: "#3c4a35", accent: "#b9d99f", soft: "#f5c6d6" },
            ] },
        ],
    };

    const DEFAULT_TEXT = {
        birthday: { headline: "You're invited to a birthday celebration", message: "Come celebrate with cake, games, and lots of fun!" },
        wedding: { headline: "Together with their families", message: "request the pleasure of your company as they celebrate their marriage" },
        christening: { headline: "You are invited to the christening of", message: "Please join us as we celebrate this blessed day." },
    };

    function svg(tag, attributes = {}, text) {
        const element = document.createElementNS(NS, tag);
        for (const [name, value] of Object.entries(attributes)) {
            if (value !== undefined && value !== null) element.setAttribute(name, String(value));
        }
        if (text !== undefined) element.textContent = text;
        return element;
    }

    function kindOf(product) {
        if (!product || !(product.fields || []).some((field) => field.section === "event")) return null;
        const text = `${product.id || ""} ${product.name || ""}`.toLowerCase();
        if (text.includes("wedding")) return "wedding";
        if (text.includes("christening") || text.includes("baptism")) return "christening";
        if (text.includes("birthday")) return "birthday";
        return null;
    }

    function formatDate(value) {
        if (!value) return "";
        const [year, month, day] = String(value).split("-").map(Number);
        if (!year || !month || !day) return String(value);
        return new Date(Date.UTC(year, month - 1, day)).toLocaleDateString("en-PH", {
            weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "UTC",
        });
    }

    function formatTime(value) {
        if (!value) return "";
        const [hours, minutes] = String(value).split(":").map(Number);
        if (Number.isNaN(hours)) return String(value);
        const suffix = hours >= 12 ? "PM" : "AM";
        return `${((hours + 11) % 12) + 1}:${String(minutes || 0).padStart(2, "0")} ${suffix}`;
    }

    function ordinal(value) {
        const number = Number(value);
        if (!Number.isInteger(number) || number <= 0) return "";
        const tail = number % 100;
        const suffix = tail >= 11 && tail <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" }[number % 10] || "th");
        return `${number}${suffix}`;
    }

    /** Word-wrap text into <tspan> lines centred at x. Returns the y after the last line. */
    function textBlock(parent, text, { x = W / 2, y, size, family = SERIF, fill, weight = "normal", style = "normal", maxChars = 30, lineHeight = 1.3, maxLines = 4, spacing = 0 }) {
        if (!text) return y;
        const words = String(text).split(/\s+/).filter(Boolean);
        const lines = [];
        let line = "";
        for (const word of words) {
            const candidate = line ? `${line} ${word}` : word;
            if (candidate.length > maxChars && line) {
                lines.push(line);
                line = word;
            } else {
                line = candidate;
            }
        }
        if (line) lines.push(line);
        if (lines.length > maxLines) {
            lines.length = maxLines;
            lines[maxLines - 1] = `${lines[maxLines - 1].replace(/[.,;:!?]*$/, "")}…`;
        }
        const element = svg("text", {
            x, y, "text-anchor": "middle", "font-family": family, "font-size": size, fill,
            "font-weight": weight, "font-style": style, "letter-spacing": spacing || null,
        });
        lines.forEach((content, index) => {
            element.append(svg("tspan", { x, dy: index === 0 ? 0 : size * lineHeight }, content));
        });
        parent.append(element);
        return y + size * lineHeight * (lines.length - 1) + size * lineHeight;
    }

    // Deterministic pseudo-random numbers so a template always looks the same.
    function seeded(seed) {
        let value = seed;
        return () => {
            value = (value * 9301 + 49297) % 233280;
            return value / 233280;
        };
    }

    // ------------------------------------------------------------------ decorations

    function confetti(root, p) {
        const random = seeded(7);
        const colors = [p.accent, p.soft, p.ink];
        for (let index = 0; index < 46; index += 1) {
            const x = random() * W;
            const y = random() < 0.5 ? random() * 150 : H - random() * 140;
            const color = colors[index % colors.length];
            if (index % 3 === 0) root.append(svg("circle", { cx: x, cy: y, r: 3 + random() * 5, fill: color, opacity: 0.8 }));
            else root.append(svg("rect", { x, y, width: 5 + random() * 8, height: 3 + random() * 4, fill: color, opacity: 0.85, transform: `rotate(${random() * 180} ${x} ${y})` }));
        }
    }

    function balloon(root, x, y, color, ink, scale = 1) {
        const group = svg("g", { transform: `translate(${x} ${y}) scale(${scale})` });
        group.append(
            svg("path", { d: "M0 58 C 6 80, -8 100, 4 130", stroke: ink, "stroke-width": 1.4, fill: "none", opacity: 0.6 }),
            svg("ellipse", { cx: 0, cy: 0, rx: 34, ry: 44, fill: color }),
            svg("path", { d: "M-6 44 L6 44 L0 54 Z", fill: color }),
            svg("ellipse", { cx: -12, cy: -16, rx: 7, ry: 12, fill: "#ffffff", opacity: 0.35 }),
        );
        root.append(group);
    }

    function stars(root, p) {
        const random = seeded(19);
        for (let index = 0; index < 70; index += 1) {
            root.append(svg("circle", { cx: random() * W, cy: random() * H, r: random() * 1.8 + 0.4, fill: index % 4 ? p.ink : p.accent, opacity: 0.35 + random() * 0.6 }));
        }
        const star = (cx, cy, r, fill) => {
            const points = [];
            for (let index = 0; index < 10; index += 1) {
                const radius = index % 2 ? r * 0.42 : r;
                const angle = (Math.PI / 5) * index - Math.PI / 2;
                points.push(`${cx + radius * Math.cos(angle)},${cy + radius * Math.sin(angle)}`);
            }
            root.append(svg("polygon", { points: points.join(" "), fill }));
        };
        star(80, 90, 22, p.accent);
        star(420, 120, 14, p.soft);
        star(430, 610, 20, p.accent);
        star(70, 600, 12, p.soft);
        root.append(svg("circle", { cx: 400, cy: 70, r: 30, fill: p.accent, opacity: 0.9 }), svg("circle", { cx: 388, cy: 62, r: 28, fill: p.bg }));
    }

    function frame(root, p, inset = 22, double = true) {
        root.append(svg("rect", { x: inset, y: inset, width: W - inset * 2, height: H - inset * 2, fill: "none", stroke: p.accent, "stroke-width": 1.6 }));
        if (double) root.append(svg("rect", { x: inset + 8, y: inset + 8, width: W - (inset + 8) * 2, height: H - (inset + 8) * 2, fill: "none", stroke: p.accent, "stroke-width": 0.8 }));
    }

    function leafSpray(root, x, y, angle, p, mirror = 1) {
        const group = svg("g", { transform: `translate(${x} ${y}) rotate(${angle}) scale(${mirror} 1)` });
        group.append(svg("path", { d: "M0 0 C 40 30, 90 50, 150 52", stroke: p.accent, "stroke-width": 2, fill: "none" }));
        for (let index = 0; index < 7; index += 1) {
            const t = 18 + index * 19;
            const offsetY = 14 + index * 5.2;
            group.append(
                svg("ellipse", { cx: t, cy: offsetY - 13, rx: 13, ry: 5.5, fill: index % 2 ? p.soft : p.accent, transform: `rotate(-32 ${t} ${offsetY - 13})` }),
                svg("ellipse", { cx: t + 4, cy: offsetY + 9, rx: 13, ry: 5.5, fill: index % 2 ? p.accent : p.soft, transform: `rotate(28 ${t + 4} ${offsetY + 9})` }),
            );
        }
        root.append(group);
    }

    function cloud(root, x, y, scale, fill, opacity = 1) {
        const group = svg("g", { transform: `translate(${x} ${y}) scale(${scale})`, opacity });
        group.append(
            svg("ellipse", { cx: 0, cy: 10, rx: 60, ry: 22, fill }),
            svg("circle", { cx: -22, cy: -2, r: 24, fill }),
            svg("circle", { cx: 10, cy: -14, r: 30, fill }),
            svg("circle", { cx: 38, cy: 2, r: 20, fill }),
        );
        root.append(group);
    }

    function dove(root, x, y, p) {
        const group = svg("g", { transform: `translate(${x} ${y})` });
        group.append(
            svg("path", { d: "M-40 10 C -20 -6, 0 -4, 12 6 C 26 -30, 54 -40, 70 -36 C 52 -22, 44 -6, 36 10 C 50 12, 60 18, 66 26 C 40 26, 20 30, 0 28 C -16 26, -30 20, -40 10 Z", fill: p.soft, stroke: p.ink, "stroke-width": 1.4 }),
            svg("circle", { cx: -28, cy: 9, r: 1.8, fill: p.ink }),
            svg("path", { d: "M-40 10 L-50 13 L-40 14 Z", fill: p.accent }),
            svg("path", { d: "M-50 30 C -30 40, 0 44, 30 40", stroke: p.accent, "stroke-width": 1.6, fill: "none" }),
        );
        for (let index = 0; index < 5; index += 1) {
            const cx = -40 + index * 16;
            group.append(svg("ellipse", { cx, cy: 37 + (index % 2 ? 6 : -4), rx: 6, ry: 2.6, fill: p.accent, transform: `rotate(${index % 2 ? 25 : -25} ${cx} ${37 + (index % 2 ? 6 : -4)})` }));
        }
        root.append(group);
    }

    function flower(root, x, y, r, petal, center) {
        const group = svg("g", { transform: `translate(${x} ${y})` });
        for (let index = 0; index < 6; index += 1) {
            const angle = (Math.PI / 3) * index;
            group.append(svg("circle", { cx: Math.cos(angle) * r, cy: Math.sin(angle) * r, r: r * 0.85, fill: petal, opacity: 0.9 }));
        }
        group.append(svg("circle", { cx: 0, cy: 0, r: r * 0.6, fill: center }));
        root.append(group);
    }

    // ------------------------------------------------------------------ layouts

    function eventLines(root, d, p, y, { family = SERIF, size = 17, color = p.ink, maxChars = 36 } = {}) {
        y = textBlock(root, formatDate(d.date) || "Event date", { y, size, family, fill: color, maxChars, maxLines: 2 });
        if (d.time || !d.date) y = textBlock(root, d.time ? `at ${formatTime(d.time)}` : "Event time", { y: y - 2, size: size - 1, family, fill: color, maxChars, maxLines: 1 });
        y = textBlock(root, d.location || "Event location", { y: y + 4, size: size - 1, family, fill: color, style: "italic", maxChars: maxChars + 4, maxLines: 3 });
        return y;
    }

    function birthday(root, template, p, data) {
        const d = data.details || {};
        const name = d.names || "Celebrant's name";
        const age = ordinal(d.age);
        if (template === "confetti") confetti(root, p);
        if (template === "balloons") {
            balloon(root, 70, 110, p.accent, p.ink, 1.05);
            balloon(root, 130, 80, p.soft, p.ink, 0.85);
            balloon(root, 430, 110, p.soft, p.ink, 1.05);
            balloon(root, 372, 84, p.accent, p.ink, 0.8);
        }
        if (template === "starry") stars(root, p);
        let y = template === "balloons" ? 250 : 200;
        y = textBlock(root, (data.headline || DEFAULT_TEXT.birthday.headline).toUpperCase(), { y, size: 15, family: SANS, fill: p.ink, weight: 600, maxChars: 34, spacing: 2, maxLines: 3 });
        y = textBlock(root, name, { y: y + 26, size: name.length > 18 ? 42 : 54, family: SCRIPT, fill: p.accent, maxChars: 18, maxLines: 2, lineHeight: 1.1 });
        if (age) y = textBlock(root, `is turning ${age}!`, { y: y + 2, size: 24, family: SERIF, fill: p.ink, style: "italic" });
        root.append(svg("line", { x1: 170, x2: 330, y1: y + 6, y2: y + 6, stroke: p.accent, "stroke-width": 2, "stroke-linecap": "round" }));
        y = eventLines(root, d, p, y + 38);
        y = textBlock(root, data.message, { y: y + 14, size: 15, family: SANS, fill: p.ink, maxChars: 42, maxLines: 3 });
        if (d.rsvp) textBlock(root, `RSVP: ${d.rsvp}`, { y: Math.max(y + 10, 640), size: 13, family: SANS, fill: p.ink, weight: 600, maxChars: 48, maxLines: 1 });
    }

    function initials(names) {
        const parts = String(names || "").split("&").map((part) => part.trim()).filter(Boolean);
        return parts.length ? parts.map((part) => part[0].toUpperCase()).join(" & ") : "A & B";
    }

    function wedding(root, template, p, data) {
        const d = data.details || {};
        const names = d.names || "Bride & Groom";
        let y = 180;
        if (template === "classic") {
            frame(root, p);
            root.append(svg("path", { d: "M200 120 Q250 92 300 120 Q250 108 200 120 Z", fill: p.accent }), svg("circle", { cx: 250, cy: 104, r: 4, fill: p.accent }));
        }
        if (template === "botanical") {
            leafSpray(root, 30, 40, 18, p);
            leafSpray(root, 470, 40, 162, p, 1);
            leafSpray(root, 40, 670, -30, p);
            leafSpray(root, 460, 670, -150, p);
        }
        if (template === "monogram") {
            root.append(svg("circle", { cx: 250, cy: 125, r: 58, fill: "none", stroke: p.accent, "stroke-width": 2 }));
            root.append(svg("circle", { cx: 250, cy: 125, r: 50, fill: "none", stroke: p.accent, "stroke-width": 0.8 }));
            textBlock(root, initials(names), { y: 138, size: 34, family: SERIF, fill: p.accent, maxChars: 10, maxLines: 1 });
            frame(root, p, 18, false);
            y = 228;
        }
        y = textBlock(root, data.headline || DEFAULT_TEXT.wedding.headline, { y, size: 16, family: template === "monogram" ? SANS : SERIF, fill: p.ink, style: template === "monogram" ? "normal" : "italic", maxChars: 38, maxLines: 2, spacing: template === "monogram" ? 1.5 : 0 });
        const nameParts = names.split("&").map((part) => part.trim()).filter(Boolean);
        const nameFont = template === "monogram" ? SERIF : SCRIPT;
        const nameSize = template === "monogram" ? 34 : 46;
        y += 24;
        if (nameParts.length === 2) {
            y = textBlock(root, nameParts[0], { y, size: nameSize, family: nameFont, fill: p.accent, maxChars: 20, maxLines: 2, lineHeight: 1.05 });
            y = textBlock(root, "&", { y: y - 6, size: 26, family: SERIF, fill: p.ink, style: "italic" });
            y = textBlock(root, nameParts[1], { y: y + 4, size: nameSize, family: nameFont, fill: p.accent, maxChars: 20, maxLines: 2, lineHeight: 1.05 });
        } else {
            y = textBlock(root, names, { y, size: nameSize, family: nameFont, fill: p.accent, maxChars: 20, maxLines: 3, lineHeight: 1.05 });
        }
        y = textBlock(root, data.message || DEFAULT_TEXT.wedding.message, { y: y + 10, size: 15, family: SERIF, fill: p.ink, style: "italic", maxChars: 44, maxLines: 3 });
        root.append(svg("line", { x1: 200, x2: 300, y1: y + 2, y2: y + 2, stroke: p.accent, "stroke-width": 1.2 }));
        y = eventLines(root, d, p, y + 32, { family: template === "monogram" ? SANS : SERIF, size: 16 });
        if (d.rsvp) textBlock(root, `RSVP · ${d.rsvp}`, { y: Math.max(y + 12, 640), size: 13, family: SANS, fill: p.ink, maxChars: 48, maxLines: 1, spacing: 1 });
    }

    function christening(root, template, p, data) {
        const d = data.details || {};
        const name = d.names || "Child's name";
        if (template === "clouds") {
            cloud(root, 90, 90, 1.1, p.soft, 0.95);
            cloud(root, 400, 140, 0.8, p.soft, 0.9);
            cloud(root, 420, 620, 1.2, p.soft, 0.95);
            cloud(root, 80, 640, 0.75, p.soft, 0.9);
            root.append(svg("circle", { cx: 250, cy: 120, r: 26, fill: "none", stroke: p.accent, "stroke-width": 3 }));
        }
        if (template === "dove") {
            frame(root, p, 24, false);
            dove(root, 240, 120, p);
        }
        if (template === "floral") {
            flower(root, 60, 70, 18, p.accent, p.soft);
            flower(root, 110, 40, 11, p.soft, p.accent);
            flower(root, 440, 80, 16, p.soft, p.accent);
            flower(root, 400, 44, 10, p.accent, p.soft);
            flower(root, 70, 630, 14, p.soft, p.accent);
            flower(root, 440, 640, 20, p.accent, p.soft);
            flower(root, 395, 668, 10, p.soft, p.accent);
        }
        let y = template === "dove" ? 230 : 200;
        y = textBlock(root, data.headline || DEFAULT_TEXT.christening.headline, { y, size: 17, family: SERIF, fill: p.ink, style: "italic", maxChars: 36, maxLines: 3 });
        y = textBlock(root, name, { y: y + 30, size: name.length > 20 ? 38 : 48, family: SCRIPT, fill: p.accent, maxChars: 20, maxLines: 2, lineHeight: 1.1 });
        y = textBlock(root, data.message || DEFAULT_TEXT.christening.message, { y: y + 10, size: 15, family: SERIF, fill: p.ink, maxChars: 42, maxLines: 3 });
        root.append(svg("path", { d: `M250 ${y - 2} l0 22 M241 ${y + 6} l18 0`, stroke: p.accent, "stroke-width": 2, "stroke-linecap": "round" }));
        y = eventLines(root, d, p, y + 50, { size: 16 });
        if (d.rsvp) textBlock(root, `RSVP: ${d.rsvp}`, { y: Math.max(y + 12, 640), size: 13, family: SANS, fill: p.ink, maxChars: 48, maxLines: 1 });
    }

    function render(data = {}) {
        const kind = TEMPLATES[data.kind] ? data.kind : "birthday";
        const list = TEMPLATES[kind];
        const template = list.find((item) => item.id === data.template) || list[0];
        const palette = template.palettes[Math.min(Math.max(Number(data.palette) || 0, 0), template.palettes.length - 1)];
        const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "invitation-mockup", role: "img", "aria-label": `${template.name} ${kind} invitation preview` });
        root.append(svg("rect", { width: W, height: H, fill: palette.bg }));
        if (kind === "birthday") birthday(root, template.id, palette, data);
        else if (kind === "wedding") wedding(root, template.id, palette, data);
        else christening(root, template.id, palette, data);
        return root;
    }

    function toDataUrl(element) {
        return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(new XMLSerializer().serializeToString(element))}`;
    }

    function templates(kind) {
        return TEMPLATES[kind] || [];
    }

    window.InvitationMockup = { render, templates, kindOf, toDataUrl, defaults: (kind) => ({ ...(DEFAULT_TEXT[kind] || {}) }), formatDate, formatTime };
})();
