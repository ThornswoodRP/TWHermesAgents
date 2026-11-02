"""The connector: carries Patrick's chat, and the "Bot told" messages, from the core into Hermes.

Runs on the VM as the Bots' `hermes` user (tw-decide-connector.service), with
the system Python and only this repository's code:

    python3 -I -m thornswood_decisions.connector --hermes /home/hermes/.local/bin/hermes

It does nothing while `connector_effects` is off. Otherwise, every few seconds
it leases from the core the oldest waiting message for each Bot and the room
that has nothing in flight (`POST /api/v1/connector/claim`, with its own token
in ~/.config/thornswood/connector.token, 0600) and hands each to Hermes the
way Hermes itself does:

- a Bot Chat: `hermes -p <bot> chat --in ~ -c "Bot Chat" --create-if-missing
  -Q --query-file <file>`, the local transport of Hermes's own message_agent
  (tools/bot_mode_dm.py:11), so the message lands in that Bot's one canonical
  Bot Chat, the same one Desktop shows. The text goes in a 0600 file in a 0700
  folder, never on a command line, and the file is gone when the turn ends.
  Before it, a readiness check finds the Bot's gateway without starting one.
- the room: `room_send.py` with Hermes's own Python, which calls `groups.send`
  on the running gateway with the core's key as the event id.

The helper (room_send.py: readiness, room, reactions) runs inside Hermes's own
runtime: its real Python, with Hermes's source on the path, found the way
own_connector.py finds it (`hermes --print-runtime-command`, else the launcher
followed by hand, never a shell). Since 30 Oct the Bots' `hermes` is a /bin/sh
wrapper, and taking its #! line for the Python ran the helper with /bin/sh:
every readiness check failed and read as "could not check the Bots' gateway"
until 2 Nov. A helper that does not start or does not answer in JSON is a
failure of the helper, said as one (a FAIL line in the journal, and the
message's reason, with the tail of what it wrote to stderr), never a gateway
that is not ready. Nothing was sent, so the message still goes back in the
queue as it was (an install that is replacing Hermes's Python is the same
case, 1 Nov), and the connector claims nothing more until the helper answers.

Every few seconds, whatever connector_effects says, it also answers the pages of
full history the phone asked for (`POST /api/v1/connector/pages`, then
`/connector/page`): a Bot's Bot Chat from that profile's state.db and a room
from shared-state.db, both read only, 50 messages at a time.
The same claim hands it Patrick's reactions on Bot Chat messages: each is set in
that Bot's own store with `room_send.py` op `react` (Hermes's SessionDB), and
what came of it goes to `POST /api/v1/connector/reacted`.
For each message, it tells the core what came of it (`POST /api/v1/connector/result`):
confirmed only when Hermes said so, failed only when nothing was sent, and
uncertain for everything else. An uncertain message is never sent again by
itself. A gateway that is not ready (it is starting, stopped, being reloaded by
an install, or its check did not answer in time) is not a failure of the message:
nothing was sent, so the outcome is not_ready and the core puts the message back
in the queue as it was. The connector then claims nothing more until that
gateway's readiness check says ready, once each round, and the message goes
then. A result the core did not take is kept and offered again until it
lands; that resends the record, never the message. Nothing here logs message
text or a token, holds a GitHub credential or listens on any port.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CORE = "http://127.0.0.1:9135/api/v1"
POLL_SECONDS = 5
TURN_SECONDS = 15 * 60
CHECK_SECONDS = 60
# At most this many `hermes` turns at once: each is a whole Hermes process, and
# the unit's MemoryMax holds them all.
PARALLEL = 3
BOT_CHAT = "Bot Chat"
PAGE_BYTES = 480 * 1024  # under the core's 512 KB
HELPER = Path(__file__).with_name("room_send.py")
HELPER_FAILED = "the connector's Hermes helper"  # how a helper failure's reason begins (chat.py HELPER_FAILED)
ROOM_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def bot_chat_id(hermes_home: Path, bot: str) -> str | None:
    """The id of a Bot's one canonical Bot Chat, from that profile's state.db, read only; None when it has none."""
    from .own_connector import _ro
    db = hermes_home / "profiles" / bot / "state.db"
    if not db.exists():
        return None
    con = _ro(db)
    try:
        s = con.execute("SELECT id FROM sessions WHERE title = ? AND archived = 0 ORDER BY started_at DESC LIMIT 1",
                        (BOT_CHAT,)).fetchone()
    finally:
        con.close()
    return s["id"] if s else None


def bot_page(hermes_home: Path, bot: str, before: int | None) -> dict:
    """One page of a Bot's one canonical Bot Chat, from that profile's state.db, read only."""
    from .own_connector import messages_page
    sid = bot_chat_id(hermes_home, bot)
    if sid is None:
        return {"messages": [], "more": False, "gone": True, "continued_as": None}
    db = hermes_home / "profiles" / bot / "state.db"
    n = 50
    while True:
        page = messages_page(db, sid, before, n)
        page["continued_as"] = None  # one Bot Chat per Bot: the phone names it by the Bot, not by its id
        if len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= PAGE_BYTES or n <= 1:
            return page
        n //= 2


def room_page(shared_db: Path, room_id: str, before: int | None) -> dict:
    from .feed_reader import room_page as read
    n = 50
    while True:
        page = read(shared_db, room_id, before, n)
        if len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= PAGE_BYTES or n <= 1:
            return page
        n //= 2


def read_token(path: Path) -> str:
    st = path.stat()
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise SystemExit(f"STOP: {path} is readable by others; it must be 0600")
    return path.read_text(encoding="utf-8").strip()


def hermes_python(hermes_bin: str) -> str:
    """The real Python the `hermes` command runs Hermes with (own_connector.hermes_runtime), never a shell."""
    from .own_connector import hermes_python as find
    return find(hermes_bin)


def helper_command(hermes_bin: str, python: str | None = None, root: str | None = None,
                   helper: Path = HELPER) -> list[str]:
    """How the helper runs inside Hermes's own runtime (own_connector.helper_command)."""
    from .own_connector import helper_command as find
    return find(hermes_bin, helper, python=python, root=root)


