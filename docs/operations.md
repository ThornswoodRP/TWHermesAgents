# Running it

The free-worker wake keeps each observed opportunity until the decision
core confirms its assignment was sent, or a fresh eligible-board read proves
no work is available for that worker. A completed Bramble sweep alone does
not settle it. Queued or leased deliveries remain in flight. At the next
five-minute check, idle unresolved workers share one complete board snapshot,
with each field filtered before the result limit. Running or queued work
coalesces the check. The existing admission caps, work hours and explicit
pause or Stop still apply. Source failures retain observations and defer
passive retries for an hour without a model turn for the failure itself.

A cancelled or uncertain receipt is reviewed by its canonical task reference,
never replayed. That exact issue is excluded from fresh eligibility while a
different issue remains available. Its receipt hold survives a no-work result
and is rechecked by exact ID in rotating batches of at most 32. A newer
explicit assignment for the same task transfers the hold to that receipt;
confirmed delivery releases it. An incomplete legacy reference is
reported as ambiguous and suppresses generic continuation of an unidentified
old task. It does not pause that worker or bar a fresh checked eligible issue.
These summaries contain identifiers,
status and timestamps, never briefs or chat. This is delivery bookkeeping,
never proof of task completion.

An incremental private receipt scan reads at most four pages of 32 unseen
non-sent assignments per check, so multiple outcomes between ticks are kept
even when they belong to the same worker. Larger catch-up scans continue at
the next check before an eligibility conclusion. Exact issue references are
deduplicated and filtered against the actual board snapshot, under the
existing IPC message-size bound. Only a board item explicitly read CLOSED
releases a closed-issue hold; absent or redacted items do not. Nullable unset
project fields are legitimate non-Todo items, while a missing selected field
is incomplete source data. Fresh assignment hints name a finite set of
checked issue references. A bounded tail scan suppresses the hint and keeps
the observation if new receipt outcomes arrived during the board read.

Once the first install is done ([setup.md](setup.md)), the VM looks after itself: it installs each merge to `main`, backs itself up every night and checks its own limits at every boot. This page is for watching it and stepping in.

## Updates

`tw-follow-main.timer` runs the follower every ten minutes. [architecture.md](architecture.md) has its steps. A new version reaches the Bots like this:

