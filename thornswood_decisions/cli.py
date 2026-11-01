"""The operator's commands for the decision core, run on the VM as its user.

    sudo -u tw-decide /opt/thornswood-hermes/bin/tw-decide --db /srv/tw/decide/core.db <command> ...

(`tw-decide` runs this from the root-owned release with `python3 -I`; from a
checkout, `python3 -m thornswood_decisions.cli` does the same.)

  init        make the workspace (every flag off), Patrick, and the six Bots' principals
  bind-room   bind the workspace to the Workbench room id the contract check printed
  pair        print a one-time code for the first device (10 minutes)
  flag        turn one flag on or off
  test-card   open a test card as Bramble, in a test workspace only
  serve       answer the API on 127.0.0.1
  feed-token  print a new token for the work or room feed (replaces the old one)
  selfcheck   a whole passkey ceremony against a scratch store, with the installed library
  backup      make a consistent copy
  verify      check the event chain

Patrick reaches this only over SSH with his own key. No route of the API
reaches any of it.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from . import backup as backup_mod
from . import contracts
from .api import DEFAULT_PORT, Api, make_server
from .core import DecisionCore
from .feeds import NAMES as FEED_NAMES
from .feeds import Feeds
from .schema import FLAGS
from .store import CoreError, Store

OPERATOR = "operator"


def _workspace(store: Store, name: str) -> tuple[str, bool]:
    row = store.db.execute("SELECT id, is_test FROM workspaces WHERE name = ?", (name,)).fetchone()
    if not row:
        raise CoreError(404, "no_workspace", f"no workspace named {name}; run init first")
    return row["id"], bool(row["is_test"])


def cmd_init(store: Store, args) -> dict:
    ws = store.ensure_workspace(args.workspace, is_test=args.test, by=OPERATOR)
    core = DecisionCore(store)
    core.auth.ensure_patrick(ws)
    ids = contracts.load()["profiles"]["ids"]
    for bot in ids:
        core.auth.ensure_service(ws, "bramble" if bot == "bramble" else "specialist", bot)
        core.bind_bot(ws, bot, bot, by=OPERATOR)
    core.auth.ensure_service(ws, "push_worker")
    return {"workspace": args.workspace, "id": ws, "test": args.test,
            "flags": {f: store.flag(ws, f) for f in FLAGS}}


def cmd_bind_room(store: Store, args) -> dict:
    ws, _ = _workspace(store, args.workspace)
    name = contracts.load()["room"]["name"]
    DecisionCore(store).bind_room(ws, args.room_id, name, by=OPERATOR)
    return {"bound": args.room_id, "name": name}


def cmd_pair(store: Store, args) -> dict:
    ws, _ = _workspace(store, args.workspace)
    return DecisionCore(store).auth.operator_pairing_code(ws, args.kind, args.label, by=OPERATOR)


def cmd_flag(store: Store, args) -> dict:
    ws, is_test = _workspace(store, args.workspace)
    on = args.state == "on"
    if on and not is_test and not args.patrick_switched:
        raise CoreError(403, "not_switched",
                        "a flag in a real workspace goes on only at the switch from the report page, "
                        "once T33 passes and Patrick says so; pass --patrick-switched then")
    store.set_flag(ws, args.name, on, by=OPERATOR)
    return {"workspace": args.workspace, args.name: on}


def cmd_test_card(store: Store, args) -> dict:
    ws, is_test = _workspace(store, args.workspace)
    if not is_test:
        raise CoreError(403, "not_a_test_workspace", "test cards go only in a test workspace")
    core = DecisionCore(store)
    bramble = core.auth.ensure_service(ws, "bramble", "bramble")
    return core.open_card(
        bramble, kind="question", title=args.title,
        body="A test card. Answering it changes nothing anywhere: no merge, no deploy, no Ship.",
        options=[{"key": "a", "label": "Option A", "consequence": "Nothing happens; this is a test."},
                 {"key": "b", "label": "Option B", "consequence": "Nothing happens either."}],
        recommendation="a", scope_of_yes="Nothing. This card is a test.", risk=args.risk)


def cmd_serve(store: Store, args) -> None:
    store.close()
    api = Api(args.db, args.workspace, origin=args.origin or None)
    server = make_server(api, "127.0.0.1", args.port)
    # SIGTERM (systemctl stop) ends serve_forever in this thread by raising here.
    # server.shutdown() would wait on this same thread and never return.
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    print(f"tw-decide listening on 127.0.0.1:{args.port} for workspace {args.workspace}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        api.close()


def cmd_feed_token(store: Store, args) -> None:
    ws, _ = _workspace(store, args.workspace)
    # Only the token goes to stdout, so the install can hand it straight to the feed's file.
    print(Feeds(store).new_token(ws, args.name, by=OPERATOR))


def cmd_selfcheck(store: Store, args) -> dict:
    """Proves the installed passkey library and the core's records work together on this
    machine: a scratch store in a temp folder, a test workspace, a software passkey."""
    import tempfile
    from .passkey import Passkeys
    from .softkey import SoftKey
    origin = "https://selfcheck.invalid"
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Store(Path(tmp) / "selfcheck.db")
        try:
            core = DecisionCore(scratch)
            ws = scratch.ensure_workspace("selfcheck", is_test=True, by=OPERATOR)
            core.auth.ensure_patrick(ws)
            code = core.auth.operator_pairing_code(ws, "phone", "selfcheck", by=OPERATOR)["code"]
            _, session = core.auth.redeem_pairing_code(ws, code)
            p = core.auth.authenticate(session.token)
            pk = Passkeys(scratch, origin)
            key = SoftKey(origin, "selfcheck.invalid")
            pk.register(p, key.create(pk.register_options(p)), "selfcheck")
            pk.sign_in(ws, key.get(pk.sign_in_options(ws)))
            try:
                pk.sign_in(ws, key.get(pk.sign_in_options(ws), origin="https://evil.invalid"))
            except CoreError as exc:
                refused = exc.reason
            else:
                raise CoreError(500, "selfcheck", "a passkey answer for another origin was accepted")
        finally:
            scratch.close()
    import webauthn
    return {"ok": True, "webauthn": webauthn.__version__, "wrong_origin": refused}


def cmd_backup(store: Store, args) -> dict:
    return backup_mod.backup(store, args.dest)


def cmd_verify(store: Store, args) -> dict:
    ok, why = store.verify_events()
    if not ok:
        raise CoreError(500, "chain_broken", why)
    return {"ok": ok, "events": why}


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="thornswood_decisions.cli", description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="the core's SQLite store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("--workspace", required=True)
    p.add_argument("--test", action="store_true", help="a test workspace, for test cards only")
    p = sub.add_parser("bind-room")
    p.add_argument("--workspace", required=True)
    p.add_argument("--room-id", required=True)
    p = sub.add_parser("pair")
    p.add_argument("--workspace", required=True)
    p.add_argument("--kind", choices=("desktop", "phone"), required=True)
    p.add_argument("--label", required=True)
    p = sub.add_parser("flag")
    p.add_argument("--workspace", required=True)
    p.add_argument("name", choices=FLAGS)
    p.add_argument("state", choices=("on", "off"))
    p.add_argument("--patrick-switched", action="store_true")
    p = sub.add_parser("test-card")
    p.add_argument("--workspace", required=True)
    p.add_argument("--title", default="Test card: pick either option")
    p.add_argument("--risk", choices=("normal", "high"), default="normal")
    p = sub.add_parser("serve")
    p.add_argument("--workspace", required=True)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--origin", default=None, help="the phone app's exact origin, once Tailscale Serve gives it")
    p = sub.add_parser("feed-token")
    p.add_argument("--workspace", required=True)
    p.add_argument("--name", choices=FEED_NAMES, required=True)
    sub.add_parser("selfcheck")
    p = sub.add_parser("backup")
    p.add_argument("--dest", required=True)
    sub.add_parser("verify")
    return ap


COMMANDS = {"init": cmd_init, "bind-room": cmd_bind_room, "pair": cmd_pair, "flag": cmd_flag,
            "test-card": cmd_test_card, "serve": cmd_serve, "feed-token": cmd_feed_token, "selfcheck": cmd_selfcheck,
            "backup": cmd_backup, "verify": cmd_verify}


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    store = Store(Path(args.db))
    try:
        out = COMMANDS[args.cmd](store, args)
    except CoreError as exc:
        print(f"STOP {exc.reason}: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    if out is not None:
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
