"""The check Hermes runs before every tool call a Bot makes (pre_tool_call).

The Bot is the Hermes profile the call runs in (ctx.profile_name), never a
name the model wrote. This is the guard Hermes itself can enforce today, while
the Bots are research, triage and edit only (Tier A). It is not the boundary
the plan relies on: every Bot runs as the one `hermes` user, so the real
boundary is the brokers running as their own users (phase 2). Until they
exist, no Bot holds any GitHub credential, and this guard refuses every
GitHub write with the reason, so a refusal reads as a stop, not a failure.
"""

from __future__ import annotations

import re
import shlex

from .policy import Policy, Verdict

TOOLS_OF = {
    "browser": re.compile(r"^browser_"),
    "code_execution": re.compile(r"^execute_code$"),
    "computer_use": re.compile(r"^computer_use$"),
    "connections": re.compile(r"^manage_connections$"),
    "catalog": re.compile(r"^manage_catalog$"),
    "image_gen": re.compile(r"^image_generate$"),
    "video_gen": re.compile(r"^(video_generate|xai_video_\w+)$"),
    "tts": re.compile(r"^text_to_speech$"),
    "web": re.compile(r"^web_(search|extract)$"),
    "search": re.compile(r"^web_search$"),
    "x_search": re.compile(r"^x_search$"),
    "discord": re.compile(r"^discord$"),
    "discord_admin": re.compile(r"^discord_admin$"),
    "delegation": re.compile(r"^delegate_task$"),
    "cronjob": re.compile(r"^cronjob(_manage)?$"),
}

FILE_WRITE_TOOLS = {"write_file", "patch"}
FILE_READ_TOOLS = {"read_file", "search_files"}
SHELL_TOOLS = {"terminal"}

# Places a Bot never reads or writes with a file tool or a shell command:
# Hermes's own home (sign-ins, keys, every profile's config, this guard),
# process environments, and the brokers' sockets and state.
SECRET_PLACES = [
    (re.compile(r"(^|[\s/'\"=:~])\.hermes(/|\b)"), "Hermes's own home holds the sign-ins, the keys and this guard"),
    (re.compile(r"HERMES_HOME|HERMES_MANAGED_DIR"), "Hermes's own home holds the sign-ins, the keys and this guard"),
    (re.compile(r"(^|[\s/'\"=:])auth\.json\b"), "auth.json holds the model sign-ins"),
    (re.compile(r"(^|[\s/'\"=:])\.env(\s|$|['\";|&)])"), ".env files hold keys"),
    (re.compile(r"/proc/[^\s]*environ|\bprintenv\b|^\s*env\s*$|^\s*env\s+-0|\bexport\s+-p\b"), "a process environment can hold keys"),
    (re.compile(r"/srv/tw/(hermes|policy|git)|/run/tw-|tw-git\.sock|tw-policy\.sock"), "the brokers' state and sockets are theirs alone"),
    (re.compile(r"(^|\s)(sudo|su|doas|pkexec)\s"), "no Bot raises its privileges"),
    (re.compile(r"\b(systemctl|crontab|loginctl|journalctl)\b"), "services and timers are Patrick's"),
    (re.compile(r"(^|[\s;&|(])hermes\s"), "a Bot never changes Hermes, its own config, tools, skills or plugins"),
    (re.compile(r"(^|[\s;&|(])(ssh|scp|sftp|rsync)\s"), "a Bot reaches no other machine"),
]

CHAINING = re.compile(r"[;&|`\n]|\$\(|\b(eval|exec|source)\b|\b(bash|sh|zsh|python3?|node|pwsh|perl|ruby)\s+-[a-zA-Z]*c\b")

GH_READ = {
    ("pr", "view"), ("pr", "list"), ("pr", "diff"), ("pr", "checks"), ("pr", "status"),
    ("issue", "view"), ("issue", "list"), ("issue", "status"),
    ("run", "view"), ("run", "list"), ("run", "watch"),
    ("workflow", "list"), ("workflow", "view"),
    ("repo", "view"), ("release", "list"), ("release", "view"),
    ("label", "list"), ("search", "issues"), ("search", "prs"), ("search", "code"),
    ("auth", "status"),
}
GH_API_WRITE_FLAGS = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}

BROKER = "GitHub writes go through the git broker (tw-git), which checks them against the guard rails; no Bot holds a GitHub credential"


def _gh_verdict(words: list[str]) -> Verdict:
    rest = words[1:]
    if not rest:
        return Verdict.yes()
    if rest[0] == "api":
        args = rest[1:]
        method = "GET"
        for i, a in enumerate(args):
            if a in ("-X", "--method") and i + 1 < len(args):
                method = args[i + 1].upper()
            elif a.startswith("--method="):
                method = a.split("=", 1)[1].upper()
        writes = [a for a in args if a in GH_API_WRITE_FLAGS or a.split("=", 1)[0] in GH_API_WRITE_FLAGS]
        if method == "GET" and not [a for a in writes if a not in ("-X", "--method")]:
            return Verdict.yes("a GitHub read")
        return Verdict.no(BROKER)
    if len(rest) >= 2 and (rest[0], rest[1]) in GH_READ:
        return Verdict.yes("a GitHub read")
    return Verdict.no(BROKER)


