"""#163: a Bot Chat turn outlives the connector that started it, and the connector after it says what came of it.

Before 15 Nov 2026 each turn was a child of the connector writing into a pipe the connector read, so an install
that started the connector again for a change to its own code ended every turn it ran. These run a real
connector process against the test core and a stand-in for Hermes's turn, stop that process the way systemd
stops it under KillMode=process (SIGTERM to the connector alone), and check that the turn runs on and that a
new connector follows it to its end and hands the core its result, once. The rest build a turn's folder as a
connector left it, for each way it can be found: ended while no connector ran, ended without saying how,
never handed to Hermes, its result not yet taken, and past its 15 minutes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from test_decisions_api import Served
from test_decisions_chat import flags, seat

from thornswood_decisions import connector
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds

SRC = Path(connector.__file__).resolve().parents[1]
# A process's start time is read from /proc: only there can a connector tell a running turn from a pid used again.
PROC = Path("/proc/self/stat").exists() and hasattr(os, "killpg")

# Hermes's turn, as room_send.py `chat` runs it: says it started, waits for the test to let it end, and logs each
# turn it ran. Anything else is the readiness check, which says ready.
FAKE_HELPER = textwrap.dedent('''
    import json, os, sys, time
    from pathlib import Path
    if sys.argv[1:2] != ["chat"]:
        sys.stdin.read()
        print(json.dumps({"ready": True}))
        sys.exit(0)
    args = sys.argv[1:]
    text = Path(args[args.index("--query-file") + 1]).read_text(encoding="utf-8")
    with Path(os.environ["TW_FAKE_LOG"]).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args, "text": text, "pid": os.getpid()}) + "\\n")
    print(json.dumps({"type": "system", "session_id": "s1"}), flush=True)
    print(json.dumps({"type": "thinking", "text": "hmm"}), flush=True)
    go = Path(os.environ["TW_FAKE_GO"])
    while not go.exists():
        time.sleep(0.05)
    print(json.dumps({"type": "text", "text": "done"}), flush=True)
    print(json.dumps({"type": "result", "text": "done"}), flush=True)
    sys.exit(int(go.read_text() or "0"))
''')

# The connector as its unit runs it, claiming once and then running on until it is stopped.
DRIVER = textwrap.dedent('''
    import sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    from thornswood_decisions import connector
    home, helper, url, token = Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
    hermes = connector.Hermes(["hermes"], [sys.executable, "-I", helper], home)
    c = connector.Connector(connector.Core(url, token), hermes, say=lambda line: print(line, flush=True))
    print("CLAIMED", c.round(), flush=True)
    while True:
        time.sleep(1)
''')


class Outlives(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        w = self.s.w
        seat(w)
        flags(w, "mobile_chat_write", "connector_effects")
        self.dir = Path(tempfile.mkdtemp())
        self.helper = self.dir / "helper.py"
        self.helper.write_text(FAKE_HELPER, encoding="utf-8")
        self.log, self.go = self.dir / "log.jsonl", self.dir / "go"
        old = {k: os.environ.get(k) for k in ("TW_FAKE_LOG", "TW_FAKE_GO")}

        def restore():
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        os.environ.update({"TW_FAKE_LOG": str(self.log), "TW_FAKE_GO": str(self.go)})
        self.addCleanup(self.go.write_text, "0")  # no stand-in turn waits on after the test
        self.token = Feeds(w.store).new_token(w.ws, "connector", by="tests")
        self.url = f"http://127.0.0.1:{self.s.port}/api/v1"
        self.said: list[str] = []

    def connector(self) -> connector.Connector:
        hermes = connector.Hermes(["hermes"], [sys.executable, "-I", str(self.helper)], self.dir)
        return connector.Connector(connector.Core(self.url, self.token), hermes, say=self.said.append)

    def send(self, target: str, text: str, key: str) -> str:
        return Chat(self.s.w.store).send(self.s.w.patrick(), target, text, key)["message_id"]

    def status(self, mid: str):
        return tuple(self.s.w.store.db.execute("SELECT status, reason FROM chat_messages WHERE id = ?", (mid,)).fetchone())

    def runs(self) -> list[dict]:
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def folders(self) -> list[Path]:
        turns = self.dir.joinpath(*connector.TURNS_DIR)
        return sorted(turns.iterdir()) if turns.is_dir() else []

    def wait_for(self, check, seconds: float = 20) -> None:
        ends = time.monotonic() + seconds
        while not check():
            if time.monotonic() > ends:
                self.fail(f"timed out; the connector said {self.said}")
            time.sleep(0.05)

    def first_connector_stops_mid_turn(self, mid: str) -> int:
        """A connector process claims the message, its turn starts, and the connector is stopped as systemd
        stops it (SIGTERM to it alone). Returns the turn's pid."""
        proc = subprocess.Popen([sys.executable, "-c", DRIVER, str(SRC), str(self.dir), str(self.helper), self.url,
                                 self.token], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertEqual(proc.stdout.readline().strip(), "CLAIMED 1")
        self.wait_for(lambda: self.runs())
        out = self.folders()[0] / connector.OUT
        self.wait_for(lambda: out.read_text(encoding="utf-8").count("\n") >= 2)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
        proc.stdout.close()
        self.assertEqual(self.status(mid)[0], "leased")
        return self.runs()[0]["pid"]

    @unittest.skipUnless(PROC, "a turn is picked up by its process's start time, read from /proc")
    def test_a_turn_runs_on_when_its_connector_stops_and_the_next_one_says_what_came_of_it(self):
        mid = self.send("sprocket", "please look at the launcher log", "key-00000301")
        pid = self.first_connector_stops_mid_turn(mid)
        self.assertIsNotNone(connector.proc_start(pid), "the turn ended with the connector that started it")
        # The new connector picks the turn up while it still runs, and follows it to its end.
        c = self.connector()
        done = threading.Thread(target=c.once)
        done.start()
        self.wait_for(lambda: c.lives)  # it shows the turn as it goes, from the start of its stream
        self.assertEqual(self.status(mid)[0], "leased")
        self.go.write_text("0")
        done.join(30)
        self.assertFalse(done.is_alive())
        self.assertEqual(self.status(mid), ("sent", None))
        self.assertEqual(len(self.runs()), 1, "the message went to the Bot a second time")
        self.assertIn("NOTE picked up 1 turn the connector before this one started", self.said)
        self.assertEqual(self.folders(), [], "the turn's folder is left behind once the core has its result")

    @unittest.skipUnless(PROC, "a turn is picked up by its process's start time, read from /proc")
    def test_a_turn_that_ended_while_no_connector_ran_is_read_to_its_end(self):
        mid = self.send("rift", "hi", "key-00000302")
        pid = self.first_connector_stops_mid_turn(mid)
        self.go.write_text("1")
        self.wait_for(lambda: connector.proc_start(pid) is None)
        self.wait_for(lambda: (self.folders()[0] / connector.EXIT).exists())
        self.connector().once()
        self.assertEqual(self.status(mid), ("uncertain", "hermes stopped with exit code 1; it may have the message"))
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.folders(), [])

    @unittest.skipUnless(PROC, "a turn is ended by its session's process group")
    def test_a_turn_past_its_15_minutes_is_ended_by_the_connector_that_picked_it_up(self):
        mid = self.send("echo", "hi", "key-00000303")
        pid = self.first_connector_stops_mid_turn(mid)
        folder = self.folders()[0]
        rec = json.loads((folder / connector.TURN).read_text(encoding="utf-8"))
        rec["started"] -= connector.TURN_SECONDS + 1
        (folder / connector.TURN).write_text(json.dumps(rec), encoding="utf-8")
        self.connector().once()
        self.assertEqual(self.status(mid),
                         ("uncertain", "no end to the Bot's turn in 15 minutes; it may have the message"))
        self.wait_for(lambda: connector.proc_start(pid) is None)

    def leased(self, target: str, text: str, key: str) -> str:
        """A message leased to a connector that then stopped, as the core holds it."""
        mid = self.send(target, text, key)
        status, body = connector.Core(self.url, self.token).post("/connector/claim", {"limit": 1})
        self.assertEqual((status, [i["id"] for i in body["items"]]), (200, [mid]))
        return mid

    def left(self, mid: str, target: str, **files) -> Path:
        """The folder a stopped connector left for that message, with `files` (name: text) in it."""
        folder = self.dir.joinpath(*connector.TURNS_DIR, f"m-{mid}")
        folder.mkdir(parents=True)
        (folder / connector.ITEM).write_text(json.dumps({"id": mid, "target": target}), encoding="utf-8")
        for name, text in files.items():
            (folder / {"turn": connector.TURN, "pid": connector.PID, "out": connector.OUT, "exit": connector.EXIT,
                       "result": connector.RESULT}[name]).write_text(text, encoding="utf-8")
        return folder

    def turn(self, target: str, **kw) -> str:
        return json.dumps({"target": target, "session": None, "started": time.time(), **kw})

    def test_a_turn_found_ended_is_read_from_its_files(self):
        mid = self.leased("wisp", "hi", "key-00000304")
        stream = "\n".join(json.dumps(e) for e in ({"type": "system"}, {"type": "text", "text": "ok"},
                                                   {"type": "result"})) + "\n"
        self.left(mid, "wisp", turn=self.turn("wisp"), pid=json.dumps({"pid": 1, "start": "never"}), out=stream,
                  exit="0")
        self.connector().once()
        self.assertEqual(self.status(mid), ("sent", None))
        self.assertEqual(self.folders(), [])

    def test_a_turn_whose_runner_ended_without_saying_may_have_the_message(self):
        mid = self.leased("magpie", "hi", "key-00000305")
        self.left(mid, "magpie", turn=self.turn("magpie"), pid=json.dumps({"pid": 1, "start": "never"}),
                  out=json.dumps({"type": "system"}) + "\n")
        self.connector().once()
        self.assertEqual(self.status(mid),
                         ("uncertain", "the turn stopped without saying how it ended; it may have the message"))

    def test_a_message_never_handed_to_hermes_goes_back_in_the_queue(self):
        mid = self.leased("bramble", "hi", "key-00000306")
        self.left(mid, "bramble")
        c = self.connector()
        c.adopt()
        c._report()
        self.assertEqual(self.status(mid)[0], "queued")
        self.assertEqual(c.waiting, {}, "a gateway was taken for not ready")
        self.assertEqual(self.runs(), [])
        # It goes now, once, as any queued message does.
        self.go.write_text("0")
        c.once()
        self.assertEqual(self.status(mid), ("sent", None))
        self.assertEqual(len(self.runs()), 1)

    def test_a_turn_that_may_have_started_is_never_sent_again(self):
        mid = self.leased("rift", "hi", "key-00000307")
        self.left(mid, "rift", turn=self.turn("rift"))
        self.connector().once()
        self.assertEqual(self.status(mid),
                         ("uncertain", "the connector stopped as the turn started; it may have the message"))
        self.assertEqual(self.runs(), [])

    def test_a_result_the_core_had_not_taken_is_offered_again(self):
        mid = self.leased("sprocket", "hi", "key-00000308")
        self.left(mid, "sprocket", result=json.dumps({"id": mid, "outcome": "confirmed", "ref": "turn finished",
                                                      "reason": ""}))
        self.connector().once()
        self.assertEqual(self.status(mid), ("sent", None))
        self.assertEqual(self.folders(), [])

    def test_a_folder_without_its_message_is_taken_away(self):
        turns = self.dir.joinpath(*connector.TURNS_DIR)
        (turns / "m-half").mkdir(parents=True)
        self.connector().once()
        self.assertEqual(self.folders(), [])

    def test_a_turn_keeps_its_state_in_its_own_folder_and_none_is_left(self):
        self.go.write_text("0")
        mid = self.send("echo", "the words", "key-00000309")
        c = self.connector()
        seen = []
        real = c.hermes.start_turn

        def start_turn(folder, *a):
            turn = real(folder, *a)
            seen.append(folder)
            if os.name == "posix":
                self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700)
                self.assertEqual(os.stat(folder / connector.MESSAGE).st_mode & 0o777, 0o600)
            return turn
        c.hermes.start_turn = start_turn
        c.once()
        self.assertEqual(self.status(mid), ("sent", None))
        self.assertEqual(seen, [self.dir.joinpath(*connector.TURNS_DIR, f"m-{mid}")])
        self.assertEqual(self.runs()[0]["text"], "the words")
        self.assertNotIn("the words", " ".join(self.runs()[0]["argv"]))
        self.assertEqual(self.folders(), [])


if __name__ == "__main__":
    unittest.main()
