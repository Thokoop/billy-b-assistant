const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync('application/webconfig/static/js/settings-form.js', 'utf8');
function setup() {
    const select = (id, values) => ({id, value: values[0], options: values.map(value => ({value})), dataset: {},
        addEventListener(event, handler) {this[event] = handler;}});
    const elements = {
        REALTIME_AI_PROVIDER: select('REALTIME_AI_PROVIDER', ['openai', 'xai', 'gpt-live']),
        OPENAI_LIVE_MODEL: select('OPENAI_LIVE_MODEL', ['gpt-live-1', 'pinned-live']),
        OPENAI_LIVE_BACKEND_MODEL: select('OPENAI_LIVE_BACKEND_MODEL', ['gpt-6-luna', 'gpt-6-sol']),
    };
    const groups = ['openai', 'openai gpt-live', 'gpt-live', 'xai', 'openai xai'].map(provider => ({
        dataset: {apiProvider: provider}, style: {}, classList: {toggle() {}},
        toggleAttribute(name, hidden) {this[name] = hidden;}
    }));
    const ctx = {window: {}, console, document: {
        getElementById: id => elements[id] || null, querySelectorAll: () => groups,
    }, localStorage: {getItem: () => 'xai', setItem() {}, removeItem() {}}};
    vm.createContext(ctx); vm.runInContext(source, ctx);
    return {form: ctx.window.SettingsForm, elements, groups};
}
test('Live model selections use saved config rather than stale browser choices', () => {
    const {form, elements} = setup();
    form.populateDropdowns({REALTIME_AI_PROVIDER: 'gpt-live', OPENAI_LIVE_MODEL: 'pinned-live', OPENAI_LIVE_BACKEND_MODEL: 'gpt-6-sol'});
    assert.equal(elements.REALTIME_AI_PROVIDER.value, 'gpt-live');
    assert.equal(elements.OPENAI_LIVE_MODEL.value, 'pinned-live');
    assert.equal(elements.OPENAI_LIVE_BACKEND_MODEL.value, 'gpt-6-sol');
});
test('Live shares OpenAI credentials, while model and Realtime controls follow their providers', () => {
    const {form, elements, groups} = setup();
    form.bindApiProviderFields();
    for (const [provider, visible] of [['openai', [true,true,false,false,true]], ['gpt-live', [false,true,true,false,false]], ['xai', [false,false,false,true,true]]]) {
        elements.REALTIME_AI_PROVIDER.value = provider;
        elements.REALTIME_AI_PROVIDER.change();
        assert.deepEqual(groups.map(g => !g.hidden), visible);
    }
});

test('Realtime-only controls hide for Live and reappear without losing values', () => {
    const {form, elements, groups} = setup();
    elements.TURN_EAGERNESS = {id: 'TURN_EAGERNESS', value: 'low'};
    elements.FOLLOW_UP_RETRY_LIMIT = {id: 'FOLLOW_UP_RETRY_LIMIT', value: '4'};
    elements.AEC_BARGE_IN_SNR_DB = {id: 'AEC_BARGE_IN_SNR_DB', value: '12'};
    form.bindApiProviderFields();
    for (const provider of ['gpt-live', 'openai', 'gpt-live', 'xai']) {
        elements.REALTIME_AI_PROVIDER.value = provider;
        elements.REALTIME_AI_PROVIDER.change();
        assert.equal(groups.at(-1).hidden, provider === 'gpt-live');
        assert.equal(groups.at(-1).style.display, provider === 'gpt-live' ? 'none' : '');
        assert.equal(elements.TURN_EAGERNESS.value, 'low');
        assert.equal(elements.FOLLOW_UP_RETRY_LIMIT.value, '4');
        assert.equal(elements.AEC_BARGE_IN_SNR_DB.value, '12');
    }
});

