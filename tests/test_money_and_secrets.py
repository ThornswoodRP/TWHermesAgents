"""The model order with no extra spend, what may sit on the Oracle box, the
decision records. The phone push's rules are the decision core's
(src/thornswood_decisions/push.py, tests/test_decisions_push.py)."""

import tempfile
import unittest
from pathlib import Path

from helpers import APP, HEAD, decide, policy, store

from thornswood_guard.decisions import new_decision
from thornswood_guard.profile_config import build, merge_into
from thornswood_guard.routes import chain_for, check_config, credits_open, next_route
from thornswood_guard.secrets_check import check_env, check_files, env_names

WORKERS = ("rift", "sprocket", "echo", "magpie", "wisp")


class Routes(unittest.TestCase):
    def setUp(self):
        self.p = policy()

    def ids(self, bot, **kw):
        return [r["id"] for r in chain_for(self.p, bot, **kw)]

    def test_order_today_is_max_then_chatgpt_then_qwen(self):
        """Qwen's route is ready (8 Nov 2026); the credits route is not."""
        for bot in ("bramble",) + WORKERS:
            self.assertEqual(self.ids(bot), ["max", "chatgpt", "qwen"])

    def test_once_the_proxy_exists_only_bramble_gets_the_credits(self):
        self.assertEqual(self.ids("bramble", proxy_ready=True), ["max", "credits", "chatgpt", "qwen"])
        for bot in WORKERS:
            self.assertEqual(self.ids(bot, proxy_ready=True), ["max", "chatgpt", "qwen"])

    def test_19_route_one_out(self):
        kw = dict(one_on=True, proxy_ready=True)
        self.assertEqual(next_route(self.p, "bramble", "low", {"max"}, **kw), "credits")
        self.assertEqual(next_route(self.p, "bramble", "high", {"max"}, **kw), "chatgpt")
        for bot in WORKERS:
            self.assertEqual(next_route(self.p, bot, "low", {"max"}, **kw), "chatgpt")

    def test_two_claude_limits_are_apart(self):
        kw = dict(one_on=True, proxy_ready=True)
        self.assertEqual(next_route(self.p, "bramble", "low", {"credits"}, **kw), "max")

    def test_qwen_only_low_risk_and_only_while_one_is_on(self):
        closed = {"max", "credits", "chatgpt"}
        self.assertEqual(next_route(self.p, "wisp", "low", closed, one_on=True, proxy_ready=True), "qwen")
        self.assertIsNone(next_route(self.p, "wisp", "high", closed, one_on=True, proxy_ready=True))
        self.assertIsNone(next_route(self.p, "wisp", "low", closed, one_on=False, proxy_ready=True))

    def test_19_all_out_means_wait_never_a_paid_route(self):
        self.assertIsNone(next_route(self.p, "bramble", "low", {"max", "credits", "chatgpt", "qwen"},
                                     one_on=True, proxy_ready=True))

    def test_every_bots_config_is_clean(self):
        for bot in ("bramble",) + WORKERS:
            self.assertEqual(check_config(self.p, bot, build(self.p, bot), set()), [], bot)

    def test_the_vms_placeholder_config_is_replaced_whole(self):
        # What the six profiles hold on 30 Oct, read from the VM.
        today = {"model": {"default": "anthropic/claude-opus-4.6", "provider": "auto",
                           "base_url": "https://openrouter.ai/api/v1"}, "_config_version": 51}
        self.assertTrue(check_config(self.p, "rift", today, set()))
        merged = merge_into(today, build(self.p, "rift"))
        self.assertEqual(merged["model"], {"provider": "anthropic", "default": "claude-opus-5-5"})
        self.assertEqual(merged["_config_version"], 51)
        self.assertEqual(check_config(self.p, "rift", merged, set()), [])

    def test_paid_routes_are_caught(self):
        cfg = build(self.p, "echo")
        cfg["fallback_providers"].append({"provider": "openrouter", "model": "anthropic/claude-opus-5-5"})
        self.assertTrue(any("bills by use" in x for x in check_config(self.p, "echo", cfg, set())))
        cfg = build(self.p, "echo")
        cfg["auxiliary"]["compression"] = {"provider": "openrouter"}
        self.assertTrue(check_config(self.p, "echo", cfg, set()))
        cfg = build(self.p, "echo")
        cfg["auxiliary"]["free_only"] = False
        self.assertTrue(check_config(self.p, "echo", cfg, set()))
        cfg = build(self.p, "echo")
        cfg["agent"]["service_tier"] = "fast"
        self.assertTrue(check_config(self.p, "echo", cfg, set()))

    def test_helpers_stay_on_the_bots_own_routes(self):
        # 31 Oct: the gateway logged "Nous Portal not configured" from the
        # vision helper's auto list. Every Bot's vision helper is pinned to its
        # first route, and no helper may name a login the Bot was not given.
        for bot in ("bramble", "rift", "sprocket", "echo", "magpie", "wisp"):
            cfg = build(self.p, bot)
            vision = cfg["auxiliary"]["vision"]
            self.assertEqual((vision["provider"], vision["model"]),
                             (cfg["model"]["provider"], cfg["model"]["default"]), bot)
            self.assertNotIn("base_url", vision, bot)
            self.assertEqual(check_config(self.p, bot, cfg, set()), [], bot)
        for provider in ("nous", "deepinfra", "gemini"):
            cfg = build(self.p, "rift")
            cfg["auxiliary"]["compression"] = {"provider": provider}
            self.assertTrue(any("not one of" in x for x in check_config(self.p, "rift", cfg, set())), provider)
        cfg = build(self.p, "rift")
        cfg["auxiliary"]["vision"] = {"provider": "auto", "model": ""}
        self.assertTrue(any("not pinned" in x for x in check_config(self.p, "rift", cfg, set())))
        cfg = build(self.p, "rift")
        cfg["auxiliary"]["vision"]["model"] = "claude-haiku-9"
        self.assertTrue(check_config(self.p, "rift", cfg, set()))
        cfg = build(self.p, "rift")
        cfg["auxiliary"]["vision"]["base_url"] = "https://example.invalid/v1"
        self.assertTrue(any("auxiliary.vision.base_url is set" in x for x in check_config(self.p, "rift", cfg, set())))
        cfg = build(self.p, "rift")
        cfg["auxiliary"]["session_search"] = {"provider": "openai-codex", "model": "gpt-6.1-sol"}
        self.assertEqual(check_config(self.p, "rift", cfg, set()), [])

    def test_the_gateways_own_profile_keeps_to_one_route(self):
        from thornswood_guard.profile_config import build_gateway
        from thornswood_guard.routes import check_gateway_config
        cfg = build_gateway(self.p)
        self.assertEqual(check_gateway_config(self.p, cfg, set()), [])
        self.assertEqual(cfg["fallback_providers"], [])
        bad = build_gateway(self.p)
        bad["fallback_providers"] = [{"provider": "openai-codex", "model": "gpt-6.1-sol"}]
        self.assertTrue(check_gateway_config(self.p, bad, set()))
        bad = build_gateway(self.p)
        bad["auxiliary"]["vision"] = {"provider": "nous", "model": ""}
        self.assertTrue(any("not one of" in x for x in check_gateway_config(self.p, bad, set())))
        # 31 Oct, 3:04 PM: the image and video tools' check asked the pinned
        # helper for a client the unsigned profile cannot have, then walked to
        # the Nous Portal. Those toolsets are off there.
        for toolset in ("vision", "video"):
            self.assertIn(toolset, cfg["agent"]["disabled_toolsets"])
            bad = build_gateway(self.p)
            bad["agent"]["disabled_toolsets"] = [t for t in bad["agent"]["disabled_toolsets"] if t != toolset]
            self.assertTrue(any(f"the {toolset} toolset is on" in x for x in check_gateway_config(self.p, bad, set())))
        for bot in self.p.bots:
            self.assertNotIn("vision", build(self.p, bot)["agent"]["disabled_toolsets"], bot)
        bad = build_gateway(self.p)
        bad["plugins"]["enabled"] = []
        self.assertTrue(any("guard plugin" in x for x in check_gateway_config(self.p, bad, set())))
        self.assertTrue(any("ANTHROPIC_API_KEY" in x for x in check_gateway_config(self.p, cfg, {"ANTHROPIC_API_KEY"})))

    def test_wrong_order_is_caught(self):
        cfg = build(self.p, "wisp")
        cfg["model"], cfg["fallback_providers"] = {"provider": "openai-codex", "default": "gpt-6.1-sol"}, [
            {"provider": "anthropic", "model": "claude-opus-5-5"}]
        self.assertTrue(check_config(self.p, "wisp", cfg, set()))

    def test_credits_stop_at_the_margin_of_200_a_month(self):
        self.assertTrue(credits_open(self.p, 0).allowed)
        self.assertTrue(credits_open(self.p, 82.40).allowed)
        self.assertTrue(credits_open(self.p, 190).allowed)
        v = credits_open(self.p, 190.01)
        self.assertFalse(v.allowed)
        self.assertIn("day 21", v.reason)

    def ready(self, credits=True, qwen=True):
        import copy
        from thornswood_guard.policy import Policy
        data = copy.deepcopy(self.p.data)
        data["admission_proxy"]["ready"] = {"credits": credits, "qwen": qwen}
        return Policy(data)

    def test_proxy_routes_point_at_the_loopback_proxy_with_the_bots_token(self):
        p = self.ready()
        cfg = build(p, "bramble")
        fb = cfg["fallback_providers"]
        self.assertEqual(fb[0], {"provider": "anthropic", "model": "claude-opus-5-5",
                                 "base_url": "http://127.0.0.1:8611/credits", "key_env": "TW_PROXY_TOKEN"})
        self.assertEqual(fb[2]["base_url"], "http://127.0.0.1:8611/qwen/v1")
        self.assertEqual(check_config(p, "bramble", cfg, set()), [])
        self.assertEqual(check_config(p, "rift", build(p, "rift"), set()), [])

    def test_a_proxy_route_cannot_skip_the_proxy_or_point_elsewhere(self):
        p = self.ready()
        for change in ({"base_url": None, "key_env": None}, {"base_url": "https://api.anthropic.com"},
                       {"key_env": "TW_CLAUDE_CREDITS_KEY"}, {"base_url": "http://127.0.0.1:8611/qwen/v1"}):
            with self.subTest(change=change):
                cfg = build(p, "bramble")
                for k, v in change.items():
                    if v is None:
                        cfg["fallback_providers"][0].pop(k, None)
                    else:
                        cfg["fallback_providers"][0][k] = v
                self.assertTrue(check_config(p, "bramble", cfg, set()))
        cfg = build(p, "rift")
        cfg["fallback_providers"][0]["base_url"] = "http://127.0.0.1:8611/credits"
        self.assertTrue(check_config(p, "rift", cfg, set()))

    def test_before_the_proxy_is_ready_no_route_names_it(self):
        p = self.ready(credits=False, qwen=False)
        cfg = build(p, "bramble")
        self.assertNotIn("base_url", str(cfg))
        cfg["fallback_providers"].insert(0, {"provider": "anthropic", "model": "claude-opus-5-5",
                                             "base_url": "http://127.0.0.1:8611/credits", "key_env": "TW_PROXY_TOKEN"})
        self.assertTrue(check_config(p, "bramble", cfg, set()))

    def test_a_route_not_ready_stays_out_while_the_other_is_in(self):
        cfg = build(self.p, "bramble")
        self.assertIn("http://127.0.0.1:8611/qwen/v1", str(cfg))
        self.assertNotIn("/credits", str(cfg))
        cfg["fallback_providers"].insert(0, {"provider": "anthropic", "model": "claude-opus-5-5",
                                             "base_url": "http://127.0.0.1:8611/credits", "key_env": "TW_PROXY_TOKEN"})
        self.assertTrue(check_config(self.p, "bramble", cfg, set()))

    def test_anthropic_api_key_would_spend_the_credits_first(self):
        problems = check_config(self.p, "bramble", build(self.p, "bramble"), {"ANTHROPIC_API_KEY"})
        self.assertTrue(any("ANTHROPIC_API_KEY" in x for x in problems))


