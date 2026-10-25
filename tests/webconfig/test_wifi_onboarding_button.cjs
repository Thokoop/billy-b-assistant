const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const source = fs.readFileSync('application/webconfig/static/js/settings-form.js', 'utf8');

function element(id, initialClasses = []) {
    const classes = new Set(initialClasses);
    const handlers = {};
    let html = '';
    const el = {
        id, dataset: {}, attributes: {}, value: '', textContent: '', disabled: false, hidden: false,
        children: [], parent: null, options: [], selectedIndex: 0,
        get className() { return [...classes].join(' '); },
        set className(value) { classes.clear(); String(value).split(/\s+/).filter(Boolean).forEach(name => classes.add(name)); },
        get innerHTML() { return html; },
        set innerHTML(value) { html = value; el.children = []; },
        classList: {
            add: (...names) => names.forEach(name => classes.add(name)),
            remove: (...names) => names.forEach(name => classes.delete(name)),
            contains: name => classes.has(name),
            toggle(name, on = !classes.has(name)) { if (on) classes.add(name); else classes.delete(name); return on; },
        },
        addEventListener(type, handler) { (handlers[type] ||= []).push(handler); },
        fire(type, event = {}) {
            const full = {target: el, preventDefault() { this.defaultPrevented = true; }, ...event};
            for (const handler of handlers[type] || []) handler(full);
            return full;
        },
        click() { return el.fire('click'); },
        setAttribute(key, value) { el.attributes[key] = value; },
        removeAttribute(key) { delete el.attributes[key]; },
        appendChild(child) { child.parent = el; el.children.push(child); },
        append(...nodes) { nodes.forEach(node => el.appendChild(node)); },
        closest(selector) {
            const name = selector.replace(/^\./, '');
            for (let node = el; node; node = node.parent) if (node.classList.contains(name)) return node;
            return null;
        },
        get text() { return el.textContent + el.children.map(child => child.text).join(''); },
        querySelectorAll: () => [],
        querySelector: () => null,
        focus() { focused.current = el; },
        blur() { if (focused.current === el) focused.current = null; },
    };
    return el;
}

const focused = {current: null};

