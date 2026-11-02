'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const Preferences = require('../zcode_gateway/dashboard/preferences.js');
const html = fs.readFileSync(require('node:path').join(__dirname, '../zcode_gateway/dashboard/index.html'), 'utf8');
function getFunction(name) {
  const start = html.indexOf('  function ' + name + '(');
  assert.ok(start > 0);
  const end = html.indexOf('\n  }', start) + 4;
  return html.slice(start, end);
}
function filters(kind, rows) {
  const controls = new Map();
  const domRows = rows.map(row => ({dataset:kind === 'account' ? {accountId:row.id} : {modelId:row.model}}));
  const preferences = Preferences.createStore();
  // Browser objects share one realm; normalize VM patches to that realm for
  // the CommonJS module's deliberate plain-object validation.
  const localPreferences={get:()=>preferences.get(),update:patch=>preferences.update(JSON.parse(JSON.stringify(patch)))};
  const context = {cache:kind === 'account' ? {accounts:rows} : {models:{models:rows}}, preferences:localPreferences,
    StudioPreferences:Preferences, document:{querySelectorAll:selector => selector.includes('tr[') ? domRows : []},
    $:id => { if (!controls.has(id)) controls.set(id,{setAttribute(k,v){this[k]=v;}}); return controls.get(id); }};
  vm.createContext(context);
  const names = kind === 'account' ? ['filterAccounts','applyAccountFilters'] : ['filterModels','applyModelFilters'];
  for (const name of names) vm.runInContext(getFunction(name),context);
  return {context,domRows,controls,preferences};
}
test('actual account filter composes status and query from cached snapshot', () => {
  const f=filters('account',[{id:'a',email:'Alice',state:'ready'},{id:'b',email:'Bob',state:'cooling'}]);
  f.preferences.update({accountFilter:'ready'});
  f.context.filterAccounts({value:'alice'});
  assert.deepEqual(f.domRows.map(r=>r.hidden),[false,true]);
  assert.equal(f.controls.get('#account-filter-summary').textContent,'1 / 2 accounts shown');
  f.context.filterAccounts({value:'bob'});
  assert.deepEqual(f.domRows.map(r=>r.hidden),[true,true]);
  assert.equal(f.controls.get('#accounts-no-match').hidden,false);
});
test('actual model filter does not read obsolete b.mono selector or rendered text', () => {
  const f=filters('model',[{model:'alpha',overall:'available'},{model:'beta',overall:'no_credit'}]);
  f.context.filterModels({value:'beta'});
  assert.deepEqual(f.domRows.map(r=>r.hidden),[true,false]);
  f.preferences.update({modelFilter:'available'});f.context.applyModelFilters();
  assert.deepEqual(f.domRows.map(r=>r.hidden),[true,true]);
  assert.equal(f.controls.get('#model-filter-summary').textContent,'0 / 2 models shown');
});
test('actual sort, favorite and copy functions operate locally on loaded IDs only', () => {
  const f=filters('model',[{model:'alpha',overall:'available',next_release_in_s:10},{model:'beta',overall:'no_credit',next_release_in_s:2}]);
  const renders=[],copies=[];
  f.context.curTab='models';f.context.palette=null;
  f.context.renderModelsData=(_,data)=>renders.push(Preferences.sortModels([...data.models],f.preferences.get().modelSort,f.preferences.get().favorites));
  f.context.copy=value=>copies.push(value);
  f.preferences.setCatalog(['alpha','beta']);
  for(const name of ['sortModels','toggleFavorite','copyModel'])vm.runInContext(getFunction(name),f.context);
  f.context.sortModels('next');assert.deepEqual(renders.at(-1).map(m=>m.model),['beta','alpha']);
  f.context.toggleFavorite('alpha');assert.deepEqual(renders.at(-1).map(m=>m.model),['alpha','beta']);
  f.context.copyModel('alpha');f.context.copyModel('not-loaded');assert.deepEqual(copies,['alpha']);
  f.context.cache.models.models=[];f.context.toggleFavorite('alpha');assert.deepEqual(f.preferences.get().favorites,[]);
});
test('actual drawer projection excludes raw secrets, notes and arbitrary error payloads', () => {
  const context={};vm.createContext(context);vm.runInContext(getFunction('projectAccount'),context);
  const result=context.projectAccount({id:'x',email:'Alice',state:'ready',expires_at:100,
    token_fingerprint:'never-show',notes:{secret:'never-show'},last_error:'never-show',
    model_caps:{alpha:{release_in_s:10,message:'never-show'}},
    error_history:[{code:'never-show',message:'never-show',ago_s:1}],
    plans:[{name:'Coding',status:'active',ends_at:Infinity}],quotas:{alpha:{remaining:2,total:10,resets_at:Infinity}}});
  assert.ok(!JSON.stringify(result).includes('never-show'));
  assert.equal(result.plans[0].ends_at,null);assert.equal(result.quotas.alpha.resets_at,null);
  assert.equal(result.errors[0].label,'Request failed (unrecognized code)');
});
test('actual palette registry has explicit local actions and no dynamic App enumeration', () => {
  const runs=[],copies=[];
  const context={PAGE_INFO:{overview:['Overview','Summary'],generator:['Generator','Tools']},
    cache:{models:{models:[{model:'alpha',overall:'available'}]},endpoint:'http://127.0.0.1:8788/v1'},
    document:{querySelector:()=>null},tab:(...args)=>runs.push(args),copy:value=>copies.push(value),
    copyModel:value=>copies.push(value),focusCurrentSearch(){}};
  vm.createContext(context);vm.runInContext(getFunction('paletteCommands'),context);
  const commands=context.paletteCommands();assert.equal(runs.length,0);assert.equal(copies.length,0);
  commands.find(c=>c.id==='nav-generator').run();assert.equal(runs[0][2].localOnly,true);
  commands.find(c=>c.id==='copy-model-alpha').run();assert.deepEqual(copies,['alpha']);
  assert.ok(!commands.some(c=>/remove|shutdown|refresh-token|claim|restart/.test(c.id)));
  commands.find(c=>c.id==='nav-overview').run();assert.equal(runs.at(-1)[2].localOnly,true);
  for (const command of commands.filter(c=>c.id.startsWith('nav-'))) {
    command.run(); assert.equal(runs.at(-1)[2].localOnly,true);
  }
});
test('actual drawer retains selected ID while offline or removed and disables actions', () => {
  const controls=new Map(),buttons=[{},{}];
  const context={selectedAccountId:'a',selectedAccountScope:'scope',preferenceScope:'scope',serverUp:true,
    cache:{accounts:[{id:'a',email:'Alice',state:'ready'},{id:'b',email:'Bob',state:'ready'}]},
    window:{getSelection:()=>null}, verifiedScope:()=> 'scope', localZone:'UTC',esc:s=>String(s),badge:s=>String(s),planCell:()=>'',quotaCell:()=>'',capCell:()=>'',fmtLeft:()=>'',fmtSecs:()=>'',
    $:id=>{if(!controls.has(id))controls.set(id,{scrollTop:90,querySelectorAll:()=>buttons});return controls.get(id);}};
  vm.createContext(context);
  for(const name of ['projectAccount','drawerAccountAvailable','updateAccountDrawer'])vm.runInContext(getFunction(name),context);
  context.updateAccountDrawer();assert.equal(context.drawerAccountAvailable(),true);
  context.serverUp=false;context.updateAccountDrawer();
  assert.match(controls.get('#account-drawer-body').innerHTML,/Stale snapshot/);assert.ok(buttons.every(b=>b.disabled));
  context.serverUp=true;context.cache.accounts=[{id:'b',email:'Bob',state:'ready'}];context.updateAccountDrawer();
  assert.match(controls.get('#account-drawer-body').innerHTML,/removed from the pool/);
  assert.ok(!controls.get('#account-drawer-body').innerHTML.includes('Bob'));
  assert.equal(context.selectedAccountId,'a');assert.equal(context.drawerAccountAvailable(),false);
  context.cache.accounts=[{id:'a',email:'Alice',state:'ready'}];context.verifiedScope=()=> 'foreign';
  assert.equal(context.drawerAccountAvailable(),false);
});
test('account panel retains selected text but marks stale data and disables actions', () => {
  const buttons = [{}]; let writes = 0;
  const anchor = {};
  const note = {textContent:''};
  const body = {scrollTop:17, contains:node=>node === anchor,
    querySelector:()=>note, set innerHTML(value) { this.markup=value; writes++; },
    get innerHTML() { return this.markup; }};
  const context = {selectedAccountId:'a', selectedAccountScope:'scope', preferenceScope:'scope', serverUp:true,
    cache:{accounts:[{id:'a',email:'Alice',state:'ready'}]}, verifiedScope:()=> 'scope',
    window:{getSelection:()=>({isCollapsed:false,anchorNode:anchor})},
    localZone:'UTC',esc:s=>String(s),badge:s=>String(s),planCell:()=>'',quotaCell:()=>'',capCell:()=>'',fmtLeft:()=>'',fmtSecs:()=>'',
    $:id=>id === '#account-drawer-body' ? body : {querySelectorAll:()=>buttons}};
  vm.createContext(context);
  for (const name of ['projectAccount','drawerAccountAvailable','updateAccountDrawer']) vm.runInContext(getFunction(name),context);
  context.updateAccountDrawer(); assert.equal(writes,1);
  context.serverUp=false; context.updateAccountDrawer();
  assert.equal(writes,1); assert.match(note.textContent,/Stale snapshot/); assert.equal(buttons[0].disabled,true);
  context.serverUp=true; context.cache.accounts=[{id:'b',email:'Bob',state:'ready'}]; context.selectedAccountId='b';
  context.updateAccountDrawer(); assert.equal(writes,2); assert.match(body.innerHTML,/Bob/);
});

