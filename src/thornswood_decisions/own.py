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
from .own_control import CONTROLS, MAX_WAIT as CONTROL_WAIT, wanted as control_wanted
from .own_jobs import DO as JOB_DO, JOB_ID, JOBS
from .own_sessions import OwnSessions
from . import alerts
from .own_turns import ORDERS, answer_value, clean_asks, clean_order
from .reactions import REACTIONS, Reactions  # noqa: F401  (REACTIONS: what the phone offers)
from .schema import OWN_GATEWAYS
from .live_turns import LIVE
from .wakes import WAKES
from .store import CoreError, Store, canonical, new_id, parse, sha256_hex

NAMES = {"me": "my-hermes", "one": "One"}
TOKENS = {"me": "own-me", "one": "own-one"}
ONLINE = timedelta(minutes=2)
SEEN_EVERY = timedelta(seconds=20)   # how often a connector's poll is written down
SIGN_IN = timedelta(minutes=30)
LEASE = timedelta(minutes=20)        # the connector gives a turn 15 minutes
MAX_TEXT = 8000
BOT_WAITING = 3                      # a Bot's messages to One waiting at once, at most
BOT_REPLY = 8000                     # the newest characters of One's answer that go back to the Bot
# What the core says to One's Hermes under a Bot's words (Patrick, 9 Nov, q-86: "why not just have it message
# my One bot?", "yeah so it doesnt have to wait on me"). The signature is Hermes's own for a Bot's message
# (tools/bot_mode_dm.py), so One's Hermes reads it as Bot Mode does.
BOT_NOTE = ("\n\n(Sent to you through the decision core. Patrick agreed on 9 Nov that steps from the agreed "
            "plan that a Bot sends here run without asking him. Do not run a step that needs a secret, money, or "
            "anything destructive that is not in the plan: say so in your answer instead, and the Bot puts it "
            "on his page as a card. Your answer goes back to the Bot.)")
# Files and photos on a message (the + in the box, 2 Nov): each at most 20 MB, at most 10 on a message, and
# at most 100 MB and 20 files uploaded and not yet sent at once for each signed-in phone, so an upload that
# is never sent cannot fill the store. An upload not sent in a day is dropped.
MAX_ATTACHMENT = 20 * 1024 * 1024
MAX_ATTACHMENTS = 10
MAX_UNSENT_BYTES = 100 * 1024 * 1024
MAX_UNSENT = 20
UNSENT_FOR = timedelta(days=1)
ATTACH_ID = re.compile(r"^att_[A-Za-z0-9_-]{8,64}$")
MIME = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,127}$")
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
FIRST = 120   # a chat Hermes has not titled: its first message, which names it (own_connector._row)
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


def _chat_row(c: dict) -> dict:
    """One chat as a connector sends it, kept to its fields and their lengths."""
    row = {k: _text(c.get(k), n) for k, n in CHAT_FIELDS.items()}
    row["id"] = _sid(row["id"])
    first = _text(c.get("first"), FIRST)
    if first and not row["title"]:
        row["first"] = first
    if c.get("bot_chat") is True:
        row["bot_chat"] = True
    if c.get("pinned") is True:
        row["pinned"] = True
    return row


def _sid(value) -> str:
    if not isinstance(value, str) or not SESSION_ID.match(value):
        raise CoreError(400, "bad_chat", "a chat's id is 1 to 128 letters, digits and _ . : -, starting with a "
                        "letter or digit")
    return value


def _when(c: dict) -> datetime:
    try:
        return parse(c.get("last_at") or "")
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _newest(chats: list) -> dict | None:
    """The chat last written in. Not simply the first: the connector lists his Bot Chat first however
    long ago it was written in."""
    return max(chats, key=_when) if chats else None


MAX_QUOTE = 2000
QUOTE_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# Hermes reads "@<kind>:<value>", "@diff" and "@staged" anywhere in a message it is given as a file, folder,
# git, url or plugin reference and attaches what it names (agent/context_references.py, REFERENCE_PATTERN and
# _PLUGIN_REFERENCE_PATTERN), and it puts what it attached under "--- Attached Context ---". A quote is words
# Patrick saw, never a request for a file: on 2 Nov a quote that held a paste's reference attached the paste a
# second time, and one cut inside a reference asked for a file that does not exist.
ATTACHED = re.compile(r"(?:^|\n)--- Attached Context ---[ \t]*\n.*\Z", re.S)
WARNINGS = re.compile(r"(?:^|\n)--- Context Warnings ---.*\Z", re.S)
QUOTED_REF = re.compile(r"""(?<![\w/])@([a-zA-Z][a-zA-Z0-9_-]*):("[^"\n]+"|'[^'\n]+'|`[^`\n]+`|\S+)""")
SIMPLE_REF = re.compile(r"(?<![\w/])@(diff|staged)\b")
PASTE = re.compile(r"(?:^|[\\/])pasted_content_[\w-]+\.txt$")


def _ref_label(m: re.Match) -> str:
    kind, target, after = m.group(1), m.group(2), ""
    if len(target) > 1 and target[0] in "\"'`" and target[-1] == target[0]:
        target = target[1:-1]
    else:
        # As Hermes reads a bare one, the punctuation after it is the sentence's, not the reference's.
        bare = target.rstrip(",.;!?)]}")
        target, after = bare or target, target[len(bare):] if bare else ""
    if kind == "file" and PASTE.search(target):
        return "[pasted text]" + after
    if kind == "url":
        return target + after
    name = re.split(r"[\\/]", target.rstrip("\\/"))[-1] or target
    return f"[{kind}: {name}]" + after


def inert(text: str) -> str:
    """Words with nothing Hermes would read as a reference: the context Hermes attached is left out, each
    reference becomes its name in brackets, and "@diff" or "@staged" gets a word joiner after the "@"."""
    text = WARNINGS.sub("", ATTACHED.sub("", text))
    return SIMPLE_REF.sub(lambda m: "@\u2060" + m.group(1), QUOTED_REF.sub(_ref_label, text))


def _quote(value) -> str:
    """The words a reply quotes, as they are in the message it answers (Hermes quotes the whole message
    rather than a preview, so nothing of a list or code is lost), with every file or other reference in
    them made plain words (inert)."""
    if not isinstance(value, str):
        raise CoreError(400, "bad_quote", "a reply quotes the words of the message it answers")
    value = inert(value.replace("\r\n", "\n")).strip()
    if not value or QUOTE_CONTROL.search(value):
        raise CoreError(400, "bad_quote", "a reply quotes the words of the message it answers, as text")
    if len(value) > MAX_QUOTE:
        raise CoreError(400, "bad_quote", f"a reply quotes at most {MAX_QUOTE} characters")
    return value


