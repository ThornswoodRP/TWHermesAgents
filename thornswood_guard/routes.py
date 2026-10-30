"""The model order, and the check that a Bot's config cannot spend money.

Order (Patrick, 29 Oct): the Claude Max 20x login, then Max's included API
credits (Bramble and low-risk work only), then ChatGPT Plus, then local Qwen
on One (low-risk only, and only while One is on). When no route is open the
work waits; it never moves to a paid provider.
"""

from __future__ import annotations

from .policy import Policy, Verdict


def chain_for(policy: Policy, bot: str, *, proxy_ready: bool = False) -> list[dict]:
    """The routes a Bot may use, in order. Routes behind the admission proxy
    are left out until the proxy exists and passes its tests."""
    out = []
    for r in policy.data["routes"]:
        who = r["who"]
        if who != "all" and bot not in who:
            continue
        if r.get("needs") == "admission-proxy" and not proxy_ready:
            continue
        out.append(r)
    return out


def config_for(policy: Policy, bot: str, *, proxy_ready: bool = False) -> dict:
    """The model part of a Bot's Hermes config, built from its chain."""
    chain = chain_for(policy, bot, proxy_ready=proxy_ready)
    first, rest = chain[0], chain[1:]
    return {
        "model": {"provider": first["provider"], "default": first["model"]},
        "fallback_providers": [{"provider": r["provider"], "model": r["model"]} for r in rest],
    }


def route_allowed(policy: Policy, bot: str, route_id: str, risk: str) -> Verdict:
    for r in policy.data["routes"]:
        if r["id"] != route_id:
            continue
        if r["who"] != "all" and bot not in r["who"]:
            return Verdict.no(f"route {route_id} is not {policy.bot_name(bot)}'s")
        if r["risk"] == "low" and risk != "low":
            return Verdict.no(f"route {route_id} takes low-risk work only")
        return Verdict.yes()
    return Verdict.no(f"{route_id} is not one of the four routes; the work waits")


def next_route(policy: Policy, bot: str, risk: str, closed: set[str], *,
               one_on: bool, proxy_ready: bool) -> str | None:
    """The first open route this Bot may use for this work, or None to wait."""
    for r in policy.data["routes"]:
        if r["id"] in closed:
            continue
        if r.get("needs") == "admission-proxy" and not proxy_ready:
            continue
        if r.get("needs_one") and not one_on:
            continue
        if route_allowed(policy, bot, r["id"], risk).allowed:
            return r["id"]
    return None


def credits_open(policy: Policy, spent_this_cycle_usd: float) -> Verdict:
    """Route 2 admits new work only while the cycle's estimated spend leaves
    the margin. The estimate is the queue's own; the hard stop from Anthropic
    ("credit balance is too low") closes the route whatever it says."""
    r = next(r for r in policy.data["routes"] if r["id"] == "credits")
    left = r["monthly_usd"] - spent_this_cycle_usd
    if left < r["stop_below_usd"]:
        return Verdict.no(f"about ${left:.2f} of this cycle's ${r['monthly_usd']} credit is left; route 2 waits for the credit on day {r['refresh_day']}")
    return Verdict.yes(f"about ${left:.2f} left this cycle")


def check_config(policy: Policy, bot: str, config: dict, env_names: set[str]) -> list[str]:
    """Every way this profile's config or .env could reach a route it may not
    use, or a paid one. Empty means clean."""
    problems = []
    paid = set(policy.data["paid_providers"])
    allowed = {(r["provider"], r.get("model")) for r in chain_for(policy, bot)}

    model = config.get("model") or {}
    if isinstance(model, str):
        problems.append("model is a bare string; it must name its provider")
        model = {}
    pair = (model.get("provider"), model.get("default") or model.get("model"))
    if pair not in allowed:
        problems.append(f"the main model {pair[0]}/{pair[1]} is not in {policy.bot_name(bot)}'s route list")
    if model.get("base_url"):
        problems.append("the main model sets base_url; routes go to their own provider or the admission proxy")
    for i, fb in enumerate(config.get("fallback_providers") or []):
        if (fb.get("provider"), fb.get("model")) not in allowed:
            problems.append(f"fallback {i + 1} {fb.get('provider')}/{fb.get('model')} is not in the route list")
    want = [(r["provider"], r["model"]) for r in chain_for(policy, bot)]
    got = [pair] + [(fb.get("provider"), fb.get("model")) for fb in config.get("fallback_providers") or []]
    if got != want:
        problems.append(f"the route order is {got}, not {want}")
    if config.get("fallback_model"):
        problems.append("the legacy fallback_model key is set; only fallback_providers is used")

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                here = f"{path}.{k}" if path else k
                if k == "provider" and isinstance(v, str) and v in paid:
                    problems.append(f"{here} names {v}, a provider that bills by use")
                if k in ("api_key", "base_url") and isinstance(v, str) and v:
                    problems.append(f"{here} is set; keys never sit in config.yaml and no helper gets its own endpoint")
                walk(v, here)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
    walk(config, "")

    agent = config.get("agent") or {}
    if agent.get("service_tier") not in ("", None, "normal"):
        problems.append("agent.service_tier asks for a faster, dearer tier")
    aux = config.get("auxiliary") or {}
    if aux.get("free_only") is not True:
        problems.append("auxiliary.free_only must be true, so no helper call reaches a paid OpenRouter lane")
    if aux.get("openrouter_model"):
        problems.append("auxiliary.openrouter_model is set")

    if "ANTHROPIC_API_KEY" in env_names:
        problems.append("ANTHROPIC_API_KEY is set; Hermes would spend the credits ahead of the subscription")
    return problems
