"""Shared fixtures for the guard rail tests. No test reads a real secret."""

from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

from thornswood_guard.decisions import DecisionStore, new_decision  # noqa: E402
from thornswood_guard.github import CheckRun, GitHubRules, Issue, PullRequest  # noqa: E402
from thornswood_guard.policy import Policy  # noqa: E402

# Every temporary folder a test makes goes in one folder for the run, which
# goes when the run ends. Tests make them with tempfile.mkdtemp and never
# remove them: 12 Nov 2026, 91,945 of them (29 GB) filled a session's disk
# after a day of full runs, and the suite runs on our own CI runners too.
# Only the process that made the folder removes it, never a forked child.
_RUN_TMP = tempfile.mkdtemp(prefix="twhermesagents-tests-")
os.chmod(_RUN_TMP, 0o1777)  # as /tmp is: a test runs a step as another user
_RUN_PID = os.getpid()
tempfile.tempdir = _RUN_TMP


@atexit.register
def _remove_run_tmp() -> None:
    if os.getpid() == _RUN_PID:
        shutil.rmtree(_RUN_TMP, ignore_errors=True)


FIXTURES = Path(__file__).with_name("fixtures")
APP = "ThornswoodRP/Thornswood"
HEAD = "a" * 40
OTHER = "b" * 40
# Thornswood's required checks on a pull request since #5119 (31 Oct).
CHECKS = ("Launcher builds", "Build check result")


def policy() -> Policy:
    return Policy.load()


def verb_lists() -> dict[str, dict]:
    return {
        "scripts/vps/gate/verbs.json": json.loads((FIXTURES / "verbs.json").read_text(encoding="utf-8")),
        "scripts/release/nexus-verbs.json": json.loads((FIXTURES / "nexus-verbs.json").read_text(encoding="utf-8")),
    }


def store(p: Policy | None = None) -> DecisionStore:
    tmp = Path(tempfile.mkdtemp()) / "decisions.json"
    return DecisionStore(tmp, p or policy())


def rules(p: Policy | None = None, s: DecisionStore | None = None) -> GitHubRules:
    p = p or policy()
    return GitHubRules(p, s or store(p))


def pr(**kw) -> PullRequest:
    base = dict(repo=APP, number=42, head_sha=HEAD, head_ref="sprocket/3561-path-fence",
                base_ref="main", author_bot="sprocket", author_login="thornswood-workers[bot]",
                files=["launcher/PathFence.cs", "tests/test-3561.ps1"])
    base.update(kw)
    return PullRequest(**base)


def green(sha: str = HEAD, name: str | None = None, conclusion: str = "success",
          status: str = "completed") -> list[CheckRun]:
    """One run of the named check, or of each of Thornswood's required checks."""
    names = [name] if name else list(CHECKS)
    return [CheckRun(name=n, head_sha=sha, status=status, conclusion=conclusion) for n in names]


def issue(**kw) -> Issue:
    base = dict(repo=APP, number=3150, author_login="EvilPatrick06", assignees=[], labels=[])
    base.update(kw)
    return Issue(**base)


def decide(s: DecisionStore, *, id="d1", kind="main", commit=HEAD, pr_number=42,
           revision=1, answered_by="EvilPatrick06", hours=72, at=None):
    d = new_decision(id=id, kind=kind, repo=APP, commit=commit, revision=revision,
                     answered_by=answered_by, pr=pr_number, hours=hours, at=at)
    s.record(d)
    return d


def past(hours: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)
