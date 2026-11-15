"""One room message, or one readiness check, through the Bots' own gateway.

The connector runs this with Hermes's own Python (the interpreter the `hermes`
command starts), as the Bots' `hermes` user, so it can use Hermes's client the
way Desktop does (contracts.json "room"):

    <hermes python> -I room_send.py < {"op": "send", "room_id", "event_id", "text", "thread_id"?,
                                       "files": [{"id", "name", "mime", "data_base64"}]?}
    <hermes python> -I room_send.py < {"op": "ready", "profile": "<bot or empty>", "name": "<optional>"}
    <hermes python> -I room_send.py < {"op": "states", "profiles": ["<bot>", ...]}
    <hermes python> -I room_send.py < {"op": "react", "profile": "<bot or empty>", "session_id",
                                       "message_id", "emoji": "<one of the phone's, or null>"}
    <hermes python> -I room_send.py < {"op": "control", "profile": "<bot or empty>", "session_id",
                                       "allowed": [[provider, model], ...] (a Bot's),
                                       "set": {"model": {...}, "reasoning"}?}

own_connector.py asks it the same readiness question for Patrick's own Hermes
(profile empty, as that Hermes's own user), naming that gateway in `name`.

It finds the gateway that is already running and never starts one
(`discover_gateway_endpoint`, "preserving uncertainty instead of spawning").
It reads the request on stdin, so no message is on a command line, and prints
one JSON line:

    {"sent": true, "seq": n}             the room took it;
    {"sent": false, "ready": false, "reason": "..."}
                                         it was not sent because the gateway
                                         is not ready (not running, or not
                                         taking connections yet); the message
                                         waits for it;
    {"sent": false, "reason": "..."}     it was not sent (the room's driver was
                                         not running, or the gateway refused it);
    {"sent": null, "reason": "..."}      it may have been sent.

A turn in a chat of Patrick's own Hermes (own_connector.py), with `hermes chat`'s own arguments in place
of the request on stdin:

    <hermes python> -I room_send.py chat -Q --format stream-json --query-file <file> [--resume <id> ...]

runs Hermes's own `hermes chat` unchanged, through the same local gateway connection Desktop uses (a
ticket from the gateway's own control socket on this machine; no new secret, nothing listens), except how
a message is handed to a chat whose turn is already running: by the chat's busy-input setting, the way
Desktop's message box does (Patrick's answer A on q-one-busy-chat, 2 Nov; apps/desktop
use-composer-submit.ts submitDraft and steerDraft, busy-submit-state.ts, use-busy-input-mode.ts;
contracts.json "busy"):

- the setting is the gateway's own answer to `config.get busy` for that chat: the chat's own setting,
  else Hermes's `display.busy_input_mode`, else Hermes's default;
- steer: `session.steer`, so the running turn takes the words at its next step;
- interrupt: `session.redirect`, so the running turn stops and goes on with these words;
- queue: `prompt.submit`, so it runs next, and is shown queued at once;
- a photo or file, a slash command, or a turn stopped at an approval or a question always queues
  (Desktop: attachments cannot ride a steer, and a steer cannot reach a turn parked on a prompt);
- a steer or redirect the gateway answers "rejected" (the turn ended meanwhile) is queued, the wire
  contract's "not corrected, queue it as the next prompt", as Desktop does, so nothing is lost.

`attended-chat` in place of `chat` runs the same turn as one Patrick is there to answer (9 Nov: the switch
from Desktop to the app). `hermes chat -Q` marks its turn finite, and Hermes takes a finite turn as one
nobody can answer: its question tool (clarify) answers itself "no user available, make the most
reasonable assumption", and an approval follows approvals.single_query_mode, so a flagged command is
refused (gateway/session_finite.py finite_clarify_reply; tools/approval_context.py
_is_single_query_approval_context). Desktop's turns are not finite: Hermes asks, and Desktop shows the
question or the approval for him to answer. So an attended turn:

- goes in without `finite` on prompt.submit (the view still waits for its own turn's end and exits, as a
  one-shot does);
- keeps waiting while a question or an approval is open, where a one-shot would detach ("Input
  required");
- says each one Hermes opens, and each one settled, as a line: {"type": "ask", "prompt_id", "kind",
  "question" or "description" and "command", "choices", "multi_select"} and {"type": "ask_settled",
  "prompt_id"}, the fields Hermes's own prompt events carry (gateway/session_pending_controls.py);
- reads orders from its stdin, one JSON object a line, and carries each out on its own gateway connection
  with the calls `hermes chat`'s own /stop, /approve and /answer make (hermes_cli/gateway_chat_view.py
  command): {"op": "stop"} is session.interrupt; {"op": "answer", "prompt_id", "value"} is
  approval.respond with that choice, or clarify.respond with that answer. It says how each went:
  {"type": "order", "op", "prompt_id"?, "status": "sent" | "gone" | "refused", "reason"?}. Hermes itself
  refuses an answer to a prompt that is no longer open, or a choice it did not offer.

Hermes's own fd 0 is the null device in an attended turn, so nothing in Hermes ever reads an order.

A steered or redirected message has no turn of its own: the running turn answers it, so this follows
that turn to its end and reports its result, as `hermes chat` reports its own. Besides Hermes's
stream-json lines it prints how Hermes took the message, once it has, and again when a queued one starts:
{"type": "admission", "status": "started" | "queued" | "steered" | "redirected", "session_id"}, the
session_id the chat's id (for a new chat, the one Hermes just made). Before this, a message
always went in with prompt.submit and waited behind the running turn, however the chat was set. This is
in room_send.py, not a file of its own, because One's connector updates itself with the files its
running copy names (own_connector.py UPDATE_FILES).

`groups.send` with the same `event_id` is the same event (hosted_rooms.py:250),
which is how the core settles an unclear send from the room feed afterwards.
A message's files (#81) go first into the room's own store, each as Desktop's +
uploads it (`groups.attachment.upload`, gateway/session_hosted_attachments.py
store_upload), and `groups.send` names them in its payload (`attachments`, the
manifest hosted_room_discussion.py validate_user_payload takes); the room's
driver then hands each Bot the photos as its turn's images and every other
file as a path line, as it does a file Desktop sent (bound_submission_payload).
A file the store refuses is said as the reason, and nothing is sent.

In a turn, `--tw-files` says the message carries files (so it is never steered
into a running turn), and each `--tw-image <path>` is a photo Patrick attached:
it is copied into Hermes's image staging folder and named on the turn's
`prompt.submit` (`attachments`), the way Desktop sends one. Hermes's own
`--image` is refused by its gateway chat (hermes_cli/gateway_chat.py
_UNSUPPORTED), so these two are taken out before Hermes reads the rest.

`react` sets Patrick's reaction on one message of a chat to exactly the emoji
asked for, or takes it off (null), with Hermes's own SessionDB, the way
Hermes's own reaction tool and Desktop do (contracts.json "reactions"). Hermes
keeps one reaction per person per message, and the same emoji again would take
it off, so this reads the reaction first and writes only when it differs:
asking twice is the same as asking once. It prints {"set": true}, or
{"set": false, "reason": "..."} when nothing changed. It needs no gateway.

`session` renames, hides (archive), brings back (unarchive) or deletes one of
Patrick's own chats (profile empty only) with Hermes's own SessionDB, the calls
`hermes sessions rename`, Hermes's session.archive, Desktop's pin and
`hermes sessions delete` make (contracts.json "sessions"):

    <hermes python> -I room_send.py < {"op": "session", "action": "rename" | "archive" | "unarchive" |
                                       "pin" | "unpin" | "read" | "unread" | "delete", "session_id",
                                       "title": "<rename only>"}

Each ends in the same state however often it runs: a chat that already has the
title, is already hidden or shown, pinned or not, or is already gone is done. It prints
{"done": true}, or {"done": false, "reason": "..."} with Hermes's own reason
(a title another chat holds, a turn running or unfinished). His Bot Chat (the
hidden chat titled exactly "Bot Chat", with every part Hermes compressed it
into) is never changed. It needs no gateway.

`tasks` reads, for the app's Tasks tab, what each named Hermes has going besides
its chats (profile "" is the user's own Hermes), the way Hermes Desktop reads it
(contracts.json "tasks"):

    <hermes python> -I room_send.py < {"op": "tasks", "profiles": ["<bot or empty>", ...]}

- subagents and background commands, from that profile's running gateway, never
  started: `session.active_list` for its live chats, then `subagent.list` and
  `process.list` for each (gateway/session_active_list.py, session_ancillary.py
  read: Desktop's status stack polls the same three); a gateway that is not
  running has none, and says its state;
- scheduled jobs, with Hermes's own `cron.jobs.list_jobs` under that profile's
  home, the reader Desktop's Scheduled jobs page gets from Hermes's dashboard
  (hermes_cli/web_server_cron.py _call_cron_for_profile and _cron_store_scope);
  a job whose latest run is claimed or running is running now (cron/executions.py).

It prints {"tasks": {"<profile>": {"state", "subagents", "processes", "jobs", ...}}}.
A part it could not read is null, with its reason beside it, never an empty list.
Commands and errors are the ones Hermes already redacts for Desktop, cut short.

`tail` reads one subagent's transcript so far, for the app's Tasks tab (profile ""
only; own_tails.py): Hermes's own `subagent.tail` for that chat and subagent, the
call Desktop's status stack polls every 2 seconds while a subagent is open
(gateway/session_ancillary.py, apps/desktop .../status-stack/subagent-transcript.tsx).
It prints {"available", "text", "truncated"}, or why it could not read it.

`job` pauses, resumes, runs now or removes one of his own Hermes's scheduled
jobs (profile "" only; a Bot's jobs are not changed from the app), with Hermes's
own cron.jobs calls under its home, the ones Hermes's dashboard makes for
Desktop's Scheduled jobs page (contracts.json "jobs"):

    <hermes python> -I room_send.py < {"op": "job", "job": "<job id>",
                                       "do": "pause" | "resume" | "run" | "remove"}

Run now is Hermes's own `trigger_job`: the job is due at once, and the running
gateway's own scheduler fires it at its next tick, with its live connections,
as `hermes cron run` does when a ticker is running. A paused job that is run
now is resumed too, as Desktop's Run now resumes it. It prints {"done": true},
or {"done": false, "reason": "..."} with Hermes's own reason. It needs no
gateway.

`search` finds his own Hermes's chats by the words in their messages, with
Hermes's own full-text search, the way Hermes Desktop's chat search does
(hermes_cli/web_routers/sessions.py search_sessions):

    <hermes python> -I room_send.py < {"op": "search", "q": "<words>", "limit": <1..50>,
                                       "exclude": ["<source>", ...]}

A chat id first (`search_sessions_by_id`), then the words of every message
(`search_messages`, each word also as the start of a longer one: "nimb" finds
"nimbus"), then titles (`list_sessions_rich` search_query). A chat Hermes
compressed is named by the chat it goes on as. It prints {"found": [{"id",
"snippet"}]}, at most `limit`, the snippet the matched words in their message,
or {"found": null, "reason": "..."} when Hermes's store could not answer. It
reads only, and needs no gateway.

`states` asks the same question of each Bot at once, for the phone's online
dots: Hermes's own runtime state of each one's gateway (absent, starting,
ready, draining, incompatible, inaccessible or conflict, gateway/runtime_contract.py),
found the same way and never started. It prints {"states": {"<bot>": "<state>"}},
with "check_failed" for a Bot whose check itself raised.

When Hermes is not installed into the Python that runs this (Hermes's own
launcher keeps its source in a folder it puts first on the path itself), the
connector names that folder, and this puts it first on the path, and runs
Hermes's own bootstrap from it (which puts Hermes's dependencies on the path),
before it imports any of Hermes:

    <hermes python> -I room_send.py --hermes-root <Hermes's source folder> < {...}

Run inside Hermes's own bootstrap (`hermes --print-runtime-command`) it needs
no folder: the bootstrap has put it there already.

A retry or an edit (TWHermesAgents #80) is a turn that first takes the chat back to before one of his turns,
as `hermes chat`'s /retry does (hermes_cli/gateway_chat_commands.py _retry and _rewind; contracts.json
"rewind"):

    <hermes python> -I room_send.py [attended-]chat --tw-rewind retry|edit <message id> <hermes chat's arguments>

- before its message goes in, the view asks Hermes's session.mutate rewind to that turn, fenced to the chat's
  revision and generation by Hermes's own PreparedMutations, with the target read from the same snapshot:
  refused with nothing changed when that message is no longer one of his turns there, or, for a retry, a turn
  Hermes cannot send again as words (/retry's own check, retryable_user_text), or while a turn runs there;
- an edit then sends the words in the query file; a retry sends that turn's own words as Hermes kept them, and
  the query file holds only the words the phone showed (`hermes chat` takes no empty query), never sent;
- it says how the rewind went, once: {"type": "rewind", "status": "done", "count": n} or
  {"type": "rewind", "status": "refused", "reason": "..."}, and a refused one ends the turn with nothing sent.

`rewind` undoes from one of his turns, or branches a new chat through one message, of a chat of his own Hermes
(profile empty) or of a Bot's, with Hermes's session.mutate on that profile's running gateway, the calls
/undo and /branch make (contracts.json "rewind"):

    <hermes python> -I room_send.py < {"op": "rewind", "profile": "<bot or empty>", "action": "undo" | "branch",
                                       "session_id", "message_id"}

It prints {"done": true} (an undo), {"done": true, "session_id": "<the new chat>"} (a branch),
{"done": false, "ready": false, "reason"} when the gateway is not running (nothing was done), or
{"done": false, "reason"} with Hermes's own reason. An undo names one of his turns, as /undo does; a branch
keeps every message up to and with the one named.

It imports nothing from this repository.
"""

