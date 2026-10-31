"""The admission proxy for routes 2 and 4, with fake upstreams and no real key
(06 section 6.3 item 6 and section 10).
"""

import sys as _sys
import unittest as _unittest

if not _sys.platform.startswith("linux"):
    # The brokers are Linux code (Unix sockets with SO_PEERCRED, pwd, O_NOFOLLOW
    # walks, systemd): they run only on the Oracle VM, and these tests run in
    # the VM half of scripts/test-at-head.sh.
    raise _unittest.SkipTest("the brokers run only on the Oracle VM; their tests run there")

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from helpers import policy

from test_policy_service import Clock
from test_policy_service import service as policy_service

from thornswood_brokers.proxy import (Proxy, Server, UpstreamDown, check_environment, load_env, load_tokens,
                                      price, token_digest)

TOKENS = {bot: f"proxy-token-{bot}" for bot in ("bramble", "rift", "sprocket", "echo", "magpie", "wisp")}
ENV = {"TW_CLAUDE_CREDITS_KEY": "real-credits-key", "QWEN_BASE_URL": "http://one.test:8089", "QWEN_API_KEY": "real-qwen-key"}
MODEL = "claude-opus-5-5"


class FakeUpstream:
    def __init__(self):
        self.calls = []
        self.answers = []   # (status, headers, [chunks]) or an exception

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body)})
        a = self.answers.pop(0) if self.answers else (200, {"content-type": "application/json"},
                                                      [json.dumps({"usage": {"input_tokens": 1000, "output_tokens": 100}}).encode()])
        if isinstance(a, Exception):
            raise a
        status, headers, chunks = a
        return status, headers, iter(chunks)


class World:
    def __init__(self, clock=None, db=None):
        self.clock = clock or Clock()
        self.db = db or Path(tempfile.mkdtemp()) / "policy.db"
        self.policy = policy_service(clock=self.clock, db=self.db)
        self.up = FakeUpstream()
        tokens = {b: token_digest(t) for b, t in TOKENS.items()}
        self.proxy = Proxy(policy(), self.ask, dict(ENV), tokens, upstream=self.up,
                           clock=lambda: self.clock().timestamp())

    def ask(self, op, args):
        handler, callers = self.policy.ops()[op]
        assert "tw-proxy" in callers, f"tw-proxy may not call {op}"
        return handler("tw-proxy", json.loads(json.dumps(args)))

    def turn(self, bot="bramble", risk="low", task="triage"):
        self.policy.set_task_risk("hermes", {"bot": "bramble", "task": task, "risk": risk})
        return self.policy.admit("hermes", {"bot": bot, "task": task})

    def credits(self, bot="bramble", body=None, headers=None):
        body = body or {"model": MODEL, "max_tokens": 1000, "messages": [{"role": "user", "content": "triage #3150"}]}
        h = {"x-api-key": TOKENS[bot], "anthropic-version": "2023-06-01", **(headers or {})}
        a = self.proxy.handle("POST", "/credits/v1/messages", h, json.dumps(body).encode())
        out = b"".join(a.body)
        a.close()
        return a, out

    def qwen(self, bot="bramble", body=None, consume=True):
        body = body or {"model": "qwen", "max_tokens": 512, "messages": [{"role": "user", "content": "packet"}]}
        a = self.proxy.handle("POST", "/qwen/v1/chat/completions", {"authorization": f"Bearer {TOKENS[bot]}"},
                              json.dumps(body).encode())
        if not consume:
            return a, None
        out = b"".join(a.body)
        a.close()
        return a, out


