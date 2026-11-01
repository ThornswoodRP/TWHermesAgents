"""Carries Patrick's chat from the phone into his own Hermes: my-hermes on the VM, or his Hermes on One.

One file, standard library only, so the same copy runs on the VM and on One
(Windows). It imports nothing from this repository. It runs as that Hermes's
own user and reaches out to the decision core; nothing listens on any port.

    python3 -I own_connector.py --gateway me  --hermes /home/patrick-hermes/.local/bin/hermes
    py -3 -I own_connector.py   --gateway one --hermes C:\\...\\hermes.exe --core https://hermes.<tailnet>.ts.net/api/v1

Every few seconds it:

1. checks that Hermes's gateway is running, without starting one (room_send.py's
   readiness check, with Hermes's own Python), and tells the core whether it is
   while it leases the oldest waiting message of each chat that has nothing in
   flight (`POST /own/<gateway>/claim`, with its token in
   ~/.config/thornswood/own-<gateway>.token on the VM, or
   %APPDATA%\\Thornswood\\own-one.token on One);
2. runs each message as a turn in that Hermes, the way Hermes runs a turn with
   no one at the keyboard (contracts.json "own_chat"):

       a new chat:      hermes chat -Q --format stream-json --query-file <file> --yolo --source cli --in <home>
       an existing one: hermes chat -Q --format stream-json --query-file <file> --resume <session id>

   A new chat is made with `--yolo`, Patrick's "full power behind the passkey"
   (the core takes a message only from a passkey sign-in of the last 30
   minutes), and listed like any chat he starts (`--source cli`); Hermes names
   it as it names any chat, and the turn's last line says its id. Hermes keeps
   a chat's launch settings for good and refuses a resume that would change
   them (gateway_chat.py check_resume_policy), so a chat started elsewhere runs
   the phone's turn with its own approvals: a step that needs an approval is
   refused, unless that chat has /yolo on. Nothing here turns /yolo on. The
   text goes in a file only this user can read, never on a command line, and
   the file is gone when the turn ends;
3. tells the core what came of it (`POST /own/<gateway>/result`): confirmed
   only when Hermes finished the turn and said so, failed only when nothing
   was sent, and uncertain for everything else. An uncertain message is never
   sent again by itself;
4. every 20 seconds, and after each turn, sends the core the chat list, the
   way Hermes's own pickers list it, with the last messages of the five most
   recent chats and of any the phone asked for (`POST /own/<gateway>/feed`).

While own_chat_write is off the core answers 423 to all of it; this waits and
copies nothing. It logs no message text, no chat title and no token.
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
from datetime import datetime, timezone
from pathlib import Path

LOCAL_CORE = "http://127.0.0.1:9135/api/v1"
GATEWAYS = {"me": "my-hermes", "one": "One's Hermes"}
POLL_SECONDS = 5
FEED_SECONDS = 20
READY_SECONDS = 60       # how often a running gateway is checked again
NOT_READY_SECONDS = 15   # and one that was not running
TURN_SECONDS = 15 * 60
PARALLEL = 3
CHATS = 50
OPEN_RECENT = 5
MESSAGES = 30
TEXT = 2000
FEED_BYTES = 480 * 1024  # under the core's 512 KB
# Sessions Hermes's own pickers leave out (hermes_state_sessions.py INTERNAL_LISTING_SOURCES), and sub-agent runs.
NOT_LISTED = ("kanban", "tool", "oneshot", "subagent")
WINDOWS = os.name == "nt"
# The core's own rule for a chat id (own.py), checked again before it goes after --resume.
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def say(line: str) -> None:
    """One line to the journal now. Under pythonw (One's task, with no window) there is no stdout."""
    if sys.stdout is not None:
        print(line, flush=True)


def read_token(path: Path) -> str:
    if not WINDOWS and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SystemExit(f"STOP: {path} is readable by others; it must be 0600")
    return path.read_text(encoding="utf-8").strip()


def hermes_home() -> Path:
    """Where Hermes keeps its state for this user (hermes_constants.py get_default_hermes_root)."""
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"])
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "hermes"
    return Path.home() / ".hermes"


# The entry that ends the command Hermes's launcher prints for --print-runtime-command (pinned
# Hermes, hermes_cli/_launchers.py runtime_command and print_runtime_command; contracts.json own_chat).
RUNTIME_ENTRY = "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
HELPER = Path(__file__).with_name("room_send.py")


def helper_command(hermes_bin: str, helper: Path = HELPER, python: str | None = None) -> list[str]:
    """How to run the readiness helper inside this Hermes's own Python, with its packages.

    Hermes's launcher says how it starts its runtime (`hermes --print-runtime-command`): on One
    that is a Python kept in Hermes's own store, started with a bootstrap that puts Hermes on its
    path, and nothing lies beside hermes.exe. The helper runs in that same bootstrap, with the
    helper's file in place of Hermes's own entry. A launcher too old to answer is a plain script
    or a venv's hermes.exe, whose interpreter the command names (hermes_python)."""
    if python:
        return [python, "-I", str(helper)]
    try:
        out = subprocess.run([hermes_bin, "--print-runtime-command"], capture_output=True, timeout=60,
                             check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"STOP: {hermes_bin} --print-runtime-command did not run ({type(exc).__name__})")
    if out.returncode != 0:
        return [hermes_python(hermes_bin), "-I", str(helper)]
    try:
        cmd = json.loads(out.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        cmd = None
    if (not isinstance(cmd, list) or len(cmd) != 4 or not all(isinstance(c, str) for c in cmd)
            or cmd[1:3] != ["-I", "-c"] or not cmd[3].endswith(RUNTIME_ENTRY)
            or not os.path.isabs(cmd[0]) or not os.path.isfile(cmd[0])):
        raise SystemExit(f"STOP: {hermes_bin} --print-runtime-command answered a command this connector does "
                         "not know; check contracts.json own_chat against this Hermes")
    run_helper = f"runpy.run_path({json.dumps(str(helper))}, run_name='__main__')"
    return [cmd[0], "-I", "-c", cmd[3][:-len(RUNTIME_ENTRY)] + run_helper]


def hermes_python(hermes_bin: str) -> str:
    """The interpreter an older `hermes` command runs with: python.exe beside a venv's hermes.exe, or its #! line."""
    path = Path(hermes_bin)
    if path.suffix.lower() == ".exe":
        py = path.with_name("python.exe")
        if not py.exists():
            raise SystemExit(f"STOP: no python.exe beside {hermes_bin}; pass --hermes-python")
        return str(py)
    first = path.read_bytes()[:400].split(b"\n", 1)[0].decode("utf-8", "replace")
    if not first.startswith("#!"):
        raise SystemExit(f"STOP: {hermes_bin} does not start with #!; pass --hermes-python")
    exe = first[2:].strip().split()[0]
    if not exe.startswith("/") or exe.rsplit("/", 1)[-1] == "env":
        raise SystemExit(f"STOP: {hermes_bin} does not name its Python by path; pass --hermes-python")
    return exe


def _iso(epoch) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


def _text(content) -> str:
    """A message's content as plain text: a string, or the text parts of a list of parts."""
    if isinstance(content, str):
        try:
            parts = json.loads(content) if content.startswith("[") else None
        except json.JSONDecodeError:
            parts = None
        if not isinstance(parts, list):
            return content
        content = parts
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


# ── reading Hermes's chats ────────────────────────────────────────────────

LIST_SQL = f"""
SELECT s.id, s.title, coalesce(s.last_activity_at, s.started_at) AS last_at FROM sessions s
WHERE s.archived = 0 AND s.hidden = 0
  AND s.source NOT IN ({",".join("?" * len(NOT_LISTED))})
  AND coalesce(s.end_reason, '') != 'compression'
  AND (s.model_config IS NULL OR NOT json_valid(s.model_config)
       OR (json_extract(s.model_config, '$._delegate_from') IS NULL))
ORDER BY last_at DESC LIMIT ?
"""
# A compressed chat goes on in a new session whose parent ended with 'compression'; only the newest
# is listed, as Hermes's pickers show one row for it (hermes_state_sessions.py _expand_compression_lineage_ids).
MESSAGES_SQL = ("SELECT role, content, timestamp FROM messages WHERE session_id = ? AND active = 1 "
                "AND role IN ('user', 'assistant') AND content IS NOT NULL AND content != '' "
                "ORDER BY id DESC LIMIT ?")


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def snapshot(state_db: Path, wanted: list[str], now: datetime, messages: int = MESSAGES) -> dict:
    """The chat list and the open chats' last messages, from Hermes's state.db, read only."""
    con = _ro(state_db)
    try:
        rows = con.execute(LIST_SQL, (*NOT_LISTED, CHATS)).fetchall()
        chats, previews = [], {}
        for r in rows:
            last = con.execute(MESSAGES_SQL, (r["id"], 1)).fetchone()
            text = _text(last["content"]) if last else ""
            chats.append({"id": r["id"], "title": r["title"] or "", "last_at": _iso(r["last_at"]),
                          "preview": text[:200]})
            previews[r["id"]] = text
        open_ids = [c["id"] for c in chats[:OPEN_RECENT]]
        open_ids += [w for w in wanted if w not in open_ids]
        opened = []
        for sid in open_ids:
            msgs = con.execute(MESSAGES_SQL, (sid, messages)).fetchall()
            opened.append({"id": sid, "messages": [{"role": m["role"], "text": _text(m["content"])[:TEXT],
                                                    "at": _iso(m["timestamp"])} for m in reversed(msgs)]})
    finally:
        con.close()
    return {"source_time": now.isoformat(), "chats": chats, "open": opened}


def fitted(state_db: Path, wanted: list[str], now: datetime) -> bytes:
    """The feed as the core takes it: under its size, with fewer messages in each open chat if need be."""
    n = MESSAGES
    while True:
        body = json.dumps(snapshot(state_db, wanted, now, n), ensure_ascii=False).encode("utf-8")
        if len(body) <= FEED_BYTES or n <= 1:
            return body
        n //= 2


# ── running a turn ────────────────────────────────────────────────────────

class Hermes:
    def __init__(self, hermes_cmd: list[str], ready_cmd: list[str], home_dir: Path, name: str):
        self.cmd, self.ready_cmd, self.home_dir, self.name = list(hermes_cmd), list(ready_cmd), home_dir, name

    def ready(self) -> tuple[bool, str]:
        """Is this Hermes's gateway running? Asked without starting one."""
        req = json.dumps({"op": "ready", "profile": "", "name": self.name}).encode("utf-8")
        try:
            out = subprocess.run(self.ready_cmd, input=req, capture_output=True,
                                 timeout=60, cwd=str(self.home_dir), check=False)
            got = json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            return False, "the readiness check did not answer in a minute"
        except (OSError, IndexError, json.JSONDecodeError):
            return False, "could not run the readiness check with Hermes's Python"
        if got.get("ready") is True:
            return True, ""
        return False, str(got.get("reason") or f"{self.name} is not ready")[:200]

    def turn(self, session_id: str | None, text: str) -> tuple[str, str, str, str | None]:
        """(outcome, ref, reason, session id) for one message."""
        if session_id is not None and not SESSION_ID.match(session_id):
            return "failed", "", "that is not a chat id Hermes gives", None
        with tempfile.TemporaryDirectory(prefix="tw-own-") as folder:
            if not WINDOWS:
                os.chmod(folder, 0o700)
            path = Path(folder) / "message.txt"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            argv = [*self.cmd, "chat", "-Q", "--format", "stream-json", "--query-file", str(path)]
            if session_id:
                argv += ["--resume", session_id]
            else:
                argv += ["--yolo", "--source", "cli", "--in", str(self.home_dir)]
            return self._run(argv, session_id)

    def _run(self, argv: list[str], session_id: str | None) -> tuple[str, str, str, str | None]:
        seen: dict = {}

        def read(stream):
            for line in stream:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict) and ev.get("type") in ("system", "result"):
                    seen[ev["type"]] = ev  # only the session id and the end are kept, never the text
                    seen[ev["type"]].pop("text", None)

        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, cwd=str(self.home_dir), text=True,
                                    encoding="utf-8", errors="replace")
        except OSError:
            return "failed", "", "could not start the hermes command", None
        reader = threading.Thread(target=read, args=(proc.stdout,), daemon=True)
        reader.start()
        try:
            code = proc.wait(timeout=TURN_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            reader.join(5)
            proc.stdout.close()
            return "uncertain", "", "no end to the turn in 15 minutes; Hermes may have the message", \
                self._sid(seen, session_id)
        reader.join(30)
        proc.stdout.close()
        result = seen.get("result")
        sid = self._sid(seen, session_id)
        if code == 0 and result and result.get("exit_code") == 0:
            return "confirmed", "turn finished", "", sid
        why = (result or {}).get("error") or f"hermes stopped with exit code {code}"
        return "uncertain", "", f"{str(why)[:150]}; Hermes may have the message", sid

    @staticmethod
    def _sid(seen: dict, session_id: str | None) -> str | None:
        for kind in ("result", "system"):
            sid = (seen.get(kind) or {}).get("session_id")
            if isinstance(sid, str) and sid:
                return sid
        return session_id


# ── the core ──────────────────────────────────────────────────────────────

class Core:
    def __init__(self, base: str, gateway: str, token: str):
        self.base, self.gateway, self.token = base.rstrip("/"), gateway, token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, op: str, body: dict | bytes) -> tuple[int, dict]:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{self.base}/own/{self.gateway}/{op}", data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.token}"})
        try:
            with self.opener.open(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                out = json.loads(exc.read() or b"{}")
            except json.JSONDecodeError:
                out = {}
            exc.close()
            return exc.code, out
        except (urllib.error.URLError, OSError, ValueError):
            return 0, {}


class OwnConnector:
    def __init__(self, core: Core, hermes: Hermes, state_db: Path, say=say, clock=time.monotonic):
        self.core, self.hermes, self.state_db, self.say, self.clock = core, hermes, state_db, say, clock
        self.unsaid: list[dict] = []
        self.running: list[threading.Thread] = []
        self.wanted: list[str] = []
        self._lock = threading.Lock()
        self._ready = (False, "not checked yet")
        self._ready_at = None
        self._fed_at = None
        self.feed_now = True
        self.last_claim = None

    def ready(self) -> tuple[bool, str]:
        now = self.clock()
        every = READY_SECONDS if self._ready[0] else NOT_READY_SECONDS
        if self._ready_at is None or now - self._ready_at >= every:
            self._ready, self._ready_at = self.hermes.ready(), now
        return self._ready

    def _report(self) -> None:
        with self._lock:
            pending, self.unsaid = self.unsaid, []
        keep = []
        for r in pending:
            status, body = self.core.post("result", r)
            if status == 200 or (status == 409 and body.get("error") == "not_leased"):
                self.say(f"{'PASS' if r['outcome'] == 'confirmed' else 'NOTE'} {r['id']}: {r['outcome']}")
            else:
                keep.append(r)
        with self._lock:
            self.unsaid = keep + self.unsaid

    def _deliver(self, item: dict) -> None:
        try:
            outcome, ref, reason, sid = self.hermes.turn(item.get("session_id"), item["text"])
        except Exception as exc:  # whatever it was, the message may have gone: say so, never resend
            outcome, ref, reason, sid = ("uncertain", "", f"the connector hit {type(exc).__name__} during the turn",
                                         item.get("session_id"))
        r = {"id": item["id"], "outcome": outcome, "ref": ref, "reason": reason}
        if not item.get("session_id") and sid:
            r["session_id"] = sid
        with self._lock:
            self.unsaid.append(r)
            self.feed_now = True

    def feed(self) -> int:
        try:
            body = fitted(self.state_db, self.wanted, datetime.now(timezone.utc))
        except (sqlite3.Error, OSError) as exc:
            self.say(f"FAIL feed: could not read Hermes's chats ({type(exc).__name__})")
            return 0
        status, out = self.core.post("feed", body)
        if status == 200:
            self.wanted = [w for w in out.get("wanted", []) if isinstance(w, str)]
        elif status not in (423,):
            self.say(f"FAIL feed: the core answered {status}")
        return status

    def once(self) -> int:
        """One round. Returns how many turns it started, or -1 when the core said to wait."""
        self._report()
        self.running = [t for t in self.running if t.is_alive()]
        ready, why = self.ready()
        free = PARALLEL - len(self.running)
        status, body = self.core.post("claim", {"limit": free if ready else 0, "ready": ready, "why": why})
        self.last_claim = status
        if status == 401:
            raise SystemExit("STOP: the core refused this connector's token")
        if status == 423:
            return -1
        if status != 200:
            return 0
        wanted = [w for w in body.get("wanted", []) if isinstance(w, str)]
        if wanted != self.wanted:
            self.wanted, self.feed_now = wanted, True
        items = body.get("items", [])
        for item in items:
            t = threading.Thread(target=self._deliver, args=(item,), daemon=True)
            t.start()
            self.running.append(t)
        now = self.clock()
        if self.feed_now or self._fed_at is None or now - self._fed_at >= FEED_SECONDS:
            self.feed_now = False
            self._fed_at = now
            self.feed()
        return len(items)

    def forever(self) -> None:
        while True:
            n = self.once()
            time.sleep(POLL_SECONDS * (6 if n < 0 else 1))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="own_connector")
    ap.add_argument("--gateway", required=True, choices=sorted(GATEWAYS))
    ap.add_argument("--hermes", required=True, help="the hermes command this user runs")
    ap.add_argument("--hermes-python", help="a Python with Hermes's packages, in place of asking the hermes command")
    ap.add_argument("--hermes-home", help="Hermes's state folder for this user (default: as Hermes finds it)")
    ap.add_argument("--token", help="this connector's token file")
    ap.add_argument("--core", default=LOCAL_CORE, help="the core's API: loopback on the VM, the VM's ts.net on One")
    ap.add_argument("--once", action="store_true", help="one round, then stop")
    args = ap.parse_args(argv)
    if not (args.core.startswith("https://") or args.core.startswith("http://127.0.0.1:")):
        raise SystemExit("STOP: the core is reached over https, or on 127.0.0.1 on the VM itself")
    if args.token:
        token = Path(args.token)
    elif WINDOWS:
        token = Path(os.environ["APPDATA"]) / "Thornswood" / f"own-{args.gateway}.token"
    else:
        token = Path.home() / ".config" / "thornswood" / f"own-{args.gateway}.token"
    home = Path(args.hermes_home) if args.hermes_home else hermes_home()
    hermes = Hermes([args.hermes], helper_command(args.hermes, python=args.hermes_python), Path.home(),
                    GATEWAYS[args.gateway])
    conn = OwnConnector(Core(args.core, args.gateway, read_token(token)), hermes, home / "state.db")
    if args.once:
        n = conn.once()
        for t in conn.running:
            t.join()
        conn._report()
        if conn.last_claim not in (200, 423):
            print(f"FAIL the core at {args.core} answered {conn.last_claim or 'nothing'}")
            return 1
        ready, why = conn._ready
        print(("WAIT own_chat_write is off" if n < 0 else f"PASS {n} handed to {GATEWAYS[args.gateway]}")
              + ("" if ready else f" (its gateway is not ready: {why})"))
        return 1 if conn.unsaid else 0
    conn.forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
