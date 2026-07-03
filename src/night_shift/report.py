"""Morning report: what got done, what failed, what it cost."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from .config import STATE_DIR, Config
from .runner import TaskResult
from .usage import Usage

REPORTS_DIR = STATE_DIR / "reports"

STATUS_ICON = {"done": "✅", "failed": "❌", "rate_limited": "⏳", "interrupted": "⏹️"}


def write_report(cfg: Config, started: datetime, wake: datetime, deadline: datetime,
                 stop_reason: str, results: list[TaskResult],
                 start_usage: Usage | None, end_usage: Usage | None, state: dict) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{started:%Y-%m-%d_%H%M}.md"

    done = [r for r in results if r.status == "done"]
    failed = [r for r in results if r.status == "failed"]
    other = [r for r in results if r.status not in ("done", "failed")]

    lines = [
        f"# night-shift report — {started:%a %Y-%m-%d %H:%M}",
        "",
        f"- run window: {started:%H:%M} → stopped {datetime.now():%H:%M} "
        f"(wake {wake:%a %H:%M}, hard stop {deadline:%H:%M})",
        f"- stop reason: **{stop_reason}**",
        f"- tasks: **{len(done)} done**, {len(failed)} failed, {len(other)} other",
    ]
    if start_usage and end_usage:
        burned = max(0.0, end_usage.seven_day.utilization - start_usage.seven_day.utilization)
        lines += [
            f"- weekly usage: {start_usage.seven_day.utilization:.0f}% → "
            f"{end_usage.seven_day.utilization:.0f}% (this run burned ~{burned:.1f}%)",
            f"- agent weekly budget: {state.get('agent_used_percent', 0):.1f}% used of "
            f"{cfg.weekly_agent_percent:.0f}% ceiling",
            f"- 5h window at stop: {end_usage.five_hour.utilization:.0f}%",
        ]
    total_cost = sum(r.cost_usd or 0 for r in results)
    if total_cost:
        lines.append(f"- reported API cost equivalent: ${total_cost:.2f}")
    lines.append("")

    for title, group in (("## Completed", done), ("## Failed", failed), ("## Other", other)):
        if not group:
            continue
        lines.append(title)
        for r in group:
            icon = STATUS_ICON.get(r.status, "•")
            lines.append(f"- {icon} **[{r.task.repo.name}]** {r.task.title[:100]}")
            detail = []
            if r.pr_url:
                detail.append(f"PR: {r.pr_url}")
            elif r.branch:
                detail.append(f"branch `{r.branch}`")
            if r.commits:
                detail.append(f"{r.commits} commit(s)")
            if r.duration_s:
                detail.append(f"{r.duration_s / 60:.0f} min")
            if r.note and not r.pr_url:
                detail.append(r.note[:200])
            if detail:
                lines.append(f"  - {' · '.join(detail)}")
            if r.log_path:
                lines.append(f"  - log: {r.log_path}")
        lines.append("")

    path.write_text("\n".join(lines))
    shutil.copyfile(path, REPORTS_DIR / "latest.md")
    return path


def latest_report() -> Path | None:
    p = REPORTS_DIR / "latest.md"
    return p if p.exists() else None
