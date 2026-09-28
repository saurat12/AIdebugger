from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from aidebug.hunt import BugHypothesis, Finding, HuntRun, hunt_project
from aidebug.hunt_registry import collect_findings
from aidebug.hunt_strategies import HuntVerifier
from aidebug.models import ProjectInfo


def item(**updates):
    base = BugHypothesis("src/main.py", "get_passing_students", "Dictionary records raise TypeError instead of producing passing students",
                         "The caller supplies dictionaries but the loop unpacks records as pairs", .95,
                         "Call with one passing student dictionary", category="other")
    return replace(base, **updates)


def structured_plan():
    return {"kind": "verification_plan", "timeout_ms": 500,
            "steps": [{"op": "call", "target": "get_passing_students",
                       "args": [[{"name": "Alice", "score": 85}]], "kwargs": {}, "as": "passing"}],
            "assertions": [{"source": "return", "op": "eq", "expected": {"Alice": 85}}]}


def test_doctest_hypothesis_does_not_fall_back_to_legacy_scalar_verifier(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.py").write_text("def get_passing_students(rows):\n    return rows\n")
    finding = HuntVerifier().verify(ProjectInfo(tmp_path, ("python",)),
                                    item(reproduction={"kind": "doctest"}, verification_spec=None))
    assert finding.status in ("unconfirmed", "high_confidence")
    assert "scalar Python doctests" not in finding.evidence
    assert "UNVERIFIABLE" in finding.evidence


def test_structured_student_input_uses_generic_planner_runtime(tmp_path):
    (tmp_path / "src").mkdir()
    source = ("def get_passing_students(students, passing_score=60):\n"
              "    passing = {}\n"
              "    for name, score in students:\n"
              "        if score >= passing_score:\n            passing[name] = score\n"
              "    return passing\n")
    (tmp_path / "src/main.py").write_text(source)
    hypothesis = item(verification_plan=structured_plan())
    detector = SimpleNamespace(hunt=lambda project: (hypothesis,))
    from aidebug.models import CheckResult
    findings = collect_findings(ProjectInfo(tmp_path, ("python",)), (), (detector,), HuntVerifier())
    assert len(findings) == 1
    assert findings[0].status == "confirmed"
    assert '"type": "TypeError"' in findings[0].evidence
    assert findings[0].verification_plan["plan"]["kind"] == "verification_plan"


def test_coverage_gap_is_not_verified_or_promoted_by_same_symbol_execution(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.py").write_text("def get_passing_students(students):\n    return students\n")
    gap = item(category="coverage_gap", reproduction={"kind": "coverage_gap"}, verification_spec=None,
               verification_plan=structured_plan(), description="No direct test reference found")
    verifier = Mock()
    findings = collect_findings(ProjectInfo(tmp_path, ("python",)), (),
                                (SimpleNamespace(hunt=lambda project: (gap,)),), verifier)
    assert len(findings) == 1 and not findings[0].is_bug
    assert findings[0].status == "unconfirmed"
    verifier.verify.assert_not_called()
    assert HuntRun((), tuple(findings), ()).metrics["bugs_discovered"] == 0


def test_different_behavioral_defect_is_a_separate_canonical_finding(tmp_path):
    (tmp_path / "src").mkdir()
    source = "def get_passing_students(students):\n    for name, score in students:\n        return score\n"
    (tmp_path / "src/main.py").write_text(source)
    gap = item(category="coverage_gap", reproduction={"kind": "coverage_gap"}, verification_spec=None,
               description="No direct test reference found", root_cause_key="coverage-gap")
    bug_plan = {"kind": "verification_plan", "timeout_ms": 500,
                "steps": [{"op": "call", "target": "get_passing_students",
                           "args": [[["Alice", 85]]], "kwargs": {}, "as": "score"}],
                "assertions": [{"source": "return", "op": "eq", "expected": {"Alice": 85}}]}
    bug = item(description="Expected mapping is replaced by a scalar score", verification_plan=bug_plan,
               root_cause_key="wrong-return-shape")
    findings = collect_findings(ProjectInfo(tmp_path, ("python",)), (),
                                (SimpleNamespace(hunt=lambda project: (gap, bug)),), HuntVerifier())
    assert len(findings) == 2
    observation, confirmed = findings
    assert not observation.is_bug and observation.status == "unconfirmed"
    assert confirmed.is_bug and confirmed.status == "confirmed"
    assert observation.finding_id != confirmed.finding_id


def test_duplicate_root_cause_merges_provenance_and_runs_one_repair(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("def f():\n    return 0\n")
    first = BugHypothesis("app.py", "f", "Return value is wrong", "Semantic review", .9, "Call f",
                          root_cause_key="wrong-constant", verification_spec={"kind": "equals", "expected": 1})
    second = replace(first, description="Independent detector found the same wrong constant", category="other", confidence=.8)
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *args, **kwargs: ())
    from aidebug.models import AnalysisReport, PatchProposal
    agent = SimpleNamespace(analyze=Mock(return_value=AnalysisReport("Return one", .9, "Expected 1")),
                            propose=Mock(return_value=PatchProposal("--- a/app.py\n+++ b/app.py\n@@ -2 +2 @@\n-    return 0\n+    return 1\n", "Correct result")))
    run = hunt_project(ProjectInfo(tmp_path, ("python",)),
                       (SimpleNamespace(hunt=lambda project: (first, second)),), HuntVerifier(), agent)
    assert len(run.findings) == 1 and len(run.repairs) == 1
    assert len(run.findings[0].signals) == 2
    assert run.findings[0].signals[0]["finding_id"] == run.findings[0].signals[1]["finding_id"]
    assert agent.propose.call_count == 1


def test_confirmed_defect_without_expected_behavior_never_calls_fixer(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.py").write_text("def get_passing_students(rows):\n    raise TypeError('bad input')\n")
    hypothesis = item(verification_spec={"kind": "expected_exception", "args": [[1]],
                                         "expected_exception": "TypeError"})
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *args, **kwargs: ())
    agent = SimpleNamespace(analyze=Mock(), propose=Mock())
    run = hunt_project(ProjectInfo(tmp_path, ("python",)),
                       (SimpleNamespace(hunt=lambda project: (hypothesis,)),), HuntVerifier(), agent)
    assert run.findings[0].verification_state == "CONFIRMED_BUT_EXPECTED_BEHAVIOR_UNKNOWN"
    assert run.findings[0].repairability == "blocked_expected_behavior_unknown"
    agent.analyze.assert_not_called()
    agent.propose.assert_not_called()
    assert run.metrics["bugs_confirmed"] == 1
    assert run.metrics["bugs_repairable"] == 0
    assert run.metrics["bugs_blocked_by_unspecified_expected_behavior"] == 1
    from aidebug.hunt import render_repair_output
    output = render_repair_output(run)
    assert "Verification: DEFECT REPRODUCED" in output
    assert "Repair Authorization: BLOCKED" in output
    assert "Reason: expected behavior is unspecified" in output
