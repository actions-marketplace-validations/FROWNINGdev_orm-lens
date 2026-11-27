"""CI-facing output formats — SARIF 2.1.0, GitHub workflow commands and
GitLab Code Quality reports.

Why a dedicated module
----------------------
``nplusone`` and ``migration-risk`` already exit ``1`` on findings, which
gates a pipeline — but a red ✗ with no annotations makes reviewers dig
through raw logs. These serializers turn findings into the two formats CI
platforms render natively:

* ``sarif``  — SARIF 2.1.0 for GitHub Code Scanning
  (``github/codeql-action/upload-sarif``) and any SARIF viewer. Findings
  show up in the repo Security tab and as PR annotations, with per-rule
  metadata and help links into ``docs/rules/``.
* ``github`` — GitHub Actions workflow commands
  (``::warning file=F,line=N::msg``). Zero-permission PR annotations: no
  upload step, no ``security-events: write`` scope — print and done.
* ``gitlab`` — a GitLab Code Quality report (the CodeClimate subset GitLab
  reads from ``artifacts:reports:codequality``). Findings show up in the
  merge request widget and the pipeline's Code Quality tab.

All are pure functions over the analyzer dataclasses (no filesystem, no
network) so they stay unit-testable and reusable from the MCP layer later.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import __version__
from .migrations_parser import MigrationRisk
from .query_analyzer import NPlusOneFinding

_SARIF_SCHEMA = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/"
    "Schemata/sarif-schema-2.1.0.json"
)
_INFO_URI = "https://github.com/FROWNINGdev/orm-lens"
_RULES_DOCS = f"{_INFO_URI}/blob/main/docs/rules"

# migration-risk severity → SARIF result level
_SEVERITY_LEVEL = {"critical": "error", "warning": "warning", "info": "note"}
# nplusone confidence → SARIF result level
_CONFIDENCE_LEVEL = {"high": "warning", "medium": "note"}
# same mappings for workflow commands (GitHub calls "note" "notice")
_SEVERITY_CMD = {"critical": "error", "warning": "warning", "info": "notice"}
_CONFIDENCE_CMD = {"high": "warning", "medium": "notice"}
# GitLab Code Quality severities: info | minor | major | critical | blocker
_SEVERITY_GITLAB = {"critical": "critical", "warning": "major", "info": "info"}
_CONFIDENCE_GITLAB = {"high": "major", "medium": "minor"}


def _checkout_relative(path: Path) -> str | None:
    """``path`` relative to the working directory, or ``None`` outside it.

    GitHub resolves an annotation's file against the checkout root, which is
    where a workflow runs the CLI. With ``--path backend`` a finding in
    ``backend/shop/views.py`` must say so, not ``shop/views.py``.
    """
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _rel_posix(root: str | None, path: str) -> str:
    """Checkout-relative POSIX path for annotation URIs.

    migration-risk paths are whatever the walk produced (absolute root →
    absolute paths). An absolute path under the working directory is made
    relative to it; one elsewhere falls back to the root, and this never
    raises.
    """
    p = Path(path)
    if root is not None and p.is_absolute():
        inside = _checkout_relative(p)
        if inside is not None:
            return inside
        try:
            return p.relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            pass
    return p.as_posix()


def _nplusone_path(root: str | None, file: str) -> str:
    """Checkout-relative path of an N+1 finding.

    ``NPlusOneFinding.file`` is relative to the scanned root, so join the two
    before making the path relative to the working directory.
    """
    if root is not None:
        inside = _checkout_relative(Path(root) / file)
        if inside is not None:
            return inside
    return Path(file).as_posix()


def _location(uri: str, line: int) -> dict[str, Any]:
    # Plain relative URIs — GitHub resolves them against the checkout root.
    # A uriBaseId reference without a matching run.originalUriBaseIds entry
    # is a SARIF 2.1.0 conformance gap, so we don't emit one.
    return {
        "physicalLocation": {
            "artifactLocation": {"uri": uri},
            "region": {"startLine": max(1, int(line or 1))},
        }
    }


def _sarif_doc(
    rules: list[dict[str, Any]], results: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "$schema": _SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        # Pre-rename name on purpose: code scanning keys alerts on the
                        # tool name, and a new one would orphan every open alert.
                        "name": "django-orm-lens",
                        "informationUri": _INFO_URI,
                        "version": __version__,
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


def _nplusone_message(f: NPlusOneFinding) -> str:
    return (
        f"Possible N+1: loop over '{f.queryset_var}' accesses "
        f"{', '.join(f.accessed)} per iteration. Fix: {f.suggested_fix}"
    )


def nplusone_sarif(
    findings: Iterable[NPlusOneFinding], root: str | None = None
) -> dict[str, Any]:
    """SARIF 2.1.0 document for ``orm-lens nplusone`` findings."""
    rule = {
        "id": "nplusone",
        "name": "NPlusOneQuery",
        "shortDescription": {
            "text": (
                "Related-object access inside a loop without eager loading "
                "(select_related/prefetch_related, selectinload/joinedload)"
            )
        },
        "helpUri": f"{_RULES_DOCS}/nplusone.md",
        "defaultConfiguration": {"level": "warning"},
    }
    results = [
        {
            "ruleId": "nplusone",
            "level": _CONFIDENCE_LEVEL.get(f.confidence, "note"),
            "message": {"text": _nplusone_message(f)},
            "locations": [_location(_nplusone_path(root, f.file), f.line)],
            "properties": {
                "confidence": f.confidence,
                "loopVar": f.loop_var,
                "querysetVar": f.queryset_var,
            },
        }
        for f in findings
    ]
    return _sarif_doc([rule], results)


def migration_risks_sarif(
    findings: Iterable[MigrationRisk], root: str | None = None
) -> dict[str, Any]:
    """SARIF 2.1.0 document for ``orm-lens migration-risk`` findings."""
    findings = list(findings)
    rules: list[dict[str, Any]] = []
    seen: set = set()
    for f in findings:
        if f.rule in seen:
            continue
        seen.add(f.rule)
        rules.append(
            {
                "id": f.rule,
                "name": "".join(w.capitalize() for w in f.rule.split("_")),
                "shortDescription": {"text": f.rule.replace("_", " ")},
                "helpUri": f"{_RULES_DOCS}/migrations.md#{f.rule}",
                "defaultConfiguration": {
                    "level": _SEVERITY_LEVEL.get(f.severity, "warning")
                },
            }
        )
    results = [
        {
            "ruleId": f.rule,
            "level": _SEVERITY_LEVEL.get(f.severity, "warning"),
            "message": {"text": f"{f.description} Mitigation: {f.mitigation}"},
            "locations": [
                _location(_rel_posix(root, f.file_path), f.line_number)
            ],
            "properties": {
                "app": f.app,
                "migration": f.migration,
                "operation": f.operation,
                "confidence": f.confidence,
            },
        }
        for f in findings
    ]
    return _sarif_doc(rules, results)


def _esc_data(s: str) -> str:
    """Escape a workflow-command message per GitHub's escaping rules."""
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _esc_prop(s: str) -> str:
    """Escape a workflow-command property (file=, title=) value."""
    return _esc_data(s).replace(":", "%3A").replace(",", "%2C")


