# Architecture

This page walks through every part of TWHermesAgents: where it runs, as which user, and how a Bot's request travels from a model turn to GitHub. [security.md](security.md) lists the rules each part enforces.

## The machines

| Machine | What runs there |
|---|---|
| **The Bots' VM** | An Ubuntu 24.04 VM (4 cores, 24 GB) on the tailnet. Hermes, the six Bot profiles, the brokers, the decision core, the backups and the follower. Everything in this repository is installed here |
| **The game box** | Thornswood's game servers. Nothing from this repository runs there. Its nightly backup is sent to the VM through an add-only drop |
| **Patrick's machines** | His own Hermes, Hermes Desktop and his phone. They reach the decision core over the tailnet |
| **GitHub** | The repositories the Bots work in, the two GitHub Apps they act through, and this repository's CI |

## Hermes and the six profiles

The Bots are six profiles in one Hermes install, under `/home/hermes/.hermes/profiles/<bot>`, all served by one gateway: `tw-hermes-gateway.service`, run as the `hermes` user inside `tw-hermes.slice`.

Each Bot's config is built from the policy by `src/thornswood_guard/profile_config.py` and set by `scripts/apply.py`. The generated copies in `hermes/profiles/` are what gets set, for reading in review. Every Bot has:

- the same model order: Claude (`claude-opus-5-5`) through Patrick's subscription sign-in first, then OpenAI Codex (`gpt-6.1-sol`). No provider that bills by use is allowed in a config;
- the toolsets it may never use turned off (browser, code execution, computer use, web search, image and video generation, Discord admin and the rest in `policy.yaml` `toolsets`), and for the five workers, cron as well;
- its terminal, file and code calls run in a rootless podman container (Hermes's docker backend) that sees only the Bot's own working folder at `/workspace`, with no network, one core and 2 GB;
- two plugins: `thornswood_guard` and `thornswood_live`;
- its skills: `thornswood-ops`, `thornswood-guide` and, for Bramble, `bramble-duties`.

`apply.py` never creates or deletes a profile and never touches a Bot's `SOUL.md`, memories, sessions, `auth.json` or any `.env`.

## The guard

`src/thornswood_guard/` is both a Python package and a Hermes plugin. Installed into each profile, it registers:

- a `pre_tool_call` hook (`tool_guard.py`) that Hermes runs before every tool a Bot calls. It refuses reads of Hermes's home (all but the folders where Hermes stages the files Patrick sends: a Bot may read a file inside `attachments`, `images` or `cache/documents`, `images`, `audio` or `videos`, named whole, which Hermes mounts read-only into its sandbox; #99), `.env` files, `auth.json`, process environments and the brokers' sockets; shell commands that push, merge or change git settings that run code; edits to the Bot's own config or shared skills; and anything else the policy forbids. A refusal comes back to the Bot with the reason;
- two system prompt sections: rule zero (`rule_zero.md`, "fix the cause, no band-aids, ever") and a short one saying a refusal is a stop. Both are frozen into each new session's prompt, so only a change here can remove them;
- the `tw_git`, `tw_files` and `tw_decide` tools (`brokers.py`), which talk to the brokers over their Unix sockets, and the turn gate, which stops any turn the fleet queue did not admit at its first tool call.

The guard reads its rules from `policy.json`, the JSON form of `policy.yaml`, because Hermes's own Python has no YAML library. A test fails if the two differ. If the guard cannot read the policy, every call is refused.

## The brokers

The brokers in `src/thornswood_brokers/` run as their own users, each from a root-owned copy of the release under `/opt/thornswood-hermes`, never from anything the `hermes` user can write. They sit in `tw-brokers.slice`.

| Broker | User | What it does |
|---|---|---|
| `tw-policy` | `tw-policy` | The fleet admission queue, exactly-once receipts, Patrick's decision records, route and credit state, and leases, in one SQLite store |
| `tw-git` | `tw-git` | Every GitHub call. It holds the two Apps' keys (mode 400, its own), checks each write against `src/thornswood_guard/github.py`, writes a receipt first, and reads GitHub back before any retry |
| `tw-files` | `tw-files` | Each worker's trees under `/srv/tw/work/<bot>`. It walks every path from a folder handle without following links, and runs checks through `tw-run-check` as the Bot's own user, with no network and only that tree visible |
| `tw-proxy` | `tw-proxy` | The admission proxy on `127.0.0.1:8611`, for model routes that need a key only it can read. It admits a route only once a pull request sets it ready in `admission_proxy.ready` |

The gateway reaches them only through their sockets under `/run/tw/`, each open to a named group. Checks run in `tw-sandbox.slice`, started through `tw-checks.socket` so `tw-files` never needs sudo.

### How a Bot's merge reaches GitHub

1. Bramble's turn asks `tw-policy` for a ticket. The queue admits it only while the host has the memory, swap, load and disk headroom the policy names.
2. Bramble calls the `tw_git` tool. The guard checks the call, and the tool sends it to `tw-git`'s socket.
3. `tw-git` checks it again: only Bramble may merge, only at the pull request's exact head, and only when every required check named in the policy is green there. Where a repository merges through GitHub's auto-merge, `tw-git` turns auto-merge on pinned to that head instead, after reading the branch's rules to confirm GitHub requires the same checks.
4. `tw-git` writes a receipt to `tw-policy`, then signs a short-lived token with Bramble's App key and makes the call.
5. If the answer is lost, `tw-git` reads GitHub back before it does anything again, so a merge never happens twice.

A worker's push follows the same path with the workers' App, and is refused if it touches `main`, a tag, another Bot's branch or a workflow file.

## The two GitHub Apps

| App | Used by | May |
|---|---|---|
| **thornswood-bramble** | Bramble | Merge, turn on auto-merge, start the listed release and deploy runs, and push a workflow change for Magpie |
| **thornswood-workers** | The five workers | Push to the Bot's own branches and open pull requests. It has no Workflows permission, so GitHub itself refuses a workflow change |

Their ids and key paths are in `policy.yaml` `brokers.apps`.

## The decision core

When a Bot needs Patrick, it opens a card in the decision core (`src/thornswood_decisions/`, the `tw-decide` service). He answers from the installable phone app or the Hermes Desktop pane, signed in with a passkey, and the core tells the Bot the answer. The core also carries his chats with the Bots and with his own Hermes, the live Board, push alerts and voice. [decision-app.md](decision-app.md) covers it in full.

## Patrick's own Hermes

His own Hermes on the VM (my-hermes, the `patrick-hermes` user) is not a Bot and is not set by `apply.py`. Each release's `deploy/decisions/install.sh` gives it two things, as that user: the `thornswood-decisions` plugin link, which adds rule zero to its prompt, and a copy of the `thornswood-coordinator` skill (`hermes/skills/thornswood-coordinator`). The skill holds Patrick's rules for talking to him and the steps for taking any ask he drops in one chat: file it on the board for the Bot that owns its field, follow it, and report status. Its status report comes from `scripts/coordinator_report.py`, which reads the board and the open pull requests with that user's own `gh` sign-in and changes nothing.

## The live Board

`thornswood_live` is a small Hermes plugin in each Bot's profile. It watches four of Hermes's own hooks and blocks nothing: when a turn thinks, which tool it runs by name, and when it ends. It writes one small file per process. `live_reader.py` (`tw-decide-live.service`) reads those files every 2 seconds and sends the core one row per running turn, so the app's Board shows what each Bot is doing right now. No words of a turn, tool arguments or output ever leave it.

## Keeping the VM on main

`tw-follow-main.timer` runs `src/thornswood_brokers/follow_main.py` as root every ten minutes. It:

1. fetches `main` with the VM's own read-only deploy key, checking GitHub's published host keys only;
2. stops if that commit is already installed, or already failed here;
3. puts the commit in a root-owned release folder, `/opt/thornswood-hermes/releases/<commit>`;
4. runs the whole suite at that commit as `tw-ci`, with no network, and installs nothing if a test fails;
5. runs that release's `deploy/install.sh` and, where the decision core is installed, `deploy/decisions/install.sh`; then `apply.py --check` and `apply.py` as `hermes`; then `tw-reload-bots`, which ends the `hermes` user's login sessions and restarts the gateway so every Bot process loads the new code; then the canary;
6. if any of step 5 fails, installs and applies the release that ran before, the same way, and says both.

[operations.md](operations.md) says how to watch it, pause it and roll back by hand.

The copy of the follower that runs is the one already installed, and it calls each new release's own `deploy/install.sh`, `deploy/decisions/install.sh`, `scripts/apply.py` and `tests/`, so those paths stay put. Where a release keeps its Python packages is in its own `deploy/layout.json`; the follower reads it before it runs another release's code (to count its store migrations, or to have it put the store back), and so does One's connector before it takes its two files from a release. A release made before that file keeps its packages at its root.

## Limits and slices

The host's caps are in `policy.yaml` `slices`, rendered into `deploy/systemd/*.slice` by `scripts/render_units.py`, so a cap changes only by pull request.

| Slice | Holds | Memory | CPU |
|---|---|---|---|
| `tw-hermes.slice` | The gateway | 3 GB | 150% |
| `tw-brokers.slice` | The brokers | 512 MB | 50% |
| `tw-sandbox.slice` | `tw-files` checks | 4 GB | 100% |
| The `hermes` user's slice | The Bots' podman containers | 4 GB | 100% |

At every boot, before the brokers and the gateway start, `tw-limits-check` reads each slice's caps from the cgroup tree, starts one job of each tier to see where it lands, and checks the swapfile, the Bots' disk quotas and the free-space floors. If anything is off, `tw-policy` admits no sandbox job.

## Backups

| When (Mountain time) | What |
|---|---|
| Every hour | `tw-snapshot` copies `tw-policy`'s store with SQLite's backup API |
| 3:30 AM | `tw-backup` writes one archive of the Bots' Hermes home and the policy store to `/srv/tw/backups`, encrypted to Patrick's age public key, kept 14 days. Sign-ins, `.env` files and keys are never in it |
| 4:30 AM | `tw-offsite` copies the archives to Google Drive (kept 90 days) and the game box's nightly (kept 30 days, as Thornswood's privacy page promises), and fails if the newest of either is over 26 hours old |

The game box sends its nightly as `tw-boxdrop` into an SFTP jail where it can add a file and do nothing else: no list, read, rename, overwrite or delete, and only from the box's address. A stolen box cannot reach any copy it sent. The install sets the jail up once the box's public key is in `deploy/keys/box-drop.pub`. The age key that opens the archives never goes on the VM.
