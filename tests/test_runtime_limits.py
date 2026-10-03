"""Synthetic regressions for bounded, fail-closed verification fragments."""

import io
import subprocess
from types import SimpleNamespace, ModuleType
from unittest.mock import Mock

import pytest

from aidebug import _verification_worker as worker
from aidebug.hunt import BugHypothesis, hunt_project
from aidebug.hunt_registry import RegistryVerifier
from aidebug.models import ProjectInfo
from aidebug.verification import observe


def verify(tmp_path, source, expected=6, **spec):
    path = tmp_path / "fragment.py"
    path.write_text(source, encoding="utf-8")
    hypothesis = BugHypothesis("fragment.py", "target", "Result violates the specified contract",
                               "Declared expected result", .95, "Bounded structured call",
                               verification_spec={"kind": "equals", "expected": expected, **spec})
    finding = RegistryVerifier().verify(ProjectInfo(tmp_path, ("python",)), hypothesis)
    assert path.read_text(encoding="utf-8") == source
    return finding


@pytest.mark.parametrize("source,expected", [
    ("def target():\n    total = 0\n    for index, value in enumerate([1, 2, 3], start=0):\n        total += value\n    return total\n", 6),
    ("def target():\n    n = 0\n    while n < 6:\n        n += 1\n    return n\n", 6),
    ("def target():\n    return sum([x for x in range(4)])\n", 6),
    ("def target():\n    return sum({x for x in range(4)})\n", 6),
    ("def target():\n    return {x: x + 1 for x in range(3)}[2]\n", 3),
    ("def target():\n    return sum(x for x in range(4))\n", 6),
    ("def target():\n    return any(x > 0 or 1 / 0 for x in [1, 0])\n", True),
    ("def target():\n    return all(x > 0 and 1 / x > 0 for x in [0, 1])\n", False),
    ("def target():\n    offset = 1\n    values = (x + offset for x in [1, 2])\n    offset = 2\n    return sum(values)\n", 7),
    ("def target():\n    values = (x for x in [1, 2])\n    return sum(values) + sum(values)\n", 3),
    ("def target():\n    return sum([a + b for a in range(2) for b in range(2)])\n", 4),
    ("def target():\n    n: int = 3\n    return n * 2 if n > 0 and n != 4 else 0\n", 6),
    ("def target():\n    return [1, 2, 3, 4][1:3]\n", [2, 3]),
    ("def target():\n    return sum([1, 2], start=3)\n", 6),
    ("def target():\n    return dict(metric=6)['metric']\n", 6),
    ("def target():\n    return 2 ** 3\n", 8),
    ("MASK = (1 << 3) | 2\ndef target():\n    return (MASK & 7) ^ 3\n", 1),
    ("def target():\n    return ~1\n", -2),
    ("import math\nROOT = math.sqrt\ndef target():\n    return ROOT(36)\n", 6),
    ("def _local(value):\n    return value * 2\nALIAS = _local\ndef target():\n    return ALIAS(3)\n", 6),
    ("def _key(value):\n    return sum(list(value))\ndef target():\n    return max([[1, 2], [6]], key=_key)\n", [6]),
    ("def target():\n    return round(number=1.234, ndigits=2)\n", 1.23),
    ("def target():\n    return sorted([1, 3, 2], reverse=True)\n", [3, 2, 1]),
    ("def target():\n    return list(zip([1, 2], [3, 4], strict=True))\n", [[1, 3], [2, 4]]),
    ("def _helper(value, *, scale=2):\n    return value * scale\ndef target():\n    return _helper(3, scale=2)\n", 6),
    ("def _rank(value):\n    return -value\ndef target():\n    return max([1, 6, 3], key=_rank)\n", 1),
    ("def _rank(value):\n    return -value\ndef target():\n    return sorted([1, 6, 3], key=_rank)\n", [6, 3, 1]),
    ("def target():\n    return min([-6, 2, -3], key=abs)\n", 2),
    ("def target():\n    return max([], default=6)\n", 6),
    ("def target():\n    return all([True, True]) and any([False, True])\n", True),
    ("class Data:\n    def __init__(self):\n        self._metric = 6\ndef target():\n    result = Data()\n    return result._metric\n", 6),
    ("from types import SimpleNamespace\ndef target():\n    return SimpleNamespace(metric=6).metric\n", 6),
    ("from math import sqrt as root\nBASE = root(9)\nimport os\nraise RuntimeError('unused initialization')\ndef target():\n    return BASE * 2\n", 6),
])
def test_safe_generic_constructs_execute(tmp_path, source, expected):
    finding = verify(tmp_path, source, expected)
    assert finding.status == "rejected", finding.evidence
    assert "Restricted execution observed" in finding.evidence


