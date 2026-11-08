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
| 5 | Web Push: the outbox, the one "Hermes needs you", deep links, denial and revoke tests | `webpush.py`, `push.py`, `push_worker.py`, `app/sw.js` |
| 6 | The Main card | Waits for phase 7. Not built |

Every feature flag starts off: `decision_write`, `mobile_read`, `mobile_chat_write`, `push_send`, `connector_effects`. Tests use a separate test workspace and test actions, never a real deploy, merge or Ship.

## The core (step 1)

Standard library only, so the same code runs in the VM's system Python and in the GitHub checks.

| File | What it holds |
|---|---|
| `schema.py` | The tables of 07 section 11.1 as ordered migrations. Artifact revisions, options, responses, events and effect receipts are history: a trigger aborts any change or delete. A store newer than the code refuses to open, so each migration from 6 on also has its way back (`DOWNS`): `tw-decide downgrade --to N`, run from the newer release with every tw-decide unit stopped, undoes the newer migrations only when nothing would be lost, and otherwise refuses and changes nothing (fix forward). `tw-decide schema` says the store's schema and the newest the code knows |
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
| `passkey.py` | Patrick's passkeys, checked by py_webauthn. A passkey is added only from a fresh session on a paired phone and belongs to it. Sign-in needs the phone's lock (user verification) and gives a fresh session on that phone. Challenges work once, for 5 minutes. Five failures in 15 minutes stop passkey sign-in for 15 minutes. Without the library every passkey route answers 503; nothing falls back. A phone can also hold its own key, the fingerprint (below) |
| `softkey.py` | A software passkey for the tests and the install's self-check, never for signing in |
| `feeds.py` | The Work and Room views (now Board and Rooms): the latest snapshot of each, sent by the feed with its own token, kept to the fields shown and marked "as of". What the Bots' Hermes reports, never authority |
| `feed_reader.py` | Runs as `hermes` once a minute: reads Hermes's `kanban.db` and the Workbench room's events read-only and sends the two snapshots (the Board's GitHub half came later; see "The Board shows the Bots' work on GitHub") |

`api.py` adds: `/m/` and its files; passkey sign-in and enrolment (exact Origin); a recovery code sign-in that makes the phone a new device; `POST /csrf`, which gives the app a new CSRF token after a reload (cookie and exact Origin, the one write without the old token); `GET /work` and `GET /room`; and `POST /feeds/<name>` for the feed's token only, never a browser. With `mobile_read` off the phone can pair, sign in and add a passkey, but reads no card, work or room.

`tests/decisions_phone_check.js` drives the app in headless Chromium with Chrome's virtual authenticator: pairing, a deep link, adding a passkey, signing in with it, the views and the service worker. It is not T10; T10 to T13 are Patrick's phone.

## Phone answers and chat (step 4)

The phone answers a card with the same command Desktop sends (`POST /cards/<id>/answer`, checked by `DecisionCore.answer`): the card revision, the attention revision and the hash Patrick saw, an idempotency key, the cookie, the CSRF token and the exact Origin. It needs `decision_write`. A high-risk card first shows its own check screen, and the answer needs a passkey sign-in from the last five minutes. The passkey prompt opens over that screen by itself (Patrick, 1 Nov), so the face or fingerprint is the yes and the same answer goes once it signs in; only if the prompt is dismissed or fails does a button show, beside what is being approved, and nothing is answered until a passkey succeeds. A save that gets no clear reply reads the card back ("Checking outcome") before it offers to send again, and sending again uses the same key, so it is never a second answer.

| File | What it holds |
|---|---|
| `chat.py` | Patrick's chat. A message is written to `chat_messages` with its command record in one commit (same key, same receipt; a changed body with the same key is refused). It needs `mobile_chat_write`, and only Patrick sends. The connector leases one message per Bot or room at a time; a lease that runs out is "uncertain" and never sent again by itself. A room message whose event the room feed shows is settled as sent, because its event id comes from the core's key. The same lease carries the "Bot told" message to the Bot that asked a card, and its confirmed receipt is that state. For a question or change card, that message also gives the Bot the steps for its DECIDED.md line (plan line 39): comment the answer on the card's issue, or file one first, then add one line in DECIDED.md's own format with the card id and Patrick's Mountain date, linking that comment, and run Check-Words and the two docs tests before the pull request. A review or to-do card approves one step and gets no line No message text goes into the event chain or a log |
| `connector.py` | Runs on the VM as `hermes` (`tw-decide-connector.service`) with its own token, and does nothing while `connector_effects` is off. A Bot Chat message goes through `hermes -p <bot> chat --in ~ -c "Bot Chat" --create-if-missing -Q --query-file <file>`, the way Hermes's own message_agent sends one on its machine, after a check that the Bot's gateway is running. A room message goes through `room_send.py`. Only Hermes saying so is "sent", only nothing sent is "not sent", and everything else is "uncertain". A gateway that is not ready (starting, stopped, reloaded by an install, or its check could not run) is `not_ready`: the core puts the message back in the queue as it was, and the connector claims nothing more until that gateway's check says ready, so it goes then. At most three turns at once |
| `room_send.py` | Run with Hermes's own Python: finds the running gateway without starting one and calls `groups.send` with the core's key as the event id and thread |
| `feed_reader.py` | Now also sends the "bots" snapshot: each Bot's canonical Bot Chat, its last 40 messages from Patrick and the Bot, read only from `profiles/<id>/state.db`. Room events carry their event id |

No API_SERVER_KEY, no api_server listener and no change to the gateway's settings: the connector uses the paths Hermes uses on its own machine (`contracts.json`, `bot_chat.local_send` and `room.client`). `api.py` adds `GET /chat` (the six Bots), `GET /chat/<bot or room>` (the snapshot, as of its time, and Patrick's own messages with their state), `POST /chat/<bot or room>`, and `POST /connector/claim` and `/connector/result` for the connector's token only, never a browser. The bottom bar's Bramble, Bots and Room now chat; each Bot has a deep link at `/m/bots/<bot>`. A message in a Bot's Bot Chat answers a card only as "A chat answers a card" below says.

## Web Push (step 5)

A card that needs Patrick puts one line on his phone: "Hermes needs you", with no body and no buttons. It pops up with the phone's normal notification sound (Patrick's answer, 2 Nov; it was silent before, which Android files with no pop-up). A tap opens that exact card in the app. Nothing a notification does answers a card. Standard Web Push with our own VAPID key, through the phone browser's own push service: no paid push, no account, no Cloudflare. The Tailscale `ts.net` origin is enough for the phone to subscribe, so Cloudflare Tunnel comes in only if T10 or T11 fail there.

| File | What it holds |
|---|---|
| `webpush.py` | The message encryption of RFC 8291 (aes128gcm), checked byte for byte against the RFC's own example, and the VAPID header of RFC 8292 (an ES256 token naming the app's origin). `seal` keeps a subscription encrypted in the core's store to the push worker's key, so the store and its backups never hold one anybody can use. `push_host_ok` allows only the browsers' push services (Google's, Mozilla's, Microsoft's, Apple's) |
| `push.py` | The phone's alerts and the push outbox. A phone subscribes only after a deliberate "Turn on alerts" tap; a "no" is kept and the app never asks again by itself; a revoked phone's subscription goes with it. The card's own commit writes the push row (`core.py`). The worker leases it only while `push_send` is on, and the lease checks again, right then, that the card is still pending at that attention revision and a live phone has alerts on, or the row is cancelled. 2xx from the push service is "push service accepted", never "shown" or "read"; 404 and 410 retire the subscription; 429, 5xx and no answer come back after Retry-After (a minute to an hour), five tries in all; a lease that runs out is "uncertain" and is not sent again |
| `push_worker.py` | Runs on the VM as its own user, `tw-push` (`tw-decide-push.service`), with the core's venv. It makes its VAPID key and seal key in `/srv/tw/push` (0700, 0600 files) the first time, says the two public keys to the core once, then encrypts each push for that one browser and posts it to the push service, following no redirect. It logs push ids and status codes only, never an endpoint, a key or a payload. It is the only part that reaches the internet, and it listens on no port |
| `app/sw.js`, `app/app.js` | The service worker shows the one line, tagged `decision:<id>` so a second push for a card replaces the first, and opens `/m/decisions/<id>` on a tap (the id is checked before it becomes a path). Settings has "Turn on alerts" and "Turn off alerts". When the app sees a card is no longer pending, it takes that card's alert off the phone |

The payload is only `{v, event_id, decision_id, attention_revision, path}`: no question, repo, answer or token, and the push service sees only ciphertext. `api.py` adds `GET /push` (the alert state, phone data like the cards), `POST /push/subscribe` (cookie, CSRF and the exact Origin), `/push/unsubscribe` and `/push/denied`, and `POST /push-worker/hello`, `/claim` and `/result` for the worker's token only, never a browser. If the worker's keys are ever lost, `tw-decide push-reset --workspace thornswood` forgets them and every subscription, and the phone turns alerts on again.

