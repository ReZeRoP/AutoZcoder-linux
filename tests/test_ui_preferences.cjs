"use strict";
// Synthetic fixtures only; no browser, gateway, credentials, or real localStorage.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const P = require("../zcode_gateway/dashboard/preferences.js");
const ORIGIN = "http://127.0.0.1:8789";
const identity = (config = "profile-one", mode = "user-service", boot = "boot-one") => ({
  schema_version: 1, product: "autozcoder-linux", version: "0.0.test", instance_id: boot,
  config_id: config, mode, stopping: false
});
const expected = () => ({version: 1, accountFilter: "all", modelFilter: "all", favoritesOnly: false,
  modelSort: {key: "rank", dir: 1}, favorites: [], selectedTab: "overview"});
function memoryStorage() {
  const values = new Map(), calls = [];
  return {values, calls,
    getItem(key) { calls.push(["get", key]); return values.get(key) ?? null; },
    setItem(key, value) { calls.push(["set", key, value]); values.set(key, value); },
    removeItem(key) { calls.push(["remove", key]); values.delete(key); }
  };
}
function fixture(options = {}) {
  const storage = options.storage || memoryStorage(), changes = [];
  const store = P.createStore({storage, origin: ORIGIN, onChange: (state, reason) => changes.push([state, reason]), ...options});
  return {storage, store, changes};
}
function activeKey(f) { return f.storage.calls.find(call => call[0] === "get")[1]; }
let tests = 0;
function test(name, fn) {
  try { fn(); tests++; process.stdout.write("ok - " + name + "\n"); }
  catch (error) { process.stderr.write("FAIL - " + name + "\n"); throw error; }
}

