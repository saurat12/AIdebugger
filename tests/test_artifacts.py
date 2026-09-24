import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug import cli
from aidebug.agent import DebugOrchestrator
from aidebug.models import AnalysisReport, CheckResult, PatchProposal, ProjectInfo, ValidationReport
from aidebug.workspace import apply_unified_diff


def repair(tmp_path, passes_on=1, attempts=3):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    project = ProjectInfo(tmp_path, ("python",), ())
    failure = CheckResult("pytest", ("python", "-m", "pytest"), 1, "", "failed", 0.01)
    analyzer = SimpleNamespace(analyze=Mock(return_value=AnalysisReport("Wrong value", 0.9, "Expected another value")))

    class Fixer:
        count = 0

        def propose(self, context, analysis):
            self.count += 1
            return PatchProposal(
                "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n"
                f"-value = {self.count}\n+value = {self.count + 1}\n",
                f"Set value to {self.count + 1}.",
            )

    class Validator:
        count = 0

        def validate(self, project):
            self.count += 1
            assert (project.root / "app.py").read_text() == f"value = {self.count + 1}\n"
            passed = self.count == passes_on
            result = CheckResult("pytest", ("python", "-m", "pytest"), 0 if passed else 1, "", "", 0.01)
            return ValidationReport(passed, (result,), project.root)

    return DebugOrchestrator(analyzer, Fixer(), Validator(), max_attempts=attempts).run(project, failure)


@pytest.mark.parametrize("attempts", [1, 2, 3])
def test_persists_final_cumulative_repair(tmp_path, attempts):
    run = repair(tmp_path, passes_on=attempts)
    patch = run.validated_patch_path.read_text()
    assert patch.startswith("--- a/app.py\n+++ b/app.py\n")
    assert "-value = 1\n" in patch
    assert f"+value = {attempts + 1}\n" in patch
    assert (tmp_path / "app.py").read_text() == "value = 1\n"
    assert not run.validation.workspace.exists()
    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "app.py").write_text("value = 1\n")
    apply_unified_diff(replay, patch)
    assert (replay / "app.py").read_text() == f"value = {attempts + 1}\n"
    report = run.debug_report_path.read_text()
    for heading in ("# AIdebug Validated Repair", "## Project", "## Finding Type", "## Initial Failure", "## Failure Evidence", "## Root Cause", "## Confidence", "## Repair", "## Validation Before Repair", "## Validation After Repair", "## What Was Verified", "## What Was Not Verified", "## Repair Risk", "## Files Changed", "## Validated Patch"):
        assert heading in report.splitlines()
    assert patch in report
    assert "Wrong value" in report
    assert "app.py" in report


def test_failed_validation_creates_no_artifacts(tmp_path):
    run = repair(tmp_path, passes_on=99)
    assert not run.validation.passed
    assert run.validated_patch_path is None
    assert run.debug_report_path is None
    assert not (tmp_path / ".aidebug").exists()
    assert (tmp_path / "app.py").read_text() == "value = 1\n"


def test_successful_runs_have_unique_paired_filenames(tmp_path):
    first = repair(tmp_path)
    original = first.validated_patch_path.read_bytes()
    second = repair(tmp_path)
    assert first.validated_patch_path != second.validated_patch_path
    assert first.validated_patch_path.read_bytes() == original
    for run in (first, second):
        assert run.validated_patch_path.parent == tmp_path / ".aidebug"
        stamp = run.validated_patch_path.stem.removeprefix("validated_patch_")
        assert run.debug_report_path.name == f"debug_report_{stamp}.md"
    assert len(list((tmp_path / ".aidebug").glob("*.diff"))) == 2


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_exposes_artifact_paths(tmp_path, monkeypatch, capsys, as_json):
    run = repair(tmp_path)
    monkeypatch.setattr(cli, "discover_repository", Mock(return_value=run.initial_context.project))
    monkeypatch.setattr(cli, "run_checks", Mock(return_value=(run.initial_context.failed_check,)))
    monkeypatch.setattr(cli, "collect_evidence", Mock(return_value=(set(), None)))
    monkeypatch.setattr(cli, "OpenAIAgent", Mock())
    monkeypatch.setattr(cli, "DebugOrchestrator", Mock(return_value=SimpleNamespace(run=Mock(return_value=run))))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("sys.argv", ["aidebug", *(["--json"] if as_json else [])])
    assert cli.main() == 1
    output = capsys.readouterr().out
    if as_json:
        saved = json.loads(output)["agent_run"]
        assert saved["validated_patch_path"] == str(run.validated_patch_path)
        assert saved["debug_report_path"] == str(run.debug_report_path)
    else:
        assert f"Validated patch saved to:\n{run.validated_patch_path}" in output
        assert f"Debug report saved to:\n{run.debug_report_path}" in output
        assert "Initial check:\n[FAIL] pytest:" in output
        assert "AI analysis:\nRoot cause: Wrong value" in output
        assert "Validated repair:\n[PASS] pytest in isolated workspace" in output
        assert "Repair validated after 1 attempt." in output
        assert output.index("Initial check:") < output.index("AI analysis:") < output.index("Validated repair:")


def test_real_headers_only_and_cumulative_artifact(tmp_path):
    from aidebug.artifacts import remember_originals, save_validated_repair
    from aidebug.context import build_debug_context
    from aidebug.models import DebugRun
    from aidebug.workspace import isolated_workspace

    (tmp_path / "app.txt").write_text("-- ../../not-a-path\n")
    (tmp_path / "delete.txt").write_text("remove me\n")
    first = (
        "--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n"
        "--- ../../not-a-path\n+++ ../../also-not-a-path\n"
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+created\n"
        "--- a/delete.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-remove me\n"
    )
    second = "--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-++ ../../also-not-a-path\n+final\n"
    originals = {}
    project = ProjectInfo(tmp_path, ("python",), ())
    failure = CheckResult("pytest", ("pytest",), 1, "", "", 0.01)
    with isolated_workspace(tmp_path) as workspace:
        remember_originals(workspace, first, originals)
        assert originals == {"app.txt": (tmp_path / "app.txt").read_bytes(), "new.txt": None,
                             "delete.txt": (tmp_path / "delete.txt").read_bytes()}
        apply_unified_diff(workspace, first)
        remember_originals(workspace, second, originals)
        apply_unified_diff(workspace, second)
        run = DebugRun(build_debug_context(project, failure), AnalysisReport("Wrong text", 1, "Repair"),
                       (PatchProposal(first, "First repair"), PatchProposal(second, "Final repair")),
                       ValidationReport(True, (), workspace), 2)
        patch_path, report_path = save_validated_repair(run, workspace, originals)
    patch = patch_path.read_text()
    assert "--- /dev/null\n+++ b/new.txt" in patch
    assert "--- a/delete.txt\n+++ /dev/null" in patch
    assert "+final\n" in patch
    assert "also-not-a-path" not in patch
    assert patch in report_path.read_text()
    with isolated_workspace(tmp_path) as replay:
        apply_unified_diff(replay, patch)
        assert (replay / "app.txt").read_text() == "final\n"
        assert (replay / "new.txt").read_text() == "created\n"
        assert not (replay / "delete.txt").exists()
    assert (tmp_path / "app.txt").read_text() == "-- ../../not-a-path\n"
    assert (tmp_path / "delete.txt").exists()
    assert not (tmp_path / "new.txt").exists()
