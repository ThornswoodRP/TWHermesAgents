"""The decision connector's helper runs inside Hermes's own runtime (2 Nov).

Since 30 Oct the Bots' `hermes` command on the VM is a /bin/sh wrapper that
execs another /bin/sh wrapper, which execs Hermes's own Python with a
bootstrap that puts Hermes's source folder first on the path. The connector
took the first wrapper's #! line for the Python and ran room_send.py with
/bin/sh, so every readiness check failed, and that failure read as "could not
check the Bots' gateway": Patrick's answers waited, queued, for good.

Checks that the launcher is followed to the real Python and Hermes's source
folder, that a shell is never taken for a Python, that the helper puts the
folder it is given first on its path, that a helper which does not answer in
JSON is a failure of the helper (with its stderr) and never "not ready", and
the canary's check of it. Everything runs against stand-in Hermes modules in
a scratch folder; nothing reaches a real Hermes.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from thornswood_decisions import connector, own_connector

POSIX = os.name != "nt"
HELPER = Path(connector.__file__).with_name("room_send.py")

# Stand-ins for the Hermes modules room_send.py imports for "ready".
STUB_RUNTIME = textwrap.dedent('''
    import os
    from types import SimpleNamespace
    def gateway_profile_home(home):
        return home
    def discover_gateway_endpoint(home, timeout=2.0):
        state = os.environ.get("TW_STUB_STATE", "ready")
        return SimpleNamespace(state=state, endpoint="ep" if state == "ready" else None, home=str(home))
    def attachable(found):
        return found.endpoint is not None
''')


def stub_hermes(folder: Path) -> Path:
    """A stand-in Hermes source folder: hermes_constants and hermes_cli.gateway_runtime."""
    (folder / "hermes_cli").mkdir(parents=True)
    (folder / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (folder / "hermes_cli" / "gateway_runtime.py").write_text(STUB_RUNTIME, encoding="utf-8")
    (folder / "hermes_constants.py").write_text(
        "from pathlib import Path\ndef get_default_hermes_root():\n    return Path('/home/hermes/.hermes')\n",
        encoding="utf-8")
    return folder


# The VM's two wrappers, as they are since 30 Oct (the bootstrap cut to the parts that matter here).
VM_INNER = """#!/bin/sh
exec {python} -I -c 'import os, re, sys
os.environ.pop('"'"'PYTHONHOME'"'"', None)
sys.path.insert(0, '"'"'{root}'"'"')
{rest}' "$@"
"""


class LauncherIsFollowed(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        path.chmod(0o755)
        return path

    def test_a_sh_wrapper_gives_the_python_it_execs_and_the_folder_its_bootstrap_puts_first(self):
        inner = self.write("inner", VM_INNER.format(
            python="/home/hermes/.hermes/tools/python-3.14.9+20251021-linux-arm64/bin/python3",
            root="/home/hermes/.hermes/hermes-agent", rest="from hermes_cli.main import main\nsys.exit(main())"))
        self.assertEqual(own_connector.hermes_runtime(str(inner)),
                         ("/home/hermes/.hermes/tools/python-3.14.9+20251021-linux-arm64/bin/python3",
                          "/home/hermes/.hermes/hermes-agent"))
        self.assertEqual(connector.hermes_python(str(inner)),
                         "/home/hermes/.hermes/tools/python-3.14.9+20251021-linux-arm64/bin/python3")

    @unittest.skipUnless(POSIX, "the wrapper chain names files by /-paths")
    def test_a_wrapper_that_execs_another_wrapper_is_followed_to_the_end(self):
        inner = self.write("inner", VM_INNER.format(python="/opt/py/bin/python3", root="/srv/hermes-agent",
                                                    rest="pass"))
        outer = self.write("hermes", f'#!/bin/sh\nexec {inner} "$@"\n')
        self.assertEqual(own_connector.hermes_runtime(str(outer)), ("/opt/py/bin/python3", "/srv/hermes-agent"))

    def test_a_venv_script_still_names_its_python_with_no_folder(self):
        cmd = self.write("hermes", "#!/home/hermes/.hermes/hermes-agent/venv/bin/python3\nimport x\n")
        self.assertEqual(own_connector.hermes_runtime(str(cmd)), ("/home/hermes/.hermes/hermes-agent/venv/bin/python3", None))

    def test_a_shell_is_never_taken_for_hermess_python(self):
        for text in ("#!/bin/sh\necho no exec here\n",                 # nothing to follow
                     '#!/bin/sh\nexec /bin/bash -c "hermes" "$@"\n',   # execs another shell
                     '#!/bin/bash\nexec relative/python3 "$@"\n'):     # not by path
            with self.subTest(text=text):
                cmd = self.write("hermes", text)
                with self.assertRaises(SystemExit) as stop:
                    own_connector.hermes_runtime(str(cmd))
                self.assertIn("STOP", str(stop.exception))
                with self.assertRaises(SystemExit):
                    connector.hermes_python(str(cmd))
        for shell in ("/bin/sh", "/usr/bin/bash", "/bin/dash"):
            with self.subTest(shell=shell), self.assertRaises(SystemExit) as stop:
                own_connector.helper_command("hermes", HELPER, python=shell)
            self.assertIn("is a shell", str(stop.exception))

    @unittest.skipUnless(POSIX, "the stand-in launcher is a #! script")
    def test_a_runtime_command_that_names_a_shell_is_refused(self):
        boot = "import runpy; " + own_connector.RUNTIME_ENTRY
        hermes = self.write("hermes", f"#!{sys.executable}\nimport json, sys\n"
                                      f"print(json.dumps(['/bin/sh', '-I', '-c', {boot!r}]))\n")
        with self.assertRaises(SystemExit):
            own_connector.helper_command(str(hermes), HELPER)

    @unittest.skipUnless(POSIX, "the stand-in launcher is a /bin/sh script")
    def test_the_vms_sh_launcher_runs_the_helper_inside_hermess_runtime_and_it_answers_ready(self):
        root = stub_hermes(self.dir / "hermes-agent")
        # The launcher answers --print-runtime-command the way Hermes's does: its Python, its bootstrap.
        # (Read from a file: the bootstrap sits in single quotes in the sh script.)
        answer = self.dir / "runtime.json"
        answer.write_text(json.dumps([sys.executable, "-I", "-c", f"import sys, runpy; sys.path.insert(0, "
                                      f"{str(root)!r}); " + own_connector.RUNTIME_ENTRY]), encoding="utf-8")
        rest = textwrap.dedent(f"""\
            if sys.argv[1:2] == ["--print-runtime-command"]:
                print(open({json.dumps(str(answer))}).read())
                sys.exit(0)
            sys.exit(5)""")
        inner = self.write("inner", VM_INNER.format(python=sys.executable, root=root, rest=rest))
        outer = self.write("hermes", f'#!/bin/sh\nexec {inner} "$@"\n')
        cmd = connector.helper_command(str(outer))
        self.assertEqual(cmd[:3], [sys.executable, "-I", "-c"])
        self.assertIn("room_send.py", cmd[3])
        self.assertEqual(connector.Hermes([str(outer)], cmd, self.dir).check("bramble"), ("ready", ""))

    @unittest.skipUnless(POSIX, "the stand-in launcher is a /bin/sh script")
    def test_an_older_sh_launcher_is_followed_by_hand_and_the_helper_gets_the_folder(self):
        root = stub_hermes(self.dir / "hermes-agent")
        inner = self.write("inner", VM_INNER.format(python=sys.executable, root=root, rest="sys.exit(2)"))
        outer = self.write("hermes", f'#!/bin/sh\nexec {inner} "$@"\n')
        cmd = connector.helper_command(str(outer))
        self.assertEqual(cmd, [sys.executable, "-I", str(HELPER), "--hermes-root", str(root)])
        self.assertEqual(connector.Hermes([str(outer)], cmd, self.dir).check("bramble"), ("ready", ""))


class HelperPutsTheRootFirst(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = stub_hermes(self.dir / "hermes-agent")

    def run_helper(self, *args: str, **env) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-I", str(HELPER), *args], input=b'{"op": "ready", "profile": "bramble"}',
                              capture_output=True, timeout=60, env={**os.environ, **env})

    def test_the_given_folder_is_imported_from(self):
        out = self.run_helper("--hermes-root", str(self.root))
        self.assertEqual(json.loads(out.stdout.decode().strip().splitlines()[-1]), {"ready": True})
        out = self.run_helper("--hermes-root", str(self.root), TW_STUB_STATE="stopped")
        self.assertEqual(json.loads(out.stdout.decode().strip().splitlines()[-1]),
                         {"sent": False, "ready": False, "reason": "the Bots' gateway is stopped"})

    def test_hermess_own_bootstrap_in_the_folder_runs_first(self):
        # On the VM, Hermes's dependencies (ruamel and the rest) are on the path only after hermes_bootstrap.
        deps = self.dir / "deps"
        deps.mkdir()
        (deps / "twdep.py").write_text("OK = True\n", encoding="utf-8")
        (self.root / "hermes_bootstrap.py").write_text(
            f"import sys\nassert sys._hermes_pin_default_home\nsys.path.append({str(deps)!r})\n", encoding="utf-8")
        runtime = self.root / "hermes_cli" / "gateway_runtime.py"
        runtime.write_text("import twdep\n" + runtime.read_text(encoding="utf-8"), encoding="utf-8")
        out = self.run_helper("--hermes-root", str(self.root))
        self.assertEqual(json.loads(out.stdout.decode().strip().splitlines()[-1]), {"ready": True}, out.stderr)
        (self.root / "hermes_bootstrap.py").write_text("", encoding="utf-8")
        self.assertIn(b"No module named 'twdep'", self.run_helper("--hermes-root", str(self.root)).stderr)

    def test_without_the_folder_hermes_is_not_found(self):
        out = self.run_helper()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(b"ModuleNotFoundError", out.stderr)

    def test_a_folder_not_by_full_path_is_refused(self):
        out = self.run_helper("--hermes-root", "hermes-agent")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(b"full path", out.stderr)

    def test_the_connectors_direct_command_carries_the_folder(self):
        cmd = connector.helper_command("hermes", sys.executable, str(self.root))
        self.assertEqual(cmd, [sys.executable, "-I", str(HELPER), "--hermes-root", str(self.root)])
        self.assertEqual(connector.Hermes(["hermes"], cmd, self.dir).check("bramble"), ("ready", ""))


class HelperFailureIsSaidAsOne(unittest.TestCase):
    """A helper that fails is the helper's failure, with its stderr: never "the gateway is not ready"."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def hermes(self, code: str) -> connector.Hermes:
        return connector.Hermes([str(self.dir / "no-such-hermes")], [sys.executable, "-c", code], self.dir)

    def test_a_helper_that_does_not_answer_in_json_is_a_failure_with_its_stderr(self):
        # What /bin/sh -I room_send.py did on the VM: a syntax error on stderr, nothing on stdout.
        h = self.hermes("import sys; sys.stderr.write('room_send.py: 61: Syntax error: \"(\" unexpected\\n'); sys.exit(2)")
        state, why = h.check("bramble")
        self.assertEqual(state, "failed")
        self.assertTrue(why.startswith("the connector's Hermes helper failed (exit 2): "), why)
        self.assertIn('Syntax error: "(" unexpected', why)
        self.assertNotIn("gateway", why)
        self.assertEqual(h.ready("bramble"), (False, why))
        # Nothing was sent: the message waits, and its reason is the helper's failure.
        self.assertEqual(h.to_bot("bramble", "hi"), ("not_ready", "", why))
        # groups.send may have gone before a helper that started stopped: unclear, settled from the room.
        self.assertEqual(h.to_room("54b3ad46-3419-4adb-b60d-c79cb347fcee", "tw-chat.k", "hi"), ("uncertain", "", why))
        self.assertEqual(h.react("bramble", "s", 1, None), ("failed", why))

    def test_hermes_not_on_the_path_is_a_failure_too(self):
        h = connector.Hermes(["hermes"], [sys.executable, "-I", str(HELPER)], self.dir)
        state, why = h.check("bramble")
        self.assertEqual(state, "failed")
        self.assertIn("ModuleNotFoundError", why)

    def test_a_helper_that_cannot_start_is_a_failure(self):
        h = connector.Hermes(["hermes"], [str(self.dir / "no-such-python"), "-I", str(HELPER)], self.dir)
        state, why = h.check("bramble")
        self.assertEqual(state, "failed")
        self.assertTrue(why.startswith("the connector's Hermes helper did not start"), why)
        self.assertEqual(h.to_bot("bramble", "hi")[0], "not_ready")
        self.assertEqual(h.to_room("54b3ad46-3419-4adb-b60d-c79cb347fcee", "tw-chat.k", "hi")[0], "not_ready")

    def test_a_json_answer_of_another_shape_is_a_failure(self):
        self.assertEqual(self.hermes("print('[1]')").check("bramble")[0], "failed")

    def test_a_gateway_that_is_not_running_is_still_not_ready(self):
        h = self.hermes("print('{\"sent\": false, \"ready\": false, \"reason\": \"the Bots\\' gateway is stopped\"}')")
        self.assertEqual(h.check("bramble"), ("not_ready", "the Bots' gateway is stopped"))
        self.assertEqual(h.to_bot("bramble", "hi"), ("not_ready", "", "the Bots' gateway is stopped"))

    def test_a_failing_helper_holds_the_claims_and_says_fail_once(self):
        said = []
        c = connector.Connector(connector.Core("http://127.0.0.1:9/api/v1", "t"),
                                self.hermes("import sys; sys.stderr.write('boom'); sys.exit(1)"), say=said.append)
        c.waiting["bramble"] = "the Bots' gateway is stopped"
        self.assertTrue(c._still_waiting())
        self.assertTrue(c._still_waiting())
        self.assertEqual(said, ["FAIL the connector's Hermes helper failed (exit 1): boom"])
        self.assertEqual(c.waiting, {"bramble": "the connector's Hermes helper failed (exit 1): boom"})

    def test_the_own_connector_says_the_helpers_stderr_too(self):
        h = own_connector.Hermes(["hermes"], [sys.executable, "-c", "import sys; sys.stderr.write('no hermes_constants'); "
                                                                     "sys.exit(1)"], self.dir, "my-hermes")
        state, why = h.ready()
        self.assertEqual(state, "check_failed")
        self.assertIn("no hermes_constants", why)


