const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync('application/webconfig/static/js/version-release.js','utf8');
const flush = async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
function setup() {
    let now=1000,id=0,reloads=0;
    const timers=new Map(),elements=new Map(),calls=[],notices=[];
    const overlay={visible:false,message:"",showSoftware(message){this.visible=true;this.message=message;},hideSoftware(){this.visible=false;}};
    const element=()=>{
        const classes=new Set(['animate-spin']);let text='',children=[];
        const icon={classList:{remove(name){classes.delete(name);},contains:name=>classes.has(name)}};
        return {dataset:{},attributes:{},disabled:false,
            get children(){return children;},
            get textContent(){return text+children.map(child=>child.textContent).join('');},
            set textContent(value){text=value;children=[];},
            append(...nodes){children.push(...nodes);},replaceChildren(...nodes){text='';children=nodes;},
            classList:{toggle(name,on){if(on)classes.add(name);else classes.delete(name);},contains:name=>classes.has(name)},
            setAttribute(name,value){this.attributes[name]=value;},querySelector(){return icon;},
            addEventListener(type,fn){this[type]=fn;}};
    };
    let status={rollback_available:true,job:{}},post=async()=>({job:{id:'new',state:'queued'}});
    const ctx={console,AbortController,TypeError,Date:{now:()=>now},CustomEvent:class{},confirm:()=>true,
        location:{reload(){reloads++;}},sessionStorage:{removeItem(){}},showNotification:(...args)=>notices.push(args),
        setTimeout(fn,ms){timers.set(++id,{fn,at:now+ms});return id;},clearTimeout(id){timers.delete(id);},
        document:{createElement:()=>element(),getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},querySelectorAll:()=>[],addEventListener(){},dispatchEvent(){}},
        addEventListener(){},BootstrapData:{take:()=>({current:'v1',latest:'v2',update_available:true})},
        fetch:async(url,options)=>{calls.push([url,options]);const value=options?.method==='POST'?await post(options):status;return {ok:value.ok!==false,json:async()=>value};}};
    ctx.LoadingOverlay=overlay;
    ctx.window=ctx;vm.createContext(ctx);vm.runInContext(source,ctx);ctx.BillyVersionUI.refresh();
    async function advance(ms){const end=now+ms;await flush();while(true){const due=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!due)break;now=due[1].at;timers.delete(due[0]);due[1].fn();await flush();}now=end;await flush();}
    return {ctx,calls,notices,elements,advance,overlay,set status(value){status=value;},set post(fn){post=fn;},get reloads(){return reloads;}};
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
 const h=setup();h.status={packages:{state:'attention',repairable:true,message:'Components missing',missing:['libportaudio2'],
  components:[{name:'libportaudio2',state:'missing'},{name:'ffmpeg',state:'installed'}]},job:{}};
 await flush();h.ctx.BillyVersionInfo={current:'v1',latest:'v1',update_available:false};h.ctx.BillyVersionUI.refresh();await flush();
 const category=h.elements.get('settings-software-badge');
 assert.equal(category.classList.contains('hidden'),false);
 assert.equal(category.classList.contains('inline-flex'),true);
 assert.equal(category.title,'System components: Install components');
 assert.equal(category.attributes['aria-label'],category.title);
 assert.equal(h.elements.get('system-packages-summary').textContent,'Install the missing components Billy needs to run.');
 assert.equal(h.elements.get('system-packages-action-label').textContent,'Install components');
 assert.equal(h.elements.get('system-packages-badge').textContent,'Installation needed');
 assert.equal(h.elements.get('system-packages-badge').hidden,false);
 assert.equal(h.elements.get('system-packages-detail-panel').hidden,true);
 assert.equal(h.elements.get('system-packages-status').textContent,'Components missing');
 const details=h.elements.get('system-packages-details');
 const rows=()=>details.children.map(row=>[row.children[0].textContent,row.children[1].children[1].textContent]);
 assert.deepEqual(rows(),[['libportaudio2','Not installed'],['ffmpeg','Installed']]);
 assert.equal(details.children[1].children[1].children[0].attributes['aria-hidden'],'true');
 h.elements.get('toggle-component-details').click();
 assert.equal(h.elements.get('toggle-component-details').attributes['aria-expanded'],'true');
 const button=h.elements.get('repair-packages-btn');assert.equal(button.disabled,false);assert.equal(button.hidden,false);
 // Version refreshes and mounting the settings page again retain component attention.
 h.elements.delete('settings-software-badge');h.ctx.BillyVersionUI.refresh();
 const remounted=h.elements.get('settings-software-badge');
 assert.equal(remounted.classList.contains('hidden'),false);
 await flush();
 h.status={packages:{state:'attention',repairable:true},job:{id:'new',state:'installing_packages',message:'Installing'}};
 await button.click();assert.equal(button.disabled,true);
 assert.equal(remounted.classList.contains('hidden'),false);
 const requests=h.calls.filter(([,o])=>o.method==='POST');assert.equal(requests.length,1);
 assert.equal(requests[0][0],'/software/packages');assert.equal(requests[0][1].headers['X-Billy-Update'],'1');
 h.status={packages:{state:'ready',repairable:false,message:'All system components are installed.',
  components:[{name:'libportaudio2',state:'installed'},{name:'ffmpeg',state:'installed'}]},job:{id:'new',state:'complete'}};
 await h.advance(2000);assert.equal(button.disabled,true);assert.equal(button.hidden,true);
 assert.equal(remounted.classList.contains('hidden'),true);
 assert.equal(h.elements.get('system-packages-operation').textContent,'');
 assert.equal(h.elements.get('system-packages-summary').textContent,'All system components are installed.');
 assert.equal(h.elements.get('system-packages-badge').hidden,true);
 assert.equal(h.elements.get('system-packages-detail-panel').hidden,false);
 assert.equal(h.elements.get('system-packages-detail-panel').hidden,false);
 assert.deepEqual(rows(),[['libportaudio2','Installed'],['ffmpeg','Installed']]);
 assert.equal(h.elements.get('system-packages-details-status').hidden,true);
 assert.equal(h.elements.get('system-packages-status').textContent,'');
 h.status={packages:{state:'unavailable',message:'Check unavailable',repairable:false},job:{}};
 h.ctx.BillyVersionUI.refresh();await flush();
 assert.equal(h.elements.get('system-packages-summary').textContent,'Check unavailable');
 assert.equal(h.elements.get('system-packages-status').textContent,'');
 assert.equal(h.elements.get('system-packages-detail-panel').hidden,false);
 assert.equal(h.elements.get('system-packages-details-status').textContent,'Check unavailable');
 assert.equal(details.children.length,0);
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
  const category=h.elements.get('settings-software-badge');
  assert.equal(category.classList.contains('hidden'),false);
  assert.equal(category.title,`Software update available. System components: ${label}`);
 }
 assert.equal(h.elements.get('system-packages-detail-panel').hidden,true);
 h.status={packages:{state:'ready',repairable:false,message:'All system components are installed.'},job:{}};
 h.ctx.BillyVersionUI.refresh();await flush();
 const category=h.elements.get('settings-software-badge');
 assert.equal(category.classList.contains('hidden'),false);
 assert.equal(category.title,'Software update available');
});


