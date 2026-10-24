const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function harness({mobile = false, hash = '', locked = false} = {}) {
    const nodes = new Map(), events = new Map();
    function node(id, extra = {}) {
        const handlers = new Map(), classes = new Set();
        const el = {id, dataset: {}, attrs: {}, hidden: false, value: '', type: 'text', ...extra,
            classList: {contains: x => classes.has(x), toggle(x, on) {if (on) classes.add(x); else classes.delete(x);}},
            addEventListener(type, fn) {if (!handlers.has(type)) handlers.set(type, []); handlers.get(type).push(fn);},
            fire(type, target = el) {for (const fn of handlers.get(type) || []) fn({target});},
            setAttribute(k, v) {el.attrs[k] = v;}, removeAttribute(k) {delete el.attrs[k];},
            focus() {ctx.document.activeElement = el;}, getBoundingClientRect: () => ({top: 20, height: 60}),
        };
        nodes.set(id, el); return el;
    }
    const panels = ['voice', 'network', 'software'].map(name => node('section-' + name));
    const fields = panels.map((panel, i) => node('field-' + i, {name: 'SETTING_' + i, value: 'saved-' + i,
        closest: () => panel}));
    fields[0].type = 'password';
    const file = node('file', {type: 'file', files: []});
    const buttons = panels.map(panel => {
        const dot = node('dot-' + panel.id), label = {textContent: panel.id};
        node('title-' + panel.id);
        return node('nav-' + panel.id, {dataset: {settingsTarget: panel.id},
            querySelector: s => s === '[data-settings-dirty]' ? dot : label});
    });
    const status = node('settings-save-status'), back = node('back');
    const root = node('settings-workspace', {dataset: {mobileSplitState: 'list'},querySelector: () => back});
    const group = node('group', {querySelectorAll: () => buttons});
    const form = node('config-form', {
        querySelector: s => s === '#settings-workspace' ? root : nodes.get(s.slice(1)),
        querySelectorAll: s => s.includes('input[name]') ? fields : s === 'input[type="file"]' ? [file]
            : s === '[data-settings-target]' ? buttons : s === '[data-settings-panel]' ? panels
            : s === '.settings-menu-group' ? [group] : [],
    });
    if (locked) panels[0].classList.toggle('hidden', true);
    const settingsMain = node('main-content', {dataset: {page: 'settings'}, querySelector: () => form});
    const profileMain = node('profile-main', {dataset: {page: 'profiles'}});
    let main = settingsMain;
    settingsMain.replaceWith = profileMain.replaceWith = next => {main = next;};
    const ctx = {console, URL, queueMicrotask, setTimeout,
        location: {hash, pathname: '/settings', search: '', origin: 'http://billy.test'},
        history: {state: null, replaceState(_, __, value) {ctx.location.hash = value.startsWith('#') ? value : '';},
            pushState(_, __, url) {ctx.location.pathname = url;ctx.location.hash = '';}},
        document: {title: 'Settings', body: {dataset: {}}, addEventListener(){},
            getElementById: id => id === 'main-content' ? main : id === 'config-form' && main !== settingsMain ? null : nodes.get(id),
            querySelector: () => null, querySelectorAll: () => []},
        scrollTo() {}, scrollY: 0,
        addEventListener(type, fn) {if (!events.has(type)) events.set(type, []);events.get(type).push(fn);},
        MobileSplitView: {
            bindAll() {back.hidden = !mobile || root.dataset.mobileSplitState !== 'detail';},
            showDetail() {root.dataset.mobileSplitState = 'detail';this.bindAll();},
            showList() {root.dataset.mobileSplitState = 'list';this.bindAll();},
        },
        Sections: {collapsible() {}}, UserProfilePanel: {bindUI() {}},
        DOMParser: class {parseFromString() {return {getElementById: () => profileMain, querySelector: () => ({textContent: 'Profiles'})};}},
        fetch: async () => ({ok: true, text: async () => ''}),
        // Any attempt to repopulate an existing draft on reconnect must fail.
        AppConfig: {load() {throw new Error('Draft was reloaded');}},
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync('application/webconfig/static/js/settings-navigation.js', 'utf8'), ctx);
    const nav = ctx.SettingsNavigation;
    nav.init();
    return {ctx, nav, form, fields, file, root, buttons, panels, status, back, events, settingsMain,
        emit: (name, event = {}) => {for (const fn of events.get(name) || []) fn(event);}};
}

