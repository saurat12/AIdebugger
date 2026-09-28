import difflib
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug.hunt import BugHunter, BugHypothesis, hunt_project
from aidebug.hunt_strategies import HuntVerifier
from aidebug.models import AnalysisReport, CheckResult, PatchProposal, ProjectInfo
from aidebug.verification import StructuredVerifier, validate_spec


def verify(tmp_path, source, spec, symbol="f"):
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    item = BugHypothesis("app.py", symbol, "Claimed behavior is incorrect", "Source evidence", 0.9,
                         "Reproduce using structured input", verification_spec=spec)
    result = HuntVerifier().verify(ProjectInfo(tmp_path, ("python",)), item)
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == source
    return result


@pytest.mark.parametrize("source,spec,observation", [
    ("def f(xs):\n    return xs[len(xs)]\n", {"kind": "expected_exception", "args": [[1, 2]], "expected_exception": "IndexError"}, '"type": "IndexError"'),
    ("def f(xs):\n    best = 0\n    for x in xs:\n        if x > best:\n            best = x\n    return best\n", {"kind": "equals", "args": [[-5, -2, -9]], "expected": -2}, '"result": 0'),
    ("def f(xs):\n    return sum(xs) / len(xs)\n", {"kind": "expected_exception", "args": [[]], "expected_exception": "ZeroDivisionError"}, '"type": "ZeroDivisionError"'),
    ("import random\ndef f(xs):\n    return xs[random.randint(0, len(xs))]\n", {"kind": "deterministic_random", "args": [[10, 20]], "random_values": {"randint": [2]}, "expected_exception": "IndexError"}, '"value": 2'),
    ("def f(xs):\n    for x in xs:\n        if x < 0:\n            xs.remove(x)\n", {"kind": "mutation_check", "args": [[-1, -2, -3]], "expected_args": [[]]}, '"args_after": [[-2]]'),
    ("def f(a, b):\n    return a is b\n", {"kind": "function_call", "args": [[1], [1]], "expected": True}, '"result": false'),
    ("def f(x):\n    return -x\n", {"kind": "predicate", "args": [2], "predicate": {"op": "ge", "value": 0}}, '"result": -2'),
    ("def f(x):\n    return -x\n", {"kind": "invariant", "args": [2], "predicate": {"op": "ge", "value": 0}}, '"result": -2'),
])
def test_independently_reproduced_bugs(tmp_path, source, spec, observation):
    finding = verify(tmp_path, source, spec)
    assert finding.status == "confirmed", finding.evidence
    assert observation in finding.evidence
    assert finding.check and not finding.check.passed


def test_class_versus_instance_state(tmp_path):
    source = "class Bag:\n    items = []\n    def add(self, value):\n        self.items.append(value)\n"
    spec = {"kind": "class_state_check", "constructors": [[], []], "calls": [{"instance": 0, "method": "add", "args": [7]}],
            "observe": {"instance": 1, "attribute": "items"}, "expected": []}
    finding = verify(tmp_path, source, spec, "Bag")
    assert finding.status == "confirmed", finding.evidence
    assert '"result": [7]' in finding.evidence
    fixed = "class Bag:\n    def __init__(self):\n        self.items = []\n    def add(self, value):\n        self.items.append(value)\n"
    assert verify(tmp_path, fixed, spec, "Bag").status == "rejected"


def test_infinite_loop_has_enforced_subprocess_timeout(tmp_path):
    finding = verify(tmp_path, "def f():\n    while True:\n        pass\n", {"kind": "timeout", "timeout_ms": 100})
    assert finding.status == "confirmed", finding.evidence
    assert "after worker readiness" in finding.evidence
    assert "not proof of infinite execution" in finding.evidence
    assert verify(tmp_path, "def f():\n    return 1\n", {"kind": "timeout", "timeout_ms": 100}).status == "rejected"


def test_resource_leak_uses_virtual_files_not_real_files(tmp_path):
    actual = tmp_path / "data.txt"
    actual.write_text("real data must remain unchanged")
    source = "def f(name):\n    handle = open(name)\n    return handle.read()\n"
    spec = {"kind": "file_resource_check", "args": ["data.txt"], "files": {"data.txt": "virtual data"}, "expected_open_resources": 0}
    finding = verify(tmp_path, source, spec)
    assert finding.status == "confirmed", finding.evidence
    assert '"open_resources": 1' in finding.evidence
    assert '"result": "virtual data"' in finding.evidence
    fixed = "def f(name):\n    with open(name) as handle:\n        return handle.read()\n"
    assert verify(tmp_path, fixed, spec).status == "rejected"
    assert actual.read_text() == "real data must remain unchanged"


def test_virtual_file_read_position_and_finally_cleanup(tmp_path):
    source = "def f(name):\n    handle = open(name)\n    try:\n        handle.read()\n        return handle.read()\n    finally:\n        handle.close()\n"
    result = verify(tmp_path, source, {"kind": "file_resource_check", "args": ["input.txt"], "files": {"input.txt": "text"}, "expected_open_resources": 0})
    assert result.status == "rejected"
    assert '"result": ""' in result.evidence


