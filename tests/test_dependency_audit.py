"""Dependency policy checks with synthetic reports; no registry requests."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "audit_dependencies", Path(__file__).parents[1] / "scripts" / "audit_dependencies.py"
)
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


def _report(name="oauthlib", version="3.3.1", vulnerabilities=None):
    return {
        "dependencies": [{"name": name, "version": version, "vulns": vulnerabilities or []}],
        "fixes": [],
    }


@pytest.mark.parametrize(
    ("identifier", "aliases"),
    [
        ("CVE-2026-49265", []),
        ("GHSA-xpv3-w29h-x7cv", ["CVE-2026-49265", "PYSEC-2026-4114"]),
        ("PYSEC-2026-4114", ["GHSA-xpv3-w29h-x7cv", "CVE-2026-49265"]),
        ("CVE-2026-49272", []),
        ("GHSA-3jp2-3mrg-2vxc", ["CVE-2026-49272"]),
    ],
)
def test_only_reviewed_identifier_families_are_excepted(identifier, aliases):
    entry = {"id": identifier, "aliases": aliases}
    exceptions, findings = audit.review_report(
        _report(vulnerabilities=[entry, entry]), {("oauthlib", "3.3.1")}
    )
    assert len(exceptions) == 1  # Upstream may return duplicate advisory rows.
    assert not findings


@pytest.mark.parametrize(
    ("name", "version", "identifier", "aliases"),
    [
        ("oauthlib", "3.3.2", "CVE-2026-49265", []),
        ("other-package", "3.3.1", "CVE-2026-49265", []),
        ("oauthlib", "3.3.1", "NEW-ADVISORY", []),
        ("oauthlib", "3.3.1", "NEW-ALIAS", ["CVE-2026-49265"]),
        ("oauthlib", "3.3.1", "CVE-2026-49265", ["NEW-ALIAS"]),
        ("oauthlib", "3.3.1", "CVE-2026-49265", ["CVE-2026-49272"]),
        ("dnspython", "2.8.0", "CVE-2026-114605", ["GHSA-45c3-73f7-pv4m"]),
    ],
)
def test_other_packages_versions_or_identifiers_block(name, version, identifier, aliases):
    exceptions, findings = audit.review_report(
        _report(name, version, [{"id": identifier, "aliases": aliases}]), {(name, version)}
    )
    assert not exceptions
    assert len(findings) == 1


@pytest.mark.parametrize(
    "report",
    [
        None,
        {},
        {"dependencies": []},
        {"dependencies": {}},
        {"dependencies": [None]},
        {"dependencies": [{"name": "oauthlib"}]},
        {"dependencies": [{"name": "oauthlib", "version": "3.3.1"}]},
        {"dependencies": [{"name": "oauthlib", "version": "3.3.1", "vulns": None}]},
        {"dependencies": [{"name": "oauthlib", "version": 3, "vulns": []}]},
        {
            "dependencies": [
                {"name": "oauthlib", "version": "3.3.1", "vulns": [], "skip_reason": "unknown"}
            ]
        },
        _report(vulnerabilities=[None]),
        _report(vulnerabilities=[{}]),
        _report(vulnerabilities=[{"id": ""}]),
        _report(vulnerabilities=[{"id": "CVE-2026-49265", "aliases": None}]),
        _report(vulnerabilities=[{"id": "CVE-2026-49265", "aliases": "CVE-2026-49265"}]),
        _report(vulnerabilities=[{"id": "CVE-2026-49265", "aliases": [""]}]),
    ],
)
def test_incomplete_or_malformed_reports_fail_closed(report):
    with pytest.raises(ValueError):
        audit.review_report(report, {("oauthlib", "3.3.1")})


@pytest.mark.parametrize("kind", ["missing", "extra", "duplicate", "wrong-version", "empty-export"])
def test_report_must_cover_every_active_exact_requirement(kind):
    report = _report()
    expected = {("oauthlib", "3.3.1")}
    if kind == "missing":
        expected.add(("dnspython", "2.9.2"))
    elif kind == "extra":
        report["dependencies"].append({"name": "other", "version": "1", "vulns": []})
    elif kind == "duplicate":
        report["dependencies"].append(report["dependencies"][0].copy())
    elif kind == "wrong-version":
        expected = {("oauthlib", "4.0.1")}
    else:
        expected = set()
    with pytest.raises(ValueError):
        audit.review_report(report, expected)


def test_export_parser_handles_hashes_markers_and_canonical_names(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "# uv export\n\nOAuthLib==3.3.1 \\\n    --hash=sha256:abc\n"
        "Some_Package==2.0 ; python_version >= '3.11' \\\n    --hash=sha256:def\n"
        "future==9 ; python_version < '3.0' \\\n    --hash=sha256:ghi\n"
    )
    expected = audit.expected_dependencies(requirements)
    assert expected == {("oauthlib", "3.3.1"), ("some-package", "2.0")}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# no packages\n",
        "oauthlib>=3",
        "oauthlib==3.*",
        "oauthlib[extra]==3.3.1",
        "oauthlib @ https://example.invalid/pkg.whl",
        "oauthlib==3.3.1\nOAuthLib==3.3.1",
        "oauthlib==3.3.1\noauthlib==4.0.1",
        "oauthlib==3.3.1 ; python_version < '3'",
    ],
)
def test_export_requires_active_unambiguous_exact_pins(tmp_path, text):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(text)
    with pytest.raises(ValueError):
        audit.expected_dependencies(requirements)


@pytest.mark.parametrize(
    ("process_status", "vulnerabilities", "expected_status"),
    [
        (0, [], 0),
        (1, [{"id": "CVE-2026-49265"}, {"id": "CVE-2026-49272"}], 0),
        (1, [{"id": "CVE-2026-49265"}, {"id": "NEW"}], 1),
        (0, [{"id": "CVE-2026-49265"}], 2),
        (1, [], 2),
        (2, [], 2),
    ],
)
def test_runner_preserves_raw_findings_and_enforces_exit_status(
    tmp_path, monkeypatch, process_status, vulnerabilities, expected_status
):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("oauthlib==3.3.1\n")
    report_path = tmp_path / "report.json"
    report_path.write_text("stale")
    raw_report = json.dumps(_report(vulnerabilities=vulnerabilities), indent=2) + "\n"

    def run(command, *, check, timeout):
        assert not report_path.exists()
        assert command[:3] == [audit.sys.executable, "-m", "pip_audit"]
        assert "--require-hashes" in command and "--disable-pip" in command
        assert "--ignore-vuln" not in command
        assert command[command.index("--requirement") + 1] == str(requirements)
        assert command[command.index("--output") + 1] == str(report_path)
        assert timeout == 180 and check is False
        report_path.write_text(raw_report)
        return SimpleNamespace(returncode=process_status)

    monkeypatch.setattr(audit.subprocess, "run", run)
    assert audit.audit_requirements(requirements, report_path) == expected_status
    assert report_path.read_text() == raw_report


@pytest.mark.parametrize("failure", ["timeout", "launch", "missing", "json"])
def test_runner_cannot_reuse_old_or_failed_report(tmp_path, monkeypatch, failure):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("oauthlib==3.3.1\n")
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(_report()))

    def run(command, **_kwargs):
        assert not report_path.exists()
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 180)
        if failure == "launch":
            raise OSError("cannot run")
        if failure == "json":
            report_path.write_text("incomplete JSON")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(audit.subprocess, "run", run)
    assert audit.audit_requirements(requirements, report_path) == 2
