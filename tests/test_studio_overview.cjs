'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Overview = require('../zcode_gateway/dashboard/overview.js');

// Synthetic DOM, not jsdom: no dependencies, browser, server, or live API.
class Element {
  constructor(tag, doc) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = doc;
    this.children = []; this.attributes = {}; this.listeners = {};
    this.className = ''; this.dataset = {}; this._text = ''; this.parentNode = null;
    this.classList = {contains: name => this.className.split(/\s+/).includes(name)};
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(' '); }
  set innerHTML(_) { throw new Error('HTML interpolation is forbidden'); }
  append(...nodes) { for (const node of nodes) { this.children.push(node); node.parentNode = this; } }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  async fire(type, event = {}) { for (const listener of this.listeners[type] || []) await listener(event); }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  querySelectorAll(selector) {
    const result = [];
    const matches = node => selector.startsWith('.') ? node.classList.contains(selector.slice(1))
      : node.tagName.toLowerCase() === selector;
    const walk = node => { for (const child of node.children) { if (matches(child)) result.push(child); walk(child); } };
    walk(this); return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() { this.ownerDocument.activeElement = this; }
  getBoundingClientRect() { return {left: 0, width: 720}; }
}
function view() {
  const clipboard = [];
  const doc = {createElement: tag => new Element(tag, doc),
    createElementNS: (_, tag) => new Element(tag, doc), activeElement: null,
    defaultView: {navigator: {clipboard: {writeText: async value => clipboard.push(value)}}}};
  return {view: new Element('main', doc), doc, clipboard};
}
function fixture() {
  return {
    pool: {accounts: 3, ready: 1, by_state: {ready: 1, dead: 1, cooling: 1},
      detail: [{id: 'a', token: 'do-not-render-account-token'}]},
    models: {generated_at: 100, summary: {total_models: 2, unavailable: 1},
      models: [{model: 'working-model', available_on: 1}, {model: 'unavailable-model', available_on: 0}]},
    analytics: {hours: 24, summary: {requests: 10, ok: 8},
      series: [{hour: '10-27 09:00', requests: 3}, {hour: '10-27 10:00', requests: 7}],
      latency: {p50: 2300, p90: 5000, p99: 7000, samples: 8},
      by_key: [{client_key: 'never-show-analytics-key'}]},
    notifications: {events: [1, 2, 3, 4, 5].map(id => ({id, ts: 100 + id,
      category: 'quota', level: 'warning', message: 'Event ' + id, count: id}))},
    server: {host: '0.0.0.0', port: 8788, version: '1.3.0', admin_key: 'private-admin-value',
      client_keys: [{key: 'private-client-value', name: 'Client'}], config_path: '/never-show/config'}
  };
}
function apiFor(data, calls, failures = []) {
  return (route, options) => {
    calls.push([route, options]);
    const entry = Overview.REQUESTS.find(([, path]) => path === route);
    assert.ok(entry, 'No optional manager or unknown route requested');
    if (failures.includes(entry[0])) throw new Error('private-admin-value raw failure');
    return data[entry[0]];
  };
}
function deepFreeze(value) {
  if (value && typeof value === 'object') { Object.freeze(value); Object.values(value).forEach(deepFreeze); }
  return value;
}

test('actual response shapes, rolling success and true successful-request median', () => {
  const summary = Overview.buildSummary(fixture());
  assert.deepEqual(summary.pool, {accounts: 3, ready: 1,
    byState: {dead: 1, exhausted: null, cooling: 1}});
  assert.equal(summary.analytics.requests, 10);
  assert.equal(summary.analytics.successRate, 80);
  assert.deepEqual(summary.analytics.latency, {p50: 2300, samples: 8});
  assert.deepEqual(summary.models.names, ['unavailable-model']);
  assert.equal(summary.server.endpoint, 'http://127.0.0.1:8788/v1');
  assert.deepEqual(summary.notifications.map(event => event.message), ['Event 5', 'Event 4', 'Event 3', 'Event 2']);
});

