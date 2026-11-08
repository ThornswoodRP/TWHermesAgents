# Setting up a VM

This page covers a VM's first install and the one-time settings it needs. Once it is done, the VM installs every merge to `main` by itself; [operations.md](operations.md) covers running it from there.

## What the VM needs

- Ubuntu 24.04 with its system Python 3.12 and PyYAML. `apply.py`, the follower and the test suite run with exactly that `python3`.
- Hermes Agent installed for the `hermes` user, with the six profiles (`bramble`, `rift`, `sprocket`, `echo`, `magpie`, `wisp`) already made, each with its `SOUL.md` and the `thornswood-ops` and `thornswood-guide` skills. `apply.py` checks they are there and never makes one.
- Rootless podman for the `hermes` user, with the cpu, memory and pids cgroup controllers delegated to it.
- Tailscale, with the VM reachable from the machine you install from under the ssh name `thornswood-bots`, and sudo on the VM.
- The two GitHub Apps' private keys at the paths `policy.yaml` `brokers.apps` names, under `/etc/thornswood/apps/`. The install makes them `tw-git`'s alone, mode 400, and stops if one is missing.

The guard plugin runs inside Hermes's own Python, which has no YAML library, so it reads `policy.json` and needs nothing outside the standard library.

## The first install

The first install is the one that brings `tw-follow-main`, so it is sent from a machine on the tailnet. Every install after it, the VM does itself.

From a clone of this repository at `main`, with the full commit `main` is at:

```bash
scripts/install-on-oracle.sh <commit> --move-gateway --quotas
```

It refuses unless that commit is `main`'s tip. It bundles the commit, copies it to the VM, and there root unpacks it into `/opt/thornswood-hermes/releases/<commit>` and runs `deploy/install.sh`, which:

- makes the broker users and groups and `/srv/tw`;
- moves the Apps' keys so only `tw-git` can read them, and moves any model route's address and token into `/etc/thornswood/proxy.env`, out of the `hermes` user's reach;
- puts the units, slices, check helper socket and SFTP jails in place;
- with `--quotas`, turns on group quotas for the filesystem holding `/srv/tw` (ext4) and sets each Bot's disk cap;
- with `--move-gateway`, moves the Hermes gateway into `tw-hermes-gateway.service` in `tw-hermes.slice`;
- points `/opt/thornswood-hermes/current` at the release and starts the brokers.

Anything it cannot do safely is a `STOP` line saying what is missing, and nothing after it runs. Run it again after fixing the cause; it is safe to repeat.

Then apply the profiles with the same commit:

```bash
scripts/send-to-oracle.sh <commit>
```

Finally, run the canary on the VM. Every line must be `PASS`:

```bash
sudo /opt/thornswood-hermes/bin/tw-canary
```

## The VM's read key

The install makes the VM its own read-only deploy key, `/etc/thornswood/hermes-read-key`, and prints its public half on a line that starts `the VM's read key for`. Add that line to this repository on GitHub once:

1. **Settings**, then **Deploy keys**, then **Add deploy key**.
2. Title `thornswood-bots`, and the printed line as the key.
3. Leave **Allow write access** off, then **Add key**.

Until it is added, each run of the follower says `GitHub refused the VM's read key` with the line to add.

## The Google Drive sign-in

The off-site copies need a Drive sign-in that belongs to `tw-offsite` alone. On the VM:

```bash
sudo -u tw-offsite rclone config --config /var/lib/tw-offsite/rclone.conf
```

Then, following [rclone's remote setup](https://rclone.org/remote_setup/) for a machine with no browser:

1. `n` for a new remote, named `thornswood-gdrive`, of type Google Drive.
2. Leave the client id and secret empty, and pick the scope `drive.file`, so rclone sees only the files it made.
3. Leave the service account empty, `n` for advanced config, and `n` to "Use web browser to automatically authenticate".
4. It prints an `rclone authorize "drive" "..."` line. Run that line on any computer with rclone and a browser, sign in to Google, and paste the token it prints back into the VM.
5. `n` for a shared drive, and `y` to keep it.

Until then each off-site run says `no Drive sign-in`.

## The decision core

The decision core installs from the release the follower put in place. Once it is installed, the follower installs it again at every merge, so these steps are needed once.

1. Wait for the VM to be on the commit: `readlink -f /opt/thornswood-hermes/current` ends in it.
2. Install the core, with the Workbench room's id (`scripts/check-hermes-contract.py` prints it):

   ```bash
   sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/install.sh <commit> --room <Workbench room id>
   ```

3. Link the Desktop pane's backend into Patrick's own Hermes (the `patrick-hermes` user). It never touches the Bots' Hermes:

   ```bash
   sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/install-plugin.sh <commit>
   ```

4. As `patrick-hermes`, turn the plugin on:

   ```bash
   hermes plugins enable thornswood-decisions
   ```

5. On the machine Hermes Desktop runs on, copy `plugins/thornswood-decisions/desktop/plugin.js` into `desktop-plugins/thornswood-decisions/`, then turn it on in **Capabilities**, **Plugins**.
6. Make the first pairing code and type it into the pane within 10 minutes:

   ```bash
   sudo -u tw-decide /opt/thornswood-hermes/bin/tw-decide --db /srv/tw/decide/core.db pair --workspace thornswood --kind desktop --label Desktop
   ```

### The phone app

The phone reaches the core through Tailscale Serve, so the app gets a real HTTPS origin on the tailnet.

1. In the Tailscale admin console, on the DNS page, turn on MagicDNS, then **Enable HTTPS** under HTTPS Certificates. The VM's name goes into the public certificate log.
2. On the VM, serve the core on 443:

   ```bash
   sudo tailscale serve --bg --https=443 127.0.0.1:9135
   ```

3. Run step 2 above again with the origin added:

   ```bash
   sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/install.sh <commit> --room <Workbench room id> --origin https://<vm name>.<tailnet>.ts.net
   ```

4. On the phone, with Tailscale on, open `https://<vm name>.<tailnet>.ts.net/m/` and pair it with a code from a device that is already signed in, or one made on the VM with `pair --kind phone`. Then add a passkey from Settings, and add the app to the home screen.

`tailscale serve status` shows what is served, and this takes it away:

```bash
sudo tailscale serve --bg --https=443 off
```

Every feature switch in a new workspace starts off. With them off the pane and the app pair and read, and an answer is refused with "Answers are off for now". [decision-app.md](decision-app.md) lists the switches.

### Patrick's own Hermes on Windows

To chat with his Hermes on his Windows machine from the app, run this once from a clone of this repository at `main`, in PowerShell:

```powershell
.\scripts\one\Install-OwnConnector.ps1
```

It has the VM make the connector's token over ssh straight into his own profile, copies the connector into `%LOCALAPPDATA%\Thornswood-Hermes\own-connector`, runs one round to check it, and adds a sign-in task that starts it under `--supervise`. From then on the connector keeps itself on the release the VM runs.

## Checking Hermes against the pinned contract

`thornswood_decisions/contracts.json` records every fact the core relies on in Hermes, each with the file and line it was read from at the pinned Hermes commit. To check the VM's Hermes against it, on the VM:

```bash
sudo -u hermes bash -c 'cd /home/hermes/thornswood-hermes/current && python3 -I scripts/check-hermes-contract.py'
```

It prints a `PASS` or `STOP` line for the Hermes commit, the six profiles and the one Workbench room, then the facts it read. Any `STOP` exits 1. It reads only and changes nothing.
