"""Pause, resume, run now and remove his own Hermes's and One's scheduled jobs from the Tasks tab (#43).

Checks:

- room_send.py op `job`, against stand-in Hermes modules: each of the four is Hermes's own cron.jobs call
  (pause_job, resume_job, trigger_job, remove_job) on the job with that id, inside his own Hermes's home,
  then the scheduler told, as Hermes's cronjob tool does; Hermes's own refusal said as it is; a job it
  does not have, a bad request and a Bot's job refused;
- the core: a change names a job that Hermes listed lately, needs a passkey sign-in of the last 30
  minutes and the Hermes online, is handed to that gateway's connector once, answered done or refused,
  replayed for the same key and never made twice, and given up after 2 minutes unanswered; a change while
  the last one is being made waits; the Tasks view carries each job's newest change and the sign-in;
  the routes over HTTP;
- the connector: each change run through the helper and answered, and the jobs read again at once.

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
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_api import ORIGIN, Served
from test_decisions_own import Gateways, on, passkey
from test_decisions_tasks import STUB_CONSTANTS, report

from thornswood_decisions import connector, own_connector, own_jobs, tasks as tasks_mod
from thornswood_decisions.chat import Chat
from thornswood_decisions.store import CoreError

HELPER = Path(connector.__file__).with_name("room_send.py")

# cron.jobs: each call works on <home>/cron/jobs.json under the home use_cron_store set, as Hermes's does.
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
    def _path():
        assert hermes_constants.HOME and Path(hermes_constants.HOME[-1]) == _STORE[-1]
        return _STORE[-1] / "cron" / "jobs.json"
    def _log(call):
        with open(os.environ["TW_STUB_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"home": str(_STORE[-1]), "call": call}) + "\\n")
    def _jobs():
        return json.loads(_path().read_text()) if _path().exists() else []
    def get_job(job_id):
        return next((j for j in _jobs() if j["id"] == job_id), None)
    def _set(job_id, call, **changes):
        _log([call, job_id])
        jobs = _jobs()
        for j in jobs:
            if j["id"] == job_id:
                j.update(changes)
                _path().write_text(json.dumps(jobs))
                return j
        return None
    def pause_job(job_id, reason=None):
        return _set(job_id, "pause_job", state="paused")
    def resume_job(job_id):
        if job_id == "j_once":
            raise ValueError("Cannot resume: one-shot time 2026-11-01T00:00:00 is in the past\\n(grace window: 120s) and will never fire.")
        return _set(job_id, "resume_job", state="scheduled")
    def trigger_job(job_id, extra_prompt=None):
        return _set(job_id, "trigger_job", state="scheduled", next_run_at="now")
    def remove_job(job_id):
        _log(["remove_job", job_id])
        jobs = _jobs()
        left = [j for j in jobs if j["id"] != job_id]
        _path().write_text(json.dumps(left))
        return len(left) != len(jobs)
''')

STUB_SCHEDULER = textwrap.dedent('''
    import json, os
    from cron import jobs
    def _notify_provider_jobs_changed():
        jobs._log(["notify", None])
''')


class Helper(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        src = self.dir / "src"
        (src / "cron").mkdir(parents=True)
        (src / "cron" / "__init__.py").write_text("", encoding="utf-8")
        (src / "cron" / "jobs.py").write_text(STUB_JOBS, encoding="utf-8")
        (src / "cron" / "scheduler.py").write_text(STUB_SCHEDULER, encoding="utf-8")
        (src / "hermes_constants.py").write_text(STUB_CONSTANTS, encoding="utf-8")
        self.src, self.root, self.log = src, self.dir / "hermes", self.dir / "calls.jsonl"
        for home in (self.root, self.root / "profiles" / "bramble"):
            (home / "cron").mkdir(parents=True)
            (home / "cron" / "jobs.json").write_text(json.dumps([
                {"id": "j1", "name": "Nightly sweep", "state": "scheduled"},
                {"id": "j_once", "name": "Once", "state": "completed"}]), encoding="utf-8")

    def run_helper(self, req: dict) -> tuple[int, dict]:
        out = subprocess.run([sys.executable, "-I", str(HELPER), "--hermes-root", str(self.src)],
                             input=json.dumps(req).encode(), capture_output=True, timeout=60,
                             env={**os.environ, "TW_STUB_LOG": str(self.log), "TW_STUB_ROOT": str(self.root)})
        return out.returncode, json.loads(out.stdout.decode().strip().splitlines()[-1])

    def calls(self) -> list:
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def jobs(self, home=None) -> dict:
        return {j["id"]: j for j in json.loads(((home or self.root) / "cron" / "jobs.json").read_text())}

    def test_each_change_is_hermess_own_cron_call_in_his_own_home_then_the_scheduler_is_told(self):
        for do, call, state in (("pause", "pause_job", "paused"), ("resume", "resume_job", "scheduled"),
                                ("run", "trigger_job", "scheduled")):
            with self.subTest(do=do):
                self.log.unlink(missing_ok=True)
                self.assertEqual(self.run_helper({"op": "job", "profile": "", "job": "j1", "do": do}),
                                 (0, {"done": True}))
                self.assertEqual([c["call"] for c in self.calls()], [[call, "j1"], ["notify", None]])
                self.assertTrue(all(c["home"] == str(self.root) for c in self.calls()))
                self.assertEqual(self.jobs()["j1"]["state"], state)
        self.assertEqual(self.run_helper({"op": "job", "job": "j1", "do": "remove"}), (0, {"done": True}))
        self.assertNotIn("j1", self.jobs())
        # Bramble's store, under its own profile, was never touched.
        self.assertEqual(self.jobs(self.root / "profiles" / "bramble")["j1"]["state"], "scheduled")

    def test_hermess_own_refusal_is_said_and_a_job_it_does_not_have_is_named(self):
        self.assertEqual(self.run_helper({"op": "job", "job": "j_once", "do": "resume"}),
                         (0, {"done": False, "reason": "Cannot resume: one-shot time 2026-11-01T00:00:00 is in the "
                                                       "past (grace window: 120s) and will never fire."}))
        self.assertEqual(self.run_helper({"op": "job", "job": "j_gone", "do": "pause"}),
                         (0, {"done": False, "reason": "Hermes has no scheduled job with that id now"}))
        self.assertNotIn(["notify", None], [c["call"] for c in self.calls()])

    def test_a_bad_request_and_a_bots_job_are_refused_before_anything_is_touched(self):
        for req in ({"job": "j1", "do": "delete"}, {"job": "../j1", "do": "pause"}, {"job": 5, "do": "pause"},
                    {"do": "pause"}):
            with self.subTest(req=req):
                self.assertEqual(self.run_helper({"op": "job", **req}), (0, {"done": False, "reason": "bad request"}))
        self.assertEqual(self.run_helper({"op": "job", "profile": "bramble", "job": "j1", "do": "pause"}),
                         (0, {"done": False, "reason": "a Bot's scheduled jobs are not changed from here"}))
        self.assertEqual(self.calls(), [])


JOB = {"id": "j1", "name": "Nightly sweep", "schedule": "0 3 * * *", "state": "scheduled", "next": None,
       "last": None, "last_status": None, "last_error": None, "running": False}


class Core(unittest.TestCase):
    def setUp(self):
        tasks_mod._TASKS.clear()
        own_jobs.JOBS.asked.clear()
        own_jobs.JOBS.shown.clear()
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        on(self.w, "mobile_read")
        self.own, self.p = self.g.own, passkey(self.w)
        self.g.hello("one")
        self.listed("one")

    def listed(self, gw, jobs=(JOB,)):
        self.own.tasks(self.w.ws, gw, self.g.tok[gw], {"tasks": report(jobs=list(jobs))})

    def ask(self, do="pause", key="k-00000001", p=None, gw="one", job="j1"):
        return self.own.job_set(p or self.p, gw, {"job": job, "do": do, "idempotency_key": key})

    def hand(self, gw="one") -> list:
        return self.g.hello(gw)["jobs"]

    def answer(self, a, done=True, reason="", gw="one"):
        return self.own.job_post(self.w.ws, gw, self.g.tok[gw], {"id": a["id"], "done": done, "reason": reason})

    def view(self) -> dict:
        return Chat(self.w.store).tasks_view(self.p)

    def one(self) -> dict:
        return next(a for a in self.view()["agents"] if a["id"] == "one")

    def test_a_change_is_handed_once_answered_and_shown_beside_its_job(self):
        out = self.ask()
        self.assertEqual(out["status"], "queued")
        self.assertEqual(self.ask(), {**out, "replayed": True})
        with self.assertRaises(CoreError) as e:
            self.ask("remove")
        self.assertEqual(e.exception.reason, "key_reused")
        asks = self.hand()
        self.assertEqual(asks, [{"id": out["id"], "job": "j1", "do": "pause"}])
        self.assertEqual(self.hand(), [], "handed once, never again by itself")
        self.assertEqual(self.hand("me"), [])
        agents = {a["id"]: a for a in self.view()["agents"]}
        self.assertEqual(agents["one"]["job_changes"]["j1"]["status"], "queued")
        self.answer(asks[0])
        agents = {a["id"]: a for a in self.view()["agents"]}
        self.assertEqual(agents["one"]["job_changes"], {"j1": {"id": out["id"], "do": "pause", "status": "done",
                                                               "reason": None}})
        self.assertEqual(agents["me"]["job_changes"], {})
        self.assertIsNotNone(self.view()["jobs_until"])
        # What was asked for is recorded.
        ev = self.w.store.db.execute("SELECT body_json FROM events WHERE kind = 'own.job'").fetchall()
        self.assertEqual([json.loads(r[0])["do"] for r in ev], ["pause"])

    def test_a_refusal_says_hermess_reason_and_an_unanswered_change_lapses(self):
        self.ask("resume")
        self.answer(self.hand()[0], done=False, reason="Cannot resume: it is done")
        c = self.one()["job_changes"]["j1"]
        self.assertEqual((c["status"], c["reason"]), ("refused", "Cannot resume: it is done"))
        self.ask("run", key="k-00000002")
        a = self.hand()[0]
        with self.assertRaises(CoreError) as e:
            self.ask("remove", key="k-00000003")
        self.assertEqual(e.exception.reason, "job_busy")
        self.w.clock.advance(seconds=121)
        self.listed("one")
        c = self.one()["job_changes"]["j1"]
        self.assertEqual(c["status"], "lapsed")
        with self.assertRaises(CoreError) as e:
            self.answer(a)
        self.assertEqual(e.exception.reason, "not_asked")
        self.w.clock.advance(minutes=11)
        self.listed("one")
        self.assertEqual(self.one()["job_changes"], {})

    def test_the_newest_change_not_handed_yet_wins(self):
        self.ask("pause")
        self.ask("run", key="k-00000002")
        self.assertEqual([a["do"] for a in self.hand()], ["run"])

    def test_a_change_needs_a_listed_job_a_recent_passkey_and_the_hermes_online(self):
        with self.assertRaises(CoreError) as e:
            self.ask(job="j_other")
        self.assertEqual(e.exception.reason, "not_listed")
        with self.assertRaises(CoreError) as e:
            self.ask(gw="me")
        self.assertEqual(e.exception.reason, "not_listed")
        for p in (passkey(self.w, minutes_ago=31), passkey(self.w, method="pairing")):
            with self.subTest(p=p.method), self.assertRaises(CoreError) as e:
                self.ask(p=p)
            self.assertEqual(e.exception.reason, "passkey_needed")
        for body, reason in (({"job": "j1", "do": "delete"}, "bad_job"), ({"job": "../j1", "do": "pause"}, "bad_job"),
                             ({"job": "j1", "do": "pause", "idempotency_key": "short"}, "bad_key")):
            with self.subTest(body=body), self.assertRaises(CoreError) as e:
                self.own.job_set(self.p, "one", {"idempotency_key": "k-00000009", **body})
            self.assertEqual(e.exception.reason, reason)
        # A Bot's jobs are not changed from the app: only his own two gateways take a change.
        with self.assertRaises(CoreError) as e:
            self.ask(gw="rift")
        self.assertEqual(e.exception.reason, "no_gateway")
        # A report older than TASKS_STALE lists nothing.
        self.w.clock.advance(seconds=151)
        self.g.hello("one")
        with self.assertRaises(CoreError) as e:
            self.ask()
        self.assertEqual(e.exception.reason, "not_listed")

    def test_tokens_and_the_switch(self):
        self.ask()
        a = self.hand()[0]
        with self.assertRaises(CoreError) as e:
            self.own.job_post(self.w.ws, "one", self.g.tok["me"], {"id": a["id"], "done": True})
        self.assertEqual(e.exception.status, 401)
        with self.assertRaises(CoreError) as e:
            self.own.job_post(self.w.ws, "one", self.g.tok["one"], {"id": a["id"]})
        self.assertEqual(e.exception.reason, "bad_job")
        self.w.store.set_flag(self.w.ws, "own_chat_write", False, by="tests")
        for call in (lambda: self.ask(key="k-00000005"), lambda: self.answer(a)):
            with self.assertRaises(CoreError) as e:
                call()
            self.assertEqual(e.exception.reason, "own_chat_off")


class Routes(unittest.TestCase):
    def setUp(self):
        tasks_mod._TASKS.clear()
        own_jobs.JOBS.asked.clear()
        own_jobs.JOBS.shown.clear()

    def test_the_routes_over_http(self):
        s = Served(self)
        on(s.w, "own_chat_write", "mobile_read")
        g = Gateways(s.w)
        g.hello("one")
        g.own.tasks(s.w.ws, "one", g.tok["one"], {"tasks": report(jobs=[JOB])})
        sess = s.w.core.auth.issue_session(s.w.ws, method="passkey", device_id=s.w.device())
        h = {"Cookie": f"tw_session={sess.token}", "Origin": ORIGIN, "X-CSRF-Token": sess.csrf}
        status, _, body = s.call("POST", "/own/one/jobs", headers=h,
                                 body={"job": "j1", "do": "run", "idempotency_key": "phone-key-01"})
        self.assertEqual((status, body.get("status")), (200, "queued"), body)
        status, _, body = s.call("POST", "/own/one/claim", headers={"Authorization": f"Bearer {g.tok['one']}"},
                                 body={"limit": 0, "ready": True})
        self.assertEqual([(j["job"], j["do"]) for j in body["jobs"]], [("j1", "run")])
        status, _, _ = s.call("POST", "/own/one/job_done", headers={"Authorization": f"Bearer {g.tok['one']}"},
                              body={"id": body["jobs"][0]["id"], "done": True, "reason": ""})
        self.assertEqual(status, 200)
        status, _, body = s.call("POST", "/own/one/job_done", headers=h, body={"id": "job_xxxxxxxxxx", "done": True})
        self.assertEqual((status, body.get("error")), (403, "not_a_feed"))
        status, _, body = s.call("GET", "/tasks", headers=h)
        one = next(a for a in body["agents"] if a["id"] == "one")
        self.assertEqual(one["job_changes"]["j1"]["status"], "done")


class FakeCore:
    def __init__(self, status=200):
        self.posts, self.status = [], status

    def post(self, op, body):
        self.posts.append((op, json.loads(json.dumps(body))))
        return self.status, {}


class Connector(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_each_change_is_run_through_the_helper_answered_and_the_jobs_read_again_at_once(self):
        script = self.dir / "helper.py"
        script.write_text("import json, sys, pathlib\nreq = json.loads(sys.stdin.read())\n"
                          f"p = pathlib.Path({str(self.dir / 'reqs.jsonl')!r})\n"
                          "p.write_text((p.read_text() if p.exists() else '') + json.dumps(req) + '\\n')\n"
                          "print(json.dumps({'done': req['job'] == 'j1', 'reason': '' if req['job'] == 'j1' "
                          "else 'Hermes has no scheduled job with that id now'}))\n", encoding="utf-8")
        hermes = own_connector.Hermes(["hermes"], [sys.executable, "-I", str(script)], self.dir, "One")
        said, core = [], FakeCore()
        conn = own_connector.OwnConnector.__new__(own_connector.OwnConnector)
        conn.hermes, conn.core, conn.say, conn._tasks_due = hermes, core, said.append, False
        sent = conn.jobs([{"id": "job_aaaaaaaaaa", "job": "j1", "do": "pause"},
                          {"id": "job_bbbbbbbbbb", "job": "j2", "do": "remove"},
                          {"id": "job_cccccccccc", "job": "j1", "do": "explode"}, "junk"])
        self.assertEqual(sent, 2)
        reqs = [json.loads(x) for x in (self.dir / "reqs.jsonl").read_text().splitlines()]
        self.assertEqual(reqs, [{"op": "job", "profile": "", "job": "j1", "do": "pause"},
                                {"op": "job", "profile": "", "job": "j2", "do": "remove"}])
        self.assertEqual(core.posts, [
            ("job_done", {"id": "job_aaaaaaaaaa", "done": True, "reason": ""}),
            ("job_done", {"id": "job_bbbbbbbbbb", "done": False, "reason": "Hermes has no scheduled job with that id now"})])
        self.assertEqual(said, ["PASS job pause: done", "NOTE job remove: Hermes has no scheduled job with that id now"])
        # The next round reads the jobs at once, though the last read was moments ago.
        self.assertTrue(conn._tasks_due)
        conn.clock = lambda: 100.0
        conn._tasks_at, conn._tasks_run = 99.0, None
        conn.hermes = type("H", (), {"tasks": lambda self: {"jobs": []}})()
        self.assertTrue(conn.tasks())
        conn._tasks_run.join(5)
        self.assertFalse(conn._tasks_due)
        self.assertEqual(core.posts[-1][0], "tasks")
        self.assertFalse(conn.tasks(), "and then not again until TASKS_SECONDS")


if __name__ == "__main__":
    unittest.main()
