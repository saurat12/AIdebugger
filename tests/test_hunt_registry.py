import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug.hunt import BugHunter, BugHypothesis, Finding, HuntRun, hunt_project
from aidebug.hunt_registry import DetectorRegistry, VerifierRegistry, RegistryVerifier, collect_findings
from aidebug.hunt_signals import StateMutationAnalysis, ResourceAnalysis, NonterminationAnalysis, CrossModuleAnalysis, NativeValidationEvidence
from aidebug.hunt_strategies import HuntVerifier
from aidebug.models import CheckResult, ProjectInfo


def item(**updates):
    return replace(BugHypothesis("app.py", "f", "Domain-specific ordering loses an event",
                                {"summary": "Event order conflicts with the declared contract", "locations": [{"file": "app.py", "line": 2}], "observations": ["Queue update precedes commit"]},
                                .92, {"approach": "Replay a bounded event sequence", "steps": ["enqueue", "commit"], "expected_behavior": "Each event occurs once"},
                                "other"), **updates)


def test_open_ended_hunter_preserves_structured_metadata(tmp_path):
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    hypothesis = item(root_cause_key="queue-commit-order")
    agent = SimpleNamespace(_complete=Mock(return_value=json.dumps({"hypotheses": [asdict(hypothesis)]})))
    assert BugHunter(agent).hunt(ProjectInfo(tmp_path, ("python",))) == (hypothesis,)
    prompt = agent._complete.call_args.args[0]
    assert 'category="other"' in prompt and "open-ended" in prompt
    custom = replace(hypothesis, category="domain_specific_event_ordering")
    agent._complete.return_value = json.dumps({"hypotheses": [asdict(custom)]})
    assert BugHunter(agent).hunt(ProjectInfo(tmp_path, ("python",)))[0].category == custom.category


@pytest.mark.parametrize("spec", [{"kind": "future_temporal_oracle"}, {"kind": []}])
def test_unsupported_verification_remains_visible(tmp_path, monkeypatch, spec):
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    hypothesis = item(verification_spec=spec)
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *a, **k: ())
    agent = Mock()
    run = hunt_project(ProjectInfo(tmp_path, ("python",)), (SimpleNamespace(hunt=lambda p: (hypothesis,)),), HuntVerifier(), agent)
    assert run.findings[0].status == "high_confidence"
    assert run.findings[0].hypothesis.confidence == .92
    assert run.findings[0].hypothesis.evidence == hypothesis.evidence
    assert run.metrics["bugs_discovered"] == 1 and run.metrics["bugs_unverifiable"] == 1
    data = json.loads(run.findings_path.read_text())
    assert data["metrics"] == run.metrics
    assert data["findings"][0]["category"] == "other"
    assert "Domain-specific" in run.report_path.read_text()
    agent.propose.assert_not_called()


def test_category_does_not_gate_independent_verification(tmp_path):
    (tmp_path / "app.py").write_text("def f():\n    return 0\n")
    finding = HuntVerifier().verify(ProjectInfo(tmp_path, ("python",)), item(verification_spec={"kind": "equals", "expected": 1}))
    assert finding.status == "confirmed" and finding.check.returncode == 1
    assert finding.hypothesis.category == "other"


def test_detectors_and_verifiers_can_extend_without_orchestrator_changes(tmp_path):
    hypothesis = item(verification_spec={"kind": "custom_oracle"})
    detectors, verifiers = DetectorRegistry(), VerifierRegistry()
    detectors.register("custom", lambda agent, quick: SimpleNamespace(hunt=lambda p: (hypothesis,)), quick=True)
    verifiers.register("custom", lambda h: h.verification_spec.get("kind") == "custom_oracle",
                       lambda quick: SimpleNamespace(verify=lambda project, h: Finding(h, "confirmed", "Independent proof",
                                                   CheckResult("custom", ("trusted-bounded-evaluator",), 1, "", "Mismatch", 0))))
    findings = collect_findings(ProjectInfo(tmp_path, ()), (), detectors.build(None, True), RegistryVerifier(registry=verifiers))
    assert len(findings) == 1 and findings[0].status == "confirmed"
    with pytest.raises(ValueError, match="Duplicate"):
        detectors.register("custom", lambda *a: None)


