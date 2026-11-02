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
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .auth import Principal
from . import reactions
from .feeds import Feeds
from .reactions import REACTIONS, Reactions  # noqa: F401  (REACTIONS: what the phone offers)
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
# Full history (step 2, plan Part 4): pages fetched from Hermes's own state.db by the connector, held in
# the core's memory for 10 minutes to answer a second look, and never stored.
PAGE_TTL = timedelta(minutes=10)
PAGE_FRESH = timedelta(seconds=20)   # the newest page of a chat or a search changes as the chat goes on
PAGE_AGAIN = timedelta(seconds=30)   # a request handed out and not answered is handed out again after this
PAGE_HAND = 3                        # page requests handed to a connector in one claim
PAGE_CHATS = 50
PAGE_MESSAGES = 50
PAGE_TEXT = 8000
PAGE_QUERY = 80
MAX_WAIT = 10
PAGE_ID = re.compile(r"^pg_[A-Za-z0-9_-]{8,40}$")
# The reactions the phone offers (redesign step 2). Hermes takes any emoji; the core takes these.
PAGE_REACTIONS = 8                   # reactions kept on one message of a page
REACTION_BY = {"user": "patrick", "agent": "hermes"}
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
        if c.get("bot_chat") is True:
            row["bot_chat"] = True
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


class _Pages:
    """The pages of Patrick's own chats that the phone asked for, in the core's memory only.

    A page is asked for (kind "chats", with `before` and `q`, or kind "messages", with `chat` and
    `before`), handed to that gateway's connector on its next claim, posted back, and held for 10
    minutes. Nothing here is written to the store, the event chain or a log."""

    def __init__(self):
        self.cond = threading.Condition()
        self.held: dict[tuple, tuple[datetime, dict]] = {}
        self.asked: dict[tuple, dict] = {}

    def _drop_old(self, now: datetime) -> None:
        for k in [k for k, (until, _) in self.held.items() if until <= now]:
            del self.held[k]
        for k in [k for k, a in self.asked.items() if now - a["at"] > PAGE_TTL]:
            del self.asked[k]

    def get(self, key: tuple, now: datetime) -> dict | None:
        with self.cond:
            self._drop_old(now)
            got = self.held.get(key)
            return got[1] if got else None

    def ask(self, key: tuple, now: datetime) -> None:
        with self.cond:
            if key not in self.asked and key not in self.held:
                self.asked[key] = {"id": new_id("pg"), "at": now, "handed": None}

    def wait(self, key: tuple, seconds: float, now) -> dict | None:
        """The page, once the connector posts it, for at most `seconds` of real time."""
        end = time.monotonic() + seconds
        with self.cond:
            while True:
                got = self.held.get(key)
                if got:
                    return got[1]
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self.cond.wait(left)

    def hand(self, ws: str, gw: str, now: datetime) -> list[dict]:
        with self.cond:
            self._drop_old(now)
            out = []
            for key, a in sorted(self.asked.items(), key=lambda kv: kv[1]["at"]):
                if key[0] != ws or key[1] != gw or len(out) >= PAGE_HAND:
                    continue
                if a["handed"] and now - a["handed"] < PAGE_AGAIN:
                    continue
                a["handed"] = now
                kind, chat, before, q = key[2:]
                out.append({"id": a["id"], "kind": kind, "chat": chat, "before": before, "q": q})
            return out

    def answer(self, ws: str, gw: str, rid: str, page: dict, now: datetime) -> tuple:
        with self.cond:
            key = next((k for k, a in self.asked.items() if a["id"] == rid and k[0] == ws and k[1] == gw), None)
            if key is None:
                raise CoreError(409, "not_asked", "no page with that id was asked of this gateway")
            a = self.asked.pop(key)
            # When the core handed the ask out, on the core's clock: the connector read the page after
            # that, so whatever the core heard was done before then is in it. read_at is One's clock.
            page = {**page, "handed_at": (a["handed"] or a["at"]).isoformat()}
            self.held[key] = (now + (PAGE_FRESH if key[4] is None else PAGE_TTL), page)
            self.cond.notify_all()
            return key

    def listed(self, ws: str, gw: str, sid: str, now: datetime) -> bool:
        """Whether a chats page this gateway's connector posted names the chat, or a messages page says the
        chat goes on under it."""
        with self.cond:
            self._drop_old(now)
            for key, (_, page) in self.held.items():
                if key[0] != ws or key[1] != gw:
                    continue
                if key[2] == "chats" and any(c["id"] == sid for c in page["chats"]):
                    return True
                if key[2] == "messages" and page.get("continued_as") == sid:
                    return True
            return False


PAGES = _Pages()


