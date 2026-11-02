"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {Supervisor, validInstance, parseBootstrap, RETRY} = require("../zcode_gateway/dashboard/connection.js");
const instance = (id = "boot-one") => ({schema_version:1, product:"autozcoder-linux", version:"1.3.0",
  instance_id:id, config_id:"config-one", mode:"user-service", stopping:false});
const flush = async () => { for (let i=0;i<60;i++) await Promise.resolve(); };
class Clock {
  constructor(){ this.now=0; this.next=1; this.jobs=new Map(); }
  setTimeout(fn, delay){ const id=this.next++; this.jobs.set(id,{fn, at:this.now+delay}); return id; }
  clearTimeout(id){ this.jobs.delete(id); }
  async advance(ms){
    const end=this.now+ms;
    while(true){
      const due=[...this.jobs].filter(([,x])=>x.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
      if(!due) break;
      this.now=due[1].at; this.jobs.delete(due[0]); due[1].fn(); await flush();
    }
    this.now=end; await flush();
  }
}
function response(data, status=200){ return {ok:status===200,status,json:async()=>data,text:async()=>String(data)}; }
function fixture(options={}){
  const clock = new Clock();
  const requests=[];
  const f={clock,requests,offline:false,authFail:false,identity:instance(),key:"key-one",states:[],online:0,credentials:[]};
  const fetch=async(url,opts)=>{
    requests.push({url,opts,time:clock.now});
    if(f.offline) throw new Error("offline");
    if(options.fetch) { const r=options.fetch(url,opts,f); if(r!==undefined) return r; }
    if(url==="/health/instance") return response(f.identity);
    if(url==="/health") return response({status:"ok"});
    if(url==="/dash") return response('<script id="autozcoder-bootstrap" type="application/json">'+JSON.stringify({key:f.key,instance:f.identity})+'</script>');
    if(url==="/admin/dash/lifecycle") return response(f.identity,f.authFail?401:200);
    return response({ok:true});
  };
  f.supervisor=new Supervisor({clock,fetch,key:f.key,instance:f.identity,
    timeout:options.timeout||5000,onState:s=>f.states.push(s),onOnline:()=>{f.online++;},
    onCredentials:(key,id)=>f.credentials.push({key,id})});
  return f;
}
test("strict identity and inert JSON bootstrap",()=>{
  assert.equal(validInstance(instance(),"config-one"),true);
  assert.equal(validInstance(instance(),"other"),false);
  assert.equal(validInstance({...instance(),stopping:"false"}),false);
  assert.equal(validInstance({...instance(),product:"foreign"}),false);
  const html='<script id="autozcoder-bootstrap" type="application/json">'+JSON.stringify({key:"fresh",instance:instance()})+'</script>';
  assert.equal(parseBootstrap(html).key,"fresh");
  assert.throws(()=>parseBootstrap('<script>alert("not executed")</script>'));
  assert.throws(()=>parseBootstrap(html.replace('"fresh"','null')));
});
test("idempotent start owns one probe and one named poll",async()=>{
  const f=fixture(); const s=f.supervisor;
  s.start(); s.start(); s.start(); await f.clock.advance(0);
  assert.equal(f.online,1); assert.equal(s.state,"online");
  let polls=0; const run=async()=>{polls++; await s.request("/poll");};
  s.poll("tab",run,1000); s.poll("tab",run,1000); s.poll("tab",run,1000);
  await f.clock.advance(3100);
  assert.equal(polls,3); assert.equal(s.polls.size,1);
  assert.equal(f.requests.filter(r=>r.url==="/health/instance").length,1);
  s.stop(); assert.equal(f.clock.jobs.size,0);
});
test("offline backoff is capped and recovery does not replay a write",async()=>{
  const f=fixture(); const s=f.supervisor; f.offline=true;
  s.start(); await f.clock.advance(0);
  for(const delay of RETRY) await f.clock.advance(delay);
  assert.deepEqual(f.requests.map(r=>r.time),[0,1000,3000,7000,15000,30000]);
  await assert.rejects(s.request("/write",{method:"POST",body:{work:"never"}}),/nothing was sent/);
  assert.equal(f.requests.some(r=>r.url==="/write"),false);
  f.offline=false; await f.clock.advance(15000); assert.equal(s.state,"online");
  f.offline=true;
  await assert.rejects(s.request("/write",{method:"PUT",body:{work:"once"}}));
  f.offline=false; await f.clock.advance(10000);
  assert.equal(f.requests.filter(r=>r.url==="/write").length,1);
  s.stop();
});
test("bounded hung fetch aborts and retries without concurrent polls",async()=>{
  let aborted=0;
  const f=fixture({timeout:50,fetch:(url,opts)=>{
    if(url==="/health/instance") {opts.signal.addEventListener("abort",()=>aborted++);return new Promise(()=>{});}
  }});
  f.supervisor.start(); await f.clock.advance(0); await f.clock.advance(50);
  assert.equal(aborted,1); assert.equal(f.supervisor.state,"offline");
  assert.equal(f.requests.length,1);
  await f.clock.advance(1000); assert.equal(f.requests.length,2);
  f.supervisor.stop(); await flush(); assert.equal(aborted,2);
});
test("new boot replaces stale key exactly once without remounting edits",async()=>{
  const f=fixture(); const s=f.supervisor;
  const ui={tab:"settings",dirty:{port:"12345"},setupChecks:0};
  s.onOnline=()=>{f.online++;};
  s.start(); await f.clock.advance(0);
  f.identity=instance("boot-two"); f.key="fresh-key";
  await f.clock.advance(10000); await f.clock.advance(10000);
  assert.equal(s.key,"fresh-key"); assert.equal(f.credentials.length,1);
  assert.equal(f.requests.filter(r=>r.url==="/dash").length,1);
  const handshake=f.requests.filter(r=>r.url==="/admin/dash/lifecycle");
  assert.equal(handshake.at(-1).opts.headers.Authorization,"Bearer fresh-key");
  assert.deepEqual(ui,{tab:"settings",dirty:{port:"12345"},setupChecks:0});
  assert.equal(f.online,2); s.stop();
});
test("persistent 401 becomes login, not reload or retry loop",async()=>{
  const f=fixture(); const s=f.supervisor; f.authFail=true;
  s.start(); await f.clock.advance(0);
  assert.equal(s.state,"unauthorized"); const count=f.requests.length;
  await f.clock.advance(120000); assert.equal(f.requests.length,count);
  f.authFail=false; s.retry("manual-key"); await f.clock.advance(0);
  assert.equal(s.state,"online"); assert.equal(s.key,"manual-key"); s.stop();
});
test("wrong configuration is never authenticated or adopted",async()=>{
  const f=fixture(); f.identity={...f.identity,config_id:"foreign"};
  f.supervisor.start(); await f.clock.advance(0);
  assert.equal(f.supervisor.state,"foreign");
  assert.deepEqual(f.requests.map(r=>r.url),["/health/instance"]);
  await f.clock.advance(30000); assert.equal(f.requests.length,1); f.supervisor.stop();
});
test("one read in flight and tab cadence replacement cannot overlap",async()=>{
  let finish, calls=0;
  const f=fixture({fetch:url=>url==="/slow"?new Promise(resolve=>{calls++; finish=()=>resolve(response({ok:true}));}):undefined});
  const s=f.supervisor; s.start(); await f.clock.advance(0);
  const one=s.request("/slow"),two=s.request("/slow");
  assert.equal(one,two); await flush(); assert.equal(calls,1); finish(); await one;
  let release, runs=0;
  const run=()=>{runs++;return new Promise(resolve=>{release=resolve;});};
  s.poll("tab",run,100); await f.clock.advance(100);
  s.poll("tab",run,200); await f.clock.advance(1000); assert.equal(runs,1);
  release(); await flush(); await f.clock.advance(200); assert.equal(runs,2);
  release(); s.stop(); await flush();
});
test("intentional stop polls identity only and never calls start",async()=>{
  const f=fixture(); const s=f.supervisor;
  s.start(); await f.clock.advance(0); s.setStopped(); f.offline=true;
  await f.clock.advance(30000); assert.equal(s.state,"stopped");
  assert.equal(f.requests.some(r=>r.url.includes("/start")),false);
  await assert.rejects(s.request("/work",{method:"POST"}),/nothing was sent/);
  s.stop();
});
test("same config in the wrong ownership mode is foreign before auth or credential fetch",async()=>{
  for (const originalMode of ["user-service","foreground"]) {
    const f=fixture(); const s=f.supervisor;
    s.instance={...s.instance,mode:originalMode}; s.expectedMode=originalMode;
    f.identity={...f.identity,mode:originalMode};
    s.start(); await f.clock.advance(0);
    const before=f.requests.length;
    f.identity={...instance("new-owner"),mode:originalMode === "user-service" ? "foreground" : "user-service"};
    await f.clock.advance(10000);
    assert.equal(s.state,"foreign"); assert.equal(s.connected,false);
    assert.deepEqual(f.requests.slice(before).map(r=>r.url),["/health/instance"]);
    assert.equal(f.credentials.length,0); assert.equal(s.expectedMode,originalMode);
    await assert.rejects(s.request("/mutation",{method:"POST"}),/nothing was sent/);
    s.retry(); await f.clock.advance(0);
    assert.equal(s.state,"foreign"); assert.equal(f.requests.some(r=>r.url==="/dash"),false);
    s.stop();
  }
});
test("real autosave functions refuse offline edits and discard deferred writes across generations",async()=>{
  const vm=require("node:vm");
  const html=fs.readFileSync(path.join(__dirname,"../zcode_gateway/dashboard/index.html"),"utf8");
  for(const [name,timer,save,delay] of [
    ["genAutoSave","genSaveTimer","genSaveOptions",350],
    ["mailAutoSave","mailSaveTimer","mailSave",500]
  ]) {
    const start=html.indexOf("  function "+name+"(){");
    const end=html.indexOf("\n  }",start)+4;
    assert.ok(start>=0 && end>start);
    const clock=new Clock(); let writes=0;
    const connection={connected:false,generation:1};
    const context={connection, setTimeout:(...args)=>clock.setTimeout(...args),clearTimeout:id=>clock.clearTimeout(id),
      [save]:()=>{writes++;},[timer]:null};
    vm.createContext(context);
    vm.runInContext(html.slice(start,end),context);
    context[name](); // offline edit followed by fast recovery must never become a write
    assert.equal(clock.jobs.size,0);
    connection.connected=true;
    await clock.advance(delay);
    assert.equal(writes,0);
    context[name](); // valid online edit, then offline -> recovery before debounce ends
    connection.connected=false; connection.generation++;
    await clock.advance(100); connection.connected=true;
    await clock.advance(delay);
    assert.equal(writes,0);
    context[name](); // online instance change also invalidates its deferred save
    connection.generation++;
    await clock.advance(delay);
    assert.equal(writes,0);
    context[name](); context[name](); // ordinary connected debounce still saves exactly once
    await clock.advance(delay);
    assert.equal(writes,1);
    context[name](); connection.connected=false;
    context[name](); assert.equal(clock.jobs.size,0); // offline edit cancels older scheduled work
    connection.connected=true; await clock.advance(delay); assert.equal(writes,1);
  }
});
test("request deadlines preserve 5s default and consume finite per-request override",async()=>{
  for (const [override,deadline] of [[undefined,5000],[25000,25000]]) {
    const f=fixture({fetch:url=>url==="/slow"?new Promise(()=>{}):undefined});
    const s=f.supervisor;s.start();await f.clock.advance(0);
    const operation=s.request("/slow",override===undefined?{}:{timeoutMs:override});
    const rejected=assert.rejects(operation,/timed out/);
    await f.clock.advance(deadline-1);assert.equal(s.connected,true);
    await f.clock.advance(1);await rejected;
    const sent=f.requests.find(r=>r.url==="/slow");assert.equal(Object.hasOwn(sent.opts,"timeoutMs"),false);
    assert.equal(s.timeout,5000);s.stop();
  }
});
test("invalid timeout override sends nothing and leaves connection online",async()=>{
  const f=fixture();const s=f.supervisor;s.start();await f.clock.advance(0);
  for (const timeoutMs of [0,-1,25001,Infinity,NaN,"25000",null])
    await assert.rejects(s.request("/never",{timeoutMs}),/timeoutMs must/);
  assert.equal(f.requests.some(r=>r.url==="/never"),false);assert.equal(s.connected,true);s.stop();
});
test("extended deadlines preserve GET dedupe, generation cancellation and no POST replay",async()=>{
  const f=fixture({fetch:url=>url==="/slow"||url==="/write"?new Promise(()=>{}):undefined});
  const s=f.supervisor;s.start();await f.clock.advance(0);
  const one=s.request("/slow",{timeoutMs:25000}),two=s.request("/slow",{timeoutMs:10000});
  assert.equal(one,two);
  const write=s.request("/write",{method:"POST",timeoutMs:25000,body:{once:true}});
  const readsRejected=assert.rejects(one,/cancelled|discarded/),writeRejected=assert.rejects(write,/cancelled|discarded/);
  await f.clock.advance(6000);assert.equal(s.connected,true);s.offline(new Error("disconnect"));
  await Promise.all([readsRejected,writeRejected]);assert.equal(s.reads.size,0);
  await f.clock.advance(10000);
  assert.equal(f.requests.filter(r=>r.url==="/slow").length,1);
  assert.equal(f.requests.filter(r=>r.url==="/write").length,1);s.stop();
});
test("job conflict errors expose only bounded reconciliation metadata",async()=>{
  const f=fixture({fetch:url=>url==="/conflict"?response({detail:"busy",code:"deployment_active",active_id:"generation:uuid",token:"secret",remote:{secret:true}},409):undefined});
  const s=f.supervisor;s.start();await f.clock.advance(0);
  await assert.rejects(s.request("/conflict",{method:"POST"}),error=>{
    assert.equal(error.status,409);assert.equal(error.code,"deployment_active");assert.equal(error.active_id,"generation:uuid");
    assert.equal(error.detail,undefined);assert.equal(JSON.stringify(error).includes("secret"),false);return true;
  });assert.equal(s.connected,true);s.stop();
});
test("dashboard restores tab, boot is guarded, and offline redraw does not run setup",()=>{
  const html=fs.readFileSync(path.join(__dirname,"../zcode_gateway/dashboard/index.html"),"utf8");
  assert.ok(html.includes('trusted ? trusted.key : sessionStorage.getItem("gw-admin-key")'));
  assert.ok(html.includes('sessionStorage.getItem("gw-selected-tab")'));
  const boot=html.slice(html.indexOf("  function boot(){"),html.indexOf("  function start(){"));
  assert.ok(boot.includes("if (booted)")); assert.equal(boot.includes("setupFirstRunCheck()"),false);
  assert.equal(boot.includes('tab("accounts")'),false);
  assert.equal(html.includes("setInterval("),false);
  assert.ok(html.includes('if (!serverUp) return; // retain form values'));
  assert.ok(html.includes('connection.unpoll("setup")'));
  assert.ok(html.includes('connection.unpoll("login")'));
});
