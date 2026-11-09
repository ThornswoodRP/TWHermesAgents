"""Chat alerts: one line on his phone when a turn he started ends, or when his own Hermes asks him something
(TWHermesAgents #83 and #42, 9 Nov: the switch from Hermes Desktop, which notifies for both).

A card's alert stays as it was (push.py): "Hermes needs you", for a card. A chat alert is the same kind of
line, sent the same way by the same worker, and as bare: who it is ("Rift", "Hermes", "One") and the chat to
open, never a word of the message, the question or the answer. The rows live in `chat_alerts`, beside the
cards' `push_outbox`; Push.claim hands both to the worker, and the worker cannot tell them apart.

- "answered": a message he sent ended its turn: a Bot Chat or one session of a Bot (chat.py result, a turn
  that finished, never one steered into a Bot's running turn, whose own end is not his) or a chat of his own
  Hermes (own.py result).
- "asks": a turn of his own Hermes opened a question or an approval for him (own.py claim, from what the
  connector says the turn has open). Once for each, by its prompt id.

The phone shows none while that very chat is open in front of him (sw.js).
"""

from __future__ import annotations

import re

from .store import new_id

KINDS = ("answered", "asks")
WHO = re.compile(r"^[A-Za-z][A-Za-z ]{0,39}$")
PATH = re.compile(r"^/m/(bots/[a-z0-9][a-z0-9_-]{0,63}(/s/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})?"
                  r"|hermes/(me|one)(/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})?)$")
OWN_WHO = {"me": "Hermes", "one": "One"}  # app.js GATEWAY


def bot_path(bot: str, session: str | None = None) -> str:
    return f"/m/bots/{bot}" + (f"/s/{session}" if session else "")


def own_path(gw: str, session: str | None) -> str:
    return f"/m/hermes/{gw}" + (f"/{session}" if session else "")


def queue(store, db, workspace_id: str, kind: str, ref: str, who: str, path: str) -> bool:
    """One alert, in the caller's transaction; the same kind and ref again is the same alert. False when it
    was not one this file makes (it is never sent half right)."""
    if kind not in KINDS or not WHO.match(who) or not PATH.match(path) or not 1 <= len(ref) <= 200:
        return False
    now = store.now()
    db.execute("INSERT INTO chat_alerts(id, workspace_id, kind, ref, who, path, status, created_at, updated_at) "
               "VALUES(?,?,?,?,?,?,'queued',?,?) ON CONFLICT(workspace_id, kind, ref) DO NOTHING",
               (new_id("pa"), workspace_id, kind, ref, who, path, now, now))
    return True


def payload(row) -> dict:
    return {"v": 1, "event_id": row["id"], "kind": row["kind"], "who": row["who"], "path": row["path"]}
