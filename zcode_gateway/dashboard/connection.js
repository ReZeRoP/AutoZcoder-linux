/* A single connection/poll owner. No account work, queued writes, or service start. */
(function (root, factory) {
  const exported = factory();
  if (typeof module === "object" && module.exports) module.exports = exported;
  else root.GatewayConnection = exported;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const RETRY = [1000, 2000, 4000, 8000, 15000];
  function validInstance(value, expectedConfig) {
    return !!value && value.schema_version === 1 && value.product === "autozcoder-linux"
      && typeof value.version === "string" && value.version.length < 80
      && typeof value.instance_id === "string" && value.instance_id.length > 0 && value.instance_id.length < 160
      && typeof value.config_id === "string" && value.config_id.length > 0 && value.config_id.length < 160
      && (!expectedConfig || value.config_id === expectedConfig)
      && ["user-service", "foreground", "embedded"].includes(value.mode)
      && typeof value.stopping === "boolean";
  }
  function parseBootstrap(html) {
    const match = String(html).match(/<script id="autozcoder-bootstrap" type="application\/json">([\s\S]*?)<\/script>/);
    if (!match || match[1].length > 8192) throw new Error("No trusted dashboard credentials");
    const data = JSON.parse(match[1]);
    if (typeof data.key !== "string" || data.key.length > 4096 || !validInstance(data.instance))
      throw new Error("Invalid dashboard credentials");
    return data;
  }
  class Supervisor {
    constructor(options = {}) {
      this.fetch = options.fetch || ((...args) => fetch(...args));
      this.clock = options.clock || {setTimeout: (...a) => setTimeout(...a), clearTimeout: id => clearTimeout(id)};
      this.timeout = options.timeout || 5000;
      this.onState = options.onState || (() => {});
      this.onOnline = options.onOnline || (() => {});
      this.onCredentials = options.onCredentials || (() => {});
      this.key = options.key || "";
      this.instance = validInstance(options.instance) ? options.instance : null;
      this.configId = this.instance && this.instance.config_id;
      this.expectedMode = this.instance && this.instance.mode;
      this.state = "offline";
      this.running = false;
      this.connected = false;
      this.intentionalStop = false;
      this.retryIndex = 0;
      this.healthTimer = null;
      this.healthFlight = null;
      this.requests = new Set();
      this.reads = new Map();
      this.polls = new Map();
      this.generation = 0;
      this.attemptedInstance = null;
    }
    _state(state, detail) {
      const changed = state !== this.state;
      this.state = state;
      this.connected = state === "online" || state === "degraded";
      if (!this.connected) this._pausePolls();
      if (changed || detail) this.onState(state, detail);
    }
    _cancelRequests() {
      this.generation++;
      for (const item of this.requests) item.cancel();
      this.reads.clear();
    }
    async _fetch(path, options = {}, text = false) {
      // Client-only option: never leak it to fetch or change the global deadline.
      const {timeoutMs, ...fetchOptions} = options;
      const timeout = timeoutMs === undefined ? this.timeout : timeoutMs;
      const controller = new AbortController();
      let timer, cancel;
      const interrupted = new Promise((_, reject) => {
        cancel = () => { controller.abort(); reject(new Error("Request cancelled")); };
        timer = this.clock.setTimeout(() => { controller.abort(); reject(new Error("Gateway request timed out")); }, timeout);
      });
      const tracked = {cancel};
      this.requests.add(tracked);
      try {
        return await Promise.race([interrupted, (async () => {
          const response = await this.fetch(path, {...fetchOptions, signal: controller.signal,
            cache: "no-store", redirect: "error"});
          if (response.status === 401 || response.status === 403) {
            const error = new Error("Enter the current admin key"); error.unauthorized = true; throw error;
          }
          if (!response.ok) {
            const error = new Error("Gateway request failed (" + response.status + ")");
            error.status = response.status;
            try {
              const body = await response.json();
              if (typeof body.detail === "string") error.message = body.detail.slice(0,1024);
              if (typeof body.code === "string") error.code = body.code.slice(0,120);
              if (typeof body.active_id === "string" && body.active_id.length < 250) error.active_id = body.active_id;
            } catch (_) {}
            throw error;
          }
          const body = text ? await response.text() : await response.json();
          if (text && body.length > 2 * 1024 * 1024) throw new Error("Dashboard response too large");
          return body;
        })()]);
      } finally {
        this.clock.clearTimeout(timer);
        this.requests.delete(tracked);
      }
    }
    _headers() { return {Authorization: "Bearer " + this.key, "Content-Type": "application/json"}; }
    start() {
      if (this.running) return;
      this.running = true;
      this.retryIndex = 0;
      this._schedule(0);
    }
    stop() {
      this.running = false;
      this.clock.clearTimeout(this.healthTimer); this.healthTimer = null;
      this._state("offline"); this._cancelRequests();
    }
    retry(key) {
      if (typeof key === "string") this.key = key;
      this.attemptedInstance = null;
      this.retryIndex = 0;
      if (!this.running) this.running = true;
      this.clock.clearTimeout(this.healthTimer); this.healthTimer = null;
      // If a probe is in flight it schedules the next probe; never overlap it.
      if (!this.healthFlight) this._schedule(0);
    }
    setStopped() {
      this.intentionalStop = true;
      this._state("stopped"); this._cancelRequests();
      this.clock.clearTimeout(this.healthTimer); this.healthTimer = null;
      if (!this.healthFlight) this._schedule(1000);
    }
    offline(error) {
      this._state(this.intentionalStop ? "stopped" : "offline", error && error.message);
      this._cancelRequests();
      if (this.running && !this.healthFlight && this.healthTimer === null) {
        this._schedule(RETRY[this.retryIndex]);
        this.retryIndex = Math.min(this.retryIndex + 1, RETRY.length - 1);
      }
    }
    _schedule(delay) {
      if (!this.running || this.healthTimer !== null) return;
      this.healthTimer = this.clock.setTimeout(() => {
        this.healthTimer = null;
        this._tick();
      }, delay);
    }
    async _tick() {
      if (!this.running || this.healthFlight) return;
      this.healthFlight = this._probe();
      try { await this.healthFlight; } finally {
        this.healthFlight = null;
        if (!this.running || this.state === "unauthorized" || this.state === "foreign") return;
        if (this.connected) this._schedule(10000);
        else {
          this._schedule(RETRY[this.retryIndex]);
          this.retryIndex = Math.min(this.retryIndex + 1, RETRY.length - 1);
        }
      }
    }
    async _probe() {
      try {
        const instance = await this._fetch("/health/instance");
        if (!validInstance(instance, this.configId) || (this.expectedMode && instance.mode !== this.expectedMode)) {
          this._state("foreign", "Another listener owns this port. No connection was adopted.");
          this._cancelRequests(); return;
        }
        if (!this.configId) this.configId = instance.config_id;
        if (!this.expectedMode) this.expectedMode = instance.mode;
        if (instance.stopping) { this.offline(new Error("Service is stopping")); return; }
        const changed = !this.instance || this.instance.instance_id !== instance.instance_id;
        if (changed) {
          this._state("offline", "Verifying the new gateway instance…");
          this._cancelRequests(); // no tab/account reads or mutations during reauthentication
          // One reauthentication attempt per boot. Read data only; never execute fetched HTML.
          if (this.attemptedInstance === instance.instance_id) {
            this._state("unauthorized", "Enter the current admin key"); return;
          }
          this.attemptedInstance = instance.instance_id;
          const bootstrap = parseBootstrap(await this._fetch("/dash", {}, true));
          if (bootstrap.instance.instance_id !== instance.instance_id || bootstrap.instance.config_id !== this.configId
              || bootstrap.instance.mode !== this.expectedMode)
            throw new Error("Gateway changed during authentication");
          this.key = bootstrap.key;
          this.onCredentials(this.key, bootstrap.instance);
        }
        if (!this.connected || changed) {
          const authenticated = await this._fetch("/admin/dash/lifecycle", {headers: this._headers()});
          if (!validInstance(authenticated, this.configId) || authenticated.instance_id !== instance.instance_id
              || authenticated.mode !== this.expectedMode)
            throw new Error("Dashboard identity did not match");
        }
        const resumed = !this.connected || changed;
        if (changed) this._cancelRequests();
        this.instance = instance;
        this.intentionalStop = false;
        this.retryIndex = 0;
        const health = await this._fetch("/health").catch(error => {
          if (error.status === 503) return {status: "degraded"};
          throw error;
        });
        this._state(health.status === "degraded" || health.status === "error" ? "degraded" : "online");
        if (resumed) {
          await this.onOnline(instance, changed);
          this._resumePolls();
        }
      } catch (error) {
        if (!this.running) return;
        if (error.unauthorized) {
          this._state("unauthorized", error.message); this._cancelRequests();
        } else {
          this.attemptedInstance = null; // transient failures may retry reads, never writes
          this.offline(error);
        }
      }
    }
    request(path, options = {}) {
      if (options.timeoutMs !== undefined && (typeof options.timeoutMs !== "number"
          || !Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0 || options.timeoutMs > 25000))
        return Promise.reject(new Error("timeoutMs must be a finite number between 0 (exclusive) and 25000"));
      if (!this.connected) return Promise.reject(new Error("Gateway disconnected; nothing was sent. Reconnect and try again."));
      const method = (options.method || "GET").toUpperCase();
      const read = method === "GET";
      if (read && this.reads.has(path)) return this.reads.get(path);
      const generation = this.generation;
      const operation = this._fetch(path, {...options,
        headers: {...this._headers(), ...(options.headers || {})},
        body: options.body && typeof options.body !== "string" ? JSON.stringify(options.body) : options.body})
        .then(data => {
          if (generation !== this.generation || !this.connected) throw new Error("Connection changed; response discarded");
          return data;
        }).catch(error => {
          if (generation === this.generation) {
            if (error.unauthorized) {
              this._state("unauthorized", error.message); this._cancelRequests();
              this.clock.clearTimeout(this.healthTimer); this.healthTimer = null;
            } else if (!error.status || error.status === 503) this.offline(error);
          }
          throw error; // Mutations are never retried, queued, or replayed.
        }).finally(() => { if (this.reads.get(path) === operation) this.reads.delete(path); });
      if (read) this.reads.set(path, operation);
      return operation;
    }
    poll(name, run, interval, immediate = false) {
      const existing = this.polls.get(name);
      if (existing && existing.run === run && existing.interval === interval) return;
      if (existing && existing.flight) {
        existing.run = run; existing.interval = interval;
        return; // one logical owner even when its cadence changes mid-request
      }
      if (existing) { existing.cancelled = true; this.clock.clearTimeout(existing.timer); }
      const owner = {run, interval, timer: null, flight: false, cancelled: false};
      this.polls.set(name, owner);
      if (this.connected) this._poll(name, owner, immediate ? 0 : interval);
    }
    unpoll(name) {
      const owner = this.polls.get(name);
      if (!owner) return;
      owner.cancelled = true; this.clock.clearTimeout(owner.timer); this.polls.delete(name);
    }
    _poll(name, owner, delay) {
      if (!this.running || !this.connected || owner.cancelled || owner.flight || owner.timer !== null) return;
      owner.timer = this.clock.setTimeout(async () => {
        owner.timer = null;
        if (!this.connected || owner.cancelled) return;
        owner.flight = true;
        try { await owner.run(); } catch (_) {} finally {
          owner.flight = false;
          if (this.polls.get(name) === owner) this._poll(name, owner, owner.interval);
        }
      }, delay);
    }
    _pausePolls() {
      for (const owner of this.polls.values()) { this.clock.clearTimeout(owner.timer); owner.timer = null; }
    }
    _resumePolls() {
      for (const [name, owner] of this.polls) this._poll(name, owner, owner.interval);
    }
  }
  return {Supervisor, validInstance, parseBootstrap, RETRY};
});
