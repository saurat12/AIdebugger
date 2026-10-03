"""Standalone syntax defects and dependent deterministic verification."""

from dataclasses import replace
from difflib import unified_diff
from types import SimpleNamespace

from aidebug.hunt import BugHypothesis, hunt_project
from aidebug.hunt_registry import RegistryVerifier, collect_findings
from aidebug.models import AnalysisReport, PatchProposal, ProjectInfo
from aidebug.syntax_findings import discover_syntax_findings
from aidebug.validation import syntax_check


BROKEN = "def value(:\n    return 1\n"
FIXED = "def value():\n    return 1\n"


def project(tmp_path, source=BROKEN):
    (tmp_path / "fragment.py").write_text(source, encoding="utf-8")
    return ProjectInfo(tmp_path, ("python",))


def behavior():
    return BugHypothesis("fragment.py", "value", "The return value is wrong", "fragment.py:1",
                         .95, "Call value and compare its result", verification_spec={"kind": "equals", "expected": 2})


def detector(*items):
    return SimpleNamespace(hunt=lambda project: items)


def patch(before, after):
    return "".join(unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                fromfile="a/fragment.py", tofile="b/fragment.py"))


class Agent:
    def __init__(self, proposals):
        self.proposals = iter(proposals)
        self.seen = []

    def analyze(self, context):
        self.seen.append(context)
        return AnalysisReport("The parser rejects the source", 1.0, "Compile-only evidence")

    def propose(self, context, analysis):
        return PatchProposal(next(self.proposals), "Repair the syntax")


def test_standalone_syntax_is_confirmed_and_behavior_remains_separate(tmp_path):
    info = project(tmp_path)
    findings = collect_findings(info, (), (detector(behavior()),), RegistryVerifier())
    assert len(findings) == 2
    syntax, behavioral = findings
    assert syntax.status == "confirmed" and syntax.repair_authorized
    assert syntax.verification_plan["verifier"] == "python-parse"
    assert syntax.hypothesis.evidence["line"] == 1
    assert syntax.hypothesis.evidence["column"] > 0
    assert syntax.hypothesis.evidence["source_context"]
    assert behavioral.status == "high_confidence" and behavioral.check is None
    assert behavioral.hypothesis.verification_spec["kind"] == "equals"


def test_duplicate_explicit_syntax_signal_merges_into_one_finding(tmp_path):
    info = project(tmp_path)
    canonical, = discover_syntax_findings(info)
    duplicate = replace(canonical.hypothesis, suspected_symbol="different symbol", root_cause_key=None)
    findings = collect_findings(info, (), (detector(duplicate), detector(duplicate)), RegistryVerifier())
    assert len(findings) == 1
    assert findings[0].finding_id == canonical.finding_id
    assert len(findings[0].signals) == 3


def test_structured_syntax_signal_and_existing_check_preserve_provenance(tmp_path):
    info = project(tmp_path)
    canonical, = discover_syntax_findings(info)
    location = canonical.hypothesis.evidence
    signal = BugHypothesis("fragment.py", "statement", "Parser rejects this statement",
                           {"file": "fragment.py", "line": location["line"],
                            "parser_error": location["parser_error"]}, .9, "Compile source")
    check = syntax_check(info)
    findings = collect_findings(info, (check,), (detector(signal),), RegistryVerifier())
    assert len(findings) == 1
    assert findings[0].status == "confirmed"
    assert {entry["detector"] for entry in findings[0].signals} == {
        "DeterministicSyntaxScan", "ExistingCheck", "SimpleNamespace"}


def test_syntax_repair_gets_bounded_evidence_and_retries_behavior(tmp_path):
    info = project(tmp_path)
    agent = Agent([patch(BROKEN, FIXED)])
    run = hunt_project(info, (detector(behavior()),), RegistryVerifier(), agent, timeout=2)
    assert len(run.repairs) == 1
    repair, = run.repairs
    assert repair.validation.passed
    assert repair.validation.confirmation_verifier == repair.validation.repair_verifier == "python-parse"
    assert repair.validated_patch_path.is_file()
    assert "SyntaxError" in agent.seen[0].failed_check.stderr
    assert "Bounded syntax evidence" in agent.seen[0].failed_check.stderr
    assert (tmp_path / "fragment.py").read_text() == BROKEN
    retried = next(item for item in run.findings if item.hypothesis.suspected_symbol == "value")
    assert retried.status == "confirmed"
    assert retried.repairability == "blocked_pending_syntax_apply"
    assert retried.verification_plan["retried_after_syntax_repair"] == repair.finding_id
    report = run.report_path.read_text(encoding="utf-8")
    assert "## Confirmed Bugs" in report and "## Unverified Bugs" in report
    assert "## Syntax-Blocked Finding Retries" in report
    assert "retried after syntax repair" in report


def test_invalid_repair_never_passes_syntax_verification(tmp_path):
    info = project(tmp_path)
    invalid = "def value(:\n    return 2\n"
    agent = Agent([patch(BROKEN, invalid), patch(invalid, FIXED)])
    run = hunt_project(info, (detector(),), RegistryVerifier(), agent, timeout=2)
    assert len(run.repairs) == 1
    repair, = run.repairs
    assert repair.attempts == 2
    assert repair.validation.passed
    assert repair.validation.targeted.passed
    assert any("syntax pre-validation failed" in error for error in repair.patch_errors)


def test_unrepaired_syntax_keeps_dependent_finding_unverified(tmp_path):
    info = project(tmp_path)
    agent = Agent([])
    # The syntax scan itself performs no LLM verification. Repair is omitted here.
    findings = collect_findings(info, (), (detector(behavior()),), RegistryVerifier())
    assert agent.seen == []
    assert findings[1].verification_state == "UNVERIFIABLE"


def test_missing_safe_source_context_confirms_but_blocks_repair(tmp_path, monkeypatch):
    info = project(tmp_path)
    monkeypatch.setattr("aidebug.syntax_findings._bounded_context", lambda source, line: None)
    finding, = discover_syntax_findings(info)
    assert finding.status == "confirmed"
    assert not finding.repair_authorized
    assert finding.repairability == "blocked_syntax_location_unavailable"


def test_syntax_evidence_is_bounded_and_does_not_echo_credential_literal(tmp_path):
    secret = "sk-example-secret-value-123456789"
    info = project(tmp_path, f"OPENAI_API_KEY = '{secret}'\nvalue = )\n")
    finding, = discover_syntax_findings(info)
    assert secret not in str(finding.hypothesis.evidence)
    assert len(str(finding.hypothesis.evidence)) < 1500


def test_syntax_discovery_never_calls_llm_verification(tmp_path, monkeypatch):
    info = project(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("LLM verification is forbidden")
    monkeypatch.setattr("aidebug.openai_agent.OpenAIAgent._complete", forbidden)
    syntax, behavior_result = collect_findings(info, (), (detector(behavior()),), RegistryVerifier())
    assert syntax.status == "confirmed"
    assert behavior_result.verification_state == "UNVERIFIABLE"
