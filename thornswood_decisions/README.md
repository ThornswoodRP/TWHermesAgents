# The decision core

Patrick's cards, his answers and their receipts, as `07-MOBILE_AND_DECISIONS.md` designs them. One core on the Oracle VM holds every card; Hermes Desktop and the Android web app send it the same commands. It listens on loopback only, behind Tailscale Serve. It never runs on the game box and never on One.

Until test T33 passes and Patrick says so, the Claude report page stays the only place he answers. Nothing here answers, clears or copies a report page card.

## Build order (07 section 11.2), one pull request per step

| Step | What | State |
|---|---|---|
| 0 | Pin the Hermes contract | `contracts.json`, checked on the VM by `scripts/check-hermes-contract.py` |
| 1 | The core: schema and migrations, idempotent answers, immutable artifacts, the hash-chained event log, outboxes, sign-in, backup and restore | `schema.py`, `store.py`, `core.py`, `auth.py`, `backup.py` |
| 2 | The Desktop pane: the same cards, revision-checked answers, a polling fallback, pairing | `api.py`, `cli.py`, `plugins/thornswood-decisions/` |
| 3 | The phone, read only: the app shell on the `ts.net` origin, sign-in, cards, the Work and Room views | `passkey.py`, `feeds.py`, `feed_reader.py`, `app/` |
| 4 | Phone answers and chat: the same commands as Desktop, the six Bot Chats and the room | `chat.py`, `connector.py`, `room_send.py`, `app/` |
| 5 | Web Push: the outbox, the one quiet "Thornswood needs you", deep links, denial and revoke tests | `webpush.py`, `push.py`, `push_worker.py`, `app/sw.js` |
| 6 | The Main card | Waits for phase 7. Not built |

Every feature flag starts off: `decision_write`, `mobile_read`, `mobile_chat_write`, `push_send`, `connector_effects`. Tests use a separate test workspace and test actions, never a real deploy, merge or Ship.

## The core (step 1)

Standard library only, so the same code runs in the VM's system Python and in the GitHub checks.

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
| `plugins/thornswood-decisions/desktop/plugin.js` | The pane: pairing, the Decisions list, a card with its text, options, the recommendation and what yes covers, and a confirmation for a high-risk card. Each answer attempt keeps one idempotency key until its outcome is known; after an unclear result it reads the card's receipt first, and sending again is the same answer. It polls every 5 seconds, one poll at a time. Off until Patrick turns it on |

Pairing: the first Desktop types a code made on the VM with `cli pair --kind desktop`; later devices get theirs from a signed-in one. The code works once, for 10 minutes. The pane keeps the session the core gave it in plugin storage on that machine.

What this leaves out: a fresh sign-in on Desktop for a high-risk card (until the passkey in step 3, a high-risk card on Desktop needs a new pairing code), and any answer from the phone.

Against a remote Hermes, Desktop does not copy the plugin's desktop half from the VM, so `desktop/plugin.js` goes in `desktop-plugins/thornswood-decisions/` on the machine Desktop runs on, and the backend half in the VM's `plugins/` with `thornswood-decisions` in `plugins.enabled`.

## The phone, read only (step 3)

| File | What it holds |
|---|---|
| `app/` | The installable app at `/m/`: `index.html`, `app.js` (no framework, no inline code; everything from the core is set as text), `app.css`, `manifest.webmanifest`, the icons, and `sw.js`, which keeps the shell for opening offline and never a card, an answer or an `/api/` reply. Bottom bar: Bramble, Bots, Rooms, Board, Decisions (first named Room, Work and Needs you; see "After the first phone test"). Cards open by deep link (`/m/decisions/<id>`) and show their text, options, recommendation and what yes covers, with no answer button until step 4 |
| `passkey.py` | Patrick's passkeys, checked by py_webauthn. A passkey is added only from a fresh session on a paired phone and belongs to it. Sign-in needs the phone's lock (user verification) and gives a fresh session on that phone. Challenges work once, for 5 minutes. Five failures in 15 minutes stop passkey sign-in for 15 minutes. Without the library every passkey route answers 503; nothing falls back |
| `softkey.py` | A software passkey for the tests and the install's self-check, never for signing in |
| `feeds.py` | The Work and Room views (now Board and Rooms): the latest snapshot of each, sent by the feed with its own token, kept to the fields shown and marked "as of". What the Bots' Hermes reports, never authority |
| `feed_reader.py` | Runs as `hermes` once a minute: reads Hermes's `kanban.db` and the Workbench room's events read-only and sends the two snapshots |

