"""When the workers start new work: policy.yaml's work section, read by the
free worker look (owed_wake.py) and the decision core's assign_task."""

from __future__ import annotations

# 6 AM to 11 PM Mountain, while the Grok lanes audit overnight (Patrick, 9 Nov 2026).
DEFAULT = (6, 23)
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
    return (f"The workers start new work only from {says(work_hours(data))} Mountain; the Grok lanes audit "
            "overnight, and a worker on an issue at the end of the day finishes it.")


def says(hours: tuple[int, int]) -> str:
    """The hours as people say them: "6 AM to 11 PM", or "at any hour"."""
    if hours == ALL_DAY:
        return "at any hour"

    def h(n: int) -> str:
        n %= 24
        return f"{12 if n % 12 == 0 else n % 12} {'AM' if n < 12 else 'PM'}"
    return f"{h(hours[0])} to {h(hours[1])}"
