import difflib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug.hunt import BugHunter, BugHypothesis, hunt_main, hunt_project
from aidebug.hunt_strategies import (CoverageGapAnalysis, CrossFunctionChecks, ExceptionPathAnalysis,
                                     GeneratedEdgeCases, HuntVerifier, PropertyChecks, StaticAnalysis, build_detectors, cases)
from aidebug.models import AnalysisReport, CheckResult, PatchProposal, ProjectInfo


def project(tmp_path, source):
    (tmp_path / "app.py").write_text(source)
    return ProjectInfo(tmp_path, ("python",))


def empty_agent():
    return SimpleNamespace(_complete=Mock(return_value='{"hypotheses": []}'), analyze=Mock(), propose=Mock())


def checks(monkeypatch, root):
    def run(project, *args, **kwargs):
        assert project.root != root
        return (CheckResult("existing", ("pytest",), 0, "1 passed", "", 0),)
    monkeypatch.setattr("aidebug.hunt.run_checks", run)


def test_default_full_is_superset_of_quick():
    agent = empty_agent()
    quick = build_detectors(agent, True)
    full = build_detectors(agent)
    assert [type(strategy) for strategy in quick] == [StaticAnalysis, BugHunter]
    assert [type(strategy) for strategy in full[:2]] == [type(strategy) for strategy in quick]
    assert [type(strategy) for strategy in full[2:7]] == [CoverageGapAnalysis, GeneratedEdgeCases, PropertyChecks, ExceptionPathAnalysis, CrossFunctionChecks]
    assert {type(strategy).__name__ for strategy in full[7:]} == {"StateMutationAnalysis", "ResourceAnalysis", "NonterminationAnalysis", "CrossModuleAnalysis", "NativeValidationEvidence"}
    assert quick[1].quick and not full[1].quick


def test_edge_case_domains_include_zero_and_empty_collections():
    tree = __import__("ast").parse("def target(values):\n    return sum(values) / len(values)\n")
    generated = cases(tree.body[0])
    assert (0,) in generated
    assert ([],) in generated


def test_multiple_independent_edge_triggers_in_one_symbol_remain_distinct(tmp_path):
    target = project(tmp_path, 'def ratio(value):\n    """aidebug total"""\n    return 1 / (value * (value - 1))\n')
    findings = GeneratedEdgeCases().hunt(target)
    observed = {finding.reproduction["args"][0] for finding in findings}
    assert {0, 1} <= observed


@pytest.mark.parametrize("quick", [False, True])
def test_healthy_project_source_is_inspected_and_empty_report_saved(tmp_path, monkeypatch, quick):
    source = "def double(x):\n    return x * 2\n"
    target = project(tmp_path, source)
    (tmp_path / "test_app.py").write_text("from app import double\ndef test_double():\n    assert double(2) == 4\n")
    checks(monkeypatch, tmp_path)
    agent = empty_agent()
    result = hunt_project(target, build_detectors(agent, quick), HuntVerifier(quick), agent, quick=quick)
    assert not result.findings
    assert "return x * 2" in agent._complete.call_args.args[1]
    assert result.mode == ("quick" if quick else "full")
    assert json.loads(result.findings_path.read_text())["findings"] == []
    assert "project is bug-free" in result.report_path.read_text()
    agent.propose.assert_not_called()
    assert (tmp_path / "app.py").read_text() == source


def test_full_generated_invariant_confirms_hidden_bug_and_repairs(tmp_path, monkeypatch):
    source = 'def magnitude(x):\n    """aidebug invariant: result >= 0"""\n    return x\n'
    fixed = source.replace("    return x\n", "    if x < 0:\n        return -x\n    return x\n")
    target = project(tmp_path, source)
    checks(monkeypatch, tmp_path)
    agent = empty_agent()
    agent.analyze.return_value = AnalysisReport("Negative magnitude", 0.9, "Declared nonnegative result violated")
    patch = "".join(difflib.unified_diff(source.splitlines(True), fixed.splitlines(True), "a/app.py", "b/app.py"))
    agent.propose.return_value = PatchProposal(patch, "Return the magnitude for negative inputs")
    result = hunt_project(target, build_detectors(agent), HuntVerifier(), agent)
    assert all(check.passed for check in result.existing_checks)
    confirmed = [finding for finding in result.findings if finding.status == "confirmed"]
    assert confirmed and confirmed[0].hypothesis.category == "property_invariant"
    assert result.repairs[0].validation.passed
    assert result.repairs[0].validation.plan_reused
    assert not result.repairs[0].validation.replanned
    assert result.repairs[0].validated_patch_path.is_file()
    record = next(item for item in json.loads(result.findings_path.read_text())["findings"] if item["verification_status"] == "confirmed")
    required = {"finding_id", "file", "symbol", "category", "hypothesis", "evidence", "confidence", "reproduction_strategy", "verification_status", "verification_evidence"}
    assert required <= record.keys()
    assert record["verification_status"] == "confirmed"
    assert (tmp_path / "app.py").read_text() == source


def test_generated_exception_without_domain_contract_is_not_confirmed(tmp_path):
    target = project(tmp_path, "def inverse(x):\n    return 1 / x\n")
    item, = GeneratedEdgeCases().hunt(target)
    result = HuntVerifier().verify(target, item)
    assert result.status == "high_confidence"
    assert "domain is not declared" in result.evidence
    assert HuntVerifier(quick=True).verify(target, item).status == "unconfirmed"


