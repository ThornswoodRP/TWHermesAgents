# Fixes for Hermes itself

Some problems the Bots hit are caused in [Hermes Agent](https://github.com/NousResearch/hermes-agent), not in this repository. Those are fixed where they come from: as patches to Hermes, sent upstream as pull requests. Nothing here is applied to the VM's Hermes by hand; the VM takes a fix in a Hermes release.

## Layout

| Folder | What it holds |
|---|---|
| `v0.22.5/` | The patches made against Hermes v0.22.5 (`bf0203a794a07aac3ff764972ab9087baa4e4f4e`). They apply in number order |
| `main/` | The patches remade on upstream `main`, where upstream had changed the same lines. 0002, 0003 and 0004 apply to upstream `main` as they are, so they have no copy here |
| `followups/` | Commits for the upstream pull request branches that answer upstream's code health check, each at its cause |

To apply the v0.22.5 series on a checkout of that commit:

```bash
git am v0.22.5/*.patch
```

## On v0.23.0

The VM and Patrick's Desktop run Hermes v0.23.0 (`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`), the version `src/thornswood_decisions/contracts.json` pins. None of these fixes is in v0.23.0 yet. Applied one after another with `git am --3way`, these apply on it as they are:

```bash
git am --3way main/0001-spill-path-idle-sandbox.patch v0.22.5/0002-ratelimit-headers-logged.patch v0.22.5/0003-spill-fallback-posix-path.patch v0.22.5/0004-aux-codex-pick-by-model.patch v0.22.5/0005-desktop-relay-local-source-once-it-signals.patch main/0006-desktop-scheduled-jobs-every-gateway.patch v0.22.5/0008-desktop-ready-wait-on-remote.patch main/0007-webhook-coalesce-survives-restart.patch main/0010-display-watch-profile-scope.patch
```

The v0.22.5 copies of 0001 and 0006 do not apply there, because v0.23.0 already has the upstream code their `main/` copies were made for. Neither does 0009: v0.23.0 has upstream's rewrite of that flow.

## The patches

| # | Fixes | Upstream pull request | Base for `main/` |
|---|---|---|---|
| 0001 | The "too large" note names a path the Bot can read | #147834 | `5aa8df74` |
| 0002 | A 429 records which rate limit refused it | #147814 | applies as is |
| 0003 | The sandbox fallback path is POSIX on every host | #147815 | applies as is |
| 0004 | An auxiliary Codex call picks the pool entry for its own model | #147816 | applies as is |
| 0005 | Desktop relays This device's Bot Chat when its main connection is remote, and reads it on its own connection | #147835 | not remade |
| 0006 | The Scheduled jobs list shows every connected gateway's jobs, and each job's buttons go to its own gateway | not opened yet | `f0d94882` |
| 0007 | A coalesced webhook delivery survives a gateway restart | not opened yet | `013bde47` (only on `main`) |
| 0008 | A remote dashboard's READY wait runs on the remote, not as one ssh login a second | not opened yet | `c305aedf` |
| 0009 | A source switch resolves the target's address in its dial, not in its commit | not opened yet | not remade |
| 0010 | The Bot Screen watcher reads each home under its own terminal policy | not opened yet | `013bde47` (only on `main`; the file is the same in v0.22.5) |

GitHub allows an account without write access five open pull requests in a repository, so the later patches open as the earlier ones merge or close.

## 0001: the "too large" note names a path the Bot can read

**Cause.** Under the docker backend a Bot's `read_file` and terminal run in its container, but Hermes passed the sandbox registered for the turn, which is none once the idle reaper drops it after 5 minutes (the container itself lives on). Hermes read "no sandbox" as "runs on the host", so the note named `$HERMES_HOME/cache/spillover/<id>.txt`, a path that does not exist in the container.

**Fix.** Bring the sandbox back (`ensure_task_env`, as vision already does) before saving, and treat "no sandbox" as host side only on the local backend. On upstream `main`, which asks `env_uses_host_filesystem(env)` for a live environment, the fix keeps that check and changes only the `env=None` case.

The note then names `/root/.hermes/cache/spillover/<id>.txt` inside the container. The Thornswood guard refuses any path with `.hermes` in it, so this alone does not let a Bot read the file; that waits on each Bot's container mounting only its own caches.

## 0002: a 429 records which rate limit refused it

**Cause.** Hermes read only the reset time from a 429's headers, so which window refused the call (a subscription's 5-hour or weekly window, or a per-minute bucket) was never written down.