test('Loading or refreshing the selected provider updates the visible controls', () => {
    const {form, elements, groups} = setup();
    form.bindApiProviderFields();
    form.populateDropdowns({REALTIME_AI_PROVIDER: 'gpt-live'});
    assert.equal(groups.at(-1).hidden, true);
    form.refreshFromConfig({REALTIME_AI_PROVIDER: 'openai'});
    assert.equal(groups.at(-1).hidden, false);
    form.refreshFromConfig({REALTIME_AI_PROVIDER: 'gpt-live'});
    assert.equal(groups.at(-1).hidden, true);
    assert.equal(elements.REALTIME_AI_PROVIDER.value, 'gpt-live');
});


test('Mood label follows the provider without resetting the saved setting', () => {
    const {form, elements, groups} = setup();
    const template = fs.readFileSync('application/webconfig/templates/components/settings-form.html', 'utf8');
    const labelGroups = ['mood-live-label', 'mood-realtime-label'].map(id => {
        const match = template.match(new RegExp(`id="${id}" data-api-provider="([^"]+)"`));
        assert.ok(match, `missing provider label ${id}`);
        return {dataset: {apiProvider: match[1]}, style: {}, classList: {toggle() {}},
            toggleAttribute(name, hidden) {this[name] = hidden;}};
    });
    groups.push(...labelGroups);
    elements.MOOD_INSTRUCTIONS_ENABLED = {value: 'false'};
    form.bindApiProviderFields();
    for (const provider of ['gpt-live', 'openai', 'xai', 'gpt-live']) {
        elements.REALTIME_AI_PROVIDER.value = provider;
        elements.REALTIME_AI_PROVIDER.change();
        assert.deepEqual(labelGroups.map(g => !g.hidden), [provider === 'gpt-live', provider !== 'gpt-live']);
        assert.equal(elements.MOOD_INSTRUCTIONS_ENABLED.value, 'false');
    }
});


function captionsUI(stored = null, deviceName = 'Billy') {
    const nodes = {};
    const document = {getElementById: id => nodes[id], activeElement: null, createElement: () => node()};
    function node() {
        return {hidden: false, attrs: {}, dataset: {}, children: [], parentElement: null,
            text: {textContent: ''}, textContent: '', scrollHeight: 0, clientHeight: 0, scrollTop: 0,
            setAttribute(k, v) {this.attrs[k] = v;}, querySelector() {return this.text;},
            addEventListener(k, f) {this[k] = f;}, focus() {document.activeElement = this;},
            append(...children) {
                for (const child of children) {
                    child.remove(); child.parentElement = this; this.children.push(child);
                }
            },
            remove() {
                if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this);
                this.parentElement = null;
            },
            replaceChildren(...children) {
                for (const child of [...this.children]) child.remove();
                this.append(...children);
            },
            prepend(child) {this.append(child); this.children.unshift(this.children.pop());},
            get firstElementChild() {return this.children[0];},
        };
    }
    for (const id of ['toggle-captions-btn', 'live-captions', 'live-captions-panel',
        'live-captions-content', 'live-captions-dialog', 'expand-captions-btn',
        'live-transcript-content', 'live-transcript-turns', 'live-transcript-empty', 'live-captions-title',
        'captions-expand-icon', 'live-captions-empty', 'live-caption-user', 'live-caption-billy']) {
        nodes[id] = node(); nodes[id].hidden = true;
    }
    const panel = nodes['live-captions'];
    panel.dataset.deviceName = deviceName;
    nodes['live-captions-panel'].append(panel);
    panel.append(nodes['live-captions-content'], nodes['live-transcript-content']);
    nodes['live-transcript-content'].append(nodes['live-transcript-turns'], nodes['live-transcript-empty']);
    panel.append(nodes['expand-captions-btn']);
    for (const id of ['live-caption-user', 'live-caption-billy', 'live-captions-empty']) {
        nodes['live-captions-content'].append(nodes[id]);
    }
    const dialog = nodes['live-captions-dialog'];
    dialog.open = false;
    dialog.showModal = () => {dialog.open = true;};
    dialog.close = () => {dialog.open = false; dialog.closeEvent?.();};
    dialog.addEventListener = (k, f) => {dialog[k === 'close' ? 'closeEvent' : k] = f;};
    const handlers = {}, sockets = [], timers = new Map();
    let now = 10000, index = 0;
    const storage = {getItem: () => stored, setItem: (_, value) => {stored = value;}};
    const ctx = {window: {location: {protocol: 'https:', host: 'billy.local'}, addEventListener: (k,f) => {handlers[k]=f;}},
        document, localStorage: storage,
        Date: {now: () => now}, setTimeout: (f, ms) => {timers.set(++index, {f, ms}); return index;}, clearTimeout: id => timers.delete(id),
        WebSocket: class {static OPEN = 1; constructor(url) {this.url=url; this.readyState=1; this.sent=[]; sockets.push(this);} send(message) {this.sent.push(JSON.parse(message));} close() {this.closed=true; this.readyState=3; this.onclose?.();}}};
    vm.runInNewContext(fs.readFileSync('application/webconfig/static/js/live-captions.js', 'utf8'), ctx);
    const status = live => handlers['billy:service:status']({detail: {live_captions: live}});
    const send = (s, text, speaker = 'billy', session='one') => s.onmessage({data: JSON.stringify({session, published: 100, lines: [{speaker, text, updated:100}]})});
    return {nodes, sockets, status, send, handlers, timers, storage, document, advance(ms) {now+=ms;}, get stored() {return stored;}};
}

