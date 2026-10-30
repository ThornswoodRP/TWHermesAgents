"""What the git broker (tw-git) lets each Bot do on GitHub.

Every function takes the Bot from the runtime (the Hermes profile the call
came from), never from text the model wrote, and the GitHub facts the broker
read itself just before the write. Each returns a Verdict. The broker makes
the write only on a yes, and GitHub's own rules (the two Apps, main's
"Restrict updates") are the second lock behind it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .decisions import DecisionStore
from .gate_line import GateLineError, parse_request
from .policy import Policy, Verdict, verb_scripts

SHA = re.compile(r"[0-9a-f]{40}")


@dataclass
class PullRequest:
    repo: str
    number: int
    head_sha: str
    head_ref: str
    base_ref: str
    author_bot: str | None          # the Bot whose branch it is, None if not a Bot's
    author_login: str
    files: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    draft: bool = False
    merged: bool = False


@dataclass
class CheckRun:
    name: str
    head_sha: str
    status: str          # queued, in_progress, completed
    conclusion: str | None  # success, failure, skipped, cancelled, neutral, ...


@dataclass
class Issue:
    repo: str
    number: int
    author_login: str
    assignees: list[str]
    labels: list[str]
    open: bool = True


class GitHubRules:
    def __init__(self, policy: Policy, decisions: DecisionStore):
        self.policy = policy
        self.decisions = decisions

    # Pushes

    def push(self, bot: str, repo: str, ref: str, *, force: bool = False,
             force_with_lease: bool = False, delete: bool = False,
             workflow_files_changed: bool = False) -> Verdict:
        p = self.policy
        if not p.is_bot(bot):
            return Verdict.no(f"{bot} is not one of the six Bots")
        if repo == p.data["repos"]["hermes"]:
            return Verdict.no("no Bot writes to Thornswood-Hermes; every change there is Patrick's")
        if not ref.startswith("refs/heads/"):
            return Verdict.no(f"{ref} is not a branch; tags and other refs are never pushed by a Bot")
        branch = ref[len("refs/heads/"):]
        bases = set(p.data["merge"]["base"].values())
        if branch in bases or branch == "main":
            return Verdict.no(f"{branch} is updated only by a merged pull request, never by a push")
        if p.lane_branch(branch):
            return Verdict.no(f"{branch} is a Grok lane's branch; the lanes are hands off")
        if force:
            return Verdict.no("a plain force push is refused; use --force-with-lease on your own branch")
        if delete:
            return Verdict.no("branches are removed after merge through the broker's cleanup, not by a delete push")
        owner = p.branch_owner(branch)
        if owner is None:
            return Verdict.no(f"{branch} is not a Bot branch; a Bot branch is <bot>/<issue>-<what>")
        if repo == p.data["repos"]["fork"] and not p.bots[owner].get("fork_branches"):
            return Verdict.no(f"only Wisp works in the engine fork, not {p.bot_name(owner)}")
        if owner == bot:
            if workflow_files_changed and p.app_of(bot) == "workers":
                return Verdict.no("the workers' App has no Workflows permission; Magpie hands the workflow change to Bramble, whose App pushes it to Magpie's branch")
            return Verdict.yes(f"{p.bot_name(bot)} pushes its own branch {branch}")
        if bot == "bramble" and owner in p.data["branches"]["bramble_may_push_for"]:
            if not workflow_files_changed:
                return Verdict.no("Bramble pushes to Magpie's branch only to carry a workflow change Magpie wrote")
            return Verdict.yes(f"Bramble's App pushes Magpie's workflow change to {branch}")
        return Verdict.no(f"{branch} belongs to {p.bot_name(owner)}; a Bot pushes only its own branch")

    # Pull requests

    def open_pr(self, bot: str, repo: str, head_ref: str, base_ref: str) -> Verdict:
        p = self.policy
        if repo == p.data["repos"]["hermes"]:
            return Verdict.no("no Bot opens a pull request in Thornswood-Hermes")
        want_base = p.data["merge"]["base"].get(repo)
        if want_base is None:
            return Verdict.no(f"{repo} is not a repo the Bots work in")
        if base_ref != want_base:
            return Verdict.no(f"pull requests in {repo} go to {want_base}, not {base_ref}")
        owner = p.branch_owner(head_ref)
        if owner != bot and not (bot == "bramble" and owner in p.data["branches"]["bramble_may_push_for"]):
            return Verdict.no(f"{p.bot_name(bot)} opens pull requests only from its own branch")
        return Verdict.yes()

    def edit_pr(self, bot: str, pr: PullRequest) -> Verdict:
        """Title, body, ready for review, and comments on its own PR."""
        if self._hands_off_pr(pr):
            return Verdict.no(self._hands_off_reason(pr.author_login, pr.head_ref))
        if pr.author_bot == bot:
            return Verdict.yes()
        if bot == "bramble" and pr.author_bot is not None:
            return Verdict.yes("Bramble reviews and comments on any Bot's PR")
        return Verdict.no(f"{self.policy.bot_name(bot)} edits only its own pull requests")

    def merge(self, bot: str, pr: PullRequest, sha: str, method: str,
              checks: list[CheckRun], *, base_verb_lists: list[dict],
              decision_id: str | None = None, card_revision: int | None = None) -> Verdict:
        p = self.policy
        if not p.is_router(bot):
            return Verdict.no(f"only Bramble merges; {p.bot_name(bot) if p.is_bot(bot) else bot} never does, its own work included")
        if p.app_of(bot) != "bramble" or not p.data["github_apps"]["bramble"]["may_merge"]:
            return Verdict.no("merges go only through Bramble's GitHub App")
        if pr.repo == p.data["repos"]["hermes"]:
            return Verdict.no("Bramble never merges in Thornswood-Hermes")
        if self._hands_off_pr(pr):
            return Verdict.no(self._hands_off_reason(pr.author_login, pr.head_ref))
        if pr.merged:
            return Verdict.no(f"PR {pr.number} is already merged")
        if pr.draft:
            return Verdict.no(f"PR {pr.number} is a draft")
        if not SHA.fullmatch(sha or ""):
            return Verdict.no("a merge names the full 40-character head SHA it was checked at")
        if sha != pr.head_sha:
            return Verdict.no(f"the head moved: checked {sha[:12]}, the PR is at {pr.head_sha[:12]}; re-read and review the new head")
        want_base = p.data["merge"]["base"].get(pr.repo)
        if pr.base_ref != want_base:
            return Verdict.no(f"PR {pr.number} targets {pr.base_ref}, not {want_base}")
        want_method = p.data["merge"]["method"].get(pr.repo)
        if method != want_method:
            return Verdict.no(f"{pr.repo} merges with {want_method}, not {method}")
        verdict = self.check_green(pr, checks)
        if not verdict.allowed:
            return verdict
        scripts = verb_scripts(base_verb_lists)
        gated = sorted(f for f in pr.files if p.patrick_path(f, scripts))
        if gated and pr.repo == p.data["repos"]["app"]:
            yes = self.decisions.check(decision_id, kind="box-change", repo=pr.repo,
                                       commit=pr.head_sha, pr=pr.number, revision=card_revision)
            if not yes.allowed:
                return Verdict.no(f"PR {pr.number} changes what runs on the live box ({', '.join(gated[:5])}); {yes.reason}")
            return Verdict.yes(f"green on {sha[:12]} and {yes.reason}")
        return Verdict.yes(f"{p.required_check(pr.repo)} is a success on {sha[:12]}")

    def check_green(self, pr: PullRequest, checks: list[CheckRun]) -> Verdict:
        name = self.policy.required_check(pr.repo)
        runs =[c for c in checks if c.name == name and c.head_sha == pr.head_sha]
        if not runs:
            return Verdict.no(f"'{name}' has not run on {pr.head_sha[:12]}; green on an old head is not green")
        latest = runs[-1]
        if latest.status != "completed":
            return Verdict.no(f"'{name}' is still {latest.status} on {pr.head_sha[:12]}")
        if latest.conclusion == "skipped":
            return Verdict.no(f"'{name}' was skipped on {pr.head_sha[:12]}; skipped is not success")
        if latest.conclusion != "success":
            return Verdict.no(f"'{name}' is {latest.conclusion} on {pr.head_sha[:12]}; red is fixed at its cause, never merged past or re-run")
        return Verdict.yes()

    # Claims

    def add_label(self, bot: str, issue: Issue, label: str, *, decision_id: str | None = None) -> Verdict:
        p = self.policy
        if self._hands_off_issue(issue):
            return Verdict.no(self._hands_off_reason(issue.author_login, None, issue))
        prefix = p.data["claims"]["prefix"]
        if not label.startswith(prefix):
            return Verdict.yes("not a claim label")
        if label in p.data["claims"]["retired_labels"]:
            return Verdict.no(f"'{label}' is kept for history and never put on a new claim")
        if label in p.data["hands_off"]["lanes"]["labels"]:
            return Verdict.no(f"'{label}' belongs to a Grok lane; the lanes are hands off")
        owner = p.claim_labels().get(label)
        if owner is None:
            return Verdict.no(f"'{label}' is not one of the six Bot claim labels")
        if owner != bot and not p.is_router(bot):
            return Verdict.no(f"{p.bot_name(bot)} may put on only its own claim label; Bramble admits work for the others")
        held = [l for l in issue.labels if l.startswith(prefix) and l != label]
        if held:
            return Verdict.no(f"#{issue.number} already carries '{held[0]}'; on a claim race both stop and reconcile, nobody removes another claim")
        return Verdict.yes(f"claim {issue.repo}#{issue.number} for {p.bot_name(owner)}")

    def remove_label(self, bot: str, issue: Issue, label: str, *, decision_id: str | None = None,
                     cutover_commit: str = "") -> Verdict:
        p = self.policy
        if self._hands_off_issue(issue):
            return Verdict.no(self._hands_off_reason(issue.author_login, None, issue))
        prefix = p.data["claims"]["prefix"]
        if not label.startswith(prefix):
            return Verdict.yes("not a claim label")
        owner = p.claim_labels().get(label)
        if owner == bot or (owner is not None and p.is_router(bot)):
            return Verdict.yes()
        old = set(p.data["claims"]["retired_labels"]) | set(p.data["hands_off"]["lanes"]["labels"])
        if p.is_router(bot) and label in old:
            # The one-time cutover swap of the Claude, ChatGPT and Codex claims,
            # bound to the commit of main the cutover card named.
            if not SHA.fullmatch(cutover_commit):
                return Verdict.no("moving an old claim is the cutover, which names the commit of main it happens at")
            yes = self.decisions.check(decision_id, kind="cutover", repo=issue.repo,
                                       commit=cutover_commit)
            if yes.allowed:
                return Verdict.yes(f"cutover swap under {yes.reason}")
            return Verdict.no(f"moving an old claim is the cutover, which needs Patrick's yes: {yes.reason}")
        return Verdict.no(f"'{label}' is not {p.bot_name(bot)}'s to remove")

    # Runs

    def start_run(self, bot: str, repo: str, workflow: str, ref: str, inputs: dict,
                  verb_lists: dict[str, dict], *, commit: str, channel: str = "dev",
                  decision_id: str | None = None) -> Verdict:
        p = self.policy
        runs = p.data["runs"]
        if not p.is_router(bot):
            return Verdict.no("only Bramble starts release, deploy, Nexus and signing runs")
        if repo != runs["repo"]:
            return Verdict.no(f"runs start in {runs['repo']}, not {repo}")
        if workflow not in runs["workflows"]:
            return Verdict.no(f"{workflow} is not a run Bramble may start; the list is {', '.join(runs['workflows'])}")
        if ref != runs["ref"]:
            return Verdict.no(f"runs start on {runs['ref']} only, not {ref}")
        if not SHA.fullmatch(commit or ""):
            return Verdict.no("a run names the exact commit of main it is for")
        needs_patrick = channel == "main"
        spec = runs["workflows"][workflow]
        if spec.get("verbs"):
            listing = verb_lists.get(spec["verbs"])
            if listing is None:
                return Verdict.no(f"{spec['verbs']} was not read from main; nothing runs without it")
            try:
                verb, params = parse_request(inputs.get("line", ""), listing)
            except GateLineError as e:
                return Verdict.no(f"the gate would refuse this line: {e}")
            if verb in runs["refused_verbs"]:
                return Verdict.no(f"{verb} handles a secret; no Bot ever runs it")
            if any(params.get(s) for s in runs["patrick_switches"].get(verb, [])):
                needs_patrick = True
                kind = "publish"
            else:
                kind = "main"
            extra = set(inputs) - {"line", "recipient"}
            if extra:
                return Verdict.no(f"{workflow} takes only a line and a recipient, not {', '.join(sorted(extra))}")
        else:
            kind = "main"
        if needs_patrick:
            yes = self.decisions.check(decision_id, kind=kind, repo=repo, commit=commit)
            if not yes.allowed:
                return Verdict.no(f"this run needs Patrick's word for {commit[:12]}: {yes.reason}")
            return Verdict.yes(f"{yes.reason}; GitHub's Main environment still waits for Patrick")
        return Verdict.yes(f"{workflow} on main at {commit[:12]}")

    # Things no Bot does

    REFUSED_API = [
        (re.compile(r"/environments/[^/]+/deployment[-_]protection|/actions/runs/\d+/pending_deployments"), "approving a deployment is Patrick's, in GitHub"),
        (re.compile(r"/rulesets|/branches/[^/]+/protection"), "rulesets and branch protection are Patrick's"),
        (re.compile(r"/environments(/|$)"), "environments are Patrick's"),
        (re.compile(r"/secrets(/|$)|/variables(/|$)"), "secrets and variables are Patrick's"),
        (re.compile(r"/collaborators|/invitations|/teams"), "who has access is Patrick's"),
        (re.compile(r"/hooks(/|$)"), "webhooks are Patrick's"),
        (re.compile(r"/actions/runners|/actions/permissions"), "runners and Actions settings are Patrick's; Magpie proposes changes by PR"),
        (re.compile(r"/keys(/|$)|/deploy[-_]keys"), "keys are Patrick's"),
        (re.compile(r"/installation|/app/"), "the GitHub Apps are Patrick's"),
    ]

    def admin_call(self, bot: str, method: str, path: str) -> Verdict:
        if method.upper() == "GET":
            return Verdict.yes("a read")
        for pattern, reason in self.REFUSED_API:
            if pattern.search(path):
                return Verdict.no(reason)
        if re.fullmatch(r"/?repos/[^/]+/[^/]+", path) and method.upper() in ("PATCH", "DELETE"):
            return Verdict.no("repository settings are Patrick's")
        return Verdict.no(f"{method} {path} is not a write the broker knows; it is refused until it is named in the rules")

    # Hands off

    def _hands_off_issue(self, issue: Issue) -> bool:
        people = set(self.policy.data["hands_off"]["people"])
        return issue.author_login in people or bool(people & set(issue.assignees))

    def _hands_off_pr(self, pr: PullRequest) -> bool:
        people = set(self.policy.data["hands_off"]["people"])
        return pr.author_login in people or self.policy.lane_branch(pr.head_ref)

    def _hands_off_reason(self, login: str, branch: str | None, issue: Issue | None = None) -> str:
        if branch and self.policy.lane_branch(branch):
            return f"{branch} is a Grok lane's work; the lanes are hands off"
        return "Djinn's and Jhinswife's work is hands off"
