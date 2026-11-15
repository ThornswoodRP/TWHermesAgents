"""The Bots' route to the decision core (#49): the tw_decide tool's broker.

Until 1 Nov the core had no route a Bot could call: its API answers Patrick's
phone and Patrick's own Hermes, both signed in, and nothing a Bot holds. So
Bramble could not file a card and asked in the Workbench room instead, where
nothing counts as Patrick's answer.

This is a Unix socket served by tw-decide-bots, as the core's own user, beside
tw-decide. The caller is checked by its Unix user from the socket's peer
credentials (thornswood_brokers.wire): only the Bots' user, hermes, and root.
The Bot is the one the tw_decide tool names from its own profile, never the
model, as the tw-git broker takes it; each Bot acts as its own service
principal in the core, so Bramble opens any card and a specialist only a
question on a task Bramble assigned it, exactly as DecisionCore allows. No Bot
holds a token or a GitHub credential for this. No Bot can answer a card: only
Patrick does, in the app.

An answered card reads back with the same words the connector sends the Bot
in the room (Chat._told_text), so the Bot learns the answer either way.
"""

from __future__ import annotations

import re
import threading
from datetime import timedelta
from pathlib import Path

from . import contracts
from .auth import Principal
from .chat import Chat
from .core import DecisionCore
from .own import OwnChat
from .store import CoreError, Store, iso

# Who may connect. Each op is open to both; the Bot inside is the tool's.
CALLERS = {"hermes", "root"}

# How long a card waits for Patrick before it expires, at most.
MAX_EXPIRY_DAYS = 14

# Who may read Patrick's answers on Bramble's outside pull request cards (#132)
# and on Main cards (step 6): tw-git alone, on a socket of its own, so it can
# carry out "close it" and "let them merge" itself, and have tw-policy record
# his "authorize" as the decision record a release needs.
GIT_CALLERS = {"tw-git", "root"}

# An outside pull request card (bramble-duties section 3) names, as pr links,
# the pull request and its author, and any Bot pull request carrying its work:
#   outside:ThornswoodRP/Thornswood#5517@rahul05ranjan
#   carried:ThornswoodRP/Thornswood#5550
OUTSIDE_LINK = re.compile(r"outside:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)@([A-Za-z0-9][A-Za-z0-9-]*)")
CARRIED_LINK = re.compile(r"carried:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)")
# The three lines an outside pull request card's body opens with answers to.
OUTSIDE_QUESTIONS = ("Safe:", "Can we make it safe:", "Of benefit:")
# The three answers tw-git carries out (Patrick, 8 Nov 2026, 1:59 PM: "I
# shou;ld be able to let them automerge, close it, and to merge it without
# allowinfg for automerge"), and the words their labels must start with, so
# the key Patrick picks is the thing the label he reads says.
# Patrick, 8 Nov 2026, 3:05 PM: "the wording should be Auto-Merge, Merge, and
# Close so we dont have a bunch of clutter with all the etxra unnesaccery
# words". The card offers these three and nothing else; the consequence line
# under each says who and which pull request.
OUTSIDE_ACTIONS = {"allow": "Auto-Merge", "merge": "Merge", "close": "Close"}