def _git_verdict(words: list[str]) -> Verdict:
    sub = [w for w in words[1:] if not w.startswith("-")]
    if not sub:
        return Verdict.yes()
    verb = sub[0]
    if verb == "push":
        return Verdict.no(BROKER)
    if verb == "remote" and len(sub) > 1 and sub[1] in ("add", "set-url", "rename", "remove", "rm"):
        return Verdict.no("remotes are set by the git broker")
    if verb == "config" and any(k in " ".join(words) for k in ("credential", "url.", "core.hooksPath", "core.fsmonitor", "filter.", "protocol.", "textconv")):
        return Verdict.no("git settings that run code or carry credentials are set by the git broker")
    if verb == "submodule" and "update" in sub:
        return Verdict.no("submodules are fetched by the git broker")
    return Verdict.yes()


def check_shell(command: str) -> Verdict:
    for pattern, reason in SECRET_PLACES:
        if pattern.search(command):
            return Verdict.no(reason)
    githubish = re.search(r"(^|[^\w.-])(gh|git)\s|api\.github\.com|github\.com/.+/(pulls|merges|dispatches)", command)
    may_write = re.search(r"(^|[^\w.-])gh\s|\bgit\b.*\b(push|remote|config|submodule)\b|api\.github\.com", command, re.S)
    if may_write and CHAINING.search(re.sub(r"\d?>&\d", "", command)):
        return Verdict.no("a command that touches git or GitHub runs on its own, with no chaining, pipes or inline scripts, so the guard can read it whole")
    if re.search(r"\b(curl|wget|http|httpie|Invoke-WebRequest|Invoke-RestMethod)\b", command) and "github.com" in command:
        if re.search(r"(\s-X\s*(POST|PUT|PATCH|DELETE)|\s--request\s|\s-d\b|\s--data|\s-F\b|\s--form|\s--upload-file|\s-T\b)", command, re.I):
            return Verdict.no(BROKER)
        if re.search(r"(?i)authorization|token", command):
            return Verdict.no("no Bot holds or sends a GitHub token")
    if not githubish:
        return Verdict.yes()
    try:
        words = shlex.split(command)
    except ValueError:
        return Verdict.no("the guard cannot read this command whole (unbalanced quotes)")
    if not words:
        return Verdict.yes()
    head = words[0].rsplit("/", 1)[-1]
    if head == "gh":
        return _gh_verdict(words)
    if head == "git":
        return _git_verdict(words)
    return Verdict.yes()


def check_path(path: str) -> Verdict:
    for pattern, reason in SECRET_PLACES[:6]:
        if pattern.search(" " + path):
            return Verdict.no(reason)
    return Verdict.yes()


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _strings(v)


def check_tool(policy: Policy, bot: str, tool_name: str, args: dict) -> Verdict:
    if not policy.is_bot(bot):
        return Verdict.no(f"the Thornswood guard is loaded in profile '{bot}', which is not one of the six Bots; every tool is refused until that is fixed")
    never = list(policy.data["toolsets"]["never"])
    if not policy.is_router(bot):
        never += policy.data["toolsets"]["workers_never"]
    elif not policy.bots[bot].get("cron"):
        never.append("cronjob")
    for toolset in never:
        rx = TOOLS_OF.get(toolset)
        if rx and rx.match(tool_name):
            return Verdict.no(f"{policy.bot_name(bot)} has no {toolset} tools")
    args = args or {}
    if tool_name == "skill_manage":
        if any("thornswood" in s.lower() for s in _strings(args)):
            return Verdict.no("the thornswood-ops and thornswood-guide skills change only by a reviewed PR to their source, never by a Bot")
        return Verdict.yes()
    if tool_name in FILE_WRITE_TOOLS or tool_name in FILE_READ_TOOLS:
        for key, value in args.items():
            if not re.search(r"path|file|dir|root|target", key, re.I):
                continue
            for s in _strings(value):
                v = check_path(s)
                if not v.allowed:
                    return v
        return Verdict.yes()
    if tool_name in SHELL_TOOLS:
        command = args.get("command") or ""
        if not isinstance(command, str):
            return Verdict.no("the terminal command is not plain text")
        workdir = args.get("workdir")
        if isinstance(workdir, str) and workdir:
            v = check_path(workdir)
            if not v.allowed:
                return v
        return check_shell(command)
    return Verdict.yes()
