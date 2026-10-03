"""Verification entry points and numeric oracles are explicit, generic metadata."""

from types import SimpleNamespace

import pytest

from aidebug.hunt import BugHypothesis, HuntRun
from aidebug.hunt_registry import RegistryVerifier, collect_findings
from aidebug.models import ProjectInfo
from aidebug.reproduction import ReproductionError, build_reproduction
from aidebug.verification import StructuredVerifier, resolve_verification_spec, validate_spec


def project(tmp_path, source):
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    return ProjectInfo(tmp_path, ("python",))


def hypothesis(reproduction, *, evidence="source call", symbol="suspect"):
    return BugHypothesis("sample.py", symbol, "Return value is wrong", evidence, .9,
                         reproduction, category="other", reproduction=reproduction)


def plan(steps, assertions):
    return {"kind": "verification_plan", "steps": steps, "assertions": assertions, "timeout_ms": 500}


def test_explicit_target_uses_local_link_but_keeps_suspected_symbol(tmp_path):
    source = "def helper(value):\n    return value + 1\ndef suspect(first, second):\n    return helper(first + second)\n"
    info = project(tmp_path, source)
    item = hypothesis({"verification_target": "helper", "args": [3], "expected": 5})
    resolved = resolve_verification_spec(info, item)
    assert resolved["verification_target"] == "helper"
    assert resolved["plan"]["steps"][0]["target"] == "helper"
    finding = RegistryVerifier().verify(info, item)
    assert finding.status == "confirmed"
    assert finding.hypothesis.suspected_symbol == "suspect"
    assert finding.record()["suspected_symbol"] == "suspect"
    assert finding.record()["verification_target"] == "helper"


def test_unrelated_alternate_target_is_unverifiable(tmp_path):
    info = project(tmp_path, "def suspect(value):\n    return value\ndef other(value):\n    return value + 1\n")
    item = hypothesis({"verification_target": "other", "args": [2], "expected": 4})
    with pytest.raises(ReproductionError, match="not evidence-supported"):
        resolve_verification_spec(info, item)
    finding = RegistryVerifier().verify(info, item)
    assert finding.status == "high_confidence"
    assert "not evidence-supported" in finding.evidence


def test_signature_preflight_uses_verification_target(tmp_path):
    info = project(tmp_path, "def helper(value):\n    return value\ndef suspect(first, second):\n    return helper(first + second)\n")
    good = hypothesis({"verification_target": "helper", "args": [3], "expected": 4})
    assert resolve_verification_spec(info, good)["verification_target"] == "helper"
    bad = hypothesis({"verification_target": "helper", "args": [3, 4], "expected": 5})
    with pytest.raises(ReproductionError, match="reproduction arguments do not match target signature"):
        resolve_verification_spec(info, bad)


def test_supplied_plan_records_both_symbols_and_conflicts_are_rejected(tmp_path):
    info = project(tmp_path, "def helper(value):\n    return value\ndef suspect(value):\n    return helper(value)\n")
    spec = {**plan([{"op": "call", "target": "helper", "args": [1], "as": "result"}],
                   [{"source": "return", "op": "eq", "expected": 2}]), "verification_target": "helper"}
    item = BugHypothesis("sample.py", "suspect", "Wrong return", "local call", .9,
                         {"kind": "structured"}, verification_plan=spec)
    finding = StructuredVerifier().verify(info, item)
    assert finding.record()["suspected_symbol"] == "suspect"
    assert finding.record()["verification_target"] == "helper"
    with pytest.raises(ReproductionError, match="suspected symbol / verification target mismatch"):
        build_reproduction({"target": "suspect", "verification_target": "helper", "args": [1], "expected": 2}, "suspect")


@pytest.mark.parametrize(("actual", "expected_status"), [(5.2634, "rejected"), (5.27, "confirmed")])
def test_approximate_numeric_assertion(tmp_path, actual, expected_status):
    info = project(tmp_path, f"def suspect():\n    return {actual}\n")
    spec = plan([{"op": "call", "target": "suspect", "as": "result"}],
                [{"source": "return", "op": "approx", "expected": 5.263, "abs_tol": 0.001, "rel_tol": 0.0001}])
    item = BugHypothesis("sample.py", "suspect", "Approximate result differs", "bounded sample", .9,
                         {"kind": "structured"}, verification_plan=spec)
    assert StructuredVerifier().verify(info, item).status == expected_status


@pytest.mark.parametrize("assertion", [
    {"source": "return", "op": "approx", "expected": 1},
    {"source": "return", "op": "approx", "expected": 1, "abs_tol": -1},
    {"source": "return", "op": "approx", "expected": True, "rel_tol": 0.1},
    {"source": "return", "op": "approx", "expected": 1, "rel_tol": 2},
    {"source": "return", "op": "approx", "expected": 1, "abs_tol": "0.1"},
])
def test_invalid_approximate_tolerance_is_rejected(assertion):
    with pytest.raises(ValueError, match="Approximate assertion"):
        validate_spec(plan([{"op": "call", "target": "suspect", "as": "result"}], [assertion]))


def test_builder_requires_structured_tolerance_and_never_reads_prose():
    result = build_reproduction({"args": [], "expected_behavior": {"approx": 5.263, "abs_tol": 0.001}}, "suspect")
    assert result["assertions"][0] == {"source": "return", "op": "approx", "expected": 5.263, "abs_tol": 0.001}
    with pytest.raises(ReproductionError, match="missing structured tolerance"):
        build_reproduction({"args": [], "expected_behavior": {"approx": 5.263}}, "suspect")


def test_generic_verification_coverage_tracks_builder_and_unverifiable(tmp_path):
    info = project(tmp_path, "def suspect():\n    return 1\n")
    item = BugHypothesis("sample.py", "suspect", "Wrong result", "observed sample", .9,
                         {"expected_behavior": {"return_value": 2}}, category="other",
                         reproduction={"expected_behavior": {"return_value": 2}})
    findings = collect_findings(info, (), (SimpleNamespace(hunt=lambda _: (item,)),), RegistryVerifier())
    metrics = HuntRun((), tuple(findings), ()).verification_metrics
    assert metrics["reproduction_builder_conclusions"] == 1
    assert metrics["verification_plans_executed"] == 1
    assert not any(key.startswith("planner_") for key in metrics)