test('rejected and malformed sources never masquerade as zero metrics', () => {
  const fail = {status: 'rejected', reason: new Error('sensitive')};
  const result = Overview.buildSummary({pool: fail, models: fail, analytics: fail, notifications: fail, server: fail});
  for (const value of Object.values(result)) assert.equal(value, null);
  const malformed = Overview.buildSummary({pool: {}, analytics: {summary: {requests: null, ok: null},
    series: [{hour: '10:00', requests: null}], latency: {p50: 0, samples: 0}}});
  assert.equal(malformed.pool, null);
  assert.equal(malformed.analytics.requests, null);
  assert.equal(malformed.analytics.successRate, null);
  assert.equal(malformed.analytics.series, null);
  assert.equal(malformed.analytics.latency, null);
});

test('empty ledger is distinct from failure; no fake 100% or 0s median', () => {
  const data = fixture(); data.analytics.summary = {requests: 0, ok: 0};
  data.analytics.series = [{hour: '10:00', requests: 0}];
  data.analytics.latency = {p50: 0, samples: 0};
  const summary = Overview.buildSummary(data);
  assert.equal(summary.analytics.empty, true);
  assert.equal(summary.analytics.requests, 0);
  assert.equal(summary.analytics.successRate, null);
  assert.equal(summary.analytics.latency, null);
});

test('single line geometry uses integer ticks and gaps, never injected labels', () => {
  const chart = Overview.chartGeometry([{hour: 'first', requests: 2}, {hour: 'gap', requests: null},
    {hour: 'last', requests: 8}]);
  assert.equal(chart.points[1].y, null);
  assert.equal((chart.path.match(/M/g) || []).length, 2);
  assert.ok(!chart.path.includes('NaN'));
  assert.ok(chart.points[0].x < chart.points[2].x);
  assert.ok(chart.points[0].y > chart.points[2].y);
  assert.ok(chart.ticks.every(tick => Number.isInteger(tick.value)));
  assert.ok(chart.yMax >= 8);
  const singleton = Overview.chartGeometry([{hour: 'one', requests: 1}]);
  assert.ok(Number.isFinite(singleton.points[0].x));
  assert.equal(Overview.chartGeometry([]).path, '');
});

test('keys never enter projection or rendered DOM, even inside notifications; text is not HTML', async () => {
  const data = fixture();
  data.notifications.events.push({ts: 999, message: '<img src=x onerror=evil()> private-admin-value private-client-value', level: 'error'});
  const original = JSON.stringify(data); deepFreeze(data);
  const summary = Overview.buildSummary(data), serialized = JSON.stringify(summary);
  for (const secret of ['private-admin-value', 'private-client-value', 'do-not-render-account-token', 'never-show-analytics-key', '/never-show/config']) {
    assert.ok(!serialized.includes(secret));
  }
  const {view: target} = view();
  await Overview.render(target, {api: apiFor(data, []), escape: () => { throw new Error('No unsafe template needed'); }});
  assert.ok(target.textContent.includes('<img src=x onerror=evil()> [redacted] [redacted]'));
  assert.equal(target.querySelectorAll('img').length, 0);
  assert.ok(!target.textContent.includes('private-admin-value'));
  assert.equal(JSON.stringify(data), original, 'Sources are not mutated');
});

test('exactly five GET requests, partial failures retain good sources and no writes', async () => {
  const calls = [], {view: target} = view();
  await Overview.render(target, {api: apiFor(fixture(), calls, ['analytics', 'notifications'])});
  assert.deepEqual(calls.map(([route]) => route), Overview.REQUESTS.map(([, route]) => route));
  assert.ok(calls.every(([, options]) => options.method === 'GET' && !('body' in options)));
  assert.ok(target.textContent.includes('Gateway responding'));
  assert.ok(target.textContent.includes('Request history unavailable'));
  assert.ok(target.textContent.includes('Recent activity unavailable'));
  assert.ok(target.textContent.includes('Unavailable'));
  assert.ok(!target.textContent.includes('raw failure'));
  assert.ok(!target.textContent.includes('No requests yet'));
});

