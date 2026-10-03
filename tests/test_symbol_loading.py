import pytest

from aidebug.hunt import BugHypothesis
from aidebug.hunt_strategies import HuntVerifier
from aidebug.models import ProjectInfo


SOURCE = '''import os
import subprocess
import random as rng
from statistics import mean
from math import sqrt as root

UNRELATED = os.environ["API_SECRET"]
os.remove("sentinel.txt")
subprocess.run(["pip", "install", "unsafe-package"])

OFFSET = 0
STEP = OFFSET + 1

def helper(values):
    return mean(values)

def calculate_average(values):
    return helper(values)

def find_max(values):
    largest = OFFSET
    for value in values:
        if value > largest:
            largest = value
    return largest

def get_random_item(values):
    return values[rng.randint(0, len(values))]

def process_data(values):
    for value in values:
        if value == "remove":
            values.remove(value)

def compare_values(a, b):
    return a is b

def wait_forever():
    while True:
        pass

def calculate_root(x=STEP):
    return root(x)

class Counter:
    items = []
    def add(self, value):
        self.items.append(value)

@unavailable_decorator
def unrelated_function():
    return UNRELATED

if __name__ == "__main__":
    wait_forever()
'''


def run(tmp_path, symbol, spec, source=SOURCE):
    directory = tmp_path / "src"
    directory.mkdir(exist_ok=True)
    path = directory / "main.py"
    path.write_text(source)
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("unchanged")
    item = BugHypothesis("src/main.py", symbol, "Source behavior hypothesis", "Inspected source", 0.9, "Structured reproduction", verification_spec=spec)
    result = HuntVerifier().verify(ProjectInfo(tmp_path, ("python",)), item)
    assert sentinel.read_text() == "unchanged"
    assert path.read_text() == source
    return result


@pytest.mark.parametrize("symbol,spec,status,evidence", [
    ("calculate_average", {"kind": "equals", "args": [[1, 2, 3]], "expected": 2}, "rejected", '"result": 2'),
    ("find_max", {"kind": "equals", "args": [[-5, -2, -10]], "expected": -2}, "confirmed", '"result": 0'),
    ("get_random_item", {"kind": "deterministic_random", "args": [["only"]], "random_values": {"randint": [1]}, "expected_exception": "IndexError"}, "confirmed", '"type": "IndexError"'),
    ("process_data", {"kind": "mutation_check", "args": [["remove", "remove"]], "expected_args": [[]]}, "confirmed", '"args_after": [["remove"]]'),
    ("compare_values", {"kind": "equals", "args": [[1], [1]], "expected": True}, "confirmed", '"result": false'),
    ("wait_forever", {"kind": "timeout", "timeout_ms": 100}, "high_confidence", "execution limit exceeded"),
    ("calculate_root", {"kind": "equals", "expected": 1}, "rejected", '"result": 1.0'),
    ("Counter", {"kind": "class_state_check", "constructors": [[], []], "calls": [{"instance": 0, "method": "add", "args": [1]}], "observe": {"instance": 1, "attribute": "items"}, "expected": []}, "confirmed", '"result": [1]'),
])
def test_nested_source_symbol_loaded_without_module_side_effects(tmp_path, symbol, spec, status, evidence):
    finding = run(tmp_path, symbol, spec)
    assert finding.status == status, finding.evidence
    assert evidence in finding.evidence


@pytest.mark.parametrize("source,diagnostic", [
    ("import os\ndef f():\n    return os.environ['API_SECRET']\n", "import is not allowlisted"),
    ("import socket\ndef f():\n    return socket.create_connection(('example.com', 80))\n", "import is not allowlisted"),
    ("import subprocess\ndef f():\n    return subprocess.run(['pip', 'install', 'package'])\n", "import is not allowlisted"),
    ("import os\nVALUE = os.getenv('API_SECRET')\ndef f():\n    return VALUE\n", "required import is not allowlisted: os"),
    ("VALUE = []\nVALUE.append(1)\ndef f():\n    return VALUE\n", "side-effectful initialization"),
    ("VALUE = OTHER\nOTHER = VALUE\ndef f():\n    return VALUE\n", "cyclic initialization"),
])
def test_required_unsafe_dependencies_stay_unconfirmed(tmp_path, source, diagnostic):
    result = run(tmp_path, "f", {"kind": "equals", "expected": 1}, source)
    assert result.status == "high_confidence", result.evidence
    assert diagnostic in result.evidence
    assert result.check is None


def test_imported_math_module_uses_allowlisted_adapter(tmp_path):
    result = run(tmp_path, "f", {"kind": "equals", "args": [4], "expected": 2}, "import math as m\ndef f(x):\n    return m.sqrt(x)\n")
    assert result.status == "rejected"


def test_missing_symbol_is_not_reproduced_exception(tmp_path):
    result = run(tmp_path, "missing", {"kind": "expected_exception", "expected_exception": "KeyError"})
    assert result.status == "high_confidence"
    assert "not defined locally" in result.evidence


def test_library_adapter_cannot_be_used_as_a_dictionary(tmp_path):
    result = run(tmp_path, "f", {"kind": "equals", "expected": 2},
                 "import math\ndef f():\n    return math['sqrt'](4)\n")
    assert result.status == "high_confidence"
    assert "module subscripting" in result.evidence
