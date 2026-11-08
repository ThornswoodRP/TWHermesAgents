#!/usr/bin/env python3
"""Apply one merged version of Thornswood-Hermes to the six existing profiles
and to the gateway's own profile.

Runs on the Oracle VM as the `hermes` user, from a checkout of the exact
commit Patrick merged:

    python3 scripts/apply.py --expect <commit> [--check]

It never creates or deletes a profile, never touches SOUL.md, memories,
sessions, auth.json or any .env, and never reads a secret's value. It checks
everything first and writes nothing if any check fails. With --check it only
reports.

For each Bot it:
  1. checks the profile and its SOUL.md exist;
  2. builds the Bot's config from the policy and merges it into config.yaml;
  3. checks the merged config can reach no route the Bot may not use;
  4. checks every .env by name against what may sit on the Oracle box;
  5. installs the guard plugin into plugins/thornswood_guard, and the live
     Board's observer into plugins/thornswood_live (it says what the Bot's
     running turns are doing, for the phone's Board, and blocks nothing);
  6. makes the Bot's working folder, and installs the skills this repository
     owns for it (OWN_SKILLS: Bramble's bramble-duties);
  7. installs the two Thornswood skills from skills/thornswood/ (until 1 Nov
     they lived only in each profile, so a stale line could be fixed by no PR);
  8. loads the installed plugin and checks it refuses what it must.

For the Bot GitHub wakes (Bramble) it also installs the owed-wake check: the
script into the profile's scripts/ and the cron job tw-owed-wake, made or
brought up to date through the `hermes` command (thornswood_guard/owed_wake.py
says what it does). A job Patrick paused stays paused.

The gateway's own profile (the hermes user's default profile, which
tw-hermes-gateway runs as) is not a Bot, but it holds the Discord connection
and makes the gateway's own helper calls. It gets one route, the helpers on
it, and the guard, which refuses every tool there; its .env is checked as
before.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
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
from thornswood_guard.profile_config import (  # noqa: E402
    GATEWAY_PROFILE, SANDBOX_IMAGE, WORK_ROOT, build, build_gateway, merge_into)
from thornswood_guard.routes import check_config, check_gateway_config  # noqa: E402
from thornswood_guard.secrets_check import check_env, check_files, env_names  # noqa: E402

SKILLS = ("thornswood-ops", "thornswood-guide")
SKILLS_DIR = REPO / "skills" / "thornswood"
# Skills this repository owns and installs, by Bot, from skills/<name>/SKILL.md
# into the profile's skills/thornswood/<name>/. 2 Nov: Bramble had merge
# authority but nothing told it to merge, hand back a dropped pull request, or
# run the Ops run notes, so fork #147 and Ops #200 and #201 sat green.
OWN_SKILLS = {"bramble": ("bramble-duties",)}
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
    """What the guard calls on Hermes's plugin context (hermes_cli/plugins.py)."""

    def __init__(self, profile_name: str):
        self.profile_name = profile_name
        self.hooks: dict[str, list] = {}
        self.tools: dict[str, str] = {}

    def register_hook(self, name, fn):
        self.hooks.setdefault(name, []).append(fn)

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = toolset

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
    # The rules themselves, from the installed copy, apart from the turn gate.
    rules = sys.modules[f"{name}.tool_guard"]
    installed_policy = sys.modules[f"{name}.policy"].Policy.load()
    for tool, args in must_refuse:
        if rules.check_tool(installed_policy, bot, tool, args).allowed:
            problems.append(f"{bot}: the installed rules allow {tool} {args}")
    if not rules.check_tool(installed_policy, bot, "terminal", {"command": "git status"}).allowed:
        problems.append(f"{bot}: the installed rules refuse git status")
    out = hook(tool_name="terminal", args={"command": "git status"})
    if installed_policy.data["brokers"].get("ready"):
        # With the brokers on, a turn the fleet queue never admitted has every
        # tool refused, and the Bot has its two broker tools.
        if not (out or {}).get("action") == "block" or "ticket" not in (out or {}).get("message", ""):
            problems.append(f"{bot}: with the brokers on, a tool call with no ticket from the fleet queue was not refused ({out})")
        if sorted(ctx.tools) != ["tw_decide", "tw_files", "tw_git"]:
            problems.append(f"{bot}: the installed guard registered the tools {sorted(ctx.tools)}, not tw_git, tw_files and tw_decide")
        for want in ("pre_llm_call", "on_session_end", "agent_loop_stopped"):
            if len(ctx.hooks.get(want) or []) != 1:
                problems.append(f"{bot}: the turn gate's {want} hook is not registered once")
    elif out is not None:
        problems.append(f"{bot}: the installed guard refused git status")
    for k in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[k]
    return problems


