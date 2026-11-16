"""Read a deploy gate line the way the gate on the box reads it.

This mirrors Split-ThornswoodGateLine and Test-ThornswoodGateRequest in
Thornswood's scripts/vps/gate/GateRules.ps1, so Bramble's request is refused
on Oracle for the same reasons the box would refuse it. The box still checks
again; this is the first of two locks, not a replacement for the second.
"""

from __future__ import annotations

import hashlib
import re


class GateLineError(Exception):
    pass


def split_line(line: str) -> list[str]:
    if line is None:
        line = ""
    if len(line) > 600:
        raise GateLineError("the line is over 600 characters")
    if re.search(r"[^\x20-\x7E]", line):
        raise GateLineError("the line holds a character outside plain printable ASCII")
    if re.search(r"[\\`$<>|]", line):
        raise GateLineError("the line holds a backslash, backtick, dollar sign, angle bracket or pipe")
    words: list[str] = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == " ":
            i += 1
            continue
        if c == '"':
            end = line.find('"', i + 1)
            if end < 0:
                raise GateLineError("a value opens a double quote and never closes it")
            after = end + 1
            if after < len(line) and line[after] != " ":
                raise GateLineError("a closing double quote is followed by more than a space")
            words.append(line[i + 1:end])
            i = after
            continue
        end = line.find(" ", i)
        if end < 0:
            end = len(line)
        word = line[i:end]
        if '"' in word:
            raise GateLineError("a double quote stands inside a word")
        words.append(word)
        i = end
    return words


def parse_request(line: str, verbs: dict) -> tuple[str, dict[str, str | bool]]:
    """Return the verb and its parameters, or raise with the gate's reason."""
    words = split_line(line)
    if not words:
        raise GateLineError("the line is empty: it names no verb")
    name = words[0]
    if name == "ping":
        if len(words) > 1:
            raise GateLineError("ping takes nothing after it")
        return "ping", {}
    known = verbs.get("verbs") or {}
    if name not in known:
        raise GateLineError(f"the gate has no verb {name}")
    rules = {str(k): str(v) for k, v in (known[name].get("params") or {}).items()}
    # A live verb ("build": true) takes -Build, main's full commit, as the
    # gate's own word: the broker runs it only when main is at exactly that
    # commit, and -Build never reaches the script (GateRules.ps1). Until
    # 16 Nov 2026 this read had no such word, so a live line was refused here
    # as taking a parameter its verb does not have (Ops #289).
    held = known[name].get("build") is True
    given: dict[str, str | bool] = {}
    i = 1
    while i < len(words):
        w = words[i]
        # A name may end in digits, as Ship's -Detail2 and -Detail3 do (Thornswood #5144).
        m = re.fullmatch(r"-([A-Za-z][A-Za-z0-9]{0,39})", w)
        if not m:
            raise GateLineError(f"{w} is not a parameter name")
        p = m.group(1)
        if held and p == "Build":
            if "Build" in given:
                raise GateLineError("-Build is given twice")
            if i + 1 >= len(words) or not re.fullmatch(r"[0-9a-f]{40}", words[i + 1]):
                raise GateLineError(f"-Build takes the full 40 character commit of main that {name} is to run")
            given["Build"] = words[i + 1]
            i += 2
            continue
        if p not in rules:
            raise GateLineError(f"{name} through the gate does not take -{p}")
        if p in given:
            raise GateLineError(f"-{p} is given twice")
        rule = rules[p]
        if rule == "switch":
            given[p] = True
            i += 1
            continue
        if i + 1 >= len(words):
            raise GateLineError(f"-{p} needs a value")
        val = words[i + 1]
        pattern = verbs.get("text", "") if rule == "text" else rule
        if val.startswith("-") or not re.search(pattern, val):
            raise GateLineError(f"the value given for -{p} is not one {name} takes through the gate")
        given[p] = val
        i += 2
    if held and "Build" not in given:
        raise GateLineError(f"{name} changes what people play on, so it runs only for the exact build Patrick gave "
                            "his word for: add -Build and main's full commit")
    return name, given


def is_live(verb: str, verbs: dict) -> bool:
    """A verb verbs.json marks "build": true, one that changes what people play on."""
    return ((verbs.get("verbs") or {}).get(verb) or {}).get("build") is True


def line_digest(line: str) -> str:
    """What a live Main card binds: the sha256 of the exact gate line, as the
    card's release link names it (thornswood_decisions/core.py)."""
    return hashlib.sha256((line or "").encode("utf-8")).hexdigest()