`tests/test_decisions_push.py` checks the encryption against the RFC's example, the VAPID header, the seal, the allowlist, the outbox rules and the worker against a stand-in push service. `tests/decisions_phone_check.js` now also turns alerts on and is told no, has DevTools deliver a push (no push service) and checks the one line, its sound and its tag, and checks the alert of an answered card is taken off. Not checked here: a real push service taking a push, the tap on an alert (DevTools cannot tap one), and T10 to T13 on Patrick's phone.

## Installing on the VM

`deploy/decisions/` holds the install. It runs from the root-owned release that `tw-follow-main` puts in `/opt/thornswood-hermes/releases/<commit>` when main reaches the VM, so it runs after that, at the same commit. Nothing here runs on One.

| File | What it does |
|---|---|
| `tw-decide` | The launcher, the core's venv Python with `-I`, from `/opt/thornswood-hermes/current` only |
| `tw-decide.service` | The core as the `tw-decide` user, on 127.0.0.1:9135, with `TW_DECIDE_ORIGIN` from `/etc/thornswood/tw-decide.env`. systemd lets it reach loopback and nothing else, and write only `/srv/tw/decide` |
| `requirements.txt` | py_webauthn and what it needs, each pinned by hash; installed as wheels only |
| `tw-decide-feed`, `tw-decide-feed.service`, `.timer` | The Room and Bot Chat feeds as `hermes`, once a minute, loopback only (the Work feed was here until the live Board, below) |
| `tw-decide-push`, `tw-decide-push.service` | The push worker as `tw-push`, writing only `/srv/tw/push`, at most 150 MB. Its outbound connections are not limited by address, because the push services' addresses are not fixed; the worker refuses every host but theirs |
| `tw-decide-connector`, `tw-decide-connector.service` | The connector as `hermes`, loopback only, at most 900 MB. It runs `hermes` and finds the gateway's control socket, so it is not `ProtectSystem=strict`; the install adds `XDG_RUNTIME_DIR` as the gateway's own unit has it |
| `install.sh <commit> [--room <id>] [--origin <url>]` | Makes the user and `/srv/tw/decide` (0700), builds the venv, runs a whole passkey ceremony under the unit's limits, puts the launchers and units in place, makes the store the first time with every flag off, binds the room, makes the three feed tokens and the connector's token into `hermes`'s `~/.config/thornswood` (0600), finds the `hermes` command as that user's login shell does, stops the core and every worker before the store migrates, starts the core, checks it answers on loopback only and on this release's schema, and only then starts the feeds and the connector. Since step 5 it also makes the `tw-push` user and `/srv/tw/push`, puts the push worker's token there, checks one sealed, encrypted and signed push under the unit's limits, and starts the push worker once `--origin` is known. It changes no flag and makes no pairing code |
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
3. Turn alerts on in Settings, close the app, lock the phone, and run `phone-test.sh <commit> card`. One "Hermes needs you" should pop up with the phone sound (T11). Tapping it opens that exact card after sign-in, and the lock screen shows nothing else (T12, T13).
4. `phone-test.sh <commit> card high` checks the high-risk path: its check screen, then a passkey sign-in.
5. `phone-test.sh <commit> stop` takes 8443 off Tailscale Serve and stops both. The test store stays.

All of these passed on Patrick's phone on 1 Nov, the high-risk card included. Since then `start` also binds two test rooms ("Test room" and "Second test room") in `phone-test`, so the Rooms list can be tried. They are names only: no room is behind them, and chat stays off, so nothing is ever sent to them.

## After the first phone test

Patrick's first phone test (1 Nov) passed, and asked for these:

