# AutoZcoder Linux — agent development instructions

## Start here
- Reply in English. Give concise progress updates and report failed/skipped verification honestly.
- Read `README.md` and the relevant docs before editing. If local `CODEX_HANDOFF.md` exists, read it for current work and evidence; it is historical context, not permission to execute pending operations.
- Inspect `git status --short` and current branch first. This checkout may contain substantial uncommitted Linux-port work. Preserve it; do not reset, clean, stash, switch branches, or stage everything indiscriminately. Commit/push/publish only when requested.
- Make focused changes in source files, matching existing Python/JavaScript style. Do not edit generated `build/` copies or installed application files to implement a fix.

## Project map
Independent Fedora 44 x86_64 desktop fork of AutoZcoder. Application version and RPM Release are distinct. The Linux gateway uses Python 3.14 and GTK3/WebKit4.1; optional bundled tooling uses private Python 3.12. The bundle includes non-MIT vendored licenses; see `THIRD_PARTY_NOTICES.md`.

- `zcode_gateway/linux_cli.py`: Linux CLI entrypoint; `packaging/fedora/autozcoder` dispatches it.
- `linux_service.py`, `linux_control.py`, `linux_config.py`, `linux_desktop.py` under `zcode_gateway/`: service lifecycle, safe diagnostics, path discovery, GTK dashboard.
- `zcode_gateway/app.py`, `state.py`, `api_*.py`: application factory, state and API surfaces.
- `zcode_gateway/service.py`, `stream_delivery.py`: request ownership, verified streaming, errors, ledger recording.
- `zcode_gateway/upstream.py`, `relay_manager.py`, `relay_deploy.py`, `relay_jobs.py`: transports, provenance classification, relay settings/probes and deployment ownership.
- `zcode_gateway/dashboard/`: HTML/CSS/JavaScript UI.
- `dashboard/index.html` is the fork's Studio at `/dash`; `dashboard/v2.html`
  and modular assets serve the upstream V2 Workspace at `/dash/v2`.
- `auto_generate.py`, generator session/claim controllers and `peer.py`:
  v2 automation ownership, recovery and proxy-corrected admin peer checks.
- `tests/`: Python tests and Node `.cjs` tests; `pytest.ini` defines discovery/async behavior.
- `packaging/fedora/`: locks, staging, RPM build and packaging tests.
- `generator/`, `mail-worker/`, `tools/`: retained optional upstream tools, not authorized live test inputs.

Relevant contracts: `docs/LINUX-SERVICE.md`, `docs/VERIFIED-DELIVERY.md`, `docs/PROXY-RELAY-RELIABILITY.md`, `docs/LINUX-TESTING.md`. Some docs contain historical installed-release assertions; recheck actual state rather than treating them as current.
V2 integration choices are in `docs/V2-MERGE.md`; the retained upstream release
workflow under `docs/reference/` is a test/reference fixture, not an active
GitHub workflow. The active release gate remains Fedora/Linux-specific.

## Production and privacy boundaries
- Installed user gateway is separate from the checkout: `/usr/share/autozcoder` source, `/usr/lib64/autozcoder` private runtime, user `autozcoder.service` on port 8789. The unrelated system `zcode-gateway.service` on 8788 must remain untouched.
- Editing/building source does not update a running service. RPMs have no install-time service restart. Opening `autozcoder` can start the user service; bare legacy launches can bootstrap state. Do not use launches as harmless inspections.
- Ask before installing/downgrading packages, starting/stopping/restarting services, changing production settings/routes/timeouts/accounts/keys, enabling capture, or destructive actions. Check active requests before an approved restart; never interrupt them silently.
- Inference consumes quota and publishes prompt data to the configured provider. Ask separately before a live request; approval for a previous request/rollout does not authorize another.
- No live signup/account generation, CAPTCHA/token minting, claims/rewards, account refresh, external proxy scanning, cloud/mail deployment, or relay redeployment during ordinary verification. Do not interpret bundled tools as authorization. Follow service permission and lawful access requirements.
- Do not dump or publish config, account files, tokens, database contents, raw journals/headers/bodies, relay URLs, browser profiles or backups. Use synthetic fixtures and bounded projections. Keep `.gitignore` exclusions and source-stage privacy filtering intact.
- Unknown relay 429 provenance is not a provider quota diagnosis. Preserve header precedence, strict known-code classification, terminal ambiguity, account/relay policy, send/read/cleanup budgets and resource ownership. A green forwarding probe does not certify inference/SSE health.

## Development and verification
Use an existing Python environment with `requirements-linux.txt` and `requirements-dev.txt`. Local interpreter paths and verified results are in `CODEX_HANDOFF.md` when available. Do not automatically install dependencies or bootstrap downloads.

From the repository root, using that environment:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -rs -p no:cacheprovider
```

```bash
node --test tests/test_*.cjs
```

```bash
PYTHONDONTWRITEBYTECODE=1 python packaging/fedora/test-packaging.py
```

```bash
git diff --check
```

Run focused tests first, then relevant complete suites. Tests must use temporary state, fake accounts and MockTransport/fake clients; `tests/conftest.py` blocks selected hosts, not every possible network call. Keep test/preview requests on isolated fixture servers, not production port 8789. Use temporary HOME/XDG roots for runtime previews and disable production loops/updaters. Never load personal accounts into fixtures.

Packaging: `bootstrap.sh` can download dependencies; `build-rpm.sh` uses prepared caches offline, stages eligible tracked AND untracked sources, builds locally and writes manifests/checksums under `build/fedora/release/`. A build can overwrite shared stage/manifest/checksum files: preserve previous release metadata and rollback RPM before an approved build. Verify version, digests, inert scriptlets, private-data exclusions, packaged source hashes and symlinks; do not equate a successful build with installation or live success.

At completion, state exactly what changed, tests/proof, what remains unverified, and whether anything was built, installed, restarted, committed or published. Keep current operational status in the ignored local handoff; portable instructions belong here.
