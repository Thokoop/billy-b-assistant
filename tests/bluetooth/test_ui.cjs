const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const btSource=fs.readFileSync('application/webconfig/static/js/bluetooth.js','utf8');
const wsSource=fs.readFileSync('application/webconfig/static/js/websocket.js','utf8');
const audioSource=fs.readFileSync('application/webconfig/static/js/audio-panel.js','utf8');
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
function target(){const events=new Map();return {addEventListener(type,fn){if(!events.has(type))events.set(type,new Set());events.get(type).add(fn);},removeEventListener(type,fn){events.get(type)?.delete(fn);},dispatchEvent(event){for(const fn of events.get(event.type)||[])fn(event);},count(type){return events.get(type)?.size||0;}};}
function element(id='') {return Object.assign(target(),{id,dataset:{},children:[],style:{},textContent:'',classList:{add(){},remove(){},toggle(){}},append(...items){this.children.push(...items);},replaceChildren(){this.children=[];},removeChild(child){this.children.splice(this.children.indexOf(child),1);},insertBefore(child,before){const old=this.children.indexOf(child);if(old>=0)this.children.splice(old,1);this.children.splice(before?this.children.indexOf(before):this.children.length,0,child);},attributes:{},setAttribute(name,value){this.attributes[name]=value;},parentElement:{classList:{add(){},toggle(){}}}});}
function section(){const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,element(id));return nodes.get(id);};const pair=get('bt-pair');pair.dataset.action='pair';return {dataset:{token:'page-token'},isConnected:true,nodes,get,querySelector:s=>get(s.slice(1)),querySelectorAll:s=>s==='[data-action]'?[pair]:[pair,get('bt-pair-stop'),get('bt-setup-retry')]};}
function setup({mounted=true,live=null}={}){
 const timers=new Map(),requests=[],labels=new Map();let serial=0;
 const h={root:mounted?section():null,live,info:null};
 const win=target(),doc=target();Object.assign(doc,{hidden:false,readyState:'loading',getElementById:id=>id==='bluetooth-settings'?h.root:labels.get(id)||null,createElement:()=>element()});
 const c={window:win,document:doc,console,AbortSignal,AbortController,CustomEvent:class{constructor(type,options={}){this.type=type;Object.assign(this,options);}},setInterval(fn){const id=++serial;timers.set(id,fn);return id;},clearInterval(id){timers.delete(id);},setTimeout(fn){const id=++serial;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},fetch(url,options){requests.push({url,options});return h.fetch(url,options);}};
 h.fetch=async()=>({ok:true,json:async()=>state()});win.billyWebSocket={getBluetoothStatus:()=>h.live,getDeviceInfo:()=>h.info};
 Object.assign(h,{c,timers,requests,labels,run:source=>vm.runInContext(source,c),status(data){h.live=data;win.dispatchEvent({type:'billy:bluetooth:status',detail:data});},device(data){h.info=data;win.dispatchEvent({type:'billy:device-info:status',detail:data});}});vm.createContext(c);return h;
}
function state(extra={}){return {setup:{state:'ready'},adapter:{Powered:true},devices:[],audio:{state:'idle'},pairing_seconds:0,pending:null,...extra};}
test('HTTP fallback stops with live status and restarts after disconnect without duplicating subscriptions',async()=>{
 const h=setup();h.run(btSource);h.c.window.BluetoothSettings.init();await flush();
 assert.equal(h.requests.length,1);assert.equal(h.timers.size,1);
 h.status(state({pairing_seconds:98,pending:{name:'Phone',passkey:'012345'}}));
 assert.equal(h.timers.size,0);assert.equal(h.root.get('bt-code').textContent,'012345');assert.equal(h.root.get('bt-pair-status').textContent,'Visible for 98s');
 h.c.window.BluetoothSettings.init();assert.equal(h.timers.size,0);assert.equal(h.root.get('bt-pair').count('click'),1);
 h.live=null;h.c.window.dispatchEvent({type:'billy:websocket:disconnected'});await flush();
 assert.equal(h.requests.length,2);assert.equal(h.timers.size,1);
 h.status(state());assert.equal(h.timers.size,0);assert.equal(h.root.get('bt-pending').hidden,true);
});
test('navigation uses live cache and cleans up listeners and polling',async()=>{
 const h=setup({mounted:false});h.run(btSource);h.c.window.BluetoothSettings.init();assert.equal(h.requests.length,0);
 h.root=section();h.live=state();h.c.window.BluetoothSettings.init();assert.equal(h.requests.length,0);
 h.root.isConnected=false;h.root=null;h.c.window.BluetoothSettings.init();assert.equal(h.c.window.count('billy:bluetooth:status'),0);assert.equal(h.timers.size,0);
 h.root=section();h.c.window.BluetoothSettings.init();assert.equal(h.c.window.count('billy:bluetooth:status'),1);assert.equal(h.requests.length,0);
});
test('late HTTP cannot overwrite newer pairing or audio status',async()=>{
 const h=setup();let answer;h.fetch=()=>new Promise(resolve=>answer=resolve);h.run(btSource);h.c.window.BluetoothSettings.init();
 h.status(state({audio:{state:'call_audio',aec:true}}));
 answer({ok:true,json:async()=>state()});await flush();assert.match(h.root.get('bt-audio').textContent,/On a call/);
});
test('actions retain CSRF protection and fetch one fresh status without resuming periodic requests',async()=>{
 const h=setup({live:state()});h.run(btSource);h.c.window.BluetoothSettings.init();
 h.root.get('bt-pair').dispatchEvent({type:'click'});await flush();
 assert.deepEqual(h.requests.map(r=>r.url),['/api/bluetooth/pair','/api/bluetooth']);
 assert.equal(h.requests[0].options.headers['X-Bluetooth-Token'],'page-token');assert.equal(h.timers.size,0);
});
test('incoming live updates do not re-enable controls mid action',async()=>{
 const h=setup({live:state()});let done;h.fetch=()=>new Promise(resolve=>done=resolve);h.run(btSource);h.c.window.BluetoothSettings.init();
 h.root.get('bt-pair').dispatchEvent({type:'click'});h.status(state());assert.equal(h.root.get('bt-pair').disabled,true);
 h.fetch=async()=>({ok:true,json:async()=>state()});done({ok:true,json:async()=>({ok:true})});await flush();assert.equal(h.root.get('bt-pair').disabled,false);
});
test('hidden tabs stop fallback and restore current status when visible',async()=>{
 const h=setup();h.run(btSource);h.c.window.BluetoothSettings.init();await flush();
 h.c.document.hidden=true;h.c.document.dispatchEvent({type:'visibilitychange'});assert.equal(h.timers.size,0);
 h.status(state({pairing_seconds:42}));h.c.document.hidden=false;h.c.document.dispatchEvent({type:'visibilitychange'});
 assert.equal(h.root.get('bt-pair-status').textContent,'Visible for 42s');assert.equal(h.requests.length,1);
});
test('WebSocket distributes both sections and clears caches before disconnect notification',()=>{
 const h=setup();const sockets=[];
 h.c.WebSocket=class{static OPEN=1;constructor(){this.readyState=1;sockets.push(this);}close(){this.readyState=3;this.onclose();}};
 h.c.window.location={protocol:'http:',host:'billy.local'};h.run(wsSource);h.c.document.dispatchEvent({type:'DOMContentLoaded'});
 const socket=sockets[0];let serviceUpdates=0,logUpdates=0;
 h.c.window.updateServiceStatus=()=>serviceUpdates++;h.c.window.updateLogs=()=>logUpdates++;
 socket.onmessage({data:JSON.stringify({type:'status',data:{status:'active',bluetooth:state(),device_info:{mic:'USB'}}})});
 assert.equal(h.c.window.billyWebSocket.getDeviceInfo().mic,'USB');assert.equal(h.c.window.billyWebSocket.getBluetoothStatus().setup.state,'ready');assert.equal(serviceUpdates,1);
 socket.onmessage({data:JSON.stringify({type:'logs',data:'logs'})});assert.equal(logUpdates,1);
 h.c.window.addEventListener('billy:websocket:disconnected',()=>{assert.equal(h.c.window.billyWebSocket.getDeviceInfo(),null);assert.equal(h.c.window.billyWebSocket.getBluetoothStatus(),null);});socket.close();
});
function audioHarness(){const h=setup({mounted:false});h.run(audioSource);h.requests.length=0;h.labels.set('mic-label',element());h.labels.set('speaker-label',element());h.c.fetchJsonWithTimeout=async()=>{h.requests.push({url:'/device-info'});return {mic:'Unknown',speaker:'Unknown'};};return h;}
test('device labels use live cache and never retry Unknown while live updates are available',async()=>{
 const h=audioHarness();h.info={mic:'Unknown',speaker:'USB Speaker'};await h.c.window.AudioPanel.updateDeviceLabels();
 assert.equal(h.requests.length,0);assert.equal(h.timers.size,0);assert.equal(h.labels.get('speaker-label').textContent,'USB Speaker');
 h.device({mic:'USB Mic',speaker:'USB Speaker'});assert.equal(h.labels.get('mic-label').textContent,'USB Mic');
});
test('device live status cancels fallback retry chain and stale HTTP response',async()=>{
 const h=audioHarness();await h.c.window.AudioPanel.updateDeviceLabels();assert.equal(h.timers.size,1);
 h.device({mic:'Mic',speaker:'Speaker'});assert.equal(h.timers.size,0);
 h.info=null;let resolve;h.c.fetchJsonWithTimeout=()=>new Promise(r=>resolve=r);
 const pending=h.c.window.AudioPanel.updateDeviceLabels();await flush();h.device({mic:'New Mic',speaker:'New Speaker'});
 resolve({mic:'Old Mic',speaker:'Old Speaker'});await pending;assert.equal(h.labels.get('mic-label').textContent,'New Mic');
});
test('device labels fall back on disconnect and skip missing pages',async()=>{
 const h=audioHarness();h.c.window.dispatchEvent({type:'billy:websocket:disconnected'});await flush();assert.equal(h.requests.length,1);
 h.labels.clear();for(const fn of [...h.timers.values()])fn();await flush();assert.equal(h.requests.length,1);
});

