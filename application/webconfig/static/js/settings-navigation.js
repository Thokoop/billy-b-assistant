// All categories share one mounted form. Navigation never serializes secrets,
// recreates controls or reloads saved configuration over an unfinished draft.
window.SettingsNavigation = (() => {
    const states = new WeakMap();
    let draftForm = null;
    let headerObserver = null;
    const currentForm = () => document.getElementById('config-form');
    const valueOf = field => ['checkbox', 'radio'].includes(field.type) ? field.checked : field.value;
    const snapshot = form => new Map(Array.from(form.querySelectorAll('input[name], select[name], textarea[name]'))
        .filter(field => field.type !== 'file').map(field => [field, valueOf(field)]));

    function changed(form) {
        const state = states.get(form);
        return state ? [...snapshot(form)].filter(([field, value]) => value !== state.baseline.get(field)) : [];
    }

    function hasDraft(form = currentForm()) {
        return !!form && (form.dataset.saving === 'true' || changed(form).length > 0
            || [...form.querySelectorAll('input[type="file"]')].some(field => field.files?.length));
    }

    function update(form) {
        const state = states.get(form);
        if (!state) return;
        const ids = new Set(changed(form).map(([field]) => field.closest('[data-settings-panel]')?.id));
        const labels = [];
        for (const button of state.buttons) {
            const dirty = ids.has(button.dataset.settingsTarget);
            button.querySelector('[data-settings-dirty]').hidden = !dirty;
            if (dirty) labels.push(button.querySelector('.settings-menu-label').textContent);
        }
        const status = form.querySelector('#settings-save-status');
        status.textContent = labels.length ? `Unsaved changes: ${labels.join(', ')}` : 'Changes in all categories are saved together.';
        status.classList.toggle('has-changes', labels.length > 0);
    }

    function writeLocation(path, replace = false) {
        const target = path + window.location.search;
        if (target === window.location.pathname + window.location.search + window.location.hash) return;
        window.history[replace ? 'replaceState' : 'pushState'](
            {...window.history.state, url: target}, '', target,
        );
    }

    function select(id, {focus = false, writePath = false, replace = false} = {}) {
        const form = currentForm();
        const state = states.get(form);
        if (!state) return false;
        const button = state.buttons.find(button => button.dataset.settingsTarget === id && !button.hidden);
        if (!button) return false;
        state.selected = id;
        for (const panel of state.panels) panel.hidden = panel.id !== id;
        for (const item of state.buttons) {
            if (item === button) item.setAttribute('aria-current', 'true');
            else item.removeAttribute('aria-current');
        }
        window.MobileSplitView?.showDetail('settings-workspace');
        if (writePath) writeLocation(button.dataset.settingsPath, replace);
        if (focus) {
            const title = form.querySelector(`#title-${id}`);
            title?.focus({preventScroll: true});
            // Reveal the top of the editor without placing it behind the header.
            const headerHeight = document.querySelector('.billy-header')?.getBoundingClientRect().height || 0;
            const top = state.root.getBoundingClientRect().top + window.scrollY - headerHeight - 16;
            window.scrollTo({top: Math.max(0, top), behavior: 'auto'});
        }
        update(form);
        return true;
    }

    function restore() {
        const form = currentForm();
        const state = states.get(form);
        if (!state) return;
        // Preserve bookmarks from before categories acquired their own routes.
        const hash = window.location.hash.slice(1);
        const legacyId = hash === 'section-software-version' ? 'section-software' : hash;
        const legacy = state.buttons.find(button => button.dataset.settingsTarget === legacyId && !button.hidden);
        const path = window.location.pathname.replace(/\/$/, '');
        const target = legacy || state.buttons.find(button => button.dataset.settingsPath === path && !button.hidden);
        if (target) {
            select(target.dataset.settingsTarget, {writePath: !!legacy, replace: true});
        } else {
            window.MobileSplitView?.showList('settings-workspace');
        }
        window.MobileSplitView?.bindAll();
        update(form);
    }

    function init() {
        const form = currentForm();
        if (!form || states.has(form)) { restore(); return; }
        const root = form.querySelector('#settings-workspace');
        if (!root) return;
        const state = {root, baseline: snapshot(form), selected: null,
            buttons: [...form.querySelectorAll('[data-settings-target]')],
            panels: [...form.querySelectorAll('[data-settings-panel]')]};
        states.set(form, state);
        draftForm = form;
        const header = document.querySelector('.billy-header');
        if (header && !headerObserver && window.ResizeObserver) {
            headerObserver = new ResizeObserver(() => {
                document.documentElement.style.setProperty('--settings-header-height', `${header.getBoundingClientRect().height}px`);
            });
            headerObserver.observe(header);
        }
        window.MobileSplitView?.bindAll();
        for (const button of state.buttons) {
            const panel = state.panels.find(panel => panel.id === button.dataset.settingsTarget);
            // Wi-Fi onboarding keeps its existing category locks.
            button.hidden = !panel || panel.classList.contains('hidden');
            button.addEventListener('click', event => {
                // Retain open-in-new-tab/window and ordinary browser link behavior.
                if (event.defaultPrevented || event.button !== 0
                    || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                event.preventDefault();
                select(button.dataset.settingsTarget, {focus: true, writePath: true});
            });
        }
        for (const group of form.querySelectorAll('.settings-menu-group')) {
            group.hidden = [...group.querySelectorAll('[data-settings-target]')].every(button => button.hidden);
        }
        const back = root.querySelector('[data-mobile-split-back]');
        back.addEventListener('click', () => {
            writeLocation('/settings');
            state.buttons.find(button => button.dataset.settingsTarget === state.selected)?.focus();
        });
        // A select/toggle can update its real hidden field in its own listener;
        // bubble listeners see that final value, including synthetic changes.
        for (const event of ['input', 'change', 'click']) form.addEventListener(event, () => update(form));
        form.addEventListener('invalid', event => {
            const panel = event.target.closest('[data-settings-panel]');
            if (!panel) return;
            // Reveal only the first failing category in this validation pass.
            // Native validation will then focus its first invalid control.
            if (!state.validating) {
                select(panel.id, {writePath: true, replace: true});
                state.validating = true;
                queueMicrotask(() => {state.validating = false;});
            }
        }, true);
        const initial = state.buttons.find(button => !button.hidden)?.dataset.settingsTarget;
        select(initial);
        restore();
    }

    function markSaved(form, submitted) {
        const state = states.get(form);
        if (!state || !submitted) return;
        // Use the submitted snapshot, not the current inputs: users can keep
        // editing/navigating while the save is in flight.
        for (const [field, value] of submitted) state.baseline.set(field, value);
        update(form);
    }

    window.addEventListener('hashchange', restore);
    window.addEventListener('beforeunload', event => {
        if (!hasDraft(draftForm)) return;
        event.preventDefault();
        event.returnValue = '';
    });
    return {init, restore, select, snapshot, markSaved, hasDraft,
        isInitialized: form => !!form && states.has(form)};
})();