`api.py` adds: `/m/` and its files; passkey sign-in and enrolment (exact Origin); a recovery code sign-in that makes the phone a new device; `POST /csrf`, which gives the app a new CSRF token after a reload (cookie and exact Origin, the one write without the old token); `GET /work` and `GET /room`; and `POST /feeds/<name>` for the feed's token only, never a browser. With `mobile_read` off the phone can pair, sign in and add a passkey, but reads no card, work or room.

`tests/decisions_phone_check.js` drives the app in headless Chromium with Chrome's virtual authenticator: pairing, a deep link, adding a passkey, signing in with it, the views and the service worker. It is not T10; T10 to T13 are Patrick's phone.

## Phone answers and chat (step 4)

The phone answers a card with the same command Desktop sends (`POST /cards/<id>/answer`, checked by `DecisionCore.answer`): the card revision, the attention revision and the hash Patrick saw, an idempotency key, the cookie, the CSRF token and the exact Origin. It needs `decision_write`. A high-risk card first shows its own check screen, and the answer needs a passkey sign-in from the last five minutes; the app offers that sign-in and then sends the same answer. A save that gets no clear reply reads the card back ("Checking outcome") before it offers to send again, and sending again uses the same key, so it is never a second answer.

| File | What it holds |
|---|---|
| `chat.py` | Patrick's chat. A message is written to `chat_messages` with its command record in one commit (same key, same receipt; a changed body with the same key is refused). It needs `mobile_chat_write`, and only Patrick sends. The connector leases one message per Bot or room at a time; a lease that runs out is "uncertain" and never sent again by itself. A room message whose event the room feed shows is settled as sent, because its event id comes from the core's key. The same lease carries the "Bot told" message to the Bot that asked a card, and its confirmed receipt is that state. No message text goes into the event chain or a log |
| `connector.py` | Runs on the VM as `hermes` (`tw-decide-connector.service`) with its own token, and does nothing while `connector_effects` is off. A Bot Chat message goes through `hermes -p <bot> chat --in ~ -c "Bot Chat" --create-if-missing -Q --query-file <file>`, the way Hermes's own message_agent sends one on its machine, after a check that the Bot's gateway is running. A room message goes through `room_send.py`. Only Hermes saying so is "sent", only nothing sent is "not sent", and everything else is "uncertain". At most three turns at once |
| `room_send.py` | Run with Hermes's own Python: finds the running gateway without starting one and calls `groups.send` with the core's key as the event id and thread |
| `feed_reader.py` | Now also sends the "bots" snapshot: each Bot's canonical Bot Chat, its last 40 messages from Patrick and the Bot, read only from `profiles/<id>/state.db`. Room events carry their event id |

No API_SERVER_KEY, no api_server listener and no change to the gateway's settings: the connector uses the paths Hermes uses on its own machine (`contracts.json`, `bot_chat.local_send` and `room.client`). `api.py` adds `GET /chat` (the six Bots), `GET /chat/<bot or room>` (the snapshot, as of its time, and Patrick's own messages with their state), `POST /chat/<bot or room>`, and `POST /connector/claim` and `/connector/result` for the connector's token only, never a browser. The bottom bar's Bramble, Bots and Room now chat; each Bot has a deep link at `/m/bots/<bot>`. A chat message is never an answer to a card.

## Web Push (step 5)

A card that needs Patrick puts one quiet line on his phone's lock screen: "Thornswood needs you", with no body, no buttons and no sound. A tap opens that exact card in the app. Nothing a notification does answers a card. Standard Web Push with our own VAPID key, through the phone browser's own push service: no paid push, no account, no Cloudflare. The Tailscale `ts.net` origin is enough for the phone to subscribe, so Cloudflare Tunnel comes in only if T10 or T11 fail there.

