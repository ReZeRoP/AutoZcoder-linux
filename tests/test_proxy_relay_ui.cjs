"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const {Supervisor} = require("../zcode_gateway/dashboard/connection.js");
const html = fs.readFileSync(path.join(__dirname,"../zcode_gateway/dashboard/index.html"),"utf8");
const flush = async () => { for (let i=0;i<100;i++) await Promise.resolve(); };
const deferred = () => { let resolve,reject; const promise=new Promise((a,b)=>{resolve=a;reject=b;}); return {promise,resolve,reject}; };
class Clock {
  constructor(){this.now=0;this.next=1;this.jobs=new Map();}
  setTimeout(fn,delay){const id=this.next++;this.jobs.set(id,{fn,at:this.now+delay});return id;}
  clearTimeout(id){this.jobs.delete(id);}
  async advance(ms){const end=this.now+ms;for(;;){const due=[...this.jobs].filter(([,v])=>v.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!due)break;this.now=due[1].at;this.jobs.delete(due[0]);due[1].fn();await flush();}this.now=end;await flush();}
}
const clone = value => JSON.parse(JSON.stringify(value));
const response = (data,status=200) => ({ok:status>=200&&status<300,status,json:async()=>clone(data)});
function functionSource(name){
  const starts=[html.indexOf("  function "+name+"("),html.indexOf("  async function "+name+"(")].filter(x=>x>=0);
  assert.ok(starts.length,name+" exists");const start=Math.min(...starts),end=html.indexOf("\n  }",start)+4;
  return html.slice(start,end);
}
function block(start,end){return html.slice(html.indexOf(start),html.indexOf(end,html.indexOf(start)));}
function fixture(options={}){
  const clock = new Clock(), controls = new Map(), calls = [], messages = [], confirmations=[];
  const storage = options.storage || new Map();
  const make = (value="")=>({value,checked:false,disabled:false,innerHTML:"",textContent:"",style:{},dataset:{},options:[],
    querySelectorAll(){return [];},replaceChildren(){this.innerHTML="";},appendChild(x){this.options.push(x);},setAttribute(k,v){this[k]=v;}});
  for(const id of ["relay-card","relay-list","relay-enabled","relay-mod-53","relay-mod-flash","relay-strategy","relay-status-msg","relay-job-status","relay-modal","relay-modal-card","relay-modal-job","btn-deploy-relay","rf-vc-token","rf-vc-name","rf-cs-name","rf-cs-url","px-auto-verify","px-auto-interval","px-auto-state","px-maint-msg","px-countries-msg","px-btn-stop","px-health"])controls.set("#"+id,make());
  controls.get("#px-auto-interval").options=[30,45,90,180].map(x=>({value:String(x)}));
  controls.get("#px-auto-interval").value="45";
  controls.get("#relay-strategy").value="single";
  controls.get("#rf-vc-token").value="SYNTHETIC-TOKEN-never-store";
  const f={clock,controls,calls,messages,confirmations,storage,refreshes:0,restores:0,
    relays:{enabled:true,models:["GLM-5.3"],strategy:"single",active_id:"a",relays:[{id:"a",name:"A",url:"https://relay.invalid",type:"custom"}]},
    auto:{auto_verify:false,auto_interval_min:45,last_verified_at:null},job:null,generation:"admission-one"};
  const s=new Supervisor({clock,instance:{schema_version:1,product:"autozcoder-linux",version:"1.3.0",instance_id:"boot-one",config_id:"config-one",mode:"user-service",stopping:false},key:"fixture-key",
    fetch:async(url,opts)=>{
      const body=opts.body?JSON.parse(opts.body):null;
      calls.push({url,method:opts.method||"GET",body,opts,time:clock.now});
      if(options.fetch){const r=options.fetch(url,opts,body,f);if(r!==undefined)return r;}
      if(url==="/admin/dash/relays/settings"){Object.assign(f.relays,body);return response({settings:f.relays});}
      if(url==="/admin/dash/relays")return response(f.relays);
      if(url==="/admin/dash/proxy/auto_verify"){
        if(opts.method!=="POST")return response({detail:"Method Not Allowed"},405);
        if(typeof body?.enabled!=="boolean")return response({detail:"enabled must be boolean"},400);
        f.auto={auto_verify:body.enabled,auto_interval_min:body.interval_min,last_verified_at:null};
        return response({auto_verify:body.enabled,interval_min:body.interval_min,last_verified_at:null});
      }
      if(url==="/admin/dash/proxy/socks_ready"){
        if(opts.method&&opts.method!=="GET")return response({detail:"Method Not Allowed"},405);
        return response(f.auto);
      }
      if(url.startsWith("/admin/dash/relays/deploy/jobs")){
        if(opts.method==="POST")f.job={id:body.operation_id,provider:body.type,name:"Fixture",status:"running",phase:"creating",started_at:123,finished_at:null,error:null,result:null,remote:{}};
        return response({generation:f.generation,active_id:f.job?.status==="running"?f.job.id:null,job:f.job},opts.method==="POST"?202:200);
      }
      if(url==="/admin/dash/generator/proxies")return response({running:false,stats:{},lines:[]});
      if(url==="/admin/dash/generator/proxies/run" || url==="/admin/dash/generator/proxies/maintain")return response({started:body.action});
      if(url==="/admin/dash/generator/proxies/countries")return response({started:"countries"});
      return response({deleted:1});
    }});
  s.running=true;s._state("online");f.connection=s;
  const context={console,connection:s,curTab:"proxy",serverUp:true,crypto:{randomUUID:()=>"00000000-0000-4000-8000-000000000001"},
    api:(...args)=>s.request(...args),sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    $:id=>controls.get(id)||null,document:{querySelectorAll:()=>[],createElement:()=>make()},
    esc:s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])),
    toast:(...args)=>messages.push(args),confirmDlg:async(...args)=>{confirmations.push(args);return options.confirm?options.confirm(...args):true;},
    preserveView:()=>()=>{f.restores++;},refresh:()=>{f.refreshes++;},genSetupCard:s=>s,pxMounted:false,pxCache:{},pxWasRunning:false,
    pxReasonsHtml:()=>"",genLoadSources(){},proxyYield(){},renderProxyHealth(){},timeAgo:()=>"2m ago"};
  vm.createContext(context);
  vm.runInContext(block("  let relayData =", "  async function renderProxyTab("),context);
  vm.runInContext(block("  let pxActionBusy =", "  async function proxyYield("),context);
  vm.runInContext(block("  const pxAutoState =", "  /* ------------ email system"),context);
  f.context=context;f.read=code=>vm.runInContext(code,context);f.control=id=>controls.get("#"+id);
  f.seed=async()=>{await context.loadRelays();await flush();};
  f.stop=()=>{s.stop();};
  return f;
}

