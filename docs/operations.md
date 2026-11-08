# Running it

Once the first install is done ([setup.md](setup.md)), the VM looks after itself: it installs each merge to `main`, backs itself up every night and checks its own limits at every boot. This page is for watching it and stepping in.

## Updates

`tw-follow-main.timer` runs the follower every ten minutes. [architecture.md](architecture.md) has its steps. A new version reaches the Bots like this:

1. A pull request merges to `main`.
2. Within ten minutes the follower fetches it, runs the whole suite on the VM as `tw-ci` with no network, and installs nothing if a test fails.
3. It installs the release, applies it to the six profiles and the gateway's own profile, reloads every Bot process, and runs the canary.
4. If any step fails, it puts the release that ran before back the same way and says both. A commit that failed is not tried again: the fix is the next merge.

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

- checks the profile, its `SOUL.md` and its two Thornswood skills are there, and installs the skills this repository owns from `skills/`;
- builds the Bot's config from the policy and sets those keys in `config.yaml`, leaving every other key as Hermes has it (`model` and `fallback_providers` are replaced whole);
- checks the result can reach no route the Bot may not use;
- reads the names, never the values, in every `.env`, and looks for key and signing files by name;
- makes sure Hermes's sandbox image is in the `hermes` user's podman store;
- installs and enables the `thornswood_guard` and `thornswood_live` plugins, and makes the Bot's working folder, `/home/hermes/tw-work/<bot>`;
- loads the installed guard and checks it refuses a push, a merge, a secret read, a config edit, a shared skill edit and code execution, and allows `git status`.

It checks everything first and writes nothing if any check fails; `--check` only reports. It records the applied commit in each profile's `local/thornswood-hermes/applied.json`.

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
| The brokers and command wrappers | The system `python3` with `-I`, importing only from `/opt/thornswood-hermes/current` |