test('polling restores model-sort and model-details keyboard focus by stable keys', () => {
  for (const key of ['model-sort-ratio','model-details-alpha']) {
    const active = {dataset:{focusKey:key}}; let restored=false;
    const replacement = {dataset:{focusKey:key},focus:options=>{restored=options.preventScroll;}};
    let current=[active];
    const view = {scrollTop:38,contains:element=>element === active,
      querySelectorAll:selector=>selector === '.tbl-wrap' ? [] : current};
    const context={document:{activeElement:active},window:{scrollX:0,scrollY:22,scrollTo(){}}};
    vm.createContext(context); vm.runInContext(getFunction('preserveView'),context);
    const restore=context.preserveView(view); current=[replacement]; view.scrollTop=0; restore();
    assert.equal(restored,true); assert.equal(view.scrollTop,38);
  }
  assert.ok(html.includes('data-focus-key="model-sort-${key}"'));
  assert.ok(html.includes('data-focus-key="model-details-${esc(m.model)}"'));
});

test('local palette navigation cancels pending fetch intent from an earlier tab', () => {
  const source=getFunction('tab');
  const branch=source.slice(source.indexOf('if (options.localOnly || !serverUp)'));
  assert.ok(branch.indexOf('pendingTab = false') < branch.indexOf('return;'));
  assert.ok(branch.indexOf('connection.unpoll("tab")') < branch.indexOf('return;'));
});

test('actual global key handler honors notification Escape and yields to higher dialogs and IME', () => {
  const start=html.indexOf('   document.addEventListener("keydown", e => {');
  const end=html.indexOf('\n   });',start)+7;
  let handler,closed=0,opened=0,blocked=false;
  const context={document:{addEventListener:(_,fn)=>handler=fn,activeElement:null,body:{classList:{remove(){}}}},
    notifyOpen:true,higherModalOpen:include=>include ? true : blocked,toggleNotify:()=>closed++,
    drawer:null,palette:null,openCommands:()=>opened++,$:()=>({setAttribute(){}}),closeDropdowns(){}};
  vm.createContext(context);vm.runInContext(html.slice(start,end),context);
  const event=(key,extra={})=>({key,preventDefault(){},target:{matches:()=>false},...extra});
  handler(event('Escape'));assert.equal(closed,1);
  blocked=true;handler(event('Escape'));assert.equal(closed,1);
  handler(event('k',{ctrlKey:true}));assert.equal(opened,0);
  handler(event('Escape',{isComposing:true}));assert.equal(closed,1);
});