test('Subtitles default off, appear only for Live and remember the browser choice', () => {
    const h = captionsUI();
    h.status(false);
    assert.equal(h.nodes['toggle-captions-btn'].hidden, true);
    h.status(true);
    assert.equal(h.nodes['toggle-captions-btn'].hidden, false);
    assert.equal(h.sockets.length, 0);
    h.nodes['toggle-captions-btn'].click();
    assert.equal(h.stored, 'true');
    assert.equal(h.nodes['toggle-captions-btn'].attrs['aria-expanded'], 'true');
    assert.equal(h.sockets[0].url, 'wss://billy.local/ws/live-captions');
    assert.equal(h.nodes['live-captions-panel'].hidden, false);
    assert.equal(h.nodes['live-captions'].hidden, false);
    assert.equal(h.nodes['live-captions-dialog'].open, false);
    h.status(true);
    assert.equal(h.sockets.length, 1);
    h.status(false);
    assert.equal(h.sockets[0].closed, true);
    assert.equal(h.nodes['live-captions'].hidden, true);
    assert.equal(h.nodes['live-captions-panel'].hidden, true);
});

test('Subtitles show text safely and clear when toggled off, stopped or disconnected', () => {
    const h = captionsUI('true');
    h.status(true);
    h.send(h.sockets[0], '<script>hi</script>');
    assert.equal(h.nodes['live-caption-billy'].text.textContent, '<script>hi</script>');
    h.sockets[0].onmessage({data: 'null'});
    assert.equal(h.nodes['live-caption-billy'].text.textContent, '');
    h.send(h.sockets[0], 'Hello');
    h.nodes['toggle-captions-btn'].click();
    assert.equal(h.nodes['live-caption-billy'].text.textContent, '');
    h.send(h.sockets[0], 'Late');
    assert.equal(h.nodes['live-caption-billy'].text.textContent, '');
    h.nodes['toggle-captions-btn'].click();
    h.send(h.sockets[1], 'Again');
    h.handlers['billy:websocket:disconnected']();
    assert.equal(h.nodes['live-captions'].hidden, true);
    assert.equal(h.nodes['live-caption-billy'].text.textContent, '');
});

