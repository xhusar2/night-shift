"""Client for the Claude Code OAuth usage endpoint (what /usage shows).

This is an unofficial endpoint; the response is parsed defensively and
`night-shift usage --raw` dumps the raw JSON so breakage is easy to diagnose.
Credentials are re-read on every call because the claude CLI refreshes the
OAuth token as it runs.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


class UsageError(RuntimeError):
    pass


@dataclass
class Window:
    utilization: float  # percent, 0-100
    resets_at: datetime | None  # aware UTC datetime


@dataclass
class Usage:
    five_hour: Window
    seven_day: Window
    raw: dict = field(repr=False, default_factory=dict)


def _access_token() -> str:
    try:
        creds = json.loads(CREDENTIALS_PATH.read_text())
    except FileNotFoundError:
        raise UsageError(f"no Claude credentials at {CREDENTIALS_PATH}") from None
    except json.JSONDecodeError as e:
        raise UsageError(f"unreadable credentials file: {e}") from None
    token = (creds.get("claudeAiOauth") or {}).get("accessToken")
    if not token:
        raise UsageError("no claudeAiOauth.accessToken in credentials file")
    return token


def fetch_raw() -> dict:
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": "night-shift",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise UsageError(f"usage endpoint returned HTTP {e.code}: {body}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise UsageError(f"usage endpoint unreachable: {e}") from None


def fetch_usage() -> Usage:
    raw = fetch_raw()
    five = _find_window(raw, "five_hour")
    seven = _find_window(raw, "seven_day")
    if five is None or seven is None:
        raise UsageError(
            "could not find five_hour/seven_day windows in usage response; "
            "run `night-shift usage --raw` and adapt usage.py"
        )
    return Usage(five_hour=five, seven_day=seven, raw=raw)


def _find_window(obj, key: str) -> Window | None:
    """Locate a window dict by key anywhere in the (shallowly nested) response."""
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], dict):
            return _parse_window(obj[key])
        for v in obj.values():
            found = _find_window(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_window(v, key)
            if found is not None:
                return found
    return None


def _parse_window(d: dict) -> Window | None:
    util = d.get("utilization")
    if util is None:
        return None
    resets = None
    raw_resets = d.get("resets_at")
    if isinstance(raw_resets, str):
        try:
            resets = datetime.fromisoformat(raw_resets.replace("Z", "+00:00"))
            if resets.tzinfo is None:
                resets = resets.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    elif isinstance(raw_resets, (int, float)):  # epoch seconds
        resets = datetime.fromtimestamp(raw_resets, tz=timezone.utc)
    return Window(utilization=float(util), resets_at=resets)