def _release(here: Path | None = None) -> str | None:
    """The commit the core runs: the VM installs each release in /opt/thornswood-hermes/releases/<commit>
    after the whole suite passed there (thornswood_brokers/follow_main.py). None anywhere else."""
    name = (here or Path(__file__).resolve().parent).parent.name
    return name if re.match(r"^[0-9a-f]{40}$", name) else None


RELEASE = _release()


class _Connectors:
    """What each own connector last said it was doing, in memory (each claim says it again within
    seconds): its commit, how many turns it is running of how many at once and since when the oldest
    runs, and an update it is on or could not do. It is how the phone says why a message waits."""

    def __init__(self):
        self.lock = threading.Lock()
        self.said: dict = {}

    def seen(self, ws: str, gw: str, body: dict, now: datetime) -> None:
        def count(v, top):
            return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= top else 0
        commit = body.get("commit")
        updating = body.get("updating")
        info = {"commit": commit if isinstance(commit, str) and re.match(r"^[0-9a-f]{40}$", commit) else None,
                "busy": count(body.get("busy"), 100), "parallel": count(body.get("parallel"), 100),
                "busy_for": count(body.get("busy_for"), 10 ** 7),
                "updating": updating if isinstance(updating, str) and re.match(r"^[0-9a-f]{40}$", updating) else None,
                "update_why": str(body.get("update_why") or "")[:200], "at": now}
        with self.lock:
            self.said[(ws, gw)] = info

    def get(self, ws: str, gw: str, now: datetime) -> dict | None:
        with self.lock:
            info = self.said.get((ws, gw))
        return info if info and now - info["at"] < ONLINE else None


CONNECTORS = _Connectors()


def _minutes(seconds: int) -> str:
    m = seconds // 60
    return "under a minute ago" if m < 1 else ("a minute ago" if m == 1 else f"{m} minutes ago")


def fetch_page(key: tuple, wait, now: datetime) -> dict:
    """A page from memory, or asked for and waited on for at most `wait` seconds; {"pending": true} until then."""
    try:
        wait = min(max(float(wait or 0), 0.0), MAX_WAIT)
    except (TypeError, ValueError):
        raise CoreError(400, "bad_wait", f"wait is 0 to {MAX_WAIT} seconds") from None
    got = PAGES.get(key, now)
    if got is None:
        PAGES.ask(key, now)
        got = PAGES.wait(key, wait, now) if wait else None
    return {"pending": True} if got is None else {"pending": False, **got}


def page_kind(rid) -> tuple | None:
    """The request a page id names, or None."""
    if not isinstance(rid, str) or not PAGE_ID.match(rid):
        return None
    with PAGES.cond:
        return next((k for k, a in PAGES.asked.items() if a["id"] == rid), None)