test('Subtitles stay until the next turn, and clear for a new or ended session', () => {
    const h = captionsUI('true'); h.status(true);
    const show = (lines, published = 100, session = 'one') => h.sockets[0].onmessage({data: JSON.stringify({
        session, published, lines: lines.map(([speaker, text, updated = published]) => ({speaker, text, updated})),
    })});
    const visible = () => h.nodes['live-captions-content'].children
        .filter(node => !node.hidden && node !== h.nodes['live-captions-empty'])
        .map(node => [node === h.nodes['live-caption-user'] ? 'user' : 'billy', node.text.textContent]);
    show([['user', 'How was your day?'], ['billy', 'Let me tell you.']]);
    h.advance(60000);
    for (const {f} of [...h.timers.values()]) f();
    assert.deepEqual(visible(), [['user', 'How was your day?'], ['billy', 'Let me tell you.']]);
    // Heartbeats retain older lines instead of expiring the question mid-answer.
    show([['user', 'How was your day?', 100], ['billy', 'Let me tell you. It was busy.']], 160);
    assert.deepEqual(visible(), [['user', 'How was your day?'], ['billy', 'Let me tell you. It was busy.']]);
    show([['billy', 'Let me tell you. It was busy.', 160], ['user', 'And tomorrow?']], 180);
    assert.deepEqual(visible(), [['billy', 'Let me tell you. It was busy.'], ['user', 'And tomorrow?']]);
    show([['user', 'And tomorrow?', 180], ['billy', 'More of the same.']], 185);
    assert.deepEqual(visible(), [['user', 'And tomorrow?'], ['billy', 'More of the same.']]);
    show([['user', 'Hello again.']], 200, 'two');
    assert.deepEqual(visible(), [['user', 'Hello again.']]);
    h.sockets[0].onmessage({data: 'null'}); // Ended or stale session from server.
    assert.deepEqual(visible(), []);
    assert.equal(h.nodes['live-captions-empty'].hidden, false);
});

test('Optional browser storage failures do not break the subtitle button', () => {
    const h = captionsUI(); h.status(true);
    h.storage.setItem = () => {throw Error('blocked');};
    h.nodes['toggle-captions-btn'].click();
    assert.equal(h.sockets.length, 1);
});


test('Subtitle rows follow conversation turns, including delayed words and reconnects', () => {
    const h = captionsUI('true'); h.status(true);
    const show = (lines, session = 'one') => h.sockets.at(-1).onmessage({data: JSON.stringify({
        session, published: 100, lines: lines.map(([speaker, text, updated = 100]) => ({speaker, text, updated})),
    })});
    const visible = () => h.nodes['live-captions-content'].children
        .filter(node => !node.hidden && node !== h.nodes['live-captions-empty'])
        .map(node => [node === h.nodes['live-caption-user'] ? 'user' : 'billy', node.text.textContent]);
    show([['user', 'Hello.'], ['billy', 'Hi!']]);
    assert.deepEqual(visible(), [['user', 'Hello.'], ['billy', 'Hi!']]);
    show([['billy', 'Hi!', 99], ['user', 'Tea?']]);
    assert.deepEqual(visible(), [['billy', 'Hi!'], ['user', 'Tea?']]);
    // The older reply can still receive words after the new speaker starts.
    show([['billy', 'Hi, there!'], ['user', 'Tea?', 99]]);
    assert.deepEqual(visible(), [['billy', 'Hi, there!'], ['user', 'Tea?']]);
    show([['user', 'Tea?'], ['billy', 'Yes, please.']]);
    assert.deepEqual(visible(), [['user', 'Tea?'], ['billy', 'Yes, please.']]);
    show([['billy', 'Yes, please.'], ['user', 'Milk?']]);
    h.sockets[0].onclose();
    for (const {f} of [...h.timers.values()]) f();
    show([['billy', 'Yes, please.'], ['user', 'Milk?']]);
    assert.deepEqual(visible(), [['billy', 'Yes, please.'], ['user', 'Milk?']]);
    show([['user', 'New conversation.']], 'two');
    assert.deepEqual(visible(), [['user', 'New conversation.']]);
});


