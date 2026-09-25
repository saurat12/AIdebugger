import difflib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug import cli
from aidebug.hunt import BugHunter, BugHypothesis, DoctestVerifier, Finding, VerifiedRepairValidator, hunt_project
from aidebug.models import AnalysisReport, CheckResult, PatchProposal, ProjectInfo


SOURCE = 'def double(x):\n    """>>> double(3)\n    6\n    """\n    return x + 2\n'


def hypothesis(**changes):
    fields = dict(suspected_file="app.py", suspected_symbol="double", description="Incorrect doubling",
                  evidence="The implementation disagrees with the declared example", confidence=0.9,
                  reproduction_strategy="Check double(3) against its declared result 6")
    return BugHypothesis(**(fields | changes))


def setup_project(tmp_path, monkeypatch, source=SOURCE):
    (tmp_path / "app.py").write_text(source)
    project = ProjectInfo(tmp_path, ("python",), ())

    def checks(project, *args, **kwargs):
        assert project.root != tmp_path  # Never run target checks in the real source tree.
        return (CheckResult("existing tests", ("pytest",), 0, "1 passed", "", 0.01),)

    monkeypatch.setattr("aidebug.hunt.run_checks", checks)
    return project


def test_healthy_project_still_runs_hunter(tmp_path, monkeypatch):
    project = setup_project(tmp_path, monkeypatch, SOURCE.replace("x + 2", "x * 2"))
    hunter = SimpleNamespace(hunt=Mock(return_value=()))
    agent = Mock()
    result = hunt_project(project, (hunter,), DoctestVerifier(), agent)
    hunter.hunt.assert_called_once()
    assert result.existing_checks[0].passed
    assert not result.findings and not result.repairs
    agent.analyze.assert_not_called()
    assert result.findings_path.is_file()
    assert result.report_path.is_file()
    assert not list((tmp_path / ".aidebug").glob("validated_patch*"))


def test_passing_tests_bug_is_confirmed_and_repaired(tmp_path, monkeypatch):
    project = setup_project(tmp_path, monkeypatch)
    fixed = SOURCE.replace("x + 2", "x * 2")
    patch = "".join(difflib.unified_diff(SOURCE.splitlines(True), fixed.splitlines(True), "a/app.py", "b/app.py"))
    agent = SimpleNamespace(analyze=Mock(return_value=AnalysisReport("Incorrect arithmetic", 0.95, "The declared example fails")),
                            propose=Mock(return_value=PatchProposal(patch, "Multiply instead of adding")))
    detector = SimpleNamespace(hunt=Mock(return_value=(hypothesis(),)))
    result = hunt_project(project, (detector,), DoctestVerifier(), agent)
    assert result.existing_checks[0].passed
    assert result.findings[0].status == "confirmed"
    assert "expected 6; observed 5" in result.findings[0].evidence
    assert result.repairs[0].validation.passed
    assert len(result.repairs[0].validation.results) == 2
    assert result.repairs[0].validated_patch_path.is_file()
    assert result.repairs[0].debug_report_path.is_file()
    assert (tmp_path / "app.py").read_text() == SOURCE
    agent.analyze.assert_called_once()


def test_false_positive_is_rejected_without_repair(tmp_path, monkeypatch):
    project = setup_project(tmp_path, monkeypatch, SOURCE.replace("x + 2", "x * 2"))
    agent = Mock()
    result = hunt_project(project, (SimpleNamespace(hunt=lambda project: (hypothesis(),)),), DoctestVerifier(), agent)
    assert result.findings[0].status == "rejected"
    assert not result.repairs
    agent.propose.assert_not_called()


def test_existing_tests_alone_cannot_validate_hunted_repair(tmp_path, monkeypatch):
    project = setup_project(tmp_path, monkeypatch)
    intermediate = SOURCE.replace("x + 2", "x + 1")
    fixed = SOURCE.replace("x + 2", "x * 2")
    patches = ["".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), "a/app.py", "b/app.py"))
               for before, after in ((SOURCE, intermediate), (intermediate, fixed))]
    agent = SimpleNamespace(analyze=Mock(return_value=AnalysisReport("Arithmetic mismatch", 0.9, "Declared example fails")),
                            propose=Mock(side_effect=[PatchProposal(patch, "Repair arithmetic") for patch in patches]))
    result = hunt_project(project, (SimpleNamespace(hunt=lambda project: (hypothesis(),)),), DoctestVerifier(), agent)
    repair = result.repairs[0]
    assert repair.attempts == 2
    assert repair.validation.passed
    assert len(repair.proposals) == 2
    assert "+    return x * 2" in repair.validated_patch_path.read_text()
    assert "Proactively discovered mismatch" in repair.debug_report_path.read_text()
    assert (tmp_path / "app.py").read_text() == SOURCE