class Credits(unittest.TestCase):
    def test_bramble_low_risk_goes_through_with_the_real_key_only(self):
        w = World()
        w.turn()
        a, _ = w.credits()
        self.assertEqual(a.status, 200)
        sent = w.up.calls[0]
        self.assertEqual(sent["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(sent["headers"]["x-api-key"], "real-credits-key")
        self.assertNotIn(TOKENS["bramble"], json.dumps(sent["headers"]))
        self.assertNotIn("authorization", sent["headers"])

    def test_route_2_never_sees_a_worker(self):
        w = World()
        w.policy.admit("hermes", {"bot": "rift", "task": "code"})
        a, out = w.credits(bot="rift")
        self.assertEqual(a.status, 403)
        self.assertIn("not Rift's", out.decode())
        self.assertEqual(w.up.calls, [])

    def test_route_2_never_sees_high_risk_work(self):
        w = World()
        w.turn(risk="high", task="big change")
        a, out = w.credits()
        self.assertEqual(a.status, 403)
        self.assertIn("low-risk", out.decode())
        self.assertEqual(w.up.calls, [])

    def test_no_ticket_no_route(self):
        w = World()
        a, _ = w.credits()
        self.assertEqual(a.status, 403)
        self.assertEqual(w.up.calls, [])

    def test_a_token_that_is_not_a_bots_is_refused(self):
        w = World()
        w.turn()
        a = w.proxy.handle("POST", "/credits/v1/messages", {"x-api-key": "real-credits-key"}, b"{}")
        self.assertEqual(a.status, 401)
        a = w.proxy.handle("POST", "/credits/v1/messages", {}, b"{}")
        self.assertEqual(a.status, 401)

    def test_only_the_two_paths_are_reachable(self):
        w = World()
        w.turn()
        for method, path in (("GET", "/credits/v1/models"), ("POST", "/credits/v1/messages/batches"),
                             ("POST", "/credits/v1/files"), ("POST", "/v1/messages"), ("GET", "/qwen/health"),
                             ("POST", "/credits/../v1/organizations")):
            with self.subTest(path=path):
                a = w.proxy.handle(method, path, {"x-api-key": TOKENS["bramble"]}, b"{}")
                self.assertEqual(a.status, 404)
        self.assertEqual(w.up.calls, [])

    def test_the_one_model_and_no_paid_speed(self):
        w = World()
        w.turn()
        for body, headers in (
                ({"model": "claude-fable-5-1", "messages": []}, {}),
                ({"model": MODEL, "speed": "fast", "messages": []}, {}),
                ({"model": MODEL, "service_tier": "priority", "messages": []}, {}),
                ({"model": MODEL, "messages": []}, {"anthropic-beta": "fast-mode-2026-02-01"})):
            with self.subTest(body=body, headers=headers):
                a, _ = w.credits(body=body, headers=headers)
                self.assertEqual(a.status, 403)
        self.assertEqual(w.up.calls, [])

    def test_usage_is_priced_into_the_estimate(self):
        w = World()
        w.turn()
        w.up.answers.append((200, {"content-type": "application/json"},
                             [json.dumps({"usage": {"input_tokens": 1_000_000, "output_tokens": 100_000,
                                                    "cache_read_input_tokens": 1_000_000}}).encode()]))
        w.credits()
        self.assertAlmostEqual(w.policy.route_state("credits")["spent_usd"], 4.0 + 2.0 + 0.2, places=4)

    def test_streamed_usage_is_read_as_it_passes(self):
        w = World()
        w.turn()
        events = [
            b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":500000,"output_tokens":1}}}\n\n',
            b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":50000}}\n',
            b'\nevent: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        w.up.answers.append((200, {"content-type": "text/event-stream"}, events))
        a, out = w.credits(body={"model": MODEL, "stream": True, "messages": []})
        self.assertEqual(out, b"".join(events))
        self.assertAlmostEqual(w.policy.route_state("credits")["spent_usd"], 2.0 + 1.0, places=4)

    def test_a_credit_refusal_closes_route_2_only(self):
        w = World()
        w.turn()
        w.up.answers.append((400, {"content-type": "application/json"}, [json.dumps(
            {"type": "error", "error": {"type": "invalid_request_error",
                                        "message": "Your credit balance is too low to access the Anthropic API."}}).encode()]))
        a, _ = w.credits()
        self.assertEqual(a.status, 400)
        self.assertFalse(w.policy.route_state("credits")["open"])
        self.assertTrue(w.policy.route_state("max")["open"])
        again, out = w.credits()
        self.assertEqual(again.status, 429)
        self.assertEqual(len(w.up.calls), 1)
        self.assertIn("2026-11-21", out.decode())

    def test_a_limit_on_route_2_closes_its_window_until_the_reset(self):
        w = World()
        w.turn()
        w.up.answers.append((429, {"anthropic-ratelimit-requests-remaining": "0",
                                   "anthropic-ratelimit-requests-reset": "2026-11-02T12:05:00+00:00"},
                             [b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}']))
        w.credits()
        st = w.policy.route_state("credits")
        self.assertEqual(st["closed"], {"requests": "2026-11-02T12:05:00+00:00"})
        self.assertTrue(w.policy.route_state("max")["open"])
        a, _ = w.credits()
        self.assertEqual(a.status, 429)
        self.assertEqual(a.headers["retry-after"], "300")

    def test_the_margin_stops_new_work_before_the_hard_stop(self):
        w = World()
        w.turn()
        w.policy.credit_spend("tw-proxy", {"usd": 191})
        a, _ = w.credits()
        self.assertEqual(a.status, 429)
        self.assertEqual(w.up.calls, [])

    def test_a_restart_keeps_the_estimate_and_the_windows(self):
        w = World()
        w.turn()
        w.policy.credit_spend("tw-proxy", {"usd": 50})
        w.policy.route_limit("tw-proxy", {"route": "qwen", "window": "one"})
        w.policy.db.close()
        w2 = World(clock=w.clock, db=w.db)
        self.assertEqual(w2.policy.route_state("credits")["spent_usd"], 50)
        self.assertFalse(w2.policy.route_state("qwen")["open"])


class Qwen(unittest.TestCase):
    def test_a_packet_goes_to_one_with_its_token(self):
        w = World()
        w.turn()
        a, _ = w.qwen()
        self.assertEqual(a.status, 200)
        self.assertEqual(w.up.calls[0]["url"], "http://one.test:8089/v1/chat/completions")
        self.assertEqual(w.up.calls[0]["headers"]["authorization"], "Bearer real-qwen-key")

    def test_any_bot_but_low_risk_only(self):
        w = World()
        w.policy.admit("hermes", {"bot": "wisp", "task": "engine work"})
        a, _ = w.qwen(bot="wisp")
        self.assertEqual(a.status, 403)
        self.assertEqual(w.up.calls, [])

    def test_qwen_gets_no_tools_and_a_bounded_answer(self):
        w = World()
        w.turn()
        for body in ({"messages": [], "tools": [{"type": "function", "function": {"name": "bash"}}]},
                     {"messages": [], "max_tokens": 100000}):
            with self.subTest(body=body):
                a, _ = w.qwen(body=body)
                self.assertEqual(a.status, 403)
        big = {"messages": [{"role": "user", "content": "x" * 600000}]}
        a, out = w.qwen(body=big)
        self.assertEqual(a.status, 403)
        self.assertIn("at most", out.decode())
        self.assertEqual(w.up.calls, [])

    def test_one_request_at_a_time_across_the_fleet(self):
        w = World()
        w.turn()
        w.up.answers.append((200, {}, [b"{}"]))
        first, _ = w.qwen(consume=False)
        self.assertEqual(first.status, 200)
        # A second request while the first is still answering waits.
        second, out = w.qwen()
        self.assertEqual(second.status, 429)
        self.assertIn("one Qwen request at a time", out.decode())
        b"".join(first.body)
        first.close()
        third, _ = w.qwen()
        self.assertEqual(third.status, 200)

    def test_with_one_off_qwen_work_waits(self):
        w = World()
        w.turn()
        w.up.answers.append(UpstreamDown("ConnectionRefusedError"))
        a, _ = w.qwen()
        self.assertEqual(a.status, 429)
        self.assertFalse(w.policy.route_state("qwen")["open"])
        # The lease went back even though One never answered.
        self.assertEqual(w.ask("lease_acquire", {"name": "qwen", "holder": "probe"})["name"], "qwen")


class Fleet(unittest.TestCase):
    def test_six_bots_at_once_never_pass_the_fleet_limit(self):
        w = World()
        for bot in TOKENS:
            w.policy.set_task_risk("hermes", {"bot": "bramble", "task": f"t-{bot}", "risk": "low"})
            w.policy.admit("hermes", {"bot": bot, "task": f"t-{bot}"})
        statuses = []
        for bot in TOKENS:
            a, _ = w.qwen(bot=bot)
            statuses.append(a.status)
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(len(w.up.calls), 1)


class Setup(unittest.TestCase):
    def test_the_proxy_listens_on_loopback_only(self):
        w = World()
        for listen in ("0.0.0.0:8611", "100.100.1.10:8611", ":8611"):
            with self.subTest(listen=listen), self.assertRaises(ValueError):
                Server(listen, w.proxy)

    def test_anthropic_api_key_is_never_in_its_environment(self):
        self.assertEqual(check_environment({"PATH": "/usr/bin"}), [])
        self.assertTrue(check_environment({"ANTHROPIC_API_KEY": "x"}))

    def test_the_token_file_holds_digests_of_the_six_only(self):
        d = Path(tempfile.mkdtemp())
        good = d / "good.json"
        good.write_text(json.dumps({"bramble": token_digest("t")}))
        self.assertEqual(load_tokens(good, policy()), {"bramble": token_digest("t")})
        for bad in ({"patrick": token_digest("t")}, {"bramble": "plain-token"}):
            p = d / "bad.json"
            p.write_text(json.dumps(bad))
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                load_tokens(p, policy())

    def test_env_file(self):
        p = Path(tempfile.mkdtemp()) / "proxy.env"
        p.write_text('# keys\nexport QWEN_BASE_URL="http://x:1"\nQWEN_API_KEY=abc\n\n')
        self.assertEqual(load_env(p), {"QWEN_BASE_URL": "http://x:1", "QWEN_API_KEY": "abc"})

    def test_cache_writes_without_a_breakdown_count_at_the_dearer_price(self):
        prices = policy().data["routes"][1]["price_usd_per_mtok"]
        self.assertAlmostEqual(price({"cache_creation_input_tokens": 1_000_000}, prices), 8.0)
        self.assertAlmostEqual(price({"cache_creation_input_tokens": 1_000_000,
                                      "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000}}, prices), 5.0)

    def test_over_http_the_answer_streams_back(self):
        """The real server, a real local upstream, chunk by chunk."""

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["content-length"]))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", "11")
                self.end_headers()
                self.wfile.write(b"data: one\n\n")

        up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        w = World()
        w.turn()
        w.proxy.env["QWEN_BASE_URL"] = f"http://127.0.0.1:{up.server_address[1]}"
        from thornswood_brokers.proxy import http_upstream
        w.proxy.upstream = http_upstream
        srv = Server("127.0.0.1:0", w.proxy)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/qwen/v1/chat/completions",
                                         data=json.dumps({"messages": []}).encode(), method="POST",
                                         headers={"authorization": f"Bearer {TOKENS['bramble']}",
                                                  "content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.read(), b"data: one\n\n")
        finally:
            srv.shutdown()
            srv.server_close()
            up.shutdown()
            up.server_close()


if __name__ == "__main__":
    unittest.main()
