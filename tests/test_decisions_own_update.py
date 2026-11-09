"""One's own connector keeps itself current, and says what it is doing (Pixel check, 2 Nov).

Patrick's Pixel check found One's chat stuck on "Waiting to send" with One's gateway up, and One's
chats short of their history. The core takes a message only while the connector says the gateway
runs, so the gateway check was not what held it; and One's copy of the connector was the one its
installer put there, which nothing ever updated, so it never had full history. Checks:

- the connector tells the core its commit, how many turns it runs of how many and for how long,
  and an update it is on or could not do; the core says in words why each waiting message waits;
- the core names its release in every claim answer;
- One's copy moves itself to that release from Patrick's clone (main only, both files compiled
  before either is put in place), finishes its turns, takes no new one and stops to be started
  again; a failed update changes nothing, is said, and is not tried again for the same release;
- --supervise starts it again at once after an update and a minute after any other stop.

Everything runs in a scratch test workspace and a scratch git repository; nothing reaches GitHub,
a real Hermes or a real core.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from decisions_helpers import World
from test_decisions_own import SID, Gateways, passkey

from thornswood_decisions import own, own_connector

GIT = shutil.which("git")


class Core:
    """The core as the connector reaches it, with what the connector says it is doing."""

    def __init__(self, g: Gateways, release: str | None):
        self.g, self.release, self.claims = g, release, []

    def post(self, op, body):
        if op != "claim":
            return 200, {}
        self.claims.append(body)
        doing = {k: body.get(k) for k in ("commit", "busy", "parallel", "busy_for", "updating", "update_why")}
        out = self.g.own.claim(self.g.w.ws, "one", self.g.tok["one"], body["limit"], body["ready"], body["why"],
                               body["state"], None, doing)
        return 200, out | {"release": self.release}


class Ready:
    name = "One's Hermes"

    def ready(self):
        return "ready", ""


class FakeUpdater:
    def __init__(self, answers):
        self.answers, self.asked = list(answers), []

    def to(self, release):
        self.asked.append(release)
        return self.answers.pop(0)


A, B, C = "a" * 40, "b" * 40, "c" * 40


class SaysWhatItDoes(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.g = Gateways(self.w)
        self.g.hello("one")
        self.g.feed("one")
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def conn(self, release=None, updater=None, commit=A):
        core = Core(self.g, release)
        c = own_connector.OwnConnector(core, Ready(), self.dir / "state.db", say=lambda _: None,
                                       commit=commit, updater=updater)
        c.feed = lambda: 200
        return c, core

    def mine(self):
        return own.OwnChat(self.w.store).view(self.w.patrick(), "one", SID)["mine"]

    def test_each_claim_says_the_commit_and_the_turns_it_runs(self):
        c, core = self.conn()
        hold = threading.Event()
        self.addCleanup(hold.set)
        for _ in range(3):
            t = threading.Thread(target=hold.wait, daemon=True)
            t.started_at = c.clock() - 125
            t.start()
            c.running.append(t)
        c.once()
        said = core.claims[-1]
        self.assertEqual((said["commit"], said["busy"], said["parallel"], said["limit"]), (A, 3, 3, 0))
        self.assertGreaterEqual(said["busy_for"], 125)

    def test_a_message_waiting_behind_three_running_turns_says_so(self):
        c, _ = self.conn()
        hold = threading.Event()
        self.addCleanup(hold.set)
        for _ in range(3):
            t = threading.Thread(target=hold.wait, daemon=True)
            t.started_at = c.clock() - 300
            t.start()
            c.running.append(t)
        self.g.own.send(passkey(self.w), "one", SID, None, "are you there", "wait-key-0001")
        c.once()
        (m,) = self.mine()
        self.assertEqual(m["status"], "queued")
        self.assertEqual(m["waits"], "One is still answering 3 earlier messages, the oldest started 5 minutes ago; "
                                     "this one goes when one ends")

    def test_a_message_behind_an_earlier_one_in_the_same_chat_says_so(self):
        o = self.g.own
        o.send(passkey(self.w), "one", SID, None, "first", "same-key-0001")
        o.send(passkey(self.w), "one", SID, None, "second", "same-key-0002")
        self.g.hello("one")
        o.claim(self.w.ws, "one", self.g.tok["one"], 3, True, "", "ready", None, {"commit": A, "parallel": 3})
        first, second = self.mine()
        self.assertEqual((first["status"], second["status"]), ("leased", "queued"))
        self.assertNotIn("waits", first)
        self.assertEqual(second["waits"], "One is still answering your earlier message in this chat; this one goes next")

    def test_an_old_copy_that_says_nothing_is_named_as_one(self):
        # One's copy from before 2 Nov sends only limit, ready and why.
        self.g.own.send(passkey(self.w), "one", SID, None, "hello", "old-key-0001")
        self.g.hello("one")
        (m,) = self.mine()
        self.assertEqual(m["waits"], "One's connector is an old copy that does not say what it is doing and does "
                                     "not update itself; it needs its one-time update")
        st = own.OwnChat(self.w.store).chats(self.w.patrick(), "one")["connector"]
        self.assertEqual((st["commit"], st["updating"]), (None, None))

    def test_the_core_names_its_release_in_every_claim_answer(self):
        old = own.RELEASE
        own.RELEASE = B
        try:
            got = self.g.own.claim(self.w.ws, "one", self.g.tok["one"], 0, True, "", "ready", None,
                                   {"commit": A, "parallel": 3})
            self.assertEqual(got["release"], B)
            st = own.OwnChat(self.w.store).chats(self.w.patrick(), "one")["connector"]
            self.assertEqual((st["commit"], st["release"], st["current"]), (A, B, False))
        finally:
            own.RELEASE = old

    def test_the_release_is_read_from_the_release_folder(self):
        here = self.dir / "releases" / C / "thornswood_decisions"
        self.assertEqual(own._release(here), C)
        self.assertIsNone(own._release(self.dir / "src" / "thornswood_decisions"))
        here.mkdir(parents=True)
        self.assertEqual(own_connector.own_commit(here), C)
        (here / "COMMIT").write_text(A + "\n", encoding="ascii")
        self.assertEqual(own_connector.own_commit(here), A)

    def test_an_update_finishes_its_turns_takes_no_new_one_and_stops(self):
        up = FakeUpdater([(True, "")])
        c, core = self.conn(release=B, updater=up)
        hold = threading.Event()
        t = threading.Thread(target=hold.wait, daemon=True)
        t.start()
        c.running.append(t)
        c.once()
        self.assertEqual((up.asked, c.leaving), ([B], B))
        self.g.own.send(passkey(self.w), "one", SID, None, "hello", "upd-key-0001")
        c.once()
        self.assertEqual((core.claims[-1]["limit"], core.claims[-1]["updating"]), (0, B))
        (m,) = self.mine()
        self.assertEqual(m["waits"], "One's connector is updating itself to bbbbbbb; it takes this next")
        hold.set()
        t.join(5)
        with self.assertRaises(SystemExit) as e:
            c.once()
        self.assertEqual(e.exception.code, own_connector.UPDATE_EXIT)

    def test_a_failed_update_is_said_and_not_tried_again_for_the_same_release(self):
        up = FakeUpdater([(False, "git fetch in TWHermesAgents failed (exit 128)"), (True, "")])
        c, core = self.conn(release=B, updater=up)
        c.once()
        c.once()
        self.assertEqual(up.asked, [B])
        self.assertIsNone(c.leaving)
        self.assertEqual(core.claims[-1]["update_why"],
                         "could not update to bbbbbbbbbbbb: git fetch in TWHermesAgents failed (exit 128)")
        st = own.OwnChat(self.w.store).chats(self.w.patrick(), "one")["connector"]
        self.assertIn("could not update", st["update_why"])
        core.release = C
        c.once()
        self.assertEqual((up.asked, c.leaving), ([B, C], C))

    def test_nothing_updates_without_a_clone_or_at_the_same_commit(self):
        c, _ = self.conn(release=B, updater=None)
        c.once()
        self.assertIsNone(c.leaving)
        up = FakeUpdater([])
        c, _ = self.conn(release=A, updater=up)
        c.once()
        c, _ = self.conn(release=None, updater=up)
        c.once()
        self.assertEqual(up.asked, [])


@unittest.skipIf(GIT is None, "needs git")
class UpdatesFromTheClone(unittest.TestCase):
    """A scratch GitHub (a bare repository), Patrick's clone of it, and the connector's folder."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        self.git("init", "-q", "--bare", "-b", "main", str(self.dir / "hub.git"), cwd=self.dir)
        self.work = self.dir / "work"
        self.git("clone", "-q", str(self.dir / "hub.git"), str(self.work), cwd=self.dir)
        self.git("checkout", "-q", "-b", "main")
        self.v1 = self.commit("v1")
        self.git("push", "-q", "origin", "main")
        self.clone = self.dir / "clone"
        self.git("clone", "-q", str(self.dir / "hub.git"), str(self.clone), cwd=self.dir)
        self.folder = self.dir / "own-connector"
        self.folder.mkdir()
        for name in own_connector.UPDATE_FILES:
            (self.folder / name).write_text("VERSION = 'v1'\n", encoding="utf-8")
        (self.folder / "COMMIT").write_text(self.v1, encoding="ascii")
        self.up = own_connector.Updater(self.clone, GIT, self.folder)

    def git(self, *args, cwd=None):
        out = subprocess.run([GIT, *args], cwd=str(cwd or self.work), env=self.env, capture_output=True, text=True,
                             check=True)
        return out.stdout.strip()

    def commit(self, version, connector=None):
        d = self.work / "thornswood_decisions"
        d.mkdir(exist_ok=True)
        (d / "own_connector.py").write_text(connector or f"VERSION = '{version}'\n", encoding="utf-8")
        (d / "room_send.py").write_text(f"VERSION = '{version}'\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", version)
        return self.git("rev-parse", "HEAD")

    def versions(self):
        return [(self.folder / n).read_text(encoding="utf-8") for n in own_connector.UPDATE_FILES]

    def test_it_moves_to_a_release_on_main(self):
        v2 = self.commit("v2")
        self.git("push", "-q", "origin", "main")
        self.assertEqual(self.up.to(v2), (True, ""))
        self.assertEqual(self.versions(), ["VERSION = 'v2'\n"] * 2)
        self.assertEqual((self.folder / "COMMIT").read_text(encoding="ascii"), v2)
        self.assertEqual(own_connector.own_commit(self.folder), v2)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["COMMIT", "own_connector.py", "room_send.py"])

    def moved(self, version, packages):
        """A release that keeps its packages in `packages`, as its deploy/layout.json says."""
        self.git("rm", "-q", "-r", "--ignore-unmatch", "thornswood_decisions")
        d = self.work / packages / "thornswood_decisions"
        d.mkdir(parents=True, exist_ok=True)
        (d / "own_connector.py").write_text(f"VERSION = '{version}'\n", encoding="utf-8")
        (d / "room_send.py").write_text(f"VERSION = '{version}'\n", encoding="utf-8")
        (self.work / "deploy").mkdir(exist_ok=True)
        (self.work / "deploy" / "layout.json").write_text(json.dumps({"packages": packages}), encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", version)
        self.git("push", "-q", "origin", "main")
        return self.git("rev-parse", "HEAD")

    def test_it_takes_its_files_from_where_the_release_keeps_its_packages(self):
        v2 = self.moved("v2", "src")
        self.assertEqual(self.up.to(v2), (True, ""))
        self.assertEqual(self.versions(), ["VERSION = 'v2'\n"] * 2)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["COMMIT", "own_connector.py", "room_send.py"])

    def test_a_layout_that_names_no_folder_inside_the_release_changes_nothing(self):
        self.moved("v2", "src")
        for packages in ("../outside", "/abs", None, 7):
            (self.work / "deploy" / "layout.json").write_text(json.dumps({"packages": packages}), encoding="utf-8")
            self.git("commit", "-q", "-am", f"layout {packages}")
            self.git("push", "-q", "origin", "main")
            bad = self.git("rev-parse", "HEAD")
            self.assertEqual(self.up.to(bad), (False, f"could not read where {bad[:12]} keeps its packages "
                                                      "(deploy/layout.json)"))
            self.assertEqual(self.versions(), ["VERSION = 'v1'\n"] * 2)

    def test_a_commit_that_is_not_on_main_changes_nothing(self):
        self.git("checkout", "-q", "-b", "side")
        side = self.commit("side")
        self.git("push", "-q", "origin", "side")
        done, why = self.up.to(side)
        self.assertFalse(done)
        self.assertEqual(why, f"{side[:12]} is not on main")
        self.assertEqual(self.versions(), ["VERSION = 'v1'\n"] * 2)
        self.assertEqual((self.folder / "COMMIT").read_text(encoding="ascii"), self.v1)

    def test_a_copy_that_does_not_compile_changes_nothing(self):
        bad = self.commit("v3", connector="def broken(:\n")
        self.git("push", "-q", "origin", "main")
        done, why = self.up.to(bad)
        self.assertFalse(done)
        self.assertEqual(why, "the update stopped (SyntaxError)")
        self.assertEqual(self.versions(), ["VERSION = 'v1'\n"] * 2)

    def test_a_fetch_that_fails_changes_nothing(self):
        self.git("remote", "set-url", "origin", str(self.dir / "gone.git"), cwd=self.clone)
        done, why = self.up.to("d" * 40)
        self.assertFalse(done)
        self.assertTrue(why.startswith("git fetch in clone failed"), why)
        self.assertEqual(self.versions(), ["VERSION = 'v1'\n"] * 2)

    def test_git_never_prompts(self):
        seen = []

        def run(argv, **kw):
            seen.append((argv, kw))
            return subprocess.CompletedProcess(argv, 128, b"", b"")

        own_connector.Updater(self.clone, GIT, self.folder, run=run).to("d" * 40)
        argv, kw = seen[0]
        self.assertEqual(argv[1:3], ["-c", "credential.interactive=never"])
        self.assertEqual((kw["env"]["GIT_TERMINAL_PROMPT"], kw["env"]["GCM_INTERACTIVE"]), ("0", "never"))
        self.assertIs(kw["stdin"], subprocess.DEVNULL)


class Supervises(unittest.TestCase):
    def test_it_starts_again_at_once_after_an_update_and_a_minute_after_any_other_stop(self):
        codes, slept, started = [own_connector.UPDATE_EXIT, 1, 0], [], []

        class Proc:
            def __init__(self, argv, **kw):
                started.append(argv)
                self.code = codes.pop(0)
                self._handle = 0

            def wait(self):
                return self.code

        own_connector.supervise(["--supervise", "--gateway", "one"], run=Proc, sleep=slept.append, rounds=3,
                                contain=False)
        self.assertEqual(slept, [own_connector.RESTART_SECONDS] * 2)
        self.assertEqual(started[0], [sys.executable, "-I", str(Path(own_connector.__file__).resolve()),
                                      "--gateway", "one"])
        self.assertEqual(len(started), 3)

    @unittest.skipUnless(os.name == "nt", "a Windows job object")
    def test_stopping_the_supervisor_ends_the_connector_with_it(self):
        job = own_connector._job()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        own_connector._in_job(job, proc)
        job[0].CloseHandle(job[1])
        self.assertIsNotNone(proc.wait(20))


if __name__ == "__main__":
    unittest.main()
