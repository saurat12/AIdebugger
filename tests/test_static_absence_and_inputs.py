"""Generic deterministic evidence mechanisms in synthetic projects."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug.hunt import BugHypothesis
from aidebug.hunt_registry import RegistryVerifier, collect_findings
from aidebug.models import ProjectInfo
from aidebug.verification import resolve_verification_spec


def finding(tmp_path, source, reproduction, symbol="target"):
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    project = ProjectInfo(tmp_path, ("python",))
    hypothesis = BugHypothesis("sample.py", symbol, "The declared local contract is broken",
                               "Explicit structured evidence", .95, reproduction, reproduction=reproduction)
    return project, hypothesis


def test_missing_local_module_is_confirmed_without_runtime(tmp_path, monkeypatch):
    project, item = finding(tmp_path, "from .missing import target\n", {"kind": "static_absence", "subject": "module",
                                                                     "target": ".missing", "scope": "."})
    runtime = Mock(side_effect=AssertionError("runtime must not run"))
    monkeypatch.setattr("aidebug.verification.observe", runtime)
    result = RegistryVerifier().verify(project, item)
    assert result.status == "confirmed"
    assert "missing.py" in result.evidence and "missing\\__init__.py" not in result.evidence
    assert result.verification_plan["capability"] == "static_absence"
    runtime.assert_not_called()


def test_symbol_absence_searches_whole_scope_and_rejects_present_symbol(tmp_path):
    project, item = finding(tmp_path, "def caller():\n    return absent()\n", {"kind": "static_absence",
                                                                           "subject": "symbol", "target": "absent", "scope": "."})
    (tmp_path / "other.py").write_text("def unrelated():\n    pass\n", encoding="utf-8")
    result = RegistryVerifier().verify(project, item)
    assert result.status == "confirmed"
    assert "sample.py" in result.evidence and "other.py" in result.evidence
    (tmp_path / "other.py").write_text("def absent():\n    pass\n", encoding="utf-8")
    assert RegistryVerifier().verify(project, item).status == "rejected"


def test_incomplete_search_and_nonlocal_import_are_unverifiable(tmp_path):
    project, item = finding(tmp_path, "def caller():\n    return missing()\n", {"kind": "static_absence",
                                                                            "subject": "symbol", "target": "missing"})
    (tmp_path / "large.py").write_text("#" * 120_001, encoding="utf-8")
    result = RegistryVerifier().verify(project, item)
    assert result.verification_state == "UNVERIFIABLE"
    assert "static absence search incomplete" in result.evidence
    nonlocal_item = replace(item, reproduction={"kind": "static_absence", "subject": "import", "target": "unknown_package"})
    result = RegistryVerifier().verify(project, nonlocal_item)
    assert result.verification_state == "UNVERIFIABLE"
    assert "installed dependency" in result.evidence


def test_unresolved_local_import_requires_a_matching_source_reference(tmp_path):
    project, item = finding(tmp_path, "from .missing import target\n", {"kind": "static_absence",
                                                                      "subject": "import", "target": ".missing"})
    assert RegistryVerifier().verify(project, item).status == "confirmed"
    (tmp_path / "sample.py").write_text("pass\n", encoding="utf-8")
    result = RegistryVerifier().verify(project, item)
    assert result.verification_state == "UNVERIFIABLE"
    assert "matching local source reference" in result.evidence


def test_relative_import_resolution_uses_referencing_package(tmp_path):
    (tmp_path / "package").mkdir()
    (tmp_path / "package" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "package" / "consumer.py").write_text("from .missing import value\n", encoding="utf-8")
    project = ProjectInfo(tmp_path, ("python",))
    spec = {"kind": "static_absence", "subject": "import", "target": ".missing"}
    item = BugHypothesis("package/consumer.py", "module", "Local import fails", "Explicit import", .95, spec,
                         reproduction=spec)
    result = RegistryVerifier().verify(project, item)
    assert result.status == "confirmed"
    assert "package\\missing.py" in result.evidence or "package/missing.py" in result.evidence
    (tmp_path / "package" / "missing.py").write_text("value = 1\n", encoding="utf-8")
    assert RegistryVerifier().verify(project, item).status == "rejected"


def test_safe_type_builtin_is_registered_and_reflection_is_blocked(tmp_path):
    project, item = finding(tmp_path, "def target(value):\n    return type(value) is list\n",
                            {"args": [[1]], "expected": False})
    result = RegistryVerifier().verify(project, item)
    assert result.status == "confirmed", result.evidence
    source = "def target(value):\n    return type(value).__mro__\n"
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    result = RegistryVerifier().verify(project, item)
    assert result.verification_state == "UNVERIFIABLE"
    assert "dunder/reflection" in result.evidence


def test_unsafe_builtin_is_not_exposed(tmp_path):
    project, item = finding(tmp_path, "def target():\n    return eval('1 + 1')\n", {"expected": 2})
    result = RegistryVerifier().verify(project, item)
    assert result.verification_state == "UNVERIFIABLE"
    assert "unsupported safe builtin: eval" in result.evidence


def test_isinstance_uses_only_registered_type_bindings(tmp_path):
    project, item = finding(tmp_path, "def target(value):\n    return isinstance(value, list)\n",
                            {"args": [[1]], "expected": False})
    assert RegistryVerifier().verify(project, item).status == "confirmed"


def test_registered_structured_adapter_builds_a_bounded_plan(tmp_path):
    project, item = finding(tmp_path, "def target(value):\n    return value.metric\n", {
        "structured_inputs": [{"type": "record", "value": {"metric": 4}, "as": "input"}],
        "expected": 5,
    })
    plan = resolve_verification_spec(project, item)["plan"]
    assert plan["steps"][0] == {"op": "construct_value", "constructor": "record", "value": {"metric": 4}, "as": "input"}
    assert plan["steps"][1]["args"] == [{"$ref": "input"}]
    assert RegistryVerifier().verify(project, item).status == "confirmed"


def test_unknown_adapter_and_insufficient_structured_metadata_are_precise(tmp_path):
    project, item = finding(tmp_path, "def target(value):\n    return value\n", {
        "structured_inputs": [{"type": "unregistered", "value": {"metric": 4}, "as": "input"}], "expected": 5})
    result = RegistryVerifier().verify(project, item)
    assert result.verification_state == "UNVERIFIABLE"
    assert "constructor adapter unavailable" in result.evidence
    item = replace(item, reproduction={"structured_inputs": [{"type": "record", "value": {"metric": 4}, "as": "input"}]})
    result = RegistryVerifier().verify(project, item)
    assert "structured input metadata insufficient" in result.evidence


def test_collection_route_uses_no_llm_planner(tmp_path, monkeypatch):
    project, item = finding(tmp_path, "def target():\n    return 1\n", {"kind": "static_absence", "subject": "file",
                                                                  "target": "missing.txt"})
    monkeypatch.setattr("aidebug.openai_agent.OpenAIAgent", Mock(side_effect=AssertionError("LLM must not run")))
    detector = SimpleNamespace(hunt=lambda project: (item,))
    result, = collect_findings(project, (), (detector,), RegistryVerifier())
    assert result.status == "confirmed"
    assert result.verification_plan["capability"] == "static_absence"