def gateway_self_test(plugin_dir: Path) -> list[str]:
    """In the gateway's own profile the installed guard refuses every tool."""
    name = "_tw_selftest_gateway"
    spec = importlib.util.spec_from_file_location(name, plugin_dir / "__init__.py",
                                                  submodule_search_locations=[str(plugin_dir)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    ctx = FakeCtx(GATEWAY_PROFILE)
    module.register(ctx)
    hooks = ctx.hooks.get("pre_tool_call") or []
    del sys.modules[name]
    if len(hooks) != 1:
        return [f"the gateway's own profile: the installed guard registered {len(hooks)} pre_tool_call hooks, not 1"]
    return [f"the gateway's own profile: the installed guard let {tool} through"
            for tool, args in (("terminal", {"command": "git status"}), ("read_file", {"path": "README.md"}),
                               ("vision_analyze", {"image_url": "x.png"}))
            if (hooks[0](tool_name=tool, args=args) or {}).get("action") != "block"]


def install_guard(pdir: Path, name: str = "thornswood_guard") -> None:
    """Put one of this repository's plugins in the profile whole, never half of one."""
    plugins = pdir / "plugins"
    plugins.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=plugins, prefix=f".{name}-"))
    shutil.copytree(REPO / name, staging / "pkg", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    target = plugins / name
    old = plugins / f".{name}-old-{os.getpid()}"
    if target.exists():
        target.rename(old)
    (staging / "pkg").rename(target)
    shutil.rmtree(staging)
    if old.exists():
        shutil.rmtree(old)


def check_skills() -> list[str]:
    """Each skill in this commit is a SKILL.md whose front matter Hermes can read
    and names the skill its folder does."""
    problems = []
    for skill in SKILLS:
        path = SKILLS_DIR / skill / "SKILL.md"
        if not path.is_file():
            problems.append(f"the {skill} skill is not in this commit at {path.relative_to(REPO)}")
            continue
        text = path.read_text(encoding="utf-8")
        parts = text.split("---\n", 2) if text.startswith("---\n") else []
        meta = yaml.safe_load(parts[1]) if len(parts) == 3 else None
        if not isinstance(meta, dict) or meta.get("name") != skill or not meta.get("description"):
            problems.append(f"the {skill} skill's front matter does not name it or describe it")
    return problems


def install_skills(pdir: Path) -> list[str]:
    """Write each skill over the profile's copy, one file at a time, never half a file."""
    changed = []
    for skill in SKILLS:
        src = (SKILLS_DIR / skill / "SKILL.md").read_bytes()
        dest_dir = pdir / "skills" / "thornswood" / skill
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "SKILL.md"
        if dest.is_file() and dest.read_bytes() == src:
            continue
        fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".SKILL-")
        with os.fdopen(fd, "wb") as f:
            f.write(src)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
        changed.append(skill)
    return changed


def install_skill(pdir: Path, name: str) -> None:
    """The repository's skills/<name>/SKILL.md into the profile, whole, in one rename."""
    target = pdir / "skills" / "thornswood" / name
    target.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target, prefix=".SKILL-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write((REPO / "skills" / name / "SKILL.md").read_text(encoding="utf-8"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, target / "SKILL.md")


def cron_jobs(hermes: list[str], bot: str) -> tuple[dict[str, list[dict]] | None, str]:
    """The profile's cron jobs by name ({name: [{"id", "state"}, ...]}), read
    with `hermes -p <bot> cron list --all`, or (None, why not)."""
    try:
        r = subprocess.run([*hermes, "-p", bot, "cron", "list", "--all"], capture_output=True, text=True,
                           timeout=120, env=dict(os.environ, NO_COLOR="1"))
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"{shlex.join(hermes)} could not be run ({e})"
    if r.returncode != 0:
        return None, f"hermes -p {bot} cron list exited {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}"
    jobs: dict[str, list[dict]] = {}
    current = None
    for raw in r.stdout.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m", "", raw)
        if m := re.fullmatch(r"  (\S+) \[([a-z ]+)\]\s*", line):
            current = {"id": m.group(1), "state": m.group(2)}
        elif current and (m := re.fullmatch(r"    Name:\s*(.+?)\s*", line)):
            jobs.setdefault(m.group(1), []).append(current)
            current = None
    return jobs, ""


