# Fixes for Hermes itself, to send upstream

Ten causes found on 1 and 2 Nov 2026 are in Hermes, not in this repository, so
they are fixed where they come from: as pull requests to Hermes
(NousResearch/hermes-agent), made against v0.22.5
(`bf0203a794a07aac3ff764972ab9087baa4e4f4e`), the version the VM and One's Desktop run.
Nothing here is applied to the VM's Hermes by hand: the VM takes them in a
Hermes release.

Apply them in order on a checkout of that commit:

```
git am 0001-spill-path-idle-sandbox.patch 0002-ratelimit-headers-logged.patch 0003-spill-fallback-posix-path.patch 0004-aux-codex-pick-by-model.patch 0005-desktop-relay-local-source-once-it-signals.patch 0006-desktop-scheduled-jobs-every-gateway.patch
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

## 0005: the Desktop relays This device's Bot Chat when its main connection is remote, and reads it on its own connection

0005 holds two commits. The first is the relay, the second the lookups.

Cause: upstream 9cbc6a5ac0 (11 Sep) made `relayEligibleRoutes`
(apps/desktop/src/plugins/hermes-bots/relay.ts) drop the local "This device"
source from the bot relay whenever one remote connection is the Desktop's
primary, so an unused local runtime is never dialed. A Desktop whose primary
is remote can still run a Hermes on this device. Its agents queue
`message_agent` envelopes in the local `bot_relay\outbox`, and its backend
signals `bot_relay.outbox.pending` over the socket the window holds. The
relay skipped that route anyway, so nothing claimed the envelopes and they
sat there until their waiters timed out. Seen 2 Nov on One: three messages
from This Device's Bot Chat, to Bramble and my-hermes, sat in `outbox\`
from 4:50 PM on, with `claimed\` and `replies\` empty. One's Desktop is
remote-primary: its "Profile bramble no longer exists" refusal comes only
from the remote branch of `resolveRegistryLocalRoute`
(electron/connection-registry.ts line 560).

The fix: once the local source's own backend signals outbox mail, the
relay keeps it as a peer like any other. It drains the outbox, posts the
replies, and the roster loop tells it the other machines' agents. Until it
signals, nothing changes: the local source of a remote-primary Desktop is
still never polled or pinned. A signal from the primary, or an untagged one,
does not count.

The second cause: the Desktop looked This device's Bot Chat up under every
remote Bot's name. Card 1's `desktop.log` on One (2 Nov) has
`GET /api/sessions/local-4e2938…` with `profile=bramble` at 5:38 PM, then
`magpie`, `rift`, `sprocket` and `wisp` at 7:43 PM. Magpie's failed after
a 120 s wait for a remote dashboard. The other three were refused at once
with "Profile ... no longer exists", one after another within 8 ms.
`probeStoredSession` (apps/desktop/src/app/session/hooks/use-session-actions/utils.ts)
took an owner only from its caller. `resolveSessionProfile` and the other
profile-only callers name none, so even a chat whose exact owner the store
already knew went down the cross-profile ladder. That ladder walks the
active source's profile names, here the remote Bots'. The session read pin
then sent each name to the chat's real connection, `local`, which has none
of them.

The fix: the probe starts from the same owner ladder that session calls use
(tile route, the one hint, a tagged row, a proven runtime event). An exact
owner found there is read once, and a miss there is final, as with an
owner the caller names. A chat with no known exact owner walks the profiles
as before.

Check both with the Desktop's own tests:

```
cd apps/desktop
npm ci --ignore-scripts
npx vitest run src/plugins/hermes-bots/ src/app/session/hooks/use-session-actions/resolve-stored-session.test.ts
```

## 0006: the Scheduled jobs list shows every connected gateway's jobs, and each job's buttons go to its own gateway

Cause: the Scheduled jobs page and the sidebar's Cron jobs section asked
only the connection in front (`getCronJobs` in
apps/desktop/src/api/cron.ts is `connectionScoped()`). Seen 2 Nov on One:
the Desktop is in Advanced mode, so Simple mode hides nothing; while Patrick's
own Hermes is in front, the list goes to that connection, which has no
jobs. Bramble's 5 jobs are on thornswood-bots, so they showed only with
Bramble in front on the Bots tab. Every per-job call (runs, edit, pause,
resume, run now, delete) also went to the connection in front, so a job
from another gateway could not be worked even once listed.

The fix: the all-profiles list (`GET /api/cron/jobs` with no profile or
`profile=all`) now joins every connected registry gateway's jobs in the
main process, the way the unified Sessions list joins their chats
(electron/cron-job-union.ts). Only already-pooled backends are read, so a
refresh never dials one; a gateway already listed (the same connection, or
the same install_id) is read once; a dead one adds nothing; an error from
the connection in front is never hidden. Each job from another gateway
carries the `connection_id` that served it, every per-job call goes to that
connection under the job's own profile, and its runs are stamped with that
connection. Rows are keyed by (connection, id), since two gateways can hold
one id. A list for one named profile is unchanged.

Upstream main has moved past v0.22.5 here: its 0e0b2328 added Stop for a
running job and the missed-run policy, which touch the same lines of
apps/desktop/src/api/cron.ts and the Scheduled jobs page. So the upstream
pull request uses `0006-desktop-scheduled-jobs-every-gateway-upstream-main.patch`,
the same change made on upstream main f0d94882 (Stop for a running job also
goes to the job's own gateway there). It needs nothing from 0001 to 0005.
It was first made on 4d26e0cc; upstream then moved `isNotFoundApiError` out
of electron/main.ts beside the new block, so it was rebased on f0d94882 with
upstream's side kept.

Check it with the Desktop's own tests:

```
cd apps/desktop
npm ci --ignore-scripts
npx vitest run electron/cron-job-union.test.ts src/api/cron-job-owner.test.ts src/store/cron.test.ts src/app/cron src/app/chat/sidebar
```

The subagents frame on One is a separate cause and is not in 0006. Its
measurement so far: the Desktop holds two saved owners for This device's
Bot Chat, `{my-hermes, remote}` and `{local, local}`, so it treats the
owner as unknown. One's `desktop.log` rules out the Bots roster as the
writer (every roster snapshot with This device in front lists `local/default`
on its own row, and no line says MISROUTED), and the id is not the Bot Chat
id One's own gateway would mint for `bot-chat:default` (generations 0 to
5). Which step wrote the my-hermes owner is still open; no fix is written
for it until that is measured.

## 0007: a coalesced webhook delivery survives a gateway restart

Made against upstream main `013bde47` (2 Nov 2026), in `on-main/`.

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
environment with the `messaging` extra (aiohttp), so run them before the pull
request is opened:

```
git am on-main/0007-webhook-coalesce-survives-restart.patch
python -m pm.build_env --source . --out /venv --group dev --group test --extra messaging
HERMES_PYTHON=/venv/bin/python scripts/run_tests.sh tests/gateway/test_webhook_coalescing.py tests/gateway/test_webhook_adapter.py tests/gateway/test_webhook_authority.py -q -rfE
```

The first run on 2 Nov (with aiohttp) failed only the drain test, 5 of 5:
its requests went to a route with a real HMAC secret unsigned, so the
adapter answered 401 and nothing was ever coalesced or spooled. The test
now signs them and checks each answer is `202 coalesced` and the spool holds
the group before the stop. Upstream without 0007 passed 71 of 71.

## 0008: a remote dashboard's READY wait runs on the remote, not as one ssh login a second

Cause, measured on 2 Nov: the box's journal showed about 40 separate ssh
logins from One between 2:27:38 and 2:28:18 PM, which made sshd,
systemd-logind and systemd its busiest sources. In that window two remote
dashboards were booting (One's desktop.log: spawned 2:27:37 and 2:27:44 PM,
bound their ports 2:28:08 and 2:28:10 PM). While one boots,
`scrapeReadyPort` (electron/remote-lifecycle.ts) re-read its spawn log with a
new `cat` exec every 750 ms, and Windows OpenSSH has no ControlMaster, so on
One every exec is its own login. Boots took 26 to 58 s that afternoon.

Fix: one exec waits on the remote for up to 10 s until the log names a port
(grep only gates the wait; the Desktop's READY regex still decides), then
prints the log. A boot costs one login per 10 s slice; the deadline is
unchanged. `0008-desktop-ready-wait-on-remote.patch` applies on 0006 (One's
v0.22.5 build); `0008-desktop-ready-wait-on-remote-upstream-main.patch` on
upstream c305aedf.

Tests (Linux, Node 26, 2 Nov): `npx vitest run electron/remote-ready-wait.test.ts
electron/remote-lifecycle.test.ts electron/remote-lifecycle-profile-missing.test.ts`,
105 passed, 1 skipped (the existing zsh probe), on both bases. The new file
runs the real remote command through sh against a temp log. The wrapped
command was also run through bash and dash as the login shell. eslint is
clean, and upstream's code-health ratchet passes.

## 0009: a source switch resolves the target's address in its dial, not in its commit

Cause, measured on 2 Nov in One's desktop.log: the switch to
thornswood-bots/bramble moved the active route at 2:19:09.182 PM, published
no connection, and gave up at 2:19:29.350 PM (exactly 20 s) with "Connection
"thornswood-bots" did not become active". The ssh tunnel had dropped at
2:18:50 PM when #94's install restarted Bramble, and the two remote
dashboards bound at 2:19:35 and 2:19:36 PM. The same switch at 2:31:37 PM
published in 407 ms. In `selectConnection` (store/connections.ts) the dial
(phase 1, 135 s budget) found the socket open, then the commit
(`ensureGatewayAgent`, 20 s budget, after the old bindings are severed)
looked up the target's descriptor through `getConnectionFor`, which waited
on main rebuilding that route's backend. The commit lands only once that
descriptor is published (`targetIsActive`), so it timed out.

Fix: `openGatewayAgent` resolves the descriptor together with the socket,
on the dial's budget, and returns it; a failed lookup fails the dial before
anything is touched. `selectConnection` hands it to `ensureGatewayAgent`,
which publishes it with no lookup of its own. Callers that pass no
descriptor (the SDK and session doors) look it up as before. No budget
changed. `0009-desktop-switch-descriptor-in-dial.patch` applies on 0008
(One's v0.22.5 build). Upstream main rewrote this flow on 1 Nov (13bd44e4a4,
the commit now has the 135 s budget, so it waits instead of failing, but
still with the old bindings severed); the upstream copy is made when an
upstream pull request slot frees.

Tests (Linux, Node 26, 2 Nov): the new
`src/store/profile-agent-dial-descriptor.test.ts` (5 tests) and one new
test in `src/store/connections.test.ts` fail without the fix (5 failed) and
pass with it. The whole Desktop suite, `npx vitest run src`: 1,343 files,
11,534 tests passed. `tsc -p tsconfig.json --noEmit` and eslint clean.

## 0010: the Bot Screen watcher reads each home under its own terminal policy

Made against upstream main `013bde47` (2 Nov 2026), in `on-main/`. The file
it changes is the same in v0.22.5, so it applies there too.

Cause of the container burst (1 Nov, 3:50:00 PM). Bramble's Bot Screen came
up at 3:49:59 PM. Within 0.4 s, five new containers labelled
`profile_bramble`, each mounting Bramble's home but another Bot's work folder
at `/workspace`, and one local environment were built. There was one build
in each serve process on the VM that watched Bramble's home, and all of them
logged to Bramble's `agent.log`. The serve backend's watcher
(`tui_gateway/methods_display_watch.py`) re-reads every served home's screen
when its files move, and every 30 s it checks for idle screens. It did both
under HERMES_HOME alone. The status read re-attaches a sandbox-hosted screen
(`runtime._owned_sandbox_env`, which acquires a terminal environment), so it
ran on the LAUNCH profile's terminal policy. Each process built a container
keyed and mounted as Bramble but rooted at its own Bot's `terminal.cwd`.

The fix binds the watched home's full runtime scope (home, secrets, terminal)
for each poll body. That is what `@_profile_scoped` binds for a `display.*`
call, and what `methods_profiles._hermes_home_scope` already binds for config
reads (upstream #120726). The re-attach then finds the profile's own
container. Two new tests in `tests/tui_gateway/test_display_watch.py` cover
the runtime-file poll and the idle check: each body sees the watched home's
own `TERMINAL_CWD`, not the launch profile's. The launch home keeps its frozen
launch environment, and its HERMES_HOME is still bound explicitly: in
single-profile mode the launch scope binds secrets only, and the watcher
thread's own context carries no home override, so without it the three
existing tests for a screen starting or stopping read the process home.

```
git am on-main/0010-display-watch-profile-scope.patch
python -m pm.build_env --source . --out /venv --group dev --group test --extra messaging
HERMES_PYTHON=/venv/bin/python scripts/run_tests.sh tests/tui_gateway/test_display_watch.py tests/tui_gateway/test_display_methods.py -q -rfE
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