def reply_pointer(quote: str, by: str) -> str:
    """Hermes's own reply pointer, the line its gateway puts at the top of a message that replies to
    another on a messaging app (contracts.json "replies"): "your previous message" when the message
    answered is Hermes's own."""
    who = " your previous message" if by == "hermes" else ""
    return f'[Replying to{who}: "{quote}"]\n\n'


def file_name(name) -> str:
    """A file's name as it is kept and given to Hermes: its last part only, without control characters or
    the characters Windows refuses in a name, at most 120 characters, its extension kept."""
    if not isinstance(name, str):
        raise CoreError(400, "bad_name", "a file has a name")
    name = re.split(r"[\\/]", name.strip())[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "_", name).strip(" .")
    if not name:
        raise CoreError(400, "bad_name", "a file has a name")
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:110] + "." + ext[:9]) if dot and len(ext) <= 9 else name[:120]
    return name


def checked_file(name, mime, body) -> tuple[str, str, bytes]:
    """One uploaded file as it is kept: its name made safe (file_name), its type plain or
    application/octet-stream, and its bytes, which are some and at most MAX_ATTACHMENT. The same for a
    message to his own Hermes (OwnChat.attach) and to a Bot or a room (Chat.attach)."""
    name = file_name(name)
    mime = (mime or "").split(";")[0].strip().lower() or "application/octet-stream"
    if not MIME.match(mime):
        mime = "application/octet-stream"
    if not isinstance(body, (bytes, bytearray)) or not body:
        raise CoreError(400, "bad_file", "a file has some bytes")
    if len(body) > MAX_ATTACHMENT:
        raise CoreError(413, "too_big", f"a file is at most {MAX_ATTACHMENT // (1024 * 1024)} MB")
    return name, mime, bytes(body)


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
        row = _chat_row(c)
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
    out = {"source_time": source_time, "chats": out_chats, "open": out_open}
    # How many chats Hermes hides (archived), for the chat list's "Hidden chats (3)" (#102).
    hidden = body.get("hidden")
    if hidden is not None:
        if not isinstance(hidden, int) or isinstance(hidden, bool) or not 0 <= hidden <= 1_000_000:
            raise CoreError(400, "bad_feed", "hidden is how many chats Hermes hides")
        out["hidden"] = hidden
    return out


def bot_words(text: str) -> str:
    """A Bot's step for One as the Bot wrote it: without the signature and the note the core put round it."""
    if text.startswith("Message from \U0001F916 ") and "): " in text:
        text = text.split("): ", 1)[1]
    return text[:-len(BOT_NOTE)] if text.endswith(BOT_NOTE) else text


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
                WAKES.bump(key[0], key[1])

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

    def forget(self, ws: str, gw: str, kinds: tuple, chat: str) -> None:
        """A turn in that chat just ended, so its newest page no longer holds the answer: dropped, and an
        ask for it handed out before now is dropped too (its page would be read before the answer). The
        next read asks the connector again. Older pages do not change."""
        with self.cond:
            for d in (self.held, self.asked):
                for k in [k for k in d if k[0] == ws and k[1] == gw and k[2] in kinds and k[3] == chat and k[4] is None]:
                    del d[k]

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

    def listed(self, ws: str, gw: str, sid: str, now: datetime, kind: str = "chats") -> bool:
        """Whether a chats page (or with kind "archived", a page of hidden chats) this gateway's connector
        posted names the chat, or a messages page says the chat goes on under it."""
        with self.cond:
            self._drop_old(now)
            for key, (_, page) in self.held.items():
                if key[0] != ws or key[1] != gw:
                    continue
                if key[2] == kind and any(c["id"] == sid for c in page["chats"]):
                    return True
                if kind == "chats" and key[2] == "messages" and page.get("continued_as") == sid:
                    return True
            return False


    def bot_chat(self, ws: str, gw: str, sid: str, now: datetime) -> bool:
        """Whether a chats page this gateway's connector posted names the chat as his Bot Chat."""
        with self.cond:
            self._drop_old(now)
            return any(key[0] == ws and key[1] == gw and key[2] == "chats"
                       and any(c["id"] == sid and c.get("bot_chat") for c in page["chats"])
                       for key, (_, page) in self.held.items())


PAGES = _Pages()


def _release(here: Path | None = None) -> str | None:
    """The commit the core runs: the VM installs each release in /opt/thornswood-hermes/releases/<commit>
    after the whole suite passed there (src/thornswood_brokers/follow_main.py). None anywhere else."""
    name = (here or Path(__file__).resolve().parent).parents[1].name  # <release>/src/thornswood_decisions
    return name if re.match(r"^[0-9a-f]{40}$", name) else None


RELEASE = _release()