test("actual relay saves are field-only, serialized, and never replay offline",async()=>{
  let pending;
  const f=fixture({fetch:(url,opts,body,f)=>{
    if(url.endsWith("/settings")&&body.active_id){pending=deferred();return pending.promise;}
  }});await f.seed();
  const select=f.context.setActiveRelay("b");await flush();
  f.control("relay-enabled").checked=false;await f.context.saveRelayOptions("enabled");
  assert.equal(f.calls.filter(x=>x.method==="POST").length,1);
  f.relays.active_id="b";pending.resolve(response({settings:f.relays}));await select;await flush();
  f.control("relay-enabled").checked=false;await f.context.saveRelayOptions("enabled");await flush();
  assert.deepEqual(f.calls.filter(x=>x.method==="POST").map(x=>x.body),[{active_id:"b"},{enabled:false}]);
  assert.equal(f.relays.active_id,"b");
  f.connection.offline(new Error("fixture disconnect"));
  await f.context.saveRelayOptions("enabled");f.connection._state("online");await f.clock.advance(0);
  assert.equal(f.calls.filter(x=>x.method==="POST").length,2);f.stop();
});

test("pre-mutation deduplicated GET settles before exactly one trailing refresh",async()=>{
  const old=deferred();let hold=false,reads=0;
  const f=fixture({fetch:(url,opts,body,f)=>{
    if(url==="/admin/dash/relays"){reads++;if(hold){hold=false;return old.promise;}}
    if(url.endsWith("/a")&&opts.method==="DELETE"){f.relays.relays=[];return response({ok:true});}
  }});await f.seed();hold=true;
  const before=f.context.loadRelays();await flush();
  await f.context.deleteRelay("a");await flush();
  f.context.loadRelays();f.context.loadRelays();assert.equal(reads,2);
  old.resolve(response({enabled:true,relays:[{id:"a",name:"OLD-DELETED",url:"https://old.invalid"}]}));
  await before;await flush();assert.equal(reads,3);
  assert.deepEqual(clone(f.read("relayData.relays")),[]);
  assert.ok(!f.control("relay-list").innerHTML.includes("OLD-DELETED"));f.stop();
});

