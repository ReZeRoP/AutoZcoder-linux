/* Local-only accessibility helpers. The shell supplies an explicit command registry
 * and owns account rendering, authorization, networking and modal precedence. */
(function (root, factory) {
  'use strict';
  const usability = factory();
  if (typeof module === 'object' && module.exports) module.exports = usability;
  else root.StudioUsability = usability;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const MAX_COMMANDS = 200;
  let serial = 0;
  const text = (value, limit) => typeof value === 'string' ? value.slice(0, limit) : '';
  const normalized = value => text(value, 256).trim().toLocaleLowerCase().replace(/\s+/g, ' ');
  const composing = event => !!(event.isComposing || event.keyCode === 229);
  function consume(event) {
    event.preventDefault();
    event.stopPropagation();
    if (event.stopImmediatePropagation) event.stopImmediatePropagation();
  }
  function registry(commands) {
    const seen = new Set(), result = [];
    for (const command of Array.isArray(commands) ? commands.slice(0, MAX_COMMANDS) : []) {
      if (!command || typeof command !== 'object' || typeof command.id !== 'string' ||
          !command.id || command.id.length > 256 || !text(command.label, 160).trim() || seen.has(command.id)) continue;
      seen.add(command.id); result.push(command);
    }
    return result;
  }
  /** Returns original descriptors, never introspects application exports or arbitrary
   * descriptor fields. Only id, label, description and keywords participate in search. */
  function searchCommands(query, commands) {
    const needle = normalized(query), tokens = needle.split(' ').filter(Boolean);
    return registry(commands).map((command, index) => {
      const label = normalized(command.label);
      const keywords = Array.isArray(command.keywords)
        ? command.keywords.slice(0, 32).map(value => text(value, 80)).join(' ')
        : text(command.keywords, 256);
      const haystack = (text(command.id, 256) + ' ' + text(command.label, 160) + ' ' +
        text(command.description, 320) + ' ' + keywords).toLocaleLowerCase();
      const score = !needle ? 0 : label === needle ? 0 : label.startsWith(needle) ? 1 : label.includes(needle) ? 2 : 3;
      return {command, index, score, matches: tokens.every(token => haystack.includes(token))};
    }).filter(entry => entry.matches).sort((a, b) => a.score - b.score || a.index - b.index)
      .map(entry => entry.command);
  }
  function connected(element, document) {
    if (!element) return false;
    return typeof element.isConnected === 'boolean' ? element.isConnected
      : !!(document.documentElement && document.documentElement.contains(element));
  }
  function available(element, document) {
    if (!connected(element, document) || typeof element.focus !== 'function' || element.disabled) return false;
    for (let node = element; node && node.getAttribute; node = node.parentElement || node.parentNode) {
      if (node.hidden || node.getAttribute('hidden') !== null || node.getAttribute('aria-hidden') === 'true' ||
          node.getAttribute('inert') !== null) return false;
      const style = document.defaultView && document.defaultView.getComputedStyle;
      if (style) {
        const computed = style.call(document.defaultView, node);
        if (computed.display === 'none' || computed.visibility === 'hidden') return false;
      }
    }
    return true;
  }
  function focus(element) {
    if (!element) return;
    try { element.focus({preventScroll: true}); } catch (_) { element.focus(); }
  }
  /** element is the backdrop/container; an existing descendant [role=dialog]
   * is the focus boundary, otherwise element itself becomes the dialog. Uses
   * hidden + .is-open, does not replace content. isBlocked must report only
   * higher-priority confirmation/login overlays, excluding this overlay. */
  function createOverlay(options) {
    const element = options.element;
    const document = options.document || (element && element.ownerDocument);
    if (!element || !document) throw new TypeError('An overlay element and document are required');
    const dialog = element.getAttribute('role') === 'dialog' ? element
      : element.querySelector('[role="dialog"]') || element;
    const blocked = () => typeof options.isBlocked === 'function' && !!options.isBlocked();
    let opened = false, destroyed = false, opener = null, redirecting = false;
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    if (!dialog.hasAttribute('tabindex')) dialog.setAttribute('tabindex', '-1');
    element.hidden = true;
    function focusables() {
      return Array.from(dialog.querySelectorAll('a[href],button,input,select,textarea,[tabindex],[contenteditable="true"]'))
        .filter(node => available(node, document) && node.getAttribute('type') !== 'hidden' &&
          (node.getAttribute('tabindex') === null || Number(node.getAttribute('tabindex')) >= 0));
    }
    function focusFirst() { focus(focusables()[0] || dialog); }
    function handleKey(event) {
      if (!opened || blocked() || event.defaultPrevented) return false;
      if (typeof options.onKey === 'function' && options.onKey(event)) return true;
      if (composing(event)) return false;
      if (event.key === 'Escape') { consume(event); close(); return true; }
      if (event.key !== 'Tab') return false;
      const nodes = focusables(), active = document.activeElement, index = nodes.indexOf(active);
      const next = !nodes.length ? dialog : index < 0 ? nodes[event.shiftKey ? nodes.length - 1 : 0]
        : nodes[(index + (event.shiftKey ? -1 : 1) + nodes.length) % nodes.length];
      consume(event); focus(next); return true;
    }
    function handleFocus(event) {
      if (!opened || blocked() || redirecting || dialog.contains(event.target)) return;
      redirecting = true;
      try { focusFirst(); } finally { redirecting = false; }
    }
    function handleBackdrop(event) {
      if (!opened || blocked() || event.target !== element || element === dialog) return;
      consume(event); close();
    }
    function open(origin) {
      if (destroyed || blocked()) return false;
      if (opened) return true;
      opener = origin || document.activeElement;
      opened = true; element.hidden = false; element.classList.add('is-open');
      document.addEventListener('keydown', handleKey, true);
      document.addEventListener('focusin', handleFocus, true);
      element.addEventListener('click', handleBackdrop);
      focusFirst(); return true;
    }
    function close() {
      if (!opened) return false;
      opened = false; element.hidden = true; element.classList.remove('is-open');
      document.removeEventListener('keydown', handleKey, true);
      document.removeEventListener('focusin', handleFocus, true);
      element.removeEventListener('click', handleBackdrop);
      const origin = opener; opener = null;
      if (!blocked()) {
        const fallback = typeof options.fallbackFocus === 'function' ? options.fallbackFocus() : options.fallbackFocus;
        focus(available(origin, document) ? origin : available(fallback, document) ? fallback : null);
      }
      if (typeof options.onClose === 'function') options.onClose();
      return true;
    }
    return {element, dialog, open, close, isOpen: () => opened, handleKey,
      destroy() { close(); destroyed = true; }};
  }
  function createPalette(options) {
    const document = options.document;
    if (!document || !document.body) throw new TypeError('A document with a body is required');
    const id = 'ui-palette-' + (++serial);
    function node(tag, className, content) {
      const element = document.createElement(tag); element.className = className;
      if (content !== undefined) element.textContent = content;
      return element;
    }
    const element = node('div', 'ui-palette-overlay');
    const dialog = node('section', 'ui-palette');
    dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-labelledby', id + '-title');
    const title = node('h2', 'ui-palette-title', 'Search commands'); title.id = id + '-title';
    const input = node('input', 'ui-palette-input');
    input.type = 'text'; input.maxLength = 256; input.placeholder = 'Search pages and local actions';
    input.setAttribute('role', 'combobox'); input.setAttribute('aria-label', 'Search commands');
    input.setAttribute('aria-autocomplete', 'list'); input.setAttribute('aria-haspopup', 'listbox');
    input.setAttribute('aria-controls', id + '-list'); input.setAttribute('aria-expanded', 'false');
    input.setAttribute('autocomplete', 'off'); input.setAttribute('spellcheck', 'false');
    const list = node('div', 'ui-palette-list'); list.id = id + '-list';
    list.setAttribute('role', 'listbox'); list.setAttribute('aria-label', 'Commands');
    const status = node('p', 'ui-palette-status'); status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    const footer = node('div', 'ui-palette-footer');
    const hint = node('span', 'ui-palette-hint', 'Arrow keys to select · Enter to activate · Escape to close');
    const closeButton = node('button', 'ui-palette-close', 'Close'); closeButton.type = 'button';
    closeButton.setAttribute('aria-label', 'Close command palette');
    footer.append(hint, closeButton); dialog.append(title, input, list, status, footer);
    element.append(dialog); document.body.append(element);
    let matches = [], rows = [], selected = -1, ime = false;
    const blocked = () => typeof options.isBlocked === 'function' && !!options.isBlocked();
    const commands = () => typeof options.commands === 'function' ? options.commands() : options.commands;
    function select(index, scroll) {
      selected = matches.length ? Math.max(0, Math.min(index, matches.length - 1)) : -1;
      rows.forEach((row, i) => {
        row.setAttribute('aria-selected', String(i === selected));
        row.classList.toggle('is-selected', i === selected);
      });
      if (selected < 0) input.removeAttribute('aria-activedescendant');
      else {
        input.setAttribute('aria-activedescendant', rows[selected].id);
        if (scroll && rows[selected].scrollIntoView) rows[selected].scrollIntoView({block: 'nearest'});
      }
    }
    function update(reset) {
      const previous = !reset && matches[selected] && matches[selected].id;
      matches = searchCommands(input.value, commands());
      rows = matches.map((command, index) => {
        const row = node('div', 'ui-command'); row.id = id + '-option-' + index;
        row.setAttribute('role', 'option'); row.setAttribute('aria-disabled', String(!!command.disabled));
        row.setAttribute('tabindex', '-1');
        row.append(node('span', 'ui-command-label', text(command.label, 160)));
        if (text(command.description, 320)) row.append(node('span', 'ui-command-description', text(command.description, 320)));
        if (command.disabled) row.append(node('span', 'ui-command-disabled', 'Unavailable'));
        row.addEventListener('click', event => {
          if (blocked() || !overlay.isOpen()) return;
          consume(event); select(index, false); activate();
        });
        return row;
      });
      list.replaceChildren(...rows);
      const oldIndex = previous ? matches.findIndex(command => command.id === previous) : -1;
      select(oldIndex >= 0 ? oldIndex : 0, false);
      status.textContent = matches.length ? matches.length + (matches.length === 1 ? ' command' : ' commands') : 'No commands found';
      return matches.length;
    }
    function activate() {
      if (!overlay.isOpen() || blocked() || ime || selected < 0) return false;
      const target = matches[selected];
      // Re-resolve at activation: a stale row must not run a disappeared or
      // newly disabled command, including when connection state changed.
      const current = searchCommands(input.value, commands()).find(command => command.id === target.id);
      if (!current || current.disabled || (typeof options.onActivate !== 'function' && typeof current.run !== 'function')) {
        update(); return false;
      }
      close();
      if (typeof options.onActivate === 'function') options.onActivate(current);
      else current.run();
      return true;
    }
    function paletteKey(event) {
      if (ime || composing(event)) return true; // Leave composition keys to the IME, including Escape.
      // Button keyboard activation remains native; only the combobox owns
      // list navigation. The shared overlay still traps Tab and Escape.
      if (event.target !== input && document.activeElement !== input) return false;
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter'].includes(event.key)) return false;
      consume(event);
      if (event.key === 'Enter') { if (!event.repeat) activate(); return true; }
      if (event.key === 'Home') select(0, true);
      else if (event.key === 'End') select(matches.length - 1, true);
      else select(matches.length ? (selected + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length : -1, true);
      return true;
    }
    const overlay = createOverlay({document, element, fallbackFocus: options.fallbackFocus,
      isBlocked: blocked, onKey: paletteKey, onClose() {
        input.setAttribute('aria-expanded', 'false');
        if (typeof options.onClose === 'function') options.onClose();
      }});
    function open(opener) {
      if (blocked()) return false;
      if (!overlay.isOpen()) { input.value = ''; ime = false; update(true); }
      const opened = overlay.open(opener);
      if (opened) input.setAttribute('aria-expanded', 'true');
      return opened;
    }
    function close() { return overlay.close(); }
    input.addEventListener('input', () => { if (overlay.isOpen() && !blocked()) update(true); });
    input.addEventListener('compositionstart', () => { ime = true; });
    input.addEventListener('compositionend', () => { ime = false; if (overlay.isOpen() && !blocked()) update(true); });
    closeButton.addEventListener('click', event => { if (!blocked()) { consume(event); close(); } });
    return {element, dialog, input, list, status, closeButton, open, close,
      isOpen: overlay.isOpen, handleKey: overlay.handleKey, update: () => update(false),
      setQuery(query) { input.value = text(query, 256); return update(true); },
      destroy() { overlay.destroy(); element.remove(); }};
  }
  return Object.freeze({MAX_COMMANDS, searchCommands, createOverlay, createDrawer: createOverlay, createPalette});
});
