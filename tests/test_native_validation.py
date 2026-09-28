import difflib
import json
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug.agent import DebugOrchestrator
from aidebug.discovery import discover_repository
from aidebug.hunt import BugHypothesis, VerifiedRepairValidator, hunt_project
from aidebug.hunt_strategies import HuntVerifier
from aidebug.models import AnalysisReport, CheckResult, CheckSpec, PatchProposal, ProjectInfo
from aidebug.runner import run_check
from aidebug.validation import capture, compare_baseline, evaluate, syntax_check


@pytest.mark.parametrize("configuration,names", [
    ("[tool.pytest.ini_options]\n", ["pytest"]),
    ("[tool.ruff]\n[tool.mypy]\n", ["ruff", "mypy"]),
    ("[project]\nname='example'\n", []),
    ('[tool.aidebug.checks]\ncompile = ["python", "-m", "compileall", "src"]\n', ["compile"]),
])
def test_python_discovery_is_evidence_based(tmp_path, monkeypatch, configuration, names):
    (tmp_path / "pyproject.toml").write_text(configuration)
    monkeypatch.setattr("aidebug.discovery._project_python", lambda p: "project-python")
    project = discover_repository(tmp_path)
    assert [c.name for c in project.checks] == names
    assert "python" in project.project_types


