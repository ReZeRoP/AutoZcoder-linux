# Linux fork v2 integration

## Provenance and recovery

Upstream: B3hnamR/AutoZcoder v2.0.0,
`38df8cd85f70433378aeb2816a53e271fb48a45b` (released 2026-10-31).
The fork's previously unpublished Linux work is preserved in commit
`da636f7` and recovery branch `backup/pre-v2-20261102`.

This integration updates source and version metadata to 2.0.0 / Fedora Release 1.
It does not build/install an RPM, restart services, modify production settings,
deploy cloud resources, or perform live inference/registration/claims.

## Compatibility choices

| Surface | Fork behavior |
| --- | --- |
| Default desktop dashboard | Studio remains `/dash` |
| Modular v2 dashboard | `/dash/v2`, with `/dash/next` alias |
| Backend | Shared v2 API, analytics, quota/import, automation and claim recovery |
| Linux lifecycle | User unit on 8789, request/child ownership and bounded cleanup retained |
| Streaming | Verified-delivery policy, account pins and no replay after output retained |
| Relay 429 | Versioned/platform header precedence and terminal ambiguous provenance retained |
| Relay 5xx | Forwarded overloads enter bounded model retry, not relay cooldown |
| New relay configuration | Disabled by default; existing saved settings remain unchanged |
| Relay deployments | Secrets and allowlists added to Cloudflare/Vercel Edge/Deno templates |
| Netlify | Source reference retained; deployment remains blocked due to buffering |
| Updates | Fedora RPM-managed; Windows executable self-update cannot run in managed Linux mode |
| Root requirements files | Compatibility wrappers retained for existing Fedora commands |
| Release workflow | Linux validation only; upstream workflow retained as a reference fixture |

V2 worker/session/claim controllers participate in owned shutdown. A manager
that cannot finish stopping is not silently dropped or replaced. Automatic
registration requires an active Start Plan, usable quota, a nonduplicate
identity and uncancelled session. Existing saved generator options win over
new defaults.

Upstream browser/Host security checks use a pure ASGI pass-through guard here,
not response-wrapping middleware, so the verified owner retains the actual
downstream send, disconnect and cleanup deadlines.

## Runtime and verification limits

The desktop/gateway remains Fedora Python 3.14; optional bundled browser tooling
uses private Python 3.12. Full Chromium is supported by an opt-in offline test
hook, `AUTOZCODER_TEST_CHROMIUM=/absolute/path/to/chrome`, without downloading
a second headless-shell browser.

Invisible Linux generation requires Xvfb, now declared in the RPM spec.
The retained minter has upstream Linux source/build/discovery support, but is
not compiled or shipped by the Fedora packaging workflow. Features requiring
that sidecar are not available out of the box. Its noncommercial vendor license
is still applicable.

CI covers both Python 3.12/3.14, intercepted browser fixtures, Node dashboard
tests, retained Bun sidecar source tests, runtime-error lint and Fedora packaging
checks. Bun dependency installation occurs in CI, not at application startup.
The upstream release-reference tests validate only the preserved workflow;
`test_v2_fork_contract.py` independently checks the actual Linux pipeline.

Local verification on 2026-11-02:

- Python 3.14: **2,047 passed**, 35 skipped (unavailable Playwright, legacy Tk
  module and Windows-only updater cases).
- Python 3.12 with the existing bundled full Chromium: **2,082 passed**,
  seven Windows-only updater cases skipped. This includes intercepted v2
  browser fixtures and both-workspace navigation/rendering.
- Node dashboard/template suite: **85 passed**. Fedora packaging checks:
  **16 passed**. Shell syntax, Python AST/symbol review and `git diff --check`
  passed; runtime lint and retained Bun source regression gates run in CI.
- The Bun/Happy DOM sidecar suite was not executed locally because its
  dependency environment was not installed. No dependencies were installed
  on the desktop for this integration.

No synthetic test proves that a service will accept live generation, claims,
CAPTCHA minting or inference. Those operations require explicit authorization
and appropriate external configuration. Upstream VPS install/update commands
in the guide/reference are not the Fedora desktop's package/service workflow;
use the root README and `packaging/fedora/README.md` for this distribution.

## Rollout

Publish reviewed source first. A candidate RPM build, installation and idle-safe
restart require separate approval. Preserve accounts/configuration/keys/current
relay routing, existing release metadata and a rollback RPM before that rollout.
Never manage the unrelated system gateway on 8788.
