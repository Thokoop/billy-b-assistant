const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const helper = fs.readFileSync('webconfig/static/js/ui-helpers.js', 'utf8');
const service = fs.readFileSync('webconfig/static/js/service-status.js', 'utf8');
const recovery = fs.readFileSync('webconfig/templates/components/script-recovery.html', 'utf8').match(/<script>([\s\S]*)<\/script>/)[1];
const flush = async () => { for(let i=0;i<20;i++) await Promise.resolve(); };
function setup(storage = new Map()) {
    let now = 100000, next = 1, reloads = 0, stops = 0;
    const timers = new Map(), events = new Map(), elements = new Map();
    const makeElement = () => ({textContent:'', children:[], style:{}, classList:{add(){},remove(){},toggle(){},contains(){return false;}}, appendChild(el){this.children.push(el);}, querySelector(){return makeElement();}});
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