test('Long Billy captions keep their opening words, then shrink to the next reply', () => {
    const h = captionsUI('true'); h.status(true);
    const reply = 'This is a longer reply that needs more room in the subtitles. '.repeat(6).trim();
    h.send(h.sockets[0], reply);
    assert.equal(h.nodes['live-caption-billy'].text.textContent, reply);
    h.send(h.sockets[0], 'Any time.');
    assert.equal(h.nodes['live-caption-billy'].text.textContent, 'Any time.');
});

test('Caption windows remain bounded without cutting Unicode characters in half', () => {
    const h = captionsUI('true'); h.status(true);
    for (const [speaker, limit] of [['user', 160], ['billy', 512]]) {
        h.send(h.sockets[0], '🐟'.repeat(1000), speaker);
        assert.equal(h.nodes[`live-caption-${speaker}`].text.textContent, '🐟'.repeat(limit));
    }
});


test('Fullscreen keeps the same live view and subscription, then Escape returns to the panel', () => {
    const h = captionsUI('true'); h.status(true);
    const panel = h.nodes['live-captions'], dialog = h.nodes['live-captions-dialog'];
    const expand = h.nodes['expand-captions-btn'];
    h.send(h.sockets[0], 'Before expanding.');
    expand.click();
    assert.equal(dialog.open, true);
    assert.equal(panel.parentElement, dialog);
    assert.equal(h.document.activeElement, expand);
    assert.equal(expand.attrs['aria-expanded'], 'true');
    assert.equal(h.nodes['captions-expand-icon'].textContent, 'fullscreen_exit');
    h.status(true);
    h.send(h.sockets[0], 'Still receiving the same conversation.');
    assert.equal(h.sockets.length, 1);
    assert.equal(h.nodes['live-caption-billy'].text.textContent, 'Still receiving the same conversation.');
    let prevented = false;
    dialog.cancel({preventDefault() {prevented = true;}});
    assert.equal(prevented, true);
    assert.equal(dialog.open, false);
    assert.equal(panel.parentElement, h.nodes['live-captions-panel']);
    assert.equal(h.nodes['toggle-captions-btn'].attrs['aria-expanded'], 'true');
    assert.equal(expand.attrs['aria-expanded'], 'false');
    assert.equal(h.document.activeElement, expand);
    assert.equal(h.sockets[0].closed, undefined);
});

test('Fullscreen exit, backdrop and native close all restore the panel without losing text', () => {
    const h = captionsUI('true'); h.status(true);
    const panel = h.nodes['live-captions'], dialog = h.nodes['live-captions-dialog'];
    const expand = h.nodes['expand-captions-btn'];
    h.send(h.sockets[0], 'Keep these words.');
    for (const close of [() => expand.click(), () => dialog.click({target: dialog}), () => dialog.close()]) {
        expand.click();
        dialog.click({target: panel});
        assert.equal(dialog.open, true, 'clicking text must not close the view');
        close();
        assert.equal(dialog.open, false);
        assert.equal(panel.parentElement, h.nodes['live-captions-panel']);
        assert.equal(h.nodes['live-caption-billy'].text.textContent, 'Keep these words.');
        assert.equal(h.sockets.length, 1);
    }
});

test('Unavailable providers and disconnects close fullscreen and clear old speech', () => {
    for (const end of [h => h.status(false), h => h.handlers['billy:websocket:disconnected']()]) {
        const h = captionsUI('true'); h.status(true);
        h.nodes['expand-captions-btn'].click();
        h.send(h.sockets[0], 'Previous speech.');
        end(h);
        assert.equal(h.nodes['live-captions-dialog'].open, false);
        assert.equal(h.nodes['live-captions'].parentElement, h.nodes['live-captions-panel']);
        assert.equal(h.nodes['live-captions-panel'].hidden, true);
        assert.equal(h.nodes['toggle-captions-btn'].hidden, true);
        assert.equal(h.sockets[0].closed, true);
        assert.equal(h.nodes['live-caption-billy'].text.textContent, '');
        h.status(true);
        assert.equal(h.nodes['live-captions-panel'].hidden, false);
        assert.equal(h.nodes['live-captions-dialog'].open, false);
        assert.equal(h.sockets.length, 2);
        h.send(h.sockets[0], 'Stale message.');
        assert.equal(h.nodes['live-caption-billy'].text.textContent, '');
    }
});

