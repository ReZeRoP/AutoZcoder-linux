# Linux port verification

## Tested target

Fedora 44 x86_64, desktop Python 3.14.7 and private optional Python 3.12.14.
Source baseline: upstream v1.2.2, commit
`ba3fff0633aea126c1856e3e2485e84743dac5ea`.

## Completed checks (2026-10-27)

- Main gateway + Linux regression suite: **381 passed**, one module skipped
  because system Python has no Tkinter. That legacy GUI module was run
  separately under private Python 3.12: **8 passed**.
- Packaging regression suite: **15 passed**, including tracked backup/state
  exclusion, restrictive-mode normalization, cache symlink rejection and exact
  transitive Node-version validation. The initially built RPM passed 12 tests;
  the additional build-script hardening does not change its application runtime.
- Audited the built RPM's directory modes and the installed Node cache against
  all lock entries; no inaccessible directories or installed-version mismatches
  were found. The original payload audit found no private-state files.
- Parsed all 63 Python files in `zcode_gateway/` and `generator/` successfully.
- Shell syntax and desktop-file validation passed.
- Desktop Python/GTK/WebKit imports and private Python optional-dependency imports
  passed. Pinned Wrangler reports version 4.151.0.
- Tested shutdown callback dispatch, off-event-loop setup checks, stop without
  constructing a manager, cancellation of queued jobs using fake jobs, owned
  subprocess-group cleanup (including a child ignoring SIGTERM), atomic config
  publication, loopback-only settings, explicit interpreter selection, and
  preservation of saved settings with temporary data.
- Source privacy scan found no private keys, GitHub tokens, JWT-shaped tokens,
  or build-user home/server paths in the candidate publishable source files.
  This is a heuristic check, not a security certification.

The main suite emits one upstream Starlette/httpx TestClient deprecation warning.

## Not verified

- Native-window rendering, browser dashboard interactions and screenshots were
  blocked by the host application's preview-tool safety service being unavailable.
  Imports and tests are not substitutes for those checks.
- No live inference, signup, CAPTCHA solving, reward/bonus claims, third-party
  proxy scanning or cloud deployment is included in this verification.
- The upstream CAPTCHA minter is not ported/built. Its retained source has its own
  license; see `THIRD_PARTY_NOTICES.md`.
- Windows and non-Fedora distributions are not targets of this Linux release.
- CI results and a successful end-user installation are separate from local tests.

## Reproduce offline tests

Install the documented Linux and development requirements in a virtual environment,
then run from the project root:

```bash
python -m pytest -q -rs
```

```bash
python packaging/fedora/test-packaging.py
```

The test suite uses synthetic state and mocks remote operations. Do not substitute
personal account files or credentials into its fixtures.