test('device live status arriving during bootstrap prevents a fallback request',async()=>{
 const h=audioHarness();let finish;
 h.c.window.BootstrapData={take:()=>new Promise(resolve=>finish=resolve)};
 const update=h.c.window.AudioPanel.updateDeviceLabels();await flush();
 h.device({mic:'Live mic',speaker:'Live speaker'});finish(null);await update;
 assert.equal(h.requests.length,0);assert.equal(h.labels.get('mic-label').textContent,'Live mic');
});

test('connected device without media transport disconnects by selecting its card',async()=>{
 const h=setup({live:state({devices:[{Address:'AA',Alias:'Phone',Paired:true,Connected:true,MediaSupported:true,MediaConnected:false}]})});
 h.run(btSource);h.c.window.BluetoothSettings.init();
 const row=h.root.get('bt-devices').children[0];
 assert.equal(row.children[0].children[1].children[1].textContent,'Connected');
 const actions=row.children[1].children.filter(c=>c.type==='button');
 assert.deepEqual(actions,[]);
 assert.equal(row.dataset.selected,'true');
 assert.equal(row.children[0].attributes['aria-pressed'],'true');
 row.children[0].dispatchEvent({type:'click'});await flush();
 assert.equal(h.requests[0].url,'/api/bluetooth/disconnect');
 assert.equal(JSON.parse(h.requests[0].options.body).address,'AA');
});
test('idle media connection needs no retry and legacy status still renders',()=>{
 for(const extra of [{MediaSupported:true,MediaConnected:true},{}]){
  const h=setup({live:state({devices:[{Address:'AA',Paired:true,Connected:true,...extra}]})});
  h.run(btSource);h.c.window.BluetoothSettings.init();
  const row=h.root.get('bt-devices').children[0];
  assert.deepEqual(row.children[1].children.filter(c=>c.type==='button').map(b=>b.children[1].textContent),[]);
 }
});