1. A pull request merges to `main`.
2. Within ten minutes the follower fetches it, runs the whole suite on the VM as `tw-ci` with no network, and installs nothing if a test fails.
3. It works out which processes load anything the release changed (`thornswood_brokers.restart_plan`: each one's modules followed through their imports, the files beside them, and its launcher and unit). The brokers start again only when something they load changed. The connector keeps running when nothing it or the brokers load changed and the core's store does not migrate. When only the connector's own code changed, it starts again and its Bot Chat turns do not: its unit stops only the connector (`KillMode=process`), each turn runs under `turn_run.py` writing its stream into a file in `~/.local/state/thornswood/connector-turns`, and the connector that starts next follows each turn to its end and says what came of it (#163). In both cases new turns are not held at all. Only when the brokers start again without systemd holding their sockets, or the store migrates, does it hold new turns and wait for the running ones to end, as before. It installs the release and applies it to the six profiles and the gateway's own profile. It reloads every Bot process only when the apply changed a file a running Bot loads (its config, or a file of a plugin it imports) or the install changed the gateway's unit. A reload's own hold waits only for the turns in the gateway and Desktop's sessions, which it stops; the connector's turns go on and keep their tickets. The reload does not hold and wait again after the follower's hold (`tw-reload-bots --held`). Skills, the owed-wake script and cron jobs are read fresh each time they are used. Then it runs the canary and ends any hold.
4. If any step fails, it puts the release that ran before back the same way and says both. A commit that failed is not tried again: the fix is the next merge.

### Updating the Hermes runtime

The follower updates this repository, not Hermes Agent itself. A runtime update is a separate maintenance operation: review the official release and its exact source against `src/thornswood_decisions/contracts.json`, update that pin in a pull request, and pass both guard rail suites before applying either part. A new contract must not reach the old runtime first.

For v0.23.2 the official release resolves to `ae2a81ccc00e9b7c06301523e5bb09608397149c`. This release passes the requested Codex model to credential selection; a limit on another model no longer blocks the healthy fallback (#142). It preserves the actual model's cooldown and an account-wide refusal. The Board's ended-session reconciliation is a separate fix (#220); this release is not evidence that upstream terminal failures now call every end hook.

Before the maintenance window, read the installed commit and clean source status, the normal follower's result and whether it is running, and the count of actual running turns. As the `hermes` user, inspect the supported read-only plan:

```bash
/home/hermes/.local/bin/hermes update --plan --channel stable
```

Finish any follower install first. Record whether `follow-main.paused` already existed, then pause the follower as above before making the pin ready to merge. Use the policy broker's normal `hold_turns` operation with `spare_connector: false`, and poll `turns_running` until zero. Do not interrupt a running turn when a wait expires: end this maintenance attempt and preserve its work. A hold has an expiry; require it to remain valid throughout the update and reload.

The normal `hermes update --channel stable --yes --no-gateway-restart` follows a mutable release. For an exact reviewed pin, use the official installer's documented `--commit` path over the existing source install ([upstream updating instructions](https://github.com/NousResearch/hermes-agent/blob/ae2a81ccc00e9b7c06301523e5bb09608397149c/website/docs/getting-started/updating.md#updating)). Download `scripts/install.sh` from that immutable commit, verify SHA256 `c73dad4dcf7c9ecadb98578e28cc763d23d7be3fa1b513545319038795493214`, and run the verified local file as `hermes` while held:

```bash
bash /path/to/verified-install-v0232.sh --commit ae2a81ccc00e9b7c06301523e5bb09608397149c --dir /home/hermes/.hermes/hermes-agent --hermes-home /home/hermes/.hermes --non-interactive
```

Do not pass `--channel`: the installer's final channel-setting command would run a mutable update. The explicit commit selects the source before hash-verified dependency and product installation. Noninteractive mode skips setup and gateway installation, leaving the fleet restart to the controlled reload. Check the installed checkout and `hermes --version` both agree with the reviewed commit afterward. Any different source is a stop. Keep all auth, credential cooldowns, chat stores, connector journals and uncertain-send records. Never replay an uncertain message to test recovery.

After the reviewed pin has merged, apply it through the normal follower and verify its offline tests, install, canary and applied commit. A runtime-only update still needs `tw-reload-bots --held` while the external hold is active, even if the follower finds no changed Bot config. Read back the gateway's actual running source, all six profile readiness states and the exact contract check. Only after compatibility, readiness and canary verification succeed, end the hold and restore the follower's previous pause state; a pre-existing pause stays paused. On failure retain the follower pause and admission containment, report the exact failed stage and remaining hold lifetime, and resolve it before the hold expires. Do not let a cleanup clause resume turns against a partial runtime. Verify a newly admitted, legitimate fallback turn independently of catalog discovery before calling work restored.

Before source movement, an aborted attempt can simply end the hold and restore the previous follower pause state after confirming the old runtime remains healthy. After movement, preserve the installer receipt and inspect the actual source and dependency state before doing anything else; do not assume the installer has the updater's automatic rollback guards. Restoring an old policy release does not restore Hermes. Do not restore a whole `HERMES_HOME` snapshot, reset cooldowns, or force a source downgrade across newer session state. A source/dependency rollback needs its own checked compatibility plan; otherwise keep the failed update contained and repair forward while retaining every chat and uncertain outcome.

### What it did

```bash
sudo cat /var/lib/thornswood/follow-main.json
```

```bash
journalctl -u tw-follow-main
```

Everything each step printed, pass or fail, is in `/var/lib/thornswood/follow-main/<commit>.log`, root's alone. The newest 20 are kept.

### Pausing it

While this file exists, the follower fetches and installs nothing:

```bash
sudo touch /etc/thornswood/follow-main.paused
```

To go on:

```bash
sudo rm /etc/thornswood/follow-main.paused
```

Do not pause it by stopping the timer: every install starts the timer again.

### Reloading the Bots by hand

Every Bot process loads its plugins and config when it starts and keeps them, so after an apply the follower ends the `hermes` user's login sessions and restarts the gateway. Hermes Desktop reconnects by itself. To do the same by hand:

```bash
ssh -t thornswood-bots sudo /opt/thornswood-hermes/bin/tw-reload-bots
```

### Going back to an earlier version

Every applied version stays in `/home/hermes/thornswood-hermes/releases/`. To apply an older one, on the VM as `hermes`, inside that release's folder:

```bash
python3 scripts/apply.py --expect <older commit>
```

Then reload the Bots as above. The lasting fix is a new pull request here.

### What `apply.py` does

For each of the six profiles under `/home/hermes/.hermes/profiles/`, and for the gateway's own profile, `apply.py`:

- checks the profile, its `SOUL.md` and its two Thornswood skills are there, and installs the skills this repository owns from `hermes/skills/`;
- builds the Bot's config from the policy and sets those keys in `config.yaml`, leaving every other key as Hermes has it (`model` and `fallback_providers` are replaced whole);
- checks the result can reach no route the Bot may not use;
- reads the names, never the values, in every `.env`, and looks for key and signing files by name;
- makes sure Hermes's sandbox image is in the `hermes` user's podman store;
- installs and enables the `thornswood_guard` and `thornswood_live` plugins, and makes the Bot's working folder, `/home/hermes/tw-work/<bot>`;
- loads the installed guard and checks it refuses a push, a merge, a secret read, a config edit, a shared skill edit and code execution, and allows `git status`.

It checks everything first and writes nothing if any check fails; `--check` only reports. It records the applied commit in each profile's `local/thornswood-hermes/applied.json`.

The GPT6.2Sol upgrade also changes explicit auxiliary helpers on the Bot's old `openai-codex/gpt-6.1-sol` route to `gpt-6.2-sol`, when that new route is in its policy chain. Other helper settings remain and are still checked. The gateway, which has no Codex fallback, gains no route through this migration.

The Bots' connector watches each local chat viewer for fifteen minutes. The gateway owns the accepted admission and can keep working after that viewer detaches. When the helper has reported its own exact submission receipt, that deadline confirms delivery, without claiming the turn finished or stopping its work. An init line, tool progress or an admission without that receipt does not prove delivery; conflicting receipts stay uncertain. A message left uncertain is never replayed automatically. Patrick's Stop and the worker hours are unchanged.

## The canary

After each install the canary attacks the live VM with planted canary files: reading secrets, breaking the caps, escaping a container, and reaching a listener with no sign-in. It prints a `PASS` or `FAIL` line for each. On the VM:

```bash
sudo /opt/thornswood-hermes/bin/tw-canary
```

## Backups

| Mountain time | What runs | Kept |
|---|---|---|
| Every hour | `tw-snapshot`: a copy of `tw-policy`'s store | 24 hourly, 7 daily |
| 3:30 AM | `tw-backup`: one encrypted archive in `/srv/tw/backups` | 14 days on the VM |
| 4:30 AM | `tw-offsite`: the archives to `thornswood-gdrive:Thornswood-Hermes-Backups`, and the game box's nightly to `thornswood-gdrive:Thornswood-Box-Backups` | 90 days and 30 days on Drive |

Each archive is encrypted to Patrick's age public key and holds no sign-in, `.env` file or key. `tw-offsite` fails when the newest of either kind is over 26 hours old, so a missed night is a failure, never a silence.

### Proving an archive opens

The age key never goes on the VM. On the machine that holds it, with an archive downloaded from Drive:

```bash
age -d -i <key file> <archive> | tar -tz
```

### Restoring

From the machine that holds the age key, on the tailnet:

```bash
scripts/restore-on-oracle.sh <path to hermes-YYYYMMDD-HHMMSS.tar.gz.age>
```

It decrypts the archive there, copies it to a private folder on the VM, stops the gateway and the brokers, puts the files back with `tw-backup restore` (which refuses an archive holding anything it should not), kills every Main, publish and cutover decision record so none can be used again, starts everything again and deletes the plain copy. Sign-ins, `.env` files and keys are never in a backup, so after a restore onto a new VM they are made again.

## Which Python runs what

| What | Python |
|---|---|
| `apply.py`, the follower and the tests | The VM's system `python3` (3.12, with PyYAML) |
| The guard and live plugins | Hermes's own runtime, under `/home/hermes/.hermes/tools/` with its environment in `/home/hermes/.hermes/installs/`. It has no YAML library, so the guard reads `policy.json` |
| The decision core and its workers | The core's own venv, `/opt/thornswood-hermes/decisions-venv`, with hash-pinned wheels from `deploy/decisions/requirements.txt` |
| The brokers and command wrappers | The system `python3` with `-I`, importing only from the release their link in `/opt/thornswood-hermes/bin` leads to |