import asyncio
import json
import re
import sys

PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _found(profile: str):
    from pathlib import Path

    from hermes_constants import get_default_hermes_root
    from hermes_cli.gateway_runtime import attachable, discover_gateway_endpoint, gateway_profile_home

    home = Path(get_default_hermes_root())
    if profile:
        home = home / "profiles" / profile
    found = discover_gateway_endpoint(gateway_profile_home(home))
    return (found.endpoint if attachable(found) else None), found.state


TASK_CHATS = 40      # the live chats whose subagents and background commands it reads, the newest first
TASK_ROWS = 30       # at most this many of each kind for one Hermes
TASK_SECONDS = 10    # one Hermes's gateway answers its part in this, or that part says it did not
UPCOMING = 14        # a job's next runs, enough for the app to say its pattern in words (tasks.py "upcoming")


def _home(profile: str):
    from pathlib import Path

    from hermes_constants import get_default_hermes_root

    home = Path(get_default_hermes_root())
    return home / "profiles" / profile if profile else home


def _cut(value, n: int):
    return None if value is None else " ".join(str(value).split())[:n]


def _jobs(profile: str) -> list:
    """That profile's scheduled jobs, paused ones too, as Hermes lists them for Desktop."""
    from agent.redact import redact_sensitive_text
    from cron import jobs as cron_jobs
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = _home(profile)
    token = set_hermes_home_override(str(home))
    try:
        with cron_jobs.use_cron_store(home):
            rows = cron_jobs.list_jobs(include_disabled=True)[:TASK_ROWS]
            # In that home too: a job's times follow its own zone, else that profile's (hermes_time).
            upcoming = [_upcoming(cron_jobs, j) for j in rows]
    finally:
        reset_hermes_home_override(token)
    out = []
    for j, (runs, left) in zip(rows, upcoming):
        run = j.get("latest_execution") if isinstance(j.get("latest_execution"), dict) else {}
        err = j.get("last_error")
        out.append({"id": _cut(j.get("id"), 64), "name": _cut(j.get("name"), 120),
                    "schedule": _cut(j.get("schedule_display"), 80), "state": _cut(j.get("state"), 20),
                    "next": _cut(j.get("next_run_at"), 40), "last": _cut(j.get("last_run_at"), 40),
                    "last_status": _cut(j.get("last_status"), 30),
                    # _format_job's own redaction (tools/cronjob_job_args.py)
                    "last_error": _cut(redact_sensitive_text(err, force=True, redact_url_credentials=True), 200)
                    if err else None,
                    "running": run.get("status") in ("claimed", "running"),
                    "upcoming": [_cut(t, 40) for t in runs], "left": left})
    return out


def _upcoming(cron_jobs, j: dict) -> tuple[list, int | None]:
    """A job's next runs as instants, the first its next_run_at and each after it from Hermes's own
    compute_next_run in the job's zone, as the scheduler advances it; and how many runs it has left when its
    repeat count ends it (None while it repeats for ever). The app says the schedule in words in Patrick's
    own time from these (Patrick, 11 Nov 8:25 PM: the Tasks tab showed the raw "20 11 2 11 *"); the cron
    line alone is in the zone Hermes keeps, which the app does not know. A finished job has none."""
    rep = j.get("repeat") if isinstance(j.get("repeat"), dict) else {}
    times, done = rep.get("times"), rep.get("completed") or 0
    finite = isinstance(times, int) and not isinstance(times, bool) and times > 0
    left = max(0, times - done) if finite and isinstance(done, int) else None
    first = j.get("next_run_at")
    if j.get("state") == "completed" or not isinstance(first, str) or not first or left == 0:
        return [], left
    runs = [first]
    while len(runs) < min(UPCOMING, left if left is not None else UPCOMING):
        later = cron_jobs.compute_next_run(j.get("schedule"), runs[-1], timezone=j.get("timezone"))
        if not isinstance(later, str) or later == runs[-1]:
            break
        runs.append(later)
    return runs, left