class TheFleetQueue(unittest.TestCase):
    """2 Nov: Patrick's answer on the #5327 ad18c2c card reached Bramble while the fleet queue turned turns
    away. The turn said it had not started and ended, and the connector called the message sent. A Bot
    Chat message now goes only when the queue would admit its turn, and one whose turn was turned away
    all the same is not_ready: it goes back in the queue unspent."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        profile = self.dir / ".hermes" / "profiles" / "bramble"
        profile.mkdir(parents=True)
        import sqlite3
        con = sqlite3.connect(profile / "state.db")
        con.execute("CREATE TABLE sessions (id TEXT, title TEXT, archived INTEGER, started_at REAL)")
        con.execute("INSERT INTO sessions VALUES ('sess-bc', 'Bot Chat', 0, 1)")
        con.commit()
        con.close()
        self.note = profile.joinpath(*connector.TURNED_AWAY_DIR, "sess-bc.json")
        self.asked = []

    def hermes(self, room=True, turn_away=None):
        """A stand-in hermes command whose turn exits 0, leaving the turn gate's note when turn_away says why."""
        code = "import sys\n"
        if turn_away:
            code += textwrap.dedent(f"""
                import json, os, datetime
                p = {str(self.note)!r}
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "w").write(json.dumps({{"at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                                "reason": {turn_away!r}}}))
            """)
        script = self.dir / "hermes.py"
        script.write_text(code, encoding="utf-8")

        def fleet(bot):
            self.asked.append(bot)
            return (True, "") if room else (False, "1 turn already running; the fleet runs at most 1 at once")
        return connector.Hermes([sys.executable, str(script)], [sys.executable, "-c", "print('{\"ready\": true}')"],
                                self.dir, fleet=fleet)

    def test_no_room_sends_nothing_and_the_message_waits_with_why(self):
        h = self.hermes(room=False)
        why = "the fleet queue has no room for a turn of bramble's: 1 turn already running; the fleet runs at most 1 at once"
        self.assertEqual(h.check("bramble"), ("not_ready", why))
        self.assertEqual(h.to_bot("bramble", "hi"), ("not_ready", "", why))
        self.assertFalse(self.note.exists())
        # The room's gateway has no fleet turn of its own: only a Bot's check asks the queue.
        self.assertEqual(h.check(""), ("ready", ""))
        self.assertEqual(self.asked, ["bramble", "bramble"])

    def test_a_turn_that_ran_is_confirmed(self):
        self.assertEqual(self.hermes().to_bot("bramble", "hi"), ("confirmed", "turn finished", ""))

    def test_a_turn_turned_away_all_the_same_goes_back_unspent_and_the_note_is_taken(self):
        h = self.hermes(turn_away="the broker at /run/tw/policy/policy.sock is not answering")
        self.assertEqual(h.to_bot("bramble", "hi"),
                         ("not_ready", "", "the fleet queue turned the turn away: the broker at "
                                           "/run/tw/policy/policy.sock is not answering"))
        self.assertFalse(self.note.exists())

    def test_an_old_note_from_an_earlier_turn_is_not_this_turns(self):
        self.note.parent.mkdir(parents=True)
        self.note.write_text(json.dumps({"at": "2026-11-02T23:00:00+00:00", "reason": "old"}), encoding="utf-8")
        self.assertEqual(self.hermes().to_bot("bramble", "hi"), ("confirmed", "turn finished", ""))

    def test_without_the_queue_nothing_is_asked(self):
        h = self.hermes()
        h.fleet = None
        self.assertEqual(h.to_bot("bramble", "hi"), ("confirmed", "turn finished", ""))
        self.assertEqual(self.asked, [])

    def test_the_connector_holds_that_bots_messages_while_the_queue_has_no_room(self):
        said = []
        h = self.hermes(room=False)
        c = connector.Connector(connector.Core("http://127.0.0.1:9/api/v1", "t"), h, say=said.append)
        c.waiting["bramble"] = "the fleet queue turned the turn away: x"
        self.assertTrue(c._still_waiting())
        self.assertIn("has no room", c.waiting["bramble"])

    def test_fleet_room_is_off_while_the_brokers_are_off_and_asks_the_policy_socket_when_on(self):
        import copy
        from helpers import policy
        data = copy.deepcopy(policy().data)
        data["brokers"]["ready"] = False
        off = self.dir / "off.json"
        off.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(connector.fleet_room(off))
        data["brokers"]["ready"] = True
        data["brokers"]["sockets"]["policy"]["path"] = str(self.dir / "no.sock")
        on = self.dir / "on.json"
        on.write_text(json.dumps(data), encoding="utf-8")
        room, why = connector.fleet_room(on)("bramble")
        self.assertFalse(room)
        self.assertTrue(why.startswith("tw-policy did not answer"), why)


