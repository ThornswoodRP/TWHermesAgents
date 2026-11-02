"""A scratch test workspace served on localhost for the phone check under Chromium.

    python tests/decisions_phone_server.py <port>

It prints one JSON line (a phone pairing code and three card ids, the second
high risk, the third left for the alert checks) and serves until killed. Test data only: no real workspace, no Bot,
no deploy; nothing takes the chat messages it queues, and the push worker's
keys are throwaway ones said straight to the core, with no worker and no push
service. One's own-Hermes connector is stood in for by one call to the core
for its chat list and a loop that answers the pages the core asks for from a
scratch state.db (the real connector's own page reads), and nothing takes the
messages sent to it.
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
f.put(w.ws, "work", tw, {"source_time": "2026-11-01T18:00:00+00:00", "tasks": [{"id": "t1", "title": "Fix the <b>launcher</b> log", "assignee": "sprocket", "status": "running"}],
                         "items": [{"repo": "ThornswoodRP/Thornswood", "number": 5033, "title": "The site hub says <i>which</i> page is open", "bot": "echo", "stage": "pr_open", "pr": 5367, "pr_repo": "ThornswoodRP/Thornswood", "pr_title": "The hub marks its open tab", "checks": "failing", "checks_line": "18 of 20 passed, 2 failed", "auto_merge": True, "updated_at": "2026-11-01T17:30:00+00:00"},
                                   {"repo": "ThornswoodRP/Thornswood", "number": 5191, "title": "Generated content tables are built from their sources", "bot": "magpie", "stage": "claimed", "updated_at": "2026-11-01T17:00:00+00:00"}]})
# A Bot's message in Markdown, longer than a list line: the Rooms list shows its plain words, cut at a word.
ROOM_MD = ("@bramble it'll show up on #5367, and it'll run, not skip.\n- **Why it isn't there yet:** "
           "`Launcher builds` has `needs: pick` and [the guide](https://example.invalid/g) says so.\n\n## Next\n1. merge it")
f.put(w.ws, "room", tr, {"source_time": "2026-11-01T18:00:00+00:00", "rooms": [
    {"room_id": "54b3ad46-3419-4adb-b60d-c79cb347fcee", "events": [{"seq": 1, "kind": "message.member", "actor": "Bramble", "text": "<script>alert(1)</script> hello", "at": "2026-11-01T17:59:00+00:00"}]},
    {"room_id": "00000000-0000-4000-8000-000000000002", "events": [{"seq": 1, "kind": "message.member", "actor": "magpie", "text": ROOM_MD, "at": "2026-11-01T17:58:00+00:00"}]}]})
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from thornswood_decisions import push_worker, webpush
from thornswood_decisions.push import Push
tp = f.new_token(w.ws, "push", by="t")
Push(w.store).hello(w.ws, tp, webpush.vapid_public(webpush.new_vapid_key()), push_worker.seal_public(X25519PrivateKey.generate()))
# Patrick's own Hermes: One heard from with its gateway running and two chats; my-hermes never heard from.
from thornswood_decisions.own import OwnChat
w.store.set_flag(w.ws, "own_chat_write", True, by="t")
w.store.set_flag(w.ws, "session_manage_write", True, by="t")
own = OwnChat(w.store)
to = f.new_token(w.ws, "own-one", by="t"); f.new_token(w.ws, "own-me", by="t")
own.claim(w.ws, "one", to, 0, True)
# A long paste as Hermes keeps it: the @file: reference where it was typed, the file's text below
# "--- Attached Context ---" (agent/context_references.py), here holding a command too long for a phone.
PASTE_REF = "@file:AppData/Local/hermes/composer-pastes/pasted_content_2026-11-02_03-17-07-762_54a0c5.txt"
PASTE_CMD = ("gh pr merge 55 -R ThornswoodRP/Thornswood-Hermes --squash --match-head-commit "
             "750aa15ef81f69c5b0be509a6ea44a9d50b0d494")
PASTE_MSG = (f"Here is the plan {PASTE_REF}\n\n--- Attached Context ---\n\n\U0001F4C4 {PASTE_REF} (40 tokens)\n"
             f"```\nStep 1. {PASTE_CMD}\n```")
# His Bot Chat comes first, as One's connector lists it (own_connector.bot_chat).
CHATS = [
    {"id": "s_obc", "title": "Bot Chat", "last_at": "2026-11-01T09:45:00+00:00", "preview": "Rift: load order is clean",
     "bot_chat": True},
    {"id": "s_plan", "title": "Plan the week", "last_at": "2026-11-01T17:30:00+00:00", "preview": "three things"},
    {"id": "s_long", "title": "A long chat", "last_at": "2026-11-01T16:00:00+00:00", "preview": "message 119"},
    {"id": "s_old", "title": "", "last_at": "2026-10-30T17:30:00+00:00", "preview": "older"},
    {"id": "s_md", "title": "Markdown check", "last_at": "2026-10-31T12:00:00+00:00", "preview": "Markdown"},
    # A chat Hermes has not titled, named by its first message; its newest row a long paste from Hermes Desktop.
    {"id": "s_paste", "title": "", "first": "Here is the plan", "last_at": "2026-10-29T12:00:00+00:00",
     "preview": PASTE_MSG[:200]}]
HIDDEN = []
OPEN = [{"id": "s_plan", "messages": [{"role": "user", "text": "What is on this week?", "at": "2026-11-01T17:29:00+00:00"},
                                      {"role": "assistant", "text": "<b>three</b> things", "at": "2026-11-01T17:30:00+00:00"}]}]
def one_feed(o=own):
    o.feed(w.ws, "one", to, {"source_time": "2026-11-01T18:00:00+00:00", "chats": CHATS, "open": OPEN})
one_feed()
# The pages: s_plan and s_long are in the scratch state.db, s_old is not (gone from Hermes).
import sqlite3, tempfile, threading, time
from datetime import datetime
from test_decisions_own import STATE_SQL
from thornswood_decisions import own_connector
from thornswood_decisions.store import CoreError, Store
sdb = Path(tempfile.mkdtemp()) / "state.db"
con = sqlite3.connect(sdb)
con.executescript(STATE_SQL)
def epoch(iso): return datetime.fromisoformat(iso).timestamp()
con.execute("INSERT INTO sessions VALUES('s_plan', 'cli', 'Plan the week', ?, ?, NULL, NULL, NULL, 0, 0)",
            (epoch("2026-11-01T17:00:00+00:00"), epoch("2026-11-01T17:30:00+00:00")))
con.execute("INSERT INTO sessions VALUES('s_long', 'cli', 'A long chat', ?, ?, NULL, NULL, NULL, 0, 0)",
            (epoch("2026-11-01T10:00:00+00:00"), epoch("2026-11-01T16:00:00+00:00")))
# Hermes keeps reactions in display_metadata; Hermes itself put eyes on Patrick's question.
con.execute("ALTER TABLE messages ADD COLUMN display_metadata TEXT")
con.execute("INSERT INTO messages(session_id, role, content, timestamp, display_metadata) VALUES('s_plan', 'user', "
            "'What is on this week?', ?, ?)", (epoch("2026-11-01T17:29:00+00:00"), json.dumps(
                {"reactions": [{"emoji": "\U0001F440", "author": "agent", "at": 1, "seen": True}]})))
con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_plan', 'assistant', '<b>three</b> things', ?)",
            (epoch("2026-11-01T17:30:00+00:00"),))
# Markdown as Hermes writes it, with things that must stay text: a script link and raw HTML.
MD = ("**Bold** and *italic* and `code`, with a_snake_name kept.\n"
      "- one\n- two\n  1. nested\n\n"
      "[safe link](https://example.com/x) and [bad](javascript:alert(1)) and <img src=x onerror=alert(1)>\n\n"
      "```python\nprint(\"hi\")\n```\n\n"
      "| a | b |\n|---|---|\n| 1 | 2 |\n\n> quoted")
con.execute("INSERT INTO sessions VALUES('s_md', 'cli', 'Markdown check', ?, ?, NULL, NULL, NULL, 0, 0)",
            (epoch("2026-10-31T11:00:00+00:00"), epoch("2026-10-31T12:00:00+00:00")))
con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_md', 'assistant', ?, ?)",
            (MD, epoch("2026-10-31T12:00:00+00:00")))
for i in range(120):
    con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_long', ?, ?, ?)",
                ("user" if i % 2 == 0 else "assistant", f"message {i}", epoch("2026-11-01T10:00:00+00:00") + i))
# His Bot Chat: hidden, as Hermes keeps it, and written in before the others, though it is listed first.
con.execute("INSERT INTO sessions VALUES('s_obc', 'cli', 'Bot Chat', ?, ?, NULL, NULL, NULL, 0, 1)",
            (epoch("2026-11-01T08:00:00+00:00"), epoch("2026-11-01T09:45:00+00:00")))
con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_obc', 'assistant', "
            "'Rift: load order is clean', ?)", (epoch("2026-11-01T09:45:00+00:00"),))
# The untitled chat with the paste, and a reply holding the long command in a code block.
con.execute("INSERT INTO sessions VALUES('s_paste', 'cli', NULL, ?, ?, NULL, NULL, NULL, 0, 0)",
            (epoch("2026-10-29T11:00:00+00:00"), epoch("2026-10-29T12:00:00+00:00")))
con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_paste', 'user', ?, ?)",
            (PASTE_MSG, epoch("2026-10-29T11:59:00+00:00")))
con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_paste', 'assistant', ?, ?)",
            (f"Run this:\n\n```\n{PASTE_CMD}\n```", epoch("2026-10-29T12:00:00+00:00")))
con.commit()
con.close()
def set_reaction(r, db=None, sid=None):
    """Patrick's reaction, as Hermes sets it: one per person on the row (the helper's own path is in
    test_decisions_reactions.py)."""
    c = sqlite3.connect(db or sdb)
    row = c.execute("SELECT display_metadata FROM messages WHERE id = ? AND session_id = ?",
                    (r["message_id"], sid or r["session_id"])).fetchone()
    if row is None:
        c.close()
        return "failed"
    meta = json.loads(row[0] or "{}")
    keep = [x for x in meta.get("reactions", []) if x["author"] != "user"]
    if r["emoji"]:
        keep.append({"emoji": r["emoji"], "author": "user", "at": time.time(), "seen": False})
    meta["reactions"] = keep
    c.execute("UPDATE messages SET display_metadata = ? WHERE id = ?", (json.dumps(meta), r["message_id"]))
    c.commit()
    c.close()
    return "set"
def one_session(o, r):
    """A rename, hide, bring back or delete, done on the scratch state.db as Hermes's own calls do it (the
    helper's own path is in test_decisions_sessions.py), then the chat list fed again, as the connector does."""
    c = sqlite3.connect(sdb)
    sid, op = r["session_id"], r["op"]
    if op == "rename":
        c.execute("UPDATE sessions SET title = ? WHERE id = ?", (r["title"], sid))
        for row in CHATS:
            if row["id"] == sid:
                row["title"] = r["title"]
    elif op == "archive":
        c.execute("UPDATE sessions SET archived = 1 WHERE id = ?", (sid,))
        HIDDEN.extend(row for row in CHATS if row["id"] == sid)
        CHATS[:] = [row for row in CHATS if row["id"] != sid]
    elif op == "unarchive":
        c.execute("UPDATE sessions SET archived = 0 WHERE id = ?", (sid,))
        CHATS.extend(row for row in HIDDEN if row["id"] == sid)
        HIDDEN[:] = [row for row in HIDDEN if row["id"] != sid]
    elif op == "delete":
        c.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        c.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        CHATS[:] = [row for row in CHATS if row["id"] != sid]
    c.commit()
    c.close()
    one_feed(o)
    o.session_done(w.ws, "one", to, {"id": r["id"], "outcome": "done"})