test("post-add and probe refreshes are not discarded; manual probe uses 25 seconds",async()=>{
  let hold=false;const old=deferred();
  const f=fixture({fetch:(url,opts,body,f)=>{
    if(url==="/admin/dash/relays"&&hold){hold=false;return old.promise;}
    if(url.endsWith("/add")){f.relays.relays.push({id:"b",name:"ADDED",url:body.url,type:"custom"});return response({ok:true});}
    if(url.endsWith("/test"))return new Promise(resolve=>f.clock.setTimeout(()=>resolve(response({ok:true,probe_kind:"forwarding",probe_verified:true,elapsed_ms:6000})),6000));
  }});await f.seed();hold=true;f.context.loadRelays();await flush();
  f.control("rf-cs-url").value="https://new.invalid";await f.context.relayAddCustom();
  old.resolve(response({relays:[]}));await flush();
  assert.match(f.control("relay-list").innerHTML,/ADDED/);
  const probe=f.context.testRelay("a");await f.clock.advance(5001);assert.equal(f.connection.connected,true);
  await f.clock.advance(1000);await probe;await flush();
  assert.match(f.messages.at(-1)[0],/Forwarding verified.*Model \/ SSE not tested/);
  assert.ok(!Object.hasOwn(f.calls.find(x=>x.url.endsWith("/test")).opts,"timeoutMs"));f.stop();
});

test("navigation and disconnect invalidate relay reads without repainting another view",async()=>{
  const old=deferred();const f=fixture({fetch:url=>url==="/admin/dash/relays"?old.promise:undefined});
  const pending=f.context.loadRelays();await flush();f.context.curTab="settings";f.read("relayRevision++");
  f.control("relay-list").innerHTML="OTHER VIEW";old.resolve(response({relays:[]}));await pending;await flush();
  assert.equal(f.control("relay-list").innerHTML,"OTHER VIEW");assert.equal(f.read("relayData"),null);f.stop();
  const next=deferred();const g=fixture({fetch:url=>url==="/admin/dash/relays"?next.promise:undefined});
  const request=g.context.loadRelays();await flush();g.connection.offline(new Error("offline"));
  next.resolve(response({relays:[]}));await request;await flush();assert.equal(g.read("relayData"),null);g.stop();
});

