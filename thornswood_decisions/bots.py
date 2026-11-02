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

box_yes (2 Nov) is the one op that reaches past the core: when Patrick said
yes on a box card (DecisionCore.box_yes), it asks tw-policy, as the core's own
user, to record that yes as a box-change decision for that pull request at
that head, which is what tw-git's merge checks. Until then only root wrote
those records and nothing carried his answer there, so no pull request on a
Patrick path could be merged by Bramble at all.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path
from typing import Callable

from . import contracts
from .auth import Principal
from .chat import Chat
from .core import DecisionCore
from .store import CoreError, Store, iso, parse

# Who may connect. Each op is open to both; the Bot inside is the tool's.
CALLERS = {"hermes", "root"}

# How long a card waits for Patrick before it expires, at most.
MAX_EXPIRY_DAYS = 14


class BotRoute:
    def __init__(self, db_path: Path | str, workspace: str, *, policy=None,
                 ask_policy: Callable[[str, dict], dict] | None = None):
        self.db_path = Path(db_path)
        self.workspace = workspace
        # The guard's policy (who answers, how long a yes lives, tw-policy's
        # socket) and the call to tw-policy; both given in tests.
        self._policy = policy
        self._ask_policy = ask_policy
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
        return self.core.open_card(
            p, kind=_text(args, "kind"), title=_text(args, "title"), body=_text(args, "body"),
            options=_options(args), recommendation=_text(args, "recommendation"),
            scope_of_yes=_text(args, "scope_of_yes"), risk=args.get("risk") or "normal",
            urgent=bool(args.get("urgent")), expires_in=timedelta(days=days),
            source_key=args.get("source_key") or None, task=args.get("task") or None,
            links=[(x["kind"], x["ref"]) for x in links])

    def revise_card(self, p: Principal, args: dict) -> dict:
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

    def box_yes(self, p: Principal, args: dict) -> dict:
        """Record Patrick's yes on a box card with tw-policy and say what to
        pass to tw_git merge. Asking again for the same card returns the same
        record; a used one says so."""
        yes = self.core.box_yes(p, _text(args, "decision_id"))
        policy = self._policy_data()
        hours = policy["decisions"]["default_expiry_hours"]
        answered = parse(yes["answered_at"])
        record = {"id": yes["decision_id"], "kind": "box-change", "repo": yes["repo"], "commit": yes["commit"],
                  "revision": yes["revision"], "answered_by": policy["decisions"]["answered_by"],
                  "answered_at": iso(answered), "expires_at": iso(answered + timedelta(hours=hours)),
                  "pr": yes["pr"], "scope": f"{yes['repo']}#{yes['pr']} at {yes['commit'][:12]}"}
        out = self._call_policy("decision_record_box", {"decision": record})
        return {"decision_id": record["id"], "card_revision": record["revision"], "repo": record["repo"],
                "pr": record["pr"], "commit": record["commit"], "expires_at": record["expires_at"],
                "recorded": not out.get("existing"), "used_at": out.get("used_at"),
                "next": (f"tw_git merge {record['repo']}#{record['pr']} at {record['commit']} with "
                         f"decision_id {record['id']} and card_revision {record['revision']}"
                         if not out.get("used_at") else "this yes was used already; a new merge needs a new card")}

    def _policy_data(self) -> dict:
        if self._policy is None:
            from thornswood_guard.policy import Policy
            self._policy = Policy.load()
        return self._policy.data

    def _call_policy(self, op: str, args: dict) -> dict:
        if self._ask_policy is None:
            from thornswood_brokers import wire
            path = self._policy_data()["brokers"]["sockets"]["policy"]["path"]
            self._ask_policy = lambda o, a: wire.call(path, o, a, timeout=30.0)
        from thornswood_brokers.wire import Refused
        try:
            return self._ask_policy(op, args)
        except Refused as e:
            raise CoreError(409, "policy_refused", f"tw-policy did not record it: {e}") from None

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

    OPS = ("open_card", "revise_card", "close_card", "assign_task", "get_card", "my_cards", "report_work_done",
           "box_yes")

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
