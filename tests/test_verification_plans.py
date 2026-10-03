import json

import pytest

from aidebug.hunt import BugHypothesis, VerifiedRepairValidator
from aidebug.models import ProjectInfo
from aidebug.verification import StructuredVerifier, load_cached_plans, resolve_verification_spec, validate_spec
from aidebug.hunt_registry import RegistryVerifier


def verify(tmp_path, source, plan, symbol="target"):
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    hypothesis = BugHypothesis("app.py", symbol, "Concrete behavior violates its contract", "Observed source path",
                               0.91, {"kind": "structured"}, category="other", verification_plan=plan)
    finding = StructuredVerifier().verify(ProjectInfo(tmp_path, ("python",)), hypothesis)
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == source
    return finding


def plan(steps, assertions, **extra):
    return {"kind": "verification_plan", "steps": steps, "assertions": assertions,
            "timeout_ms": 500, **extra}


def test_plan_reproduces_incorrect_return_then_rejects_fixed_behavior(tmp_path):
    broken = "def target(values):\n    total = 0\n    for i in range(len(values) + 1):\n        total += values[i]\n    return total / len(values)\n"
    expected = plan([{"op": "call", "target": "target", "args": [[1, 2, 3]], "as": "average"}],
                    [{"source": "return", "op": "eq", "expected": 2}])
    result = verify(tmp_path, broken, expected)
    assert result.status == "confirmed"
    assert '"exception": {"type": "IndexError"' in result.evidence

    fixed = "def target(values):\n    return sum(values) / len(values)\n"
    result = verify(tmp_path, fixed, expected)
    assert result.status == "rejected"
    assert '"return": 2.0' in result.evidence


def test_plan_reproduces_unexpected_exception_against_expected_result(tmp_path):
    finding = verify(tmp_path, "def target(rows):\n    for name, score in rows:\n        if score >= 60:\n            return name\n",
                     plan([{"op": "call", "target": "target", "args": [[{"name": "Alice", "score": 85}]], "as": "passing"}],
                          [{"source": "return", "op": "eq", "expected": "Alice"}]))
    assert finding.status == "confirmed"
    assert '"type": "TypeError"' in finding.evidence


def test_plan_supports_multi_step_instance_state_and_mutation(tmp_path):
    source = ("class Basket:\n    items = []\n"
              "    def add(self, value):\n        self.items.append(value)\n")
    state_plan = plan([
        {"op": "construct", "symbol": "Basket", "args": [], "as": "first"},
        {"op": "construct", "symbol": "Basket", "args": [], "as": "second"},
        {"op": "call", "target": "first.add", "args": ["apple"], "kwargs": {}, "as": "added"},
        {"op": "observe", "target": "second.items", "as": "other_items"},
    ], [{"source": "other_items", "op": "eq", "expected": []}])
    finding = verify(tmp_path, source, state_plan, "Basket")
    assert finding.status == "confirmed"
    assert '"other_items": ["apple"]' in finding.evidence

    mutation = verify(tmp_path, "def target(items):\n    for item in items:\n        if item == 'remove':\n            items.remove(item)\n    return items\n",
                      plan([{"op": "call", "target": "target", "args": [["remove", "remove"]], "as": "result"}],
                           [{"source": "args_after", "op": "eq", "expected": [[]]}]))
    assert mutation.status == "confirmed"
    assert '"args_after": [["remove"]]' in mutation.evidence


def test_plan_captures_bounded_stdout_and_stderr(tmp_path):
    source = "from sys import stderr\ndef target():\n    print('ready', end='!')\n    print('warning', file=stderr)\n    return 4\n"
    finding = verify(tmp_path, source,
                     plan([{"op": "call", "target": "target", "args": [], "as": "value"}],
                          [{"source": "stdout", "op": "eq", "expected": "ready!"},
                           {"source": "stderr", "op": "eq", "expected": "warning\n"},
                           {"source": "return", "op": "eq", "expected": 4}]))
    assert finding.status == "rejected"
    assert '"stdout": "ready!"' in finding.evidence
    assert '"stderr": "warning\\n"' in finding.evidence