test('Closing subtitles cancels a pending reconnect and never reopens on a late socket event', () => {
    const h = captionsUI('true'); h.status(true);
    h.sockets[0].onclose();
    assert.equal(h.timers.size, 1);
    h.nodes['toggle-captions-btn'].click();
    assert.equal(h.timers.size, 0);
    h.sockets[0].onclose();
    h.status(true);
    assert.equal(h.sockets.length, 1);
    assert.equal(h.nodes['live-captions-panel'].hidden, true);
    assert.equal(h.nodes['toggle-captions-btn'].attrs['aria-expanded'], 'false');
});

test('Resizing or receiving long subtitles follows the newest words in either view', () => {
    const h = captionsUI('true'); h.status(true);
    const content = h.nodes['live-captions-content'];
    content.clientHeight = 100;
    content.scrollHeight = 300;
    h.send(h.sockets[0], 'A long reply.');
    assert.equal(content.scrollTop, 300);
    assert.equal(h.nodes['expand-captions-btn'].dataset.overflow, 'true');
    h.nodes['expand-captions-btn'].click();
    assert.equal(h.nodes['expand-captions-btn'].dataset.overflow, 'false');
    const transcript = h.nodes['live-transcript-content'];
    transcript.scrollHeight = 450;
    h.handlers.resize();
    assert.equal(transcript.scrollTop, 450);
});


function sessionTranscript(h, turns, session = 'one', include = true) {
    const data = {session, published: 100, lines: turns.slice(-2).map(turn => ({...turn, updated:100}))};
    if (include) data.transcript = turns;
    h.sockets.at(-1).onmessage({data:JSON.stringify(data)});
}
const fullTurns = h => h.nodes['live-transcript-turns'].children.map(row => [row.dataset.speaker, row.children[1].textContent]);

test('Fullscreen requests full history and shows unabridged turns without changing compact captions', () => {
    const h=captionsUI('true');h.status(true);h.sockets[0].onopen();
    assert.equal(h.sockets[0].sent.at(-1).view,'captions');
    const long='A full answer, including its opening words. '.repeat(40);
    const turns=[{speaker:'user',text:'First question'},{speaker:'billy',text:'First answer'},
        {speaker:'user',text:'Tell me more'},{speaker:'billy',text:long}];
    sessionTranscript(h,turns,'one',false);
    assert.equal(h.nodes['live-caption-billy'].text.textContent.length,512);
    h.nodes['expand-captions-btn'].click();
    assert.equal(h.sockets[0].sent.at(-1).view,'transcript');
    assert.equal(h.nodes['live-captions-content'].hidden,true);
    assert.equal(h.nodes['live-transcript-content'].hidden,false);
    sessionTranscript(h,turns);
    assert.deepEqual(fullTurns(h),turns.map(turn=>[turn.speaker,turn.text.trim()]));
    assert.equal(h.nodes['live-captions-title'].textContent,'Session transcript');
    assert.equal(h.nodes['live-captions-title'].hidden,false);
    h.nodes['expand-captions-btn'].click();
    assert.equal(h.sockets[0].sent.at(-1).view,'captions');
    assert.equal(h.nodes['live-transcript-content'].hidden,true);
    assert.equal(h.nodes['live-captions-content'].hidden,false);
    assert.equal(h.nodes['live-captions-title'].hidden,true);
    assert.equal(h.nodes['live-caption-billy'].text.textContent.length,512);
});