test("truthful ages, legacy success, failed no-status, escaped provider and credential-free titles",async()=>{
  const f=fixture();f.relays.relays=[
    {id:"a",type:"<b id='injected'>x</b>",url:"https://user:pa@ss@relay.invalid/path",last_status:200,last_tested_at:123},
    {id:"b",type:"constructor",url:"socks5://user:p@a@s@s@proxy.invalid:9",last_status:0,last_error:"Failed https://user:pa@ss@host.invalid",last_tested_at:123},
    {id:"c",type:"netlify",url:"https://safe.invalid",last_status:200,last_tested_at:123,last_probe_kind:"forwarding",last_probe_verified:true},
    {id:"d",type:"__proto__",url:"https://safe.invalid"}];await f.seed();
  const markup=f.control("relay-list").innerHTML;
  assert.match(markup,/&lt;b/);assert.ok(!markup.includes("<b id='injected'"));assert.ok(!markup.includes("function Object"));
  assert.match(markup,/constructor/);assert.match(markup,/__proto__/);assert.match(markup,/Legacy buffered/);
  assert.match(markup,/legacy \/ unverified/);assert.match(markup,/Probe failed \(no status\)/);assert.match(markup,/Never tested/);
  assert.equal((markup.match(/checked \d+m ago/g)||[]).length,3);assert.match(markup,/Forwarding verified/);
  assert.ok(!markup.includes("user:"));assert.ok(!markup.includes("pa@ss"));assert.ok(!markup.includes("p@a@s@s"));
  assert.ok(f.restores>0);f.stop();
});

test("failed initial load is not an empty list; failed refresh marks retained rows stale",async()=>{
  let fail=true;const f=fixture({fetch:url=>url==="/admin/dash/relays"&&fail?response({detail:"unavailable"},500):undefined});
  await f.seed();assert.match(f.control("relay-status-msg").textContent,/Could not load/);
  assert.ok(!f.control("relay-list").innerHTML.includes("No edge relays configured"));
  fail=false;await f.seed();fail=true;await f.seed();assert.match(f.control("relay-status-msg").textContent,/Stale snapshot.*unavailable/);
  assert.match(f.control("relay-list").innerHTML,/relay.invalid/);f.stop();
});

test("maintenance checks ack, serializes conflicts, confirms reset and rejects changed connectivity",async()=>{
  const ack=deferred(),confirm=deferred();let confirmationAsync=false;
  const f=fixture({confirm:()=>confirmationAsync?confirm.promise:true,fetch:url=>url.endsWith("/maintain")?ack.promise:undefined});
  const first=f.context.proxyMaintain("clear-used");await flush();
  await f.context.proxyRun("all");assert.equal(f.calls.filter(x=>x.method==="POST").length,1);
  assert.equal(f.confirmations[0][3],true);
  ack.resolve(response({error:"checker already running",started:false}));await first;
  assert.match(f.control("px-maint-msg").textContent,/failed: checker already running/);
  assert.ok(!f.messages.some(x=>x[0]==="clear-used started"));
  confirmationAsync=true;const next=f.context.proxyMaintain("prune");await flush();
  f.connection.offline(new Error("disconnect"));f.connection._state("online");confirm.resolve(true);await next;
  assert.equal(f.calls.filter(x=>x.method==="POST").length,1);f.stop();
});

test("actual proxy acknowledgement uses matching action strings for run, maintenance and countries",async()=>{
  for (const [method,action,endpoint,expected] of [
    ["proxyRun","verify","run","verify"],
    ["proxyMaintain","clear-dead","maintain","clear-dead"],
    ["proxyCountries",undefined,"countries","countries"]
  ]) {
    const f=fixture({fetch:url=>url==="/admin/dash/generator/proxies/"+endpoint?response({started:expected}):undefined});
    await f.context[method](action);
    assert.equal(f.calls[0].url,"/admin/dash/generator/proxies/"+endpoint);assert.equal(f.calls[0].method,"POST");
    assert.equal(f.calls[0].body.action,action);
    assert.equal(f.read("pxCheckerRunning"),true);
    assert.ok(f.messages.some(([message,kind])=>message.endsWith(" started")&&kind==="ok"));
    assert.match(f.control(endpoint==="countries"?"px-countries-msg":"px-maint-msg").textContent,/completion not yet verified/);f.stop();
  }
  for (const ack of [{started:false},{started:true},{started:""},{started:"other"},{started:"verify",error:"checker already running"}]) {
    const f=fixture({fetch:url=>url.endsWith("/run")?response(ack):undefined});
    await f.context.proxyRun("verify");assert.equal(f.read("pxCheckerRunning"),false);
    assert.ok(!f.messages.some(([,kind])=>kind==="ok"));assert.match(f.messages.at(-1)[0],/failed/);f.stop();
  }
});