async function setup({onboarding, networks = [], saveReply = {ok: true, ssid: 'Home'}, steps = false} = {}) {
    const ids = [
        'config-form', 'wifi-setup-modal', 'close-wifi-setup-modal', 'wifi-setup-modal-description',
        'wifi-scan-btn', 'wifi-save-btn', 'wifi-save-label', 'wifi-save-icon', 'wifi-network-list',
        'wifi-ssid', 'wifi-ssid-field', 'wifi-password', 'wifi-country-modal', 'wifi-unified-fields',
        'section-internet', 'wifi-connection-status', 'wifi-test-result', 'wifi-error-result',
        'wifi-onboarding-banner', 'wifi-edit-toggle-label',
    ];
    const nodes = Object.fromEntries(ids.map(id => [id, element(id)]));
    for (const id of ['wifi-ssid', 'wifi-password']) nodes[id].tagName = 'INPUT';
    focused.current = null;
    nodes['wifi-setup-modal'].classList.add('hidden');
    const icon = element('edit-icon', ['material-icons']);
    icon.textContent = 'edit';
    const button = element('wifi-edit-toggle-btn', ['secondary-action', 'secondary-action--hover--cyan', 'button-reveal']);
    button.querySelector = selector => selector === '.material-icons' ? icon : null;
    nodes['wifi-edit-toggle-btn'] = button;
    nodes['wifi-edit-toggle-label'].classList.add('button-reveal-label');
    nodes['config-form'].dataset.wifiOnboardingActive = onboarding ? 'true' : 'false';
    nodes['config-form'].dataset.wifiOnboardingMode = 'unified';
    const html = element('html');
    const body = element('body');
    for (const id of ['wifi-ssid-next', 'wifi-password-back', 'wifi-password-next', 'wifi-password-hint',
        'wifi-connect-back', 'wifi-summary-password', 'wifi-summary-country']) {
        if (steps) nodes[id] = element(id);
    }
    const panels = steps ? ['network', 'password', 'connect'].map(name => {
        const panel = element('wifi-step-' + name);
        panel.dataset.wifiStep = name;
        panel.hidden = name !== 'network';
        return panel;
    }) : [];
    const indicators = steps ? ['network', 'password', 'connect'].map(name => {
        const indicator = element('indicator-' + name);
        indicator.dataset.wifiStepIndicator = name;
        return indicator;
    }) : [];
    const selectedSsid = [element('selected-ssid-1'), element('selected-ssid-2')];
    nodes['wifi-setup-modal'].querySelectorAll = selector => selector === '[data-wifi-step]'
        ? panels : selector === '[data-wifi-step-indicator]' ? indicators : [];
    const country = nodes['wifi-country-modal'];
    country.value = 'NL';
    country.options = [{value: 'NL', textContent: 'Netherlands (NL)'}];
    const saves = [];
    const ctx = {
        window: {location: {hostname: '10.42.0.1', port: '', href: ''}}, console, setTimeout, clearTimeout,
        document: {
            getElementById: id => nodes[id] || null,
            querySelectorAll: selector => selector === '[data-wifi-selected-ssid]' ? (steps ? selectedSsid : []) : [],
            createElement: tag => element(tag),
            get activeElement() { return focused.current; },
            documentElement: html,
            body,
        },
        localStorage: {getItem: () => null, setItem() {}, removeItem() {}},
        showNotification() {},
        fetch: async (url, options) => {
            if (url === '/wifi/save') saves.push(JSON.parse(options.body));
            const reply = url === '/wifi/status'
                ? {connected: onboarding, ssid: onboarding ? 'Billy-Onboarding-Hotspot' : 'Home', hotspot_active: onboarding, country: 'NL'}
                : url === '/wifi/save' ? saveReply : {networks};
            return {ok: reply.ok !== false, json: async () => reply};
        },
    };
    vm.createContext(ctx);
    vm.runInContext(source, ctx);
    vm.runInContext('SettingsForm', ctx).bindWiFiSection();
    await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));
    const list = nodes['wifi-network-list'];
    const item = name => list.children.find(child => child.dataset.ssid === name);
    const visible = () => panels.filter(panel => !panel.hidden).map(panel => panel.dataset.wifiStep);
    const tap = node => list.fire('click', {target: node.children[0] || node});
    return {nodes, button, icon, label: nodes['wifi-edit-toggle-label'], modal: nodes['wifi-setup-modal'],
        list, item, visible, tap, saves, indicators, selectedSsid,
        error: () => nodes['wifi-error-result'].classList.contains('hidden') ? '' : nodes['wifi-error-result'].textContent};
}

test('unfinished Wi-Fi setup shows a labelled primary connect button', async () => {
    const h = await setup({onboarding: true});
    assert.equal(h.label.textContent, 'Connect to Wi-Fi');
    assert.ok(h.button.classList.contains('primary-action'));
    assert.ok(!h.button.classList.contains('button-reveal'));
    assert.ok(!h.label.classList.contains('button-reveal-label'));
    assert.equal(h.icon.textContent, 'wifi');
});

test('closing the setup dialog during onboarding leaves a way to reopen it', async () => {
    const h = await setup({onboarding: true});
    assert.ok(!h.modal.classList.contains('hidden'), 'setup dialog opens automatically');
    h.nodes['close-wifi-setup-modal'].click();
    assert.ok(h.modal.classList.contains('hidden'));
    assert.ok(!h.button.classList.contains('hidden'));
    h.button.click();
    assert.ok(!h.modal.classList.contains('hidden'));
});

test('a connected Billy keeps the compact edit connection button', async () => {
    const h = await setup({onboarding: false});
    assert.equal(h.label.textContent, 'Edit connection');
    assert.ok(h.button.classList.contains('secondary-action'));
    assert.ok(h.button.classList.contains('button-reveal'));
    assert.ok(!h.button.classList.contains('primary-action'));
    assert.ok(!h.button.classList.contains('hidden'));
});

const NETWORKS = [
    {ssid: 'Home', signal: 80, security: 'WPA2'},
    {ssid: 'Cafe', signal: 40, security: ''},
    {ssid: '<img src=x onerror=alert(1)>', signal: 30, security: 'WPA2'},
];