# "queued": in Hermes's queue behind the turn already running in that chat; "steered" and "redirected": handed
# into that running turn, by the chat's busy-input setting (room_send.py's turn).
DOING_STATES = ("thinking", "writing", "tool", "queued", "steered", "redirected")
DOING_ID = re.compile(r"^own_[A-Za-z0-9_-]{1,40}$")
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class _Connectors:
    """What each own connector last said it was doing, in memory (each claim says it again within
    seconds): its commit, how many turns it is running of how many at once and since when the oldest
    runs, an update it is on or could not do, and what each running turn is doing now (thinking,
    writing, or a tool by its name, from Hermes's own stream). It is how the phone says why a message
    waits, and the live line under a message being answered."""

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
                "update_why": str(body.get("update_why") or "")[:200], "at": now, "turns": {}}
        for t in body.get("turns") or [] if isinstance(body.get("turns"), list) else []:
            if (isinstance(t, dict) and isinstance(t.get("id"), str) and DOING_ID.match(t["id"])
                    and t.get("state") in DOING_STATES and len(info["turns"]) < 10):
                tool = t.get("tool") if t["state"] == "tool" else None
                info["turns"][t["id"]] = {"state": t["state"],
                                          "tool": tool if isinstance(tool, str) and TOOL_NAME.match(tool) else None,
                                          "admitted": t.get("admitted") is True,
                                          **({"taken": t["taken"]} if t.get("taken") in ("steered", "redirected") else {}),
                                          **({"asks": a} if (a := clean_asks(t.get("asks"))) else {}),
                                          **({"order": o} if (o := clean_order(t.get("order"))) else {}),
                                          **({"session": t["session"]} if isinstance(t.get("session"), str)
                                             and SESSION_ID.match(t["session"]) else {})}
        with self.lock:
            self.said[(ws, gw)] = info

    def get(self, ws: str, gw: str, now: datetime) -> dict | None:
        with self.lock:
            info = self.said.get((ws, gw))
        return info if info and now - info["at"] < ONLINE else None

    def admitted(self, ws: str, gw: str, message_id: str, now: datetime) -> bool:
        """Whether the connector last said Hermes has this message (room_send.py's turn said how it took it)."""
        info = self.get(ws, gw, now)
        return bool(info) and (info["turns"].get(message_id) or {}).get("admitted") is True


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
    if kind in ("chats", "archived"):
        chats = body.get("chats")
        if not isinstance(chats, list) or len(chats) > PAGE_CHATS:
            raise CoreError(400, "bad_page", f"a page of chats has at most {PAGE_CHATS}")
        out = []
        for c in chats:
            if not isinstance(c, dict):
                raise CoreError(400, "bad_page", "each chat is an object")
            row = _chat_row(c)
            out.append(row)
        if len({c["id"] for c in out}) != len(out):
            raise CoreError(400, "bad_page", "the page names a chat twice")
        # Why a search found nothing it could show: Hermes's own search did not answer (own_connector.py pages).
        why = _text(body.get("why"), 200)
        return {"chats": out, "more": more} | ({"why": why} if why and not out else {})
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

    def must_be_online(self, workspace_id: str, gw: str) -> None:
        """Refuses at once, saying why, when that Hermes is not answering."""
        if self.online(workspace_id, gw):
            return
        why = self._off_why(workspace_id, gw)
        st = self.gateway_state(workspace_id, gw)
        if st["state"] == "machine_off":
            raise CoreError(409, "one_off", "One is off", why=why, state=st["state"], since=st["since"])
        raise CoreError(409, "gateway_off", f"{NAMES[gw]} is not answering: {why}", why=why,
                        state=st["state"], since=st["since"])

    @staticmethod
    def name(gw: str) -> str:
        return NAMES[gw]

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
            self.s.wake(workspace_id, gw)

    # ── Patrick's side ────────────────────────────────────────────────────

    def _state(self, p: Principal, gw: str) -> dict:
        snap = Feeds(self.s).get(p.workspace_id, gw)
        return {"gateway": gw, "name": NAMES[gw], "online": self.online(p.workspace_id, gw),
                "connector": self._connector(p.workspace_id, gw),
                **self.gateway_state(p.workspace_id, gw),
                "off_why": self._off_why(p.workspace_id, gw), "last_seen": self._last_seen(p.workspace_id, gw),
                "as_of": snap["as_of"], "snap": snap[gw]}

    def _can(self, p: Principal) -> dict:
        return {"can_send": self.s.flag(p.workspace_id, "own_chat_write"), "sign_in_until": self.sign_in_until(p),
                "can_manage": self.s.flag(p.workspace_id, "own_chat_write")
                and self.s.flag(p.workspace_id, "session_manage_write")}

    def summary(self, p: Principal) -> dict:
        self._patrick(p)
        out = []
        for gw in OWN_GATEWAYS:
            st = self._state(p, gw)
            chats = (st.pop("snap") or {}).get("chats", [])
            out.append(st | {"chats": len(chats), "last": _newest(chats),
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
            "AND principal_id IN (SELECT id FROM principals WHERE kind = 'patrick') "
            "ORDER BY created_at DESC, rowid DESC LIMIT 10", (p.workspace_id, gw)).fetchall()
        listed = {c["id"] for c in snap.get("chats", [])}
        starting = [dict(r) for r in rows if r["made_session_id"] not in listed]
        # Which chat each one made, listed or not: the app goes into the chat it just started (10 Nov).
        made = {r["id"]: r["made_session_id"] for r in rows if r["made_session_id"]}
        return st | {"chats": snap.get("chats", []), "hidden": snap.get("hidden"), "starting": starting, "made": made,
                     "ops": OwnSessions(self.s).shown(p.workspace_id, gw), **self._can(p)}

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
                # What its turn is doing now, as the connector said it seconds ago.
                if info is not None and m["id"] in info["turns"]:
                    m["doing"] = info["turns"][m["id"]]
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
        # In his Bot Chat, the Bots' steps for One too (ask_one), each with the Bot that sent it, in its words.
        bot_chat = bool(chat and chat.get("bot_chat")) or self.is_bot_chat(p.workspace_id, gw, sid)
        rows = self.s.db.execute(
            "SELECT m.id, m.text, m.status, m.reason, m.created_at, m.updated_at, m.reply_to, p.bot AS from_bot "
            "FROM own_messages m JOIN principals p ON p.id = m.principal_id WHERE m.workspace_id = ? "
            "AND m.gateway = ? AND (m.session_id = ? OR m.made_session_id = ? OR (? AND p.bot IS NOT NULL)) "
            "ORDER BY m.created_at DESC, m.rowid DESC LIMIT ?",
            (p.workspace_id, gw, sid, sid, int(bot_chat), MINE)).fetchall()
        # His reactions in this chat that the phone has asked for, so it shows each one at once, and
        # how it went, until the chat's own page shows it.
        mine = [dict(r) if r["from_bot"] else {k: r[k] for k in r.keys() if k != "from_bot"} for r in reversed(rows)]
        for m in mine:
            if m.get("from_bot"):
                m["text"] = bot_words(m["text"])
        files = self._files(p.workspace_id, [m["id"] for m in mine])
        for m in mine:
            if m["id"] in files:
                m["files"] = files[m["id"]]
        self._waits(p.workspace_id, gw, mine)
        return st | {"chat": chat or {"id": sid, "title": ""}, "loaded": opened is not None, "history": history,
                     "mine": mine, "reactions": Reactions(self.s).shown(p.workspace_id, gw, sid),
                     "ops": OwnSessions(self.s).shown(p.workspace_id, gw, sid),
                     **self._can(p)}

    def _unsettled(self, workspace_id: str, gw: str) -> int:
        return self.s.db.execute("SELECT count(*) FROM own_messages WHERE workspace_id = ? AND gateway = ? "
                                 "AND status IN ('queued', 'leased', 'uncertain')", (workspace_id, gw)).fetchone()[0]

    def send(self, p: Principal, gw: str, session_id, new, text, idempotency_key, reply_to=None, quote=None,
             quote_by=None, attachments=None) -> dict:
        """One message to one chat (session_id), or the first message of a new chat (new: true), which
        Hermes names itself, as it does any chat.

        A reply (reply style A, redesign step 4) names the message it answers by Hermes's own id
        (reply_to), the words it quotes and whose they are (quote_by: hermes or patrick). Hermes keeps
        no reply link, so the message Hermes gets opens with Hermes's own reply pointer, the line its
        gateway writes for a reply on a messaging app, in plain sight, exactly as Patrick sees it, and
        the core keeps the link back."""
        self._patrick(p)
        gw = self._gateway(gw)
        if not self.s.flag(p.workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "chat with your own Hermes from the phone is off for now")
        if (session_id is None) != (new is True):
            raise CoreError(400, "bad_chat", "a message goes to one chat: its session_id, or new: true for a new one")
        if session_id is not None:
            session_id = _sid(session_id)
        files = self._wanted_files(attachments)
        if not isinstance(text, str) or not (text.strip() or files):
            raise CoreError(400, "bad_text", "a message has some text or a file")
        text = text.strip()
        if reply_to is not None or quote is not None or quote_by is not None:
            if session_id is None:
                raise CoreError(400, "bad_reply", "a reply goes to the chat its message is in, not to a new chat")
            if quote_by not in ("hermes", "patrick"):
                raise CoreError(400, "bad_reply", "a reply says whose message it answers: hermes or patrick")
            reply_to, quote = reactions.message_id(reply_to), _quote(quote)
            text = reply_pointer(quote, quote_by) + text
        if len(text) > MAX_TEXT:
            raise CoreError(400, "bad_text", f"a message is at most {MAX_TEXT} characters")
        if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 200:
            raise CoreError(400, "bad_key", "a message carries an idempotency key of 8 to 200 characters")
        body_sha = sha256_hex(canonical({"gateway": gw, "session_id": session_id, "text": text}
                                        | ({"reply_to": reply_to} if reply_to is not None else {})
                                        | ({"attachments": files} if files else {})))
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
            self.must_be_online(p.workspace_id, gw)
            now = self.s.now()
            mid = new_id("own")
            db.execute("INSERT INTO own_messages(id, workspace_id, gateway, session_id, text, send_key, "
                       "principal_id, device_id, status, created_at, updated_at, reply_to) "
                       "VALUES(?,?,?,?,?,?,?,?,'queued',?,?,?)",
                       (mid, p.workspace_id, gw, session_id, text, f"tw-own.{mid}", p.id, p.device_id, now, now,
                        reply_to))
            self.s.wake(p.workspace_id, gw)
            for fid in files:
                done = db.execute("UPDATE own_attachments SET message_id = ? WHERE id = ? AND workspace_id = ? "
                                  "AND gateway = ? AND principal_id = ? AND message_id IS NULL AND body IS NOT NULL",
                                  (mid, fid, p.workspace_id, gw, p.id)).rowcount
                if not done:
                    raise CoreError(409, "no_attachment", "a file on this message is not one uploaded here for "
                                    "this Hermes, or it went with another message; add it again")
            self.s.event(db, p.workspace_id, "own.queued", mid, "patrick",
                         {"gateway": gw, "new_chat": session_id is None, "device": p.device_id}
                         | ({"reply": True} if reply_to is not None else {})
                         | ({"files": len(files)} if files else {}))
            receipt = {"message_id": mid, "gateway": gw, "session_id": session_id, "status": "queued",
                       "created_at": now}
            db.execute("INSERT INTO commands(workspace_id, principal_id, idempotency_key, op, body_sha256, "
                       "result_json, created_at) VALUES(?,?,?,?,?,?,?)",
                       (p.workspace_id, p.id, idempotency_key, "own.send", body_sha, canonical(receipt), now))
        return receipt

    def ask_one(self, p: Principal, text, key=None) -> dict:
        """A Bot's message to One's Hermes, run in its Bot Chat without Patrick (Patrick, 9 Nov, q-86); its
        answer goes back to the Bot (result, Chat._one_text). Only to One: the steps the Bots cannot run
        themselves are the ones on Patrick's PC. One message per Bot in flight and BOT_WAITING waiting, so a
        Bot cannot flood his PC."""
        if p.kind not in ("bramble", "specialist") or not p.bot:
            raise CoreError(403, "not_a_bot", "only a Bot sends a message to One this way")
        if not self.s.flag(p.workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "nothing goes to One while own_chat_write is off")
        if not isinstance(text, str) or not text.strip():
            raise CoreError(400, "bad_text", "a message to One has its text")
        sign = f"Message from \U0001F916 {p.bot.capitalize()} (@{p.bot}): "
        body = sign + text.strip() + BOT_NOTE
        if len(body) > MAX_TEXT:
            raise CoreError(400, "bad_text", f"a message to One is at most {MAX_TEXT - len(sign) - len(BOT_NOTE)} "
                            "characters")
        if key is not None and (not isinstance(key, str) or not 8 <= len(key) <= 200):
            raise CoreError(400, "bad_key", "a source_key is 8 to 200 characters")
        body_sha = sha256_hex(canonical({"gateway": "one", "text": body}))
        with self.s.tx() as db:
            if key is not None:
                cmd = db.execute("SELECT op, body_sha256, result_json FROM commands WHERE workspace_id = ? "
                                 "AND principal_id = ? AND idempotency_key = ?", (p.workspace_id, p.id, key)).fetchone()
                if cmd:
                    if cmd["op"] != "own.ask_one" or cmd["body_sha256"] != body_sha:
                        raise CoreError(409, "key_reused", "that source_key was used for a different message")
                    return json.loads(cmd["result_json"]) | {"replayed": True}
            waiting = db.execute("SELECT count(*) FROM own_messages WHERE workspace_id = ? AND gateway = 'one' "
                                 "AND principal_id = ? AND status IN ('queued', 'leased')",
                                 (p.workspace_id, p.id)).fetchone()[0]
            if waiting >= BOT_WAITING:
                raise CoreError(429, "one_busy", f"{BOT_WAITING} of your messages to One are waiting already; "
                                "send the next once One answers one")
            now = self.s.now()
            mid = new_id("own")
            db.execute("INSERT INTO own_messages(id, workspace_id, gateway, session_id, text, send_key, "
                       "principal_id, device_id, status, created_at, updated_at) "
                       "VALUES(?,?,'one',NULL,?,?,?,NULL,'queued',?,?)",
                       (mid, p.workspace_id, body, f"tw-own.{mid}", p.id, now, now))
            self.s.wake(p.workspace_id, "one")
            self.s.event(db, p.workspace_id, "own.queued", mid, p.label, {"gateway": "one", "bot": p.bot})
            receipt = {"message_id": mid, "gateway": "one", "status": "queued", "created_at": now,
                       "one_online": self.online(p.workspace_id, "one")}
            if key is not None:
                db.execute("INSERT INTO commands(workspace_id, principal_id, idempotency_key, op, body_sha256, "
                           "result_json, created_at) VALUES(?,?,?,?,?,?,?)",
                           (p.workspace_id, p.id, key, "own.ask_one", body_sha, canonical(receipt), now))
        return receipt

    def one_answer(self, p: Principal, message_id) -> dict:
        """A Bot's message to One as it stands, and One's answer once it came: only the Bot that sent it."""
        row = self.s.db.execute("SELECT status, reason, created_at, updated_at FROM own_messages WHERE id = ? "
                                "AND workspace_id = ? AND gateway = 'one' AND principal_id = ?",
                                (message_id, p.workspace_id, p.id)).fetchone() if isinstance(message_id, str) else None
        if not row:
            raise CoreError(404, "no_item", "no message of yours to One by that id")
        out = {"message_id": message_id, "status": row["status"], "reason": row["reason"],
               "created_at": row["created_at"], "updated_at": row["updated_at"]}
        cx = self.s.db.execute("SELECT body_json FROM connector_outbox WHERE idempotency_key = ?",
                               (f"one:{message_id}",)).fetchone()
        if cx:
            out["answer"] = json.loads(cx["body_json"]).get("reply")
        return out

    @staticmethod
    def _wanted_files(attachments) -> list[str]:
        if attachments is None:
            return []
        if (not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS
                or not all(isinstance(a, str) and ATTACH_ID.match(a) for a in attachments)
                or len(set(attachments)) != len(attachments)):
            raise CoreError(400, "bad_attachments", f"attachments are up to {MAX_ATTACHMENTS} ids of files uploaded here")
        return list(attachments)

    def attach(self, p: Principal, gw: str, name, mime, body: bytes) -> dict:
        """One file or photo for a message to this Hermes, uploaded on its own before the message is sent.
        Nothing reaches Hermes until a message names it."""
        self._patrick(p)
        gw = self._gateway(gw)
        if not self.s.flag(p.workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "chat with your own Hermes from the phone is off for now")
        name, mime, body = checked_file(name, mime, body)
        now = self.s.now()
        with self.s.tx() as db:
            # Uploads never sent go after a day.
            db.execute("DELETE FROM own_attachments WHERE workspace_id = ? AND message_id IS NULL AND created_at < ?",
                       (p.workspace_id, (self.s.clock() - UNSENT_FOR).isoformat()))
            n, total = db.execute("SELECT count(*), coalesce(sum(size), 0) FROM own_attachments WHERE workspace_id = ? "
                                  "AND principal_id = ? AND message_id IS NULL", (p.workspace_id, p.id)).fetchone()
            if n >= MAX_UNSENT or total + len(body) > MAX_UNSENT_BYTES:
                raise CoreError(429, "too_many_files", "too many files are waiting to be sent; send or take some off first")
            fid = new_id("att")
            digest = sha256_hex(bytes(body))
            db.execute("INSERT INTO own_attachments(id, workspace_id, gateway, principal_id, name, mime, size, sha256, "
                       "body, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (fid, p.workspace_id, gw, p.id, name, mime, len(body), digest, bytes(body), now))
        return {"id": fid, "name": name, "mime": mime, "size": len(body), "sha256": digest}

    def unattach(self, p: Principal, gw: str, fid) -> dict:
        """Takes off a file uploaded and not sent."""
        self._patrick(p)
        gw = self._gateway(gw)
        if not isinstance(fid, str) or not ATTACH_ID.match(fid):
            raise CoreError(404, "no_attachment", "no such file")
        with self.s.tx() as db:
            gone = db.execute("DELETE FROM own_attachments WHERE id = ? AND workspace_id = ? AND gateway = ? "
                              "AND principal_id = ? AND message_id IS NULL", (fid, p.workspace_id, gw, p.id)).rowcount
        if not gone:
            raise CoreError(404, "no_attachment", "no such file waiting to be sent")
        return {"id": fid, "removed": True}

    def _files(self, workspace_id: str, mids: list[str]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        if not mids:
            return out
        q = ",".join("?" * len(mids))
        for r in self.s.db.execute(f"SELECT id, message_id, name, mime, size FROM own_attachments WHERE workspace_id = ? "
                                   f"AND message_id IN ({q}) ORDER BY rowid", (workspace_id, *mids)).fetchall():
            out.setdefault(r["message_id"], []).append({"id": r["id"], "name": r["name"], "mime": r["mime"],
                                                        "size": r["size"]})
        return out

    def attachment(self, workspace_id: str, gw: str, token: str | None, fid) -> tuple[str, bytes]:
        """A file's bytes, for that gateway's connector, only while the message it goes with is leased."""
        self._check_token(workspace_id, gw, token)
        if not isinstance(fid, str) or not ATTACH_ID.match(fid):
            raise CoreError(404, "no_attachment", "no such file")
        row = self.s.db.execute("SELECT a.mime, a.body FROM own_attachments a JOIN own_messages m ON m.id = a.message_id "
                                "WHERE a.id = ? AND a.workspace_id = ? AND a.gateway = ? AND m.status = 'leased' "
                                "AND a.body IS NOT NULL", (fid, workspace_id, gw)).fetchone()
        if not row:
            raise CoreError(404, "no_attachment", "no such file on a message being sent")
        with self.s.tx() as db:
            db.execute("UPDATE own_attachments SET fetched_at = coalesce(fetched_at, ?) WHERE id = ?", (self.s.now(), fid))
        return row["mime"], bytes(row["body"])

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
        self.must_be_online(p.workspace_id, gw)
        return {"session_id": sid} | Reactions(self.s).want(p, gw, sid, message_id, emoji)

    def session(self, p: Principal, gw: str, session_id, op, title=None, idempotency_key=None) -> dict:
        """Rename, hide (archive), bring back (unarchive) or delete one chat (own_sessions.py)."""
        self._patrick(p)
        gw = self._gateway(gw)
        if not self.s.flag(p.workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "chat with your own Hermes from the phone is off for now")
        return OwnSessions(self.s).ask(self, p, gw, _sid(session_id), op, title, idempotency_key)

    def listed_or_hidden(self, workspace_id: str, gw: str, sid: str, hidden: bool = False) -> bool:
        """A chat this gateway listed, or for bringing one back, one a page of its hidden chats listed."""
        if hidden:
            return PAGES.listed(workspace_id, gw, sid, self.s.clock(), kind="archived")
        return self._listed(workspace_id, gw, sid)

    def is_bot_chat(self, workspace_id: str, gw: str, sid: str) -> bool:
        """Whether this gateway's connector listed the chat as his Bot Chat, in its chat list or a page of it."""
        snap = Feeds(self.s).get(workspace_id, gw)[gw] or {}
        if any(c["id"] == sid and c.get("bot_chat") for c in snap.get("chats", [])):
            return True
        return PAGES.bot_chat(workspace_id, gw, sid, self.s.clock())

    def session_done(self, workspace_id: str, gw: str, token: str | None, body) -> dict:
        self._check_token(workspace_id, gw, token)
        return OwnSessions(self.s).done(workspace_id, gw, body)

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
        elif kind == "archived":
            # The chats Hermes hides (archived), to bring one back: never in the chat list.
            key = (p.workspace_id, gw, "archived", None, _page_time(before), "")
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
        if key is None or key[1] != gw or key[2] not in ("chats", "archived", "messages"):
            raise CoreError(409, "not_asked", "no page with that id was asked of this gateway")
        PAGES.answer(workspace_id, gw, body["id"], clean_page(key[2], body.get("page")), self.s.clock())
        return {"ok": True}

    # ── this chat's model and thinking: the details pane (own_control.py) ───

    def control(self, p: Principal, gw: str, chat, wait=0) -> dict:
        """What Hermes holds for one chat: its model, thinking level, approvals and busy setting, and the
        models that can be picked, with the newest change asked for and how it went. From memory, or read
        through the connector, waiting at most `wait` seconds; "pending" while no read is in."""
        self._patrick(p)
        gw = self._gateway(gw)
        if not self.s.flag(p.workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "chat with your own Hermes from the phone is off for now")
        chat = _sid(chat)
        if not self._listed(p.workspace_id, gw, chat):
            raise CoreError(404, "not_listed", f"{NAMES[gw]} has not listed that chat")
        try:
            wait = min(max(float(wait or 0), 0.0), CONTROL_WAIT)
        except (TypeError, ValueError):
            raise CoreError(400, "bad_wait", f"wait is 0 to {CONTROL_WAIT} seconds") from None
        key, now = (p.workspace_id, gw, chat), self.s.clock()
        CONTROLS.ask(key, now)
        if wait:
            CONTROLS.wait(key, wait, None)
        got = CONTROLS.state(key, self.s.clock())
        return {"pending": got["state"] is None, **got, "can_change": self.sign_in_until(p) is not None}

    def control_set(self, p: Principal, gw: str, chat, body) -> dict:
        """Change one chat's model, thinking level or both; that chat only, never Hermes's default."""
        self._patrick(p)
        gw = self._gateway(gw)
        if not self.s.flag(p.workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "chat with your own Hermes from the phone is off for now")
        chat = _sid(chat)
        if not self._listed(p.workspace_id, gw, chat):
            raise CoreError(404, "not_listed", f"{NAMES[gw]} has not listed that chat")
        if self.sign_in_until(p) is None:
            raise CoreError(403, "passkey_needed", "changing a chat's model or thinking needs a passkey sign-in "
                            "from the last 30 minutes, as a message does; sign in with your passkey again")
        idem = body.get("idempotency_key") if isinstance(body, dict) else None
        if not isinstance(idem, str) or not 8 <= len(idem) <= 200:
            raise CoreError(400, "bad_key", "a change carries an idempotency key of 8 to 200 characters")
        key = (p.workspace_id, gw, chat)
        want = control_wanted(CONTROLS.state(key, self.s.clock())["state"], body)
        self.must_be_online(p.workspace_id, gw)
        out = CONTROLS.change(key, want, idem, self.s.clock())
        if not out.get("replayed"):
            with self.s.tx() as db:
                # What was asked for, never any words of the chat.
                self.s.event(db, p.workspace_id, "own.control", out["id"], "patrick",
                             {"gateway": gw, "device": p.device_id, **want})
        return out

    def turn_order(self, p: Principal, gw: str, body) -> dict:
        """Stop a turn of his that is running, or answer a question or an approval it has open
        (own_turns.py): an order for that Hermes's connector, carried out by the turn itself."""
        self._patrick(p)
        gw = self._gateway(gw)
        if not self.s.flag(p.workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "chat with your own Hermes from the phone is off for now")
        body = body if isinstance(body, dict) else {}
        mid, op = body.get("message_id"), body.get("op")
        if op not in ("stop", "answer"):
            raise CoreError(400, "bad_order", "an order is stop or answer")
        if not isinstance(mid, str):
            raise CoreError(400, "bad_order", "an order names the message whose turn it is for")
        row = self.s.db.execute("SELECT status FROM own_messages WHERE id = ? AND workspace_id = ? AND gateway = ?",
                                (mid, p.workspace_id, gw)).fetchone()
        info = CONNECTORS.get(p.workspace_id, gw, self.s.clock())
        turn = (info or {}).get("turns", {}).get(mid) if row and row["status"] == "leased" else None
        if turn is None or not turn.get("admitted"):
            raise CoreError(409, "not_running", f"that message's turn is not running in {NAMES[gw]} now")
        if self.sign_in_until(p) is None:
            raise CoreError(403, "passkey_needed", "stopping a turn or answering it runs with your Hermes's full "
                            "power, so it needs a passkey sign-in from the last 30 minutes; sign in again")
        pid, value = None, None
        if op == "answer":
            pid = body.get("prompt_id")
            ask = next((a for a in turn.get("asks", []) if a["prompt_id"] == pid), None)
            if ask is None:
                raise CoreError(409, "not_open", "that question is no longer open")
            value = answer_value(ask, body.get("value"))
            if value is None:
                raise CoreError(400, "bad_answer", "an approval takes one of the choices Hermes offered, and a "
                                "question takes an answer of at most 4000 characters or some of its choices")
        self.must_be_online(p.workspace_id, gw)
        out = ORDERS.add(p.workspace_id, gw, mid, op, pid, value, self.s.clock())
        with self.s.tx() as db:
            # What was done, never the words of a question or an answer.
            self.s.event(db, p.workspace_id, "own.order", out["order"], "patrick",
                         {"gateway": gw, "message": mid, "op": op, "device": p.device_id}
                         | ({"kind": ask["kind"]} if op == "answer" else {}))
        return out

    def control_post(self, workspace_id: str, gw: str, token: str | None, body) -> dict:
        """What the connector read for a chat, and for a change whether Hermes took it."""
        self._check_token(workspace_id, gw, token)
        if not self.s.flag(workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "the core takes no control answer while own_chat_write is off")
        return CONTROLS.answer(workspace_id, gw, body, self.s.clock())

    # ── his scheduled jobs: Pause or Resume, Run now and Remove, from the Tasks tab (own_jobs.py) ──

    def job_set(self, p: Principal, gw: str, body) -> dict:
        """Ask that Hermes's connector to pause, resume, run now or remove one of its scheduled jobs."""
        self._patrick(p)
        gw = self._gateway(gw)
        if not self.s.flag(p.workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "chat with your own Hermes from the phone is off for now")
        body = body if isinstance(body, dict) else {}
        job, do, idem = body.get("job"), body.get("do"), body.get("idempotency_key")
        if do not in JOB_DO:
            raise CoreError(400, "bad_job", "a job change is pause, resume, run or remove")
        if not isinstance(job, str) or not JOB_ID.match(job):
            raise CoreError(400, "bad_job", "a job change names the job by its id")
        if not isinstance(idem, str) or not 8 <= len(idem) <= 200:
            raise CoreError(400, "bad_key", "a job change carries an idempotency key of 8 to 200 characters")
        from .tasks import Tasks
        if Tasks(self.s).job(p.workspace_id, gw, job) is None:
            raise CoreError(404, "not_listed", f"{NAMES[gw]} has not listed that job lately; open Tasks again")
        if self.sign_in_until(p) is None:
            raise CoreError(403, "passkey_needed", "changing a scheduled job runs with your Hermes's full power, so "
                            "it needs a passkey sign-in from the last 30 minutes; sign in with your passkey again")
        self.must_be_online(p.workspace_id, gw)
        out = JOBS.add(p.workspace_id, gw, job, do, idem, self.s.clock())
        if not out.get("replayed"):
            with self.s.tx() as db:
                self.s.event(db, p.workspace_id, "own.job", out["id"], "patrick",
                             {"gateway": gw, "job": job, "do": do, "device": p.device_id})
        return out

    def job_post(self, workspace_id: str, gw: str, token: str | None, body) -> dict:
        """Whether Hermes made a job change the connector was handed."""
        self._check_token(workspace_id, gw, token)
        if not self.s.flag(workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "the core takes no job answer while own_chat_write is off")
        return JOBS.answer(workspace_id, gw, body, self.s.clock())

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

    def tasks(self, workspace_id: str, gw: str, token: str | None, body) -> dict:
        """What this Hermes has going besides its chats, for the app's Tasks tab (tasks.py). Refused while
        own_chat_write is off, like the chat list, since it names his chats."""
        self._check_token(workspace_id, gw, token)
        if not self.s.flag(workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "the core takes nothing of his own Hermes while own_chat_write is off")
        from .tasks import Tasks
        Tasks(self.s).put(workspace_id, gw, body)
        return {}

    def claim(self, workspace_id: str, gw: str, token: str | None, limit: int = MAX_CLAIM, ready=False,
              why: str = "", state: str | None = None, forwarded_for: str | None = None,
              doing: dict | None = None) -> dict:
        """Lease the oldest waiting message of each chat, at most `limit`.

        A chat that has a message in flight gets its next one too, like Desktop's box while a turn runs
        (Patrick's answer A on q-one-busy-chat, 2 Nov): room_send.py's turn then steers it into the running turn,
        redirects that turn, or queues it, by the chat's own busy-input setting. Only once every message
        before it in that chat is in Hermes (the connector says so), so Hermes gets them in the order he
        sent them; and never in a new chat, which has no id to send a second message to until its first
        turn made one.

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
        self._asks_alerts(workspace_id, gw)
        if not self.s.flag(workspace_id, "own_chat_write"):
            raise CoreError(423, "own_chat_off", "nothing goes to your own Hermes while own_chat_write is off")
        items = []
        with self.s.tx() as db:
            self._lapse(db, workspace_id, gw)
            self._made(db, workspace_id, gw)
            rows = db.execute("SELECT m.id, m.session_id, m.text, m.send_key, m.status, p.bot FROM own_messages m "
                              "JOIN principals p ON p.id = m.principal_id "
                              "WHERE m.workspace_id = ? AND m.gateway = ? AND m.status IN ('queued', 'leased') "
                              "ORDER BY m.created_at, m.rowid", (workspace_id, gw)).fetchall()
            # In each chat, the next message only once every one before it is in Hermes; each new chat is a
            # chat of its own, one message at a time; the Bots' messages share the Bot Chat, one at a time.
            seen_at = self.s.clock()

            def chat_of(r):
                return "bot chat" if r["bot"] else r["session_id"] or r["id"]
            busy = {chat_of(r) for r in rows if r["status"] == "leased"
                    and not (r["session_id"] and CONNECTORS.admitted(workspace_id, gw, r["id"], seen_at))}
            until = (self.s.clock() + LEASE).isoformat()
            now = self.s.now()
            for r in rows:
                key = chat_of(r)
                if r["status"] != "queued" or key in busy or len(items) >= limit or not ready:
                    continue
                busy.add(key)
                db.execute("UPDATE own_messages SET status = 'leased', lease_until = ?, attempts = attempts + 1, "
                           "updated_at = ? WHERE id = ?", (until, now, r["id"]))
                self.s.event(db, workspace_id, "own.leased", r["id"], f"own connector ({gw})", {})
                files = [dict(f) for f in db.execute(
                    "SELECT id, name, mime, size, sha256 FROM own_attachments WHERE message_id = ? ORDER BY rowid",
                    (r["id"],)).fetchall()]
                items.append({"id": r["id"], "session_id": r["session_id"], "text": r["text"],
                              "send_key": r["send_key"], "lease_until": until}
                             | ({"attachments": files} if files else {}) | ({"bot_chat": True} if r["bot"] else {}))
            reactions = [{"session_id": r.pop("chat"), **r} for r in Reactions(self.s).hand(db, workspace_id, gw)] if ready else []
            sessions = OwnSessions(self.s).hand(db, workspace_id, gw) if ready else []
        return {"items": items, "wanted": self._wanted(workspace_id, gw),
                "pages": PAGES.hand(workspace_id, gw, self.s.clock()), "reactions": reactions, "sessions": sessions,
                "controls": CONTROLS.hand(workspace_id, gw, self.s.clock()) if ready else [],
                "jobs": JOBS.hand(workspace_id, gw, self.s.clock()) if ready else [],
                "orders": ORDERS.hand(workspace_id, gw, self.s.clock()), "release": RELEASE}

    def _made(self, db, workspace_id: str, gw: str) -> None:
        """A new chat's id, as the connector says it the moment Hermes made the chat, is kept on its first
        message while that first turn still runs: the app goes into the chat at once, as Desktop does, and the
        turn shows there as it happens. 10 Nov: the id came only with the turn's result, so a long first turn
        sat under Starting on "Sending" with no chat to open until it ended."""
        info = CONNECTORS.get(workspace_id, gw, self.s.clock())
        for mid, t in ((info or {}).get("turns") or {}).items():
            if t.get("session"):
                db.execute("UPDATE own_messages SET made_session_id = ? WHERE id = ? AND workspace_id = ? "
                           "AND gateway = ? AND status = 'leased' AND session_id IS NULL AND made_session_id IS NULL",
                           (t["session"], mid, workspace_id, gw))

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
                db.execute("UPDATE own_attachments SET body = NULL WHERE message_id = ?", (r["id"],))
                self.s.event(db, workspace_id, "own.uncertain", r["id"], "core", {"reason": "lease ran out"})

    def _asks_alerts(self, workspace_id: str, gw: str) -> None:
        """A question or approval a turn of his opened, as the connector just said: one line on his phone for
        each, once (alerts.py), opening the chat it waits in."""
        info = CONNECTORS.get(workspace_id, gw, self.s.clock())
        waiting = {mid: t["asks"] for mid, t in (info or {}).get("turns", {}).items() if t.get("asks")}
        if not waiting:
            return
        with self.s.tx() as db:
            for mid, asks in waiting.items():
                row = db.execute("SELECT session_id, made_session_id FROM own_messages WHERE id = ? AND workspace_id = ? "
                                 "AND gateway = ?", (mid, workspace_id, gw)).fetchone()
                if not row:
                    continue
                for a in asks:
                    alerts.queue(self.s, db, workspace_id, "asks", f"{mid}:{a['prompt_id']}", alerts.OWN_WHO[gw],
                                 alerts.own_path(gw, row["session_id"] or row["made_session_id"]))

    def wait(self, workspace_id: str, gw: str, token: str | None, body) -> dict:
        """That gateway's connector, waiting for new work (wakes.py): the number once it changes, or after
        `seconds`. Only a number, so it is answered whatever the flags say."""
        self._check_token(workspace_id, gw, token)
        return WAKES.wait(workspace_id, self._gateway(gw), body.get("after"), body.get("seconds"))

    def turns(self, workspace_id: str, gw: str, token: str | None, body) -> dict:
        """What each turn this connector runs now shows, as Desktop draws it (live_turns.py), each time it
        changes. Only turns of this gateway's own messages."""
        self._check_token(workspace_id, gw, token)
        turns = body.get("turns") if isinstance(body, dict) else None
        ids = [t.get("id") for t in turns if isinstance(t, dict)] if isinstance(turns, list) else []
        if any(not isinstance(i, str) or not DOING_ID.match(i) for i in ids) or (ids and self.s.db.execute(
                "SELECT count(*) FROM own_messages WHERE workspace_id = ? AND gateway = ? "
                f"AND id IN ({','.join('?' * len(ids))})", (workspace_id, gw, *ids)).fetchone()[0] != len(set(ids))):
            raise CoreError(400, "bad_live", "each turn is one of his messages to this Hermes")
        return {"kept": LIVE.put(workspace_id, turns, self.s.clock())}

    def result(self, workspace_id: str, gw: str, token: str | None, item_id: str, outcome: str, ref: str = "",
               reason: str = "", session_id=None, reply=None) -> dict:
        self._check_token(workspace_id, gw, token)
        if outcome not in OUTCOMES:
            raise CoreError(400, "bad_outcome", "an outcome is confirmed, failed or uncertain")
        made = _sid(session_id) if session_id else None
        ref, reason = str(ref)[:128], str(reason)[:200]
        state = OUTCOMES[outcome]
        row = self.s.db.execute("SELECT m.status, m.session_id, p.bot FROM own_messages m JOIN principals p "
                                "ON p.id = m.principal_id WHERE m.id = ? AND m.workspace_id = ? AND m.gateway = ?",
                                (item_id, workspace_id, gw)).fetchone()
        if not row:
            raise CoreError(404, "no_item", "no such message")
        if row["status"] == state:
            return {"id": item_id, "status": state, "replayed": True}
        if row["status"] != "leased":
            raise CoreError(409, "not_leased", f"that message is {row['status']}, not leased")
        # The turn is over and its answer is in Hermes: the chat's newest page is read again, not shown
        # from memory for up to PAGE_FRESH without it.
        for sid in {row["session_id"], made} - {None}:
            PAGES.forget(workspace_id, gw, ("messages",), sid)
        LIVE.done(workspace_id, item_id)
        with self.s.tx() as db:
            db.execute("UPDATE own_messages SET status = ?, lease_until = NULL, reason = ?, updated_at = ?, "
                       "made_session_id = coalesce(made_session_id, ?) WHERE id = ?",
                       (state, reason or None, self.s.now(), made if row["session_id"] is None else None, item_id))
            self.s.event(db, workspace_id, f"own.{state}", item_id, f"own connector ({gw})", {"ref": ref})
            if row["bot"]:
                # A Bot's message: what came of it, and One's answer, go back to that Bot in its own Bot Chat
                # (Chat.claim, _one_text). One's words are kept as long as a card's answer is.
                db.execute("INSERT OR IGNORE INTO connector_outbox(id, workspace_id, decision_id, kind, target, "
                           "body_json, idempotency_key, status, created_at, updated_at) "
                           "VALUES(?,?,NULL,'one.answered',?,?,?,'queued',?,?)",
                           (new_id("cx"), workspace_id, row["bot"],
                            canonical({"message_id": item_id, "outcome": outcome, "reason": reason,
                                       "reply": reply[-BOT_REPLY:] if isinstance(reply, str) else None}),
                            f"one:{item_id}", self.s.now(), self.s.now()))
                self.s.wake(workspace_id, "bots")
            elif outcome == "confirmed":
                # The turn he started ended: one line on his phone (alerts.py), opening that chat.
                alerts.queue(self.s, db, workspace_id, "answered", item_id, alerts.OWN_WHO[gw],
                             alerts.own_path(gw, row["session_id"] or made))
            # The files are in that Hermes's own folders now, or the message did not go: either way the core
            # keeps only their names.
            db.execute("UPDATE own_attachments SET body = NULL WHERE message_id = ?", (item_id,))
        return {"id": item_id, "status": state}
