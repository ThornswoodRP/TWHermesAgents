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

Then it tells the core what came of it (`POST /api/v1/connector/result`):
confirmed only when Hermes said so, failed only when nothing was sent, and
uncertain for everything else. An uncertain message is never sent again by
itself. A result the core did not take is kept and offered again until it
lands; that resends the record, never the message. Nothing here logs message
text or a token, holds a GitHub credential or listens on any port.
"""

from __future__ import annotations

import argparse
import json
import os
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


def read_token(path: Path) -> str:
    st = path.stat()
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise SystemExit(f"STOP: {path} is readable by others; it must be 0600")
    return path.read_text(encoding="utf-8").strip()


def hermes_python(hermes_bin: str) -> str:
    """The interpreter the `hermes` command runs with, read from its first line."""
    first = Path(hermes_bin).read_bytes()[:400].split(b"\n", 1)[0].decode("utf-8", "replace")
    if not first.startswith("#!"):
        raise SystemExit(f"STOP: {hermes_bin} does not start with #!; pass --hermes-python")
    exe = first[2:].strip().split()[0]
    if not exe.startswith("/") or exe.rsplit("/", 1)[-1] == "env":
        raise SystemExit(f"STOP: {hermes_bin} does not name its Python by path; pass --hermes-python")
    return exe


class Hermes:
    """The two stock ways into the Bots' Hermes."""

    def __init__(self, hermes_cmd: list[str], python: str, home: Path,
                 helper: Path = Path(__file__).with_name("room_send.py")):
        self.cmd, self.python, self.home, self.helper = list(hermes_cmd), python, home, helper

    def _helper(self, req: dict, timeout: float) -> dict | None:
        try:
            out = subprocess.run([self.python, "-I", str(self.helper)], input=json.dumps(req).encode("utf-8"),
                                 capture_output=True, timeout=timeout, cwd=str(self.home), check=False)
        except subprocess.TimeoutExpired:
            return None
        except OSError:
            return {"sent": False, "ready": False, "reason": "could not start Hermes's Python"}
        try:
            line = out.stdout.decode("utf-8", "replace").strip().splitlines()[-1]
            return json.loads(line)
        except (IndexError, json.JSONDecodeError):
            return {"bad": out.returncode}

    def to_room(self, room_id: str, send_key: str, text: str) -> tuple[str, str, str]:
        got = self._helper({"op": "send", "room_id": room_id, "event_id": send_key, "text": text}, CHECK_SECONDS)
        if got is None:
            return "uncertain", "", "no answer from the room helper in a minute"
        if got.get("sent") is True:
            return "confirmed", f"seq {got.get('seq')}", ""
        if got.get("sent") is False:
            return "failed", "", str(got.get("reason", "not sent"))
        return "uncertain", "", str(got.get("reason") or "the room helper stopped without saying")

    def to_bot(self, bot: str, text: str) -> tuple[str, str, str]:
        ready = self._helper({"op": "ready", "profile": bot}, CHECK_SECONDS)
        if not ready or ready.get("ready") is not True:
            return "failed", "", str((ready or {}).get("reason") or "could not check the Bots' gateway")
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
    def __init__(self, core: Core, hermes: Hermes, say=say):
        self.core, self.hermes, self.say = core, hermes, say
        self.unsaid: list[dict] = []  # results the core has not taken yet
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
            status, body = self.core.post("/connector/result", r)
            if status == 200 or (status == 409 and body.get("error") == "not_leased"):
                self.say(f"{'PASS' if r['outcome'] == 'confirmed' else 'NOTE'} {r['id']}: {r['outcome']}")
            else:
                keep.append(r)
        self.unsaid = keep

    def once(self) -> int:
        """One round. Returns how many messages went to Hermes, or -1 when the core said to wait."""
        self._report()
        status, body = self.core.post("/connector/claim", {"limit": PARALLEL})
        if status == 423:
            return -1
        if status == 401:
            raise SystemExit("STOP: the core refused the connector token")
        if status != 200:
            return 0
        items = body.get("items", [])
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
        self.unsaid.extend(results)
        self._report()
        return len(items)

    def forever(self) -> None:
        while True:
            n = self.once()
            time.sleep(POLL_SECONDS * (6 if n < 0 else 1))


def main(argv: list[str]) -> int:
    home = Path(os.environ.get("HOME", "~")).expanduser()
    ap = argparse.ArgumentParser(prog="thornswood_decisions.connector")
    ap.add_argument("--hermes", required=True, help="the hermes command the Bots' user runs")
    ap.add_argument("--hermes-python", help="its Python, when its first line does not name it")
    ap.add_argument("--token", default=str(home / ".config" / "thornswood" / "connector.token"))
    ap.add_argument("--core", default=CORE)
    ap.add_argument("--once", action="store_true", help="one round, then stop")
    args = ap.parse_args(argv)
    hermes = Hermes([args.hermes], args.hermes_python or hermes_python(args.hermes), home)
    conn = Connector(Core(args.core, read_token(Path(args.token))), hermes)
    if args.once:
        n = conn.once()
        print("WAIT connector_effects is off" if n < 0 else f"PASS {n} handed to Hermes")
        return 1 if conn.unsaid else 0
    conn.forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
