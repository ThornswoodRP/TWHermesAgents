"""The seal on what box-run.yml sends back (TWHermesAgents #265).

Ops's box-run.yml seals a gate verb's output to the public half of a key the
run's starter sends as its recipient input, and writes the sealed file into
the job's log as TWSEAL-LOG lines; a run started with no recipient seals
nothing and its output stays on the box (TWSEAL-LOG NONE). Until #265
Bramble's runs sent none, so no Bramble box run could say what it printed:
Ops box run 45982471857 (Build-Content -Limited, Thornswood #6879) ended 1 on
27 Nov 2026 and its last line was nowhere but the box.

So tw-git makes a key for each box-run.yml it starts, keeps the private half
in its own folder, sends the public half, and reads the lines back with
run_output. The format is Thornswood's scripts/ops/Sealed.ps1: "TWSEAL1", the
wrapped key's length (2 bytes, big end first), an AES-256 key wrapped with
RSA-OAEP-SHA256 (MGF1 SHA-256), the 12 byte nonce, the 16 byte tag, then a zip
encrypted with AES-256-GCM; a key under 3072 bits is refused by the runner.

WHY A SECOND PYTHON. tw-git runs on the system's python3 with the standard
library alone (bin/tw-serve, -I), which has no RSA and no AES. The decision
core's venv holds the cryptography library, installed by
deploy/decisions/install.sh from hashes only, so the key work runs there, in
this same file started as a program, with the key and the sealed bytes over
stdin and nothing on the command line.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys

# The decision core's venv (deploy/decisions/install.sh).
VENV_PYTHON = "/opt/thornswood-hermes/decisions-venv/bin/python3"
BITS = 3072
_LINE = re.compile(r"^(?:\S+ )?TWSEAL-LOG (\d+|END|NONE)(?: (\S+))?(?: ([0-9a-f]{64}))?$")
_COLOUR = re.compile(r"\x1b\[[0-9;]*m")


class SealError(Exception):
    """Why a seal could not be made or opened, said plainly."""


def _helper(op: str, payload: dict, python: str) -> dict:
    try:
        r = subprocess.run([python, "-I", __file__, op], input=json.dumps(payload).encode(),
                           capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SealError(f"the seal helper did not run ({python}): {e}") from e
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
        raise SealError(f"the seal helper ended {r.returncode}: {tail[0]}")
    return json.loads(r.stdout)


def new_key(python: str = VENV_PYTHON) -> tuple[str, bytes]:
    """A key for one run: the public half as base64 SubjectPublicKeyInfo, the
    form box-run.yml's recipient takes, and the private half as PKCS#8 PEM."""
    out = _helper("new", {}, python)
    return out["public"], out["private"].encode()


def unseal(sealed: bytes, private_pem: bytes, python: str = VENV_PYTHON) -> bytes:
    """The zip inside a TWSEAL1 file."""
    if sealed[:7] != b"TWSEAL1":
        raise SealError("the sealed file is not one a Thornswood run made")
    out = _helper("open", {"sealed": base64.b64encode(sealed).decode(), "private": private_pem.decode()}, python)
    return base64.b64decode(out["zip"])


def sealed_from_log(log: str) -> tuple[bytes | None, str]:
    """The sealed bytes from one job's log, held to its END line's count and
    sha256 (Sealed.ps1, Read-ThornswoodSealedLog). (None, why) when the log
    has none, or (None, "") when it has no TWSEAL-LOG lines at all."""
    parts: dict[int, str] = {}
    end = None
    for raw in log.splitlines():
        m = _LINE.match(_COLOUR.sub("", raw).rstrip("\r"))
        if not m:
            continue
        if m.group(1) == "NONE":
            return None, "the run wrote no sealed output (it was started with no recipient key)"
        if m.group(1) == "END":
            end = m
            continue
        parts[int(m.group(1))] = m.group(2) or ""
    if end is None:
        return None, ""
    count = int(end.group(2) or 0)
    missing = [n for n in range(1, max(1, count) + 1) if n not in parts]
    if count < 1 or missing:
        return None, f"the log names {count} sealed part(s) and lacks {len(missing)}"
    data = base64.b64decode("".join(parts[n] for n in range(1, count + 1)))
    if hashlib.sha256(data).hexdigest() != end.group(3):
        return None, "the sealed lines in the log do not hash to the sha256 the log names"
    return data, ""


def _main(op: str) -> None:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    args = json.loads(sys.stdin.read() or "{}")
    if op == "new":
        key = rsa.generate_private_key(public_exponent=65537, key_size=BITS)
        public = key.public_key().public_bytes(serialization.Encoding.DER,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
        private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
        print(json.dumps({"public": base64.b64encode(public).decode(), "private": private.decode()}))
        return
    if op == "open":
        sealed = base64.b64decode(args["sealed"])
        key = serialization.load_pem_private_key(args["private"].encode(), password=None)
        n = (sealed[7] << 8) + sealed[8]
        at = 9
        wrapped = sealed[at:at + n]; at += n
        nonce = sealed[at:at + 12]; at += 12
        tag = sealed[at:at + 16]; at += 16
        aes = key.decrypt(wrapped, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
        plain = AESGCM(aes).decrypt(nonce, sealed[at:] + tag, None)
        print(json.dumps({"zip": base64.b64encode(plain).decode()}))
        return
    raise SystemExit(f"no seal op {op}")


if __name__ == "__main__":
    _main(sys.argv[1] if len(sys.argv) > 1 else "")