**Fix.** Keep every `anthropic-ratelimit-*`, `x-ratelimit-*` and `retry-after` header of a refusal (counters, statuses and times, never a credential) and log them in one line.

## 0003: the sandbox fallback path is POSIX on every host

**Cause.** When a terminal environment names no temp dir, Hermes writes an oversized result inside it at `STORAGE_DIR`, which came from the host's `tempfile.gettempdir()`. On Windows that is a backslash path the environment's POSIX shell cannot use.

**Fix.** Make it `/tmp/hermes-results`, the same default as `BaseEnvironment.get_temp_dir()`, with a test that holds it.

## 0004: an auxiliary Codex call picks the pool entry for its own model

**Cause.** The auxiliary Codex resolver knows the model it will call but asked the pool for an entry without naming one. An unscoped pick honours every model cooldown on an entry, and a Codex entitlement bench lasts a year. An entry benched for models the account was refused came back empty for a model it was never refused, so `auxiliary_client` found no token.

**Fix.** Pass the model to the pick in both Codex callers. The year-long bench on a model the account really cannot use is upstream's design and stays; it now benches only that model. A pick with no model is the unscoped `select()` it always was, so no other provider's path changes.

## 0005: Desktop relays This device's Bot Chat when its main connection is remote

Two commits: the relay, then the lookups.

**Cause, the relay.** Upstream made `relayEligibleRoutes` (`apps/desktop/src/plugins/hermes-bots/relay.ts`) drop the local "This device" source whenever a remote connection is the Desktop's primary, so an unused local runtime is never dialed. A Desktop whose primary is remote can still run a Hermes on this device, whose agents queue `message_agent` envelopes in the local outbox and signal `bot_relay.outbox.pending`. The relay skipped that route anyway, so nothing claimed the envelopes and they sat until their waiters timed out.

**Fix.** Once the local source's own backend signals outbox mail, the relay keeps it as a peer like any other. Until it signals, nothing changes. A signal from the primary, or an untagged one, does not count.

**Cause, the lookups.** Desktop looked This device's Bot Chat up under every remote Bot's name. `probeStoredSession` took an owner only from its caller, so even a chat whose exact owner the store already knew went down the cross-profile ladder, and each remote Bot's name was sent to the chat's real connection, `local`, which has none of them.

**Fix.** The probe starts from the same owner ladder session calls use. An exact owner found there is read once, and a miss there is final. A chat with no known owner walks the profiles as before.

```bash
cd apps/desktop
npm ci --ignore-scripts
npx vitest run src/plugins/hermes-bots/ src/app/session/hooks/use-session-actions/resolve-stored-session.test.ts
```

## 0006: the Scheduled jobs list shows every connected gateway's jobs

**Cause.** The Scheduled jobs page and the sidebar's Cron jobs section asked only the connection in front (`getCronJobs` in `apps/desktop/src/api/cron.ts` is `connectionScoped()`). Jobs on another gateway showed only with that gateway in front, and every per-job call (runs, edit, pause, resume, run now, delete) went to the connection in front, so a job from another gateway could not be worked even once listed.

**Fix.** The all-profiles list now joins every connected gateway's jobs in the main process, the way the unified Sessions list joins their chats (`electron/cron-job-union.ts`). Only already-pooled backends are read, so a refresh never dials one; a gateway is read once; a dead one adds nothing; an error from the connection in front is never hidden. Each job from another gateway carries the `connection_id` that served it, every per-job call goes to that connection under the job's own profile, and rows are keyed by connection and id. On upstream `main`, Stop for a running job also goes to the job's own gateway.

```bash
cd apps/desktop
npm ci --ignore-scripts
npx vitest run electron/cron-job-union.test.ts src/api/cron-job-owner.test.ts src/store/cron.test.ts src/app/cron src/app/chat/sidebar
```

## 0007: a coalesced webhook delivery survives a gateway restart

