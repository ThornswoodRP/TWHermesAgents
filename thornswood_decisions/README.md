# The decision core

Patrick's cards, his answers and their receipts, as `07-MOBILE_AND_DECISIONS.md` designs them. One core on the Oracle VM holds every card; Hermes Desktop and the Android web app send it the same commands. It listens on loopback only, behind Tailscale Serve. It never runs on the game box and never on One.

Until test T33 passes and Patrick says so, the Claude report page stays the only place he answers. Nothing here answers, clears or copies a report page card.

## Build order (07 section 11.2), one pull request per step

| Step | What | State |
|---|---|---|
| 0 | Pin the Hermes contract | `contracts.json`, checked on the VM by `scripts/check-hermes-contract.py` |
| 1 | The core: schema and migrations, idempotent answers, immutable artifacts, the hash-chained event log, outboxes, sign-in, backup and restore | `schema.py`, `store.py`, `core.py`, `auth.py`, `backup.py` |
| 2 | The Desktop pane: the same cards, revision-checked answers, a polling fallback, pairing | `api.py`, `cli.py`, `plugins/thornswood-decisions/` |
| 3 | The phone, read only: the app shell on the `ts.net` origin, sign-in, cards, the Work and Room views | |
| 4 | Phone answers and chat: the same commands as Desktop, the six Bot Chats and the room | |
| 5 | Web Push: the outbox, the one quiet "Thornswood needs you", deep links, denial and revoke tests | |
| 6 | The Main card | Waits for phase 7. Not built |

Every feature flag starts off: `decision_write`, `mobile_read`, `mobile_chat_write`, `push_send`, `connector_effects`. Tests use a separate test workspace and test actions, never a real deploy, merge or Ship.

## The core (step 1)

Standard library only, so the same code runs in the VM's system Python and in the tests on One.

| File | What it holds |
|---|---|
| `schema.py` | The tables of 07 section 11.1 as ordered migrations. Artifact revisions, options, responses, events and effect receipts are history: a trigger aborts any change or delete. A store newer than the code refuses to open |
| `store.py` | One connection, WAL, foreign keys on, every write in `BEGIN IMMEDIATE`. The workspaces and their flags (all off when a workspace is made; only the operator on the VM turns one on). The event log, each event hashed over the one before it; `verify_events` names the first broken link |
| `core.py` | Cards and answers. Bramble opens any card but Main; a specialist opens a question only for a task Bramble assigned it; only Patrick answers. Bramble merges repeated requests by source key. A changed card is a new revision and a new attention revision, and voids answers to the old one. An answer is checked against the revision, attention revision and hash Patrick saw, and the response, status, event, cancelled pushes, the message to the requesting Bot and the command result commit together. The same idempotency key and body give the same receipt; the same key with another body is refused. A high-risk card needs a fresh sign-in. The four states (decision accepted, Bot told, work done, release authorized) are read from four different records |
| `auth.py` | Patrick's sessions (only hashes kept; 12 hours idle, 7 days at most, "fresh" for 5 minutes), CSRF tokens bound to a session, ten scrypt-hashed recovery codes that work once, and one-time pairing codes that expire in 10 minutes. Five wrong codes in 15 minutes stop that kind of sign-in for 15 minutes. A Bot is a service principal and never holds a session. The passkey ceremony comes in step 3 and ends in the same `issue_session` |
| `backup.py` | A consistent copy by SQLite's online backup, kept only if its event chain holds, never over an existing file. A restore refuses a broken chain or an existing store, then quarantines every undelivered push and connector message and revokes every session and pairing code, so nothing from before the backup runs again |

Not in step 1: the loopback HTTP API, cookies and Origin checks (step 2, with its first client, below), the passkey (step 3), any delivery worker (steps 4 and 5; `connector_effects` and `push_send` stay off), and putting the backup copy into the nightly encrypted archive. That archive is the Phase 2 backup job's (`thornswood_brokers/backup.py`), so that change is its own PR, coordinated with the Phase 2 thread, once the core has a place on the VM.

The push rule here (board review findings never push; weekly project review findings only when urgent) and `thornswood_guard/notify.py` both describe pushes. Step 5 makes the core's outbox the one place, and `notify.py` and the `notifications` part of `policy.yaml` change with it.

## The Desktop pane (step 2)