def test_deterministic_capability_confirms_without_a_structured_plan(tmp_path):
    (tmp_path / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    hypothesis = item(reproduction={"kind": "custom_evidence"})
    registry = VerifierRegistry()
    registry.register("deterministic", lambda h: True,
                      lambda quick: SimpleNamespace(verify=lambda project, h: Finding(
                          h, "confirmed", "Independent deterministic evidence",
                          CheckResult("deterministic", ("bounded-check",), 1, "", "Mismatch", 0))))
    detector = SimpleNamespace(hunt=lambda project: (hypothesis,))
    findings = collect_findings(ProjectInfo(tmp_path, ("python",)), (), (detector,),
                                RegistryVerifier(registry=registry))
    assert findings[0].status == "confirmed"
    assert findings[0].verification_plan["source"] == "deterministic_verifier"


def test_syntax_location_in_evidence_routes_to_parser_without_llm(tmp_path):
    (tmp_path / "app.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    hypothesis = item(suspected_symbol="f", description="Parser rejects this definition",
                      evidence={"summary": "Syntax error at the declared location", "locations": [
                          {"file": "app.py", "line": 1}]}, verification_spec=None, verification_plan=None,
                      reproduction={"approach": "Parse the source", "steps": ["ast.parse"], "expected_behavior": "parses"})
    detector = SimpleNamespace(hunt=lambda project: (hypothesis,))
    findings = collect_findings(ProjectInfo(tmp_path, ("python",)), (), (detector,), RegistryVerifier())
    assert findings[0].status == "confirmed"
    assert findings[0].verification_plan["source"] == "deterministic_verifier"


def test_parse_capability_handles_module_finding_without_spec_or_callable_symbol(tmp_path):
    (tmp_path / "app.py").write_text("import math\nvalue = )\n", encoding="utf-8")
    hypothesis = item(suspected_symbol="module import and assignment",
                      description="The module source has a parse defect",
                      evidence={"summary": "The assignment is malformed", "source_range": {
                          "file": "app.py", "start_line": 2, "end_line": 2}},
                      verification_spec=None, verification_plan=None, reproduction=None)
    detector = SimpleNamespace(hunt=lambda project: (hypothesis,))
    syntax, original = collect_findings(ProjectInfo(tmp_path, ("python",)), (), (detector,), RegistryVerifier())
    assert syntax.status == "confirmed" and syntax.repair_authorized
    assert syntax.verification_plan["capability"] == "parse_compile"
    assert "SyntaxError:" in syntax.evidence and "app.py:2:" in syntax.evidence
    assert original.verification_state == "UNVERIFIABLE"


def test_parse_capability_does_not_confirm_unrelated_error(tmp_path):
    (tmp_path / "app.py").write_text("def f():\n    return 1\n\nvalue = (\n", encoding="utf-8")
    hypothesis = item(suspected_symbol="not necessarily callable",
                      evidence={"summary": "The finding concerns the function body", "locations": [
                          {"file": "app.py", "line": 2}]},
                      verification_spec=None, verification_plan=None, reproduction=None)
    detector = SimpleNamespace(hunt=lambda project: (hypothesis,))
    findings = collect_findings(ProjectInfo(tmp_path, ("python",)), (), (detector,), RegistryVerifier())
    assert len(findings) == 2
    assert findings[0].status == "confirmed"
    assert findings[1].status == "high_confidence"
    assert "remains separate" in findings[1].evidence
    assert findings[1].verification_plan["source"] == "deterministic_verifier"


def test_claimed_confirmation_without_reproduction_is_withheld(tmp_path):
    registry = VerifierRegistry()
    registry.register("unsafe_claim", lambda h: True, lambda q: SimpleNamespace(verify=lambda p, h: Finding(h, "confirmed", "Trust me")))
    assert RegistryVerifier(registry=registry).verify(ProjectInfo(tmp_path, ()), item()).status == "high_confidence"


def test_dedup_preserves_all_signals_and_does_not_mix_independent_causes(tmp_path):
    first = item(root_cause_key="queue-update-before-commit")
    duplicate = replace(first, description="Commit ordering causes dropped event", category="static_analysis", confidence=.6,
                        evidence={"summary": "Second detector found the same ordering"})
    different = replace(first, root_cause_key="unclosed-resource", description="Missing cleanup")
    other_file = replace(first, suspected_file="other.py")
    verifier = SimpleNamespace(verify=lambda p, h: Finding(h, "unconfirmed", "Not reproducible by current engine"))
    findings = collect_findings(ProjectInfo(tmp_path, ()), (),
                                (SimpleNamespace(hunt=lambda p: (first, duplicate, different, other_file)),), verifier)
    assert len(findings) == 3
    assert [s["hypothesis"]["confidence"] for s in findings[0].signals] == [.92, .6]
    assert findings[0].signals[1]["hypothesis"]["evidence"] == duplicate.evidence


def test_conflicting_root_cause_verification_is_merged_conservatively(tmp_path):
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    first = item(root_cause_key="same")
    other = replace(first, description="Contradictory reproduction")
    verifier = SimpleNamespace(verify=lambda p, h: Finding(h, "rejected" if h == other else "unconfirmed", "Evidence"))
    findings = collect_findings(ProjectInfo(tmp_path, ()), (), (SimpleNamespace(hunt=lambda p: (first, other)),), verifier)
    assert len(findings) == 1
    assert findings[0].status == "unconfirmed"
    assert len(findings[0].signals) == 2
    run = HuntRun((), tuple(findings), ())
    assert run.metrics["bugs_rejected"] == 0 and run.metrics["bugs_unverifiable"] == 1


def test_source_signals_are_read_only_and_conservative(tmp_path):
    source = "from helper import work\ndef mutate(xs):\n    for x in xs:\n        xs.remove(x)\ndef resource():\n    return open('data').read()\ndef wait():\n    while True:\n        pass\ndef caller():\n    return work(1, 2)\n"
    (tmp_path / "app.py").write_text(source)
    (tmp_path / "helper.py").write_text("def work(x):\n    return x\n")
    project = ProjectInfo(tmp_path, ("python",))
    for detector in (StateMutationAnalysis(), ResourceAnalysis(), NonterminationAnalysis(), CrossModuleAnalysis()):
        findings = detector.hunt(project)
        assert findings
        assert all(HuntVerifier().verify(project, h).status in ("high_confidence", "unconfirmed") for h in findings)
    assert (tmp_path / "app.py").read_text() == source


def test_native_evidence_does_not_imply_confirmed_application_bug(tmp_path):
    project = ProjectInfo(tmp_path, ())
    check = CheckResult("npm test", ("npm", "test"), 127, "", "Tool unavailable", 0, "npm missing")
    detector = NativeValidationEvidence()
    assert detector.hunt(project) == ()
    hypothesis, = detector.hunt_with_checks(project, (check,))
    finding = HuntVerifier().verify(project, hypothesis)
    assert finding.status == "unconfirmed" and finding.check is None
    assert hypothesis.evidence["status"] == "BLOCKED"


def test_overlapping_confirmed_findings_trigger_only_one_repair(tmp_path, monkeypatch):
    from aidebug.models import AnalysisReport, PatchProposal
    (tmp_path / "app.py").write_text("def f():\n    return 0\n")
    first = item(root_cause_key="incorrect-constant", verification_spec={"kind": "equals", "expected": 1})
    second = replace(first, description="Same constant defect from semantic review", confidence=.7, category="semantic")
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *a, **k: ())
    agent = SimpleNamespace(analyze=Mock(return_value=AnalysisReport("Wrong constant", .9, "Expected one")),
                            propose=Mock(return_value=PatchProposal("--- a/app.py\n+++ b/app.py\n@@ -2 +2 @@\n-    return 0\n+    return 1\n", "Return one")))
    run = hunt_project(ProjectInfo(tmp_path, ("python",)), (SimpleNamespace(hunt=lambda p: (first, second)),), HuntVerifier(), agent)
    assert len(run.findings) == len(run.repairs) == 1
    assert run.findings[0].status == "confirmed" and len(run.findings[0].signals) == 2
    assert run.repairs[0].validation.passed
    assert run.metrics["bugs_discovered"] == run.metrics["bugs_confirmed"] == run.metrics["bugs_repairable"] == 1
    agent.propose.assert_called_once()


def test_custom_verifier_can_pin_repair_postcondition(tmp_path, monkeypatch):
    from aidebug.hunt import VerifiedRepairValidator
    from aidebug.verification import StructuredVerifier
    class CustomOracle:
        def prepare_repair(self, project, hypothesis):
            return hypothesis
        def contract(self, project, hypothesis):
            return hypothesis.verification_spec["expected"]
        def verify(self, project, hypothesis):
            safe = replace(hypothesis, verification_spec={"kind": "equals", "expected": hypothesis.verification_spec["expected"]})
            return replace(StructuredVerifier().verify(project, safe), hypothesis=hypothesis)
    registry = VerifierRegistry()
    registry.register("domain_oracle", lambda h: h.verification_spec.get("kind") == "domain_oracle", lambda q: CustomOracle())
    verifier = RegistryVerifier(registry=registry)
    project = ProjectInfo(tmp_path, ("python",))
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *a, **k: ())
    hypothesis = item(verification_spec={"kind": "domain_oracle", "expected": 1})
    assert VerifiedRepairValidator(verifier, hypothesis, 1, project).validate(project).passed