test("checker run errors are handled and exited/cancelled is not called success",async()=>{
  const f=fixture({fetch:url=>url.endsWith("/run")?response({started:false}):undefined});
  await f.context.proxyRun("recheck");assert.match(f.messages.at(-1)[0],/did not start/);f.stop();
  const source=functionSource("renderProxyTab");assert.ok(!source.includes("✓ finished"));assert.match(source,/Process exited — inspect log for outcome/);
});

test("auto-verify state and interval apply atomically before painting, including interval-only change",async()=>{
  const f=fixture();f.auto={auto_verify:true,auto_interval_min:90,last_verified_at:123};
  await f.context.pxAutoRefresh();assert.equal(f.control("px-auto-verify").checked,true);assert.equal(f.control("px-auto-interval").value,"90");
  assert.equal(f.calls[0].url,"/admin/dash/proxy/socks_ready");assert.equal(f.calls[0].method,"GET");
  f.control("px-auto-interval").value="180";await f.context.pxAutoVerify(true);await flush();
  const post=f.calls.find(x=>x.method==="POST");
  assert.equal(post.url,"/admin/dash/proxy/auto_verify");assert.equal(post.method,"POST");
  assert.deepEqual(post.body,{enabled:true,interval_min:180});
  assert.equal(f.control("px-auto-verify").checked,true);assert.equal(f.control("px-auto-interval").value,"180");
  assert.match(html,/id="px-auto-interval" onchange="App.pxAutoVerify/);
  f.control("px-auto-interval").value="NaN";await f.context.pxAutoVerify(true);assert.equal(f.calls.filter(x=>x.method==="POST").length,1);f.stop();
});

test("auto stale GET cannot undo saved enabled/interval and waits for dedupe before trailing read",async()=>{
  let hold=false,reads=0;const old=deferred();
  const f=fixture({fetch:(url,opts)=>{if(url.endsWith("/socks_ready")&&(!opts.method||opts.method==="GET")){reads++;if(hold){hold=false;return old.promise;}}}});
  await f.context.pxAutoRefresh();hold=true;const pending=f.context.pxAutoRefresh();await flush();
  f.control("px-auto-interval").value="90";await f.context.pxAutoVerify(true);assert.equal(reads,2);
  old.resolve(response({auto_verify:false,auto_interval_min:30,last_verified_at:null}));await pending;await flush();
  assert.equal(reads,3);assert.equal(f.control("px-auto-verify").checked,true);assert.equal(f.control("px-auto-interval").value,"90");f.stop();
});

test("deployment start is one short POST, polling passes five seconds and clears credentials",async()=>{
  const f=fixture();f.context.openRelayModal("vercel");await flush();
  await Promise.all([f.context.relayDeploy("vercel"),f.context.relayDeploy("vercel")]);await flush();
  assert.equal(f.calls.filter(x=>x.method==="POST").length,1);assert.equal(f.connection.polls.size,1);
  assert.equal(f.control("rf-vc-token").value,"");assert.equal(f.storage.size,1);
  const saved=JSON.parse([...f.storage.values()][0]);assert.deepEqual(Object.keys(saved).sort(),["config_id","operation_id"]);
  assert.ok(!JSON.stringify(saved).includes("TOKEN"));assert.match(saved.operation_id,/^admission-one:/);
  await f.clock.advance(6500);assert.equal(f.connection.connected,true);assert.equal(f.read("relayJob.status"),"running");
  f.job.status="succeeded";f.job.finished_at=124;await f.clock.advance(1500);
  assert.match(f.control("relay-job-status").innerHTML,/Deployment succeeded/);assert.equal(f.connection.polls.has("relay-deploy"),false);f.stop();
});

test("lost deployment ACK resumes own ID after reconnect/reload with GET only",async()=>{
  const f=fixture({fetch:(url,opts,body,f)=>{
    if(url.endsWith("/deploy/jobs")&&opts.method==="POST"){
      f.job={id:body.operation_id,provider:"vercel",status:"running",phase:"creating"};return new Promise(()=>{});
    }
  }});f.context.openRelayModal("vercel");await flush();
  const submit=f.context.relayDeploy("vercel");await flush();await f.clock.advance(5000);await submit;
  assert.equal(f.connection.connected,false);const saved=f.read("relayOperationId");assert.ok(saved);
  f.connection._state("online");await f.context.relayRefreshJob();await f.context.relayDeploy("vercel");
  assert.equal(f.calls.filter(x=>x.method==="POST").length,1);
  assert.ok(f.calls.some(x=>x.url.includes("?id="+encodeURIComponent(saved))));
  const g=fixture({storage:f.storage});g.job=f.job;await g.context.relayRefreshJob();
  assert.equal(g.read("relayOperationId"),saved);assert.equal(g.calls.filter(x=>x.method==="POST").length,0);
  f.stop();g.stop();
});

test("deployment 404/410 is explicit unknown and never retries POST",async()=>{
  for(const code of [404,410]){
    const storage=new Map([["gw-relay-deploy:config-one",JSON.stringify({config_id:"config-one",operation_id:"old:uuid"})]]);
    const f=fixture({storage,fetch:url=>url.includes("?id=")?response({detail:"not retained"},code):undefined});
    await f.context.relayRefreshJob();assert.equal(f.read("relayJobState"),"unknown");
    assert.match(f.control("relay-job-status").innerHTML,/reconciliation required/);
    f.context.openRelayModal("vercel");await f.context.relayDeploy("vercel");await flush();
    assert.equal(f.calls.filter(x=>x.method==="POST").length,0);assert.equal(f.connection.polls.has("relay-deploy"),false);f.stop();
  }
});

test("409 active conflict adopts different active ID by GET, payload conflict keeps own ID",async()=>{
  for(const code of ["deployment_active","operation_payload_conflict"]){
    const f=fixture({fetch:(url,opts,body,f)=>{
      if(url.endsWith("/jobs")&&opts.method==="POST")return response({detail:"busy/conflict",code,active_id:"admission-one:other"},409);
      if(url.includes("?id="))return response({generation:f.generation,active_id:null,job:{id:decodeURIComponent(url.split("?id=")[1]),status:"failed",provider:"vercel"}});
    }});f.context.openRelayModal("vercel");await flush();await f.context.relayDeploy("vercel");await flush();
    const id=f.read("relayOperationId");assert.equal(id==="admission-one:other",code==="deployment_active");
    await f.context.relayRefreshJob();assert.equal(f.calls.filter(x=>x.method==="POST").length,1);
    assert.ok(!f.messages.some(x=>x[0].includes("succeeded")));f.stop();
  }
});

test("closing modal never cancels accepted deployment; navigation stops named observer",async()=>{
  const f=fixture();f.context.openRelayModal("vercel");await flush();await f.context.relayDeploy("vercel");
  f.context.closeRelayModal();assert.equal(f.connection.polls.has("relay-deploy"),true);
  f.context.curTab="models";f.context.relayJobPolling();assert.equal(f.connection.polls.has("relay-deploy"),false);
  assert.equal(f.calls.filter(x=>x.method==="POST").length,1);assert.ok(!f.calls.some(x=>x.url.includes("cancel")));
  f.context.curTab="proxy";await f.context.relayRefreshJob();assert.equal(f.connection.polls.has("relay-deploy"),true);f.stop();
});

test("closing during preflight cancels unsent creation and storage failure fails closed",async()=>{
  const gate=deferred();let hold=false;
  const f=fixture({fetch:(url,opts)=>url.endsWith("/jobs")&&hold&&!opts.method?gate.promise:undefined});
  f.context.openRelayModal("vercel");await flush();hold=true;const pending=f.context.relayDeploy("vercel");await flush();
  f.context.closeRelayModal();gate.resolve(response({generation:"one",active_id:null,job:null}));await pending;
  assert.equal(f.calls.filter(x=>x.method==="POST").length,0);f.stop();
  const g=fixture();g.context.openRelayModal("vercel");await flush();g.context.sessionStorage.setItem=()=>{throw new Error("storage denied");};
  await g.context.relayDeploy("vercel");assert.equal(g.calls.filter(x=>x.method==="POST").length,0);g.stop();
});

test("registration failure retries only local register endpoint and Netlify new deploy is gated",async()=>{
  const f=fixture({fetch:(url,opts,body,f)=>url.endsWith("/register")?response({generation:f.generation,active_id:null,job:{...f.job,status:"succeeded"}}):undefined});
  f.job={id:"admission-one:old",provider:"vercel",status:"registration_failed",phase:"registering"};
  await f.context.relayRefreshJob();await f.context.relayRegisterJob();
  assert.deepEqual(f.calls.filter(x=>x.method==="POST").map(x=>x.url),["/admin/dash/relays/deploy/jobs/admission-one%3Aold/register"]);
  f.context.openRelayModal("netlify");await f.context.relayDeploy("netlify");
  assert.equal(f.calls.filter(x=>x.method==="POST").length,1);assert.match(f.messages.at(-1)[0],/unsupported \/ buffered/);f.stop();
});

test("named deployment polling never races a slow start acknowledgement",async()=>{
  const accepted=deferred();
  const f=fixture({fetch:(url,opts,body,f)=>{
    if(url.endsWith("/jobs")&&opts.method==="POST")return accepted.promise;
  }});f.context.openRelayModal("vercel");await flush();
  const submit=f.context.relayDeploy("vercel");await flush();
  const count=f.calls.length;await f.clock.advance(3000);
  assert.equal(f.calls.length,count);assert.equal(f.connection.polls.has("relay-deploy"),false);
  const id=f.read("relayOperationId");accepted.resolve(response({generation:f.generation,active_id:id,job:{id,status:"running",provider:"vercel"}},202));
  await submit;assert.equal(f.connection.polls.has("relay-deploy"),true);f.stop();
});

test("explicit prepare-new does not re-adopt the last terminal job on modal reopen",async()=>{
  const f=fixture();f.job={id:"admission-one:old",status:"succeeded",provider:"vercel"};
  await f.context.relayRefreshJob();assert.equal(f.read("relayOperationId"),"admission-one:old");
  await f.context.relayNewDeployment();f.context.openRelayModal("vercel");await flush();
  assert.equal(f.read("relayOperationId"),null);assert.equal(f.control("btn-deploy-relay").disabled,false);
  assert.equal(f.calls.filter(x=>x.method==="POST").length,0);f.stop();
});

test("actual proxy health table redacts embedded @ in both display and tooltip",async()=>{
  const f=fixture({fetch:url=>url.endsWith("/health")?response({available:true,stats:{total:1},proxies:[{url:"socks5://user:pa@ss@proxy.invalid:9",status:"failing",reason:"https://x:se@cret@bad.invalid"}]}):undefined});
  vm.runInContext(functionSource("renderProxyHealth"),f.context);
  const container=f.control("px-health");await f.context.renderProxyHealth(container);
  assert.ok(!container.innerHTML.includes("pa@ss"));assert.ok(!container.innerHTML.includes("se@cret"));
  assert.match(container.innerHTML,/\*\*\*@proxy.invalid/);f.stop();
});
