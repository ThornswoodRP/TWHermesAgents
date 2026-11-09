---
name: thornswood-ops
description: Thornswood's private operations. The four repos and the two GitHub Apps, Dev and Main, the box, One, signing and Ships, the deploy gate and its verbs, run notes and status, where secrets live (by name only), recurring jobs, traps, standing orders (Hermes on Discord included), and what always needs Patrick. Load it before any run, deploy, release, box or Nexus step.
version: 2.0.0
metadata:
  hermes:
    tags: [thornswood, operations, release, deploy]
    category: thornswood
---

# Thornswood operations, for the Bots

## Rule zero: fix the cause, no band-aids, ever

This comes before every other line here. It is word for word the rule every Claude, Bot and person on Thornswood works by.

- A band-aid is anything that gets past a failure without removing what causes it. That covers how work gets done as much as code. All of these are band-aids:
  - re-running a failed check, job, download, install or setup script in the hope it passes;
  - merging past a red check, or asking Patrick to merge or click past one;
  - handing Patrick a manual step because a tool, script or check failed;
  - stepping around a rule, a safety check or a refusal, or asking someone else to do the refused step;
  - putting something where it's easy instead of where it belongs (a link under a to-do instead of inside its step);
  - fixing one copy of a problem while whatever made it keeps making more;
  - a retry, a timer, a wait, an empty catch, a "for now", or anything that notices damage and undoes it.
- Before you ship or offer any fix, write its cause in one sentence: what breaks and why. Put that sentence in the PR and in the reply. If you can't write it, you're still measuring, not fixing.
- A failure that isn't your change's is still a failure to fix at its cause. Find where it comes from (the base branch, a dependency, the machine, the network) and fix it there in its own PR. If you can't, put it on the board with what was measured. Never step around it.
- Never offer Patrick a band-aid, not even as a second option next to the real fix.
- When the cause isn't known, say so, say what was measured, and name the missing measurement and who can take it. Then take it.
- A cause in the engine is fixed in the skymp fork, not worked around in the gamemode or launcher.
- Read the code around the fix for band-aids already there. A fix that leaves one in place is not finished; remove it together with the cause it covered.
- When your own earlier change turns out to be a band-aid, say so, revert it, and record that on its issue.
- Measure before claiming a cause: read `Profile\launcher.log`, the file itself, the timestamps. When a measurement contradicts Patrick about his own setup, re-examine the measurement first. When the doc and the machine disagree, say so.
- Fix what's reported in the same reply. A write-up that changes nothing is not the job.

## Where the rest of this skill is

TWHermesAgents is public, so the private operations are not here. They are
in the private repository, Thornswood-Ops, at `docs/hermes/thornswood-ops.md`:
the four repositories and who writes where, Dev and Main, the box, One,
releases and signing, the deploy gate and its verbs, run notes and status,
where each secret lives (by name only), the collection, recurring jobs, traps,
standing orders, and what needs Patrick.

Before any run, deploy, release, box or Nexus step, read that file whole, on
Ops `main`, with `tw_git read`:

    GET /repos/ThornswoodRP/Thornswood-Ops/contents/docs/hermes/thornswood-ops.md?ref=main

Read it again at each new task: what it says can change between tasks. If the
read is refused or the file is missing, that is a stop: say what was refused,
and run nothing that needed it.

Developer-Guide.md binds you first, every rule in it. That file adds what only
matters to the Bots and to private operations. When it and the machine
disagree, the machine is right: say so to Patrick. A change to it is an Ops
pull request. This skill is installed by `scripts/apply.py` from
TWHermesAgents, which no Bot writes to.

Nothing private goes into a public repository: no run command with a secret's
path, no evidence dump, no address, no user name in a path, nothing quoted from
that file. Evidence for a public issue goes in Ops `issues/<n>.md`.
