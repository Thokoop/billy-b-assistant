const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync('webconfig/static/js/version-release.js','utf8');
const flush = async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
function setup() {
    let now=1000,id=0,reloads=0;
    const timers=new Map(),elements=new Map(),calls=[],notices=[];
    const element=()=>({dataset:{},disabled:false,textContent:'',classList:{toggle(){}},querySelector(){return null;},addEventListener(type,fn){this[type]=fn;}});
    let status={rollback_available:true,job:{}},post=async()=>({job:{id:'new',state:'queued'}});
    const ctx={console,AbortController,TypeError,Date:{now:()=>now},CustomEvent:class{},confirm:()=>true,
        location:{reload(){reloads++;}},sessionStorage:{removeItem(){}},showNotification:(...args)=>notices.push(args),
        setTimeout(fn,ms){timers.set(++id,{fn,at:now+ms});return id;},clearTimeout(id){timers.delete(id);},
        document:{getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},querySelectorAll:()=>[],addEventListener(){},dispatchEvent(){}},
        addEventListener(){},BootstrapData:{take:()=>({current:'v1',latest:'v2',update_available:true})},
        fetch:async(url,options)=>{calls.push([url,options]);const value=options?.method==='POST'?await post(options):status;return {ok:value.ok!==false,json:async()=>value};}};
    ctx.window=ctx;vm.createContext(ctx);vm.runInContext(source,ctx);ctx.BillyVersionUI.refresh();
    async function advance(ms){const end=now+ms;await flush();while(true){const due=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!due)break;now=due[1].at;timers.delete(due[0]);due[1].fn();await flush();}now=end;await flush();}
    return {ctx,calls,notices,elements,advance,set status(value){status=value;},set post(fn){post=fn;},get reloads(){return reloads;}};
}
test('update submits once, follows durable progress, and reloads only after completion',async()=>{
    const h=setup();await flush();
    h.status={rollback_available:true,job:{id:'new',state:'preparing',message:'Preparing'}};
    const button=h.elements.get('update-btn');
    const action=button.click();await button.click();await action;
    assert.equal(h.calls.filter(([,o])=>o.method==='POST').length,1);
    assert.equal(button.disabled,true);assert.equal(h.reloads,0);
    h.status={rollback_available:true,job:{id:'new',state:'complete',message:'Ready'}};
    await h.advance(2000);assert.equal(h.reloads,0);await h.advance(1200);assert.equal(h.reloads,1);
});
test('ambiguous POST timeout never retries or mistakes an old completed job for success',async()=>{
    const h=setup();h.status={job:{id:'old',state:'complete',message:'Old result'}};await flush();h.ctx.BillyVersionUI.refresh();await flush();
    h.post=options=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{const e=new Error('timeout');e.name='AbortError';reject(e);}));
    const action=h.elements.get('update-btn').click();await h.advance(5000);await action;
    await h.advance(4000);assert.equal(h.reloads,0);
    assert.equal(h.calls.filter(([,o])=>o.method==='POST').length,1);
    h.status={job:{id:'new',state:'validating',message:'Checking'}};await h.advance(2000);
    h.status={job:{id:'new',state:'rolled_back',message:'Previous selected'}};await h.advance(3200);
    assert.equal(h.reloads,1);
});
test('HTTP rejection does not display old completion as a successful update',async()=>{
    const h=setup();await flush();h.status={job:{id:'old',state:'complete',message:'Old result'}};
    h.post=async()=>({ok:false,error:'Busy'});
    await h.elements.get('update-btn').click();await h.advance(5000);
    assert.equal(h.reloads,0);assert.ok(h.notices.some(n=>n[0]==='Busy'));
});
test('rollback uses protected POST and failed preparation leaves UI usable',async()=>{
    const h=setup();await flush();h.status={rollback_available:true,job:{id:'new',state:'failed',message:'Preparation failed'}};
    await h.elements.get('rollback-software-btn').click();
    const request=h.calls.find(([url,o])=>url==='/software/rollback'&&o.method==='POST');
    assert.equal(request[1].headers['X-Billy-Update'],'1');
    assert.equal(h.elements.get('rollback-software-btn').disabled,false);
    assert.ok(h.notices.some(n=>n[0]==='Preparation failed'));assert.equal(h.reloads,0);
});
test('a page opened during an update resumes status without starting another job',async()=>{
    const h=setup();h.status={job:{id:'running',state:'validating'}};await flush();h.ctx.BillyVersionUI.refresh();await flush();
    assert.equal(h.elements.get('update-btn').disabled,true);
    assert.equal(h.calls.filter(([,o])=>o.method==='POST').length,0);
});
test('disabled browser storage does not prevent a software request',async()=>{
    const h=setup();await flush();h.ctx.sessionStorage.removeItem=()=>{throw new Error('storage disabled');};
    h.status={job:{id:'new',state:'queued'}};
    await h.elements.get('update-btn').click();
    assert.equal(h.calls.filter(([,o])=>o.method==='POST').length,1);
});
test('recovery names current and previous versions and confirms the selected version',async()=>{
    const h=setup();await flush();
    h.status={current:'v2.6.0',previous:'v2.5.1',rollback_available:true,job:{}};
    h.ctx.BillyVersionUI.refresh();await flush();
    assert.equal(h.elements.get('recovery-current-version').textContent,'v2.6.0');
    assert.equal(h.elements.get('recovery-previous-version').textContent,'v2.5.1');
    let confirmation='';h.ctx.confirm=message=>{confirmation=message;return false;};
    await h.elements.get('rollback-software-btn').click();
    assert.match(confirmation,/Restore v2.5.1\?/);assert.match(confirmation,/Current version: v2.6.0/);
    await h.elements.get('simulate-update-btn').click();assert.match(confirmation,/Reinstall v2.6.0\?/);
    assert.equal(h.calls.filter(([,o])=>o.method==='POST').length,0);
});
test('no saved version is explained and progress is shown inside the recovery dialog',async()=>{
    const h=setup();await flush();h.status={current:'v2.5.1',previous:null,rollback_available:false,job:{id:'job',state:'preparing',message:'Preparing software'}};
    h.ctx.BillyVersionUI.refresh();await flush();
    assert.equal(h.elements.get('recovery-previous-version').textContent,'No previous version saved');
    assert.equal(h.elements.get('rollback-software-btn').disabled,true);
    assert.equal(h.elements.get('recovery-software-status').textContent,'Preparing software');
    assert.equal(h.elements.get('software-operation-status').textContent,'Preparing software');
});