test('software operations use maintenance progress without spinning icons and release on failure',async()=>{
 const h=setup();await flush();
 let finish;h.post=()=>new Promise(resolve=>{finish=resolve;});
 const button=h.elements.get('update-btn');
 const action=button.click();await flush();
 assert.equal(h.overlay.visible,true);assert.match(h.overlay.message,/Preparing/);
 assert.equal(button.querySelector('.material-icons').classList.contains('animate-spin'),false);
 h.status={job:{id:'new',state:'installing_packages',message:'Installing components'}};
 finish({job:{id:'new',state:'queued'}});await action;
 assert.equal(h.overlay.message,'Installing components');
 h.status={job:{id:'new',state:'validating',message:'Checking Billy'}};
 await h.advance(2000);assert.equal(h.overlay.visible,true);assert.equal(h.overlay.message,'Checking Billy');
 h.status={job:{id:'new',state:'failed',message:'Could not install'}};
 await h.advance(2000);assert.equal(h.overlay.visible,false);assert.equal(button.disabled,false);
 assert.equal(h.reloads,0);
});

test('maintenance resumes for a running job and stays up through temporary connection loss',async()=>{
 const h=setup();await flush();
 h.status={job:{id:'running',state:'validating',message:'Checking startup'}};
 h.ctx.BillyVersionUI.refresh();await flush();
 assert.equal(h.overlay.visible,true);assert.equal(h.overlay.message,'Checking startup');
 h.status={ok:false,error:'Unavailable'};
 await h.advance(2000);assert.equal(h.overlay.visible,true);assert.match(h.overlay.message,/Reconnecting/);
 h.status={job:{id:'running',state:'complete',message:'Done'}};
 await h.advance(2000);assert.equal(h.overlay.visible,false);
 await h.advance(1200);assert.equal(h.reloads,1);
 assert.equal(h.calls.filter(([,o])=>o.method==='POST').length,0);
});

