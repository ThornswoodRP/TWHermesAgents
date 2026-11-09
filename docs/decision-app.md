# The decision app

When a Bot needs Patrick, it opens a card. He answers from an installable phone app or a pane in Hermes Desktop, signed in with a passkey, and the Bot is told the answer. The same app carries his chats with the Bots, the Workbench rooms and his own Hermes, a live Board of what each Bot is doing, push alerts and voice.

All of it is the decision core, `src/thornswood_decisions/`, one service on the Bots' VM.

## The parts

| Part | Where | What it does |
|---|---|---|
| **The core** | `tw-decide.service`, as `tw-decide`, on `127.0.0.1:9135` | Holds every card, answer, chat message and receipt in one SQLite store under `/srv/tw/decide`. Listens on loopback only; systemd lets it reach loopback and nothing else |
| **The Bots' route** | `tw-decide-bots.service` | A Unix socket only the Bots' user can open, for the `tw_decide` tool: open, revise and close a card, assign a task, read a card, list the Bot's own cards, and report work done. No network |
| **The phone app** | `src/thornswood_decisions/app/`, served at `/m/` | An installable web app with no framework and no inline code. It reaches the core through Tailscale Serve on the tailnet |
| **The Desktop pane** | `hermes/plugins/thornswood-decisions/` | A Hermes plugin in Patrick's own Hermes: a backend (`dashboard/plugin_api.py`) that forwards to the core, and the pane itself (`desktop/plugin.js`) |
| **The Bots' connector** | `tw-decide-connector.service`, as `hermes` | Delivers Patrick's chat messages and "Bot told" messages into the Bots' Bot Chats and rooms, reads their history, and reports each Bot's state |
| **His own Hermes's connectors** | `tw-decide-own.service` on the VM; a sign-in task on his Windows machine | Carry his chats with his own two Hermes gateways. Nothing listens on his machine: its connector reaches out to the core |
| **The feeds** | `tw-decide-feed.timer`, once a minute | Send the rooms and the Bot Chats to the core, read only |
| **The live Board** | `tw-decide-live.service` | Reads what the `thornswood_live` plugin writes and sends the core each running turn |
| **The tailnet check** | `tw-decide-peers.timer`, once a minute | Reads `tailscale status` so the app can tell "machine off" from "connector stopped" |
| **The push worker** | `tw-decide-push.service`, as `tw-push` | Sends alerts to the phone. The only part that reaches the internet, and only the browsers' own push services |
| **The voice worker** | `tw-decide-voice.service`, as `hermes` | Writes down dictation with Hermes's own local Whisper. Nothing paid |

`deploy/decisions/` holds every unit, wrapper and install script.

## Cards and answers

- **Who asks.** Bramble opens any card. A worker opens a question only for a task Bramble assigned it. Bramble merges repeated requests by their source key.
- **Main cards.** Patrick's tap for what the brokers need his yes for: a collection publish, the claim label cutover, and a main run while `runs.main_without_patrick` is false. Only Bramble opens one. It names the exact repository and commit in one `release` link that never changes, its scope of a yes names that commit, it offers `authorize`, and it is high risk. When Bramble passes the card's id to `tw_git`, tw-git reads his answer on its own socket and tw-policy records an `authorize` as his decision record, bound to that commit and the card's revision, used once. An answer given before the core was restored from a backup authorizes nothing.
- **Who answers.** Only Patrick. A Bot is a service principal and never holds a session.
- **Revisions.** A changed card is a new revision and voids answers to the old one. An answer is checked against the revision and hash Patrick saw, and the answer, the event, the cancelled alerts, the message to the Bot and the command's result commit together.
- **Exactly once.** Each answer carries an idempotency key. The same key and body give the same receipt; the same key with another body is refused. After an unclear result, the app reads the card's receipt before it offers to send again, so a retry is never a second answer.
- **High-risk cards** need a passkey sign-in from the last five minutes. The passkey prompt opens by itself over the card's check screen.
- **Four states**, each read from its own record: decision accepted, Bot told, work done, release authorized.
- **A chat can answer.** A Bot's Bot Chat ends with the cards that Bot asked and that wait on Patrick. When his message starts with an option's label as a whole word, the app shows which card and option it answers, and sending goes through the same answer path as a tap, with every check.

## Signing in