test("UMD global exports without any browser activity", () => {
  const context = vm.createContext({URL});
  vm.runInContext(fs.readFileSync(require.resolve("../zcode_gateway/dashboard/preferences.js"), "utf8"), context);
  assert.equal(typeof context.StudioPreferences.createStore, "function");
  assert.equal(typeof context.StudioPreferences.accountMatches, "function");
  assert.equal(typeof context.StudioPreferences.sortModels, "function");
});
test("unbound, unknown and explicitly foreign identities never touch storage", () => {
  const f = fixture();
  assert.deepEqual(f.store.get(), expected());
  f.store.update({accountFilter: "attention", selectedAccountId: "private-account", query: "pasted-secret"});
  f.store.reset();
  const invalid = [null, {}, {config_id: "profile-one", mode: "user-service"},
    {...identity(), schema_version: 2}, {...identity(), product: "other"}, {...identity(), mode: "unknown"},
    {...identity(), config_id: ""}, {...identity(), config_id: "../private/path"},
    {...identity(), config_id: "__proto__"}, {...identity(), config_id: "x".repeat(160)},
    {...identity(), verified: false}, {...identity(), foreign: true}, {...identity(), state: "foreign"},
    {...identity(), instance_id: ""}, {...identity(), stopping: "false"}];
  for (const value of invalid) {
    assert.deepEqual(f.store.bind(value), expected());
    f.store.update({accountFilter: "ready"});
  }
  assert.equal(f.storage.calls.length, 0);
});
test("untrusted origin uses RAM only", () => {
  for (const origin of [undefined, "null", "file:///tmp/a", "http://user:pass@localhost:8789", ORIGIN + "/path", "x".repeat(257)]) {
    const f = fixture({origin});
    f.store.bind(identity()); f.store.update({modelFilter: "limited"}); f.store.reset();
    assert.equal(f.storage.calls.length, 0);
  }
});
test("all allowlisted presentation fields round-trip and no secrets persist", () => {
  const f = fixture(); f.storage.values.set("credentials", "do-not-touch");
  f.store.bind({...identity(), key: "admin-secret", access_token: "access-secret", notes: {private: true}});
  f.store.setCatalog(["model/a", "model-b", "unavailable-model"]);
  f.store.update({accountFilter: "attention", modelFilter: "limited", favoritesOnly: true,
    modelSort: {key: "next", dir: -1}, favorites: ["model/a", "unavailable-model"], selectedTab: "models",
    accountQuery: "person@example.test", modelQuery: "pasted-api-key", paletteQuery: "private query",
    selectedAccount: "account-sensitive-id", selectedAccountId: "other-private-id", key: "admin-secret",
    token: "access-secret", bootid: "boot-one", notes: {nested: "secret"}});
  const key = activeKey(f), raw = f.storage.values.get(key), stored = JSON.parse(raw);
  assert.deepEqual(Object.keys(stored).sort(), Object.keys(expected()).sort());
  assert.deepEqual(stored, f.store.get());
  for (const secret of ["person@example.test", "pasted-api-key", "private query", "account-sensitive-id", "admin-secret", "access-secret", "boot-one", "notes"]) {
    assert.equal(raw.includes(secret), false, secret);
    assert.equal(key.includes(secret), false, "key: " + secret);
  }
  const restarted = fixture({storage: f.storage});
  assert.deepEqual(restarted.store.bind(identity("profile-one", "user-service", "next-boot")), stored);
  assert.equal(f.storage.values.get("credentials"), "do-not-touch");
});
test("profile, mode and origin separation; boot changes do not reload or reset RAM", () => {
  const f = fixture(); f.store.bind(identity()); f.store.setCatalog(["a"]);
  f.store.update({favorites: ["a"], accountFilter: "ready"});
  const original = f.store.get(), n = f.storage.calls.length;
  assert.deepEqual(f.store.bind(identity("profile-one", "user-service", "new-boot")), original);
  assert.equal(f.storage.calls.length, n);
  f.store.update({favorites: ["a"]}); assert.equal(f.storage.calls.length, n);
  assert.deepEqual(f.store.bind(identity("profile-two")), expected());
  f.store.update({modelFilter: "unavailable"});
  assert.deepEqual(f.store.bind(identity("profile-one", "foreground")), expected());
  f.store.update({accountFilter: "cooling"});
  assert.deepEqual(f.store.bind(identity()), original);
  // Catalog is scope-local, not inherited on switching back to a profile.
  f.store.update({favorites: ["a", "new-unloaded"]});
  assert.deepEqual(f.store.get().favorites, ["a"]);
  const otherOrigin = fixture({storage: f.storage, origin: "http://localhost:8789"});
  assert.deepEqual(otherOrigin.store.bind(identity()), expected());
  const beforeForeign = f.storage.calls.length;
  assert.deepEqual(f.store.bind(null), expected());
  f.store.update({accountFilter: "attention"});
  assert.equal(f.storage.calls.length, beforeForeign);
});
test("strict stored-payload validation rejects unknown keys, invalid enums and prototype pollution", () => {
  const f = fixture(); f.store.bind(identity()); const key = activeKey(f);
  const malformed = ["", "{", "null", "[]", "42", JSON.stringify({version: 2}),
    JSON.stringify({...expected(), accountFilter: "READY"}), JSON.stringify({...expected(), modelFilter: "active"}),
    JSON.stringify({...expected(), selectedTab: "logout"}), JSON.stringify({...expected(), favoritesOnly: 1}),
    JSON.stringify({...expected(), modelSort: {key: "next", dir: 0}}),
    JSON.stringify({...expected(), modelSort: {key: "next", dir: "-1"}}),
    JSON.stringify({...expected(), modelSort: {key: "next", dir: 1, secret: "x"}}),
    JSON.stringify({...expected(), modelSort: {key: "__proto__", dir: 1}}),
    JSON.stringify({...expected(), favorites: ["a", "person@example.test"]}),
    JSON.stringify({...expected(), favorites: ["x".repeat(257)]}),
    JSON.stringify({...expected(), favorites: Array.from({length: 101}, (_, i) => "m" + i)}),
    JSON.stringify({...expected(), favorites: ["constructor"]}),
    JSON.stringify({...expected(), accountQuery: "secret"}),
    '{"version":1,"__proto__":{"polluted":true}}',
    '{"version":1,"modelSort":{"key":"rank","dir":1,"__proto__":{"polluted":true}}}',
    " ".repeat(16385), '"' + "€".repeat(6000) + '"'];
  for (const raw of malformed) {
    f.storage.values.set(key, raw);
    const next = fixture({storage: f.storage});
    assert.deepEqual(next.store.bind(identity()), expected(), raw.slice(0, 90));
  }
  assert.equal({}.polluted, undefined);
  f.storage.values.set(key, JSON.stringify({version: 1, favorites: ["model", "model"]}));
  assert.deepEqual(fixture({storage: f.storage}).store.bind(identity()).favorites, ["model"]);
});
test("local updates ignore invalid fields/prototypes and cannot mutate store snapshots", () => {
  const f = fixture(); f.store.bind(identity());
  const inherited = Object.create({accountFilter: "ready"}); inherited.modelFilter = "limited";
  f.store.update(inherited);
  f.store.update(JSON.parse('{"__proto__":{"polluted":true},"key":"secret","accountFilter":"invalid","favoritesOnly":"true"}'));
  assert.deepEqual(f.store.get(), expected());
  f.store.update({modelSort: {key: "lane", dir: -1}, accountFilter: "cooling"});
  const snapshot = f.store.get(); snapshot.modelSort.key = "wrong"; snapshot.favorites.push("not-loaded");
  assert.deepEqual(f.store.get().modelSort, {key: "lane", dir: -1});
  assert.deepEqual(f.store.get().favorites, []);
  f.changes.at(-1)[0].modelSort.key = "wrong";
  assert.equal(f.store.get().modelSort.key, "lane");
  assert.equal({}.polluted, undefined);
});
test("new favorites require catalog membership; vanished and unavailable favorites survive", () => {
  const f = fixture(); f.store.bind(identity());
  f.store.update({favorites: ["not-loaded"]}); assert.deepEqual(f.store.get().favorites, []);
  f.store.setCatalog(["available", "unavailable", "vanishing", "space secret", "../bad@id", "constructor", "__proto__", "x".repeat(257)]);
  f.store.update({favorites: ["available", "unavailable", "vanishing", "available", "not-loaded", "constructor", "space secret"]});
  assert.deepEqual(f.store.get().favorites, ["available", "unavailable", "vanishing"]);
  const writes = f.storage.calls.length;
  f.store.setCatalog([]); assert.equal(f.storage.calls.length, writes);
  assert.deepEqual(f.store.get().favorites, ["available", "unavailable", "vanishing"]);
  f.store.update({favorites: ["unavailable", "vanishing"]});
  assert.deepEqual(f.store.get().favorites, ["unavailable", "vanishing"]);
  const next = fixture({storage: f.storage}); next.store.bind(identity()); next.store.setCatalog(["available"]);
  assert.deepEqual(next.store.get().favorites, ["unavailable", "vanishing"]);
  next.store.update({favorites: ["unavailable"]}); assert.deepEqual(next.store.get().favorites, ["unavailable"]);
});
test("favorite count, ID lengths and byte caps are bounded without silent unpinning", () => {
  const f = fixture(); f.store.bind(identity());
  const ids = Array.from({length: 100}, (_, i) => "m" + i); f.store.setCatalog(ids);
  f.store.update({favorites: ids}); assert.equal(f.store.get().favorites.length, 100);
  f.store.update({favorites: [...ids, "more"]}); assert.deepEqual(f.store.get().favorites, ids);
  const longIds = Array.from({length: 100}, (_, i) => "m" + String(i).padStart(3, "0") + "x".repeat(252));
  f.store.setCatalog(longIds); f.store.update({favorites: longIds});
  assert.deepEqual(f.store.get().favorites, ids);
  for (const call of f.storage.calls.filter(call => call[0] === "set")) assert.ok(Buffer.byteLength(call[2]) <= 16384);
});
test("storage exceptions fall back to RAM and reset never clears unrelated keys", () => {
  for (const throwingMethod of ["getItem", "setItem", "removeItem"]) {
    const storage = memoryStorage(), original = storage[throwingMethod];
    storage[throwingMethod] = function (...args) { original.apply(this, args); throw new Error("disabled storage"); };
    storage.values.set("credentials", "preserved");
    const f = fixture({storage});
    assert.doesNotThrow(() => f.store.bind(identity()));
    assert.equal(f.store.update({accountFilter: "attention"}).accountFilter, "attention");
    assert.equal(f.store.update({modelFilter: "limited"}).modelFilter, "limited");
    assert.deepEqual(f.store.reset(), expected());
    assert.equal(storage.values.get("credentials"), "preserved");
    const n = storage.calls.length; f.store.update({accountFilter: "ready"}); assert.equal(storage.calls.length, n);
  }
  const f = fixture({storage: {}}); f.store.bind(identity());
  assert.equal(f.store.update({accountFilter: "attention"}).accountFilter, "attention");
});
test("same-scope cross-tab events validate payload/origin/area and never echo writes", () => {
  const f = fixture(); f.store.bind(identity()); const key = activeKey(f), n = f.storage.calls.length;
  const remote = {...expected(), modelFilter: "unavailable", favorites: ["disappeared-model"], selectedTab: "guide"};
  assert.equal(f.store.handleStorage({key, newValue: JSON.stringify(remote), storageArea: f.storage, url: ORIGIN + "/dash"}), true);
  assert.deepEqual(f.store.get(), remote); assert.equal(f.storage.calls.length, n);
  for (const event of [{key: key + "foreign", newValue: null}, {key: null, newValue: null},
    {key, newValue: "malformed"}, {key, newValue: JSON.stringify({...remote, search: "secret"})},
    {key, newValue: null, storageArea: {}}, {key, newValue: null, url: "http://localhost:8789/dash"},
    {key, newValue: null, url: "not-a-url"}]) assert.equal(f.store.handleStorage(event), false);
  assert.deepEqual(f.store.get(), remote);
  assert.equal(f.store.handleStorage({key, newValue: null}), true);
  assert.deepEqual(f.store.get(), expected()); assert.equal(f.storage.calls.length, n);
  f.store.reset(); assert.deepEqual(f.storage.calls.at(-1), ["remove", key]);
  assert.equal(f.changes.at(-1)[1], "reset");
});
test("dispose stops callbacks, cross-tab handling and all further storage activity", () => {
  const f = fixture(); f.store.bind(identity()); const key = activeKey(f);
  f.store.dispose(); const calls = f.storage.calls.length, changes = f.changes.length;
  f.store.bind(identity()); f.store.update({accountFilter: "attention"}); f.store.reset(); f.store.setCatalog(["a"]);
  assert.equal(f.store.handleStorage({key, newValue: JSON.stringify(expected())}), false);
  assert.equal(f.storage.calls.length, calls); assert.equal(f.changes.length, changes);
  assert.deepEqual(f.store.get(), expected());
});