class Secrets(unittest.TestCase):
    def setUp(self):
        self.p = policy()
        self.dir = Path(tempfile.mkdtemp())

    def env(self, text):
        f = self.dir / ".env"
        f.write_text(text, encoding="utf-8")
        return env_names(f)

    def test_only_names_are_read(self):
        names = self.env("# comment\nDISCORD_BOT_TOKEN=canary-value-123\nexport DISCORD_ALLOWED_USERS=1\n")
        self.assertEqual(names, {"DISCORD_BOT_TOKEN", "DISCORD_ALLOWED_USERS"})

    def test_the_discord_token_is_the_one_exception_in_the_gateways_env(self):
        self.assertEqual(check_env(self.p, {"DISCORD_BOT_TOKEN", "DISCORD_ALLOWED_USERS"}, "root"), [])
        self.assertTrue(check_env(self.p, {"DISCORD_BOT_TOKEN"}, "echo"))

    def test_never_on_oracle(self):
        for name in ("NEXUS_API_KEY", "TW_NEXUS_DOWNLOAD_KEY", "DESK_KEY_B64", "GITHUB_TOKEN", "GH_TOKEN",
                     "GITHUB_APP_PRIVATE_KEY", "AZURE_CLIENT_ID", "CLOUDFLARE_API_TOKEN", "ANTHROPIC_API_KEY",
                     "OPENAI_API_KEY", "OPENROUTER_API_KEY", "THORNSWOOD_NOTICE_TOKEN", "TW_SIGNING_PASS",
                     "DISCORD_ALLOW_ALL_USERS"):
            self.assertTrue(check_env(self.p, {name}, "root"), name)

    def test_the_gamedata_nexus_key_never_sits_in_hermes_files(self):
        # Its one holder is the tw-gamedata user, outside Hermes's reach.
        holders = [e["user"] for e in self.p.data["secrets"]["oracle_other_users"] if "NEXUS" in e["name"]]
        self.assertEqual(holders, ["tw-gamedata"])
        for where in ("root", "bramble", "rift"):
            self.assertTrue(check_env(self.p, {"TW_GAMEDATA_NEXUS_KEY"}, where), where)

    def test_the_proxy_token_sits_only_in_each_bots_own_profile(self):
        # No real key: it reaches only the loopback proxy, which admits a route once it is ready.
        self.assertEqual(check_env(self.p, {"TW_PROXY_TOKEN"}, "bramble"), [])
        self.assertEqual(check_env(self.p, {"TW_PROXY_TOKEN"}, "wisp"), [])
        self.assertTrue(check_env(self.p, {"TW_PROXY_TOKEN"}, "root"))

    def test_the_routes_real_keys_never_sit_in_hermes_files(self):
        # Only the admission proxy's own user holds them (proxy.env).
        holders = {e["name"]: e["user"] for e in self.p.data["secrets"]["oracle_other_users"]}
        self.assertEqual(holders["TW_CLAUDE_CREDITS_KEY"], "tw-proxy")
        self.assertEqual(holders["QWEN_API_KEY"], "tw-proxy")
        for name in ("TW_CLAUDE_CREDITS_KEY", "QWEN_API_KEY", "TW_QWEN_TOKEN"):
            for where in ("root", "bramble", "rift"):
                self.assertTrue(check_env(self.p, {name}, where), (name, where))

    def test_unknown_secret_shaped_names_are_refused(self):
        for name in ("SOME_NEW_KEY", "BOX_PASSWORD", "X_API_TOKEN", "MY_SECRET", "AWS_SESSION_TOKEN"):
            self.assertTrue(check_env(self.p, {name}, "root"), name)

    def test_hermes_settings_are_not_secrets(self):
        # Seeded into the root .env by the Hermes installer, read on the VM 30 Oct.
        seeded = {"BROWSERBASE_ADVANCED_STEALTH", "BROWSERBASE_PROXIES", "BROWSER_INACTIVITY_TIMEOUT",
                  "BROWSER_SESSION_TIMEOUT", "IMAGE_TOOLS_DEBUG", "MOA_TOOLS_DEBUG",
                  "TERMINAL_LIFETIME_SECONDS", "TERMINAL_MODAL_IMAGE", "TERMINAL_TIMEOUT",
                  "VISION_TOOLS_DEBUG", "WEB_TOOLS_DEBUG"}
        self.assertEqual(check_env(self.p, seeded, "root"), [])

    def test_forbidden_files_by_name(self):
        (self.dir / "a" / "b").mkdir(parents=True)
        (self.dir / "a" / "b" / "signing.json").write_text("{}", encoding="utf-8")
        (self.dir / "a" / "thornswood_ed25519").write_text("x", encoding="utf-8")
        (self.dir / "skip").mkdir()
        (self.dir / "skip" / "signing.json").write_text("{}", encoding="utf-8")
        (self.dir / "a" / "cacert.pem").write_text("public certificates", encoding="utf-8")
        (self.dir / "a" / "thornswood-workers.2026-11-01.private-key.pem").write_text("x", encoding="utf-8")
        found = check_files(self.p, self.dir, {"skip"})
        self.assertEqual(len(found), 3, found)
        self.assertFalse(any("cacert" in f for f in found))


