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
from .policy import Policy, Verdict

SHA = re.compile(r"[0-9a-f]{40}")


@dataclass
class Credit:
    """Someone whose work is in a commit: its author, or a Co-authored-by line."""
    commit: str
    name: str
    email: str
    login: str | None = None     # the GitHub account GitHub links the commit's author to
    co_author: bool = False

    def who(self) -> str:
        return self.login or self.email or self.name or self.commit[:12]


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
    credits: list[Credit] = field(default_factory=list)   # every commit's author and co-authors
    carried_from: list[tuple[str, int]] = field(default_factory=list)  # Carried-from lines: the outside PRs it carries


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
    pull_request: bool = False   # an issue number GitHub says is a pull request


class GitHubRules:
    def __init__(self, policy: Policy, decisions: DecisionStore):
        self.policy = policy
        self.decisions = decisions

    def _outside_app(self, bot: str, repo: str) -> Verdict | None:
        """A refusal when the Bot's App is not installed on the repo: the
        workers' App is not on Ops, which is Bramble's alone (policy.yaml
        github_apps)."""
        if self.policy.app_reaches(self.policy.app_of(bot), repo):
            return None
        return Verdict.no(f"{self.policy.bot_name(bot)} does not write in {repo}: it is Bramble's alone, since GitHub "
                          "keeps no rules on that private repository and the workers' App is not installed there")

    # Pushes

    def push(self, bot: str, repo: str, ref: str, *, force: bool = False,
             force_with_lease: bool = False, delete: bool = False,
             workflow_files_changed: bool = False) -> Verdict:
        p = self.policy
        if not p.is_bot(bot):
            return Verdict.no(f"{bot} is not one of the six Bots")
        if repo == p.data["repos"]["hermes"]:
            return Verdict.no("no Bot writes to TWHermesAgents; every change there is Patrick's")
        if (no := self._outside_app(bot, repo)) is not None:
            return no
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
                return Verdict.no(f"the workers' App has no Workflows permission; {p.bot_name(bot)} hands the workflow change to Bramble, whose App pushes it to {branch}")
            return Verdict.yes(f"{p.bot_name(bot)} pushes its own branch {branch}")
        if bot == "bramble" and owner in p.data["branches"]["bramble_may_push_for"]:
            if not workflow_files_changed:
                return Verdict.no(f"Bramble pushes to {p.bot_name(owner)}'s branch only to carry a workflow change {p.bot_name(owner)} wrote")
            return Verdict.yes(f"Bramble's App pushes {p.bot_name(owner)}'s workflow change to {branch}")
        return Verdict.no(f"{branch} belongs to {p.bot_name(owner)}; a Bot pushes only its own branch")

    # Pull requests

    def open_pr(self, bot: str, repo: str, head_ref: str, base_ref: str) -> Verdict:
        p = self.policy
        if repo == p.data["repos"]["hermes"]:
            return Verdict.no("no Bot opens a pull request in TWHermesAgents")
        if (no := self._outside_app(bot, repo)) is not None:
            return no
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
        if (no := self._outside_app(bot, pr.repo)) is not None:
            return no
        if self._hands_off_pr(pr):
            return Verdict.no(self._hands_off_reason(pr.author_login, pr.head_ref))
        if pr.author_bot == bot:
            return Verdict.yes()
        if bot == "bramble" and pr.author_bot is not None:
            return Verdict.yes("Bramble reviews and comments on any Bot's PR")
        return Verdict.no(f"{self.policy.bot_name(bot)} edits only its own pull requests")

    def outside_pr(self, pr: PullRequest) -> bool:
        """A pull request from outside the team: not a Bot's, not a robot's,
        not by anyone on Patrick's list, and not hands-off work."""
        return (pr.author_bot is None and not self.policy.trusted_login(pr.author_login)
                and not self._hands_off_pr(pr))

    def comment_pr(self, bot: str, pr: PullRequest) -> Verdict:
        """A comment on a pull request: as edit_pr, and Bramble may also
        comment on a robot's pull request (Dependabot's, the pin robot's:
        merge.robot_authors), whose red check or conflict its duties send back
        as one comment there (Ops #281), and answer a pull request from outside
        the team (what its review found, where the work went). Nothing else of
        a robot's or an outside pull request is a Bot's to change: no edit,
        label or close."""
        v = self.edit_pr(bot, pr)
        if v.allowed:
            return v
        if self.policy.is_router(bot) and self.policy.robot_author(pr.author_login):
            return Verdict.yes("Bramble comments on a robot's pull request")
        if self.policy.is_router(bot) and self.outside_pr(pr):
            return Verdict.yes("Bramble answers a pull request from outside the team")
        return v

    def outside_credits(self, pr: PullRequest) -> list[Credit]:
        """The credits on a pull request that are not on Patrick's list, a Bot,
        a robot or one of our own workflows."""
        return [c for c in pr.credits if not self.policy.trusted_credit(c.login, c.email)]

    def outside_work(self, pr: PullRequest, allowed: set[str] | None = None) -> Verdict:
        """Patrick, 8 Nov 2026: outside work never merges "unless I specficallyt
        say that I want that person to automerge". Every commit's author and
        every Co-authored-by line must be on his list, a Bot, a robot or one of
        our own workflows, or be a person he answered "Auto-Merge" for,
        or "merge this one" for on the card of the outside pull request this one
        carries (allowed: their logins, read by tw-git from the decision core,
        #132)."""
        p = self.policy
        allowed = {a.lower() for a in (allowed or set())}
        outside = sorted({c.who() for c in self.outside_credits(pr)
                          if p.credit_login(c.login, c.email) not in allowed})
        if outside:
            return Verdict.no(f"PR {pr.number} carries work by {', '.join(outside)}, who is not on Patrick's list of "
                              "people whose work may merge (policy merge.auto_merge_people), and Patrick has not "
                              "answered \"let them auto-merge\" for them, or \"merge this one\" for this pull "
                              "request, on an outside pull request card; it waits for that card")
        label = p.data["merge"].get("outside_label")
        if label and label in pr.labels and not any(c.co_author for c in pr.credits):
            return Verdict.no(f"PR {pr.number} is labelled '{label}' and no commit credits its author with a "
                              "Co-authored-by line, so whose work it is cannot be read; credit them, then ask again")
        return Verdict.yes()

    def merge(self, bot: str, pr: PullRequest, sha: str, method: str,
              checks: list[CheckRun], *, branch_checks: set[str] | None = None,
              allowed_people: set[str] | None = None) -> Verdict:
        """Bramble's merge. Where the policy has GitHub's auto-merge do it
        (merge.auto_merge), a yes here turns auto-merge on at this head and
        GitHub merges once the checks its branch rules require pass:
        branch_checks is that list, read from GitHub just before. Elsewhere
        (Ops) a yes is the merge itself, once every required check passed."""
        p = self.policy
        if not p.is_router(bot):
            return Verdict.no(f"only Bramble merges; {p.bot_name(bot) if p.is_bot(bot) else bot} never does, its own work included")
        if p.app_of(bot) != "bramble" or not p.data["github_apps"]["bramble"]["may_merge"]:
            return Verdict.no("merges go only through Bramble's GitHub App")
        if pr.repo == p.data["repos"]["hermes"]:
            return Verdict.no("Bramble never merges in TWHermesAgents")
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
        verdict = self.outside_work(pr, allowed_people)
        if not verdict.allowed:
            return verdict
        if p.auto_merge(pr.repo):
            return self._auto_merge(pr, sha, checks, branch_checks)
        verdict = self.check_green(pr, checks)
        if not verdict.allowed:
            return verdict
        return Verdict.yes(f"green on {sha[:12]}: {', '.join(p.required_checks(pr.repo))}")

    def _auto_merge(self, pr: PullRequest, sha: str, checks: list[CheckRun],
                    branch_checks: set[str] | None) -> Verdict:
        p = self.policy
        if pr.author_bot is None and not p.robot_author(pr.author_login):
            return Verdict.no(f"PR {pr.number} is not a Bot's or a robot's; in {pr.repo} its owner puts it on "
                              "auto-merge, and Bramble never merges it")
        rule = p.rule_checks(pr.repo)
        missing = [n for n in rule if n not in (branch_checks or set())]
        if branch_checks is None or missing:
            return Verdict.no(f"{pr.base_ref}'s rules in {pr.repo} do not require {', '.join(missing or rule)}, "
                              "so auto-merge would not wait for them; the rules are fixed, not stepped around")
        for name in p.required_checks(pr.repo):
            runs = [c for c in checks if c.name == name and c.head_sha == pr.head_sha]
            if runs and runs[-1].status == "completed" and runs[-1].conclusion != "success":
                return Verdict.no(f"'{name}' is {runs[-1].conclusion} on {pr.head_sha[:12]}; red goes back to "
                                  "its owner and is fixed at its cause, never put on auto-merge")
        return Verdict.yes(f"auto-merge on at {sha[:12]}: GitHub merges once {', '.join(rule)} pass there")

    def close_pr(self, bot: str, pr: PullRequest, answers: list[dict], merged: dict[tuple[str, int], bool],
                 open_now: bool, replaced: bool = False) -> Verdict:
        """Bramble closes a pull request only on Patrick's answer (#132): his
        "close" on the outside pull request card that names it, as the outside
        pull request or as a Bot's pull request carrying its work; or his
        "allow" or "merge" on that card once the work carried from it merged,
        which replaced it. answers: the decision core's outside answers, read
        by tw-git; merged: whether each carried pull request merged; replaced:
        tw-git read a merged pull request whose Carried-from line names it."""
        p = self.policy
        if not p.is_router(bot):
            return Verdict.no(f"only Bramble closes a pull request, and only on Patrick's answer; "
                              f"{p.bot_name(bot) if p.is_bot(bot) else bot} never does")
        if pr.repo == p.data["repos"]["hermes"]:
            return Verdict.no("Bramble never closes anything in TWHermesAgents")
        if self._hands_off_pr(pr):
            return Verdict.no(self._hands_off_reason(pr.author_login, pr.head_ref))
        if pr.merged or not open_now:
            return Verdict.no(f"PR {pr.number} is not open")
        here = (pr.repo, pr.number)
        for a in reversed(answers):
            carried = [(c["repo"], c["number"]) for c in a.get("carried") or []]
            names_it = here == (a["repo"], a["number"])
            if not (names_it or here in carried):
                continue
            if a["option"] == "close":
                if names_it and not self.outside_pr(pr):
                    return Verdict.no(f"card {a['decision_id']} names PR {pr.number} as outside work, but it is "
                                      "not from outside the team")
                if not names_it and pr.author_bot is None:
                    return Verdict.no(f"card {a['decision_id']} names PR {pr.number} as carried work, but it is "
                                      "not a Bot's")
                return Verdict.yes(f"Patrick answered close on card {a['decision_id']}")
            if a["option"] in ("allow", "merge") and names_it:
                if replaced:
                    return Verdict.yes(f"replaced by merged work carried from it, which Patrick let merge on card "
                                       f"{a['decision_id']}")
                if not carried:
                    return Verdict.no(f"Patrick let {a['author']}'s work merge on card {a['decision_id']}, and no "
                                      "merged pull request carrying it was named; their pull request stays open")
                left = [f"#{n}" for (r, n) in carried if not merged.get((r, n))]
                if left:
                    return Verdict.no(f"the carried {', '.join(left)} has not merged yet, so PR {pr.number} is not "
                                      "replaced")
                return Verdict.yes(f"replaced by the carried work Patrick let merge on card {a['decision_id']}")
            return Verdict.no(f"Patrick answered {a['option']!r} on card {a['decision_id']}, which closes nothing")
        return Verdict.no(f"no answer from Patrick on an outside pull request card names PR {pr.number}; "
                          "a pull request is closed only on his answer")

    def check_green(self, pr: PullRequest, checks: list[CheckRun]) -> Verdict:
        """Every required check of the repo is a success on the exact head."""
        for name in self.policy.required_checks(pr.repo):
            verdict = self._one_check_green(pr, checks, name)
            if not verdict.allowed:
                return verdict
        return Verdict.yes()

    @staticmethod
    def _one_check_green(pr: PullRequest, checks: list[CheckRun], name: str) -> Verdict:
        runs = [c for c in checks if c.name == name and c.head_sha == pr.head_sha]
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

    # Issues

    # What the guide's "How an issue and a comment are written" keeps off the
    # public board: a footer or a session link, and a closing --- line.
    NOT_PLAIN = re.compile(r"(?im)generated (by|with) \[?claude|claude\.ai/code|^---\s*\Z")

    def open_issue(self, bot: str, repo: str, title: str, body: str) -> Verdict:
        """A new issue on the board: any Bot, in a repo the Bots work in, plain, and
        unclaimed (a finding goes on the board unclaimed, in Todo; the broker sends no
        label and no assignee, so whoever picks it claims it the usual way)."""
        p = self.policy
        if repo == p.data["repos"]["hermes"]:
            return Verdict.no("no Bot writes to Thornswood-Hermes; every change there is Patrick's")
        if p.data["merge"]["base"].get(repo) is None:
            return Verdict.no(f"{repo} is not a repo the Bots work in")
        if not title.strip() or len(title) > 256 or "\n" in title:
            return Verdict.no("an issue has a title of one line, at most 256 characters")
        if not body.strip():
            return Verdict.no("an issue says what it is for and when it is done")
        if self.NOT_PLAIN.search(body):
            return Verdict.no("an issue is plain: no footer, no session link and no closing --- line")
        return Verdict.yes(f"a new issue in {repo}, unclaimed")

    def close_issue(self, bot: str, issue: Issue, reason: str, body: str, *, duplicate: Issue | None = None,
                    done_pr: PullRequest | None = None, done_run: dict | None = None) -> Verdict:
        """Bramble closes an issue (Patrick, 9 Nov 2026: the Bots need the access
        to do their jobs; bramble-duties section 4 has Bramble close the issue a
        run finished, and Bramble's duplicates #5580 and #5585 waited for a
        person to close them). Only as Developer-Guide's board rules allow: a
        duplicate, pointing at the issue it repeats, or done, on a merged pull
        request or a successful run read from GitHub. Never "not planned":
        an issue is never closed for being later, and dropping work is Patrick's."""
        p = self.policy
        if not p.is_router(bot):
            return Verdict.no(f"only Bramble closes an issue; {p.bot_name(bot) if p.is_bot(bot) else bot} files and comments")
        if issue.repo == p.data["repos"]["hermes"]:
            return Verdict.no("Bramble never closes anything in TWHermesAgents")
        if p.data["merge"]["base"].get(issue.repo) is None:
            return Verdict.no(f"{issue.repo} is not a repo the Bots work in")
        if issue.pull_request:
            return Verdict.no(f"#{issue.number} is a pull request; close_pr closes those")
        if not issue.open:
            return Verdict.no(f"#{issue.number} is not open")
        if self._hands_off_issue(issue):
            return Verdict.no(self._hands_off_reason(issue.author_login, None, issue))
        if not body.strip():
            return Verdict.no("a close says why in a comment: what repeats it, or what finished it")
        if self.NOT_PLAIN.search(body):
            return Verdict.no("a comment is plain: no footer, no session link and no closing --- line")
        if reason == "duplicate":
            if duplicate is None or duplicate.number == issue.number or duplicate.pull_request:
                return Verdict.no("a duplicate names the other issue it repeats, in the same repo")
            return Verdict.yes(f"#{issue.number} repeats #{duplicate.number}")
        if reason == "completed":
            if done_pr is not None:
                if not done_pr.merged:
                    return Verdict.no(f"PR {done_pr.number} has not merged, so it has not finished #{issue.number}")
                return Verdict.yes(f"#{issue.number} was finished by PR {done_pr.number}")
            if done_run is not None:
                if done_run.get("status") != "completed" or done_run.get("conclusion") != "success":
                    return Verdict.no(f"run {done_run.get('id')} did not succeed "
                                      f"({done_run.get('status')}, {done_run.get('conclusion')})")
                return Verdict.yes(f"#{issue.number} was finished by run {done_run.get('id')}")
            return Verdict.no("a done issue names what finished it: a merged pull request or a successful run")
        return Verdict.no("an issue closes as a duplicate or as done; dropping work is Patrick's")

    # Claims

    def add_label(self, bot: str, issue: Issue, label: str, *, decision_id: str | None = None) -> Verdict:
        p = self.policy
        if (no := self._outside_app(bot, issue.repo)) is not None:
            return no
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
        if (no := self._outside_app(bot, issue.repo)) is not None:
            return no
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
        needs_patrick = channel == "main" and not runs.get("main_without_patrick")
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
        elif spec.get("inputs"):
            # A run with no gate line names each input it takes and the
            # pattern its value must match (tunnel.yml: the mode, the wait and
            # a recipient key). Anything else is refused before GitHub sees it.
            kind = "main"
            allowed = spec["inputs"]
            extra = set(inputs) - set(allowed)
            if extra:
                return Verdict.no(f"{workflow} takes only {', '.join(sorted(allowed))}, not {', '.join(sorted(extra))}")
            missing = [n for n in spec.get("required", []) if not inputs.get(n)]
            if missing:
                return Verdict.no(f"{workflow} needs {', '.join(missing)}")
            for name, value in inputs.items():
                if not re.fullmatch(allowed[name], value):
                    return Verdict.no(f"{workflow}'s {name} must match {allowed[name]}, not {value[:40]!r}")
        else:
            kind = "main"
        if needs_patrick:
            yes = self.decisions.check(decision_id, kind=kind, repo=repo, commit=commit)
            if not yes.allowed:
                return Verdict.no(f"this run needs Patrick's word for {commit[:12]}: {yes.reason}")
            return Verdict.yes(f"{yes.reason}; GitHub's Main environment still waits for Patrick")
        if channel == "main":
            return Verdict.yes(f"{workflow} for main at {commit[:12]}: Patrick allows main releases without "
                               f"a yes for each (policy.yaml runs.main_without_patrick)")
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