async def _live(endpoint) -> dict:
    """The subagents and background commands of the gateway's live chats."""
    from hermes_cli.gateway_client import GatewayRPCError, connect_gateway

    subs, procs, missed = [], [], []
    async with connect_gateway(endpoint=endpoint) as client:
        chats = (await client.rpc("session.active_list") or {}).get("sessions") or []
        chats = sorted((c for c in chats if isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"]),
                       key=lambda c: float(c.get("started_at") or 0), reverse=True)[:TASK_CHATS]
        for c in chats:
            chat, title = c["id"][:128], _cut(c.get("title"), 120) or ""
            try:
                got_s = await client.rpc("subagent.list", session_id=c["id"]) or {}
                got_p = await client.rpc("process.list", session_id=c["id"]) or {}
            except GatewayRPCError as exc:
                if str(exc) != "not_found":  # not_found: the chat closed after it was listed
                    missed.append(str(exc))
                continue
            for r in got_s.get("subagents") or []:
                if isinstance(r, dict):
                    subs.append({"id": _cut(r.get("subagent_id"), 64), "chat": chat, "chat_title": title,
                                 "goal": _cut(r.get("goal"), 300), "model": _cut(r.get("model"), 80),
                                 "status": _cut(r.get("status"), 20), "depth": r.get("depth"),
                                 "background": bool(r.get("delegation_id")), "tools": r.get("tool_count"),
                                 "last_tool": _cut(r.get("last_tool"), 60), "started": r.get("started_at")})
            for p in got_p.get("processes") or []:
                if isinstance(p, dict):
                    procs.append({"id": _cut(p.get("session_id"), 64), "chat": chat, "chat_title": title,
                                  "command": _cut(p.get("command"), 200), "status": _cut(p.get("status"), 20),
                                  "exit_code": p.get("exit_code"), "started": p.get("started_epoch"),
                                  "ended": p.get("exited_at"), "why": _cut(p.get("completion_reason"), 40)})
    out = {"subagents": subs[:TASK_ROWS], "processes": procs[:TASK_ROWS]}
    if missed:
        out["live_why"] = f"{len(missed)} of its chats did not answer ({missed[0][:40]})"
    return out


def _tasks(profile: str) -> dict:
    out: dict = {}
    try:
        out["jobs"] = _jobs(profile)
    except Exception as exc:  # its store could not be read: said, never an empty list
        out["jobs"], out["jobs_why"] = None, f"Hermes could not read its scheduled jobs ({type(exc).__name__})"
    try:
        endpoint, state = _found(profile)
    except Exception as exc:
        endpoint, state = None, "check_failed"
        out["live_why"] = f"could not find its gateway ({type(exc).__name__})"
    out["state"] = str(state)
    if endpoint is None:
        out["subagents"] = out["processes"] = None
        out.setdefault("live_why", f"its gateway is {state}")
        return out
    from hermes_cli.gateway_client import GatewayClientError

    try:
        out.update(asyncio.run(asyncio.wait_for(_live(endpoint), TASK_SECONDS)))
    except (GatewayClientError, TimeoutError, asyncio.TimeoutError, OSError) as exc:
        out["subagents"] = out["processes"] = None
        out["live_why"] = (f"its gateway did not answer in {TASK_SECONDS} seconds"
                           if isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                           else f"could not read its gateway ({type(exc).__name__}: {str(exc)[:60]})")
    return out


JOB_DO = ("pause", "resume", "run", "remove")
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


def _job(req: dict) -> dict:
    """One change to one of his own Hermes's scheduled jobs (contracts.json "jobs")."""
    do, jid = req.get("do"), req.get("job")
    if do not in JOB_DO or not isinstance(jid, str) or not JOB_ID.match(jid):
        return {"done": False, "reason": "bad request"}
    from cron import jobs as cron_jobs
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = _home("")
    token = set_hermes_home_override(str(home))
    try:
        with cron_jobs.use_cron_store(home):
            # By its id only, never a name that another job could share.
            job = cron_jobs.get_job(jid)
            if not job:
                return {"done": False, "reason": "Hermes has no scheduled job with that id now"}
            try:
                got = {"pause": cron_jobs.pause_job, "resume": cron_jobs.resume_job, "run": cron_jobs.trigger_job,
                       "remove": cron_jobs.remove_job}[do](job["id"])
            except ValueError as exc:  # Hermes's own refusal: a one-shot past its time, a finished job
                return {"done": False, "reason": " ".join(str(exc).split())[:200]}
            if not got:
                return {"done": False, "reason": "Hermes has no scheduled job with that id now"}
            # As Hermes's cronjob tool does after each change (tools/cronjob_tools.py
            # _notify_provider_jobs_changed_safe): an outside scheduler reconciles, the built-in one reads the
            # store again at its next tick. The change is in already, so this never undoes it.
            try:
                from cron.scheduler import _notify_provider_jobs_changed
                _notify_provider_jobs_changed()
            except Exception:
                pass
    finally:
        reset_hermes_home_override(token)
    return {"done": True}


CONTROL_SECONDS = 60  # one read, or one change and its read back, answers in this
SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")  # own.py SESSION_ID: a chat id Hermes gives
SUBAGENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
TAIL_SECONDS = 20     # one transcript read answers in this
TAIL_TEXT = 16384     # Desktop shows the newest 16384 characters


async def _tail_read(endpoint, sid: str, sub: str) -> dict:
    from hermes_cli.gateway_client import connect_gateway

    async with connect_gateway(endpoint=endpoint) as client:
        got = await client.rpc("subagent.tail", session_id=sid, subagent_id=sub) or {}
    text = got.get("text") if isinstance(got.get("text"), str) else ""
    return {"available": got.get("available") is True, "text": text[-TAIL_TEXT:],
            "truncated": got.get("truncated") is True or len(text) > TAIL_TEXT}


def _tail(req: dict) -> dict:
    from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError

    sid, sub = req.get("session_id"), req.get("subagent_id")
    if not (isinstance(sid, str) and SESSION.match(sid) and isinstance(sub, str) and SUBAGENT.match(sub)):
        return {"available": False, "reason": "bad request"}
    endpoint, state = _found("")
    if endpoint is None:
        name = req.get("name") if isinstance(req.get("name"), str) else "Hermes"
        return {"available": False, "reason": f"{name[:40]}'s gateway is {state}"}
    try:
        return asyncio.run(asyncio.wait_for(_tail_read(endpoint, sid, sub), TAIL_SECONDS))
    except (TimeoutError, asyncio.TimeoutError):
        return {"available": False, "reason": f"Hermes did not answer in {TAIL_SECONDS} seconds"}
    except GatewayRPCError as exc:
        # not_found: the chat closed, or the subagent is gone from it.
        return {"available": False, "reason": "that subagent has ended" if str(exc) == "not_found"
                else f"Hermes refused it ({str(exc)[:120]})"}
    except GatewayClientError as exc:
        return {"available": False, "reason": f"Hermes refused it ({str(exc)[:120]})"}
    except OSError as exc:
        return {"available": False, "reason": f"could not reach the gateway ({type(exc).__name__})"}
# Sign-ins that cost nothing per message (Hermes's registry, hermes_cli/auth.py PROVIDER_REGISTRY): ChatGPT
# (openai-codex), Grok's and Qwen's own logins and MiniMax's. Nous Portal is a sign-in too, but it spends
# the plan's credits, so only its free models are offered. Anthropic is a sign-in only when its route
# carries his Claude login (agent/anthropic_credentials.py anthropic_route_is_oauth); an API key there pays.
SIGN_IN_AUTH = ("oauth_external", "oauth_minimax")


def _signed_in(slug: str) -> bool:
    from hermes_cli.auth import PROVIDER_REGISTRY

    if slug == "anthropic":
        from agent.anthropic_credentials import anthropic_route_is_oauth
        from hermes_cli.runtime_provider import resolve_runtime_provider

        try:
            route = resolve_runtime_provider(requested="anthropic")
        except Exception:  # no Anthropic credential at all: nothing of it is offered
            return False
        # The credential is only tested for its kind here; it is never printed or kept.
        return anthropic_route_is_oauth(route.get("base_url"), route.get("api_key"), provider="anthropic")
    cfg = PROVIDER_REGISTRY.get(slug)
    return cfg is not None and cfg.auth_type in SIGN_IN_AUTH


def _offered(options: dict) -> list:
    """The models of Hermes's own picker (model.options) that cost nothing per message: covered by one of
    his sign-ins, free in Hermes's own price list, or local (plan Part 6, decision 4 A)."""
    out, signed = [], {}
    for row in (options or {}).get("providers") or []:
        if not isinstance(row, dict) or row.get("authenticated") is False or not isinstance(row.get("slug"), str):
            continue
        slug, pricing = row["slug"], row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
        locked = set(row.get("unavailable_models") or [])
        for model in row.get("models") or []:
            if not isinstance(model, str) or model in locked:
                continue
            price = pricing.get(model) if isinstance(pricing.get(model), dict) else {}
            if row.get("auth_type") == "local" or row.get("source") == "local-runtime":
                why = "local"
            elif model.endswith(":free") or price.get("free") is True:
                why = "free"
            else:
                if slug not in signed:
                    signed[slug] = _signed_in(slug)
                why = "subscription" if signed[slug] else None
            if why:
                out.append({"provider": slug, "model": model, "name": _cut(row.get("name"), 40) or slug, "why": why})
    return out[:80]


def _bot_offered(options: dict, allowed: list) -> list:
    """For a Bot's chat: the models of Hermes's own picker that are in the Bot's own route list as the
    connector sent it (connector.bot_models: its routes it reaches with its own sign-in), and nothing else."""
    pairs = {(p[0], p[1]) for p in allowed if isinstance(p, list) and len(p) == 2}
    out = []
    for row in (options or {}).get("providers") or []:
        if not isinstance(row, dict) or row.get("authenticated") is False or not isinstance(row.get("slug"), str):
            continue
        locked = set(row.get("unavailable_models") or [])
        for model in row.get("models") or []:
            if isinstance(model, str) and model not in locked and (row["slug"], model) in pairs:
                out.append({"provider": row["slug"], "model": model, "name": _cut(row.get("name"), 40) or row["slug"],
                            "why": "subscription"})
    return out


def _chat_facts(sid: str) -> dict:
    """The chat's size and start, from Hermes's own SessionDB row (message_count, started_at), as Desktop's
    session list shows them; nothing when Hermes has no row for it."""
    from hermes_state_registry import acquire, release_or_close

    db = acquire(None)
    try:
        row = db.get_session(sid) or {}
    finally:
        release_or_close(db)
    return {"messages": row.get("message_count"), "started": row.get("started_at")}


async def _control_read(client, sid: str, allowed: list | None = None) -> dict:
    """What Hermes holds for one chat, read the way Desktop's details and pickers read it. The model is the
    one the chat's agent is on now: when Hermes has moved it to a fallback, session.usage says so in its own
    words (fallback_model, gateway/session_usage.py) and names the fallback's model and provider, so the
    pane never shows the main model while a fallback answers. `allowed`: a Bot's own models (_bot_offered)."""
    from agent.reasoning_effort import route_supported_efforts

    info = await client.rpc("session.info", session_id=sid) or {}
    reasoning = (await client.rpc("config.get", session_id=sid, key="reasoning") or {}).get("value")
    busy = (await client.rpc("config.get", session_id=sid, key="busy") or {}).get("value")
    yolo = (await client.rpc("config.get", session_id=sid, key="yolo") or {}).get("value")
    # The approval mode is the whole profile's, read as Desktop's approval chip reads it (no session).
    approvals = (await client.rpc("config.get", key="approvals.mode") or {}).get("value")
    options = await client.rpc("model.options", session_id=sid) or {}
    use = await _usage_read(client, sid)
    served = use.pop("served")
    model = served.get("model") or info.get("model") or options.get("model")
    provider = served.get("provider") or info.get("provider") or options.get("provider")
    offered = _bot_offered(options, allowed) if allowed is not None else _offered(options)
    return {"model": model, "provider": provider, "fallback": served.get("fallback") is True, "reasoning": reasoning,
            "levels": list(route_supported_efforts(provider, model)), "yolo": yolo == "1", "busy": busy,
            "approvals": approvals, "folder": info.get("cwd"), **await asyncio.to_thread(_chat_facts, sid),
            "offered": offered, **use}


USAGE_KEYS = ("input", "output", "total", "calls", "compressions")
CONTEXT_KEYS = ("context_used", "context_max", "context_percent", "context_estimated")


async def _usage_read(client, sid: str) -> dict:
    """How full one chat's context is and the tokens it has used, read as Desktop's context meter reads them
    (apps/desktop use-statusbar-items.tsx): session.context_breakdown when it knows the window (it estimates
    from the chat's prompt, tools and messages before a turn has measured it), else session.usage's own
    context figures; the token counters from session.usage. Neither calls a model. A part Hermes would not
    give is null with Hermes's reason, and the rest of the read stands."""
    from hermes_cli.gateway_client import GatewayRPCError

    out = {}
    try:
        usage = await client.rpc("session.usage", session_id=sid) or {}
        out["usage"] = {k: usage.get(k) for k in USAGE_KEYS}
    except GatewayRPCError as exc:
        usage, out["usage"], out["usage_why"] = {}, None, f"Hermes did not say ({str(exc)[:120]})"
    # The chat's live agent's own model and provider, and whether it is on a fallback (fallback_model is
    # set only then); empty for a chat whose agent is not built yet, which starts on its main model.
    out["served"] = {"model": usage.get("model") or None, "provider": usage.get("provider") or None,
                     "fallback": bool(usage.get("fallback_model"))}
    try:
        breakdown = await client.rpc("session.context_breakdown", session_id=sid) or {}
    except GatewayRPCError as exc:
        breakdown, out["context_why"] = {}, f"Hermes did not say ({str(exc)[:120]})"
    reading = breakdown if (breakdown.get("context_max") or 0) > 0 else usage
    out["context"] = {k: reading.get(k) for k in CONTEXT_KEYS} if (reading.get("context_max") or 0) > 0 else None
    return out


async def _control(endpoint, sid: str, want, allowed: list | None = None) -> dict:
    """One chat's settings, changed first when `want` asks: a model (session.mutate model) and a thinking
    level (session.mutate reasoning), each fenced to the chat's revision and generation by Hermes's own
    PreparedMutations, as its ACP and TUI clients send them; what a message sent while it works does
    (config.set busy for that chat, as Desktop's busy picker sends it); and that Hermes's approval mode
    (config.set approvals.mode, the whole profile's, as Desktop's approval chip and `/approvals` write it).
    A model Hermes's selection guard holds for a confirmation is not switched: no confirmation is given
    from here."""
    from hermes_cli.gateway_client import GatewayClientError, connect_gateway
    from hermes_cli.gateway_mutations import PreparedMutations

    applied, reason = False, ""
    async with connect_gateway(endpoint=endpoint) as client:
        # Opening the chat, as Desktop does before it reads or changes one (session.info needs it open).
        await client.rpc("session.resume", session_id=sid)
        if want:
            mutations = PreparedMutations()
            try:
                pick = want.get("model")
                if pick:
                    # Checked again against Hermes's own list just before the switch: a request made by
                    # hand never moves a chat to a model that is billed per message.
                    options = await client.rpc("model.options", session_id=sid) or {}
                    now = _bot_offered(options, allowed) if allowed is not None else _offered(options)
                    if not any(o["provider"] == pick.get("provider") and o["model"] == pick.get("model") for o in now):
                        raise GatewayClientError("that model is not one of this Bot's own routes" if allowed is not None
                                                 else "that model is not one his sign-ins, a free model or a local one covers")
                    got = await mutations.apply(client, sid, "model",
                                                {"model": pick["model"], "provider": pick["provider"]})
                    if got.get("status") == "deferred":
                        reason = "a turn is running; Hermes switches this chat when it ends"
                if want.get("reasoning"):
                    await mutations.apply(client, sid, "reasoning", {"effort": want["reasoning"]})
                if want.get("busy"):
                    await client.rpc("config.set", session_id=sid, key="busy", value=want["busy"])
                if want.get("approvals"):
                    await client.rpc("config.set", key="approvals.mode", value=want["approvals"])
                applied = True
            except GatewayClientError as exc:
                reason = str(exc)
        state = await _control_read(client, sid, allowed)
    return {"state": state, "applied": applied, "reason": reason[:200]}


def _control_op(req: dict) -> dict:
    from hermes_cli.gateway_client import GatewayClientError

    sid, want, profile = req.get("session_id"), req.get("set"), req.get("profile") or ""
    # A Bot's chat: only its own models (the connector's `allowed`), and only its model and thinking change;
    # its approvals and what a message does while it works are its policy's.
    allowed = req.get("allowed") if profile else None
    if not isinstance(sid, str) or not sid or len(sid) > 128 or not (want is None or isinstance(want, dict)) or (
            profile and not (isinstance(profile, str) and PROFILE.match(profile) and isinstance(allowed, list)
                             and all(isinstance(a, list) and len(a) == 2 and all(isinstance(x, str) for x in a)
                                     for a in allowed))):
        return {"state": None, "applied": False, "reason": "bad request"}
    if profile and want is not None and set(want) - {"model", "reasoning"}:
        return {"state": None, "applied": False, "reason": "a Bot's chat takes a model and a thinking level only"}
    if want is not None:
        pick, level = want.get("model"), want.get("reasoning")
        busy, approvals = want.get("busy"), want.get("approvals")
        if set(want) - {"model", "reasoning", "busy", "approvals"} or not (pick or level or busy or approvals) or not (
                busy is None or busy in ("steer", "queue", "interrupt")) or not (
                approvals is None or approvals in ("manual", "smart", "off")) or not (
                pick is None or (isinstance(pick, dict) and isinstance(pick.get("model"), str)
                                 and isinstance(pick.get("provider"), str))) or not (
                level is None or isinstance(level, str)):
            return {"state": None, "applied": False, "reason": "bad request"}
    endpoint, state = _found(profile)
    if endpoint is None:
        name = req.get("name") if isinstance(req.get("name"), str) else "Hermes"
        return {"state": None, "applied": False, "reason": f"{name[:40]}'s gateway is {state}"}
    try:
        return asyncio.run(asyncio.wait_for(_control(endpoint, sid, want, allowed), CONTROL_SECONDS))
    except (TimeoutError, asyncio.TimeoutError):
        return {"state": None, "applied": False, "reason": f"Hermes did not answer in {CONTROL_SECONDS} seconds"}
    except GatewayClientError as exc:
        return {"state": None, "applied": False, "reason": f"Hermes refused it ({str(exc)[:120]})"}
    except OSError as exc:
        return {"state": None, "applied": False, "reason": f"could not reach the gateway ({type(exc).__name__})"}


async def _send(endpoint, req: dict) -> dict:
    from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError, connect_gateway

    try:
        async with connect_gateway(endpoint=endpoint) as client:
            caps = await client.rpc("groups.capabilities")
            if not caps.get("driver"):
                return {"sent": False, "reason": "the room's driver is not running"}
            try:
                manifest = [await _upload(client, req["room_id"], f) for f in req.get("files") or []]
            except GatewayRPCError as exc:
                # The room's store refused a file: nothing was sent, and no retry changes that.
                return {"sent": False, "reason": f"the room did not take a file ({exc})"}
            # Into the thread he replied in (TWHermesAgents #108), as Desktop's Reply in thread sends it; else a
            # thread of its own, named by the message's own event id, as Desktop starts one (canonical-group-send.ts).
            thread = req.get("thread_id") or req["event_id"]
            try:
                out = await client.rpc("groups.send", room_id=req["room_id"], event_id=req["event_id"],
                                       payload={"text": req["text"], "thread_id": thread}
                                       | ({"attachments": manifest} if manifest else {}))
            except GatewayRPCError as exc:
                return {"sent": False, "reason": f"the gateway refused it ({exc})"}
            except (GatewayClientError, TimeoutError, OSError):
                return {"sent": None, "reason": "the gateway stopped answering after the message went"}
            return {"sent": True, "seq": int((out.get("event") or {}).get("seq") or 0)}
    except (GatewayClientError, TimeoutError, OSError):
        # The connection failed before groups.send went out.
        return {"sent": False, "ready": False, "reason": "could not reach the Bots' gateway"}


def room_file(mime: str, data: bytes) -> tuple[str, str]:
    """(kind, type) a file goes into a room's store as: a photo or a PDF by its bytes, as the store checks
    them, else a file with the type the phone gave, or the type its bytes are when the store would refuse
    that one (gateway/hosted_room_attachments.py _sniff_known_mime and _validate_mime_and_kind). Desktop
    picks the kind by the type alone (apps/desktop canonical-group-attachments.tsx kindFor), so a photo
    whose bytes are not the type its name says was refused there; here it goes as a file."""
    from gateway.hosted_room_attachments import _sniff_known_mime
    sniffed = _sniff_known_mime(data)
    if sniffed.startswith("image/"):
        return "image", sniffed
    if sniffed == "application/pdf":
        return "pdf", sniffed
    if mime.startswith("image/") or mime == "application/pdf" or (mime.startswith("text/") and sniffed != "text/plain"):
        return "file", sniffed
    return "file", mime


async def _upload(client, room_id: str, f: dict) -> dict:
    """One file into the room's own store, as Desktop's + uploads it (groups.attachment.upload). Its upload
    id comes from the core's id for it (the store takes letters, digits and . : - only), so sending the same
    message again uploads nothing twice. Its manifest entry, as groups.send takes it."""
    import base64
    import hashlib
    data = base64.b64decode(f["data_base64"], validate=True)
    kind, mime = room_file(f["mime"], data)
    upload = "tw-" + hashlib.sha256(f["id"].encode("utf-8")).hexdigest()
    got = await client.rpc("groups.attachment.upload", room_id=room_id, upload_id=upload, kind=kind,
                           name=f["name"], mime=mime, data_base64=f["data_base64"])
    return {k: got[k] for k in ("attachment_id", "kind", "name", "size", "mime")}


def _react(profile: str, req: dict) -> dict:
    from pathlib import Path

    from hermes_constants import get_default_hermes_root
    from hermes_state_registry import acquire, release_or_close

    sid, mid, want = req.get("session_id"), req.get("message_id"), req.get("emoji")
    if (not isinstance(sid, str) or not sid or type(mid) is not int or mid < 1
            or not (want is None or (isinstance(want, str) and want.strip()))
            or (profile and not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", profile))):
        return {"set": False, "reason": "bad request"}
    path = Path(get_default_hermes_root()) / "profiles" / profile / "state.db" if profile else None
    db = acquire(path)
    try:
        mine = next((r.get("emoji") for r in db.get_message_reactions(sid, mid) if r.get("author") == "user"), None)
        if mine == want:
            return {"set": True}
        if db.set_message_reaction(sid, mid, want, author="user") is None:
            return {"set": False, "reason": "that message is not in this chat"}
        return {"set": True}
    finally:
        release_or_close(db)


REWIND_SECONDS = 60  # one session.mutate through the gateway


def rewind_payload(mid: int, retry: bool = False):
    """session.mutate rewind's payload, read from the same snapshot as its fences (PreparedMutations.apply takes
    a function for it, as /retry passes one): nothing is changed when that message is no longer one of his turns
    in the chat, or, for a retry, when Hermes cannot send that turn again as words (gateway_chat_commands.py
    _rewind_to and _user_turns, the turns /undo and /retry count)."""
    def payload(snapshot):
        from hermes_cli.gateway_chat_commands import _user_turns
        from hermes_cli.gateway_client import GatewayClientError

        target = next((t for t in _user_turns(snapshot) if t.get("row_id") == mid), None)
        if target is None:
            raise GatewayClientError("that turn is no longer one of yours in the chat")
        if retry:
            from agent.context_compressor import retryable_user_text, user_originated_turn_view
            try:
                retryable_user_text((user_originated_turn_view(target) or {}).get("content"))
            except ValueError as exc:
                raise GatewayClientError(f"Hermes cannot send that turn again ({exc})") from exc
        return {"target_message_id": mid}
    return payload


async def _rewind(endpoint, sid: str, action: str, mid: int) -> dict:
    from hermes_cli.gateway_client import connect_gateway
    from hermes_cli.gateway_mutations import PreparedMutations

    async with connect_gateway(endpoint=endpoint) as client:
        mutations = PreparedMutations()
        if action == "undo":
            payload = rewind_payload(mid)
            got = await mutations.apply(client, sid, "rewind", payload)
            mutations.acknowledge(sid, "rewind", payload)
            if got.get("status") == "nothing" or not got.get("rewound_count"):
                return {"done": False, "reason": "Hermes had nothing to take back there"}
            return {"done": True}
        got = await mutations.apply(client, sid, "branch", {"through_message_id": mid})
        mutations.acknowledge(sid, "branch", {"through_message_id": mid})
        child = got.get("branched_session_id")
        if not isinstance(child, str) or not child:
            return {"done": False, "reason": "Hermes made no new chat"}
        return {"done": True, "session_id": child}


def _rewind_op(profile: str, req: dict) -> dict:
    from hermes_cli.gateway_client import GatewayClientError

    action, sid, mid = req.get("action"), req.get("session_id"), req.get("message_id")
    if (action not in ("undo", "branch") or not isinstance(sid, str) or not sid or len(sid) > 128
            or type(mid) is not int or mid < 1 or (profile and not PROFILE.match(profile))):
        return {"done": False, "reason": "bad request"}
    endpoint, state = _found(profile)
    if endpoint is None:
        return {"done": False, "ready": False, "reason": f"{profile or 'Hermes'}'s gateway is {state}"}
    try:
        return asyncio.run(asyncio.wait_for(_rewind(endpoint, sid, action, mid), REWIND_SECONDS))
    except (TimeoutError, asyncio.TimeoutError):
        return {"done": None, "reason": f"Hermes did not answer in {REWIND_SECONDS} seconds; it may be done"}
    except GatewayClientError as exc:
        return {"done": False, "reason": f"Hermes refused it ({str(exc)[:150]})"}
    except OSError as exc:
        return {"done": False, "ready": False, "reason": f"could not reach the gateway ({type(exc).__name__})"}


def _session(req: dict) -> dict:
    from pathlib import Path

    from hermes_constants import get_hermes_home
    from hermes_state_errors import SessionActiveWriteGuardError
    from hermes_state_registry import acquire, release_or_close
    from hermes_state_runtime import RuntimeStoreError

    action, sid, title = req.get("action"), req.get("session_id"), req.get("title")
    if (action not in ("rename", "archive", "unarchive", "pin", "unpin", "read", "unread", "delete")
            or not isinstance(sid, str) or not sid
            or (action == "rename") != isinstance(title, str) or (action == "rename" and not title.strip())):
        return {"done": False, "reason": "bad request"}
    db = acquire(None)
    try:
        if db.get_session(sid) is None:
            # Gone already: a delete is done; anything else has nothing left to change.
            return {"done": True} if action == "delete" else {"done": False, "reason": "that chat is gone"}
        # His Bot Chat is kept as it is: Bot Mode and every Bot's message to him find it by its exact
        # title while it is hidden and not archived (hermes_state_titles.py), so a new title, a hide or a
        # delete would cut him off from it. Hermes's own apps never offer these for it either.
        if action in ("read", "unread"):
            # A read mark changes nothing he finds a chat by, so his Bot Chat takes it too, as in Desktop.
            db.set_session_read(sid, action == "read")
            return {"done": True}
        bot = db.get_session_by_title(db.CANONICAL_BOT_CHAT_TITLE)
        if bot and bot.get("hidden") and sid in db.get_compression_lineage(bot["id"]):
            return {"done": False, "reason": "his Bot Chat is kept as it is: it is not renamed, hidden or deleted"}
        if action == "rename":
            if "\n" in title or "\r" in title:
                return {"done": False, "reason": "a title is on one line"}
            try:
                if db.get_session_title(sid) != db.sanitize_title(title):
                    db.set_session_title(sid, title)
            except ValueError as exc:
                return {"done": False, "reason": str(exc)[:200]}
            return {"done": True}
        if action in ("archive", "unarchive"):
            db.set_session_archived(sid, action == "archive")
            return {"done": True}
        if action in ("pin", "unpin"):
            # Hermes's own pin, as Desktop sets it (PATCH /api/sessions/<id> pinned): the whole compression
            # lineage, and a pin also brings back a chat Hermes had hidden.
            db.set_session_pinned(sid, action == "pin")
            return {"done": True}
        try:
            db.delete_session(sid, sessions_dir=Path(get_hermes_home()) / "sessions", exclude_active_write_guards=True)
        except SessionActiveWriteGuardError:
            return {"done": False, "reason": "a turn is running in that chat; try again once it ends"}
        except RuntimeStoreError as exc:
            why = {"session_busy": "it has a queued or running turn; try again once it ends",
                   "unknown_execution": "a turn was lost across a gateway restart; "
                                        f"acknowledge it first with: hermes sessions discard {sid}"}
            return {"done": False, "reason": why.get(exc.reason, f"Hermes refused it ({exc.reason})")[:200]}
        return {"done": True} if db.get_session(sid) is None else {"done": False, "reason": "Hermes kept the chat"}
    finally:
        release_or_close(db)


SEARCH_WORDS = 200  # the longest search the app sends
SEARCH_MAX = 50  # one page of the chat list (own_connector.PAGE_CHATS)


def _snippet(text) -> str:
    """Hermes's snippet of the matched message, without the >>> <<< marks its index puts round each match."""
    return re.sub(r"\s+", " ", str(text or "").replace(">>>", "").replace("<<<", "")).strip()[:200]


def _search(req: dict) -> dict:
    from hermes_state_registry import acquire, release_or_close

    q, limit, exclude = req.get("q"), req.get("limit"), req.get("exclude", [])
    if (not isinstance(q, str) or not q.strip() or len(q) > SEARCH_WORDS or "\n" in q or "\r" in q
            or type(limit) is not int or not 0 < limit <= SEARCH_MAX
            or not (isinstance(exclude, list) and len(exclude) <= 16
                    and all(isinstance(x, str) and PROFILE.match(x) for x in exclude))):
        return {"found": None, "reason": "bad request"}
    q = q.strip()
    db = acquire(None)
    try:
        found: dict = {}

        def add(sid, snippet: str) -> None:
            if not isinstance(sid, str) or not sid or len(found) >= limit:
                return
            tip = db.get_compression_tip(sid) or sid
            if tip not in found:
                found[tip] = snippet

        for row in db.search_sessions_by_id(q, limit=limit, include_archived=False, exclude_sources=exclude or None):
            add(row.get("id"), "")
        # Each word as typed and as the start of a longer one; a quoted phrase and a word ending in * stay as
        # they are (Desktop's own prefix_query). Over-fetched, as Desktop does, since hits in one chat collapse.
        words = " ".join(t if t.startswith('"') or t.endswith("*") else t + "*"
                         for t in re.findall(r'"[^"]*"|\S+', q))
        for m in db.search_messages(query=words, exclude_sources=exclude or None, limit=max(limit * 5, 50),
                                    fields=("session_id", "snippet")):
            add(m.get("session_id"), _snippet(m.get("snippet")))
        if len(found) < limit:
            for row in db.list_sessions_rich(search_query=q, include_archived=False, order_by_last_active=True,
                                             exclude_sources=exclude or None, limit=limit):
                add(row.get("id"), "")
        return {"found": [{"id": k, "snippet": v} for k, v in found.items()]}
    finally:
        release_or_close(db)


def main() -> int:
    req = json.loads(sys.stdin.read() or "{}")
    op = req.get("op")
    profile = req.get("profile") or ""
    if op == "states":
        names = req.get("profiles")
        if not (isinstance(names, list) and len(names) <= 16 and all(isinstance(n, str) and PROFILE.match(n) for n in names)):
            print(json.dumps({"states": None, "reason": "bad request"}))
            return 2
        states = {}
        for name in names:
            try:
                states[name] = str(_found(name)[1])
            except Exception:  # this one's check could not run; the others still answer
                states[name] = "check_failed"
        print(json.dumps({"states": states}))
        return 0
    if op == "tasks":
        names = req.get("profiles")
        if not (isinstance(names, list) and 0 < len(names) <= 16 and len(set(names)) == len(names)
                and all(isinstance(n, str) and (n == "" or PROFILE.match(n)) for n in names)):
            print(json.dumps({"tasks": None, "reason": "bad request"}))
            return 2
        print(json.dumps({"tasks": {name: _tasks(name) for name in names}}))
        return 0
    if op == "control":
        # His own Hermes's chats, and a Bot's Bot Chat within its own routes (Patrick, 11 Nov: each Bot's model
        # and think level, and a switch, as Desktop has).
        print(json.dumps(_control_op(req)))
        return 0
    if op == "tail":
        # His own Hermes's subagents only: a Bot's chats stay with its own Board session.
        print(json.dumps(_tail(req) if not profile else
                         {"available": False, "reason": "a Bot's subagents are not read from here"}))
        return 0
    if op == "job":
        # His own Hermes's jobs only: a Bot's scheduled jobs are not changed from the app.
        if profile:
            print(json.dumps({"done": False, "reason": "a Bot's scheduled jobs are not changed from here"}))
            return 0
        try:
            print(json.dumps(_job(req)))
        except Exception as exc:  # Hermes's store refused or was busy: said, and nothing is tried again by itself
            print(json.dumps({"done": False, "reason": f"Hermes's cron store did not take it ({type(exc).__name__})"}))
        return 0
    if op == "search":
        # His own Hermes's chats only: a Bot's chats are not searched from the app.
        if profile:
            print(json.dumps({"found": None, "reason": "a Bot's chats are not searched from here"}))
            return 0
        try:
            print(json.dumps(_search(req)))
        except Exception as exc:  # Hermes's store refused or was busy: say so; nothing was changed
            print(json.dumps({"found": None, "reason": f"Hermes's search did not answer ({type(exc).__name__})"}))
        return 0
    if op == "rewind":
        print(json.dumps(_rewind_op(profile if isinstance(profile, str) else "?", req)))
        return 0
    if op not in ("send", "ready", "react", "session") or not isinstance(profile, str):
        print(json.dumps({"sent": False, "reason": "bad request"}))
        return 2
    if op == "session":
        if profile:
            print(json.dumps({"done": False, "reason": "a Bot's Bot Chat is not renamed, hidden or deleted from here"}))
            return 0
        try:
            print(json.dumps(_session(req)))
        except Exception as exc:  # Hermes's store refused or was busy: say so; the core may hand it out again
            print(json.dumps({"done": False, "reason": f"Hermes's store did not take it ({type(exc).__name__})"}))
        return 0
    if op == "react":
        try:
            print(json.dumps(_react(profile, req)))
        except Exception as exc:  # Hermes's store refused or was busy: nothing was set
            print(json.dumps({"set": False, "reason": f"Hermes's store did not take it ({type(exc).__name__})"}))
        return 0
    endpoint, state = _found(profile)
    if endpoint is None:
        name = req.get("name") if isinstance(req.get("name"), str) else "the Bots' gateway"
        print(json.dumps({"sent": False, "ready": False, "reason": f"{name[:40]} is {state}"}))
        return 0
    if op == "ready":
        print(json.dumps({"ready": True}))
        return 0
    print(json.dumps(asyncio.run(_send(endpoint, req))))
    return 0


def say(line) -> None:
    if line:
        sys.stdout.write(json.dumps(line) + "\n")
        sys.stdout.flush()


def announce(status: str, session_id=None) -> None:
    # With the chat's id: a new chat has none until Hermes makes it, and the app goes into it from here, while
    # its first turn runs, not once that turn ends (10 Nov: a long first turn sat on "Sending" with no chat).
    say({"type": "admission", "status": status,
         **({"session_id": session_id[:128]} if isinstance(session_id, str) and session_id else {})})


def steerable(text, has_file: bool) -> bool:
    return isinstance(text, str) and bool(text.strip()) and not has_file and not text.lstrip().startswith("/")


ASK_FIELDS = {"prompt_id": 128, "kind": 16, "question": 4000, "description": 4000, "command": 4000}


def ask_line(prompt) -> dict | None:
    """One open question or approval, as the connector reads it: Hermes's own fields, nothing more."""
    if not isinstance(prompt, dict) or prompt.get("kind") not in ("approval", "clarify"):
        return None
    out = {"type": "ask"}
    for k, n in ASK_FIELDS.items():
        if isinstance(prompt.get(k), str):
            out[k] = prompt[k][:n]
    if not out.get("prompt_id"):
        return None
    choices = prompt.get("choices")
    out["choices"] = [c[:300] for c in choices if isinstance(c, str)][:12] if isinstance(choices, list) else []
    out["multi_select"] = prompt.get("multi_select") is True
    return out


# What a turn shows while it runs, as Desktop shows it (Patrick, 9 Nov: "I want it all to be live"): besides
# the stream-json lines Hermes's own emitter writes (text, tool_use, tool_result), each event of the chat's
# gateway stream that Desktop draws is said as one line, cut to size: the thinking as it comes, the tools
# with the preview Hermes's own display makes of each, a tool's progress, the status lines, the turn's
# subagents, and a background task's end. The connector keeps them in memory and the core holds them in
# memory only (own_connector.LiveTurn, live_turns.py), as the pages are held.
LIVE_TEXT = 4000


def _cut_str(payload: dict, key: str, n: int) -> str | None:
    v = payload.get(key)
    return v[:n] if isinstance(v, str) else None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _tool_preview(name, args) -> str | None:
    if not isinstance(name, str) or not isinstance(args, dict):
        return None
    try:
        from agent.display import build_tool_preview
        got = build_tool_preview(name, args, max_len=200)
    except Exception:  # a preview is a nicety: a tool Hermes's display cannot preview shows by its name
        return None
    return got[:200] if isinstance(got, str) and got.strip() else None


def live_line(kind, payload) -> dict | None:
    """One gateway event of the turn as the connector reads it, or None for one the app does not draw."""
    if not isinstance(payload, dict) or not isinstance(kind, str):
        return None
    if kind in ("reasoning.delta", "thinking.delta"):
        text = _cut_str(payload, "text", LIVE_TEXT)
        return {"type": "thinking", "text": text} if text else None
    if kind == "status.update":
        text = _cut_str(payload, "text", 300)
        return {"type": "status", "kind": _cut_str(payload, "kind", 32) or "", "text": text} \
            if text and text.strip() else None
    if kind == "tool.generating":
        return {"type": "tool_generating", "name": _cut_str(payload, "name", 64) or ""}
    if kind == "tool.start":
        name = payload.get("name") or payload.get("tool_name")
        preview = _cut_str(payload, "preview", 200) or _tool_preview(name, payload.get("args"))
        return {"type": "step", "id": _cut_str(payload, "tool_id", 128) or "",
                "name": name[:64] if isinstance(name, str) else "", "preview": preview or ""}
    if kind == "tool.progress":
        return {"type": "step_progress", "id": _cut_str(payload, "tool_id", 128) or "",
                "percent": _num(payload.get("percent")), "message": _cut_str(payload, "message", 200) or ""}
    if kind == "tool.complete":
        return {"type": "step_done", "id": _cut_str(payload, "tool_id", 128) or "",
                "name": _cut_str(payload, "name", 64) or "", "error": payload.get("is_error") is True,
                "secs": _num(payload.get("duration_s"))}
    if kind.startswith("subagent."):
        sid = payload.get("subagent_id") or payload.get("child_session_id")
        index = _num(payload.get("task_index"))
        return {"type": "subagent", "event": kind[9:40],
                "id": sid[:64] if isinstance(sid, str) and sid else f"task-{index}",
                "goal": _cut_str(payload, "goal", 300) or "", "status": _cut_str(payload, "status", 16) or "",
                "tool": _cut_str(payload, "tool_name", 64) or "", "preview": _cut_str(payload, "tool_preview", 200) or "",
                "tools": _num(payload.get("tool_count")), "summary": _cut_str(payload, "summary", 600) or "",
                "secs": _num(payload.get("duration_seconds"))}
    if kind == "background.complete":
        return {"type": "background", "id": _cut_str(payload, "task_id", 64) or "",
                "text": _cut_str(payload, "text", 300) or ""}
    return None


class _Attended:
    """The view's gateway client, submitting its turn without `finite`: a turn Patrick is there to answer."""

    def __init__(self, client):
        self._client = client

    async def rpc(self, method, **params):
        if method == "prompt.submit":
            params.pop("finite", None)
            params.pop("unattended", None)
        return await self._client.rpc(method, **params)

    def __getattr__(self, name):
        return getattr(self._client, name)


class _WithPhotos:
    """The view's gateway client, naming the turn's photos on its prompt.submit (`attachments`), the way
    Desktop sends a photo (apps/desktop use-prompt-actions/submit.ts submitParams; gateway/session_controls.py
    submission takes `attachments`, and gateway/session_ingress_media.py admit_attachments each {path, mime}
    in Hermes's image staging folder)."""

    def __init__(self, client, photos: list):
        self._client, self._photos = client, photos

    async def rpc(self, method, **params):
        if method == "prompt.submit" and self._photos:
            params["attachments"] = self._photos
        return await self._client.rpc(method, **params)

    def __getattr__(self, name):
        return getattr(self._client, name)


def staged_photos(paths: list[str]) -> list[dict]:
    """Each photo Patrick attached, copied into this Hermes's image staging folder under the SHA-256 of its
    bytes, as Hermes stages a room's photo for a turn (gateway/session_hosted_attachments.py
    bound_submission_payload): [{path, mime}] for prompt.submit. A file Hermes would not take as an image
    stops the turn here, before anything is sent."""
    import hashlib
    import os
    import tempfile
    from pathlib import Path
    from gateway.platforms.base import get_image_cache_dir
    from gateway.session_ingress_media import sniff_image_mime

    staging = get_image_cache_dir().resolve()
    out = []
    for name in paths:
        data = Path(name).read_bytes()
        mime = sniff_image_mime(data)
        if mime is None:
            raise SystemExit(f"{Path(name).name} is not a photo Hermes takes as an image")
        target = staging / (hashlib.sha256(data).hexdigest() + {"image/png": ".png", "image/jpeg": ".jpg",
                                                                  "image/gif": ".gif", "image/webp": ".webp"}[mime])
        if not (target.is_file() and not target.is_symlink() and target.read_bytes() == data):
            fd, temporary = tempfile.mkstemp(dir=staging)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        out.append({"path": str(target), "mime": mime})
    return out


def follow_busy_setting(has_file: bool, orders=None, photos: list | None = None, rewind=None) -> None:
    """Hermes's chat view, with the hand-over of a one-shot message changed as this file's words say.
    With `orders` (a file of order lines), the turn is attended, as this file's words say. With `photos`
    ([{path, mime}], staged_photos), the turn's prompt.submit names them. With `rewind` ("retry" or "edit",
    and his turn's id), the chat is first taken back to before that turn."""
    from hermes_cli import gateway_chat_view

    base = gateway_chat_view.GatewayChatView

    class Photos(base):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            if photos:
                self.client = _WithPhotos(self.client, photos)

    base = Photos

    class Attended(base):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            self.client = _Attended(self.client)

        # The open prompts. The view sets them whole from each snapshot (at the start and after a replay
        # gap) and changes them one at a time from the prompt events; each way, the change is said.
        @property
        def prompts(self):
            return self.__dict__.get("_tw_prompts", {})

        @prompts.setter
        def prompts(self, now):
            was = self.__dict__.get("_tw_prompts", {})
            self.__dict__["_tw_prompts"] = now
            for pid in [k for k in now if k not in was]:
                say(ask_line(now[pid]))
            for pid in [k for k in was if k not in now]:
                say({"type": "ask_settled", "prompt_id": str(pid)[:128]})

        def _finite_block(self, admission):
            # An open question or approval waits for Patrick: only Hermes's other reasons to stop count.
            held = self.__dict__.get("_tw_prompts", {})
            self.__dict__["_tw_prompts"] = {}
            try:
                return super()._finite_block(admission)
            finally:
                self.__dict__["_tw_prompts"] = held

        def show_prompt(self, prompt):
            pass  # said once already, when the prompts were set

        def _request(self, admission, payload):
            super()._request(admission, payload)
            say(ask_line(payload))

        def _settled(self, admission, payload):
            super()._settled(admission, payload)
            if isinstance(payload, dict) and isinstance(payload.get("prompt_id"), str):
                say({"type": "ask_settled", "prompt_id": payload["prompt_id"][:128]})

        async def run(self, query=None, *, oneshot=False):
            loop = asyncio.get_running_loop()
            import threading

            def read():
                for line in orders:
                    try:
                        order = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(order, dict):
                        try:
                            asyncio.run_coroutine_threadsafe(self._order(order), loop)
                        except RuntimeError:  # the turn is over and its loop closed
                            return

            threading.Thread(target=read, daemon=True).start()
            return await super().run(query, oneshot=oneshot)

        async def _order(self, order):
            op, pid = order.get("op"), order.get("prompt_id")
            told = {"type": "order", "op": op if op in ("stop", "answer") else "?"}
            try:
                if op == "stop":
                    await self.client.rpc("session.interrupt", session_id=self.session_id,
                                          execution_generation=self.generation)
                    return say(told | {"status": "sent"})
                prompt = self.prompts.get(pid) if isinstance(pid, str) else None
                value = order.get("value")
                if op != "answer" or not isinstance(value, str):
                    return say(told | {"status": "refused", "reason": "not an order this turn takes"})
                told["prompt_id"] = pid[:128]
                if prompt is None:
                    return say(told | {"status": "gone", "reason": "that question is no longer open"})
                kind = prompt["kind"]
                got = await self.client.rpc(kind + ".respond", session_id=self.session_id,
                                            execution_generation=prompt["execution_generation"], prompt_id=pid,
                                            **({"choice": value} if kind == "approval" else {"answer": value}))
                if isinstance(got, dict) and got.get("status") == "already_resolved":
                    return say(told | {"status": "gone", "reason": "that question was already answered"})
                return say(told | {"status": "sent"})
            except Exception as exc:  # Hermes refused it (a choice it did not offer, a stale turn): say so
                return say(told | {"status": "refused", "reason": f"Hermes refused it ({type(exc).__name__})"})

    class BusyAware(Attended if orders is not None else base):
        def _dispatch_event(self, kind, admission, payload):
            super()._dispatch_event(kind, admission, payload)
            say(live_line(kind, payload))

        def _running(self):
            # The admission running now, from the chat's snapshot (the resume this view was made from).
            return next((r["admission_id"] for r in self.pending
                         if isinstance(r, dict) and r.get("status") == "started"), None)

        async def submit(self, text):
            if rewind is not None and not self.__dict__.get("_tw_rewound"):
                self.__dict__["_tw_rewound"] = True
                text = await self._tw_rewind(text)
            running = self._running() if self.finite else None
            if running and not self.prompts and steerable(text, has_file):
                mode = (await self.client.rpc("config.get", key="busy", session_id=self.session_id) or {}).get("value")
                if mode in ("steer", "interrupt"):
                    got = await self.client.rpc("session.steer" if mode == "steer" else "session.redirect",
                                                session_id=self.session_id, text=text,
                                                execution_generation=self.generation) or {}
                    if got.get("voice_stopped") is True:
                        return got
                    if got.get("status") in ("queued", "redirected"):
                        announce("steered" if mode == "steer" else "redirected", self.session_id)
                        return {"admission_id": running}
            receipt = await super().submit(text)
            status = (receipt or {}).get("status")
            if status in ("started", "queued"):
                announce(status, self.session_id)
                self._queued = (receipt.get("admission_id") if status == "queued" else None)
            return receipt

        async def _tw_rewind(self, text):
            """The chat taken back to before his turn, as /retry and /undo take it (gateway_chat_commands.py
            _rewind), and the words to send: a retry's from Hermes, an edit's as given."""
            from hermes_cli.gateway_client import GatewayClientError

            kind, mid = rewind
            payload = rewind_payload(mid, retry=kind == "retry")
            try:
                got = await self.mutations.apply(self.client, self.session_id, "rewind", payload)
            except GatewayClientError as exc:
                say({"type": "rewind", "status": "refused", "reason": f"Hermes refused it ({str(exc)[:150]})"})
                raise
            if got.get("status") == "nothing" or not got.get("rewound_count"):
                self.mutations.acknowledge(self.session_id, "rewind", payload)
                say({"type": "rewind", "status": "refused", "reason": "Hermes had nothing to take back there"})
                raise GatewayClientError("nothing to take back")
            # Kept until the chat is read again, as _rewind keeps it: asked again after a lost read, the same
            # rewind is replayed, never a new target worked out from the chat already taken back.
            snapshot = await self.client.rpc("session.resume", session_id=self.session_id)
            self.mutations.acknowledge(self.session_id, "rewind", payload)
            self.generation = snapshot["execution_generation"]
            self.pending = snapshot.get("pending", [])
            say({"type": "rewind", "status": "done", "count": int(got["rewound_count"])})
            if kind == "edit":
                return text
            from agent.context_compressor import retryable_user_text
            return retryable_user_text((got.get("target_message") or {}).get("content"))

        def _session_info(self, admission, payload):
            super()._session_info(admission, payload)
            # Its turn left Hermes's queue and runs: said once, so "In queue" ends when it really does.
            mine = getattr(self, "_queued", None)
            if mine and any(isinstance(r, dict) and r.get("admission_id") == mine and r.get("status") == "started"
                            for r in self.pending):
                self._queued = None
                announce("started", self.session_id)

    gateway_chat_view.GatewayChatView = BusyAware


def own_args(args: list[str]) -> tuple[list[str], bool, list[str]]:
    """(Hermes's own arguments, whether the message carries files, the photos to hand the turn): this file's
    `--tw-files` and each `--tw-image <path>` taken out before Hermes reads the rest."""
    rest, files, photos, i = [], False, [], 0
    while i < len(args):
        if args[i] == "--tw-files":
            files = True
        elif args[i] == "--tw-image" and i + 1 < len(args):
            photos.append(args[i + 1])
            i += 1
        else:
            rest.append(args[i])
        i += 1
    return rest, files or bool(photos), photos


def turn(args: list[str], attended: bool = False) -> None:
    """`hermes chat <args>` in this runtime, with the busy-input hand-over above. Exits as Hermes exits.
    Attended: the orders come on this process's stdin, and Hermes's own fd 0 becomes the null device.
    `--tw-files` says the message carries files, and each `--tw-image <path>` is a photo for the turn.
    `--tw-rewind retry|edit <id>` first, before Hermes's own arguments: the chat is taken back first."""
    rewind = None
    if args[1:2] == ["--tw-rewind"]:
        kind, mid = (args[2:4] + ["", ""])[:2]
        if kind not in ("retry", "edit") or not mid.isdigit() or int(mid) < 1:
            raise SystemExit("--tw-rewind takes retry or edit and his turn's id")
        rewind, args = (kind, int(mid)), [args[0], *args[4:]]
    args, has_file, photos = own_args(args)
    photos = staged_photos(photos) if photos else []
    orders = None
    if attended:
        import os
        orders = os.fdopen(os.dup(0), "r", encoding="utf-8", errors="replace")
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.close(null)
        sys.stdin = open(os.devnull, encoding="utf-8")
    follow_busy_setting(has_file, orders, photos, rewind)
    sys.argv = ["hermes", *args]
    import runpy
    runpy.run_module("hermes_cli.main", run_name="__main__", alter_sys=True)


def use_root(argv: list[str]) -> None:
    """--hermes-root <folder>: Hermes's source folder, put first on the path before any of Hermes is imported."""
    if "--hermes-root" not in argv:
        return
    i = argv.index("--hermes-root")
    root = argv[i + 1] if i + 1 < len(argv) else ""
    import os
    if not (os.path.isabs(root) and os.path.isdir(root)):
        raise SystemExit(f"--hermes-root must name Hermes's source folder by its full path, not {root!r}")
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        os.environ.pop(name, None)
    sys.path.insert(0, root)
    if os.path.isfile(os.path.join(root, "hermes_bootstrap.py")):
        # What Hermes's own runtime command does next (hermes_cli/_launchers.py runtime_command): its
        # bootstrap puts Hermes's dependencies on the path (2 Nov on the VM: without it, ruamel is missing).
        sys._hermes_pin_default_home = True
        import hermes_bootstrap  # noqa: F401
        if not os.environ.get("HERMES_HOME"):
            from hermes_constants import get_default_hermes_root
            os.environ["HERMES_HOME"] = str(get_default_hermes_root())


if __name__ == "__main__":
    use_root(sys.argv[1:])
    rest = sys.argv[3:] if sys.argv[1:2] == ["--hermes-root"] else sys.argv[1:]
    if rest[:1] == ["chat"]:
        turn(rest)
    if rest[:1] == ["attended-chat"]:
        turn(["chat", *rest[1:]], attended=True)
    sys.exit(main())
