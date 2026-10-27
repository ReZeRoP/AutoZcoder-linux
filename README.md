# AutoZcoder Linux 1.2.2

An **unofficial, independent Linux desktop fork** of
[B3hnamR/AutoZcoder](https://github.com/B3hnamR/AutoZcoder), a local gateway for
the ZCode coding-plan API with OpenAI- and Anthropic-compatible endpoints.
This fork is not affiliated with ZCode, z.ai, or the Fedora Project.

The Linux release targets **Fedora 44 x86_64** and provides a GTK3/WebKit4.1
desktop window, an isolated local gateway, and XDG-based configuration/state.
Upstream's original Windows-oriented documentation is preserved unchanged at
[docs/UPSTREAM-README.md](docs/UPSTREAM-README.md).

## Install and launch

Release artifacts are intended for
[ReZeRoP/AutoZcoder-linux releases](https://github.com/ReZeRoP/AutoZcoder-linux/releases).
The RPM is built locally; CI runs Linux tests/release validation, **not an
automated Fedora RPM build or publication**. A release may not yet be published.
After downloading an RPM and its `SHA256SUMS` from a trusted release:

```bash
sha256sum --ignore-missing -c SHA256SUMS
sudo dnf install ./autozcoder-1.2.2-1.fc44.x86_64.rpm
autozcoder
```

You can also launch **AutoZcoder Linux** from the application menu. It needs a
working graphical GTK/WebKit session. Fedora installs native runtime libraries
and Node.js/npm as package dependencies. No Python packages or browsers are
downloaded at RPM installation or normal startup.

The desktop's default API is `http://127.0.0.1:8789/v1` and its dashboard is
`http://127.0.0.1:8789/dash`. Use the client key from your own Server settings;
no shared API keys or accounts are distributed. Keep the desktop open while
using its gateway. The Linux desktop does not replace a separate gateway on
port 8788 or start/modify an existing systemd service.

| Location | Purpose |
|---|---|
| `~/.config/autozcoder/config.yaml` | Per-user configuration and local API keys |
| `~/.local/share/autozcoder/` | Accounts, database and other writable state |
| `~/.cache/autozcoder/` | Desktop/browser cache |
| `/usr/share/autozcoder/` | Read-only application source, templates and resources |
| `/usr/lib64/autozcoder/` | Private Python libraries, optional runtime and tools |

Respect the confidentiality of the XDG directories and any exported backups.
The source checkout and public RPM do not contain private `.zcode` credentials,
accounts, config, database, logs, browser profiles, or personal migration reports.
Upgrade with a new RPM; upstream Windows executable self-update is disabled in
managed Linux mode. Uninstalling the RPM does not remove your per-user data.

## Runtime support and limits

- The desktop/gateway uses **Fedora Python 3.14** plus the locked Linux packages.
  GTK, PyGObject, Cairo and WebKit come from Fedora, not pip.
- Ordinary upstream gateway/generator/proxy helpers and the mail-worker template
  are retained. Optional modules have a private **Python 3.12.14** environment,
  Playwright **1.64.0**, full Chromium revision **1248**, FFmpeg revision **1013**,
  and Wrangler **4.151.0**. The separate Chromium headless-shell is not bundled;
  the configured runtime uses the full browser for visible and headless modes.
- The newer upstream **CAPTCHA token minter is not ported or built for Linux**.
  Its TypeScript sources and original license are retained for provenance, but
  no Windows `.exe`, Bun runtime, or Linux minter binary is shipped. Features
  that require that sidecar are unavailable out of the box. Bundling optional
  dependencies is not a claim of full upstream automation parity.
- No live account generation, CAPTCHA/token minting, rewards/invite claims,
  third-party proxy scans, or cloud deployment is part of this release's build
  verification. These workflows require appropriate service permission and
  external configuration; do not infer service-side approval from local tests.
- Existing upstream optional actions are not started by the packaging scripts.
  Any cloud/mail tooling needs your own domain and service credentials. Linux
  managed mode does not install Windows runtimes or modify system Python.

## Build from source

See [packaging/fedora/README.md](packaging/fedora/README.md) for prerequisites,
cache reuse, exact dependency locks, offline build steps and verification.
The main flow is:

```bash
bash packaging/fedora/bootstrap.sh
bash packaging/fedora/build-rpm.sh
```

Bootstrap may download the pinned assets into `build/fedora/`; the RPM build
uses those caches without dependency downloads. Output is under
`build/fedora/release/`: the RPM, runtime manifest, and `SHA256SUMS`.

For an isolated graphical source preview after bootstrap:

```bash
/usr/bin/python3 packaging/fedora/preview.py
```

This preview uses disposable HOME/XDG directories under `build/preview/` and
port **8790**, rather than the installed app's configuration or accounts. It is
still the real app: do not invoke external account or cloud operations during a
preview. `packaging/fedora/preview-complete.py` previews the staged package at
the same preview port with separate state after a successful build.

## Licensing

The main AutoZcoder application is MIT-licensed; see [LICENSE](LICENSE).
**The whole source tree/RPM is not MIT-only.** The vendored
`tools/captcha-mint/captcha-*.ts` solver is **CC BY-NC-SA 4.0** from
TriDefender/zcode-api, including noncommercial and ShareAlike conditions.
Other bundled libraries/browser components have their own licenses.

Read [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and the preserved
[solver license](tools/captcha-mint/LICENSE.vendor) before redistribution.
The RPM carries collected dependency notices in `BUNDLED-LICENSES.txt`.
This is a locally bundled application RPM, not an official Fedora package or
an assertion of Fedora repository-policy compliance.
