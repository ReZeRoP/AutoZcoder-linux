# Dark studio UI candidate — 1.3.0-2

This build changes the visible interface, not the working gateway/service contract.

## Visible changes

- New AutoZcoder Studio shell, grouped sidebar and page descriptions.
- Overview landing page with endpoint copy, readiness, rolling24-hour activity,
  success rate, genuine sampled median latency, attention items and recent events.
- Single-series request timeline with crosshair/tooltip, keyboard hour inspector,
  and expandable data table. Missing data is not presented as zero.
- Accounts reduced from nine default columns to six. Token validity, IDs, cap resets
  and error history move into expandable account details. Add account is primary;
  import/reload/bulk maintenance remains reachable through Manage accounts.
- Readable model names and keyboard-operable sorting headers.
- Structured log rows and clearer shared typography/spacing in settings/tools.
- Scoped native GTK styling: matching service strip, contextual Start button,
  compact Service popover and independent login-autostart control.
- Matching self-contained offline/waiting page; no external fonts/assets or new
  native JavaScript bridge.

All existing destinations remain accessible, including Generator, Proxy tools,
mail/relay controls, Stats, Usage, Analytics, Connection, Settings and Help.
Valid saved tabs are retained; new sessions start on Overview. The gateway,
service ownership, authentication, reconnection, quotas and external workflows
remain unchanged.

## Verification

- 593 Python tests passed; one legacy Tk module skipped under system Python3.14.
  Its8 tests passed separately under private Python3.12.
- 24 JavaScript tests passed (connection lifecycle and Overview fixtures).
- 16 packaging regressions passed.
- New fixed CSS/JS routes and existing safe bootstrap/auth contracts tested.
- Chart color#8B75DC passes the data-viz validator on surface#1B2738.
- Native GTK CSS parses without warnings. Inline dashboard JavaScript syntax passes.
- Tests cover all retained nav destinations, progressive account details,
  preserved action/filter hooks, secret-free Overview projection, partial/empty
  states, chart/table controls and late-render tab protection.

Actual visual layout/screenshots, live browser interactions and GTK appearance
were not verified because the host preview-tool safety service is unavailable.
These test results are not a substitute for a human visual review.

## Try the candidate

First close the current dashboard window. Install the candidate RPM through
Fedora's package manager; no personal configuration/accounts are replaced.
The RPM does not restart the running user service. Restart it explicitly so it
serves the new dashboard assets, then reopen the app.

Candidate:
`build/fedora/release/autozcoder-1.3.0-2.fc44.x86_64.rpm`

The installed version before this rollout remains1.3.0-1. This candidate has not
been installed or published by the implementation task. Source changes remain
uncommitted on `feature/always-on-linux`.

Review Overview, Accounts, Models, Settings and the Service menu first. Useful
feedback: what looks crowded, what is hard to find, what needs fewer clicks,
what should be shown/hidden by default. Do not share screenshots containing
API keys or private account/mail credentials.
