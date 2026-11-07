"""A tour of cron-audit against the example crontab and run log.

    python examples/demo.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cronaudit import (  # noqa: E402
    analyze_drift,
    find_collisions,
    load_runs,
    parse_crontab,
    parse_moment,
)

HERE = os.path.dirname(os.path.abspath(__file__))


def banner(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def main() -> int:
    with open(os.path.join(HERE, "crontab.txt"), "r", encoding="utf-8") as handle:
        tab = parse_crontab(handle.read())

    banner("1. what the crontab actually says")
    for job in tab.jobs:
        state = "disabled" if job.disabled else ("boot" if job.boot else job.schedule)
        print(f"  {job.name:<16} {state:<18} {job.timezone}")

    banner("2. the next runs of one job")
    backup = tab.by_name("nightly-backup")
    assert backup is not None and backup.expression is not None
    start = datetime(2026, 3, 2, tzinfo=timezone.utc)
    for moment in backup.expression.occurrences(start, count=3):
        print(f"  {moment.isoformat()}")

    banner("3. jobs that fire on top of each other")
    window_start = datetime(2026, 3, 2, tzinfo=timezone.utc)
    window_end = window_start + timedelta(days=1)
    report = find_collisions(
        tab, window_start, window_end, threshold=600, ignore=("healthcheck",)
    )
    print(f"  {report.events} run(s) in the window")
    for collision in report.collisions:
        print(f"  {collision.at.isoformat()}  {', '.join(collision.jobs)}")

    banner("4. schedule vs. what actually ran")
    with open(os.path.join(HERE, "runs.json"), "r", encoding="utf-8") as handle:
        runs = load_runs(json.load(handle))
    drift = analyze_drift(
        tab,
        runs,
        start=parse_moment("2026-03-02T03:00:00Z"),
        end=parse_moment("2026-03-02T04:25:00Z"),
        grace=300,
        late_after=60,
    )
    print(f"  {drift.jobs_checked} scheduled job(s), {drift.runs_checked} run(s) observed")
    for issue in drift.issues:
        when = issue.actual or issue.expected
        print(f"  {issue.kind:<11} {issue.job:<16} {when.isoformat() if when else '-'}")
        print(f"              {issue.detail}")

    banner("5. the summary a cron mail should have contained")
    print(f"  overlaps: {len(report.collisions)}")
    print(f"  drift findings: {drift.counts()}")

    assert report.collisions, "the example crontab is supposed to collide"
    assert drift.counts().get("missed") == 1
    assert drift.counts().get("late") == 1
    assert drift.counts().get("failed") == 1
    assert drift.counts().get("slow") == 1
    assert drift.counts().get("disabled") == 1
    print("\nall demo assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