| File | What it holds |
|---|---|
| `webpush.py` | The message encryption of RFC 8291 (aes128gcm), checked byte for byte against the RFC's own example, and the VAPID header of RFC 8292 (an ES256 token naming the app's origin). `seal` keeps a subscription encrypted in the core's store to the push worker's key, so the store and its backups never hold one anybody can use. `push_host_ok` allows only the browsers' push services (Google's, Mozilla's, Microsoft's, Apple's) |
| `push.py` | The phone's alerts and the push outbox. A phone subscribes only after a deliberate "Turn on alerts" tap; a "no" is kept and the app never asks again by itself; a revoked phone's subscription goes with it. The card's own commit writes the push row (`core.py`). The worker leases it only while `push_send` is on, and the lease checks again, right then, that the card is still pending at that attention revision and a live phone has alerts on, or the row is cancelled. 2xx from the push service is "push service accepted", never "shown" or "read"; 404 and 410 retire the subscription; 429, 5xx and no answer come back after Retry-After (a minute to an hour), five tries in all; a lease that runs out is "uncertain" and is not sent again |
| `push_worker.py` | Runs on the VM as its own user, `tw-push` (`tw-decide-push.service`), with the core's venv. It makes its VAPID key and seal key in `/srv/tw/push` (0700, 0600 files) the first time, says the two public keys to the core once, then encrypts each push for that one browser and posts it to the push service, following no redirect. It logs push ids and status codes only, never an endpoint, a key or a payload. It is the only part that reaches the internet, and it listens on no port |
| `app/sw.js`, `app/app.js` | The service worker shows the one line, tagged `decision:<id>` so a second push for a card replaces the first, and opens `/m/decisions/<id>` on a tap (the id is checked before it becomes a path). Settings has "Turn on alerts" and "Turn off alerts". When the app sees a card is no longer pending, it takes that card's alert off the phone |

The payload is only `{v, event_id, decision_id, attention_revision, path}`: no question, repo, answer or token, and the push service sees only ciphertext. `api.py` adds `GET /push` (the alert state, phone data like the cards), `POST /push/subscribe` (cookie, CSRF and the exact Origin), `/push/unsubscribe` and `/push/denied`, and `POST /push-worker/hello`, `/claim` and `/result` for the worker's token only, never a browser. If the worker's keys are ever lost, `tw-decide push-reset --workspace thornswood` forgets them and every subscription, and the phone turns alerts on again.

`tests/test_decisions_push.py` checks the encryption against the RFC's example, the VAPID header, the seal, the allowlist, the outbox rules and the worker against a stand-in push service. `tests/decisions_phone_check.js` now also turns alerts on and is told no, has DevTools deliver a push (no push service) and checks the one quiet line and its tag, and checks the alert of an answered card is taken off. Not checked here: a real push service taking a push, the tap on an alert (DevTools cannot tap one), and T10 to T13 on Patrick's phone.

## Installing on the VM

`deploy/decisions/` holds the install. It runs from the root-owned release that `tw-follow-main` puts in `/opt/thornswood-hermes/releases/<commit>` when main reaches the VM, so it runs after that, at the same commit. Nothing here runs on One.

| File | What it does |
|---|---|
| `tw-decide` | The launcher, the core's venv Python with `-I`, from `/opt/thornswood-hermes/current` only |
| `tw-decide.service` | The core as the `tw-decide` user, on 127.0.0.1:9135, with `TW_DECIDE_ORIGIN` from `/etc/thornswood/tw-decide.env`. systemd lets it reach loopback and nothing else, and write only `/srv/tw/decide` |
| `requirements.txt` | py_webauthn and what it needs, each pinned by hash; installed as wheels only |
| `tw-decide-feed`, `tw-decide-feed.service`, `.timer` | The Work, Room and Bot Chat feeds as `hermes`, once a minute, loopback only |
| `tw-decide-push`, `tw-decide-push.service` | The push worker as `tw-push`, writing only `/srv/tw/push`, at most 150 MB. Its outbound connections are not limited by address, because the push services' addresses are not fixed; the worker refuses every host but theirs |
| `tw-decide-connector`, `tw-decide-connector.service` | The connector as `hermes`, loopback only, at most 900 MB. It runs `hermes` and finds the gateway's control socket, so it is not `ProtectSystem=strict`; the install adds `XDG_RUNTIME_DIR` as the gateway's own unit has it |
| `install.sh <commit> [--room <id>] [--origin <url>]` | Makes the user and `/srv/tw/decide` (0700), builds the venv, runs a whole passkey ceremony under the unit's limits, puts the launchers and units in place, makes the store the first time with every flag off, binds the room, makes the three feed tokens and the connector's token into `hermes`'s `~/.config/thornswood` (0600), finds the `hermes` command as that user's login shell does, starts the core, the feeds and the connector, and checks the core answers on loopback only. Since step 5 it also makes the `tw-push` user and `/srv/tw/push`, puts the push worker's token there, checks one sealed, encrypted and signed push under the unit's limits, and starts the push worker once `--origin` is known. It changes no flag and makes no pairing code |
| `install-plugin.sh <commit>` | Links `plugins/thornswood-decisions` in Patrick's own Hermes (`patrick-hermes`) to the root-owned release. It never touches the Bots' Hermes and never edits a Hermes setting; Patrick turns the plugin on with `hermes plugins enable thornswood-decisions` |

