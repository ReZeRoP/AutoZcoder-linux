# Proxy and edge-relay reliability (release8 candidate)

## Configuration is separate from observations

Relay settings use independent snapshots and atomic read/modify/write transactions. A health probe runs outside the configuration lock and merges only its own observation into the latest matching relay ID/URL. Deleted/replaced relays and superseded probe results are ignored. A late health response cannot restore an older model filter, enable toggle, strategy or selected relay. Failed persistence does not commit a different in-memory configuration.

Connectivity-probe cooldowns are separate from inference cooldowns. A successful forwarding probe clears only its own probe failure; it cannot certify that GLM inference has recovered. The existing60-second policy and intentional all-cooled fallback remain. Each relay send attempt excludes URLs already tried by that send operation; required-relay exhaustion never silently routes directly.

## What a healthy badge means

A new probe verifies TLS, forwards a unique challenge to the existing echo target, and checks the expected JSON/target/challenge with bounded response size. It refuses redirects, invalid TLS, static200pages, malformed or oversized bodies. Ordinary network waits are bounded by the probe's monotonic budget and remaining-time reads; pathological native/DNS operations still require process containment rather than claiming Python can forcibly interrupt them.

- **Forwarding verified:** this connectivity challenge passed.
- **Legacy / unverified:** a saved observation predates challenge verification.
- **Probe failed:** a completed forwarding test did not pass.
- **Never tested:** no completed observation.

All states display observation age when available. **Model inference and SSE capability are not tested by this probe.** A green result is not a model-health guarantee or a cryptographic attestation.

New Cloudflare/Vercel/Deno worker templates preserve streaming bodies and emit versioned response-origin markers. These markers are evidence from an operator-trusted worker, not proof against a malicious relay. Existing workers are not redeployed or rewritten automatically.

##429handling

Known relay-host quota responses can cool/rotate distinct relays within existing request budgets. Recognized upstream/provider rate-limit responses retain account/model handling. An unclassified legacy relay429returns an explicit ambiguous failure: no false healthy mark, account penalty, speculative model fallback or automatic rotation based solely on status. Host failures occurring before worker execution can lack provenance.

## Legacy error envelopes and safe diagnostics (release9 candidate)

The classifier recognizes exact provider-specific11113/1005quota codes in flat
or nested error objects, including canonical integer/string code or statusCode
and a string message/msg. It does not guess from error wording, generic429,
untyped generic limits, malformed aliases or conflicting body evidence.
Versioned origin markers take precedence, then recognized relay-host evidence,
then body evidence. Conflicts are reported as bounded flags, not raw text.

Canonical `3009` envelopes use the same strict flat/nested validation but are
handled separately as terminal provider busy/rate-limit responses, not quota
exhaustion. ZCode 3.16.3's client maps this code to nonretryable `ModelRateLimited`;
this does not establish which upstream concurrency/resource limit was reached.
Authoritative relay-origin markers/platform headers still take precedence and
retain their existing host-rotation policy. A recognized upstream `3009` does not
rotate accounts/relays/models, strike quotas or park accounts. The public error
keeps code `3009`, logical 429, `reason:provider_busy`, and
`retryable:false`/`terminal:true`. Client policies can still override retry hints.
This compatibility path covers opening HTTP 429 and HTTP 200 JSON business errors,
including direct requests and the response after an existing 401 token refresh.
It does not add a new initial/mid-stream SSE-code policy, enable automatic retries,
redeploy relays or guarantee that a later inference request will be accepted.

Terminal relay errors include an additive `diagnostic` projection with a locally
generated gateway request ID, relay attempt ordinal/provider enum, observed HTTP
status, normalized MIME/body shape, known code/type or other, provenance flags,
retry-header presence and bounded read outcome. No prompts, raw response text,
arbitrary key/header values, credentials, account identifiers or relay URLs are
retained there. The same request ID appears in the safe terminal log.

Diagnostic-only observations additionally retain `numeric_codes` at four fixed
locations: `root_code`, `root_statusCode`, `error_code`, `error_statusCode`.
Only parsed integers from 0 to 999999 or canonical ASCII decimal strings of at
most six digits are accepted, normalized to integers. Other strings, padded or
signed strings, booleans, floats and out-of-range values are omitted, never
truncated. This cannot identify every possible textual or oversized error code.
`json_structure` reports `root_kind` and the JSON kinds of `code`, `statusCode`,
`type`, `message`, `msg`, `error` at root and immediate error-object locations;
absent fields are omitted and null fields have kind `null`. No arbitrary keys,
raw values or deeper structure are retained. Both maps are revalidated at the
error/log boundary; failed or incomplete parses provide no payload observations.
These additive schema-version1 fields are observations only: an unfamiliar code
still has unknown origin and never changes retries, accounts or classification.

