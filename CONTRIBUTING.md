# Contributing

Thank you for looking. This page says how a change to TWHermesAgents is made, tested and merged.

## Before you start

- **Fix the cause.** A change finds why something goes wrong and changes that. A retry, a wait, a cap or a step that undoes damage after it happens is not a fix here. If the cause is not known yet, finding it is the work: say what you measured in the pull request.
- **Security problems** go to [SECURITY.md](SECURITY.md), never to a public issue.
- **Every issue carries a label**: `bug` for something wrong, `enhancement` for something new. The issue forms add it; an issue filed through the API names it itself.
- **The Bots cannot change this repository.** The guard and the git broker both refuse it, so every change here comes from the team, through a pull request.

## Setting up

You need Python 3.12 or newer and git. The tests run on Linux and Windows.

```bash
git clone https://github.com/ThornswoodRP/TWHermesAgents.git
cd TWHermesAgents
python -m pip install --require-hashes --only-binary :all: --no-deps -r tests/requirements-ci.txt
python -m pip install --require-hashes --only-binary :all: --no-deps -r deploy/decisions/requirements.txt
```

These are the exact libraries CI installs, each pinned by hash.

## Running the tests

```bash
python -m unittest discover -s tests
```

One test file at a time:

```bash
python -m unittest discover -s tests -p test_github.py -v
```

Some tests need Linux, Windows, root, bash, Node or PowerShell and skip themselves where those are missing. The suite holds no secret and reaches no network.

## Making a change

1. Branch from `main`.
2. Write a test that fails without the change. Every rule held in code has a test, and the attack tests in [docs/security.md](docs/security.md) must keep passing.
3. Make the change.
4. If you changed `src/thornswood_guard/policy.yaml`, regenerate what is built from it. A test fails if either is stale:

   ```bash
   python scripts/render_profiles.py
   ```

   ```bash
   python scripts/render_units.py
   ```

5. Run the whole suite.
6. Update the docs that describe what you changed.
7. Open a pull request against `main` that says what was wrong, what caused it, and how you checked the fix.

One fix per pull request. If several commits build one change, they go up as one pull request.

## How a pull request merges

Every push to a pull request, and every push to `main`, runs **Guard rail tests** on GitHub's own runners:

- **Guard rail tests (VM's Python)**: Ubuntu 24.04 with Python 3.12, the VM's own;
- **Guard rail tests (One's Python)**: Windows with Python 3.13, named for Patrick's Windows machine, so the suite keeps working on a Windows checkout.

When both pass on a pull request's exact head, `.github/workflows/merge-green.yml` merges it with nobody touching it. It runs `main`'s own `scripts/merge_green.py`, never a pull request's code, and merges a pull request only when it is open, not a draft, from this repository, aimed at `main`, green on both checks with nothing else on that head failing, and clean to merge. A new push runs the checks again.

A failing check is fixed at its cause, never re-run to get a pass.

Pull requests from forks are welcome. GitHub may ask a maintainer to approve their checks before they run, and merge-green never merges one: a maintainer reviews it.

No job here runs on a self-hosted runner. In a public repository any pull request can name any runner label, so `tests/test_ci_workflow.py` holds every job to GitHub's own.

## After a merge

The Bots' VM picks the merge up within ten minutes, runs the whole suite again there with no network, and installs it only if every test passes ([docs/operations.md](docs/operations.md)). There is nothing to deploy by hand.

## Writing

The docs, comments and pull requests here follow Thornswood's house style:

- Plain English, in full sentences.
- No em dashes.
- Times in Mountain time on the 12-hour clock (3:30 AM).
- They/them for anyone whose pronouns you do not know.
- The owner is Patrick.
- Thornswood's word list, which `scripts/dev/Check-Words.ps1` in the [Thornswood repository](https://github.com/ThornswoodRP/Thornswood) checks.