test('Full transcript updates stable rows safely, preserves readback scroll and resumes at the bottom', () => {
    const h=captionsUI('true');h.status(true);h.nodes['expand-captions-btn'].click();
    const turns=[{speaker:'user',text:'Hello'},{speaker:'billy',text:'Hi'}, {speaker:'user',text:'Tea?'}];
    sessionTranscript(h,turns);
    const first=h.nodes['live-transcript-turns'].children[0];
    const content=h.nodes['live-transcript-content'];
    content.scrollHeight=1000;content.clientHeight=200;content.scrollTop=100;content.scroll();
    turns[1].text='Hi, there!';turns.push({speaker:'billy',text:'<img src=x onerror=alert(1)> Tea!'});
    sessionTranscript(h,turns);
    assert.equal(h.nodes['live-transcript-turns'].children[0],first);
    assert.equal(fullTurns(h)[1][1],'Hi, there!');
    assert.equal(fullTurns(h)[3][1],'<img src=x onerror=alert(1)> Tea!');
    h.handlers.resize();h.status(true);
    assert.equal(content.scrollTop,100,'updates and status must not interrupt reading');
    h.sockets[0].onmessage({data:JSON.stringify({session:'one',lines:[],transcript_error:'unavailable'})});
    assert.equal(fullTurns(h).length,4,'a publication race must not clear this session’s last full snapshot');
    assert.equal(h.nodes['live-transcript-turns'].children[0],first);
    assert.equal(content.scrollTop,100);
    content.scrollTop=800;content.scroll();content.scrollHeight=1200;
    sessionTranscript(h,turns);
    assert.equal(content.scrollTop,1200);
    sessionTranscript(h,turns,'one',false); // Small heartbeat keeps full history intact.
    assert.equal(fullTurns(h).length,4);
});

test('Full history is requested again after reconnect and clears across sessions or unavailable snapshots', () => {
    const h=captionsUI('true');h.status(true);h.nodes['expand-captions-btn'].click();
    const turns=[{speaker:'user',text:'Old conversation'},{speaker:'billy',text:'Old reply'}];
    sessionTranscript(h,turns);
    h.sockets[0].onclose();
    assert.deepEqual(fullTurns(h),[]);
    for(const {f} of [...h.timers.values()])f();
    h.sockets.at(-1).onopen();
    assert.equal(h.sockets.at(-1).sent.at(-1).view,'transcript');
    sessionTranscript(h,turns);
    assert.equal(fullTurns(h).length,2);
    sessionTranscript(h,[{speaker:'user',text:'New conversation'}],'two',false);
    assert.deepEqual(fullTurns(h),[]);
    h.sockets.at(-1).onmessage({data:JSON.stringify({session:'two',lines:[],transcript_error:'unavailable'})});
    assert.match(h.nodes['live-transcript-empty'].textContent,/unavailable/);
    sessionTranscript(h,[{speaker:'user',text:'New conversation'}],'two');
    assert.deepEqual(fullTurns(h),[['user','New conversation']]);
    h.sockets.at(-1).onmessage({data:'null'});
    assert.deepEqual(fullTurns(h),[]);
});


test('Fullscreen speaker labels use the rendered hostname, not the browser address or transcript role', () => {
    for (const name of ['Billy-kitchen', 'BILLY', '<fish>']) {
        const h = captionsUI('true', name); h.status(true);
        h.nodes['expand-captions-btn'].click();
        sessionTranscript(h, [{speaker: 'user', text: 'Hello'}, {speaker: 'billy', text: 'Hi'}]);
        const rows = h.nodes['live-transcript-turns'].children;
        assert.equal(rows[0].children[0].textContent, 'You');
        assert.equal(rows[1].children[0].textContent, name);
        assert.equal(rows[1].dataset.speaker, 'billy');
        h.nodes['expand-captions-btn'].click();
        h.nodes['expand-captions-btn'].click();
        assert.equal(rows[1].children[0].textContent, name);
    }
});