def test_unittest_only_and_empty_tests_directory(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").touch()
    (tmp_path / "tests").mkdir()
    assert not discover_repository(tmp_path).checks
    (tmp_path / "tests/test_example.py").write_text("import unittest\nclass Example(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n")
    monkeypatch.setattr("aidebug.discovery._project_python", lambda p: sys.executable)
    project = discover_repository(tmp_path)
    assert [c.name for c in project.checks] == ["unittest"]
    assert run_check(project, project.checks[0]).passed


def test_plain_pytest_functions_are_discovered(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").touch()
    (tmp_path / "test_example.py").write_text("def test_ok():\n    assert True\n")
    monkeypatch.setattr("aidebug.discovery._project_python", lambda p: "project-python")
    assert [c.name for c in discover_repository(tmp_path).checks] == ["pytest"]


@pytest.mark.parametrize("manifest,content,names", [
    ("package.json", '{"dependencies":{"react":"1"},"scripts":{"test":"test-runner","lint":"eslint .","build":"vite build"}}', ["npm test", "npm lint", "npm build"]),
    ("pom.xml", "<project/>", ["maven test"]),
    ("build.gradle", "plugins {}", ["gradle test"]),
    ("go.mod", "module example", ["go test"]),
    ("Cargo.toml", '[package]\nname="example"', ["cargo check", "cargo test"]),
    ("Makefile", "test:\n\techo ok", []),
    ("pyproject.toml", '[tool.aidebug.checks]\ntest=["make", "test"]', ["test"]),
])
def test_native_adapters(tmp_path, manifest, content, names):
    (tmp_path / manifest).write_text(content)
    assert [c.name for c in discover_repository(tmp_path).checks] == names


def check(code=0, error="", name="project", blocked=None):
    return CheckResult(name, (name,), code, "", error, .01, blocked)


@pytest.mark.parametrize("before,after,label,regression", [
    (check(1, "AssertionError at app.py:5"), check(1, "AssertionError at app.py:5"), "unchanged pre-existing", False),
    (check(), check(1, "AssertionError at app.py:5"), "newly introduced", True),
    (check(1, "AssertionError at app.py:5"), check(1, "TypeError at other.py:9"), "worsened/changed", True),
    (check(1, "AssertionError"), check(), "resolved failure", False),
])
def test_baseline_compares_failure_evidence(before, after, label, regression):
    comparisons, regressions = compare_baseline(capture((before,)), capture((after,)))
    assert label in comparisons[0]
    assert bool(regressions) == regression


def test_baseline_is_bounded_and_status_serializable(tmp_path):
    from dataclasses import asdict
    result = capture((replace(check(1), stderr="x" * 20000),))[0]
    assert len(result.stderr) == 12000
    assert asdict(result)["status"] == "FAIL"


@pytest.mark.parametrize("after,baseline,status,accepted", [
    ((), (), "TARGET FIX VERIFIED", True),
    ((check(),), (check(),), "FULLY VALIDATED", True),
    ((check(1, "old"),), (check(1, "old"),), "TARGET FIX VERIFIED", True),
    ((check(127, blocked="tool unavailable"),), (check(127, blocked="tool unavailable"),), "TARGET FIX VERIFIED / PROJECT VALIDATION BLOCKED", True),
    ((check(1, "new"),), (check(),), "REGRESSION DETECTED", False),
])
def test_final_status_scope(tmp_path, after, baseline, status, accepted):
    report = evaluate(ProjectInfo(tmp_path, ()), check(name="target"), after, baseline)
    assert report.final_status == status and report.passed == accepted
    assert "does not establish" in report.scope


def test_missing_configured_tool_blocks_without_execution(tmp_path, monkeypatch):
    (tmp_path / "pytest.ini").touch()
    project = discover_repository(tmp_path)
    runner = Mock()
    monkeypatch.setattr("aidebug.runner.subprocess.run", runner)
    result = run_check(project, project.checks[0])
    assert result.status == "BLOCKED"
    runner.assert_not_called()


def test_missing_module_and_application_import_failure_are_distinct(tmp_path):
    spec = CheckSpec("missing", (sys.executable, "-m", "aidebug_nonexistent_validation_tool"), "configured")
    assert run_check(ProjectInfo(tmp_path, ()), spec).status == "BLOCKED"
    failure = CheckResult("pytest", (sys.executable, "-m", "pytest"), 1, "", "ModuleNotFoundError: No module named 'application_dependency'", 0)
    assert capture((failure,))[0].status == "FAIL"


def test_environment_blocker_never_calls_fixer(tmp_path):
    spec = CheckSpec("pytest", ("missing", "-m", "pytest"), "configured", "Missing project environment")
    project = ProjectInfo(tmp_path, ("python",), (spec,))
    result = run_check(project, spec)
    agent = Mock()
    run = DebugOrchestrator(agent, agent).run(project, result)
    assert not run.validation.passed and run.attempts == 0
    agent.propose.assert_not_called()
    assert not (tmp_path / "requirements.txt").exists()


def hypothesis(spec):
    return BugHypothesis("app.py", "f", "Wrong result", "Source", .99, "Bounded reproduction", verification_spec=spec)


@pytest.mark.parametrize("body,expected_pass", [("return xs[-1]", True), ("return 0", False), ("raise TypeError('changed exception')", False)])
def test_positive_corrected_behavior_required(tmp_path, monkeypatch, body, expected_pass):
    (tmp_path / "app.py").write_text("def f(xs):\n    return xs[len(xs)]\n")
    project = ProjectInfo(tmp_path, ("python",))
    item = hypothesis({"kind": "expected_exception", "args": [[1, 2]], "expected_exception": "IndexError", "expected": 2})
    validator = VerifiedRepairValidator(HuntVerifier(), item, 1, project)
    (tmp_path / "app.py").write_text("def f(xs):\n    " + body + "\n")
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *a, **k: ())
    report = validator.validate(project)
    assert report.passed == expected_pass
    assert report.targeted.passed == expected_pass


def test_exception_disappearing_without_postcondition_is_insufficient(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("def f(xs):\n    return None\n")
    item = hypothesis({"kind": "expected_exception", "args": [[1]], "expected_exception": "IndexError"})
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *a, **k: ())
    report = VerifiedRepairValidator(HuntVerifier(), item, 1, ProjectInfo(tmp_path, ("python",))).validate(ProjectInfo(tmp_path, ("python",)))
    assert not report.passed
    assert "positive corrected behavior" in report.targeted.stderr


def test_random_repair_uses_new_valid_boundary(tmp_path, monkeypatch):
    source = "import random\ndef f(xs):\n    return xs[random.randint(0, len(xs))]\n"
    (tmp_path / "app.py").write_text(source)
    item = hypothesis({"kind": "deterministic_random", "args": [[42]], "random_values": {"randint": [1]}, "expected_exception": "IndexError", "expected": 42})
    project = ProjectInfo(tmp_path, ("python",))
    validator = VerifiedRepairValidator(HuntVerifier(), item, 1, project)
    (tmp_path / "app.py").write_text(source.replace("len(xs))", "len(xs) - 1)"))
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *a, **k: ())
    report = validator.validate(project)
    assert report.passed, report.targeted.stderr
    assert '"value": 0' in report.targeted.stdout
    assert '"result": 42' in report.targeted.stdout


