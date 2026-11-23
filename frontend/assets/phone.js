"use strict";

/**
 * Shared client-side contact-number validation for customer forms.
 *
 * Mirrors backend/app/schemas/user.py::validate_phone(): plausible Philippine mobile numbers
 * (09XX XXX XXXX or +63 9XX XXX XXXX) and landlines (02 XXX XXXX). Server validation remains
 * authoritative; this only avoids a needless round trip for obviously invalid input.
 */
(() => {
    function valid(value) {
        const text = String(value ?? "").trim();
        if (!/^[0-9+\s()-]{7,40}$/.test(text)) return false;
        const digits = text.replace(/\D/g, "");
        return /^(?:0|63)9\d{9}$/.test(digits) || /^0\d{7,9}$/.test(digits);
    }

    window.PrintShopPhone = { valid };
})();
