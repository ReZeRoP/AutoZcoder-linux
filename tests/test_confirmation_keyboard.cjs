'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../zcode_gateway/dashboard/index.html'), 'utf8');
const start = html.indexOf('  function confirmDlg(');
const end = html.indexOf('\n  }', start) + 4;
assert.ok(start > 0 && end > start);

function fixture(danger = true) {
  const elements = new Map();
  const oldHandler = () => {};
  const document = {onkeydown: oldHandler, activeElement: null};
  const button = () => ({style: {}, disabled: false, isConnected: true,
    setAttribute() {}, focus() { document.activeElement = this; }});
  for (const id of ['confirm-modal', 'cf-title', 'cf-body', 'cf-ok', 'cf-cancel', 'btn-commands']) {
    elements.set('#' + id, button());
  }
  const opener = button(); opener.focus();
  const context = {document, $: id => elements.get(id)};
  vm.createContext(context);
  vm.runInContext(html.slice(start, end), context);
  const promise = context.confirmDlg('Remove?', 'Synthetic confirmation', 'Remove', danger);
  const key = (value, extra = {}) => {
    const event = {key: value, preventDefault() {}, stopImmediatePropagation() {}, ...extra};
    document.onkeydown(event);
  };
  return {elements, document, oldHandler, opener, promise, key};
}

test('danger confirmation initially focuses Cancel and Enter cancels', async () => {
  const f = fixture();
  assert.equal(f.document.activeElement, f.elements.get('#cf-cancel'));
  f.key('Enter');
  assert.equal(await f.promise, false);
  assert.equal(f.document.activeElement, f.opener);
  assert.equal(f.document.onkeydown, f.oldHandler);
});

test('Tab to the affirmative button permits explicit Enter confirmation', async () => {
  const f = fixture();
  f.key('Tab');
  assert.equal(f.document.activeElement, f.elements.get('#cf-ok'));
  f.key('Enter');
  assert.equal(await f.promise, true);
});

test('repeated Enter, composition and stray focus cannot accept', async () => {
  const f = fixture(false);
  let resolved = false;
  f.promise.then(() => { resolved = true; });
  f.key('Enter', {repeat: true});
  f.key('Enter', {isComposing: true});
  f.key('Enter', {keyCode: 229});
  f.document.activeElement = f.opener;
  f.key('Enter');
  await Promise.resolve();
  assert.equal(resolved, false);
  f.key('Escape');
  assert.equal(await f.promise, false);
});

test('cancel click and backdrop resolve once with a safe focus fallback', async () => {
  const f = fixture();
  f.opener.isConnected = false;
  const cancel = f.elements.get('#cf-cancel').onclick;
  const confirm = f.elements.get('#cf-ok').onclick;
  cancel(); confirm();
  assert.equal(await f.promise, false);
  assert.equal(f.document.activeElement, f.elements.get('#btn-commands'));
  const backdrop = fixture();
  const modal = backdrop.elements.get('#confirm-modal');
  modal.onclick({target: modal});
  assert.equal(await backdrop.promise, false);
});
