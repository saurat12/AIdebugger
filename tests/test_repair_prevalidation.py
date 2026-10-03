"""Syntax gates and pinned postconditions in generic temporary repositories."""

import difflib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug.agent import CheckValidator, DebugOrchestrator
from aidebug.artifacts import save_validated_repair
from aidebug.hunt import BugHypothesis, VerifiedRepairValidator
from aidebug.hunt_registry import RegistryVerifier
from aidebug.models import AnalysisReport, CheckResult, DebugContext, DebugRun, PatchProposal, ProjectInfo, ValidationReport
from aidebug.validation import evaluate, syntax_check


def diff(before, after, name="fragment.py"):
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{name}", f"b/{name}"))


def result(code=0):
    return CheckResult("target", ("structured-reproduction",), code, "", "", 0)


def test_invalid_python_patch_retries_before_any_behavioral_validation(tmp_path):
    original = "def target():\n    return 0\n"
    invalid = "def target():\n    return (\n"
    fixed = "def target():\n    return 1\n"
    path = tmp_path / "fragment.py"
    path.write_text(original)
    contexts = []

    def propose(context, analysis):
        contexts.append(context)
        current = (context.project.root / path.name).read_text()
        if len(contexts) == 1:
            assert current == original
            return PatchProposal(diff(original, invalid), "Repair target")
        assert current == invalid
        assert "SyntaxError" in context.failed_check.stderr
        assert "fragment.py:2:" in context.failed_check.stderr
        assert "syntax pre-validation" in context.patch_feedback[-1]
        assert not (tmp_path / ".aidebug").exists()
        return PatchProposal(diff(invalid, fixed), "Correct syntax and target")

    agent = SimpleNamespace(analyze=Mock(return_value=AnalysisReport("Incorrect return", .9, "Explicit oracle")), propose=propose)
    validator = SimpleNamespace(validate=Mock(side_effect=lambda p: ValidationReport(True, (result(),), p.root, targeted=result())))
    run = DebugOrchestrator(agent, agent, validator, max_attempts=2).run(ProjectInfo(tmp_path, ("python",)), result(1), finding_id="canonical")
    assert run.attempts == 2 and run.validation.passed and run.validation.syntax.passed
    validator.validate.assert_called_once()
    assert run.finding_id == "canonical"
    assert run.validated_patch_path.read_text() == diff(original, fixed)
    assert "SyntaxError" in run.patch_errors[0]
    assert path.read_text() == original


@pytest.mark.parametrize("project_types", [("python",), ("custom",)])
def test_invalid_created_python_file_never_reaches_validator_or_persistence(tmp_path, project_types):
    original = "value = 0\n"
    (tmp_path / "fragment.py").write_text(original)
    proposal = diff(original, "value = 1\n") + "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+def broken(:\n"
    agent = SimpleNamespace(analyze=Mock(return_value=AnalysisReport("Incorrect value", .9, "Oracle")),
                            propose=Mock(return_value=PatchProposal(proposal, "Repair")))
    validator = SimpleNamespace(validate=Mock())
    run = DebugOrchestrator(agent, agent, validator, max_attempts=1).run(ProjectInfo(tmp_path, project_types), result(1))
    validator.validate.assert_not_called()
    assert not run.validation.passed and not run.validation.syntax.passed
    assert "new.py:1:" in run.validation.syntax.stderr
    assert run.validation.targeted is None and not run.validation.project_results
    assert run.validated_patch_path is None and run.debug_report_path is None
    assert not (tmp_path / ".aidebug").exists()
    assert (tmp_path / "fragment.py").read_text() == original
    assert not (tmp_path / "new.py").exists()


def test_every_modified_python_file_is_compiled_even_when_discovery_ignores_it(tmp_path):
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "fragment.py").write_text("value = )\n")
    project = ProjectInfo(tmp_path, ("custom",))
    check = syntax_check(project, (".hidden/fragment.py",))
    assert not check.passed and "SyntaxError" in check.stderr
    assert ".hidden/fragment.py:1:" in check.stderr
    with pytest.raises(ValueError, match="escapes workspace"):
        syntax_check(project, ("../outside.py",))


def test_deleted_python_file_does_not_require_compilation(tmp_path):
    check = syntax_check(ProjectInfo(tmp_path, ("python",)), ("deleted.py",))
    assert check.passed
    assert "0 Python files" in check.stdout


def test_direct_validators_reject_syntax_before_target_or_project_checks(tmp_path, monkeypatch):
    path = tmp_path / "fragment.py"
    path.write_text("def target():\n    return 0\n")
    project = ProjectInfo(tmp_path, ("python",))
    spec = {"kind": "equals", "expected": 1}
    item = BugHypothesis(path.name, "target", "Wrong result", "Declared result", .95, "Structured call", verification_spec=spec)
    verifier = SimpleNamespace(contract=lambda *a: None, prepare_repair=lambda p, h: h, verify=Mock())
    targeted = VerifiedRepairValidator(verifier, item, 1, project, pinned_plan={"plan": spec, "verifier": "restricted-ast-runtime"})
    path.write_text("def target():\n    return )\n")
    hunt_checks = Mock()
    project_checks = Mock()
    monkeypatch.setattr("aidebug.hunt.run_checks", hunt_checks)
    monkeypatch.setattr("aidebug.agent.run_checks", project_checks)
    for report in (targeted.validate(project), CheckValidator().validate(project)):
        assert not report.passed and report.final_status == "REPAIR FAILED"
        assert report.targeted is None and not report.project_results
        assert "SyntaxError" in report.syntax.stderr
    verifier.verify.assert_not_called()
    hunt_checks.assert_not_called()
    project_checks.assert_not_called()