def _tail(data: bytes, n: int = 160) -> str:
    text = " ".join(data.decode("utf-8", "replace").split())
    return text[-n:] if text else "nothing"


class Hermes:
    """The two stock ways into the Bots' Hermes."""

    def __init__(self, hermes_cmd: list[str], helper_cmd: list[str] | str, home: Path, helper: Path = HELPER):
        # A Python's path alone (the tests' stand-ins) runs the helper file straight with it.
        self.cmd, self.home = list(hermes_cmd), home
        self.helper_cmd = [helper_cmd, "-I", str(helper)] if isinstance(helper_cmd, str) else list(helper_cmd)

    def _helper(self, req: dict, timeout: float) -> dict | None:
        """The helper's one JSON answer; None when it did not answer in time. A helper that could not start,
        or did not answer in JSON, answers {"helper_failed": True, "reason": ...} with its stderr's tail."""
        try:
            out = subprocess.run(self.helper_cmd, input=json.dumps(req).encode("utf-8"),
                                 capture_output=True, timeout=timeout, cwd=str(self.home), check=False)
        except subprocess.TimeoutExpired:
            return None
        except OSError as exc:
            return {"helper_failed": True, "started": False,
                    "reason": f"{HELPER_FAILED} did not start ({type(exc).__name__}: {self.helper_cmd[0]})"}
        try:
            got = json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            got = None
        if not isinstance(got, dict):
            return {"helper_failed": True, "started": True,
                    "reason": f"{HELPER_FAILED} failed (exit {out.returncode}): {_tail(out.stderr)}"}
        return got

    def check(self, profile: str) -> tuple[str, str]:
        """(state, why) of that profile's gateway, found without starting one: "ready", "not_ready" (the check
        ran and found it not running or not taking connections, or did not answer in time), or "failed"
        (the helper itself failed: nothing about the gateway is known)."""
        got = self._helper({"op": "ready", "profile": profile}, CHECK_SECONDS)
        if got is None:
            return "not_ready", "the gateway's readiness check did not answer in a minute"
        if got.get("helper_failed"):
            return "failed", str(got.get("reason"))[:200]
        if got.get("ready") is True:
            return "ready", ""
        return "not_ready", str(got.get("reason") or "the Bots' gateway is not ready")[:200]

    def ready(self, profile: str) -> tuple[bool, str]:
        """(ready, why not), from check."""
        state, why = self.check(profile)
        return state == "ready", why

    def to_room(self, room_id: str, send_key: str, text: str) -> tuple[str, str, str]:
        got = self._helper({"op": "send", "room_id": room_id, "event_id": send_key, "text": text}, CHECK_SECONDS)
        if got is None:
            return "uncertain", "", "no answer from the room helper in a minute"
        if got.get("helper_failed"):
            # Never started: nothing was sent, so it waits for the helper. Started and said nothing readable:
            # groups.send may have gone, and the room settles it from its own feed.
            return ("uncertain" if got.get("started") else "not_ready"), "", str(got.get("reason"))[:200]
        if got.get("sent") is True:
            return "confirmed", f"seq {got.get('seq')}", ""
        if got.get("sent") is False and got.get("ready") is False:
            return "not_ready", "", str(got.get("reason") or "the Bots' gateway is not ready")
        if got.get("sent") is False:
            return "failed", "", str(got.get("reason", "not sent"))
        return "uncertain", "", str(got.get("reason") or "the room helper stopped without saying")

    def react(self, bot: str, session_id: str, message_id: int, emoji: str | None) -> tuple[str, str]:
        """(outcome, reason): set Patrick's reaction on one message of a Bot's Bot Chat to exactly `emoji`
        (None takes it off), with that profile's own store."""
        got = self._helper({"op": "react", "profile": bot, "session_id": session_id, "message_id": message_id,
                            "emoji": emoji}, CHECK_SECONDS)
        if got is None:
            return "failed", "Hermes's store did not answer in a minute"
        if got.get("set") is True:
            return "set", ""
        return "failed", str(got.get("reason") or "not set")[:200]

    def to_bot(self, bot: str, text: str) -> tuple[str, str, str]:
        state, why = self.check(bot)
        if state != "ready":
            # Nothing was sent: the message waits, with why (the gateway, or the helper's own failure).
            return "not_ready", "", why
        with tempfile.TemporaryDirectory(prefix="tw-chat-") as folder:
            os.chmod(folder, 0o700)
            path = Path(folder) / "message.txt"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            argv = [*self.cmd, "-p", bot, "chat", "--in", str(self.home), "-c", BOT_CHAT, "--create-if-missing",
                    "-Q", "--query-file", str(path)]
            try:
                done = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      timeout=TURN_SECONDS, cwd=str(self.home), check=False)
            except subprocess.TimeoutExpired:
                return "uncertain", "", "no end to the Bot's turn in 15 minutes; it may have the message"
            except OSError:
                return "failed", "", "could not start the hermes command"
        if done.returncode == 0:
            return "confirmed", "turn finished", ""
        return "uncertain", "", f"hermes stopped with exit code {done.returncode}; it may have the message"