- **Tabs.** Work is now **Board** (`/m/board`), Needs you is now **Decisions** (`/m/decisions`, with its badge), and Room is now **Rooms** (`/m/rooms`): a list like Bots, one button per room this workspace is bound to, each opening that room at `/m/rooms/<room id>`. The first paths (`/m/needs-you`, `/m/work`, `/m/room`) still open, so an old alert or home screen link works. The app opens on Decisions, and its home screen name is **Hermes**. The Desktop pane's heading is Decisions too.
- **Rooms.** A room's chat target is its room id; `room` still means the first room bound. The room feed carries every bound room at once (`{source_time, rooms: [{room_id, events}]}`; the one-room shape is still taken), `feed_reader.py --room` takes one `--room` for each room, and `tw-decide bind-room --name` binds a room other than the Workbench. The connector sends each message to its own room's id, and a message is settled only by its own room's events.
- **An answered card stops showing.** After an answer the app goes back to Decisions, which says in one line what was chosen; the card is not listed again (2 Nov, Patrick: "I only want things waiting for me there": Decisions lists only cards waiting on him, and an answered card is reached only by its own link, with its receipt). The service worker, which shows the alerts, now takes them off: the app tells it which cards still need Patrick each time it reads Decisions, and which card was just opened or answered. An alert of a card answered on Desktop while the phone app is closed stays until the app next opens.
- **Alerts that stop reaching the phone.** Alerts were turned on in the Chrome tab before the app was installed; the installed app's subscription replaced that one, and the tab's answered 410. The core cannot see endpoints (they are sealed to the push worker), so it now keeps the SHA-256 of each one (migration 6) and `GET /push` says it, and says `lapsed` when the push service retired the subscription (404 or 410) or the operator reset the keys while the phone's yes still stands. Each time the app opens (and when it comes back after ten minutes) it compares that with the browser's own subscription: the same, nothing; a different one, it sends the browser's; none, one made with another key, or the very one that lapsed, it makes a new one and sends that. It never does this after Patrick turned alerts off or said no, or once the phone's permission is gone, and Settings says when it renewed them. `alertsPlan` in `app.js` is that choice, checked case by case in the browser check.
- **Installing.** Chrome keeps its install under the three-dot menu. In a browser tab the app shows a line saying where it is (Chrome's menu, Add to home screen, Install), or an **Install the app** button when Chrome hands it the install prompt, and Settings says to install first and turn alerts on from the installed app.

- **The look** (Patrick's answers, 1 Nov, from the mock-up): Thornswood's whole emblem everywhere (`icon-192.png`, `icon-512.png`, and `icon-maskable-512.png` with the emblem inside the safe circle on the night ground), dark only in the site's night palette with its gold (`--gold #C9A24B`, `--gold-hot #E4BE66`), the HERMES title in the site's Cinzel (under the SIL Open Font License in `cinzel-OFL.txt`; since 2 Nov the whole Latin set, `cinzel-latin.woff2`, 15 KB, for every screen's title and the avatars) and the phone's own font for everything else, and the alert line "Hermes needs you". Measured contrast: gold on night 8.1 to 1, text on night 15.5 to 1, quiet text 6.6 to 1, and warnings in a lighter ember at 5.2 to 1.

The fingerprint on the phone (Patrick, 2 Nov). A sign-in that asks for any passkey lets Android hand it to the passkey provider he chose (Bitwarden), which leaves the app, unlocks, takes the finger and comes back. On a paired phone the app offers its own key once on Decisions and always in Settings: `POST /passkeys/register/options {"on_phone": true}` asks for the phone's platform authenticator (`authenticatorAttachment: platform`, hint `client-device`, user verification required, and no exclude list, since the passkey app on the same phone holds the passkey). The app keeps the key's public id (`hermes.phoneKey.v1`) and names it in every sign-in (`POST /passkeys/sign-in/options {"phone_key": <id>}`, so `allowCredentials` is that one key with transport `internal`), and the phone answers with its fingerprint sheet over the app. The core checks it as any passkey: signed challenge, counter, user verification, an unrevoked phone. A key the core no longer has answers 404 `no_phone_key`; the app forgets it and the passkey signs in. Sign-in, high-risk answers, own-Hermes sends and deletes all go through that one sign-in, so all of them use the fingerprint. A Desktop is refused the phone key (`not_a_phone`) and keeps the passkey. When Android asks where to save the key, Google Password Manager keeps it in the app; Bitwarden would leave the app again.

Signing in: `GET /api/v1/sign-in/state` says only whether a passkey exists. With one, opening the app asks for the passkey at once, the way a bank app asks for a face or a fingerprint (Patrick, 1 Nov), and the screen's one button shows only if that prompt was dismissed, failed or was blocked. It asks by itself once each time the app opens, never again after a dismissal and never after signing out. Pairing and the recovery code stay folded away. With none it is "Set up this phone": the pairing command, the code, and the passkey prompt straight after the code works, inside the 5 minutes a pairing gives (the check is unchanged). When those 5 minutes ran out with no passkey, adding one says `setup_expired` and asks for a new pairing, not a passkey sign-in that cannot exist.

Checked here: the unit tests above, and `tests/decisions_phone_check.js` in Chromium (setting up the phone, the tabs and old paths, the Rooms list and a room's deep link, an answer leaving Decisions for good, the alerts of answered and opened cards taken off, every `alertsPlan` case, the install line, and the manifest). Not checked here: a real push service, the install prompt itself (headless Chromium never offers it), and Patrick's phone.

## Chat with Patrick's own Hermes

Patrick, 1 Nov: "I should be able to reach all three gateways and chats from the app". The three are the ones his Desktop picker shows: **thornswood-bots** (the Bots' gateway, with its Bot Chats and rooms, as before), **my-hermes** (his own Hermes on the VM, the `patrick-hermes` user) and **This device** (his Hermes on One). His answers on the report page set how it works:

- **Full power behind the passkey** (C). A message to his own Hermes needs `own_chat_write` and a session signed in with a passkey in the last 30 minutes (A, 30 minutes): the session's sign-in method and time come with every request (`auth.py`), and a pairing or recovery-code session never sends here. The app says until when it can send, and offers the passkey sign-in when it cannot.
- **One** (A): a small connector on One that reaches out to the core through Tailscale Serve; nothing listens on One. **One off** (A): refused at once with "One is off". A gateway is on while its connector was heard from in the last 2 minutes and said that Hermes's gateway was running.
- **Names and states** (Patrick's 8-item feedback and his mock-up answer A, 1 Nov): the app calls them **Hermes** and **One** (the ids stay `me` and `one`) and lists them first in one Thornswood Bots list with the six Bots. The grouping is presentation only. Each gateway has one state, with its own words: `ready`, `gateway_down` (the connector runs, Hermes's gateway does not), `check_failed` (the connector could not run its check, with the reason), `connector_stopped`, `machine_off`, `not_heard`, `missing` and `turned_off`. "One is off" is said only when the tailnet says One's machine is offline: the core keeps the tailnet address each connector posts from (Tailscale Serve's `X-Forwarded-For`, tailnet ranges only) and `tw-decide peers` reads `tailscale status --json` once a minute (`tw-decide-peers.timer`, as `tw-decide`, read only, no network). A quiet connector on a machine that is on is "connector stopped"; with no fresh tailnet view it is "not heard from", never "off".

| Part | What it does |
|---|---|
| `own.py` | His own two gateways, `me` and `one`. A message goes in `own_messages`, a table of its own, so the Bots' connector can never lease one, with its command record in the same commit (same key, same receipt). A message has a chat's `session_id`, or none for a new chat, whose id comes back from Hermes. One turn at a time in each chat; a lapsed lease is "uncertain" and never sent again by itself. Each connector's token (`own-me`, `own-one`) reaches only its own gateway. The chat list (at most 50 chats, the last 30 messages of up to 8 of them: the 5 newest and up to 3 the phone asked for) is refused while `own_chat_write` is off, so nothing of his chats is copied into the core before he turns this on. No message text goes into the event chain or a log |
| `own_connector.py` | One file, standard library only, the same on the VM and on One, run as that Hermes's own user. A new chat is `hermes chat -Q --format stream-json --query-file <file> --yolo --source cli --in <home>`; a turn in a chat that is there is the same with `--resume <id>` and nothing else, because Hermes keeps a chat's launch settings for good and refuses a resume that changes them (`contracts.json` `own_chat`). So a chat started elsewhere runs the phone's turn with its own approvals: a step that needs an approval is refused unless that chat has /yolo on, and nothing here turns /yolo on (Patrick's answer A on the page, 1 Nov: new phone chats have full power, and in an older chat he types /yolo there himself if he wants it). Only exit 0 with Hermes's own `result` saying 0 is "sent". It lists chats from `state.db` read only, the way Hermes's pickers do, and checks the gateway with `room_send.py` without starting one |
| `deploy/decisions/tw-decide-own`, `tw-decide-own.service` | my-hermes's connector as `patrick-hermes`, loopback only, at most 900 MB. `install.sh` makes its token once into that user's `~/.config/thornswood/own-me.token` (0600) and starts it, at each merge like the rest, when that user is there |
| `scripts/one/Install-OwnConnector.ps1` | Run on One from a clone at main: has the VM make One's token over ssh straight into `%APPDATA%\Thornswood\own-one.token` (his user only; it never shows), copies the connector into `%LOCALAPPDATA%\Thornswood-Hermes\own-connector` (never inside the launcher's Velopack folder `%LOCALAPPDATA%\Thornswood`, which Velopack's Setup.exe deletes on a reinstall; it refuses one), runs one round to check it, and adds the task "Thornswood Own Hermes Connector", which starts it at every sign-in under `--supervise`. It is run once: from then on the connector keeps itself current from that clone (below) |
| The way back (`schema.DOWNS[7]`) | `tw-decide downgrade --to 6` takes schema 7 back only while no message was ever sent to his own Hermes and `own_chat_write` is off everywhere. It drops the two own tables, the `own_chat_write` flag rows and the `own-me` and `own-one` tokens, and says so. The next install makes my-hermes's token again by itself, because `install.sh` asks the core (`token-known`) whether it still keeps the token in the file. On One, `Install-OwnConnector.ps1 -NewToken` makes One's |
| `deploy/decisions/tw-decide-peers.service`, `.timer` | `tw-decide peers --workspace thornswood` once a minute: the tailnet's view (Online, LastSeen) of each address a connector posts from, into `own_gateways` (migration 8). `install.sh` step 13 starts it |
| `app/app.js` | Four tabs: Bots, Rooms, Board and Decisions (`/m/bramble` still opens Bramble's chat, under Bots). The Bots tab is the Thornswood Bots list: Hermes ("your own Hermes on the VM"), One ("your Hermes on One"), then Bramble, Rift, Sprocket, Echo, Magpie and Wisp, each gateway with its state in a few words. `/m/hermes/<me or one>` is that Hermes's chats, with "Start a new chat"; `/m/hermes/<me or one>/<session id>` is one chat, as text, with what Patrick sent and how it went |

Checked here: `tests/test_decisions_own.py` (the flag, the 30-minute passkey rule, "One is off", idempotency, one turn per chat, the Bots' connector never seeing one, the tokens, migration 7 keeping every older row, the restore quarantine, the routes, and the connector against a scratch `state.db`, a stand-in `hermes` and a stand-in readiness check, and One's installer with a stand-in ssh), and the browser check. Not checked here: a real Hermes turn on the VM or One.

## Full history of his own chats (step 2)

Plan Part 4, for Hermes, One, each Bot's Bot Chat and each room.

- **Bot Chats and rooms.** The Bots' connector reads these pages: a Bot's one Bot Chat from that profile's `state.db`, and a room's messages from `shared-state.db` by the room's own sequence number. It asks `POST /connector/pages` every few seconds and posts with `POST /connector/page`. This is reading only, so it runs whatever `connector_effects` says, and only while `mobile_read` is on.
- **Each page is checked against its request.** A page names the Bot or room it read, and that must be the one asked for. Neither the own-Hermes tokens nor One's connector can answer one.
- **Route.** The phone asks `GET /chat/<bot or room id>/messages?before=&wait=`. A room is reached by its id, as the chat routes already do.

- **Where a page comes from.** The phone asks the core, and the core hands the request to that gateway's connector on its next claim, within about 5 seconds. The connector reads Hermes's `state.db` read only and posts the page (`POST /own/<gateway>/page`, its own token). A page holds 50 messages, or 50 chats.
- **Ordering and compression.** Messages are in Hermes's own message-id order, across the chat's whole compression lineage. A compressed chat's id says what it goes on as (`continued_as`), and the phone follows it there and says so.
- **Memory only.** The core holds a page in memory, never in the store or a log: 10 minutes for an older page, and 20 seconds for the newest one, which changes as the chat goes on. `GET /own/<gateway>/chats?before=<time>&q=<words>` is the picker, newest first, with a title search. Its first page is the chat list the connector already sends. `GET /own/<gateway>/chats/<chat id>/messages?before=<message id>&wait=<seconds>` is one page, answered at once from memory or within the wait once the connector posts it. Until then it answers `{"pending": true}`.
- **Chat ids stay with their gateway.** A chat id answers only on the gateway whose own connector listed it: in its chat list, on a page of chats, as a chat Patrick started there, or as what a compressed chat goes on as. So an id from One never opens on Hermes, and the reverse.
- **No silent new chats.** A message to a chat Hermes no longer has comes back "not sent: that chat is gone", and Hermes is never run for it. "New chat" is the only way a new conversation starts.

Checked here: `tests/test_decisions_history.py`:

- 500 messages page back to the first, in order, with no gaps and no repeats;
- the picker pages and searches titles;
- a compression lineage is read and followed;
- a deleted chat is gone and its message is refused with no turn;
- a chat id is refused on the other gateway, and another gateway's token and the Bots' connector are refused;
- nothing of a page is in the store's files;
- the newest page goes after 20 seconds.

The browser check covers the picker search, a long chat paged back to its first message, a gone chat, and ids that do not open on the other gateway.

## The chat layout and Markdown (redesign step 1)

Plan Part 11, build step 1, after Patrick approved the redone mock-up (answer A on q-hermes-app-mockup-2, 1 Nov). App only: no route, table or connector changes.

- **Bubbles.** In a chat with Hermes, One, a Bot or a room, Patrick's messages are gold bubbles on the right and everyone else's are on the left, with the sender's name once per run and a day line (Today, Yesterday, then the date) when the day changes. Under each bubble: the time, and for what Patrick sent from the phone, how it went (Waiting to send, Not sent and the reason).
- **Markdown.** Paragraphs keep their line breaks; bold, italic, struck text, inline code, fenced code blocks with their language and a Copy button, lists (nested too), quotes, headings, rules and tables are drawn. Every node is made by `app.js` and filled with text: nothing in a message is read as HTML, no image is drawn from a message, and a link becomes a link only for an `http` or `https` address, opening in a new tab with no referrer. An underscore inside a name (`a_snake_name`) stays as it is. Copy falls back to selecting the code where the clipboard is refused.
- **The foot of a chat.** The message box grows with what is typed, up to six lines, and stays above the tabs; the round gold button sends. Enter makes a new line. The box keeps its draft, and the draft's idempotency key, while the chat redraws.
- **Where a chat is scrolled.** Opening a chat, or sending, goes to the newest message. When the chat redraws while Patrick reads further up, it keeps his place and a "N new messages" button above the box takes him down. Older messages keeps the message he was looking at on screen.
- **The bar on top.** Back (to that gateway's chats, the Thornswood Bots list or the rooms), the chat's name, and where it lives.

Checked here: `tests/test_decisions_phone_browser.py` draws a Markdown message with a `javascript:` link and raw HTML in it and checks each piece, that only the `https` link is a link, that nothing of the HTML is drawn, that Copy copies or selects, that a chat opens on its newest message, that Older messages keeps the place, and that at 412 pixels wide nothing scrolls sideways and the box ends above the tabs.

## Reactions in Hermes, One and the Bot Chats (redesign step 2)

Plan Part 11, build step 2. A reaction is Hermes's own: Hermes keeps one per person on a message row (`display_metadata`'s `reactions`, author `user` for Patrick and `agent` for Hermes or the Bot), Desktop shows the same ones, and Hermes tells the model about a new one on a later turn only when that Hermes or profile has `display.message_reactions` on (`contracts.json` `reactions`). Nothing here turns that on.

- **On the phone.** Tapping a message (or Enter on it) opens its actions: eight reactions (👍 ❤️ 😂 😮 😢 🙏 ✅ 👀), Take my reaction off when one is on, Copy text and Close. Patrick's reaction shows at once, dashed until the connector says it is set; tapping his chip takes it off. Hermes's or the Bot's own reactions show beside his, with its name. A reaction that could not be set says why under the message. Rooms have no reactions in Hermes, so a room's actions offer Copy only, and so does the feed's last copy of a Bot Chat before Hermes's own page comes.
- **One table for all three.** `chat_reactions` (migration 9) keeps one row per message: what should be there, with a version. The gateway is `me` or `one` (the chat is a session id) or `bots` (the chat is the Bot's profile id, since each Bot has one Bot Chat), so neither connector is ever handed the other's. A second tap before the first is set replaces it, and the same tap twice is one. A lease that ran out after 2 minutes goes out again, because setting exactly this emoji is the same whether or not the last try got there. An answer about an older version than the newest is dropped. No message text goes into the event chain (`reactions.py`).
- **His own Hermes.** `POST /own/<me or one>/react` with `{session_id, message_id, emoji}` (or `null` to take it off). It needs `own_chat_write`, a chat that gateway listed and that gateway answering, but no fresh passkey: it runs no turn. That gateway's claim hands its connector up to 10 to set, and it answers on `POST /own/<gateway>/reacted`.
- **A Bot Chat.** `POST /chat/<bot>/react` with `{message_id, emoji}`. It rides `mobile_chat_write` like a message to that Bot, and a room refuses it. The Bots' connector is handed them with its claim, so only while `connector_effects` is on, finds that Bot's one Bot Chat in its `state.db` read only, and answers on `POST /connector/reacted`.
- **The helper.** Both connectors set each one through `room_send.py` op `react` in Hermes's own Python and through Hermes's own `SessionDB` (a Bot's through its profile's store), writing only when Patrick's reaction differs from what is asked, so asking twice never takes it off (Hermes's own rule takes the same emoji off). Hermes checks the message is in that chat's lineage. The pages carry each message's reactions.
- **Which copy shows.** A page now says when the core handed out the ask for it (`handed_at`, the core's clock; `read_at` is the reading machine's). A set reaction shows from the core's row until a page asked for after it was set, so a clock on One that runs ahead never hides it. What Patrick sent from the phone gives way to Hermes's copy by the same time.
- **The way back.** `tw-decide downgrade --to 8` refuses while a reaction is queued or leased, then drops `chat_reactions`. Reactions already set stay in Hermes.

Checked here: `tests/test_decisions_reactions.py`:

- the rows: one wish per message, set and taken off, a late answer dropped, a lapsed lease handed out again, a failure and a new tap, nothing handed out while Hermes is not ready, the refusals, each token only for its own gateway;
- a Bot Chat: `mobile_chat_write`, a room refused, set only while `connector_effects` is on, and kept apart from his own chats;
- the routes for both, and that a browser can never answer for a connector;
- a page keeping only Hermes's reactions, and `handed_at` on the core's clock;
- both connectors against a scratch `state.db` (one without the column too), and a Bot with no Bot Chat;
- the helper against a stand-in for Hermes's store with Hermes's own rule.

The browser check: Hermes's and Bramble's reactions show with their names, a tap opens the actions, a reaction shows at once and asks for exactly that message and emoji, it stops being dashed once set (in One's chat and in Bramble's Bot Chat), and it comes off. Not checked here: a real Hermes on One or the VM, and Desktop showing a reaction set from the phone.

## Renaming, hiding and deleting his own chats (redesign step 3)

Plan Part 11, build step 3, for his own Hermes and One only: never a Bots' Bot Chat, his own Bot Chat or a room. Each is done with Hermes's own call, as Hermes's own commands do it (`contracts.json` `sessions`), and all of it is off until Patrick turns on `session_manage_write` (`tw-decide flag`), on top of `own_chat_write`.

- **On the phone.** A chat of his own has Chat actions (the dots in its bar) while that gateway answers: Rename, Hide this chat, and Delete for good. A request shows at once ("Renaming to ..."), and a failure says Hermes's reason. A hidden or deleted chat says so and offers no send. Hidden chats, under the chat list, lists the chats Hermes hides, from Hermes itself, each with Bring back.
- **Rename** is `hermes sessions rename`: Hermes's own `set_session_title`, which refuses a title another chat holds or one over 100 characters. The core takes one line of text and closes up its spaces.
- **Hide and Bring back** are Hermes's archive: `set_session_archived` on the whole chat (every compressed part of it), with every message kept. Hiding is never deleting.
- **Delete for good** is `hermes sessions delete`. Hermes refuses it while a turn runs or one is unfinished in the chat or a side task of it, and says why. It deletes that chat with all its messages, the earlier parts Hermes compressed it from and the side tasks (delegate chats) Hermes ran for it; a chat branched off it is kept, and nothing else changes. Before anything is asked, the phone says exactly that, and Keep it changes nothing. A delete needs a passkey sign-in from the last 30 minutes: when there is none, the prompt opens by itself on Delete for good. What Patrick sent from the phone stays in the core's record.
- **In the core.** `POST /own/<me or one>/session` with `{session_id, op, title, idempotency_key}` (op `rename`, `archive`, `unarchive` or `delete`). It needs a chat that gateway listed (for Bring back, one a page of its hidden chats listed) and that gateway answering. `own_session_ops` (migration 10) keeps each request with its command record, so the same key again gets the same receipt. No title goes into the event chain. `GET /own/<gateway>/chats/archived` asks for a page of the hidden chats, like the chat list's own pages.
- **The connector.** That gateway's claim hands its connector one request at a time in each chat, up to 5, even while every turn slot is busy, and it answers on `POST /own/<gateway>/session_done`, then feeds the chat list at once. Each request ends the same however often it runs, so a lease that ran out after 2 minutes goes out again, and a delete of a chat already gone is done. `room_send.py` op `session` does it in Hermes's own Python through Hermes's own `SessionDB`.
- **His own Bot Chat is kept as it is.** Bot Mode and every Bot's message to him find it by its exact title while it is hidden (`contracts.json` `sessions` `bot_chat`), so a new title, a hide or a delete would cut him off from it, and Hermes's own apps never offer these for it. On the phone it has no Chat actions. The core refuses a request for a chat the connector listed as his Bot Chat (`bot_chat_kept`), and `room_send.py` refuses it again by Hermes's own record, for the canonical row and every part Hermes compressed it into. Nothing is written.
- **The way back.** `tw-decide downgrade --to 9` refuses while a request is queued or leased or `session_manage_write` is on, then drops `own_session_ops` and the flag.

Checked here: `tests/test_decisions_sessions.py`:

- the requests: the flag, the title rules, the idempotency key, a chat that gateway never listed, the passkey for a delete only, and the gateway answering;
- one at a time in each chat, a lapsed lease handed out again, a failure with Hermes's reason, a late answer;
- the routes, and that a browser can never answer for a connector;
- the connector and the helper against a stand-in for Hermes's own calls, including a delete Hermes refuses while a turn runs and a delete of a chat already gone;
- his Bot Chat refused by the core as listed (in the chat list or a page of it) and by the helper for each part of it, with nothing written, and the Bots list naming the chat last written in rather than the Bot Chat listed first;
- migration 10 and its way back.

## A gateway that is not ready is not a failed message (1 Nov)

Patrick answered a card at 10:17 PM on 1 Nov while an install was reloading the
Bots' gateway. The connector's readiness check got no answer, the connector
called that a failure, and the "Bot told" message was dead after one try, so
Bramble never got the answer. Now:

- the connector's outcome for a gateway that is not ready is `not_ready`
  (`connector.py`: the Bot Chat readiness check, or a room send the helper says
  never reached a ready gateway). Nothing was sent, so `POST /connector/result`
  puts the message back to `queued` (`chat.py` `_not_ready`): lease cleared,
  that attempt not counted, no effect receipt, and an event
  `connector.not_ready` or `chat.not_ready`. The phone shows a chat message
  waiting with the reason.
- after a `not_ready`, each round the connector checks that gateway once and
  claims nothing until it is ready, so nothing is leased and put back over and
  over. There is no timer: the message goes on the first round the gateway
  answers ready.
- a real failure (the `hermes` command cannot start, the gateway refuses a room
  message, the room's driver is not running) is still `failed` and dead.
- migration 11 puts back in the queue each "Bot told" message that died only
  of a readiness check (its reason is one of those checks' and its every
  receipt is that failure), on a card still answered. Its failed receipt stays
  as history. Chat messages are left as they are. Its way back changes nothing.

### The install stops every worker before the store migrates (1 Nov)

At 10:53 PM the install of the fix above ran migration 11, which put Patrick's
answer A on dec_fWitODbwp3NuQ7dNhxaRTw (#5327, for Bramble) back in the queue.
The release before's connector was still running: three seconds later it leased
that answer while the Bots' gateway was reloading and failed it the old way, so
it was dead again with a second failed receipt, and only then was it restarted
on the new code. Now:

- `install.sh` step 5b stops the timers (first, so none starts its service
  again), every worker that leases from the core or opens its store
  (`tw-decide-bots`, `-connector`, `-push`, `-own`, `-feed`, `-peers`) and the
  core itself, and checks each really stopped, before its first `tw-decide`
  command opens the store and migrates it. Each starts again, with
  `reset-failed` first as before, only after the core answers on loopback and
  `tw-decide schema` says the store is at the schema this release knows (step
  8). `tests/test_decisions_install_order.py` pins that order, and finds the
  workers from the unit files, so a new worker unit must join the stop.
- the same race hit the feed at 10:57 PM, in the install of #74: the feed
  timer's run posted the new `work` snapshot, with `items`, at 10:57 PM while
  the release before's core still served (it restarted 38 seconds later) and
  dropped `items`, so the Board was empty until the next read. Both timers that
  post to the core (`tw-decide-feed.timer`, `tw-decide-peers.timer`) are in
  that stop, before the core restarts, and start again only after the core
  answers on the new code; the order test pins them too.
- migration 12 is migration 11's rule once more (the same SQL), for that
  answer: dead only of a readiness check, every receipt that failure, the card
  still answered. Against a read-only copy of the VM's store it changes that
  one row and nothing else. Its way back changes nothing.

The browser check: a chat's actions, a rename that shows at once and then from One, hiding (off the list, no send), Hidden chats from One with Bring back, the delete's words before anything goes, Keep it, a delete that goes through, no chat actions on a Bot Chat, and his own Bot Chat first on One's list, marked Bot Mode, opening with its messages and no chat actions. Not checked here: a real Hermes on One or the VM.

## The pinned contract (step 0)

`contracts.json` records what the core relies on in Hermes, each fact with the file and line it was read from at the pinned commit:

- **Hermes v0.23.0** is upstream commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` (tag `v0.23.0`), read again on 8 Nov. Until then it was v0.22.5 (`bf0203a794a07aac3ff764972ab9087baa4e4f4e`), the commit the 29 Oct audit read; v0.22.5's update check made every hermes command on the VM take 40 seconds (ThornswoodRP/Thornswood#5520).
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

## One's connector keeps itself current, and says what it is doing (Pixel check, 2 Nov)

Patrick's Pixel check found One's chat on "Waiting to send" with One's gateway up, his Bot Chat missing, and One's chats short of their history. What the code showed:

- **The core takes a message only while the connector says the gateway runs** (`own.py` send, `online`), so a message waiting to send was never held by the gateway check. It waits when the connector is already running as many turns as it runs at once (3), when an earlier message in the same chat is still being answered, or when the connector cannot take it. The phone could not tell these apart: it said "Waiting to send" and nothing more.
- **One's connector was the copy its installer put there.** Nothing updated it, while the VM follows main by itself. The copy installed on 1 Nov came before full history (#44) and reactions (#54), so One never answered a page and the phone showed only the feed's last 30 messages of a chat.
- **His Bot Chat is hidden from Hermes's own pickers**, and the connector listed chats as the pickers do, so it never showed.

The fixes:

- **The connector says what it is doing.** Each claim carries its commit, how many turns it runs of how many and since when the oldest runs, and an update it is on or could not do (`OwnConnector.busy`). The core keeps it in memory (each claim says it again within seconds) and gives each waiting message its reason in words (`waits`): behind an earlier message in this chat, behind three running turns with the oldest's age, updating itself, an update it could not do, or an old copy that says nothing and needs its one-time update. The gateway's own screen says the same of the connector (`connector`).
- **One's connector follows the release the VM runs.** The core names its release (the commit folder it runs from, which the VM installs only after the whole suite passed there) in every claim answer. A connector started with `--clone` (One's task) fetches main in Patrick's own clone, refuses a commit that is not on main, takes `own_connector.py` and `room_send.py` from that commit with `git show`, compiles both before either is put in place, writes `COMMIT`, finishes the turns it runs, takes no new one and stops with code 75. `--supervise`, which the task runs, starts it again at once on the new copy, and a minute after any other stop; on Windows it holds the connector in a job object, so stopping the task never leaves one behind. The code comes from GitHub through his clone, never from the VM. A failed update changes nothing, is said on the phone, and is tried again only for a newer release or at the next sign-in. The VM's connector has no `--clone`: the VM's follower installs it with each release.
- **Git never waits on a sign-in prompt** (2 Nov, the install on One). Nobody is there to answer one, in the install or under Task Scheduler, and One's git asks its credential helper selector by default, a window that waits for a click: the first install hung on its fetch. `Install-OwnConnector.ps1` now sets the clone's own GitHub sign-in to gh (`credential.https://github.com.helper`, an empty entry and then `gh auth git-credential`, in the clone's config only), stops first if gh is missing or not signed in, and runs git with prompts off. The connector's updates run git with `credential.interactive=never`, `GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=never`, so a fetch that cannot sign in fails and the phone says so.
- **His Bot Chat comes first** in Hermes's and One's lists: the hidden, unarchived row titled exactly "Bot Chat" (Hermes's canonical one), by the id it goes on as after Hermes compresses it, marked Bot Mode. An archived one is retired and not shown.

Tests: `test_decisions_own_update.py` (what the connector says and the words for each case, the release, an update from a scratch clone: main only, a copy that does not compile, a fetch that fails, finishing turns and stopping, a failed update not tried twice, the supervisor, and on Windows the job object) and `test_decisions_own.py` (the Bot Chat first, after compression, in pages and searches, and an archived one left out). The copy on One installed before this needs `Install-OwnConnector.ps1` run once more to get the self-update; that is the last time.

## The approved look on every screen (Pixel check, 2 Nov)

Patrick's Pixel check: the app "looks very different" from the approved mock-up v2, which is "colorful and has everything aligned nicely and lots of color and pictures and logos". The cause: steps 1 and 2 built the bubbles and reactions on the old plain style (boxed rows of text, a word-only tab bar, no avatars, one gold for every name), and nothing carried the mock-up's visual system into the app. This brings the whole system over, once, for every screen (`app.css`, `index.html`, and the helpers `avatar`, `icon`, `screenBar` and `listRow` in `app.js`):

- **Each agent's colour and initial**, the mock-up's: Hermes gold, One teal, Bramble green, Rift blue-violet, Sprocket copper, Echo blue, Magpie lilac, Wisp sea green (`.a-<id>`). The same avatar shows in the Thornswood Bots list, on a chat's bar and on the gateway's screen, and the sender's name over a bubble is in that agent's colour. A room has a plain avatar with its first letter.
- **Every screen opens with a bar**: back where there is somewhere to go back to, the avatar, the title (in Cinzel on a list screen) and a line under it, and the state on the right. The app's own bar keeps the Thornswood emblem and HERMES in gold, with a line-icon Settings button.
- **Lists are full-width rows**: the avatar on the left, the name, what it does in gold and the last message under it, and the state chip (One's teal when on, ember when not) or the time on the right. His Bot Chat row is shaded and marked Bot Mode. Decisions waiting on him are counted in the section label, and a high-risk card carries the ember tag.
- **The tabs carry the mock-up's line icons** over their names, with the count on Decisions as a gold badge.

The browser check runs every screen at Pixel widths and with larger text, as before, and saves the screenshots it always did.

## Online and offline dots (2 Nov)

Patrick: "the design had green and red circles to indicate bots online or offline yet that isn't in the app". Each agent's avatar now carries a dot, and each Bot's row says its state in words on the right; a Bot Chat's bar says what the Bot does and its state ("Bramble · router, merges · On"), and his own chats' bars say the same of Hermes or One. Green is running, red is not, amber is on its way up or down or not known. Nothing is made up:

- **The Bots**: every half minute the Bots' connector runs `room_send.py` op `states`, which asks Hermes for each Bot's gateway the way the readiness check does (`discover_gateway_endpoint`, never starting one) and prints Hermes's own state (`absent`, `starting`, `ready`, `draining`, `incompatible`, `inaccessible`, `conflict`, or `check_failed` for a check that raised). It posts them to `POST /connector/live` with its own token, whatever `connector_effects` says (reading only, under `mobile_read`); the core's answer names the Bots to check. The core holds the last report in memory only, like the pages: after a restart, and whenever no report came in two minutes, the phone says "Not heard from", never the last state it heard.
- **Hermes and One**: their own state, as before (`ready`, `machine_off`, `gateway_down`, `connector_stopped`, `missing`, `turned_off` red; `not_heard` and `check_failed` amber).

`tests/test_decisions_live.py` covers the helper against stand-ins for Hermes, the connector's timing and its first report, the core's checks and staleness, and the route. The browser check reads each row's dot and words and the Bot Chat's bar.

## The desktop layout (2 Nov)

Patrick opened the app on One and got the phone's one column, in a browser and installed. The cause: `app.css` had no rule that looked at the window's width, so every screen was laid out for a phone; the mock-up's Desktop board was never built. Now the width alone decides, and the layout is redrawn when it changes (turning a phone, or widening a window):

- **Under 1000 pixels**: the phone, unchanged.
- **From 1000**: the tabs become a rail on the left with Hermes, One and the six Bots above them (each a link to its chats, the open one ringed); the chat list sits beside it (the Thornswood Bots list beside a Bot Chat, a gateway's chats beside one of its chats or its Hidden chats, the Rooms list beside a room) with the open chat marked; the conversation fills the middle, its lines no wider than 880 pixels. A list on its own (Bots, Rooms, a gateway) goes in the list pane and the middle says what to pick.
- **From 1360**: Decisions on the right, kept current by the same poll as the badge, except on Decisions itself.
- Both panes fold away from the rail's buttons for the visit. Each pane scrolls by itself; the middle scrolls the page, so a chat keeps its place, its bar and its box as on the phone. Sheets open in the middle of the screen.

The panes are drawn by the phone's own views (`decisionsView`, `botsView`, `roomsView`, `gatewayView`), each told which pane to fill (`showIn`); nothing is fetched or kept twice. The browser check covers the desktop at 1600 and 1200 pixels, the rail, the marked rows, folding, no pane twice, the long-link check at 1440, and narrowing back to the phone.

## The Board shows the Bots' work on GitHub (2 Nov; replaced the same day by the live Board, below)

Patrick: the Board tab showed nothing while the Bots worked, so he could not see which issues they were on or how far along they were. The cause, measured on the VM: the Board read only Hermes's Kanban (`kanban.db`, `tasks`), and the Bots keep no Kanban. Its table had no rows, so the feed sent `{"tasks": []}` every minute. The decision core's `task_assignments` was empty too. The Bots' work is on GitHub, recorded two ways the guard rails already enforce: a Bot's claim label on the issue (`claimed: <Bot>`, `policy.yaml` `claim_label`) and its branch, `<bot>/<issue>-<what>` (`branches.pattern`), which heads every pull request it opens.

What the feed reads now (`feed_reader.github_work`), once in each 5 minutes:

- the open issues with each Bot's claim label, in Thornswood and Thornswood-Ops (skymp has issues off);
- the pull requests from a Bot's own branch in the three work repos, open or closed in the last day;
- for each open one, its head's check runs, as one state (passed, running, failing, none) and a line like "18 of 20 passed, 2 failed".

A Bot's pull request goes with the issue its branch names when that Bot has the issue claimed (a skymp branch names a Thornswood issue); an open pull request wins over a finished one. A Bot's pull request for an issue it has not claimed is its own item. Each item says the repo, the issue's number and title, which Bot has it, its stage (claimed with no pull request, pull request open or draft, merged, closed), the checks, whether auto-merge is on, and when it last changed.

How it reads, and what it does not get: through tw-git's `read` op on `/run/tw/git/git.sock`, which `hermes` may already call (GETs inside the three work repos only, as the workers' App). It reads as Bramble, since tw-git names a Bot for each read. The feed gets no GitHub credential, its unit keeps its loopback-only network, and nothing is written to GitHub. Reading GitHub every minute would spend the workers' App's hourly limit, which five Bots share, so a run reads it only in the first 65 seconds of each 5 minutes (about 22 reads each time, under 300 an hour); between those the Board's last snapshot stands with its own "as of". If a read fails, nothing is sent and the last Board stays, with its time.

The core keeps the items to these fields (`feeds.py` `_items`): a repo must be `owner/name`, a stage and a check state must be ones the Board knows, and numbers are numbers. The app builds every link itself, to `https://github.com/<repo>/pull/<n>` while a pull request is open and to the issue otherwise, so a feed can never put another link on the phone. The Board lists the six Bots in the Bots list's order, each with what it has open and what finished in the last day, or "Nothing claimed". Hermes's Kanban tasks still show under them when there are any.

Not on the Board yet: the project board's Status and Order. Neither App tw-git holds can read the org's project (that needs Organization projects permission), and the App that can (thornswood-board) keeps its key off the VM. That is Patrick's choice to make; until then the stage above (which board.yml follows: an issue's board Status comes from its pull requests, #5185) stands in for Status.

Tests: `test_decisions_phone.py` `GitHubWork` (claims and pull requests into items, the order, reads only inside the three repos, a failed read sends nothing, the 5-minute pace, tw-git's read op as Bramble, and the run that skips or keeps the Board) and `FeedsIn` (the fields kept and anything else cut). `decisions_phone_check.js` checks the Board in Chromium: each Bot's rows, the progress line and both kinds of link.

## Formatting everywhere, clean quotes and no clutter (Pixel check, 2 Nov)

Patrick's Pixel check of the Rooms previews and of Reply, 2 Nov: "it should render properly everywhere in the app", "I hate clutter", and a Reply to One's Bot Chat that showed "Waiting to send" long after it had gone.

- **One way to draw a message.** A message whole is drawn as it always was (`markdown`). A message in a short space, a list's line or a reply's quote, is drawn by `snippet`: the same bold, italic, code, links and mentions, with its blocks (headings, bullets, quote marks, fences, a table's rule) laid out as one run, cut by lines at a whole word (two in a list, three in a quote). A link inside a row is drawn as one but is not one, since the whole row is tapped. Cards draw their title, evidence, options and "What yes covers" the same way.
- **Mentions and files.** `@bramble` is drawn as "@Bramble" in Bramble's colour, and the same for each Bot, Hermes, One and Patrick. A file or other reference (`@file:`, `@folder:`, `@url:`...) is drawn as its name, plain text that opens and attaches nothing.
- **The sender apart.** A room's last line starts with who said it, by name and in their colour ("Magpie:"), never the raw id.
- **A quote never asks Hermes for a file.** Hermes reads `@<kind>:<value>`, `@diff` and `@staged` anywhere in a message as a reference and attaches it. A Reply's quote held a paste's reference, so the paste was attached again, and one cut inside a reference asked for a file that does not exist. Now the phone quotes a paste by its words, and the core (`own.inert`) leaves out what Hermes attached and turns each reference left in a quote into its name in brackets ("[pasted text]", "[file: notes.md]"), so nothing in a quote is a reference. `tests/test_decisions_replies.py` checks a quote against Hermes's own two patterns.
- **A chat reads itself again under the box.** A chat was not read again while its box had the focus, and putting the keyboard away leaves the focus there, so a message's state stood still. Now the messages are drawn again every 15 seconds and the box, with what is in it and the keyboard, stays as it is.
- **Tapping a pasted file's chip** opens the chip, no longer the message's actions as well.
- **No clutter.** No line under a tab's or chat's title ("Each group chat, as of ...", "A chat is never an answer to a card", "your Hermes on One"), no "as of" footers, and no help paragraphs over the new chat box or the hidden chats. A card shows "high risk" when it is; its revision and state are in the receipt.

## A chat answers a card (Pixel check, 2 Nov)

Patrick, on the line "A chat is never an answer to a card": "Which is wrong anyways it should be".

- **The cards in the Bot Chat.** A Bot's Bot Chat ends with the cards that Bot asked and that wait on him (`GET /chat/<bot>` `cards`, `DecisionCore.asked_by`), each with its options. A tap answers it as on Decisions, a high-risk one through the passkey screen, and he comes back to the chat. A room shows none.
- **A message that answers.** When his words start with an option's label or key, as a whole word ("Wait, until tonight"), the line over the box says which card and which option they answer; the longest name wins, two options alike answer nothing and say so, and the x sends the words without answering. The app never guesses past that, and the core never reads an answer out of a message by itself.
- **One answer path.** A message sent with it carries the card, the option and the revision he saw (`answer` on `POST /chat/<bot>`). `Chat.send` takes it first through `DecisionCore.answer`, with every check a tap has: only a card that Bot asked, still waiting and not expired, the exact revision, an option of that revision, and a passkey sign-in from the last minutes for a high-risk card (the app opens the passkey prompt). His words are the answer's note. Nothing is sent when it is refused. The Bot is told the answer as for a tap and gets his words as a message too. The same send again is the same answer and the same message.
- His own Hermes and One ask no cards, so a message there still answers none.

Tests: `test_decisions_card_chat.py` and the Rift step in `decisions_phone_check.js`.

## Where a card's alert spent its time (2 Nov)

Patrick: Bramble's #5334 card was stored at 12:47 PM and reached his phone around 12:50 PM, "there has to be a delay we need to fix", and "I shouldn't have had to do a card for that" about measuring it. The alert now goes with Urgency high (#93). Each card also says, to the second, when it was stored, when the core's worker handed its alert to the push service (`alert` on the card, from `push_outbox`), and when this phone got it (the service worker keeps that time on the phone, in the `tw-alerts` cache, never sent anywhere). A late alert shows which hop was slow, and nobody is asked to measure it.

## Photos and files on a message to his own Hermes (Pixel check, 2 Nov)

Patrick: "no way to add files or photos or anything add a + button like Claude and Hermes have".

- **The +.** In a chat with Hermes or One, the + beside the box offers Take a photo, Photos and Files. Each file is uploaded to the core as soon as it is picked (`POST /own/<gateway>/attach?name=`, the file's bytes as the body, the app's cookie and CSRF token), shows above the box with its size, and can be taken off before sending. A message can be files alone. Up to 10 a message and 20 MB each; at most 20 files (100 MB) wait unsent, and one never sent goes after a day.
- **Only with one message, only to that Hermes.** Sending names the files (`attachments`); a file goes with one message, only to the Hermes it was added for, and only once (409 `no_attachment` otherwise). The message's SHA-256 covers its files, so the same send key with other files is refused.
- **Only that connector gets the bytes, only while it sends.** The claim carries each file's id, name, type, size and SHA-256. The connector fetches the bytes (`POST /own/<gateway>/attachment`, its own token, never a browser), checks size and SHA-256, and saves the file in that Hermes's own folders under its home: a photo in `images/phone/<message>/`, any other file in `attachments/phone/<message>/` (folder 0700, file 0600; a name the core never gives is refused). The first photo goes as the turn's image (`hermes chat --image`), every other file as an `@file:` reference after his words, as Hermes Desktop attaches them. A file that does not come whole fails the message before Hermes is asked. The core drops a file's bytes once its message has a result, so the file then lives only in that Hermes's folder.
- **Never a page.** The core gives a file's bytes only as a download (`application/octet-stream`, `Content-Disposition: attachment`), so nothing uploaded runs or renders on the app's origin, and the CSP is unchanged.
- **Schema 14** adds `own_attachments` and lets a message have no words. The way back stops while a file is on its way or a message is files alone.

Tests: `test_decisions_attach.py` (the core, the connector with a stand-in `hermes`, and over HTTP) and the + step in `decisions_phone_check.js`.

## One live line while a message is answered (2 Nov)

Patrick: "There's also no typing or thinking indicators and no like what they are currently doing", then "Those would be the same lines". The cause: One's connector ran each turn with `hermes chat -Q` and read only the final answer, so nothing knew what a turn was doing until it ended, and the app drew nothing between his message and the reply.

Now one line, under the message being answered, says both that the turn is working and what it is doing:

- **From Hermes's own stream.** The connector runs turns with `--format stream-json` and reads Hermes's events as they come (`hermes_cli/stream_json.py`): the start and a tool's result are "thinking", text is "writing", and a tool's start is that tool by name. Each change is said once. Only the state and a plain tool name (letters, digits, `_ . : -`, at most 64) leave the turn: never the words of the reply, a tool's input or its output.
- **Said with every claim.** The connector's claim, a few seconds apart while it runs, carries `turns` (each message id, its state, and the tool when the state is a tool), and stops carrying a turn once it ends (`OwnConnector.busy`). The core keeps them in memory like the rest of what the connector says, only well-formed ones (a known state, an `own_` id, a plain tool name, at most 10), puts each on the message it answers in his chat view (`doing`), never on one still waiting, and drops them when the connector has not been heard from.
- **One line in the app.** The message being answered gets a bubble from that Hermes with working dots and the words: "Starting", "Thinking", "Writing", or the tool in words ("Running a command", "Reading a file", "Searching the web", "Using the browser", otherwise "Using <tool>"). It is a status for screen readers, the dots stand still when reduced motion is on, and it is redrawn every 2 seconds from the chat's own view until the turn ends, then the chat is drawn again with the reply (not while he is typing).

Not in this yet: the same line for a Bot Chat (the Bots' connector still runs `-Q` and reads only the answer) and a Bot's own work started from Discord or GitHub (which needs the gateway's session list). Both come next on the same events.

Tests: `test_decisions_doing.py` (the events from a stand-in `hermes` in order and never the words, said with each claim while the turn runs and not after, the core keeping only well-formed turns, on the leased message only, gone when the connector goes quiet, and over HTTP). `decisions_phone_check.js` opens a chat whose message is being answered and checks the one live line, its status role, its dots and "Running a command".

## The same live line in each Bot Chat (2 Nov)

His own chats got the live line first (above). A Bot Chat had the same gap: the Bots' connector ran each turn with `hermes -p <bot> chat ... -Q` and read nothing but its exit code. Now:

- **The Bots' connector reads the turn the same way.** It adds `--format stream-json` and reads Hermes's events with One's own reader (`own_connector.follow_turn`, one copy for both), so a Bot and One say the same states for the same events: thinking, writing, or a tool by its plain name, never the words, a tool's input or its output. Whether the message was delivered is still read from the exit code, as before.
- **Said while the turns run.** A round's turns run side by side; while they do, the connector posts what each is doing to `POST /connector/doing` when it changes (checked every 2 seconds) and at least every 20 seconds (`Connector.say_doing`). It is reading only, so the core takes it while `mobile_read` is on, whatever `connector_effects` says, with the connector's own token.
- **The core** keeps the set in memory only (`chat.py` `TURNS`), only well-formed turns (a known state, a `msg_` id, a plain tool name, at most 10), puts each on the message it answers when that message is being answered (`doing` in the chat view), and drops it when the connector has not said it again in a minute, when the next set leaves it out, or when the result comes.
- **The app** draws the line in a Bot Chat as in his own chats (`workingItem`, followed by `watchTurn` on the Bot Chat's view), with the Bot's name and colour. Rooms have no turn of their own, so no line.

Tests: `test_decisions_botline.py` (one reader for both connectors, a Bot Chat turn from a stand-in `hermes` with the line showing while it runs and gone once sent, the core's checks, and the route). `decisions_phone_check.js` opens a Bot Chat whose message Wisp is answering and checks the line reads "Writing".

## The Board, live: what each Bot is doing now (2 Nov)

Patrick: the Board "Is supposed to show only what each is currently doing ... it's supposed to be live what they are doing". The GitHub Board (above, "The Board shows the Bots' work on GitHub") showed history: claimed issues, pull requests and what finished in the last day, read once in each 5 minutes. That is gone, with the Kanban table under it. Now:

- **Where it comes from.** A Hermes plugin in each Bot's profile, `thornswood_live` (installed and enabled by `scripts/apply.py`, like the guard), observes four of Hermes's own hooks and blocks nothing: a turn starts thinking (`pre_llm_call`), runs a tool by its name (`pre_tool_call`), thinks again after it (`post_tool_call`), and is gone when it ends (`on_session_end`, which Hermes calls at the end of every turn, finished, failed or interrupted). Tools at once are each held by their call id, and the turn says the newest. Each change rewrites one small file, `thornswood-live/<pid>.json` in that profile's home (0600, written whole), holding the process's id and start time and each running turn's session id, state, tool and start. Never a tool's arguments, its output or any words of a turn. A failed write is logged once and the turn goes on. So the Board covers every turn a Bot runs, from a Bot Chat, Discord, a wake or a schedule, which the connector's live line (below) could not.
- **The reader.** `live_reader.py`, as `hermes`, all the time (`tw-decide-live.service`, loopback only): every 2 seconds it reads those files, keeps only the ones whose process is still the one that wrote them (its pid and its start time from `/proc`, so an ended or reused process says nothing), reads each running session's title from that Bot's `state.db` read-only, and sends the core the work feed: `{source_time, doing: [{bot, title, state, tool, since, platform}]}`, one row per running turn. It sends when that changes and at least once in each 30 seconds.
- **The core** keeps only those fields (`feeds.py`): a Bot this workspace has, thinking or a tool, a tool name in Hermes's own shape (otherwise the row says thinking), at most 60 rows. `GET /work` says `live`, false once the reader has not sent for 90 seconds.
- **The app.** One row for each running turn, in the Bots list's order: the Bot, the session's title when it has one, what the turn is doing in the Bot Chat's own words ("Thinking", "Running a command", "Reading a file", "Working on GitHub"...), and for how long. A Bot running nothing has no row, and with none running the Board says "No Bot is working right now." It is read again every 3 seconds while it is open. When `live` is false it says when it last heard from the Bots instead of showing what may have ended.

The feed timer (`tw-decide-feed`) now sends only the rooms and the Bot Chats, and reads nothing from GitHub.

Tests: `test_decisions_live_board.py` (the hooks and their file, tools at once, a fresh turn, one file per profile, a hook that cannot write, the reader keeping only running processes and skipping odd files and rows, the real process check, never writing to Hermes, the plugin to the core end to end, and sending only on a change or each half minute), `test_decisions_phone.py` `FeedsIn` (the fields kept, the shapes, the Bots, `live`) and `test_apply.py` (the plugin installed and enabled in each Bot's profile). `decisions_phone_check.js` checks the Board in Chromium: only the running turns in the Bots' order, as text, no idle Bot, and a change shown with no tap.

## Voice: dictation, Read aloud, voice chat and "Hey Hermes" (2 Nov)

Patrick asked for voice in the app: dictation with the VM's Whisper, reading aloud, voice chat, and "Hey Hermes" only while the app is open, with a visible Listening bar. All of it costs nothing: no paid voice or model service.

- **Where the words come from.** The phone records (MediaRecorder, Opus in WebM on Android) and posts the recording as its own bytes to `POST /voice/transcribe` (Patrick only, with the CSRF token, while `mobile_read` is on, at most 4 MB). The core (`voice.py`) hands it over a Unix socket to the voice worker and answers the words. It keeps nothing and logs nothing of it. The worker (`voice_worker.py`, `tw-decide-voice.service`) runs as the Bots' `hermes` user inside Hermes's own runtime, started the way the connector starts its helper, and writes the recording down with Hermes's local Whisper only (`transcribe_audio_local_fallback`: faster-whisper or the local whisper command, with the model Hermes's `stt.local.model` names; it never installs anything and never falls back to a provider). The recording lives in a folder only the worker can read and is deleted as soon as its words are back; its log has the size and the time taken, never the words. Its socket is 0660 in the group `tw-decide-voice`, whose one member is `tw-decide`, and the worker also checks the caller's user from the socket.
- **The install** makes the group, starts the worker last, checks the socket's mode and group, and asks the worker which Whisper Hermes has. It stops, saying so, when Hermes on the VM has no local Whisper, since dictation would answer nothing.
- **Dictation.** The microphone beside a chat's box records until Done (or 2.5 seconds of quiet after speech), and the words go into the box, where he reads them before sending.
- **Read aloud.** In a message's actions. The phone's own speech reads the message as plain words.
- **Voice chat.** The waves button in a chat's bar. It listens, hears where he stops (the loudness against the room's own level, WebAudio's analyser: nothing loaded), sends his words as the send button does, reads the answer aloud, and listens again. Words that answer a card are never sent by voice alone: they wait in the box with the line over it, for his tap.
- **"Hey Hermes".** Off until he turns it on in Settings (this phone only). Then, while a chat is open and the app is in front, each thing he says is written down by the VM's Whisper, and words that start with "Hey Hermes" start voice chat in that chat, sending what follows. A minute with nothing said goes back to listening for it. It never listens in the background: it is not claimed to, and Android does not let a web app do it.
- **The bar.** Under the top, the whole time the microphone is open or the phone reads aloud: what it is doing ("Listening", "Hearing you", "Writing down what you said", "Waiting for the answer", "Reading the answer aloud", "Listening for Hey Hermes"), with a lit dot while it records, and Stop. Leaving the page stops it. Hiding the app or locking the phone stops listening and reading at once, so nothing is read out on a locked phone.
- **An answer shows at once.** Found on the way: a Bot's or Hermes's answer showed up to about 35 seconds late, because the core kept a chat's newest page for 20 seconds after the turn that changed it, and the app read a waiting message again only every 15 seconds. Now the core drops a chat's newest page (and any ask for it already handed out) when a turn's result comes in, and the app watches a waiting message every 2 seconds, as it watches a running turn.

Tests: `test_decisions_voice.py` (the worker's wire, the words back, the recording deleted, nothing but sizes and times in its log, only the core's user, every bad request, Hermes's own failure said as it is, only the local Whisper, the core's side and each failure's word, over HTTP with the CSRF token and `mobile_read`, the deploy, and the app's rules), and its `AnAnswerShowsAtOnce` (a turn's result, in a Bot Chat and with his own Hermes, drops only that chat's newest page and its ask). `decisions_phone_check.js` runs voice in Chromium with a fake microphone playing a voice-like hum and a stand-in Whisper: dictation into the box with the bar up, voice chat sending his words and reading Bramble's answer aloud when the turn ends, Read aloud, "Hey Hermes" off until turned on and silent outside a chat, sending what follows it, and hiding the app stopping it.

## A message into a running turn follows the chat's busy setting, like Desktop (2 Nov)

Patrick's answer A on q-one-busy-chat: a message from the phone into a chat whose turn is already running does what Desktop's box does with it. Then: "if a message on the app is in queue instead of waiting to send it should say in queue like it does on Desktop". The cause of the slow Reply: the connector ran every message as `hermes chat -Q ... --resume <id>`, whose one-shot always hands the message over with `prompt.submit`, so it always waited behind the running turn, however the chat was set, and the phone said "Waiting to send" or "Sending" the whole time.

What happens now (contracts.json `own_chat.busy`):

- **The same `hermes chat`, one step changed.** The connector runs the turn as `room_send.py chat <the same arguments>` in Hermes's own runtime (the readiness helper's Python and bootstrap). It runs Hermes's own `hermes chat` through the same local gateway connection as before, with one change: when the chat's snapshot shows a turn running, it asks the gateway for that chat's busy setting (`config.get busy`, the chat's own, else Hermes's `display.busy_input_mode`, else Hermes's default), as Desktop does. **Steer** goes to `session.steer`: the running turn takes the words at its next step. **Interrupt** goes to `session.redirect`: the running turn stops and goes on with these words. **Queue** goes to `prompt.submit`: it runs next. A photo or file, a slash command, or a turn stopped at an approval or a question always queues, and a steer the gateway answers "rejected" (the turn ended meanwhile) is queued, as Desktop does, so nothing is lost. A steered or redirected message has no turn of its own; the running turn answers it, and the connector follows that turn to its end. It is in `room_send.py`, not a new file, because One's connector updates itself with the files its running copy names.
- **It says how Hermes took it.** `room_send.py` adds one line to Hermes's stream: started, queued, steered or redirected, and started again when a queued one leaves the queue. The connector passes it on with the claim like the rest of the live line, and says once Hermes has the message (`admitted`).
- **The next message goes as soon as the one before is in Hermes.** The core used to hold a chat's second message until the first was answered. Now it hands it over once every earlier message in that chat is in Hermes, so it can be steered or queued at once, in the order he sent them. A new chat still takes one message at a time, since it has no id until its first turn makes one.
- **The phone says it like Desktop.** Under the message: "In queue" while it waits in Hermes's queue, "Steered into the running turn", "Interrupted the running turn", or "Sent". The live line with the dots follows only the turn being answered, never one in the queue, and the chat is drawn again the moment a queued one starts. A turn held by another Hermes process's lease on the chat (`turn_held`) is in a queue too and says so.

Tests: `test_decisions_busy.py` (the hand-over for each setting, the cases that always queue, a rejected steer, a steer following the running turn, the admission lines, the core's order, the new states, the app's words), and the existing live line and waiting tests with "queued" in place of "waiting". It has not yet run against a real gateway with a running Desktop turn: that is checked on One after it lands.
