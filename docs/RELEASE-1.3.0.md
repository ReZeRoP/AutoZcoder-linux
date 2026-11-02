# AutoZcoder Linux 1.3.0 release candidate

## Features

- Foreground backend owned by an optional `autozcoder.service` systemd user unit.
- Desktop is an independent GTK/WebKit client; opening deliberately starts the
  default unit and closing does not stop it. Custom configs are attach-only.
- Explicit start/stop/restart and opt-in autostart enable/disable commands.
- Read-only status/doctor, bounded redacted configuration inspection, allowlisted
  user-manager queries and loopback-only identity/liveness probes.
- Reconnection with bounded retries/request cancellation, unique poll ownership,
  trusted key recovery and preserved tab/form state. Deferred autosaves cannot
  replay after offline edits or a connection-generation change.
- Bounded shutdown, startup rollback, tracked callbacks/producers and owned-process
  cleanup. SQLite closure interrupts queries and bounds its lock wait.
- Default configuration, API keys, accounts and endpoint remain compatible with
  1.2.2. The unrelated port8788 system gateway is never managed.

## Verification completed

- 576 Python regression tests passed; one legacy Tk module skipped on Python3.14.
  Its8 tests passed separately under private Python3.12.
- 12 Node dashboard tests passed.
- 16 packaging tests passed locally and in a Fedora44 source-fixture container.
- Real systemd USER service integration passed in a rootless Fedora44 container:
  start, restart, crash recovery, autostart independence while active/stopped,
  intentional stop, authenticated dashboard shutdown, config preservation.
- Final RPM digests/checksums pass. Its tested core matches staged source.
- 10,849 RPM payload paths audited; no personal-state/Windows executable payloads,
  directory permissions accessible, user unit included and no RPM scriptlets.
- Source heuristic privacy scan: no private keys, GitHub tokens or personal
  build-home/server paths detected. This is not a security certification.

## Artifact

`build/fedora/release/autozcoder-1.3.0-1.fc44.x86_64.rpm`

Size:442435182 bytes (~422MiB).

SHA-256:
`0bc3e071a165d2404d579d3600f316eabdb3910b26e418a8e9833be268795cfd`

The accompanying runtime manifest and SHA256SUMS are in the same directory.
The RPM is unsigned. Checksums verify integrity, not publisher authenticity.

## Limits and rollout state

- Actual GTK/window-close/reopen and graphical dashboard screenshots remain
  unverified: host preview-tool safety service unavailable. Mock/JS tests are not
  substitutes for graphical proof.
- Actual login/logout was not exercised. No lingering is enabled; service lifetime
  follows the user manager.
- Legacy helpers can remain inside synchronous I/O until return. Shutdown fences
  further work and reports deadline warnings; systemd/process containment remains
  necessary rather than claiming forced cancellation of Python threads.
- No live inference, account/signup/CAPTCHA/bonus/proxy-scan/cloud workflow tested.
- Production package remains1.2.2. No host user-unit, autostart or running production
  service was changed. Source is on `feature/always-on-linux`, uncommitted and
  unpublished. CI changes are local; there is no1.3.0 remote CI claim.
- Source metadata/docs and preview-helper updates after staging may not appear in
  this candidate RPM's docs; all application/runtime fixes were checked byte-for-byte.

See `LINUX-SERVICE.md` for usage and migration, and `LINUX-TESTING.md` for details.