def one_pages():
    o = OwnChat(Store(w.path, clock=w.clock))
    while True:
        try:
            got = o.claim(w.ws, "one", to, 0, True)
            for r in got.get("sessions", []):
                one_session(o, r)
            for r in got.get("reactions", []):
                o.reacted(w.ws, "one", to, {"session_id": r["session_id"], "message_id": r["message_id"],
                                            "version": r["version"], "outcome": set_reaction(r)})
            for req in got["pages"]:
                o.page_post(w.ws, "one", to, json.loads(own_connector.fitted_page(sdb, req)))
        except CoreError:
            pass
        time.sleep(0.3)
threading.Thread(target=one_pages, daemon=True).start()
# The Bots' connector's page reads, against a stand-in ~/.hermes: Bramble's Bot Chat of 70 messages and the
# Workbench room's messages.
from thornswood_decisions.chat import Chat
from thornswood_decisions.connector import Connector
bhome = Path(tempfile.mkdtemp()) / ".hermes"
(bhome / "profiles" / "bramble").mkdir(parents=True)
con = sqlite3.connect(bhome / "profiles" / "bramble" / "state.db")
con.executescript(STATE_SQL)
con.execute("INSERT INTO sessions VALUES('s_bc', 'cli', 'Bot Chat', 1, 900, NULL, NULL, NULL, 0, 0)")
for i in range(68):
    con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_bc', ?, ?, ?)",
                ("user" if i % 2 == 0 else "assistant", f"bramble {i}", epoch("2026-11-01T09:00:00+00:00") + i))
