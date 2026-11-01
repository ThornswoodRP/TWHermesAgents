"""Patrick's chat with his own Hermes, from the phone (Patrick, 1 Nov).

Beside the Bots' gateway (thornswood-bots, the six Bots and the rooms) the
phone reaches two more, the same three Desktop's gateway picker shows:

- "me": my-hermes, Patrick's own Hermes on the VM (the patrick-hermes user);
- "one": his Hermes on One ("This device" in One's Desktop).

Each has a small connector (`own_connector.py`) that runs as that Hermes's own
user and reaches out to the core with a token of its own (`own-me`,
`own-one`). It sends the core that Hermes's chat list, leases Patrick's
messages one chat at a time, runs each in that Hermes and says what came of
it. The core never reaches into either machine, and the Bots' connector can
never lease one of these messages: they are in a table of their own.

Patrick's answers on the report page (1 Nov):
- full power behind the passkey (C): a message here runs with Hermes's
  approval prompts bypassed, the way he would type it at his own keyboard;
- so sending needs a session signed in with a passkey in the last 30 minutes
  (A, 30 minutes). A pairing or recovery-code session never sends here;
- a connector on One that reaches out to the core (A);
- One off: refused at once, "One is off" (A). A gateway counts as on while
  its connector was heard from in the last 2 minutes.

A message here is never an answer, an approval or authority of any kind (07
section 5): it goes to Patrick's own Hermes and nowhere else. Its text goes in
`own_messages` and to the connector, never into the event chain or a log.
"""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime, timedelta, timezone

from .auth import Principal
from .feeds import Feeds
from .schema import OWN_GATEWAYS
from .store import CoreError, Store, canonical, new_id, parse, sha256_hex

NAMES = {"me": "my-hermes", "one": "One"}
TOKENS = {"me": "own-me", "one": "own-one"}
ONLINE = timedelta(minutes=2)
SEEN_EVERY = timedelta(seconds=20)   # how often a connector's poll is written down
SIGN_IN = timedelta(minutes=30)
LEASE = timedelta(minutes=20)        # the connector gives a turn 15 minutes
MAX_TEXT = 8000
MAX_CLAIM = 3
MAX_CHATS = 50
MAX_OPEN = 8
MAX_MESSAGES = 30
MAX_WANTED = 3
MINE = 50
# A chat's id goes on Hermes's command line after --resume, so it never starts with "-".
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
OUTCOMES = {"confirmed": "sent", "failed": "dead", "uncertain": "uncertain"}
CHAT_FIELDS = {"id": 128, "title": 120, "last_at": 40, "preview": 200}
MESSAGE_FIELDS = {"role": 16, "text": 2000, "at": 40}
ROLES = ("user", "assistant")
# What a connector says of its Hermes (migration 8): running, its gateway not running, or the
# readiness check itself failing. Anything else it sends is read as no word at all.
STATES = ("ready", "gateway_down", "check_failed")
PEER_FRESH = timedelta(minutes=3)    # `tw-decide peers` runs once a minute
# The tailnet's own addresses: an X-Forwarded-For from Tailscale Serve is one of these.
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))


