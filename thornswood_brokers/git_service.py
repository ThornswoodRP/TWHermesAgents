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
import hashlib
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Callable

from thornswood_guard.github import SHA, CheckRun, GitHubRules, Issue, PullRequest
from thornswood_guard.policy import Policy, Verdict

from .github_api import GitHub, GitHubError, quote
from .wire import Refused

logger = logging.getLogger(__name__)

WRITE = {
    "push": {"contents": "write", "metadata": "read"},
    "push_workflows": {"contents": "write", "workflows": "write", "metadata": "read"},
    "pulls": {"pull_requests": "write", "contents": "read", "metadata": "read"},
    "merge": {"contents": "write", "pull_requests": "write", "metadata": "read"},
    "issues": {"issues": "write", "metadata": "read"},
    "actions": {"actions": "write", "contents": "read", "metadata": "read"},
}
READ = {"contents": "read", "pull_requests": "read", "issues": "read", "actions": "read", "checks": "read", "metadata": "read"}
CHECKS_APP = "github-actions"
QUEUE_STATE = """query($owner: String!, $name: String!, $number: Int!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    mergeQueue(branch: $branch) { id }
    pullRequest(number: $number) { id headRefOid isInMergeQueue merged }
  }
}"""
ENQUEUE = """mutation($pr: ID!, $head: GitObjectID!) {
  enqueuePullRequest(input: {pullRequestId: $pr, expectedHeadOid: $head}) { mergeQueueEntry { id position } }
}"""
WORKFLOW_DIR = ".github/workflows/"


class RemoteDecisions:
    """GitHubRules' decision store, answered by tw-policy."""

    def __init__(self, ask: Callable[[str, dict], dict]):
        self.ask = ask

    def check(self, decision_id, *, kind, repo, commit, revision=None, pr=None, at=None) -> Verdict:
        r = self.ask("decision_check", {"id": decision_id, "kind": kind, "repo": repo, "commit": commit,
                                        "revision": revision, "pr": pr})
        return Verdict(bool(r["allowed"]), r["reason"])