class Core:
    def __init__(self, base: str, token: str):
        self.base, self.token = base, token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.token}"})
        try:
            with self.opener.open(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                out = json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                out = {}
            exc.close()
            return exc.code, out
        except (urllib.error.URLError, OSError):
            return 0, {}


def say(line: str) -> None:
    """One line to the journal now: under systemd stdout is a pipe, and `-I` ignores PYTHONUNBUFFERED."""
    print(line, flush=True)


class Connector:
    def __init__(self, core: Core, hermes: Hermes, say=say, hermes_home: Path | None = None):
        self.core, self.hermes, self.say = core, hermes, say
        self.hermes_home = hermes_home or Path.home() / ".hermes"
        self.unsaid: list[dict] = []  # results the core has not taken yet
        self.waiting: dict[str, str] = {}  # gateways (by profile, "" for the room) found not ready: why
        self._lock = threading.Lock()

    def deliver(self, item: dict) -> dict:
        if item["target"] == "room":
            outcome, ref, reason = self.hermes.to_room(item["room_id"], item["send_key"], item["text"])
        else:
            outcome, ref, reason = self.hermes.to_bot(item["target"], item["text"])
        return {"id": item["id"], "outcome": outcome, "ref": ref, "reason": reason}

    def _report(self) -> None:
        keep = []
        for r in self.unsaid:
            if "version" in r:
                # A reaction: the core drops an answer about an older wish by itself.
                status, _ = self.core.post("/connector/reacted", r)
                if status in (200, 404):
                    self.say(f"{'PASS' if r['outcome'] == 'set' else 'NOTE'} reaction for {r['bot']}: {r['outcome']}")
                else:
                    keep.append(r)
                continue
            status, body = self.core.post("/connector/result", r)
            if status == 200 or (status == 409 and body.get("error") == "not_leased"):
                self.say(f"{'PASS' if r['outcome'] == 'confirmed' else 'NOTE'} {r['id']}: {r['outcome']}")
            else:
                keep.append(r)
        self.unsaid = keep

    def react(self, wanted: list) -> int:
        """The reactions the core handed over, each set in that Bot's Bot Chat one after another (each is one
        quick write). Returns how many were set; what came of each goes to the core with the report."""
        done = 0
        for r in wanted[:10]:
            if not (isinstance(r, dict) and isinstance(r.get("bot"), str) and PROFILE_ID.match(r["bot"])
                    and type(r.get("message_id")) is int and type(r.get("version")) is int
                    and (r.get("emoji") is None or isinstance(r.get("emoji"), str))):
                continue
            try:
                sid = bot_chat_id(self.hermes_home, r["bot"])
            except (sqlite3.Error, OSError) as exc:
                outcome, reason = "failed", f"could not read the Bot's store ({type(exc).__name__})"
            else:
                if sid is None:
                    outcome, reason = "failed", "that Bot has no Bot Chat"
                else:
                    outcome, reason = self.hermes.react(r["bot"], sid, r["message_id"], r.get("emoji"))
            done += outcome == "set"
            self.unsaid.append({"bot": r["bot"], "message_id": r["message_id"], "version": r["version"],
                                "outcome": outcome, "reason": reason})
        return done

    def _still_waiting(self) -> bool:
        """After a message came back not_ready: check each such gateway once this round. True while any is
        still not ready, so nothing more is claimed (and leased, and put back) until it is."""
        for profile in list(self.waiting):
            state, why = self.hermes.check(profile)
            if state == "ready":
                del self.waiting[profile]
                self.say(f"PASS {profile or 'the room'}'s gateway is ready; its messages go now")
            else:
                if state == "failed" and why != self.waiting[profile]:
                    self.say(f"FAIL {why}")
                self.waiting[profile] = why
        return bool(self.waiting)

    def once(self) -> int:
        """One round. Returns how many messages went to Hermes, or -1 when the core said to wait."""
        self._report()
        if self.waiting and self._still_waiting():
            return 0
        status, body = self.core.post("/connector/claim", {"limit": PARALLEL})
        if status == 423:
            return -1
        if status == 401:
            raise SystemExit("STOP: the core refused the connector token")
        if status != 200:
            return 0
        items = body.get("items", [])
        self.react(body.get("reactions", []))
        results: list[dict] = []

        def run(item):
            try:
                r = self.deliver(item)
            except Exception as exc:  # whatever it was, the message may have gone: say so, never resend
                r = {"id": item["id"], "outcome": "uncertain", "ref": "",
                     "reason": f"the connector hit {type(exc).__name__} while sending"}
            with self._lock:
                results.append(r)

        threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in items]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        gateway = {i["id"]: "" if i["target"] == "room" else i["target"] for i in items}
        for r in results:
            helper = r["reason"].startswith(HELPER_FAILED)
            if helper and r["outcome"] != "not_ready":
                self.say(f"FAIL {r['id']}: {r['reason']}")
            if r["outcome"] == "not_ready" and r["id"] in gateway:
                if gateway[r["id"]] not in self.waiting:
                    self.say(f"FAIL {r['reason']}; {gateway[r['id']] or 'the room'}'s messages wait in the queue"
                             if helper else
                             f"NOTE {gateway[r['id']] or 'the room'}'s gateway is not ready ({r['reason']}); "
                             "its messages wait in the queue")
                self.waiting[gateway[r["id"]]] = r["reason"]
        self.unsaid.extend(results)
        self._report()
        return len(items)

    def pages(self) -> int:
        """The pages the phone asked for: each Bot Chat or room read from Hermes's own stores, read only, and
        posted. Reading only, so it runs whatever connector_effects says. Returns how many were posted."""
        status, body = self.core.post("/connector/pages", {})
        if status != 200:
            return 0
        sent = 0
        for req in body.get("pages", [])[:3]:
            if not isinstance(req, dict) or not isinstance(req.get("id"), str):
                continue
            before = req.get("before") if isinstance(req.get("before"), int) else None
            target = req.get("chat")
            try:
                if req.get("kind") == "bot" and isinstance(target, str) and PROFILE_ID.match(target):
                    page = bot_page(self.hermes_home, target, before)
                elif req.get("kind") == "room" and isinstance(target, str) and ROOM_ID.match(target):
                    page = room_page(self.hermes_home / "shared-state.db", target, before)
                else:
                    continue
            except (sqlite3.Error, OSError) as exc:
                self.say(f"FAIL page: could not read Hermes's store ({type(exc).__name__})")
                continue
            status, _ = self.core.post("/connector/page", {"id": req["id"], "target": target, "page": page})
            if status == 200:
                sent += 1
            elif status not in (409, 423):
                self.say(f"FAIL page: the core answered {status}")
        return sent

    def forever(self) -> None:
        # Pages every few seconds, whatever connector_effects says; the claim every few seconds, or every
        # half minute while the core says connector_effects is off.
        claim_at = 0.0
        while True:
            self.pages()
            if time.monotonic() >= claim_at:
                n = self.once()
                claim_at = time.monotonic() + (POLL_SECONDS * 6 if n < 0 else 0)
            time.sleep(POLL_SECONDS)


