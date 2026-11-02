"""One room message, or one readiness check, through the Bots' own gateway.

The connector runs this with Hermes's own Python (the interpreter the `hermes`
command starts), as the Bots' `hermes` user, so it can use Hermes's client the
way Desktop does (contracts.json "room"):

    <hermes python> -I room_send.py < {"op": "send", "room_id", "event_id", "text"}
    <hermes python> -I room_send.py < {"op": "ready", "profile": "<bot or empty>", "name": "<optional>"}
    <hermes python> -I room_send.py < {"op": "react", "profile": "<bot or empty>", "session_id",
                                       "message_id", "emoji": "<one of the phone's, or null>"}

own_connector.py asks it the same readiness question for Patrick's own Hermes
(profile empty, as that Hermes's own user), naming that gateway in `name`.

It finds the gateway that is already running and never starts one
(`discover_gateway_endpoint`, "preserving uncertainty instead of spawning").
It reads the request on stdin, so no message is on a command line, and prints
one JSON line:

    {"sent": true, "seq": n}             the room took it;
    {"sent": false, "reason": "..."}     it was not sent (the gateway was not
                                         ready, the room's driver was not
                                         running, or the gateway refused it);
    {"sent": null, "reason": "..."}      it may have been sent.

`groups.send` with the same `event_id` is the same event (hosted_rooms.py:250),
which is how the core settles an unclear send from the room feed afterwards.

`react` sets Patrick's reaction on one message of a chat to exactly the emoji
asked for, or takes it off (null), with Hermes's own SessionDB, the way
Hermes's own reaction tool and Desktop do (contracts.json "reactions"). Hermes
keeps one reaction per person per message, and the same emoji again would take
it off, so this reads the reaction first and writes only when it differs:
asking twice is the same as asking once. It prints {"set": true}, or
{"set": false, "reason": "..."} when nothing changed. It needs no gateway.

It imports nothing from this repository.
"""

import asyncio
import json
import re
import sys


def _found(profile: str):
    from pathlib import Path

    from hermes_constants import get_default_hermes_root
    from hermes_cli.gateway_runtime import attachable, discover_gateway_endpoint, gateway_profile_home

    home = Path(get_default_hermes_root())
    if profile:
        home = home / "profiles" / profile
    found = discover_gateway_endpoint(gateway_profile_home(home))
    return (found.endpoint if attachable(found) else None), found.state


async def _send(endpoint, req: dict) -> dict:
    from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError, connect_gateway

    try:
        async with connect_gateway(endpoint=endpoint) as client:
            caps = await client.rpc("groups.capabilities")
            if not caps.get("driver"):
                return {"sent": False, "reason": "the room's driver is not running"}
            try:
                out = await client.rpc("groups.send", room_id=req["room_id"], event_id=req["event_id"],
                                       payload={"text": req["text"], "thread_id": req["event_id"]})
            except GatewayRPCError as exc:
                return {"sent": False, "reason": f"the gateway refused it ({exc})"}
            except (GatewayClientError, TimeoutError, OSError):
                return {"sent": None, "reason": "the gateway stopped answering after the message went"}
            return {"sent": True, "seq": int((out.get("event") or {}).get("seq") or 0)}
    except (GatewayClientError, TimeoutError, OSError):
        # The connection failed before groups.send went out.
        return {"sent": False, "reason": "could not reach the Bots' gateway"}


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


def main() -> int:
    req = json.loads(sys.stdin.read() or "{}")
    op = req.get("op")
    profile = req.get("profile") or ""
    if op not in ("send", "ready", "react") or not isinstance(profile, str):
        print(json.dumps({"sent": False, "reason": "bad request"}))
        return 2
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


if __name__ == "__main__":
    sys.exit(main())
