// ===================== PIN PROFILE =====================
const PinProfile = (() => {
    function bindUI(cfg = {}) {
        const sel = document.getElementById('BILLY_PINS_SELECT');
        if (!sel) return;

        const mode = String(cfg.BILLY_PINS || 'new').toLowerCase();
        sel.value = mode;
        sel.setAttribute('data-original', mode);
    }

    return { bindUI };
})();


