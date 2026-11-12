"""tw-git: the only GitHub writer on the VM (08 section 4).

Runs as tw-git, the only user that can read the two Apps' private keys. The
gateway asks it over /run/tw/git.sock; the Bot is the profile the call came
from (the gateway plugin sends it, never the model), and the socket checks the
caller is the gateway's user.

For every write tw-git reads the facts itself, just before the write: the
PR's head, base, author, files and check runs, main's verb lists, the
branch's real changed files. The rules are thornswood_guard.github's, the same
code the guard's tests hold. Then it asks tw-policy for the operation
(op_begin), makes the one write, reads it back, and reports the outcome. A
write whose answer was lost is "unknown", and the next ask for it reads
GitHub before anything else (attack test 20): a lost answer is never a reason
to write again.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import logging
import os
import re
import subprocess
import urllib.parse
from pathlib import Path
from typing import Callable

from thornswood_guard.github import SHA, CheckRun, Credit, GitHubRules, Issue, PullRequest
from thornswood_guard.policy import Policy, Verdict

from .github_api import GitHub, GitHubError, quote
from .wire import Refused

# The board through the workers' App (policy.yaml board).
BOARD_READ = {"organization_projects": "read", "issues": "read", "metadata": "read"}
BOARD_QUERY = """
query($org: String!, $number: Int!, $after: String, $status: String!, $order: String!) {
  organization(login: $org) { projectV2(number: $number) { items(first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      status: fieldValueByName(name: $status) { ... on ProjectV2ItemFieldSingleSelectValue { name } }
      order: fieldValueByName(name: $order) { ... on ProjectV2ItemFieldNumberValue { number } }
      content { __typename ... on Issue { number title state repository { nameWithOwner } author { login }
        labels(first: 30) { nodes { name } } assignees(first: 10) { nodes { login } } } }
    } } } }
}"""

logger = logging.getLogger(__name__)

# What an issue's comment says it waits on, as Bramble writes it when a claim
# comes off (bramble-duties, section 6, item 5): "Waits on #N", "waits on
# Thornswood-Ops #N", "Waits on ThornswoodRP/skymp#N", anywhere in it.
WAITS_ON = re.compile(r"\bwaits on\s+(?:([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)\s*)?#(\d+)", re.I)

WRITE = {
    "push": {"contents": "write", "metadata": "read"},
    "push_workflows": {"contents": "write", "workflows": "write", "metadata": "read"},
    "pulls": {"pull_requests": "write", "contents": "read", "metadata": "read"},
    "merge": {"contents": "write", "pull_requests": "write", "metadata": "read"},
    "issues": {"issues": "write", "metadata": "read"},
    # A label or a comment on a pull request goes through the issues API but
    # GitHub holds it to pull_requests write: issues write alone is a 403
    # (measured on Thornswood-Ops #220, 2 Nov 2026).
    "pr_conversation": {"pull_requests": "write", "metadata": "read"},
    "actions": {"actions": "write", "contents": "read", "metadata": "read"},
}
READ = {"contents": "read", "pull_requests": "read", "issues": "read", "actions": "read", "checks": "read", "metadata": "read"}
CHECKS_APP = "github-actions"
# Each App's own login, as GitHub names the author of what it writes
# (Thornswood records/github-apps.json, the slugs).
APP_LOGIN = {"bramble": "thornswood-bramble[bot]", "workers": "thornswood-workers[bot]"}
QUEUE_STATE = """query($owner: String!, $name: String!, $number: Int!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    mergeQueue(branch: $branch) { id }
    pullRequest(number: $number) { id headRefOid isInMergeQueue merged autoMergeRequest { enabledAt } }
  }
}"""
ENQUEUE = """mutation($pr: ID!, $head: GitObjectID!) {
  enqueuePullRequest(input: {pullRequestId: $pr, expectedHeadOid: $head}) { mergeQueueEntry { id position } }
}"""
# GitHub's auto-merge at one exact head (policy merge.auto_merge): GitHub
# merges once the checks the base branch's rules require pass, through the
# merge queue where the branch has one, whose own method then applies.
AUTO_MERGE = """mutation($pr: ID!, $head: GitObjectID!, $method: PullRequestMergeMethod) {
  enablePullRequestAutoMerge(input: {pullRequestId: $pr, expectedHeadOid: $head, mergeMethod: $method}) {
    pullRequest { autoMergeRequest { enabledAt } }
  }
}"""
# Auto-merge off again (Ops #272): before a new head goes on a branch whose
# pull request has it on, since GitHub leaves it on for a pusher with write.
AUTO_MERGE_OFF = """mutation($pr: ID!) {
  disablePullRequestAutoMerge(input: {pullRequestId: $pr}) { pullRequest { autoMergeRequest { enabledAt } } }
}"""
WORKFLOW_DIR = ".github/workflows/"
# A Co-authored-by line in a commit message: the credit for someone else's work.
CO_AUTHOR = re.compile(r"^co-authored-by:[ \t]*(.*?)[ \t]*<([^>\n]+)>[ \t]*$", re.IGNORECASE | re.MULTILINE)
# A Carried-from line: the outside pull request whose work a commit carries
# (bramble-duties section 3), what a "merge this one" answer is bound to.
CARRIED_FROM = re.compile(r"^carried-from:[ \t]*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)[ \t]*$",
                          re.IGNORECASE | re.MULTILINE)


class RemoteDecisions:
    """GitHubRules' decision store, answered by tw-policy.

    A decision core card id (dec_...) is a Main card (thornswood_decisions/core.py):
    tw-git reads Patrick's answer on it from the core, on its own socket, and
    has tw-policy record an "authorize" (decision_import) before the check, so
    his tap in the app is the record. Any other id is one root recorded."""

    def __init__(self, ask: Callable[[str, dict], dict], ask_decide: Callable[[str, dict], object] | None = None):
        self.ask = ask
        self.ask_decide = ask_decide

    def check(self, decision_id, *, kind, repo, commit, revision=None, pr=None, at=None) -> Verdict:
        if isinstance(decision_id, str) and decision_id.startswith("dec_"):
            no = self._bring(decision_id)
            if no:
                return Verdict.no(no)
        r = self.ask("decision_check", {"id": decision_id, "kind": kind, "repo": repo, "commit": commit,
                                        "revision": revision, "pr": pr})
        return Verdict(bool(r["allowed"]), r["reason"])

    def _bring(self, decision_id: str) -> str | None:
        """Patrick's answer on a Main card, recorded in tw-policy; or why not."""
        if self.ask_decide is None:
            return (f"{decision_id} is a Main card, and tw-git has no route to the decision core to read "
                    "Patrick's answer on it")
        try:
            a = self.ask_decide("main_answer", {"decision_id": decision_id})
        except Refused as e:
            return f"the decision core did not answer for card {decision_id}: {e}"
        if a is None:
            return f"Patrick has not answered Main card {decision_id} on its current revision"
        if not isinstance(a, dict) or a.get("decision_id") != decision_id:
            return f"the decision core's answer for card {decision_id} is not one tw-git reads"
        if a.get("option") != "authorize":
            return f"Patrick answered {a.get('option')!r} on Main card {decision_id}, which authorizes nothing"
        try:
            self.ask("decision_import", {"answer": a})
        except Refused as e:
            return str(e)
        return None


BASE_BUNDLE = re.compile(r"(?P<line>[A-Za-z0-9._-]+)-[0-9a-f]{12}\.bundle")


def branches_file(repo: str) -> str:
    """The file in spool/in/<bot> naming the Bot's branches on GitHub in repo."""
    return f"branches-{repo.split('/')[1]}.json"


def prune_base_bundles(folder: Path, *, current: str | None = None, half_made: bool = False) -> list[str]:
    """A Bot's spool/in keeps only its newest base bundle for each repo and
    base, and no bundle a crash left half made. A base bundle is read once,
    by the checkout that starts a tree from it, and a newer base replaces
    it; until 11 Nov 2026 none was ever deleted, 16 GB of them filled the
    VM's disk past the fleet queue's floor and no Bot turn was admitted.
    current is the bundle just handed out, which is its line's newest;
    without it the newest file of each line stays. half_made is for tw-git's
    start only, when no bundle is being written."""
    newest: dict[str, tuple[float, str]] = {}
    gone: list[str] = []
    with os.scandir(folder) as it:
        entries = [e for e in it if e.is_file(follow_symlinks=False)]
    for e in entries:
        if e.name.startswith(".") and e.name.endswith(".bundle.tmp"):
            continue
        m = BASE_BUNDLE.fullmatch(e.name)
        if not m:
            continue
        when = e.stat(follow_symlinks=False).st_mtime
        if m["line"] not in newest or (when, e.name) > newest[m["line"]]:
            newest[m["line"]] = (when, e.name)
    if current and BASE_BUNDLE.fullmatch(current):
        newest[BASE_BUNDLE.fullmatch(current)["line"]] = (float("inf"), current)
    keep = {name for _, name in newest.values()}
    for e in entries:
        stale_tmp = half_made and e.name.startswith(".") and e.name.endswith(".bundle.tmp")
        if stale_tmp or (BASE_BUNDLE.fullmatch(e.name) and e.name not in keep):
            os.unlink(folder / e.name)
            gone.append(e.name)
    return gone


