"""Dictation for the phone app: speech to words with the VM's own Whisper, run inside Hermes's runtime.

Patrick, 2 Nov: voice in the app, with "the VM's Whisper", and no paid voice or model service. This runs
as the Bots' `hermes` user (tw-decide-voice.service) with Hermes's own Python, started by
`tw-decide-voice` the way the connector starts its helper (own_connector.helper_command). It listens on one
Unix socket that only the decision core may use (its group, and the caller's uid checked from the socket's
peer credentials), takes one recording at a time, and answers its words. The words come from Hermes's
local Whisper only (tools.transcription_tools.transcribe_audio_local_fallback: faster-whisper, or the local
whisper command, already installed; it never installs anything and never falls through to a cloud
provider), with the model Hermes's own `stt.local.model` names. The recording is written to a folder only
this process can read and is deleted as soon as its words are back. Nothing is logged but the size, how
long it took, and a failure's reason; never the words.

The wire, one request per connection: one JSON line {"op": "transcribe", "type": <audio type>, "size": n},
then exactly n bytes of audio. The answer is one JSON line: {"text": ...} or {"error": ..., "reason": ...}.
{"op": "check"} answers {"whisper": <which local Whisper Hermes has, or null>}, for the install.
{"op": "prepare"} loads the model Hermes's `stt.local.model` names, as Hermes's own first transcription
would, downloading it the first time, and keeps it loaded: the install asks it once, so Patrick's first
recording does not wait on a download. It answers {"model": <name>} or {"error": ..., "reason": ...}.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger("tw-decide-voice")

MAX_AUDIO = 4 * 1024 * 1024
MAX_HEADER = 1024
TYPES = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/mpeg": ".mp3", "audio/wav": ".wav",
         "audio/x-wav": ".wav", "audio/aac": ".aac"}
READ_TIMEOUT = 30


def hermes_transcribe(path: str) -> dict:
    """Hermes's own local Whisper, never a cloud provider (see the module's words)."""
    from tools.transcription_tools import transcribe_audio_local_fallback
    return transcribe_audio_local_fallback(path)


def hermes_whisper() -> str | None:
    """Which local Whisper Hermes has here, without loading or installing anything."""
    from tools import transcription_tools as t
    if getattr(t, "_HAS_FASTER_WHISPER", False):
        return "faster-whisper"
    has_command = getattr(t, "_has_local_command", None)
    return "the local whisper command" if has_command and has_command() else None


def hermes_prepare() -> str:
    """Hermes's own model load for local Whisper, with its stt.local settings: the cached model a
    transcription then uses (tools.transcription_tools._get_or_load_local_model, at the pinned commit)."""
    from tools import transcription_tools as t
    from tools.transcription_common import DEFAULT_LOCAL_MODEL
    if not getattr(t, "_HAS_FASTER_WHISPER", False):
        raise RuntimeError("Hermes has no faster-whisper to load a model with")
    local = t._load_stt_config().get("local") or {}
    name = t._normalize_local_model(local.get("model", DEFAULT_LOCAL_MODEL))
    t._get_or_load_local_model(name, local)
    return name


def peer_uid(conn: socket.socket) -> int | None:
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except (OSError, AttributeError):
        return None
    return struct.unpack("3i", creds)[1]


def _read_exact(conn: socket.socket, n: int) -> bytes:
    parts, got = [], 0
    while got < n:
        chunk = conn.recv(min(65536, n - got))
        if not chunk:
            raise ConnectionError("the request ended early")
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def _read_line(conn: socket.socket) -> bytes:
    line = b""
    while not line.endswith(b"\n"):
        chunk = conn.recv(1)
        if not chunk:
            raise ConnectionError("the request ended early")
        line += chunk
        if len(line) > MAX_HEADER:
            raise ValueError("the request's first line is too long")
    return line


class Worker:
    def __init__(self, transcribe=hermes_transcribe, allow_uid: int | None = None, scratch: Path | None = None,
                 whisper=hermes_whisper, prepare=hermes_prepare):
        self.transcribe, self.whisper, self.prepare = transcribe, whisper, prepare
        self.allow_uid = allow_uid
        self.scratch = scratch or Path(tempfile.mkdtemp(prefix="tw-voice-"))
        self.scratch.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.scratch, 0o700)
        self.one_at_a_time = threading.Lock()  # one Whisper model, one recording at a time

    def answer(self, conn: socket.socket) -> dict:
        if self.allow_uid is not None and peer_uid(conn) != self.allow_uid:
            return {"error": "not_the_core", "reason": "only the decision core asks for words"}
        try:
            head = json.loads(_read_line(conn))
        except (ValueError, ConnectionError):
            return {"error": "bad_request", "reason": "the first line is one JSON object"}
        if isinstance(head, dict) and head.get("op") == "check":
            try:
                return {"whisper": self.whisper()}
            except Exception as exc:
                return {"whisper": None, "reason": f"{type(exc).__name__}: {exc}"[:300]}
        if isinstance(head, dict) and head.get("op") == "prepare":
            with self.one_at_a_time:
                try:
                    return {"model": self.prepare()}
                except Exception as exc:
                    return {"error": "whisper_failed", "reason": f"{type(exc).__name__}: {exc}"[:300]}
        if not isinstance(head, dict) or head.get("op") != "transcribe":
            return {"error": "bad_request", "reason": "the ops are check, prepare and transcribe"}
        suffix = TYPES.get(str(head.get("type", "")).split(";")[0].strip().lower())
        size = head.get("size")
        if suffix is None:
            return {"error": "bad_type", "reason": "a recording is webm, ogg, mp4, mp3, wav or aac"}
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_AUDIO:
            return {"error": "too_big", "reason": f"a recording is at most {MAX_AUDIO // (1024 * 1024)} MB"}
        try:
            audio = _read_exact(conn, size)
        except (ConnectionError, OSError):
            return {"error": "bad_request", "reason": "the recording did not come whole"}
        with self.one_at_a_time:
            fd, path = tempfile.mkstemp(dir=self.scratch, suffix=suffix)
            started = time.monotonic()
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(audio)
                try:
                    got = self.transcribe(path)
                except Exception as exc:  # Hermes's own code failed: say so, never a guess
                    got = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
            finally:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
            took = time.monotonic() - started
        if not isinstance(got, dict) or not got.get("success"):
            reason = str((got or {}).get("error") or "Whisper gave no words")[:300]
            logger.warning("a recording of %d bytes failed after %.1f s: %s", size, took, reason)
            return {"error": "whisper_failed", "reason": reason}
        logger.info("a recording of %d bytes took %.1f s", size, took)
        return {"text": str(got.get("transcript") or "").strip()[:8000]}

    def serve(self, path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        os.chmod(path, 0o660)
        server.listen(8)
        logger.info("listening on %s", path)
        while True:
            conn, _ = server.accept()
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(READ_TIMEOUT)
            try:
                out = self.answer(conn)
            except Exception as exc:  # one bad request never stops the worker
                out = {"error": "failed", "reason": type(exc).__name__}
            try:
                conn.sendall(json.dumps(out).encode("utf-8") + b"\n")
            except OSError:
                pass

    def close(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="tw-decide-voice")
    ap.add_argument("--socket", required=True)
    ap.add_argument("--allow-user", required=True, help="the only user that may ask: the decision core's")
    ap.add_argument("--hermes-root", help="Hermes's source folder, when Hermes is not installed into this Python")
    args = ap.parse_args(argv)
    if args.hermes_root:
        sys.path.insert(0, args.hermes_root)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import pwd
    worker = Worker(allow_uid=pwd.getpwnam(args.allow_user).pw_uid)
    try:
        worker.serve(args.socket)
    finally:
        worker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