@pytest.mark.parametrize("source,reason", [
    ("def target():\n    n = 0\n    while n < 1001:\n        n += 1\n    return n\n", "iteration budget"),
    ("def target():\n    return [x + y for x in range(40) for y in range(40)]\n", "iteration budget"),
    ("def target():\n    return list(range(1001))\n", "collection limit"),
    ("def target():\n    return 2 ** 1000\n", "numeric power limit"),
    ("def target():\n    return 2 ** 10000000\n", "bounded numeric base and integer exponent"),
    ("def target():\n    return 1 << 100000000\n", "integer shift size limit"),
    ("def target():\n    return [1] * 1001\n", "sequence multiplication limit"),
    ("def target():\n    return f'{1:100000000}'\n", "formatted value spec limit"),
    ("def target():\n    return 'x' * 10001\n", "sequence multiplication limit"),
    ("def target():\n    for x in range(100):\n        print('a' * 100)\n", "captured stdout limit"),
    ("import sys\ndef target():\n    for x in range(100):\n        print('a' * 100, file=sys.stderr)\n", "captured stdout limit"),
    ("def target():\n    return target()\n", "recursion depth"),
    ("def target():\n    return (1).__class__.__mro__\n", "dunder/reflection"),
    ("def target():\n    return getattr(1, 'real')\n", "unsupported safe builtin: getattr"),
    ("import socket\ndef target():\n    return socket.socket()\n", "required import is not allowlisted"),
    ("import subprocess\ndef target():\n    return subprocess.run(['anything'])\n", "required import is not allowlisted"),
    ("import os\ndef target():\n    return os.remove('fragment.py')\n", "required import is not allowlisted"),
    ("def target():\n    return open('fragment.py').read()\n", "resource not explicitly supplied"),
    ("def target():\n    return eval('1 + 2')\n", "unsupported safe builtin: eval"),
    ("def target():\n    return __import__('os')\n", "unsupported safe builtin: __import__"),
    ("def _key(value):\n    print(value)\n    return value\ndef target():\n    return max([1, 2], key=_key)\n", "key callback cannot perform I/O"),
    ("def _key(value):\n    handle = open('virtual.txt')\n    return value\ndef target():\n    return max([1, 2], key=_key)\n", "key callback cannot perform I/O"),
    ("DATA = []\ndef _key(value):\n    DATA.append(value)\n    return value\ndef target():\n    return max([1, 2], key=_key)\n", "key callback cannot perform I/O"),
    ("DATA = []\ndef _key(value):\n    items = DATA\n    items += [value]\n    return value\ndef target():\n    return max([1, 2], key=_key)\n", "key callback cannot mutate"),
    ("def _key(value):\n    value[0] = 3\n    return 0\ndef target():\n    return max([[1], [2]], key=_key)\n", "key callback cannot mutate"),
    ("from absent_dependency import operation\ndef target():\n    return operation()\n", "required import is not allowlisted"),
    ("REQUIRED = missing_binding\ndef target():\n    return REQUIRED\n", "unresolved local dependency: missing_binding"),
])
def test_limits_and_unsafe_capabilities_fail_closed(tmp_path, source, reason):
    finding = verify(tmp_path, source)
    assert finding.verification_state == "UNVERIFIABLE", finding.evidence
    assert "UNVERIFIABLE:" in finding.evidence
    assert reason in finding.evidence
    assert finding.check is None


def test_dependency_resolver_classifies_only_minimal_bindings():
    engine = worker.Engine("from math import sqrt\nVALUE = sqrt(9)\nLITERAL = 2\n"
                           "import absent_dependency\ndef _helper():\n    return VALUE + LITERAL\n", {})
    assert engine.invoke(engine.resolve("_helper"), []) == 5
    assert engine.dependency_classifications == {"_helper": "LOCAL_SAFE", "VALUE": "SAFE_DERIVED_BINDING",
                                               "sqrt": "SAFE_STDLIB", "LITERAL": "SAFE_LITERAL"}
    with pytest.raises(worker.Unsupported, match="required import"):
        engine.resolve("absent_dependency")
    assert engine.dependency_classifications["absent_dependency"] == "UNRESOLVED"
    assert "absent_dependency" not in worker.sys.modules