def tailnet_ip(value) -> str | None:
    """The tailnet address a connector posted from, as Tailscale Serve sets it, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ip = ipaddress.ip_address(value.split(",")[0].strip())
    except ValueError:
        return None
    return str(ip) if any(ip in net for net in TAILNET) else None


def _tailscale_time(value) -> str | None:
    if not isinstance(value, str) or value.startswith("0001-"):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).isoformat()


def _text(value, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise CoreError(400, "bad_feed", "a feed field is text")
    return str(value)[:limit]


def _sid(value) -> str:
    if not isinstance(value, str) or not SESSION_ID.match(value):
        raise CoreError(400, "bad_chat", "a chat's id is 1 to 128 letters, digits and _ . : -, starting with a "
                        "letter or digit")
    return value


def clean(body) -> dict:
    """Only what the phone shows of one Hermes's chats, each field cut to size."""
    if not isinstance(body, dict):
        raise CoreError(400, "bad_feed", "a feed is a JSON object")
    try:
        source_time = parse(str(body.get("source_time"))).isoformat()
    except (ValueError, TypeError):
        raise CoreError(400, "bad_feed", "source_time is an ISO time with a zone") from None
    chats, opened = body.get("chats"), body.get("open", [])
    if not isinstance(chats, list) or len(chats) > MAX_CHATS:
        raise CoreError(400, "bad_feed", f"chats is a list of at most {MAX_CHATS}")
    if not isinstance(opened, list) or len(opened) > MAX_OPEN:
        raise CoreError(400, "bad_feed", f"open is a list of at most {MAX_OPEN} chats")
    out_chats = []
    for c in chats:
        if not isinstance(c, dict):
            raise CoreError(400, "bad_feed", "each chat is an object")
        row = {k: _text(c.get(k), n) for k, n in CHAT_FIELDS.items()}
        row["id"] = _sid(row["id"])
        out_chats.append(row)
    ids = [c["id"] for c in out_chats]
    if len(set(ids)) != len(ids):
        raise CoreError(400, "bad_feed", "the feed names a chat twice")
    out_open = []
    for o in opened:
        msgs = o.get("messages") if isinstance(o, dict) else None
        if not isinstance(msgs, list) or len(msgs) > MAX_MESSAGES:
            raise CoreError(400, "bad_feed", f"an open chat sends at most its last {MAX_MESSAGES} messages")
        kept = []
        for m in msgs:
            row = {k: _text(m.get(k) if isinstance(m, dict) else None, n) for k, n in MESSAGE_FIELDS.items()}
            if row["role"] not in ROLES:
                raise CoreError(400, "bad_feed", "a message is from the user or the assistant")
            kept.append(row)
        out_open.append({"id": _sid(o.get("id")), "messages": kept})
    if len({o["id"] for o in out_open}) != len(out_open):
        raise CoreError(400, "bad_feed", "the feed opens a chat twice")
    return {"source_time": source_time, "chats": out_chats, "open": out_open}