def _page_time(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        return parse(str(value)).isoformat()
    except (ValueError, TypeError):
        raise CoreError(400, "bad_before", "before is an ISO time with a zone") from None


def _page_mid(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        mid = int(str(value))
    except ValueError:
        raise CoreError(400, "bad_before", "before is a message id, a whole number") from None
    if not 0 < mid < 2 ** 63:
        raise CoreError(400, "bad_before", "before is a message id, a whole number")
    return mid


def clean_page(kind: str, body) -> dict:
    """Only what the phone shows of one page, each field cut to size."""
    if not isinstance(body, dict):
        raise CoreError(400, "bad_page", "a page is a JSON object")
    more = body.get("more")
    if not isinstance(more, bool):
        raise CoreError(400, "bad_page", "more is true or false")
    if kind == "chats":
        chats = body.get("chats")
        if not isinstance(chats, list) or len(chats) > PAGE_CHATS:
            raise CoreError(400, "bad_page", f"a page of chats has at most {PAGE_CHATS}")
        out = []
        for c in chats:
            if not isinstance(c, dict):
                raise CoreError(400, "bad_page", "each chat is an object")
            row = {k: _text(c.get(k), n) for k, n in CHAT_FIELDS.items()}
            row["id"] = _sid(row["id"])
            if c.get("bot_chat") is True:
                row["bot_chat"] = True
            out.append(row)
        if len({c["id"] for c in out}) != len(out):
            raise CoreError(400, "bad_page", "the page names a chat twice")
        return {"chats": out, "more": more}
    msgs = body.get("messages")
    if not isinstance(msgs, list) or len(msgs) > PAGE_MESSAGES:
        raise CoreError(400, "bad_page", f"a page of messages has at most {PAGE_MESSAGES}")
    gone = body.get("gone", False)
    if not isinstance(gone, bool):
        raise CoreError(400, "bad_page", "gone is true or false")
    out, last = [], None
    for m in msgs:
        if not isinstance(m, dict):
            raise CoreError(400, "bad_page", "each message is an object")
        mid = m.get("mid")
        if not isinstance(mid, int) or isinstance(mid, bool) or not 0 < mid < 2 ** 63:
            raise CoreError(400, "bad_page", "each message has Hermes's own id, a whole number")
        if last is not None and mid <= last:
            raise CoreError(400, "bad_page", "a page's messages are in Hermes's own order, oldest first")
        last = mid
        row = {"mid": mid, "role": _text(m.get("role"), 16), "text": _text(m.get("text"), PAGE_TEXT),
               "at": _text(m.get("at"), 40)}
        if row["role"] not in ROLES:
            raise CoreError(400, "bad_page", "a message is from the user or the assistant")
        reactions = m.get("reactions", [])
        if not isinstance(reactions, list):
            raise CoreError(400, "bad_page", "a message's reactions are a list")
        row["reactions"] = [{"emoji": _text(r.get("emoji"), 16), "by": r.get("by")}
                            for r in reactions[:PAGE_REACTIONS]
                            if isinstance(r, dict) and r.get("by") in REACTION_BY.values() and r.get("emoji")]
        out.append(row)
    cont = body.get("continued_as")
    try:
        read_at = parse(str(body.get("read_at"))).isoformat() if body.get("read_at") else None
    except (ValueError, TypeError):
        raise CoreError(400, "bad_page", "read_at is an ISO time with a zone") from None
    return {"messages": out, "more": more, "gone": gone, "continued_as": _sid(cont) if cont else None,
            "read_at": read_at}


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
                "connector": self._connector(p.workspace_id, gw),
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

    def _connector(self, workspace_id: str, gw: str) -> dict | None:
        """Which copy its connector runs against the core's release, and an update it is on or could not do."""
        info = CONNECTORS.get(workspace_id, gw, self.s.clock())
        if info is None:
            return None
        return {"commit": info["commit"], "release": RELEASE,
                "current": RELEASE is None or info["commit"] == RELEASE,
                "updating": info["updating"], "update_why": info["update_why"]}

    def _waits(self, workspace_id: str, gw: str, mine: list[dict]) -> None:
        """Why each message still waiting to send waits, in words, from what the connector last said."""
        info = CONNECTORS.get(workspace_id, gw, self.s.clock())
        name = NAMES[gw]
        earlier = False
        for m in mine:
            if m["status"] == "leased":
                earlier = True
            if m["status"] != "queued":
                continue
            if earlier:
                m["waits"] = f"{name} is still answering your earlier message in this chat; this one goes next"
            elif info is None:
                m["waits"] = f"{name}'s connector has not said what it is doing in the last 2 minutes"
            elif info["updating"]:
                m["waits"] = f"{name}'s connector is updating itself to {info['updating'][:7]}; it takes this next"
            elif info["parallel"] and info["busy"] >= info["parallel"]:
                m["waits"] = (f"{name} is still answering {info['busy']} earlier messages, the oldest started "
                              f"{_minutes(info['busy_for'])}; this one goes when one ends")
            elif info["commit"] is None and info["parallel"] == 0:
                m["waits"] = (f"{name}'s connector is an old copy that does not say what it is doing and does not "
                              "update itself; it needs its one-time update")
            elif info["update_why"]:
                m["waits"] = f"{name}'s connector {info['update_why']}"

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
        # His reactions in this chat that the phone has asked for, so it shows each one at once, and
        # how it went, until the chat's own page shows it.
        mine = [dict(r) for r in reversed(rows)]
        self._waits(p.workspace_id, gw, mine)
        return st | {"chat": chat or {"id": sid, "title": ""}, "loaded": opened is not None, "history": history,
                     "mine": mine, "reactions": Reactions(self.s).shown(p.workspace_id, gw, sid),
                     **self._can(p)}

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

    def react(self, p: Principal, gw: str, session_id, message_id, emoji) -> dict:
        """Patrick's reaction on one message of one chat: exactly this emoji, or none (null).

        It rides own_chat_write like a message, and like a message it needs that Hermes to be
        answering. It runs no turn, so it needs no fresh passkey. Hermes keeps it on the message,
        where Desktop shows it too, and tells the model about it on a later turn only when that
        Hermes has display.message_reactions on (contracts.json "reactions"). A row says what
        should be there, so a second tap before the first is set replaces it, and the same tap
        twice is one wish."""
        self._patrick(p)
        gw = self._gateway(gw)
        if not self.s.flag(p.workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "chat with your own Hermes from the phone is off for now")
        sid = _sid(session_id)
        message_id, emoji = reactions.message_id(message_id), reactions.emoji(emoji)
        if not self._listed(p.workspace_id, gw, sid):
            raise CoreError(404, "not_listed", f"{NAMES[gw]} has not listed that chat")
        if not self.online(p.workspace_id, gw):
            why = self._off_why(p.workspace_id, gw)
            st = self.gateway_state(p.workspace_id, gw)
            if st["state"] == "machine_off":
                raise CoreError(409, "one_off", "One is off", why=why, state=st["state"], since=st["since"])
            raise CoreError(409, "gateway_off", f"{NAMES[gw]} is not answering: {why}", why=why,
                            state=st["state"], since=st["since"])
        return {"session_id": sid} | Reactions(self.s).want(p, gw, sid, message_id, emoji)

    # ── full history: pages ───────────────────────────────────────────────

    def _listed(self, workspace_id: str, gw: str, sid: str) -> bool:
        """A chat id answers only on the gateway whose own connector listed it: in its chat list, a page
        of it, a chat Patrick started there, or the chat a compressed one goes on as."""
        snap = Feeds(self.s).get(workspace_id, gw)[gw] or {}
        if any(c["id"] == sid for c in snap.get("chats", [])):
            return True
        if self.s.db.execute("SELECT 1 FROM own_messages WHERE workspace_id = ? AND gateway = ? "
                             "AND made_session_id = ?", (workspace_id, gw, sid)).fetchone():
            return True
        return PAGES.listed(workspace_id, gw, sid, self.s.clock())

    def page(self, p: Principal, gw: str, kind: str, chat=None, before=None, q=None, wait=0) -> dict:
        """One page of the chat list (newest first, `before` a time, `q` words in the title) or of one
        chat's messages (`before` Hermes's own message id). Answered from memory, or once the connector
        posts it, waiting at most `wait` seconds; {"pending": true} until then."""
        self._patrick(p)
        gw = self._gateway(gw)
        if not self.s.flag(p.workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "chat with your own Hermes from the phone is off for now")
        if kind == "chats":
            before = _page_time(before)
            q = str(q or "").strip()[:PAGE_QUERY]
            if before is None and not q:
                # The newest chats are the chat list the connector already sends.
                snap = Feeds(self.s).get(p.workspace_id, gw)[gw] or {}
                chats = snap.get("chats", [])
                return {"pending": False, "chats": chats, "more": len(chats) >= MAX_CHATS}
            key = (p.workspace_id, gw, "chats", None, before, q)
        elif kind == "messages":
            chat = _sid(chat)
            if not self._listed(p.workspace_id, gw, chat):
                raise CoreError(404, "not_listed", f"{NAMES[gw]} has not listed that chat")
            key = (p.workspace_id, gw, "messages", chat, _page_mid(before), "")
        else:
            raise CoreError(404, "no_route", "no such route")
        return fetch_page(key, wait, self.s.clock())

    def page_post(self, workspace_id: str, gw: str, token: str | None, body) -> dict:
        """A page the connector read from Hermes's state.db, for a request the core handed it."""
        self._check_token(workspace_id, gw, token)
        if not self.s.flag(workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "the core takes no page while own_chat_write is off")
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not PAGE_ID.match(body["id"]):
            raise CoreError(400, "bad_page", "a page names the request it answers")
        key = page_kind(body["id"])
        if key is None or key[1] != gw or key[2] not in ("chats", "messages"):
            raise CoreError(409, "not_asked", "no page with that id was asked of this gateway")
        PAGES.answer(workspace_id, gw, body["id"], clean_page(key[2], body.get("page")), self.s.clock())
        return {"ok": True}

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
              why: str = "", state: str | None = None, forwarded_for: str | None = None,
              doing: dict | None = None) -> dict:
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
        CONNECTORS.seen(workspace_id, gw, doing if isinstance(doing, dict) else {}, self.s.clock())
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
            reactions = [{"session_id": r.pop("chat"), **r} for r in Reactions(self.s).hand(db, workspace_id, gw)] if ready else []
        return {"items": items, "wanted": self._wanted(workspace_id, gw),
                "pages": PAGES.hand(workspace_id, gw, self.s.clock()), "reactions": reactions, "release": RELEASE}

    def reacted(self, workspace_id: str, gw: str, token: str | None, body) -> dict:
        """What came of setting one reaction. An answer about an older wish than the newest is dropped,
        and the newest goes out on the next claim."""
        self._check_token(workspace_id, gw, token)
        if not isinstance(body, dict):
            raise CoreError(400, "bad_result", "a result is a JSON object")
        return Reactions(self.s).answered(workspace_id, gw, _sid(body.get("session_id")), body, f"own connector ({gw})")

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