def test_random_runs_are_deterministic(tmp_path):
    source = "import random\ndef f():\n    return random.randint(0, 10)\n"
    spec = {"kind": "deterministic_random", "seed": 7, "expected": 5}
    first = verify(tmp_path, source, spec)
    second = verify(tmp_path, source, spec)
    assert first.status == "rejected"
    assert first.evidence == second.evidence


def test_model_input_is_data_not_executable_code(tmp_path):
    source = "def f(text):\n    return text\n"
    text = "__import__('os').remove('app.py')"
    result = verify(tmp_path, source, {"kind": "equals", "args": [text], "expected": text})
    assert result.status == "rejected"


def test_false_positive_is_rejected_with_observed_value(tmp_path):
    result = verify(tmp_path, "def f(xs):\n    return max(xs)\n", {"kind": "equals", "args": [[-5, -2]], "expected": -2})
    assert result.status == "rejected"
    assert '"result": -2' in result.evidence


def test_random_stub_cannot_manufacture_invalid_api_values(tmp_path):
    result = verify(tmp_path, "from random import randrange\ndef f(xs):\n    return xs[randrange(len(xs))]\n",
                    {"kind": "deterministic_random", "args": [[1, 2]], "random_values": {"randrange": [2]}, "expected_exception": "IndexError"})
    assert result.status == "high_confidence"
    assert result.check is None


@pytest.mark.parametrize("source", [
    "import os\ndef f():\n    os.remove('app.py')\n",
    "def f():\n    return eval('1 + 1')\n",
    "def f():\n    return ().__class__.__bases__\n",
    "def f():\n    return [1] * 1000000000\n",
    "def f():\n    return '%1000000000s' % 'x'\n",
    "class Custom:\n    def __len__(self):\n        return 1\ndef f():\n    return len(Custom())\n",
    "def f():\n    return open('input.txt', encoding='utf-8').read()\n",
])
def test_unsupported_operations_never_confirm(tmp_path, source):
    result = verify(tmp_path, source, {"kind": "equals", "expected": 1})
    assert result.status == "high_confidence", result.evidence
    assert result.check is None


@pytest.mark.parametrize("spec", [
    {"kind": "function_call", "code": "print('unsafe')", "expected": 1},
    {"kind": "predicate", "predicate": "result > 0"},
    {"kind": "timeout", "timeout_ms": 100000},
    {"kind": "equals", "expected": 1, "args": [[1] * 1001]},
    {"kind": "file_resource_check", "files": {"../outside": "x"}, "expected_open_resources": 0},
])
def test_malformed_specs_are_rejected(spec):
    with pytest.raises(ValueError):
        validate_spec(spec)


def test_source_path_cannot_escape_workspace(tmp_path):
    hypothesis = BugHypothesis("../outside.py", "f", "bug", "evidence", 0.9, "call", verification_spec={"kind": "equals", "expected": 1})
    assert StructuredVerifier().verify(ProjectInfo(tmp_path, ("python",)), hypothesis).status == "high_confidence"


def test_hunter_preserves_structured_spec(tmp_path):
    (tmp_path / "app.py").write_text("def f():\n    return 0\n")
    item = BugHypothesis("app.py", "f", "wrong result", "source", 0.9, "call", verification_spec={"kind": "equals", "expected": 1})
    agent = SimpleNamespace(_complete=Mock(return_value=json.dumps({"hypotheses": [asdict(item)]})))
    assert BugHunter(agent).hunt(ProjectInfo(tmp_path, ("python",))) == (item,)


def test_structured_confirmation_reaches_repair_and_reverification(tmp_path, monkeypatch):
    source = "def f(xs):\n    return xs[len(xs)]\n"
    fixed = source.replace("xs[len(xs)]", "xs[len(xs) - 1]")
    (tmp_path / "app.py").write_text(source)
    item = BugHypothesis("app.py", "f", "off by one", "source", 0.9, "call",
                         verification_spec={"kind": "expected_exception", "args": [[1, 2]], "expected_exception": "IndexError", "expected": 2})
    patch = "".join(difflib.unified_diff(source.splitlines(True), fixed.splitlines(True), "a/app.py", "b/app.py"))
    agent = SimpleNamespace(analyze=Mock(return_value=AnalysisReport("Off by one", 0.9, "Index equals length")),
                            propose=Mock(return_value=PatchProposal(patch, "Use final valid index")))
    monkeypatch.setattr("aidebug.hunt.run_checks", Mock(return_value=(CheckResult("existing", ("pytest",), 0, "", "", 0),)))
    result = hunt_project(ProjectInfo(tmp_path, ("python",)), (SimpleNamespace(hunt=lambda project: (item,)),), HuntVerifier(), agent)
    assert result.findings[0].status == "confirmed"
    assert result.repairs[0].validation.passed
    assert result.repairs[0].validated_patch_path.is_file()
    assert (tmp_path / "app.py").read_text() == source