test('mobile starts with the full menu; selection/back preserves original fields and dirty categories', () => {
    const h = harness({mobile: true});
    assert.equal(h.root.dataset.mobileSplitState, 'list');
    h.buttons[0].fire('click');
    assert.equal(h.root.dataset.mobileSplitState, 'detail');
    h.fields[0].value = 'secret draft'; h.form.fire('input');
    h.buttons[1].fire('click');
    assert.equal(h.panels[0].hidden, true);
    h.fields[1].value = 'other draft'; h.form.fire('input');
    h.nav.init(); // Idempotent: do not reset the baseline on repeat setup.
    h.buttons[0].fire('click'); h.back.fire('click');
    assert.equal(h.ctx.document.activeElement, h.buttons[0]);
    assert.equal(h.fields[0].value, 'secret draft');
    assert.equal(h.nav.hasDraft(h.form), true);
    assert.match(h.status.textContent, /section-voice, section-network/);
    h.fields[0].value = 'saved-0'; h.form.fire('input');
    assert.equal(h.buttons[0].querySelector('[data-settings-dirty]').hidden, true);
});

test('deep links select the pane; onboarding-locked categories stay inaccessible', () => {
    const h = harness({mobile: true, hash: '#section-software', locked: true});
    assert.equal(h.root.dataset.mobileSplitState, 'detail');
    assert.equal(h.panels[2].hidden, false);
    assert.equal(h.buttons[0].hidden, true);
    assert.equal(h.nav.select('section-voice'), false);
    assert.equal(h.panels[2].hidden, false);
    assert.equal(h.nav.select('missing'), false);
});

test('validation reveals the first failing category without replacing input values', async () => {
    const h = harness({mobile: true});
    h.fields[1].value = '';
    h.form.fire('invalid', h.fields[1]);
    h.form.fire('invalid', h.fields[2]);
    assert.equal(h.panels[1].hidden, false);
    assert.equal(h.root.dataset.mobileSplitState, 'detail');
    assert.equal(h.fields[1].value, '');
    await Promise.resolve();
    h.form.fire('invalid', h.fields[2]);
    assert.equal(h.panels[2].hidden, false);
});

test('save completion clears only submitted edits; later input and failed fields stay dirty', () => {
    const h = harness();
    h.fields[0].value = 'submitted'; h.fields[1].value = 'new zone';
    const submitted = h.nav.snapshot(h.form);
    h.fields[0].value = 'edited during save';
    submitted.delete(h.fields[1]); // A separate timezone operation failed.
    h.nav.markSaved(h.form, submitted);
    assert.equal(h.fields[0].value, 'edited during save');
    assert.equal(h.nav.hasDraft(h.form), true);
    assert.match(h.status.textContent, /section-voice, section-network/);
    h.nav.markSaved(h.form, h.nav.snapshot(h.form));
    assert.equal(h.nav.hasDraft(h.form), false);
});

test('file inputs and in-flight saves also retain the page; reload warning uses no persistent storage', () => {
    const h = harness();
    h.file.files = [{name: 'wake.onnx'}];
    assert.equal(h.nav.hasDraft(h.form), true);
    let warned = false;
    h.emit('beforeunload', {preventDefault() {warned = true;}});
    assert.equal(warned, true);
    h.file.files = []; h.form.dataset.saving = 'true';
    assert.equal(h.nav.hasDraft(h.form), true);
    delete h.form.dataset.saving;
    assert.equal(h.nav.hasDraft(h.form), false);
});

test('app navigation and WebSocket reconnect preserve the mounted draft instead of reinitializing settings', async () => {
    const h = harness();
    vm.runInContext(fs.readFileSync('application/webconfig/static/js/page-router.js', 'utf8'), h.ctx);
    h.fields[0].value = 'not yet saved';
    await h.ctx.MainPageRouter.loadPage('/profiles');
    assert.equal(h.ctx.document.getElementById('config-form'), null);
    await h.ctx.MainPageRouter.loadPage('/settings');
    assert.equal(h.ctx.document.getElementById('config-form'), h.form);
    assert.equal(h.fields[0].value, 'not yet saved');
    h.emit('billy:websocket:disconnected');h.emit('billy:websocket:connected');
    await Promise.resolve();
    assert.equal(h.fields[0].value, 'not yet saved');
    assert.equal(h.nav.hasDraft(h.form), true);
});

test('background configuration refresh does not overwrite a dirty settings form', () => {
    const h = harness();
    vm.runInContext(fs.readFileSync('application/webconfig/static/js/settings-form.js', 'utf8'), h.ctx);
    h.fields[0].value = 'unsaved credential';
    // This must return before touching any controls or starting discovery.
    vm.runInContext('SettingsForm.refreshFromConfig({REALTIME_AI_PROVIDER: "openai"})', h.ctx);
    assert.equal(h.fields[0].value, 'unsaved credential');
    assert.equal(h.nav.hasDraft(h.form), true);
});