class GitService:
    def __init__(self, policy: Policy, github: GitHub, ask_policy: Callable[[str, dict], dict], *,
                 mirrors: Path, spool: Path, git_base: str = "https://github.com/",
                 git_env: dict | None = None, allow_file_protocol: bool = False,
                 ask_decide: Callable[[str, dict], object] | None = None):
        self.policy = policy
        self.gh = github
        self.ask = ask_policy
        # tw-git's read of Patrick's answers on outside pull request cards
        # (#132) and Main cards (step 6): tw-decide-bots' socket for tw-git alone.
        self.ask_decide = ask_decide
        self.rules = GitHubRules(policy, RemoteDecisions(ask_policy, ask_decide))
        self.mirrors = Path(mirrors)
        self.spool = Path(spool)
        self.git_base = git_base
        self.git_env = git_env or {}
        self.allow_file_protocol = allow_file_protocol

    # Helpers

    def _repo(self, args: dict) -> str:
        repo = args.get("repo")
        repos = self.policy.data["repos"]
        if repo == repos["hermes"]:
            raise Refused("no Bot writes to TWHermesAgents; every change there is Patrick's")
        if repo not in (repos["app"], repos["fork"], repos["ops"]):
            raise Refused(f"{repo} is not a repo the Bots work in")
        return repo

    def _bot(self, args: dict) -> str:
        bot = args.get("bot")
        if not isinstance(bot, str) or not self.policy.is_bot(bot):
            raise Refused(f"{bot!r} is not one of the six Bots")
        return bot

    def _task(self, args: dict) -> str:
        task = str(args.get("task") or "")
        if not task:
            raise Refused("every write names the task it is for")
        return task

    def _app(self, bot: str) -> str:
        return self.policy.app_of(bot)

    def _git(self, repo_dir: Path, *args: str, check: bool = True, extra_config: list[str] | None = None) -> str:
        safe = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "credential.helper=",
                "-c", "submodule.recurse=false", "-c", "transfer.fsckObjects=true",
                "-c", f"protocol.file.allow={'always' if self.allow_file_protocol else 'never'}"]
        for c in extra_config or []:
            safe += ["-c", c]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.mirrors), "GIT_TERMINAL_PROMPT": "0",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", **self.git_env}
        r = subprocess.run(["git", "-C", str(repo_dir), *safe, *args], capture_output=True, text=True, env=env)
        if check and r.returncode != 0:
            raise Refused(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-400:]}")
        return r.stdout.strip()

    def _mirror(self, repo: str, app: str) -> Path:
        path = self.mirrors / (repo.replace("/", "__") + ".git")
        path.parent.mkdir(parents=True, exist_ok=True)
        auth = [] if self.git_base.startswith("file:") else [f"http.extraHeader={self.gh.git_auth_header(app, repo, {'contents': 'read', 'metadata': 'read'})}"]
        # One fetch of a mirror at a time. tw-git answers each call on its own
        # thread, and two fetches of the same mirror moved the same refs under
        # each other: a push of echo/4394-hub-signout-target failed on 11 Nov
        # 2026 with "cannot lock ref 'refs/remotes/origin/main': is at 472ffbd
        # but expected f6ae996" (Ops #293). The second waits for the first.
        with open(path.parent / (path.name + ".fetching"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            if not path.exists():
                subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True, capture_output=True)
            self._git(path, "fetch", "-q", "--prune", self._url(repo), "+refs/heads/*:refs/remotes/origin/*",
                      extra_config=auth)
        return path

    def _url(self, repo: str) -> str:
        return f"{self.git_base}{repo}" + ("" if self.git_base.startswith("file:") else ".git")

    def _begin(self, bot, task, action, repo, target, commit, undone: bool = False) -> dict:
        args = {"bot": bot, "task": task, "action": action, "repo": repo, "target": target, "commit": commit}
        if undone:
            args["undone"] = True
        return self.ask("op_begin", args)

    def _finish(self, key: str, outcome: str, receipt=None, reason: str | None = None) -> None:
        self.ask("op_finish", {"key": key, "outcome": outcome, "receipt": receipt, "reason": reason})

    def _guarded_write(self, bot, task, action, repo, target, commit, *, write: Callable[[], object],
                       read_back: Callable[[], object | None], undone: bool = False) -> dict:
        """op_begin, the one write, read back, report. read_back returns a
        receipt when the write is visible on GitHub, None when it is not.
        undone: the caller read GitHub just now and the write's effect is not
        there, so a receipt for the same write is of a past state."""
        op = self._begin(bot, task, action, repo, target, commit, undone)
        if op["state"] == "reconcile":
            found = read_back()
            self.ask("op_reconcile", {"key": op["key"], "found": found is not None, "receipt": found})
            if found is not None:
                return {"done": True, "reconciled": True, "receipt": found}
            op = self._begin(bot, task, action, repo, target, commit, undone)
        try:
            answer = write()
        except GitHubError as e:
            if e.status and 400 <= e.status < 500:
                self._finish(op["key"], "failed", reason=str(e))
                raise Refused(f"GitHub refused {action}: {e}") from e
            self._finish(op["key"], "unknown", reason=str(e))
            raise Refused(f"{action} on {target}: GitHub's answer was lost ({e}); it will be read back before anything else") from e
        except Refused as e:
            self._finish(op["key"], "failed", reason=str(e))
            raise
        except Exception as e:
            self._finish(op["key"], "unknown", reason=f"{type(e).__name__}: {e}")
            raise Refused(f"{action} on {target}: the outcome is unknown ({type(e).__name__}); it will be read back before anything else") from e
        receipt = read_back()
        if receipt is None:
            self._finish(op["key"], "unknown", {"answer": answer}, "the write was sent but is not visible on GitHub yet")
            raise Refused(f"{action} on {target} was sent but GitHub does not show it yet; it will be read back before anything else")
        self._finish(op["key"], "confirmed", receipt)
        return {"done": True, "receipt": receipt}

    def _check(self, v: Verdict) -> None:
        if not v.allowed:
            raise Refused(v.reason)

    # Reads GitHub itself

    def _pr(self, repo: str, number: int) -> PullRequest:
        raw = self.gh.get("bramble", repo, f"/repos/{repo}/pulls/{int(number)}")
        head_repo = ((raw.get("head") or {}).get("repo") or {}).get("full_name")
        head_ref = raw["head"]["ref"]
        author_bot = self.policy.branch_owner(head_ref) if head_repo == repo else None
        files = []
        for f in self.gh.get_all("bramble", repo, f"/repos/{repo}/pulls/{int(number)}/files"):
            files.append(f["filename"])
            if f.get("previous_filename"):
                files.append(f["previous_filename"])
        if raw.get("changed_files", 0) > len({f for f in files}):
            raise Refused(f"PR {number} changes {raw['changed_files']} files and GitHub listed {len(files)}; nothing merges unseen")
        # Whose work it holds: every commit's author and each Co-authored-by
        # line (Patrick, 8 Nov 2026: outside work merges only on his word).
        commits = self.gh.get_all("bramble", repo, f"/repos/{repo}/pulls/{int(number)}/commits")
        if raw.get("commits", 0) > len(commits):
            raise Refused(f"PR {number} has {raw['commits']} commits and GitHub listed {len(commits)}; nothing merges unseen")
        credits, carried_from = [], []
        for c in commits:
            sha, body = c.get("sha", ""), c.get("commit") or {}
            author = body.get("author") or {}
            credits.append(Credit(commit=sha, name=author.get("name") or "", email=author.get("email") or "",
                                  login=(c.get("author") or {}).get("login")))
            for name, email in CO_AUTHOR.findall(body.get("message") or ""):
                credits.append(Credit(commit=sha, name=name, email=email, co_author=True))
            for r, n in CARRIED_FROM.findall(body.get("message") or ""):
                if (r, int(n)) not in carried_from:
                    carried_from.append((r, int(n)))
        body_carried = []
        for r, n in CARRIED_FROM.findall(raw.get("body") or ""):
            if (r, int(n)) not in body_carried:
                body_carried.append((r, int(n)))
        return PullRequest(repo=repo, number=int(number), head_sha=raw["head"]["sha"], head_ref=head_ref,
                           base_ref=raw["base"]["ref"], author_bot=author_bot, author_login=raw["user"]["login"],
                           files=files, labels=[l["name"] for l in raw.get("labels", [])],
                           draft=bool(raw.get("draft")), merged=bool(raw.get("merged")), credits=credits,
                           carried_from=carried_from, body_carried_from=body_carried)

    def _merged_pr(self, repo: str, number: int) -> PullRequest:
        """A pull request as it stands, without its files and commits: all
        that closing an issue as done by it asks is whether it merged. _pr
        reads every file and commit so that nothing merges unseen, and it
        refused a merged pull request over 250 commits, so its issue could
        not be closed (Ops #307, 12 Nov 2026: Thornswood #4536 and PR 5381,
        280 commits)."""
        raw = self.gh.get("bramble", repo, f"/repos/{repo}/pulls/{int(number)}")
        return PullRequest(repo=repo, number=int(number), head_sha=raw["head"]["sha"], head_ref=raw["head"]["ref"],
                           base_ref=raw["base"]["ref"], author_bot=None, author_login=raw["user"]["login"],
                           merged=bool(raw.get("merged")))

    def _checks(self, repo: str, sha: str) -> list[CheckRun]:
        runs = self.gh.get_all("bramble", repo, f"/repos/{repo}/commits/{sha}/check-runs?filter=all", key="check_runs")
        # Only GitHub Actions' own runs count: an App that can post checks
        # cannot post a green one by name.
        runs = [r for r in runs if ((r.get("app") or {}).get("slug") == CHECKS_APP)]
        runs.sort(key=lambda r: r["id"])
        return [CheckRun(name=r["name"], head_sha=r["head_sha"], status=r["status"], conclusion=r.get("conclusion")) for r in runs]

    def _queue_state(self, repo: str, number: int, base: str) -> dict:
        """The base branch's merge queue (None when it has none), and the PR's
        node id, head and whether it is in the queue, from GraphQL."""
        owner, name = repo.split("/", 1)
        data = self.gh.graphql("bramble", repo, READ, QUEUE_STATE,
                               {"owner": owner, "name": name, "number": int(number), "branch": base})
        r = data.get("repository") or {}
        p = r.get("pullRequest") or {}
        return {"queue": (r.get("mergeQueue") or {}).get("id"), "id": p.get("id"), "head": p.get("headRefOid"),
                "queued": bool(p.get("isInMergeQueue")), "merged": bool(p.get("merged")),
                "auto": bool(p.get("autoMergeRequest"))}

    def _branch_checks(self, repo: str, branch: str) -> set[str]:
        """The status checks GitHub's rules require on this branch, read from
        GitHub itself, so auto-merge waits for what the policy names."""
        rules = self.gh.get_all("bramble", repo, f"/repos/{repo}/rules/branches/{quote(branch)}")
        return {c.get("context") for r in rules if r.get("type") == "required_status_checks"
                for c in ((r.get("parameters") or {}).get("required_status_checks") or []) if c.get("context")}

    def _verb_lists(self, repo: str, ref: str) -> dict[str, dict]:
        out = {}
        for path in self.policy.data["runs"]["verb_lists"]:
            try:
                raw = self.gh.get("bramble", repo, f"/repos/{repo}/contents/{path}?ref={quote(ref)}")
            except GitHubError as e:
                if e.status == 404:
                    continue
                raise
            out[path] = json.loads(base64.b64decode(raw["content"]))
        return out

    def _issue(self, repo: str, number: int) -> Issue:
        raw = self.gh.get(self.policy.reader_for(repo), repo, f"/repos/{repo}/issues/{int(number)}")
        return Issue(repo=repo, number=int(number), author_login=raw["user"]["login"],
                     assignees=[a["login"] for a in raw.get("assignees", [])],
                     labels=[l["name"] for l in raw.get("labels", [])], open=raw.get("state") == "open",
                     pull_request=bool(raw.get("pull_request")))

    def _ref_sha(self, repo: str, branch: str) -> str | None:
        try:
            raw = self.gh.get(self.policy.reader_for(repo), repo, f"/repos/{repo}/git/ref/heads/{quote(branch)}")
        except GitHubError as e:
            if e.status == 404:
                return None
            raise
        return raw["object"]["sha"]

    # Ops

    def push(self, caller: str, args: dict) -> dict:
        bot, repo, task = self._bot(args), self._repo(args), self._task(args)
        branch = str(args.get("branch") or "")
        bundle = self._carried_bundle(bot, branch, str(args.get("bundle") or ""))
        app = self._app(bot)
        mirror = self._mirror(repo, app)
        incoming = f"refs/tw/in/{bot}/{branch}"
        self._git(mirror, "bundle", "verify", "-q", str(bundle))
        # Git reads a bundle through its file transport, which is off for
        # every other fetch here; it is on for this one path, which _bundle
        # checked is a plain file in this Bot's spool.
        self._git(mirror, "fetch", "-q", str(bundle), f"+refs/heads/{branch}:{incoming}",
                  extra_config=["protocol.file.allow=always"])
        new = self._git(mirror, "rev-parse", incoming)
        base_branch = self.policy.data["merge"]["base"][repo]
        base = self._git(mirror, "merge-base", new, f"refs/remotes/origin/{base_branch}", check=False)
        if not SHA.fullmatch(base or ""):
            raise Refused(f"{branch} shares no history with {base_branch}")
        names = self._git(mirror, "log", "--format=", "--name-only", f"{base}..{new}").splitlines()
        changed = sorted({n for n in names if n})
        limit = self.policy.data["brokers"]["files"]["max_changed_files"]
        if len(changed) > limit:
            raise Refused(f"{branch} changes {len(changed)} files; a task changes at most {limit}")
        workflows = any(n.startswith(WORKFLOW_DIR) for n in changed)
        remote = self._git(mirror, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}", check=False) or None
        rewrites = bool(remote) and subprocess.run(
            ["git", "-C", str(mirror), "merge-base", "--is-ancestor", remote, new]).returncode != 0
        self._check(self.rules.push(bot, repo, f"refs/heads/{branch}", force_with_lease=rewrites,
                                    workflow_files_changed=workflows))
        push_app = "bramble" if (workflows and bot == "bramble") else app
        perms = WRITE["push_workflows"] if push_app == "bramble" and workflows else WRITE["push"]

        def write():
            spec = [f"{new}:refs/heads/{branch}"]
            lease = [f"--force-with-lease=refs/heads/{branch}:{remote or ''}"] if rewrites or remote is None else []
            # The token is minted for the push App even in the tests' file://
            # world, so the App and permissions a push uses are what is tested.
            header = self.gh.git_auth_header(push_app, repo, perms)
            auth = [] if self.git_base.startswith("file:") else [f"http.extraHeader={header}"]
            self._git(mirror, "push", "-q", *lease, self._url(repo), *spec, extra_config=auth)
            return {"pushed": new}

        def read_back():
            return {"ref": f"refs/heads/{branch}", "sha": new} if self._ref_sha(repo, branch) == new else None

        if remote and remote != new:
            self._auto_merge_off(bot, task, repo, branch, remote)
        out = self._guarded_write(bot, task, "push", repo, f"refs/heads/{branch}", new, write=write, read_back=read_back)
        out.update({"sha": new, "changed_files": len(changed), "workflow_change": workflows})
        return out

    def _auto_merge_off(self, bot: str, task: str, repo: str, branch: str, head: str) -> None:
        """Before a new head goes on a branch, auto-merge comes off its open
        pull request, so nothing merges at a head Bramble did not merge.
        Bramble turns auto-merge on at one exact head, but GitHub leaves it on
        when someone with write access pushes again, and the workers' App has
        contents write: Thornswood #5651 (9 Nov 2026) went into the merge
        queue at a new head before anyone read it, and #5744 (11 Nov) merged
        at a head Bramble had said it was holding (Ops #272). A merge at the
        new head is a new merge op. When it cannot be turned off, the push
        does not go."""
        owner = repo.split("/", 1)[0]
        for raw in self.gh.get_all("bramble", repo, f"/repos/{repo}/pulls?state=open&head={quote(owner + ':' + branch)}"):
            if raw.get("state") != "open" or (raw.get("head") or {}).get("ref") != branch or not raw.get("auto_merge"):
                continue
            number = int(raw["number"])
            node = self._queue_state(repo, number, raw["base"]["ref"])["id"]

            def write(node=node):
                data = self.gh.graphql("bramble", repo, WRITE["merge"], AUTO_MERGE_OFF, {"pr": node})
                return {"auto_merge": bool((((data.get("disablePullRequestAutoMerge") or {}).get("pullRequest") or {})
                                            .get("autoMergeRequest")))}

            def read_back(number=number):
                now = self.gh.get("bramble", repo, f"/repos/{repo}/pulls/{number}")
                return {"auto_merge": False} if not now.get("auto_merge") else None

            # undone: it was read on just now, so an earlier "off" at this
            # head is of a past state (Bramble turned it on again since).
            self._guarded_write(bot, task, "auto-merge-off", repo, f"pr/{number}", head,
                                write=write, read_back=read_back, undone=True)

    def _carried_bundle(self, bot: str, branch: str, name: str) -> Path:
        """The bundle a push sends: from the Bot's own spool, or, when Bramble
        carries a workflow change for a Bot it may push for (policy branches
        bramble_may_push_for), from that Bot's spool, where its tw-files made
        it. Bramble cannot make it: its own tree is not the worker's, and the
        push must carry the worker's commit as it is. Until 12 Nov 2026 only
        Bramble's spool was read, so every such push was refused ("is not a
        bundle in bramble's spool", Ops #277), and the workers' workflow fixes
        (#4135, #5765, #2760, #5706 and more) could never reach GitHub. Whether the push
        may go at all is still the push rule's (github.py push): Bramble pushes
        to that branch only a change to a workflow file."""
        owner = self.policy.branch_owner(branch)
        if bot == "bramble" and owner in self.policy.data["branches"]["bramble_may_push_for"]:
            try:
                return self._bundle(owner, name)
            except Refused:
                pass
        return self._bundle(bot, name)

    def _bundle(self, bot: str, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}\.bundle", name):
            raise Refused("name the bundle tw-files made, by its file name")
        root = (self.spool / "out" / bot).resolve()
        path = (root / name).resolve()
        if path.parent != root or not path.is_file() or path.is_symlink():
            raise Refused(f"{name} is not a bundle in {bot}'s spool")
        return path

    def base(self, caller: str, args: dict) -> dict:
        """The base branch as a bundle in spool/in/<bot>, for tw-files to
        start a task's tree from. Read only.

        With branch, one of the Bot's own branches that GitHub has, the
        bundle carries that branch's head too, and tw-files opens the tree
        there: a fix to an open pull request goes on top of its head instead
        of being rebuilt on today's base and force-pushed, which made its
        review start over (Ops #311, 12 Nov 2026: Magpie's #5865)."""
        bot, repo = self._bot(args), self._repo(args)
        branch = str(args.get("branch") or "")
        if branch and not self.policy.own_branch(bot, branch):
            raise Refused(f"{branch} is not {self.policy.bot_name(bot)}'s; a tree opens only on a branch of its own")
        if repo == self.policy.data["repos"]["fork"] and not self.policy.bots[bot].get("fork_branches"):
            raise Refused(f"only Wisp works in the engine fork, not {self.policy.bot_name(bot)}")
        if not self.policy.app_reaches(self._app(bot), repo):
            raise Refused(f"{self.policy.bot_name(bot)} does not work in {repo}; it is Bramble's alone")
        base = self.policy.data["merge"]["base"][repo]
        mirror = self._mirror(repo, self._app(bot))
        sha = self._git(mirror, "rev-parse", f"refs/remotes/origin/{base}")
        self._git(mirror, "update-ref", f"refs/heads/{base}", sha)
        tip = None
        if branch:
            tip = self._git(mirror, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}^{{commit}}",
                            check=False)
            if not SHA.fullmatch(tip or ""):
                raise Refused(f"GitHub has no branch {branch} in {repo}; leave branch out to start from {base}")
        with_branch = bool(tip) and tip != sha
        out = self.spool / "in" / bot
        out.mkdir(parents=True, exist_ok=True)
        # One line per repo and base, so a bundle with a branch in it is the
        # newest of its line and the next base call prunes it like any other.
        name = f"{repo.split('/')[1]}-{base.replace('/', '-')}-{(tip if with_branch else sha)[:12]}.bundle"
        if not (out / name).exists():
            tmp = out / f".{name}.tmp"
            if with_branch:
                ref = f"refs/heads/{branch}"
                self._git(mirror, "update-ref", ref, tip)
                try:
                    self._git(mirror, "bundle", "create", "-q", str(tmp), f"refs/heads/{base}", ref)
                finally:
                    self._git(mirror, "update-ref", "-d", ref, check=False)
            else:
                self._git(mirror, "bundle", "create", "-q", str(tmp), f"refs/heads/{base}")
            os.replace(tmp, out / name)
        prune_base_bundles(out, current=name)
        # The Bot's branches GitHub has now, read from the mirror the fetch
        # above pruned, for tw-files: a pushed tree whose branch is gone
        # (merged, and GitHub deleted it, or deleted by hand) is finished.
        on_github = sorted(b for b in self._git(mirror, "for-each-ref", "--format=%(refname:lstrip=3)",
                                                "refs/remotes/origin/").split("\n")
                           if b and self.policy.own_branch(bot, b))
        listed = out / branches_file(repo)
        tmp = out / f".{listed.name}.tmp"
        tmp.write_text(json.dumps({"repo": repo, "branches": on_github}))
        os.replace(tmp, listed)
        got = {"bundle": name, "base": base, "sha": sha}
        if branch:
            got.update(branch=branch, branch_sha=tip)
        return got

    def prune_spool_in(self) -> list[str]:
        """At start, each Bot's spool/in back to its newest base per repo."""
        root = self.spool / "in"
        gone: list[str] = []
        if root.is_dir():
            for d in sorted(root.iterdir()):
                if d.is_dir() and not d.is_symlink():
                    gone += prune_base_bundles(d, half_made=True)
        return gone

    def open_pr(self, caller: str, args: dict) -> dict:
        bot, repo, task = self._bot(args), self._repo(args), self._task(args)
        head, base = str(args.get("head") or ""), str(args.get("base") or self.policy.data["merge"]["base"][repo])
        title, body = str(args.get("title") or ""), str(args.get("body") or "")
        if not title:
            raise Refused("a pull request has a title")
        self._check(self.rules.open_pr(bot, repo, head, base))
        sha = self._ref_sha(repo, head)
        if sha is None:
            raise Refused(f"{head} is not on GitHub; push it first")
        owner = repo.split("/")[0]
        app = "bramble" if bot == "bramble" else self._app(bot)
        # One open pull request per issue per Bot. 8 Nov 2026, 6:12 PM UTC:
        # Bramble opened #5502 and #5503 for #5495 fifteen seconds apart from
        # two branches, and at 6:39 PM #5508 and #5509, so both of the first
        # merged and the second pair was needed to take a line out again.
        issue = re.match(rf"^{re.escape(bot)}/(\d+)-", head)
        if issue:
            mine = re.compile(rf"^{re.escape(bot)}/{issue.group(1)}-")
            for other in self.gh.get(app, repo, f"/repos/{repo}/pulls?state=open&per_page=100") or []:
                ref = (other.get("head") or {}).get("ref") or ""
                if ref != head and mine.match(ref):
                    raise Refused(f"PR {other.get('number')} from {ref} is already open for #{issue.group(1)}; "
                                  "push to its branch instead of opening a second one")

        def find():
            prs = self.gh.get(app, repo, f"/repos/{repo}/pulls?state=all&head={quote(owner + ':' + head)}")
            return {"number": prs[0]["number"], "url": prs[0]["html_url"]} if prs else None

        def write():
            r = self.gh.call(app, repo, WRITE["pulls"], "POST", f"/repos/{repo}/pulls",
                             {"title": title, "body": body, "head": head, "base": base, "draft": bool(args.get("draft"))})
            if r.status != 201:
                raise GitHubError(r.status, str((r.body or {}).get("message") if isinstance(r.body, dict) else r.body))
            return {"number": r.body["number"]}

        return self._guarded_write(bot, task, "open_pr", repo, f"pr-open/{head}", sha, write=write, read_back=find)

    def edit_pr(self, caller: str, args: dict) -> dict:
        bot, repo, task = self._bot(args), self._repo(args), self._task(args)
        pr = self._pr(repo, args.get("number"))
        self._check(self.rules.edit_pr(bot, pr))
        fields = {k: args[k] for k in ("title", "body") if isinstance(args.get(k), str)}
        if not fields:
            raise Refused("an edit changes the title or the body")
        app = "bramble" if bot == "bramble" else self._app(bot)
        digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:16]

        def write():
            r = self.gh.call(app, repo, WRITE["pulls"], "PATCH", f"/repos/{repo}/pulls/{pr.number}", fields)
            if r.status != 200:
                raise GitHubError(r.status, str(r.body))
            return {"number": pr.number}

        def read_back():
            raw = self.gh.get(app, repo, f"/repos/{repo}/pulls/{pr.number}")
            return {"number": pr.number} if all(raw.get(k) == v for k, v in fields.items()) else None

        return self._guarded_write(bot, task, "edit_pr", repo, f"pr/{pr.number}/{digest}", pr.head_sha, write=write, read_back=read_back)

    def comment(self, caller: str, args: dict) -> dict:
        bot, repo, task = self._bot(args), self._repo(args), self._task(args)
        number, body = int(args.get("number")), str(args.get("body") or "")
        if not body.strip():
            raise Refused("a comment says something")
        if (no := self.rules._outside_app(bot, repo)) is not None:
            raise Refused(no.reason)
        issue_raw = self.gh.get(self.policy.reader_for(repo), repo, f"/repos/{repo}/issues/{number}")
        if issue_raw.get("pull_request"):
            self._check(self.rules.comment_pr(bot, self._pr(repo, number)))
        else:
            issue = self._issue(repo, number)
            if self.rules._hands_off_issue(issue):
                raise Refused("Djinn's and Jhinswife's work is hands off")
            own = self.policy.bots[bot]["claim_label"] in issue.labels
            if not (own or self.policy.is_router(bot)):
                raise Refused(f"{self.policy.bot_name(bot)} comments only on issues it has claimed")
        app = "bramble" if bot == "bramble" else self._app(bot)
        perms = WRITE["pr_conversation"] if issue_raw.get("pull_request") else WRITE["issues"]
        digest = hashlib.sha256(body.encode()).hexdigest()[:16]
        marker = f"<!-- tw:{digest} -->"

        def find():
            for c in self.gh.get_all(app, repo, f"/repos/{repo}/issues/{number}/comments"):
                if marker in (c.get("body") or ""):
                    return {"comment": c["id"]}
            return None

        def write():
            r = self.gh.call(app, repo, perms, "POST", f"/repos/{repo}/issues/{number}/comments", {"body": f"{body}\n\n{marker}"})
            if r.status != 201:
                raise GitHubError(r.status, str(r.body))
            return {"comment": r.body["id"]}

        return self._guarded_write(bot, task, "comment", repo, f"issue/{number}/{digest}", "", write=write, read_back=find)

    def open_issue(self, caller: str, args: dict) -> dict:
        """A new issue, unclaimed, through the Bot's own App. Any Bot files what it
        finds; an answered card's DECIDED.md line needs one when the card has no issue
        (src/thornswood_decisions/chat.py _decided_text). Patrick's answer on
        dec_zf92laU0xp8mvmyfa8C7pA, 2 Nov 2026: five of his answers waited on it.

        The body ends with the write's marker, so a lost answer is found by it and
        never filed twice. No label and no assignee: Todo, unclaimed."""
        bot, repo, task = self._bot(args), self._repo(args), self._task(args)
        title, body = str(args.get("title") or ""), str(args.get("body") or "")
        self._check(self.rules.open_issue(bot, repo, title, body))
        app = "bramble" if bot == "bramble" else self._app(bot)
        digest = hashlib.sha256(json.dumps([repo, title, body]).encode()).hexdigest()[:16]
        marker = f"<!-- tw:{digest} -->"

        def find():
            path = (f"/repos/{repo}/issues?state=all&creator={quote(APP_LOGIN[app])}"
                    "&sort=created&direction=desc&per_page=100")
            for i in self.gh.get(app, repo, path):
                if not i.get("pull_request") and marker in (i.get("body") or ""):
                    return {"number": i["number"], "url": i.get("html_url")}
            return None

        def write():
            r = self.gh.call(app, repo, WRITE["issues"], "POST", f"/repos/{repo}/issues",
                             {"title": title, "body": f"{body}\n\n{marker}"})
            if r.status != 201:
                raise GitHubError(r.status, str((r.body or {}).get("message") if isinstance(r.body, dict) else r.body))
            return {"number": r.body["number"]}

        return self._guarded_write(bot, task, "open_issue", repo, f"issue-new/{digest}", "", write=write, read_back=find)

    def label(self, caller: str, args: dict) -> dict:
        bot, repo, task = self._bot(args), self._repo(args), self._task(args)
        number, label, action = int(args.get("number")), str(args.get("label") or ""), args.get("action")
        issue = self._issue(repo, number)
        if action == "add":
            v = self.rules.add_label(bot, issue, label, decision_id=args.get("decision_id"))
        elif action == "remove":
            v = self.rules.remove_label(bot, issue, label, decision_id=args.get("decision_id"),
                                        cutover_commit=str(args.get("cutover_commit") or ""))
        else:
            raise Refused("a label is added or removed")
        self._check(v)
        # A label is a state on the issue, read just now: one already as asked
        # needs no write, and one that is not is a write to make now, whatever
        # an older receipt for the same label says (Thornswood-Ops #280).
        if (label in issue.labels) == (action == "add"):
            return {"done": True, "already": True, "receipt": {"label": label, "on": action == "add"}}
        if action == "add" and label in self.policy.claim_labels() and not issue.pull_request:
            waits = self._waits_on(repo, number)
            if waits:
                raise Refused(f"#{number} waits on {waits}, which is still open (the issue's newest comment says so), "
                              f"so it is not eligible and takes no claim until that closes: pick the next issue")
        app = "bramble" if bot == "bramble" else self._app(bot)
        perms = WRITE["pr_conversation"] if issue.pull_request else WRITE["issues"]

        def write():
            if action == "add":
                r = self.gh.call(app, repo, perms, "POST", f"/repos/{repo}/issues/{number}/labels", {"labels": [label]})
            else:
                r = self.gh.call(app, repo, perms, "DELETE", f"/repos/{repo}/issues/{number}/labels/{quote(label)}")
            if r.status not in (200, 204):
                raise GitHubError(r.status, str(r.body))
            return {"status": r.status}

        def read_back():
            has = label in self._issue(repo, number).labels
            return {"label": label, "on": has} if has == (action == "add") else None

        return self._guarded_write(bot, task, f"label-{action}", repo, f"issue/{number}/{label}", "", write=write,
                                   read_back=read_back, undone=True)

    def _waits_on(self, repo: str, number: int) -> str | None:
        """The open issue this issue waits on, by its newest comment, or None.
        An issue whose newest comment says it waits on an open issue is not
        eligible until that issue closes (bramble-duties, section 6, item 5).
        Until 11 Nov 2026 only the skill said so, and the board's eligible
        read cannot see a comment: Thornswood #4482's claim came off at
        "Waits on Thornswood-Ops #275", went on again at 2:37 PM Mountain
        while #275 was open (the newest comment then ended "it waits on
        Thornswood-Ops #275"), and came off again. So a claim is refused
        here, where it is made."""
        comments = self.gh.get_all(self.policy.reader_for(repo), repo, f"/repos/{repo}/issues/{int(number)}/comments")
        newest = (comments[-1].get("body") or "") if comments else ""
        for m in WAITS_ON.finditer(newest):
            name, n = m.group(1), int(m.group(2))
            # A word that names no repo ("waits on PR #5797") is this repo's;
            # an owner/repo that tw-git does not read is refused.
            where = repo if not name else next(
                (r for r in self.policy.data["repos"].values() if name.lower() in (r.lower(), r.split("/")[1].lower())),
                None if "/" in name else repo)
            if where is None:
                raise Refused(f"#{number}'s newest comment says it waits on {name} #{n}, which is not a repo tw-git "
                              "reads; say on the issue what it waits on, as owner/repo#N")
            raw = self.gh.get(self.policy.reader_for(where), where, f"/repos/{where}/issues/{n}")
            if raw.get("state") == "open":
                return f"{where}#{n}"
        return None

    def _repo_label(self, repo: str, name: str) -> dict | None:
        try:
            raw = self.gh.get("bramble", repo, f"/repos/{repo}/labels/{quote(name)}")
        except GitHubError as e:
            if e.status == 404:
                return None
            raise
        return {"name": raw["name"], "color": str(raw.get("color") or "").lower(),
                "description": raw.get("description") or ""}

    def claim_labels(self, caller: str, args: dict) -> dict:
        """The six Bot claim labels in one repo, exactly as the policy names
        them (Phase 3, 04 section 6.1): made when missing, set back to the
        policy's colour and description when they differ, then read back from
        GitHub. Bramble only, through Bramble's App. Nothing the model sends
        names a label, a colour or a description, and no other label is made,
        changed or deleted."""
        bot, repo, task = self._bot(args), self._repo(args), self._task(args)
        if not self.policy.is_router(bot):
            raise Refused("only Bramble makes the claim labels")
        claims = self.policy.data["claims"]
        held = []
        for owner in self.policy.bots:
            name = self.policy.bots[owner]["claim_label"]
            want = {"name": name, "color": str(claims["label_color"]).lower(),
                    "description": claims["label_description"].format(name=self.policy.bot_name(owner))}
            have = self._repo_label(repo, name)
            if have != want:
                def write(have=have, want=want, name=name):
                    if have is None:
                        r = self.gh.call("bramble", repo, WRITE["issues"], "POST", f"/repos/{repo}/labels", want)
                        ok = r.status == 201
                    else:
                        r = self.gh.call("bramble", repo, WRITE["issues"], "PATCH", f"/repos/{repo}/labels/{quote(name)}",
                                         {"new_name": name, "color": want["color"], "description": want["description"]})
                        ok = r.status == 200
                    if not ok:
                        raise GitHubError(r.status, str(r.body))
                    return {"status": r.status}

                def read_back(want=want, name=name):
                    got = self._repo_label(repo, name)
                    return got if got == want else None

                # The target names the state it changes from, so a label that
                # drifts again later is a new operation, not "already done".
                was = hashlib.sha256(json.dumps(have, sort_keys=True).encode()).hexdigest()[:12]
                self._guarded_write(bot, task, "claim-label", repo, f"label/{name}/from/{was}", "",
                                    write=write, read_back=read_back)
            held.append(self._repo_label(repo, name))
        return {"repo": repo, "labels": held}

    def merge(self, caller: str, args: dict) -> dict:
        bot, repo, task = self._bot(args), self._repo(args), self._task(args)
        if not self.policy.is_router(bot):
            raise Refused(f"only Bramble merges; {self.policy.bot_name(bot)} never does, its own work included")
        number, sha = int(args.get("number")), str(args.get("sha") or "")
        method = str(args.get("method") or self.policy.data["merge"]["method"][repo])
        pr = self._pr(repo, number)
        queue = None  # the base branch's merge queue, read once the merge is allowed
        auto = self.policy.auto_merge(repo)

        def read_back():
            raw = self.gh.get("bramble", repo, f"/repos/{repo}/pulls/{number}")
            if raw.get("merged") and raw.get("head", {}).get("sha") == sha:
                return {"merged": True, "merge_commit_sha": raw.get("merge_commit_sha")}
            if auto or queue is not None:
                now = self._queue_state(repo, number, pr.base_ref)
                if now["head"] == sha and now["queued"]:
                    return {"queued": True}
                if now["head"] == sha and now["auto"]:
                    return {"auto_merge": True}
            return None

        if pr.merged:
            # Merged already: either our own lost answer (reconcile it) or someone else's merge.
            op = self._begin(bot, task, "merge", repo, f"pr/{number}", sha) if pr.head_sha == sha else None
            if op and op["state"] == "reconcile":
                found = read_back()
                self.ask("op_reconcile", {"key": op["key"], "found": found is not None, "receipt": found})
                return {"done": True, "reconciled": True, "receipt": found}
            if op:
                self._finish(op["key"], "failed", reason="already merged when read")
            raise Refused(f"PR {number} is already merged")
        checks = self._checks(repo, pr.head_sha)
        branch_checks = self._branch_checks(repo, pr.base_ref) if auto else None
        # Work by someone off Patrick's list merges only once he answered
        # "let them merge" for that person (#132); read only when it is needed.
        allowed = self._allowed_people(pr) if self.rules.outside_credits(pr) else None
        self._check(self.rules.merge(bot, pr, sha, method, checks, branch_checks=branch_checks,
                                     allowed_people=allowed))

        # Where GitHub's auto-merge merges (policy merge.auto_merge), this turns
        # it on at this exact head, and GitHub merges once the checks the
        # branch's rules require pass. With a merge queue on the base branch
        # (the redesign's part 5) GitHub refuses a direct merge, so a merge
        # that is not auto-merge joins the queue at this exact head. Read each
        # time, so the queue can come and go in either order with this code.
        state = self._queue_state(repo, number, pr.base_ref)
        queue = state["queue"]
        # GitHub took it off since our receipt (out of the merge queue on a
        # red queue run turns auto-merge off at the same head), so "already
        # done" would describe the past: Thornswood #5856, 12 Nov 2026, read
        # auto_merge null at 5b8162c and the merge op there answered done
        # (Ops #272).
        undone = state["head"] == sha and not (state["merged"] or state["queued"] or state["auto"])

        def write():
            if auto:
                variables = {"pr": state["id"], "head": sha, "method": None if queue else method.upper()}
                data = self.gh.graphql("bramble", repo, WRITE["merge"], AUTO_MERGE, variables)
                pr_now = ((data.get("enablePullRequestAutoMerge") or {}).get("pullRequest") or {})
                return {"auto_merge": bool(pr_now.get("autoMergeRequest"))}
            if queue is not None:
                data = self.gh.graphql("bramble", repo, WRITE["merge"], ENQUEUE,
                                       {"pr": state["id"], "head": sha})
                return {"queued": bool((data.get("enqueuePullRequest") or {}).get("mergeQueueEntry"))}
            r = self.gh.call("bramble", repo, WRITE["merge"], "PUT", f"/repos/{repo}/pulls/{number}/merge",
                             {"sha": sha, "merge_method": method})
            if r.status != 200:
                raise GitHubError(r.status, str((r.body or {}).get("message") if isinstance(r.body, dict) else r.body))
            return {"merged": True}

        out = self._guarded_write(bot, task, "merge", repo, f"pr/{number}", sha, write=write, read_back=read_back,
                                  undone=undone)
        receipt = out.get("receipt") or {}
        if receipt.get("queued"):
            out["queued"] = True
        if receipt.get("auto_merge"):
            out["auto_merge"] = True
        return out

    # Patrick's answers on outside pull request cards (#132)

    def _outside_answers(self) -> list[dict]:
        if self.ask_decide is None:
            raise Refused("tw-git has no route to Patrick's answers on outside pull request cards; "
                          "nothing waits on one until it has")
        got = self.ask_decide("outside_answers", {})
        if not isinstance(got, list):
            raise Refused("the decision core's outside answers are not a list; nothing is done on them")
        known = set(self.policy.data["merge"]["base"])
        out = []
        for a in got:
            if not (isinstance(a, dict) and a.get("repo") in known and isinstance(a.get("number"), int)
                    and isinstance(a.get("author"), str) and isinstance(a.get("option"), str)):
                raise Refused(f"an outside answer from the decision core is not one tw-git reads: {a!r:.200}")
            out.append(a)
        return out

    def _allowed_people(self, pr: PullRequest) -> set[str]:
        """Whose work may merge in this pull request on Patrick's word, once
        GitHub says they opened the outside pull request the card names: each
        person he answered "Auto-Merge" for, and the author of an
        outside pull request he answered "merge this one" for, when this pull
        request carries it (a carried link on the card, or a Carried-from line
        in its commits) and theirs is still open, so that yes merges it once."""
        people = set()
        here = (pr.repo, pr.number)
        for a in self._outside_answers():
            if a["option"] == "merge":
                carries = here in [(c["repo"], c["number"]) for c in a.get("carried") or []] or (
                    (a["repo"], a["number"]) in pr.carried_from)
                if not carries:
                    continue
            elif a["option"] != "allow":
                continue
            try:
                raw = self.gh.get("bramble", a["repo"], f"/repos/{a['repo']}/pulls/{int(a['number'])}")
            except GitHubError as e:
                if e.status == 404:
                    continue  # the pull request the card names is gone, so whose it was cannot be read: not allowed
                raise Refused(f"{a['repo']}#{a['number']}, which card {a['decision_id']} names, cannot be read ({e})") from e
            if a["option"] == "merge" and raw.get("state") != "open":
                continue  # its yes was for the one pull request carrying it, which already went
            if ((raw.get("user") or {}).get("login") or "").lower() == a["author"].lower():
                people.add(a["author"].lower())
        return people

    def close_pr(self, caller: str, args: dict) -> dict:
        """Close a pull request on Patrick's answer on its outside card (#132)."""
        bot, repo, task = self._bot(args), self._repo(args), self._task(args)
        number = int(args.get("number"))
        pr = self._pr(repo, number)
        raw = self.gh.get("bramble", repo, f"/repos/{repo}/pulls/{number}")
        answers = self._outside_answers()
        # Whether the work an "allow" answer for this pull request carried has
        # merged, which is what replaces it.
        merged = {}
        for a in answers:
            if (a["repo"], a["number"]) != (repo, number) or a["option"] not in ("allow", "merge"):
                continue
            for c in a.get("carried") or []:
                key = (c["repo"], int(c["number"]))
                if key in merged or c["repo"] not in self.policy.data["merge"]["base"]:
                    continue
                try:
                    got = self.gh.get("bramble", c["repo"], f"/repos/{c['repo']}/pulls/{int(c['number'])}")
                except GitHubError as e:
                    raise Refused(f"the carried {c['repo']}#{c['number']} cannot be read ({e}); nothing is closed") from e
                merged[key] = bool(got.get("merged"))
        # A merged pull request named as what replaced it counts only when its
        # own commits say, in a Carried-from line, that they carry this one.
        # A Bot's own pull request, with no outside work in it, may be named in
        # the merged one's body too (GitHubRules.close_pr says when).
        replaced, other = False, None
        if args.get("replaced_by") is not None:
            other = self._pr(repo, int(args["replaced_by"]))
            if not other.merged:
                raise Refused(f"#{other.number} has not merged, so it has not replaced PR {number}")
            team = pr.author_bot is not None and other.author_bot is not None and not self.rules.outside_credits(pr)
            if (repo, number) in other.carried_from:
                replaced = True
            elif not (team and (repo, number) in other.body_carried_from):
                where = "its commits or its body" if team else "its commits"
                raise Refused(f"#{other.number} has no Carried-from line naming {repo}#{number} in {where}")
        self._check(self.rules.close_pr(bot, pr, answers, merged, raw.get("state") == "open", replaced,
                                        replaced_by=other))

        def write():
            r = self.gh.call("bramble", repo, WRITE["pulls"], "PATCH", f"/repos/{repo}/pulls/{number}",
                             {"state": "closed"})
            if r.status != 200:
                raise GitHubError(r.status, str(r.body))
            return {"closed": True}

        def read_back():
            now = self.gh.get("bramble", repo, f"/repos/{repo}/pulls/{number}")
            return {"closed": True} if now.get("state") == "closed" else None

        return self._guarded_write(bot, task, "close_pr", repo, f"pr/{number}", pr.head_sha,
                                   write=write, read_back=read_back)

    def close_issue(self, caller: str, args: dict) -> dict:
        """Bramble closes an issue as a duplicate (duplicate_of) or as done
        (done_by_pr, or done_by_run in run_repo), with one plain comment saying
        why. GitHub reads "Duplicate of #N" in that comment and links the two."""
        bot, repo, task = self._bot(args), self._repo(args), self._task(args)
        number, reason = int(args.get("number")), str(args.get("reason") or "")
        body = str(args.get("body") or "")
        if (no := self.rules._outside_app(bot, repo)) is not None:
            raise Refused(no.reason)
        issue = self._issue(repo, number)
        duplicate = done_pr = done_run = None
        if args.get("duplicate_of") is not None:
            duplicate = self._issue(repo, int(args["duplicate_of"]))
        if args.get("done_by_pr") is not None:
            done_pr = self._merged_pr(repo, int(args["done_by_pr"]))
        if args.get("done_by_run") is not None:
            run_repo = str(args.get("run_repo") or repo)
            if self.policy.data["merge"]["base"].get(run_repo) is None:
                raise Refused(f"{run_repo} is not a repo the Bots work in")
            done_run = self.gh.get(self.policy.reader_for(run_repo), run_repo,
                                   f"/repos/{run_repo}/actions/runs/{int(args['done_by_run'])}")
        self._check(self.rules.close_issue(bot, issue, reason, body, duplicate=duplicate,
                                           done_pr=done_pr, done_run=done_run))
        app = "bramble"
        text = f"Duplicate of #{duplicate.number}.\n\n{body}" if reason == "duplicate" else body
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        marker = f"<!-- tw:{digest} -->"

        def write():
            said = any(marker in (c.get("body") or "")
                       for c in self.gh.get_all(app, repo, f"/repos/{repo}/issues/{number}/comments"))
            if not said:
                r = self.gh.call(app, repo, WRITE["issues"], "POST", f"/repos/{repo}/issues/{number}/comments",
                                 {"body": f"{text}\n\n{marker}"})
                if r.status != 201:
                    raise GitHubError(r.status, str(r.body))
            r = self.gh.call(app, repo, WRITE["issues"], "PATCH", f"/repos/{repo}/issues/{number}",
                             {"state": "closed", "state_reason": reason})
            if r.status != 200:
                raise GitHubError(r.status, str(r.body))
            return {"closed": True}

        def read_back():
            now = self.gh.get(self.policy.reader_for(repo), repo, f"/repos/{repo}/issues/{number}")
            return {"closed": True, "state_reason": now.get("state_reason")} if now.get("state") == "closed" else None

        return self._guarded_write(bot, task, "close_issue", repo, f"issue/{number}/close", "",
                                   write=write, read_back=read_back)

    def delete_branch(self, caller: str, args: dict) -> dict:
        """Bramble deletes a Bot's branch that has no open pull request, at the
        head it read (sha), so a branch someone pushed to since stays. GitHub
        deletes a pull request's branch when it merges, but a branch that never
        got one, or whose work went into another pull request, stayed for good:
        rift/4509-hide-weapon-recipes, whose test went onto #5731's branch
        (Ops #286, 10 Nov 2026)."""
        bot, repo, task = self._bot(args), self._repo(args), self._task(args)
        branch, sha = str(args.get("branch") or ""), str(args.get("sha") or "")
        if not self.policy.is_router(bot):
            raise Refused(f"only Bramble deletes a branch; {self.policy.bot_name(bot)} never does")
        if (no := self.rules._outside_app(bot, repo)) is not None:
            raise Refused(no.reason)
        owner = self.policy.branch_owner(branch)
        if owner is None:
            raise Refused(f"{branch or 'that'} is not a Bot's branch (<bot>/...); only a Bot's branch is deleted")
        if not SHA.fullmatch(sha):
            raise Refused("name the head you read (sha), so a branch that moved since is not deleted")
        now = self._ref_sha(repo, branch)
        if now is None:
            raise Refused(f"GitHub has no branch {branch} in {repo}")
        if now != sha:
            raise Refused(f"{branch} is at {now[:12]}, not {sha[:12]}; read it again")
        org = repo.split("/", 1)[0]
        open_prs = [p["number"] for p in self.gh.get_all("bramble", repo, f"/repos/{repo}/pulls?state=open&head={quote(org + ':' + branch)}")
                    if p.get("state") == "open" and (p.get("head") or {}).get("ref") == branch]
        if open_prs:
            raise Refused(f"{branch} is the head of open pull request #{open_prs[0]}; that pull request closes or merges first")

        def write():
            r = self.gh.call("bramble", repo, WRITE["push"], "DELETE", f"/repos/{repo}/git/refs/heads/{quote(branch)}")
            if r.status != 204:
                raise GitHubError(r.status, str(r.body))
            return {"deleted": True}

        def read_back():
            return {"deleted": True} if self._ref_sha(repo, branch) is None else None

        return self._guarded_write(bot, task, "delete_branch", repo, f"refs/heads/{branch}", sha,
                                   write=write, read_back=read_back)

    def start_run(self, caller: str, args: dict) -> dict:
        bot, task = self._bot(args), self._task(args)
        repo = self.policy.data["runs"]["repo"]
        # Every run starts on the policy's branch (main). tw_git takes no ref
        # from the caller, so it is the policy's: reading it from the call
        # left it empty, and every run was refused as "not on main" (2 Nov,
        # #5334's tunnel run).
        ref = self.policy.data["runs"]["ref"]
        workflow = str(args.get("workflow") or "")
        inputs = args.get("inputs") or {}
        if not isinstance(inputs, dict) or not all(isinstance(v, str) for v in inputs.values()):
            raise Refused("a run's inputs are named strings")
        commit = self._ref_sha(repo, ref)
        lists = self._verb_lists(repo, commit)
        channel = str(args.get("channel") or "dev")
        decision_id = args.get("decision_id")
        self._check(self.rules.start_run(bot, repo, workflow, ref, inputs, lists,
                                         commit=commit, channel=channel, decision_id=decision_id))
        # Where the workflow itself lives: Thornswood, or Ops for box-run.yml
        # (policy runs.workflows.<name>.repo). The line was checked against
        # Thornswood's main above; the run starts on main of its own repo.
        where = (self.policy.data["runs"]["workflows"][workflow] or {}).get("repo", repo)
        at = commit if where == repo else self._ref_sha(where, ref)
        digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]
        started: dict = {}

        def find():
            runs = self.gh.get("bramble", where, f"/repos/{where}/actions/workflows/{quote(workflow)}/runs?event=workflow_dispatch&branch={quote(ref)}&head_sha={at}&per_page=20")
            for run in runs.get("workflow_runs", []):
                if run.get("head_sha") == at and (not started or run.get("created_at", "") >= started.get("at", "")):
                    return {"run": run["id"], "url": run.get("html_url")}
            return None

        def write():
            from datetime import datetime, timezone
            started["at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            r = self.gh.call("bramble", where, WRITE["actions"], "POST", f"/repos/{where}/actions/workflows/{quote(workflow)}/dispatches",
                             {"ref": ref, "inputs": inputs})
            if r.status != 204:
                raise GitHubError(r.status, str(r.body))
            return {"dispatched": True}

        out = self._guarded_write(bot, task, f"run-{workflow}", where, f"{workflow}/{digest}", at, write=write, read_back=find)
        if decision_id and not out.get("reconciled"):
            self.ask("decision_use", {"id": decision_id})
        out["commit"] = commit
        if where != repo:
            out["repo"], out["repo_commit"] = where, at
        return out

    def read(self, caller: str, args: dict) -> dict:
        """A GET for any Bot, in the three repos only: as the workers' App, or
        Bramble's in Ops, where the workers' App is not installed."""
        bot, repo = self._bot(args), self._repo(args)
        path = str(args.get("path") or "")
        root = f"/repos/{repo}"
        # The repository itself (default_branch and its other fields), or
        # anything under it. 1 Nov: Bramble could not read default_branch.
        inside = path == root or path.startswith((root + "/", root + "?"))
        if not inside and path.startswith("/search/issues?"):
            inside = self._search_in(repo, path)
        if not inside or ".." in path:
            raise Refused(f"reads stay inside {root}, or search its issues with repo:{repo} in the query")
        self._check(self.rules.admin_call(bot, "GET", path))
        return {"body": self.gh.get(self.policy.reader_for(repo), repo, path)}

    def board(self, caller: str, args: dict) -> dict:
        """The board's open issues in do-order, for any Bot: each one's repo,
        number, title, Status, Order, labels and assignees, read from the org
        project through the workers' App (organization projects read).
        eligible keeps only what a Bot may pick (the guide, section 3): Todo,
        no claim label, assigned to nobody or to Patrick, not hands off, and
        not waiting on an open issue by its newest comment (waiting lists the
        ones passed over for that).
        limit (at most 100) bounds what comes back; the read itself is whole,
        so the order is the board's. areas keeps only issues with one of
        those `area:` labels, and not_areas leaves out any with one, so a
        worker's field is read across the whole board: until 9 Nov 2026 a
        pick read the first 100 eligible issues of every field, found none
        of Wisp's (Gamemode, Engine, In-game) there, and Wisp sat idle with
        181 open in its field."""
        bot = self._bot(args)
        cfg = self.policy.data["board"]
        limit = max(1, min(int(args.get("limit") or 20), 100))
        eligible = bool(args.get("eligible"))
        areas, not_areas = (self._areas(args, k) for k in ("areas", "not_areas"))
        repo = self.policy.data["repos"]["app"]
        items, after = [], None
        while True:
            data = self.gh.graphql("workers", repo, BOARD_READ, BOARD_QUERY,
                                   {"org": cfg["org"], "number": int(cfg["number"]), "after": after,
                                    "status": cfg["status_field"], "order": cfg["order_field"]})
            page = ((data.get("organization") or {}).get("projectV2") or {}).get("items") or {}
            items.extend(page.get("nodes") or [])
            if not (page.get("pageInfo") or {}).get("hasNextPage") or len(items) >= 20000:
                break
            after = page["pageInfo"]["endCursor"]
        prefix = self.policy.data["claims"]["prefix"]
        people = set(self.policy.data["hands_off"]["people"])
        lane_labels = set(self.policy.data["hands_off"]["lanes"]["labels"])
        patrick = self.policy.data["decisions"]["answered_by"]
        out = []
        for node in items:
            c = node.get("content") or {}
            if c.get("__typename") != "Issue" or c.get("state") != "OPEN":
                continue
            labels = [l["name"] for l in (c.get("labels") or {}).get("nodes") or []]
            assignees = [a["login"] for a in (c.get("assignees") or {}).get("nodes") or []]
            row = {"repo": (c.get("repository") or {}).get("nameWithOwner"), "number": c.get("number"),
                   "title": c.get("title"), "author": (c.get("author") or {}).get("login"),
                   "status": (node.get("status") or {}).get("name"),
                   "order": (node.get("order") or {}).get("number"),
                   "labels": labels, "assignees": assignees}
            if eligible and (row["status"] != "Todo" or any(l.startswith(prefix) for l in labels)
                             or set(labels) & lane_labels or row["author"] in people
                             or set(assignees) & people or set(assignees) - {patrick}):
                continue
            mine = {l[len("area: "):] for l in labels if l.startswith("area: ")}
            if (areas and not mine & areas) or mine & not_areas:
                continue
            out.append(row)
        out.sort(key=lambda r: (r["order"] is None, r["order"] or 0, r["number"]))
        if not eligible:
            return {"open": len(out), "issues": out[:limit]}
        # An issue whose newest comment says it waits on an open issue is not
        # eligible, read as label reads it when a claim is asked for (Ops
        # #300, #312: on 11 and 12 Nov 2026 #4507, #4518, #4536 and Magpie's
        # field headed eligible while label refused every claim, and each was
        # picked, briefed and re-measured again). Read in do-order until limit
        # are found, so a pick costs one comments read per issue it passes.
        picks, waiting = [], []
        reads = set(self.policy.data["repos"].values())
        for row in out:
            if len(picks) >= limit:
                break
            if row["repo"] in reads:
                try:
                    waits = self._waits_on(row["repo"], row["number"])
                except Refused as e:  # it names a repo tw-git does not read; label refuses it the same way
                    waits = str(e)
                if waits:
                    waiting.append({"repo": row["repo"], "number": row["number"], "waits_on": waits})
                    continue
            picks.append(row)
        return {"open": len(out) - len(waiting), "issues": picks, "waiting": waiting}

    @staticmethod
    def _areas(args: dict, key: str) -> set[str]:
        """An areas list: area names as the `area:` labels carry them, without the prefix."""
        got = args.get(key) or []
        if isinstance(got, str) or not isinstance(got, list) or not all(isinstance(a, str) and a.strip() for a in got):
            raise Refused(f"{key} is a list of area names, e.g. [\"Gamemode\", \"Engine\"]")
        return {a.strip().removeprefix("area: ") for a in got}

    @staticmethod
    def _search_in(repo: str, path: str) -> bool:
        """A search of issues and pull requests in this one repo: the query
        names repo:<repo> and no other repo, owner or org. Before filing, a Bot
        looks for the same problem in open and closed issues (the guide,
        sections 2 and 4); until 9 Nov 2026 only a repo's own paths could be
        read, so the one way to look was paging through a thousand open
        issues, and Bramble filed #5580 and #5585 for a failure #5547 held."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get("q", [])
        if len(query) != 1:
            return False
        scopes = [t.lower() for t in query[0].split() if re.match(r"(?i)-?(repo|org|user|owner):", t)]
        return scopes == [f"repo:{repo}".lower()]

    def refused(self, caller: str, args: dict) -> dict:
        """Any other write a Bot asks for: named, and refused with the rule's reason."""
        bot = self._bot(args)
        v = self.rules.admin_call(bot, str(args.get("method") or "POST"), str(args.get("path") or ""))
        raise Refused(v.reason)

    def ledger_event(self, caller: str, args: dict) -> dict:
        bot = self._bot(args)
        if not self.policy.is_router(bot):
            raise Refused("only Bramble keeps the Dev ledger")
        return self.ask("ledger_event", {k: args.get(k) for k in ("repo", "issue", "event", "release", "note")})

    # Every op that changes something on GitHub.
    WRITES = ("push", "open_pr", "edit_pr", "open_issue", "comment", "label", "claim_labels", "merge", "close_pr",
              "close_issue", "delete_branch", "start_run")

    def _executor_only(self, name: str, op: Callable[[str, dict], dict]) -> Callable[[str, dict], dict]:
        """One executor at a time (plan phases 4 and 5): until the cutover the
        Bots read and plan only, and Patrick's Hermes is the one executor.
        Read from the policy at each call, so nothing a Bot asks can change it."""
        def guarded(caller: str, args: dict) -> dict:
            who = self.policy.data["executor"]
            if who != "bramble":
                raise Refused(f"{name} is refused: until the cutover the Bots read and plan only, and {who} "
                              f"is the one executor (policy.yaml executor; Patrick sets it to bramble at the cutover)")
            return op(caller, args)
        return guarded

    def ops(self) -> dict:
        gw = self.policy.data["brokers"]["users"]["gateway"]
        names = ("base", "push", "open_pr", "edit_pr", "open_issue", "comment", "label", "claim_labels", "merge", "close_pr",
                 "close_issue", "delete_branch", "start_run", "read", "board", "refused", "ledger_event")
        ops = {n: (self._executor_only(n, getattr(self, n)) if n in self.WRITES else getattr(self, n), {gw}) for n in names}
        # The VM's own install results (vm_report.py, ThornswoodRP/Thornswood#5564):
        # root's follower alone, never a Bot.
        from .vm_report import VmReport
        ops["vm_install"] = (VmReport(self.policy, self.gh).install, {"root"})
        return ops