## Upstream's lint on the five open pull requests

Upstream's blocking "code health ratchet" (scripts/code_health) failed four
of the five pull requests opened on 1 Nov, as its reviewer said, and failed
0006 too. Reproduced here with upstream's own `python scripts/check --only
health --base HEAD^1` on each branch merged into upstream main:

- #147814 (0002): agent_runtime_helpers.py grew past its cap, and
  ratelimit_headers caught every exception.
- #147816 (0004): auxiliary_client.py and test_auxiliary_client.py grew past
  their caps.
- #147834 (0001): the env=None backend lookup caught every exception.
- #147835 (0005): use-session-actions/utils.ts grew past its cap.
- 0006: electron/main.ts grew past its cap.

The follow-up commits in `followups/` fix each one at its cause (the one
exception the code can raise is caught by name; new code goes in a topical
sibling and existing code moves with it, as upstream's fix text asks). They
apply on the fork branches as they are and go there as ordinary commits. On
upstream main 8819d883 all five then pass the ratchet. Tests: tool result
storage 60 passed; the rate-limit and error-context suites 371 passed; the
auxiliary client suites 294 passed and 1 failed (the Anthropic messages
adapter test, which fails the same way on upstream main here because the
anthropic package isn't installed); session-actions, relay and chat 1,857
passed; cron union and registry sources 11 passed; `tsc -p
tsconfig.electron.json --noEmit` and eslint clean.

Why `gh pr create` was refused for 0006, found 2 Nov: GitHub's compare
page, opened in the browser, says "You've reached the open pull request
limit for this repository. Users without write access can have up to 5 pull
requests open at a time." EvilPatrick06 has five open there (#147814,
#147815, #147816, #147834, #147835). gh's "does not have the correct
permissions to execute `CreatePullRequest`" and REST's `404 Not Found` were
that limit, worded badly. The earlier check that ruled out a five-PR cap was
wrong: the accounts it counted with more than five open must have write
access, which it didn't check. The fork branch is ready (the follow-up
commit included), so 0006 opens with the same title and body once one of
the five merges or closes. 0007 and 0010 wait behind it the same way.

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
- 0005 (Linux, Node 22, 2 Nov, on 0001 to 0004): `vitest run src/plugins/hermes-bots/` 107 files, 873 passed, 0 failed (2 new; the first fails without the fix), and `tsc --noEmit` is clean.
- 0005's second commit (Linux, Node 22, 2 Nov): resolve-stored-session.test.ts
  24 passed (3 new; the first two fail without the fix). `vitest run` over
  src/app/session, src/store, src/app/contrib and src/plugins/hermes-bots:
  4,581 passed and 3 failed. The 3 are dead-session-prune's debounce test and
  two voice-prefs tests, and they fail the same way on v0.22.5 without any
  of these patches. `tsc -p . --noEmit` and eslint on the changed files are
  clean.
- 0006 (Linux, Node 26, 2 Nov, on 0001 to 0005): the five suites above, 57
  files, 447 passed, 0 failed (the new cron-job-owner test fails 8 of 9
  without the fix); `tsc -p . --noEmit`, `tsc -p tsconfig.electron.json
  --noEmit` and eslint on the changed files are clean. The upstream-main
  patch on 4d26e0cc (Linux, Node 26, 2 Nov): the same suites, 62 files, 478
  passed, 0 failed; both tsc runs clean. Rebased on f0d94882 (2 Nov, 11 AM):
  67 files, 492 passed, 0 failed; both tsc runs and eslint clean.
