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
