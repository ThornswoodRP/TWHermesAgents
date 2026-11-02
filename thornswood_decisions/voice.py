"""Dictation: the app's recording to words, through the VM's own Whisper (voice_worker.py).

The phone records while Patrick holds the microphone (or while voice chat hears him) and posts the
recording here as its own bytes (`POST /voice/transcribe`, `Content-Type: audio/...`). The core hands it
to the voice worker on its Unix socket and answers the words; it keeps nothing, neither the recording nor
the words, and logs neither. The words go into the box he is typing in, where he sees them before
anything is sent.
"""

from __future__ import annotations

import json
import socket

from .store import CoreError

SOCKET = "/run/tw/decide-voice/voice.sock"
MAX_AUDIO = 4 * 1024 * 1024
TYPES = ("audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "audio/x-wav", "audio/aac")
TIMEOUT = 90


class Voice:
    def __init__(self, path: str = SOCKET):
        self.path = path

    def _ask(self, head: dict, body: bytes = b"") -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(TIMEOUT)
                s.connect(self.path)
                try:
                    s.sendall(json.dumps(head).encode("utf-8") + b"\n")
                    if body:
                        s.sendall(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the worker answered before taking it all (a refusal): its answer says why
                line = b""
                while not line.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    line += chunk
        except (FileNotFoundError, ConnectionRefusedError):
            raise CoreError(503, "voice_off", "dictation is not running on the VM (tw-decide-voice)") from None
        except socket.timeout:
            raise CoreError(504, "voice_slow", "Whisper on the VM did not answer in 90 seconds") from None
        except OSError as exc:
            raise CoreError(503, "voice_off", f"dictation on the VM could not be reached ({type(exc).__name__})") from None
        try:
            out = json.loads(line)
        except json.JSONDecodeError:
            raise CoreError(502, "voice_failed", "dictation on the VM gave no answer") from None
        if not isinstance(out, dict):
            raise CoreError(502, "voice_failed", "dictation on the VM gave no answer")
        return out

    def check(self) -> str | None:
        """Which local Whisper the voice worker found in Hermes, or None: for the install's line."""
        return self._ask({"op": "check"}).get("whisper")

    def transcribe(self, p, mime: str, body: bytes) -> dict:
        if p.kind != "patrick":
            raise CoreError(403, "not_patrick", f"only Patrick dictates; {p.label} cannot")
        kind = (mime or "").split(";")[0].strip().lower()
        if kind not in TYPES:
            raise CoreError(415, "bad_type", "a recording is webm, ogg, mp4, mp3, wav or aac")
        if not body:
            raise CoreError(400, "no_audio", "the recording is empty")
        if len(body) > MAX_AUDIO:
            raise CoreError(413, "too_big", "a recording is at most 4 MB, about four minutes")
        out = self._ask({"op": "transcribe", "type": kind, "size": len(body)}, body)
        if "text" not in out:
            reason = str(out.get("reason") or "no words")
            raise CoreError(502, "voice_failed", f"Whisper on the VM failed: {reason}"[:300])
        return {"text": str(out["text"])[:8000]}
