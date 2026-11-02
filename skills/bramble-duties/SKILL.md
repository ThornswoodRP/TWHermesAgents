---
name: bramble-duties
description: Bramble's standing work. Merge green pull requests, hand a pull request the merge queue dropped back to its owner, and run the Ops run notes and the Desk's list. Use on every GitHub wake and whenever asked to work the queue.
version: 1.0.0
author: Thornswood-Hermes (repo-owned; scripts/apply.py installs it, never edit it on the VM)
platforms: [linux]
metadata:
  hermes:
    tags: [Thornswood, Bramble, merge, queue, run notes]
    related_skills: [thornswood-ops, thornswood-guide]
---

# Bramble's duties

Nobody else merges, releases or runs the notes. Since 1 Nov the Desk only
keeps the report page, and no thread waits on a merge. If you don't do these,
nobody does. On 2 Nov fork #147 and Ops #200 and #201 sat green for hours
because nothing told you about them. Do every step through `tw_git` and
`tw_decide`. A refusal is a stop: say what was refused and why, and do not
try another route.

## 1. Merge what is green

For each of ThornswoodRP/Thornswood, ThornswoodRP/Thornswood-Ops and
ThornswoodRP/skymp:

1. `tw_git read` `GET /repos/<repo>/pulls?state=open&per_page=100`.
2. For each pull request that is not a draft, read its head's check runs
   (`GET /repos/<repo>/commits/<head sha>/check-runs`). Merge it with
   `tw_git merge` on that exact head when:
   - every check the policy requires for the repository passed;
   - your review stands;
   - nothing in the guide holds it (a Patrick path needs his yes on a card for
     that head).

   In Thornswood the merge puts the pull request in the merge queue. In the
   fork it is a merge commit, and in Ops a squash, as the policy says.
3. A red required check goes back to the pull request's owner as a comment
   that names the check and links its run. Do nothing else with it.
4. Never merge ThornswoodRP/Thornswood-Hermes. Patrick's own Hermes merges it.

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
   owner pushes, the check suite on the new head wakes you, and step 1 merges
   it when it is green.

## 3. The run notes and the Desk's list

Notes are in ThornswoodRP/Thornswood-Ops at `ops/claude-queue/` on main
(`ops/README.md` has their template). The Desk's handover list is
`ops/bramble-list.md`. Read both whenever you are woken by a merged pull
request, and whenever you are asked to work the queue.

1. List them: `GET /repos/ThornswoodRP/Thornswood-Ops/contents/ops/claude-queue?ref=main`,
   then read each note.
2. A note runs only once the public pull request it names is merged. A dev
   note runs when it is ready. A main Ship, a hotfix, and a live server or
   gamemode deploy run only on Patrick's yes on a card for that exact build:
   open the card with `tw_decide` and wait for his answer. Never ask in a room.
3. Run a step only through `tw_git start_run` and a verb the policy allows. A
   step no verb covers goes to Patrick as a card, written out in full. Never
   run it another way.
4. After a run:
   - put what ran, when, what it printed and the health check on the issue;
   - make one Ops pull request for the batch that deletes the notes that ran
     and rewrites `ops/status.md`;
   - close the issue the run finished, with its `Test task:` comment when
     people would notice the fix;
   - a note that cannot run keeps the reason written into it.
5. Work the items on `ops/bramble-list.md` the same way, and take each one
   off the list in the same Ops pull request once it is done.

## Every time

Say what you did in the Thornswood Workbench room: each pull request merged
or handed back, and each note run, with links.
