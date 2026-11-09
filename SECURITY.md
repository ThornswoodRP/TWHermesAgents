# Reporting a vulnerability

This repository holds the guard rails for Thornswood's AI agents: the rules that keep them away from credentials, the brokers that hold those credentials, and the decision app Patrick answers them from. A hole here could let an agent, or someone posing as one, do what only a person should. Please report it quietly.

## Where to send it

Any of these. Nothing about a report is public until we say so.

- **On GitHub:** the **Report a vulnerability** button on this repository's Security tab, when it is shown. Only the owner reads it.
- **On Discord:** a direct message to the owner, who alone reads it. The Discord is linked from every page of [thornswood.net](https://thornswood.net).
- **On the site, with no account:** the [report page](https://thornswood.net/report). The staff read it, so say at the top that it is a security hole and leave out the steps; the owner will ask for them.

Please do not open a public issue or pull request for a security problem, and please do not post it in a Discord channel.

## What to include

Whatever you have. A sentence is a real report.

- What you did, in the order you did it.
- What happened that should not have.
- Which part: the guard, a broker, the decision app, the install, or the VM.

You do not need a proof of concept. Please do not test against the live VM or anybody's accounts; the test suite and a scratch VM are enough to show it.

## What happens next

- A reply within **72 hours**, saying whether it is understood and whether it is being worked on.
- The fix lands here as a pull request with a test that fails without it, and the VM installs it by itself once it merges.
- You will be told when it is fixed, and credited if you want to be.

## What is in scope

- Anything that lets an agent reach a credential, a GitHub write, a release or a deploy that the policy does not allow.
- Anything that gets past the guard, a broker, the sandbox or the host's limits.
- The decision app: signing in, answering a card, chat, alerts and voice.
- The install and the follower: anything that would run code that did not merge to `main`.

Thornswood's own [security policy](https://github.com/ThornswoodRP/Thornswood/blob/main/SECURITY.md) covers the game, the site, the launcher and the rest.
