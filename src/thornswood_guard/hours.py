"""When the workers start new work: policy.yaml's work section, read by the
free worker look (owed_wake.py) and the decision core's assign_task."""

from __future__ import annotations

# 7 AM to midnight Mountain, while the Grok lanes audit overnight (Patrick, 17 Nov 2026).
DEFAULT = (7, 24)
ALL_DAY = (0, 24)


def work_hours(data: dict | None) -> tuple[int, int]:
    """[start, end) in Mountain hours. Around the clock while the lanes' audits
    are paused (work.lanes_audit_overnight false)."""
    work = (data or {}).get("work") or {}
    if work.get("lanes_audit_overnight", True) is False:
        return ALL_DAY
    hours = work.get("hours") or DEFAULT
    return int(hours[0]), int(hours[1])


def paused(data: dict | None) -> bool:
    """Whether Patrick has the Grok lanes' overnight audits paused."""
    return ((data or {}).get("work") or {}).get("lanes_audit_overnight", True) is False


def rule(data: dict | None) -> str:
    """When the workers start new work, as one line every wake of Bramble's
    carries, so it never has to guess whether the lanes' audits are paused
    (12 Nov 2026: twice at night a sweep with no free worker lines said the
    lanes were not paused, while they were)."""
    if paused(data):
        return ("The Grok lanes' audits are paused (policy.yaml work.lanes_audit_overnight is false), so the "
                "workers start new work at any hour: bramble-duties section 6 runs at night as by day.")
    return (f"The workers start new work only from {says(work_hours(data))} Mountain; overnight is the requested "
            "audit window, and a worker on an issue at the end of the day finishes it. You do not stop: every "
            "other duty of yours (merges, Main cards, runs, the Desk's list) is yours at any hour.")


def says(hours: tuple[int, int]) -> str:
    """The hours as people say them: "7 AM to midnight", or "at any hour"."""
    if hours == ALL_DAY:
        return "at any hour"

    def h(n: int) -> str:
        n %= 24
        if n == 0:
            return "midnight"
        return f"{12 if n % 12 == 0 else n % 12} {'AM' if n < 12 else 'PM'}"
    return f"{h(hours[0])} to {h(hours[1])}"
