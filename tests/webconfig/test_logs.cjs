const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync('application/webconfig/static/js/log-panel.js', 'utf8');
const socketSource = fs.readFileSync('application/webconfig/static/js/websocket.js', 'utf8');
const flush = async () => { for (let i=0; i<20; i++) await Promise.resolve(); };
function setup() {
    const nodes = new Map(), events = new Map(), intervals = new Map(), subscriptions = [], requests = [];
    let counter = 0, live = false;
    function node(id) {
        if (!nodes.has(id)) {
            const listeners = {}, classes = new Set();
            nodes.set(id, {value: 'billy', textContent:'', dataset:{}, hidden:false, title:'',
                classList:{contains:c=>classes.has(c),add:c=>classes.add(c),remove:c=>classes.delete(c),toggle(c,b){if(b??!classes.has(c)) classes.add(c);else classes.delete(c);}},
                addEventListener:(e,fn)=>listeners[e]=fn,
                dispatch:(e)=>listeners[e]?.({target:node(id),stopPropagation(){}}),
                contains:()=>false,scrollIntoView(){},
            });
        }
        return nodes.get(id);
    }
    ['toggle-log-btn','log-panel','log-output','log-container','log-source-select','log-error','clear-logs-btn'].forEach(node);
    const ctx = {console,AbortController,
        document:{hidden:false,readyState:'loading',getElementById:id=>nodes.get(id)||null,addEventListener:(e,fn)=>events.set(e,fn)},
        localStorage:{getItem(){return null;}},requestAnimationFrame:fn=>fn(),
        setTimeout:()=>++counter,clearTimeout(){},
        setInterval:fn=>{const id=++counter;intervals.set(id,fn);return id;},clearInterval:id=>intervals.delete(id),
        addEventListener(){},dispatchEvent(){},CustomEvent:class{constructor(type){this.type=type;}},
        location:{protocol:'http:',host:'billy.local'},
        fetch:async(url,opts)=>{requests.push({url,opts});return {ok:true,json:async()=>({logs:'first\n'})};},
        billyWebSocket:{subscribeLogs:s=>subscriptions.push(s),hasLogStream:()=>live},
    };
    ctx.window=ctx;vm.createContext(ctx);vm.runInContext(source,ctx);ctx.LogPanel.bindUI({});
    return {ctx,node,nodes,events,requests,intervals,subscriptions,set live(v){live=v;},open:()=>ctx.LogPanel.openLogPanel(),switch(s){node('log-source-select').value=s;node('log-source-select').dispatch('change');},tick(){for(const fn of intervals.values())fn();}};
}
test('only an open, visible panel fetches logs; close and tab hiding unsubscribe',async()=>{
    const h=setup();await h.ctx.LogPanel.fetchLogs();assert.equal(h.requests.length,0);
    h.open();await flush();assert.equal(h.subscriptions.at(-1),'billy');assert.equal(h.requests.length,1);
    h.live=true;h.tick();await flush();assert.equal(h.requests.length,1);
    h.ctx.document.hidden=true;h.events.get('visibilitychange')();assert.equal(h.intervals.size,0);assert.equal(h.subscriptions.at(-1),null);
    h.ctx.document.hidden=false;h.events.get('visibilitychange')();await flush();assert.equal(h.requests.length,2);
    h.node('toggle-log-btn').dispatch('click');assert.equal(h.intervals.size,0);assert.equal(h.subscriptions.at(-1),null);
});
test('switching source ignores old HTTP and socket snapshots, including markup',async()=>{
    const h=setup();let answer;
    h.ctx.fetch=()=>new Promise(resolve=>answer=resolve);
    h.open();const old=answer;h.switch('pipewire');const current=answer;
    h.ctx.updateLogs('wrong source','billy');assert.equal(h.node('log-output').textContent,'');
    old({ok:true,json:async()=>({logs:'stale'})});await flush();assert.equal(h.node('log-output').textContent,'');
    current({ok:true,json:async()=>({source:'pipewire',logs:'<script>plain text</script>\n'})});await flush();
    assert.equal(h.node('log-output').textContent,'<script>plain text</script>\n');
    h.ctx.updateLogs('next\n','pipewire','Journal access denied');assert.equal(h.node('log-error').hidden,false);
    h.ctx.updateLogs('next\n','pipewire','');assert.equal(h.node('log-error').hidden,true);
});
test('clear view keeps journal untouched; unchanged and overlapping snapshots do not duplicate',async()=>{
    const h=setup();h.open();await flush();
    h.ctx.updateLogs('first\nsecond\n');h.ctx.updateLogs('first\nsecond\n');
    assert.equal(h.node('log-output').textContent,'first\nsecond\n');
    h.node('clear-logs-btn').dispatch('click');assert.equal(h.node('log-output').textContent,'');
    h.ctx.updateLogs('first\nsecond\n');assert.equal(h.node('log-output').textContent,'');
    h.ctx.updateLogs('second\nthird\n');assert.equal(h.node('log-output').textContent,'third\n');
    assert.ok(h.requests.every(r=>!r.opts.method));
});
test('late HTTP cannot overwrite a fresher WebSocket snapshot',async()=>{
    const h=setup();let answer;h.ctx.fetch=()=>new Promise(resolve=>answer=resolve);h.open();
    h.ctx.updateLogs('fresh\n');answer({ok:true,json:async()=>({logs:'old\n'})});await flush();
    assert.equal(h.node('log-output').textContent,'fresh\n');
});
test('HTTP fallback retries after errors and bounds displayed log memory',async()=>{
    const h=setup();h.ctx.fetch=async()=>{throw Error('offline');};h.open();await flush();
    assert.match(h.node('log-error').textContent,/reconnect/);
    h.ctx.fetch=async()=>({ok:true,json:async()=>({logs:'x'.repeat(400100)})});h.tick();await flush();
    assert.equal(h.node('log-output').textContent.length,400000);assert.equal(h.node('log-error').hidden,true);
});
test('socket remembers source through reconnect; received source controls fallback',()=>{
    const h=setup(), sockets=[];
    h.ctx.WebSocket=class{static OPEN=1;constructor(){this.readyState=1;this.sent=[];sockets.push(this);}send(data){this.sent.push(JSON.parse(data));}};
    vm.runInContext(socketSource,h.ctx);h.events.get('DOMContentLoaded')();
    const socket=sockets[0];socket.onopen();assert.equal(socket.sent[0].source,null);
    h.ctx.billyWebSocket.subscribeLogs('pipewire');assert.equal(h.ctx.billyWebSocket.hasLogStream('pipewire'),false);
    socket.onmessage({data:JSON.stringify({type:'logs',source:'pipewire',data:'pw'})});assert.equal(h.ctx.billyWebSocket.hasLogStream('pipewire'),true);
    socket.readyState=3;socket.onclose();assert.equal(h.ctx.billyWebSocket.hasLogStream('pipewire'),false);
    h.ctx.billyWebSocket.connect();sockets[1].onopen();assert.equal(sockets[1].sent[0].source,'pipewire');
});

test('old backend responses cannot be shown as PipeWire logs',async()=>{
    const h=setup();h.open();await flush();h.switch('pipewire');await flush();
    assert.equal(h.node('log-output').textContent,'');
    assert.match(h.node('log-error').textContent,/does not provide/);
});
