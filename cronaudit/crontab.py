"""Reading a crontab the way cron reads it -- including the parts people forget.

A crontab is not five columns and a command. It carries state:

* ``NAME=value`` lines set the environment for every job below them, and
  ``CRON_TZ`` changes the timezone *at that point in the file*, so one crontab
  can contain jobs in two zones.
* ``@reboot`` has no schedule at all.
* A commented-out job is usually a disabled job, not a comment. Reporting it as
  ``disabled`` is the difference between "the backup stopped three weeks ago"
  and "nothing to see here".
* ``# name: nightly-backup`` on the line above is a common, low-tech way to give
  a job a name. Supporting it means the report says ``nightly-backup`` instead
  of ``/bin/bash``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .expression import CronError, CronExpression

__all__ = ["Crontab", "Job", "parse_crontab"]

_NAME_DIRECTIVE = re.compile(r"^#\s*name\s*:\s*(?P<name>.+?)\s*$", re.IGNORECASE)
_ENV_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$")
# A commented line that still looks like a schedule: five fields, or one @shortcut.
_DISABLED_JOB = re.compile(r"^#\s*(?P<schedule>@[A-Za-z]+|\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(?P<command>\S.*)$")
_SCRIPT_SUFFIXES = (".py", ".sh", ".bash", ".rb", ".js", ".php", ".pl")


@dataclass(frozen=True)
class Job:
    """One schedulable entry. ``expression`` is None only for ``@reboot``."""

    name: str
    command: str
    line: int
    expression: Optional[CronExpression] = None
    disabled: bool = False
    timezone: str = "UTC"
    raw: str = ""
    boot: bool = False

    @property
    def schedule(self) -> str:
        return self.expression.source if self.expression is not None else "@reboot"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "command": self.command,
            "schedule": self.schedule,
            "line": self.line,
            "disabled": self.disabled,
            "timezone": self.timezone,
            "boot": self.boot,
        }


@dataclass(frozen=True)
class Crontab:
    jobs: Tuple[Job, ...]
    env: Mapping[str, str] = field(default_factory=dict)

    def enabled(self) -> Tuple[Job, ...]:
        return tuple(job for job in self.jobs if not job.disabled and job.expression is not None)

    def scheduled(self) -> Tuple[Job, ...]:
        """Jobs with a real schedule, disabled or not."""
        return tuple(job for job in self.jobs if job.expression is not None)

    def by_name(self, name: str) -> Optional[Job]:
        for job in self.jobs:
            if job.name == name:
                return job
        return None


def _derive_name(command: str) -> str:
    """A readable name from a command line, without inventing a UUID."""
    tokens = [token for token in command.split() if not token.startswith("-")]
    chosen = None
    for token in tokens:
        stripped = token.strip("'\"")
        if "/" in stripped or stripped.endswith(_SCRIPT_SUFFIXES):
            chosen = stripped
            break
    if chosen is None and tokens:
        chosen = tokens[0]
    if not chosen:
        return "job"
    base = os.path.basename(chosen)
    if base in ("python", "python3", "bash", "sh", "node", "php", "ruby", "perl"):
        # `python /srv/backup.py` names the interpreter; the script is the job.
        for token in tokens[1:]:
            stripped = token.strip("'\"")
            if "/" in stripped or stripped.endswith(_SCRIPT_SUFFIXES):
                base = os.path.basename(stripped)
                break
    return base or "job"


def _unique(name: str, taken: Sequence[str]) -> str:
    if name not in taken:
        return name
    index = 2
    while f"{name}#{index}" in taken:
        index += 1
    return f"{name}#{index}"


def parse_crontab(text: str, *, default_tz: str = "UTC") -> Crontab:
    """Parse crontab text into jobs, preserving order and line numbers.

    >>> tab = parse_crontab('''
    ... CRON_TZ=Europe/Berlin
    ... # name: nightly-backup
    ... 0 3 * * * /usr/local/bin/backup.sh
    ... # 0 4 * * * /usr/local/bin/old-backup.sh
    ... @reboot /usr/local/bin/warm-cache
    ... ''')
    >>> [(job.name, job.disabled, job.timezone) for job in tab.jobs]
    [('nightly-backup', False, 'Europe/Berlin'), ('old-backup.sh', True, 'Europe/Berlin'), ('warm-cache', False, 'Europe/Berlin')]
    """
    env: Dict[str, str] = {}
    jobs: List[Job] = []
    names: List[str] = []
    pending_name: Optional[str] = None
    timezone = default_tz

    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#"):
            directive = _NAME_DIRECTIVE.match(line)
            if directive:
                pending_name = directive.group("name")
                continue
            disabled = _DISABLED_JOB.match(line)
            if disabled:
                try:
                    job = _build_job(
                        schedule=disabled.group("schedule"),
                        command=disabled.group("command").strip(),
                        line=number,
                        timezone=timezone,
                        disabled=True,
                        raw=line,
                        name=pending_name,
                    )
                except CronError:
                    # A comment that happens to have five words is a comment.
                    continue
                pending_name = None
                assert job is not None
                job = _replace_name(job, _unique(job.name, names))
                names.append(job.name)
                jobs.append(job)
            continue

        assignment = _ENV_LINE.match(line)
        if assignment and " " not in line.split("=", 1)[0]:
            key = assignment.group("key")
            value = assignment.group("value").strip().strip("'\"")
            env[key] = value
            if key == "CRON_TZ" and value:
                timezone = value
            continue

        parts = line.split(maxsplit=5)
        if parts[0].startswith("@"):
            schedule = parts[0]
            command = " ".join(parts[1:]).strip()
        else:
            if len(parts) < 6:
                # Not a job and not a variable: cron would treat it as a command
                # with the current environment, but we cannot audit what we
                # cannot schedule, so say so rather than guess.
                raise CronError(f"line {number}: expected 5 schedule fields and a command")
            schedule = " ".join(parts[:5])
            command = parts[5]

        job = _build_job(
            schedule=schedule,
            command=command,
            line=number,
            timezone=timezone,
            disabled=False,
            raw=line,
            name=pending_name,
        )
        pending_name = None
        if job is None:
            continue
        job = _replace_name(job, _unique(job.name, names))
        names.append(job.name)
        jobs.append(job)

    return Crontab(tuple(jobs), MappingProxyType(env))


def _build_job(
    *,
    schedule: str,
    command: str,
    line: int,
    timezone: str,
    disabled: bool,
    raw: str,
    name: Optional[str],
) -> Optional[Job]:
    boot = schedule.lower() == "@reboot"
    expression: Optional[CronExpression]
    if boot:
        expression = None
    else:
        try:
            expression = CronExpression.parse(schedule)
        except CronError as exc:
            raise CronError(f"line {line}: {exc}") from exc

    if not command:
        raise CronError(f"line {line}: schedule has no command")

    return Job(
        name=name or _derive_name(command),
        command=command,
        line=line,
        expression=expression,
        disabled=disabled,
        timezone=timezone,
        raw=raw,
        boot=boot,
    )


def _replace_name(job: Job, name: str) -> Job:
    return Job(
        name=name,
        command=job.command,
        line=job.line,
        expression=job.expression,
        disabled=job.disabled,
        timezone=job.timezone,
        raw=job.raw,
        boot=job.boot,
    )
