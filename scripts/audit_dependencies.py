"""Audit locked requirements without hiding findings from the raw report."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

# These authorization-server features are not used by Ring's password-grant
# client. Keep each family separate: a new alias requires a fresh review.
# The package and version checks below are part of the exception. See SECURITY.md.
_OAUTHLIB_ADVISORIES = (
    frozenset({"CVE-2026-49265", "GHSA-xpv3-w29h-x7cv", "PYSEC-2026-4114"}),
    frozenset({"CVE-2026-49272", "GHSA-3jp2-3mrg-2vxc"}),
)


def expected_dependencies(requirements: Path) -> set[tuple[str, str]]:
    """Read the active exact pins from uv's hash-locked requirements export."""
    expected = set()
    for line in requirements.read_text(encoding="utf-8").splitlines():
        line = line.strip().removesuffix("\\").strip()
        if not line or line.startswith(("#", "--hash=")):
            continue
        requirement = Requirement(line)
        pins = list(requirement.specifier)
        if (
            requirement.url
            or requirement.extras
            or len(pins) != 1
            or pins[0].operator != "=="
            or "*" in pins[0].version
        ):
            raise ValueError(f"Requirement is not an exact package pin: {line}")
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        entry = (canonicalize_name(requirement.name), pins[0].version)
        if any(name == entry[0] for name, _ in expected):
            raise ValueError(f"Duplicate active requirement: {requirement.name}")
        expected.add(entry)
    if not expected:
        raise ValueError("Requirements export has no active dependencies")
    return expected


def review_report(report: object, expected: set[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """Return reviewed exceptions and blocking findings; reject incomplete data."""
    if not isinstance(report, dict):
        raise ValueError("Audit report is not an object")
    dependencies = report.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        raise ValueError("Audit report has no dependency results")
    seen: set[tuple[str, str]] = set()
    exceptions, findings = set(), set()
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise ValueError("Invalid dependency result")
        name, version = dependency.get("name"), dependency.get("version")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise ValueError("Dependency result is missing its package or version")
        if dependency.get("skip_reason"):
            raise ValueError(f"Dependency was not audited: {name}")
        entry = (canonicalize_name(name), version)
        if entry in seen:
            raise ValueError(f"Duplicate dependency result: {name}=={version}")
        seen.add(entry)
        vulnerabilities = dependency.get("vulns")
        if not isinstance(vulnerabilities, list):
            raise ValueError(f"Dependency result has no vulnerability list: {name}")
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                raise ValueError(f"Invalid vulnerability result: {name}")
            identifier = vulnerability.get("id")
            aliases = vulnerability.get("aliases", [])
            if (
                not isinstance(identifier, str)
                or not identifier
                or not isinstance(aliases, list)
                or any(not isinstance(alias, str) or not alias for alias in aliases)
            ):
                raise ValueError(f"Invalid vulnerability identifiers: {name}")
            identifiers = {identifier, *aliases}
            summary = f"{name}=={version}: {', '.join(sorted(identifiers))}"
            reviewed = (
                name == "oauthlib"
                and version == "3.3.1"
                and any(identifiers <= family for family in _OAUTHLIB_ADVISORIES)
            )
            (exceptions if reviewed else findings).add(summary)
    if not expected or seen != expected:
        raise ValueError(
            f"Audit coverage differs from requirements: missing={sorted(expected - seen)}, "
            f"unexpected={sorted(seen - expected)}"
        )
    return sorted(exceptions), sorted(findings)


def audit_requirements(requirements: Path, report_path: Path) -> int:
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        # A failed invocation must never reuse a report from a previous run.
        report_path.unlink(missing_ok=True)
        expected = expected_dependencies(requirements)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "--requirement",
                str(requirements),
                "--require-hashes",
                "--disable-pip",
                "--progress-spinner",
                "off",
                "--format",
                "json",
                "--output",
                str(report_path),
                "--timeout",
                "20",
            ],
            check=False,
            timeout=180,
        )
        if result.returncode not in (0, 1):
            raise ValueError(f"pip-audit failed with exit status {result.returncode}")
        exceptions, findings = review_report(
            json.loads(report_path.read_text(encoding="utf-8")), expected
        )
        if (result.returncode == 1) != bool(exceptions or findings):
            raise ValueError("pip-audit exit status contradicts its vulnerability results")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Dependency audit failed: {exc}", file=sys.stderr)
        return 2
    print(f"Raw dependency audit: {report_path}")
    for exception in exceptions:
        print(f"Reviewed authorization-server exception: {exception}")
    for finding in findings:
        print(f"Blocking vulnerability: {finding}", file=sys.stderr)
    if findings:
        return 1
    print(f"Dependency audit passed; {len(expected)} packages checked")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    return audit_requirements(args.requirements, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
