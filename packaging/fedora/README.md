# Fedora 44 build and packaging

AutoZcoder Linux **1.2.2, release 1** is an independent desktop fork. The target
is **Fedora 44 / x86_64 / system Python 3.14**; other Fedora releases, Python ABIs
and distributions are not claimed to work. Main application version detection
parses `zcode_gateway/__init__.py` with `ast`, without importing the app. The RPM
spec must match that version. RPM release numbering is separate.

## Prerequisites

Install native build/runtime prerequisites as an administrator yourself. The
bootstrap/build scripts never invoke sudo or change system packages:

```bash
sudo dnf install rpm-build desktop-file-utils git tar gzip nodejs npm \
  python3 python3-gobject python3-cairo gtk3 webkit2gtk4.1
```

Also install [uv](https://docs.astral.sh/uv/). `UV=/path/to/uv` overrides its PATH
lookup (then `~/.local/bin/uv` fallback). Node **22 or newer** is required. Native
libraries needed by private Chromium/OCR/CPython are listed in
`autozcoder.spec`; the RPM package manager resolves them when installing the app.
For a first build, the same libraries must exist on the build host for `%check`;
use `rpmspec -q --requires packaging/fedora/autozcoder.spec` to inspect the list.
`patchelf` may be installed through Fedora or bootstrapped into
`build/fedora/build-tools/` at the pinned version **0.17.2.4**.

Allow several GB for caches, staged payload, RPM buildroot and compressed
artifacts (roughly 6–10 GB; disk and download sizes vary). No Rust compiler,
Bun installation, PyInstaller, Windows SDK, or CAPTCHA-minter build is needed.

## Bootstrap pinned assets

Run from the checkout root; scripts resolve paths relative to themselves and
also work when invoked with absolute paths from another directory:

```bash
bash packaging/fedora/bootstrap.sh
```

Bootstrap prepares only the following build-local assets:

| Directory | Input / role |
|---|---|
| `build/fedora/python/` | System Python 3.14 desktop `requirements.lock` |
| `build/fedora/generator-packages/` | Private Python 3.12 `generator-requirements.lock` |
| `build/fedora/browsers/` | Playwright 1.64.0: Chromium 1248, FFmpeg 1013 |
| `build/fedora/node-tools/` | Committed npm integrity lock: Wrangler 4.151.0 |
| `build/fedora/build-tools/` | Build-local patchelf if none selected/installed |
| `build/fedora/python-install/` | CPython 3.12.14 if no managed copy is available |

An existing uv-managed CPython **3.12.14** can be reused read-only. Override it
with `AUTOZCODER_PYTHON_ROOT=/path/to/cpython-3.12.14-linux-x86_64-gnu` (the
prefix directory, not the executable). Its path is saved in ignored
`build/fedora/python-root.txt` for subsequent builds. Bootstrap uses `uv pip
sync --link-mode copy` to match exact package versions and avoid mutating uv's
cache. GTK/PyGObject/Cairo are deliberately excluded from the pip locks.

The full Chromium download is installed with Playwright's `--no-shell`; no
separate headless-shell is required. The app's Linux runtime selects the full
Chromium executable for both headless and visible launches. Bootstrap does
**not** run any browser/account workflow or deploy a worker.

### Reuse a previous build cache safely

This saves browser/Node/Python downloads, but still validates the pins. Supply
the old cache directory, not a writable staging directory:

```bash
AUTOZCODER_CACHE_FROM=/path/to/previous-checkout/build/fedora \
AUTOZCODER_PYTHON_ROOT=/path/to/cpython-3.12.14-linux-x86_64-gnu \
  bash packaging/fedora/bootstrap.sh
```

Only missing `python`, `generator-packages`, `browsers`, and `node-tools`
directories are copied. Copies use `cp -a --reflink=auto`: independent files
with copy-on-write where supported, **never hardlinks or symlinks to an old
checkout**. Do not point the new build directory at the old one. Source code,
config, accounts, databases, logs, browser profiles and old verification reports
are not cache inputs. Internal dependency symlinks are allowed only when they
resolve within the final staged payload.

The dependency locks are intentionally retained from the working Linux port.
There is no implicit `latest` update. `npm ci` uses the committed `package-lock`
integrities. Python packages are exact version pins, not hash-pinned wheels;
Playwright pins browser revisions but downloads them from its upstream CDN;
uv selects a build of the pinned CPython version. Keep trusted caches/toolchain
versions for repeatable releases. This does **not** promise identical binaries
across different upstream mirrors, OS toolchains, or uv runtime rebuilds.

## Offline build and output

Resolve any merge conflicts and stop concurrent source edits before staging.
After bootstrap:

```bash
bash packaging/fedora/build-rpm.sh
```

This step never installs Python/npm packages or fetches browsers. It verifies
cached distributions and version metadata, stages a source allowlist from git
(including nonignored, untracked Linux files), excludes private state/Windows
binaries, and rejects merge markers. An optional `PATCHELF=/path/to/patchelf`
selects a build-local executable. `SYSTEM_PYTHON` defaults to `/usr/bin/python3`
and must remain Python 3.14.

Build locations:

- `build/fedora/autozcoder-1.2.2-fedora/`: staged runtime, original dependency
  license files, collected `BUNDLED-LICENSES.txt`, `SOURCE-FILES.txt` and
  `RUNTIME-MANIFEST.json` (SHA-256/size or symlink target per staged entry).
- `build/fedora/rpmbuild/`: rpmbuild sources, buildroot and binary RPM output.
- `build/fedora/release/`: `autozcoder-1.2.2-1.fc44.x86_64.rpm`, a copy of the
  runtime manifest and `SHA256SUMS`, suitable for manual release publication.

Tar input ordering, UID/GID, timestamps and gzip headers are normalized using
`SOURCE_DATE_EPOCH` (defaults to the latest commit's timestamp). Set that variable
to a fixed release epoch to reproduce archive timestamps. This improves build
repeatability, but is not a claim of cross-host bit-for-bit RPM reproducibility.
The manifest describes the staged input; RPM scripts can change buildroot
metadata. It is not a post-install filesystem attestation.

## Offline checks

```bash
python3 packaging/fedora/test-packaging.py
bash -n packaging/fedora/bootstrap.sh packaging/fedora/build-rpm.sh
desktop-file-validate packaging/fedora/autozcoder.desktop
rpmspec -q --srpm packaging/fedora/autozcoder.spec
(cd build/fedora/release && sha256sum -c SHA256SUMS)
```

`%check` validates the desktop entry, imports the private Python
modules, and runs pinned Wrangler's `--version` with metrics disabled. It does
not exercise accounts, captcha/token solving, rewards, proxy scans or cloud APIs.
The main project's offline test suite is separate; a successful RPM build is
not evidence that every GUI or remote workflow works.

Preview helpers (`preview.py` for source, `preview-complete.py` after staging)
use isolated HOME/XDG folders below `build/`, detect the current version, and
use port 8790 rather than the installed app's 8789. They open the real GTK app;
do not click external operations in previews. They do not import real accounts.

## Licensing and distribution

The root application license is MIT. Vendored solver sources under
`tools/captcha-mint/` are **CC BY-NC-SA 4.0**, and the private binary dependencies
carry further licenses. Full solver text and attribution are included as license
documents. See `THIRD_PARTY_NOTICES.md`; the bundle is not MIT-only. Upstream
Windows CAPTCHA minter executables are excluded and no Linux minter is built.
Source retention is not functionality support.

This is a locally built application RPM, not a Fedora repository submission.
The noncommercial source license and bundled third-party components need review
before redistribution, especially commercial redistribution. CI validates Linux
source/tests and Linux release tags; RPM building and publication are **manual**.