function notificationHarness(h) {
 const bar=element('notification'),classes=new Set(['hidden']);
 bar.classList={add(...v){v.forEach(x=>classes.add(x));},remove(...v){v.forEach(x=>classes.delete(x));},contains(v){return classes.has(v);}};
 let text='';Object.defineProperty(bar,'textContent',{get(){return text;},set(v){text=v;bar.children=[];}});
 h.labels.set('notification',bar);
 const helpers=fs.readFileSync('application/webconfig/static/js/ui-helpers.js','utf8');
 h.run(helpers.slice(0,helpers.indexOf('function toggleInputVisibility')));
 h.tick=()=>{const [id,fn]=h.timers.entries().next().value;h.timers.delete(id);fn();};
 return bar;
}

test('recovery uses notification bar and clears live without hiding unrelated messages',()=>{
 const h=setup(),sockets=[],bar=notificationHarness(h);
 h.labels.set('env-recovery-notice',element());
 let opened=0;h.c.window.LogPanel={openEnvEditorModal(){opened++;}};
 h.c.WebSocket=class{static OPEN=1;constructor(){this.readyState=1;sockets.push(this);}};
 h.c.window.location={protocol:'http:',host:'billy.local'};h.run(wsSource);h.c.document.dispatchEvent({type:'DOMContentLoaded'});
 const send=recovery=>sockets[0].onmessage({data:JSON.stringify({type:'status',data:{status:'active',recovery}})});
 send({active:true,kind:'configuration',message:'Restored'});
 assert.equal(bar.textContent,'Restored');assert.equal(bar.classList.contains('hidden'),false);
 const action=bar.children[0];action.onclick();assert.equal(opened,1);
 send({active:true,kind:'configuration',message:'Restored'});assert.equal(bar.children[0],action);
 send(undefined);send({error:'unavailable'});assert.equal(bar.textContent,'Restored');
 h.c.showNotification('Saved','success');assert.equal(bar.textContent,'Saved');
 send({active:false,message:''});assert.equal(bar.textContent,'Saved');
 assert.equal(h.labels.get('env-recovery-notice').hidden,true);
 h.tick();h.tick();assert.equal(bar.classList.contains('hidden'),true);
 send({active:true,kind:'configuration',message:'New error <script>'});
 assert.equal(bar.textContent,'New error <script>');assert.equal(bar.children.length,1);
 assert.equal(bar.children[0].textContent,'Open .env editor');
 assert.equal(h.requests.length,0);
});