# Bramble put eyes on Patrick's question, in Hermes's own display_metadata.
con.execute("ALTER TABLE messages ADD COLUMN display_metadata TEXT")
con.execute("INSERT INTO messages(session_id, role, content, timestamp, display_metadata) VALUES('s_bc', 'user', "
            "'How is the nightly?', ?, ?)", (epoch("2026-11-01T17:00:00+00:00"), json.dumps(
                {"reactions": [{"emoji": "\U0001F440", "author": "agent", "at": 1, "seen": True}]})))
con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_bc', 'assistant', 'All green <i>tonight</i>.', ?)",
            (epoch("2026-11-01T17:01:00+00:00"),))
con.commit()
con.close()
con = sqlite3.connect(bhome / "shared-state.db")
con.execute("CREATE TABLE hosted_room_events (room_id TEXT, seq INTEGER, event_id TEXT, kind TEXT, "
            "actor_json TEXT, authority_epoch INTEGER, payload_json TEXT, created_at REAL)")
con.execute("INSERT INTO hosted_room_events VALUES('54b3ad46-3419-4adb-b60d-c79cb347fcee', 1, 'e1', 'message.member', "
            "?, 1, ?, ?)", (json.dumps({"display_name": "Bramble"}), json.dumps({"text": "<script>alert(1)</script> hello"}),
                            epoch("2026-11-01T17:59:00+00:00")))