@pytest.mark.parametrize("confidence,status", [(0.9, "high_confidence"), (0.5, "unconfirmed")])
def test_unsupported_reproduction_is_not_confirmation(tmp_path, monkeypatch, confidence, status):
    project = setup_project(tmp_path, monkeypatch, "def double(x):\n    return x + 2\n")
    agent = Mock()
    result = hunt_project(project, (SimpleNamespace(hunt=lambda project: (hypothesis(confidence=confidence),)),), DoctestVerifier(), agent)
    assert result.findings[0].status == status
    assert not result.repairs
    agent.analyze.assert_not_called()


def test_verifier_never_executes_source(tmp_path):
    (tmp_path / "app.py").write_text("raise RuntimeError('must not execute')\n" + SOURCE.replace("return x + 2", "return __import__('os').remove('app.py')"))
    result = DoctestVerifier().verify(ProjectInfo(tmp_path, ("python",)), hypothesis())
    assert result.status == "high_confidence"
    assert (tmp_path / "app.py").exists()


def test_changed_contract_does_not_validate(tmp_path, monkeypatch):
    project = setup_project(tmp_path, monkeypatch)
    validator = VerifiedRepairValidator(DoctestVerifier(), hypothesis(), 120, project)
    other = tmp_path / "isolated"
    other.mkdir()
    (other / "app.py").write_text(SOURCE.replace("    6", "    5"))
    result = validator.validate(ProjectInfo(other, ("python",)))
    assert not result.passed
    assert "contract changed" in result.results[-1].stderr


def test_hunter_parses_structured_response_and_uses_read_tools(tmp_path):
    (tmp_path / "app.py").write_text(SOURCE)
    from dataclasses import asdict
    from aidebug.openai_agent import OpenAIAgent

    responses = Mock(side_effect=[
        SimpleNamespace(id="first", output=[SimpleNamespace(type="function_call", name="read_file", arguments='{"relative_path":"app.py"}', call_id="read")], output_text=""),
        SimpleNamespace(output=[], output_text=json.dumps({"hypotheses": [asdict(hypothesis())]})),
    ])
    agent = OpenAIAgent(client=SimpleNamespace(responses=SimpleNamespace(create=responses)))
    assert BugHunter(agent).hunt(ProjectInfo(tmp_path, ("python",))) == (hypothesis(),)
    assert "return x + 2" in responses.call_args_list[1].kwargs["input"][0]["output"]


def test_hunter_rejects_escaping_source_path(tmp_path):
    from dataclasses import asdict
    agent = SimpleNamespace(_complete=Mock(return_value=json.dumps({"hypotheses": [asdict(hypothesis(suspected_file="../outside.py"))]})))
    with pytest.raises(ValueError, match="project-relative"):
        BugHunter(agent).hunt(ProjectInfo(tmp_path, ("python",)))


@pytest.mark.parametrize("json_mode", [False, True])
def test_hunt_cli_dispatch_and_output(tmp_path, monkeypatch, capsys, json_mode):
    from aidebug.hunt import HuntRun
    monkeypatch.setattr("aidebug.discovery.discover_repository", Mock(return_value=ProjectInfo(tmp_path, ("python",))))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    result = HuntRun((CheckResult("existing", ("pytest",), 0, "", "", 0),), (Finding(hypothesis(), "unconfirmed", "No reproduction"),), ())
    monkeypatch.setattr("aidebug.hunt.hunt_project", Mock(return_value=result))
    monkeypatch.setattr("sys.argv", ["aidebug", "hunt", str(tmp_path), *(["--json"] if json_mode else [])])
    assert cli.main() == 0
    output = capsys.readouterr().out
    if json_mode:
        assert json.loads(output)["findings"][0]["status"] == "unconfirmed"
    else:
        assert "Existing checks (isolated workspace):" in output
        assert "Proactive findings:" in output