test('temporary notification returns to recovery and old timers cannot hide newer messages',()=>{
 const h=setup(),bar=notificationHarness(h);
 h.c.window.NotificationBar.setPersistent({message:'Repair needed',type:'warning'});
 h.c.showNotification('First');h.c.showNotification('Second','success');
 assert.equal(h.timers.size,1);assert.equal(bar.textContent,'Second');
 h.tick();assert.equal(bar.textContent,'Repair needed');assert.equal(h.timers.size,0);
 h.c.window.NotificationBar.setPersistent(null);assert.equal(h.timers.size,1);
 h.c.showNotification('Third');assert.equal(h.timers.size,1);
 assert.equal(bar.classList.contains('hidden'),false);
 h.tick();h.tick();assert.equal(bar.classList.contains('hidden'),true);
});

test('initial recovery snapshot populates existing bar before WebSocket connects',()=>{
 const h=setup(),bar=notificationHarness(h);
 bar.dataset.recoveryNotice=JSON.stringify({active:true,message:'Startup failed'});
 h.run(wsSource);
 assert.equal(bar.textContent,'Startup failed');
 assert.equal(bar.children[0].textContent,'Open Debug');
 assert.equal(h.timers.size,0);assert.equal(h.requests.length,0);
});


test('paired device auto-connect defaults off, persists through action and reflects live state',async()=>{
 const phone={Address:'AA:BB:CC:DD:EE:FF',Alias:'Phone',Paired:true,Connected:false};
 const h=setup({live:state({devices:[phone]})});h.run(btSource);h.c.window.BluetoothSettings.init();
 const checkbox=()=>h.root.get('bt-devices').children[0].children[1].children.at(-1).children[0];
 assert.equal(checkbox().checked,false);
 assert.equal(h.root.get('bt-devices').children[0].children[1].children.at(-1).children[2].textContent,'Auto-connect');
 h.fetch=async()=>({ok:true,json:async()=>state({devices:[{...phone,AutoConnect:true}]})});
 checkbox().checked=true;checkbox().dispatchEvent({type:'change'});await flush();
 assert.equal(h.requests[0].url,'/api/bluetooth/auto-connect');
 assert.deepEqual(JSON.parse(h.requests[0].options.body),{address:phone.Address,enabled:true});
 assert.equal(h.requests[0].options.headers['X-Bluetooth-Token'],'page-token');
 assert.equal(checkbox().checked,true);
 h.status(state({devices:[{...phone,AutoConnect:false}]}));assert.equal(checkbox().checked,false);
 h.status(state({devices:[{...phone,Paired:false,Connected:true}]}));
 assert.equal(h.root.get('bt-devices').children[0].children[0].children[1].children.length,2);
});


