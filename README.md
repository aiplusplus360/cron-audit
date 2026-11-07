# cron-audit

**Find the cron jobs that pile up and the ones that quietly stopped.** Parse a crontab the way cron does, expand it into real datetimes, and compare the schedule with what actually ran.

[![CI](https://github.com/aiplusplus360/cron-audit/actions/workflows/ci.yml/badge.svg)](https://github.com/aiplusplus360/cron-audit/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.11%20%7C%203.13-blue)](pyproject.toml)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

```console
$ cron-audit check /etc/crontab --overlap-window 600 --ignore healthcheck
window 2026-03-02T00:00:00+00:00 .. 2026-03-03T00:00:00+00:00
3 run(s) across 3 job(s), collision threshold 600s
     1  cleanup
     1  nightly-backup
     1  weekly-report

1 collision(s):
  2026-03-02T03:00:00+00:00  cleanup, nightly-backup
```

```console
$ cron-audit drift /etc/crontab --runs runs.json --hours 24
window 2026-03-02T03:00:00+00:00 .. 2026-03-02T04:25:00+00:00
4 job(s) scheduled, 12 run(s) observed

findings: disabled 1, failed 1, late 1, missed 1, slow 1
  late        cleanup                  2026-03-02T03:09:00+00:00  started 240s late
  disabled    legacy-export.sh         2026-03-02T03:30:00+00:00  ran although the crontab entry is disabled
  missed      weekly-report            2026-03-02T04:00:00+00:00  scheduled for 2026-03-02T04:00:00+00:00 and never ran
  failed      healthcheck              2026-03-02T04:10:01+00:00  exit code 2
  slow        healthcheck              2026-03-02T04:20:00+00:00  ran 700s but the next run starts in 600s
```

---

## Why this exists

Cron fails in two ways that `crontab -l` cannot show you:

1. **Jobs collide.** Three nightly jobs at 03:00 saturate a disk or a database connection pool at the one hour nobody is watching.
2. **Jobs stop.** Someone comments a line, a schedule drifts, a run dies with an exit code that goes to a mailbox nobody reads — and nobody notices for three weeks.

Both are answerable from two inputs you already have: the crontab and a run log. cron-audit does the two things that require actually understanding cron semantics — expand five cryptic fields into datetimes, and match those datetimes against history — and reports the difference.

It also reads crontabs the way cron does, which is where most tools get it wrong:

| Detail | Why it matters |
|---|---|
| `CRON_TZ` is positional | One crontab can contain jobs in two timezones; the variable applies from that line down. |
| Day-of-month **OR** day-of-week | `0 0 1 * 1` runs on the 1st *or* any Monday. Libraries that AND them silently skip Mondays. |
| `5/10` starts at 5 | It expands to `5,15,25,...,55` — not "5 divided by 10". |
| `@reboot` has no schedule | It cannot be expanded or checked for overlap, and pretending otherwise produces nonsense. |
| A commented-out job is a disabled job | Reported as `disabled`, kept in the output, and flagged if a run log shows it still running. |
| `# name: backup` | A common convention for naming jobs; honoured so reports name the job, not `/bin/bash`. |

---

## Install

```bash
pip install cron-audit
```

Python 3.9+, no dependencies. Timezone support uses `zoneinfo`; on a slim container without tzdata, unknown zones fall back to UTC rather than crash.

From a checkout:

```bash
pip install -e ".[dev]"
python examples/demo.py
python -m unittest discover -s tests -v
```

---

## Commands

### `jobs` — what the file actually says

```console
$ cron-audit jobs examples/crontab.txt
nightly-backup               0 3 * * *          UTC
  line 11: /usr/local/bin/backup.sh --incremental
cleanup                      5 3 * * *          UTC
  line 14: /usr/local/bin/cleanup.sh --older-than 30d
weekly-report                0 4 * * 1          UTC
  line 17: /usr/local/bin/weekly-report.py --email ops@example.com
healthcheck                  */10 * * * *       UTC
  line 20: /usr/local/bin/healthcheck.sh --json
warm-cache                   @reboot            UTC
legacy-export.sh             30 2 * * *         UTC              [disabled]
  line 25: /usr/local/bin/legacy-export.sh
```

Disabled jobs and `@reboot` entries are visible instead of being dropped.

### `expand` — the next runs, as datetimes

```console
$ cron-audit expand examples/crontab.txt --job nightly-backup --count 3 --from 2026-03-02T00:00:00Z
nightly-backup:
  2026-03-02T03:00:00+00:00
  2026-03-03T03:00:00+00:00
  2026-03-04T03:00:00+00:00
```

### `check` — jobs that fire on top of each other

```bash
cron-audit check /etc/crontab --overlap-window 600 --ignore healthcheck
cron-audit check /etc/crontab --from 2026-03-02 --until 2026-03-09 --json
```

A collision is a group of runs whose first events fall within `--overlap-window` seconds. The window is measured from the first event in the group, not the previous event, so a train of jobs one minute apart is not reported as a single giant collision. `--ignore` (repeatable) keeps a per-minute probe from colliding with everything.

### `drift` — the schedule versus the machine

```bash
cron-audit drift /etc/crontab --runs runs.json --hours 24 \
  --grace 300 --late-after 60
```

`--runs` is a JSON list (or `{"runs": [...]}`) of observed executions:

```json
[
  {"job": "nightly-backup", "started": "2026-03-02T03:00:12Z", "exit_code": 0, "duration": 240},
  {"job": "healthcheck", "started": "2026-03-02T04:10:01Z", "exit_code": 2, "duration": 1}
]
```

Every finding is one of:

| Kind | Meaning |
|---|---|
| `missed` | The schedule says it should have run; no run exists. The silent failure. |
| `unexpected` | It ran at a time the schedule does not contain. Drift, or a duplicate scheduler. |
| `late` | It ran, matched an occurrence, but later than `--late-after`. |
| `failed` | Non-zero exit code (a missing exit code is *not* a failure). |
| `slow` | Long enough to still be running when the next occurrence starts. |
| `unknown-job` | Runs exist for a name that is not in the crontab at all. |
| `disabled` | Runs exist for a line that is commented out. Someone's "off" is not off. |

Matching tolerates a run up to 5 seconds early (daemons do fire early) and up to `--grace` seconds late; beyond that it is a different occurrence.

### `parse` — one expression, quickly

```console
$ cron-audit parse "*/15 9-17 * * MON-FRI" --count 3 --from 2026-03-02T09:00:00Z
at [9, 10, 11, 12, 13, 14, 15, 16, 17]:[0, 15, 30, 45], on weekday(s) [1, 2, 3, 4, 5]
2026-03-02T09:00:00+00:00
2026-03-02T09:15:00+00:00
2026-03-02T09:30:00+00:00
```

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | clean |
| `1` | findings (collisions, or drift) |
| `2` | bad input: unreadable file, invalid expression, malformed run log |

That makes it usable as a cron job itself:

```cron
17 6 * * * /usr/local/bin/cron-audit drift /etc/crontab --runs /var/log/cron-runs.json --hours 24
```

A non-zero exit is one line in a monitoring system, without a dashboard.

---

## Library

```python
from datetime import datetime, timedelta, timezone
from cronaudit import parse_crontab, find_collisions, analyze_drift, load_runs

tab = parse_crontab(open("/etc/crontab").read())
start = datetime(2026, 3, 2, tzinfo=timezone.utc)

report = find_collisions(tab, start, start + timedelta(days=1),
                         threshold=600, ignore=("healthcheck",))
for collision in report.collisions:
    print(collision.at, collision.jobs)

drift = analyze_drift(tab, load_runs(open("runs.json")),
                      start=start, end=start + timedelta(days=1))
print(drift.counts())
```

`CronExpression.parse(text)` exposes `matches(moment)`, `occurrences(start, count=…, end=…)`, `next_after(moment)`, and `describe()`. All datetimes are timezone-aware when you pass aware ones, and naive when you pass naive ones — mixing the two raises rather than guessing.

---

## Why this is harder than it looks

- **Expansion cannot be minute-by-minute.** `@yearly` with `count=100` is a century of minutes. The expander walks *days* and then emits the small hour × minute cross-product, so a sparse schedule is cheap and a dense one is bounded.
- **The day rule is not a boolean.** It is `AND` when either field is a wildcard and `OR` when both are restricted, and `*/2` counts as a wildcard because Vixie cron checks the literal first character. This is the single most common source of "the backup didn't run on the 1st" bugs.
- **"Late" and "missed" are different failures.** A run 4 minutes late is a retry that might still succeed; a run that never happened is silent. Treating both as "missing" throws away the distinction that tells you which one to page on.
- **A run log contains runs you cannot schedule.** A disabled line that still executes is the scariest finding in the report, and it is invisible to any tool that only reports enabled entries.

---

## Limits

- No `@every`/Quartz/seconds-field crons. Five fields and the seven standard shortcuts.
- DST follows the platform's `zoneinfo` offsets; a job scheduled in a skipped hour does not fire that day, which matches what most cron daemons do. Local time inside the repeated hour is not disambiguated.
- The run log is whatever your wrapper writes. cron-audit does not install a wrapper — it reads a file you produce.
- Windows has no crontab; the parser, expander, and analysis all still work on Windows, which is what the test suite does.

---

## Testing

```bash
python -m unittest discover -s tests -v   # 83 tests
python examples/demo.py
```

The suite covers step/range/list parsing, the DOM/DOW OR rule in both directions, Sunday-as-7, leap-day expansion, timezone fallbacks, disabled and `@reboot` entries, collision grouping (including the no-chaining rule), every drift finding kind, grace/early-tolerance boundaries, malformed run logs, and the CLI exit-code contract.

CI runs Python 3.9, 3.11, and 3.13, then builds a wheel and runs the installed console script.

---

## License

MIT © 2026 aiplusplus360