def nplusone_github(
    findings: Iterable[NPlusOneFinding], root: str | None = None
) -> str:
    """GitHub Actions ``::warning``/``::notice`` lines, one per finding."""
    lines = []
    for f in findings:
        cmd = _CONFIDENCE_CMD.get(f.confidence, "notice")
        path = _nplusone_path(root, f.file)
        lines.append(
            f"::{cmd} file={_esc_prop(path)},line={f.line},"
            f"title={_esc_prop('orm-lens: N+1 query')}"
            f"::{_esc_data(_nplusone_message(f))}"
        )
    return "\n".join(lines)


def migration_risks_github(
    findings: Iterable[MigrationRisk], root: str | None = None
) -> str:
    """GitHub Actions annotation lines for migration risks."""
    lines = []
    for f in findings:
        cmd = _SEVERITY_CMD.get(f.severity, "warning")
        path = _rel_posix(root, f.file_path)
        msg = f"{f.description} Mitigation: {f.mitigation}"
        lines.append(
            f"::{cmd} file={_esc_prop(path)},line={f.line_number},"
            f"title={_esc_prop('orm-lens: ' + f.rule)}"
            f"::{_esc_data(msg)}"
        )
    return "\n".join(lines)


def blast_radius_github(report: Any, root: str | None = None) -> str:
    """GitHub Actions annotations for a blast-radius report.

    Annotates the migration line itself — that is the line a reviewer can act
    on — and folds the reference count into the message so the consequence is
    visible without opening a second tab. ``certain`` references against a
    destructive operation are the signal worth interrupting for, so they are
    named in the annotation title too.

    ``report`` is typed ``Any`` to keep this module free of an import cycle:
    :mod:`.blast_radius` imports these emitters, never the other way round.
    """
    lines = []
    for target in report.targets:
        certain = target.impact_counts()["certain"]
        for r in target.risks:
            cmd = _SEVERITY_CMD.get(r.severity, "warning")
            path = _rel_posix(root, r.file_path)
            msg = f"{r.description} Mitigation: {r.mitigation}"
            title = f"orm-lens: {r.rule}"
            if certain:
                msg = (
                    f"{msg} Blast radius: {certain} certain reference(s) to "
                    f"{target.label} still in the codebase."
                )
                title = f"{title} ({certain} certain reference(s))"
            lines.append(
                f"::{cmd} file={_esc_prop(path)},line={r.line_number},"
                f"title={_esc_prop(title)}::{_esc_data(msg)}"
            )
    for r in report.unscanned_risks:
        cmd = _SEVERITY_CMD.get(r.severity, "warning")
        path = _rel_posix(root, r.file_path)
        msg = f"{r.description} Mitigation: {r.mitigation}"
        lines.append(
            f"::{cmd} file={_esc_prop(path)},line={r.line_number},"
            f"title={_esc_prop('orm-lens: ' + r.rule)}::{_esc_data(msg)}"
        )
    return "\n".join(lines)


