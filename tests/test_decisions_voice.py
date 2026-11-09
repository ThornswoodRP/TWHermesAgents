"""Voice in the phone app (Patrick, 2 Nov: dictation with the VM's Whisper, read aloud, voice chat, "Hey Hermes").

The phone posts a recording to the core (POST /voice/transcribe); the core hands it over a Unix socket to the
voice worker, which runs inside Hermes's runtime and writes it down with Hermes's own local Whisper. Here:
- the worker: its wire, the words back, the recording deleted at once, nothing but sizes and times in its log,
  only the core's user served, every bad request answered, Hermes's own failure said as it is;
- the worker's link to Hermes: only the local Whisper (transcribe_audio_local_fallback), never a provider;
- the core's side: only Patrick, only a recording's types, the size bound, and each failure as its own word;
- over HTTP: the recording as its own bytes, with the CSRF token, and only while the phone app is on;
- the deploy: the worker as the Bots' hermes user, its socket's group the core's alone, and the install's check.
A stand-in transcriber does the writing down; nothing here loads a model or reaches a real Hermes.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from decisions_helpers import World
from test_decisions_api import ORIGIN, Served

from thornswood_decisions import voice_worker
from thornswood_decisions.store import CoreError
from thornswood_decisions.voice import MAX_AUDIO, Voice

REPO = Path(__file__).resolve().parent.parent
AUDIO = b"\x1aE\xdf\xa3" + b"\x00" * 3000  # the start of a WebM file, then nothing: the stand-in never decodes it


class Transcriber:
    """Stands in for Hermes's local Whisper: says what it was given, and answers `words`."""

    def __init__(self, words="hey hermes what is on the board", fail=None, boom=None):
        self.words, self.fail, self.boom = words, fail, boom
        self.seen = []

    def __call__(self, path):
        p = Path(path)
        self.seen.append((p.suffix, p.read_bytes(), oct(p.parent.stat().st_mode & 0o777)))
        if self.boom:
            raise self.boom
        if self.fail:
            return {"success": False, "transcript": "", "error": self.fail}
        return {"success": True, "transcript": "  " + self.words + "\n", "provider": "local"}


# The voice worker runs only on the VM (Linux): it checks its caller by the Unix socket's peer credentials
# (SO_PEERCRED) and a uid, which Windows has not; One never runs it.
ON_THE_VM = unittest.skipIf(os.name == "nt" or not hasattr(socket, "SO_PEERCRED"),
                            "the voice worker runs only on the VM's Linux (Unix socket peer credentials)")


class Running:
    """A voice worker on a socket in a scratch folder, served on a thread."""

    def __init__(self, test, transcriber=None, allow_uid=None, whisper=lambda: "faster-whisper", prepare=lambda: "base"):
        self.dir = Path(tempfile.mkdtemp(prefix="tw-voice-test-"))
        test.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = str(self.dir / "voice.sock")
        self.t = transcriber or Transcriber()
        self.worker = voice_worker.Worker(self.t, allow_uid=os.getuid() if allow_uid is None else allow_uid,
                                          scratch=self.dir / "scratch", whisper=whisper, prepare=prepare)
        test.addCleanup(self.worker.close)
        threading.Thread(target=self._serve, daemon=True).start()
        for _ in range(500):  # until it takes a connection
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(self.path)
                break
            except OSError:
                threading.Event().wait(0.01)

    def _serve(self):
        try:
            self.worker.serve(self.path)
        except OSError:
            pass

    def raw(self, data: bytes) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(self.path)
            try:
                s.sendall(data)
                s.shutdown(socket.SHUT_WR)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the worker answers a stranger before reading a byte and closes; its answer is still there to read
            out = b""
            while not out.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
        return json.loads(out)


def patrick():
    return types.SimpleNamespace(kind="patrick", label="Patrick")