def install_owed_wake(pdir: Path, hermes: list[str], bot: str) -> list[str]:
    """The owed-wake script into <profile>/scripts/, and its cron job made or
    brought up to date. Returns what failed."""
    from thornswood_guard import owed_wake

    scripts = pdir / "scripts"
    scripts.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=scripts, prefix=".tw-owed-wake-")
    with os.fdopen(fd, "wb") as f:
        f.write((REPO / "deploy" / "hermes-scripts" / owed_wake.SCRIPT).read_bytes())
    os.chmod(tmp, 0o644)
    os.replace(tmp, scripts / owed_wake.SCRIPT)

    jobs, why = cron_jobs(hermes, bot)
    if jobs is None:
        return [f"{bot}: the owed-wake job could not be read: {why}"]
    spec = ["--name", owed_wake.OWED_WAKE_JOB, "--skill", owed_wake.SKILL, "--script", owed_wake.SCRIPT,
            "--deliver", "local"]
    found = jobs.get(owed_wake.OWED_WAKE_JOB, [])
    if len(found) > 1:
        return [f"{bot}: {len(found)} cron jobs are named {owed_wake.OWED_WAKE_JOB} "
                f"({', '.join(j['id'] for j in found)}); one must go, with hermes -p {bot} cron remove <id>"]
    if not found:
        argv = [*hermes, "-p", bot, "cron", "create", owed_wake.SCHEDULE, owed_wake.PROMPT, *spec]
    else:
        argv = [*hermes, "-p", bot, "cron", "edit", found[0]["id"], "--schedule", owed_wake.SCHEDULE,
                "--prompt", owed_wake.PROMPT, *spec]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=dict(os.environ, NO_COLOR="1"))
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"{bot}: the owed-wake job could not be written ({e})"]
    if r.returncode != 0:
        return [f"{bot}: hermes cron {argv[4]} exited {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}"]
    jobs, why = cron_jobs(hermes, bot)
    found = (jobs or {}).get(owed_wake.OWED_WAKE_JOB, [])
    if len(found) != 1:
        return [f"{bot}: hermes cron {argv[4]} answered, but {len(found)} jobs named {owed_wake.OWED_WAKE_JOB} "
                f"are listed after it ({why or 'one was expected'})"]
    job = found[0]
    if job["state"] not in ("active", "paused"):
        return [f"{bot}: the owed-wake job {job['id']} is {job['state']}, so it never runs"]
    print(f"{bot}: owed-wake job {job['id']} is {job['state']}"
          + ("; Patrick paused it, so it stays paused" if job["state"] == "paused" else ""))
    return []


