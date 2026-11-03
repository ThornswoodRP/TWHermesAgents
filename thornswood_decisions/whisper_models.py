"""Whisper models for dictation, fetched by the install into a cache the Bots' user only reads.

At 4:47 PM on 2 Nov every VM install stopped at dictation's model load: Hermes's own first load downloads
the model from the Hugging Face Hub into the home of the user it runs as, and the Bots' `hermes` user has a
disk quota that it was already near ("Disk quota exceeded (os error 122)", with the disk 27% full). The quota
is a guard and stays. So the install fetches the model as root, outside that user's quota, into
/var/lib/tw-decide-voice/hub, laid out as the Hub's own cache (models--<org>--<name>/refs/main and
snapshots/<commit>/), and tw-decide-voice.service points Hermes at it with HF_HUB_CACHE and HF_HUB_OFFLINE=1:
Hermes's loader (tools/transcription_local.py _create_whisper_model, local_files_only first, at the pinned
commit) finds it there and never downloads.

Each model is pinned here: the repository faster-whisper names for it (faster_whisper/utils.py _MODELS), the
commit, and every file's size and SHA-256, as the Hub listed them on 2 Nov. A file is fetched from that
commit only, to a .part file, checked, then moved in place; a file already there with the right hash is kept.
Standard library only, run with the system Python as root:

    /usr/bin/python3 -I whisper_models.py --model base --cache /var/lib/tw-decide-voice/hub

It prints PASS or STOP lines and exits 1 on any STOP.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import urllib.request
from pathlib import Path

CACHE = "/var/lib/tw-decide-voice/hub"
HUB = "https://huggingface.co"
# Room kept free beyond the model itself, so a fetch never fills the disk the core and the Bots write to.
SPARE = 2 * 1024 ** 3

MODELS = {
    "base": {
        "repo": "Systran/faster-whisper-base",
        "commit": "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
        "files": {
            "config.json": (2309, "56a6d8110d311f19c8f0471e562832c7527f146b567275bfca59fcf7c184da9a"),
            "model.bin": (145217532, "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9"),
            "tokenizer.json": (2203239, "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"),
            "vocabulary.txt": (459861, "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"),
        },
    },
}


class Stop(Exception):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(cache: Path, name: str) -> Path:
    m = MODELS[name]
    return cache / ("models--" + m["repo"].replace("/", "--")) / "snapshots" / m["commit"]


def _get(url: str, to: Path, opener=urllib.request.urlopen) -> None:
    with opener(urllib.request.Request(url, headers={"User-Agent": "thornswood-hermes-install"}), timeout=60) as r, \
            to.open("wb") as out:
        shutil.copyfileobj(r, out, 1024 * 1024)


def fetch(name: str, cache: Path, get=_get, free=shutil.disk_usage, say=print) -> Path:
    if name not in MODELS:
        raise Stop(f"Hermes's stt.local.model is {name!r}, which is not pinned in whisper_models.py; "
                   f"pinned: {', '.join(sorted(MODELS))}")
    m = MODELS[name]
    snap = snapshot(cache, name)
    snap.mkdir(parents=True, exist_ok=True)
    d = snap
    while True:  # readable by the Bots' user, writable by root alone
        os.chmod(d, 0o755)
        if d == cache:
            break
        d = d.parent
    missing = [f for f, (size, digest) in m["files"].items()
               if not ((snap / f).is_file() and (snap / f).stat().st_size == size and sha256(snap / f) == digest)]
    need = sum(m["files"][f][0] for f in missing)
    if missing and free(cache).free < need + SPARE:
        raise Stop(f"{cache} has {free(cache).free // 1024 ** 2} MB free; the {name} model needs "
                   f"{need // 1024 ** 2} MB with {SPARE // 1024 ** 3} GB kept spare")
    for f in missing:
        size, digest = m["files"][f]
        part = snap / (f + ".part")
        try:
            get(f"{HUB}/{m['repo']}/resolve/{m['commit']}/{f}", part)
            got_size, got = part.stat().st_size, sha256(part)
            if (got_size, got) != (size, digest):
                raise Stop(f"{f} of {m['repo']} at {m['commit'][:12]} came as {got_size} bytes, sha256 {got}; "
                           f"pinned {size} bytes, {digest}")
            os.chmod(part, 0o644)
            os.replace(part, snap / f)
        finally:
            part.unlink(missing_ok=True)
        say(f"PASS {f} fetched and checked ({size} bytes)")
    refs = snap.parent.parent / "refs"
    refs.mkdir(exist_ok=True)
    os.chmod(refs, 0o755)
    (refs / "main.part").write_text(m["commit"], encoding="ascii")
    os.chmod(refs / "main.part", 0o644)
    os.replace(refs / "main.part", refs / "main")
    say(f"PASS the {name} model ({m['repo']} at {m['commit'][:12]}) is in {cache}")
    return snap


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="whisper_models")
    ap.add_argument("--model", required=True)
    ap.add_argument("--cache", default=CACHE)
    a = ap.parse_args(argv)
    try:
        fetch(a.model, Path(a.cache))
    except Stop as exc:
        print(f"STOP {exc}")
        return 1
    except OSError as exc:
        print(f"STOP the {a.model} model was not fetched: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