@ON_THE_VM
class TheWorker(unittest.TestCase):
    def setUp(self):
        self.r = Running(self)

    def test_a_recording_comes_back_as_its_words_and_is_deleted_at_once(self):
        out = self.r.raw(json.dumps({"op": "transcribe", "type": "audio/webm", "size": len(AUDIO)}).encode() + b"\n" + AUDIO)
        self.assertEqual(out, {"text": "hey hermes what is on the board"})
        suffix, body, mode = self.r.t.seen[0]
        self.assertEqual((suffix, body, mode), (".webm", AUDIO, "0o700"))
        self.assertEqual(list((self.r.dir / "scratch").iterdir()), [], "the recording was kept")

    def test_each_type_is_written_with_its_own_ending(self):
        for kind, suffix in (("audio/ogg", ".ogg"), ("audio/mp4", ".m4a"), ("audio/wav", ".wav"), ("audio/mpeg", ".mp3")):
            self.r.raw(json.dumps({"op": "transcribe", "type": kind, "size": 4}).encode() + b"\n" + b"abcd")
            self.assertEqual(self.r.t.seen[-1][0], suffix)

    def test_the_log_has_sizes_and_times_never_the_words(self):
        with self.assertLogs("tw-decide-voice", level="INFO") as logs:
            self.r.raw(json.dumps({"op": "transcribe", "type": "audio/webm", "size": len(AUDIO)}).encode() + b"\n" + AUDIO)
        said = "\n".join(logs.output)
        self.assertIn(f"{len(AUDIO)} bytes", said)
        self.assertNotIn("board", said)
        self.assertNotIn("hermes", said.replace("tw-decide-voice", ""))

    def test_check_says_which_whisper(self):
        self.assertEqual(self.r.raw(b'{"op": "check"}\n'), {"whisper": "faster-whisper"})

    def test_prepare_loads_hermess_model_and_says_which(self):
        self.assertEqual(self.r.raw(b'{"op": "prepare"}\n'), {"model": "base"})

        def no_model():
            raise RuntimeError("Hermes has no faster-whisper to load a model with")
        r = Running(self, prepare=no_model)
        self.assertEqual(r.raw(b'{"op": "prepare"}\n'),
                         {"error": "whisper_failed", "reason": "RuntimeError: Hermes has no faster-whisper to load a model with"})

    def test_every_bad_request_is_answered_and_none_reaches_whisper(self):
        cases = [
            (b"not json\n", "bad_request"),
            (b'{"op": "delete"}\n', "bad_request"),
            (b'{"op": "transcribe", "type": "video/mp4", "size": 4}\nabcd', "bad_type"),
            (b'{"op": "transcribe", "type": "audio/webm", "size": 0}\n', "too_big"),
            (json.dumps({"op": "transcribe", "type": "audio/webm", "size": MAX_AUDIO + 1}).encode() + b"\n", "too_big"),
            (b'{"op": "transcribe", "type": "audio/webm", "size": true}\na', "too_big"),
            (b'{"op": "transcribe", "type": "audio/webm", "size": 100}\nonly some', "bad_request"),
            (b"x" * 2000, "bad_request"),
        ]
        for data, error in cases:
            self.assertEqual(self.r.raw(data).get("error"), error, data[:60])
        self.assertEqual(self.r.t.seen, [])

    def test_hermess_own_failure_is_said_as_it_is(self):
        r = Running(self, Transcriber(fail="faster-whisper is not installed"))
        out = r.raw(b'{"op": "transcribe", "type": "audio/webm", "size": 4}\nabcd')
        self.assertEqual(out, {"error": "whisper_failed", "reason": "faster-whisper is not installed"})
        r = Running(self, Transcriber(boom=RuntimeError("the model did not load")))
        out = r.raw(b'{"op": "transcribe", "type": "audio/webm", "size": 4}\nabcd')
        self.assertEqual(out, {"error": "whisper_failed", "reason": "RuntimeError: the model did not load"})
        self.assertEqual(list((r.dir / "scratch").iterdir()), [], "a failed recording was kept")

    def test_only_the_cores_user_is_served(self):
        r = Running(self, allow_uid=os.getuid() + 1)
        self.assertEqual(r.raw(b'{"op": "check"}\n').get("error"), "not_the_core")
        self.assertEqual(r.t.seen, [])

    def test_the_socket_is_its_group_s_and_no_one_else_s(self):
        self.assertEqual(oct(os.stat(self.r.path).st_mode & 0o777), "0o660")


