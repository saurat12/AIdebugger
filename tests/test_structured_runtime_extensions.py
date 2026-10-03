"""Generic structured verification stays bounded and source-local."""

import json

import pytest

from aidebug.hunt import BugHypothesis
from aidebug.models import ProjectInfo
from aidebug.reproduction import build_reproduction
from aidebug.verification import StructuredVerifier, validate_spec


def run(tmp_path, source, steps, assertions, **controls):
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    spec = {"kind": "verification_plan", "steps": steps, "assertions": assertions,
            "timeout_ms": 500, **controls}
    hypothesis = BugHypothesis("sample.py", "target", "Observable behavior differs", "synthetic input",
                               .9, {"kind": "structured"}, category="other", verification_plan=spec)
    return StructuredVerifier().verify(ProjectInfo(tmp_path, ("python",)), hypothesis)


def call(target="target", args=None, name="result"):
    return {"op": "call", "target": target, "args": args or [], "as": name}


def expected(value, source="return"):
    return [{"source": source, "op": "eq", "expected": value}]


def test_local_and_nested_helpers_resolve_without_module_initialization(tmp_path):
    source = ("def helper(x):\n    return x + 1\n"
              "def target(x):\n    def nested(y):\n        return helper(y) * 2\n"
              "    return nested(x)\n"
              "raise RuntimeError('module side effect')\n")
    result = run(tmp_path, source, [call(args=[2])], expected(7))
    assert result.status == "confirmed"
    assert '"return": 6' in result.evidence


def test_unresolved_local_call_names_scope_and_candidate(tmp_path):
    result = run(tmp_path, "def target():\n    return absent()\n", [call()], expected(1))
    assert result.status == "high_confidence"
    assert "absent" in result.evidence
    assert "selected source" in result.evidence
    assert "no candidate found" in result.evidence


@pytest.mark.parametrize(("constructor", "value", "want"), [
    ("list", [1, 2], [1, 2]), ("tuple", [1, 2], [1, 2]),
    ("dict", {"key": 3}, {"key": 3}), ("set", [2, 1], [1, 2]),
    ("record", {"field": 4}, {"field": 4}),
])
def test_bounded_constructor_adapters(tmp_path, constructor, value, want):
    result = run(tmp_path, "def target(value):\n    return value\n", [
        {"op": "construct_value", "constructor": constructor, "value": value, "as": "input"},
        call(args=[{"$ref": "input"}]),
    ], expected(None))
    assert result.status == "confirmed"
    assert '"return": ' + json.dumps(want) in result.evidence


def test_constructor_schema_and_unavailable_dependency_fail_closed(tmp_path):
    result = run(tmp_path, "def target():\n    return 1\n", [
        {"op": "construct_value", "constructor": "third_party_table", "value": [], "as": "data"},
        call(),
    ], expected(2))
    assert result.status == "high_confidence"
    assert "Constructor adapter is not registered" in result.evidence
    with pytest.raises(ValueError, match="Constructor input shape"):
        validate_spec({"kind": "verification_plan", "steps": [
            {"op": "construct_value", "constructor": "list", "value": {}, "as": "data"}],
            "assertions": expected(1), "timeout_ms": 500})


@pytest.mark.parametrize(("outcomes", "want", "actual"), [
    ([{"return": 2}], 5, 4),
    ([{"raise": "ValueError"}], "ok", "fallback"),
    ([{"return": 2}, {"return": 3}], 8, 5),
])
def test_deterministic_helper_stub_variants(tmp_path, outcomes, want, actual):
    source = ("def helper():\n    return 99\n"
              "def target():\n    try:\n        left = helper()\n        right = helper() if left != 99 else 0\n"
              "        return left + right\n    except ValueError:\n        return 'fallback'\n")
    result = run(tmp_path, source, [call()], expected(want),
                 stubs={"helper": {"outcomes": outcomes, "max_calls": 2}})
    assert result.status == "confirmed"
    assert str(actual) in result.evidence


def test_stub_call_limit_is_controlled_unverifiable(tmp_path):
    result = run(tmp_path, "def helper():\n    return 1\ndef target():\n    return helper() + helper()\n",
                 [call()], expected(3), stubs={"helper": {"outcomes": [{"return": 1}], "max_calls": 1}})
    assert result.status == "high_confidence"
    assert "stub call count" in result.evidence


def test_multi_step_bindings_and_nested_observation(tmp_path):
    source = ("from types import SimpleNamespace\n"
              "def target(value):\n    return SimpleNamespace(data={'items': [value, value + 1]})\n")
    result = run(tmp_path, source, [
        {"op": "construct_value", "constructor": "list", "value": [3], "as": "values"},
        call(args=[3]),
        {"op": "observe", "target": "result.data.items.1", "as": "second"},
    ], expected(5, "second"))
    assert result.status == "confirmed"
    assert '"second": 4' in result.evidence


def test_previously_bound_local_callable_and_safe_dependency(tmp_path):
    source = "from math import sqrt\ndef helper(value):\n    return value + 1\ndef target(value):\n    return helper(value)\n"
    local = run(tmp_path, source, [
        {"op": "bind_callable", "symbol": "helper", "as": "callback"},
        call("callback", [2]),
    ], expected(4))
    assert local.status == "confirmed"
    dependency = run(tmp_path, source, [
        {"op": "bind_callable", "symbol": "sqrt", "as": "callback"},
        call("callback", [9]),
    ], expected(4))
    assert dependency.status == "confirmed"
    assert '"return": 3.0' in dependency.evidence


def test_stub_cannot_replace_directly_observed_target(tmp_path):
    result = run(tmp_path, "def target():\n    return 1\n", [call()], expected(2),
                 stubs={"target": {"outcomes": [{"return": 3}], "max_calls": 1}})
    assert result.status == "high_confidence"
    assert "not a directly observed target" in result.evidence


def test_observation_blocks_dunder_and_callable_exposure(tmp_path):
    result = run(tmp_path, "def target():\n    return {'safe': 1}\n", [
        call(), {"op": "observe", "target": "result.__class__", "as": "escape"}], expected(1, "escape"))
    assert result.status == "high_confidence"
    assert "dunder" in result.evidence.lower()


def test_returned_project_local_object_fields_are_observable(tmp_path):
    source = ("class Result:\n    def __init__(self, value):\n        self.value = value\n"
              "def target():\n    return Result(3)\n")
    result = run(tmp_path, source, [call(), {"op": "observe", "target": "result.value", "as": "field"}],
                 expected(4, "field"))
    assert result.status == "confirmed"
    assert '"field": 3' in result.evidence


def test_dependency_depth_limit(tmp_path):
    source = "".join(f"def f{i}():\n    return f{i+1}()\n" for i in range(35)) + "def f35():\n    return 1\ndef target():\n    return f0()\n"
    result = run(tmp_path, source, [call()], expected(2))
    assert result.status == "high_confidence"
    assert "recursion depth" in result.evidence or "dependency depth" in result.evidence


def test_builder_emits_multi_step_stubbed_spec():
    spec = build_reproduction({"steps": [call()], "stubs": {"helper": {"outcomes": [{"return": 1}], "max_calls": 1}},
                               "expected_behavior": {"return_value": 2}}, "target")
    assert spec["kind"] == "verification_plan"
    assert spec["stubs"]["helper"]["outcomes"] == [{"return": 1}]
    assert spec["assertions"] == expected(2)
