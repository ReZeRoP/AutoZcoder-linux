# Always-on Linux gateway

AutoZcoder Linux 1.3.0 separates the gateway process from its dashboard. The
background service belongs to the current user, not root. Existing accounts,
configuration, API keys and port8789 are retained. This is a Linux feature release
based on upstream AutoZcoder1.2.2, not a claim of a new upstream version.

## Normal use

Launching `autozcoder` opens the dashboard and deliberately starts the user unit
if it is not already running. Closing the window leaves the gateway running.
The dashboard can reconnect after a service restart. An explicit stop is not
undone by automatic reconnection.

The RPM ships `/usr/lib/systemd/user/autozcoder.service` **disabled**, with no
install/upgrade service actions. It does not create users, enumerate sessions,
modify global user-manager settings, enable lingering, or manage the unrelated
system `zcode-gateway.service` on8788.

### Commands

```bash
autozcoder status
```

```bash
autozcoder doctor --json
```

```bash
autozcoder doctor --online
```

`status` reads a small allowlist of the current user's unit state and the configured
loopback instance. `doctor` is offline unless `--online` is explicitly supplied;
that flag checks only local instance identity/liveness. Neither command creates
configuration, rotates keys, reads account/database contents, starts a service,
downloads dependencies, or performs inference/upstream account checks. Reports
use sanitized structured check codes rather than private config or journal dumps.

```bash
autozcoder start
```

```bash
autozcoder stop
```

```bash
autozcoder restart
```

These commands manage only the current user's `autozcoder.service`. Configuration
or port conflicts fail rather than killing/adopting another process. Manager
unavailability is reported with the explicit foreground alternative.

### Login autostart

```bash
autozcoder autostart enable
```

```bash
autozcoder autostart disable
```

Autostart affects future user-manager starts only. Enabling does not imply start
now; disabling does not stop the current gateway. No linger is enabled: service
lifetime follows systemd's user manager and should not be interpreted as a promise
of operation after logout. The backend requires no display or compositor target;
the GTK dashboard still requires a graphical session.

## Custom profiles and manual mode

```bash
autozcoder serve --config /absolute/path/to/profile.yaml
```

```bash
autozcoder ui --config /absolute/path/to/profile.yaml
```

Foreground serving owns the backend; custom-profile UI is attach-only. A custom
profile never repoints or controls the default user unit. The existing `--headless`
flag remains a foreground alias. Missing explicit configuration does not fall back
to another file.

The singleton user service expects standard per-user XDG roots. Conflicting
terminal/user-manager XDG or `ZCODE_GATEWAY_*` runtime overrides are rejected,
not silently imported into the unit. Use explicit foreground profiles for custom
roots/settings. All binding/attachment remains loopback-only, including bracketed
IPv6 origins.

## Upgrade and removal

The1.2.2 embedded process must be closed before the new service can own the same
configured port. An upgrade does not kill or restart it. Existing XDG settings and
data stay in place; ownership separation does not require key rotation, account
import or database migration.

Before removal, explicitly stop the unit and disable its future login starts.
The RPM does not enumerate users or silently stop their running gateways.
Uninstalling does not remove private XDG data. An orphaned enabled-unit link may
need user-manager reload/reset after package removal; reinstallation preserves
the user's explicit autostart choice.

## Safety and shutdown

Service failures are bounded by systemd restart limits. Permanent configuration
and ownership errors use exit78 and73 and do not restart repeatedly. HTTP `/ready`
503 means no account is ready, not that the process crashed.

Shutdown drains requests briefly, rejects new background work, stops existing
producers/workers, and closes clients/database. `KillMode=control-group` with a
20-second unit deadline is the containment backstop. A timeout is not presented as
successful thread cancellation. The desktop never owns the service lock and never
stops the gateway merely by closing.

A public local identity response carries only version, opaque config/boot identity,
mode and stopping state. Identity detects mismatches; it is not a replacement for
authentication. Dashboard controls remain authenticated, and local key injection
retains bind-address, Host and TCP-peer checks. External navigation is sent to the
system browser; native controls are restricted to the verified local app.

## Verification boundaries

Unit/mock tests prove diagnostic side-effect restrictions, command allowlists,
identity checks and lifecycle invariants. Static Fedora/unit checks prove packaging
structure, not login/logout or GUI behavior. Synthetic user-manager and GTK tests
must be reported separately from mock coverage. Production accounts, inference,
signup, CAPTCHA, bonus claims, proxy scans and cloud operations are not test inputs.