def write_config(pdir: Path, cfg_path: Path, merged: dict, commit: str, stamp: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=pdir, prefix=".config-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        yaml.safe_dump(merged, f, sort_keys=False, allow_unicode=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, cfg_path)
    record = pdir / "local" / "thornswood-hermes"
    record.mkdir(parents=True, exist_ok=True)
    (record / "applied.json").write_text(json.dumps({"commit": commit, "applied_at": stamp}, indent=2) + "\n",
                                         encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--expect", required=True, help="the full commit Patrick merged; this checkout must be exactly it")
    ap.add_argument("--hermes-home", default="/home/hermes/.hermes")
    ap.add_argument("--work-root", default=WORK_ROOT)
    ap.add_argument("--check", action="store_true", help="report only; write nothing")
    ap.add_argument("--podman", default="podman", help="the container runtime command (tests pass a stand-in)")
    ap.add_argument("--hermes", default="/home/hermes/.local/bin/hermes",
                    help="the hermes command, for Bramble's owed-wake cron job (tests pass a stand-in)")
    ap.add_argument("--wake-secret-file", default=None,
                    help="where deploy/install.sh made the webhook secret (default: the policy's wake.secret_file; tests pass a stand-in)")
    a = ap.parse_args()
    podman = shlex.split(a.podman)
    hermes = shlex.split(a.hermes)

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
        for skill in OWN_SKILLS.get(bot, ()):
            if not (REPO / "skills" / skill / "SKILL.md").is_file():
                problems.append(f"{bot}: this commit has no skills/{skill}/SKILL.md to install")
        cfg_path = pdir / "config.yaml"
        existing = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        merged = merge_into(existing or {}, build(policy, bot))
        names = env_names(pdir / ".env")
        problems += check_env(policy, names, bot)
        problems += [f"{bot}: {p}" for p in check_config(policy, bot, merged, names | root_env)]
        plans[bot] = (pdir, cfg_path, existing or {}, merged)
    gw_path = home / "config.yaml"
    gw_existing = (yaml.safe_load(gw_path.read_text(encoding="utf-8")) if gw_path.exists() else {}) or {}
    gw_merged = merge_into(gw_existing, build_gateway(policy))
    problems += [f"the gateway's own profile: {p}" for p in check_gateway_config(policy, gw_merged, root_env)]
    # The gateway's webhook route needs its secret, which only systemd reads:
    # without the file the gateway would not start (its drop-in names it). The
    # hermes user can see that the file is there, never what is in it.
    wake_secret = Path(a.wake_secret_file or policy.data["wake"]["secret_file"])
    if not wake_secret.exists():
        problems.append(f"{wake_secret} is not there; deploy/install.sh makes it, so run the install for this commit first")
    problems += scan_files(policy, home)
    problems += check_skills()
    # Bramble's owed-wake job is written through the hermes command, so it must
    # answer before anything is written.
    waker = policy.data["wake"]["app"]
    if waker in plans:
        _, why = cron_jobs(hermes, waker)
        if why:
            problems.append(f"{waker}: {why}")

    # Every Bot's terminal runs in Hermes's sandbox image under rootless podman.
    present, why = image_present(podman)
    if present is None:
        problems.append(f"podman could not be run ({why}); the Bots' terminals need it")
    elif not present:
        print(f"The sandbox image {SANDBOX_IMAGE} is not in the hermes user's podman store yet; it is pulled before anything is written.")

    for bot, (_, _, existing, merged) in plans.items():
        changed = sorted(k for k in merged if existing.get(k) != merged.get(k))
        print(f"{bot}: config keys set: {', '.join(changed) or 'none, already current'}")
    changed = sorted(k for k in gw_merged if gw_existing.get(k) != gw_merged.get(k))
    print(f"the gateway's own profile: config keys set: {', '.join(changed) or 'none, already current'}")

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

    stamp = datetime.now(timezone.utc).isoformat()
    for bot, (pdir, cfg_path, _, merged) in plans.items():
        work = Path(a.work_root) / bot
        work.mkdir(parents=True, exist_ok=True)
        os.chmod(work, 0o700)
        install_guard(pdir)
        install_guard(pdir, "thornswood_live")
        for skill in OWN_SKILLS.get(bot, ()):
            install_skill(pdir, skill)
        write_config(pdir, cfg_path, merged, a.expect, stamp)
        skills = install_skills(pdir)
        print(f"{bot}: skills written: {', '.join(skills) or 'none, already current'}")
    install_guard(home)
    write_config(home, gw_path, gw_merged, a.expect, stamp)

    owed = install_owed_wake(plans[waker][0], hermes, waker) if waker in plans else []
    failures = []
    for bot, (pdir, *_rest) in plans.items():
        failures += self_test(pdir / "plugins" / "thornswood_guard", bot)
    failures += gateway_self_test(home / "plugins" / "thornswood_guard")
    if failures:
        print("\nThe guard was installed but its self-test failed. Treat every Bot as unguarded until this is fixed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    if owed:
        print("\nThe guard was installed and its self-test passed, but Bramble's owed-wake job was not written, so a "
              "wake the fleet queue turns away waits for the next one:")
        for f in owed:
            print(f"  - {f}")
        return 1
    print(f"\nApplied {a.expect} to {', '.join(plans)} and the gateway's own profile. The guard's self-test passed in every profile.")
    print("Every Bot process picks up the plugin and config when it starts again. End the hermes user's login sessions"
          " (Hermes Desktop's backends) and restart the gateway in one step: ssh -t thornswood-bots sudo /opt/thornswood-hermes/bin/tw-reload-bots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