| File | What it holds |
|---|---|
| `api.py` | The core's HTTP API, on 127.0.0.1 only (`make_server` refuses any other address). A session comes one way, never two: the phone's in an HttpOnly, Secure cookie, where every write needs the app's exact `Origin` and the CSRF token; Desktop's as `Authorization: Bearer` from the plugin's backend, where every write needs the CSRF token and any `Origin` sent must be exact. A Desktop device answers only through the Hermes sign-in that paired it. A phone reads only with `mobile_read` on and does not answer until step 4. `GET` never changes a card. `/changes?since=<seq>` names the cards that moved, for polling. It logs nothing |
| `cli.py` | The operator's commands on the VM: `init` (the workspace with every flag off, Patrick, the six Bots from `contracts.json`), `bind-room`, `pair` (the first device's code), `flag` (a flag in a real workspace goes on only with `--patrick-switched`, at the switch), `test-card` (a test workspace only), `serve`, `backup`, `verify` |
| `plugins/thornswood-decisions/dashboard/plugin_api.py` | The plugin's backend, in Patrick's own Hermes (`patrick-hermes`), never the Bots'. Every route is a `POST`, since the pane's `ctx.rest` cannot set headers, and forwards to the core with the pane's session. It names the caller from what Hermes verified, the way Hermes names it, and answers `{status, body}` so the pane reads a 409's detail; status 0 means the core did not answer. It holds and logs nothing |
| `plugins/thornswood-decisions/desktop/plugin.js` | The pane: pairing, the Needs you list, a card with its text, options, the recommendation and what yes covers, and a confirmation for a high-risk card. Each answer attempt keeps one idempotency key until its outcome is known; after an unclear result it reads the card's receipt first, and sending again is the same answer. It polls every 5 seconds, one poll at a time. Off until Patrick turns it on |

Pairing: the first Desktop types a code made on the VM with `cli pair --kind desktop`; later devices get theirs from a signed-in one. The code works once, for 10 minutes. The pane keeps the session the core gave it in plugin storage on that machine.

What this leaves out: the core's install on the VM as its own user (the next pull request; it needs Patrick's `sudo`), a fresh sign-in on Desktop for a high-risk card (until the passkey in step 3, a high-risk card on Desktop needs a new pairing code), and any answer from the phone.

Against a remote Hermes, Desktop does not copy the plugin's desktop half from the VM, so `desktop/plugin.js` goes in `desktop-plugins/thornswood-decisions/` on the machine Desktop runs on, and the backend half in the VM's `plugins/` with `thornswood-decisions` in `plugins.enabled`.

## The pinned contract (step 0)

`contracts.json` records what the core relies on in Hermes, each fact with the file and line it was read from at the pinned commit:

- **Hermes v0.22.5** is upstream commit `bf0203a794a07aac3ff764972ab9087baa4e4f4e` (tag `v0.22.5`, also `rc.3-v0.22.5`), the same commit the 29 Oct audit read.
- **The six profile ids:** `bramble`, `rift`, `sprocket`, `echo`, `magpie`, `wisp`, each matching Hermes's `PROFILE_ID_RE`.
- **The Workbench room** is a hosted room in the gateway's `shared-state.db`, reached through the `groups.*` methods. `groups.send` appends one `message.user` event with `{text, thread_id}`; its `event_id` is a retry key the gateway turns into a stable event id; `groups.log` pages events after a cursor, 500 at most.
- **A Bot's DM** is its canonical, hidden "Bot Chat" session. It is found by exact title before anything is created, and a turn is sent through the api_server platform's `/p/<profile>/v1/runs` with an `Idempotency-Key`. A timeout after the run was accepted is never resent.
- **The Desktop pane** is a Hermes plugin with a `plugin_api.py` backend under `/api/plugins/thornswood-decisions/`. Its socket is a no-op on OAuth remotes, so the pane polls. A signed-in request carries a Hermes session or a token principal, never "Patrick" by itself, so Desktop pairs once with the core.

The room's id is data on the VM, not in the source. `check-hermes-contract.py` reads it from the room store without writing and prints it; the core binds to that id.

## Checking the VM

On the VM, as `hermes`, from the release folder `send-to-oracle.sh` made:

```bash
python3 -I scripts/check-hermes-contract.py
```

It prints a `PASS` or `STOP` line for the Hermes commit, the six profiles and the one Workbench room, then the facts it read. Any `STOP` exits 1.
