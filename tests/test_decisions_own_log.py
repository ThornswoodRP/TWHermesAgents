"""One's connector leaves a record of why it stopped (8 Nov).

One's task runs the connector with pythonw, which has no stdout, so every line it said went nowhere: on
7 Nov One's connector stopped at 7:17 PM and nothing on One said why. Checks:
- with no stdout, each line goes to connector.log beside the connector, with the time; with a stdout it
  goes there as before and no file is written;
- the log is kept to about LOG_BYTES, with the one before it as connector.log.1;
- --supervise writes each stop of the connector with its exit code, and an update as an update;
- a supervisor that stops writes why before it goes, and so does the connector itself, except when it
  stops to load an update.

Everything runs in a scratch folder; nothing starts a real connector, Hermes or process.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import decisions_helpers  # noqa: F401  (puts the repository on the path)
from thornswood_decisions import own_connector


class Proc:
    def __init__(self, code):
        self.code = code

    def wait(self):
        return self.code


class Log(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.log = self.dir / own_connector.LOG
        patches = [mock.patch.object(own_connector, "__file__", str(self.dir / "own_connector.py")),
                   mock.patch.object(own_connector.sys, "stdout", None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def lines(self):
        return [line.split(" ", 1)[1] for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_with_no_stdout_each_line_goes_to_the_log_with_its_time(self):
        own_connector.say("PASS 1 handed to One")
        own_connector.say("NOTE second")
        self.assertEqual(self.lines(), ["PASS 1 handed to One", "NOTE second"])
        stamp = self.log.read_text(encoding="utf-8").split(" ", 1)[0]
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")

    def test_with_a_stdout_it_prints_and_writes_no_file(self):
        out = io.StringIO()
        with mock.patch.object(own_connector.sys, "stdout", out):
            own_connector.say("PASS printed")
        self.assertEqual(out.getvalue(), "PASS printed\n")
        self.assertFalse(self.log.exists())

    def test_the_log_is_kept_to_its_size_with_the_one_before(self):
        self.log.write_text("x" * (own_connector.LOG_BYTES + 1), encoding="utf-8")
        own_connector.say("NOTE after the turn")
        self.assertEqual(self.lines(), ["NOTE after the turn"])
        self.assertEqual((self.dir / (own_connector.LOG + ".1")).stat().st_size, own_connector.LOG_BYTES + 1)

    def test_the_supervisor_writes_each_stop_and_its_exit_code(self):
        codes = iter([own_connector.UPDATE_EXIT, 1, 3221225786])
        own_connector.supervise(["--supervise"], run=lambda argv, **kw: Proc(next(codes)), sleep=lambda s: None,
                                rounds=3, contain=False)
        wait = own_connector.RESTART_SECONDS
        self.assertEqual(self.lines(), [
            "NOTE the connector stopped to load an update; starting the new copy",
            f"NOTE the connector stopped with exit code 1; starting it again in {wait}s",
            f"NOTE the connector stopped with exit code 3221225786; starting it again in {wait}s"])

    def test_a_supervisor_that_stops_writes_why_first(self):
        def run(argv, **kw):
            raise OSError(2, "The system cannot find the file specified")
        with self.assertRaises(OSError):
            own_connector.supervise(["--supervise"], run=run, sleep=lambda s: None, rounds=1, contain=False)
        (line,) = self.lines()
        self.assertTrue(line.startswith("STOP the supervisor: "), line)
        self.assertIn("FileNotFoundError: [Errno 2] The system cannot find the file specified", line)

    def test_a_supervisor_stopped_by_its_job_writes_its_own_words(self):
        with mock.patch.object(own_connector, "WINDOWS", True), \
                mock.patch.object(own_connector, "_job", return_value=object()), \
                mock.patch.object(own_connector, "_in_job",
                                  side_effect=SystemExit("STOP: could not put the connector in its job object")):
            with self.assertRaises(SystemExit):
                own_connector.supervise(["--supervise"], run=lambda argv, **kw: Proc(0), sleep=lambda s: None,
                                        rounds=1)
        self.assertEqual(self.lines(), ["STOP the supervisor: STOP: could not put the connector in its job object"])

    def test_the_connector_writes_why_it_stopped_but_not_for_an_update(self):
        for raised, said in ((RuntimeError("the store went away"), True),
                             (SystemExit(own_connector.UPDATE_EXIT), False),
                             (SystemExit("STOP: the core refused this connector's token"), True)):
            self.log.unlink(missing_ok=True)
            conn = mock.Mock()
            conn.forever.side_effect = raised
            with mock.patch.object(own_connector, "OwnConnector", return_value=conn), \
                    mock.patch.object(own_connector, "helper_command", return_value=["py"]), \
                    mock.patch.object(own_connector, "read_token", return_value="t"):
                with self.assertRaises(type(raised)):
                    own_connector.main(["--gateway", "one", "--hermes", "hermes", "--token", str(self.dir / "t"),
                                        "--core", "https://example.invalid/api/v1", "--hermes-home", str(self.dir)])
            if said:
                (line,) = self.lines()
                self.assertTrue(line.startswith("STOP the connector: "), line)
            else:
                self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