def test_declared_totality_allows_exception_confirmation(tmp_path):
    target = project(tmp_path, 'def inverse(x):\n    """aidebug total"""\n    return 1 / x\n')
    item, = GeneratedEdgeCases().hunt(target)
    assert HuntVerifier().verify(target, item).status == "confirmed"


def test_property_false_positive_is_rejected(tmp_path):
    target = project(tmp_path, 'def f(x):\n    """aidebug invariant: result >= 0"""\n    return x * x\n')
    item = BugHypothesis("app.py", "f", "Negative result suspected", "source", 0.9, "check -1", "property_invariant", {"kind": "invariant", "args": [-1]})
    assert HuntVerifier().verify(target, item).status == "rejected"
    assert not PropertyChecks().hunt(target)


def test_coverage_gap_is_not_a_confirmed_bug(tmp_path):
    target = project(tmp_path, "def f(x):\n    return x\n")
    (tmp_path / "coverage.json").write_text(json.dumps({"files": {"app.py": {"missing_lines": [2]}}}))
    item, = CoverageGapAnalysis().hunt(target)
    assert item.category == "coverage_gap"
    result = HuntVerifier().verify(target, item)
    assert result.status == "unconfirmed"
    assert "not proof" in result.evidence


def test_static_and_exception_evidence_remains_non_confirmed(tmp_path):
    target = project(tmp_path, "def f(values=[]):\n    try:\n        return values[0]\n    except:\n        pass\n")
    items = (*StaticAnalysis().hunt(target), *ExceptionPathAnalysis().hunt(target))
    assert {item.category for item in items} == {"static_analysis", "exception_path"}
    assert all(HuntVerifier().verify(target, item).status == "high_confidence" for item in items)


def test_cross_function_declared_equivalence(tmp_path):
    target = project(tmp_path, 'def left(x):\n    """aidebug equivalent: right"""\n    return x + 1\n\ndef right(x):\n    return x * 2\n')
    item, = CrossFunctionChecks().hunt(target)
    assert HuntVerifier().verify(target, item).status == "confirmed"


def test_cross_function_arity_is_a_report_only_suspicion(tmp_path):
    target = project(tmp_path, "def caller(x):\n    return callee(x, x)\ndef callee(x):\n    return x\n")
    item, = CrossFunctionChecks().hunt(target)
    assert item.reproduction["kind"] == "call_arity"
    assert HuntVerifier().verify(target, item).status == "high_confidence"


def test_static_coverage_fallback_is_explicitly_heuristic(tmp_path):
    target = project(tmp_path, "def untested(x):\n    return x\n")
    item, = CoverageGapAnalysis().hunt(target)
    assert "heuristic" in item.evidence
    assert HuntVerifier().verify(target, item).status == "unconfirmed"


def test_non_confirmed_statuses_are_all_persisted_without_repair(tmp_path, monkeypatch):
    from aidebug.hunt import Finding
    target = project(tmp_path, "def f(x):\n    return x\n")
    checks(monkeypatch, tmp_path)
    items = tuple(BugHypothesis("app.py", "f", status, "source evidence", 0.9, "review")
                  for status in ("high_confidence", "unconfirmed", "rejected"))
    detector = SimpleNamespace(hunt=lambda project: items)
    verifier = SimpleNamespace(verify=lambda project, item: Finding(item, item.description, "verification evidence"))
    agent = empty_agent()
    result = hunt_project(target, (detector,), verifier, agent)
    assert {row["verification_status"] for row in json.loads(result.findings_path.read_text())["findings"]} == {"high_confidence", "unconfirmed", "rejected"}
    assert not result.repairs
    agent.propose.assert_not_called()


def test_failed_repair_still_saves_confirmed_finding(tmp_path, monkeypatch):
    from aidebug.hunt import Finding
    target = project(tmp_path, "def f(x):\n    return x\n")
    checks(monkeypatch, tmp_path)
    item = BugHypothesis("app.py", "f", "Mismatch", "source", 0.9, "reproduce")
    failed = CheckResult("hunt:case", ("bounded",), 1, "", "AssertionError", 0)
    verifier = SimpleNamespace(verify=lambda project, hypothesis: Finding(hypothesis, "confirmed", "Proof", failed))
    agent = empty_agent()
    agent.analyze.side_effect = RuntimeError("error")
    result = hunt_project(target, (SimpleNamespace(hunt=lambda project: (item,)),), verifier, agent)
    assert not result.repair_errors
    assert not result.repairs
    assert result.findings[0].verification_state == "CONFIRMED_BUT_EXPECTED_BEHAVIOR_UNKNOWN"
    assert result.findings_path.is_file()
    assert "confirmed" in result.report_path.read_text()
    assert not list((tmp_path / ".aidebug").glob("validated_patch*"))


@pytest.mark.parametrize("quick", [False, True])
def test_cli_default_and_quick_selection(tmp_path, monkeypatch, capsys, quick):
    from aidebug.hunt import HuntRun
    target = project(tmp_path, "def f():\n    return 1\n")
    monkeypatch.setattr("aidebug.discovery.discover_repository", Mock(return_value=target))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    run = Mock(return_value=HuntRun((), (), (), mode="quick" if quick else "full"))
    monkeypatch.setattr("aidebug.hunt.hunt_project", run)
    assert hunt_main([str(tmp_path), "--json", *(["--quick"] if quick else [])]) == 0
    assert run.call_args.kwargs["quick"] == quick
    assert len(run.call_args.args[1]) == (2 if quick else 12)
    assert json.loads(capsys.readouterr().out)["mode"] == ("quick" if quick else "full")