test('rejected software request releases maintenance even if the status endpoint then fails',async()=>{
 const h=setup();await flush();
 h.post=async()=>{h.status={ok:false,error:'Unavailable'};return {ok:false,error:'Request rejected'};};
 await h.elements.get('update-btn').click();
 assert.equal(h.overlay.visible,false);assert.equal(h.elements.get('update-btn').disabled,false);
 assert.equal(h.reloads,0);
});

test('unconfirmed request releases maintenance after its deadline without resubmitting',async()=>{
 const h=setup();await flush();
 h.post=async()=>{throw new TypeError('Connection lost');};
 await h.elements.get('update-btn').click();
 assert.equal(h.overlay.visible,true);
 await h.advance(32000);assert.equal(h.overlay.visible,false);
 assert.equal(h.calls.filter(([,o])=>o.method==='POST').length,1);
});


test('completed software status stays clear while failures and rollback remain visible', async()=>{
 const h=setup();await flush();
 for(const [state,message,expected] of [
  ['preparing','Preparing software','Preparing software'],
  ['complete','Software is ready. Both services passed startup checks.',''],
  ['failed','Preparation failed','Preparation failed'],
  ['rolled_back','Previous version restored','Previous version restored'],
 ]){
  h.status={job:{id:'job',state,message}};
  h.ctx.BillyVersionUI.refresh();await flush();
  assert.equal(h.elements.get('software-operation-status').textContent,expected);
  assert.equal(h.elements.get('recovery-software-status').textContent,expected);
 }
 const fresh=setup();await flush();
 fresh.status={job:{id:'old',state:'complete',message:'Software is ready. Both services passed startup checks.'}};
 fresh.ctx.BillyVersionUI.refresh();await flush();
 assert.equal(fresh.elements.get('software-operation-status').textContent,'');
 assert.equal(fresh.notices.length,0);
 assert.equal(fresh.overlay.visible,false);
});

test('component versions are rendered as text and refresh preserves disclosure state',async()=>{
 const h=setup();await flush();
 h.status={packages:{state:'ready',repairable:false,components:[
  {name:'bluez',state:'installed',version:'5.79-1'},
  {name:'pipewire',state:'missing'},
 ]},job:{}};
 h.ctx.BillyVersionUI.refresh();await flush();
 const rows=h.elements.get('system-packages-details').children;
 assert.equal(rows[0].children[0].children[0].textContent,'Installed version: 5.79-1');
 assert.equal(rows[1].children[0].children.length,0);
 const panel=h.elements.get('system-packages-detail-panel');
 assert.equal(panel.hidden,true);
 h.elements.get('toggle-component-details').click();
 assert.equal(panel.hidden,false);
 h.ctx.BillyVersionUI.refresh();await flush();
 assert.equal(panel.hidden,false);
 h.elements.get('toggle-component-details').click();
 assert.equal(panel.hidden,true);
});
