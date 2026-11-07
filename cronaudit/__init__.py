"""cron-audit: find the jobs that pile up and the jobs that quietly stopped.

    from cronaudit import parse_crontab, find_collisions

    tab = parse_crontab(open("/etc/crontab").read())
    report = find_collisions(tab, start, end, threshold=60)
    for collision in report.collisions:
        print(collision.at, collision.jobs)
"""

from __future__ import annotations

from .analysis import (
    Collision,
    DriftIssue,
    DriftReport,
    OverlapReport,
    Run,
    analyze_drift,
    find_collisions,
    load_runs,
    parse_moment,
)
from .crontab import Crontab, Job, parse_crontab
from .expression import CronError, CronExpression, Field, parse_field

__all__ = [
    "Collision",
    "CronError",
    "CronExpression",
    "Crontab",
    "DriftIssue",
    "DriftReport",
    "Field",
    "Job",
    "OverlapReport",
    "Run",
    "analyze_drift",
    "find_collisions",
    "load_runs",
    "parse_crontab",
    "parse_field",
    "parse_moment",
]

__version__ = "1.0.0"
