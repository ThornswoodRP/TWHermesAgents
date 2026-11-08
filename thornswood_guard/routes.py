"""The model order, and the check that a Bot's config cannot spend money.

Order (Patrick, 29 Oct): the Claude Max 20x login, then Max's included API
credits (Bramble and low-risk work only), then ChatGPT Plus, then local Qwen
on One (low-risk only, and only while One is on). When no route is open the
work waits; it never moves to a paid provider.
"""

from __future__ import annotations

from .policy import Policy, Verdict


PROXY_ROUTES = ("credits", "qwen")


def proxy_ready_in(policy: Policy) -> set[str]:
    """The routes behind the admission proxy that a PR has said are ready."""
    ready = (policy.data.get("admission_proxy") or {}).get("ready") or {}
    return {r for r in PROXY_ROUTES if ready.get(r) is True}


def _ready_set(policy: Policy, proxy_ready: bool | set[str] | None) -> set[str]:
    if proxy_ready is None:
        return proxy_ready_in(policy)
    if isinstance(proxy_ready, bool):
        return set(PROXY_ROUTES) if proxy_ready else set()
    return set(proxy_ready)


def proxy_url(policy: Policy, route: dict) -> str:
    """Where a route behind the admission proxy points: the loopback proxy,
    under the route's own path. Anthropic's SDK adds /v1/messages; an
    OpenAI-style provider adds /chat/completions."""
    listen = policy.data["admission_proxy"]["listen"]
    suffix = "" if route["provider"] == "anthropic" else "/v1"
    return f"http://{listen}/{route['id']}{suffix}"


def chain_for(policy: Policy, bot: str, *, proxy_ready: bool | set[str] | None = None) -> list[dict]:
    """The routes a Bot may use, in order. A route behind the admission proxy
    is left out until a PR sets it ready in admission_proxy.ready."""
    ready = _ready_set(policy, proxy_ready)
    out = []
    for r in policy.data["routes"]:
        who = r["who"]
        if who != "all" and bot not in who:
            continue
        if r.get("needs") == "admission-proxy" and r["id"] not in ready:
            continue
        out.append(r)
    return out


def _entry(policy: Policy, r: dict) -> dict:
    e = {"provider": r["provider"], "model": r["model"]}
    if r.get("via") == "admission-proxy":
        # The Bot's own proxy token, never the real key: the proxy adds it.
        e["base_url"] = proxy_url(policy, r)
        e["key_env"] = policy.data["admission_proxy"]["token_env"]
    return e


def config_for(policy: Policy, bot: str, *, proxy_ready: bool | set[str] | None = None) -> dict:
    """The model part of a Bot's Hermes config, built from its chain."""
    chain = chain_for(policy, bot, proxy_ready=proxy_ready)
    first, rest = chain[0], chain[1:]
    return {
        "model": {"provider": first["provider"], "default": first["model"]},
        "fallback_providers": [_entry(policy, r) for r in rest],
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
               one_on: bool, proxy_ready: bool | set[str]) -> str | None:
    """The first open route this Bot may use for this work, or None to wait."""
    ready = _ready_set(policy, proxy_ready)
    for r in policy.data["routes"]:
        if r["id"] in closed:
            continue
        if r.get("needs") == "admission-proxy" and r["id"] not in ready:
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


def gateway_chain(policy: Policy) -> list[dict]:
    """The gateway's own profile (the hermes user's default profile, which the
    gateway runs as and which holds the Discord connection) is not a Bot. It
    gets the first route every Bot shares, with no fallback: the Max login."""
    for r in policy.data["routes"]:
        if r["who"] == "all" and not r.get("needs") and r.get("risk") == "any":
            return [r]
    raise ValueError("no route every Bot shares; the gateway's own profile has nothing to use")


def check_config(policy: Policy, bot: str, config: dict, env_names: set[str]) -> list[str]:
    """Every way this profile's config or .env could reach a route it may not
    use, or a paid one. Empty means clean."""
    return _check(policy, policy.bot_name(bot), chain_for(policy, bot), config, env_names)


def check_gateway_config(policy: Policy, config: dict, env_names: set[str]) -> list[str]:
    """The same rules for the gateway's own profile, against its one route,
    the guard loaded there, which refuses every tool in a profile that is not
    a Bot, and the toolsets whose check asks for a helper turned off."""
    problems = _check(policy, "the gateway's own profile", gateway_chain(policy), config, env_names)
    if "thornswood_guard" not in ((config.get("plugins") or {}).get("enabled") or []):
        problems.append("the guard plugin is not enabled in the gateway's own profile")
    from .profile_config import GATEWAY_HELPER_TOOLSETS
    off = set((config.get("agent") or {}).get("disabled_toolsets") or [])
    for toolset in GATEWAY_HELPER_TOOLSETS:
        if toolset not in off:
            problems.append(f"the {toolset} toolset is on in the gateway's own profile; its check asks for a helper the profile has no sign-in for, and Hermes then walks to the Nous Portal")
    problems += _check_wake(policy, config)
    return problems


