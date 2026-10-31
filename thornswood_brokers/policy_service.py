"""tw-policy: the fleet admission queue, receipts, decisions, routes, leases
and the Dev ledger, in one SQLite store (08 section 7; 04 sections 5 and 6).

It holds no credential and runs no model or shell. Every other service asks
it before acting:

- the gateway (as hermes) asks for a ticket before each model turn, and gives
  it back after; a turn with no ticket waits, and new work past the queue's
  limits is refused at the gateway's door;
- tw-git asks before every GitHub write (op_begin) and reports what GitHub
  said (op_finish), so a write is done at most once: after a crash the next
  ask says "reconcile", and tw-git reads GitHub before anything else;
- the admission proxy asks whether a Bot's current turn may use route 2 or 4;
- the limits check at boot (as root) records whether the slices hold; with no
  passing check, no sandbox job is admitted.

Nothing here trusts a Bot's word for who it is: the gateway plugin sends the
profile the call runs in, and the socket tells tw-policy which user sent it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
import weakref
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from thornswood_guard.decisions import Decision
from thornswood_guard.policy import Policy, Verdict
from thornswood_guard.routes import credits_open, route_allowed

from . import host as hostmod
from .wire import Refused

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY, bot TEXT NOT NULL, kind TEXT NOT NULL, risk TEXT NOT NULL,
  task TEXT NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL,
  created_at TEXT NOT NULL, admitted_at TEXT, seen_at TEXT, finished_at TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS tickets_state ON tickets(state, created_at);
CREATE TABLE IF NOT EXISTS task_risk (task TEXT PRIMARY KEY, risk TEXT NOT NULL, set_by TEXT NOT NULL, set_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations (
  key TEXT PRIMARY KEY, bot TEXT NOT NULL, action TEXT NOT NULL, repo TEXT NOT NULL,
  target TEXT NOT NULL, commit_sha TEXT NOT NULL, task TEXT NOT NULL, state TEXT NOT NULL,
  attempt INTEGER NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, receipt TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routes (id TEXT PRIMARY KEY, windows TEXT NOT NULL, spent_usd REAL NOT NULL, cycle TEXT NOT NULL, refused_until TEXT);
CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, holder TEXT NOT NULL, generation INTEGER NOT NULL, acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lease_generations (name TEXT PRIMARY KEY, generation INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, issue INTEGER NOT NULL, event TEXT NOT NULL,
  release TEXT NOT NULL DEFAULT '', at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
  UNIQUE(repo, issue, event, release));
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, caller TEXT NOT NULL, op TEXT NOT NULL, detail TEXT NOT NULL);
"""

TICKET_SEEN_TIMEOUT = timedelta(minutes=10)
WAITING_TIMEOUT = timedelta(hours=24)
RISKS = ("low", "high")
OUTCOMES = ("confirmed", "failed", "unknown")
LEDGER_EVENTS = ("eligible", "reserved_for_candidate", "Dev_verified", "Main_verified",
                 "reverted", "superseded", "counter_consumed", "candidate_failed")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


