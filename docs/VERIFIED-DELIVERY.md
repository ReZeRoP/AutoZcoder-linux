# Verified response delivery

## Why verified delivery exists

An upstream HTTP200 can close with an incomplete response body. Replaying a turn
whose text/tool output already reached the client can duplicate downstream work.
Verified delivery keeps each attempt private until a complete native message and
its content/tool structure validate, then delivers only that successful attempt.
Eligible transport/truncation errors may be retried twice, subject to shared limits.

This is not resumable streaming, exactly-once upstream inference/billing, semantic
verification of answers, or a guarantee against persistent provider failure.

## Release7 candidate: keepalives and request-wide bounds

Release6 withheld response headers until completion and used a300-second total
verification deadline. One observed ZCode client retried that504 eleven times,
leading to58m49.851s of waiting. The upstream accepted those requests; whether it
subsequently progressed or stalled was not observable in the old timeout logs.

The release7 candidate returns verified SSE promptly after local validation.
Native clients receive `ping`; OpenAI chat/legacy clients receive SSE comments.
The connection stays active but **thinking, text and tool calls still wait until
full verification succeeds**. Keepalives never reset upstream progress timers.
ZCode3.16.3 also has a separate parsed-model-event idle watchdog(default10minutes):
comments/native pings in its ordinary mode do not reset it. To use a30-minute
verified gateway profile, explicitly align ZCode's user setting
`modelStream.idleTimeoutMs` in `~/.zcode/cli/config.json` to1900000(or an equivalent
larger finite limit), preserving other values and restarting the client bootstrap.
Project overrides can supersede the user setting. This client edit requires
separate permission; do not fabricate assistant output to evade the watchdog.

After response headers, a failure is a dialect-correct SSE error under HTTP200.
It cannot become a different HTTP status. No successful `message_stop`, `[DONE]`
or completion finish is fabricated on error. Local malformed-request/auth errors
before streaming retain their ordinary HTTP error status.

### Configuration

All values are finite and validated; existing explicit values stay authoritative.
The default delivery mode remains `realtime`. There is no silent fallback to it.

| `upstream` setting | Default | Purpose |
|---|---:|---|
| `stream_delivery` | `realtime` | Select `verified` for private complete-turn delivery |
| `stream_buffer_max_bytes` |16777216| Bound retained representations,16MiB |
| `stream_verify_timeout_seconds` |300| Hard request deadline, including opening/retries |
| `stream_progress_timeout_seconds` |600| Maximum interval without accepted meaningful output |
| `stream_heartbeat_seconds` |10| Downstream keepalive interval |
| `stream_max_sends` |12| Actual chat POST budget across all nested retry layers |
| `stream_max_concurrent` |4| Verified admission bound, held through delivery/cleanup |
| `stream_cleanup_timeout_seconds` |5| Bounded cleanup wait, with slow work still owned |
| `stream_send_timeout_seconds` |30| Bound a blocked downstream ASGI send |

A long-reasoning profile may explicitly select1800seconds total,600seconds without
meaningful progress, and verified delivery. Installing the candidate alone does
not overwrite personal settings or silently extend the300-second compatibility
policy. A separate approved rollout selects the long-reasoning profile.

Meaningful progress is strictly accepted nonempty text/thinking/JSON-fragment/
signature/redacted-thinking content. Pings, comments, empty setup/deltas,
usage-only updates and byte trickles do not renew it. Retries/backoff do not reset
any request-wide clock. A hard deadline also bounds valid tiny-payload trickles.

Each actual chat POST counts, including failed transport sends, relay rotation,
account/model retries and token-refresh resends. Existing lower-level limits and
fallback selections still apply; shared controls only add a ceiling. No model,
effort, token cap, account selection or relay-route policy is silently changed.

### Error and observability contract

Machine-readable codes distinguish total deadline, no-progress deadline,
send-budget exhaustion, buffer overflow and protocol/transport failure. Terminal
policy failures carry additive `retryable:false` and `terminal:true` metadata.
These are hints: a third-party client may use its own retry classifier. An offline
execution of ZCode3.16.3's actual byte-level SSE parser and business-error classifier
confirmed that HTTP200 with these nonnumeric codes is nonretryable under its
normal bounded policy. The `retryable` field itself is ignored. Numeric504 codes
remain retryable. Unbounded workflow policy and a separate model-idle watchdog
can still override the practical outcome; no universal end-to-end guarantee is
claimed.

Diagnostics retain safe request correlation, phase, elapsed time, attempts/sends,
validated event/byte counts and last-progress age. They omit request/response
contents, tool arguments, credentials, account snapshots and private relay URLs.
A local timeout is recorded as logical504, not mislabeled499; actual downstream
disconnect remains499. Observed partial usage is not proof of completed billing.

Collectors, sends and cleanup are owned and cancellable. Slow cleanup retains
admission/ownership rather than silently creating an unlimited orphan queue.
Python cannot forcibly terminate an arbitrary cancellation-resistant coroutine;
app shutdown and systemd's existing cgroup deadline remain containment backstops.
Pre-transfer relay cleanup owns a shielded timeout waiter too, so request
cancellation cannot abandon the deadline that cancels a cooperative stuck close.

## Protocol protections retained

Strict completion requires a recognized terminal reason after all blocks close.
Empty completed OpenAI tool inputs emit `{}`; mixed nonempty initial input and
JSON deltas are rejected. Routing pins remain immutable across retries/fallback;
routing-only fields never reach the upstream body. Relay responses are closed on
cancellation/errors before ownership transfers. Signatures and tool fragments
are preserved; signatures are not cryptographically verified by the gateway.

Realtime retains its existing no-replay boundary after semantic output. This
candidate does not silently change its delivery guarantee into full verification.

## Verification and deployment boundary

Release6 historical offline checks:703Python3.14 cases passed (one legacy Tk module
skipped),711Python3.12 cases passed,54Node cases,16Fedora packaging cases.
Release6 was subsequently installed and enabled on8789; accounts/settings were
preserved. These historical counts are not release7 verification results.

Release7 requires deterministic timing/budget/lifecycle tests, full Python/Node/
packaging regressions and candidate RPM inspection before rollout. Test prompt
headers/keepalive timing through ASGI, not only collected TestClient bodies.

The user authorized two harmless isolated Flash smoke requests after offline
tests, each with at most one actual POST, no executable tools or private history,
and a512-token output cap. Small smoke requests do not prove long Max-effort
history reliability. No other provider inference is authorized by these tests.

Installed rollout remains separately approved: fresh private backup and release6
rollback, modify only agreed verification settings, restart only the current
user's AutoZcoder service, and verify account/key/settings preservation. The
separate system gateway on8788 is never part of that rollout. Never publish private
backup/config/account artifacts.
