# UI usability candidate — 1.3.0-5

This update keeps the pure-black theme, existing gateway API and user-service
behavior. Generator and Proxy tools remain directly below Accounts.

## Intended workflows

- Open Search commands or Ctrl+K/Cmd+K to navigate or perform safe local copy
  actions. Search/highlight never runs commands. No service controls, account
  mutations or network checks are registered. Palette navigation is local-only:
  uncached screens show an explicit Refresh action instead of automatically loading.
  Navigation from the normal sidebar retains ordinary page loading.
- Use account status filters and model availability filters alongside text search.
  Status/sort/filter preferences and model favorites are stored locally for the
  verified gateway profile. Free-form queries and selected accounts stay in memory.
- Pin model favorites to the top, without changing the model's routing/default or
  availability. Copy model IDs locally. Missing catalog favorites remain removable.
- Open account details in a side panel: existing snapshot status, plan/quota/reset,
  validity and safe error explanation, without automatic balance/token refresh.
  Manual actions retain authentication and confirmations.

## Privacy and behavior boundaries

Preference storage is schema-versioned, bounded and scoped by origin, config
identity and ownership mode. Credentials, account IDs/emails, account records,
search strings and error text are not persisted as preferences. Denied/corrupt
storage falls back to memory without breaking the dashboard.

Details are projected from loaded public snapshots, not arbitrary notes or token
fingerprints. Polling and reconnects must not retarget actions, replace focused
controls or replay writes. Foreign context invalidates ephemeral detail views.

The command palette owns focus while open, yields to confirmations/login,
honors composition input, and restores focus to a safe target on close.
Danger confirmations initially focus Cancel; Enter activates only the focused
button, repeated Enter is ignored, and the previous keyboard handler is restored.
Model sort/detail buttons use stable focus keys across table rerenders. The account
panel retains selected text during polls and still updates stale/removed warnings
and action availability; selecting another account always replaces its content.

## Verification

- 593 Python tests passed; the legacy Tk module remains skipped under system
  Python3.14 and is checked separately under private Python3.12.
- Node regression suites passed 54 test entries, plus 17 preference assertions
  inside the standalone preference suite. Coverage includes 15 overlay/palette
  tests, 10 executable dashboard integration tests and 4 confirmation-keyboard
  tests. No live account or service mutations are test inputs.
- 16 packaging tests passed; fixed static assets and safe bootstrap routes tested.
- Source parity with final staged package inputs checked.
- Actual browser/GTK interaction and screenshots remain unverified because the
  host preview safety service is unavailable. Synthetic tests are not visual proof.
- On first binding of a profile without saved UI preferences, Overview is selected;
  subsequent selected tabs persist for that profile. Free-form searches remain
  ephemeral by design.

## Rollout

The candidate is built locally only. Installing/restarting the service and GitHub
publication are separate authorized actions. The installed interface is not
modified during development. Browser preview may remain unavailable; test
coverage and any visual verification limitations are reported at completion.