def test_plan_supports_deterministic_randomness_and_virtual_resources(tmp_path):
    random_finding = verify(tmp_path,
                            "import random\ndef target(items):\n    return items[random.randint(0, len(items))]\n",
                            plan([{"op": "call", "target": "target", "args": [["only"]], "kwargs": {}, "as": "item"}],
                                 [{"source": "return", "op": "eq", "expected": "only"}], random_values={"randint": [1]}))
    assert random_finding.status == "confirmed"
    assert '"type": "IndexError"' in random_finding.evidence
    resource_finding = verify(tmp_path,
                              "def target(name):\n    handle = open(name)\n    return handle.read()\n",
                              plan([{"op": "call", "target": "target", "args": ["data.txt"], "kwargs": {}, "as": "data"}],
                                   [{"source": "resources_open", "op": "eq", "expected": 0}],
                                   files={"data.txt": "virtual contents"}))
    assert resource_finding.status == "confirmed"
    assert '"resources_open": 1' in resource_finding.evidence


def test_plan_timeout_confirmation_requires_explicit_timeout_assertion(tmp_path, monkeypatch):
    monkeypatch.setattr("aidebug.verification.observe", lambda *a: {"timed_out": True, "deadline_ms": 100})
    timeout_plan = plan([{"op": "call", "target": "target", "args": [], "kwargs": {}, "as": "result"}],
                        [{"source": "timed_out", "op": "eq", "expected": True}], timeout_ms=100)
    finding = verify(tmp_path, "def target():\n    while True:\n        pass\n", timeout_plan)
    assert finding.status == "confirmed"
    assert finding.check is not None and not finding.check.passed


@pytest.mark.parametrize("invalid", [
    {"kind": "verification_plan", "steps": [{"op": "execute", "code": "unsafe"}], "assertions": [], "timeout_ms": 500},
    {"kind": "verification_plan", "steps": [{"op": "call", "target": "os.system", "args": ["echo x"], "as": "x"}], "assertions": [{"source": "x", "op": "eq", "expected": 0}], "timeout_ms": 500},
    {"kind": "verification_plan", "steps": [{"op": "call", "target": "target", "args": [], "as": "x"}], "assertions": [{"source": "x", "op": "eq", "expected": float("nan")}], "timeout_ms": 500},
])
def test_invalid_or_future_plan_stays_visible_and_unverifiable(tmp_path, invalid):
    finding = verify(tmp_path, "def target():\n    return 0\n", invalid)
    assert finding.status == "high_confidence"
    assert finding.check is None
    assert "no confirmation or repair authorized" in finding.evidence


def test_plan_schema_bounds_inputs_and_nested_repair_plans():
    excessive = plan([{"op": "call", "target": "target", "args": [[[[[[[[[[[[[[1]]]]]]]]]]]]]], "as": "x"}],
                     [{"source": "x", "op": "eq", "expected": None}])
    with pytest.raises(ValueError):
        validate_spec(excessive)
    repair = plan([{"op": "call", "target": "target", "args": [], "as": "x"}],
                  [{"source": "x", "op": "eq", "expected": 1}])
    nested = plan([{"op": "call", "target": "target", "args": [], "as": "x"}],
                  [{"source": "x", "op": "eq", "expected": 1}], repair_plan={**repair, "repair_plan": repair})
    with pytest.raises(ValueError, match="Nested repair plans"):
        validate_spec(nested)


def test_plan_cache_is_source_and_hypothesis_bound_and_revalidated(tmp_path):
    source = "def target():\n    return 1\n"
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    project = ProjectInfo(tmp_path, ("python",))
    hypothesis = BugHypothesis("app.py", "target", "Expected 2", "evidence", .9, "call",
                               verification_plan=plan([{"op": "call", "target": "target", "args": [], "as": "v"}],
                                                      [{"source": "return", "op": "eq", "expected": 2}]))
    first = resolve_verification_spec(project, hypothesis)
    artifact_dir = tmp_path / ".aidebug"
    artifact_dir.mkdir()
    record = {"findings": [{"verification_status": "confirmed", "verification_plan": first}]}
    (artifact_dir / "hunt_findings_fixture.json").write_text(json.dumps(record), encoding="utf-8")
    cache = load_cached_plans(tmp_path)
    second = resolve_verification_spec(project, hypothesis, cache)
    assert second["source"] == "validated_cache"
    assert second["plan"] == first["plan"]

    (tmp_path / "app.py").write_text(source.replace("return 1", "return 3"), encoding="utf-8")
    changed = resolve_verification_spec(project, hypothesis, cache)
    assert changed["source"] == "hunter_plan"
    assert changed["fingerprint"] != first["fingerprint"]


