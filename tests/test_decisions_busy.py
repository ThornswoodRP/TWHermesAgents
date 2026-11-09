"""A phone message into a chat whose turn is already running follows that chat's busy-input setting, like
Desktop (Patrick's answer A on q-one-busy-chat, 2 Nov), and a message in Hermes's queue says "In queue"
("it should say in queue like it does on Desktop").

Checks:
- room_send.py's turn (`room_send.py chat ...`) runs Hermes's own `hermes chat` with the hand-over changed
  only for a chat with a running turn: steer goes to session.steer, interrupt to session.redirect, queue to
  prompt.submit; a file, a slash command or a turn parked on a prompt always queues; a "rejected" steer is
  queued; a steered message follows the running turn to its end; and it says how Hermes took it, and again
  when a queued one starts;
- the connector reads those lines into the live line's states ("queued", "steered", "redirected") and
  says once Hermes has the message, and runs every turn through room_send.py, which One's connector
  already updates itself with;
- the core hands over a chat's next message only once each before it is in Hermes, never a new chat's
  second, and keeps the new states;
- the app says "In queue", "Steered into the running turn" and "Interrupted the running turn" under the
  message, and draws the live line only for the turn being answered.
Everything runs with a stand-in Hermes in a scratch folder; nothing reaches a real Hermes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_own import SID, Gateways, passkey

from thornswood_decisions import own_connector
from thornswood_decisions.own import CONNECTORS, DOING_STATES

HERE = Path(own_connector.__file__).resolve().parent
ROOM_SEND = HERE / "room_send.py"
APP = (HERE / "app" / "app.js").read_text(encoding="utf-8")

# A stand-in for the parts of Hermes the turn uses, shaped like hermes_cli/gateway_chat_view.py (its
# constructor, pending, prompts, generation, finite, submit through prompt.submit, _session_info) and
# hermes_cli/main.py run as __main__ (a one-shot: resume, submit, wait for the admission, print a result).
VIEW = textwrap.dedent('''
    class GatewayChatView:
        def __init__(self, client, snapshot, *, quiet=False, emitter=None, usage_file=None):
            self.client = client
            self.session_id = snapshot["stored_session_id"]
            self.generation = snapshot.get("execution_generation", 0)
            self.prompts = {p["prompt_id"]: p for p in snapshot.get("prompts", [])}
            self.pending = snapshot.get("pending", [])
            self.finite = False

        async def submit(self, text):
            return await self.client.rpc("prompt.submit", session_id=self.session_id, input_id="x", text=text)

        def _session_info(self, admission, payload):
            self.pending = payload.get("pending", self.pending)
            self.generation = payload.get("execution_generation", self.generation)
''')
MAIN = textwrap.dedent('''
    import asyncio, json, os, sys
    from hermes_cli import gateway_chat_view

    S = json.loads(os.environ["TW_STUB"])

    class Client:
        async def rpc(self, method, **p):
            with open(os.environ["TW_CALLS"], "a", encoding="utf-8") as f:
                f.write(json.dumps([method, {k: v for k, v in p.items() if k != "input_id"}]) + "\\n")
            if method == "config.get":
                return {"key": "busy", "value": S["busy"]}
            if method in ("session.steer", "session.redirect"):
                return {"status": S.get("steer", "queued")}
            if method == "prompt.submit":
                return {"admission_id": "a2", "status": "queued" if S["running"] else "started"}
            raise SystemExit("unexpected " + method)

    def main():
        args = sys.argv[1:]
        assert args[0] == "chat", args
        text = open(args[args.index("--query-file") + 1], encoding="utf-8").read()
        snapshot = {"stored_session_id": "s1", "execution_generation": 7,
                    "prompts": [{"prompt_id": "p1"}] if S.get("parked") else [],
                    "pending": [{"admission_id": "a1", "status": "started"}] if S["running"] else []}
        view = gateway_chat_view.GatewayChatView(Client(), snapshot, quiet=True)
        view.finite = True
        receipt = asyncio.run(view.submit(text))
        admission = receipt["admission_id"]
        if receipt.get("status") == "queued":
            view._session_info(None, {"pending": [{"admission_id": "a2", "status": "started"}]})
        print(json.dumps({"type": "text", "text": "answer"}))
        print(json.dumps({"type": "result", "session_id": "s1", "exit_code": 0, "admission": admission}))
        return 0

    if __name__ == "__main__":
        sys.exit(main())
''')


def stub_hermes(folder: Path) -> Path:
    (folder / "hermes_cli").mkdir(parents=True)
    (folder / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (folder / "hermes_cli" / "gateway_chat_view.py").write_text(VIEW, encoding="utf-8")
    (folder / "hermes_cli" / "main.py").write_text(MAIN, encoding="utf-8")
    return folder


class TheTurn(unittest.TestCase):
    """room_send.py's turn, run the way the connector runs it: Python -I room_send.py --hermes-root <root> chat ..."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = stub_hermes(self.dir / "hermes")
        self.calls = self.dir / "calls.jsonl"

    def run_turn(self, text="carry on with the tests", *, busy="steer", running=True, extra=(), **more):
        query = self.dir / "message.txt"
        query.write_text(text, encoding="utf-8")
        self.calls.write_text("", encoding="utf-8")
        env = {**os.environ, "TW_STUB": json.dumps({"busy": busy, "running": running, **more}),
               "TW_CALLS": str(self.calls)}
        out = subprocess.run([sys.executable, "-I", str(ROOM_SEND), "--hermes-root", str(self.root), "chat", "-Q",
                              "--format", "stream-json", "--query-file", str(query), *extra, "--resume", "s1"],
                             capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = [json.loads(ln) for ln in out.stdout.splitlines()]
        calls = [json.loads(ln) for ln in self.calls.read_text(encoding="utf-8").splitlines()]
        return [c[0] for c in calls], calls, [ln["status"] for ln in lines if ln["type"] == "admission"], lines[-1]

    def test_steer_goes_into_the_running_turn_and_follows_it(self):
        methods, calls, said, result = self.run_turn(busy="steer")
        self.assertEqual(methods, ["config.get", "session.steer"])
        self.assertEqual(calls[0][1], {"key": "busy", "session_id": "s1"})
        self.assertEqual(calls[1][1], {"session_id": "s1", "text": "carry on with the tests", "execution_generation": 7})
        self.assertEqual(said, ["steered"])
        self.assertEqual(result["admission"], "a1", "it waits for the running turn's end, which answers it")

    def test_interrupt_redirects_the_running_turn(self):
        methods, _, said, result = self.run_turn(busy="interrupt")
        self.assertEqual(methods, ["config.get", "session.redirect"])
        self.assertEqual((said, result["admission"]), (["redirected"], "a1"))

    def test_queue_goes_in_the_queue_and_says_when_it_starts(self):
        methods, _, said, result = self.run_turn(busy="queue")
        self.assertEqual(methods, ["config.get", "prompt.submit"])
        self.assertEqual((said, result["admission"]), (["queued", "started"], "a2"))

    def test_an_idle_chat_is_a_plain_submit(self):
        methods, _, said, _ = self.run_turn(running=False)
        self.assertEqual((methods, said), (["prompt.submit"], ["started"]))

    def test_a_file_a_slash_command_or_a_parked_turn_always_queues(self):
        for kw in ({"extra": ("--image", str(self.dir / "photo.png"))}, {"text": "/model gpt"}, {"parked": True}):
            methods, _, said, _ = self.run_turn(busy="interrupt", **kw)
            self.assertEqual((methods, said), (["prompt.submit"], ["queued", "started"]), kw)

    def test_a_rejected_steer_is_queued_not_lost(self):
        methods, _, said, result = self.run_turn(busy="interrupt", steer="rejected")
        self.assertEqual(methods, ["config.get", "session.redirect", "prompt.submit"])
        self.assertEqual((said, result["admission"]), (["queued", "started"], "a2"))

    def test_other_ops_still_read_their_request_on_stdin(self):
        out = subprocess.run([sys.executable, "-I", str(ROOM_SEND), "--hermes-root", str(self.root)],
                             input='{"op": "nothing"}', capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(out.stdout.splitlines()[-1])["reason"], "bad request")


class TheConnector(unittest.TestCase):
    def test_admission_lines_become_states_and_say_hermes_has_it(self):
        lines = ['{"type": "system", "subtype": "init"}', '{"type": "admission", "status": "queued"}',
                 '{"type": "admission", "status": "started"}', '{"type": "text", "text": "x"}']
        seen, said = {}, []
        own_connector.follow_turn(iter(lines), seen, lambda s, t: said.append(s))
        self.assertEqual(said, ["thinking", "queued", "thinking", "writing"])
        self.assertTrue(seen["admitted"])
        self.assertNotIn("taken", seen)
        seen, said = {}, []
        own_connector.follow_turn(iter(['{"type": "admission", "status": "steered"}', '{"type": "text"}']), seen,
                                  lambda s, t: said.append(s))
        self.assertEqual((said, seen["taken"]), (["steered", "writing"], "steered"))

    def test_the_init_line_alone_is_not_in_hermes(self):
        seen = {}
        own_connector.follow_turn(iter(['{"type": "system", "subtype": "init"}']), seen, lambda s, t: None)
        self.assertNotIn("admitted", seen)

    def test_every_turn_runs_through_room_send_which_one_already_updates_itself_with(self):
        ready = ["py", "-I", "room_send.py"]
        self.assertEqual(own_connector.Hermes(["hermes"], ready, Path("."), "One", turn_cmd=ready).turn_cmd, ready)
        src = Path(own_connector.__file__).read_text(encoding="utf-8")
        self.assertIn("turn_cmd=ready", src)
        self.assertIn('argv = [*self.turn_cmd, "attended-chat", "-Q", "--format", "stream-json"', src)
        self.assertEqual(own_connector.UPDATE_FILES, ("own_connector.py", "room_send.py"))

    def test_the_connector_says_what_each_turn_is_doing_and_whether_hermes_has_it(self):
        lines = ['{"type": "system", "subtype": "init"}', '{"type": "admission", "status": "redirected"}',
                 '{"type": "result", "session_id": "s1", "exit_code": 0}']
        script = Path(tempfile.mkdtemp()) / "hermes.py"
        self.addCleanup(shutil.rmtree, script.parent, ignore_errors=True)
        script.write_text("import sys\nfor ln in %r:\n    print(ln, flush=True)\n" % lines, encoding="utf-8")
        h = own_connector.Hermes([sys.executable, str(script)], [], script.parent, "One")
        said = []
        outcome = h._run([*h.cmd], "s1", lambda *a: said.append(a))
        self.assertEqual(outcome[0], "confirmed")
        self.assertEqual(said[0], ("thinking", None, False, None))
        self.assertEqual(said[1], ("redirected", None, True, "redirected"))


class TheCore(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.own = self.g.own
        self.g.hello("one")

    def send(self, sid=SID, key="own-key-0001"):
        return self.own.send(passkey(self.w), "one", sid, None if sid else True, "hello", key)["message_id"]

    def claim(self, turns=()):
        body = {"turns": list(turns)}
        return [i["id"] for i in self.own.claim(self.w.ws, "one", self.g.tok["one"], 3, True, doing=body)["items"]]

    def test_the_next_message_goes_once_the_one_before_is_in_hermes(self):
        a, b, c = self.send(key="own-key-0001"), self.send(key="own-key-0002"), self.send(key="own-key-0003")
        self.assertEqual(self.claim(), [a])
        self.assertEqual(self.claim([{"id": a, "state": "thinking", "admitted": False}]), [], "not in Hermes yet")
        self.assertEqual(self.claim([{"id": a, "state": "thinking", "admitted": True}]), [b])
        self.assertEqual(self.claim([{"id": a, "state": "writing", "admitted": True},
                                     {"id": b, "state": "thinking", "admitted": False}]), [], "in order")
        self.assertEqual(self.claim([{"id": a, "state": "writing", "admitted": True},
                                     {"id": b, "state": "queued", "admitted": True}]), [c])

    def test_a_new_chat_never_gets_a_second_message_before_it_has_an_id(self):
        n1 = self.send(sid=None, key="own-key-0001")
        self.assertEqual(self.claim(), [n1])
        self.assertEqual(self.claim([{"id": n1, "state": "thinking", "admitted": True}]), [])

    def test_the_states_and_the_steer_are_kept(self):
        for s in ("queued", "steered", "redirected"):
            self.assertIn(s, DOING_STATES)
        self.assertNotIn("waiting", DOING_STATES)
        a = self.send()
        self.claim()
        self.claim([{"id": a, "state": "writing", "admitted": True, "taken": "steered"}])
        got = CONNECTORS.get(self.w.ws, "one", self.w.store.clock())["turns"][a]
        self.assertEqual(got, {"state": "writing", "tool": None, "admitted": True, "taken": "steered"})
        self.claim([{"id": a, "state": "writing", "admitted": True, "taken": "anything"}])
        self.assertNotIn("taken", CONNECTORS.get(self.w.ws, "one", self.w.store.clock())["turns"][a])


class TheApp(unittest.TestCase):
    def test_in_queue_and_the_steer_words_like_desktop(self):
        self.assertIn("queued: 'In queue', steered: 'Steered into the running turn', "
                      "redirected: 'Interrupted the running turn'", APP)
        self.assertIn("note: mineNote(m)", APP)
        self.assertNotIn("Waiting for the turn already running", APP)

    def test_the_live_line_follows_only_the_turn_being_answered(self):
        self.assertIn("function beingAnswered(mine) { return mine.find(m => m.status === 'leased' && !inQueue(m)) }", APP)
        self.assertIn("const turn = beingAnswered(v.mine)", APP)
        self.assertIn("(was === 'leased' && inQueue(m) !== queued)", APP)


if __name__ == "__main__":
    unittest.main()