test('a protected network goes network, password, connect and saves what was entered', async () => {
    const h = await setup({onboarding: true, steps: true, networks: NETWORKS});
    assert.deepEqual(h.visible(), ['network']);
    h.tap(h.item('Home'));
    assert.deepEqual(h.visible(), ['password']);
    assert.equal(focused.current, h.nodes['wifi-password']);
    assert.equal(h.selectedSsid[0].textContent, 'Home');
    h.nodes['wifi-password-next'].click();
    assert.match(h.error(), /password/);
    assert.deepEqual(h.visible(), ['password']);
    h.nodes['wifi-password'].value = 'correct horse';
    assert.equal(h.nodes['wifi-password'].fire('keydown', {key: 'Enter'}).defaultPrevented, true);
    assert.deepEqual(h.visible(), ['connect']);
    assert.equal(h.error(), '');
    assert.equal(h.nodes['wifi-summary-password'].textContent, 'Entered');
    assert.equal(h.nodes['wifi-summary-country'].textContent, 'Netherlands (NL)');
    assert.equal(h.indicators[2].attributes['aria-current'], 'step');
    assert.equal(h.indicators[0].dataset.done, 'true');
    await h.nodes['wifi-save-btn'].click();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(h.saves, [{ssid: 'Home', password: 'correct horse', country: 'NL'}]);
});

test('an open network skips the password step and back returns to the list', async () => {
    const h = await setup({onboarding: true, steps: true, networks: NETWORKS});
    h.tap(h.item('Home'));
    h.nodes['wifi-password'].value = 'for another network';
    h.nodes['wifi-password-back'].click();
    h.tap(h.item('Cafe'));
    assert.deepEqual(h.visible(), ['connect']);
    assert.equal(h.nodes['wifi-password'].value, '');
    assert.equal(h.nodes['wifi-summary-password'].textContent, 'None (open network)');
    h.nodes['wifi-connect-back'].click();
    assert.deepEqual(h.visible(), ['network']);
});

test('a manual network name is asked for first and its password may stay empty', async () => {
    const h = await setup({onboarding: true, steps: true, networks: NETWORKS});
    const manual = h.item('__manual__');
    assert.match(manual.innerHTML, /Enter network name manually/);
    h.tap(manual);
    assert.deepEqual(h.visible(), ['network']);
    assert.equal(h.nodes['wifi-ssid-field'].hidden, false);
    assert.equal(h.nodes['wifi-ssid'].disabled, false);
    h.nodes['wifi-ssid-next'].click();
    assert.match(h.error(), /network name/);
    h.nodes['wifi-ssid'].value = 'Hidden SSID';
    h.nodes['wifi-ssid'].fire('keydown', {key: 'Enter'});
    assert.deepEqual(h.visible(), ['password']);
    assert.equal(h.nodes['wifi-password-hint'].hidden, false);
    h.nodes['wifi-password-next'].click();
    assert.deepEqual(h.visible(), ['connect']);
    assert.equal(h.selectedSsid[1].textContent, 'Hidden SSID');
});

test('a rejected password returns to the password step with the reason', async () => {
    const h = await setup({onboarding: true, steps: true, networks: NETWORKS,
        saveReply: {ok: false, error: 'Billy could not join this Wi-Fi network. Check the password and try again.'}});
    h.tap(h.item('Home'));
    h.nodes['wifi-password'].value = 'wrong';
    h.nodes['wifi-password-next'].click();
    await h.nodes['wifi-save-btn'].click();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(h.visible(), ['password']);
    assert.match(h.error(), /Check the password/);
});

test('network names are shown as text, never parsed as markup', async () => {
    const h = await setup({onboarding: true, steps: true, networks: NETWORKS});
    const hostile = h.item('<img src=x onerror=alert(1)>');
    assert.equal(hostile.innerHTML, '');
    assert.match(hostile.text, /<img src=x onerror=alert\(1\)>/);
});

test('steps that need no typing leave the phone keyboard closed', async () => {
    const page = element('timezone');
    page.tagName = 'INPUT';
    page.focus(); // a field on the settings page behind the dialog
    const h = await setup({onboarding: true, steps: true, networks: NETWORKS});
    assert.equal(focused.current, null, 'opening on the network list drops the page field');
    h.tap(h.item('Home'));
    assert.equal(focused.current, h.nodes['wifi-password']);
    h.nodes['wifi-password-back'].click();
    assert.equal(focused.current, null, 'back to the list closes the keyboard');
    h.tap(h.item('Home'));
    h.nodes['wifi-password'].value = 'secret';
    h.nodes['wifi-password-next'].click();
    assert.equal(focused.current, null, 'the connect step has nothing to type');
    h.tap(h.item('__manual__'));
    assert.equal(focused.current, h.nodes['wifi-ssid']);
});
