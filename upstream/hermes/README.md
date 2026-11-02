# Fixes for Hermes itself, to send upstream

Four causes found on 1 and 2 Nov 2026 are in Hermes, not in this repository, so
they are fixed where they come from: as pull requests to Hermes
(NousResearch/hermes-agent), made against v0.22.5
(`bf0203a794a07aac3ff764972ab9087baa4e4f4e`), the version the VM runs.
Nothing here is applied to the VM's Hermes by hand: the VM takes them in a
Hermes release.

Apply them in order on a checkout of that commit:

```
git am 0001-spill-path-idle-sandbox.patch 0002-ratelimit-headers-logged.patch 0003-spill-fallback-posix-path.patch 0004-aux-codex-pick-by-model.patch
scripts/run_tests.sh tests/tools/test_tool_result_storage.py tests/agent/test_ratelimit_headers_logged.py tests/agent/test_retry_delay_parsers_shared.py tests/agent/test_auxiliary_client.py tests/hermes_cli/test_codex_credential_host_binding.py tests/agent/test_codex_usage_attribution.py tests/agent/test_codex_cloudflare_headers.py tests/plugins/image_gen/test_openai_codex_provider.py tests/hermes_cli/test_auth_store_windows_encoding.py tests/gateway/test_multiplex_process_memo_scope.py
```

## 0001: the "too large" note names a path the Bot can read

Cause: under the docker backend a Bot's read_file and terminal run in its
container, but Hermes passed the sandbox it had registered for the turn,
which is none once the idle reaper drops it after 5 minutes (the container
itself lives on), and it read "no sandbox" as "runs on the host", so the
note named `$HERMES_HOME/cache/spillover/<id>.txt`, which does not exist in
the container. Seen 1 Nov: Bramble's container last ran at 3:49 PM, and a
192,576-character tw_git result saved at 4:00:42 PM got the VM's path.

The fix brings the sandbox back (`ensure_task_env`, as vision already does)
before saving, and treats "no sandbox" as host side only on the local
backend.

