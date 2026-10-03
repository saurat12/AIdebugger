"""Evidence-driven DSL construction and static call binding in synthetic projects."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug.hunt import BugHypothesis
from aidebug.hunt_registry import RegistryVerifier, collect_findings
from aidebug.models import ProjectInfo
from aidebug.verification import resolve_verification_spec


def setup(tmp_path, source, reproduction, symbol="_target", evidence="Explicit reproduction contract"):
    (tmp_path / "fragment.py").write_text(source, encoding="utf-8")
    item = BugHypothesis("fragment.py", symbol, "Observed behavior violates the supplied contract", evidence, .95, reproduction)
    return ProjectInfo(tmp_path, ("python",)), item


@pytest.mark.parametrize("data,source,kind,status", [
    ({"args": [3], "expected": 6}, "def _target(value):\n    return value * 2\n", "function_call", "rejected"),
    ({"args": [3], "expected_behavior": {"return_value": 6}}, "def _target(value):\n    return value * 2\n", "function_call", "rejected"),
    ({"args": [3], "kwargs": {"scale": 2}, "expected": 6}, "def _target(value, *, scale):\n    return value * scale\n", "function_call", "rejected"),
    ({"expected": 6}, "def _target(value=6):\n    return value\n", "function_call", "rejected"),
    ({"args": [3], "expected": 6}, "def _helper(value):\n    return value * 2\n_target = _helper\n", "function_call", "rejected"),
    ({"args": [[]], "expected_exception": "IndexError"}, "def _target(values):\n    return values[0]\n", "expected_exception", "confirmed"),
    ({"args": [[]], "expected_behavior": {"exception": "IndexError"}}, "def _target(values):\n    return values[0]\n", "expected_exception", "confirmed"),
    ({"expected_behavior": {"field": "metric", "expected": 6}}, "from types import SimpleNamespace\ndef _target():\n    return SimpleNamespace(metric=6)\n", "verification_plan", "rejected"),
    ({"expected_behavior": {"source": "return", "op": "contains", "expected": 2}}, "def _target():\n    return [1, 2]\n", "verification_plan", "rejected"),
    ({"expected_behavior": {"source": "return", "op": "ge", "expected": 6}}, "def _target():\n    return 5\n", "verification_plan", "confirmed"),
    ({"expected_behavior": {"source": "return", "op": "is_null", "expected": True}}, "def _target():\n    return None\n", "verification_plan", "rejected"),
    ({"args": [[1]], "expected_args": [[1, 2]]}, "def _target(values):\n    values.append(2)\n", "mutation_check", "rejected"),
    ({"args": ["sample.txt"], "files": {"sample.txt": "value"}, "expected_open_resources": 0}, "def _target(name):\n    with open(name) as handle:\n        return handle.read()\n", "file_resource_check", "rejected"),
    ({"expected": 2, "random_values": {"randint": [2]}}, "import random\ndef _target():\n    return random.randint(0, 3)\n", "deterministic_random", "rejected"),
])
def test_concrete_metadata_builds_and_executes_existing_dsl(tmp_path, data, source, kind, status):
    project, item = setup(tmp_path, source, data)
    resolved = resolve_verification_spec(project, item)
    assert resolved["plan"]["kind"] == kind
    finding = RegistryVerifier().verify(project, item)
    assert finding.status == status, finding.evidence
    assert (tmp_path / "fragment.py").read_text() == source


def test_object_construction_multiple_calls_and_state_observation(tmp_path):
    source = "class _State:\n    def __init__(self, initial=0):\n        self.value = initial\n    def add(self, *, amount):\n        self.value += amount\n"
    data = {"steps": [{"op": "construct", "symbol": "_State", "args": [1], "as": "state"},
                      {"op": "call", "target": "state.add", "kwargs": {"amount": 2}, "as": "first"},
                      {"op": "call", "target": "state.add", "kwargs": {"amount": 3}, "as": "second"},
                      {"op": "observe", "target": "state.value", "as": "value"}],
            "expected_behavior": {"source": "value", "op": "eq", "expected": 6}}
    project, item = setup(tmp_path, source, data, symbol="module state")
    assert resolve_verification_spec(project, item)["plan"]["timeout_ms"] == 500
    finding = RegistryVerifier().verify(project, item)
    assert finding.status == "rejected", finding.evidence
    observed_data = {**data, "steps": data["steps"][:-1],
                     "expected_behavior": {"source": "state.value", "op": "eq", "expected": 6}}
    assert RegistryVerifier().verify(project, replace(item, reproduction_strategy=observed_data)).status == "rejected"


@pytest.mark.parametrize("source,data,reason", [
    ("def _target(value):\n    return value\n", {"args": [1, 2], "expected_exception": "TypeError"}, "too many positional arguments"),
    ("def _target(value):\n    return value\n", {"kwargs": {"other": 1}, "expected": 1}, "unexpected keyword argument"),
    ("def _target(value):\n    return value\n", {"args": [], "expected": 1}, "missing a required argument"),
    ("def _target(value, /):\n    return value\n", {"kwargs": {"value": 1}, "expected": 1}, "positional-only"),
    ("def _target(*, value):\n    return value\n", {"args": [1], "expected": 1}, "too many positional arguments"),
])
def test_argument_mismatch_never_reaches_runtime(tmp_path, monkeypatch, source, data, reason):
    project, item = setup(tmp_path, source, data)
    execution = Mock(side_effect=AssertionError("runtime must not execute malformed calls"))
    monkeypatch.setattr("aidebug.verification.observe", execution)
    finding = RegistryVerifier().verify(project, item)
    assert finding.verification_state == "UNVERIFIABLE", finding.evidence
    assert "target mismatch" in finding.evidence
    assert "reproduction arguments do not match target signature" in finding.evidence
    assert reason in finding.evidence
    assert finding.check is None
    execution.assert_not_called()


def test_all_plan_calls_are_bound_before_any_execution(tmp_path, monkeypatch):
    source = "class _State:\n    def __init__(self):\n        print('not executed')\n    def add(self, value):\n        self.value = value\n"
    data = {"steps": [{"op": "construct", "symbol": "_State", "as": "state"},
                      {"op": "call", "target": "state.add", "args": [1, 2], "as": "result"}],
            "assertions": [{"source": "return", "op": "eq", "expected": None}]}
    project, item = setup(tmp_path, source, data)
    execution = Mock()
    monkeypatch.setattr("aidebug.verification.observe", execution)
    finding = RegistryVerifier().verify(project, item)
    assert finding.verification_state == "UNVERIFIABLE"
    assert "steps[1]" in finding.evidence
    execution.assert_not_called()


def test_internal_project_call_failure_is_not_a_plan_mismatch(tmp_path):
    source = "def _helper(value):\n    return value\ndef _target(value):\n    return _helper(value, 2)\n"
    project, item = setup(tmp_path, source, {"args": [1], "expected_exception": "TypeError"})
    finding = RegistryVerifier().verify(project, item)
    assert finding.status == "confirmed", finding.evidence
    assert '"type": "TypeError"' in finding.evidence


@pytest.mark.parametrize("explicit", ["metadata", "evidence", "absent"])
def test_alternative_target_requires_explicit_evidence(tmp_path, explicit):
    source = "def _helper(value):\n    return value + 1\ndef _target(first, second):\n    return _helper(first + second)\n"
    data = {"args": [3], "expected": 5}
    evidence = {"reproduction_target": "_helper"} if explicit == "evidence" else "Boundary evidence"
    if explicit == "metadata":
        data["target"] = "_helper"
    project, item = setup(tmp_path, source, data, evidence=evidence)
    finding = RegistryVerifier().verify(project, item)
    assert finding.status == ("high_confidence" if explicit == "absent" else "confirmed"), finding.evidence
    assert finding.hypothesis.suspected_symbol == "_target"
    if explicit != "absent":
        assert resolve_verification_spec(project, item)["plan"]["steps"][0]["target"] == "_helper"


@pytest.mark.parametrize("data", ["inspect the result", {"args": [1]}, {"expected_behavior": "inspect the result"}])
def test_ambiguous_or_prose_reproduction_stays_unverifiable(tmp_path, monkeypatch, data):
    project, item = setup(tmp_path, "def _target(value=1):\n    return value\n", data)
    execution = Mock()
    monkeypatch.setattr("aidebug.verification.observe", execution)
    finding = RegistryVerifier().verify(project, item)
    assert finding.verification_state == "UNVERIFIABLE"
    execution.assert_not_called()


def test_deterministic_builder_confirms_without_model_planning(tmp_path):
    project, item = setup(tmp_path, "def _target(value):\n    return value\n", {"args": [1], "expected_behavior": {"return_value": 2}})
    findings = collect_findings(project, (), (SimpleNamespace(hunt=lambda p: (item,)),), RegistryVerifier())
    finding, = findings
    assert finding.status == "confirmed", finding.evidence
    assert finding.repair_authorized


@pytest.mark.parametrize("generated,field,failure_kind", [
    ({"kind": "verification_plan", "steps": [{"op": "execute", "as": "result"}],
      "assertions": [{"source": "return", "op": "eq", "expected": 2}], "timeout_ms": 500}, "steps[0].op", "unsupported operation"),
    ({"kind": "verification_plan", "steps": [{"op": "call", "target": "_target", "args": [1], "as": "result"}],
      "assertions": [{"source": "return", "op": "eq", "expected": 2}]}, "timeout_ms", "schema violation"),
    ({"kind": "verification_plan", "steps": [{"op": "call", "target": "_target", "args": [1, 2], "as": "result"}],
      "assertions": [{"source": "return", "op": "eq", "expected": 2}], "timeout_ms": 500}, "steps[0]", "target mismatch"),
    ({"kind": "verification_plan", "steps": [{"op": "call", "target": "_target", "args": [1], "as": "result"}],
      "assertions": [{"source": "missing", "op": "eq", "expected": 2}], "timeout_ms": 500}, "assertions[0].source", "unsupported operation"),
])
def test_invalid_supplied_plan_reports_field_and_failure_kind(tmp_path, monkeypatch, generated, field, failure_kind):
    project, item = setup(tmp_path, "def _target(value):\n    return value\n", generated)
    execution = Mock()
    monkeypatch.setattr("aidebug.verification.observe", execution)
    finding, = collect_findings(project, (), (SimpleNamespace(hunt=lambda p: (item,)),), RegistryVerifier())
    assert finding.verification_state == "UNVERIFIABLE"
    assert field in finding.evidence
    assert failure_kind in finding.evidence
    assert finding.verification_plan["validation_error"]["field"] == field
    assert finding.verification_plan["validation_error"]["failure_kind"] == failure_kind
    assert finding.evidence == finding.verification_plan["unsupported_reason"]
    execution.assert_not_called()


def test_free_form_reproduction_stays_unverifiable_without_a_model_call(tmp_path):
    project, item = setup(tmp_path, "def _target(value):\n    return value\n", "Concrete strategy requires a plan")
    finding, = collect_findings(project, (), (SimpleNamespace(hunt=lambda p: (item,)),), RegistryVerifier())
    assert finding.verification_state == "UNVERIFIABLE"
    assert "free-form reproduction text is never executed" in finding.evidence
    assert finding.verification_plan["plan"] is None


@pytest.mark.parametrize("name", ["_entry", "operation", "arbitrary_symbol"])
def test_target_binding_is_independent_of_symbol_and_file_names(tmp_path, name):
    project, item = setup(tmp_path, f"def {name}(value):\n    return value\n", {"args": [1], "expected": 1}, symbol=name)
    new_path = tmp_path / "another_name.py"
    (tmp_path / "fragment.py").rename(new_path)
    item = replace(item, suspected_file=new_path.name, category="other")
    assert RegistryVerifier().verify(project, item).status == "rejected"
