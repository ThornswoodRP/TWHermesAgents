"""Whether a new issue's title repeats an open or just-closed one.

Patrick, 15 Nov 2026: "If something is already done or already an issue it
should not be an issue or an extra one in any of our repos". Thornswood's
duplicate-check.yml only comments on a repeat after it is filed, and Bots
file through tw_git, so a repeat a Bot files is found by a person later, if
at all. tw_git open_issue asks this first and refuses a repeat, naming the
issue it repeats.

The match is Thornswood's scripts/ops/Find-Duplicate.js, ported as it is:
titles only (a title there is a whole sentence about the problem), each
title its words less the small ones and the short numbers, a plain ending
taken off an ordinary word, each word weighted by how few titles have it.
Two titles score from 0 to 1, and one is named at THRESHOLD or more with at
least two words shared. Its header holds the measurement behind 0.5: of 18
repeats found by hand among 1,795 Thornswood issues, 0.5 named 15, and it
commented on one filing in 43. Change the rule there and here together.
"""

from __future__ import annotations

import math
import re

THRESHOLD = 0.5
MOST = 3

SMALL = set((
    "a an the and or of to in on for is are be was were it its this that these those with from at by as "
    "not no can cannot does do when what which who how so but if then than into out up off one two every each all any some "
    "only still now there here their they them we our us you your has have had after before about over under per via just "
    "also more most get gets make makes made say says said").split())


def words_of(title: str) -> list[str]:
    """A title's words, once each, in the order they first come."""
    out: list[str] = []
    text = re.sub(r"[`'\"’]", "", str(title or "").lower())
    for w in re.split(r"[^a-z0-9]+", text):
        if len(w) < 2 or w in SMALL or re.fullmatch(r"\d{1,2}", w):
            continue
        if re.fullmatch(r"[a-z]{4,}", w):
            m = re.search(r"(ing|ed|s)$", w)
            if m and len(w) - len(m.group(1)) >= 3:
                w = w[: -len(m.group(1))]
        if w not in out:
            out.append(w)
    return out


def likely_repeats(title: str, others: list[dict]) -> list[dict]:
    """Each of others ({number, title, ...}) that title may repeat, best first,
    at most MOST, each with its score added."""
    docs = [(o, words_of(o.get("title", ""))) for o in others]
    mine = words_of(title)
    count: dict[str, int] = {}
    for ws in [w for _, w in docs] + [mine]:
        for w in ws:
            count[w] = count.get(w, 0) + 1
    n = len(docs) + 1

    def weight(w: str) -> float:
        return math.log((n + 1) / (count.get(w, 0) + 1))

    def size(ws: list[str]) -> float:
        return math.sqrt(sum(weight(w) ** 2 for w in ws))

    mine_size = size(mine)
    found = []
    for o, ws in docs:
        theirs = set(ws)
        shared = [w for w in mine if w in theirs]
        both = mine_size * size(ws)
        score = sum(weight(w) ** 2 for w in shared) / both if both else 0.0
        if len(shared) >= 2 and score >= THRESHOLD:
            found.append(dict(o, score=round(score, 2)))
    found.sort(key=lambda f: (-f["score"], f["number"]))
    return found[:MOST]
