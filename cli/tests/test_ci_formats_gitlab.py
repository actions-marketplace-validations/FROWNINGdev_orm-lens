"""Tests for the GitLab Code Quality serializers in ci_formats.

GitLab reads ``artifacts:reports:codequality`` as a JSON array of
CodeClimate-style issues and drops anything it can't parse without telling
anyone. These tests pin the required keys, the severity mapping, the
repository-relative path and the fingerprint, which GitLab uses to decide
whether a finding in a merge request is new.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orm_lens.blast_radius import analyze_blast_radius
from orm_lens.ci_formats import (
    blast_radius_gitlab,
    migration_risks_gitlab,
    nplusone_gitlab,
)
from orm_lens.migrations_parser import MigrationRisk
from orm_lens.query_analyzer import NPlusOneFinding

REQUIRED_KEYS = {"description", "check_name", "fingerprint", "severity", "location"}
GITLAB_SEVERITIES = {"info", "minor", "major", "critical", "blocker"}
BLAST_FIXTURE = Path(__file__).parent / "fixtures" / "blast_radius"


def _nplusone_finding(**overrides) -> NPlusOneFinding:
    base = dict(
        file="shop/views.py",
        line=12,
        loop_var="order",
        queryset_var="orders",
        accessed=["customer"],
        suggested_fix='.select_related("customer")',
        confidence="high",
    )
    base.update(overrides)
    return NPlusOneFinding(**base)


def _migration_risk(**overrides) -> MigrationRisk:
    base = dict(
        file_path="orders/migrations/0002_add_status.py",
        line_number=7,
        app="orders",
        migration="0002_add_status",
        operation="AddField",
        model="order",
        field="status",
        rule="add_field_not_null_no_default",
        description="Adds NOT NULL column 'status' without a default.",
        mitigation="Add null=True first, backfill, then set null=False.",
        confidence="high",
        severity="critical",
    )
    base.update(overrides)
    return MigrationRisk(**base)


class GitLabShapeTest(unittest.TestCase):
    def test_issue_has_the_required_keys(self) -> None:
        for issue in (
            nplusone_gitlab([_nplusone_finding()])[0],
            migration_risks_gitlab([_migration_risk()])[0],
        ):
            self.assertTrue(issue.keys() >= REQUIRED_KEYS)
            self.assertIn(issue["severity"], GITLAB_SEVERITIES)
            self.assertIsInstance(issue["location"]["lines"]["begin"], int)
            self.assertRegex(issue["fingerprint"], r"^[0-9a-f]{32}$")

    def test_migration_risk_fields(self) -> None:
        issue = migration_risks_gitlab([_migration_risk()])[0]
        self.assertEqual(issue["check_name"], "orm-lens/add_field_not_null_no_default")
        self.assertEqual(
            issue["location"],
            {"path": "orders/migrations/0002_add_status.py", "lines": {"begin": 7}},
        )
        self.assertIn("Mitigation: Add null=True first", issue["description"])

    def test_nplusone_fields(self) -> None:
        issue = nplusone_gitlab([_nplusone_finding()])[0]
        self.assertEqual(issue["check_name"], "orm-lens/nplusone")
        self.assertEqual(issue["location"]["path"], "shop/views.py")
        self.assertIn('select_related("customer")', issue["description"])

    def test_severity_mapping(self) -> None:
        sev = [
            migration_risks_gitlab([_migration_risk(severity=s)])[0]["severity"]
            for s in ("critical", "warning", "info")
        ]
        self.assertEqual(sev, ["critical", "major", "info"])
        conf = [
            nplusone_gitlab([_nplusone_finding(confidence=c)])[0]["severity"]
            for c in ("high", "medium")
        ]
        self.assertEqual(conf, ["major", "minor"])

    def test_empty_findings_produce_an_empty_array(self) -> None:
        self.assertEqual(nplusone_gitlab([]), [])
        self.assertEqual(migration_risks_gitlab([]), [])
        self.assertEqual(json.dumps(migration_risks_gitlab([])), "[]")

    def test_line_zero_is_clamped_to_one(self) -> None:
        issue = migration_risks_gitlab([_migration_risk(line_number=0)])[0]
        self.assertEqual(issue["location"]["lines"]["begin"], 1)


class GitLabFingerprintTest(unittest.TestCase):
    def test_moving_a_finding_keeps_its_fingerprint(self) -> None:
        # Lines added above a finding must not make it "new" in the MR widget.
        before = migration_risks_gitlab([_migration_risk(line_number=7)])[0]
        after = migration_risks_gitlab([_migration_risk(line_number=40)])[0]
        self.assertEqual(before["fingerprint"], after["fingerprint"])

        before = nplusone_gitlab([_nplusone_finding(line=12)])[0]
        after = nplusone_gitlab([_nplusone_finding(line=90)])[0]
        self.assertEqual(before["fingerprint"], after["fingerprint"])

    def test_different_findings_get_different_fingerprints(self) -> None:
        issues = migration_risks_gitlab(
            [
                _migration_risk(),
                _migration_risk(field="total"),
                _migration_risk(rule="add_index_non_concurrent"),
                _migration_risk(file_path="orders/migrations/0003_more.py"),
            ]
        )
        self.assertEqual(len({i["fingerprint"] for i in issues}), 4)

    def test_identical_findings_in_one_report_stay_distinct(self) -> None:
        issues = nplusone_gitlab(
            [_nplusone_finding(line=12), _nplusone_finding(line=30)]
        )
        self.assertNotEqual(issues[0]["fingerprint"], issues[1]["fingerprint"])

    def test_fingerprint_is_deterministic(self) -> None:
        a = nplusone_gitlab([_nplusone_finding()])[0]["fingerprint"]
        b = nplusone_gitlab([_nplusone_finding()])[0]["fingerprint"]
        self.assertEqual(a, b)


class GitLabPathTest(unittest.TestCase):
    def test_paths_under_a_subdirectory_root_stay_checkout_relative(self) -> None:
        # `orm-lens migration-risk --path backend` run from the repository
        # root: GitLab resolves location.path against the repository.
        with TemporaryDirectory() as tmp:
            checkout = Path(tmp).resolve()
            migration = checkout / "backend" / "orders" / "migrations" / "0002.py"
            previous = os.getcwd()
            os.chdir(checkout)
            try:
                for root in ("backend", str(checkout / "backend")):
                    risk = _migration_risk(
                        file_path=str(migration)
                        if Path(root).is_absolute()
                        else "backend/orders/migrations/0002.py"
                    )
                    issue = migration_risks_gitlab([risk], root=root)[0]
                    self.assertEqual(
                        issue["location"]["path"], "backend/orders/migrations/0002.py"
                    )

                    finding = _nplusone_finding(file="shop/views.py")
                    issue = nplusone_gitlab([finding], root=root)[0]
                    self.assertEqual(issue["location"]["path"], "backend/shop/views.py")
            finally:
                os.chdir(previous)

    def test_leading_dot_slash_is_dropped(self) -> None:
        issue = migration_risks_gitlab(
            [_migration_risk(file_path="./orders/migrations/0002_add_status.py")]
        )[0]
        self.assertEqual(
            issue["location"]["path"], "orders/migrations/0002_add_status.py"
        )


class GitLabBlastRadiusTest(unittest.TestCase):
    def test_report_carries_the_certain_reference_count(self) -> None:
        report = analyze_blast_radius(BLAST_FIXTURE)
        issues = blast_radius_gitlab(report, str(BLAST_FIXTURE.resolve()))
        self.assertTrue(issues)
        for issue in issues:
            self.assertTrue(issue.keys() >= REQUIRED_KEYS)
            self.assertFalse(Path(issue["location"]["path"]).is_absolute())
        self.assertTrue(any("Blast radius:" in i["description"] for i in issues))


class GitLabCliTest(unittest.TestCase):
    def test_cli_prints_a_json_array(self) -> None:
        cli_dir = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": str(cli_dir), "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run(
            [
                sys.executable, "-m", "orm_lens", "migration-risk",
                "--path", str(BLAST_FIXTURE), "--format", "gitlab",
                "--severity", "all", "--exit-zero",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertIsInstance(data, list)
        self.assertTrue(data)
        self.assertTrue(data[0].keys() >= REQUIRED_KEYS)


if __name__ == "__main__":
    unittest.main()