class OwnChat:
    def __init__(self, store: Store):
        self.s = store

    # ── checks ────────────────────────────────────────────────────────────

    @staticmethod
    def _gateway(gw: str) -> str:
        if gw not in OWN_GATEWAYS:
            raise CoreError(404, "no_gateway", "there is no such gateway")
        return gw

    @staticmethod
    def _patrick(p: Principal) -> None:
        if p.kind != "patrick":
            raise CoreError(403, "not_patrick", f"only Patrick chats with his own Hermes; {p.label} cannot")

    def sign_in_until(self, p: Principal) -> str | None:
        """When this session stops being able to send here: 30 minutes after a passkey sign-in."""
        if p.method != "passkey" or not p.signed_in_at:
            return None
        until = parse(p.signed_in_at) + SIGN_IN
        return until.isoformat() if self.s.clock() < until else None

    def _gw_row(self, workspace_id: str, gw: str):
        return self.s.db.execute("SELECT last_seen, ready, why, wanted_json, state, peer_ip, peer_online, peer_seen, "
                                 "peer_checked FROM own_gateways WHERE workspace_id = ? AND gateway = ?",
                                 (workspace_id, gw)).fetchone()

    def _last_seen(self, workspace_id: str, gw: str) -> str | None:
        row = self._gw_row(workspace_id, gw)
        return row["last_seen"] if row else None

    def online(self, workspace_id: str, gw: str) -> bool:
        """On: its connector was heard from in the last 2 minutes, and said that Hermes's gateway was running."""
        row = self._gw_row(workspace_id, gw)
        return bool(row and row["ready"]) and self.s.clock() - parse(row["last_seen"]) < ONLINE

    def _off_why(self, workspace_id: str, gw: str) -> str | None:
        row = self._gw_row(workspace_id, gw)
        if self.online(workspace_id, gw):
            return None
        if not row:
            return "its connector has never been heard from"
        if self.s.clock() - parse(row["last_seen"]) >= ONLINE:
            return "its connector has not been heard from in 2 minutes"
        return row["why"] or "its gateway is not running"

    def _seen(self, workspace_id: str, gw: str, ready: bool, why: str, state: str | None = None,
              peer_ip: str | None = None) -> None:
        if state not in STATES:
            state = "ready" if ready else None
        row = self._gw_row(workspace_id, gw)
        if row and bool(row["ready"]) == ready and (row["why"] or "") == why and row["state"] == state \
                and (peer_ip is None or row["peer_ip"] == peer_ip) \
                and self.s.clock() - parse(row["last_seen"]) < SEEN_EVERY:
            return
        with self.s.tx() as db:
            db.execute("INSERT INTO own_gateways(workspace_id, gateway, last_seen, ready, why, state, peer_ip) "
                       "VALUES(?,?,?,?,?,?,?) ON CONFLICT(workspace_id, gateway) DO UPDATE SET "
                       "last_seen = excluded.last_seen, ready = excluded.ready, why = excluded.why, "
                       "state = excluded.state, peer_ip = coalesce(excluded.peer_ip, own_gateways.peer_ip)",
                       (workspace_id, gw, self.s.now(), int(ready), why or None, state, peer_ip))

    def gateway_state(self, workspace_id: str, gw: str) -> dict:
        """Which state the gateway is in, for the phone's words (plan Part 1), and since when.

        turned_off      own_chat_write is off
        missing         its connector was never heard from
        ready           heard from in the last 2 minutes, and Hermes's gateway runs
        gateway_down    heard from, and Hermes's gateway does not run
        check_failed    heard from, and its check of Hermes could not run
        machine_off     silent, and the tailnet says the machine it posts from is offline
        connector_stopped  silent, and the machine is on (the VM always is: the core runs there)
        not_heard       silent, and the tailnet's view of that machine is not in
        """
        row = self._gw_row(workspace_id, gw)
        why = (row["why"] if row else None) or ""
        if not self.s.flag(workspace_id, "own_chat_write"):
            return {"state": "turned_off", "since": None, "why": ""}
        if not row:
            return {"state": "missing", "since": None, "why": ""}
        now = self.s.clock()
        if now - parse(row["last_seen"]) >= ONLINE:
            if gw == "me":
                return {"state": "connector_stopped", "since": row["last_seen"], "why": ""}
            fresh = row["peer_checked"] and now - parse(row["peer_checked"]) < PEER_FRESH
            if fresh and row["peer_online"] == 0:
                return {"state": "machine_off", "since": row["peer_seen"] or row["last_seen"], "why": ""}
            if fresh and row["peer_online"] == 1:
                return {"state": "connector_stopped", "since": row["last_seen"], "why": ""}
            return {"state": "not_heard", "since": row["last_seen"], "why": ""}
        if row["ready"]:
            return {"state": "ready", "since": row["last_seen"], "why": ""}
        return {"state": row["state"] if row["state"] in ("gateway_down", "check_failed") else "gateway_down",
                "since": row["last_seen"], "why": why}

    def peers(self, workspace_id: str, status) -> int:
        """The tailnet's view (`tailscale status --json`) of each machine a connector posts from. Returns
        how many rows it wrote. Only Online and LastSeen are kept, for the address the connector used."""
        if not isinstance(status, dict):
            raise CoreError(400, "bad_status", "tailscale status --json is one JSON object")
        nodes = [n for n in [status.get("Self"), *((status.get("Peer") or {}).values())] if isinstance(n, dict)]
        now = self.s.now()
        written = 0
        with self.s.tx() as db:
            for r in db.execute("SELECT gateway, peer_ip FROM own_gateways WHERE workspace_id = ? "
                                "AND peer_ip IS NOT NULL", (workspace_id,)).fetchall():
                node = next((n for n in nodes if r["peer_ip"] in (n.get("TailscaleIPs") or [])), None)
                if node is None:
                    online, seen = None, None
                else:
                    online = 1 if node.get("Online") is True else 0
                    seen = now if online else _tailscale_time(node.get("LastSeen"))
                db.execute("UPDATE own_gateways SET peer_online = ?, peer_seen = ?, peer_checked = ? "
                           "WHERE workspace_id = ? AND gateway = ?", (online, seen, now, workspace_id, r["gateway"]))
                written += 1
        return written

    def _check_token(self, workspace_id: str, gw: str, token: str | None) -> None:
        Feeds(self.s).check_token(workspace_id, TOKENS[self._gateway(gw)], token)

    def _wanted(self, workspace_id: str, gw: str) -> list[str]:
        row = self._gw_row(workspace_id, gw)
        return [w["id"] for w in json.loads(row["wanted_json"])] if row else []

    def _want(self, workspace_id: str, gw: str, sid: str) -> None:
        """The phone opened a chat the feed does not carry: ask the connector for it, newest first, at most 3."""
        row = self._gw_row(workspace_id, gw)
        if not row:
            return  # no connector yet; nothing to ask
        wanted = [w for w in json.loads(row["wanted_json"]) if w["id"] != sid]
        wanted = [{"id": sid, "at": self.s.now()}, *wanted][:MAX_WANTED]
        with self.s.tx() as db:
            db.execute("UPDATE own_gateways SET wanted_json = ? WHERE workspace_id = ? AND gateway = ?",
                       (canonical(wanted), workspace_id, gw))

    # ── Patrick's side ────────────────────────────────────────────────────

    def _state(self, p: Principal, gw: str) -> dict:
        snap = Feeds(self.s).get(p.workspace_id, gw)
        return {"gateway": gw, "name": NAMES[gw], "online": self.online(p.workspace_id, gw),
                **self.gateway_state(p.workspace_id, gw),
                "off_why": self._off_why(p.workspace_id, gw), "last_seen": self._last_seen(p.workspace_id, gw),
                "as_of": snap["as_of"], "snap": snap[gw]}

    def _can(self, p: Principal) -> dict:
        return {"can_send": self.s.flag(p.workspace_id, "own_chat_write"), "sign_in_until": self.sign_in_until(p)}

    def summary(self, p: Principal) -> dict:
        self._patrick(p)
        out = []
        for gw in OWN_GATEWAYS:
            st = self._state(p, gw)
            chats = (st.pop("snap") or {}).get("chats", [])
            out.append(st | {"chats": len(chats), "last": chats[0] if chats else None,
                             "unsettled": self._unsettled(p.workspace_id, gw)})
        return {"gateways": out, **self._can(p)}

    def chats(self, p: Principal, gw: str) -> dict:
        self._patrick(p)
        st = self._state(p, self._gateway(gw))
        snap = st.pop("snap") or {}
        # New chats Patrick started here that the chat list does not show yet, with how each went.
        rows = self.s.db.execute(
            "SELECT id, substr(text, 1, 80) AS text, made_session_id, status, reason, created_at FROM own_messages "
            "WHERE workspace_id = ? AND gateway = ? AND session_id IS NULL "
            "ORDER BY created_at DESC, rowid DESC LIMIT 10", (p.workspace_id, gw)).fetchall()
        listed = {c["id"] for c in snap.get("chats", [])}
        starting = [dict(r) for r in rows if r["made_session_id"] not in listed]
        return st | {"chats": snap.get("chats", []), "starting": starting, **self._can(p)}

    def view(self, p: Principal, gw: str, sid: str) -> dict:
        self._patrick(p)
        st = self._state(p, self._gateway(gw))
        sid = _sid(sid)
        snap = st.pop("snap") or {}
        chat = next((c for c in snap.get("chats", []) if c["id"] == sid), None)
        opened = next((o for o in snap.get("open", []) if o["id"] == sid), None)
        if opened is None:
            self._want(p.workspace_id, gw, sid)
        history = [{"who": "Patrick" if m["role"] == "user" else "Hermes", "role": m["role"], "text": m["text"],
                    "at": m["at"]} for m in (opened or {}).get("messages", [])]
        rows = self.s.db.execute(
            "SELECT id, text, status, reason, created_at, updated_at FROM own_messages WHERE workspace_id = ? "
            "AND gateway = ? AND (session_id = ? OR made_session_id = ?) ORDER BY created_at DESC, rowid DESC "
            "LIMIT ?", (p.workspace_id, gw, sid, sid, MINE)).fetchall()
        return st | {"chat": chat or {"id": sid, "title": ""}, "loaded": opened is not None, "history": history,
                     "mine": [dict(r) for r in reversed(rows)], **self._can(p)}

    def _unsettled(self, workspace_id: str, gw: str) -> int:
        return self.s.db.execute("SELECT count(*) FROM own_messages WHERE workspace_id = ? AND gateway = ? "
                                 "AND status IN ('queued', 'leased', 'uncertain')", (workspace_id, gw)).fetchone()[0]

    def send(self, p: Principal, gw: str, session_id, new, text, idempotency_key) -> dict:
        """One message to one chat (session_id), or the first message of a new chat (new: true), which
        Hermes names itself, as it does any chat."""
        self._patrick(p)
        gw = self._gateway(gw)
        if not self.s.flag(p.workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "chat with your own Hermes from the phone is off for now")
        if (session_id is None) != (new is True):
            raise CoreError(400, "bad_chat", "a message goes to one chat: its session_id, or new: true for a new one")
        if session_id is not None:
            session_id = _sid(session_id)
        if not isinstance(text, str) or not text.strip():
            raise CoreError(400, "bad_text", "a message has some text")
        text = text.strip()
        if len(text) > MAX_TEXT:
            raise CoreError(400, "bad_text", f"a message is at most {MAX_TEXT} characters")
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 200:
            raise CoreError(400, "bad_key", "a message carries an idempotency key of 8 to 200 characters")
        body_sha = sha256_hex(canonical({"gateway": gw, "session_id": session_id, "text": text}))
        with self.s.tx() as db:
            cmd = db.execute("SELECT op, body_sha256, result_json FROM commands WHERE workspace_id = ? "
                             "AND principal_id = ? AND idempotency_key = ?",
                             (p.workspace_id, p.id, idempotency_key)).fetchone()
            if cmd:
                if cmd["op"] != "own.send" or cmd["body_sha256"] != body_sha:
                    raise CoreError(409, "key_reused", "that idempotency key was used for a different request")
                return json.loads(cmd["result_json"]) | {"replayed": True}
            if self.sign_in_until(p) is None:
                raise CoreError(403, "passkey_needed", "a message to your own Hermes runs with full power, so it "
                                "needs a passkey sign-in from the last 30 minutes; sign in with your passkey again")
            if not self.online(p.workspace_id, gw):
                why = self._off_why(p.workspace_id, gw)
                st = self.gateway_state(p.workspace_id, gw)
                if st["state"] == "machine_off":
                    raise CoreError(409, "one_off", "One is off", why=why, state=st["state"], since=st["since"])
                raise CoreError(409, "gateway_off", f"{NAMES[gw]} is not answering: {why}", why=why,
                                state=st["state"], since=st["since"])
            now = self.s.now()
            mid = new_id("own")
            db.execute("INSERT INTO own_messages(id, workspace_id, gateway, session_id, text, send_key, "
                       "principal_id, device_id, status, created_at, updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,'queued',?,?)",
                       (mid, p.workspace_id, gw, session_id, text, f"tw-own.{mid}", p.id, p.device_id, now, now))
            self.s.event(db, p.workspace_id, "own.queued", mid, "patrick",
                         {"gateway": gw, "new_chat": session_id is None, "device": p.device_id})
            receipt = {"message_id": mid, "gateway": gw, "session_id": session_id, "status": "queued",
                       "created_at": now}
            db.execute("INSERT INTO commands(workspace_id, principal_id, idempotency_key, op, body_sha256, "
                       "result_json, created_at) VALUES(?,?,?,?,?,?,?)",
                       (p.workspace_id, p.id, idempotency_key, "own.send", body_sha, canonical(receipt), now))
        return receipt

    # ── the connector's side ──────────────────────────────────────────────

    def feed(self, workspace_id: str, gw: str, token: str | None, body) -> dict:
        """The chat list. Refused while own_chat_write is off, so nothing of Patrick's own chats is copied
        into the core before he turns this on."""
        self._check_token(workspace_id, gw, token)
        if not self.s.flag(workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "the core takes no chat list while own_chat_write is off")
        snap = clean(body)
        text = canonical(snap)
        digest = sha256_hex(text)
        with self.s.tx() as db:
            db.execute("INSERT INTO feed_snapshots(workspace_id, name, body, sha256, source_time, received_at) "
                       "VALUES(?,?,?,?,?,?) ON CONFLICT(workspace_id, name) DO UPDATE SET body = excluded.body, "
                       "sha256 = excluded.sha256, source_time = excluded.source_time, "
                       "received_at = excluded.received_at",
                       (workspace_id, gw, text, digest, snap["source_time"], self.s.now()))
        return {"sha256": digest, "wanted": self._wanted(workspace_id, gw)}

    def claim(self, workspace_id: str, gw: str, token: str | None, limit: int = MAX_CLAIM, ready=False,
              why: str = "", state: str | None = None, forwarded_for: str | None = None) -> dict:
        """Lease the oldest waiting message of each chat that has nothing in flight, at most `limit`.

        Every claim is also the connector saying it is there and whether that Hermes's gateway is running
        (`ready`, with `why` when it is not). A claim with limit 0 says only that, while every turn it may
        run at once is running."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= MAX_CLAIM:
            raise CoreError(400, "bad_limit", f"the limit is 0 to {MAX_CLAIM}")
        if not isinstance(ready, bool):
            raise CoreError(400, "bad_ready", "ready is true or false")
        self._check_token(workspace_id, gw, token)
        self._seen(workspace_id, gw, ready, "" if ready else str(why)[:200], state if isinstance(state, str) else None,
                   tailnet_ip(forwarded_for))
        if not self.s.flag(workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "nothing goes to your own Hermes while own_chat_write is off")
        items = []
        with self.s.tx() as db:
            self._lapse(db, workspace_id, gw)
            rows = db.execute("SELECT id, session_id, text, send_key, status FROM own_messages "
                              "WHERE workspace_id = ? AND gateway = ? AND status IN ('queued', 'leased') "
                              "ORDER BY created_at, rowid", (workspace_id, gw)).fetchall()
            # One turn at a time in each chat; each new chat is a chat of its own.
            busy = {r["session_id"] or r["id"] for r in rows if r["status"] == "leased"}
            until = (self.s.clock() + LEASE).isoformat()
            now = self.s.now()
            for r in rows:
                key = r["session_id"] or r["id"]
                if r["status"] != "queued" or key in busy or len(items) >= limit or not ready:
                    continue
                busy.add(key)
                db.execute("UPDATE own_messages SET status = 'leased', lease_until = ?, attempts = attempts + 1, "
                           "updated_at = ? WHERE id = ?", (until, now, r["id"]))
                self.s.event(db, workspace_id, "own.leased", r["id"], f"own connector ({gw})", {})
                items.append({"id": r["id"], "session_id": r["session_id"], "text": r["text"],
                              "send_key": r["send_key"], "lease_until": until})
        return {"items": items, "wanted": self._wanted(workspace_id, gw)}

    def _lapse(self, db, workspace_id: str, gw: str) -> None:
        """A lease that ran out means the connector stopped mid-turn: unclear, so never sent again by itself."""
        now = self.s.clock()
        for r in db.execute("SELECT id, lease_until FROM own_messages WHERE workspace_id = ? AND gateway = ? "
                            "AND status = 'leased'", (workspace_id, gw)).fetchall():
            if r["lease_until"] and now >= parse(r["lease_until"]):
                db.execute("UPDATE own_messages SET status = 'uncertain', lease_until = NULL, reason = ?, "
                           "updated_at = ? WHERE id = ?",
                           ("the connector stopped before it said what happened", self.s.now(), r["id"]))
                self.s.event(db, workspace_id, "own.uncertain", r["id"], "core", {"reason": "lease ran out"})

    def result(self, workspace_id: str, gw: str, token: str | None, item_id: str, outcome: str, ref: str = "",
               reason: str = "", session_id=None) -> dict:
        self._check_token(workspace_id, gw, token)
        if outcome not in OUTCOMES:
            raise CoreError(400, "bad_outcome", "an outcome is confirmed, failed or uncertain")
        made = _sid(session_id) if session_id else None
        ref, reason = str(ref)[:128], str(reason)[:200]
        state = OUTCOMES[outcome]
        row = self.s.db.execute("SELECT status, session_id FROM own_messages WHERE id = ? AND workspace_id = ? "
                                "AND gateway = ?", (item_id, workspace_id, gw)).fetchone()
        if not row:
            raise CoreError(404, "no_item", "no such message")
        if row["status"] == state:
            return {"id": item_id, "status": state, "replayed": True}
        if row["status"] != "leased":
            raise CoreError(409, "not_leased", f"that message is {row['status']}, not leased")
        with self.s.tx() as db:
            db.execute("UPDATE own_messages SET status = ?, lease_until = NULL, reason = ?, updated_at = ?, "
                       "made_session_id = coalesce(made_session_id, ?) WHERE id = ?",
                       (state, reason or None, self.s.now(), made if row["session_id"] is None else None, item_id))
            self.s.event(db, workspace_id, f"own.{state}", item_id, f"own connector ({gw})", {"ref": ref})
        return {"id": item_id, "status": state}
