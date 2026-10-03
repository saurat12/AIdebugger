import difflib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug.hunt import BugHypothesis, Finding, HuntRun, hunt_main, hunt_project, render_hunt_summary, render_repair_output
from aidebug.models import AnalysisReport, CheckResult, PatchProposal, ProjectInfo
from aidebug.hunt_strategies import HuntVerifier


def repaired_findings(tmp_path, monkeypatch):
    source = 'def double(x):\n    """>>> double(3)\n    6\n    """\n    return x + 2\n'
    items = []
    for name in ("one.py", "two.py"):
        (tmp_path / name).write_text(source)
        items.append(BugHypothesis(name, "double", "Wrong arithmetic", "Declared example", .9, "Run example",
                                   verification_spec={"kind": "equals", "args": [3], "expected": 6}))
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *a, **k: ())
    def propose(context, analysis):
        name = context.relevant_files[0].name
        diff = "".join(difflib.unified_diff(source.splitlines(True), source.replace("x + 2", "x * 2").splitlines(True), "a/" + name, "b/" + name))
        return PatchProposal(diff, "Correct arithmetic")
    agent = SimpleNamespace(analyze=Mock(return_value=AnalysisReport("Arithmetic", .9, "Example")), propose=propose)
    return hunt_project(ProjectInfo(tmp_path, ("python",)), (SimpleNamespace(hunt=lambda p: tuple(items)),), HuntVerifier(), agent)