class Decisions(unittest.TestCase):
    def setUp(self):
        self.p = policy()
        self.s = store(self.p)

    def test_only_patrick_answers(self):
        with self.assertRaises(ValueError):
            self.s.record(new_decision(id="x", kind="main", repo=APP, commit=HEAD, revision=1, answered_by="bramble[bot]"))

    def test_a_stale_revision_approves_nothing(self):
        decide(self.s, kind="publish", revision=1)
        self.assertFalse(self.s.check("d1", kind="publish", repo=APP, commit=HEAD, revision=2).allowed)
        self.assertTrue(self.s.check("d1", kind="publish", repo=APP, commit=HEAD, revision=1).allowed)

    def test_21_restore_kills_main_records(self):
        decide(self.s, id="m", kind="main", pr_number=None)
        self.assertTrue(self.s.check("m", kind="main", repo=APP, commit=HEAD).allowed)
        self.assertEqual(self.s.mark_restored(), 1)
        v = self.s.check("m", kind="main", repo=APP, commit=HEAD)
        self.assertFalse(v.allowed)
        self.assertIn("restored", v.reason)

    def test_records_survive_a_restart(self):
        decide(self.s, id="m", kind="main", pr_number=None)
        self.s.use("m")
        from thornswood_guard.decisions import DecisionStore
        again = DecisionStore(self.s.path, self.p)
        self.assertFalse(again.check("m", kind="main", repo=APP, commit=HEAD).allowed)


if __name__ == "__main__":
    unittest.main()