def _check_wake(policy: Policy, config: dict) -> list[str]:
    """Bramble's wake (policy `wake`): the gateway's webhook receiver listens
    on loopback only, holds exactly the one route this repository writes, and
    no secret is in the file (it comes from systemd, never from config)."""
    from .profile_config import wake_route
    w = policy.data["wake"]
    hook = ((config.get("platforms") or {}).get("webhook") or {})
    extra = hook.get("extra") or {}
    problems = []
    if hook.get("enabled") is not True:
        problems.append("the gateway's webhook receiver is off, so Bramble never wakes when a check finishes")
    if extra.get("host") != "127.0.0.1":
        problems.append(f"the gateway's webhook receiver listens on {extra.get('host')!r}, not 127.0.0.1; only Tailscale Funnel's one path may reach it")
    if extra.get("port") != w["port"]:
        problems.append(f"the gateway's webhook receiver is on port {extra.get('port')}, not {w['port']}, where Funnel sends Bramble's events")
    if "secret" in extra or any("secret" in (r or {}) for r in (extra.get("routes") or {}).values()):
        problems.append(f"a webhook secret is written in the gateway's config.yaml; it belongs only in {w['secret_file']}, which systemd hands the gateway")
    routes = extra.get("routes") or {}
    if set(routes) != {w["route"]}:
        problems.append(f"the gateway's webhook routes are {sorted(routes)}, not only {w['route']}")
    elif routes[w["route"]] != wake_route(policy):
        problems.append(f"the {w['route']} webhook route differs from what this repository writes")
    return problems


def _check(policy: Policy, who: str, chain: list[dict], config: dict, env_names: set[str]) -> list[str]:
    problems = []
    paid = set(policy.data["paid_providers"])
    allowed = {(r["provider"], r.get("model")) for r in chain}
    proxied = {(r["provider"], r.get("model"), proxy_url(policy, r)) for r in chain if r.get("via") == "admission-proxy"}
    token_env = policy.data["admission_proxy"]["token_env"]

    model = config.get("model") or {}
    if isinstance(model, str):
        problems.append("model is a bare string; it must name its provider")
        model = {}
    pair = (model.get("provider"), model.get("default") or model.get("model"))
    if pair not in allowed:
        problems.append(f"the main model {pair[0]}/{pair[1]} is not in {who}'s route list")
    if model.get("base_url"):
        problems.append("the main model sets base_url; routes go to their own provider or the admission proxy")
    fallbacks = config.get("fallback_providers") or []
    for i, fb in enumerate(fallbacks):
        if (fb.get("provider"), fb.get("model")) not in allowed:
            problems.append(f"fallback {i + 1} {fb.get('provider')}/{fb.get('model')} is not in the route list")
        via_proxy = (fb.get("provider"), fb.get("model"), fb.get("base_url")) in proxied
        if fb.get("base_url") and not via_proxy:
            problems.append(f"fallback {i + 1} points at {fb.get('base_url')}; the only address a route may name is the admission proxy's for that route")
        if via_proxy and fb.get("key_env") != token_env:
            problems.append(f"fallback {i + 1} goes through the admission proxy with key_env {fb.get('key_env')!r}, not {token_env}")
        expected = chain[i + 1] if i + 1 < len(chain) else None
        if expected and expected.get("via") == "admission-proxy" and not via_proxy:
            problems.append(f"fallback {i + 1} {fb.get('provider')}/{fb.get('model')} must go through the admission proxy")
        if fb.get("key_env") and not via_proxy:
            problems.append(f"fallback {i + 1} names key_env {fb.get('key_env')}; only the admission proxy's entries carry a key, the Bot's proxy token")
    want = [(r["provider"], r["model"]) for r in chain]
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
                proxied_entry = path.startswith("fallback_providers[") and path.count(".") == 0 and \
                    (node.get("provider"), node.get("model"), node.get("base_url")) in proxied
                if k == "base_url" and proxied_entry:
                    continue
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
    # A helper names "auto" (the Bot's own main route) or one of the Bot's
    # routes, never a login the Bot was not given.
    route_providers = {r["provider"] for r in chain}
    for task, block in aux.items():
        if not isinstance(block, dict):
            continue
        name = str(block.get("provider") or "").strip().lower()
        if name not in ("", "auto") and name not in route_providers:
            problems.append(f"auxiliary.{task}.provider is {name}, not one of {who}'s routes")
        elif name not in ("", "auto") and block.get("model") and (name, block.get("model")) not in allowed:
            problems.append(f"auxiliary.{task} asks {name} for {block.get('model')}, which is not one of {who}'s routes")
    # Vision's "auto" walks OpenRouter, the Nous Portal and DeepInfra when the
    # main route does not answer, so it is pinned to the Bot's first route.
    vision = aux.get("vision") if isinstance(aux.get("vision"), dict) else {}
    if (vision.get("provider"), vision.get("model")) != pair:
        problems.append(f"auxiliary.vision is {vision.get('provider') or 'auto'}/{vision.get('model') or 'auto'}, not pinned to the main route {pair[0]}/{pair[1]}")

    if "ANTHROPIC_API_KEY" in env_names:
        problems.append("ANTHROPIC_API_KEY is set; Hermes would spend the credits ahead of the subscription")
    return problems
