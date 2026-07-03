"""Persistent state: the agent's cumulative weekly consumption and the runfile.

Weekly attribution: while a night run is active the user is AFK, so every
increase in seven_day utilization observed between polls is attributed to the
agent. The counter resets when the API's seven_day resets_at changes.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from .config import STATE_DIR
from .usage import Usage

STATE_FILE = STATE_DIR / "state.json"
RUN_FILE = STATE_DIR / "current-run.json"


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"week_resets_at": None, "agent_used_percent": 0.0}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def rollover_week(state: dict, usage: Usage) -> dict:
    """Zero the agent counter when a new weekly window starts."""
    resets = usage.seven_day.resets_at.isoformat() if usage.seven_day.resets_at else None
    if resets != state.get("week_resets_at"):
        state["week_resets_at"] = resets
        state["agent_used_percent"] = 0.0
    return state


def write_runfile(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(data, pid=os.getpid(), updated_at=datetime.now().isoformat(timespec="seconds"))
    tmp = RUN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(RUN_FILE)


def read_runfile() -> dict | None:
    try:
        data = json.loads(RUN_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    # Stale if the process is gone.
    try:
        os.kill(data.get("pid", -1), 0)
    except (OSError, TypeError):
        data["stale"] = True
    return data


def clear_runfile() -> None:
    RUN_FILE.unlink(missing_ok=True)
