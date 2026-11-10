"""A scratch test workspace served on localhost for the phone check under Chromium.

    python tests/decisions_phone_server.py <port>

It prints one JSON line (a phone pairing code and three card ids, the second
high risk, the third left for the alert checks, and the voice check's fake
microphone file and words) and serves until killed. Test data only: no real workspace, no Bot,
no deploy; nothing takes the chat messages it queues, and the push worker's
keys are throwaway ones said straight to the core, with no worker and no push
service. One's own-Hermes connector is stood in for by one call to the core
for its chat list and a loop that answers the pages the core asks for from a
scratch state.db (the real connector's own page reads), and nothing takes the
messages sent to it.
"""
import json
import sys
import tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from decisions_helpers import World
from thornswood_decisions.api import Api, make_server
from thornswood_decisions.feeds import Feeds
port = int(sys.argv[1])
# Chrome's virtual authenticator, which the browser check uses as the phone's own, says this AAGUID.
from thornswood_decisions import passkey as _passkey
_passkey.PHONE_AUTHENTICATORS["01020304-0506-0708-0102-030405060708"] = "Chrome's test authenticator"

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
# A card Rift asked, which waits in Rift's Bot Chat and is answered there by a message.
w.core.assign_task(w.bramble, "load-order", "rift")
w.card(by=w.rift, task="load-order", title="Rebuild the load order now?", source_key="rift-1")
f = Feeds(w.store)
tw = f.new_token(w.ws, "work", by="t"); tr = f.new_token(w.ws, "room", by="t"); tb = f.new_token(w.ws, "bots", by="t")
f.put(w.ws, "bots", tb, {"source_time": "2026-11-01T18:00:00+00:00", "bots": [{"bot": "bramble", "session_id": "s1", "messages": [{"role": "user", "text": "How is the nightly?", "at": "2026-11-01T17:00:00+00:00"}, {"role": "assistant", "text": "All green <i>tonight</i>.", "at": "2026-11-01T17:01:00+00:00"}]}]})
# The live Board: Rift running a command in a session with a title, Echo thinking with none, and Sprocket's
# two turns at once. When the test makes the file BOARD_NEXT, Rift's command has ended and it is thinking.
BOARD = [{"bot": "echo", "state": "thinking", "since": "2026-11-01T17:59:30+00:00"},
         {"bot": "rift", "title": "Rebuild the <b>load order</b>", "state": "tool", "tool": "terminal",
          "since": "2026-11-01T17:40:00+00:00", "session_id": "s_rift_lo"},
         {"bot": "sprocket", "title": "Installer log", "state": "tool", "tool": "read_file",
          "since": "2026-11-01T17:50:00+00:00"},
         {"bot": "sprocket", "title": "Launcher news", "state": "tool", "tool": "browser_navigate",
          "since": "2026-11-01T17:55:00+00:00"}]
f.put(w.ws, "work", tw, {"source_time": "2026-11-01T18:00:00+00:00", "doing": BOARD})
BOARD_NEXT = Path(tempfile.mkdtemp()) / "board-next"
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
PASTE_CMD = ("gh pr merge 55 -R ThornswoodRP/TWHermesAgents --squash --match-head-commit "
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
     "preview": PASTE_MSG[:200]},
    # A chat whose newest message is being answered right now (the live line under it).
    {"id": "s_work", "title": "Check the build", "last_at": "2026-10-28T12:00:00+00:00", "preview": "earlier"}]
HIDDEN = []
OPEN = [{"id": "s_plan", "messages": [{"role": "user", "text": "What is on this week?", "at": "2026-11-01T17:29:00+00:00"},
                                      {"role": "assistant", "text": "<b>three</b> things", "at": "2026-11-01T17:30:00+00:00"}]}]
