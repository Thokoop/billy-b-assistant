const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function disclosures() {
    const handlers = [], nodes = new Map();
    const document = {activeElement: null, getElementById: id => nodes.get(id),
        addEventListener: (type, fn, capture) => handlers.push({type, fn, capture})};
    const context = {document, window: {addEventListener() {}}, console};
    vm.runInNewContext(fs.readFileSync('application/webconfig/static/js/ui-helpers.js', 'utf8'), context);
    const click = handlers.find(h => h.type === 'click' && h.capture).fn;
    const keydown = handlers.find(h => h.type === 'keydown').fn;
    function mount(id) {
        const button = {dataset: {disclosureTarget: id}, attrs: {'aria-expanded': 'false'},
            setAttribute(k, v) {this.attrs[k] = v;}, getAttribute(k) {return this.attrs[k];},
            focus() {document.activeElement = this;}};
        const panel = {hidden: true, contains: el => el === link,
            getAttribute: () => id + '-button'};
        const link = {closest: selector => selector === '[data-disclosure-panel]' ? panel : null};
        button.closest = selector => selector === '[data-disclosure-target]' ? button : null;
        nodes.set(id, panel); nodes.set(id + '-button', button);
        return {button, panel, link};
    }
    const event = target => ({target, preventDefault() {this.prevented = true;}, stopPropagation() {this.stopped = true;}});
    return {context, document, mount, event, click, keydown};
}

test('badge clicks disclose only their own panel and do not toggle a surrounding section', () => {
    const h = disclosures(), a = h.mount('update'), b = h.mount('deprecated');
    const event = h.event(a.button); h.click(event);
    assert.equal(a.panel.hidden, false); assert.equal(b.panel.hidden, true);
    assert.equal(a.button.attrs['aria-expanded'], 'true');
    assert.equal(event.stopped, true); assert.equal(event.prevented, true);
    h.click(h.event(a.button)); assert.equal(a.panel.hidden, true);
    // Delegation also works on markup added after initial page setup.
    const later = h.mount('beta'); h.click(h.event(later.button));
    assert.equal(later.panel.hidden, false);
});

test('Escape and withdrawn update badges restore focus without swallowing unrelated keys or links', () => {
    const h = disclosures(), a = h.mount('update'); h.click(h.event(a.button));
    h.document.activeElement = a.link;
    const linkClick = h.event(a.link); h.click(linkClick); assert.equal(linkClick.prevented, undefined);
    const tab = {...h.event(a.link), key: 'Tab'}; h.keydown(tab);
    assert.equal(tab.prevented, undefined); assert.equal(a.panel.hidden, false);
    h.keydown({...h.event(a.link), key: 'Escape'});
    assert.equal(a.panel.hidden, true); assert.equal(h.document.activeElement, a.button);
    h.click(h.event(a.button)); h.document.activeElement = a.link;
    h.context.window.InlineDisclosure.close(a.button);
    assert.equal(a.panel.hidden, true); assert.equal(h.document.activeElement, a.button);
    h.context.window.InlineDisclosure.close({dataset: {}}); // Header's noninteractive indicator.
});

const flush = async () => {for (let i = 0; i < 20; i++) await Promise.resolve();};
test('the update badge links only to the matching GitHub release and clears stale or unsafe links', async () => {
    let note = {tag: 'v2.7.0', body: 'Release notes', url: 'https://github.com/Thokoop/billy-b-assistant/releases/tag/v2.7.0'};
    const link = {hidden: true, removeAttribute(name) {delete this[name];}};
    const events = new Map();
    const ctx = {console, setTimeout() {}, clearTimeout() {},
        CustomEvent: class {constructor(type) {this.type = type;}},
        document: {getElementById: id => id === 'software-update-release-link' ? link : null,
            querySelectorAll: () => [], addEventListener: (type, f) => events.set(type, f),
            dispatchEvent: e => events.get(e.type)?.()},
        addEventListener() {}, BootstrapData: {take: async key => key === 'version'
            ? {current: 'v2.6.0', latest: 'v2.7.0', update_available: true} : note}};
    ctx.window = ctx; vm.createContext(ctx);
    vm.runInContext(fs.readFileSync('application/webconfig/static/js/version-release.js', 'utf8'), ctx);
    await flush(); await ctx.ReleaseNotes.init();
    assert.equal(link.hidden, false); assert.equal(link.href, note.url);
    ctx.BillyVersionInfo.latest = 'v2.8.0'; events.get('billy:version-info-updated')();
    assert.equal(link.hidden, true); assert.equal(link.href, undefined);
    for (const url of ['javascript:alert(1)', 'https://github.com.evil.test/o/r/releases/tag/v2.8.0', '']) {
        note = {tag: 'v2.8.0', body: 'Notes', url}; ctx.ReleaseNotes.reload(); await flush();
        assert.equal(link.hidden, true); assert.equal(link.href, undefined);
    }
    note = {tag: 'v2.8.0', body: 'Notes', url: 'https://github.com/Thokoop/billy-b-assistant/releases/tag/v2.8.0'};
    ctx.ReleaseNotes.reload(); await flush(); assert.equal(link.hidden, false); assert.equal(link.href, note.url);
});
