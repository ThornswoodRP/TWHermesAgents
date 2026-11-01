"""One room message, or one readiness check, through the Bots' own gateway.

The connector runs this with Hermes's own Python (the interpreter the `hermes`
command starts), as the Bots' `hermes` user, so it can use Hermes's client the
way Desktop does (contracts.json "room"):

    <hermes python> -I room_send.py < {"op": "send", "room_id", "event_id", "text"}
    <hermes python> -I room_send.py < {"op": "ready", "profile": "<bot or empty>"}

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
It imports nothing from this repository.
"""

import asyncio
import json
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


def main() -> int:
    req = json.loads(sys.stdin.read() or "{}")
    op = req.get("op")
    profile = req.get("profile") or ""
    if op not in ("send", "ready") or not isinstance(profile, str):
        print(json.dumps({"sent": False, "reason": "bad request"}))
        return 2
    endpoint, state = _found(profile)
    if endpoint is None:
        print(json.dumps({"sent": False, "ready": False, "reason": f"the Bots' gateway is {state}"}))
        return 0
    if op == "ready":
        print(json.dumps({"ready": True}))
        return 0
    print(json.dumps(asyncio.run(_send(endpoint, req))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
