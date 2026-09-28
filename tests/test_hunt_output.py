import difflib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug.hunt import BugHypothesis, Finding, HuntRun, hunt_main, hunt_project, render_repair_output
from aidebug.models import AnalysisReport, PatchProposal, ProjectInfo
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
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("aidebug.discovery.discover_repository", lambda p: ProjectInfo(tmp_path, ("python",)))
    monkeypatch.setattr("aidebug.hunt.hunt_project", lambda *a, **k: run)
    hunt_main([str(tmp_path)])
    output = capsys.readouterr().out
    for label in ("Targeted Verification:", "Syntax Check:", "Project-wide validation:", "Final Repair Status:"):
        assert output.count(label) == 2
    assert output.count("Confirmed bugs:") == 1
    assert output.endswith("Confirmed bugs: 2\nRepairs verified: 2\nRepairs failed: 0\nRepairs blocked: 0\n")
    stamps = []
    for index, finding in enumerate(run.findings):
        identifier = finding.finding_id
        assert output.count(identifier + ": repair result") == 1
        block = output.split(identifier + ": repair result", 1)[1].split(": repair result", 1)[0]
        repair = run.repairs[index]
        assert "File: " + finding.hypothesis.suspected_file in block
        assert "Symbol: double" in block
        for label, path in (("Validated patch", repair.validated_patch_path), ("Debug report", repair.debug_report_path)):
            assert label + " saved to:\n" + str(path.resolve()) in block
            assert path.is_file()
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
    assert "Artifact-save error (PermissionError): could not persist hunt findings/report" in output
    assert "Hunt report saved to:" not in output
    assert "Hunt findings saved to:" not in output
    for repair in run.repairs:
        assert str(repair.validated_patch_path) in output
