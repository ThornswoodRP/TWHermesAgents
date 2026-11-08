"""A GitHub for tw-git's tests: REST answers from memory, branches from real
bare repositories on disk (reached as file:// remotes). Nothing here talks to
the network, and no key is read: tokens are made up and the JWT signer is a
stub.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

from thornswood_brokers.github_api import API, Response

GIT_ENV_CONFIG = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "init.defaultBranch=main",
                  "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]


def git(cwd, *args) -> str:
    r = subprocess.run(["git", *GIT_ENV_CONFIG, "-C", str(cwd), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


class Lost(Exception):
    """The connection dropped after GitHub did the write."""


class FakeGitHub:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or tempfile.mkdtemp())
        self.remotes = self.root / "remotes"
        self.prs: dict[tuple[str, int], dict] = {}
        self.pr_files: dict[tuple[str, int], list[str]] = {}
        self.pr_commits: dict[tuple[str, int], list[dict]] = {}
        self.check_runs: dict[tuple[str, str], list[dict]] = {}
        self.issues: dict[tuple[str, int], dict] = {}
        self.comments: dict[tuple[str, int], list[dict]] = {}
        self.contents: dict[tuple[str, str, str], bytes] = {}
        self.runs: list[dict] = []
        self.repo_labels: dict[tuple[str, str], dict] = {}   # (repo, lower-case name) -> label
        self.calls: list[tuple[str, str, dict | None]] = []
        self.tokens: list[dict] = []
        self.token_permissions: dict[str, dict] = {}   # token -> the permissions it was minted with
        self.lose_answer: set[str] = set()   # "METHOD path" whose answer is lost after the write
        self.refuse: dict[str, tuple[int, str]] = {}
        self.merge_queues: set[tuple[str, str]] = set()   # (repo, branch) with a merge queue
        self.queued: list[tuple[str, int, str]] = []       # (repo, number, head) put in a queue
        # (repo, branch) -> the status checks the branch's rules require
        self.branch_rules: dict[tuple[str, str], list[str]] = {}
        self.auto_merged: list[tuple[str, int, str, str | None]] = []  # (repo, number, head, method) put on auto-merge
        self._next_id = 1000

    # Repositories on disk

    def remote(self, repo: str) -> Path:
        return self.remotes / repo

    def make_repo(self, repo: str, base: str = "main", files: dict[str, str] | None = None) -> Path:
        path = self.remote(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", base, str(path)], check=True)
        work = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", "-b", base, str(work)], check=True)
        for name, text in (files or {"README.md": "Thornswood\n"}).items():
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(text)
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "start")
        git(work, "push", "-q", str(path), f"HEAD:refs/heads/{base}")
        return path

    def ref(self, repo: str, branch: str) -> str | None:
        r = subprocess.run(["git", "-C", str(self.remote(repo)), "rev-parse", "-q", "--verify", f"refs/heads/{branch}"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    # State helpers for tests

    def add_pr(self, repo: str, number: int, *, head_ref: str, head_sha: str, base: str = "main",
               files: list[str], author: str = "thornswood-workers[bot]", draft: bool = False) -> None:
        self.prs[(repo, number)] = {
            "number": number, "html_url": f"https://github.com/{repo}/pull/{number}",
            "head": {"ref": head_ref, "sha": head_sha, "repo": {"full_name": repo}},
            "base": {"ref": base}, "user": {"login": author}, "labels": [], "draft": draft,
            "merged": False, "merge_commit_sha": None, "changed_files": len(files), "state": "open",
            "title": "t", "body": "b"}
        self.pr_files[(repo, number)] = list(files)
        self.issues[(repo, number)] = {"number": number, "user": {"login": author}, "assignees": [], "labels": [],
                                       "state": "open", "pull_request": {"url": "x"}}

    def add_check(self, repo: str, sha: str, name: str, conclusion: str = "success",
                  status: str = "completed", app: str = "github-actions") -> None:
        self._next_id += 1
        self.check_runs.setdefault((repo, sha), []).append(
            {"id": self._next_id, "name": name, "head_sha": sha, "status": status,
             "conclusion": conclusion if status == "completed" else None, "app": {"slug": app}})

    def add_issue(self, repo: str, number: int, *, author: str = "EvilPatrick06", labels: list[str] = (),
                  assignees: list[str] = ()) -> None:
        self.issues[(repo, number)] = {"number": number, "user": {"login": author},
                                       "assignees": [{"login": a} for a in assignees],
                                       "labels": [{"name": l} for l in labels], "state": "open"}

    def writes(self, method: str | None = None, contains: str = "") -> list:
        return [c for c in self.calls if c[0] != "GET" and (method is None or c[0] == method) and contains in c[1]]

    # The transport

    def __call__(self, method: str, url: str, headers: dict, data: bytes | None) -> Response:
        assert url.startswith(API), url
        parsed = urllib.parse.urlsplit(url[len(API):])
        path = parsed.path
        query = dict(urllib.parse.parse_qsl(parsed.query))
        body = json.loads(data) if data else None
        self.calls.append((method, path, body))
        key = f"{method} {path}"
        if key in self.refuse:
            status, msg = self.refuse[key]
            return Response(status, {"message": msg}, {})
        if path.startswith("/app/installations/"):
            assert headers["Authorization"].startswith("Bearer ")
            self.tokens.append({"installation": path.split("/")[3], **body})
            self.token_permissions[f"tok-{len(self.tokens)}"] = dict(body.get("permissions") or {})
            return Response(201, {"token": f"tok-{len(self.tokens)}", "expires_at": "x"}, {})
        assert headers["Authorization"].startswith("token tok-"), headers
        refused = self._pr_write_refused(method, path, headers["Authorization"][len("token "):])
        if refused:
            return refused
        resp = self._graphql(body) if path == "/graphql" else self._route(method, path, query, body)
        if key in self.lose_answer:
            self.lose_answer.discard(key)
            raise Lost(f"connection reset after {key}")
        return resp

    def _pr_write_refused(self, method: str, path: str, token: str) -> Response | None:
        """GitHub's rule for a label or a comment on a pull request through the
        issues API: the token needs pull_requests write, and issues write alone
        is answered 403 (measured with Actions tokens on Thornswood-Ops #220,
        2 Nov 2026: issues write labelled an issue, 200, and was refused on a
        pull request, 403, for a label and a comment alike; pull_requests write
        labelled the pull request, 200)."""
        m = re.match(r"^/repos/([^/]+/[^/]+)/issues/(\d+)/(labels|comments)(/|$)", path)
        if method not in ("POST", "DELETE", "PUT") or not m:
            return None
        issue = self.issues.get((m.group(1), int(m.group(2))))
        if not issue or not issue.get("pull_request"):
            return None
        if self.token_permissions.get(token, {}).get("pull_requests") != "write":
            return Response(403, {"message": "Resource not accessible by integration"}, {})
        return None

    def _graphql(self, body) -> Response:
        """The GraphQL shapes tw-git sends: the queue state, the enqueue and auto-merge."""
        q, v = body["query"], body["variables"]
        if "enablePullRequestAutoMerge" in q:
            repo, number = v["pr"].split("#")
            pr = self.prs[(repo, int(number))]
            if pr["head"]["sha"] != v["head"]:
                return Response(200, {"errors": [{"message": "Expected head oid does not match"}]}, {})
            if pr["merged"]:
                return Response(200, {"errors": [{"message": "Pull request is already merged"}]}, {})
            pr["auto_merge"] = {"enabled_at": "2099-01-01T00:00:00Z", "merge_method": v.get("method")}
            self.auto_merged.append((repo, int(number), v["head"], v.get("method")))
            if "auto_merge" in self.lose_answer:
                self.lose_answer.discard("auto_merge")
                raise Lost("connection reset after auto-merge was turned on")
            return Response(200, {"data": {"enablePullRequestAutoMerge": {"pullRequest": {
                "autoMergeRequest": {"enabledAt": "2099-01-01T00:00:00Z"}}}}}, {})
        if "enqueuePullRequest" in q:
            repo, number = v["pr"].split("#")
            pr = self.prs[(repo, int(number))]
            if not any(r == repo and b == pr["base"]["ref"] for r, b in self.merge_queues):
                return Response(200, {"errors": [{"message": "Pull request's base branch has no merge queue"}]}, {})
            if pr["head"]["sha"] != v["head"]:
                return Response(200, {"errors": [{"message": "Expected head oid does not match"}]}, {})
            if pr.get("in_queue") or pr["merged"]:
                return Response(200, {"errors": [{"message": "Pull request is already queued or merged"}]}, {})
            pr["in_queue"] = True
            self.queued.append((repo, int(number), v["head"]))
            if "enqueue" in self.lose_answer:
                self.lose_answer.discard("enqueue")
                raise Lost("connection reset after the enqueue")
            return Response(200, {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"id": "MQE", "position": 1}}}}, {})
        repo = f"{v['owner']}/{v['name']}"
        pr = self.prs.get((repo, v["number"]))
        return Response(200, {"data": {"repository": {
            "mergeQueue": {"id": f"MQ:{repo}"} if (repo, v["branch"]) in self.merge_queues else None,
            "pullRequest": None if pr is None else {"id": f"{repo}#{v['number']}", "headRefOid": pr["head"]["sha"],
                                                    "isInMergeQueue": bool(pr.get("in_queue")), "merged": pr["merged"],
                                                    "autoMergeRequest": ({"enabledAt": pr["auto_merge"]["enabled_at"]}
                                                                         if pr.get("auto_merge") else None)}}}}, {})

    def land_queue(self, repo: str, number: int) -> None:
        """The queue's own checks passed: GitHub merges the PR."""
        pr = self.prs[(repo, number)]
        pr.update(in_queue=False, merged=True, state="closed", merge_commit_sha="d" * 40)

    def _route(self, method, path, query, body) -> Response:
        parts = path.strip("/").split("/")
        assert parts[0] == "repos", path
        repo = f"{parts[1]}/{parts[2]}"
        rest = parts[3:]
        page = int(query.get("page", "1"))
        if not rest and method == "GET":
            return Response(200, {"full_name": repo, "default_branch": "main"}, {})
        if rest[:2] == ["rules", "branches"]:
            branch = urllib.parse.unquote("/".join(rest[2:]))
            names = self.branch_rules.get((repo, branch))
            rules = [] if not names or page > 1 else [{"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": n, "integration_id": 15368} for n in names]}}]
            return Response(200, rules, {})
        if rest[:3] == ["git", "ref", "heads"]:
            branch = urllib.parse.unquote("/".join(rest[3:]))
            sha = self.ref(repo, branch)
            return Response(200, {"object": {"sha": sha}}, {}) if sha else Response(404, {"message": "Not Found"}, {})
        if rest[0] == "pulls" and len(rest) == 1:
            if method == "GET":
                head = query.get("head", "").split(":", 1)[-1]
                found = [p for (r, _), p in self.prs.items() if r == repo and p["head"]["ref"] == head]
                return Response(200, found, {})
            n = max([k[1] for k in self.prs if k[0] == repo] + [99]) + 1
            self.add_pr(repo, n, head_ref=body["head"], head_sha=self.ref(repo, body["head"]), base=body["base"], files=[])
            self.prs[(repo, n)].update(title=body["title"], body=body["body"])
            return Response(201, {"number": n}, {})
        if rest[0] == "pulls":
            n = int(rest[1])
            pr = self.prs.get((repo, n))
            if pr is None:
                return Response(404, {"message": "Not Found"}, {})
            if len(rest) == 2 and method == "GET":
                return Response(200, json.loads(json.dumps(pr)), {})
            if len(rest) == 2 and method == "PATCH":
                pr.update(body)
                return Response(200, pr, {})
            if rest[2] == "commits":
                commits = self.pr_commits.get((repo, n))
                if commits is None:
                    # By default the head commit, made by the Bot whose branch it is
                    # (the files broker's identity), or by the PR's author.
                    bot = pr["head"]["ref"].split("/")[0]
                    login = pr["user"]["login"]
                    if bot in ("bramble", "rift", "sprocket", "echo", "magpie", "wisp"):
                        who, author = {"name": bot, "email": f"{bot}@bots.thornswood.invalid"}, None
                    else:
                        who, author = {"name": login, "email": f"1+{login}@users.noreply.github.com"}, {"login": login}
                    commits = [{"sha": pr["head"]["sha"], "author": author,
                                "commit": {"author": who, "message": "a change"}}]
                return Response(200, commits[(page - 1) * 100: page * 100], {})
            if rest[2] == "files":
                files = self.pr_files[(repo, n)][(page - 1) * 100: page * 100]
                return Response(200, [{"filename": f} for f in files], {})
            if rest[2] == "merge" and method == "PUT":
                if pr["merged"]:
                    return Response(405, {"message": "Pull Request is not mergeable"}, {})
                if body.get("sha") != pr["head"]["sha"]:
                    return Response(409, {"message": "Head branch was modified"}, {})
                pr["merged"] = True
                pr["state"] = "closed"
                pr["merge_commit_sha"] = "c" * 40
                return Response(200, {"merged": True, "sha": "c" * 40}, {})
        if rest[0] == "commits" and rest[2] == "check-runs":
            runs = self.check_runs.get((repo, rest[1]), [])
            return Response(200, {"total_count": len(runs), "check_runs": runs[(page - 1) * 100: page * 100]}, {})
        if rest[0] == "contents":
            name = "/".join(rest[1:])
            raw = self.contents.get((repo, query.get("ref", "main"), name))
            if raw is None:
                return Response(404, {"message": "Not Found"}, {})
            return Response(200, {"content": base64.b64encode(raw).decode()}, {})
        if rest[0] == "issues":
            n = int(rest[1])
            issue = self.issues.get((repo, n))
            if issue is None:
                return Response(404, {"message": "Not Found"}, {})
            if len(rest) == 2:
                return Response(200, issue, {})
            if rest[2] == "comments":
                if method == "GET":
                    return Response(200, self.comments.get((repo, n), [])[(page - 1) * 100: page * 100], {})
                self._next_id += 1
                c = {"id": self._next_id, "body": body["body"]}
                self.comments.setdefault((repo, n), []).append(c)
                return Response(201, c, {})
            if rest[2] == "labels":
                if method == "POST":
                    for l in body["labels"]:
                        if l not in [x["name"] for x in issue["labels"]]:
                            issue["labels"].append({"name": l})
                    return Response(200, issue["labels"], {})
                name = urllib.parse.unquote(rest[3])
                issue["labels"] = [x for x in issue["labels"] if x["name"] != name]
                return Response(200, issue["labels"], {})
        if rest[0] == "labels":
            if len(rest) == 1 and method == "POST":
                k = (repo, body["name"].lower())
                if k in self.repo_labels:
                    return Response(422, {"message": "Validation Failed", "errors": [{"code": "already_exists"}]}, {})
                self.repo_labels[k] = {"name": body["name"], "color": body["color"], "description": body.get("description")}
                return Response(201, self.repo_labels[k], {})
            k = (repo, urllib.parse.unquote(rest[1]).lower())
            label = self.repo_labels.get(k)
            if label is None:
                return Response(404, {"message": "Not Found"}, {})
            if method == "GET":
                return Response(200, dict(label), {})
            if method == "PATCH":
                label.update({"name": body.get("new_name", label["name"]), "color": body.get("color", label["color"]),
                              "description": body.get("description", label["description"])})
                return Response(200, dict(label), {})
        if rest[0] == "actions" and rest[1] == "workflows":
            wf = urllib.parse.unquote(rest[2])
            if rest[3] == "dispatches":
                self._next_id += 1
                self.runs.append({"id": self._next_id, "workflow": wf, "head_sha": self.ref(repo, "main"),
                                  "created_at": "2099-01-01T00:00:00Z", "html_url": "u", "inputs": body["inputs"]})
                return Response(204, None, {})
            return Response(200, {"workflow_runs": [r for r in self.runs if r["workflow"] == wf
                                                    and r["head_sha"] == query.get("head_sha")]}, {})
        return Response(404, {"message": f"the fake has no {method} {path}"}, {})