- **Pairing.** The first Desktop types a code made on the VM with `tw-decide pair`; later devices get theirs from a signed-in one. A code works once, for 10 minutes.
- **Passkeys.** Checked by py_webauthn, added only from a fresh session on a paired phone. A phone can also hold its own platform key, so its fingerprint sheet signs in without leaving the app. A sign-in asks only for the keys the core holds, so a passkey app cannot offer one the core would refuse (9 Nov: a key a refused registration had left in a passkey app was offered and refused); each refused sign-in records the start of the offered key's credential id. A sign-in that fails before any key is checked is recorded too, without counting toward the lock: a request already used or expired, or one the phone ended (the browser or passkey app said no, with its error name, which the app also shows). `tw-decide passkeys` lists the keys held, the last refusals and the last sign-in requests with whether an answer came, read only. A fingerprint key that a passkey app saved, instead of the phone itself, is kept as a plain passkey and never as the phone's fingerprint key: the app has stored it already, perhaps in place of the key it held, and refusing it left the phone with no key the core took (9 Nov). Such a try ends the Decisions list's offer of the fingerprint on that phone, since that phone's passkey app takes its keys; Settings still has it. A key the phone made and the core then refused is named for the passkey app to drop. A passkey app also keeps a key the core removed (a replaced fingerprint key, a passkey or phone removed in Settings) and offers it with the live ones, so the app tells it through WebAuthn's Signal API: after each sign-in which keys the core takes, and after a refused one that the key it offered was removed here.
- **Sessions** keep only hashes: 12 hours idle, 7 days at most, "fresh" for 5 minutes. Ten recovery codes, scrypt-hashed, each works once.
- **Lockouts.** Five wrong codes or passkey failures in 15 minutes stop that kind of sign-in for 15 minutes.
- **Requests.** The phone's session is an HttpOnly, Secure cookie, and every write needs the app's exact `Origin` and a CSRF token. Desktop's comes as a bearer token from the plugin's backend. `GET` never changes anything, and the API logs nothing.

## Chat

| Chat with | How it goes |
|---|---|
| **A Bot** | Each Bot's one Bot Chat, through `hermes -p <bot> chat`, as Hermes's own message tool sends one on its machine. Needs `mobile_chat_write` and `connector_effects` |
| **A room** | Through `room_send.py`, which finds the running gateway without starting one and calls `groups.send` with the core's key as the event id |
| **His own Hermes** | Two gateways: Hermes on the VM (`me`) and Hermes on his Windows machine (`one`). Needs `own_chat_write` and a passkey sign-in from the last 30 minutes. New chats run with full power; an older chat keeps its own approval settings |

For every chat:

- **Delivery** is honest: only Hermes saying so is "sent", only nothing sent is "not sent", and everything else is "uncertain", never sent again by itself. A gateway that is not ready, or a Bot whose turn the fleet queue would not admit, leaves the message queued, and it goes the moment the gateway answers ready and the queue has room. A turn the queue turns away after all counts the same way, never as sent.
- **A running turn** follows the chat's busy setting, as Desktop does: the message steers the turn, interrupts it or waits in Hermes's queue, and the app says which.
- **A live line** under the message being answered says what the turn is doing ("Thinking", "Writing", "Running a command"), read from Hermes's own event stream. Never the words, a tool's input or its output.
- **History** pages back to a chat's first message, across Hermes's compression, held in the core's memory only, never in its store or a log.
- **Markdown** is drawn node by node as text: nothing in a message is read as HTML, and only `http` and `https` links are links.
- **Reactions** are Hermes's own, one per person on a message, set through Hermes's own session store.
- **Questions, approvals and Stop**, in his own Hermes, as Desktop has them. Each message runs as a turn he is there to answer (`room_send.py attended-chat`), not a one-shot, so when Hermes asks a question (its clarify tool) or asks to run a flagged command, the question or the command shows under the live line with Hermes's own choices. A tap, or his own words for a question, answers it; Stop ends the turn. The turn makes Hermes's own `clarify.respond`, `approval.respond` or `session.interrupt` call, the ones `hermes chat`'s /answer, /approve and /stop make. Each needs the passkey sign-in a message needs, and the core keeps the order in memory only, with none of its words in the event chain (`own_turns.py`).
- **Files and photos** go to his own Hermes only: up to 10 a message and 20 MB each, handed only to that gateway's connector, checked by size and SHA-256, and served back only as downloads.
- **Rename, hide and delete** of his own chats use Hermes's own calls, behind `session_manage_write`. A delete needs a fresh passkey. His own Bot Chat is never renamed, hidden or deleted.

## The Board

