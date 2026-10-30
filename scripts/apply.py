#!/usr/bin/env python3
"""Apply one merged version of Thornswood-Hermes to the six existing profiles.

Runs on the Oracle VM as the `hermes` user, from a checkout of the exact
commit Patrick merged:

    python3 scripts/apply.py --expect <commit> [--check]

It never creates or deletes a profile, never touches SOUL.md, memories,
sessions, auth.json or any .env, and never reads a secret's value. It checks
everything first and writes nothing if any check fails. With --check it only
reports.

For each Bot it:
  1. checks the profile, its SOUL.md and its two Thornswood skills exist;
  2. builds the Bot's config from the policy and merges it into config.yaml;
  3. checks the merged config can reach no route the Bot may not use;
  4. checks every .env by name against what may sit on the Oracle box;
  5. installs the guard plugin into plugins/thornswood_guard;
  6. makes the Bot's working folder;
  7. loads the installed plugin and checks it refuses what it must.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import yaml  # noqa: E402

from thornswood_guard.policy import Policy  # noqa: E402
from thornswood_guard.profile_config import SANDBOX_IMAGE, WORK_ROOT, build, merge_into  # noqa: E402
from thornswood_guard.routes import check_config  # noqa: E402
from thornswood_guard.secrets_check import check_env, check_files, env_names  # noqa: E402

SKILLS = ("thornswood-ops", "thornswood-guide")
SKIP_SCAN = {"hermes-agent", "sessions", "logs", "cache", "image_cache", "audio_cache",
             "document_cache", "node_modules", "backups", "checkpoints"}


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def scan_files(policy: Policy, home: Path) -> list[str]:
    """Look for files that must never be on the box, by name, in Hermes's
    home and every profile, skipping the install and the big runtime folders."""
    user_home = home.parent
    return (check_files(policy, home, SKIP_SCAN) + check_files(policy, user_home / ".ssh")
            + check_files(policy, user_home / ".config" / "gh")
            + [f for f in check_files(policy, user_home, SKIP_SCAN | {".hermes", ".config", ".ssh", ".local", ".cache", "tw-work", "thornswood-hermes"})])


def image_present(podman: list[str]) -> tuple[bool | None, str]:
    """Whether the sandbox image is in the hermes user's podman store.
    None means podman itself could not be run."""
    try:
        r = subprocess.run([*podman, "image", "exists", SANDBOX_IMAGE], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"{type(e).__name__}: {e}"
    return r.returncode == 0, (r.stderr or r.stdout).strip()


class FakeCtx:
    def __init__(self, profile_name: str):
        self.profile_name = profile_name
        self.hooks: dict[str, list] = {}

    def register_hook(self, name, fn):
        self.hooks.setdefault(name, []).append(fn)

    def register_system_prompt_section(self, *a, **k):
        pass


def self_test(plugin_dir: Path, bot: str) -> list[str]:
    """Load the installed copy and check it refuses what it must."""
    name = f"_tw_selftest_{bot}"
    spec = importlib.util.spec_from_file_location(name, plugin_dir / "__init__.py",
                                                  submodule_search_locations=[str(plugin_dir)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    ctx = FakeCtx(bot)
    module.register(ctx)
    hooks = ctx.hooks.get("pre_tool_call") or []
    if len(hooks) != 1:
        return [f"{bot}: the installed guard registered {len(hooks)} pre_tool_call hooks, not 1"]
    hook = hooks[0]
    problems = []
    must_refuse = [
        ("terminal", {"command": "git push origin HEAD:main"}),
        ("terminal", {"command": "cat ~/.hermes/auth.json"}),
        ("terminal", {"command": "gh pr merge 1 --squash"}),
        ("write_file", {"path": "/home/hermes/.hermes/profiles/" + bot + "/config.yaml", "content": "x"}),
        ("skill_manage", {"action": "patch", "name": "thornswood-guide"}),
        ("execute_code", {"code": "print(1)"}),
    ]
    for tool, args in must_refuse:
        if not (hook(tool_name=tool, args=args) or {}).get("action") == "block":
            problems.append(f"{bot}: the installed guard let {tool} {args} through")
    if hook(tool_name="terminal", args={"command": "git status"}) is not None:
        problems.append(f"{bot}: the installed guard refused git status")
    del sys.modules[name]
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--expect", required=True, help="the full commit Patrick merged; this checkout must be exactly it")
    ap.add_argument("--hermes-home", default="/home/hermes/.hermes")
    ap.add_argument("--work-root", default=WORK_ROOT)
    ap.add_argument("--check", action="store_true", help="report only; write nothing")
    ap.add_argument("--podman", default="podman", help="the container runtime command (tests pass a stand-in)")
    a = ap.parse_args()
    podman = shlex.split(a.podman)

    problems: list[str] = []
    head = git("rev-parse", "HEAD")
    if head != a.expect:
        print(f"STOP: this checkout is {head}, not {a.expect}. Nothing was changed.")
        return 1
    if git("status", "--porcelain"):
        print("STOP: this checkout has changes that are not in the merged commit. Nothing was changed.")
        return 1

    policy = Policy.load(REPO / "thornswood_guard" / "policy.json")
    home = Path(a.hermes_home)
    profiles = home / "profiles"
    root_env = env_names(home / ".env")
    problems += check_env(policy, root_env, "root")

    plans = {}
    for bot in policy.bots:
        pdir = profiles / bot
        if not pdir.is_dir():
            problems.append(f"{bot}: no profile at {pdir}; this script never creates one")
            continue
        if not (pdir / "SOUL.md").is_file():
            problems.append(f"{bot}: no SOUL.md")
        for skill in SKILLS:
            if not (pdir / "skills" / "thornswood" / skill / "SKILL.md").is_file():
                problems.append(f"{bot}: the {skill} skill is missing")
        cfg_path = pdir / "config.yaml"
        existing = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        merged = merge_into(existing or {}, build(policy, bot))
        names = env_names(pdir / ".env")
        problems += check_env(policy, names, bot)
        problems += [f"{bot}: {p}" for p in check_config(policy, bot, merged, names | root_env)]
        plans[bot] = (pdir, cfg_path, existing or {}, merged)
    problems += scan_files(policy, home)

    # Every Bot's terminal runs in Hermes's sandbox image under rootless podman.
    present, why = image_present(podman)
    if present is None:
        problems.append(f"podman could not be run ({why}); the Bots' terminals need it")
    elif not present:
        print(f"The sandbox image {SANDBOX_IMAGE} is not in the hermes user's podman store yet; it is pulled before anything is written.")

    for bot, (_, _, existing, merged) in plans.items():
        changed = sorted(k for k in merged if existing.get(k) != merged.get(k))
        print(f"{bot}: config keys set: {', '.join(changed) or 'none, already current'}")

    if problems:
        print("\nSTOP. Nothing was changed:")
        for p in problems:
            print(f"  - {p}")
        return 1
    if a.check:
        print("\nCheck passed. Nothing was changed (--check).")
        return 0

    if not present:
        r = subprocess.run([*podman, "pull", "-q", SANDBOX_IMAGE], capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            print(f"\nSTOP. Pulling {SANDBOX_IMAGE} failed, so nothing was changed:\n  {(r.stderr or r.stdout).strip()}")
            return 1
        print(f"Pulled {SANDBOX_IMAGE}.")

    src = REPO / "thornswood_guard"
    stamp = datetime.now(timezone.utc).isoformat()
    for bot, (pdir, cfg_path, _, merged) in plans.items():
        work = Path(a.work_root) / bot
        work.mkdir(parents=True, exist_ok=True)
        os.chmod(work, 0o700)

        plugins = pdir / "plugins"
        plugins.mkdir(exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=plugins, prefix=".thornswood_guard-"))
        shutil.copytree(src, staging / "pkg", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        target = plugins / "thornswood_guard"
        old = plugins / f".thornswood_guard-old-{os.getpid()}"
        if target.exists():
            target.rename(old)
        (staging / "pkg").rename(target)
        shutil.rmtree(staging)
        if old.exists():
            shutil.rmtree(old)

        fd, tmp = tempfile.mkstemp(dir=pdir, prefix=".config-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(merged, f, sort_keys=False, allow_unicode=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, cfg_path)

        record = pdir / "local" / "thornswood-hermes"
        record.mkdir(parents=True, exist_ok=True)
        (record / "applied.json").write_text(json.dumps({"commit": a.expect, "applied_at": stamp}, indent=2) + "\n",
                                             encoding="utf-8")

    failures = []
    for bot, (pdir, *_rest) in plans.items():
        failures += self_test(pdir / "plugins" / "thornswood_guard", bot)
    if failures:
        print("\nThe guard was installed but its self-test failed. Treat every Bot as unguarded until this is fixed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nApplied {a.expect} to {', '.join(plans)}. The guard's self-test passed in every profile.")
    print("A running gateway picks up the plugin and config on restart: hermes gateway restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
