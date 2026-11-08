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
from .store import CoreError, Store, iso

# Who may connect. Each op is open to both; the Bot inside is the tool's.
CALLERS = {"hermes", "root"}

# How long a card waits for Patrick before it expires, at most.
MAX_EXPIRY_DAYS = 14

# Who may read Patrick's answers on Bramble's outside pull request cards (#132):
# tw-git alone, on a socket of its own, so it can carry out "close it" and
# "let them merge" itself.
GIT_CALLERS = {"tw-git", "root"}

# An outside pull request card (bramble-duties section 3) names, as pr links,
# the pull request and its author, and any Bot pull request carrying its work:
#   outside:ThornswoodRP/Thornswood#5517@rahul05ranjan
#   carried:ThornswoodRP/Thornswood#5550
OUTSIDE_LINK = re.compile(r"outside:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)@([A-Za-z0-9][A-Za-z0-9-]*)")
CARRIED_LINK = re.compile(r"carried:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)")
# The two answers tw-git carries out, and the words their labels must start
# with, so the key Patrick picks is the thing the label he reads says.
OUTSIDE_ACTIONS = {"close": "Close", "allow": "Let {author} merge"}


class BotRoute:
    def __init__(self, db_path: Path | str, workspace: str):
        self.db_path = Path(db_path)
        self.workspace = workspace
        self.bots = tuple(contracts.load()["profiles"]["ids"])
        self.store = Store(self.db_path)
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
        self._check_outside(p, [x["ref"] for x in links if x["kind"] == "pr"], _options(args))
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
        self._check_outside(p, refs, _options(args))
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
        self.core.assign_task(p, _text(args, "task"), bot)
        return {"task": args["task"], "bot": bot}

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

    # ── outside pull request cards (#132) ─────────────────────────────────

    @staticmethod
    def _check_outside(p: Principal, refs: list[str], options: list[dict]) -> None:
        """An outside pull request card names exactly one outside pull request,
        only Bramble opens one, and the options tw-git carries out say so in
        their labels: "close" starts "Close", "allow" starts "Let <author> merge"."""
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
        author = outside[0].group(3)
        for o in options:
            want = OUTSIDE_ACTIONS.get(o["key"])
            if want and not o["label"].startswith(want.format(author=author)):
                raise CoreError(400, "outside_label",
                                f"option {o['key']!r} is carried out by tw-git, so its label starts "
                                f"{want.format(author=author)!r}")

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
        """tw-git's one op, for its own socket: read Patrick's outside answers."""
        def handle(caller: str, args: dict):
            with self.lock:
                return self.outside_answers()
        return {"outside_answers": (handle, set(GIT_CALLERS))}

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

    OPS = ("open_card", "revise_card", "close_card", "assign_task", "get_card", "my_cards", "report_work_done")

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