test('disconnect refresh and late cached live snapshot retain latest device state',async()=>{
 const phone={Address:'AA',Paired:true,Connected:true,MediaSupported:true,MediaConnected:false};
 const h=setup({live:state({sampled_at:10,devices:[phone]})});h.run(btSource);h.c.window.BluetoothSettings.init();
 assert.equal(h.root.get('bt-audio').textContent,'No Bluetooth audio playing.');
 h.fetch=async()=>({ok:true,json:async()=>state({sampled_at:12,devices:[{...phone,Connected:false}]})});
 h.root.get('bt-devices').children[0].children[0].dispatchEvent({type:'click'});await flush();
 h.status(state({sampled_at:11,devices:[phone]}));
 const row=h.root.get('bt-devices').children[0];
 assert.equal(row.children[0].children[1].children[1].textContent,'Disconnected');
 assert.deepEqual(row.children[1].children.filter(c=>c.type==='button').map(b=>b.children[1].textContent),['Forget']);
 assert.equal(h.root.get('bt-audio').textContent,'No phone connected.');
 h.status(state({sampled_at:13,devices:[phone],audio:{state:'media_audio',source:'bluez_input.AA.0'}}));
 assert.match(h.root.get('bt-audio').textContent,/Playing media/);
});

test('newer HTTP measurement wins over cached status received while request was pending',async()=>{
 const h=setup();let finish;h.fetch=()=>new Promise(resolve=>finish=resolve);
 h.run(btSource);h.c.window.BluetoothSettings.init();
 h.status(state({sampled_at:20,devices:[{Address:'AA',Paired:true,Connected:true}]}));
 finish({ok:true,json:async()=>state({sampled_at:21,devices:[{Address:'AA',Paired:true,Connected:false}]})});await flush();
 assert.equal(h.root.get('bt-audio').textContent,'No phone connected.');
});

test('failed disconnect remains an error and keeps Disconnect available',async()=>{
 const live=state({devices:[{Address:'AA',Paired:true,Connected:true}]});
 const h=setup({live});h.run(btSource);h.c.window.BluetoothSettings.init();
 h.fetch=async(url,options)=>options?.method==='POST'
  ? {ok:false,json:async()=>({error:'Phone is still connected'})}
  : {ok:true,json:async()=>live};
 h.root.get('bt-devices').children[0].children[0].dispatchEvent({type:'click'});await flush();
 assert.equal(h.root.get('bt-error').textContent,'Phone is still connected');
 assert.equal(h.root.get('bt-devices').children[0].dataset.selected,'true');
 assert.equal(h.root.get('bt-devices').children[0].children[0].disabled,false);
});