test("account categories use exact states, current warnings and the same counts predicates", () => {
  const now = 1700000000000;
  const rows = [
    {id: "healthy", state: "ready", last_error: "old failure", error_count: 50, error_history: [{code: "past"}]},
    {id: "cool", state: "cooling"}, {id: "dead", state: "dead"}, {id: "spent", state: "exhausted"},
    {id: "paid", state: "ready", paid_exhausted: true}, {id: "refresh", state: "ready", needs_refresh: true},
    {id: "expired", state: "ready", expires_at: now - 1}, {id: "flag", state: "ready", expired: true},
    {id: "remaining", state: "ready", expires_in_s: 0},
    {id: "cap-list", state: "cooling", capped_models: ["m"]},
    {id: "cap-info", state: "ready", model_caps: {m: {release_in_s: 10}}},
    {id: "cap-date", state: "ready", model_caps: {m: {release_at: now / 1000 + 1}}},
    {id: "old-cap", state: "ready", model_caps: {m: {release_in_s: 0, release_at: now / 1000 - 1}}},
    {id: "future", state: "ready", expires_at: now + 1, expires_in_s: 1},
    {id: "no-expiry", state: "ready", expires_at: 0}, {id: "unknown", state: "mystery"},
    {id: "case", state: "READY"}
  ];
  assert.deepEqual(P.accountCounts(rows, now), {all: 17, ready: 11, cooling: 2, attention: 10});
  for (const filter of ["all", "ready", "cooling", "attention"]) {
    assert.equal(P.accountCounts(rows, now)[filter], rows.filter(row => P.accountMatches(row, filter, "", now)).length);
  }
  assert.equal(P.accountMatches(rows[0], "attention", "", now), false);
  assert.equal(P.accountMatches(rows.at(-2), "all"), true);
  assert.equal(P.accountMatches(rows.at(-2), "ready"), false);
  assert.equal(P.accountMatches(rows[9], "cooling", "", now), true);
  assert.equal(P.accountMatches(rows[9], "attention", "", now), true);
  assert.deepEqual(P.accountCounts(null), {all: 0, ready: 0, cooling: 0, attention: 0});
  assert.equal(P.accountMatches(null), false);
});
test("account text search composes with category and does not inspect private/raw fields", () => {
  const a = {id: "account-one", email: "Example@Example.test", state: "cooling", source: "synthetic",
    capped_models: ["model-one"], notes: {secret: "private-marker"}, token_fingerprint: "fingerprint-marker", access_token: "token-marker"};
  assert.equal(P.accountMatches(a, "cooling", "EXAMPLE@"), true);
  assert.equal(P.accountMatches(a, "ready", "example@"), false);
  assert.equal(P.accountMatches(a, "attention", "model-one"), true);
  assert.equal(P.accountMatches(a, "all", "account-one"), true);
  for (const secret of ["private-marker", "fingerprint-marker", "token-marker"]) assert.equal(P.accountMatches(a, "all", secret), false);
});
test("model categories and favorites-only search use model.model not rendered text", () => {
  const rows = ["available", "partial", "cooling", "capped", "no_credit", "no_entitlement", "unavailable", "mystery"]
    .map((overall, i) => ({model: "model-" + i, overall, lane: "coding"}));
  assert.deepEqual(P.modelCounts(rows), {all: 8, available: 1, limited: 3, unavailable: 3});
  for (const filter of ["all", "available", "limited", "unavailable"]) {
    assert.equal(P.modelCounts(rows)[filter], rows.filter(row => P.modelMatches(row, filter)).length);
  }
  assert.equal(P.modelMatches(rows[6], "unavailable", "MODEL-6", true, ["model-6"]), true);
  assert.equal(P.modelMatches(rows[6], "available", "model-6", true, ["model-6"]), false);
  assert.equal(P.modelMatches(rows[6], "all", "model-6", true, ["other"]), false);
  assert.equal(P.modelMatches(rows[6], "all", "coding", true, new Set(["model-6"])), true);
  assert.equal(P.modelMatches(rows[7], "all"), true);
  assert.equal(P.modelMatches(rows[7], "unavailable"), false);
  assert.equal(P.modelMatches({model: "correct-id", name: "wrong-id", accounts: [{email: "private@email.test"}]}, "all", "correct-id"), true);
  assert.equal(P.modelMatches({model: "correct-id", name: "wrong-id"}, "all", "wrong-id"), false);
  assert.deepEqual(P.modelCounts(undefined), {all: 0, available: 0, limited: 0, unavailable: 0});
});
test("typed tuple comparator fixes 2/10, ratios and secondary numeric values", () => {
  assert.equal(P.compareTuple([2, "z"], [10, "a"]), -1);
  assert.equal(P.compareTuple([0.2, "z"], [0.9, "a"]), -1);
  assert.equal(P.compareTuple(["lane", 2], ["lane", 10]), -1);
  assert.equal(P.compareTuple([2, "a"], [2, "b"]), -1);
  assert.equal(P.compareTuple([2], [2]), 0);
});
test("sorts use numeric primary keys, stable ID ties and never mutate input", () => {
  const rows = [
    {model: "z", overall: "available", lane: "b", available_on: 2, total_accounts: 10, next_release_in_s: 10},
    {model: "a", overall: "capped", lane: "a", available_on: 9, total_accounts: 10, next_release_in_s: 2},
    {model: "b", overall: "mystery", lane: "a", available_on: 4, total_accounts: 0, next_release_in_s: null},
    {model: "c", overall: "available", lane: "a", available_on: 2, total_accounts: 10, next_release_in_s: 10}
  ];
  const snapshot = JSON.stringify(rows), ids = sorted => sorted.map(row => row.model);
  assert.deepEqual(ids(P.sortModels(rows, {key: "next", dir: 1})), ["a", "c", "z", "b"]);
  assert.deepEqual(ids(P.sortModels(rows, {key: "next", dir: -1})), ["b", "c", "z", "a"]);
  assert.deepEqual(ids(P.sortModels(rows, {key: "ratio", dir: 1})), ["b", "c", "z", "a"]);
  assert.deepEqual(ids(P.sortModels(rows, {key: "ratio", dir: -1})), ["a", "c", "z", "b"]);
  assert.deepEqual(ids(P.sortModels(rows, {key: "rank", dir: 1})), ["c", "z", "a", "b"]);
  assert.deepEqual(ids(P.sortModels(rows, {key: "model", dir: -1})), ["z", "c", "b", "a"]);
  assert.deepEqual(ids(P.sortModels(rows, {key: "lane", dir: 1})), ["a", "b", "c", "z"]);
  assert.deepEqual(ids(P.sortModels(rows, {key: "next", dir: 1}, ["b", "z"])), ["z", "b", "a", "c"]);
  assert.deepEqual(ids(P.sortModels(rows, {key: "next", dir: -1}, new Set(["b", "z"]))), ["b", "z", "c", "a"]);
  assert.deepEqual(ids(P.sortModels(rows, {key: "__proto__", dir: 1})), ["c", "z", "a", "b"]);
  assert.equal(JSON.stringify(rows), snapshot);
  assert.deepEqual(P.sortModels(null), []);
  assert.equal(P.sortModels([{model: "constructor", overall: "constructor"}])[0].model, "constructor");
});
process.stdout.write("Passed " + tests + " UI preference tests.\n");