def _gitlab_issue(
    check_name: str, description: str, severity: str, path: str, line: int,
    key: tuple[str, ...], seen: dict[str, int],
) -> dict[str, Any]:
    """One Code Quality entry.

    The fingerprint is how GitLab matches a finding between the source and
    target branch of a merge request, so it leaves the line number out: a
    finding that only moved because lines were added above it is not new.
    Two findings with the same key in one report get an occurrence index so
    they stay distinct.
    """
    base = "".join((check_name, path, *key))
    n = seen.get(base, 0)
    seen[base] = n + 1
    digest = hashlib.md5(f"{base}{n}".encode(), usedforsecurity=False)
    return {
        "description": description,
        "check_name": check_name,
        "fingerprint": digest.hexdigest(),
        "severity": severity,
        # GitLab wants a repository-relative path without a leading "./".
        "location": {
            "path": path.removeprefix("./"),
            "lines": {"begin": max(1, int(line or 1))},
        },
    }


def nplusone_gitlab(
    findings: Iterable[NPlusOneFinding], root: str | None = None
) -> list[dict[str, Any]]:
    """GitLab Code Quality report for ``orm-lens nplusone`` findings."""
    seen: dict[str, int] = {}
    return [
        _gitlab_issue(
            "orm-lens/nplusone",
            _nplusone_message(f),
            _CONFIDENCE_GITLAB.get(f.confidence, "minor"),
            _nplusone_path(root, f.file),
            f.line,
            (f.loop_var, f.queryset_var, ",".join(sorted(f.accessed))),
            seen,
        )
        for f in findings
    ]


def _risk_gitlab(
    r: MigrationRisk, path: str, description: str, seen: dict[str, int]
) -> dict[str, Any]:
    return _gitlab_issue(
        f"orm-lens/{r.rule}",
        description,
        _SEVERITY_GITLAB.get(r.severity, "major"),
        path,
        r.line_number,
        (r.app, r.migration, r.operation, r.model or "", r.field or ""),
        seen,
    )


def migration_risks_gitlab(
    findings: Iterable[MigrationRisk], root: str | None = None
) -> list[dict[str, Any]]:
    """GitLab Code Quality report for ``orm-lens migration-risk`` findings."""
    seen: dict[str, int] = {}
    return [
        _risk_gitlab(
            f,
            _rel_posix(root, f.file_path),
            f"{f.description} Mitigation: {f.mitigation}",
            seen,
        )
        for f in findings
    ]


def blast_radius_gitlab(report: Any, root: str | None = None) -> list[dict[str, Any]]:
    """GitLab Code Quality report for a blast-radius report.

    Same placement as :func:`blast_radius_github`: each entry sits on the
    migration line, with the count of ``certain`` references still in the
    codebase folded into the description.
    """
    seen: dict[str, int] = {}
    issues = []
    for target in report.targets:
        certain = target.impact_counts()["certain"]
        for r in target.risks:
            msg = f"{r.description} Mitigation: {r.mitigation}"
            if certain:
                msg = (
                    f"{msg} Blast radius: {certain} certain reference(s) to "
                    f"{target.label} still in the codebase."
                )
            issues.append(_risk_gitlab(r, _rel_posix(root, r.file_path), msg, seen))
    for r in report.unscanned_risks:
        msg = f"{r.description} Mitigation: {r.mitigation}"
        issues.append(_risk_gitlab(r, _rel_posix(root, r.file_path), msg, seen))
    return issues