class HermessOwnWhisper(unittest.TestCase):
    """The worker asks Hermes for its local Whisper only: no provider, no install, no cloud fallback."""

    def fake_tools(self, **attrs):
        tools = types.ModuleType("tools")
        tt = types.ModuleType("tools.transcription_tools")
        for k, v in attrs.items():
            setattr(tt, k, v)
        tools.transcription_tools = tt
        return mock.patch.dict(sys.modules, {"tools": tools, "tools.transcription_tools": tt})

    def test_transcribe_is_the_local_fallback_and_nothing_else(self):
        called = []
        cloud = mock.Mock(side_effect=AssertionError("a provider was asked"))
        with self.fake_tools(transcribe_audio_local_fallback=lambda p: called.append(p) or {"success": True, "transcript": "hi"},
                             transcribe_audio=cloud):
            self.assertEqual(voice_worker.hermes_transcribe("/x.webm"), {"success": True, "transcript": "hi"})
        self.assertEqual(called, ["/x.webm"])
        cloud.assert_not_called()

    def test_which_whisper(self):
        with self.fake_tools(_HAS_FASTER_WHISPER=True):
            self.assertEqual(voice_worker.hermes_whisper(), "faster-whisper")
        with self.fake_tools(_HAS_FASTER_WHISPER=False, _has_local_command=lambda: True):
            self.assertEqual(voice_worker.hermes_whisper(), "the local whisper command")
        with self.fake_tools(_HAS_FASTER_WHISPER=False, _has_local_command=lambda: False):
            self.assertIsNone(voice_worker.hermes_whisper())

    def test_prepare_is_hermess_own_model_load_with_its_stt_local_settings(self):
        loaded = []
        common = types.ModuleType("tools.transcription_common")
        common.DEFAULT_LOCAL_MODEL = "base"
        local = {"model": "Small", "device": "cpu"}
        with self.fake_tools(_HAS_FASTER_WHISPER=True, _load_stt_config=lambda: {"local": local},
                             _normalize_local_model=lambda m: m.lower(),
                             _get_or_load_local_model=lambda name, cfg: loaded.append((name, cfg))), \
                mock.patch.dict(sys.modules, {"tools.transcription_common": common}):
            self.assertEqual(voice_worker.hermes_prepare(), "small")
        self.assertEqual(loaded, [("small", local)])
        with self.fake_tools(_HAS_FASTER_WHISPER=True, _load_stt_config=lambda: {}, _normalize_local_model=lambda m: m,
                             _get_or_load_local_model=lambda name, cfg: loaded.append((name, cfg))), \
                mock.patch.dict(sys.modules, {"tools.transcription_common": common}):
            self.assertEqual(voice_worker.hermes_prepare(), "base")
        with self.fake_tools(_HAS_FASTER_WHISPER=False), mock.patch.dict(sys.modules, {"tools.transcription_common": common}):
            with self.assertRaises(RuntimeError):
                voice_worker.hermes_prepare()