def one_feed(o=own):
    # Pinned chats first, as the connector lists them (own_connector.snapshot), and how many are hidden.
    o.feed(w.ws, "one", to, {"source_time": "2026-11-01T18:00:00+00:00", "open": OPEN, "hidden": len(HIDDEN),
                             "chats": sorted(CHATS, key=lambda row: not row.get("pinned"))})
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
# The reply ran two tools before it wrote (TWHermesAgents #106): Hermes keeps the calls on an assistant row
# with no words and each result on a tool row; the second command failed.
con.execute("ALTER TABLE messages ADD COLUMN tool_calls TEXT")
con.execute("ALTER TABLE messages ADD COLUMN tool_call_id TEXT")
con.execute("INSERT INTO messages(session_id, role, content, timestamp, tool_calls) VALUES('s_md', 'assistant', '', ?, ?)",
            (epoch("2026-10-31T11:59:00+00:00"), json.dumps([
                {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"command": "git status"})}},
                {"id": "c2", "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"command": "false"})}}])))
con.execute("INSERT INTO messages(session_id, role, content, timestamp, tool_call_id) VALUES('s_md', 'tool', ?, ?, 'c1')",
            (json.dumps({"output": "nothing to commit", "exit_code": 0}), epoch("2026-10-31T11:59:10+00:00")))
con.execute("INSERT INTO messages(session_id, role, content, timestamp, tool_call_id) VALUES('s_md', 'tool', ?, ?, 'c2')",
            (json.dumps({"output": "", "exit_code": 1}), epoch("2026-10-31T11:59:20+00:00")))
con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_md', 'assistant', ?, ?)",
            (MD, epoch("2026-10-31T12:00:00+00:00")))
# Links of each kind, a mention and a phone number (Patrick, 2 Nov 3:18 PM and 3:21 PM); and a chat Hermes has
# not titled that began with a /skill, kept with the skill's whole text ahead of what he typed.
LINKS = ("Ask @Rift. The [plan thread](https://claude.ai/code/project/chan_1?thread=cmsg_9), "
         "https://claude.ai/artifact/AbC123, https://github.com/ThornswoodRP/TWHermesAgents/pull/96, "
         "www.example.com and call 720-555-0199.")
con.execute("INSERT INTO sessions VALUES('s_links', 'cli', 'Links check', ?, ?, NULL, NULL, NULL, 0, 0)",
            (epoch("2026-10-31T10:00:00+00:00"), epoch("2026-10-31T10:30:00+00:00")))
con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_links', 'assistant', ?, ?)",
            (LINKS, epoch("2026-10-31T10:30:00+00:00")))
SKILL = ('[IMPORTANT: The user has invoked the "thornswood-operator-cards" skill, indicating they want you to '
         'follow its instructions. The full skill content is loaded below.]\n\n# Operator cards\nLong rules.\n\n'
         'The user has provided the following instruction alongside the skill invocation: run card 2')
con.execute("INSERT INTO sessions VALUES('s_skill', 'cli', NULL, ?, ?, NULL, NULL, NULL, 0, 0)",
            (epoch("2026-10-31T09:00:00+00:00"), epoch("2026-10-31T09:05:00+00:00")))
con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_skill', 'user', ?, ?)",
            (SKILL, epoch("2026-10-31T09:00:00+00:00")))
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
con.execute("INSERT INTO sessions VALUES('s_work', 'cli', 'Check the build', ?, ?, NULL, NULL, NULL, 0, 0)",
            (epoch("2026-10-28T11:00:00+00:00"), epoch("2026-10-28T12:00:00+00:00")))
con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_work', 'assistant', 'earlier', ?)",
            (epoch("2026-10-28T12:00:00+00:00"),))
con.commit()
# The two new chats are listed as One's connector lists them, by its own row maker, so the /skill chat's
# name is what own_connector makes of the skill's text, not a name written here.
con.row_factory = sqlite3.Row
for sid in ("s_links", "s_skill"):
    CHATS.append(own_connector._row(con, con.execute(
        "SELECT id, title, coalesce(last_activity_at, started_at) AS last_at FROM sessions WHERE id = ?", (sid,)).fetchone()))
con.close()
one_feed()
# His message in s_work, taken by the stand-in connector, whose turn is running a command; each claim the loop
# below makes says so, as One's connector does while a turn runs (own_connector.OwnConnector.busy).
from test_decisions_own import passkey
WORK = own.send(passkey(w), "one", "s_work", None, "is the build green?", "own-key-work")["message_id"]
own.claim(w.ws, "one", to, 1, True)
# Hermes has it, and has a question open for Patrick (own_turns.py); his answer settles it, as Hermes does.
ASK = {"prompt_id": "p_colour", "kind": "clarify", "question": "Which **branch** first?", "choices": ["main", "dev"],
       "multi_select": False}