def test_failed_syntax_can_never_be_accepted_as_unchanged_baseline(tmp_path):
    (tmp_path / "fragment.py").write_text("value = )\n")
    project = ProjectInfo(tmp_path, ("python",))
    syntax = syntax_check(project)
    report = evaluate(project, result(), (), syntax=syntax, baseline_syntax=syntax)
    assert not report.passed
    assert report.final_status == "REPAIR FAILED"


def test_persistence_rechecks_actual_source_even_with_claimed_pass(tmp_path):
    (tmp_path / "fragment.py").write_text("value = )\n")
    project = ProjectInfo(tmp_path, ("python",))
    claimed = ValidationReport(True, (result(),), tmp_path)
    run = DebugRun(DebugContext(project, result(1), ()), AnalysisReport("Bad value", .9, "Oracle"), (), claimed, 1)
    with pytest.raises(ValueError, match="syntax pre-validation failed"):
        save_validated_repair(run, tmp_path, {"fragment.py": b"value = 0\n"})
    assert not (tmp_path / ".aidebug").exists()


@pytest.mark.parametrize("spec,original,fixed,mechanism", [
    ({"kind": "equals", "expected": 1}, "def target():\n    return 0\n", "def target():\n    return 1\n", "restricted-ast-runtime"),
    ({"kind": "python_syntax", "line": 1}, "def target(:\n    pass\n", "def target():\n    pass\n", "python-parse"),
    ({"kind": "module_fragment", "target_lines": [1, 1], "expected": {"state": {"value": 1}}}, "value = 0\n", "value = 1\n", "restricted-module-fragment"),
])
def test_valid_repair_replays_pinned_capability_spec(tmp_path, monkeypatch, spec, original, fixed, mechanism):
    path = tmp_path / "fragment.py"
    path.write_text(original)
    project = ProjectInfo(tmp_path, ("python",))
    item = BugHypothesis(path.name, "target", "Contract violated", "fragment.py:1", .95, "Structured contract", verification_spec=spec)
    verifier = RegistryVerifier()
    assert verifier.verify(project, item).status == "confirmed"
    validator = VerifiedRepairValidator(verifier, item, 1, project, pinned_plan={"plan": spec, "verifier": mechanism})
    path.write_text(fixed)
    checks = Mock(return_value=())
    monkeypatch.setattr("aidebug.hunt.run_checks", checks)
    report = validator.validate(project)
    assert report.passed, report.targeted.stderr
    assert report.syntax.passed and report.targeted.passed
    assert report.plan_reused
    assert report.confirmation_verifier == report.repair_verifier == mechanism
    assert validator.repair_hypothesis.verification_spec == spec
    checks.assert_called_once()
    assert not (tmp_path / "__pycache__").exists()


def test_pinned_exception_reproduction_requires_positive_postcondition(tmp_path, monkeypatch):
    path = tmp_path / "fragment.py"
    path.write_text("def target(xs):\n    return xs[len(xs)]\n")
    project = ProjectInfo(tmp_path, ("python",))
    spec = {"kind": "expected_exception", "args": [[1]], "expected_exception": "IndexError", "expected": 1}
    item = BugHypothesis(path.name, "target", "Boundary violation", "Declared result 1", .95, "Structured call", verification_spec=spec)
    validator = VerifiedRepairValidator(RegistryVerifier(), item, 1, project,
                                        pinned_plan={"plan": spec, "verifier": "restricted-ast-runtime"})
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *a, **k: ())
    path.write_text("def target(xs):\n    return 0\n")
    assert not validator.validate(project).passed
    assert validator.repair_hypothesis.verification_spec == {"kind": "equals", "args": [[1]], "expected": 1}
    path.write_text("def target(xs):\n    return xs[0]\n")
    assert validator.validate(project).passed


def test_unsupported_pinned_verification_remains_blocked_without_substitution(tmp_path, monkeypatch):
    path = tmp_path / "fragment.py"
    path.write_text("def target():\n    return 0\n")
    project = ProjectInfo(tmp_path, ("python",))
    spec = {"kind": "equals", "expected": 1}
    item = BugHypothesis(path.name, "target", "Expected one", "Explicit result contract", .95, "Structured call", verification_spec=spec)
    validator = VerifiedRepairValidator(RegistryVerifier(), item, 1, project,
                                        pinned_plan={"plan": spec, "verifier": "restricted-ast-runtime"})
    path.write_text("def target():\n    match 1:\n        case 1:\n            return 1\n")
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *a, **k: ())
    verifier = validator.verifier
    verifier.verify = Mock(return_value=SimpleNamespace(status="unconfirmed", evidence="UNVERIFIABLE: unsupported AST node: Match", check=None))
    report = validator.validate(project)
    assert not report.passed and report.final_status == "TARGETED VERIFICATION BLOCKED"
    assert "unsupported AST node" in report.targeted.stderr
    assert validator.repair_hypothesis.verification_spec == spec
    verifier.verify.assert_called_once()
