'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const Usability = require('../zcode_gateway/dashboard/usability.js');

// Synthetic, detached accounts/commands only. No browser/server/API/dependencies.
class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, fn, capture = false) {
    const list = this.listeners.get(type) || [];
    if (!list.some(entry => entry.fn === fn && entry.capture === !!capture)) list.push({fn, capture: !!capture});
    this.listeners.set(type, list);
  }
  removeEventListener(type, fn, capture = false) {
    this.listeners.set(type, (this.listeners.get(type) || []).filter(entry => entry.fn !== fn || entry.capture !== !!capture));
  }
  count(type) { return (this.listeners.get(type) || []).length; }
  invoke(type, event, capture) {
    for (const entry of [...(this.listeners.get(type) || [])]) {
      if (entry.capture === capture && !event.immediateStopped) entry.fn(event);
    }
  }
}
function event(type, values = {}) {
  return {type, defaultPrevented: false, propagationStopped: false, immediateStopped: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.propagationStopped = true; },
    stopImmediatePropagation() { this.immediateStopped = true; this.propagationStopped = true; }, ...values};
}
class Element extends Events {
  constructor(tag, doc) {
    super(); this.tagName = tag.toUpperCase(); this.ownerDocument = doc;
    this.children = []; this.attributes = new Map(); this.parentNode = null;
    this.className = ''; this._text = ''; this.value = ''; this.hidden = false; this.disabled = false;
    this.classList = {
      contains: name => this.className.split(/\s+/).includes(name),
      add: name => { if (!this.classList.contains(name)) this.className = (this.className + ' ' + name).trim(); },
      remove: name => { this.className = this.className.split(/\s+/).filter(item => item !== name).join(' '); },
      toggle: (name, force) => { const show = force === undefined ? !this.classList.contains(name) : force;
        this.classList[show ? 'add' : 'remove'](name); return show; }
    };
  }
  set id(value) { this.setAttribute('id', value); }
  get id() { return this.getAttribute('id') || ''; }
  get parentElement() { return this.parentNode; }
  get isConnected() { return this.ownerDocument.documentElement.contains(this); }
  set textContent(value) { this._text = String(value); this.replaceChildren(); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(' '); }
  set innerHTML(_) { throw new Error('HTML interpolation is forbidden'); }
  setAttribute(key, value) { this.attributes.set(key, String(value)); }
  getAttribute(key) { return this.attributes.has(key) ? this.attributes.get(key) : null; }
  hasAttribute(key) { return this.attributes.has(key); }
  removeAttribute(key) { this.attributes.delete(key); }
  append(...nodes) { for (const node of nodes) { node.remove(); this.children.push(node); node.parentNode = this; } }
  replaceChildren(...nodes) { for (const child of this.children) child.parentNode = null; this.children = []; this.append(...nodes); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null; }
  contains(node) { return this === node || this.children.some(child => child.contains(node)); }
  matches(selector) {
    if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
    const parsed = selector.match(/^([a-z]+)?(?:\[([^=\]]+)(?:="([^"]*)")?\])?$/);
    return !!parsed && (!parsed[1] || this.tagName.toLowerCase() === parsed[1]) &&
      (!parsed[2] || this.hasAttribute(parsed[2]) && (parsed[3] === undefined || this.getAttribute(parsed[2]) === parsed[3]));
  }
  querySelectorAll(selector) {
    const selectors = selector.split(',').map(value => value.trim()), result = [];
    const walk = root => { for (const child of root.children) {
      if (selectors.some(value => child.matches(value))) result.push(child); walk(child);
    }};
    walk(this); return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.ownerDocument.activeElement = this; this.ownerDocument.dispatch(event('focusin', {target: this})); }
  scrollIntoView(options) { this.lastScroll = options; }
  fire(type, values = {}) { return this.ownerDocument.dispatch(event(type, {target: this, ...values})); }
}
class Document extends Events {
  constructor() {
    super(); this.documentElement = new Element('html', this); this.body = new Element('body', this);
    this.documentElement.append(this.body); this.activeElement = this.body;
    this.defaultView = {getComputedStyle: element => ({display: element.hidden ? 'none' : 'block', visibility: 'visible'})};
  }
  createElement(tag) { return new Element(tag, this); }
  dispatch(e) {
    this.invoke(e.type, e, true);
    if (!e.propagationStopped) {
      const route = []; for (let node = e.target; node; node = node.parentNode) route.push(node);
      for (const node of route) { if (e.propagationStopped) break; node.invoke(e.type, e, false); }
    }
    if (!e.propagationStopped) {
      this.invoke(e.type, e, false);
      if (typeof this['on' + e.type] === 'function') this['on' + e.type](e);
    }
    return e;
  }
  key(key, values = {}) { return this.dispatch(event('keydown', {key, target: this.activeElement, ...values})); }
}
function fixture(options = {}) {
  const document = new Document();
  const opener = document.createElement('button'), fallback = document.createElement('button');
  document.body.append(opener, fallback); opener.focus();
  const runs = []; let items = [
    {id: 'models', label: 'Models', description: 'Browse loaded models', keywords: ['catalog', 'navigation'], run: () => runs.push('models')},
    {id: 'accounts', label: 'Accounts', description: 'View account status', run: () => runs.push('accounts')},
    {id: 'copy', label: 'Copy model ID', keywords: 'cached identifier', run: () => runs.push('copy')},
    {id: 'stale', label: 'Unavailable catalog', disabled: true, run: () => runs.push('stale')}
  ];
  let blocked = false, reads = 0;
  const palette = Usability.createPalette({document, commands: () => { reads++; return items; },
    fallbackFocus: () => fallback, isBlocked: () => blocked, ...options});
  return {document, opener, fallback, palette, runs,
    setCommands(value) { items = value; }, setBlocked(value) { blocked = value; }, get reads() { return reads; }};
}
function selected(palette) { return palette.list.children.find(row => row.getAttribute('aria-selected') === 'true'); }
function drawerFixture() {
  const document = new Document(), root = document.createElement('div'), panel = document.createElement('section');
  panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-label', 'Account details');
  const first = document.createElement('button'), last = document.createElement('button');
  const skipped = document.createElement('button'); skipped.disabled = true;
  const hidden = document.createElement('div'), hiddenButton = document.createElement('button');
  hidden.hidden = true; hidden.append(hiddenButton);
  const opener = document.createElement('button'), fallback = document.createElement('button');
  document.body.append(opener, fallback, root); root.append(panel); panel.append(first, skipped, hidden, last);
  opener.focus(); let blocked = false, closes = 0;
  const drawer = Usability.createOverlay({document, element: root, fallbackFocus: () => fallback,
    isBlocked: () => blocked, onClose: () => closes++});
  return {document, root, panel, first, last, opener, fallback, drawer,
    setBlocked(value) { blocked = value; }, get closes() { return closes; }};
}

test('CommonJS and standalone UMD export the agreed local-only APIs', () => {
  const sandbox = {globalThis: {}};
  vm.runInNewContext(fs.readFileSync(require.resolve('../zcode_gateway/dashboard/usability.js'), 'utf8'), sandbox);
  for (const name of ['createPalette', 'createOverlay', 'createDrawer', 'searchCommands']) {
    assert.equal(typeof Usability[name], 'function'); assert.equal(typeof sandbox.globalThis.StudioUsability[name], 'function');
  }
  assert.equal(Usability.createDrawer, Usability.createOverlay);
});

test('pure search uses only allowlisted descriptor fields, ranks labels and caps registry/query', () => {
  const commands = [
    {id: 'other', label: 'Other', description: 'models help', token: 'private-credential'},
    {id: 'prefix', label: 'Models favorites', keywords: ['local', 'pin']},
    {id: 'models', label: 'Models'}, {id: 'models', label: 'Duplicate'},
    {id: 'invalid', label: ''}, null, {id: 12, label: 'Numeric'}
  ];
  commands.forEach(value => { if (value) Object.freeze(value); }); Object.freeze(commands);
  assert.deepEqual(Usability.searchCommands('  MODELS  ', commands).map(row => row.id), ['models', 'prefix', 'other']);
  assert.deepEqual(Usability.searchCommands('pin local', commands).map(row => row.id), ['prefix']);
  assert.deepEqual(Usability.searchCommands('private-credential', commands), []);
  assert.deepEqual(Usability.searchCommands('missing', commands), []);
  assert.equal(Usability.searchCommands('', null).length, 0);
  assert.equal(Usability.searchCommands('', Array.from({length: 500}, (_, i) => ({id: String(i), label: 'Command ' + i}))).length, 200);
  assert.equal(Usability.searchCommands('x'.repeat(300), [{id: 'long', label: 'Long command', description: 'x'.repeat(300)}]).length, 1);
});

test('palette is body-mounted with dialog, combobox, listbox and active option semantics', () => {
  const {document, palette, opener, runs} = fixture();
  assert.equal(palette.element.parentNode, document.body); assert.equal(palette.element.hidden, true);
  assert.equal(document.count('keydown'), 0); assert.equal(palette.open(opener), true);
  assert.equal(palette.element.hidden, false); assert.equal(palette.element.classList.contains('is-open'), true);
  assert.equal(palette.dialog.getAttribute('role'), 'dialog'); assert.equal(palette.dialog.getAttribute('aria-modal'), 'true');
  assert.equal(palette.input.getAttribute('role'), 'combobox'); assert.equal(palette.input.getAttribute('aria-expanded'), 'true');
  assert.equal(palette.input.getAttribute('aria-controls'), palette.list.id);
  assert.equal(palette.list.getAttribute('role'), 'listbox'); assert.equal(document.activeElement, palette.input);
  assert.equal(selected(palette).id, palette.input.getAttribute('aria-activedescendant'));
  assert.equal(palette.list.children[3].getAttribute('aria-disabled'), 'true');
  assert.deepEqual(runs, []);
});

test('Arrow/Home/End highlight but never execute; Enter activates exactly once and consumes global keys', () => {
  const {document, palette, opener, runs} = fixture(); let underlying = 0;
  document.onkeydown = () => underlying++;
  palette.open(opener);
  for (const [key, index] of [['ArrowDown', 1], ['End', 3], ['Home', 0], ['ArrowUp', 3], ['ArrowDown', 0]]) {
    const e = document.key(key);
    assert.equal(selected(palette), palette.list.children[index]);
    assert.equal(e.defaultPrevented, true); assert.equal(e.immediateStopped, true);
    assert.deepEqual(runs, []);
  }
  assert.equal(underlying, 0); assert.equal(document.key('x').defaultPrevented, false); assert.equal(underlying, 1);
  assert.equal(document.key('Enter', {repeat: true}).defaultPrevented, true); assert.deepEqual(runs, []);
  assert.equal(document.key('Enter').defaultPrevented, true); assert.deepEqual(runs, ['models']);
  assert.equal(palette.isOpen(), false); assert.equal(document.activeElement, opener);
  assert.equal(palette.input.getAttribute('aria-expanded'), 'false'); assert.equal(document.count('keydown'), 0);
});

test('query input recreates a fresh registry without focus loss or execution; updates retain selected ID', () => {
  const f = fixture(), {document, palette, runs} = f; palette.open(); const readBefore = f.reads;
  palette.input.value = 'cached'; palette.input.fire('input');
  assert.equal(f.reads, readBefore + 1); assert.equal(palette.list.children.length, 1);
  assert.equal(selected(palette).textContent.includes('Copy model ID'), true);
  assert.equal(document.activeElement, palette.input); assert.deepEqual(runs, []);
  f.setCommands([{id: 'new', label: 'Cached other'}, {id: 'copy', label: 'Copy cached ID', run: () => runs.push('new-copy')}]);
  palette.update(); assert.equal(selected(palette), palette.list.children[1]);
  assert.equal(document.activeElement, palette.input); assert.deepEqual(runs, []);
  palette.setQuery('no matching result'); assert.equal(palette.list.children.length, 0);
  assert.equal(palette.status.textContent, 'No commands found'); assert.equal(palette.input.hasAttribute('aria-activedescendant'), false);
  document.key('End'); document.key('Enter'); assert.deepEqual(runs, []);
  palette.setQuery('x'.repeat(300)); assert.equal(palette.input.value.length, 256);
});

test('explicit clicks activate only known commands; disabled/missing commands never run', () => {
  const f = fixture(), {palette, document, runs} = f; palette.open();
  palette.list.children[3].fire('click'); assert.deepEqual(runs, []); assert.equal(palette.isOpen(), true);
  document.key('Enter'); assert.deepEqual(runs, []);
  palette.setQuery('Copy'); f.setCommands([{id: 'copy', label: 'Copy model ID', disabled: true, run: () => runs.push('bad')}]);
  document.key('Enter'); assert.deepEqual(runs, []); assert.equal(palette.isOpen(), true);
  f.setCommands([]); document.key('Enter'); assert.deepEqual(runs, []);
  f.setCommands([{id: 'accounts', label: 'Accounts', run: () => runs.push('accounts')}]); palette.setQuery('');
  const e = palette.list.children[0].fire('click'); assert.equal(e.defaultPrevented, true);
  assert.deepEqual(runs, ['accounts']); assert.equal(palette.isOpen(), false);
});

test('onActivate is the single dispatch hook, called after closing and restoring focus', () => {
  let f; const activated = [];
  f = fixture({onActivate: command => { activated.push(command.id); assert.equal(f.palette.isOpen(), false); assert.equal(f.document.activeElement, f.opener); }});
  f.palette.open(f.opener); f.document.key('Enter');
  assert.deepEqual(activated, ['models']); assert.deepEqual(f.runs, []);
});

test('IME Enter/Escape do not activate or close; completion resumes normal behavior', () => {
  const {palette, document, runs} = fixture(); palette.open();
  assert.equal(document.key('Enter', {isComposing: true}).defaultPrevented, false);
  assert.equal(document.key('Escape', {keyCode: 229}).defaultPrevented, false);
  palette.input.fire('compositionstart');
  assert.equal(document.key('Enter').defaultPrevented, false); assert.equal(document.key('Escape').defaultPrevented, false);
  assert.equal(palette.isOpen(), true); assert.deepEqual(runs, []);
  palette.input.fire('compositionend'); document.key('Enter'); assert.deepEqual(runs, ['models']);
});

test('palette traps Tab in both directions, ignores negative-tabindex options and restores fallback for removed opener', () => {
  const {palette, document, opener, fallback} = fixture(); palette.open(opener);
  assert.equal(document.key('Tab').defaultPrevented, true); assert.equal(document.activeElement, palette.closeButton);
  document.key('ArrowDown'); assert.equal(document.activeElement, palette.closeButton);
  document.key('Tab'); assert.equal(document.activeElement, palette.input);
  document.key('Tab', {shiftKey: true}); assert.equal(document.activeElement, palette.closeButton);
  opener.remove(); document.key('Escape'); assert.equal(document.activeElement, fallback);
});

test('confirmation/login owns keyboard, focus and backdrop before palette; blocked opens do not read registry', () => {
  const f = fixture(), {palette, document, runs} = f; f.setBlocked(true);
  assert.equal(palette.open(), false); assert.equal(f.reads, 0); assert.equal(document.count('keydown'), 0);
  f.setBlocked(false); palette.open(); f.setBlocked(true);
  const confirmation = document.createElement('button'); document.body.append(confirmation); confirmation.focus();
  let confirms = 0; document.onkeydown = e => { if (e.key === 'Escape') confirms++; };
  assert.equal(document.activeElement, confirmation);
  assert.equal(document.key('Escape').defaultPrevented, false); assert.equal(confirms, 1);
  assert.equal(document.key('Tab').defaultPrevented, false);
  assert.equal(document.key('Enter').defaultPrevented, false);
  palette.element.fire('click'); palette.list.children[0].fire('click');
  assert.equal(palette.isOpen(), true); assert.deepEqual(runs, []);
  // Explicit close during profile teardown must not steal confirmation focus.
  palette.close(); assert.equal(document.activeElement, confirmation);
  f.setBlocked(false);
});

test('backdrop closes without click-through; inside clicks do not close; Escape is consumed once', () => {
  const {palette, document, opener} = fixture(); let leakedClicks = 0, leakedKeys = 0;
  document.addEventListener('click', () => leakedClicks++); document.onkeydown = () => leakedKeys++;
  palette.open(opener); palette.dialog.fire('click'); assert.equal(palette.isOpen(), true);
  const e = palette.element.fire('click'); assert.equal(e.defaultPrevented, true);
  assert.equal(leakedClicks, 1); assert.equal(palette.isOpen(), false); assert.equal(document.activeElement, opener);
  palette.open(); document.key('Escape'); assert.equal(leakedKeys, 0); assert.equal(palette.isOpen(), false);
});

test('all data is text-only, extra credential fields are never rendered or searched', () => {
  const {palette, runs, setCommands} = fixture();
  setCommands([{id: 'safe', label: '<img src=x onerror=evil()>', description: '<script>evil()</script>',
    token: 'do-not-render', notes: 'private notes', run: () => runs.push('safe')}]);
  palette.open(); assert.ok(palette.list.textContent.includes('<img src=x onerror=evil()>'));
  assert.equal(palette.list.querySelectorAll('img').length, 0); assert.equal(palette.list.querySelectorAll('script').length, 0);
  assert.equal(palette.element.textContent.includes('do-not-render'), false);
  assert.equal(palette.element.textContent.includes('private notes'), false); assert.deepEqual(runs, []);
});

test('generic drawer retains mounted content/focus across refresh and skips hidden/disabled controls', () => {
  const {drawer, document, first, last, panel, opener} = drawerFixture();
  assert.equal(drawer.open(opener), true); assert.equal(document.activeElement, first);
  const content = document.createElement('p'); content.textContent = 'Synthetic account: ready'; panel.append(content);
  last.focus(); content.textContent = 'Synthetic account: stale / removed';
  assert.equal(document.activeElement, last); assert.equal(drawer.isOpen(), true);
  document.key('Tab'); assert.equal(document.activeElement, first);
  document.key('Tab', {shiftKey: true}); assert.equal(document.activeElement, last);
  opener.focus(); assert.equal(document.activeElement, first, 'Background focus redirects into drawer');
  drawer.close(); assert.equal(document.activeElement, opener); assert.ok(panel.textContent.includes('stale / removed'));
});

test('generic drawer handles empty dialog, removed origin, nested modal precedence and outside clicks', () => {
  const f = drawerFixture(), {drawer, document, root, panel, opener, fallback} = f;
  panel.replaceChildren(); drawer.open(); assert.equal(document.activeElement, panel);
  document.key('Tab'); assert.equal(document.activeElement, panel);
  f.setBlocked(true); fallback.focus(); document.key('Escape'); root.fire('click');
  assert.equal(drawer.isOpen(), true); assert.equal(document.activeElement, fallback);
  f.setBlocked(false); opener.remove(); root.fire('click'); assert.equal(drawer.isOpen(), false);
  assert.equal(document.activeElement, fallback); assert.equal(f.closes, 1);
});

test('listeners are open-scoped and idempotent; destroy prevents resurrection', () => {
  const {palette, document} = fixture();
  for (let i = 0; i < 5; i++) {
    palette.open(); palette.open(); assert.equal(document.count('keydown'), 1); assert.equal(document.count('focusin'), 1);
    palette.close(); palette.close(); assert.equal(document.count('keydown'), 0); assert.equal(document.count('focusin'), 0);
  }
  palette.open(); palette.destroy(); assert.equal(document.count('keydown'), 0); assert.equal(palette.element.isConnected, false);
  assert.equal(palette.open(), false);
  const {drawer} = drawerFixture(); drawer.destroy(); assert.equal(drawer.open(), false);
});
