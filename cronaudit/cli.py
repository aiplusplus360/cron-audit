"""Command line for cron-audit."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from .analysis import DriftReport, OverlapReport, analyze_drift, find_collisions, load_runs, parse_moment
from .crontab import Crontab, parse_crontab
from .expression import CronError, CronExpression

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc


def _load(path: str, *, tz: str) -> Crontab:
    return parse_crontab(_read(path), default_tz=tz)


def _window(args: argparse.Namespace, *, backwards: bool) -> tuple:
    """The analysis window: ``--from``/``--until`` if given, else ``--hours``."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    hours = args.hours
    if hours is not None:
        if hours <= 0:
            raise ValueError("--hours must be > 0")
    else:
        hours = 24.0

    start = parse_moment(args.from_) if args.from_ else None
    end = parse_moment(args.until) if args.until else None

    if start is not None and end is not None:
        return start, end
    if start is not None:
        return start, start + timedelta(hours=hours)
    if end is not None:
        return end - timedelta(hours=hours), end
    if backwards:
        return now - timedelta(hours=hours), now
    return now, now + timedelta(hours=hours)


def cmd_jobs(args: argparse.Namespace) -> int:
    tab = _load(args.file, tz=args.tz)
    if args.json:
        print(json.dumps({
            "jobs": [job.to_dict() for job in tab.jobs],
            "env": dict(tab.env),
        }, indent=2))
        return EXIT_OK
    if not tab.jobs:
        print("no jobs")
        return EXIT_OK
    for job in tab.jobs:
        flags = " [disabled]" if job.disabled else ""
        if job.boot:
            print(f"{job.name:<28} {'@reboot':<18} {job.timezone:<16}{flags}")
        else:
            print(f"{job.name:<28} {job.schedule:<18} {job.timezone:<16}{flags}")
            print(f"  line {job.line}: {job.command}")
    return EXIT_OK


def cmd_expand(args: argparse.Namespace) -> int:
    tab = _load(args.file, tz=args.tz)
    jobs = [job for job in tab.enabled() if args.job is None or job.name == args.job]
    if args.job is not None and not jobs:
        raise ValueError(f"no enabled job named {args.job!r}")
    start = parse_moment(args.from_) if args.from_ else datetime.now(timezone.utc)
    plan = {}
    for job in jobs:
        assert job.expression is not None
        plan[job.name] = job.expression.occurrences(start, count=args.count)
    if args.json:
        print(json.dumps({
            "from": start.isoformat(),
            "count": args.count,
            "next": {
                name: [moment.isoformat() for moment in moments]
                for name, moments in plan.items()
            },
        }, indent=2))
        return EXIT_OK
    for name, moments in plan.items():
        print(f"{name}:")
        for moment in moments:
            print(f"  {moment.isoformat()}")
    return EXIT_OK


def _print_overlap(report: OverlapReport) -> None:
    print(f"window {report.start.isoformat()} .. {report.end.isoformat()}")
    print(f"{report.events} run(s) across {len(report.counts)} job(s), "
          f"collision threshold {report.threshold:.0f}s")
    for name, count in sorted(report.counts.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {count:>4}  {name}")
    if not report.collisions:
        print("no overlapping jobs")
        return
    print(f"\n{len(report.collisions)} collision(s):")
    for collision in report.collisions:
        print(f"  {collision.at.isoformat()}  {', '.join(collision.jobs)}")


def cmd_check(args: argparse.Namespace) -> int:
    tab = _load(args.file, tz=args.tz)
    start, end = _window(args, backwards=False)
    report = find_collisions(
        tab, start, end, threshold=args.overlap_window, ignore=args.ignore or ()
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_overlap(report)
    return EXIT_OK if report.clean else EXIT_FINDINGS


def _print_drift(report: DriftReport) -> None:
    print(f"window {report.start.isoformat()} .. {report.end.isoformat()}")
    print(f"{report.jobs_checked} job(s) scheduled, {report.runs_checked} run(s) observed")
    if not report.issues:
        print("schedule and history agree")
        return
    counts = ", ".join(f"{kind} {number}" for kind, number in sorted(report.counts().items()))
    print(f"\nfindings: {counts}")
    for issue in report.issues:
        when = issue.actual or issue.expected
        stamp = when.isoformat() if when else "-"
        print(f"  {issue.kind:<11} {issue.job:<24} {stamp}  {issue.detail}")


def cmd_drift(args: argparse.Namespace) -> int:
    tab = _load(args.file, tz=args.tz)
    start, end = _window(args, backwards=True)
    try:
        with open(args.runs, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise ValueError(f"cannot read {args.runs}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{args.runs} is not valid JSON: {exc}") from exc

    report = analyze_drift(
        tab,
        load_runs(payload),
        start=start,
        end=end,
        grace=args.grace,
        late_after=args.late_after,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_drift(report)
    return EXIT_OK if report.clean else EXIT_FINDINGS


def cmd_parse(args: argparse.Namespace) -> int:
    expression = CronExpression.parse(args.expression)
    start = parse_moment(args.from_) if args.from_ else datetime.now(timezone.utc)
    moments = expression.occurrences(start, count=args.count)
    if args.json:
        print(json.dumps({
            "expression": expression.source,
            "description": expression.describe(),
            "next": [moment.isoformat() for moment in moments],
        }, indent=2))
    else:
        print(expression.describe())
        for moment in moments:
            print(moment.isoformat())
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cron-audit",
        description="Audit crontabs: overlapping jobs, missed runs, silent drift.",
    )
    parser.add_argument("--tz", default="UTC", help="default timezone for the crontab")
    sub = parser.add_subparsers(dest="command", required=True)

    jobs = sub.add_parser("jobs", help="list the jobs in a crontab")
    jobs.add_argument("file")
    jobs.add_argument("--json", action="store_true")
    jobs.set_defaults(func=cmd_jobs)

    expand = sub.add_parser("expand", help="show the next runs of each job")
    expand.add_argument("file")
    expand.add_argument("--job", default=None)
    expand.add_argument("--count", type=int, default=5)
    expand.add_argument("--from", dest="from_", default=None, help="ISO-8601 start")
    expand.add_argument("--json", action="store_true")
    expand.set_defaults(func=cmd_expand)

    check = sub.add_parser("check", help="find jobs that fire at the same time")
    check.add_argument("file")
    check.add_argument("--from", dest="from_", default=None)
    check.add_argument("--until", default=None)
    check.add_argument("--hours", type=float, default=24.0)
    check.add_argument("--overlap-window", type=float, default=60.0, dest="overlap_window")
    check.add_argument("--ignore", action="append", default=[], metavar="JOB",
                       help="leave a noisy job out of the analysis (repeatable)")
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=cmd_check)

    drift = sub.add_parser("drift", help="compare a run log with the schedule")
    drift.add_argument("file")
    drift.add_argument("--runs", required=True, help="JSON list of observed runs")
    drift.add_argument("--from", dest="from_", default=None)
    drift.add_argument("--until", default=None)
    drift.add_argument("--hours", type=float, default=24.0)
    drift.add_argument("--grace", type=float, default=300.0,
                       help="a run this late still counts as the occurrence")
    drift.add_argument("--late-after", type=float, default=60.0, dest="late_after")
    drift.add_argument("--json", action="store_true")
    drift.set_defaults(func=cmd_drift)

    parse = sub.add_parser("parse", help="expand one expression")
    parse.add_argument("expression")
    parse.add_argument("--count", type=int, default=5)
    parse.add_argument("--from", dest="from_", default=None)
    parse.add_argument("--json", action="store_true")
    parse.set_defaults(func=cmd_parse)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (CronError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
