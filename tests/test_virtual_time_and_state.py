"""Bounded time and module-state plans use only declared source bindings."""

import sys
import types

import pytest

from aidebug.hunt import BugHypothesis
from aidebug._verification_worker import ConstructorAdapter, Engine, Unsupported
from aidebug.models import ProjectInfo
from aidebug.reproduction import ReproductionError, build_reproduction
from aidebug.verification import StructuredVerifier, validate_spec


def verify(tmp_path, source, steps, assertions, **extra):
    (tmp_path / "module.py").write_text(source, encoding="utf-8")
    spec = {"kind": "verification_plan", "steps": steps, "assertions": assertions, "timeout_ms": 500, **extra}
    hypothesis = BugHypothesis("module.py", "target", "Explicit expected result", "synthetic",
                               .9, {"kind": "structured"}, category="other", verification_plan=spec)
    return StructuredVerifier().verify(ProjectInfo(tmp_path, ("python",)), hypothesis)


def call(name="result"):
    return {"op": "call", "target": "target", "as": name}


def assert_eq(source, expected):
    return {"source": source, "op": "eq", "expected": expected}


def test_fixed_virtual_time_and_bounded_advancement(tmp_path):
    source = "import time\ndef target():\n    return time.time()\n"
    result = verify(tmp_path, source, [call("before"), {"op": "advance_time", "seconds": 10, "as": "advanced"},
                                       call("after")], [assert_eq("before", 100), assert_eq("after", 111)],
                    clock={"source": "time.time", "start": 100})
    assert result.status == "confirmed"
    assert '"before": 100' in result.evidence
    assert '"after": 110' in result.evidence


def test_time_binding_is_explicit_and_unsafe_source_unverifiable(tmp_path):
    source = "import time\ndef target():\n    return time.time()\n"
    missing = verify(tmp_path, source, [call()], [assert_eq("return", 2)])
    assert missing.status == "high_confidence"
    assert "time" in missing.evidence.lower()
    invalid = verify(tmp_path, source, [call()], [assert_eq("return", 2)],
                     clock={"source": "datetime.now", "start": 100})
    assert invalid.status == "high_confidence"
    assert "Deterministic time source cannot be safely substituted" in invalid.evidence
    blocked = verify(tmp_path, "import time\ndef target():\n    return time.sleep(1)\n",
                     [call()], [assert_eq("return", 0)], clock={"source": "time.time", "start": 100})
    assert blocked.status == "high_confidence"
    assert "allowlisted" in blocked.evidence


def test_virtual_time_total_limit(tmp_path):
    result = verify(tmp_path, "def target():\n    return 1\n", [
        {"op": "advance_time", "seconds": 50000, "as": "first"},
        {"op": "advance_time", "seconds": 50000, "as": "second"}, call()],
        [assert_eq("return", 2)], clock={"source": "time.time", "start": 100})
    assert result.status == "high_confidence"
    assert "virtual-time advancement" in result.evidence


def test_bounded_module_dictionary_observation_and_repeated_calls(tmp_path):
    source = ("STATE = {}\n"
              "def target():\n    STATE['calls'] = STATE.get('calls', 0) + 1\n    return STATE['calls']\n")
    result = verify(tmp_path, source, [
        {"op": "state_setup", "symbol": "STATE", "action": "clear", "as": "clear"},
        call("first"), call("second"),
        {"op": "observe_state", "symbol": "STATE", "mode": "snapshot", "as": "state"},
        {"op": "observe_state", "symbol": "STATE", "mode": "size", "as": "size"},
        {"op": "observe_state", "symbol": "STATE", "mode": "contains", "key": "calls", "as": "contains"},
    ], [assert_eq("first", 1), assert_eq("second", 2), assert_eq("state", {"calls": 3}),
        assert_eq("size", 1), assert_eq("contains", True)])
    assert result.status == "confirmed"
    assert '"state": {"calls": 2}' in result.evidence


def test_controlled_state_insert_and_scalar_reset(tmp_path):
    source = "STATE = {}\nFLAG = 1\ndef target():\n    return STATE['key'] + FLAG\n"
    result = verify(tmp_path, source, [
        {"op": "state_setup", "symbol": "STATE", "action": "insert", "value": {"key": "key", "value": 2}, "as": "insert"},
        {"op": "state_setup", "symbol": "FLAG", "action": "reset", "value": 3, "as": "reset"},
        call(),
    ], [assert_eq("return", 6)])
    assert result.status == "confirmed"
    assert '"return": 5' in result.evidence