class GitService:
    def __init__(self, policy: Policy, github: GitHub, ask_policy: Callable[[str, dict], dict], *,
                 mirrors: Path, spool: Path, git_base: str = "https://github.com/",
                 git_env: dict | None = None, allow_file_protocol: bool = False):
        self.policy = policy
        self.gh = github
        self.ask = ask_policy
        self.rules = GitHubRules(policy, RemoteDecisions(ask_policy))
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
            raise Refused("no Bot writes to Thornswood-Hermes; every change there is Patrick's")
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
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True, capture_output=True)
        auth = [] if self.git_base.startswith("file:") else [f"http.extraHeader={self.gh.git_auth_header(app, repo, {'contents': 'read', 'metadata': 'read'})}"]
        self._git(path, "fetch", "-q", "--prune", self._url(repo), "+refs/heads/*:refs/remotes/origin/*", extra_config=auth)
        return path

    def _url(self, repo: str) -> str:
        return f"{self.git_base}{repo}" + ("" if self.git_base.startswith("file:") else ".git")

    def _begin(self, bot, task, action, repo, target, commit) -> dict:
        return self.ask("op_begin", {"bot": bot, "task": task, "action": action, "repo": repo, "target": target, "commit": commit})

    def _finish(self, key: str, outcome: str, receipt=None, reason: str | None = None) -> None:
        self.ask("op_finish", {"key": key, "outcome": outcome, "receipt": receipt, "reason": reason})

    def _guarded_write(self, bot, task, action, repo, target, commit, *, write: Callable[[], object],
                       read_back: Callable[[], object | None]) -> dict:
        """op_begin, the one write, read back, report. read_back returns a
        receipt when the write is visible on GitHub, None when it is not."""
        op = self._begin(bot, task, action, repo, target, commit)
        if op["state"] == "reconcile":
            found = read_back()
            self.ask("op_reconcile", {"key": op["key"], "found": found is not None, "receipt": found})
            if found is not None:
                return {"done": True, "reconciled": True, "receipt": found}
            op = self._begin(bot, task, action, repo, target, commit)
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
        return PullRequest(repo=repo, number=int(number), head_sha=raw["head"]["sha"], head_ref=head_ref,
                           base_ref=raw["base"]["ref"], author_bot=author_bot, author_login=raw["user"]["login"],
                           files=files, labels=[l["name"] for l in raw.get("labels", [])],
                           draft=bool(raw.get("draft")), merged=bool(raw.get("merged")))

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
                "queued": bool(p.get("isInMergeQueue")), "merged": bool(p.get("merged"))}

    def _verb_lists(self, repo: str, ref: str) -> dict[str, dict]:
        out = {}
        for path in self.policy.data["merge"]["verb_lists"]:
            try:
                raw = self.gh.get("bramble", repo, f"/repos/{repo}/contents/{path}?ref={quote(ref)}")
            except GitHubError as e:
                if e.status == 404:
                    continue
                raise
            out[path] = json.loads(base64.b64decode(raw["content"]))
        return out

    def _issue(self, repo: str, number: int) -> Issue:
        raw = self.gh.get("workers", repo, f"/repos/{repo}/issues/{int(number)}")
        return Issue(repo=repo, number=int(number), author_login=raw["user"]["login"],
                     assignees=[a["login"] for a in raw.get("assignees", [])],
                     labels=[l["name"] for l in raw.get("labels", [])], open=raw.get("state") == "open")

    def _ref_sha(self, repo: str, branch: str) -> str | None:
        try:
            raw = self.gh.get("workers", repo, f"/repos/{repo}/git/ref/heads/{quote(branch)}")
        except GitHubError as e:
            if e.status == 404:
                return None
            raise
        return raw["object"]["sha"]

    # Ops

    def push(self, caller: str, args: dict) -> dict:
        bot, repo, task = self._bot(args), self._repo(args), self._task(args)
        branch = str(args.get("branch") or "")
        bundle = self._bundle(bot, str(args.get("bundle") or ""))
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

        out = self._guarded_write(bot, task, "push", repo, f"refs/heads/{branch}", new, write=write, read_back=read_back)
        out.update({"sha": new, "changed_files": len(changed), "workflow_change": workflows})
        return out

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
        start a task's tree from. Read only."""
        bot, repo = self._bot(args), self._repo(args)
        if repo == self.policy.data["repos"]["fork"] and not self.policy.bots[bot].get("fork_branches"):
            raise Refused(f"only Wisp works in the engine fork, not {self.policy.bot_name(bot)}")
        base = self.policy.data["merge"]["base"][repo]
        mirror = self._mirror(repo, self._app(bot))
        sha = self._git(mirror, "rev-parse", f"refs/remotes/origin/{base}")
        self._git(mirror, "update-ref", f"refs/heads/{base}", sha)
        out = self.spool / "in" / bot
        out.mkdir(parents=True, exist_ok=True)
        name = f"{repo.split('/')[1]}-{base.replace('/', '-')}-{sha[:12]}.bundle"
        if not (out / name).exists():
            tmp = out / f".{name}.tmp"
            self._git(mirror, "bundle", "create", "-q", str(tmp), f"refs/heads/{base}")
            os.replace(tmp, out / name)
        return {"bundle": name, "base": base, "sha": sha}

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
        issue_raw = self.gh.get("workers", repo, f"/repos/{repo}/issues/{number}")
        if issue_raw.get("pull_request"):
            self._check(self.rules.edit_pr(bot, self._pr(repo, number)))
        else:
            issue = self._issue(repo, number)
            if self.rules._hands_off_issue(issue):
                raise Refused("Djinn's and Jhinswife's work is hands off")
            own = self.policy.bots[bot]["claim_label"] in issue.labels
            if not (own or self.policy.is_router(bot)):
                raise Refused(f"{self.policy.bot_name(bot)} comments only on issues it has claimed")
        app = "bramble" if bot == "bramble" else self._app(bot)
        digest = hashlib.sha256(body.encode()).hexdigest()[:16]
        marker = f"<!-- tw:{digest} -->"

        def find():
            for c in self.gh.get_all(app, repo, f"/repos/{repo}/issues/{number}/comments"):
                if marker in (c.get("body") or ""):
                    return {"comment": c["id"]}
            return None

        def write():
            r = self.gh.call(app, repo, WRITE["issues"], "POST", f"/repos/{repo}/issues/{number}/comments", {"body": f"{body}\n\n{marker}"})
            if r.status != 201:
                raise GitHubError(r.status, str(r.body))
            return {"comment": r.body["id"]}

        return self._guarded_write(bot, task, "comment", repo, f"issue/{number}/{digest}", "", write=write, read_back=find)

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
        app = "bramble" if bot == "bramble" else self._app(bot)

        def write():
            if action == "add":
                r = self.gh.call(app, repo, WRITE["issues"], "POST", f"/repos/{repo}/issues/{number}/labels", {"labels": [label]})
            else:
                r = self.gh.call(app, repo, WRITE["issues"], "DELETE", f"/repos/{repo}/issues/{number}/labels/{quote(label)}")
            if r.status not in (200, 204):
                raise GitHubError(r.status, str(r.body))
            return {"status": r.status}

        def read_back():
            has = label in self._issue(repo, number).labels
            return {"label": label, "on": has} if has == (action == "add") else None

        return self._guarded_write(bot, task, f"label-{action}", repo, f"issue/{number}/{label}", "", write=write, read_back=read_back)

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

        def read_back():
            raw = self.gh.get("bramble", repo, f"/repos/{repo}/pulls/{number}")
            if raw.get("merged") and raw.get("head", {}).get("sha") == sha:
                return {"merged": True, "merge_commit_sha": raw.get("merge_commit_sha")}
            if queue is not None:
                now = self._queue_state(repo, number, pr.base_ref)
                if now["head"] == sha and now["queued"]:
                    return {"queued": True}
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
        main_lists = list(self._verb_lists(repo, self.policy.data["merge"]["base"][repo]).values()) if repo == self.policy.data["repos"]["app"] else []
        decision_id, revision = args.get("decision_id"), args.get("card_revision")
        self._check(self.rules.merge(bot, pr, sha, method, checks, base_verb_lists=main_lists,
                                     decision_id=decision_id, card_revision=revision))

        # With a merge queue on the base branch (the redesign's part 5), GitHub
        # refuses a direct merge: the PR joins the queue at this exact head,
        # and the queue merges it once its own checks pass (method is the
        # queue's). Read each time, so the rule can come and go in either
        # order with this code.
        state = self._queue_state(repo, number, pr.base_ref)
        queue = state["queue"]

        def write():
            if queue is not None:
                data = self.gh.graphql("bramble", repo, WRITE["merge"], ENQUEUE,
                                       {"pr": state["id"], "head": sha})
                return {"queued": bool((data.get("enqueuePullRequest") or {}).get("mergeQueueEntry"))}
            r = self.gh.call("bramble", repo, WRITE["merge"], "PUT", f"/repos/{repo}/pulls/{number}/merge",
                             {"sha": sha, "merge_method": method})
            if r.status != 200:
                raise GitHubError(r.status, str((r.body or {}).get("message") if isinstance(r.body, dict) else r.body))
            return {"merged": True}

        out = self._guarded_write(bot, task, "merge", repo, f"pr/{number}", sha, write=write, read_back=read_back)
        if (out.get("receipt") or {}).get("queued"):
            out["queued"] = True
        if decision_id and not out.get("reconciled"):
            self.ask("decision_use", {"id": decision_id})
        return out

    def start_run(self, caller: str, args: dict) -> dict:
        bot, task = self._bot(args), self._task(args)
        repo = self.policy.data["runs"]["repo"]
        workflow = str(args.get("workflow") or "")
        inputs = args.get("inputs") or {}
        if not isinstance(inputs, dict) or not all(isinstance(v, str) for v in inputs.values()):
            raise Refused("a run's inputs are named strings")
        commit = self._ref_sha(repo, "main")
        lists = self._verb_lists(repo, commit)
        channel = str(args.get("channel") or "dev")
        decision_id = args.get("decision_id")
        self._check(self.rules.start_run(bot, repo, workflow, str(args.get("ref") or ""), inputs, lists,
                                         commit=commit, channel=channel, decision_id=decision_id))
        digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]
        started: dict = {}

        def find():
            runs = self.gh.get("bramble", repo, f"/repos/{repo}/actions/workflows/{quote(workflow)}/runs?event=workflow_dispatch&branch=main&head_sha={commit}&per_page=20")
            for run in runs.get("workflow_runs", []):
                if run.get("head_sha") == commit and (not started or run.get("created_at", "") >= started.get("at", "")):
                    return {"run": run["id"], "url": run.get("html_url")}
            return None

        def write():
            from datetime import datetime, timezone
            started["at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            r = self.gh.call("bramble", repo, WRITE["actions"], "POST", f"/repos/{repo}/actions/workflows/{quote(workflow)}/dispatches",
                             {"ref": "main", "inputs": inputs})
            if r.status != 204:
                raise GitHubError(r.status, str(r.body))
            return {"dispatched": True}

        out = self._guarded_write(bot, task, f"run-{workflow}", repo, f"{workflow}/{digest}", commit, write=write, read_back=find)
        if decision_id and not out.get("reconciled"):
            self.ask("decision_use", {"id": decision_id})
        out["commit"] = commit
        return out

    def read(self, caller: str, args: dict) -> dict:
        """A GET for any Bot, as the workers' App, in the three repos only."""
        bot, repo = self._bot(args), self._repo(args)
        path = str(args.get("path") or "")
        root = f"/repos/{repo}"
        # The repository itself (default_branch and its other fields), or
        # anything under it. 1 Nov: Bramble could not read default_branch.
        inside = path == root or path.startswith((root + "/", root + "?"))
        if not inside or ".." in path:
            raise Refused(f"reads stay inside {root}")
        self._check(self.rules.admin_call(bot, "GET", path))
        return {"body": self.gh.get("workers", repo, path)}

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
    WRITES = ("push", "open_pr", "edit_pr", "comment", "label", "claim_labels", "merge", "start_run")

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
        names = ("base", "push", "open_pr", "edit_pr", "comment", "label", "claim_labels", "merge", "start_run", "read", "refused", "ledger_event")
        return {n: (self._executor_only(n, getattr(self, n)) if n in self.WRITES else getattr(self, n), {gw}) for n in names}