def test_installed_adapter_requires_explicit_registration_and_loaded_library():
    engine = worker.Engine("from statistics import mean\ndef target():\n    return mean([1, 3])\n", {})
    # Trusted adapters expose only a chosen binding; registration itself imports nothing.
    engine.dependencies.register_module("statistics", {"mean": worker.SafeCall(worker.statistics.mean, safe_tag="pure_dependency")},
                                        category="SAFE_INSTALLED_LIBRARY")
    assert engine.invoke(engine.resolve("target"), []) == 2
    assert engine.dependency_classifications["mean"] == "SAFE_INSTALLED_LIBRARY"
    with pytest.raises(worker.Unsupported, match="adapter is unavailable"):
        engine.dependencies.register_module("absent_dependency", {})


def test_minimal_binding_selects_only_safe_required_exports(monkeypatch):
    monkeypatch.setitem(worker.sys.modules, "synthetic_dependency", ModuleType("synthetic_dependency"))
    engine = worker.Engine("import synthetic_dependency\nraise RuntimeError('unrelated initialization')\n"
                           "def target():\n    return synthetic_dependency.transform(6)\n", {})
    unsafe = Mock()
    engine.dependencies.register_module("synthetic_dependency", {
        "transform": worker.SafeCall(worker.operator.neg, safe_tag="pure_dependency"),
        "escape": worker.SafeCall(unsafe)}, category="SAFE_INSTALLED_LIBRARY")
    assert engine.invoke(engine.resolve("target"), []) == -6
    binding = engine.globals["synthetic_dependency"]
    assert isinstance(binding, worker.ModuleBinding)
    assert set(binding.selected) == {"transform"}
    assert engine.dependency_classifications["synthetic_dependency.transform"] == "SAFE_INSTALLED_LIBRARY"
    with pytest.raises(worker.Unsupported, match="unsafe dependency binding"):
        engine.attribute(binding, "escape")
    assert engine.dependency_classifications["synthetic_dependency.escape"] == "UNSAFE"
    assert "escape" not in binding.selected
    unsafe.assert_not_called()
    with pytest.raises(worker.Unsupported, match="dunder/reflection"):
        engine.attribute(binding, "__dict__")


@pytest.mark.parametrize("reproduction", [
    {"args": [[1, 2, 3]], "expected": 6},
    {"kind": "equals", "args": [[1, 2, 3]], "expected": 6},
    {"verification_spec": {"kind": "equals", "args": [[1, 2, 3]], "expected": 6}},
])
def test_structured_reproduction_is_resolved_without_model_or_code_text(tmp_path, reproduction):
    from aidebug.hunt_registry import collect_findings
    (tmp_path / "fragment.py").write_text("def _target(values):\n    return sum(values) - 1\n")
    item = BugHypothesis("fragment.py", "_target", "Result differs from explicit example", "Expected six", .95,
                         reproduction)
    project = ProjectInfo(tmp_path, ("python",))
    findings = collect_findings(project, (), (SimpleNamespace(hunt=lambda p: (item,)),), RegistryVerifier())
    finding, = findings
    assert finding.status == "confirmed", finding.evidence
    assert finding.repair_authorized
    assert finding.hypothesis.verification_spec["expected"] == 6
    assert finding.verification_plan["plan"] == finding.hypothesis.verification_spec
    assert finding.verification_plan["source"] != "unresolved_without_safe_plan"


