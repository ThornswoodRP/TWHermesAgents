"""tw-runner-register: registers the guard rail tests' runner with GitHub,
once, as root (deploy/runner; Patrick's A on q-hermes-ci-free-runners).

    gh api -X POST repos/ThornswoodRP/Thornswood-Hermes/actions/runners/registration-token --jq .token \\
      | ssh thornswood-bots "sudo /opt/thornswood-hermes/bin/tw-runner-register"

The token arrives on standard input and reaches the runner's config only in
its environment (ACTIONS_RUNNER_INPUT_TOKEN, which the runner reads as its
--token): never in a process's arguments, which every user may read in
/proc, nor in a file or a log. It is good for one hour and one registration.
The runner then keeps its own credential in its folder (GitHub's design),
tw-runner's alone. Nothing is registered unless tw-runner's network table is
on. Run it again to register afresh (--replace).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PIN = Path("/opt/thornswood-hermes/current/deploy/runner/runner.json")


def account(name: str):
    """The runner's user from the system's accounts. pwd is Linux's alone, so it
    is imported here, where the VM calls it, and the module still loads on
    Windows, where the guard rail tests read it too (3 Nov: One's run could
    not load test_runner)."""
    import pwd
    return pwd.getpwnam(name)


def config_command(pin: dict) -> list[str]:
    """The runner's config, as the command line every user may see: no token."""
    return ["./config.sh", "--unattended", "--url", pin["repository"], "--name", pin["name"],
            "--labels", ",".join(pin["labels"]), "--work", "_work", "--replace", "--no-default-labels"]


def config_env(pin: dict, token: str) -> dict:
    """The config's whole environment: tw-runner's home, a plain PATH, and the token."""
    return {"HOME": pin["home"], "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
            "ACTIONS_RUNNER_INPUT_TOKEN": token}


def register(pin: dict, token: str, *, run=subprocess.run, sleep=time.sleep) -> list[str]:
    """The lines to print; raises SystemExit with a STOP line when anything is wrong."""
    runner = Path(pin["home"]) / "runner"
    if not (runner / "config.sh").is_file():
        raise SystemExit(f"STOP: the runner is not unpacked in {runner}; the install puts it there.")
    if run(["systemctl", "is-active", "--quiet", "tw-runner-egress.service"]).returncode != 0:
        raise SystemExit("STOP: tw-runner-egress is not active, so tw-runner's network is not limited; "
                         "nothing was registered.")
    if not token:
        raise SystemExit("STOP: nothing arrived on standard input, so there is no registration token. "
                         "Nothing was registered.")
    run(["systemctl", "stop", "tw-runner.service"])
    user = account(pin["user"])

    def as_runner():
        os.setgroups([])
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)

    r = run(config_command(pin), cwd=str(runner), env=config_env(pin, token), preexec_fn=as_runner)
    if r.returncode != 0:
        raise SystemExit("STOP: the runner's config refused (above); tw-runner was left stopped.")
    if not (runner / ".runner").is_file():
        raise SystemExit(f"STOP: the config ended without registering (no {runner / '.runner'}).")
    run(["systemctl", "enable", "--quiet", "--now", "tw-runner.service"])
    sleep(10)
    if run(["systemctl", "is-active", "--quiet", "tw-runner.service"]).returncode != 0:
        raise SystemExit("STOP: tw-runner did not stay up: journalctl -u tw-runner -n 50")
    return [f"PASS the runner {pin['name']} is registered to {pin['repository']} with the label "
            f"{','.join(pin['labels'])}, and tw-runner is up"]


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        print("STOP: run as root.")
        return 1
    if not PIN.is_file():
        print(f"STOP: {PIN} is missing; the install puts it there.")
        return 1
    pin = json.loads(PIN.read_text(encoding="utf-8"))
    token = sys.stdin.readline().strip()
    try:
        for line in register(pin, token):
            print(line)
    except SystemExit as e:
        print(e)
        return 1
    return 0