The note will then name `/root/.hermes/cache/spillover/<id>.txt` inside the
container. The Thornswood guard refuses any path with `.hermes` in it, so
this alone does not let a Bot read the file. That is a separate decision
for Patrick (the guard's rule is about Hermes's home on the VM, and the
container's `/root/.hermes` is Hermes's read-only view of the Bot's caches),
and it waits on each Bot's container mounting only its own caches.

## 0002: a 429 records which limit refused it

Cause: Hermes read only the reset time from a 429's headers, so which window
refused the call (the subscription's 5-hour or weekly window, or a
per-minute bucket) was never written down. On 1 Nov the 8:46 AM 429 left
only the body and an 836-second cooldown.

The fix keeps every `anthropic-ratelimit-*`, `x-ratelimit-*` and
`retry-after` header of a refusal (counters, statuses and times; never a
credential) and logs them in one line.

## 0003: the sandbox fallback path is POSIX on every host

Cause: when a terminal environment names no temp dir, Hermes writes an
oversized result inside it at `STORAGE_DIR`, which came from the host's
`tempfile.gettempdir()`. On Windows that is a backslash path, quoted by
`shlex.quote`, that the environment's POSIX shell cannot use. Seen 1 Nov:
`test_tool_use_id_cannot_escape_storage_dir` failed on One (Windows) on
v0.22.5 with and without 0001 and 0002, and only there.

The fix makes it `/tmp/hermes-results`, the same default as
`BaseEnvironment.get_temp_dir()`, with a test that holds it.

## 0004: an auxiliary Codex call picks the pool entry for its own model

Cause: the auxiliary Codex resolver knows the model it will call but asked
the pool for an entry without naming one. An unscoped pick honours every
model cooldown on an entry, and a Codex entitlement bench lasts a year. Seen
2 Nov on the VM: the one Codex entry (c76109) is healthy (token good to 9
Nov, not dead, not exhausted, no refresh due) but carries benches until 30
Oct 2027 for anthropic/claude-opus-4.6, gpt-5.4 and gpt-5.5, set on 30 Oct
when the account was refused those slugs. The Bots' Codex model is
gpt-6.1-sol, which was never refused, yet every auxiliary pick came back
empty and auxiliary_client found no token.

The fix passes the model to the pick in both Codex callers. The year-long
bench on a slug the account really cannot use is upstream's design (#71970)
and stays; it now benches only that slug. A pick with no model is the
unscoped `select()` it always was, so no other provider's path changes. The
test stand-ins on the Codex paths take the model, as the real pool does.

The first Linux run of all four (2 Nov) failed 3 tests in
test_auxiliary_client.py: the first version passed `model=None` to every
pick, and stand-in pools written as `select(self)` refused it. Unscoped picks
are back to `select()`, and the Codex stand-ins take `model`.

## 0007: a coalesced webhook delivery survives a gateway restart

Made against upstream main `f0d94882` (2 Nov 2026), in `on-main/`.

Cause, measured on the VM (gateway.log line 2032, 1 Nov 10:31:12 PM, during
the #72 install's gateway stop): two GitHub deliveries on the coalesced
`github-checks` route were lost. A coalesced delivery is answered
`202 coalesced` before any admission, and GitHub does not resend an
answered delivery, so the coalescer's buffer is its only copy. On a stop,
`drain_gateway_runtime` withdraws every adapter's message handler before the
adapters disconnect. The webhook adapter's disconnect flush then admitted
each pending group against the withdrawn ingress: `producer_scope` found no
runner and raised `RuntimeStoreError('not_found')`, the group was logged
"was not admitted", and it was gone. A hard kill lost the buffer outright.

The fix keeps each pending group in a spool in the gateway's home
(`webhook_coalesce_pending.json`, 0600, written atomically) until its
admission commits. The route's config is never written (it can hold the
HMAC secret); it is read back from the live routes. While ingress is
withdrawn, a settled group is not tried and stays spooled. When the next run's
listener is up and its ingress is wired, every spooled group is buffered
again and admitted as usual. A route that is gone is dropped and named, and
an unreadable spool is set aside. A refusal by a serving gateway behaves as
before.

Tests: four new ones in `tests/gateway/test_webhook_coalescing.py` (drain
then next run, admitted and refused, a newer event during admission, a gone
route and a bad spool); the existing helper's runner now has a session
authority, as `producer_scope` requires. The coalescer's own spool logic
passed a stand-alone run here; the gateway tests need upstream's test
environment, so run them before the pull request is opened:

```
git am on-main/0007-webhook-coalesce-survives-restart.patch
scripts/run_tests.sh tests/gateway/test_webhook_coalescing.py tests/gateway/test_webhook_adapter.py tests/gateway/test_webhook_authority.py
```

## On upstream main

The pull requests go to upstream main, not to v0.22.5. On 2 Nov, main was
`5aa8df74`. 0002, 0003 and 0004 apply there as they are. 0001 does not:
upstream had changed `_is_host_side_env` to ask
`env_uses_host_filesystem(env)` for a live environment. `on-main/0001-spill-path-idle-sandbox.patch`
is 0001 on that main. It keeps upstream's check for a live environment and
changes only the `env=None` case. Run on that main (cloud, Python 3.14):
test_tool_result_storage passed 59 of 59. The tool executor, spill, persist
and turn budget tests failed 4 of 433 with the patch, and they fail the same
4 ways on main without it: two checkpoint path tests that fail only when
run after the storage tests, and two opencode routing tests that need the
anthropic extra. The files here at the top stay as they are for v0.22.5,
the version the VM runs.

## Test results so far

- Windows (One, Python 3.14, 1 Nov), 0001 and 0002: test_tool_result_storage
  58 passed and 1 failed (the 0003 cause, also failing without the patches),
  test_ratelimit_headers_logged 3 passed, test_retry_delay_parsers_shared 9
  passed.
- Linux (the VM, python:3.14 container, 2 Nov), 0001 to 0003: 72 passed,
  0 failed.
- Linux (cloud, Python 3.14.6, uv.lock's dev group, 2 Nov), 0001 to 0004:
  the five files above plus the Nous, xAI and Anthropic pool tests, 369
  passed and 1 failed. The failing test,
  test_anthropic_messages_profile_resolves_to_messages_adapter, fails the
  same way on v0.22.5 with no patches: the anthropic extra is not installed
  there.
- 0004 on the VM: waits on the Linux run card. The upstream pull requests
  wait on it.