def test_state_snapshot_is_frozen_before_later_calls(tmp_path):
    source = "STATE = []\ndef target():\n    STATE.append(1)\n    return STATE\n"
    result = verify(tmp_path, source, [
        call("first"), {"op": "observe_state", "symbol": "STATE", "mode": "snapshot", "as": "snapshot"},
        call("second")], [assert_eq("snapshot", [1]), assert_eq("second", [1, 1])])
    assert result.status == "rejected"
    assert '"snapshot": [1]' in result.evidence


def test_module_traversal_and_nonlocal_state_are_blocked(tmp_path):
    source = "import os\nSTATE = {}\ndef target():\n    return 1\n"
    result = verify(tmp_path, source, [{"op": "observe_state", "symbol": "os", "mode": "snapshot", "as": "value"}],
                    [assert_eq("value", {})])
    assert result.status == "high_confidence"
    assert "local literal binding required" in result.evidence
    with pytest.raises(ValueError, match="selected local identifier"):
        validate_spec({"kind": "verification_plan", "steps": [
            {"op": "observe_state", "symbol": "STATE.__class__", "mode": "snapshot", "as": "value"}],
            "assertions": [assert_eq("value", {})], "timeout_ms": 500})


def test_builder_preserves_explicit_clock_and_state_plan():
    data = {"clock": {"source": "time.time", "start": 100}, "steps": [
        {"op": "state_setup", "symbol": "STATE", "action": "clear", "as": "reset"},
        {"op": "advance_time", "seconds": 1, "as": "tick"},
        call(), {"op": "observe_state", "symbol": "STATE", "mode": "size", "as": "size"}],
        "assertions": [assert_eq("size", 1)]}
    spec = build_reproduction(data, "target")
    assert spec["clock"] == data["clock"]
    assert spec["steps"] == data["steps"]
    with pytest.raises(ReproductionError, match="constructor adapter unavailable"):
        build_reproduction({"required_adapter": "missing"}, "target")
    with pytest.raises(ReproductionError, match="helper dependency cannot be stubbed"):
        build_reproduction({"required_helper": "unknown"}, "target")
    with pytest.raises(ReproductionError, match="deterministic time source cannot be substituted"):
        build_reproduction({"time_source": "unknown.clock"}, "target")


def test_trusted_constructor_adapter_registration_is_bounded_and_opaque(monkeypatch):
    engine = Engine("", {"kind": "verification_plan"})
    adapter = ConstructorAdapter("synthetic", list, 3, "SAFE_LITERAL", {"snapshot", "index"},
                                 False, True, tuple)
    engine.register_constructor_adapter("synthetic", adapter)
    observed = engine.run_plan({"steps": [
        {"op": "construct_value", "constructor": "synthetic", "value": [2, 3], "as": "data"},
        {"op": "observe", "target": "data.1", "as": "item"}]})
    assert observed["item"] == 3
    with pytest.raises(Unsupported, match="shape or size"):
        engine.run_plan({"steps": [{"op": "construct_value", "constructor": "synthetic", "value": [1, 2, 3, 4], "as": "too_large"}]})
    engine.register_constructor_adapter("restricted", ConstructorAdapter("restricted", list, 3, "SAFE_LITERAL", {"snapshot"}, False, True, tuple))
    with pytest.raises(Unsupported, match="does not allow read-only index"):
        engine.run_plan({"steps": [
            {"op": "construct_value", "constructor": "restricted", "value": [1], "as": "limited"},
            {"op": "observe", "target": "limited.0", "as": "item"}]})
    with pytest.raises(Unsupported, match="contract is invalid"):
        engine.register_constructor_adapter("unsafe", ConstructorAdapter("unsafe", list, 3, "SAFE_LITERAL", {"module"}, False, True, tuple))
    with pytest.raises(Unsupported, match="not installed and allowlisted"):
        engine.register_constructor_adapter("external", ConstructorAdapter("external", list, 3, "SAFE_INSTALLED_LIBRARY", {"snapshot"}, False, True, tuple, "absent_dependency"))
    monkeypatch.setitem(sys.modules, "synthetic_dependency", types.ModuleType("synthetic_dependency"))
    engine.register_constructor_adapter("external", ConstructorAdapter("external", list, 3, "SAFE_INSTALLED_LIBRARY", {"snapshot", "index"}, False, True, tuple, "synthetic_dependency"))
    assert engine.run_plan({"steps": [
        {"op": "construct_value", "constructor": "external", "value": [7], "as": "external_data"},
        {"op": "observe", "target": "external_data.0", "as": "element"}]})["element"] == 7