class PolicyService:
    def __init__(self, db_path: Path | str, policy: Policy, *,
                 host_reader: Callable[[], hostmod.HostState] | None = None,
                 clock: Callable[[], datetime] = now_utc):
        self.policy = policy
        self.rules = policy.data["admission"]
        self.clock = clock
        self.host_reader = host_reader or (lambda: hostmod.read("/", srv=policy.data["brokers"]["state"]["work"].rsplit("/", 1)[0]))
        self.db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        weakref.finalize(self, self.db.close)
        self._recover()

    # Plumbing

    def _tx(self):
        return _Tx(self)

    def _meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def _audit(self, caller: str, op: str, detail: dict) -> None:
        self.db.execute("INSERT INTO audit(at, caller, op, detail) VALUES(?,?,?,?)",
                        (iso(self.clock()), caller, op, json.dumps(detail, sort_keys=True)))

    def _recover(self) -> None:
        """At start: a write that was in flight when the service stopped has an
        unknown outcome. It is never assumed done or not done: the next ask for
        it gets "reconcile", and tw-git reads GitHub first."""
        with self._tx():
            n = self.db.execute("UPDATE operations SET state='unknown', reason='tw-policy restarted while it was in flight' WHERE state='started'").rowcount
            if n:
                self._audit("tw-policy", "recover", {"unknown_ops": n})

    # Limits check (root only)

    def limits_record(self, caller: str, args: dict) -> dict:
        ok = bool(args.get("ok"))
        details = args.get("details") or []
        with self._tx():
            self._set_meta("limits_ok", "1" if ok else "0")
            self._set_meta("limits_at", iso(self.clock()))
            self._set_meta("limits_details", json.dumps(details))
            self._audit(caller, "limits_record", {"ok": ok, "details": details})
        return {"ok": ok}

    def limits_ok(self) -> bool:
        return self._meta("limits_ok") == "1"

    # The queue

    def _stage_limit(self, name: str) -> int:
        return int(self.rules[name][self.rules["stage"]])

    def _expire_silent(self) -> None:
        """An admitted ticket nobody has touched for ten minutes is given back,
        so a crashed turn never holds the fleet's one slot. It frees a slot for a
        model turn; it never lets a second writer in (writes are fenced by the
        operations table, not by tickets)."""
        cutoff = iso(self.clock() - TICKET_SEEN_TIMEOUT)
        self.db.execute("UPDATE tickets SET state='expired', finished_at=?, reason='no word from the turn for ten minutes' "
                        "WHERE state='admitted' AND seen_at < ?", (iso(self.clock()), cutoff))
        # A waiting ticket keeps its place for a day; after that it is gone.
        self.db.execute("UPDATE tickets SET state='expired', finished_at=?, reason='waited a day with no word' "
                        "WHERE state='waiting' AND COALESCE(seen_at, created_at) < ?",
                        (iso(self.clock()), iso(self.clock() - WAITING_TIMEOUT)))

    def admit(self, caller: str, args: dict) -> dict:
        """Ask for a ticket, or ask again for one already waiting. Returns
        {"ticket", "state": "admitted"|"waiting", "reason"}; raises Refused when
        the queue will not take it at all."""
        bot = _bot(self.policy, args)
        kind = args.get("kind", "turn")
        if kind not in ("turn", "sandbox"):
            raise Refused(f"{kind} is not a kind of work the queue admits")
        ticket = args.get("ticket")
        with self._tx():
            self._expire_silent()
            if ticket:
                row = self.db.execute("SELECT bot, kind, state, created_at FROM tickets WHERE id=?", (ticket,)).fetchone()
                if row is None:
                    raise Refused(f"ticket {ticket} is not in the queue")
                if row[0] != bot:
                    raise Refused(f"ticket {ticket} is {row[0]}'s, not {bot}'s")
                if row[2] == "admitted":
                    return {"ticket": ticket, "state": "admitted", "reason": "already admitted"}
                if row[2] != "waiting":
                    raise Refused(f"ticket {ticket} is {row[2]}; ask for a new one")
                kind, created = row[1], row[3]
                self.db.execute("UPDATE tickets SET seen_at=? WHERE id=?", (iso(self.clock()), ticket))
            else:
                task = str(args.get("task") or "")
                if not task:
                    raise Refused("a ticket names the task it is for")
                waiting = self.db.execute("SELECT COUNT(*) FROM tickets WHERE state='waiting'").fetchone()[0]
                if waiting >= self.rules["queue_max_waiting"]:
                    raise Refused(f"{waiting} tickets are already waiting; the queue holds at most {self.rules['queue_max_waiting']}")
                hour_ago = iso(self.clock() - timedelta(hours=1))
                new = self.db.execute("SELECT COUNT(*) FROM tickets WHERE created_at >= ?", (hour_ago,)).fetchone()[0]
                if new >= self.rules["queue_new_per_hour"]:
                    raise Refused(f"{new} tickets were made in the last hour; the queue takes at most {self.rules['queue_new_per_hour']} an hour")
                risk_row = self.db.execute("SELECT risk FROM task_risk WHERE task=?", (task,)).fetchone()
                risk = risk_row[0] if risk_row else "high"
                ticket = uuid.uuid4().hex
                created = iso(self.clock())
                self.db.execute("INSERT INTO tickets(id, bot, kind, risk, task, source, state, created_at) VALUES(?,?,?,?,?,?, 'waiting', ?)",
                                (ticket, bot, kind, risk, task, str(args.get("source") or "chat"), created))
            reason = self._why_not_now(kind, ticket)
            if reason:
                self.db.execute("UPDATE tickets SET reason=? WHERE id=?", (reason, ticket))
                return {"ticket": ticket, "state": "waiting", "reason": reason}
            t = iso(self.clock())
            self.db.execute("UPDATE tickets SET state='admitted', admitted_at=?, seen_at=?, reason=NULL WHERE id=?", (t, t, ticket))
            self._audit(caller, "admit", {"ticket": ticket, "bot": bot, "kind": kind})
            return {"ticket": ticket, "state": "admitted", "reason": ""}

    def _why_not_now(self, kind: str, ticket: str) -> str:
        if kind == "sandbox":
            limit = self._stage_limit("sandbox_jobs")
            if limit == 0:
                return f"no sandbox job runs in the {self.rules['stage']} stage"
            if not self.limits_ok():
                return "the last limits check at boot did not pass; no sandbox job runs until one does"
        else:
            limit = self._stage_limit("model_turns")
        # Only tickets still asking hold the line: one whose turn went away
        # without a word (no ask for ten minutes) keeps its place in the count
        # but lets the next one by, so a lost turn never stalls the fleet.
        fresh = iso(self.clock() - TICKET_SEEN_TIMEOUT)
        ahead = self.db.execute("SELECT COUNT(*) FROM tickets WHERE state='waiting' AND kind=? "
                                "AND COALESCE(seen_at, created_at) >= ? "
                                "AND rowid < (SELECT rowid FROM tickets WHERE id=?)", (kind, fresh, ticket)).fetchone()[0]
        if ahead:
            return f"{ahead} earlier {kind} tickets are waiting first"
        active = self.db.execute("SELECT COUNT(*) FROM tickets WHERE state='admitted' AND kind=?", (kind,)).fetchone()[0]
        if active >= limit:
            return f"{active} {kind} already running; the fleet runs at most {limit} at once"
        state = self.host_reader()
        drain = hostmod.drain_problems(state, self.rules)
        if drain:
            return "the host is draining: " + "; ".join(drain)
        problems = hostmod.admit_problems(state, self.rules, sandbox=(kind == "sandbox"))
        if problems:
            return "; ".join(problems)
        return ""

    def touch(self, caller: str, args: dict) -> dict:
        bot = _bot(self.policy, args)
        with self._tx():
            n = self.db.execute("UPDATE tickets SET seen_at=? WHERE id=? AND bot=? AND state='admitted'",
                                (iso(self.clock()), args.get("ticket"), bot)).rowcount
        if not n:
            raise Refused(f"ticket {args.get('ticket')} is not running for {bot}")
        return {"ok": True}

    def finish(self, caller: str, args: dict) -> dict:
        bot = _bot(self.policy, args)
        with self._tx():
            n = self.db.execute("UPDATE tickets SET state='done', finished_at=? WHERE id=? AND bot=? AND state IN ('admitted','waiting')",
                                (iso(self.clock()), args.get("ticket"), bot)).rowcount
        return {"finished": bool(n)}

    def active(self, bot: str) -> dict | None:
        self._expire_silent()
        row = self.db.execute("SELECT id, kind, risk, task FROM tickets WHERE bot=? AND state='admitted' AND kind='turn' ORDER BY admitted_at DESC LIMIT 1", (bot,)).fetchone()
        if row is None:
            return None
        return {"ticket": row[0], "kind": row[1], "risk": row[2], "task": row[3]}

    def set_task_risk(self, caller: str, args: dict) -> dict:
        """Bramble sets a task's risk class when it admits it; nobody else does.
        A task with no class is high risk, so it never reaches routes 2 or 4."""
        bot = _bot(self.policy, args)
        if not self.policy.is_router(bot):
            raise Refused("only Bramble sets a task's risk class")
        risk = args.get("risk")
        if risk not in RISKS:
            raise Refused(f"a risk class is low or high, not {risk}")
        task = str(args.get("task") or "")
        if not task:
            raise Refused("name the task")
        with self._tx():
            self.db.execute("INSERT INTO task_risk(task, risk, set_by, set_at) VALUES(?,?,?,?) "
                            "ON CONFLICT(task) DO UPDATE SET risk=excluded.risk, set_by=excluded.set_by, set_at=excluded.set_at",
                            (task, risk, bot, iso(self.clock())))
        return {"task": task, "risk": risk}

    def status(self, caller: str, args: dict) -> dict:
        self._expire_silent()
        counts = dict(self.db.execute("SELECT state, COUNT(*) FROM tickets GROUP BY state").fetchall())
        return {"stage": self.rules["stage"], "limits_ok": self.limits_ok(), "limits_at": self._meta("limits_at"),
                "tickets": counts, "routes": {r["id"]: self.route_state(r["id"]) for r in self.policy.data["routes"]}}

    # Operations: exactly once (08 section 7; attack test 20)

    @staticmethod
    def op_key(task: str, action: str, repo: str, target: str, commit: str) -> str:
        return hashlib.sha256("|".join((task, action, repo, target, commit)).encode()).hexdigest()

    def op_begin(self, caller: str, args: dict) -> dict:
        bot = _bot(self.policy, args)
        fields = {k: str(args.get(k) or "") for k in ("task", "action", "repo", "target", "commit")}
        for k in ("action", "repo", "target"):
            if not fields[k]:
                raise Refused(f"an operation names its {k}")
        key = self.op_key(fields["task"], fields["action"], fields["repo"], fields["target"], fields["commit"])
        with self._tx():
            row = self.db.execute("SELECT state, attempt, receipt FROM operations WHERE key=?", (key,)).fetchone()
            if row and row[0] == "confirmed":
                raise Refused(f"{fields['action']} on {fields['target']} was already done; receipt {row[2]}")
            if row and row[0] in ("started", "unknown"):
                return {"key": key, "state": "reconcile",
                        "reason": "an earlier attempt's outcome is unknown; read GitHub and report it with op_reconcile before anything else"}
            in_flight = self.db.execute("SELECT COUNT(*) FROM operations WHERE state='started'").fetchone()[0]
            if in_flight >= self.rules["mutating_ops"]:
                raise Refused(f"{in_flight} GitHub write already in flight; the fleet makes {self.rules['mutating_ops']} at a time")
            attempt = (row[1] + 1) if row else 1
            self.db.execute("INSERT INTO operations(key, bot, action, repo, target, commit_sha, task, state, attempt, started_at) "
                            "VALUES(?,?,?,?,?,?,?, 'started', ?, ?) ON CONFLICT(key) DO UPDATE SET state='started', attempt=excluded.attempt, "
                            "started_at=excluded.started_at, finished_at=NULL, reason=NULL",
                            (key, bot, fields["action"], fields["repo"], fields["target"], fields["commit"], fields["task"],
                             attempt, iso(self.clock())))
            self._audit(caller, "op_begin", {"key": key, "bot": bot, **fields})
        return {"key": key, "state": "go", "attempt": attempt}

    def op_finish(self, caller: str, args: dict) -> dict:
        outcome = args.get("outcome")
        if outcome not in OUTCOMES:
            raise Refused(f"an outcome is one of {', '.join(OUTCOMES)}")
        with self._tx():
            n = self.db.execute("UPDATE operations SET state=?, finished_at=?, receipt=?, reason=? WHERE key=? AND state='started'",
                                (outcome, iso(self.clock()), json.dumps(args.get("receipt")), args.get("reason"), args.get("key"))).rowcount
            if not n:
                raise Refused(f"operation {args.get('key')} is not in flight")
            self._audit(caller, "op_finish", {"key": args.get("key"), "outcome": outcome})
        return {"state": outcome}

    def op_reconcile(self, caller: str, args: dict) -> dict:
        """After reading GitHub: found means it happened (confirmed); not found
        means it did not (failed, so a new attempt may begin)."""
        found = bool(args.get("found"))
        state = "confirmed" if found else "failed"
        with self._tx():
            n = self.db.execute("UPDATE operations SET state=?, finished_at=?, receipt=?, reason=? WHERE key=? AND state IN ('started','unknown')",
                                (state, iso(self.clock()), json.dumps(args.get("receipt")),
                                 "reconciled from GitHub", args.get("key"))).rowcount
            if not n:
                raise Refused(f"operation {args.get('key')} has nothing to reconcile")
            self._audit(caller, "op_reconcile", {"key": args.get("key"), "found": found})
        return {"state": state}

    def op_get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT state, attempt, receipt, reason FROM operations WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        return {"state": row[0], "attempt": row[1], "receipt": json.loads(row[2]) if row[2] else None, "reason": row[3]}

    # Decisions (08 section 7): only Patrick's, recorded by root on the VM

    def decision_record(self, caller: str, args: dict) -> dict:
        d = Decision(**args["decision"])
        owner = self.policy.data["decisions"]["answered_by"]
        if d.answered_by != owner:
            raise Refused(f"only {owner} answers decisions; this one names {d.answered_by}")
        with self._tx():
            if self.db.execute("SELECT 1 FROM decisions WHERE id=?", (d.id,)).fetchone():
                raise Refused(f"decision {d.id} already exists")
            self.db.execute("INSERT INTO decisions(id, body) VALUES(?, ?)", (d.id, json.dumps(d.__dict__)))
            self._audit(caller, "decision_record", {"id": d.id, "kind": d.kind, "commit": d.commit})
        return {"id": d.id}

    def _decision(self, decision_id: str) -> Decision | None:
        row = self.db.execute("SELECT body FROM decisions WHERE id=?", (decision_id,)).fetchone()
        return Decision(**json.loads(row[0])) if row else None

    def decision_check(self, caller: str, args: dict) -> dict:
        v = self.check_decision(args.get("id"), kind=args["kind"], repo=args["repo"], commit=args["commit"],
                                revision=args.get("revision"), pr=args.get("pr"))
        return {"allowed": v.allowed, "reason": v.reason}

    def check_decision(self, decision_id, *, kind, repo, commit, revision=None, pr=None) -> Verdict:
        # The same rules as thornswood_guard.decisions.DecisionStore.check, on
        # the SQLite copy.
        from thornswood_guard.decisions import DecisionStore
        d = self._decision(decision_id) if decision_id else None
        tmp = DecisionStore.__new__(DecisionStore)
        tmp.policy = self.policy
        tmp._records = {d.id: d} if d else {}
        return tmp.check(decision_id, kind=kind, repo=repo, commit=commit, revision=revision, pr=pr, at=self.clock())

    def decision_use(self, caller: str, args: dict) -> dict:
        with self._tx():
            d = self._decision(args.get("id"))
            if d is None or d.used_at or d.dead:
                raise Refused(f"decision {args.get('id')} cannot be used")
            d.used_at = iso(self.clock())
            self.db.execute("UPDATE decisions SET body=? WHERE id=?", (json.dumps(d.__dict__), d.id))
            self._audit(caller, "decision_use", {"id": d.id})
        return {"used_at": d.used_at}

    def mark_restored(self, caller: str, args: dict) -> dict:
        """Run once after the store is restored from a backup: every Main,
        publish and cutover record dies, and every in-flight write is unknown."""
        killed = 0
        with self._tx():
            for (body,) in self.db.execute("SELECT body FROM decisions").fetchall():
                d = Decision(**json.loads(body))
                if d.kind in ("main", "publish", "cutover") and not d.dead:
                    d.dead = True
                    d.notes.append("dead: store restored from a backup")
                    self.db.execute("UPDATE decisions SET body=? WHERE id=?", (json.dumps(d.__dict__), d.id))
                    killed += 1
            self.db.execute("UPDATE operations SET state='unknown', reason='restored from a backup' WHERE state='started'")
            self._set_meta("restored_at", iso(self.clock()))
            self._audit(caller, "mark_restored", {"killed": killed})
        return {"killed": killed}

    # Routes (06 section 4)

    def _route_row(self, route: str):
        cycle = self._credit_cycle()
        row = self.db.execute("SELECT windows, spent_usd, cycle, refused_until FROM routes WHERE id=?", (route,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO routes(id, windows, spent_usd, cycle) VALUES(?, '{}', 0, ?)", (route, cycle))
            return {}, 0.0, cycle, None
        windows, spent, rcycle, refused = json.loads(row[0]), row[1], row[2], row[3]
        if route == "credits" and rcycle != cycle:
            spent, refused = 0.0, None
            self.db.execute("UPDATE routes SET spent_usd=0, cycle=?, refused_until=NULL WHERE id=?", (cycle, route))
        return windows, spent, cycle, refused

    def _credit_cycle(self) -> str:
        """The date the current credit month began: the most recent refresh day."""
        day = min(next(r for r in self.policy.data["routes"] if r["id"] == "credits")["refresh_day"], 28)
        t = self.clock()
        if t.day >= day:
            start = t.replace(day=day)
        else:
            year, month = (t.year, t.month - 1) if t.month > 1 else (t.year - 1, 12)
            start = t.replace(year=year, month=month, day=day)
        return start.date().isoformat()

    def _next_credit_date(self) -> datetime:
        start = datetime.fromisoformat(self._credit_cycle()).replace(tzinfo=timezone.utc)
        year, month = (start.year, start.month + 1) if start.month < 12 else (start.year + 1, 1)
        return start.replace(year=year, month=month)

    def route_state(self, route: str) -> dict:
        with self._tx():
            windows, spent, cycle, refused = self._route_row(route)
        t = self.clock()
        closed = {name: until for name, until in windows.items()
                  if not name.startswith("backoff:") and parse(until) > t}
        if refused and parse(refused) > t:
            closed["credit"] = refused
        out = {"open": not closed, "closed": closed}
        if route == "credits":
            v = credits_open(self.policy, spent)
            out.update({"spent_usd": round(spent, 4), "cycle": cycle, "credit_margin_ok": v.allowed})
            if not v.allowed:
                out["open"] = False
        return out

    def route_limit(self, caller: str, args: dict) -> dict:
        """A limit error: close that window until its reset. With no reset
        reported, back off 60 seconds doubling to 4 hours. The route opens only
        when every closed window has reset (06 section 4.1)."""
        route = args.get("route")
        if route not in {r["id"] for r in self.policy.data["routes"]}:
            raise Refused(f"{route} is not one of the four routes")
        window = str(args.get("window") or "default")
        if window.startswith("backoff:"):
            raise Refused("that is not a window name")
        with self._tx():
            windows, _, _, _ = self._route_row(route)
            reset = args.get("reset_at")
            if reset:
                until = parse(reset)
            else:
                b = self.rules["route_backoff_seconds"]
                prev = windows.get(f"backoff:{window}")
                step = min(int(prev) * 2, b["max"]) if prev else b["first"]
                windows[f"backoff:{window}"] = str(step)
                until = self.clock() + timedelta(seconds=step)
            windows[window] = iso(until)
            self.db.execute("UPDATE routes SET windows=? WHERE id=?", (json.dumps(windows), route))
            self._audit(caller, "route_limit", {"route": route, "window": window, "until": iso(until)})
        return self.route_state(route)

    def route_ok(self, caller: str, args: dict) -> dict:
        """A turn on this route answered: its backoff starts over."""
        route = args.get("route")
        with self._tx():
            windows, _, _, _ = self._route_row(route)
            windows = {k: v for k, v in windows.items() if not k.startswith("backoff:")}
            self.db.execute("UPDATE routes SET windows=? WHERE id=?", (json.dumps(windows), route))
        return self.route_state(route)

    def credit_spend(self, caller: str, args: dict) -> dict:
        usd = float(args.get("usd") or 0)
        if usd < 0:
            raise Refused("spend is never negative")
        with self._tx():
            self._route_row("credits")
            self.db.execute("UPDATE routes SET spent_usd = spent_usd + ? WHERE id='credits'", (usd,))
        return self.route_state("credits")

    def credit_refused(self, caller: str, args: dict) -> dict:
        """Anthropic refused for lack of credit: route 2 shuts until the next
        credit date. Route 1 is untouched (the two limits never share a counter)."""
        with self._tx():
            self._route_row("credits")
            nxt = self._next_credit_date()
            self.db.execute("UPDATE routes SET refused_until=? WHERE id='credits'", (iso(nxt),))
        return self.route_state("credits")

    def route_admit(self, caller: str, args: dict) -> dict:
        """The admission proxy's question: may this Bot's current turn use this
        route now? Bot from the proxy's token map, never from the request body."""
        bot = _bot(self.policy, args)
        route = args.get("route")
        if route not in ("credits", "qwen"):
            raise Refused(f"the proxy carries routes 2 and 4 only, not {route}")
        turn = self.active(bot)
        if turn is None:
            raise Refused(f"{bot} has no admitted turn; a turn with no ticket never reaches a route")
        v = route_allowed(self.policy, bot, route, turn["risk"])
        if not v.allowed:
            raise Refused(v.reason)
        state = self.route_state(route)
        if not state["open"]:
            # Closed is a wait, not a refusal: the proxy answers it as a limit
            # with the time the route opens again (every window reset).
            until = max(state["closed"].values()) if state["closed"] else None
            if until is None and route == "credits":
                until = iso(self._next_credit_date())
            return {"admitted": False, "wait": True, "until": until,
                    "reason": f"route {route} is closed until {until}"}
        return {"admitted": True, "ticket": turn["ticket"], "risk": turn["risk"]}

    # Leases, with fencing generations

    def lease_acquire(self, caller: str, args: dict) -> dict:
        name, holder = str(args.get("name") or ""), str(args.get("holder") or "")
        ttl = int(args.get("ttl_seconds") or 600)
        if not name or not holder:
            raise Refused("a lease names itself and its holder")
        with self._tx():
            t = self.clock()
            row = self.db.execute("SELECT holder, expires_at FROM leases WHERE name=?", (name,)).fetchone()
            if row and parse(row[1]) > t and row[0] != holder:
                raise Refused(f"{name} is held by {row[0]} until {row[1]}")
            gen_row = self.db.execute("SELECT generation FROM lease_generations WHERE name=?", (name,)).fetchone()
            gen = (gen_row[0] if gen_row else 0) + 1
            self.db.execute("INSERT INTO lease_generations(name, generation) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET generation=excluded.generation", (name, gen))
            self.db.execute("INSERT INTO leases(name, holder, generation, acquired_at, expires_at) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(name) DO UPDATE SET holder=excluded.holder, generation=excluded.generation, "
                            "acquired_at=excluded.acquired_at, expires_at=excluded.expires_at",
                            (name, holder, gen, iso(t), iso(t + timedelta(seconds=ttl))))
        return {"name": name, "generation": gen}

    def lease_release(self, caller: str, args: dict) -> dict:
        with self._tx():
            n = self.db.execute("DELETE FROM leases WHERE name=? AND holder=? AND generation=?",
                                (args.get("name"), args.get("holder"), args.get("generation"))).rowcount
        return {"released": bool(n)}

    # The Dev ledger (04 section 6.6)

    def ledger_event(self, caller: str, args: dict) -> dict:
        repo, issue, event = args.get("repo"), args.get("issue"), args.get("event")
        if event not in LEDGER_EVENTS:
            raise Refused(f"{event} is not a ledger event")
        if not isinstance(issue, int) or issue <= 0 or not repo:
            raise Refused("a ledger event names the repo and the issue number")
        with self._tx():
            try:
                self.db.execute("INSERT INTO ledger(repo, issue, event, release, at, note) VALUES(?,?,?,?,?,?)",
                                (repo, issue, event, str(args.get("release") or ""), iso(self.clock()), str(args.get("note") or "")))
                added = True
            except sqlite3.IntegrityError:
                added = False  # a duplicate webhook or a follow-up PR never counts twice
            self._audit(caller, "ledger_event", {"repo": repo, "issue": issue, "event": event, "added": added})
        return {"added": added, "pending": self.ledger_pending()}

    def ledger_pending(self) -> list[dict]:
        """Distinct issues eligible and not yet used by a verified Dev release,
        reverted or superseded."""
        rows = self.db.execute("SELECT repo, issue, event FROM ledger ORDER BY id").fetchall()
        state: dict[tuple, set] = {}
        for repo, issue, event in rows:
            state.setdefault((repo, issue), set()).add(event)
        out = []
        for (repo, issue), events in state.items():
            if "eligible" in events and not events & {"Dev_verified", "counter_consumed", "reverted", "superseded"}:
                out.append({"repo": repo, "issue": issue})
        return out

    # The socket's ops: (handler, who may call it)

    def ops(self) -> dict:
        u = self.policy.data["brokers"]["users"]
        gw, git, files, proxy = u["gateway"], u["git"], u["files"], u["proxy"]
        return {
            "admit": (self.admit, {gw, files}),
            "touch": (self.touch, {gw, files}),
            "finish": (self.finish, {gw, files}),
            "set_task_risk": (self.set_task_risk, {gw}),
            "status": (self.status, {gw, git, files, proxy, "root"}),
            "op_begin": (self.op_begin, {git}),
            "op_finish": (self.op_finish, {git}),
            "op_reconcile": (self.op_reconcile, {git}),
            "decision_check": (self.decision_check, {git}),
            "decision_use": (self.decision_use, {git}),
            "decision_record": (self.decision_record, {"root"}),
            "mark_restored": (self.mark_restored, {"root"}),
            "limits_record": (self.limits_record, {"root"}),
            "route_limit": (self.route_limit, {gw, proxy}),
            "route_ok": (self.route_ok, {gw, proxy}),
            "route_admit": (self.route_admit, {proxy}),
            "credit_spend": (self.credit_spend, {proxy}),
            "credit_refused": (self.credit_refused, {proxy}),
            "lease_acquire": (self.lease_acquire, {proxy, git, files}),
            "lease_release": (self.lease_release, {proxy, git, files}),
            "ledger_event": (self.ledger_event, {git}),
            "ledger_pending": (lambda c, a: self.ledger_pending(), {gw, git, "root"}),
        }


class _Tx:
    def __init__(self, svc: PolicyService):
        self.svc = svc

    def __enter__(self):
        self.svc.lock.acquire()
        self.svc.db.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.svc.db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.svc.lock.release()
        return False


def _bot(policy: Policy, args: dict) -> str:
    bot = args.get("bot")
    if not isinstance(bot, str) or not policy.is_bot(bot):
        raise Refused(f"{bot!r} is not one of the six Bots")
    return bot