The order, after the merge, with the merged commit. Every line but 5 and 7 runs on the VM, by someone with sudo there (Patrick's own Hermes, as it ran #21's install):

1. Wait for main to reach the VM: `readlink -f /opt/thornswood-hermes/current` ends in the merged commit. `tw-follow-main` does that by itself; `/var/lib/thornswood/follow-main.json` says what it last did.
2. `sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/install.sh <commit> --room <Workbench room id>`
3. `sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/install-plugin.sh <commit>`
4. As `patrick-hermes`: `hermes plugins enable thornswood-decisions`.
5. On the machine Desktop runs on: `desktop/plugin.js` into `desktop-plugins/thornswood-decisions/`, then turn it on in Capabilities, Plugins.
6. The first code: `sudo -u tw-decide /opt/thornswood-hermes/bin/tw-decide --db /srv/tw/decide/core.db pair --workspace thornswood --kind desktop --label Desktop`, typed into the pane within 10 minutes.
7. For the phone, once: in the Tailscale admin console, the DNS page, MagicDNS on, then HTTPS Certificates, Enable HTTPS (the VM's name goes into the public certificate log). Then on the VM `sudo tailscale serve --bg --https=443 127.0.0.1:9135`, and step 2 again with `--origin https://hermes.<tailnet>.ts.net` added. `tailscale serve status` shows it; `sudo tailscale serve --bg --https=443 off` takes it away.

With every flag off the pane pairs and reads, and an answer is refused with "Answers are off for now".

After this first install, `tw-follow-main` runs `deploy/decisions/install.sh` again at each merge it installs, right after `deploy/install.sh`, keeping the room and origin from `/etc/thornswood/tw-decide.env`; if a later step fails, it runs it again for the release it puts back. Steps 3 to 6 are not repeated: the plugin is linked through `/opt/thornswood-hermes/current`.

## The phone tests (T10 to T13)

Tests use a separate test workspace and test actions, never a real deploy, merge or Ship, and the real workspace keeps every flag off until the switch. So `deploy/decisions/phone-test.sh` runs a test bench beside the real core:

- a second core (`tw-decide-test.service`) serving only the test workspace `phone-test`, from its own store in `/srv/tw/decide-test`, on 127.0.0.1:9136;
- its own push worker (`tw-decide-push-test.service`), with keys and a token of its own in `/srv/tw/push-test`;
- Tailscale Serve on port 8443, so its address is the real origin with `:8443`.

Each unit is the real one with only its store, port, settings file and description changed (the tests check this), and neither starts at boot. Only in `phone-test` does the bench turn on `mobile_read`, `decision_write` and `push_send`. It never turns on chat or `connector_effects`, so nothing reaches a Bot or the room. Its cards are test cards, and answering one changes nothing anywhere.

On the VM, after step 7 above, with the installed commit:

1. `sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/phone-test.sh <commit> start`
2. On the phone, with Tailscale on: open `https://hermes.<tailnet>.ts.net:8443/m/`, then `phone-test.sh <commit> pair` and type the code within 10 minutes. Add a passkey from Settings, and add the app to the home screen (T10).
3. Turn alerts on in Settings, close the app, lock the phone, and run `phone-test.sh <commit> card`. One quiet "Thornswood needs you" should arrive (T11). Tapping it opens that exact card after sign-in, and the lock screen shows nothing else (T12, T13).
4. `phone-test.sh <commit> card high` checks the high-risk path: its check screen, then a passkey sign-in.
5. `phone-test.sh <commit> stop` takes 8443 off Tailscale Serve and stops both. The test store stays.

All of these passed on Patrick's phone on 1 Nov, the high-risk card included. Since then `start` also binds two test rooms ("Test room" and "Second test room") in `phone-test`, so the Rooms list can be tried. They are names only: no room is behind them, and chat stays off, so nothing is ever sent to them.

## After the first phone test

Patrick's first phone test (1 Nov) passed, and asked for these:

- **Tabs.** Work is now **Board** (`/m/board`), Needs you is now **Decisions** (`/m/decisions`, with its badge), and Room is now **Rooms** (`/m/rooms`): a list like Bots, one button per room this workspace is bound to, each opening that room at `/m/rooms/<room id>`. The first paths (`/m/needs-you`, `/m/work`, `/m/room`) still open, so an old alert or home screen link works. The app opens on Decisions, and its home screen name is **Hermes**. The Desktop pane's heading is Decisions too.
- **Rooms.** A room's chat target is its room id; `room` still means the first room bound. The room feed carries every bound room at once (`{source_time, rooms: [{room_id, events}]}`; the one-room shape is still taken), `feed_reader.py --room` takes one `--room` for each room, and `tw-decide bind-room --name` binds a room other than the Workbench. The connector sends each message to its own room's id, and a message is settled only by its own room's events.
- **An answered card stops showing.** After an answer the app goes back to Decisions, which says what was chosen and keeps the card under **Answered** for a day. The service worker, which shows the alerts, now takes them off: the app tells it which cards still need Patrick each time it reads Decisions, and which card was just opened or answered. An alert of a card answered on Desktop while the phone app is closed stays until the app next opens.
- **Alerts that stop reaching the phone.** Alerts were turned on in the Chrome tab before the app was installed; the installed app's subscription replaced that one, and the tab's answered 410. The core cannot see endpoints (they are sealed to the push worker), so it now keeps the SHA-256 of each one (migration 6) and `GET /push` says it, and says `lapsed` when the push service retired the subscription (404 or 410) or the operator reset the keys while the phone's yes still stands. Each time the app opens (and when it comes back after ten minutes) it compares that with the browser's own subscription: the same, nothing; a different one, it sends the browser's; none, one made with another key, or the very one that lapsed, it makes a new one and sends that. It never does this after Patrick turned alerts off or said no, or once the phone's permission is gone, and Settings says when it renewed them. `alertsPlan` in `app.js` is that choice, checked case by case in the browser check.
- **Installing.** Chrome keeps its install under the three-dot menu. In a browser tab the app shows a line saying where it is (Chrome's menu, Add to home screen, Install), or an **Install the app** button when Chrome hands it the install prompt, and Settings says to install first and turn alerts on from the installed app.

Checked here: the unit tests above, and `tests/decisions_phone_check.js` in Chromium (the tabs and old paths, the Rooms list and a room's deep link, an answer leaving Decisions for Answered, the alerts of answered and opened cards taken off, every `alertsPlan` case, the install line, and the manifest). Not checked here: a real push service, the install prompt itself (headless Chromium never offers it), and Patrick's phone.

## The pinned contract (step 0)

`contracts.json` records what the core relies on in Hermes, each fact with the file and line it was read from at the pinned commit:

- **Hermes v0.22.5** is upstream commit `bf0203a794a07aac3ff764972ab9087baa4e4f4e` (tag `v0.22.5`, also `rc.3-v0.22.5`), the same commit the 29 Oct audit read.
- **The six profile ids:** `bramble`, `rift`, `sprocket`, `echo`, `magpie`, `wisp`, each matching Hermes's `PROFILE_ID_RE`.
- **The Workbench room** is a hosted room in the gateway's `shared-state.db`, reached through the `groups.*` methods. `groups.send` appends one `message.user` event with `{text, thread_id}`; its `event_id` is a retry key the gateway turns into a stable event id; `groups.log` pages events after a cursor, 500 at most.
- **A Bot's DM** is its canonical, hidden "Bot Chat" session. It is found by exact title before anything is created. From another machine a turn goes through the api_server platform's `/p/<profile>/v1/runs` with an `Idempotency-Key`; on the Bots' own machine (step 4) it goes through `hermes -p <profile> chat -c "Bot Chat" --query-file`, as Hermes's message_agent does, with no key and no listener. A timeout after a turn was taken is never resent.
- **The Desktop pane** is a Hermes plugin with a `plugin_api.py` backend under `/api/plugins/thornswood-decisions/`. Its socket is a no-op on OAuth remotes, so the pane polls. A signed-in request carries a Hermes session or a token principal, never "Patrick" by itself, so Desktop pairs once with the core.

The room's id is data on the VM, not in the source. `check-hermes-contract.py` reads it from the room store without writing and prints it; the core binds to that id.

## Checking the VM

On the VM, as `hermes`, from the release folder `tw-follow-main` made for it (`/home/hermes/thornswood-hermes/current`):

```bash
python3 -I scripts/check-hermes-contract.py
```

It prints a `PASS` or `STOP` line for the Hermes commit, the six profiles and the one Workbench room, then the facts it read. Any `STOP` exits 1.
