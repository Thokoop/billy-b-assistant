const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const helper = fs.readFileSync('application/webconfig/static/js/ui-helpers.js', 'utf8');
const service = fs.readFileSync('application/webconfig/static/js/service-status.js', 'utf8');
const recovery = fs.readFileSync('application/webconfig/templates/components/script-recovery.html', 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];
const flush = async () => { for(let i=0;i<20;i++) await Promise.resolve(); };
function setup(storage = new Map()) {
    let now = 100000, next = 1, reloads = 0, stops = 0;
    const timers = new Map(), events = new Map(), elements = new Map();
    const makeElement = () => ({textContent:'', children:[], style:{}, setAttribute(k,v){this[k]=v;}, classList:{add(){},remove(){},toggle(){},contains(){return false;}}, appendChild(el){this.children.push(el);}, querySelector(){return makeElement();}});
    const context = { console:{log(){},error(){}}, AbortController,
        Date: {now:()=>now},
        sessionStorage:{getItem:k=>storage.get(k)||null, setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
        document:{addEventListener(){},getElementById(id){if(!elements.has(id)) elements.set(id,makeElement());return elements.get(id);},createElement:makeElement},
        CustomEvent:class {constructor(type){this.type=type;}},
        setTimeout(fn, ms){const id=next++;timers.set(id,{fn,at:now+ms});return id;},
        clearTimeout(id){timers.delete(id);},
        fetch:async()=>({ok:true,json:async()=>({status:'ok',webconfig_instance:'new',billy_service:'active',billy_ready_at:101})}),
        location:{reload(){reloads++;}}, stop(){stops++;},
        addEventListener(type,fn){if(!events.has(type)) events.set(type,new Set());events.get(type).add(fn);},
        removeEventListener(type,fn){events.get(type)?.delete(fn);},
        dispatchEvent(event){for(const fn of events.get(event.type)||[])fn(event);},
    };
    context.window=context;
    vm.createContext(context);
    const run = src => vm.runInContext(src,context);
    async function advance(ms) {
        const end=now+ms;
        await flush();
        while(true){
            const due=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
            if(!due) break;
            now=due[1].at; timers.delete(due[0]); due[1].fn(); await flush();
        }
        now=end; await flush();
    }
    return {context,run,advance,storage,elements,get reloads(){return reloads;},get stops(){return stops;}};
}
function hanging(signal) {return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted'))));}
test('JSON deadline covers a body stalled after headers', async()=>{
    const h=setup();h.run(helper);
    h.context.fetch=async(_,options)=>({ok:true,json:()=>hanging(options.signal)});
    const request=h.context.fetchJsonWithTimeout('/health',2500);
    const rejected=assert.rejects(request,/aborted/);
    await h.advance(2500);await rejected;
});
test('restart POST timeout starts health polling, without resending POST', async()=>{
    const h=setup();h.run(helper);h.run(service);
    const calls=[];
    h.context.fetch=async(url,options)=>{
        calls.push(url);
        if(url==='/restart')return hanging(options.signal);
        return {ok:true,json:async()=>({status:'ok',webconfig_instance:'new',billy_service:'active',billy_ready_at:101})};
    };
    const action=h.context.ServiceStatus.handleServiceAction('restart');
    await h.advance(0);
    h.context.dispatchEvent({type:'billy:websocket:connected'});
    await h.advance(200);assert.equal(h.reloads,0,'early old socket must not reload');
    await h.advance(2300);await action;
    await h.advance(250);assert.ok(calls.includes('/health'));
    assert.equal(calls.filter(u=>u==='/restart').length,1);
});
test('new interface waits for non-null readiness, stale instance does not count as recovered',async()=>{
    const h=setup();h.run(helper);h.storage.set('billy:reload_on_ws_reconnect','1');
    let id='old',ready=null;
    h.context.fetch=async()=>({ok:true,json:async()=>({status:'ok',webconfig_instance:id,billy_service:'active',billy_ready_at:ready})});
    h.context.LoadingOverlay.waitForReload('old',0,60000,20000,100);
    h.context.dispatchEvent({type:'billy:restart-unavailable'});
    await h.advance(700);assert.equal(h.reloads,0);
    id='new';await h.advance(700);assert.equal(h.reloads,0,'active Type=simple is not readiness');
    h.context.dispatchEvent({type:'billy:websocket:connected'});
    await h.advance(700);assert.equal(h.reloads,0,'socket cannot bypass ready marker');
    ready=99;await h.advance(700);assert.equal(h.reloads,0,'old readiness marker');
    ready=101;await h.advance(550);assert.equal(h.reloads,1);
});
test('health body timeout resumes polling',async()=>{
    const h=setup();h.run(helper);let calls=0;
    h.context.fetch=async(_,options)=>++calls===1
        ? {ok:true,json:()=>hanging(options.signal)}
        : {ok:true,json:async()=>({status:'ok',webconfig_instance:'new',billy_service:'failed',billy_ready_at:null})};
    h.context.LoadingOverlay.waitForReload('old',0);
    await h.advance(3050);assert.equal(calls,2);assert.equal(h.reloads,1);
});
test('legacy health without marker and failed Billy still allow web UI recovery',async()=>{
    for(const data of [
        {status:'ok',webconfig_instance:'new',billy_service:'active'},
        {status:'ok',webconfig_instance:'new',billy_service:'failed',billy_ready_at:null},
    ]) {
        const h=setup();h.run(helper);h.context.fetch=async()=>({ok:true,json:async()=>data});
        h.context.LoadingOverlay.waitForReload('old',0);
        await h.advance(200);assert.equal(h.reloads,1);
    }
});
test('offline restart terminates with feedback and clears restart flag',async()=>{
    const h=setup();h.run(helper);h.run(service);
    h.storage.set('billy:reload_on_ws_reconnect','1');
    h.context.fetch=async()=>{throw new Error('offline');};
    h.context.LoadingOverlay.waitForReload('old',0,1000);
    await h.advance(1050);assert.equal(h.reloads,0);
    assert.equal(h.storage.has('billy:reload_on_ws_reconnect'),false);
    assert.match(h.elements.get('notification').textContent,/longer than expected/);
    // A later software update can still use the socket reconnect fallback.
    h.storage.set('billy:reload_on_ws_reconnect','1');
    h.context.dispatchEvent({type:'billy:websocket:connected'});
    await h.advance(200);assert.equal(h.reloads,1);
});
test('other update flows keep their socket reconnect fallback',async()=>{
    const h=setup();h.run(helper);h.storage.set('billy:reload_on_ws_reconnect','1');
    h.context.dispatchEvent({type:'billy:websocket:connected'});
    await h.advance(200);assert.equal(h.reloads,1);
});
test('stalled script load retries after 10s and stops after two attempts',async()=>{
    const storage=new Map();
    for(let i=0;i<3;i++){
        const h=setup(storage);h.run(recovery);
        await h.advance(9999);assert.equal(h.reloads,0);
        await h.advance(1);assert.equal(h.reloads,i<2?1:0);assert.equal(h.stops,1);
        if(i===2){
            const text=h.elements.get('loading-overlay-text');assert.match(text.textContent,/could not finish/);
            text.children[0].onclick();assert.equal(h.reloads,1);assert.equal(storage.size,0);
        }
    }
});
test('successful scripts reset stall timer, full completion clears retry history',async()=>{
    const h=setup(new Map([['billy:script-load-recovery','{"startedAt":100000,"attempts":1}']]));h.run(recovery);
    await h.advance(9000);h.context.dispatchEvent({type:'load',target:{tagName:'SCRIPT',src:'/static/js/ui-helpers.js'}});
    await h.advance(9000);assert.equal(h.reloads,0);
    h.context.ScriptLoadRecovery.complete();await h.advance(30000);
    assert.equal(h.reloads,0);assert.equal(h.storage.size,0);
});
test('failed dependency is recovered even if init.js subsequently loads',async()=>{
    const h=setup();h.run(recovery);
    h.context.dispatchEvent({type:'error',target:{tagName:'SCRIPT',src:'/static/js/config-service.js'}});
    h.context.ScriptLoadRecovery.complete();await h.advance(0);assert.equal(h.reloads,1);
});
test('image errors do not reload, unavailable storage uses manual retry',async()=>{
    const h=setup();h.run(recovery);
    h.context.dispatchEvent({type:'error',target:{tagName:'IMG',src:'/missing.png'}});
    await h.advance(0);assert.equal(h.reloads,0);
    h.context.sessionStorage.getItem=()=>{throw new Error('storage blocked');};
    await h.advance(10000);assert.equal(h.reloads,0);assert.equal(h.elements.get('loading-overlay-text').children.length,1);
});


test('software maintenance blocks background controls and survives WebSocket reconnects',()=>{
 const h=setup();h.run(helper);
 const overlay=h.context.document.getElementById('loading-overlay');
 const classes=new Set(['hidden']);
 overlay.classList={add:n=>classes.add(n),remove:n=>classes.delete(n),contains:n=>classes.has(n)};
 let focus=0,restored=0;overlay.focus=()=>focus++;
 const button={inert:false,isConnected:true,focus(){restored++;}},modal={inert:true};
 h.context.document.body={children:[button,modal,overlay]};
 h.context.document.activeElement=button;
 h.context.LoadingOverlay.showSoftware('Installing components');
 assert.equal(classes.has('hidden'),false);assert.equal(button.inert,true);assert.equal(modal.inert,true);
 assert.equal(h.elements.get('loading-overlay-hint').hidden,false);assert.equal(focus,1);
 h.context.dispatchEvent({type:'billy:websocket:connected'});
 assert.equal(classes.has('hidden'),false);assert.equal(h.reloads,0);
 h.context.LoadingOverlay.showSoftware('Checking Billy');assert.equal(focus,1);
 assert.equal(h.elements.get('loading-overlay-text').textContent,'Checking Billy');
 h.context.LoadingOverlay.hideSoftware();
 assert.equal(classes.has('hidden'),true);assert.equal(button.inert,false);assert.equal(modal.inert,true);
 assert.equal(h.elements.get('loading-overlay-hint').hidden,true);assert.equal(restored,1);
 h.context.LoadingOverlay.hideSoftware();assert.equal(restored,1);
});


test('header translates activity, keeps controls stable and never shows raw MQTT state',()=>{
    const h=setup(); h.run(service);
    const update=h.context.ServiceStatus.updateServiceStatusUI;
    update('active',{billy_state:'idle'});
    assert.equal(h.elements.get('service-status').textContent,'Ready');
    const control=h.elements.get('service-controls').children[0];
    for(const [state,label] of Object.entries({listening:'Listening',speaking:'Speaking',bt_media:'Playing Bluetooth audio',in_call:'Bluetooth call',playing_song:'Playing music'})){
        update('active',{billy_state:state});
        assert.equal(h.elements.get('service-status').textContent,label);
        assert.equal(h.elements.get('service-controls').children[0],control);
        assert.equal(h.elements.get('service-controls').children.length,1);
    }
    update('active',{billy_state:'future_internal_state'});
    assert.equal(h.elements.get('service-status').textContent,'Running');
    update('inactive',{billy_state:'bt_media'});
    assert.equal(h.elements.get('service-status').textContent,'Stopped');
    assert.equal(control['aria-label'],'Restart');
});
test('header separates connected Bluetooth from playback and clears stale activity offline',()=>{
    const h=setup(); h.run(service);
    const update=h.context.ServiceStatus.updateServiceStatusUI;
    h.context.billyWebSocket={getBluetoothStatus:()=>({devices:[{Alias:'Phone',Connected:true}]})};
    update('active',{billy_state:'idle'});
    assert.equal(h.elements.get('service-status').textContent,'Ready');
    assert.equal(h.elements.get('header-bluetooth').hidden,false);
    assert.equal(h.elements.get('header-bluetooth')['aria-label'],'Bluetooth connected: Phone');
    h.context.dispatchEvent({type:'billy:websocket:disconnected'});
    assert.equal(h.elements.get('service-status').textContent,'Reconnecting…');
    assert.equal(h.elements.get('header-bluetooth').hidden,true);
    update('active',{billy_state:null});
    assert.equal(h.elements.get('service-status').textContent,'Running');
});


test('late HTTP and offline fallback do not restore stale header activity',async()=>{
    const h=setup();h.run(service);
    let reply;
    h.context.fetch=()=>new Promise(resolve=>reply=resolve);
    const pending=h.context.ServiceStatus.fetchStatus(true);
    await h.advance(10);
    h.context.updateServiceStatus('active',{billy_state:'listening'});
    reply({ok:true,json:async()=>({status:'active',billy_state:'bt_media'})});
    await pending;
    assert.equal(h.elements.get('service-status').textContent,'Listening');
    h.context.dispatchEvent({type:'billy:websocket:disconnected'});
    h.context.fetch=async()=>{throw new Error('offline');};
    await h.context.ServiceStatus.fetchStatus();
    assert.equal(h.elements.get('service-status').textContent,'Reconnecting…');
});


test('live status cannot keep full profile and mood data out of the cache', async () => {
    const h = setup(); h.run(service);
    let requests = 0;
    h.context.fetch = async () => {
        requests++;
        return {ok: true, json: async () => ({status: 'active', current_user: 'tom',
            mood: {label: requests === 1 ? 'curious' : 'calm'}})};
    };
    h.context.updateServiceStatus('active', {billy_state: 'listening'});
    assert.equal((await h.context.ServiceStatus.fetchStatus()).mood?.label, 'curious');
    assert.equal(requests, 1);
    await h.advance(1000);
    h.context.updateServiceStatus('active', {billy_state: 'speaking'});
    assert.equal((await h.context.ServiceStatus.fetchStatus()).mood.label, 'curious');
    assert.equal(requests, 1, 'full snapshots still have a short cache');
    await h.advance(1100);
    h.context.updateServiceStatus('active', {billy_state: 'listening'});
    assert.equal((await h.context.ServiceStatus.fetchStatus()).mood.label, 'calm');
    assert.equal(requests, 2, 'partial updates must not renew the full-data cache');
});

test('full profile response supplies mood without reverting newer live updates', async () => {
    const h = setup(); h.run(service);
    let finish;
    h.context.fetch = () => new Promise(resolve => { finish = resolve; });
    const pending = h.context.ServiceStatus.fetchStatus();
    await flush();
    // Same clock tick: freshness must not depend on Date.now() granularity.
    h.context.updateServiceStatus('active', {billy_state: 'listening',
        current_persona: 'new-persona'});
    h.context.updateServiceStatus('active', {billy_state: 'speaking'});
    finish({ok: true, json: async () => ({status: 'active', billy_state: 'idle',
        current_persona: 'old-persona', current_user: 'tom', mood: {label: 'curious'}})});
    const status = await pending;
    assert.equal(status.mood?.label, 'curious');
    assert.equal(status.current_user, 'tom');
    assert.equal(status.current_persona, 'new-persona');
    assert.equal(status.billy_state, 'speaking');
    assert.equal(h.elements.get('service-status').textContent, 'Speaking');
});

test('older full response cannot replace more recent mood data', async () => {
    const h = setup(); h.run(service);
    const replies = [];
    h.context.fetch = () => new Promise(resolve => replies.push(resolve));
    const first = h.context.ServiceStatus.fetchStatus(true);
    await flush(); await h.advance(1);
    const second = h.context.ServiceStatus.fetchStatus(true);
    await flush();
    replies[1]({ok: true, json: async () => ({status: 'active', mood: {label: 'calm'}})});
    await second;
    replies[0]({ok: true, json: async () => ({status: 'active', mood: {label: 'grumpy'}})});
    assert.equal((await first).mood.label, 'calm');
});
test('notifications move above modal overlays while one is open', async()=>{
    const h=setup();h.run(helper);
    const bars={notification:h.context.document.getElementById('notification'),overlay:h.context.document.getElementById('notification-overlay')};
    for(const bar of Object.values(bars)){const state=new Set(['hidden']);bar.state=state;bar.classList={add(...c){c.forEach(x=>state.add(x));},remove(...c){c.forEach(x=>state.delete(x));},toggle(){},contains:c=>state.has(c)};}
    h.context.document.querySelector=()=>null;
    h.context.showNotification('Saved','success',1000);
    assert.equal(bars.notification.textContent,'Saved');
    assert.ok(!bars.notification.state.has('hidden'));
    assert.ok(bars.overlay.state.has('hidden'));
    h.context.document.querySelector=()=>({});
    h.context.showNotification('Duplicate entry','error',1000);
    assert.equal(bars.overlay.textContent,'Duplicate entry');
    assert.ok(!bars.overlay.state.has('hidden'));
    assert.ok(bars.notification.state.has('hidden'));
    await h.advance(1300);
    assert.ok(bars.overlay.state.has('hidden'));
});