test('empty, disconnected and unavailable presentations remain distinct', async () => {
  const data = fixture(); data.analytics.summary = {requests: 0, ok: 0}; data.analytics.series = [];
  const {view: target} = view();
  await Overview.render(target, {api: apiFor(data, []), connected: () => false});
  assert.ok(target.textContent.includes('Disconnected'));
  assert.ok(target.textContent.includes('snapshot may be stale'));
  assert.ok(target.textContent.includes('No requests yet in the last 24 hours'));
  assert.ok(!target.textContent.includes('0.0%'));
});

test('attention navigation and clipboard require explicit clicks', async () => {
  const {view: target, clipboard} = view(), destinations = [];
  await Overview.render(target, {api: apiFor(fixture(), []), go: (...args) => destinations.push(args)});
  assert.equal(destinations.length, 0); assert.equal(clipboard.length, 0);
  const links = target.querySelectorAll('.overview-link');
  for (const link of links) await link.fire('click');
  assert.ok(destinations.some(([tab, query]) => tab === 'accounts' && query === 'dead'));
  assert.ok(destinations.some(([tab, query]) => tab === 'models' && query === 'unavailable-model'));
  await target.querySelector('.overview-copy').fire('click');
  assert.deepEqual(clipboard, ['http://127.0.0.1:8788/v1']);
  assert.equal(target.querySelector('.overview-copy').textContent, 'Copied');
});

test('hour crosshair, accessible native slider, table and poll focus preserved', async () => {
  const {view: target, doc} = view();
  await Overview.render(target, {api: apiFor(fixture(), [])});
  let slider = target.querySelector('.overview-chart-slider');
  assert.equal(slider.type, 'range'); assert.equal(slider.attributes['aria-label'], 'Inspect request hour');
  slider.value = '0'; await slider.fire('input'); slider.focus();
  assert.ok(target.querySelector('.overview-chart-tooltip').textContent.includes('3 requests'));
  target.querySelector('.overview-chart-table').open = true;
  await Overview.render(target, {api: apiFor(fixture(), [])});
  slider = target.querySelector('.overview-chart-slider');
  assert.equal(slider.value, '0'); assert.equal(doc.activeElement, slider);
  assert.equal(target.querySelector('.overview-chart-table').open, true);
  assert.ok(target.querySelector('caption'));
  const line = target.querySelectorAll('path').find(node => node.attributes.stroke === 'var(--chart-line, #A3A3A3)');
  assert.equal(line.attributes['stroke-width'], '2');
});

test('late rendering cannot replace another tab', async () => {
  const {view: target, doc} = view(); let release;
  const pending = new Promise(resolve => { release = resolve; });
  const rendering = Overview.render(target, {api: () => pending});
  const other = doc.createElement('p'); other.textContent = 'Settings'; target.replaceChildren(other);
  release({}); await rendering;
  assert.equal(target.textContent, 'Settings');
});

test('server endpoint allowlist rejects supplied URLs and userinfo', () => {
  for (const host of ['https://evil.example', 'user:password@host', 'localhost/path', 'localhost?key=secret']) {
    assert.equal(Overview.buildSummary({server: {host, port: 8788}}).server, null);
  }
  assert.equal(Overview.buildSummary({server: {host: '::1', port: 8788}}).server.endpoint, 'http://[::1]:8788/v1');
});

test('source contains no inline JS injection, timer, manager initialization or mutation HTTP verbs', () => {
  const source = fs.readFileSync(path.join(__dirname, '../zcode_gateway/dashboard/overview.js'), 'utf8');
  assert.ok(!/\.innerHTML\s*=|onclick\s*=|setInterval\(|setTimeout\(|method:\s*['"](?:POST|PUT|DELETE|PATCH)/.test(source));
});