test('runtime failure opens Debug only; probation has no repair action',()=>{
 const h=setup(),bar=notificationHarness(h);h.labels.set('env-recovery-notice',element());
 let debug=0,editor=0;
 h.c.window.LogPanel={openLogPanel(){debug++;},openEnvEditorModal(){editor++;}};
 h.run(wsSource);
 h.c.window.updateRecoveryNotice({active:true,kind:'runtime',message:'Worker exited'});
 assert.equal(bar.children[0].textContent,'Open Debug');assert.equal(debug,0);assert.equal(editor,0);
 bar.children[0].onclick();assert.equal(debug,1);assert.equal(editor,0);
 assert.equal(h.labels.get('env-recovery-notice').hidden,true);
 assert.equal(h.labels.get('env-recovery-notice').textContent,'');
 // Identical text with a different kind must replace the action too.
 h.c.window.updateRecoveryNotice({active:true,kind:'configuration',message:'Worker exited'});
 assert.equal(bar.children[0].textContent,'Open .env editor');
 assert.equal(h.labels.get('env-recovery-notice').hidden,false);
 h.c.window.updateRecoveryNotice({active:true,kind:'startup',message:'Checking startup'});
 assert.equal(bar.children.length,0);assert.equal(h.labels.get('env-recovery-notice').hidden,true);
});


test('Open Debug opens an already visible panel without toggling it closed',async()=>{
 const h=setup();const panel=element(),button=element();
 const classes=new Set(['hidden']);
 panel.classList={remove(name){classes.delete(name);}};
 let highlighted=false,scrolled=0;
 button.classList={add(name){highlighted=name==='secondary-action--active-amber';}};
 panel.scrollIntoView=()=>scrolled++;
 h.labels.set('log-panel',panel);h.labels.set('toggle-log-btn',button);
 h.fetch=async()=>({ok:true,json:async()=>({logs:'Startup traceback'})});
 h.run(fs.readFileSync('application/webconfig/static/js/log-panel.js','utf8'));
 h.c.window.LogPanel.openLogPanel();await flush();
 h.c.window.LogPanel.openLogPanel();await flush();
 assert.equal(classes.has('hidden'),false);assert.equal(highlighted,true);assert.equal(scrolled,2);
 assert.deepEqual(h.requests.map(r=>r.url),['/logs?source=billy','/logs?source=billy']);
});


test('unchanged Bluetooth snapshots preserve rows and handlers while live details update',()=>{
 const phone={Address:'AA',Alias:'Phone',Paired:true,Connected:true,AutoConnect:false};
 const other={Address:'BB',Alias:'Other',Paired:true,Connected:false};
 const h=setup({live:state({devices:[phone,other]})});h.run(btSource);h.c.window.BluetoothSettings.init();
 const list=h.root.get('bt-devices'),row=list.children[0],second=list.children[1];
 const toggle=row.children[1].children.at(-1).children[0];
 for(let i=1;i<=5;i++)h.status(state({sampled_at:i,pairing_seconds:100-i,audio:{state:'idle',updated:i},devices:[{...phone,RSSI:-i,MediaConnected:!!(i%2)},other]}));
 assert.equal(list.children[0],row);assert.equal(list.children[1],second);
 assert.equal(h.root.get('bt-pair-status').textContent,'Visible for 95s');
 assert.equal(row.children[0].count('click'),1);
 h.status(state({devices:[{...phone,AutoConnect:true},{...other,Connected:true}]}));
 assert.equal(list.children[0],row);assert.notEqual(list.children[1],second);assert.equal(toggle.checked,true);
 h.status(state({devices:[phone]}));assert.equal(list.children.length,1);assert.equal(list.children[0],row);assert.equal(toggle.checked,false);
 h.status(state({devices:[{...phone,Connected:false}]}));assert.notEqual(list.children[0],row);
 assert.equal(list.children[0].children[0].children[1].children[1].textContent,'Disconnected');
 h.status(state());const empty=list.children[0];h.status(state());assert.equal(list.children[0],empty);
 h.status(state({devices:[phone]}));assert.equal(list.children.length,1);assert.notEqual(list.children[0],row);
});