con.commit()
con.close()
tc = f.new_token(w.ws, "connector", by="t")
class Direct:
    def __init__(self):
        self.chat = Chat(Store(w.path, clock=w.clock))
    def post(self, path, body):
        try:
            if path == "/connector/pages":
                return 200, self.chat.page_requests(w.ws, tc)
            return 200, self.chat.page_post(w.ws, tc, json.loads(json.dumps(body)))
        except CoreError as exc:
            return exc.status, {"error": exc.reason}
def bots_pages():
    from thornswood_decisions.reactions import Reactions
    conn = Connector(Direct(), None, say=lambda _: None, hermes_home=bhome)
    store = Store(w.path, clock=w.clock)
    chat = Chat(store)
    while True:
        # connector_effects stays off here, so the Bots' reactions are handed as its claim would hand them.
        with store.tx() as db:
            wanted = Reactions(store).hand(db, w.ws, "bots")
        for r in wanted:
            out = set_reaction(r, bhome / "profiles" / r["chat"] / "state.db", "s_bc")
            chat.reacted(w.ws, tc, {"bot": r["chat"], "message_id": r["message_id"], "version": r["version"],
                                    "outcome": out})
        conn.pages()
        time.sleep(0.3)
threading.Thread(target=bots_pages, daemon=True).start()
code = w.core.auth.operator_pairing_code(w.ws, "phone", "Pixel", by="t")["code"]
api = Api(w.path, "test", origin=f"http://localhost:{port}", clock=w.clock)
srv = make_server(api, "127.0.0.1", port)
print(json.dumps({"code": code, "card": card["id"], "high": high["id"], "third": third["id"]}), flush=True)
srv.serve_forever()
