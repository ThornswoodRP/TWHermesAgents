"""A scratch test workspace served on localhost for the phone check under Chromium.

    python tests/decisions_phone_server.py <port>

It prints one JSON line (a phone pairing code and a card id) and serves until
killed. Test data only: no real workspace, no Bot, no deploy.
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
w.core.bind_room(w.ws, "54b3ad46-3419-4adb-b60d-c79cb347fcee", "Thornswood Workbench", by="t")
card = w.card(title="Turn on the nightly backup check?")
f = Feeds(w.store)
tw = f.new_token(w.ws, "work", by="t"); tr = f.new_token(w.ws, "room", by="t")
f.put(w.ws, "work", tw, {"source_time": "2026-11-01T18:00:00+00:00", "tasks": [{"id": "t1", "title": "Fix the <b>launcher</b> log", "assignee": "sprocket", "status": "running"}]})
f.put(w.ws, "room", tr, {"source_time": "2026-11-01T18:00:00+00:00", "room_id": "54b3ad46-3419-4adb-b60d-c79cb347fcee", "events": [{"seq": 1, "kind": "message.member", "actor": "Bramble", "text": "<script>alert(1)</script> hello", "at": "2026-11-01T17:59:00+00:00"}]})
code = w.core.auth.operator_pairing_code(w.ws, "phone", "Pixel", by="t")["code"]
api = Api(w.path, "test", origin=f"http://localhost:{port}", clock=w.clock)
srv = make_server(api, "127.0.0.1", port)
print(json.dumps({"code": code, "card": card["id"]}), flush=True)
srv.serve_forever()