One row for each Bot turn running right now: the Bot, the session's title, what the turn is doing, and for how long. A row opens that very session as a chat like any other: its messages from the Bot's own store, what the turn is doing under them while it runs, and the box with dictation and voice chat. A message sent there goes into that session through the Bot's own gateway, the way Desktop sends into an open chat: while the Bot's turn runs, the session's busy setting steers it into the turn, interrupts it, or queues it behind, and it never runs beside it. It is the Bot's own unattended turn under its guard rails, as a Bot Chat message is. Reactions stay in the Bot Chat. With nothing running it says "No Bot is working right now", and when the reader goes quiet for 90 seconds it says when it last heard from the Bots, instead of showing what may have ended.

## Alerts

A card that needs Patrick puts one line on his phone, "Hermes needs you", with no body and no buttons. A tap opens that exact card. Nothing an alert does answers a card.

- Standard Web Push with Thornswood's own VAPID key, through the phone browser's own push service: no paid push and no account. The message is encrypted as RFC 8291 says, and the payload holds only ids.
- A phone subscribes only after a deliberate "Turn on alerts" tap, and a "no" is kept.
- The worker checks again, right before sending, that the card still waits at that revision. A push service's answer is "accepted", never "shown". An expired subscription is retired, and the app renews its own when it next opens.
- Answered cards' alerts are taken off the phone.

## Voice

All of it runs on the VM's own Whisper, with no paid voice service.

- **Dictation**: the microphone beside a chat's box writes words into the box, for Patrick to read before sending.
- **Read aloud**: the phone's own speech reads a message.
- **Voice chat**: listens, sends his words, reads the answer aloud, and listens again. Words that would answer a card wait in the box for a tap.
- **"Hey Hermes"**: off until turned on in Settings, and only while a chat is open and the app is in front. It never listens in the background.
- A bar shows whenever the microphone is open or the phone is reading aloud, with Stop. Locking the phone stops both.

## Feature switches

Every switch starts off in a new workspace, and in a real workspace only the operator on the VM turns one on, with `tw-decide flag ... --patrick-switched`.

| Switch | Allows |
|---|---|
| `mobile_read` | The phone reads cards, chats, the Board and rooms |
| `decision_write` | Answering cards |
| `mobile_chat_write` | Patrick's messages to the Bots and rooms |
| `connector_effects` | The Bots' connector delivering anything |
| `push_send` | Alerts to the phone |
| `own_chat_write` | Chat with his own Hermes |
| `session_manage_write` | Renaming, hiding and deleting his own chats |

## The store

- One connection, WAL, foreign keys on, every write in `BEGIN IMMEDIATE`. Card revisions, options, responses, events and receipts are history: a trigger aborts any change or delete.
- Every event is hashed over the one before it, and `tw-decide verify` names the first broken link.
- The schema moves by ordered migrations (`schema.py`). A store newer than the code refuses to open, so each migration from 6 on has its way back: `tw-decide downgrade --to N`, run with every `tw-decide` unit stopped, undoes newer migrations only when nothing would be lost, and otherwise refuses and changes nothing. `tw-decide schema` says the store's schema number and the newest the code knows.
- The install stops every worker before the store migrates, and starts them again only on the new schema.
- `tw-decide backup` makes a consistent copy, kept only if its event chain holds. A restore quarantines every undelivered alert and connector message and revokes every session and pairing code.

## The pinned contract

`contracts.json` records every fact the core relies on in Hermes, each with the file and line it was read from at the pinned Hermes commit: the six profile ids, how the Workbench room and the Bot Chats are reached, the Desktop plugin's routes, and how chats, reactions and sessions are read and changed. `scripts/check-hermes-contract.py` checks the VM against it ([setup.md](setup.md)).

## Testing

- The unit tests, `tests/test_decisions_*.py`, run with the rest of the suite.
- `tests/decisions_phone_check.js` drives the app in headless Chromium with Chrome's virtual authenticator: pairing, passkeys, every tab, answers, alerts delivered through DevTools, voice with a fake microphone, and the desktop layout at several widths.
- `deploy/decisions/phone-test.sh` runs a test bench beside the real core on the VM, for checks on a real phone: its own core on port 9136, its own push worker and store, and Tailscale Serve on 8443. It serves only the `phone-test` workspace and never turns on chat or the connector, so nothing reaches a Bot or a room.

```bash
sudo /opt/thornswood-hermes/releases/<commit>/deploy/decisions/phone-test.sh <commit> start
```

Its other commands are `pair`, `card`, `card high` and `stop`.
