"""Generic source-span and bounded runtime regressions in synthetic projects."""

import pytest

from aidebug.hunt import BugHypothesis
from aidebug.hunt_registry import RegistryVerifier
from aidebug.models import ProjectInfo
from aidebug.verification import StructuredVerifier, _syntax_evidence_matches


@pytest.mark.parametrize("evidence,line,confirmed", [
    ("src/unit.py:20", 20, True),
    ("src/unit.py:20-24", 20, True),
    ("src/unit.py:20-24", 24, True),
    ("src/unit.py:20-24", 25, False),
    ("src/unit.py:20:99", 20, True),
    ("unrelated.py:20-24", 20, False),
    ({"summary": "Source span src/unit.py:20-24", "locations": []}, 20, True),
    ({"source_range": "src/unit.py:20-24"}, 20, True),
    ({"file": "src/unit.py", "line_range": "20-24"}, 20, True),
    ({"locations": [{"file": "src/unit.py", "line_range": [20, 24], "column": 99}]}, 20, True),
    ({"source_range": {"file": "src/unit.py", "start_line": 20, "end_line": 24, "start_column": 99}}, 20, True),
    ({"source_range": {"file": "unrelated.py", "start_line": 20, "end_line": 24}}, 20, False),
    ({"locations": [{"file": "src/unit.py", "start_line": 21, "end_line": 24}]}, 20, False),
    ({"locations": [{"file": "unrelated.py", "line": 20}, {"file": "src/unit.py", "line": 20}]}, 20, True),
])
def test_parse_error_matches_only_target_file_inclusive_line_spans(tmp_path, evidence, line, confirmed):
    path = tmp_path / "src" / "unit.py"
    path.parent.mkdir()
    source = "\n" * (line - 1) + "value = )\n"
    path.write_text(source, encoding="utf-8")
    hypothesis = BugHypothesis("src/unit.py", "module statement", "Malformed statement", evidence,
                               .95, "Parse source", category="other")
    assert _syntax_evidence_matches(hypothesis, line, 9) is confirmed
    finding = RegistryVerifier().verify(ProjectInfo(tmp_path, ("python",)), hypothesis)
    assert finding.verification_state == "UNVERIFIABLE", finding.evidence
    assert "SyntaxError" in finding.evidence
    assert path.read_text(encoding="utf-8") == source


def runtime(tmp_path, source, expected=5):
    path = tmp_path / "unit.py"
    path.write_text(source, encoding="utf-8")
    hypothesis = BugHypothesis("unit.py", "inspect_value", "Return violates declared expectation", "Source contract",
                               .95, "Bounded call", verification_spec={"kind": "equals", "expected": expected})
    finding = StructuredVerifier().verify(ProjectInfo(tmp_path, ("python",)), hypothesis)
    assert path.read_text(encoding="utf-8") == source
    return finding


@pytest.mark.parametrize("field", ["value", "metric", "field", "_local"])
def test_safe_verifier_owned_record_attribute_read(tmp_path, field):
    source = f"from types import SimpleNamespace\ndef inspect_value():\n    result = SimpleNamespace({field}=5)\n    return result.{field}\n"
    finding = runtime(tmp_path, source)
    assert finding.status == "rejected", finding.evidence
    assert '"result": 5' in finding.evidence


@pytest.mark.parametrize("field", ["__class__", "__dict__", "__mro__", "__getattribute__"])
def test_record_reflection_remains_unverifiable(tmp_path, field):
    source = f"from types import SimpleNamespace\ndef inspect_value():\n    result = SimpleNamespace(value=5)\n    return result.{field}\n"
    finding = runtime(tmp_path, source)
    assert finding.verification_state == "UNVERIFIABLE"
    assert "dunder/reflection attribute is blocked" in finding.evidence


def test_read_only_record_mutation_is_blocked(tmp_path):
    source = "from types import SimpleNamespace\ndef inspect_value():\n    result = SimpleNamespace(value=5)\n    result.value = 7\n    return result.value\n"
    finding = runtime(tmp_path, source)
    assert finding.verification_state == "UNVERIFIABLE"
    assert "read-only records" in finding.evidence


def test_structured_plan_reads_verifier_owned_call_result(tmp_path):
    (tmp_path / "unit.py").write_text("from types import SimpleNamespace\ndef make_result():\n    return SimpleNamespace(value=5)\n", encoding="utf-8")
    spec = {"kind": "verification_plan", "timeout_ms": 500,
            "steps": [{"op": "call", "target": "make_result", "args": [], "as": "result"},
                      {"op": "observe", "target": "result.value", "as": "value"}],
            "assertions": [{"source": "value", "op": "eq", "expected": 5}]}
    hypothesis = BugHypothesis("unit.py", "make_result", "Concrete expected state", "Contract", .95,
                               "Bounded structured plan", verification_spec=spec)
    finding = StructuredVerifier().verify(ProjectInfo(tmp_path, ("python",)), hypothesis)
    assert finding.status == "rejected", finding.evidence


def test_interpreted_local_object_data_field_read(tmp_path):
    source = "class Result:\n    def __init__(self, value):\n        self.value = value\ndef inspect_value():\n    result = Result(5)\n    return result.value\n"
    finding = runtime(tmp_path, source)
    assert finding.status == "rejected", finding.evidence


def test_safe_statistical_result_fields_and_minimal_pure_bindings(tmp_path):
    source = ("import os\nfrom math import sqrt as root\nfrom statistics import linear_regression as fit\n"
              "SCALE = root(16)\nRESULT = fit([1, 2, 3], [3, 5, 7])\n"
              "raise RuntimeError('unrelated initialization must never execute')\n"
              "def inspect_value():\n    return RESULT.slope + RESULT.intercept + SCALE\n")
    finding = runtime(tmp_path, source, expected=7)
    assert finding.status == "rejected", finding.evidence
    assert '"result": 7.0' in finding.evidence
    assert '"stdout": ""' in finding.evidence
    # Incorrect expected behavior is reproduced independently against the same source.
    assert runtime(tmp_path, source, expected=8).status == "confirmed"


def test_unallowlisted_dependency_stays_unverifiable_with_precise_reason(tmp_path):
    source = "from unavailable_library import compute\ndef inspect_value():\n    return compute([1, 2])\n"
    finding = runtime(tmp_path, source)
    assert finding.verification_state == "UNVERIFIABLE"
    assert "required import is not allowlisted: unavailable_library.compute" in finding.evidence


def test_project_initializer_call_is_not_executed(tmp_path):
    source = "def build():\n    print('side effect')\n    return 5\nVALUE = build()\ndef inspect_value():\n    return VALUE\n"
    finding = runtime(tmp_path, source)
    assert finding.verification_state == "UNVERIFIABLE"
    assert "not an allowlisted pure dependency" in finding.evidence


def test_native_descriptor_access_is_blocked_precisely(tmp_path):
    finding = runtime(tmp_path, "def inspect_value():\n    return (5).real\n")
    assert finding.verification_state == "UNVERIFIABLE"
    assert "native descriptors and reflection are blocked" in finding.evidence