**Cause.** A coalesced delivery is answered `202 coalesced` before any admission, and GitHub does not resend an answered delivery, so the coalescer's buffer is its only copy. On a stop, `drain_gateway_runtime` withdraws every adapter's message handler before the adapters disconnect. The webhook adapter's disconnect flush then admitted each pending group against the withdrawn ingress, which raised `RuntimeStoreError('not_found')`, and the group was gone. A hard kill lost the buffer outright.

**Fix.** Keep each pending group in a spool in the gateway's home (`webhook_coalesce_pending.json`, 0600, written atomically) until its admission commits. The route's config is never written, since it can hold the HMAC secret; it is read back from the live routes. While ingress is withdrawn a group stays spooled, and when the next run's listener is up every spooled group is admitted as usual. A route that is gone is dropped and named, and an unreadable spool is set aside.

```bash
git am main/0007-webhook-coalesce-survives-restart.patch
python -m pm.build_env --source . --out /venv --group dev --group test --extra messaging
HERMES_PYTHON=/venv/bin/python scripts/run_tests.sh tests/gateway/test_webhook_coalescing.py tests/gateway/test_webhook_adapter.py tests/gateway/test_webhook_authority.py -q -rfE
```

## 0008: a remote dashboard's READY wait runs on the remote

**Cause.** While a remote dashboard boots, `scrapeReadyPort` (`electron/remote-lifecycle.ts`) re-read its spawn log with a new `cat` exec every 750 ms. Windows OpenSSH has no ControlMaster, so every exec is its own ssh login: about 40 logins in 40 seconds for one boot, which made sshd and systemd-logind the remote's busiest processes.

**Fix.** One exec waits on the remote for up to 10 seconds until the log names a port (grep only gates the wait; the Desktop's READY pattern still decides), then prints the log. A boot costs one login per 10 seconds, and the deadline is unchanged. The v0.22.5 copy applies on 0006.

## 0009: a source switch resolves the target's address in its dial

**Cause.** In `selectConnection` (`store/connections.ts`) the dial (135 second budget) found the socket open, then the commit (20 second budget, after the old bindings are severed) looked up the target's descriptor through `getConnectionFor`, which waited on main rebuilding that route's backend. The commit lands only once that descriptor is published, so a switch right after a gateway restart timed out at exactly 20 seconds.

**Fix.** `openGatewayAgent` resolves the descriptor together with the socket, on the dial's budget, and returns it; a failed lookup fails the dial before anything is touched. `selectConnection` hands it to `ensureGatewayAgent`, which publishes it with no lookup of its own. No budget changed. It applies on 0008. Upstream `main` has since rewritten this flow, so its copy is made when it is opened.

## 0010: the Bot Screen watcher reads each home under its own terminal policy

**Cause.** The serve backend's watcher (`tui_gateway/methods_display_watch.py`) re-reads every served home's screen when its files move, and checks for idle screens every 30 seconds, under `HERMES_HOME` alone. The status read re-attaches a sandbox-hosted screen, which acquires a terminal environment, so it ran on the launch profile's terminal policy. Each serve process built a container keyed and mounted as one Bot but rooted at its own Bot's working folder: five stray containers within half a second of one Bot Screen opening.

**Fix.** Bind the watched home's full runtime scope (home, secrets, terminal) for each poll, as `@_profile_scoped` does for a `display.*` call. The re-attach then finds the profile's own container.

```bash
git am main/0010-display-watch-profile-scope.patch
python -m pm.build_env --source . --out /venv --group dev --group test --extra messaging
HERMES_PYTHON=/venv/bin/python scripts/run_tests.sh tests/tui_gateway/test_display_watch.py tests/tui_gateway/test_display_methods.py -q -rfE
```

## The follow-up commits

Upstream's blocking code health check failed four of the first five pull requests, and 0006: a file grew past its size cap, or new code caught every exception. Each commit in `followups/` fixes one at its cause: the one exception the code can raise is caught by name, and new code goes in a topical sibling file with the existing code it belongs with. They go on the upstream pull request branches as ordinary commits.

| Follow-up | For |
|---|---|
| `spill-path-idle-sandbox.patch` | 0001 |
| `ratelimit-headers-logged.patch` | 0002 |
| `aux-codex-pick-by-model.patch` | 0004 |
| `desktop-local-source-relay.patch` | 0005 |
| `desktop-cron-every-gateway.patch` | 0006 |