Observed response status can be 429 or 200 (for a recognized JSON business error);
direct-request diagnostics retain the actual observed status and completed read
without inventing a relay host. No response means no observed status or byte count.

Opening-stage relay failures are recorded once as logical503plus relay error code
with unknown token columns leftNULL. That path skips exchange capture even if
capture is separately enabled. At most four safe-record writes remain outstanding;
saturation, failure, or pending disk work is reported rather than implying
persistence succeeded. The record queue is not expanded by caller cancellation.
The existing ledger has no request-ID column; correlation lives in the client
error and log, not repurposed ledger fields.

Recognized opening `3009` uses the same bounded, capture-free writer with logical
429 and error code `3009`; unknown token columns remain NULL.

This improves compatibility and observability, but cannot reconstruct earlier
uncaptured429responses or prove a legacy relay is honest. Genuinely ambiguous
limits remain terminal/nonretryable and do not penalize accounts or trigger
speculative failover. No new inference or cloud deployment is required.

## Deployment operations

The dashboard starts an operation promptly and observes it using short authenticated GETs. It no longer waits for an entire provider deployment under the generic five-second dashboard deadline.

- One owned worker per manager, no queue;300-second cooperative operation budget.
- Operation ID saved before submission; same ID/payload observes the same operation, different payload conflicts.
- Lost acknowledgement/reconnect/reload uses status GETs, never automatic creation replay.
- At most32terminal results retained. Admission-generation rotation prevents expired/previous-instance IDs from silently becoming new operations.
- No persisted credential/job journal. A process restart or expired unknown operation requires provider reconciliation; this is not exactly-once remote creation.
- Non-idempotent provider mutations are not automatically retried after ambiguous transport/5xx outcomes.
- A local timeout/disconnect is **not** proof of remote cancellation. Work remains owned through actual thread exit and shutdown containment.
- A known remote success followed by failed local registration offers **registration-only retry**, not another remote create.
- Status/error projection contains only safe identifiers and code-mapped messages, never provider tokens, raw bodies or exception tracebacks.
- Closing the dialog stops neither the backend deployment nor an already-created remote resource.

The legacy synchronous `/admin/dash/relays/deploy` route retains its success/error shape through the same worker owner. Legacy submissions without an operation ID do not acquire lost-ack protection. New start/status routes are `/admin/dash/relays/deploy/jobs`; an optional `id` query selects the operation, and `POST /jobs/{id}/register` retries only failed local registration.

Manual probe requests use a bounded25-second browser allowance while ordinary dashboard requests retain5seconds. Connection generation/cancellation rules and no mutation replay remain intact.

## Dashboard and proxy controls

Relay controls send only changed fields, serialize conflicting writes, and invalidate older GET snapshots. A requested post-mutation refresh runs after the old deduplicated GET completes rather than being dropped. Failed loads preserve the last snapshot with a stale/error label. Unknown provider metadata is escaped, not interpreted as HTML.

Maintenance reports accepted starts rather than treating HTTP200as success regardless of response content. Destructive/reset maintenance uses the existing Cancel-first confirmation; overlapping checker actions are guarded. Checker completion remains distinct from merely starting or exiting.

Auto-verification is still a runtime-only setting, off by default. The UI normalizes GET/POST interval fields and saves interval changes. Settings are validated before mutation, interval changes wake the existing scheduler, rapid disable/re-enable does not lose ownership, and last_verified_at records completed verification instead of job launch. Checker Stop does not disable the separate auto policy; stop-all/shutdown does.

Public proxy health projection and visible URLs redact userinfo, including passwords containing@. Internal pool URLs/credentials are unchanged. Redaction happens after status classification against the private used/bad lists and does not initialize a generator just to read the health card.

## Netlify limitation

New deployment of the bundled legacy Netlify template is disabled: it buffers the upstream body and has no verified streaming-compatible runtime/package contract. The UI and API explain this before using credentials or making any provider request.

Existing Netlify records/routes are preserved and labelled as legacy buffered/streaming-unverified. They are not silently deleted, excluded or redeployed. Re-enabling automatic Netlify deployment needs a separately verified streaming implementation.

## Verification and rollout

Release8must pass synthetic state/job/routing/probe regressions, actual-function Node tests, isolated browser interactions, full Python3.14/3.12suites and Fedora packaging inspection. Template tests exercise the actual emitted worker JavaScript offline for streaming-before-EOF, cancellation and origin-header override.

No test requires production accounts, inference quota, a cloud deployment or a public proxy scan. Building a candidate does not install it. Keep release7artifacts/metadata and obtain separate approval before production rollout. Evidence for this candidate is stored locally under `build/fedora/verification-1.3.0-8/`; those local results are not remote-CI or live cloud-provider certification.