@ON_THE_VM
class TheCoresSide(unittest.TestCase):
    def setUp(self):
        self.r = Running(self)
        self.v = Voice(self.r.path)

    def refused(self, fn, status, reason):
        with self.assertRaises(CoreError) as e:
            fn()
        self.assertEqual((e.exception.status, e.exception.reason), (status, reason))

    def test_patricks_recording_comes_back_as_words(self):
        self.assertEqual(self.v.transcribe(patrick(), "audio/webm;codecs=opus", AUDIO),
                         {"text": "hey hermes what is on the board"})
        self.assertEqual(self.v.check(), "faster-whisper")
        self.assertEqual(self.v.prepare(), "base")

    def test_only_patrick_and_only_a_recording(self):
        bot = types.SimpleNamespace(kind="service", label="Bramble")
        self.refused(lambda: self.v.transcribe(bot, "audio/webm", AUDIO), 403, "not_patrick")
        self.refused(lambda: self.v.transcribe(patrick(), "text/html", AUDIO), 415, "bad_type")
        self.refused(lambda: self.v.transcribe(patrick(), "audio/webm", b""), 400, "no_audio")
        self.refused(lambda: self.v.transcribe(patrick(), "audio/webm", b"x" * (MAX_AUDIO + 1)), 413, "too_big")
        self.assertEqual(self.r.t.seen, [])

    def test_each_failure_is_its_own_word(self):
        self.refused(lambda: Voice(str(self.r.dir / "none.sock")).transcribe(patrick(), "audio/webm", AUDIO), 503, "voice_off")
        failing = Voice(Running(self, Transcriber(fail="no model")).path)
        with self.assertRaises(CoreError) as e:
            failing.transcribe(patrick(), "audio/webm", AUDIO)
        self.assertEqual((e.exception.status, e.exception.reason), (502, "voice_failed"))
        self.assertIn("no model", str(e.exception))

    def test_a_worker_that_does_not_answer_in_time_is_voice_slow(self):
        quiet = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(quiet.close)
        path = str(self.r.dir / "quiet.sock")
        quiet.bind(path)
        quiet.listen(1)
        with mock.patch("thornswood_decisions.voice.TIMEOUT", 0.2):
            self.refused(lambda: Voice(path).transcribe(patrick(), "audio/webm", AUDIO), 504, "voice_slow")


class AnAnswerShowsAtOnce(unittest.TestCase):
    """A turn's result drops the chat's newest page from the core's memory (and any ask for it already handed
    out), so the next read asks the connector again and has the answer; older pages are kept."""

    def setUp(self):
        from thornswood_decisions.own import PAGES
        self.pages = PAGES
        self.w = World()
        self.addCleanup(self.w.close)

    def hold(self, key):
        self.pages.ask(key, self.w.clock())
        rid = self.pages.asked[key]["id"]
        self.pages.answer(key[0], key[1], rid, {"messages": []}, self.w.clock())
        self.addCleanup(lambda: self.pages.held.pop(key, None))

    def test_a_bot_chat_turn(self):
        from datetime import timedelta
        from thornswood_decisions.chat import PAGES_GW, Chat
        from thornswood_decisions.feeds import Feeds
        w = self.w
        w.store.set_flag(w.ws, "mobile_chat_write", True, by="t")
        w.core.bind_bot(w.ws, "rift", "rift", by="t")
        tc = Feeds(w.store).new_token(w.ws, "connector", by="t")
        mid = Chat(w.store).send(w.patrick(), "rift", "what is on the board?", "voice-key-0001")["message_id"]
        w.store.db.execute("UPDATE chat_messages SET status = 'leased', lease_until = ? WHERE id = ?",
                           ((w.clock() + timedelta(minutes=5)).isoformat(), mid))
        newest, older = (w.ws, PAGES_GW, "bot", "rift", None, ""), (w.ws, PAGES_GW, "bot", "rift", 40, "")
        other = (w.ws, PAGES_GW, "bot", "echo", None, "")
        for k in (newest, older, other):
            self.hold(k)
        asked = (w.ws, PAGES_GW, "room", "rift", None, "")
        self.pages.ask(asked, w.clock())
        self.addCleanup(lambda: self.pages.asked.pop(asked, None))
        Chat(w.store).result(w.ws, tc, mid, "confirmed")
        self.assertIsNone(self.pages.get(newest, w.clock()))
        self.assertNotIn(asked, self.pages.asked)
        self.assertIsNotNone(self.pages.get(older, w.clock()))
        self.assertIsNotNone(self.pages.get(other, w.clock()))

    def test_a_turn_with_his_own_hermes(self):
        from datetime import timedelta
        from test_decisions_own import SID, Gateways, passkey
        w = self.w
        g = Gateways(w)
        g.hello("one")
        g.feed("one")
        mid = g.own.send(passkey(w), "one", SID, None, "what is on the board?", "voice-key-0002")["message_id"]
        w.store.db.execute("UPDATE own_messages SET status = 'leased', lease_until = ? WHERE id = ?",
                           ((w.clock() + timedelta(minutes=5)).isoformat(), mid))
        newest, older = (w.ws, "one", "messages", SID, None, ""), (w.ws, "one", "messages", SID, 12, "")
        for k in (newest, older):
            self.hold(k)
        g.own.result(w.ws, "one", g.tok["one"], mid, "confirmed")
        self.assertIsNone(self.pages.get(newest, w.clock()))
        self.assertIsNotNone(self.pages.get(older, w.clock()))


