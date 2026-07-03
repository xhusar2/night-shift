"""Wake-time resolution and the session-guard deadline.

A Claude 5h session window opens on the first message and lasts 5 hours.
To hand the user a 100% full window at wake time, night-shift hard-stops
ALL activity at wake - 5h - margin (strict mode: covers waking early).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .config import Config, VALID_DAYS, _parse_hhmm

SESSION_WINDOW = timedelta(hours=5)


def resolve_wake(cfg: Config, until: str | None, now: datetime | None = None) -> datetime:
    """Return the next wake time as a local, naive datetime."""
    now = now or datetime.now()

    if until:
        return _parse_until(until, now)

    if not cfg.schedule:
        raise ValueError("no [schedule] in config and no --until given")

    # Find the earliest configured wake time strictly after now, within 8 days.
    for offset in range(8):
        day = now + timedelta(days=offset)
        key = VALID_DAYS[day.weekday()]
        if key not in cfg.schedule:
            continue
        h, m = _parse_hhmm(cfg.schedule[key])
        candidate = day.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate > now:
            return candidate
    raise ValueError("schedule has no upcoming wake time")


def _parse_until(until: str, now: datetime) -> datetime:
    until = until.strip()

    m = re.fullmatch(r"\+(\d+)([hm])", until)
    if m:  # relative: +8h, +90m
        amount = int(m.group(1))
        delta = timedelta(hours=amount) if m.group(2) == "h" else timedelta(minutes=amount)
        return now + delta

    m = re.fullmatch(r"(\d{1,2}):(\d{2})", until)
    if m:  # next occurrence of HH:MM
        h, mm = int(m.group(1)), int(m.group(2))
        candidate = now.replace(hour=h, minute=mm, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    try:  # full datetime, e.g. "2026-07-04 07:00"
        return datetime.fromisoformat(until)
    except ValueError:
        raise ValueError(
            f"can't parse --until {until!r}; use HH:MM, +Nh/+Nm, or YYYY-MM-DD HH:MM"
        ) from None


def work_deadline(wake: datetime, cfg: Config) -> datetime:
    return wake - SESSION_WINDOW - timedelta(minutes=cfg.session_guard_margin_minutes)
