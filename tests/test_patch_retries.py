import difflib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug.agent import DebugOrchestrator
from aidebug.context import build_agent_prompt
from aidebug.hunt import BugHypothesis, DoctestVerifier, hunt_project, hunt_main
from aidebug.models import AnalysisReport, CheckResult, PatchProposal, ProjectInfo, ValidationReport
from aidebug.workspace import PatchApplicabilityError, apply_unified_diff


def patch(before, after, name="app.py"):
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), "a/" + name, "b/" + name))


@pytest.mark.parametrize("malformed,reason", [
    ("diff --git a/app.py b/app.py\n", "header at line 1"),
    ("--- a/app.py\n", "missing a unified diff target"),
    ("--- a/app.py\n+++ b/app.py\n@@\n", "hunk at line 3"),
    ("--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-value = 2\n+value = 3\n", "expected old=2, new=2; observed old=1, new=1"),
    ("--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 99\n+value = 3\n", "does not match"),
])
def test_rejected_patch_retries_current_cumulative_workspace(tmp_path, malformed, reason):
    original, intermediate, fixed = "value = 1\n", "value = 2\n", "value = 3\n"
    (tmp_path / "app.py").write_text(original)
    snapshots, prompts = [], []
    proposals = iter([patch(original, intermediate), malformed, patch(intermediate, fixed)])

    def propose(context, analysis):
        assert context.project.root != tmp_path
        snapshots.append((context.project.root / "app.py").read_text())
        prompts.append(build_agent_prompt(context))
        return PatchProposal(next(proposals), "Correct value")

    def validate(project):
        passed = (project.root / "app.py").read_text() == fixed
        result = CheckResult("pytest", ("pytest",), 0 if passed else 1, "", "app.py failed", 0)
        return ValidationReport(passed, (result,), project.root)

    agent = SimpleNamespace(analyze=Mock(return_value=AnalysisReport("Wrong value", .9, "Evidence")), propose=propose)
    run = DebugOrchestrator(agent, agent, SimpleNamespace(validate=validate)).run(
        ProjectInfo(tmp_path, ("python",)), CheckResult("pytest", ("pytest",), 1, "", "app.py failed", 0))
    assert run.validation.passed and run.attempts == 3
    assert snapshots == [original, intermediate, intermediate]
    assert reason in prompts[-1] and reason in run.patch_errors[0]
    assert len(run.proposals) == 2
    assert run.validated_patch_path.read_text() == patch(original, fixed)
    assert (tmp_path / "app.py").read_text() == original


def test_nonapplicable_multifile_patch_leaves_workspace_unchanged(tmp_path):
    for name in ("app.py", "other.py"):
        (tmp_path / name).write_text("old\n")
    proposal = patch("old\n", "new\n") + patch("stale\n", "new\n", "other.py")
    with pytest.raises(PatchApplicabilityError):
        apply_unified_diff(tmp_path, proposal)
    assert (tmp_path / "app.py").read_text() == "old\n"
    assert (tmp_path / "other.py").read_text() == "old\n"


@pytest.mark.parametrize("correct_retry", [True, False])
def test_independent_findings_retry_and_report_once(tmp_path, monkeypatch, capsys, correct_retry):
    source = 'def double(x):\n    """>>> double(3)\n    6\n    """\n    return x + 2\n'
    hypotheses = []
    for name in ("app.py", "other.py"):
        (tmp_path / name).write_text(source)
        hypotheses.append(BugHypothesis(name, "double", "Wrong result", "Example fails", .9, "Run example",
                                        verification_spec={"kind": "equals", "args": [3], "expected": 6}))
    project = ProjectInfo(tmp_path, ("python",))
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *a, **k: (CheckResult("pytest", ("pytest",), 0, "1 passed", "", 0),))
    calls = {}

    def propose(context, analysis):
        name = context.relevant_files[0].name
        calls[name] = calls.get(name, 0) + 1
        assert (context.project.root / name).read_text() == source
        diff = patch(source, source.replace("x + 2", "x * 2"), name) if correct_retry and calls[name] > 1 else "diff --git unsupported\n"
        return PatchProposal(diff, "Fix arithmetic")

    agent = SimpleNamespace(analyze=Mock(return_value=AnalysisReport("Arithmetic", .9, "Example")), propose=propose)
    run = hunt_project(project, (SimpleNamespace(hunt=lambda p: tuple(hypotheses)),), DoctestVerifier(), agent)
    ids = {finding.finding_id for finding in run.findings}
    if correct_retry:
        assert {repair.finding_id for repair in run.repairs} == ids
        assert all(repair.validation.passed and repair.attempts == 2 for repair in run.repairs)
    else:
        assert not run.repairs and len(run.repair_errors) == 2
        assert not list((tmp_path / ".aidebug").glob("validated_patch*"))
    report = run.report_path.read_text(encoding="utf-8")
    assert "header at line 1" in report
    assert all(identifier in report for identifier in ids)
    monkeypatch.setattr("aidebug.discovery.discover_repository", lambda p: project)
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setattr("aidebug.hunt.hunt_project", lambda *a, **k: run)
    hunt_main([str(tmp_path)])
    output = capsys.readouterr().out
    assert "Proactive repair: not validated" not in output
    for identifier in ids:
        assert output.count(f"{identifier}: repair ") == 1
    assert "test-secret" not in output
    assert all((tmp_path / name).read_text() == source for name in calls)
