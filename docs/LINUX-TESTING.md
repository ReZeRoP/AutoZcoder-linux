# Linux 1.3.0 verification

## Target and baseline

Fedora44 x86_64; desktop Python3.14, private optional runtime Python3.12.14.
This Linux feature release is based on upstream AutoZcoder v1.2.2 commit
`ba3fff0633aea126c1856e3e2485e84743dac5ea`. Release6(1.3.0-6) is installed locally
with verified delivery on8789. Release7 development does not replace it or modify
private settings/service state without a separately approved rollout.

## Release9 relay diagnostics candidate

Release9extends established provider quota-envelope compatibility and adds
bounded, privacy-safe opening diagnostics and ledger failure records. All new
verification is offline/synthetic. The installed release8and its production
routes/settings/accounts are unchanged; release9installation is separately
approved. Evidence is in `build/fedora/verification-1.3.0-9/`.

## Release8 proxy/relay candidate

Release8adds transactional relay observations, bounded deployment jobs, verified
forwarding probes and proxy/dashboard control fixes. Verification is entirely
synthetic/offline; no provider inference, proxy scan or cloud deployment is part
of this candidate. See `PROXY-RELAY-RELIABILITY.md` for the guarantees and limits.
Local evidence is in `build/fedora/verification-1.3.0-8/`. Release7remains installed
until a separate rollout is approved.

## Release7 verification

The recovered release7 suites passed794cases on Python3.14(one legacy Tk
module skipped),802on Python3.12,54Node entries and16packaging cases.
Timing/budget tests and browser-driven synthetic SSE verify prompt keepalives,
private semantic output and one successful terminal. Review-exposed pending-send
races, buffering middleware boundaries, environment validation and cooperative
relay-close cancellation were fixed and covered. Six additional regressions
cover cancellation, disconnect and total deadlines during pre-transfer relay
close, with cooperative and cancellation-resistant cleanup. All six reproduced
the lost timeout before the fix and pass with an owned, shielded timeout waiter.
Candidate packaging/live checks are recorded separately; no release7 installation
is claimed by these results.

## Historical Linux1.3.0 checks

- **576 Python tests passed**; one legacy Tk module was skipped on system Python.
  That module was run separately under private Python3.12: **8 passed**.
- **12 JavaScript tests passed** with fake time/fetch and executable autosave
  checks: reconnect retry/timeouts, single poll owners, stale-key recovery,
  ownership-mode changes, no queued/replayed writes, selected-tab preservation.
- **16 packaging tests passed**, including source privacy, permissions/cache
  validation and disabled user-unit/scriptlet policy.
- Source Python and inline dashboard JavaScript syntax, shell syntax,
  desktop entry and user-unit syntax validated.
- Bounded SQLite shutdown tested with a held worker lock and overridden close hook.
- Startup rollback, signal ownership, permanent failure codes, stopping fences,
  safe identity/auth checks and environment-isolated diagnostics tested.

The main suite emits one upstream Starlette/httpx TestClient deprecation warning.

## Real isolated systemd integration

A rootless Fedora44 container with a disposable user and real systemd user manager
ran the source gateway with empty synthetic state. Container-only D-Bus aliases
and runtime environment were configured because the minimal image lacked the
normal PAM/user-session setup. No host user-unit or production account was changed.

The integration test passed:

- explicit start and local healthy status;
- restart with a different boot identity and unchanged configuration;
- crash recovery with restart count increasing;
- enabling/disabling login autostart without changing the running PID;
- enabling/disabling while stopped without starting the service;
- intentional stop remaining stopped;
- authenticated dashboard shutdown exiting zero without crash restart;
- byte-for-byte configuration preservation.

A separate Fedora container source-fixture check passed all16 packaging tests,
`rpmspec`, desktop validation and `systemd-analyze --user verify`.
An initial attempt to copy build caches into container `/tmp` exhausted that
tmpfs; it was removed and the check reran successfully using only small source
fixtures. This was a test-environment failure, not a package runtime failure.

## Not verified / limitations

- Native GTK rendering, dashboard interaction screenshots and real window-close/
  reopen behavior remain unverified: the host preview-tool safety service was
  unavailable. Unit/fake-DOM tests are not substitutes for graphical proof.
- Actual login/logout/autostart across a real desktop login was not exercised.
  Service lifetime follows the user manager; no lingering is enabled.
- Source CI changes have not been pushed; these are local results, not remote CI.
- No live inference, signup, CAPTCHA solving, rewards/claims, external proxy scans
  or cloud deployment was performed. The CAPTCHA minter is not ported/built.
- Legacy helper threads may remain inside noncooperative synchronous I/O until it
  returns. Shutdown fences further work and reports a deadline warning; systemd
  cgroup/process containment is the backstop, not claimed thread cancellation.
- Windows/non-Fedora distributions are not targets of this Linux release.

## Reproduce

From a development virtual environment with Linux/dev requirements installed:

```bash
python -m pytest -q -rs
```

```bash
node --test tests/test_dashboard_connection.cjs
```

```bash
python packaging/fedora/test-packaging.py
```

Tests use synthetic state. Never substitute personal credential/account files into
fixtures. See `LINUX-SERVICE.md` for lifecycle and migration semantics.