def test_non_llm_resolution_handles_explicit_spec_without_planner(tmp_path):
    (tmp_path / "app.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    spec = plan([{"op": "call", "target": "target", "args": [], "as": "value"}],
                [{"source": "return", "op": "eq", "expected": 2}])
    hypothesis = BugHypothesis("app.py", "target", "Expected 2", "Observed 1", .9, "example", verification_spec=spec)
    resolved = resolve_verification_spec(ProjectInfo(tmp_path, ("python",)), hypothesis)
    assert resolved["source"] == "structured_hypothesis"
    assert resolved["plan"] == spec


@pytest.mark.parametrize(("source", "line", "expected"), [
    ("def target(:\n    pass\n", 1, "confirmed"),
    ("def target():\n    return 1\n", 1, "rejected"),
    ("def target():\n    return 1\n\nvalue = (\n", 1, "unconfirmed"),
])
def test_python_parse_capability_is_location_bound_and_never_imports(tmp_path, source, line, expected):
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    hypothesis = BugHypothesis("app.py", "target", "Syntax failure at claimed location", "Parser diagnostic", .95,
                               {"kind": "python_syntax"}, verification_spec={"kind": "python_syntax", "line": line})
    finding = StructuredVerifier().verify(ProjectInfo(tmp_path, ("python",)), hypothesis)
    assert finding.status == expected
    assert "parser" in finding.evidence.lower()


def test_private_project_helper_is_verifiable_but_dunder_traversal_is_blocked(tmp_path):
    source = "def _relative_periods(value):\n    return value + 1\n"
    spec = plan([{"op": "call", "target": "_relative_periods", "args": [1], "kwargs": {}, "as": "value"}],
                [{"source": "return", "op": "eq", "expected": 3}])
    finding = verify(tmp_path, source, spec, "_relative_periods")
    assert finding.status == "confirmed"
    invalid = plan([{"op": "call", "target": "__class__", "args": [], "kwargs": {}, "as": "value"}],
                   [{"source": "return", "op": "eq", "expected": None}])
    with pytest.raises(ValueError, match="dunder"):
        validate_spec(invalid)


def test_restricted_runtime_supports_max_with_local_mapping_get_key(tmp_path):
    source = "def target(mapping):\n    return max(mapping, key=mapping.get)\n"
    spec = plan([{"op": "call", "target": "target", "args": [{"a": 1, "b": 5, "c": 3}], "kwargs": {}, "as": "key"}],
                [{"source": "return", "op": "eq", "expected": "b"}])
    finding = verify(tmp_path, source, spec)
    assert finding.status == "rejected", finding.evidence


def test_module_expression_slicing_reproduces_only_selected_unresolved_name(tmp_path):
    source = "import os\nvalue = f'{missing_local}'\n"
    (tmp_path / "module.py").write_text(source, encoding="utf-8")
    spec = {"kind": "module_fragment", "target_lines": [2, 2], "target_kind": "expression",
            "expected": {"exception": None}, "timeout_ms": 500}
    hypothesis = BugHypothesis("module.py", "", "Selected expression references an unresolved local", "Static reference evidence",
                               .91, {"kind": "module_fragment"}, verification_spec=spec)
    finding = StructuredVerifier().verify(ProjectInfo(tmp_path, ("python",)), hypothesis)
    assert finding.status == "confirmed", finding.evidence
    assert '"type": "NameError"' in finding.evidence


def test_open_ended_hypothesis_remains_unverifiable_without_a_structured_plan(tmp_path):
    source = "def target(items):\n    return items[len(items)]\n"
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    hypothesis = BugHypothesis("app.py", "target", "A valid index is rejected at the boundary", "Index equals length",
                               .9, "Call with an input whose index equals its length", category="other")
    assert resolve_verification_spec(ProjectInfo(tmp_path, ("python",)), hypothesis) is None
    finding = StructuredVerifier().verify(ProjectInfo(tmp_path, ("python",)), hypothesis)
    assert finding.verification_state == "UNVERIFIABLE"
    assert "free-form reproduction text is never executed" in finding.evidence


