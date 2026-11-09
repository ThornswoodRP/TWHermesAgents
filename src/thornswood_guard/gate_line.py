"""Read a deploy gate line the way the gate on the box reads it.

This mirrors Split-ThornswoodGateLine and Test-ThornswoodGateRequest in
Thornswood's scripts/vps/gate/GateRules.ps1, so Bramble's request is refused
on Oracle for the same reasons the box would refuse it. The box still checks
again; this is the first of two locks, not a replacement for the second.
"""

from __future__ import annotations

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
    given: dict[str, str | bool] = {}
    i = 1
    while i < len(words):
        w = words[i]
        m = re.fullmatch(r"-([A-Za-z]{1,40})", w)
        if not m:
            raise GateLineError(f"{w} is not a parameter name")
        p = m.group(1)
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
    return name, given
