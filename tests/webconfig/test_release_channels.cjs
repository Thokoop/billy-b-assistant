const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync('application/webconfig/static/js/settings-form.js', 'utf8');

function setup() {
    const warning = {hidden: true, open: false};
    const channel = {id: 'UPDATE_CHANNEL', value: 'beta', dataset: {}, attributes: {}, addEventListener(type, handler) { this[type] = handler; }, setAttribute(key, value) { this.attributes[key] = value; }, removeAttribute(key) { delete this.attributes[key]; }};
    channel.options = ['beta', 'rc', 'stable'].map(value => ({value, text: value}));
    Object.defineProperty(channel, 'selectedIndex', {get() { return this.options.findIndex(o => o.value === this.value); }});
    const bar = {attributes: {}, setAttribute(key, value) { this.attributes[key] = value; }, removeAttribute(key) { delete this.attributes[key]; }};
    const fill = {style: {}, dataset: {}}, label = {};
    const slider = {'UPDATE_CHANNEL-bar': bar, 'UPDATE_CHANNEL-fill': fill, 'UPDATE_CHANNEL-label': label};
    const ctx = {
        window: {}, console,
        document: {getElementById: id => id === 'UPDATE_CHANNEL' ? channel : id === 'update-channel-warning' ? warning : slider[id] || null},
        localStorage: {getItem: () => 'beta', setItem() {}, removeItem() {}},
    };
    vm.createContext(ctx);
    vm.runInContext(source, ctx);
    return {ctx, channel, warning, bar, fill, label, form: vm.runInContext('SettingsForm', ctx)};
}

test('dropdown migrates old RC values and ignores another unit’s localStorage', () => {
    const h = setup();
    for (const [cfg, expected] of [
        [{}, 'stable'],
        [{SHOW_RC_VERSIONS: 'False'}, 'stable'],
        [{SHOW_RC_VERSIONS: true}, 'rc'],
        [{SHOW_RC_VERSIONS: ' TRUE '}, 'rc'],
        [{UPDATE_CHANNEL: 'stable', SHOW_RC_VERSIONS: 'True'}, 'stable'],
        [{UPDATE_CHANNEL: 'rc', SHOW_RC_VERSIONS: 'False'}, 'rc'],
        [{UPDATE_CHANNEL: 'beta'}, 'beta'],
        [{UPDATE_CHANNEL: 'invalid', SHOW_RC_VERSIONS: 'True'}, 'stable'],
    ]) {
        h.form.populateDropdowns(cfg);
        assert.equal(h.channel.value, expected);
        assert.equal(h.fill.style.width, {beta: '0%', rc: '50%', stable: '100%'}[expected]);
        assert.equal(h.bar.attributes['aria-valuetext'], expected);
        assert.equal(h.label.textContent, expected);
    }
});


test('preview warning follows saved and changed channel, closing when Stable is selected', () => {
    const h = setup();
    h.form.populateDropdowns({SHOW_RC_VERSIONS: true});
    assert.equal(h.warning.hidden, false);
    assert.equal(h.channel.attributes['aria-describedby'], 'update-channel-warning-text');
    assert.equal(h.bar.attributes['aria-describedby'], 'update-channel-warning-text');
    const handler = h.channel.change;
    h.form.populateDropdowns({UPDATE_CHANNEL: 'stable'});
    assert.equal(h.warning.hidden, true);
    assert.equal(h.channel.change, handler);
    for (const value of ['rc', 'beta']) {
        h.channel.value = value;
        h.channel.change();
        assert.equal(h.warning.hidden, false);
        h.warning.open = true;
        h.channel.value = 'stable';
        h.channel.change();
        assert.equal(h.warning.hidden, true);
        assert.equal(h.warning.open, false);
        assert.equal(h.channel.attributes['aria-describedby'], undefined);
        assert.equal(h.bar.attributes['aria-describedby'], undefined);
    }
});