def test_one_block_per_finding_and_complete_matching_artifacts(tmp_path, monkeypatch, capsys):
    run = repaired_findings(tmp_path, monkeypatch)
    report_before = run.report_path.read_text(encoding="utf-8")
    findings_before = run.findings_path.read_text(encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("aidebug.discovery.discover_repository", lambda p: ProjectInfo(tmp_path, ("python",)))
    monkeypatch.setattr("aidebug.hunt.hunt_project", lambda *a, **k: run)
    hunt_main([str(tmp_path)])
    output = capsys.readouterr().out
    assert output.startswith("AIdebug Hunt\n\nBaseline: NOT AVAILABLE")
    assert "Bugs found: 2" in output and "Confirmed: 2" in output
    assert "Repairs verified: 2" in output
    assert output.count("AIdebug Hunt") == 1
    assert output.rstrip().endswith("Report:\n" + str(run.report_path.resolve()))
    assert str(run.findings_path.resolve()) not in output
    for private_detail in ("Verification evidence", "confidence", "reproduction", "Detector signals",
                           "Normalized verification plan", "Repair Authorization", "Strategy Coverage"):
        assert private_detail not in output
    assert run.report_path.read_text(encoding="utf-8") == report_before
    assert run.findings_path.read_text(encoding="utf-8") == findings_before
    stamps = []
    for finding in run.findings:
        identifier = finding.finding_id
        assert identifier not in output
        repair = next(repair for repair in run.repairs if repair.finding_id == identifier)
        for path in (repair.validated_patch_path, repair.debug_report_path):
            assert path.is_file()
            assert str(path.resolve()) not in output
        stamp = repair.validated_patch_path.stem.removeprefix("validated_patch_")
        assert repair.debug_report_path.stem == "debug_report_" + stamp
        assert identifier in repair.debug_report_path.read_text(encoding="utf-8")
        assert finding.hypothesis.suspected_file in repair.validated_patch_path.read_text()
        stamps.append(stamp)
    assert len(set(stamps)) == 2
    record = json.loads(run.findings_path.read_text())
    assert {r["finding_id"]: r["validated_patch_path"] for r in record["repairs"]} == {
        r.finding_id: str(r.validated_patch_path) for r in run.repairs}


def test_artifact_save_error_retains_verified_result(tmp_path, monkeypatch):
    monkeypatch.setattr("aidebug.agent.save_validated_repair", Mock(side_effect=PermissionError("private credential must not be echoed")))
    run = repaired_findings(tmp_path, monkeypatch)
    output = render_repair_output(run)
    assert len(run.repairs) == 2 and not run.repair_errors
    assert all(r.validation.passed and r.artifact_error for r in run.repairs)
    assert output.count("Artifact-save error (PermissionError)") == 2
    assert "private credential" not in output
    assert "saved to:" not in output
    assert "Repairs verified: 2" in output
    assert all(r.validated_patch_path is None and r.debug_report_path is None for r in run.repairs)


def test_partial_report_write_cleans_up_pair(tmp_path, monkeypatch):
    original = Path.open
    class FailedWrite:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            return self
        def write(self, text):
            self.handle.write(text[:10])
            raise OSError("disk full")
        def __exit__(self, *args):
            self.handle.close()
    def opening(path, *args, **kwargs):
        handle = original(path, *args, **kwargs)
        return FailedWrite(handle) if path.name.startswith("debug_report_") and args and args[0] == "x" else handle
    monkeypatch.setattr(Path, "open", opening)
    run = repaired_findings(tmp_path, monkeypatch)
    assert all(r.artifact_error for r in run.repairs)
    assert not list((tmp_path / ".aidebug").glob("validated_patch_*"))
    assert not list((tmp_path / ".aidebug").glob("debug_report_*"))


def test_incomplete_artifact_metadata_never_prints_empty_label(tmp_path, monkeypatch):
    run = repaired_findings(tmp_path, monkeypatch)
    run = replace(run, repairs=(replace(run.repairs[0], debug_report_path=None),), findings=run.findings[:1])
    output = render_repair_output(run)
    assert "Debug report saved to:" not in output
    assert "Artifact-save error: verified repair has incomplete patch/report paths" in output


def test_aggregate_outcomes_include_errors_and_blocked_findings(tmp_path, monkeypatch):
    run = repaired_findings(tmp_path, monkeypatch)
    first, second = run.findings
    blocked = Finding(replace(first.hypothesis, suspected_file="blocked.py"), "confirmed", "No actionable check")
    run = replace(run, findings=(*run.findings, blocked), repairs=run.repairs[:1],
                  repair_errors=(f"{second.finding_id}: repair failed (ValueError)",))
    output = render_repair_output(run)
    assert output.count(": repair result") == 3
    assert output.endswith("Confirmed bugs: 3\nRepairs verified: 1\nRepairs failed: 1\nRepairs blocked: 1")


def test_hunt_report_save_failure_preserves_repair_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("aidebug.hunt_reports.save_hunt_report", Mock(side_effect=PermissionError("cannot write")))
    run = repaired_findings(tmp_path, monkeypatch)
    assert run.artifact_error and run.findings_path is None and run.report_path is None
    assert all(repair.validated_patch_path.is_file() for repair in run.repairs)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("aidebug.discovery.discover_repository", lambda p: ProjectInfo(tmp_path, ("python",)))
    monkeypatch.setattr("aidebug.hunt.hunt_project", lambda *a, **k: run)
    hunt_main([str(tmp_path)])
    output = capsys.readouterr().out
    assert output.rstrip().endswith("Report:\nunavailable")
    assert "Artifact-save error" not in output


def _finding(name, status, description="Concrete behavior is incorrect"):
    hypothesis = BugHypothesis(name, "symbol", description, "private evidence", .99, "private reproduction")
    return Finding(hypothesis, status, "private verifier diagnostic")


@pytest.mark.parametrize(("checks", "expected"), [
    ((CheckResult("pytest", ("pytest",), 0, "", "", 0),), "Baseline: PASS"),
    ((CheckResult("pytest", ("pytest",), 1, "", "", 0),), "Baseline: FAIL (pytest)"),
    ((CheckResult("pytest", ("pytest",), 1, "", "", 0),
      CheckResult("ruff", ("ruff",), 1, "", "", 0)), "Baseline: FAIL (pytest, ruff)"),
    ((), "Baseline: NOT AVAILABLE"),
])
def test_compact_baseline_status_shows_only_nonpassing_check_names(checks, expected, tmp_path):
    run = HuntRun(checks, (), (), report_path=tmp_path / "hunt_report.md")
    output = render_hunt_summary(run)
    assert expected in output
    assert "private" not in output


def test_zero_counts_and_empty_unresolved_or_repair_sections_are_hidden(tmp_path):
    run = HuntRun((), (), (), report_path=tmp_path / "hunt_report.md")
    output = render_hunt_summary(run)
    assert "Bugs found: 0" in output
    assert "Confirmed:" not in output
    assert "Unverifiable:" not in output
    assert "Unresolved:" not in output
    assert "Repairs verified:" not in output
    assert "Repairs failed:" not in output
    assert "Repairs blocked:" not in output


def test_every_unresolved_finding_is_one_compact_line_and_long_text_is_shortened(tmp_path):
    hypotheses = [
        _finding("src/one.py", "high_confidence", "An unverified issue appears in this function."),
        _finding("lib/two.py", "unconfirmed", "A second issue appears."),
        _finding("lib/three.py", "high_confidence", "A lengthy behavioral hypothesis " + "detail " * 50),
    ]
    run = HuntRun((), tuple(hypotheses), (), report_path=tmp_path / "hunt_report.md")
    output = render_hunt_summary(run)
    lines = [line for line in output.splitlines() if line.startswith("- ")]
    assert len(lines) == 3
    assert lines[0] == "- src/one.py:symbol — An unverified issue appears in this function."
    assert lines[1] == "- lib/two.py:symbol — A second issue appears."
    assert len(lines[2]) <= 150 and lines[2].endswith("…")
    assert "private verifier diagnostic" not in output
    assert "99%" not in output


@pytest.mark.parametrize(("repairs", "errors", "expected"), [
    ((SimpleNamespace(finding_id="id", validation=SimpleNamespace(passed=True, final_status="TARGET FIX VERIFIED", targeted=None)),), (), "Repairs verified: 1"),
    ((), ("id: invalid patch",), "Repairs failed: 1"),
    ((SimpleNamespace(finding_id="id", validation=SimpleNamespace(passed=False, final_status="TARGETED VERIFICATION BLOCKED", targeted=None)),), (), "Repairs blocked: 1"),
])
def test_repair_counters_show_only_nonzero_outcomes(tmp_path, repairs, errors, expected):
    finding = _finding("app.py", "confirmed")
    finding = replace(finding, hypothesis=replace(finding.hypothesis, root_cause_key="id"))
    # Repair results are keyed to the finding's canonical ID.
    repairs = tuple(SimpleNamespace(finding_id=finding.finding_id, validation=repair.validation) for repair in repairs)
    errors = tuple(error.replace("id:", finding.finding_id + ":") for error in errors)
    output = render_hunt_summary(HuntRun((), (finding,), repairs, repair_errors=errors,
                                         report_path=tmp_path / "hunt_report.md"))
    assert expected in output
    for zero_line in ("Repairs verified: 0", "Repairs failed: 0", "Repairs blocked: 0"):
        assert zero_line not in output