TURNS = {"turns": [{"id": WORK, "state": "tool", "tool": "terminal", "admitted": True, "asks": [ASK]}]}
# What that turn shows, as Desktop draws it (live_turns.py): its thinking, a command running, a subagent.
ONE_PARTS = [{"k": "think", "text": "Which branch builds?"},
             {"k": "step", "name": "terminal", "preview": "npm test", "done": False, "error": False},
             {"k": "sub", "goal": "Read the launcher log", "status": "running", "tool": "read_file",
              "preview": "launcher.log", "tools": 2, "summary": ""}]
ANSWERS = []
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
    """A rename, pin, hide, bring back or delete, done on the scratch state.db as Hermes's own calls do it (the
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
    elif op in ("pin", "unpin"):
        # The scratch state.db has no pinned column; the feed carries the pin, as the connector reads it.
        for row in CHATS:
            if row["id"] == sid:
                row["pinned"] = op == "pin"
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
def one_replies(o):
    """A reply Patrick sends is taken as One's connector takes a message, and lands in the scratch
    state.db as Hermes keeps a user message: its text exactly as sent. Only replies: every other
    message here stays waiting, as the checks before expect."""
    from datetime import timedelta
    rows = o.s.db.execute("SELECT id, session_id, text FROM own_messages WHERE gateway = 'one' "
                          "AND reply_to IS NOT NULL AND status = 'queued'").fetchall()
    for r in rows:
        with o.s.tx() as db:
            db.execute("UPDATE own_messages SET status = 'leased', lease_until = ? WHERE id = ?",
                       ((w.clock() + timedelta(minutes=2)).isoformat(), r["id"]))
        c = sqlite3.connect(sdb)
        c.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES(?, 'user', ?, ?)",
                  (r["session_id"], r["text"], epoch("2026-11-01T17:45:00+00:00")))
        c.commit()
        c.close()
        o.result(w.ws, "one", to, r["id"], "confirmed")
        # The core's clock here stands still; let the 20 seconds pass after which the core asks One for
        # the chat's newest page again, as they would while One answers.
        w.clock.advance(seconds=21)
def one_new_chats(o):
    """A new chat whose first message starts "Plan a trip" is taken as One's connector takes one: Hermes makes
    the chat (s_trip) and its long first turn runs on, and each claim says the chat's id with that turn, as
    One's connector does from the turn's admission line (room_send.py announce). No result comes: the app goes
    into the chat while the turn still runs (10 Nov)."""
    from datetime import timedelta
    rows = o.s.db.execute("SELECT id, text FROM own_messages WHERE gateway = 'one' AND session_id IS NULL "
                          "AND status = 'queued' AND text LIKE 'Plan a trip%'").fetchall()
    for r in rows:
        with o.s.tx() as db:
            db.execute("UPDATE own_messages SET status = 'leased', lease_until = ? WHERE id = ?",
                       ((w.clock() + timedelta(minutes=10)).isoformat(), r["id"]))
        c = sqlite3.connect(sdb)
        c.execute("INSERT INTO sessions VALUES('s_trip', 'cli', NULL, ?, ?, NULL, NULL, NULL, 0, 0)",
                  (epoch("2026-11-01T17:50:00+00:00"), epoch("2026-11-01T17:50:00+00:00")))
        c.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_trip', 'user', ?, ?)",
                  (r["text"], epoch("2026-11-01T17:50:00+00:00")))
        c.commit()
        c.close()
        TURNS["turns"].append({"id": r["id"], "state": "tool", "tool": "terminal", "admitted": True,
                               "session": "s_trip"})
# One's chat settings, as its connector reads and changes them (room_send.py op control): a ChatGPT
# sign-in model now, and a local model it can move to.
CTL = {"model": "gpt-5.5", "provider": "openai-codex", "reasoning": "medium", "levels": ["low", "medium", "high", "xhigh"],
       "yolo": False, "busy": "steer",
       "offered": [{"provider": "openai-codex", "model": "gpt-5.5", "name": "OpenAI Codex", "why": "subscription"},
                   {"provider": "openai-codex", "model": "gpt-5.5-mini", "name": "OpenAI Codex", "why": "subscription"},
                   {"provider": "llamacpp", "model": "qwen3-8b.gguf", "name": "Local", "why": "local"}],
       "context": {"context_used": 82000, "context_max": 200000, "context_percent": 41, "context_estimated": True},
       "usage": {"input": 12345, "output": 4100, "total": 16445, "calls": 3, "compressions": 1}}
def one_control(o, r):
    want = r.get("set") or {}
    if want.get("model"):
        CTL.update(model=want["model"]["model"], provider=want["model"]["provider"])
    if want.get("reasoning"):
        CTL["reasoning"] = want["reasoning"]
    o.control_post(w.ws, "one", to, {"id": r["id"], "state": dict(CTL), "applied": bool(want), "reason": ""})
def hermes_search(req):
    """A stand-in for Hermes's own search (room_send.py op `search`): chats whose messages or title hold the
    words, the matched message as the snippet. None for any page that is not a search."""
    q = str(req.get("q") or "").strip().lower()
    if req["kind"] != "chats" or not q:
        return None
    c = sqlite3.connect(sdb)
    try:
        hits = c.execute("SELECT session_id, content FROM messages WHERE lower(content) LIKE ? ORDER BY id DESC",
                         (f"%{q}%",)).fetchall()
        titles = c.execute("SELECT id FROM sessions WHERE lower(coalesce(title, '')) LIKE ?", (f"%{q}%",)).fetchall()
    finally:
        c.close()
    found = {}
    for sid, text in hits:
        found.setdefault(sid, text)
    for (sid,) in titles:
        found.setdefault(sid, "")
    return [{"id": k, "snippet": v} for k, v in found.items()]


def one_pages():
    o = OwnChat(Store(w.path, clock=w.clock))
    while True:
        try:
            got = o.claim(w.ws, "one", to, 0, True, "", None, None, TURNS)
            o.turns(w.ws, "one", to, {"turns": [{"id": WORK, "parts": ONE_PARTS}]})
            for r in got.get("sessions", []):
                one_session(o, r)
            for r in got.get("controls", []):
                one_control(o, r)
            for r in got.get("orders", []):
                if r["op"] == "answer":
                    ANSWERS.append(r["value"])
                    TURNS["turns"][0]["asks"] = []
                    TURNS["turns"][0]["order"] = {"op": "answer", "prompt_id": r["prompt_id"], "status": "sent"}
            one_replies(o)
            one_new_chats(o)
            for r in got.get("reactions", []):
                o.reacted(w.ws, "one", to, {"session_id": r["session_id"], "message_id": r["message_id"],
                                            "version": r["version"], "outcome": set_reaction(r)})
            for req in got["pages"]:
                o.page_post(w.ws, "one", to, json.loads(own_connector.fitted_page(sdb, req, hermes_search(req))))
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
# Rift's session the Board shows it working in, which a Board row opens.
(bhome / "profiles" / "rift").mkdir(parents=True)
con = sqlite3.connect(bhome / "profiles" / "rift" / "state.db")
con.executescript(STATE_SQL)
con.execute("INSERT INTO sessions VALUES('s_rift_lo', 'webhook', 'Rebuild the load order', 1, 900, NULL, NULL, NULL, 0, 0)")
con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_rift_lo', 'user', "
            "'Rebuild the load order for #5367', ?)", (epoch("2026-11-01T17:40:00+00:00"),))
con.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_rift_lo', 'assistant', "
            "'Running the sort now.', ?)", (epoch("2026-11-01T17:41:00+00:00"),))
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
# The Bots' connector's report of each Bot's gateway (room_send.py op states): Bramble running, Rift not,
# Sprocket starting; the other three left out, so they are not known.
Chat(w.store).live(w.ws, tc, {"states": {"bramble": "ready", "rift": "absent", "sprocket": "starting"}})
# Tasks, as the connectors report them (room_send.py op tasks): One has a subagent and a background command in
# its chat s_md, and a job; Bramble two jobs, one paused and one whose last run failed, and a finished command;
# Rift's gateway is absent, so it has no live part; my-hermes is never heard from.
TASK_T0 = time.time()
def tasks_feed():
    store = Store(w.path, clock=w.clock)
    chat, o = Chat(store), OwnChat(store)
    while True:
        o.tasks(w.ws, "one", to, {"tasks": {"state": "ready", "subagents": [
            {"id": "sa_1", "chat": "s_md", "chat_title": "Markdown check", "goal": "Read the launcher log and find the crash",
             "model": "claude", "status": "running", "depth": 1, "background": False, "tools": 4, "last_tool": "terminal",
             "started": TASK_T0 - 300}], "processes": [
            {"id": "proc_1", "chat": "s_md", "chat_title": "Markdown check", "command": "npm run build -- --watch",
             "status": "running", "exit_code": None, "started": TASK_T0 - 1500, "ended": None, "why": None}],
            "jobs": [{"id": "j1", "name": "Morning summary", "schedule": "every day at 8:00", "state": "scheduled",
                      "next": "2026-11-04T15:00:00+00:00", "last": None, "last_status": None, "last_error": None,
                      "running": False}]}})
        chat.tasks(w.ws, tc, {"tasks": {
            "bramble": {"state": "ready", "subagents": [], "processes": [
                {"id": "proc_2", "chat": "s_bc", "chat_title": "Bot Chat", "command": "pytest -q", "status": "exited",
                 "exit_code": 0, "started": TASK_T0 - 900, "ended": TASK_T0 - 600, "why": "exited"}],
                "jobs": [{"id": "j2", "name": "Nightly merge sweep", "schedule": "0 3 * * *", "state": "scheduled",
                          "next": "2026-11-04T10:00:00+00:00", "last": "2026-11-03T10:00:00+00:00",
                          "last_status": "error", "last_error": "the fork was not reachable", "running": False},
                         {"id": "j3", "name": "Weekly board review", "schedule": "every Monday", "state": "paused",
                          "next": None, "last": None, "last_status": None, "last_error": None, "running": False}]},
            "rift": {"state": "absent", "subagents": None, "processes": None, "jobs": [],
                     "live_why": "its gateway is absent"}}})
        time.sleep(1)
threading.Thread(target=tasks_feed, daemon=True).start()
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
# His message to Wisp, taken by the Bots' connector, whose turn is writing its reply: leased as the claim
# leases it (connector_effects stays off here), and each round below says what the turn is doing, as the
# connector does while a Bot Chat turn runs (connector.Connector.say_doing).
from datetime import timedelta
WISP = Chat(w.store).send(w.patrick(), "wisp", "how is the gamemode build?", "key-wisp-0001")["message_id"]
w.store.db.execute("UPDATE chat_messages SET status = 'leased', lease_until = ? WHERE id = ?",
                   ((w.clock() + timedelta(days=1)).isoformat(), WISP))
def bots_pages():
    from thornswood_decisions.reactions import Reactions
    conn = Connector(Direct(), None, say=lambda _: None, hermes_home=bhome)
    store = Store(w.path, clock=w.clock)
    chat = Chat(store)
    words = 0
    while True:
        # connector_effects stays off here, so the Bots' reactions are handed as its claim would hand them.
        with store.tx() as db:
            wanted = Reactions(store).hand(db, w.ws, "bots")
        for r in wanted:
            out = set_reaction(r, bhome / "profiles" / r["chat"] / "state.db", "s_bc")
            chat.reacted(w.ws, tc, {"bot": r["chat"], "message_id": r["message_id"], "version": r["version"],
                                    "outcome": out})
        conn.pages()
        chat.doing(w.ws, tc, {"turns": [{"id": WISP, "state": "writing"}]})
        # Wisp's reply as it is written, a word more each round, as the Bots' connector posts it.
        words += 1  # it keeps writing for the whole check (a part keeps its newest 12000 characters)
        chat.turns(w.ws, tc, {"turns": [{"id": WISP, "parts": [
            {"k": "text", "text": "The gamemode build is" + " still going" * words}]}]})
        time.sleep(0.3)
threading.Thread(target=bots_pages, daemon=True).start()
def board_feed():
    # What live_reader.py does on the VM: the Board sent again when it changes.
    while not BOARD_NEXT.exists():
        time.sleep(0.2)
    nxt = [dict(d, state="thinking", tool="") if d["bot"] == "rift" else d for d in BOARD]
    Feeds(Store(w.path, clock=w.clock)).put(w.ws, "work", tw, {"source_time": "2026-11-01T18:00:05+00:00",
                                                              "doing": nxt})
threading.Thread(target=board_feed, daemon=True).start()
# Voice: the browser's fake microphone plays VOICE_WAV (a second of silence, a second of a voice-like hum,
# three of silence, over and over), and the voice worker's Whisper is a stand-in that writes every recording
# down as VOICE_WORDS. When Bramble's Bot Chat gets those words, or the words after "Hey Hermes", Bramble
# answers in its state.db, for voice chat to read aloud.
import math, struct, wave
from thornswood_decisions.voice_worker import Worker
VOICE_WORDS = "Hey Hermes, what is on the board?"
VOICE_REPLY = "Two Bots are working on the board."
vdir = Path(tempfile.mkdtemp())
VOICE_WAV = vdir / "speech.wav"
with wave.open(str(VOICE_WAV), "wb") as wav:
    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
    frames = []
    for i in range(16000 * 5):
        t = i / 16000
        if 1 <= t < 2:
            env = 0.5 * (1 - math.cos(2 * math.pi * 4 * (t - 1)))
            v = env * 0.3 * sum(math.sin(2 * math.pi * 150 * k * t) / k for k in range(1, 9))
        else:
            v = 0.0
        frames.append(struct.pack("<h", int(max(-1, min(1, v)) * 32767)))
    wav.writeframes(b"".join(frames))
voice_worker = Worker(lambda path: {"success": True, "transcript": VOICE_WORDS}, scratch=vdir / "scratch",
                      whisper=lambda: "faster-whisper")
threading.Thread(target=voice_worker.serve, args=(str(vdir / "voice.sock"),), daemon=True).start()
def bramble_answers():
    # As the Bots' connector does it: the message taken, its turn run (Bramble's answer in its state.db),
    # and the result told to the core, which reads the Bot Chat's newest page again for it.
    store = Store(w.path, clock=w.clock)
    while True:
        heard = store.db.execute("SELECT id FROM chat_messages WHERE target = 'bramble' AND text IN (?, ?) "
                                 "AND status = 'queued'", (VOICE_WORDS, "what is on the board?")).fetchone()
        if heard:
            with store.tx() as db:
                db.execute("UPDATE chat_messages SET status = 'leased', lease_until = ? WHERE id = ?",
                           ((w.clock() + timedelta(minutes=5)).isoformat(), heard["id"]))
            time.sleep(1)
            c = sqlite3.connect(bhome / "profiles" / "bramble" / "state.db")
            c.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_bc', 'assistant', ?, ?)",
                      (VOICE_REPLY, time.time()))
            c.commit()
            c.close()
            Chat(store).result(w.ws, tc, heard["id"], "confirmed")
            return
        time.sleep(0.2)
threading.Thread(target=bramble_answers, daemon=True).start()
# Read replies aloud: what he types to Bramble with it on is answered as voice chat's words are, for the
# phone to read out by itself.
ALOUD_ASK, ALOUD_REPLY = "Read this one to me.", "Read aloud is on for this chat."
def bramble_reads():
    store = Store(w.path, clock=w.clock)
    while True:
        heard = store.db.execute("SELECT id FROM chat_messages WHERE target = 'bramble' AND text = ? "
                                 "AND status = 'queued'", (ALOUD_ASK,)).fetchone()
        if heard:
            with store.tx() as db:
                db.execute("UPDATE chat_messages SET status = 'leased', lease_until = ? WHERE id = ?",
                           ((w.clock() + timedelta(minutes=5)).isoformat(), heard["id"]))
            time.sleep(1)
            c = sqlite3.connect(bhome / "profiles" / "bramble" / "state.db")
            c.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES('s_bc', 'assistant', ?, ?)",
                      (ALOUD_REPLY, time.time()))
            c.commit()
            c.close()
            Chat(store).result(w.ws, tc, heard["id"], "confirmed")
            return
        time.sleep(0.2)
threading.Thread(target=bramble_reads, daemon=True).start()
code = w.core.auth.operator_pairing_code(w.ws, "phone", "Pixel", by="t")["code"]
api = Api(w.path, "test", origin=f"http://localhost:{port}", clock=w.clock)
api.voice_socket = str(vdir / "voice.sock")
srv = make_server(api, "127.0.0.1", port)
print(json.dumps({"code": code, "card": card["id"], "high": high["id"], "third": third["id"],
                  "board_next": str(BOARD_NEXT), "voice_wav": str(VOICE_WAV), "voice_words": VOICE_WORDS,
                  "voice_reply": VOICE_REPLY}), flush=True)
srv.serve_forever()