def main(argv: list[str]) -> int:
    home = Path(os.environ.get("HOME", "~")).expanduser()
    ap = argparse.ArgumentParser(prog="thornswood_decisions.connector")
    ap.add_argument("--hermes", required=True, help="the hermes command the Bots' user runs")
    ap.add_argument("--hermes-python", help="Hermes's own Python, in place of asking the hermes command")
    ap.add_argument("--hermes-root", help="with --hermes-python: Hermes's source folder, when it is not installed "
                                          "into that Python")
    ap.add_argument("--check-ready", metavar="BOT",
                    help="run the helper's readiness check for one Bot, print its answer and stop: exit 0 only "
                         "when it says ready (tw-canary)")
    ap.add_argument("--token", default=str(home / ".config" / "thornswood" / "connector.token"))
    ap.add_argument("--core", default=CORE)
    ap.add_argument("--once", action="store_true", help="one round, then stop")
    args = ap.parse_args(argv)
    hermes = Hermes([args.hermes], helper_command(args.hermes, args.hermes_python, args.hermes_root), home)
    if args.check_ready is not None:
        if not PROFILE_ID.match(args.check_ready):
            raise SystemExit("STOP: --check-ready takes a Bot's profile name")
        got = hermes._helper({"op": "ready", "profile": args.check_ready}, CHECK_SECONDS)
        print(json.dumps(got if got is not None else {"ready": False, "reason": "no answer in a minute"}))
        return 0 if got and got.get("ready") is True else 1
    conn = Connector(Core(args.core, read_token(Path(args.token))), hermes, hermes_home=home / ".hermes")
    if args.once:
        n = conn.once()
        print("WAIT connector_effects is off" if n < 0 else f"PASS {n} handed to Hermes")
        return 1 if conn.unsaid else 0
    conn.forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