@pytest.mark.parametrize("strategy,reason", [
    ("__import__('os').remove('fragment.py')", "free-form reproduction text is never executed"),
    ({"args": [[1, 2]]}, "Ambiguous expected behavior"),
    ({"args": [[1, 2]], "expected": 3, "code": "arbitrary()"}, "unsupported operations"),
    ({"kind": "verification_plan", "steps": [{"op": "exec", "code": "arbitrary()"}], "assertions": []}, "verification"),
])
def test_unrepresentable_reproduction_remains_unverifiable(tmp_path, strategy, reason):
    (tmp_path / "fragment.py").write_text("def target(values):\n    return sum(values)\n")
    item = BugHypothesis("fragment.py", "target", "Possible contract violation", "Incomplete evidence", .95, strategy)
    finding = RegistryVerifier().verify(ProjectInfo(tmp_path, ("python",)), item)
    assert finding.verification_state == "UNVERIFIABLE", finding.evidence
    assert reason.casefold() in finding.evidence.casefold()
    assert (tmp_path / "fragment.py").is_file()


def test_steps_objects_and_recursion_are_bounded():
    engine = worker.Engine("def target():\n    return 1\n", {})
    engine.steps = worker.MAX_STEPS
    with pytest.raises(worker.ExecutionLimit, match="step budget"):
        engine.invoke(engine.resolve("target"), [])
    engine.steps = 0
    engine.instances_created = worker.MAX_OBJECTS
    with pytest.raises(worker.ExecutionLimit, match="object allocation budget"):
        engine.make_record(value=1)


def test_virtual_file_write_size_is_bounded(tmp_path):
    source = "def target():\n    handle = open('virtual.txt', 'a')\n    handle.write('y')\n    return 6\n"
    finding = verify(tmp_path, source, files={"virtual.txt": "x" * 10000})
    assert finding.verification_state == "UNVERIFIABLE", finding.evidence
    assert "string limit" in finding.evidence
    assert not (tmp_path / "virtual.txt").exists()


def test_required_unsafe_initialization_is_classified_and_not_run():
    engine = worker.Engine("VALUES = []\nVALUES.append(1)\ndef target():\n    return VALUES\n", {})
    with pytest.raises(worker.Unsupported, match="side-effectful initialization"):
        engine.invoke(engine.resolve("target"), [])
    assert engine.dependency_classifications["VALUES"] == "UNSAFE_SIDE_EFFECT"


def test_structured_plan_operation_count_stays_bounded(tmp_path):
    plan = {"kind": "verification_plan", "steps": [{"op": "call", "target": "target", "as": f"r{i}"} for i in range(41)],
            "assertions": [{"source": "r0", "op": "eq", "expected": 6}]}
    finding = verify(tmp_path, "def target():\n    return 6\n", **plan)
    assert finding.verification_state == "UNVERIFIABLE", finding.evidence
    assert finding.check is None


def test_wallclock_timeout_kills_and_reaps_worker(tmp_path, monkeypatch):
    process = Mock()
    process.stdout = io.StringIO("ready\n")
    process.stdin = io.StringIO()
    process.communicate.side_effect = subprocess.TimeoutExpired("restricted-worker", .1)
    process.poll.return_value = None
    monkeypatch.setattr("aidebug.verification.subprocess.Popen", Mock(return_value=process))
    result = observe("def target():\n    return 1\n", "target", {"kind": "equals", "timeout_ms": 100}, tmp_path)
    assert result == {"timed_out": True, "deadline_ms": 100}
    process.kill.assert_called()
    process.wait.assert_called()


def test_exhausted_runtime_does_not_crash_hunt_or_authorize_repair(tmp_path, monkeypatch):
    source = "def excessive():\n    while True:\n        pass\ndef bounded():\n    return 6\n"
    (tmp_path / "fragment.py").write_text(source, encoding="utf-8")
    def hypothesis(symbol):
        return BugHypothesis("fragment.py", symbol, "Return violates expected contract", "Source contract", .95,
                             "Bounded call", verification_spec={"kind": "equals", "expected": 6})
    detector = SimpleNamespace(hunt=lambda p: (hypothesis("excessive"), hypothesis("bounded")))
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *a, **k: ())
    agent = Mock()
    run = hunt_project(ProjectInfo(tmp_path, ("python",)), (detector,), RegistryVerifier(), agent)
    assert len(run.findings) == 2
    statuses = {finding.hypothesis.suspected_symbol: finding.verification_state for finding in run.findings}
    assert statuses == {"excessive": "UNVERIFIABLE", "bounded": "REJECTED"}
    assert run.report_path.is_file()
    assert "iteration budget" in run.report_path.read_text(encoding="utf-8")
    agent.propose.assert_not_called()
    assert (tmp_path / "fragment.py").read_text(encoding="utf-8") == source
