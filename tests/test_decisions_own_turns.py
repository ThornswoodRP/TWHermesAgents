"""Hermes's questions and approvals, and Stop, in Patrick's own chats from the app (own_turns.py, 9 Nov 2026).

Patrick, 9 Nov: "so I can fully switch to that versus the official Hermes Desktop app". Desktop asks him
Hermes's questions and approvals and has Stop; the app's one-shot turns made Hermes guess instead.

Checks:
- room_send.py's attended turn (`attended-chat`) submits without `finite`, keeps waiting while a question is
  open, says each question opened and settled, answers it from an order on its stdin with Hermes's own
  clarify.respond or approval.respond, and stops the turn with session.interrupt; the plain `chat` turn
  is the one-shot it was;
- the connector runs every turn attended, keeps the open questions with what the turn is doing, and writes
  each order to its turn's process;
- the core takes an order only for his running turn, with a fresh passkey sign-in, and an answer only to an
  open question with a choice Hermes offered; it hands each order out once and never writes the words down;
- the app draws the questions, the approvals and Stop under the live line.
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
import threading
import time
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_own import SID, Gateways, passkey

from thornswood_decisions import own_connector
from thornswood_decisions.own import CONNECTORS
from thornswood_decisions.own_turns import ORDERS, answer_value, clean_asks
from thornswood_decisions.store import CoreError

HERE = Path(own_connector.__file__).resolve().parent
ROOM_SEND = HERE / "room_send.py"
APP = (HERE / "app" / "app.js").read_text(encoding="utf-8")

# A stand-in for hermes_cli/gateway_chat_view.py as v0.23.0 has the parts an attended turn changes: prompts set
# from the snapshot, _request and _settled from the prompt events, submit with `finite` on a one-shot,
# _finite_block detaching while a prompt is open, and run (submit, then wait for the admission's end).
VIEW = textwrap.dedent('''
    import asyncio, json, os, sys

    CURRENT = None

    class GatewayChatView:
        def __init__(self, client, snapshot, *, quiet=False):
            self.client = client
            self.session_id = snapshot["stored_session_id"]
            self.generation = snapshot.get("execution_generation", 0)
            self.prompts = {p["prompt_id"]: p for p in snapshot.get("prompts", [])}
            self.pending = []
            self.finite = False
            self.done = None

        async def submit(self, text):
            return await self.client.rpc("prompt.submit", session_id=self.session_id, input_id="x", text=text,
                                         **({"finite": True} if self.finite else {}))

        def show_prompt(self, prompt):
            print("PROMPT " + prompt["prompt_id"], file=sys.stderr)

        def _request(self, admission, payload):
            self.prompts[payload["prompt_id"]] = payload
            if not self.finite:
                self.show_prompt(payload)

        def _settled(self, admission, payload):
            self.prompts.pop(payload["prompt_id"], None)

        def _finite_block(self, admission):
            if self.prompts:
                return "Input required; detached without cancelling. Resume this session interactively."
            return None

        async def run(self, query=None, *, oneshot=False):
            self.finite = oneshot
            for p in self.prompts.values():
                self.show_prompt(p)
            self.done = asyncio.Event()
            global CURRENT
            CURRENT = self
            await self.submit(query)
            S = json.loads(os.environ["TW_STUB"])
            if S.get("ask"):
                self._request("a2", dict(S["ask"], execution_generation=7))
            while not self.done.is_set():
                blocked = self._finite_block("a2")
                if blocked:
                    print(json.dumps({"type": "result", "exit_code": 3, "error": blocked}))
                    return 3
                if not S.get("ask"):
                    break
                await asyncio.sleep(0.05)
            print(json.dumps({"type": "result", "session_id": "s1", "exit_code": 0}))
            return 0
''')
MAIN = textwrap.dedent('''
    import asyncio, json, os, sys
    from hermes_cli import gateway_chat_view

    class Client:
        @property
        def view(self):
            return gateway_chat_view.CURRENT

        async def rpc(self, method, **p):
            with open(os.environ["TW_CALLS"], "a", encoding="utf-8") as f:
                f.write(json.dumps([method, {k: v for k, v in p.items() if k != "input_id"}]) + "\\n")
            if method == "prompt.submit":
                return {"admission_id": "a2", "status": "started"}
            if method in ("clarify.respond", "approval.respond"):
                ask = self.view.prompts.get(p["prompt_id"])
                if method == "approval.respond" and p["choice"] not in ask["choices"]:
                    raise RuntimeError("invalid_params")
                self.view._settled("a2", {"prompt_id": p["prompt_id"], "execution_generation": 7})
                self.view.done.set()
                return {"status": "resolved"}
            if method == "session.interrupt":
                self.view._settled("a2", {"prompt_id": "p9", "execution_generation": 7})
                self.view.done.set()
                return {"status": "interrupted"}
            raise SystemExit("unexpected " + method)

    def main():
        args = sys.argv[1:]
        assert args[0] == "chat", args
        text = open(args[args.index("--query-file") + 1], encoding="utf-8").read()
        view = gateway_chat_view.GatewayChatView(Client(), {"stored_session_id": "s1", "execution_generation": 7})
        return asyncio.run(view.run(text, oneshot=True))

    if __name__ == "__main__":
        sys.exit(main())
''')
QUESTION = {"kind": "clarify", "prompt_id": "p9", "question": "Which colour?", "choices": ["blue", "red"],
            "multi_select": False}
APPROVAL = {"kind": "approval", "prompt_id": "p9", "command": "rm -rf build", "description": "Delete build",
            "choices": ["once", "session", "deny"]}


class TheAttendedTurn(unittest.TestCase):
    """room_send.py's turn, run the way the connector runs it, with orders on its stdin."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        root = self.dir / "hermes"
        (root / "hermes_cli").mkdir(parents=True)
        (root / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
        (root / "hermes_cli" / "gateway_chat_view.py").write_text(VIEW, encoding="utf-8")
        (root / "hermes_cli" / "main.py").write_text(MAIN, encoding="utf-8")
        self.root = root
        self.calls = self.dir / "calls.jsonl"

    def run_turn(self, verb="attended-chat", ask=None, order=None):
        query = self.dir / "message.txt"
        query.write_text("tidy the build folder", encoding="utf-8")
        self.calls.write_text("", encoding="utf-8")
        env = {**os.environ, "TW_STUB": json.dumps({"ask": ask}), "TW_CALLS": str(self.calls)}
        proc = subprocess.Popen([sys.executable, "-I", str(ROOM_SEND), "--hermes-root", str(self.root), verb, "-Q",
                                 "--format", "stream-json", "--query-file", str(query), "--resume", "s1"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=env)
        lines = []

        def read():
            for ln in proc.stdout:
                lines.append(json.loads(ln))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        for o in ([order] if isinstance(order, dict) else order or []):
            deadline = time.time() + 30
            while not any(ln.get("type") == "ask" for ln in lines) and proc.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            proc.stdin.write(json.dumps(o) + "\n")
            proc.stdin.flush()
            time.sleep(0.3)
        try:
            proc.wait(timeout=60)
        finally:
            proc.kill()
        reader.join(10)
        proc.stdin.close()
        err = proc.stderr.read()
        proc.stderr.close()
        calls = [json.loads(ln) for ln in self.calls.read_text(encoding="utf-8").splitlines()]
        return proc.returncode, lines, calls, err

    def test_a_question_is_said_and_answered_from_an_order(self):
        code, lines, calls, err = self.run_turn(ask=QUESTION, order={"op": "answer", "prompt_id": "p9", "value": "blue"})
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[0], ["prompt.submit", {"session_id": "s1", "text": "tidy the build folder"}],
                         "an attended turn goes in without finite, so Hermes asks instead of guessing")
        self.assertIn(["clarify.respond", {"session_id": "s1", "execution_generation": 7, "prompt_id": "p9",
                                           "answer": "blue"}], calls)
        kinds = [ln["type"] for ln in lines]
        self.assertEqual(sorted(kinds), sorted(["admission", "ask", "order", "ask_settled", "result"]))
        self.assertEqual(kinds[-1], "result")
        self.assertIn({"type": "ask", "prompt_id": "p9", "kind": "clarify", "question": "Which colour?",
                       "choices": ["blue", "red"], "multi_select": False}, lines)
        self.assertIn({"type": "ask_settled", "prompt_id": "p9"}, lines)
        self.assertIn({"type": "order", "op": "answer", "prompt_id": "p9", "status": "sent"}, lines)
        self.assertEqual(lines[-1]["exit_code"], 0)
        self.assertNotIn("PROMPT", err, "nothing of it goes to stderr")

    def test_an_approval_takes_only_a_choice_hermes_offered(self):
        code, lines, calls, _ = self.run_turn(ask=APPROVAL, order={"op": "answer", "prompt_id": "p9", "value": "once"})
        self.assertEqual(code, 0)
        self.assertIn(["approval.respond", {"session_id": "s1", "execution_generation": 7, "prompt_id": "p9",
                                            "choice": "once"}], calls)
        (ask,) = [ln for ln in lines if ln["type"] == "ask"]
        self.assertEqual((ask["command"], ask["description"], ask["choices"]), ("rm -rf build", "Delete build",
                                                                                ["once", "session", "deny"]))

    def test_a_refused_answer_is_said_and_the_turn_keeps_waiting(self):
        query = {"op": "answer", "prompt_id": "p9", "value": "always"}
        code, lines, calls, _ = self.run_turn(ask=APPROVAL, order=[query, {"op": "stop"}])
        # Hermes refused it; the question stays open, so the turn waits until it is stopped.
        self.assertEqual(code, 0)
        self.assertIn({"type": "order", "op": "answer", "prompt_id": "p9", "status": "refused",
                       "reason": "Hermes refused it (RuntimeError)"}, lines)

    def test_stop_interrupts_the_turn_at_its_generation(self):
        code, lines, calls, _ = self.run_turn(ask=QUESTION, order={"op": "stop"})
        self.assertEqual(code, 0)
        self.assertIn(["session.interrupt", {"session_id": "s1", "execution_generation": 7}], calls)
        self.assertIn({"type": "order", "op": "stop", "status": "sent"}, lines)

    def test_an_answer_to_a_question_not_open_is_gone(self):
        code, lines, calls, _ = self.run_turn(ask=QUESTION, order=[{"op": "answer", "prompt_id": "p1", "value": "x"},
                                                                     {"op": "stop"}])
        self.assertIn({"type": "order", "op": "answer", "prompt_id": "p1", "status": "gone",
                       "reason": "that question is no longer open"}, lines)
        self.assertNotIn("clarify.respond", [c[0] for c in calls])

    def test_the_plain_chat_turn_is_the_one_shot_it_was(self):
        code, lines, calls, _ = self.run_turn(verb="chat", ask=QUESTION)
        self.assertEqual(calls[0][1].get("finite"), True)
        self.assertEqual(lines[-1]["exit_code"], 3, "a one-shot detaches at a question")
        self.assertNotIn("ask", [ln["type"] for ln in lines])


class TheConnector(unittest.TestCase):
    def test_every_turn_is_attended(self):
        src = Path(own_connector.__file__).read_text(encoding="utf-8")
        self.assertIn('argv = [*self.turn_cmd, "attended-chat", "-Q"', src)
        self.assertIn("stdin=subprocess.PIPE", src)

    def test_the_open_questions_are_kept_with_what_the_turn_is_doing(self):
        seen, told = {}, []
        lines = [json.dumps({"type": "system"}), json.dumps({"type": "ask", **QUESTION}),
                 json.dumps({"type": "order", "op": "answer", "prompt_id": "p9", "status": "sent"}),
                 json.dumps({"type": "ask_settled", "prompt_id": "p9"}), json.dumps({"type": "text"})]
        snaps = []

        def tell(state, tool):
            told.append((state, tool))
            snaps.append(dict(seen.get("asks") or {}))

        own_connector.follow_turn(lines, seen, tell)
        self.assertEqual(told, [("thinking", None)] * 4 + [("writing", None)])
        self.assertEqual(snaps[1], {"p9": {k: QUESTION[k] for k in QUESTION}})
        self.assertEqual(snaps[3], {})
        self.assertEqual(seen["order"], {"op": "answer", "prompt_id": "p9", "status": "sent"})

    def test_an_order_goes_to_its_turns_stdin_and_nowhere_else(self):
        class Proc:
            def __init__(self):
                self.written = []
                self.stdin = self

            def poll(self):
                return None

            def write(self, s):
                self.written.append(s)

            def flush(self):
                pass

        c = own_connector.OwnConnector.__new__(own_connector.OwnConnector)
        c._lock = threading.Lock()
        said = []
        c.say = said.append
        mine = Proc()
        c.procs = {"own_1": mine}
        n = c.orders([{"message_id": "own_1", "op": "answer", "prompt_id": "p9", "value": "blue"},
                      {"message_id": "own_2", "op": "stop"}, {"message_id": "own_1", "op": "rm"}])
        self.assertEqual(n, 1)
        self.assertEqual([json.loads(s) for s in mine.written], [{"op": "answer", "prompt_id": "p9", "value": "blue"}])
        self.assertIn("NOTE order stop: that turn has ended", said)


class TheCore(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.own = self.g.own
        self.g.hello("one")
        ORDERS.waiting.clear()

    def running(self, asks=(), admitted=True):
        mid = self.own.send(passkey(self.w), "one", SID, None, "tidy up", "own-key-0001")["message_id"]
        self.claim()
        self.claim([{"id": mid, "state": "tool", "tool": "clarify", "admitted": admitted, "asks": list(asks)}])
        return mid

    def claim(self, turns=()):
        return self.own.claim(self.w.ws, "one", self.g.tok["one"], 3, True, doing={"turns": list(turns)})

    def order(self, body, p=None):
        return self.own.turn_order(p or passkey(self.w), "one", body)

    def refused(self, reason, body, p=None):
        with self.assertRaises(CoreError) as e:
            self.order(body, p)
        self.assertEqual(e.exception.reason, reason)

    def test_the_open_questions_reach_the_chat(self):
        mid = self.running([QUESTION])
        doing = CONNECTORS.get(self.w.ws, "one", self.w.store.clock())["turns"][mid]
        self.assertEqual(doing["asks"], [{"prompt_id": "p9", "kind": "clarify", "choices": ["blue", "red"],
                                          "question": "Which colour?", "multi_select": False}])
        view = self.own.view(passkey(self.w), "one", SID)
        self.assertEqual(view["mine"][0]["doing"]["asks"][0]["question"], "Which colour?")

    def test_an_answer_is_handed_to_the_connector_once(self):
        mid = self.running([QUESTION])
        out = self.order({"message_id": mid, "op": "answer", "prompt_id": "p9", "value": "blue"})
        self.assertEqual(out["status"], "queued")
        handed = self.claim([{"id": mid, "state": "tool", "admitted": True, "asks": [QUESTION]}])["orders"]
        self.assertEqual([{k: o[k] for k in ("message_id", "op", "prompt_id", "value")} for o in handed],
                         [{"message_id": mid, "op": "answer", "prompt_id": "p9", "value": "blue"}])
        self.assertEqual(self.claim()["orders"], [])

    def test_stop_needs_only_a_running_turn(self):
        mid = self.running()
        self.order({"message_id": mid, "op": "stop"})
        self.assertEqual([o["op"] for o in self.claim()["orders"]], ["stop"])

    def test_refusals(self):
        mid = self.running([QUESTION, APPROVAL | {"prompt_id": "p8"}])
        self.refused("bad_order", {"message_id": mid, "op": "rm"})
        self.refused("not_running", {"message_id": "own_nothing", "op": "stop"})
        self.refused("not_open", {"message_id": mid, "op": "answer", "prompt_id": "p1", "value": "blue"})
        self.refused("bad_answer", {"message_id": mid, "op": "answer", "prompt_id": "p8", "value": "always"})
        self.refused("bad_answer", {"message_id": mid, "op": "answer", "prompt_id": "p9", "value": ["blue"]})
        self.refused("passkey_needed", {"message_id": mid, "op": "stop"}, passkey(self.w, minutes_ago=31))

    def test_a_turn_hermes_does_not_have_yet_takes_no_order(self):
        mid = self.running(admitted=False)
        self.refused("not_running", {"message_id": mid, "op": "stop"})

    def test_no_words_go_into_the_event_chain(self):
        mid = self.running([QUESTION])
        self.order({"message_id": mid, "op": "answer", "prompt_id": "p9", "value": "a secret colour"})
        rows = self.w.store.db.execute("SELECT kind, body_json FROM events WHERE kind = 'own.order'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("secret", rows[0]["body_json"])
        self.assertNotIn("Which colour", rows[0]["body_json"])

    def test_answers_take_hermess_own_shape(self):
        (q,) = clean_asks([QUESTION | {"multi_select": True}])
        self.assertEqual(answer_value(q, ["red", "blue"]), '["red", "blue"]')
        self.assertIsNone(answer_value(q, ["green"]))
        self.assertEqual(answer_value(q, "something else"), "something else")
        self.assertIsNone(answer_value(q, "x" * 4001))
        (a,) = clean_asks([APPROVAL])
        self.assertEqual(answer_value(a, "deny"), "deny")
        self.assertIsNone(answer_value(a, "always"), "not offered")
        self.assertEqual(clean_asks([{"kind": "secret", "prompt_id": "p1"}, {"kind": "clarify", "prompt_id": "bad id"}]),
                         [])


class TheApp(unittest.TestCase):
    def test_questions_approvals_and_stop_under_the_live_line(self):
        self.assertIn("workingItem(name, gw, turn.doing, turn.id), turnPart(gw, name, turn, canAct)", APP)
        self.assertIn("call('POST', `/own/${gw}/turn`, { message_id: turn.id, ...body })", APP)
        self.assertIn("once: 'Allow once', session: 'Allow in this chat', always: 'Always allow', deny: 'Deny'", APP)
        self.assertIn("'Stop'", APP)
        self.assertIn("askSig(m.doing) !== drawn", APP, "a question opened or settled draws the chat again")
        self.assertIn("!!v.sign_in_until", APP, "only with the passkey sign-in a message needs")


if __name__ == "__main__":
    unittest.main()
