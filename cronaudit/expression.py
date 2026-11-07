"""Cron expressions: parse, match, expand.

Two details make this more than splitting a string on spaces:

1. **Day-of-month and day-of-week are OR'ed when both are restricted.** The
   classic Vixie rule is: if either field starts with ``*`` the job runs when
   both match; otherwise it runs when *either* matches. Most calendar libraries
   use AND, and then a job meant to run "on the 1st or on Mondays" silently
   stops running on Mondays.
2. **``5/10`` means "from 5, every 10"**, not "5 divided by 10". A step is
   allowed to start in the middle of a range, and the end of the range is the
   field's maximum, not the start.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

__all__ = ["CronError", "CronExpression", "Field", "parse_field"]

MONTH_NAMES = {
    name: index
    for index, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}
DOW_NAMES = {
    name: index
    for index, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))
}

SPECIALS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

MAX_SEARCH_DAYS = 366 * 8  # eight years: enough for 29 February on any weekday


class CronError(ValueError):
    """The expression is not a schedule cron would accept."""


@dataclass(frozen=True)
class Field:
    """One column of a cron expression.

    ``wildcard`` is True when the field started with ``*`` -- including ``*/5``
    -- because that is exactly the test Vixie cron uses for the day OR rule.
    """

    values: FrozenSet[int]
    wildcard: bool
    source: str = ""

    def __contains__(self, value: int) -> bool:
        return value in self.values


def _resolve(token: str, names: Optional[Dict[str, int]], lo: int, hi: int, what: str) -> int:
    if not token:
        raise CronError(f"empty {what} value")
    if token.isdigit():
        value = int(token)
    else:
        key = token.upper()[:3]
        if not names or key not in names:
            raise CronError(f"{what} value {token!r} is not a number or a known name")
        value = names[key]
    if not lo <= value <= hi:
        raise CronError(f"{what} value {value} is outside {lo}-{hi}")
    return value


def parse_field(spec: str, lo: int, hi: int, *, names: Optional[Dict[str, int]] = None,
                what: str = "field") -> Field:
    """Parse one field into the set of values it matches."""
    if not spec:
        raise CronError(f"empty {what} field")

    wildcard = spec.startswith("*") or spec == "?"
    values: set = set()
    for part in spec.split(","):
        if not part:
            raise CronError(f"empty list element in {what} field {spec!r}")

        step = 1
        base = part
        if "/" in part:
            base, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"step in {what} field must be a positive integer: {part!r}")
            step = int(step_text)

        if base in ("*", "?"):
            start, end = lo, hi
        elif "-" in base:
            left, _, right = base.partition("-")
            start = _resolve(left, names, lo, hi, what)
            end = _resolve(right, names, lo, hi, what)
            if start > end:
                raise CronError(f"descending range {base!r} in {what} field")
        else:
            start = _resolve(base, names, lo, hi, what)
            end = hi if step != 1 else start

        values.update(range(start, end + 1, step))

    if not values:
        raise CronError(f"{what} field {spec!r} matches nothing")
    return Field(frozenset(values), wildcard, spec)


def _merge(a: Iterable[int], b: Iterable[int]) -> FrozenSet[int]:
    return frozenset(set(a) | set(b))


@dataclass(frozen=True)
class CronExpression:
    """A five-field cron schedule, or one of the ``@`` shortcuts.

    >>> expr = CronExpression.parse("*/15 9-17 * * MON-FRI")
    >>> expr.matches(datetime(2026, 11, 9, 9, 30))     # a Monday
    True
    >>> expr.matches(datetime(2026, 11, 8, 9, 30))     # a Sunday
    False
    """

    minute: Field
    hour: Field
    day_of_month: Field
    month: Field
    day_of_week: Field
    source: str = ""

    @classmethod
    def parse(cls, text: str) -> "CronExpression":
        raw = text.strip()
        if not raw:
            raise CronError("empty expression")
        if raw.lower().startswith("@reboot"):
            raise CronError("@reboot has no schedule; it only runs at boot")

        key = raw.lower()
        if key in SPECIALS:
            raw = SPECIALS[key]
        elif key.startswith("@"):
            raise CronError(f"unknown shortcut {text!r}")

        parts = raw.split()
        if len(parts) != 5:
            raise CronError(f"expected 5 fields, got {len(parts)}: {text!r}")

        minute = parse_field(parts[0], 0, 59, what="minute")
        hour = parse_field(parts[1], 0, 23, what="hour")
        dom = parse_field(parts[2], 1, 31, what="day-of-month")
        month = parse_field(parts[3], 1, 12, names=MONTH_NAMES, what="month")
        dow = parse_field(parts[4], 0, 7, names=DOW_NAMES, what="day-of-week")
        # 7 and 0 are both Sunday. Normalise so matching only has to know one.
        if 7 in dow.values:
            dow = Field(_merge(dow.values - {7}, {0}), dow.wildcard, dow.source)

        return cls(minute, hour, dom, month, dow, source=text.strip())

    # -- matching ----------------------------------------------------------

    def _day_matches(self, day: date) -> bool:
        if day.month not in self.month:
            return False
        dom_ok = day.day in self.day_of_month
        dow_ok = (day.isoweekday() % 7) in self.day_of_week
        if self.day_of_month.wildcard or self.day_of_week.wildcard:
            # One of them is unrestricted, so the usual AND applies.
            return dom_ok and dow_ok
        # Both restricted: Vixie cron ORs them.
        return dom_ok or dow_ok

    def matches(self, moment: datetime) -> bool:
        return (
            moment.minute in self.minute
            and moment.hour in self.hour
            and self._day_matches(moment.date())
        )

    # -- expansion ---------------------------------------------------------

    def occurrences(
        self,
        start: datetime,
        *,
        end: Optional[datetime] = None,
        count: Optional[int] = None,
    ) -> List[datetime]:
        """Every run in ``[start, end]``, or the next ``count`` runs.

        Days are walked, then the (usually tiny) hour x minute cross-product is
        emitted in order. That is fast for sparse schedules like ``@yearly``
        where minute-by-minute scanning would take tens of millions of steps.
        """
        if count is None and end is None:
            raise ValueError("pass count= or end= (or both) to bound the search")
        if count is not None and count < 1:
            raise ValueError("count must be >= 1")
        if end is not None:
            if (end.tzinfo is None) != (start.tzinfo is None):
                raise ValueError("start and end must both be naive or both be aware")
            if end < start:
                raise ValueError("end is before start")

        zone = start.tzinfo
        hours = sorted(self.hour.values)
        minutes = sorted(self.minute.values)
        found: List[datetime] = []
        day = start.date()
        last_day = (start + timedelta(days=MAX_SEARCH_DAYS)).date()
        if end is not None and end.date() < last_day:
            last_day = end.date()

        while day <= last_day:
            if self._day_matches(day):
                for hour in hours:
                    for minute in minutes:
                        moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
                        if moment < start:
                            continue
                        if end is not None and moment > end:
                            return found
                        found.append(moment)
                        if count is not None and len(found) >= count:
                            return found
            day += timedelta(days=1)

        if count is not None and not found:  # pragma: no cover - needs a broken calendar
            raise CronError(f"no occurrence of {self.source!r} within {MAX_SEARCH_DAYS} days")
        return found

    def next_after(self, moment: datetime, count: int = 1) -> List[datetime]:
        floored = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        return self.occurrences(floored, count=count)

    def describe(self) -> str:
        """A short human sentence, for reports that are read at 3 a.m."""
        bits = []
        if self.minute.wildcard and self.hour.wildcard:
            bits.append("every minute")
        elif self.hour.wildcard:
            bits.append("every hour" if self.minute.wildcard else f"at minute {sorted(self.minute.values)}")
        else:
            bits.append(f"at {sorted(self.hour.values)}:{sorted(self.minute.values)}")
        if not self.day_of_month.wildcard:
            bits.append(f"on day(s) {sorted(self.day_of_month.values)}")
        if not self.day_of_week.wildcard:
            bits.append(f"on weekday(s) {sorted(self.day_of_week.values)}")
        if not self.month.wildcard:
            bits.append(f"in month(s) {sorted(self.month.values)}")
        return ", ".join(bits)


def expand(text: str, start: datetime, *, count: int = 5) -> Tuple[CronExpression, List[datetime]]:
    """Parse and expand in one call; handy for the CLI and for tests."""
    expression = CronExpression.parse(text)
    return expression, expression.occurrences(start, count=count)