class CheckReadyCommand(unittest.TestCase):
    """`tw-decide-connector --check-ready <bot>`, which the canary runs as hermes."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.root = stub_hermes(self.dir / "hermes-agent")

    def main(self, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = connector.main(["--hermes", "hermes", "--hermes-python", sys.executable, *args])
        return code, json.loads(out.getvalue().strip().splitlines()[-1])

    def test_ready_exits_0(self):
        self.assertEqual(self.main("--hermes-root", str(self.root), "--check-ready", "bramble"), (0, {"ready": True}))

    def test_a_failed_helper_exits_1_and_says_so(self):
        code, got = self.main("--check-ready", "bramble")
        self.assertEqual(code, 1)
        self.assertIs(got["helper_failed"], True)
        self.assertIn("ModuleNotFoundError", got["reason"])


@unittest.skipUnless(POSIX, "tw-canary is the VM's (it imports pwd)")
class CanaryChecksTheHelper(unittest.TestCase):
    def setUp(self):
        from thornswood_brokers import canary
        self.canary = canary
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.env = self.dir / "tw-decide.env"
        self.env.write_text("TW_DECIDE_ROOM=\nTW_HERMES_BIN=/home/hermes/.local/bin/hermes\n", encoding="utf-8")

    def run_canary(self, answers: list[tuple[int, str]]):
        import pwd
        from types import SimpleNamespace
        me = pwd.getpwuid(os.getuid()).pw_name
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            code, out = answers[min(len(calls), len(answers)) - 1]
            return subprocess.CompletedProcess(argv, code, out, "")
        c = self.canary.Canary(SimpleNamespace(data={"brokers": {"users": {"gateway": me}}}), run=run)
        c.sleep = lambda s: None
        c.connector_helper(env_file=self.env)
        return c.results, calls

    def test_ready_passes(self):
        results, calls = self.run_canary([(0, '{"ready": true}\n')])
        self.assertTrue(results[0][1])
        self.assertIn("bramble", results[0][0])
        self.assertEqual(calls[0][-5:], ["/opt/thornswood-hermes/bin/tw-decide-connector", "--hermes",
                                         "/home/hermes/.local/bin/hermes", "--check-ready", "bramble"])

    def test_a_helper_failure_fails_at_once(self):
        results, calls = self.run_canary([(1, '{"helper_failed": true, "reason": "the connector\'s Hermes helper '
                                               'failed (exit 2): Syntax error"}\n')])
        self.assertFalse(results[0][1])
        self.assertIn("Syntax error", results[0][2])
        self.assertEqual(len(calls), 1)

    def test_a_gateway_still_starting_is_asked_again_then_fails(self):
        starting = (1, '{"sent": false, "ready": false, "reason": "the Bots\' gateway is starting"}\n')
        results, calls = self.run_canary([starting, (0, '{"ready": true}\n')])
        self.assertTrue(results[0][1])
        self.assertEqual(len(calls), 2)
        results, calls = self.run_canary([starting])
        self.assertFalse(results[0][1])
        self.assertEqual(len(calls), 7)

    def test_no_decision_core_no_check(self):
        self.env.unlink()
        results, calls = self.run_canary([(0, "")])
        self.assertEqual((results, calls), ([], []))

    def test_problem_reading(self):
        self.assertIsNone(self.canary.connector_problem(0, '{"ready": true}\n', ""))
        self.assertIsNotNone(self.canary.connector_problem(0, '{"ready": false}\n', ""))
        self.assertIn("Traceback", self.canary.connector_problem(1, "", "Traceback ..."))


if __name__ == "__main__":
    unittest.main()