test('central component status uses shared polling and repair submits one protected job',async()=>{
 const h=setup();h.status={packages:{state:'attention',repairable:true,message:'Components missing',missing:['libportaudio2']},job:{}};
 await flush();h.ctx.BillyVersionUI.refresh();await flush();
 assert.equal(h.elements.get('system-packages-summary').textContent,'Install the missing components Billy needs to run.');
 assert.equal(h.elements.get('system-packages-action-label').textContent,'Install components');
 assert.equal(h.elements.get('system-packages-badge').textContent,'Installation needed');
 assert.equal(h.elements.get('system-packages-badge').hidden,false);
 assert.equal(h.elements.get('system-packages-detail-panel').hidden,false);
 assert.equal(h.elements.get('system-packages-status').textContent,'Components missing');
 assert.equal(h.elements.get('system-packages-details').textContent,'libportaudio2');
 const button=h.elements.get('repair-packages-btn');assert.equal(button.disabled,false);assert.equal(button.hidden,false);
 h.status={packages:{state:'attention',repairable:true},job:{id:'new',state:'installing_packages',message:'Installing'}};
 await button.click();assert.equal(button.disabled,true);
 const requests=h.calls.filter(([,o])=>o.method==='POST');assert.equal(requests.length,1);
 assert.equal(requests[0][0],'/software/packages');assert.equal(requests[0][1].headers['X-Billy-Update'],'1');
 h.status={packages:{state:'ready',repairable:false,message:'All system components are installed.'},job:{id:'new',state:'complete'}};
 await h.advance(2000);assert.equal(button.disabled,true);assert.equal(button.hidden,true);
 assert.equal(h.elements.get('system-packages-operation').textContent,'');
 assert.equal(h.elements.get('system-packages-summary').textContent,'All system components are installed.');
 assert.equal(h.elements.get('system-packages-badge').hidden,true);
 assert.equal(h.elements.get('system-packages-detail-panel').hidden,true);
 assert.equal(h.elements.get('system-packages-status').textContent,'');
 h.status={packages:{state:'unavailable',message:'Check unavailable',repairable:false},job:{}};
 h.ctx.BillyVersionUI.refresh();await flush();
 assert.equal(h.elements.get('system-packages-summary').textContent,'Check unavailable');
 assert.equal(h.elements.get('system-packages-status').textContent,'');
});


test('component actions distinguish installation, updates, damage and setup without duplicate status',async()=>{
 const h=setup();await flush();
 for(const [fields,label,badge] of [
  [{missing:['pipewire'],updates:['pipewire']},'Install components','Installation needed'],
  [{updates:['pipewire']},'Update components','Update available'],
  [{damaged:['pipewire'],missing:['bluez']},'Repair components','Repair needed'],
  [{configuration_needed:true},'Set up components','Setup needed'],
 ]){
  h.status={packages:{state:'attention',repairable:true,message:'System components need attention.',...fields},job:{}};
  h.ctx.BillyVersionUI.refresh();await flush();
  assert.equal(h.elements.get('system-packages-action-label').textContent,label);
  assert.equal(h.elements.get('system-packages-badge').textContent,badge);
  assert.equal(h.elements.get('system-packages-status').textContent,'');
 }
 assert.equal(h.elements.get('system-packages-detail-panel').hidden,true);
});
