/* Presentation preferences only. No credentials, requests, or automatic listeners. */
(function (root, factory) {
  const exported = factory();
  if (typeof module === "object" && module.exports) module.exports = exported;
  else root.StudioPreferences = exported;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const VERSION = 1, MAX_BYTES = 16384, MAX_STRING = 256, MAX_FAVORITES = 100, MAX_CATALOG = 4096;
  const ACCOUNT_FILTERS = ["all", "ready", "cooling", "attention"];
  const MODEL_FILTERS = ["all", "available", "limited", "unavailable"];
  const SORT_KEYS = ["rank", "model", "lane", "ratio", "next"];
  const TABS = ["overview", "accounts", "models", "usage", "analytics", "stats", "logs",
    "generator", "proxy", "server", "settings", "guide"];
  const FIELDS = ["version", "accountFilter", "modelFilter", "favoritesOnly", "modelSort", "favorites", "selectedTab"];
  const STATUS_RANK = {available: 0, cooling: 1, partial: 1, capped: 2,
    no_entitlement: 3, no_credit: 4, unavailable: 5};
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const record = value => !!value && typeof value === "object" && !Array.isArray(value)
    && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
  const safeId = value => typeof value === "string" && value.length <= MAX_STRING
    && /^[A-Za-z0-9][A-Za-z0-9._:/-]*$/.test(value)
    && !["__proto__", "prototype", "constructor"].includes(value);
  function defaults() {
    return {version: VERSION, accountFilter: "all", modelFilter: "all", favoritesOnly: false,
      modelSort: {key: "rank", dir: 1}, favorites: [], selectedTab: "overview"};
  }
  function copy(value) {
    return {version: VERSION, accountFilter: value.accountFilter, modelFilter: value.modelFilter,
      favoritesOnly: value.favoritesOnly, modelSort: {...value.modelSort},
      favorites: value.favorites.slice(), selectedTab: value.selectedTab};
  }
  // Count UTF-8 bytes without relying on browser/Node globals. Lone surrogates encode as replacement characters.
  function byteLength(text) {
    let count = 0;
    for (const char of text) {
      const n = char.codePointAt(0);
      count += n <= 0x7f ? 1 : n <= 0x7ff ? 2 : n <= 0xffff ? 3 : 4;
      if (count > MAX_BYTES) break;
    }
    return count;
  }
  function validSort(value) {
    return record(value) && Object.keys(value).every(key => key === "key" || key === "dir")
      && own(value, "key") && SORT_KEYS.includes(value.key)
      && own(value, "dir") && (value.dir === 1 || value.dir === -1);
  }
  function parse(text) {
    if (typeof text !== "string" || text.length > MAX_BYTES || byteLength(text) > MAX_BYTES) return null;
    let value;
    try { value = JSON.parse(text); } catch (_) { return null; }
    if (!record(value) || !own(value, "version") || value.version !== VERSION
        || Object.keys(value).some(key => !FIELDS.includes(key))) return null;
    const next = defaults();
    for (const [field, allowed] of [["accountFilter", ACCOUNT_FILTERS], ["modelFilter", MODEL_FILTERS], ["selectedTab", TABS]]) {
      if (own(value, field)) {
        if (!allowed.includes(value[field])) return null;
        next[field] = value[field];
      }
    }
    if (own(value, "favoritesOnly")) {
      if (typeof value.favoritesOnly !== "boolean") return null;
      next.favoritesOnly = value.favoritesOnly;
    }
    if (own(value, "modelSort")) {
      if (!validSort(value.modelSort)) return null;
      next.modelSort = {key: value.modelSort.key, dir: value.modelSort.dir};
    }
    if (own(value, "favorites")) {
      if (!Array.isArray(value.favorites) || value.favorites.length > MAX_FAVORITES
          || !value.favorites.every(safeId)) return null;
      next.favorites = [...new Set(value.favorites)];
    }
    return byteLength(JSON.stringify(next)) <= MAX_BYTES ? next : null;
  }
  function validOrigin(value) {
    if (typeof value !== "string" || value.length > MAX_STRING) return null;
    try {
      const url = new URL(value);
      return ["http:", "https:"].includes(url.protocol) && url.origin === value ? value : null;
    } catch (_) { return null; }
  }
  function scope(identity, origin) {
    if (!origin || !record(identity) || identity.verified === false || identity.foreign === true
        || identity.state === "foreign" || identity.schema_version !== 1
        || identity.product !== "autozcoder-linux" || typeof identity.version !== "string"
        || identity.version.length >= 80 || typeof identity.instance_id !== "string"
        || !identity.instance_id.length || identity.instance_id.length >= 160
        || typeof identity.config_id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,158}$/.test(identity.config_id)
        || ["__proto__", "prototype", "constructor"].includes(identity.config_id)
        || !["user-service", "foreground", "embedded"].includes(identity.mode)
        || typeof identity.stopping !== "boolean") return null;
    // Deliberately do not retain the input identity, boot ID, version, or credentials.
    return "autozcoder.ui-preferences.v1:" + encodeURIComponent(origin) + ":"
      + encodeURIComponent(identity.config_id) + ":" + identity.mode;
  }
  function createStore(options = {}) {
    const storage = options.storage;
    const origin = validOrigin(options.origin);
    const onChange = typeof options.onChange === "function" ? options.onChange : () => {};
    let state = defaults(), key = null, catalog = new Set(), failed = false, disposed = false;
    const get = () => copy(state);
    const notify = reason => { if (!disposed) onChange(get(), reason); };
    function io(method, ...args) {
      if (!key || failed || disposed || !storage) return null;
      try { return storage[method](...args); } catch (_) { failed = true; return null; }
    }
    function persist() {
      const text = JSON.stringify(state);
      if (byteLength(text) <= MAX_BYTES) io("setItem", key, text);
    }
    function bind(identity) {
      if (disposed) return get();
      const nextKey = scope(identity, origin);
      if (nextKey && key === nextKey) return get(); // A new boot is not a new profile.
      key = nextKey; failed = false; state = defaults(); catalog = new Set();
      const loaded = parse(io("getItem", key));
      if (loaded) state = loaded;
      notify("bind");
      return get();
    }
    function update(patch) {
      if (disposed || !record(patch)) return get();
      const next = copy(state);
      for (const [field, allowed] of [["accountFilter", ACCOUNT_FILTERS], ["modelFilter", MODEL_FILTERS], ["selectedTab", TABS]]) {
        if (own(patch, field) && allowed.includes(patch[field])) next[field] = patch[field];
      }
      if (own(patch, "favoritesOnly") && typeof patch.favoritesOnly === "boolean") next.favoritesOnly = patch.favoritesOnly;
      if (own(patch, "modelSort") && validSort(patch.modelSort)) next.modelSort = {key: patch.modelSort.key, dir: patch.modelSort.dir};
      if (own(patch, "favorites") && Array.isArray(patch.favorites) && patch.favorites.length <= MAX_FAVORITES) {
        const previous = new Set(state.favorites), seen = new Set();
        next.favorites = [];
        // Persisted/previous favorites can be retained or unpinned, even if no longer in the catalog.
        // Only newly pinned IDs must occur in the currently loaded catalog.
        for (const id of patch.favorites) {
          if (safeId(id) && !seen.has(id) && (previous.has(id) || catalog.has(id))) {
            seen.add(id); next.favorites.push(id);
            if (next.favorites.length === MAX_FAVORITES) break;
          }
        }
        // Reject an oversized favorite edit rather than silently unpinning existing IDs.
        if (byteLength(JSON.stringify(next)) > MAX_BYTES) next.favorites = state.favorites.slice();
      }
      if (byteLength(JSON.stringify(next)) > MAX_BYTES || JSON.stringify(next) === JSON.stringify(state)) return get();
      state = next; persist(); notify("update");
      return get();
    }
    function reset() {
      if (disposed) return get();
      state = defaults(); io("removeItem", key); notify("reset");
      return get();
    }
    function setCatalog(ids) {
      if (!disposed) {
        catalog = new Set();
        if (Array.isArray(ids)) {
          for (let i = 0; i < Math.min(ids.length, MAX_CATALOG); i++) {
            if (safeId(ids[i])) catalog.add(ids[i]);
          }
        }
      }
      return get(); // Never prune favorites or write merely because a catalog refreshed.
    }
    function handleStorage(event) {
      if (disposed || !key || !event || event.key !== key
          || (event.storageArea != null && event.storageArea !== storage)) return false;
      if (event.url != null) {
        try { if (new URL(event.url).origin !== origin) return false; } catch (_) { return false; }
      }
      const next = event.newValue === null ? defaults() : parse(event.newValue);
      if (!next) return false;
      if (JSON.stringify(next) !== JSON.stringify(state)) { state = next; notify("storage"); }
      return true; // No echo writes, including cross-tab resets.
    }
    function dispose() { disposed = true; key = null; catalog.clear(); state = defaults(); }
    return {bind, get, update, reset, setCatalog, handleStorage, dispose};
  }
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const nowValue = now => finite(now) ? now : Date.now();
  function attention(account, now) {
    if (["dead", "exhausted"].includes(account.state) || account.paid_exhausted === true
        || account.needs_refresh === true || account.expired === true) return true;
    if ((finite(account.expires_at) && account.expires_at > 0 && account.expires_at <= now)
        || (finite(account.expires_in_s) && account.expires_in_s <= 0)) return true;
    // capped_models is already the active-only list in Account.to_public().
    if (Array.isArray(account.capped_models) && account.capped_models.some(id => typeof id === "string" && id.length)) return true;
    if (record(account.model_caps)) {
      return Object.values(account.model_caps).some(cap => record(cap)
        && ((finite(cap.release_in_s) && cap.release_in_s > 0)
          || (finite(cap.release_at) && cap.release_at * 1000 > now)));
    }
    return false; // A recovered account's historical errors are not current warnings.
  }
  function queryMatches(values, query) {
    const text = typeof query === "string" ? query.trim().toLowerCase() : "";
    return !text || values.filter(value => typeof value === "string").join(" ").toLowerCase().includes(text);
  }
  function accountMatches(account, filter = "all", query = "", now) {
    if (!record(account)) return false;
    const category = ACCOUNT_FILTERS.includes(filter) ? filter : "all";
    const matches = category === "all" || (category === "ready" && account.state === "ready")
      || (category === "cooling" && account.state === "cooling")
      || (category === "attention" && attention(account, nowValue(now)));
    return matches && queryMatches([account.email, account.id, account.state, account.source,
      ...(Array.isArray(account.capped_models) ? account.capped_models : [])], query);
  }
  function accountCounts(rows, now) {
    const counts = {all: 0, ready: 0, cooling: 0, attention: 0}, time = nowValue(now);
    for (const account of Array.isArray(rows) ? rows : []) {
      for (const filter of ACCOUNT_FILTERS) if (accountMatches(account, filter, "", time)) counts[filter]++;
    }
    return counts;
  }
  function favoriteSet(favorites) {
    return favorites instanceof Set ? favorites : new Set(Array.isArray(favorites) ? favorites.filter(safeId) : []);
  }
  function modelMatches(model, filter = "all", query = "", favoritesOnly = false, favorites = []) {
    if (!record(model)) return false;
    const category = MODEL_FILTERS.includes(filter) ? filter : "all";
    const matches = category === "all" || (category === "available" && model.overall === "available")
      || (category === "limited" && ["partial", "cooling", "capped"].includes(model.overall))
      || (category === "unavailable" && ["no_credit", "no_entitlement", "unavailable"].includes(model.overall));
    return matches && (!favoritesOnly || favoriteSet(favorites).has(model.model))
      && queryMatches([model.model, model.lane, model.overall], query);
  }
  function modelCounts(rows) {
    const counts = {all: 0, available: 0, limited: 0, unavailable: 0};
    for (const model of Array.isArray(rows) ? rows : []) {
      for (const filter of MODEL_FILTERS) if (modelMatches(model, filter)) counts[filter]++;
    }
    return counts;
  }
  function compareTuple(a, b) {
    const aa = Array.isArray(a) ? a : [a], bb = Array.isArray(b) ? b : [b];
    for (let i = 0; i < Math.min(aa.length, bb.length); i++) {
      if (aa[i] < bb[i]) return -1;
      if (aa[i] > bb[i]) return 1;
    }
    return aa.length < bb.length ? -1 : aa.length > bb.length ? 1 : 0;
  }
  function sortModels(models, sort, favorites = []) {
    const order = validSort(sort) ? sort : {key: "rank", dir: 1}, pinned = favoriteSet(favorites);
    const id = model => typeof model.model === "string" ? model.model : "";
    const keyFns = {
      rank: model => [own(STATUS_RANK, model.overall) ? STATUS_RANK[model.overall] : 9],
      model: model => [id(model)],
      lane: model => [typeof model.lane === "string" ? model.lane : ""],
      ratio: model => [finite(model.total_accounts) && model.total_accounts > 0 && finite(model.available_on)
        ? model.available_on / model.total_accounts : 0],
      next: model => [finite(model.next_release_in_s) ? model.next_release_in_s : Infinity]
    };
    return (Array.isArray(models) ? models.filter(record) : []).slice().sort((a, b) => {
      const favoriteOrder = Number(pinned.has(id(b))) - Number(pinned.has(id(a)));
      return favoriteOrder || compareTuple(keyFns[order.key](a), keyFns[order.key](b)) * order.dir
        || compareTuple(id(a), id(b));
    });
  }
  return {createStore, accountMatches, accountCounts, modelMatches, modelCounts, sortModels, compareTuple};
});
