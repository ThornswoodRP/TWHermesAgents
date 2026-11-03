"""The app's Tasks tab: subagents, background commands and scheduled jobs (Patrick, 3 Nov).

"I wanted to see the subagents, jobs, and background tasks on the Mobile and Desktop app." Checks:

- room_send.py op `tasks`, against stand-in Hermes modules: the live chats' subagents and background
  commands from the running gateway (session.active_list, subagent.list, process.list), a chat that closed
  meanwhile passed over, the scheduled jobs from Hermes's own cron store under each profile's home with
  the error redacted, and a part it could not read null with its reason, never an empty list;
- the core: each report checked field by field and kept in memory, a Hermes not heard from in TASKS_STALE
  shown as not heard, the Bots' connector and an own connector each with its own token and switch, and
  the app's GET /tasks in the rail's order;
- both connectors: the helper's answer read, and the report posted in a thread of its own, at most once a
  round and never while the last one still runs.

Everything runs on stand-ins and a scratch workspace; nothing reaches a real Hermes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import timedelta
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import ORIGIN, Served
from test_decisions_chat import flags, seat

from thornswood_decisions import connector, own_connector, tasks as tasks_mod
from thornswood_decisions.api import APP_VIEWS
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.own import OwnChat
from thornswood_decisions.store import CoreError

HELPER = Path(connector.__file__).with_name("room_send.py")

STUB_RUNTIME = textwrap.dedent('''
    import os
    from types import SimpleNamespace
    def gateway_profile_home(home):
        return home
    def discover_gateway_endpoint(home, timeout=2.0):
        down = os.environ.get("TW_STUB_DOWN", "").split(",")
        state = "absent" if home.name in down else "ready"
        return SimpleNamespace(state=state, endpoint=str(home) if state == "ready" else None)
    def attachable(found):
        return found.endpoint is not None
''')

# The gateway's answers, as gateway/session_active_list.py and session_ancillary.py give them.
STUB_CLIENT = textwrap.dedent('''
    import json, os
    from contextlib import asynccontextmanager
    class GatewayClientError(ValueError):
        pass
    class GatewayRPCError(GatewayClientError):
        pass
    LIVE = {
        "sessions": [
            {"id": "s_old", "title": "Older chat", "started_at": 100.0, "source": "tui", "status": "idle"},
            {"id": "s_new", "title": "Fix the  launcher", "started_at": 200.0, "source": "desktop", "status": "working"},
            {"id": "s_gone", "title": "Closed meanwhile", "started_at": 150.0, "source": "tui", "status": "idle"},
        ]}
    class Client:
        def __init__(self, endpoint):
            self.endpoint = endpoint
        async def rpc(self, method, **params):
            with open(os.environ["TW_STUB_LOG"], "a", encoding="utf-8") as f:
                f.write(json.dumps({"endpoint": self.endpoint, "method": method, "params": params}) + "\\n")
            if method == "session.active_list":
                return LIVE
            sid = params["session_id"]
            if sid == "s_gone":
                raise GatewayRPCError("not_found")
            if sid == "s_old" and os.environ.get("TW_STUB_REFUSE"):
                raise GatewayRPCError("permission_denied")
            if method == "subagent.list":
                return {"subagents": [] if sid == "s_old" else [
                    {"subagent_id": "sa_1", "parent_id": None, "depth": 1, "goal": "Read the log", "delegation_id": None,
                     "model": "m", "started_at": 1000.5, "status": "running", "tool_count": 3, "last_tool": "terminal",
                     "accepting_steer": True},
                    {"subagent_id": "sa_2", "parent_id": None, "depth": 1, "goal": "Sweep", "delegation_id": "d_1",
                     "model": "m", "started_at": 1001.0, "status": "running", "tool_count": 0, "last_tool": None,
                     "accepting_steer": True}], "delegations": []}
            if method == "process.list":
                return {"processes": [] if sid == "s_new" else [
                    {"session_id": "proc_1", "command": "pytest -q", "status": "exited", "exit_code": 1,
                     "started_epoch": 900.0, "exited_at": 950.0, "completion_reason": "exited",
                     "output_tail": "x" * 50000}]}
            raise GatewayRPCError("method_not_found")
    @asynccontextmanager
    async def connect_gateway(endpoint=None, subject=None):
        yield Client(endpoint)
''')

STUB_CONSTANTS = textwrap.dedent('''
    import os
    from pathlib import Path
    HOME = []
    def get_default_hermes_root():
        return Path(os.environ["TW_STUB_ROOT"])
    def set_hermes_home_override(path):
        HOME.append(path)
        return len(HOME)
    def reset_hermes_home_override(token):
        HOME.pop()
''')

# cron.jobs: list_jobs reads <home>/cron/jobs.json under the home use_cron_store set, as Hermes's does.
STUB_JOBS = textwrap.dedent('''
    import contextlib, json, os
    from pathlib import Path
    import hermes_constants
    _STORE = []
    @contextlib.contextmanager
    def use_cron_store(home):
        _STORE.append(Path(home))
        try:
            yield
        finally:
            _STORE.pop()
    def list_jobs(include_disabled=False):
        assert include_disabled is True
        assert hermes_constants.HOME and Path(hermes_constants.HOME[-1]) == _STORE[-1]
        path = _STORE[-1] / "cron" / "jobs.json"
        if os.environ.get("TW_STUB_JOBS_BROKEN") == _STORE[-1].name:
            raise RuntimeError("Cron database corrupted and unrepairable")
        return json.loads(path.read_text()) if path.exists() else []
''')

STUB_REDACT = "def redact_sensitive_text(text, **kw):\n    return text.replace('hunter2', '***')\n"


class Helper(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        src = self.dir / "src"
        for pkg in ("hermes_cli", "cron", "agent"):
            (src / pkg).mkdir(parents=True)
            (src / pkg / "__init__.py").write_text("", encoding="utf-8")
        (src / "hermes_cli" / "gateway_runtime.py").write_text(STUB_RUNTIME, encoding="utf-8")
        (src / "hermes_cli" / "gateway_client.py").write_text(STUB_CLIENT, encoding="utf-8")
        (src / "hermes_constants.py").write_text(STUB_CONSTANTS, encoding="utf-8")
        (src / "cron" / "jobs.py").write_text(STUB_JOBS, encoding="utf-8")
        (src / "agent" / "redact.py").write_text(STUB_REDACT, encoding="utf-8")
        self.src, self.root, self.log = src, self.dir / "hermes", self.dir / "rpc.jsonl"
        bramble = self.root / "profiles" / "bramble" / "cron"
        bramble.mkdir(parents=True)
        (bramble / "jobs.json").write_text(json.dumps([
            {"id": "j1", "name": "Nightly sweep", "schedule_display": "0 3 * * *", "state": "scheduled",
             "next_run_at": "2026-11-04T03:00:00-07:00", "last_run_at": "2026-11-03T03:00:00-07:00",
             "last_status": "error", "last_error": "token hunter2 refused\nat line 2",
             "latest_execution": {"status": "completed"}},
            {"id": "j2", "name": "Board review", "schedule_display": "every Monday", "state": "paused",
             "next_run_at": None, "last_run_at": None, "last_status": None, "last_error": None,
             "latest_execution": {"status": "running"}}]), encoding="utf-8")

    def run_helper(self, req: dict, **env) -> tuple[int, dict]:
        out = subprocess.run([sys.executable, "-I", str(HELPER), "--hermes-root", str(self.src)],
                             input=json.dumps(req).encode(), capture_output=True, timeout=60,
                             env={**os.environ, "TW_STUB_LOG": str(self.log), "TW_STUB_ROOT": str(self.root), **env})
        return out.returncode, json.loads(out.stdout.decode().strip().splitlines()[-1])

    def rpcs(self) -> list:
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def test_live_parts_come_from_the_running_gateways_live_chats(self):
        code, out = self.run_helper({"op": "tasks", "profiles": ["bramble"]})
        self.assertEqual(code, 0)
        got = out["tasks"]["bramble"]
        self.assertEqual(got["state"], "ready")
        self.assertNotIn("live_why", got)
        self.assertEqual(got["subagents"], [
            {"id": "sa_1", "chat": "s_new", "chat_title": "Fix the launcher", "goal": "Read the log", "model": "m",
             "status": "running", "depth": 1, "background": False, "tools": 3, "last_tool": "terminal",
             "started": 1000.5},
            {"id": "sa_2", "chat": "s_new", "chat_title": "Fix the launcher", "goal": "Sweep", "model": "m",
             "status": "running", "depth": 1, "background": True, "tools": 0, "last_tool": None, "started": 1001.0}])
        # The command's output is never carried: only what the row shows.
        self.assertEqual(got["processes"], [
            {"id": "proc_1", "chat": "s_old", "chat_title": "Older chat", "command": "pytest -q", "status": "exited",
             "exit_code": 1, "started": 900.0, "ended": 950.0, "why": "exited"}])
        # The newest chat first; the one that closed after the list was passed over; nothing but reads.
        calls = [(r["method"], r["params"].get("session_id")) for r in self.rpcs()]
        self.assertEqual(calls, [("session.active_list", None), ("subagent.list", "s_new"), ("process.list", "s_new"),
                                 ("subagent.list", "s_gone"), ("subagent.list", "s_old"), ("process.list", "s_old")])
        self.assertTrue(all(r["endpoint"].endswith("/profiles/bramble") for r in self.rpcs()))

    def test_jobs_come_from_hermess_cron_store_under_each_profiles_home(self):
        _, out = self.run_helper({"op": "tasks", "profiles": ["bramble", ""]})
        jobs = out["tasks"]["bramble"]["jobs"]
        self.assertEqual(jobs[0], {"id": "j1", "name": "Nightly sweep", "schedule": "0 3 * * *", "state": "scheduled",
                                   "next": "2026-11-04T03:00:00-07:00", "last": "2026-11-03T03:00:00-07:00",
                                   "last_status": "error", "last_error": "token *** refused at line 2",
                                   "running": False})
        self.assertEqual((jobs[1]["state"], jobs[1]["running"]), ("paused", True))
        # His own Hermes (profile empty) is the root home, which has no jobs here.
        self.assertEqual(out["tasks"][""]["jobs"], [])

    def test_a_gateway_that_is_not_running_has_no_live_part_and_says_its_state(self):
        _, out = self.run_helper({"op": "tasks", "profiles": ["bramble"]}, TW_STUB_DOWN="bramble")
        got = out["tasks"]["bramble"]
        self.assertEqual((got["state"], got["subagents"], got["processes"]), ("absent", None, None))
        self.assertEqual(got["live_why"], "its gateway is absent")
        self.assertEqual(len(got["jobs"]), 2)
        self.assertEqual(self.rpcs(), [])

    def test_a_part_it_could_not_read_is_null_with_its_reason(self):
        _, out = self.run_helper({"op": "tasks", "profiles": ["bramble"]}, TW_STUB_JOBS_BROKEN="bramble")
        got = out["tasks"]["bramble"]
        self.assertIsNone(got["jobs"])
        self.assertEqual(got["jobs_why"], "Hermes could not read its scheduled jobs (RuntimeError)")
        self.assertEqual(len(got["subagents"]), 2)
        _, out = self.run_helper({"op": "tasks", "profiles": ["bramble"]}, TW_STUB_REFUSE="1")
        got = out["tasks"]["bramble"]
        self.assertEqual(got["live_why"], "1 of its chats did not answer (permission_denied)")
        self.assertEqual(len(got["subagents"]), 2)

    def test_a_bad_request_is_refused(self):
        for profiles in (None, [], ["Bramble"], ["../x"], ["rift", "rift"], ["a"] * 17):
            with self.subTest(profiles=profiles):
                code, out = self.run_helper({"op": "tasks", "profiles": profiles})
                self.assertEqual((code, out), (2, {"tasks": None, "reason": "bad request"}))


def report(**over) -> dict:
    base = {"state": "ready", "subagents": [], "processes": [], "jobs": []}
    return {**base, **over}


class Core(unittest.TestCase):
    def setUp(self):
        tasks_mod._TASKS.clear()
        self.w = World()
        self.addCleanup(self.w.close)
        seat(self.w)
        flags(self.w, "mobile_read", "own_chat_write")
        f = Feeds(self.w.store)
        self.tc = f.new_token(self.w.ws, "connector", by="tests")
        self.to = f.new_token(self.w.ws, "own-one", by="tests")
        self.chat, self.own = Chat(self.w.store), OwnChat(self.w.store)

    def view(self) -> dict:
        return {a["id"]: a for a in self.chat.tasks_view(self.w.patrick())["agents"]}

    def test_each_report_is_kept_and_shown_in_the_rails_order(self):
        sub = {"id": "sa_1", "chat": "s1", "chat_title": "T", "goal": "Read  the\nlog", "model": "m", "status": "running",
               "depth": 1, "background": True, "tools": 2, "last_tool": "terminal", "started": 1000}
        self.chat.tasks(self.w.ws, self.tc, {"tasks": {"rift": report(subagents=[sub])}})
        self.own.tasks(self.w.ws, "one", self.to, {"tasks": report(subagents=None, processes=None,
                                                                   live_why="its gateway is absent")})
        got = self.chat.tasks_view(self.w.patrick())["agents"]
        self.assertEqual([a["id"] for a in got], ["me", "one", "bramble", "rift", "sprocket", "echo", "magpie", "wisp"])
        v = self.view()
        self.assertEqual(v["rift"]["subagents"][0]["goal"], "Read the log")
        self.assertEqual(v["rift"]["subagents"][0]["started"], 1000.0)
        self.assertEqual((v["one"]["subagents"], v["one"]["live_why"]), (None, "its gateway is absent"))
        self.assertEqual(v["me"], {"id": "me", "heard": False, "since": None})

    def test_a_hermes_not_heard_from_lately_shows_no_old_list(self):
        self.chat.tasks(self.w.ws, self.tc, {"tasks": {"rift": report(jobs=[{"id": "j", "name": "x"}])}})
        self.w.clock.advance(seconds=149)
        self.assertTrue(self.view()["rift"]["heard"])
        self.w.clock.advance(seconds=2)
        rift = self.view()["rift"]
        self.assertEqual((rift["heard"], set(rift)), (False, {"id", "heard", "since"}))

    def test_a_helper_that_could_not_read_says_why_for_every_bot(self):
        self.chat.tasks(self.w.ws, self.tc, {"why": "the tasks helper did not answer in a minute"})
        v = self.view()
        self.assertEqual(v["wisp"]["why"], "the tasks helper did not answer in a minute")
        self.assertNotIn("jobs", v["wisp"])

    def test_a_report_is_checked_field_by_field(self):
        bad = [report(subagents=[{"tools": "3"}]), report(jobs=[{"running": 1}]), report(processes=[{"started": "x"}]),
               report(jobs=[{"name": 5}]), report(jobs=[{}] * 31), report(jobs="none"), "no", report(state=["x"])]
        for r in bad:
            with self.subTest(r=str(r)[:60]), self.assertRaises(CoreError) as e:
                self.chat.tasks(self.w.ws, self.tc, {"tasks": {"rift": r}})
            self.assertEqual(e.exception.status, 400)
        self.chat.tasks(self.w.ws, self.tc, {"tasks": {"rift": report(jobs=[{"name": "n" * 500, "extra": "dropped"}])}})
        job = self.view()["rift"]["jobs"][0]
        self.assertEqual((len(job["name"]), "extra" in job, job["running"]), (120, False, None))

    def test_the_bots_connector_names_bound_bots_only_with_its_own_token(self):
        with self.assertRaises(CoreError) as e:
            self.chat.tasks(self.w.ws, self.tc, {"tasks": {"stranger": report()}})
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(CoreError) as e:
            self.chat.tasks(self.w.ws, self.to, {"tasks": {"rift": report()}})
        self.assertEqual(e.exception.status, 401)
        with self.assertRaises(CoreError) as e:
            self.own.tasks(self.w.ws, "one", self.tc, {"tasks": report()})
        self.assertEqual(e.exception.status, 401)

    def test_nothing_is_taken_while_its_switch_is_off(self):
        self.w.store.set_flag(self.w.ws, "own_chat_write", False, by="tests")
        with self.assertRaises(CoreError) as e:
            self.own.tasks(self.w.ws, "one", self.to, {"tasks": report()})
        self.assertEqual((e.exception.status, e.exception.reason), (423, "own_chat_off"))
        self.w.store.set_flag(self.w.ws, "mobile_read", False, by="tests")
        with self.assertRaises(CoreError) as e:
            self.chat.tasks(self.w.ws, self.tc, {"tasks": {}})
        self.assertEqual(e.exception.status, 423)
        self.assertEqual(tasks_mod._TASKS, {})

    def test_the_app_reads_it_and_the_connectors_post_it_over_http(self):
        tasks_mod._TASKS.clear()
        s = Served(self)
        seat(s.w)
        flags(s.w, "mobile_read", "own_chat_write")
        f = Feeds(s.w.store)
        tc, to = f.new_token(s.w.ws, "connector", by="tests"), f.new_token(s.w.ws, "own-one", by="tests")
        status, _, body = s.call("POST", "/connector/tasks", headers={"Authorization": f"Bearer {tc}"},
                                 body={"tasks": {"rift": report()}})
        self.assertEqual((status, len(body["bots"])), (200, 6))
        status, _, _ = s.call("POST", "/own/one/tasks", headers={"Authorization": f"Bearer {to}"},
                              body={"tasks": report()})
        self.assertEqual(status, 200)
        status, _, body = s.call("POST", "/own/one/tasks", headers={"Authorization": f"Bearer {to}", "Origin": ORIGIN},
                                 body={"tasks": report()})
        self.assertEqual((status, body["error"]), (403, "not_a_feed"))
        cookie, _ = s.phone()
        status, _, body = s.call("GET", "/tasks", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual([a["heard"] for a in body["agents"]], [False, True, False, True, False, False, False, False])
        self.assertTrue(APP_VIEWS.match("tasks"))
        status, headers, _ = s.api.handle("GET", "/m/tasks", {}, b"")
        self.assertEqual((status, headers["Content-Type"]), (200, "text/html; charset=utf-8"))


class FakeCore:
    def __init__(self, status=200):
        self.posts, self.status = [], status

    def post(self, op, body):
        self.posts.append((op, json.loads(json.dumps(body))))
        return self.status, {}


class Connectors(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def helper(self, answer: str, code: int = 0) -> list[str]:
        """A stand-in helper: reads the request, keeps it, and prints the given answer."""
        script = self.dir / "helper.py"
        script.write_text("import sys, pathlib\n"
                          f"pathlib.Path({str(self.dir / 'req.json')!r}).write_text(sys.stdin.read())\n"
                          f"print({answer!r})\nsys.exit({code})\n", encoding="utf-8")
        return [sys.executable, "-I", str(script)]

    def asked(self) -> dict:
        return json.loads((self.dir / "req.json").read_text())

    def test_the_bots_connector_asks_for_the_bots_and_posts_their_reports(self):
        ans = json.dumps({"tasks": {"rift": report(), "stranger": report()}})
        h = connector.Hermes(["hermes"], self.helper(ans), self.dir)
        self.assertEqual(h.tasks(["rift", "wisp"]), {"tasks": {"rift": report()}})
        self.assertEqual(self.asked(), {"op": "tasks", "profiles": ["rift", "wisp"]})
        h = connector.Hermes(["hermes"], self.helper("not json", 3), self.dir)
        self.assertIn("failed (exit 3)", h.tasks(["rift"])["why"])

    def test_the_bots_connector_posts_in_a_thread_once_a_round(self):
        core = FakeCore()
        conn = connector.Connector(core, connector.Hermes(["hermes"], self.helper(json.dumps({"tasks": {"rift": report()}})),
                                                          self.dir), say=lambda _: None, hermes_home=self.dir)
        clock = iter([0.0, 1.0, 31.0]).__next__
        self.assertFalse(conn.tasks(clock))       # the core has named no Bots yet
        conn.live_bots = ["rift"]
        self.assertTrue(conn.tasks(lambda: 0.0))
        conn._tasks_run.join(30)
        self.assertEqual(core.posts, [("/connector/tasks", {"tasks": {"rift": report()}})])
        self.assertFalse(conn.tasks(lambda: 10.0))
        self.assertTrue(conn.tasks(lambda: 31.0))
        conn._tasks_run.join(30)
        self.assertEqual(len(core.posts), 2)

    def test_a_round_still_running_holds_the_next(self):
        core = FakeCore()
        slow = self.dir / "slow.py"
        slow.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(2)\nprint('{\"tasks\": {}}')\n", encoding="utf-8")
        conn = connector.Connector(core, connector.Hermes(["hermes"], [sys.executable, "-I", str(slow)], self.dir),
                                   say=lambda _: None, hermes_home=self.dir)
        conn.live_bots = ["rift"]
        self.assertTrue(conn.tasks(lambda: 0.0))
        self.assertFalse(conn.tasks(lambda: 100.0))
        conn._tasks_run.join(30)
        self.assertEqual(core.posts, [("/connector/tasks", {"tasks": {}})])

    def test_an_own_connector_posts_its_hermess_report_or_why_not(self):
        said = []
        ans = json.dumps({"tasks": {"": report(jobs=None, jobs_why="x")}})
        hermes = own_connector.Hermes(["hermes"], self.helper(ans), self.dir, "One's Hermes")
        self.assertEqual(hermes.tasks(), report(jobs=None, jobs_why="x"))
        self.assertEqual(self.asked(), {"op": "tasks", "profiles": [""]})
        core = FakeCore()
        oc = own_connector.OwnConnector(core, hermes, self.dir / "state.db", say=said.append, clock=lambda: 0.0)
        self.assertTrue(oc.tasks())
        oc._tasks_run.join(30)
        self.assertEqual(core.posts, [("tasks", {"tasks": report(jobs=None, jobs_why="x")})])
        self.assertFalse(oc.tasks())   # the same moment: not again until TASKS_SECONDS
        broken = own_connector.Hermes(["hermes"], self.helper("nope", 1), self.dir, "One's Hermes")
        core = FakeCore()
        oc = own_connector.OwnConnector(core, broken, self.dir / "state.db", say=said.append, clock=time.monotonic)
        oc.tasks()
        oc._tasks_run.join(30)
        self.assertEqual(core.posts[0][0], "tasks")
        self.assertIn("the tasks helper failed (exit 1)", core.posts[0][1]["why"])
        self.assertTrue(any(s.startswith("FAIL tasks:") for s in said))


if __name__ == "__main__":
    unittest.main()