@pytest.mark.parametrize("existing", [(), (check(127, blocked="pytest unavailable"),), (check(1, "unrelated baseline failure"),)])
def test_standalone_target_repair_persists_scope_without_dependency_edits(tmp_path, monkeypatch, existing):
    source, fixed = "def f():\n    return 0\n", "def f():\n    return 1\n"
    (tmp_path / "app.py").write_text(source)
    (tmp_path / "requirements.txt").write_text("")
    project = discover_repository(tmp_path)
    assert project.project_types == ("python",) and not project.checks
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *a, **k: existing)
    item = hypothesis({"kind": "equals", "expected": 1})
    patch = "".join(difflib.unified_diff(source.splitlines(True), fixed.splitlines(True), "a/app.py", "b/app.py"))
    agent = SimpleNamespace(analyze=Mock(return_value=AnalysisReport("Wrong constant", .9, "Expected one")), propose=Mock(return_value=PatchProposal(patch, "Return one")))
    run = hunt_project(project, (SimpleNamespace(hunt=lambda p: (item,)),), HuntVerifier(), agent)
    repair = run.repairs[0]
    assert repair.validation.passed and repair.attempts == 1
    assert repair.validation.syntax.passed
    assert agent.propose.call_count == 1
    report = repair.debug_report_path.read_text(encoding="utf-8")
    for heading in ("Targeted Verification", "Expected Corrected Behavior", "Observed Corrected Behavior", "Syntax Check", "Project-Wide Validation", "Validation Strategy", "Checks Used", "Checks Skipped", "Blocked Checks", "Baseline Comparison", "Regressions", "Final Repair Status", "Validation Scope / Limitations"):
        assert f"## {heading}" in report
    assert (tmp_path / "requirements.txt").read_text() == ""
    assert (tmp_path / "app.py").read_text() == source
    assert repair.validated_patch_path.read_text() == patch
    record = json.loads(run.findings_path.read_text())
    if not existing:
        assert "Project-wide validation: NOT AVAILABLE" in run.report_path.read_text(encoding="utf-8")
    assert record["repairs"][0]["validation"]["baseline"] == [__import__("dataclasses").asdict(r) | {"command": list(r.command)} for r in existing]


def test_syntax_checks_do_not_execute_source(tmp_path):
    (tmp_path / "app.py").write_text("raise RuntimeError('do not execute')\n")
    assert syntax_check(ProjectInfo(tmp_path, ("python",))).passed
    assert not (tmp_path / "__pycache__").exists()


def test_standalone_script_without_project_markers(tmp_path, monkeypatch):
    monkeypatch.setattr("aidebug.discovery._PROJECT_MARKERS", ())
    child = tmp_path / "standalone"
    child.mkdir()
    (child / "main.py").write_text("VALUE = 1\n")
    project = discover_repository(child)
    assert project.root == child and project.project_types == ("python",)
    assert not project.checks


def test_failure_comparison_ignores_workspace_and_timing_only():
    before = check(1, 'File "C:\\Temp\\aidebug-before\\app\\main.py", line 5\nAssertionError: wrong\n1 failed in 0.15s')
    after = check(1, 'File "C:\\Temp\\aidebug-after\\app\\main.py", line 5\nAssertionError: wrong\n1 failed in 1.50s')
    assert not compare_baseline((before,), (after,))[1]
    assert compare_baseline((before,), (replace(after, stderr=after.stderr.replace("line 5", "line 9")),))[1]


def test_regression_blocks_saved_repair(tmp_path, monkeypatch):
    source, fixed = "def f():\n    return 0\n", "def f():\n    return 1\n"
    (tmp_path / "app.py").write_text(source)
    project = ProjectInfo(tmp_path, ("python",))
    baseline = check()
    checks = Mock(side_effect=[(baseline,), (check(1, "new regression"),)])
    monkeypatch.setattr("aidebug.hunt.run_checks", checks)
    item = hypothesis({"kind": "equals", "expected": 1})
    patch = "".join(difflib.unified_diff(source.splitlines(True), fixed.splitlines(True), "a/app.py", "b/app.py"))
    agent = SimpleNamespace(analyze=Mock(return_value=AnalysisReport("Wrong constant", .9, "Expected one")),
                            propose=Mock(side_effect=[PatchProposal(patch, "Fix target"), PatchProposal(None, "Cannot safely fix regression", "no_patch")]))
    run = hunt_project(project, (SimpleNamespace(hunt=lambda p: (item,)),), HuntVerifier(), agent)
    repair = run.repairs[0]
    assert repair.validation.targeted.passed
    assert repair.validation.final_status == "REGRESSION DETECTED"
    assert not repair.validation.passed and repair.validated_patch_path is None
    assert not list((tmp_path / ".aidebug").glob("validated_patch*"))
    assert "new regression" in agent.propose.call_args_list[1].args[0].failed_check.stderr


def test_mutation_postcondition_keeps_original_inputs(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("def f(xs):\n    for i in range(len(xs)):\n        if xs[i] == 'remove':\n            xs.pop(i)\n")
    spec = {"kind": "expected_exception", "args": [["remove", "keep"]], "expected_exception": "IndexError",
            "postcondition": {"kind": "mutation_check", "expected_args": [["keep"]]}}
    project = ProjectInfo(tmp_path, ("python",))
    validator = VerifiedRepairValidator(HuntVerifier(), hypothesis(spec), 1, project)
    (tmp_path / "app.py").write_text("def f(xs):\n    xs.remove('remove')\n")
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *a, **k: ())
    assert validator.validate(project).passed
