"""Which cards may push to Patrick's phone, and what the push says.

Only a real decision pushes. Each card revision pushes once. The lock screen
says only "Thornswood needs you", and the tap opens that exact card.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .policy import Policy, Verdict


@dataclass
class Card:
    id: str
    kind: str          # decision, main-approval, box-change, report, progress, ...
    revision: int
    title: str
    answered: bool = False


class PushGate:
    def __init__(self, policy: Policy, sent_file: Path | str):
        self.policy = policy
        self.sent_file = Path(sent_file)
        self._sent: set[str] = set()
        if self.sent_file.exists():
            self._sent = set(json.loads(self.sent_file.read_text(encoding="utf-8")))

    def _key(self, card: Card) -> str:
        return f"{card.id}@{card.revision}"

    def decide(self, card: Card) -> Verdict:
        n = self.policy.data["notifications"]
        if card.kind not in n["push_kinds"]:
            return Verdict.no(f"a {card.kind} card does not push; only a decision for Patrick does")
        if card.answered:
            return Verdict.no("the card is already answered")
        if self._key(card) in self._sent:
            return Verdict.no(f"revision {card.revision} of this card already pushed")
        return Verdict.yes()

    def payload(self, card: Card) -> dict:
        n = self.policy.data["notifications"]
        return {
            "title": n["lock_screen_text"],
            "body": "",
            "url": n["link"].format(card_id=card.id, revision=card.revision),
            "tag": self._key(card),
        }

    def mark_sent(self, card: Card) -> None:
        self._sent.add(self._key(card))
        self.sent_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.sent_file.parent, prefix=".sent-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sorted(self._sent), f)
        os.replace(tmp, self.sent_file)
