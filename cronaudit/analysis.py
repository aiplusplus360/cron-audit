"""Finding the two failures a crontab hides: jobs that pile up, and jobs that stop.

Overlap is a property of the schedule: two entries that fire within a minute of
each other for the whole window. Drift is a property of the *history*: a run log
that no longer lines up with the schedule. Both are invisible in a crontab
listing, which is why they are worth a tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # pragma: no cover - platform dependent, exercised on Linux CI
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:  # pragma: no cover - Python built without tzdata
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

from .crontab import Crontab, Job

__all__ = [
    "Collision",
    "DriftIssue",
    "DriftReport",
    "OverlapReport",
    "Run",
    "analyze_drift",
    "find_collisions",
    "load_runs",
    "parse_moment",
]

EARLY_TOLERANCE = 5.0  # a cron daemon may fire a few seconds early


def parse_moment(text: str) -> datetime:
    """Parse ISO-8601, accepting a trailing ``Z`` on Python 3.9/3.10 too."""
    cleaned = text.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError(f"{text!r} is not an ISO-8601 timestamp") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _zone(name: str):
    if not name or name.upper() in ("UTC", "ETC/UTC"):
        return timezone.utc
    if ZoneInfo is None:  # pragma: no cover
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - host lacks tzdata
        return timezone.utc


@dataclass(frozen=True)
class Collision:
    """Jobs that fire close enough together to contend for the same resources."""

    at: datetime
    jobs: Tuple[str, ...]

    def to_dict(self) -> dict:
        return {"at": self.at.isoformat(), "jobs": list(self.jobs)}


@dataclass(frozen=True)
class OverlapReport:
    start: datetime
    end: datetime
    threshold: float
    events: int
    counts: Mapping[str, int]
    collisions: Tuple[Collision, ...]

    @property
    def clean(self) -> bool:
        return not self.collisions

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "threshold_seconds": self.threshold,
            "events": self.events,
            "counts": dict(self.counts),
            "collisions": [collision.to_dict() for collision in self.collisions],
        }


def _job_occurrences(job: Job, start: datetime, end: datetime) -> List[datetime]:
    expression = job.expression
    if expression is None:
        return []
    zone = _zone(job.timezone)
    local_start = start.astimezone(zone) if start.tzinfo else start
    local_end = end.astimezone(zone) if end.tzinfo else end
    if (local_start.tzinfo is None) != (local_end.tzinfo is None):
        raise ValueError("start and end must both be naive or both be aware")
    return [
        moment.astimezone(start.tzinfo) if start.tzinfo else moment
        for moment in expression.occurrences(local_start, end=local_end)
    ]


def find_collisions(
    crontab: Crontab,
    start: datetime,
    end: datetime,
    *,
    threshold: float = 60.0,
    ignore: Sequence[str] = (),
) -> OverlapReport:
    """Group runs that start within ``threshold`` seconds of each other.

    The window is measured from the first event in a group, not from the previous
    event: a chain of jobs one minute apart should not be reported as one giant
    collision merely because each hop is under the threshold.

    ``ignore`` names jobs to leave out, which is how a per-minute health probe
    stops drowning the report.
    """
    if threshold < 0:
        raise ValueError("threshold must be >= 0")
    if end < start:
        raise ValueError("end is before start")

    skipped = set(ignore)
    counts: Dict[str, int] = {}
    events: List[Tuple[datetime, str]] = []
    for job in crontab.enabled():
        if job.name in skipped:
            continue
        moments = _job_occurrences(job, start, end)
        if not moments:
            continue
        counts[job.name] = len(moments)
        events.extend((moment, job.name) for moment in moments)

    events.sort(key=lambda item: (item[0], item[1]))
    collisions: List[Collision] = []

    group: List[Tuple[datetime, str]] = []
    group_start: Optional[datetime] = None
    for moment, name in events:
        if group_start is None or (moment - group_start).total_seconds() > threshold:
            _flush(group, group_start, collisions)
            group = [(moment, name)]
            group_start = moment
        else:
            group.append((moment, name))
    _flush(group, group_start, collisions)

    return OverlapReport(
        start=start,
        end=end,
        threshold=float(threshold),
        events=len(events),
        counts=counts,
        collisions=tuple(collisions),
    )


def _flush(
    group: Sequence[Tuple[datetime, str]],
    group_start: Optional[datetime],
    out: List[Collision],
) -> None:
    if group_start is None:
        return
    names = tuple(sorted({name for _, name in group}))
    if len(names) >= 2:
        out.append(Collision(group_start, names))


@dataclass(frozen=True)
class Run:
    """One observed execution, typically from a wrapper script or scheduler log."""

    job: str
    started: datetime
    exit_code: Optional[int] = None
    duration: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Run":
        if not isinstance(data, Mapping):
            raise ValueError("each run must be an object")
        name = data.get("job") or data.get("name")
        started = data.get("started") or data.get("started_at") or data.get("at")
        if not name or not started:
            raise ValueError("each run needs 'job' and 'started'")
        if not isinstance(started, str):
            raise ValueError("'started' must be an ISO-8601 string")
        exit_code = data.get("exit_code", data.get("exit"))
        duration = data.get("duration")
        if exit_code is not None and not isinstance(exit_code, int):
            raise ValueError("'exit_code' must be an integer or null")
        if duration is not None and not isinstance(duration, (int, float)):
            raise ValueError("'duration' must be a number of seconds or null")
        return cls(
            job=str(name),
            started=parse_moment(started),
            exit_code=exit_code,
            duration=float(duration) if duration is not None else None,
        )


def load_runs(payload: Any) -> List[Run]:
    """Accept either a list of runs or ``{"runs": [...]}``."""
    if isinstance(payload, Mapping):
        payload = payload.get("runs", [])
    if not isinstance(payload, list):
        raise ValueError("runs file must be a list or an object with a 'runs' list")
    return [Run.from_dict(item) for item in payload]


@dataclass(frozen=True)
class DriftIssue:
    """``kind`` is one of: missed, unexpected, late, failed, slow, unknown-job, disabled."""

    kind: str
    job: str
    detail: str
    expected: Optional[datetime] = None
    actual: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "job": self.job,
            "detail": self.detail,
            "expected": self.expected.isoformat() if self.expected else None,
            "actual": self.actual.isoformat() if self.actual else None,
        }


@dataclass(frozen=True)
class DriftReport:
    start: datetime
    end: datetime
    issues: Tuple[DriftIssue, ...]
    jobs_checked: int
    runs_checked: int

    @property
    def clean(self) -> bool:
        return not self.issues

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for issue in self.issues:
            out[issue.kind] = out.get(issue.kind, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "jobs_checked": self.jobs_checked,
            "runs_checked": self.runs_checked,
            "counts": self.counts(),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def analyze_drift(
    crontab: Crontab,
    runs: Iterable[Run],
    *,
    start: datetime,
    end: datetime,
    grace: float = 300.0,
    late_after: float = 60.0,
) -> DriftReport:
    """Match observed runs to expected occurrences, then report every gap.

    ``grace`` is how late a run may be and still count as the same occurrence;
    ``late_after`` is when a matched run is additionally called out as late.
    A run that matches nothing is drift: the schedule and the machine disagree
    about what is supposed to run.
    """
    if end < start:
        raise ValueError("end is before start")
    if grace < 0 or late_after < 0:
        raise ValueError("grace and late_after must be >= 0")

    observed: Dict[str, List[Run]] = {}
    for run in runs:
        observed.setdefault(run.job, []).append(run)
    for bucket in observed.values():
        bucket.sort(key=lambda item: item.started)

    issues: List[DriftIssue] = []
    jobs = crontab.enabled()
    known = {job.name for job in crontab.jobs}
    disabled = {job.name for job in crontab.jobs if job.disabled or job.expression is None}

    for name in sorted(set(observed) - known):
        issues.append(DriftIssue(
            "unknown-job",
            name,
            "runs exist for a job that is not in the crontab",
            actual=observed[name][0].started,
        ))

    for name in sorted(set(observed) & disabled):
        for run in observed[name]:
            issues.append(DriftIssue(
                "disabled",
                name,
                "ran although the crontab entry is disabled",
                actual=run.started,
            ))

    for job in jobs:
        expected = _job_occurrences(job, start, end)
        actual = observed.get(job.name, [])
        remaining = list(actual)

        for moment in expected:
            best_index: Optional[int] = None
            best_delta = 0.0
            for index, run in enumerate(remaining):
                delta = (run.started - moment).total_seconds()
                if -EARLY_TOLERANCE <= delta <= grace:
                    if best_index is None or abs(delta) < abs(best_delta):
                        best_index, best_delta = index, delta
            if best_index is None:
                issues.append(DriftIssue(
                    "missed",
                    job.name,
                    f"scheduled for {moment.isoformat()} and never ran",
                    expected=moment,
                ))
                continue
            run = remaining.pop(best_index)
            if best_delta > late_after:
                issues.append(DriftIssue(
                    "late",
                    job.name,
                    f"started {best_delta:.0f}s late",
                    expected=moment,
                    actual=run.started,
                ))

        for run in remaining:
            issues.append(DriftIssue(
                "unexpected",
                job.name,
                "ran outside any scheduled occurrence",
                actual=run.started,
            ))

        for run in actual:
            if run.exit_code not in (None, 0):
                issues.append(DriftIssue(
                    "failed",
                    job.name,
                    f"exit code {run.exit_code}",
                    actual=run.started,
                ))
            if run.duration is not None and job.expression is not None:
                following = job.expression.occurrences(run.started + timedelta(minutes=1), count=1)
                if following:
                    gap = (following[0] - run.started).total_seconds()
                    if run.duration > gap:
                        issues.append(DriftIssue(
                            "slow",
                            job.name,
                            f"ran {run.duration:.0f}s but the next run starts in {gap:.0f}s",
                            actual=run.started,
                        ))

    issues.sort(key=lambda issue: (issue.expected or issue.actual or start, issue.job, issue.kind))
    return DriftReport(
        start=start,
        end=end,
        issues=tuple(issues),
        jobs_checked=len(jobs),
        runs_checked=sum(len(bucket) for bucket in observed.values()),
    )
