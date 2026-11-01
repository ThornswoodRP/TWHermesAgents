"""A scratch test workspace served on localhost for the phone check under Chromium.

    python tests/decisions_phone_server.py <port>

It prints one JSON line (a phone pairing code and three card ids, the second
high risk, the third left for the alert checks) and serves until killed. Test data only: no real workspace, no Bot,
no deploy; nothing takes the chat messages it queues, and the push worker's
keys are throwaway ones said straight to the core, with no worker and no push
service. One's own-Hermes connector is stood in for by one call to the core,
and nothing takes the messages sent to it.
"""
import json
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
from decisions_helpers import World
from thornswood_decisions.api import Api, make_server
from thornswood_decisions.feeds import Feeds
port = int(sys.argv[1])
w = World()  # a test workspace with writes on, in a temp folder
w.store.set_flag(w.ws, "mobile_read", True, by="t")
w.store.set_flag(w.ws, "mobile_chat_write", True, by="t")
w.core.bind_room(w.ws, "54b3ad46-3419-4adb-b60d-c79cb347fcee", "Thornswood Workbench", by="t")
w.core.bind_room(w.ws, "00000000-0000-4000-8000-000000000002", "Second test room", by="t")
for bot in ("bramble", "rift", "sprocket", "echo", "magpie", "wisp"):
    w.core.bind_bot(w.ws, bot, bot, by="t")
card = w.card(title="Turn on the nightly backup check?")
high = w.card(title="Delete the old test branches?", risk="high", source_key="high-1")
third = w.card(title="Move the backup to Sunday?", source_key="third-1")
f = Feeds(w.store)
tw = f.new_token(w.ws, "work", by="t"); tr = f.new_token(w.ws, "room", by="t"); tb = f.new_token(w.ws, "bots", by="t")
f.put(w.ws, "bots", tb, {"source_time": "2026-11-01T18:00:00+00:00", "bots": [{"bot": "bramble", "session_id": "s1", "messages": [{"role": "user", "text": "How is the nightly?", "at": "2026-11-01T17:00:00+00:00"}, {"role": "assistant", "text": "All green <i>tonight</i>.", "at": "2026-11-01T17:01:00+00:00"}]}]})
f.put(w.ws, "work", tw, {"source_time": "2026-11-01T18:00:00+00:00", "tasks": [{"id": "t1", "title": "Fix the <b>launcher</b> log", "assignee": "sprocket", "status": "running"}]})
f.put(w.ws, "room", tr, {"source_time": "2026-11-01T18:00:00+00:00", "room_id": "54b3ad46-3419-4adb-b60d-c79cb347fcee", "events": [{"seq": 1, "kind": "message.member", "actor": "Bramble", "text": "<script>alert(1)</script> hello", "at": "2026-11-01T17:59:00+00:00"}]})
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from thornswood_decisions import push_worker, webpush
from thornswood_decisions.push import Push
tp = f.new_token(w.ws, "push", by="t")
Push(w.store).hello(w.ws, tp, webpush.vapid_public(webpush.new_vapid_key()), push_worker.seal_public(X25519PrivateKey.generate()))
# Patrick's own Hermes: One heard from with its gateway running and two chats; my-hermes never heard from.
from thornswood_decisions.own import OwnChat
w.store.set_flag(w.ws, "own_chat_write", True, by="t")
own = OwnChat(w.store)
to = f.new_token(w.ws, "own-one", by="t"); f.new_token(w.ws, "own-me", by="t")
own.claim(w.ws, "one", to, 0, True)
own.feed(w.ws, "one", to, {"source_time": "2026-11-01T18:00:00+00:00", "chats": [
    {"id": "s_plan", "title": "Plan the week", "last_at": "2026-11-01T17:30:00+00:00", "preview": "three things"},
    {"id": "s_old", "title": "", "last_at": "2026-10-30T17:30:00+00:00", "preview": "older"}],
    "open": [{"id": "s_plan", "messages": [{"role": "user", "text": "What is on this week?", "at": "2026-11-01T17:29:00+00:00"},
                                           {"role": "assistant", "text": "<b>three</b> things", "at": "2026-11-01T17:30:00+00:00"}]}]})
code = w.core.auth.operator_pairing_code(w.ws, "phone", "Pixel", by="t")["code"]
api = Api(w.path, "test", origin=f"http://localhost:{port}", clock=w.clock)
srv = make_server(api, "127.0.0.1", port)
print(json.dumps({"code": code, "card": card["id"], "high": high["id"], "third": third["id"]}), flush=True)
srv.serve_forever()
