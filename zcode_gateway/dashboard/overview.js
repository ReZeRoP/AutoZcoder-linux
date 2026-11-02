/* Read-only studio landing page. No timers: the shell owns refresh/reconnection. */
(function (root, factory) {
  'use strict';
  const overview = factory();
  if (typeof module === 'object' && module.exports) module.exports = overview;
  else root.StudioOverview = overview;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const REQUESTS = Object.freeze([
    ['pool', '/admin/pool/state'],
    ['models', '/admin/models/availability'],
    ['analytics', '/admin/dash/analytics?hours=24'],
    ['notifications', '/admin/dash/notifications'],
    ['server', '/admin/dash/server']
  ].map(Object.freeze));
  const owners = new WeakMap();
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const number = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
  const count = value => Number.isInteger(number(value)) ? value : null;
  const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  const text = value => typeof value === 'string' ? value : '';
  function response(value) {
    if (value && value.status === 'rejected') return null;
    return object(value && value.status === 'fulfilled' ? value.value : value);
  }
  // Only allow a listener host, never userinfo, a path, query, or supplied URL.
  function endpoint(host, port) {
    host = text(host).trim().replace(/^\[|\]$/g, '');
    if (!host || !Number.isInteger(port) || port < 1 || port > 65535) return null;
    if (!/^[a-zA-Z0-9._-]+$/.test(host) && !/^[a-fA-F0-9:]+$/.test(host)) return null;
    if (host === '0.0.0.0' || host === '::') host = '127.0.0.1';
    return 'http://' + (host.includes(':') ? '[' + host + ']' : host) + ':' + port + '/v1';
  }
  function secretsFrom(server) {
    const secrets = [];
    if (server) {
      if (text(server.admin_key)) secrets.push(server.admin_key);
      for (const entry of Array.isArray(server.client_keys) ? server.client_keys : []) {
        const value = typeof entry === 'string' ? entry : entry && entry.key;
        if (text(value)) secrets.push(value);
      }
    }
    return secrets.sort((a, b) => b.length - a.length);
  }
  function redact(value, secrets) {
    let result = text(value);
    for (const secret of secrets) result = result.split(secret).join('[redacted]');
    return result.replace(/\b(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[^\s,;]+)/gi, '[redacted]');
  }
  /** Pure allowlisted projection. Rejected/malformed sources become null, not zero.
   * Accepts raw responses or named Promise.allSettled results. Never returns keys,
   * notification data, account tokens, raw errors, or per-client analytics. */
  function buildSummary(data) {
    data = object(data) || {};
    const pool = response(data.pool), models = response(data.models);
    const analytics = response(data.analytics), notifications = response(data.notifications);
    const server = response(data.server), secrets = secretsFrom(server);
    const poolValid = pool && count(pool.accounts) !== null && count(pool.ready) !== null;
    const modelSummary = models && object(models.summary);
    const modelsValid = modelSummary && count(modelSummary.total_models) !== null && Array.isArray(models.models);
    const totals = analytics && object(analytics.summary);
    const requests = totals ? count(totals.requests) : null;
    const ok = totals ? count(totals.ok) : null;
    const sourceSeries = analytics && Array.isArray(analytics.series) ? analytics.series : null;
    const series = sourceSeries && sourceSeries.map(row => ({
      hour: redact(row && row.hour, secrets), requests: row ? count(row.requests) : null
    }));
    const seriesValid = series && series.every(row => row.hour && row.requests !== null);
    const lat = analytics && object(analytics.latency);
    const sampleCount = lat ? count(lat.samples) : null;
    const stateCounts = poolValid ? Object.fromEntries(['dead', 'exhausted', 'cooling']
      .map(state => [state, object(pool.by_state) ? count(pool.by_state[state]) : null])) : null;
    const safeEndpoint = server ? endpoint(server.host, server.port) : null;
    const safeEvents = notifications && Array.isArray(notifications.events) ? notifications.events
      .filter(event => event && text(event.message).trim())
      .map(event => ({
        message: redact(event.message, secrets), category: redact(event.category, secrets),
        level: ['info', 'success', 'warning', 'error'].includes(event.level) ? event.level : 'info',
        ts: number(event.ts), count: count(event.count)
      }))
      .sort((a, b) => (b.ts ?? -1) - (a.ts ?? -1)).slice(0, 4) : null;
    const unavailableModels = modelsValid ? models.models
      .filter(model => model && count(model.available_on) === 0)
      .map(model => redact(model.model, secrets)).filter(Boolean) : null;
    return {
      pool: poolValid ? {accounts: pool.accounts, ready: pool.ready, byState: stateCounts} : null,
      models: modelsValid ? {total: modelSummary.total_models,
        unavailable: count(modelSummary.unavailable), names: unavailableModels} : null,
      analytics: analytics ? {requests, ok,
        successRate: requests > 0 && ok !== null && ok <= requests ? ok / requests * 100 : null,
        series: seriesValid ? series : null,
        latency: sampleCount > 0 && number(lat.p50) !== null ? {p50: lat.p50, samples: sampleCount} : null,
        empty: requests === 0} : null,
      notifications: safeEvents,
      server: server && safeEndpoint ? {endpoint: redact(safeEndpoint, secrets),
        host: redact(text(server.host), secrets), port: server.port,
        version: redact(text(server.version), secrets)} : null
    };
  }
  /** Honest integer request axis and one ordered hourly line; missing values
   * break segments rather than being converted to fabricated zero requests. */
  function chartGeometry(series, width = 720, height = 230) {
    const rows = Array.isArray(series) ? series : [];
    const bounds = {left: 48, right: Math.max(49, width - 18), top: 18, bottom: Math.max(19, height - 38)};
    const values = rows.map(row => row ? count(row.requests) : null).filter(value => value !== null);
    const maximum = values.length ? Math.max(...values) : 0;
    const rough = Math.max(1, maximum / 4);
    const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
    const step = Math.max(1, [1, 2, 5, 10].find(value => value * magnitude >= rough) * magnitude);
    const yMax = Math.max(step, Math.ceil(maximum / step) * step);
    const points = rows.map((row, index) => ({
      index, hour: text(row && row.hour), requests: row ? count(row.requests) : null,
      x: rows.length > 1 ? bounds.left + index / (rows.length - 1) * (bounds.right - bounds.left)
        : (bounds.left + bounds.right) / 2,
      y: row && count(row.requests) !== null ? bounds.bottom - row.requests / yMax * (bounds.bottom - bounds.top) : null
    }));
    let pen = false;
    const path = points.map(point => {
      if (point.y === null) { pen = false; return ''; }
      const command = pen ? 'L' : 'M'; pen = true;
      return command + point.x.toFixed(2) + ',' + point.y.toFixed(2);
    }).filter(Boolean).join(' ');
    const ticks = [];
    for (let value = 0; value <= yMax; value += step) ticks.push({value,
      y: bounds.bottom - value / yMax * (bounds.bottom - bounds.top)});
    return {width, height, bounds, points, path, ticks, yMax};
  }
  function el(doc, tag, className, content) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = String(content);
    return node;
  }
  function svg(doc, tag, attributes, content) {
    const node = doc.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attributes || {})) node.setAttribute(key, String(value));
    if (content !== undefined) node.textContent = String(content);
    return node;
  }
  function icon(doc, kind) {
    const paths = {
      info: 'M12 8h.01M11 12h1v5M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18',
      success: 'm7 12 3 3 7-7M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18',
      warning: 'M12 8v5m0 3h.01M3 20h18L12 3 3 20Z',
      error: 'm8 8 8 8m0-8-8 8M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18'
    };
    const node = svg(doc, 'svg', {viewBox: '0 0 24 24', width: 18, height: 18,
      fill: 'none', stroke: 'currentColor', 'stroke-width': 1.6, 'aria-hidden': 'true', focusable: 'false'});
    node.append(svg(doc, 'path', {d: paths[kind] || paths.info}));
    return node;
  }
  function button(doc, label, handler, className) {
    const node = el(doc, 'button', className || 'btn btn-quiet btn-sm overview-link', label);
    node.type = 'button';
    node.addEventListener('click', handler);
    return node;
  }
  function section(doc, title, className) {
    const node = el(doc, 'section', 'studio-section ' + (className || ''));
    node.append(el(doc, 'h2', 'section-heading', title));
    return node;
  }
  function numeric(value, format) {
    if (value === null || value === undefined) return 'Unavailable';
    // Shell may provide its formatter directly or as format.number/format.count.
    const fn = typeof format === 'function' ? format : format && (format.number || format.count);
    return fn ? String(fn(value)) : value.toLocaleString();
  }
  function timestamp(ts) {
    if (ts === null) return 'Time unavailable';
    const date = new Date(ts * 1000);
    return Number.isFinite(date.getTime()) ? date.toLocaleString() : 'Time unavailable';
  }
  function renderChart(doc, analytics, format, stale, selected, tableOpen) {
    const panel = section(doc, 'Request activity', 'overview-traffic');
    panel.append(el(doc, 'p', 'overview-note', 'Requests per hour · Last 24 hours · Local time'));
    if (stale) panel.append(el(doc, 'p', 'overview-stale', 'Disconnected — this snapshot may be stale.'));
    if (!analytics || !analytics.series) {
      panel.append(el(doc, 'p', 'overview-empty', 'Request history unavailable. No zero values have been substituted.'));
      return panel;
    }
    const rows = analytics.series;
    if (!rows.length || rows.every(row => row.requests === 0)) {
      panel.append(el(doc, 'p', 'overview-empty', analytics.empty ? 'No requests yet in the last 24 hours.'
        : 'No hourly requests recorded in this window.'));
    } else {
      const geometry = chartGeometry(rows);
      const frame = el(doc, 'div', 'overview-chart');
      const chart = svg(doc, 'svg', {viewBox: '0 0 ' + geometry.width + ' ' + geometry.height,
        width: '100%', role: 'img', 'aria-label': 'Requests per hour over the last 24 hours. Use the hour controls or data table below.'});
      chart.append(svg(doc, 'title', {}, 'Requests per hour'));
      for (const tick of geometry.ticks) {
        chart.append(svg(doc, 'line', {x1: geometry.bounds.left, x2: geometry.bounds.right,
          y1: tick.y, y2: tick.y, stroke: 'var(--border, #262626)', 'stroke-width': 1}));
        chart.append(svg(doc, 'text', {x: geometry.bounds.left - 8, y: tick.y + 4,
          'text-anchor': 'end', fill: 'var(--muted, #A3A3A3)', 'font-size': 12}, numeric(tick.value, format)));
      }
      const indices = [...new Set([0, Math.floor((rows.length - 1) / 2), rows.length - 1])];
      for (const index of indices) {
        const point = geometry.points[index];
        chart.append(svg(doc, 'text', {x: point.x, y: geometry.height - 12,
          'text-anchor': index === 0 ? 'start' : index === rows.length - 1 ? 'end' : 'middle',
          fill: 'var(--muted, #A3A3A3)', 'font-size': 12}, point.hour));
      }
      chart.append(svg(doc, 'path', {d: geometry.path, fill: 'none', stroke: 'var(--chart-line, #A3A3A3)',
        'stroke-width': 2, 'vector-effect': 'non-scaling-stroke', 'stroke-linejoin': 'round'}));
      // One isolated sample is still visible without implying a trend.
      if (rows.length === 1) chart.append(svg(doc, 'circle', {cx: geometry.points[0].x,
        cy: geometry.points[0].y, r: 3, fill: 'var(--chart-line, #A3A3A3)'}));
      const crosshair = svg(doc, 'line', {y1: geometry.bounds.top, y2: geometry.bounds.bottom,
        stroke: 'var(--chart-line, #A3A3A3)', 'stroke-width': 1, 'stroke-dasharray': '3 4', visibility: 'hidden'});
      const dot = svg(doc, 'circle', {r: 4, fill: 'var(--chart-line, #A3A3A3)', stroke: 'var(--card, #0A0A0A)', 'stroke-width': 2, visibility: 'hidden'});
      chart.append(crosshair, dot);
      const tooltip = el(doc, 'output', 'overview-chart-tooltip', 'Select an hour to inspect requests.');
      tooltip.setAttribute('aria-live', 'polite');
      const controls = el(doc, 'div', 'overview-chart-controls');
      const slider = el(doc, 'input', 'overview-chart-slider');
      slider.type = 'range'; slider.min = '0'; slider.max = String(rows.length - 1); slider.step = '1';
      slider.value = String(Math.max(0, Math.min(selected ?? rows.length - 1, rows.length - 1)));
      slider.setAttribute('aria-label', 'Inspect request hour');
      function inspect(index) {
        index = Math.max(0, Math.min(index, rows.length - 1));
        const point = geometry.points[index];
        slider.value = String(index);
        frame.dataset.selected = String(index);
        const label = point.hour + ' · ' + numeric(point.requests, format) + ' requests';
        slider.setAttribute('aria-valuetext', label); tooltip.textContent = label;
        crosshair.setAttribute('x1', point.x); crosshair.setAttribute('x2', point.x);
        crosshair.setAttribute('visibility', 'visible');
        dot.setAttribute('cx', point.x); dot.setAttribute('cy', point.y); dot.setAttribute('visibility', 'visible');
      }
      slider.addEventListener('input', () => inspect(Number(slider.value)));
      // Native range supports Arrow keys, Home and End; focus shows the crosshair.
      slider.addEventListener('focus', () => inspect(Number(slider.value)));
      chart.addEventListener('pointermove', event => {
        const box = chart.getBoundingClientRect();
        if (!box.width) return;
        const x = (event.clientX - box.left) / box.width * geometry.width;
        const fraction = (x - geometry.bounds.left) / (geometry.bounds.right - geometry.bounds.left);
        inspect(Math.round(fraction * (rows.length - 1)));
      });
      controls.append(el(doc, 'label', 'overview-note', 'Inspect an hour'), slider);
      frame.append(chart, tooltip, controls); panel.append(frame);
      inspect(Number(slider.value));
    }
    const details = el(doc, 'details', 'overview-chart-table'); details.open = !!tableOpen;
    details.append(el(doc, 'summary', '', 'View hourly data table'));
    const wrap = el(doc, 'div', 'tbl-wrap');
    const table = el(doc, 'table', 'tbl-compact');
    table.append(el(doc, 'caption', 'overview-note', 'Hourly requests · Last 24 hours · Local time'));
    const head = el(doc, 'thead'), tr = el(doc, 'tr');
    for (const title of ['Hour', 'Requests']) { const th = el(doc, 'th', '', title); th.scope = 'col'; tr.append(th); }
    head.append(tr); table.append(head);
    const body = el(doc, 'tbody');
    for (const row of rows) { const r = el(doc, 'tr'); const hour = el(doc, 'th', '', row.hour); hour.scope = 'row';
      r.append(hour, el(doc, 'td', '', numeric(row.requests, format))); body.append(r); }
    table.append(body); wrap.append(table); details.append(wrap); panel.append(details);
    return panel;
  }
  function renderAttention(doc, summary, go) {
    const panel = section(doc, 'Needs attention', 'overview-attention');
    const list = el(doc, 'ul', 'attention-list');
    function item(level, message, tab, query, action) {
      const row = el(doc, 'li', 'overview-attention-item overview-status-' + level);
      row.append(icon(doc, level), el(doc, 'span', '', message));
      if (tab && typeof go === 'function') row.append(button(doc, action || 'Review', () => go(tab, query)));
      list.append(row);
    }
    if (!summary.pool) item('warning', 'Account readiness unavailable.');
    else {
      if (summary.pool.accounts === 0) item('warning', 'No accounts configured. Add an account to serve requests.', 'accounts', undefined, 'Open accounts');
      else if (summary.pool.ready === 0) item('warning', 'No accounts ready to serve requests.', 'accounts', undefined, 'Review accounts');
      for (const state of ['dead', 'exhausted', 'cooling']) {
        const n = summary.pool.byState[state];
        if (n > 0) item(state === 'dead' ? 'error' : 'warning', n + ' account' + (n === 1 ? '' : 's') + ' ' + state + '.', 'accounts', state, 'Review accounts');
      }
    }
    if (!summary.models) item('warning', 'Model availability unavailable.');
    else if (summary.models.unavailable > 0 && summary.pool && summary.pool.accounts > 0) {
      item('warning', summary.models.unavailable + ' model' + (summary.models.unavailable === 1 ? '' : 's') + ' unavailable on all accounts.',
        'models', summary.models.names[0] || undefined, 'Review models');
    }
    if (!list.children.length) item('success', 'No account or model issues reported.');
    panel.append(list);
    return panel;
  }
  function renderActivity(doc, events) {
    const panel = section(doc, 'Recent activity', 'overview-activity');
    if (events === null) { panel.append(el(doc, 'p', 'overview-empty', 'Recent activity unavailable.')); return panel; }
    if (!events.length) { panel.append(el(doc, 'p', 'overview-empty', 'No recent notifications.')); return panel; }
    const list = el(doc, 'ol', 'activity-list');
    for (const event of events) {
      const row = el(doc, 'li', 'overview-activity-item overview-status-' + event.level);
      const content = el(doc, 'div', 'overview-event-content');
      const title = el(doc, 'div', 'overview-event-title');
      title.append(el(doc, 'span', 'overview-event-level', {info: 'Info', success: 'Success', warning: 'Warning', error: 'Error'}[event.level]),
        el(doc, 'span', '', event.message));
      content.append(title);
      const meta = el(doc, 'div', 'overview-note');
      meta.append(el(doc, 'span', '', event.category ? event.category.replace(/_/g, ' ') + ' · ' : ''));
      const time = el(doc, 'time', '', timestamp(event.ts));
      if (event.ts !== null && Number.isFinite(new Date(event.ts * 1000).getTime())) time.dateTime = new Date(event.ts * 1000).toISOString();
      meta.append(time);
      if (event.count > 1) meta.append(el(doc, 'span', '', ' · ' + event.count + ' occurrences'));
      content.append(meta); row.append(icon(doc, event.level), content); list.append(row);
    }
    panel.append(list); return panel;
  }
  async function render(view, options) {
    options = options || {};
    if (!view || !view.ownerDocument || typeof options.api !== 'function') throw new TypeError('StudioOverview needs a view and api function.');
    const doc = view.ownerDocument, previous = view.querySelector('.overview-grid');
    const oldChart = previous && previous.querySelector('.overview-chart');
    const selected = oldChart ? Number(oldChart.dataset.selected) : undefined;
    const oldTable = previous && previous.querySelector('.overview-chart-table');
    const tableOpen = oldTable && oldTable.open;
    const focusedChart = previous && previous.contains(doc.activeElement) && doc.activeElement.classList.contains('overview-chart-slider');
    const token = {}; owners.set(view, token);
    // Ownership marker also prevents a late request replacing a different tab.
    const loading = el(doc, 'p', 'overview-loading overview-note', previous ? 'Refreshing Overview…' : 'Loading Overview…');
    loading.setAttribute('role', 'status');
    if (previous) previous.append(loading); else view.replaceChildren(loading);
    const results = await Promise.allSettled(REQUESTS.map(([, path]) => Promise.resolve().then(() => options.api(path, {method: 'GET'}))));
    const summary = buildSummary(Object.fromEntries(REQUESTS.map(([name], index) => [name, results[index]])));
    if (owners.get(view) !== token || !view.contains(loading)) return summary;
    const connected = typeof options.connected === 'function' ? options.connected() !== false : options.connected !== false;
    const stale = !connected;
    const root = el(doc, 'div', 'overview-grid');
    const gateway = section(doc, 'Your gateway', 'gateway-panel');
    const gatewayHeader = el(doc, 'div', 'overview-gateway-heading');
    const live = summary.server && connected;
    const status = el(doc, 'span', 'overview-gateway-status overview-status-' + (live ? 'success' : 'warning'));
    status.append(icon(doc, live ? 'success' : 'warning'), el(doc, 'span', '', live ? 'Gateway responding' : connected ? 'Gateway information unavailable' : 'Disconnected'));
    gatewayHeader.append(status);
    if (summary.server && summary.server.version) gatewayHeader.append(el(doc, 'span', 'overview-note', 'Version ' + summary.server.version));
    gateway.append(gatewayHeader);
    const endpointRow = el(doc, 'div', 'overview-endpoint-row');
    const endpointValue = el(doc, 'div', 'overview-endpoint-value');
    endpointValue.append(el(doc, 'span', 'overview-note', 'Local API base URL'),
      el(doc, 'code', 'overview-endpoint mono', summary.server ? summary.server.endpoint : 'Endpoint unavailable'));
    endpointRow.append(endpointValue);
    if (summary.server) {
      const value = summary.server.endpoint;
      const copy = button(doc, 'Copy endpoint', async () => {
        try {
          const navigator = doc.defaultView && doc.defaultView.navigator;
          if (!navigator || !navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') throw new Error('Clipboard unavailable');
          await navigator.clipboard.writeText(value); copy.textContent = 'Copied';
        } catch (_) { copy.textContent = 'Copy unavailable'; }
      }, 'btn btn-quiet btn-sm overview-copy');
      endpointRow.append(copy);
    }
    gateway.append(endpointRow);
    const metrics = el(doc, 'dl', 'metric-strip');
    function metric(label, value, note) {
      const cell = el(doc, 'div', 'metric');
      cell.append(el(doc, 'dt', 'metric-label', label), el(doc, 'dd', 'metric-value', value));
      if (note) cell.append(el(doc, 'dd', 'metric-note overview-note', note));
      metrics.append(cell);
    }
    metric('Ready accounts', summary.pool ? numeric(summary.pool.ready, options.format) : 'Unavailable',
      summary.pool ? 'of ' + numeric(summary.pool.accounts, options.format) + ' configured' : 'Readiness could not be loaded');
    metric('Requests · 24h', summary.analytics ? numeric(summary.analytics.requests, options.format) : 'Unavailable',
      summary.analytics && summary.analytics.empty ? 'No requests yet' : 'Rolling 24-hour ledger');
    metric('Success rate · 24h', summary.analytics && summary.analytics.successRate !== null
      ? summary.analytics.successRate.toFixed(1) + '%' : summary.analytics && summary.analytics.empty ? 'No requests yet' : 'Unavailable', 'Successful requests / all requests');
    if (summary.analytics && summary.analytics.latency) metric('Median latency · p50',
      (summary.analytics.latency.p50 / 1000).toFixed(2) + ' s', numeric(summary.analytics.latency.samples, options.format) + ' successful request samples');
    gateway.append(metrics);
    const unavailable = ['pool', 'models', 'analytics', 'notifications', 'server'].filter(name => !summary[name]);
    const refreshed = el(doc, 'p', 'overview-refresh overview-note',
      'Last refreshed ' + new Date().toLocaleTimeString() + (stale ? ' · Disconnected — snapshot may be stale.' : '')
      + (unavailable.length ? ' · Some data unavailable: ' + unavailable.join(', ') + '.' : ''));
    refreshed.setAttribute('role', 'status'); gateway.append(refreshed);
    root.append(gateway, renderChart(doc, summary.analytics, options.format, stale, selected, tableOpen),
      renderAttention(doc, summary, options.go), renderActivity(doc, summary.notifications));
    view.replaceChildren(root);
    if (focusedChart) { const slider = root.querySelector('.overview-chart-slider'); if (slider) slider.focus(); }
    return summary;
  }
  // escape is accepted for shell compatibility. All data goes through textContent,
  // not HTML interpolation, so escaping again would double-encode user text.
  return Object.freeze({render, buildSummary, chartGeometry, REQUESTS});
});