@ON_THE_VM
class OverHttp(unittest.TestCase):
    def setUp(self):
        self.s = Served(self)
        self.w = self.s.w
        self.r = Running(self)
        self.s.api.voice_socket = self.r.path
        self.w.store.set_flag(self.w.ws, "mobile_read", True, by="tests")
        sess = self.w.core.auth.issue_session(self.w.ws, method="passkey", device_id=self.w.device())
        self.h = {"Cookie": f"tw_session={sess.token}", "Origin": ORIGIN, "X-CSRF-Token": sess.csrf}

    def post(self, body, headers):
        con = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=10)
        try:
            con.request("POST", "/api/v1/voice/transcribe", body=body, headers=headers)
            resp = con.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        finally:
            con.close()

    def test_the_recording_goes_as_its_bytes_and_the_words_come_back(self):
        status, body = self.post(AUDIO, {**self.h, "Content-Type": "audio/webm;codecs=opus"})
        self.assertEqual((status, body), (200, {"text": "hey hermes what is on the board"}))
        self.assertEqual(self.r.t.seen[0][1], AUDIO)

    def test_without_the_csrf_token_nothing_is_written_down(self):
        h = {k: v for k, v in self.h.items() if k != "X-CSRF-Token"}
        status, body = self.post(AUDIO, {**h, "Content-Type": "audio/webm"})
        self.assertEqual((status, body.get("error")), (403, "csrf"))
        self.assertEqual(self.r.t.seen, [])

    def test_signed_out_or_with_the_phone_app_off_nothing_is_written_down(self):
        status, body = self.post(AUDIO, {"Origin": ORIGIN, "Content-Type": "audio/webm"})
        self.assertEqual(status, 401, body)
        self.w.store.set_flag(self.w.ws, "mobile_read", False, by="tests")
        status, body = self.post(AUDIO, {**self.h, "Content-Type": "audio/webm"})
        self.assertEqual((status, body.get("error")), (423, "mobile_read_off"))
        self.assertEqual(self.r.t.seen, [])

    def test_more_than_four_megabytes_is_refused_before_it_is_read(self):
        con = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=10)
        self.addCleanup(con.close)
        con.putrequest("POST", "/api/v1/voice/transcribe")
        for k, v in {**self.h, "Content-Type": "audio/webm", "Content-Length": str(MAX_AUDIO + 1)}.items():
            con.putheader(k, v)
        con.endheaders()  # and not one byte of it
        resp = con.getresponse()
        self.assertEqual((resp.status, json.loads(resp.read()).get("error")), (413, "too_big"))
        self.assertEqual(self.r.t.seen, [])

    def test_the_worker_refusing_early_is_said_as_its_refusal(self):
        self.s.api.voice_socket = Running(self, allow_uid=os.getuid() + 1).path
        status, body = self.post(AUDIO * 300, {**self.h, "Content-Type": "audio/webm"})
        self.assertEqual((status, body.get("error")), (502, "voice_failed"))
        self.assertIn("only the decision core", body.get("message", ""))


