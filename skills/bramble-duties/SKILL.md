---
name: bramble-duties
description: Bramble's standing work. Put the Bots' reviewed pull requests on GitHub's auto-merge, merge green Ops pull requests, hand a pull request the merge queue dropped back to its owner, and run the Ops run notes and the Desk's list. Use on every GitHub wake and whenever asked to work the queue.
version: 1.3.0
author: Thornswood-Hermes (repo-owned; scripts/apply.py installs it, never edit it on the VM)
platforms: [linux]
metadata:
  hermes:
    tags: [Thornswood, Bramble, merge, queue, run notes]
    related_skills: [thornswood-ops, thornswood-guide]
---

# Bramble's duties

GitHub's auto-merge merges in Thornswood and the fork, not you (Patrick,
2 Nov 2026: "He shouldn't have to be merging to begin with", and "I
shouldn't need to approve"). Whoever opens a pull request there puts it on
auto-merge; for the Bots' and the robots' pull requests that is you, once your review
stands.
Ops is private on GitHub Free, which has no auto-merge, so you merge Ops
pull requests yourself. Nothing you merge waits for a yes from Patrick. You
also run the notes. Since 1 Nov the Desk only keeps the report page. Do every
step through `tw_git` and `tw_decide`. A refusal is a stop: say what was
refused and why, and do not try another route.

## 1. The Bots' pull requests go on auto-merge, Ops merges when green

Every finished check suite in the three repositories is gathered into one
wake, so a wake is never only about the commit it names. Sweep all of them,
every time.

In ThornswoodRP/Thornswood and ThornswoodRP/skymp:

1. `tw_git read` `GET /repos/<repo>/pulls?state=open&per_page=100`.
2. Only a Bot's pull request (its branch is `<bot>/<issue>-<what>`) and a
   robot's (Dependabot's, the pin robot's: the policy's `robot_authors`) are
   yours. A person's or a thread's is its owner's, who puts it on
   auto-merge; leave it alone, green or not.
3. For each of those pull requests that is not a draft and has no auto-merge on
   (`auto_merge` is null), review its exact head. Once your review stands,
   `tw_git merge` on that head turns GitHub's auto-merge on. Do not wait for
   the checks: GitHub merges once the checks main's (or the fork branch's)
   rules require pass, through the merge queue in Thornswood. tw_git refuses
   when those rules do not require every check the policy names, or when one
   is already red.
4. A new push to a Bot's pull request after its review needs your review of
   the new head, then `tw_git merge` again.

In ThornswoodRP/Thornswood-Ops:

1. `tw_git read` `GET /repos/ThornswoodRP/Thornswood-Ops/pulls?state=open&per_page=100`.
2. For each pull request that is not a draft, read its head's check runs
   (`GET /repos/ThornswoodRP/Thornswood-Ops/commits/<head sha>/check-runs`).
   Merge it with `tw_git merge` on that exact head when `check` passed there
   and your review stands. It merges as a squash.

In all three:

- A red required check goes back to the pull request's owner as a comment
  that names the check and links its run. Do nothing else with it.
- No merge needs a card or Patrick's yes, and none is recorded in
  DECIDED.md.
- Never merge ThornswoodRP/Thornswood-Hermes. Patrick's own Hermes merges it.

## 2. A pull request the merge queue dropped

A wake for `pull_request` `dequeued` with `merged` false means the queue let
the pull request go. GitHub also turns its auto-merge off. The event's
`reason` is GitHub's own word, for example MERGE_CONFLICT, CI_FAILURE or
CI_TIMEOUT.

1. Find the queue's run:
   `GET /repos/<repo>/actions/runs?event=merge_group&per_page=30`, the run
   whose `head_branch` starts with `gh-readonly-queue/main/pr-<number>-`. A
   conflict has no run; say so.
2. Hand the pull request back with one `tw_git comment` on it: the reason,
   the run's link, and what the owner does (MERGE_CONFLICT: bring main in and
   push; CI_FAILURE: fix the named job and push; CI_TIMEOUT: say how long the
   run waited for a runner and how long it ran). If a `claimed: <Bot>` label
   names a Bot, also `tw_decide assign_task` the pull request to that Bot.
3. Do not put it back yourself. There is no timer and no retry. When the
   owner pushes the fix, the owner turns auto-merge on again; for a Bot's
   pull request that is you, through step 1, once your review of the new
   head stands.

## 3. The run notes and the Desk's list

Notes are in ThornswoodRP/Thornswood-Ops at `ops/claude-queue/` on main
(`ops/README.md` has their template). The Desk's handover list is
`ops/bramble-list.md`. Read both on every wake, and whenever you are asked
to work the queue.

1. List them: `GET /repos/ThornswoodRP/Thornswood-Ops/contents/ops/claude-queue?ref=main`,
   then read each note.
2. A note runs once the public pull request it names is merged, dev, main
   and live alike. Work Patrick already agreed runs without a card: the note
   or its issue cites his answer, or it is agreed plan work (Patrick, 2 Nov
   2026: "I shouldnt have had to fucking do a card for that", about the
   #5334 tunnel note he had answered A on the day before). A card goes to
   him only for a step that weakens security, spends money (listed, never
   recommended, and never Actions minutes or billing, which he will not pay
   for: the ops skill, section 13) or touches a
   secret, or for a question you need answered that he has not already
   decided. Never ask whether to do what he already told you to do ("they
   better not ask questions like do you want to do this thign that you told
   me to do"). A note that asks for a card for anything else is wrong: run
   it, and say on its issue that the card was not needed. Never ask in a
   room.
3. Run a step only through `tw_git start_run` and a verb the policy allows. A
   step no verb covers is a stop: file it for Magpie, with what is missing,
   and never run it another way. It is not a card for Patrick.
4. After a run:
   - put what ran, when, what it printed and the health check on the issue;
   - make one Ops pull request for the batch that deletes the notes that ran
     and rewrites `ops/status.md`;
   - close the issue the run finished, with its `Test task:` comment when
     people would notice the fix;
   - a note that cannot run keeps the reason written into it.
5. Work the items on `ops/bramble-list.md` the same way, and take each one
   off the list in the same Ops pull request once it is done. Done means
   what it asks for is true on GitHub, whoever did it: a merge whose pull
   request is already merged, a run whose result is already on its issue.
   Check every item against GitHub each time you read the list, and take
   the done ones off with the rest, so the list holds only what is left
   (2 Nov 2026: Ops merges others had landed stayed on it for a day,
   because only Bramble's own runs counted as done).

## Every time

Say what you did in the Thornswood Workbench room: each pull request merged
or handed back, and each note run, with links.