test('audio badge and alias changes update only the affected Bluetooth row',()=>{
 const phone={Address:'AA:BB',Alias:'Phone',Paired:true,Connected:true};
 const h=setup({live:state({devices:[phone]})});h.run(btSource);h.c.window.BluetoothSettings.init();
 const list=h.root.get('bt-devices'),idle=list.children[0];
 h.status(state({devices:[phone],audio:{state:'media_audio',source:'bluez_input.AA_BB.0'}}));
 const playing=list.children[0];assert.notEqual(playing,idle);
 assert.equal(playing.children[0].children[1].children[1].children[0].textContent,'playing');
 h.status(state({devices:[phone],audio:{state:'media_audio',source:'bluez_input.AA_BB.1',updated:100}}));assert.equal(list.children[0],playing);
 h.status(state({devices:[{...phone,Alias:'Renamed'}]}));assert.equal(list.children[0].children[0].children[1].children[0].textContent,'Renamed');
});

test('failed auto-connect save restores checkbox without replacing its row',async()=>{
 const phone={Address:'AA',Paired:true,Connected:true,AutoConnect:false};
 const h=setup({live:state({devices:[phone]})});h.run(btSource);h.c.window.BluetoothSettings.init();
 const list=h.root.get('bt-devices'),row=list.children[0],checkbox=row.children[1].children.at(-1).children[0];
 h.fetch=async(url,options)=>options?.method==='POST'
  ? {ok:false,json:async()=>({error:'Save failed'})}
  : {ok:true,json:async()=>state({devices:[phone]})};
 checkbox.checked=true;checkbox.dispatchEvent({type:'change'});await flush();
 assert.equal(list.children[0],row);assert.equal(checkbox.checked,false);assert.equal(h.root.get('bt-error').textContent,'Save failed');
});


test('pair new device is hidden for any active connection and returns after disconnect',()=>{
 const h=setup({live:state()});h.run(btSource);h.c.window.BluetoothSettings.init();
 const pair=h.root.get('bt-pair');
 assert.equal(pair.hidden,false);assert.equal(pair.disabled,false);
 for(const extra of [{Paired:true},{Paired:false},{Paired:true,MediaConnected:false}]){
  h.status(state({devices:[{Address:'AA',Alias:'Phone',Connected:true,...extra}]}));
  assert.equal(pair.hidden,true);assert.equal(pair.disabled,true);
 }
 h.status(state({devices:[{Address:'AA',Alias:'Phone',Paired:true,Connected:false}]}));
 assert.equal(pair.hidden,false);assert.equal(pair.disabled,false);
});
test('pairing confirmation and stop control stay available while a phone is connecting',()=>{
 const h=setup({live:state()});h.run(btSource);h.c.window.BluetoothSettings.init();
 h.status(state({pairing_seconds:45,devices:[{Address:'AA',Connected:true,Paired:false}],pending:{name:'Phone',passkey:'012345'}}));
 assert.equal(h.root.get('bt-pair').hidden,true);
 assert.equal(h.root.get('bt-pair-stop').hidden,false);assert.equal(h.root.get('bt-pending').hidden,false);
 assert.equal(h.root.get('bt-code').textContent,'012345');
});
test('other disconnected paired devices do not hide pairing; setup errors hide pairing',()=>{
 const h=setup({live:state({devices:[{Address:'AA',Paired:true,Connected:false},{Address:'BB',Paired:true,Connected:false}]})});h.run(btSource);h.c.window.BluetoothSettings.init();
 assert.equal(h.root.get('bt-pair').hidden,false);
 h.status(state({setup:{state:'missing'},devices:[{Address:'AA',Connected:true}]}));
 assert.equal(h.root.get('bt-pair').hidden,true);
});