class BotRoute:
    def __init__(self, db_path: Path | str, workspace: str, *, clock=None):
        self.db_path = Path(db_path)
        self.workspace = workspace
        self.bots = tuple(contracts.load()["profiles"]["ids"])
        # The clock is the store's: the wall clock on the VM, a test's own
        # clock in the tests, so a brief's work hours do not hang on when
        # the suite runs (a1e61654's run failed at 11:21 PM, 11 Nov).
        self.store = Store(self.db_path, clock=clock) if clock else Store(self.db_path)
        self.core = DecisionCore(self.store)
        row = self.store.db.execute("SELECT id FROM workspaces WHERE name = ?", (workspace,)).fetchone()
        if not row:
            self.store.close()
            raise CoreError(404, "no_workspace", f"no workspace named {workspace}; run init first")
        self.ws = row["id"]
        # One connection, used by one request at a time (Store's own rule).
        self.lock = threading.Lock()

    def close(self) -> None:
        self.store.close()

    # ── who is asking ─────────────────────────────────────────────────────

    def _principal(self, args: dict) -> Principal:
        bot = args.get("bot")
        if not isinstance(bot, str) or bot not in self.bots:
            raise CoreError(400, "not_a_bot", f"{bot!r} is not one of the six Bots")
        kind = "bramble" if bot == "bramble" else "specialist"
        return self.core.auth.ensure_service(self.ws, kind, bot)

    # ── the ops ───────────────────────────────────────────────────────────

    def open_card(self, p: Principal, args: dict) -> dict:
        days = args.get("expires_in_days", 3)
        if not isinstance(days, int) or not 1 <= days <= MAX_EXPIRY_DAYS:
            raise CoreError(400, "expiry", f"a card expires in 1 to {MAX_EXPIRY_DAYS} days")
        links = args.get("links") or []
        if not isinstance(links, list) or not all(isinstance(x, dict) and isinstance(x.get("kind"), str)
                                                  and isinstance(x.get("ref"), str) for x in links):
            raise CoreError(400, "links", "links is a list of {kind, ref}")
        self._check_outside(p, [x["ref"] for x in links if x["kind"] == "pr"], _options(args), _text(args, "body"))
        return self.core.open_card(
            p, kind=_text(args, "kind"), title=_text(args, "title"), body=_text(args, "body"),
            options=_options(args), recommendation=_text(args, "recommendation"),
            scope_of_yes=_text(args, "scope_of_yes"), risk=args.get("risk") or "normal",
            urgent=bool(args.get("urgent")), expires_in=timedelta(days=days),
            source_key=args.get("source_key") or None, task=args.get("task") or None,
            links=[(x["kind"], x["ref"]) for x in links])

    def revise_card(self, p: Principal, args: dict) -> dict:
        refs = [r["ref"] for r in self.store.db.execute(
            "SELECT ref FROM task_links WHERE decision_id = ? AND kind = 'pr'", (_text(args, "decision_id"),))]
        self._check_outside(p, refs, _options(args), _text(args, "body"))
        return self.core.revise_card(
            p, _text(args, "decision_id"), expected_revision=_int(args, "expected_revision"),
            title=_text(args, "title"), body=_text(args, "body"), options=_options(args),
            recommendation=_text(args, "recommendation"), scope_of_yes=_text(args, "scope_of_yes"))

    def close_card(self, p: Principal, args: dict) -> dict:
        return self.core.close_card(p, _text(args, "decision_id"), status=_text(args, "status"),
                                    reason=_text(args, "reason"), superseded_by=args.get("superseded_by") or None)

    def assign_task(self, p: Principal, args: dict) -> dict:
        bot = _text(args, "assign_to")
        if bot not in self.bots or bot == "bramble":
            raise CoreError(400, "not_a_specialist", f"{bot!r} is not one of the five specialists")
        brief = args.get("brief")
        return self.core.assign_task(p, _text(args, "task"), bot, None if brief is None else str(brief))

    def get_card(self, p: Principal, args: dict) -> dict:
        card = self.core.get_card(p, _text(args, "decision_id"))
        return self._with_answer(card)

    def my_cards(self, p: Principal, args: dict) -> list[dict]:
        """The cards this Bot opened that are waiting or answered in the last two
        days; Bramble's include every card, as its Needs you list does."""
        db = self.store.db
        since = iso(self.store.clock() - timedelta(days=2))
        where = "" if p.kind == "bramble" else " AND requested_by = ?"
        params = [self.ws, since] + ([] if p.kind == "bramble" else [p.id])
        rows = db.execute("SELECT id FROM decisions WHERE workspace_id = ? AND (status = 'pending' OR updated_at >= ?)"
                          + where + " ORDER BY created_at", params).fetchall()
        return [self._with_answer(self.core.get_card(p, r["id"])) for r in rows]

    def report_work_done(self, p: Principal, args: dict) -> dict:
        self.core.report_work_done(p, _text(args, "decision_id"), _text(args, "detail"))
        return {"recorded": True}

    def ask_one(self, p: Principal, args: dict) -> dict:
        """A step of the agreed plan for One's Hermes, run there without Patrick (Patrick, 9 Nov, q-86); the
        answer comes back to this Bot's Bot Chat (own.py ask_one)."""
        return OwnChat(self.store).ask_one(p, _text(args, "text"), args.get("source_key"))

    def one_answer(self, p: Principal, args: dict) -> dict:
        return OwnChat(self.store).one_answer(p, _text(args, "message_id"))

    # ── outside pull request cards (#132) ─────────────────────────────────

    @staticmethod
    def _check_outside(p: Principal, refs: list[str], options: list[dict], body: str) -> None:
        """An outside pull request card names exactly one outside pull request,
        only Bramble opens one, and its options are exactly "allow" labelled
        "Auto-Merge", "merge" labelled "Merge" and "close" labelled "Close"
        (Patrick, 8 Nov 2026, 3:05 PM: no extra words). Its body answers, each on
        a line of its own, Patrick's three questions (8 Nov 2026, 2:00 PM: "the
        card also needs to tell me if its safe, if we can make it safe, and if
        its benifitial to us"): "Safe:", "Can we make it safe:" and "Of
        benefit:"."""
        outside = [m for m in (OUTSIDE_LINK.fullmatch(r) for r in refs) if m]
        if not outside:
            if any(r.startswith("outside:") for r in refs):
                raise CoreError(400, "outside_link", "an outside link is outside:<owner>/<repo>#<number>@<login>")
            return
        if len(outside) != 1:
            raise CoreError(400, "outside_link", "an outside pull request card names one outside pull request")
        if p.kind != "bramble":
            raise CoreError(403, "not_bramble", "only Bramble opens an outside pull request card")
        bad = [r for r in refs if r.startswith("carried:") and not CARRIED_LINK.fullmatch(r)]
        if bad:
            raise CoreError(400, "carried_link", "a carried link is carried:<owner>/<repo>#<number>")
        lines = [ln.strip().lstrip("*_ ").lower() for ln in body.splitlines()]
        missing = [q for q in OUTSIDE_QUESTIONS if not any(ln.startswith(q.lower()) for ln in lines)]
        if missing:
            raise CoreError(400, "outside_questions",
                            f"an outside pull request card answers each of {', '.join(OUTSIDE_QUESTIONS)} on a line "
                            f"of its own; it lacks {', '.join(missing)}")
        got = {o["key"]: o["label"] for o in options}
        if len(options) != len(OUTSIDE_ACTIONS) or got != OUTSIDE_ACTIONS:
            want = ", ".join(f"{k} labelled {v!r}" for k, v in OUTSIDE_ACTIONS.items())
            raise CoreError(400, "outside_label",
                            f"an outside pull request card offers exactly {want}")

    def outside_answers(self) -> list[dict]:
        """Patrick's answers on Bramble's outside pull request cards, newest
        last: what each card names and the option he picked on its current
        revision. Only answered cards Bramble asked, and only his answers."""
        db = self.store.db
        rows = db.execute(
            "SELECT d.id, r.option_key, r.answered_at FROM decisions d "
            "JOIN principals b ON b.id = d.requested_by "
            "JOIN responses r ON r.decision_id = d.id AND r.revision = d.revision "
            "JOIN principals who ON who.id = r.principal_id "
            "WHERE d.workspace_id = ? AND d.status = 'resolved' AND b.kind = 'bramble' AND who.kind = 'patrick' "
            # An answer in his own words chose no option (#107), so tw-git and the guard have nothing to act on.
            "AND r.option_key IS NOT NULL "
            "ORDER BY r.answered_at, d.id", (self.ws,)).fetchall()
        out = []
        for row in rows:
            refs = [r["ref"] for r in db.execute("SELECT ref FROM task_links WHERE decision_id = ? AND kind = 'pr'",
                                                 (row["id"],))]
            outside = [m for m in (OUTSIDE_LINK.fullmatch(r) for r in refs) if m]
            if len(outside) != 1:
                continue
            m = outside[0]
            carried = [{"repo": c.group(1), "number": int(c.group(2))}
                       for c in (CARRIED_LINK.fullmatch(r) for r in refs) if c]
            out.append({"decision_id": row["id"], "repo": m.group(1), "number": int(m.group(2)),
                        "author": m.group(3), "carried": carried, "option": row["option_key"],
                        "answered_at": row["answered_at"]})
        return out

    def git_ops(self) -> dict:
        """tw-git's ops, for its own socket: read Patrick's outside answers, and
        his answer on one Main card."""
        def outside(caller: str, args: dict):
            with self.lock:
                return self.outside_answers()

        def main(caller: str, args: dict):
            with self.lock:
                try:
                    return self.core.main_answer(self.ws, _text(args, "decision_id"))
                except CoreError as e:
                    from thornswood_brokers.wire import Refused
                    raise Refused(f"{e.reason}: {e}") from None
        return {"outside_answers": (outside, set(GIT_CALLERS)), "main_answer": (main, set(GIT_CALLERS))}

    def _with_answer(self, card: dict) -> dict:
        """The four states, and once Patrick answered, the words the Bot is told."""
        card = dict(card, states=self.core.states(card["id"]))
        if card["status"] == "resolved":
            cx = self.store.db.execute("SELECT id FROM connector_outbox WHERE decision_id = ? AND kind = "
                                       "'decision.answered' ORDER BY created_at DESC LIMIT 1",
                                       (card["id"],)).fetchone()
            if cx:
                card["answer_text"] = Chat._told_text(self.store.db, cx["id"])
        return card

    def keep_turned_away(self, p: Principal, args: dict) -> dict:
        """The Bot's own turn gate keeps a message the fleet queue turned away (core.keep_turned_away). It is
        not one of the model's tw_decide ops (brokers.DECIDE_OPS): only the gate asks it, for its own Bot."""
        return self.core.keep_turned_away(p, _text(args, "text"), str(args.get("reason") or ""),
                                          str(args.get("session") or ""))

    def queued_for(self, p: Principal, args: dict) -> dict:
        """How many messages wait in the connector's queue for each Bot, not yet handed to its Hermes: a
        brief, a kept message, an answer. Bramble's owed-wake check asks it, so a worker whose next turn is
        already on its way is not named as stopped, and the free worker look does not run while Bramble
        itself has a message coming (a worker's hand-back, answered in that turn). Not one of the model's
        tw_decide ops (brokers.DECIDE_OPS)."""
        if p.kind != "bramble":
            raise CoreError(403, "not_bramble", "only Bramble's wake check reads the connector's queue")
        rows = self.store.db.execute("SELECT target, COUNT(*) AS n FROM connector_outbox WHERE workspace_id = ? "
                                     "AND status IN ('queued', 'leased') GROUP BY target", (self.ws,)).fetchall()
        return {"bots": {r["target"]: r["n"] for r in rows if r["target"] in self.bots}}

    OPS = ("open_card", "revise_card", "close_card", "assign_task", "get_card", "my_cards", "report_work_done",
           "keep_turned_away", "queued_for", "ask_one", "one_answer")

    def ops(self) -> dict:
        """The ops for thornswood_brokers.wire.Server: name -> (handler(caller, args), callers)."""
        from thornswood_brokers.wire import Refused

        def wrap(name):
            fn = getattr(self, name)

            def handle(caller: str, args: dict):
                with self.lock:
                    try:
                        return fn(self._principal(args), args)
                    except CoreError as e:
                        raise Refused(f"{e.reason}: {e}") from None
            return handle
        return {name: (wrap(name), set(CALLERS)) for name in self.OPS}


def _text(args: dict, key: str) -> str:
    v = args.get(key)
    if not isinstance(v, str) or not v:
        raise CoreError(400, "missing", f"{key} is a text the op needs")
    return v


def _int(args: dict, key: str) -> int:
    v = args.get(key)
    if not isinstance(v, int) or isinstance(v, bool):
        raise CoreError(400, "missing", f"{key} is a whole number the op needs")
    return v


def _options(args: dict) -> list[dict]:
    opts = args.get("options")
    if not isinstance(opts, list) or not all(isinstance(o, dict) for o in opts):
        raise CoreError(400, "options", "options is a list of {key, label, consequence}")
    return [{"key": str(o.get("key") or ""), "label": str(o.get("label") or ""),
             "consequence": str(o.get("consequence") or "")} for o in opts]
