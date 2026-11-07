"""Tests for cron-audit. Standard library only."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cronaudit import (  # noqa: E402
    CronError,
    CronExpression,
    Run,
    analyze_drift,
    find_collisions,
    load_runs,
    parse_crontab,
    parse_field,
    parse_moment,
)
from cronaudit import cli  # noqa: E402
from cronaudit.analysis import _zone  # noqa: E402


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0,
        second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


SIMPLE_CRONTAB = """\
# a comment
MAILTO=ops@example.com
CRON_TZ=UTC

# name: backup
0 3 * * * /usr/local/bin/backup.sh --full
5 3 * * * /usr/local/bin/cleanup.sh
# 30 2 * * * /usr/local/bin/legacy.sh
@reboot /usr/local/bin/warm.sh
"""


class FieldTest(unittest.TestCase):
    def test_star_is_every_value_and_is_wild(self):
        field = parse_field("*", 0, 59)
        self.assertTrue(field.wildcard)
        self.assertEqual(len(field.values), 60)
        self.assertIn(59, field)

    def test_step_from_the_start(self):
        self.assertEqual(parse_field("*/15", 0, 59).values, frozenset({0, 15, 30, 45}))

    def test_step_with_a_start_offset(self):
        self.assertEqual(parse_field("5/10", 0, 59).values, frozenset({5, 15, 25, 35, 45, 55}))

    def test_range_with_step(self):
        self.assertEqual(parse_field("1-10/3", 0, 59).values, frozenset({1, 4, 7, 10}))

    def test_lists_and_names(self):
        values = parse_field("MON-FRI", 0, 7, names={"SUN": 0, "MON": 1, "TUE": 2, "WED": 3,
                                                     "THU": 4, "FRI": 5, "SAT": 6},
                             what="day-of-week").values
        self.assertEqual(values, frozenset({1, 2, 3, 4, 5}))

    def test_star_with_step_is_still_a_wildcard_for_the_day_rule(self):
        self.assertTrue(parse_field("*/2", 1, 31).wildcard)

    def test_question_mark_is_accepted_as_wild(self):
        field = parse_field("?", 0, 59)
        self.assertTrue(field.wildcard)
        self.assertEqual(len(field.values), 60)

    def test_rejects_bad_fields(self):
        for spec in ("", "*/0", "5-1", "1,,2", "abc", "60", "-1", "1-", "1/2/3"):
            with self.subTest(spec=spec), self.assertRaises(CronError):
                parse_field(spec, 0, 59)


class ExpressionTest(unittest.TestCase):
    def test_simple_match_and_miss(self):
        expr = CronExpression.parse("30 9 * * *")
        self.assertTrue(expr.matches(utc(2026, 5, 4, 9, 30)))
        self.assertFalse(expr.matches(utc(2026, 5, 4, 9, 31)))
        self.assertFalse(expr.matches(utc(2026, 5, 4, 10, 30)))

    def test_month_names(self):
        expr = CronExpression.parse("0 0 1 JAN,jul *")
        self.assertTrue(expr.matches(utc(2026, 1, 1)))
        self.assertTrue(expr.matches(utc(2026, 7, 1)))
        self.assertFalse(expr.matches(utc(2026, 2, 1)))

    def test_sunday_can_be_written_as_seven(self):
        seven = CronExpression.parse("0 0 * * 7")
        zero = CronExpression.parse("0 0 * * 0")
        self.assertEqual(seven.day_of_week.values, zero.day_of_week.values)
        self.assertTrue(seven.matches(utc(2026, 3, 1)))  # a Sunday

    def test_day_of_month_and_day_of_week_are_ored_when_both_restricted(self):
        expr = CronExpression.parse("0 0 1 * 1")  # first of the month OR Mondays
        moments = expr.occurrences(utc(2026, 3, 1), count=4)
        self.assertEqual(
            moments,
            [utc(2026, 3, 1), utc(2026, 3, 2), utc(2026, 3, 9), utc(2026, 3, 16)],
        )

    def test_a_wildcard_side_switches_the_rule_to_and(self):
        first_only = CronExpression.parse("0 0 1 * *")
        self.assertFalse(first_only.matches(utc(2026, 3, 2)))
        monday_only = CronExpression.parse("0 0 * * 1")
        self.assertTrue(monday_only.matches(utc(2026, 3, 2)))
        self.assertFalse(monday_only.matches(utc(2026, 3, 3)))

    def test_shortcut_aliases(self):
        self.assertEqual(CronExpression.parse("@hourly").source, "@hourly")
        hourly = CronExpression.parse("@hourly")
        self.assertTrue(hourly.matches(utc(2026, 3, 2, 7, 0)))
        self.assertFalse(hourly.matches(utc(2026, 3, 2, 7, 1)))
        weekly = CronExpression.parse("@weekly")
        self.assertTrue(weekly.matches(utc(2026, 3, 1)))  # Sunday midnight

    def test_leap_day_is_found_two_years_out(self):
        expr = CronExpression.parse("0 0 29 2 *")
        moments = expr.occurrences(utc(2026, 1, 1), count=2)
        self.assertEqual(moments, [utc(2028, 2, 29), utc(2032, 2, 29)])

    def test_occurrences_are_ordered_and_bounded_by_end(self):
        expr = CronExpression.parse("0 3 * * *")
        moments = expr.occurrences(utc(2026, 3, 1), end=utc(2026, 3, 3, 3, 0))
        self.assertEqual(moments, [utc(2026, 3, 1, 3), utc(2026, 3, 2, 3), utc(2026, 3, 3, 3)])

    def test_start_is_inclusive(self):
        expr = CronExpression.parse("0 3 * * *")
        self.assertEqual(expr.occurrences(utc(2026, 3, 1, 3), count=1), [utc(2026, 3, 1, 3)])

    def test_sparse_expression_expands_quickly(self):
        expr = CronExpression.parse("@yearly")
        moments = expr.occurrences(utc(2026, 1, 1), count=3)
        self.assertEqual([moment.year for moment in moments], [2026, 2027, 2028])

    def test_next_after_skips_the_current_minute(self):
        every_minute = CronExpression.parse("* * * * *")
        self.assertEqual(every_minute.next_after(utc(2026, 3, 2, 10, 0))[0], utc(2026, 3, 2, 10, 1))
        hourly = CronExpression.parse("0 * * * *")
        self.assertEqual(hourly.next_after(utc(2026, 3, 2, 10, 0))[0], utc(2026, 3, 2, 11, 0))

    def test_occurrences_require_a_bound(self):
        with self.assertRaises(ValueError):
            CronExpression.parse("* * * * *").occurrences(utc(2026, 1, 1))
        with self.assertRaises(ValueError):
            CronExpression.parse("* * * * *").occurrences(utc(2026, 1, 1), count=0)
        with self.assertRaises(ValueError):
            CronExpression.parse("* * * * *").occurrences(
                utc(2026, 1, 2), end=utc(2026, 1, 1)
            )

    def test_mixing_naive_and_aware_is_refused(self):
        expr = CronExpression.parse("* * * * *")
        with self.assertRaises(ValueError):
            expr.occurrences(datetime(2026, 1, 1), end=utc(2026, 1, 2))

    def test_naive_expansion_works(self):
        expr = CronExpression.parse("0 12 * * *")
        moments = expr.occurrences(datetime(2026, 3, 1), count=1)
        self.assertEqual(moments, [datetime(2026, 3, 1, 12)])

    def test_describe_is_human_readable(self):
        self.assertIn("every minute", CronExpression.parse("* * * * *").describe())
        self.assertIn("day(s)", CronExpression.parse("0 0 1 * 1").describe())

    def test_rejects_invalid_expressions(self):
        for text in ("", "* * * *", "* * * * * *", "@sometimes", "@reboot", "0 0 0 * *",
                     "* * * * 8", "60 * * * *"):
            with self.subTest(text=text), self.assertRaises(CronError):
                CronExpression.parse(text)


class CrontabTest(unittest.TestCase):
    def setUp(self):
        self.tab = parse_crontab(SIMPLE_CRONTAB)

    def test_jobs_in_order_with_names(self):
        self.assertEqual(
            [job.name for job in self.tab.jobs],
            ["backup", "cleanup.sh", "legacy.sh", "warm.sh"],
        )

    def test_environment_and_timezone_are_captured(self):
        self.assertEqual(self.tab.env["MAILTO"], "ops@example.com")
        self.assertEqual(self.tab.env["CRON_TZ"], "UTC")
        self.assertTrue(all(job.timezone == "UTC" for job in self.tab.jobs))

    def test_disabled_jobs_are_reported_not_dropped(self):
        legacy = self.tab.by_name("legacy.sh")
        self.assertIsNotNone(legacy)
        assert legacy is not None
        self.assertTrue(legacy.disabled)
        self.assertEqual(legacy.expression.source, "30 2 * * *")

    def test_enabled_excludes_disabled_and_boot(self):
        names = [job.name for job in self.tab.enabled()]
        self.assertEqual(names, ["backup", "cleanup.sh"])

    def test_boot_job_has_no_expression(self):
        boot = self.tab.by_name("warm.sh")
        assert boot is not None
        self.assertTrue(boot.boot)
        self.assertIsNone(boot.expression)
        self.assertEqual(boot.schedule, "@reboot")

    def test_by_name_returns_none_when_missing(self):
        self.assertIsNone(self.tab.by_name("nope"))

    def test_line_numbers_point_at_the_source(self):
        self.assertEqual([job.line for job in self.tab.jobs], [6, 7, 8, 9])

    def test_a_comment_with_five_words_is_not_a_job(self):
        tab = parse_crontab("# this is not a cron job at all\n")
        self.assertEqual(tab.jobs, ())

    def test_cron_tz_changes_mid_file(self):
        tab = parse_crontab(
            "CRON_TZ=UTC\n0 0 * * * /bin/a\nCRON_TZ=Europe/Berlin\n0 0 * * * /bin/b\n"
        )
        self.assertEqual([job.timezone for job in tab.jobs], ["UTC", "Europe/Berlin"])

    def test_variable_values_may_be_quoted(self):
        tab = parse_crontab("MAILTO='ops@example.com'\nSHELL=\"/bin/bash\"\n")
        self.assertEqual(tab.env["MAILTO"], "ops@example.com")
        self.assertEqual(tab.env["SHELL"], "/bin/bash")

    def test_duplicate_names_are_disambiguated(self):
        tab = parse_crontab("0 0 * * * /bin/a.sh\n1 0 * * * /bin/a.sh\n")
        self.assertEqual([job.name for job in tab.jobs], ["a.sh", "a.sh#2"])

    def test_name_directive_applies_to_the_next_job_only(self):
        tab = parse_crontab("# name: first\n0 0 * * * /bin/a.sh\n0 1 * * * /bin/b.sh\n")
        self.assertEqual([job.name for job in tab.jobs], ["first", "b.sh"])

    def test_disabled_shortcut_line(self):
        tab = parse_crontab("# @hourly /bin/tick.sh\n")
        self.assertEqual(len(tab.jobs), 1)
        self.assertTrue(tab.jobs[0].disabled)
        self.assertEqual(tab.jobs[0].expression.source, "@hourly")

    def test_name_derived_from_the_script_after_the_interpreter(self):
        tab = parse_crontab("0 0 * * * /usr/bin/python3 /srv/jobs/report.py --fast\n")
        self.assertEqual(tab.jobs[0].name, "report.py")

    def test_to_dict_is_json_ready(self):
        data = self.tab.jobs[0].to_dict()
        self.assertEqual(json.loads(json.dumps(data))["name"], "backup")
        self.assertFalse(data["disabled"])
        self.assertEqual(data["schedule"], "0 3 * * *")

    def test_rejects_unparseable_lines_with_the_line_number(self):
        with self.assertRaises(CronError) as caught:
            parse_crontab("0 0 * * * /bin/ok\nnot a cron line\n")
        self.assertIn("line 2", str(caught.exception))

    def test_rejects_a_schedule_without_a_command(self):
        with self.assertRaises(CronError):
            parse_crontab("0 0 * * *\n")

    def test_rejects_a_bad_expression(self):
        with self.assertRaises(CronError):
            parse_crontab("99 0 * * * /bin/a\n")

    def test_empty_crontab_is_valid(self):
        tab = parse_crontab("\n\n# nothing\n\n")
        self.assertEqual(tab.jobs, ())
        self.assertEqual(dict(tab.env), {})


class CollisionTest(unittest.TestCase):
    def setUp(self):
        self.tab = parse_crontab(SIMPLE_CRONTAB)

    def test_jobs_that_share_a_minute_collide(self):
        same = parse_crontab("0 3 * * * /bin/a.sh\n0 3 * * * /bin/b.sh\n")
        report = find_collisions(same, utc(2026, 3, 2), utc(2026, 3, 3), threshold=60)
        self.assertEqual(len(report.collisions), 1)
        self.assertEqual(report.collisions[0].jobs, ("a.sh", "b.sh"))
        self.assertEqual(report.collisions[0].at, utc(2026, 3, 2, 3))

    def test_jobs_outside_the_threshold_do_not_collide(self):
        same = parse_crontab("0 3 * * * /bin/a.sh\n3 3 * * * /bin/b.sh\n")
        self.assertTrue(
            find_collisions(same, utc(2026, 3, 2), utc(2026, 3, 3), threshold=60).clean
        )
        self.assertEqual(
            len(find_collisions(same, utc(2026, 3, 2), utc(2026, 3, 3), threshold=600).collisions),
            1,
        )

    def test_ignore_removes_noisy_jobs(self):
        both = parse_crontab("0 3 * * * /bin/a.sh\n0 3 * * * /bin/b.sh\n")
        report = find_collisions(
            both, utc(2026, 3, 2), utc(2026, 3, 3), threshold=60, ignore=("b.sh",)
        )
        self.assertTrue(report.clean)
        self.assertEqual(set(report.counts), {"a.sh"})

    def test_counts_reflect_the_window(self):
        report = find_collisions(self.tab, utc(2026, 3, 2), utc(2026, 3, 3), threshold=60)
        self.assertEqual(report.counts["backup"], 1)
        self.assertEqual(report.counts["cleanup.sh"], 1)
        self.assertEqual(report.events, 2)
        self.assertTrue(report.clean)  # 03:00 and 03:05 are five minutes apart

    def test_collision_report_is_json_ready(self):
        same = parse_crontab("0 3 * * * /bin/a.sh\n0 3 * * * /bin/b.sh\n")
        report = find_collisions(same, utc(2026, 3, 2), utc(2026, 3, 3), threshold=60)
        data = json.loads(json.dumps(report.to_dict()))
        self.assertEqual(data["collisions"][0]["jobs"], ["a.sh", "b.sh"])
        self.assertEqual(data["threshold_seconds"], 60.0)
        self.assertEqual(data["events"], 2)

    def test_groups_are_measured_from_the_first_event(self):
        # 03:00 and 03:50 group; 04:40 and 05:30 group; the groups do not chain.
        tab = parse_crontab(
            "0 3 * * * /bin/a.sh\n50 3 * * * /bin/b.sh\n"
            "40 4 * * * /bin/c.sh\n30 5 * * * /bin/d.sh\n"
        )
        report = find_collisions(tab, utc(2026, 3, 2), utc(2026, 3, 3), threshold=3600)
        self.assertEqual(len(report.collisions), 2)
        self.assertEqual(report.collisions[0].at, utc(2026, 3, 2, 3, 0))
        self.assertEqual(report.collisions[1].at, utc(2026, 3, 2, 4, 40))

    def test_validation(self):
        with self.assertRaises(ValueError):
            find_collisions(self.tab, utc(2026, 3, 3), utc(2026, 3, 2))
        with self.assertRaises(ValueError):
            find_collisions(self.tab, utc(2026, 3, 2), utc(2026, 3, 3), threshold=-1)

    def test_unknown_timezone_falls_back_to_utc(self):
        self.assertEqual(_zone("Nowhere/Atlantis"), timezone.utc)
        self.assertEqual(_zone(""), timezone.utc)


class RunParsingTest(unittest.TestCase):
    def test_loads_a_bare_list(self):
        runs = load_runs([{"job": "a", "started": "2026-03-02T03:00:00Z", "exit_code": 0}])
        self.assertEqual(runs[0].job, "a")
        self.assertEqual(runs[0].started, utc(2026, 3, 2, 3))

    def test_loads_the_wrapped_form(self):
        payload = {"runs": [{"name": "b", "started_at": "2026-03-02T03:00:00+00:00"}]}
        runs = load_runs(payload)
        self.assertEqual(runs[0].job, "b")
        self.assertIsNone(runs[0].exit_code)

    def test_null_exit_code_is_not_zero(self):
        runs = load_runs([{"job": "a", "started": "2026-03-02T03:00:00Z", "exit_code": None,
                           "duration": 5}])
        self.assertIsNone(runs[0].exit_code)
        self.assertEqual(runs[0].duration, 5.0)

    def test_validation_errors(self):
        with self.assertRaises(ValueError):
            load_runs("nope")
        with self.assertRaises(ValueError):
            load_runs({"runs": "nope"})
        for item in (
            {"started": "2026-03-02T03:00:00Z"},
            {"job": "a"},
            {"job": "a", "started": 123},
            {"job": "a", "started": "2026-03-02T03:00:00Z", "exit_code": "1"},
            {"job": "a", "started": "2026-03-02T03:00:00Z", "duration": "5"},
            "not an object",
        ):
            with self.subTest(item=item), self.assertRaises(ValueError):
                load_runs([item])

    def test_parse_moment_accepts_z_and_rejects_garbage(self):
        self.assertEqual(parse_moment("2026-03-02T03:00:00Z").tzinfo, timezone.utc)
        self.assertEqual(parse_moment("2026-03-02").hour, 0)
        with self.assertRaises(ValueError):
            parse_moment("not a date")


class DriftTest(unittest.TestCase):
    def setUp(self):
        self.tab = parse_crontab("0 3 * * * /bin/backup.sh\n")
        self.start = utc(2026, 3, 1)
        self.end = utc(2026, 3, 4)

    def run_at(self, moment, *, job="backup.sh", exit_code=0, duration=None):
        return Run(job=job, started=moment, exit_code=exit_code, duration=duration)

    def analyze(self, runs, **kwargs):
        options = dict(start=self.start, end=self.end, grace=300, late_after=60)
        options.update(kwargs)
        return analyze_drift(self.tab, runs, **options)

    def test_a_perfect_history_is_clean(self):
        runs = [self.run_at(utc(2026, 3, day, 3)) for day in (1, 2, 3)]
        report = self.analyze(runs)
        self.assertTrue(report.clean)
        self.assertEqual(report.jobs_checked, 1)
        self.assertEqual(report.runs_checked, 3)

    def test_a_missing_run_is_reported(self):
        report = self.analyze([self.run_at(utc(2026, 3, 1, 3)), self.run_at(utc(2026, 3, 3, 3))])
        self.assertEqual(report.counts(), {"missed": 1})
        issue = report.issues[0]
        self.assertEqual(issue.expected, utc(2026, 3, 2, 3))

    def test_a_late_run_still_matches_inside_the_grace_window(self):
        report = self.analyze([self.run_at(utc(2026, 3, 1, 3, 4))], start=self.start,
                             end=utc(2026, 3, 2))
        self.assertEqual(report.counts(), {"late": 1})
        self.assertIn("240s late", report.issues[0].detail)

    def test_a_late_run_beyond_grace_is_missed_plus_unexpected(self):
        report = self.analyze([self.run_at(utc(2026, 3, 1, 4, 0))], start=self.start,
                             end=utc(2026, 3, 2))
        self.assertEqual(report.counts(), {"missed": 1, "unexpected": 1})

    def test_a_slightly_early_run_matches(self):
        report = self.analyze([self.run_at(utc(2026, 3, 1, 2, 59, 56))], start=self.start,
                             end=utc(2026, 3, 2))
        self.assertTrue(report.clean)

    def test_a_failed_run_is_reported(self):
        report = self.analyze([self.run_at(utc(2026, 3, 1, 3), exit_code=7)], start=self.start,
                             end=utc(2026, 3, 2))
        self.assertEqual(report.counts(), {"failed": 1})
        self.assertIn("exit code 7", report.issues[0].detail)

    def test_a_null_exit_code_is_not_a_failure(self):
        report = self.analyze([Run("backup.sh", utc(2026, 3, 1, 3))], start=self.start,
                             end=utc(2026, 3, 2))
        self.assertTrue(report.clean)

    def test_a_run_longer_than_its_interval_is_slow(self):
        tab = parse_crontab("*/10 * * * * /bin/probe.sh\n")
        report = analyze_drift(
            tab,
            [Run("probe.sh", utc(2026, 3, 1, 3, 10), 0, 700)],
            start=utc(2026, 3, 1, 3, 10),
            end=utc(2026, 3, 1, 3, 10),
        )
        self.assertEqual(report.counts(), {"slow": 1})
        self.assertIn("next run starts in 600s", report.issues[0].detail)

    def test_a_run_for_an_unknown_job_is_reported(self):
        report = self.analyze([self.run_at(utc(2026, 3, 1, 3), job="mystery.sh")])
        self.assertIn("unknown-job", report.counts())
        self.assertEqual(report.counts()["unknown-job"], 1)

    def test_a_run_for_a_disabled_job_is_reported(self):
        tab = parse_crontab("0 3 * * * /bin/backup.sh\n# 0 4 * * * /bin/old.sh\n")
        report = analyze_drift(
            tab,
            [Run("backup.sh", utc(2026, 3, 1, 3)), Run("old.sh", utc(2026, 3, 1, 4))],
            start=self.start,
            end=utc(2026, 3, 2),
        )
        self.assertEqual(report.counts(), {"disabled": 1})
        self.assertEqual(report.issues[0].job, "old.sh")

    def test_report_is_json_ready_and_counts_add_up(self):
        runs = [
            self.run_at(utc(2026, 3, 1, 3), exit_code=1),
            self.run_at(utc(2026, 3, 2, 9), job="mystery.sh"),
        ]
        report = self.analyze(runs, start=self.start, end=utc(2026, 3, 2))
        data = json.loads(json.dumps(report.to_dict()))
        self.assertEqual(sum(data["counts"].values()), len(data["issues"]))
        self.assertEqual(data["jobs_checked"], 1)

    def test_validation(self):
        with self.assertRaises(ValueError):
            analyze_drift(self.tab, [], start=self.end, end=self.start)
        with self.assertRaises(ValueError):
            analyze_drift(self.tab, [], start=self.start, end=self.end, grace=-1)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.crontab = os.path.join(self.folder.name, "crontab.txt")
        with open(self.crontab, "w", encoding="utf-8") as handle:
            handle.write(SIMPLE_CRONTAB)
        self.runs = os.path.join(self.folder.name, "runs.json")
        with open(self.runs, "w", encoding="utf-8") as handle:
            json.dump({"runs": [
                {"job": "backup", "started": "2026-03-01T03:00:00Z", "exit_code": 0},
                {"job": "cleanup.sh", "started": "2026-03-01T03:05:00Z", "exit_code": 0},
                {"job": "mystery.sh", "started": "2026-03-01T03:02:00Z", "exit_code": 0},
            ]}, handle)

    def tearDown(self):
        self.folder.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_jobs_table_and_json(self):
        code, out, _err = self.run_cli("jobs", self.crontab)
        self.assertEqual(code, 0)
        self.assertIn("backup", out)
        self.assertIn("[disabled]", out)

        code, out, _err = self.run_cli("jobs", self.crontab, "--json")
        data = json.loads(out)
        self.assertEqual(len(data["jobs"]), 4)
        self.assertEqual(data["env"]["MAILTO"], "ops@example.com")

    def test_expand_shows_the_next_runs(self):
        code, out, _err = self.run_cli(
            "expand", self.crontab, "--job", "backup", "--count", "2",
            "--from", "2026-03-01T00:00:00Z", "--json",
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["next"]["backup"],
                         ["2026-03-01T03:00:00+00:00", "2026-03-02T03:00:00+00:00"])

    def test_expand_of_a_missing_job_is_an_error(self):
        code, _out, err = self.run_cli("expand", self.crontab, "--job", "nope")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("no enabled job", err)

    def test_check_exits_one_on_collisions(self):
        colliding = os.path.join(self.folder.name, "collide.txt")
        with open(colliding, "w", encoding="utf-8") as handle:
            handle.write("0 3 * * * /bin/a.sh\n0 3 * * * /bin/b.sh\n")
        code, out, _err = self.run_cli(
            "check", colliding, "--from", "2026-03-01T00:00:00Z",
            "--until", "2026-03-02T00:00:00Z", "--json",
        )
        self.assertEqual(code, cli.EXIT_FINDINGS)
        data = json.loads(out)
        self.assertEqual(data["collisions"][0]["jobs"], ["a.sh", "b.sh"])

    def test_check_is_clean_when_nothing_overlaps(self):
        code, out, _err = self.run_cli(
            "check", self.crontab, "--from", "2026-03-01T00:00:00Z",
            "--until", "2026-03-02T00:00:00Z", "--json",
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(out)["collisions"], [])

    def test_check_honours_ignore(self):
        colliding = os.path.join(self.folder.name, "collide.txt")
        with open(colliding, "w", encoding="utf-8") as handle:
            handle.write("0 3 * * * /bin/a.sh\n0 3 * * * /bin/b.sh\n")
        code, _out, _err = self.run_cli(
            "check", colliding, "--from", "2026-03-01T00:00:00Z",
            "--until", "2026-03-02T00:00:00Z", "--ignore", "b.sh",
        )
        self.assertEqual(code, cli.EXIT_OK)

    def test_drift_exits_one_when_history_disagrees(self):
        code, out, _err = self.run_cli(
            "drift", self.crontab, "--runs", self.runs,
            "--from", "2026-03-01T03:00:00Z", "--until", "2026-03-01T03:10:00Z", "--json",
        )
        self.assertEqual(code, cli.EXIT_FINDINGS)
        data = json.loads(out)
        self.assertEqual(data["counts"], {"unknown-job": 1})
        self.assertEqual(data["issues"][0]["job"], "mystery.sh")

    def test_drift_is_clean_when_every_run_matches(self):
        clean_runs = os.path.join(self.folder.name, "clean.json")
        with open(clean_runs, "w", encoding="utf-8") as handle:
            json.dump([{"job": "cleanup.sh", "started": "2026-03-01T03:05:00Z"}], handle)
        code, out, _err = self.run_cli(
            "drift", self.crontab, "--runs", clean_runs,
            "--from", "2026-03-01T03:05:00Z", "--until", "2026-03-01T03:10:00Z", "--json",
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(out)["issues"], [])

    def test_parse_expands_one_expression(self):
        code, out, _err = self.run_cli(
            "parse", "0 3 * * *", "--count", "2", "--from", "2026-03-01T00:00:00Z", "--json"
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["next"], ["2026-03-01T03:00:00+00:00", "2026-03-02T03:00:00+00:00"])

    def test_parse_rejects_a_bad_expression(self):
        code, _out, err = self.run_cli("parse", "nonsense")
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("expected 5 fields", err)

    def test_missing_file_is_an_error(self):
        code, _out, err = self.run_cli("jobs", os.path.join(self.folder.name, "nope.txt"))
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("cannot read", err)

    def test_bad_runs_file_is_an_error(self):
        broken = os.path.join(self.folder.name, "broken.json")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        code, _out, err = self.run_cli("drift", self.crontab, "--runs", broken)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("not valid JSON", err)

    def test_negative_hours_is_an_error(self):
        code, _out, err = self.run_cli(
            "check", self.crontab, "--hours", "0",
            "--from", "2026-03-01T00:00:00Z",
        )
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("--hours", err)


class ModuleTest(unittest.TestCase):
    def test_exports_and_version(self):
        import cronaudit

        self.assertEqual(cronaudit.__version__, "1.0.0")
        for name in ("CronExpression", "parse_crontab", "find_collisions", "analyze_drift"):
            self.assertTrue(hasattr(cronaudit, name))

    def test_timezone_fallback_is_utc(self):
        tab = parse_crontab("CRON_TZ=Nowhere/Nowhere\n0 3 * * * /bin/a.sh\n")
        run = Run("a.sh", utc(2026, 3, 1, 3))
        report = analyze_drift(tab, [run], start=utc(2026, 3, 1), end=utc(2026, 3, 2))
        self.assertTrue(report.clean)


if __name__ == "__main__":
    unittest.main()