test('selecting another paired phone requests one server-side switch without optimistic selection',async()=>{
 const phone={Address:'AA',Alias:'Phone',Paired:true,Connected:true};
 const other={Address:'BB',Alias:'Other',Paired:true,Connected:false};
 const h=setup({live:state({devices:[phone,other]})});h.run(btSource);h.c.window.BluetoothSettings.init();
 const list=h.root.get('bt-devices'),otherRow=list.children[1];
 assert.equal(otherRow.children[0].disabled,false);
 h.status(state({devices:[{...phone,Connected:false},other]}));
 assert.equal(list.children[1],otherRow,'another phone changing must not replace the unchanged target');
 h.status(state({devices:[phone,other]}));
 let complete;
 h.fetch=async(url,options)=>options?.method==='POST'
  ? new Promise(resolve=>complete=resolve)
  : {ok:true,json:async()=>state({devices:[{...phone,Connected:false},{...other,Connected:true}]})};
 otherRow.children[0].dispatchEvent({type:'click'});
 otherRow.children[0].dispatchEvent({type:'click'});
 assert.equal(h.requests.length,1);
 assert.equal(h.requests[0].url,'/api/bluetooth/select');
 assert.equal(JSON.parse(h.requests[0].options.body).address,'BB');
 assert.equal(otherRow.dataset.selected,'false');
 assert.equal(list.children[0].dataset.selected,'true');
 complete({ok:true,json:async()=>({ok:true})});await flush();
 assert.equal(list.children[0].dataset.selected,'false');
 assert.equal(list.children[1].dataset.selected,'true');
 assert.equal(h.root.get('bt-pair').hidden,true);
});

test('Forget follows its own device connection state, including after a live disconnect',()=>{
 const connected={Address:'AA',Alias:'Phone',Paired:true,Connected:true};
 const other={Address:'BB',Alias:'Other',Paired:true,Connected:false};
 const h=setup({live:state({devices:[connected,other]})});h.run(btSource);h.c.window.BluetoothSettings.init();
 const list=h.root.get('bt-devices');
 const actions=row=>row.children[1].children.filter(child=>child.type==='button').map(button=>button.children[1].textContent);
 assert.deepEqual(actions(list.children[0]),[]);
 assert.deepEqual(actions(list.children[1]),['Forget']);
 h.status(state({devices:[{...connected,Connected:false},{...other,Connected:true}]}));
 assert.deepEqual(actions(list.children[0]),['Forget']);
 assert.deepEqual(actions(list.children[1]),[]);
 h.status(state({devices:[{...connected,Connected:false},other]}));
 assert.deepEqual(actions(list.children[0]),['Forget']);
 assert.deepEqual(actions(list.children[1]),['Forget']);
});


test('card selection connects once and waits for confirmed state; failed connect stays unselected',async()=>{
 const phone={Address:'AA',Alias:'Phone',Paired:true,Connected:false};
 const live=state({devices:[phone]});
 const h=setup({live});h.run(btSource);h.c.window.BluetoothSettings.init();
 const row=h.root.get('bt-devices').children[0],select=row.children[0];
 assert.equal(select.type,'button');
 assert.equal(select.attributes['aria-label'],'Phone');
 assert.equal(select.attributes['aria-pressed'],'false');
 let complete;
 h.fetch=async(url,options)=>options?.method==='POST'
  ? new Promise(resolve=>complete=resolve) : {ok:true,json:async()=>live};
 select.dispatchEvent({type:'click'});select.dispatchEvent({type:'click'});
 assert.equal(h.requests.length,1);
 assert.equal(h.requests[0].url,'/api/bluetooth/select');
 assert.equal(JSON.parse(h.requests[0].options.body).address,'AA');
 assert.equal(row.dataset.selected,'false');
 complete({ok:false,json:async()=>({error:'Could not connect'})});await flush();
 assert.equal(h.root.get('bt-devices').children[0],row);
 assert.equal(row.dataset.selected,'false');
 assert.equal(h.root.get('bt-error').textContent,'Could not connect');
 h.status(state({devices:[{...phone,Connected:true}]}));
 assert.equal(h.root.get('bt-devices').children[0].dataset.selected,'true');
});

test('Forget only sends forget and auto-connect remains a separate sibling control',async()=>{
 const live=state({devices:[{Address:'AA',Alias:'Phone',Paired:true}]});
 const h=setup({live});h.run(btSource);h.c.window.BluetoothSettings.init();
 const row=h.root.get('bt-devices').children[0];
 h.c.window.confirm=()=>true;
 h.fetch=async()=>({ok:true,json:async()=>live});
 row.children[1].children[0].dispatchEvent({type:'click'});await flush();
 assert.deepEqual(h.requests.filter(r=>r.options?.method==='POST').map(r=>r.url),['/api/bluetooth/forget']);
 assert.equal(row.count('click'),0,'the card wrapper must not capture sibling control clicks');
});
