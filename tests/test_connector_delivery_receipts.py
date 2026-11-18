"""A local viewer's deadline is not the gateway-owned admission's end.

The gateway has accepted these fixture messages and keeps working after its
viewer detaches. Delivery can be acknowledged only with the helper's own exact
submission receipt; an init, progress, or a different admission is not that proof.
"""
import json
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

from decisions_helpers import World
from test_decisions_chat import flags, seat
from thornswood_decisions import connector, room_send
from thornswood_decisions.chat import Chat
from thornswood_decisions.feeds import Feeds
from thornswood_decisions.own_connector import follow_turn


def receipt(admission="a2", session="s1", status="started"):
    return {"type": "admission", "status": status, "session_id": session, "admission_id": admission}


class DeliveryAtViewerDeadline(unittest.TestCase):
    def finish(self, events, *, process_ended=False):
        consumed, viewer_closed = threading.Event(), threading.Event()
        let_gateway_work, gateway_worked = threading.Event(), threading.Event()
        def gateway_work():
            if let_gateway_work.wait(5):
                gateway_worked.set()
        gateway = threading.Thread(target=gateway_work)
        gateway.start()
        with tempfile.TemporaryDirectory() as directory:
            class Viewer:
                folder = Path(directory)
                rec = {"target": "echo", "session": None}
                started = 0

                def lines(self, stop):
                    for event in events:
                        yield json.dumps(event)
                    consumed.set()
                    if not process_ended:
                        viewer_closed.wait(5)

                def kill(self):
                    # The gateway is a separate owner, not in the local viewer's group.
                    viewer_closed.set()

                def reap(self):
                    pass

                def code(self):
                    return "-9"

            hermes = connector.Hermes(["unused"], ["unused"], Path(directory))
            def clock():
                return connector.TURN_SECONDS + 1 if consumed.is_set() and not process_ended else 0
            with patch.object(connector.time, "time", side_effect=clock):
                answer = hermes.finish(Viewer())
            self.assertEqual(viewer_closed.is_set(), not process_ended)
            self.assertFalse(gateway_worked.is_set(), "delivery confirmation does not wait for gateway completion")
            let_gateway_work.set()
            gateway.join(5)
            self.assertTrue(gateway_worked.is_set(), "the gateway keeps working independently after its viewer ends")
            return answer

    def test_owned_started_and_queued_receipts_confirm_delivery_not_completion(self):
        for status in ("started", "queued"):
            with self.subTest(status=status):
                self.assertEqual(self.finish([receipt(status=status)]),
                                 ("confirmed", "handed to Hermes; its turn may still be running", ""))

    def test_init_tool_progress_or_admission_without_receipt_is_still_uncertain(self):
        for event in ({"type": "system", "session_id": "s1"},
                      {"type": "tool_use", "name": "tw_git"},
                      {"type": "admission", "status": "started", "session_id": "s1"}):
            with self.subTest(event=event):
                self.assertEqual(self.finish([event])[0], "uncertain")

    def test_malformed_or_conflicting_receipts_cannot_confirm_delivery(self):
        for events in ([receipt(admission="../x")], [receipt(admission=7)],
                       [receipt(session="s1\n")], [receipt(), receipt(admission="other")],
                       [receipt(), receipt(session="other")]):
            with self.subTest(events=events):
                self.assertEqual(self.finish(events)[0], "uncertain")

    def test_queued_to_started_keeps_the_same_owned_receipt(self):
        self.assertEqual(self.finish([receipt(status="queued"), receipt()])[0], "confirmed")

    def test_unexpected_process_death_is_not_reclassified_by_the_timeout_rule(self):
        self.assertEqual(self.finish([receipt()], process_ended=True)[0], "uncertain")

    def test_conflicting_receipt_cannot_be_restored_by_a_later_repeat(self):
        self.assertEqual(self.finish([receipt(), receipt(admission="other"), receipt()])[0], "uncertain")

    def test_receipt_does_not_change_steer_or_redirect_ownership(self):
        seen = {}
        follow_turn(iter([json.dumps(receipt(status="steered"))]), seen, lambda *args: None)
        self.assertNotIn("delivery_receipt", seen)

    def test_helper_never_turns_a_truncated_identity_into_an_exact_receipt(self):
        for sid in ("s" * 128 + "/bad", "s1\n"):
            output = io.StringIO()
            with redirect_stdout(output):
                room_send.announce("started", sid, "a2")
            self.assertEqual(self.finish([json.loads(output.getvalue())])[0], "uncertain")


class CoreDeliveryReceipts(unittest.TestCase):
    def test_delivery_confirmation_does_not_complete_work_or_duplicate_the_message(self):
        world = World()
        self.addCleanup(world.close)
        seat(world)
        flags(world, "connector_effects")
        chat = Chat(world.store)
        token = Feeds(world.store).new_token(world.ws, "connector", by="tests")
        card = world.card()
        world.core.answer(world.patrick(), **world.answer_args(card))
        item = chat.claim(world.ws, token)["items"][0]
        ref = "handed to Hermes; its turn may still be running"
        self.assertEqual(chat.result(world.ws, token, item["id"], "confirmed", ref)["status"], "sent")
        self.assertTrue(world.core.states(card["id"])["bot_told"])
        self.assertFalse(world.core.states(card["id"])["work_done"])
        self.assertTrue(chat.result(world.ws, token, item["id"], "confirmed", ref)["replayed"])
        self.assertEqual(chat.claim(world.ws, token)["items"], [])
        self.assertEqual(world.store.db.execute("SELECT count(*) FROM effect_receipts WHERE outbox_id = ?",
                                                (item["id"],)).fetchone()[0], 1)