class TheDeploy(unittest.TestCase):
    def read(self, name):
        return (REPO / "deploy" / "decisions" / name).read_text()

    def test_the_worker_runs_as_hermes_with_its_socket_the_cores_alone(self):
        unit = self.read("tw-decide-voice.service")
        for line in ("User=hermes", "Group=tw-decide-voice", "RuntimeDirectory=tw/decide-voice", "RuntimeDirectoryMode=0750",
                     "UMask=0077", "NoNewPrivileges=yes", "PrivateTmp=yes"):
            self.assertIn(line + "\n", unit)
        self.assertIn("SupplementaryGroups=tw-decide-voice\n", self.read("tw-decide.service"))
        launcher = self.read("tw-decide-voice")
        self.assertIn("helper_command(a.hermes, worker)", launcher)
        self.assertIn('default="tw-decide"', launcher)

    def test_the_install_puts_it_in_and_checks_the_socket_and_whisper(self):
        sh = self.read("install.sh")
        self.assertIn("tw-decide-voice tw-decide-connector", sh)
        self.assertIn("tw-decide-voice.service \\", sh)
        self.assertIn("tw-decide-live.service tw-decide-voice.service", sh)  # stopped with the core's workers
        self.assertIn('[[ "$members" == tw-decide ]]', sh)
        self.assertIn('"660 tw-decide-voice"', sh)
        self.assertIn("Voice().$1()", sh)
        self.assertIn("voice_ask check", sh)

    def test_the_install_puts_hermess_own_whisper_in_when_it_is_missing_and_loads_its_model(self):
        """3:55 PM, 2 Nov: the install only checked for a local Whisper, so with none every install stopped."""
        sh = self.read("install.sh")
        step = sh[sh.index("# 14. Dictation"):]
        # Only when dictation finds none, with Hermes's own package manager, as its install's owner.
        self.assertIn('if [[ -z "$whisper" ]]; then', step)
        self.assertIn('runuser -l "$hermes_owner" -c "$(printf \'%q\' "$hermes_bin") pm install --extra stt-whisper"', step)
        self.assertIn('hermes_owner="$(stat -c %U "$hermes_dir")"', step)
        self.assertNotIn("pip install", step)
        self.assertNotIn("uv pip", step)
        self.assertLess(step.index("pm install --extra stt-whisper"), step.index('ok "dictation uses Hermes\'s local Whisper'))
        self.assertIn("still finds no local Whisper", step)
        self.assertIn('model="$(voice_ask prepare)"', step)


class TheApp(unittest.TestCase):
    """What the phone does, read from the app's source: the browser check drives it (decisions_phone_check.js)."""

    def setUp(self):
        self.js = (REPO / "src" / "thornswood_decisions" / "app" / "app.js").read_text()

    def test_hey_hermes_is_off_until_turned_on_and_only_in_front(self):
        self.assertIn("const WAKE_KEY = 'hermes.wake.v1'", self.js)
        self.assertIn("return localStorage.getItem(WAKE_KEY) === '1'", self.js)
        self.assertIn("document.visibilityState !== 'visible'", self.js)

    def test_hiding_the_app_stops_listening_and_reading(self):
        self.assertIn("// Hidden or locked: no listening and nothing read out.\n    voiceStop(false)", self.js)
        self.assertIn("addEventListener('pagehide', () => voiceStop(false))", self.js)

    def test_the_words_go_only_to_the_core(self):
        self.assertIn("fetch(API + '/voice/transcribe'", self.js)
        self.assertNotIn("SpeechRecognition", self.js)


if __name__ == "__main__":
    unittest.main()