@pytest.mark.parametrize(("expression", "expected"), [
    ("sum(x for x in values)", 6),
    ("[x * 2 for x in values if x > 1]", [4, 6]),
    ("{x: x * 2 for x in values}", {"1": 2, "2": 4, "3": 6}),
    ("{x for x in values}", [1, 2, 3]),
    ("min(values) + max(values) + len(values)", 7),
    ("len(max([values]))", 3),
    ("all(x > 0 for x in values)", True),
    ("any(x == 2 for x in values)", True),
    ("list(enumerate(values))", [[0, 1], [1, 2], [2, 3]]),
    ("list(zip(values, range(3)))", [[1, 0], [2, 1], [3, 2]]),
])
def test_restricted_runtime_supports_common_comprehensions_and_builtins(tmp_path, expression, expected):
    source = f"def target(values):\n    return {expression}\n"
    plan_value = plan([{"op": "call", "target": "target", "args": [[1, 2, 3]], "kwargs": {}, "as": "result"}],
                      [{"source": "return", "op": "eq", "expected": expected}])
    finding = verify(tmp_path, source, plan_value)
    assert finding.status == "rejected", finding.evidence


def module_finding(tmp_path, source, target_line, expected=None, bindings=()):
    (tmp_path / "module.py").write_text(source, encoding="utf-8")
    spec = {"kind": "module_fragment", "target_lines": [target_line, target_line],
            "required_bindings": list(bindings), "expected": expected or {"exception": None}, "timeout_ms": 500}
    hypothesis = BugHypothesis("module.py", "", "Selected module statement violates its expected behavior", "Bounded source evidence",
                               .91, {"kind": "module_fragment"}, verification_spec=spec)
    return StructuredVerifier().verify(ProjectInfo(tmp_path, ("python",)), hypothesis)


def test_module_fragment_reproduces_undefined_name_and_call(tmp_path):
    name = module_finding(tmp_path, "message = f'{missing_name}'\n", 1)
    assert name.status == "confirmed" and '"type": "NameError"' in name.evidence
    call = module_finding(tmp_path, "result = missing_function(3)\n", 1)
    assert call.status == "confirmed" and '"type": "NameError"' in call.evidence


def test_module_fragment_resolves_safe_preceding_binding_and_missing_key(tmp_path):
    source = "sales_data = {'known': 3}\nresult = sales_data['missing']\n"
    finding = module_finding(tmp_path, source, 2, bindings=("sales_data",))
    assert finding.status == "confirmed" and '"type": "KeyError"' in finding.evidence


def test_module_fragment_does_not_run_unrelated_module_side_effects(tmp_path):
    source = "print('unrelated setup')\nvalue = 1 / 0\n"
    finding = module_finding(tmp_path, source, 2)
    assert finding.status == "confirmed"
    assert '"stdout": ""' in finding.evidence


def test_unsupported_expression_names_exact_ast_capability(tmp_path):
    finding = verify(tmp_path, "def target(value):\n    return (value := 2)\n",
                     plan([{"op": "call", "target": "target", "args": [1], "as": "result"}],
                          [{"source": "return", "op": "eq", "expected": 2}]))
    assert finding.status == "high_confidence"
    assert "unsupported expression capability: NamedExpr" in finding.evidence


def test_pinned_plan_is_reused_and_invalidated_plan_is_blocked(tmp_path):
    source = "def target():\n    return 1\n"
    path = tmp_path / "app.py"
    path.write_text(source, encoding="utf-8")
    project = ProjectInfo(tmp_path, ("python",))
    spec = plan([{"op": "call", "target": "target", "args": [], "kwargs": {}, "as": "result"}],
                [{"source": "return", "op": "eq", "expected": 2}])
    hypothesis = BugHypothesis("app.py", "target", "Expected return 2", "Concrete contract", .95, "structured",
                               verification_spec=spec)
    wrapper = {"fingerprint": "source-bound", "verifier": "restricted-ast-runtime", "plan": spec}
    validator = VerifiedRepairValidator(RegistryVerifier(), hypothesis, 1, project, pinned_plan=wrapper)
    path.write_text("def target():\n    match 2:\n        case 2:\n            return 2\n", encoding="utf-8")
    report = validator.validate(project)
    assert report.final_status == "TARGETED VERIFICATION BLOCKED"
    assert report.plan_reused and not report.passed
    assert "unsupported" in report.targeted.stderr.casefold()
    assert "doctest" not in report.targeted.name
