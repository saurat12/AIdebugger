"""Validate declarative verification data and interpret source in a subprocess."""

import json
import hashlib
import ast
import math
import operator
import os
import re
from pathlib import Path
import queue
import subprocess
import sys
import threading
from dataclasses import replace

from .hunt import Finding, _source_path
from .models import CheckResult
from .workspace import isolated_workspace
from .reproduction import ReproductionError, build_reproduction, validate_targets
from ._verification_worker import trusted_constructor_adapters


KINDS = {"function_call", "expected_exception", "equals", "predicate", "invariant", "mutation_check",
         "deterministic_random", "class_state_check", "timeout", "file_resource_check", "verification_plan", "module_fragment", "doctest", "python_syntax"}
EXCEPTIONS = {"IndexError", "ZeroDivisionError", "ValueError", "TypeError", "KeyError", "RuntimeError", "AssertionError", "NameError"}


class VerificationCapabilityRegistry:
    """Extensible registry of evidence mechanisms, keyed by safe plan kind."""

    def __init__(self):
        self._capabilities = {}

    def register(self, name, kinds):
        if not name or name in self._capabilities:
            raise ValueError("Duplicate verification capability: " + str(name))
        for kind in kinds:
            if kind in self._capabilities:
                raise ValueError("Verification kind already routed: " + kind)
            self._capabilities[kind] = name

    def resolve(self, spec):
        return self._capabilities.get(spec.get("kind")) if isinstance(spec, dict) else None

    @property
    def registrations(self):
        return dict(self._capabilities)


def default_capability_registry():
    registry = VerificationCapabilityRegistry()
    registry.register("python-parse", ("python_syntax",))
    registry.register("restricted-module-fragment", ("module_fragment",))
    registry.register("restricted-timeout-runtime", ("timeout",))
    registry.register("restricted-resource-state-runtime", ("file_resource_check", "class_state_check", "mutation_check"))
    registry.register("restricted-structured-runtime", KINDS - {"python_syntax", "module_fragment", "timeout",
                                                                 "file_resource_check", "class_state_check", "mutation_check", "doctest"})
    registry.register("explicit-doctest-adapter", ("doctest",))
    return registry


CAPABILITY_REGISTRY = default_capability_registry()


def validate_spec(spec):
    """No expressions, code strings, module names, or arbitrary calls in specs."""
    if not isinstance(spec, dict) or not isinstance(spec.get("kind"), str) or spec.get("kind") not in KINDS:
        raise ReproductionError("kind", "Invalid verification kind", "invalid value")
    allowed = {"kind", "verification_target", "args", "kwargs", "expected", "expected_exception", "predicate", "expected_args", "target_lines", "target_kind", "required_bindings", "stubs", "clock",
               "seed", "random_values", "random_boundaries", "postcondition", "constructors", "calls", "observe", "timeout_ms", "files", "expected_open_resources",
               "steps", "assertions", "repair_plan", "line", "column", "message_contains"}
    if set(spec) - allowed:
        raise ReproductionError("verification_spec", "Unknown verification fields")
    if "verification_target" in spec and (not isinstance(spec["verification_target"], str) or
                                           not spec["verification_target"].isidentifier() or
                                           spec["verification_target"].startswith("__")):
        raise ReproductionError("verification_target", "Verification target must be a local identifier without dunder traversal", "unsupported operation")
    if spec["kind"] in ("verification_plan", "module_fragment"):
        _validate_plan(spec)
        return spec
    if spec["kind"] == "doctest":
        if set(spec) != {"kind"}:
            raise ValueError("Explicit doctest adapter accepts only {kind: doctest}")
        return spec
    if spec["kind"] == "python_syntax":
        if set(spec) - {"kind", "line", "column", "message_contains"}:
            raise ValueError("Python syntax verification accepts only a source location and message fragment")
        if type(spec.get("line")) is not int or spec["line"] < 1:
            raise ValueError("Python syntax verification requires the claimed error line")
        if "column" in spec and (type(spec["column"]) is not int or spec["column"] < 1):
            raise ValueError("Python syntax verification column must be positive")
        if "message_contains" in spec and (not isinstance(spec["message_contains"], str) or not spec["message_contains"]):
            raise ValueError("Python syntax verification message fragment must be non-empty")
        return spec
    encoded = json.dumps(spec, allow_nan=False)
    if len(encoded) > 64000:
        raise ValueError("Verification input too large")

    def bounded(value, depth=0):
        if depth > 8:
            raise ValueError("Verification nesting limit")
        if isinstance(value, dict):
            if len(value) > 1000 or any(not isinstance(key, str) for key in value):
                raise ValueError("Invalid JSON object")
            for item in value.values():
                bounded(item, depth + 1)
        elif isinstance(value, list):
            if len(value) > 1000:
                raise ValueError("Collection limit")
            for item in value:
                bounded(item, depth + 1)
        elif type(value) in (int, float):
            if not math.isfinite(value) or abs(value) > 10**12:
                raise ValueError("Numeric limit")
        elif type(value) is str:
            if len(value) > 10000:
                raise ValueError("String limit")
        elif value is not None and type(value) is not bool:
            raise ValueError("Only JSON inputs are allowed")
    bounded(spec)
    if "postcondition" in spec:
        post = spec["postcondition"]
        if not isinstance(post, dict) or "postcondition" in post or "expected_exception" in post or post.get("kind") in ("timeout", "expected_exception"):
            raise ValueError("Postcondition must specify positive corrected behavior")
        validate_spec(post)
        for key in ("args", "kwargs", "constructors", "calls", "observe", "files"):
            if key in post and post[key] != spec.get(key, {} if key in ("kwargs", "files") else []):
                raise ValueError("Postcondition must preserve reproduction inputs")
    if not isinstance(spec.get("args", []), list):
        raise ReproductionError("args", "Invalid call arguments: positional arguments must be a JSON list", "invalid value")
    if not isinstance(spec.get("kwargs", {}), dict):
        raise ReproductionError("kwargs", "Invalid call arguments: keywords must be a JSON object", "invalid value")
    if type(spec.get("timeout_ms", 500)) is not int or not 50 <= spec.get("timeout_ms", 500) <= 2000:
        raise ReproductionError("timeout_ms", "Timeout must be 50 to 2000 milliseconds", "invalid value")
    if "expected_exception" in spec and spec["expected_exception"] not in EXCEPTIONS:
        raise ReproductionError("expected_exception", "Unsupported expected exception", "unsupported operation")
    if spec["kind"] == "expected_exception" and "expected_exception" not in spec:
        raise ValueError("Exception reproduction requires an exception type")
    if spec["kind"] in ("equals", "function_call", "class_state_check") and "expected" not in spec:
        raise ReproductionError("expected", "A declared expected result is required")
    if spec["kind"] == "deterministic_random" and not ({"expected", "expected_exception"} & spec.keys()):
        raise ValueError("Random verification requires a claimed outcome")
    if spec["kind"] in ("predicate", "invariant"):
        predicate = spec.get("predicate")
        if not isinstance(predicate, dict) or set(predicate) != {"op", "value"} or predicate["op"] not in ("eq", "ne", "lt", "le", "gt", "ge", "contains", "length_equals"):
            raise ValueError("Predicate must be a structured comparison")
    if spec["kind"] == "mutation_check" and not isinstance(spec.get("expected_args"), list):
        raise ValueError("Mutation verification requires expected post-call arguments")
    if type(spec.get("seed", 0)) is not int:
        raise ValueError("Random seed must be an integer")
    stubs = spec.get("random_values", {})
    if not isinstance(stubs, dict) or set(stubs) - {"randint", "randrange", "random"} or any(not isinstance(values, list) or len(values) > 100 for values in stubs.values()):
        raise ValueError("Invalid random stubs")
    boundaries = spec.get("random_boundaries", {})
    if not isinstance(boundaries, dict) or set(boundaries) - {"randint", "randrange", "random"} or any(
        not isinstance(values, list) or len(values) > 100 or any(value not in ("lower", "upper") for value in values)
        for values in boundaries.values()
    ):
        raise ValueError("Invalid random boundary strategy")
    files = spec.get("files", {})
    if not isinstance(files, dict) or any(not isinstance(value, str) for value in files.values()):
        raise ValueError("Virtual files must contain text")
    for name in files:
        if name.startswith(("/", "\\")) or ":" in name or ".." in name.replace("\\", "/").split("/"):
            raise ValueError("Virtual file paths must be project-relative")
    if spec["kind"] == "file_resource_check" and (type(spec.get("expected_open_resources")) is not int or not 0 <= spec["expected_open_resources"] <= 100):
        raise ValueError("Expected open resource count is required")
    if spec["kind"] == "class_state_check":
        constructors, calls, observe = spec.get("constructors"), spec.get("calls"), spec.get("observe")
        if not isinstance(constructors, list) or not 1 <= len(constructors) <= 5 or any(not isinstance(args, list) for args in constructors):
            raise ValueError("Invalid instance constructors")
        if not isinstance(calls, list) or len(calls) > 20 or not isinstance(observe, dict) or set(observe) != {"instance", "attribute"}:
            raise ValueError("Invalid class-state sequence")
        for operation in [*calls, observe]:
            name = operation.get("method", operation.get("attribute")) if isinstance(operation, dict) else None
            if not isinstance(operation, dict) or set(operation) - {"instance", "method", "args", "attribute"} or type(operation.get("instance")) is not int or not 0 <= operation["instance"] < len(constructors) or not isinstance(name, str) or not name.isidentifier() or name.startswith("__") or not isinstance(operation.get("args", []), list):
                raise ValueError("Invalid class operation")
    return spec


def _validate_plan(plan, *, allow_repair=True):
    """Validate a tiny operation language; plans contain no source/code fields."""
    if plan.get("kind") == "module_fragment":
        if set(plan) - {"kind", "target_lines", "target_kind", "required_bindings", "expected", "timeout_ms", "repair_plan"}:
            raise ValueError("Module fragment has unsupported fields")
        lines, bindings, expected = plan.get("target_lines"), plan.get("required_bindings", []), plan.get("expected")
        if (not isinstance(lines, list) or len(lines) != 2 or any(type(line) is not int or line < 1 for line in lines)
                or lines[1] < lines[0] or lines[1] - lines[0] > 20):
            raise ValueError("Module fragment target_lines must be a bounded line range")
        if plan.get("target_kind", "statement") not in ("statement", "expression"):
            raise ValueError("Module fragment target_kind must be statement or expression")
        if (not isinstance(bindings, list) or len(bindings) > 30 or any(not isinstance(name, str) or not name.isidentifier() or name.startswith("__") for name in bindings)):
            raise ValueError("Module fragment required_bindings must be local names without dunder traversal")
        if not isinstance(expected, dict) or set(expected) - {"result", "exception", "stdout", "stderr", "state"} or not expected:
            raise ValueError("Module fragment expected must declare bounded observed fields")
        if "exception" in expected and expected["exception"] is not None and expected["exception"] not in EXCEPTIONS:
            raise ValueError("Module fragment exception expectation must be an allowlisted type or null")
        if type(plan.get("timeout_ms", 500)) is not int or not 50 <= plan.get("timeout_ms", 500) <= 2000:
            raise ValueError("Plan timeout must be 50 to 2000 milliseconds")
        if "repair_plan" in plan:
            if not allow_repair or not isinstance(plan["repair_plan"], dict):
                raise ValueError("Nested repair plans are not supported")
            _validate_plan(plan["repair_plan"], allow_repair=False)
        encoded = json.dumps(plan, allow_nan=False)
        if len(encoded) > 64000:
            raise ValueError("Verification plan exceeds size limit")
        def validate_json(value, depth=0, budget=None):
            budget = [0] if budget is None else budget
            budget[0] += len(value) if type(value) is str else 1
            if depth > 8 or budget[0] > 64000:
                raise ValueError("Module fragment expected value exceeds nesting or size limits")
            if isinstance(value, dict):
                if len(value) > 1000 or any(not isinstance(key, str) for key in value):
                    raise ValueError("Invalid verification JSON object")
                for key, child in value.items():
                    validate_json(key, depth + 1, budget)
                    validate_json(child, depth + 1, budget)
            elif isinstance(value, list):
                if len(value) > 1000:
                    raise ValueError("Verification collection limit")
                for child in value:
                    validate_json(child, depth + 1, budget)
            elif type(value) in (int, float):
                if not math.isfinite(value) or abs(value) > 10**12:
                    raise ValueError("Verification numeric limit")
            elif type(value) is str and len(value) > 10000:
                raise ValueError("Verification string limit")
            elif value is not None and type(value) is not bool and type(value) is not str:
                raise ValueError("Verification values must be JSON values")
        validate_json(expected)
        return
    missing = next((field for field in ("kind", "steps", "assertions", "timeout_ms") if field not in plan), None)
    if missing is not None:
        raise ReproductionError(missing, "Plan fields must be kind, steps, assertions, timeout_ms, and optional repair_plan: required field is missing")
    if set(plan) - {"kind", "verification_target", "steps", "assertions", "timeout_ms", "repair_plan", "files", "seed", "random_values", "random_boundaries", "stubs", "clock"}:
        raise ReproductionError("verification_plan", "Plan fields must be kind, steps, assertions, timeout_ms, and optional repair_plan")
    if type(plan["timeout_ms"]) is not int or not 50 <= plan["timeout_ms"] <= 2000:
        raise ReproductionError("timeout_ms", "Plan timeout must be 50 to 2000 milliseconds", "invalid value")
    steps, assertions = plan["steps"], plan["assertions"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 40 or not isinstance(assertions, list) or not 1 <= len(assertions) <= 40:
        raise ValueError("Verification plans require bounded steps and assertions")
    names = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or step.get("op") not in ("call", "construct", "observe", "construct_value", "bind_callable", "advance_time", "state_setup", "observe_state"):
            raise ReproductionError(f"steps[{index}].op", "Unsupported verification plan operation", "unsupported operation")
        op = step["op"]
        expected = {"call": {"op", "target", "args", "kwargs", "as"},
                    "construct": {"op", "symbol", "args", "kwargs", "as"},
                    "observe": {"op", "target", "as"},
                    "construct_value": {"op", "constructor", "value", "as"},
                    "bind_callable": {"op", "symbol", "as"},
                    "advance_time": {"op", "seconds", "as"},
                    "state_setup": {"op", "symbol", "action", "value", "as"},
                    "observe_state": {"op", "symbol", "mode", "key", "as"}}[op]
        if set(step) - expected:
            raise ReproductionError(f"steps[{index}]", "Invalid verification plan step fields or result name: unsupported fields")
        if "as" not in step or not isinstance(step["as"], str) or not step["as"].isidentifier() or step["as"] in names:
            raise ReproductionError(f"steps[{index}].as", "Invalid verification plan step fields or result name")
        names.add(step["as"])
        if op == "advance_time":
            if "clock" not in plan or type(step.get("seconds")) not in (int, float) or not 0 <= step["seconds"] <= 86400:
                raise ReproductionError(f"steps[{index}].seconds", "Virtual time requires an explicit clock and bounded nonnegative advancement", "unsupported operation")
        elif op in {"state_setup", "observe_state"}:
            symbol = step.get("symbol")
            if not isinstance(symbol, str) or not symbol.isidentifier() or symbol.startswith("__"):
                raise ReproductionError(f"steps[{index}].symbol", "Module state must be a selected local identifier", "unsupported operation")
            if op == "state_setup" and (step.get("action") not in {"clear", "insert", "reset"} or
                                         (step["action"] in {"insert", "reset"} and "value" not in step)):
                raise ReproductionError(f"steps[{index}].action", "Unsupported controlled state setup", "unsupported operation")
            if op == "observe_state" and (step.get("mode") not in {"snapshot", "size", "contains"} or
                                           (step["mode"] == "contains" and "key" not in step)):
                raise ReproductionError(f"steps[{index}].mode", "Unsupported read-only state observation", "unsupported operation")
        elif op == "construct_value":
            adapter = trusted_constructor_adapters().get(step.get("constructor"))
            if adapter is None:
                raise ReproductionError(f"steps[{index}].constructor", "Constructor adapter is not registered or dependency is unavailable", "unsupported operation")
            value = step.get("value")
            if type(value) is not adapter.input_shape or len(value) > adapter.maximum_size:
                raise ReproductionError(f"steps[{index}].value", "Constructor input shape is invalid", "invalid value")
            if not adapter.allow_nested and any(type(item) in (dict, list) for item in (value.values() if type(value) is dict else value)):
                raise ReproductionError(f"steps[{index}].value", "Constructor input shape is invalid", "invalid value")
            if step["constructor"] == "record" and any(not key.isidentifier() or key.startswith("__") for key in value):
                raise ReproductionError(f"steps[{index}].value", "Record fields must be safe identifiers", "unsupported operation")
            if step["constructor"] == "set" and any(type(item) not in (str, int, float, bool, type(None)) for item in value):
                raise ReproductionError(f"steps[{index}].value", "Set members must be bounded scalars", "unsupported operation")
        elif op in {"call", "construct", "observe", "bind_callable"}:
            target = step.get("target", step.get("symbol", ""))
            if not isinstance(target, str) or not target or len(target.split(".")) > (4 if op == "observe" else 2) or any(not (part.isidentifier() or op == "observe" and part.isdecimal()) or part.startswith("__") for part in target.split(".")):
                raise ReproductionError(f"steps[{index}].target", "Plan targets must be local project symbols without dunder traversal", "unsupported operation")
        if op in {"call", "construct"} and (not isinstance(step.get("args", []), list) or not isinstance(step.get("kwargs", {}), dict)):
            raise ReproductionError(f"steps[{index}].args/kwargs", "Plan call arguments must be JSON collections", "invalid value")
        if op == "observe" and len(step["target"].split(".")) < 2:
            raise ReproductionError(f"steps[{index}].target", "Observation requires a bounded data path", "unsupported operation")
    allowed_sources = names | {"return", "stdout", "stderr", "exception.type", "exception.message", "resources_open", "resources_created", "args_after", "timed_out"}
    for index, assertion in enumerate(assertions):
        if isinstance(assertion, dict) and assertion.get("op") == "approx":
            if (set(assertion) - {"source", "op", "expected", "abs_tol", "rel_tol"} or
                    not {"source", "op", "expected"} <= set(assertion) or
                    not ({"abs_tol", "rel_tol"} & set(assertion)) or
                    assertion["source"] not in allowed_sources or
                    type(assertion["expected"]) not in (int, float) or abs(assertion["expected"]) > 10**12 or not math.isfinite(assertion["expected"]) or
                    any(type(assertion[key]) not in (int, float) or abs(assertion[key]) > 10**12 or not math.isfinite(assertion[key]) or
                        not 0 <= assertion[key] <= (1 if key == "rel_tol" else 10**6)
                        for key in ("abs_tol", "rel_tol") if key in assertion)):
                raise ReproductionError(f"assertions[{index}]", "Approximate assertion requires a finite numeric expected value and explicit bounded tolerance", "invalid value")
            continue
        if isinstance(assertion, dict) and set(assertion) == {"source", "op", "expected"}:
            if not isinstance(assertion["source"], str) or assertion["source"] not in allowed_sources:
                raise ReproductionError(f"assertions[{index}].source", "Unsupported verification plan assertion: source must be a named result or supported observation", "unsupported operation")
            if not isinstance(assertion["op"], str) or assertion["op"] not in ("eq", "ne", "lt", "le", "gt", "ge", "contains", "is_null"):
                raise ReproductionError(f"assertions[{index}].op", "Unsupported verification plan assertion: comparison operation is not supported", "unsupported operation")
        if (not isinstance(assertion, dict) or set(assertion) != {"source", "op", "expected"}
                or assertion["source"] not in allowed_sources
                or assertion["op"] not in ("eq", "ne", "lt", "le", "gt", "ge", "contains", "is_null")):
            raise ReproductionError(f"assertions[{index}]", "Unsupported verification plan assertion", "unsupported operation")
    if "repair_plan" in plan:
        if not allow_repair or not isinstance(plan["repair_plan"], dict):
            raise ValueError("Nested repair plans are not supported")
        _validate_plan(plan["repair_plan"], allow_repair=False)
    encoded = json.dumps(plan, allow_nan=False)
    if len(encoded) > 64000:
        raise ValueError("Verification plan exceeds size limit")

    def bounded_json(value, depth=0, budget=None):
        budget = [0] if budget is None else budget
        budget[0] += len(value) if type(value) is str else 1
        if depth > 8 or budget[0] > 64000:
            raise ValueError("Verification input nesting or size limit")
        if isinstance(value, dict):
            if len(value) > 1000 or any(not isinstance(key, str) for key in value):
                raise ValueError("Invalid verification JSON object")
            for key, child in value.items():
                bounded_json(key, depth + 1, budget)
                bounded_json(child, depth + 1, budget)
        elif isinstance(value, list):
            if len(value) > 1000:
                raise ValueError("Verification collection limit")
            for child in value:
                bounded_json(child, depth + 1, budget)
        elif type(value) in (int, float):
            if not math.isfinite(value) or abs(value) > 10**12:
                raise ValueError("Verification numeric limit")
        elif type(value) is str:
            if len(value) > 10000:
                raise ValueError("Verification string limit")
        elif value is not None and type(value) is not bool:
            raise ValueError("Verification inputs must be JSON values")

    bounded_json(plan)
    clock = plan.get("clock")
    if clock is not None and (not isinstance(clock, dict) or set(clock) != {"source", "start"} or
                              clock["source"] != "time.time" or type(clock["start"]) not in (int, float) or
                              not -10**12 <= clock["start"] <= 10**12):
        raise ReproductionError("clock", "Deterministic time source cannot be safely substituted", "unsupported operation")
    if sum(step["op"] == "observe_state" for step in steps) > 20:
        raise ReproductionError("steps", "Module state observation count limit", "invalid value")
    stubs = plan.get("stubs", {})
    if not isinstance(stubs, dict) or len(stubs) > 16:
        raise ReproductionError("stubs", "Local callable stubs must be a bounded object", "invalid value")
    for symbol, stub in stubs.items():
        if not isinstance(symbol, str) or not symbol.isidentifier() or symbol.startswith("__"):
            raise ReproductionError("stubs", "Stub targets must be local callable names", "unsupported operation")
        if not isinstance(stub, dict) or set(stub) != {"outcomes", "max_calls"} or type(stub["max_calls"]) is not int or not 1 <= stub["max_calls"] <= 20 or not isinstance(stub["outcomes"], list) or not 1 <= len(stub["outcomes"]) <= stub["max_calls"]:
            raise ReproductionError("stubs." + symbol, "Stub outcomes and maximum call count are required and bounded", "invalid value")
        for outcome in stub["outcomes"]:
            if not isinstance(outcome, dict) or len(outcome) != 1 or not ({"return", "raise"} & outcome.keys()) or ("raise" in outcome and outcome["raise"] not in EXCEPTIONS):
                raise ReproductionError("stubs." + symbol, "Stub outcome must return a JSON value or raise an allowed exception", "invalid value")
    if "seed" in plan and type(plan["seed"]) is not int:
        raise ValueError("Verification plan seed must be an integer")
    files = plan.get("files", {})
    if not isinstance(files, dict) or len(files) > 100:
        raise ValueError("Verification plan virtual files are invalid")
    for name, content in files.items():
        if (not isinstance(name, str) or not name or name.startswith(("/", "\\")) or ":" in name or
                ".." in name.replace("\\", "/").split("/") or not isinstance(content, str)):
            raise ValueError("Verification plan file paths must be relative virtual files")
    for field in ("random_values", "random_boundaries"):
        mapping = plan.get(field, {})
        if not isinstance(mapping, dict) or set(mapping) - {"randint", "randrange", "random"}:
            raise ValueError("Verification plan random controls are invalid")
        if any(not isinstance(values, list) or len(values) > 100 for values in mapping.values()):
            raise ValueError("Verification plan random controls are unbounded")
    for operations in plan.get("random_boundaries", {}).values():
        if any(value not in ("lower", "upper") for value in operations):
            raise ValueError("Verification plan random boundary is invalid")


def load_cached_plans(project_root):
    """Load only previously executed, schema-valid plans from project artifacts."""
    folder = Path(project_root).resolve() / ".aidebug"
    if folder.is_symlink() or not folder.is_dir() or folder.resolve() != folder:
        return {}
    cached = {}
    for path in sorted(folder.glob("hunt_findings_*.json"), key=lambda item: item.name, reverse=True)[:12]:
        try:
            if path.is_symlink() or path.stat().st_size > 1_000_000 or not path.resolve().is_relative_to(folder):
                continue
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            for finding in data.get("findings", []):
                wrapper = finding.get("verification_plan")
                if (finding.get("verification_status") not in ("confirmed", "rejected") or
                        not isinstance(wrapper, dict) or not isinstance(wrapper.get("fingerprint"), str) or
                        not isinstance(wrapper.get("plan"), dict)):
                    continue
                validate_spec(wrapper["plan"])
                cached.setdefault(wrapper["fingerprint"], wrapper["plan"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return cached


def structured_reproduction_spec(data, suspected_symbol="", evidence=None, verification_target=None):
    """Translate data-only call examples, never source strings or expressions."""
    if isinstance(data, dict) and isinstance(data.get("kind"), str) and data["kind"] not in KINDS - {"doctest"}:
        return None  # Explicit registered specialized adapters retain their own metadata.
    if isinstance(data, dict):
        data = dict(data)
        declared = verification_target or (evidence.get("reproduction_target") if isinstance(evidence, dict) else None)
        if declared and "verification_target" not in data and "target" not in data:
            data["verification_target"] = declared
    result = build_reproduction(data, suspected_symbol)
    return validate_spec(result) if result is not None else None


def resolve_verification_spec(project, hypothesis, cached_plans=None):
    """Resolve cached and hypothesis-provided plans without invoking a model."""
    source_path = _source_path(project.root, hypothesis.suspected_file)
    if source_path.stat().st_size > 120_000:
        raise ValueError("Unsupported source file")
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    identity = {"source_sha256": source_hash, "file": hypothesis.suspected_file.replace("\\", "/"),
                "symbol": hypothesis.suspected_symbol, "category": hypothesis.category,
                "hypothesis": hypothesis.description}
    declared_target = (hypothesis.verification_target or
                       (hypothesis.verification_plan.get("verification_target") if isinstance(hypothesis.verification_plan, dict) else None) or
                       (hypothesis.verification_spec.get("verification_target") if isinstance(hypothesis.verification_spec, dict) else None) or
                       (hypothesis.reproduction.get("verification_target", hypothesis.reproduction.get("target"))
                        if isinstance(hypothesis.reproduction, dict) else None) or
                       (hypothesis.evidence.get("reproduction_target") if isinstance(hypothesis.evidence, dict) else None))
    if declared_target:
        identity["verification_target"] = declared_target
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    cached = (cached_plans or {}).get(fingerprint)
    if cached is not None:
        normalized = validate_spec(validate_targets(source_path.read_text(encoding="utf-8"), hypothesis.suspected_symbol,
                                                   validate_spec(cached), hypothesis.evidence))
        return {"schema_version": 1, **identity, "verification_target": normalized.get("verification_target", hypothesis.suspected_symbol),
                "fingerprint": fingerprint, "plan": normalized, "source": "validated_cache"}
    explicit = hypothesis.verification_plan
    if explicit is not None:
        if hypothesis.verification_target and "verification_target" not in explicit:
            explicit = {**explicit, "verification_target": hypothesis.verification_target}
        normalized = validate_spec(validate_targets(source_path.read_text(encoding="utf-8"), hypothesis.suspected_symbol,
                                                   validate_spec(explicit), hypothesis.evidence))
        return {"schema_version": 1, **identity, "verification_target": normalized.get("verification_target", hypothesis.suspected_symbol), "fingerprint": fingerprint,
                "plan": normalized, "source": "hunter_plan"}
    spec = hypothesis.verification_spec
    built = spec is None
    if spec is None:
        spec = structured_reproduction_spec(hypothesis.reproduction, hypothesis.suspected_symbol, hypothesis.evidence, hypothesis.verification_target)
        if spec is None:
            spec = structured_reproduction_spec(hypothesis.reproduction_strategy, hypothesis.suspected_symbol, hypothesis.evidence, hypothesis.verification_target)
    if spec is None:
        return None
    if hypothesis.verification_target and "verification_target" not in spec:
        spec = {**spec, "verification_target": hypothesis.verification_target}
    normalized = validate_spec(validate_targets(source_path.read_text(encoding="utf-8"), hypothesis.suspected_symbol,
                                               validate_spec(spec), hypothesis.evidence))
    return {"schema_version": 1, **identity, "verification_target": normalized.get("verification_target", hypothesis.suspected_symbol), "fingerprint": fingerprint,
            "plan": normalized, "source": "deterministic_reproduction_builder" if built else "structured_hypothesis"}


def infer_python_syntax_spec(project, hypothesis):
    """Infer a parser check only when structured finding evidence pins its location."""
    try:
        path = _source_path(project.root, hypothesis.suspected_file)
        if path.suffix != ".py" or path.stat().st_size > 120_000:
            return None
        ast.parse(path.read_text(encoding="utf-8"), filename=hypothesis.suspected_file)
    except SyntaxError as error:
        if _syntax_evidence_matches(hypothesis, error.lineno, error.offset):
            return {"kind": "python_syntax", "line": error.lineno,
                    "column": error.offset, "message_contains": error.msg[:80]}
    except (OSError, UnicodeError, RecursionError, ValueError):
        return None
    return None


def _syntax_evidence_matches(hypothesis, error_line, error_column):
    """Match inclusive source-line spans for the target file; columns are diagnostics."""
    def normalize(name):
        return name.replace("\\", "/").removeprefix("./")

    file_name = normalize(hypothesis.suspected_file)
    spans = []

    def walk(value, inherited_file=file_name):
        if isinstance(value, dict):
            declared_file = value.get("file", value.get("path", inherited_file))
            if not isinstance(declared_file, str) or normalize(declared_file) != file_name:
                return
            pair = value.get("line_range", value.get("source_range"))
            start = value.get("start_line", value.get("lineno", value.get("line", value.get("start"))))
            end = value.get("end_line", value.get("end_lineno", value.get("end", start)))
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                start, end = pair[:2]
            elif isinstance(pair, dict):
                # Recurse so the range's own file metadata is respected.
                start = end = None
            elif isinstance(pair, str):
                line_range = re.fullmatch(r"\s*(\d+)\s*[-\u2013]\s*(\d+)\s*", pair)
                if line_range:
                    start, end = map(int, line_range.groups())
            if type(start) is int and type(end) is int and 1 <= start <= end:
                spans.append((start, end))
            if isinstance(pair, (dict, str)):
                walk(pair, declared_file)
            for key, child in value.items():
                if key not in {"file", "path", "line", "lineno", "start_line", "end_line", "end_lineno",
                               "line_range", "source_range", "column", "start_column", "end_column"}:
                    walk(child, declared_file)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, inherited_file)
        elif isinstance(value, str):
            locations = list(re.finditer(
                r"(?P<file>(?:[A-Za-z]:)?[^\s`\"'<>(),;]+\.py):(?P<start>\d+)"
                r"(?:\s*[-\u2013]\s*(?P<end>\d+))?(?::\d+)?", value))
            for match in locations:
                if normalize(match.group("file")) == file_name:
                    spans.append((int(match.group("start")), int(match.group("end") or match.group("start"))))
            if not locations:
                for match in re.finditer(r"\b(?:lines?|L)\s*[:#]?\s*(\d+)(?:\s*[-\u2013]\s*(\d+))?\b", value, re.IGNORECASE):
                    spans.append((int(match.group(1)), int(match.group(2) or match.group(1))))

    walk(hypothesis.evidence)
    return any(start <= error_line <= end for start, end in spans)


class PythonSyntaxVerifier:
    """Compile-only capability for Python files; it needs neither a symbol nor a plan."""

    def verify(self, project, hypothesis):
        from .hunt import Finding
        explicit = (hypothesis.verification_spec if isinstance(hypothesis.verification_spec, dict)
                    and hypothesis.verification_spec.get("kind") == "python_syntax" else None)
        try:
            path = _source_path(project.root, hypothesis.suspected_file)
        except (OSError, ValueError):
            return None
        if path.suffix != ".py" or path.stat().st_size > 120_000:
            return None
        with isolated_workspace(project.root) as workspace:
            isolated_path = _source_path(workspace, hypothesis.suspected_file)
            source = isolated_path.read_text(encoding="utf-8")
            try:
                ast.parse(source, filename=hypothesis.suspected_file)
            except SyntaxError as error:
                location = f"{hypothesis.suspected_file}:{error.lineno}:{error.offset}"
                details = f"SyntaxError: {error.msg} at {location}"
                if explicit is None:
                    return Finding(hypothesis, "high_confidence" if hypothesis.confidence >= .85 else "unconfirmed",
                                   "UNVERIFIABLE: Python source fails compile-only parsing; this behavioral hypothesis remains separate. " + details,
                                   verification_plan={"schema_version": 1, "file": hypothesis.suspected_file,
                                                      "symbol": hypothesis.suspected_symbol,
                                                      "source": "deterministic_verifier", "verifier": "python-parse",
                                                      "capability": "parse_compile", "plan": None,
                                                      "unsupported_reason": "Behavioral verification blocked by a separate syntax defect"})
                matched = error.lineno == explicit.get("line") and (
                    not explicit.get("message_contains") or explicit["message_contains"].casefold() in error.msg.casefold())
                status = "confirmed" if matched else "unconfirmed"
                if matched:
                    evidence = f"Python source fails compile-only parsing at the claimed syntax location. {details}."
                else:
                    evidence = (f"Python source has a parse failure, but finding evidence does not identify that location; "
                                f"it cannot confirm this finding. {details}.")
                check = CheckResult("hunt:python_syntax", ("ast.parse", hypothesis.suspected_file),
                                    1 if matched else 0, "", details if matched else "", 0) if matched else None
                return Finding(hypothesis, status, evidence, check,
                               verification_plan={"schema_version": 1, "file": hypothesis.suspected_file,
                                                  "symbol": hypothesis.suspected_symbol, "source": "deterministic_verifier",
                                                  "verifier": "python-parse", "capability": "parse_compile",
                                                  "parser_error_type": type(error).__name__,
                                                  "plan": ({"kind": "python_syntax", "line": error.lineno,
                                                            "column": error.offset, "message_contains": error.msg[:80]}
                                                           if matched else explicit)})
        if explicit is not None:
            check = CheckResult("hunt:python_syntax", ("ast.parse", hypothesis.suspected_file), 0,
                                "Python source parses after repair", "", 0)
            return Finding(hypothesis, "rejected", "Python parser accepted the source; the syntax defect is resolved.", check,
                           verification_plan={"schema_version": 1, "file": hypothesis.suspected_file,
                                              "symbol": hypothesis.suspected_symbol, "source": "deterministic_verifier",
                                              "verifier": "python-parse", "capability": "parse_compile", "plan": explicit})
        return None


def prepare_repair_hypothesis(project, hypothesis):
    """Pin a positive postcondition and replay boundary intent, not stale draws."""
    spec = validate_spec(hypothesis.verification_spec)
    positive = dict(spec)
    post = positive.pop("postcondition", None)
    if post:
        positive.update(post)
    positive.pop("expected_exception", None)
    if positive["kind"] in ("expected_exception", "timeout"):
        if "expected" not in positive:
            return None
        positive["kind"] = "equals"
    if positive["kind"] == "deterministic_random" and "expected" not in positive:
        return None
    if spec.get("random_values") and not spec.get("random_boundaries"):
        with isolated_workspace(project.root) as workspace:
            path = _source_path(workspace, hypothesis.suspected_file)
            if path.stat().st_size > 120000:
                return None
            observed = observe(path.read_text(encoding="utf-8"), spec.get("verification_target", hypothesis.suspected_symbol), spec, workspace)
        draws = observed.get("observed", {}).get("random_draws", [])
        boundaries = {}
        for operation, values in spec["random_values"].items():
            matching = [draw for draw in draws if draw["operation"] == operation]
            modes = []
            for value, draw in zip(values, matching):
                args = draw["args"]
                if operation == "randint":
                    lower, upper = args
                elif operation == "randrange":
                    choices = range(*args)
                    lower, upper = choices[0], choices[-1]
                else:
                    lower, upper = 0.0, math.nextafter(1.0, 0.0)
                if value == upper:
                    modes.append("upper")
                elif value == lower:
                    modes.append("lower")
                else:
                    return None  # No justified way to reinterpret this fixed draw.
            if len(modes) != len(values):
                return None
            boundaries[operation] = modes
        positive.pop("random_values", None)
        positive["random_boundaries"] = boundaries
    return replace(hypothesis, verification_spec=validate_spec(positive))


def observe(source, symbol, spec, cwd):
    worker = Path(__file__).with_name("_verification_worker.py")
    # The tool interpreter runs our evaluator only, not the target environment.
    env = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    process = subprocess.Popen([sys.executable, "-I", "-S", str(worker)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=cwd, env=env)
    try:
        process.stdin.write(json.dumps({"source": source, "symbol": symbol, "spec": spec}) + "\n")
        process.stdin.flush()
        ready = queue.Queue()
        reader = threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True)
        reader.start()
        try:
            first = ready.get(timeout=5)
        except queue.Empty:
            return {"unsupported": "Worker startup exceeded its deadline; not a reproduced project timeout"}
        if first.strip() != "ready":
            try:
                diagnostic = json.loads(first).get("unsupported")
            except (ValueError, AttributeError):
                diagnostic = None
            return {"unsupported": diagnostic or "Source initialization or symbol is unsupported"}
        try:
            stdout, _ = process.communicate("run\n", timeout=spec.get("timeout_ms", 500) / 1000)
        except subprocess.TimeoutExpired:
            return {"timed_out": True, "deadline_ms": spec.get("timeout_ms", 500)}
        if process.returncode:
            return {"unsupported": "Verification worker failed"}
        return json.loads(stdout)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()


def compare(spec, observed):
    exception = observed["exception"]
    kind = spec["kind"]
    if kind == "module_fragment":
        expected = spec["expected"]
        actual = observed.get("result", {})
        if "exception" in expected:
            actual_exception = observed.get("exception")
            observed_exception = actual_exception.get("type") if actual_exception else None
            if observed_exception != expected["exception"]:
                return "confirmed"
        for key in ("result", "stdout", "stderr", "state"):
            if key in expected and actual.get(key) != expected[key]:
                return "confirmed"
        return "rejected"
    if kind == "verification_plan":
        values = dict(observed.get("result") or {})
        values["exception.type"] = exception.get("type") if exception else None
        values["exception.message"] = exception.get("message") if exception else None
        values["timed_out"] = observed.get("timed_out", False)
        failed = []
        for assertion in spec["assertions"]:
            actual = values.get(assertion["source"])
            expected = assertion["expected"]
            if assertion["op"] == "approx":
                if type(actual) not in (int, float) or not math.isfinite(actual):
                    return "unconfirmed"
                passed = math.isclose(actual, expected, abs_tol=assertion.get("abs_tol", 0),
                                      rel_tol=assertion.get("rel_tol", 0))
                if not passed:
                    failed.append(assertion["source"])
                continue
            operations = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt, "le": operator.le,
                          "gt": operator.gt, "ge": operator.ge, "contains": lambda a, b: b in a,
                          "is_null": lambda a, b: a is None}
            try:
                passed = operations[assertion["op"]](actual, expected)
            except (TypeError, ValueError, KeyError):
                return "unconfirmed"
            if not passed:
                failed.append(assertion["source"])
        return "confirmed" if failed else "rejected"
    if "expected_exception" in spec:
        if exception is None:
            return "rejected"
        return "confirmed" if exception["type"] == spec["expected_exception"] else "unconfirmed"
    if kind == "timeout":
        return "rejected"
    if exception:
        if kind in ("function_call", "equals", "predicate", "invariant", "deterministic_random") and "expected" in spec:
            return "confirmed"
        return "unconfirmed"
    if kind == "mutation_check":
        correct = observed["args_after"] == spec["expected_args"]
    elif kind == "file_resource_check":
        if not observed["resources_created"]:
            return "unconfirmed"
        correct = observed["open_resources"] == spec["expected_open_resources"]
    elif kind in ("predicate", "invariant"):
        predicate = spec["predicate"]
        operations = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge,
                      "contains": lambda a, b: b in a, "length_equals": lambda a, b: len(a) == b}
        correct = operations[predicate["op"]](observed["result"], predicate["value"])
    else:
        correct = observed["result"] == spec["expected"]
    return "rejected" if correct else "confirmed"


class StructuredVerifier:
    def prepare_repair(self, project, hypothesis):
        if hypothesis.verification_spec and hypothesis.verification_spec.get("kind") == "module_fragment":
            return hypothesis
        if hypothesis.verification_spec and hypothesis.verification_spec.get("kind") == "verification_plan":
            plan = hypothesis.verification_spec
            repair_plan = plan.get("repair_plan")
            timeout_claim = any(assertion["source"] == "timed_out" and assertion["expected"] is True
                                for assertion in plan["assertions"])
            if repair_plan is None and not timeout_claim:
                # These assertions define the positive contract to recheck after repair.
                repair_plan = {key: value for key, value in plan.items() if key != "repair_plan"}
            if repair_plan is None:
                return None
            from dataclasses import replace
            return replace(hypothesis, verification_spec=repair_plan, verification_plan=repair_plan)
        return prepare_repair_hypothesis(project, hypothesis)

    def verify(self, project, hypothesis):
        pending = "high_confidence" if hypothesis.confidence >= 0.85 else "unconfirmed"
        try:
            plan = resolve_verification_spec(project, hypothesis)
            if plan is None:
                reason = hypothesis.verification_unsupported or "No safe structured input or reproduction strategy is available; free-form reproduction text is never executed."
                return Finding(hypothesis, pending, "UNVERIFIABLE: " + reason)
            spec = validate_spec(plan["plan"])
            capability = CAPABILITY_REGISTRY.resolve(spec)
            if capability is None:
                raise ValueError("No deterministic capability is registered for this verification kind")
            if spec["kind"] == "python_syntax":
                _source_path(project.root, hypothesis.suspected_file)
                with isolated_workspace(project.root) as workspace:
                    path = _source_path(workspace, hypothesis.suspected_file)
                    if path.suffix != ".py" or path.stat().st_size > 120_000:
                        raise ValueError("Unsupported Python syntax target")
                    source = path.read_text(encoding="utf-8")
                    try:
                        ast.parse(source, filename=hypothesis.suspected_file)
                    except SyntaxError as error:
                        expected_line = spec["line"]
                        matches = error.lineno == expected_line
                        status = "confirmed" if matches else "unconfirmed"
                        evidence = (f"Python parser observed SyntaxError at line {error.lineno}, column {error.offset}: "
                                    f"{error.msg}. Claimed location: line {expected_line}.")
                    else:
                        status = "rejected"
                        evidence = "Python parser accepted the source; the claimed syntax defect was not reproduced."
                check = CheckResult("hunt:python_syntax", ("ast.parse", hypothesis.suspected_file),
                                    1 if status == "confirmed" else 0,
                                    evidence if status == "rejected" else "",
                                    evidence if status == "confirmed" else "", 0)
                return Finding(hypothesis, status, evidence, check)
            module_fragment = spec["kind"] == "module_fragment"
            if not module_fragment and spec["kind"] != "verification_plan" and (not hypothesis.suspected_symbol.isidentifier() or hypothesis.suspected_symbol.startswith("__")):
                raise ValueError("Project symbol must be a local identifier without dunder traversal")
            _source_path(project.root, hypothesis.suspected_file)
            with isolated_workspace(project.root) as workspace:
                path = _source_path(workspace, hypothesis.suspected_file)
                if path.suffix != ".py" or path.stat().st_size > 120_000:
                    raise ValueError("Unsupported source file")
                source = path.read_text(encoding="utf-8")
                spec = validate_spec(validate_targets(source, hypothesis.suspected_symbol, spec, hypothesis.evidence))
                observation = observe(source, spec.get("verification_target", hypothesis.suspected_symbol), spec, workspace)
            if observation.get("unsupported"):
                return Finding(hypothesis, pending, "UNVERIFIABLE: " + observation["unsupported"] + "; no confirmation or repair authorized.")
            if observation.get("timed_out"):
                timed_out_claim = (spec["kind"] == "verification_plan" and any(
                    assertion["source"] == "timed_out" and assertion["expected"] is True for assertion in spec["assertions"]))
                status = "confirmed" if spec["kind"] == "timeout" or timed_out_claim else "unconfirmed"
                evidence = f"Restricted project call exceeded {observation['deadline_ms']} ms after worker readiness; worker terminated. This is a bounded non-completion observation, not proof of infinite execution."
                if status != "confirmed":
                    evidence = "UNVERIFIABLE: execution limit exceeded: wall-clock timeout. " + evidence
                check = CheckResult("hunt:" + spec["kind"], ("restricted-verifier", hypothesis.suspected_file,
                                    hypothesis.suspected_symbol), 1 if status == "confirmed" else 0,
                                    "", evidence if status == "confirmed" else "", 0)
            else:
                actual = observation["observed"]
                status = compare(spec, actual)
                evidence = "Restricted execution observed: " + json.dumps(actual, ensure_ascii=True)[:8000]
                evidence += ". Compared with the structured claimed outcome; correctness beyond that expectation is not established."
            check = None
            if status in ("confirmed", "rejected"):
                check = CheckResult("hunt:" + spec["kind"], ("restricted-verifier", hypothesis.suspected_file, hypothesis.suspected_symbol),
                                    1 if status == "confirmed" else 0, evidence if status == "rejected" else "",
                                    "AssertionError: " + evidence if status == "confirmed" else "", 0)
            return Finding(hypothesis, status, evidence, check)
        except (ValueError, OSError, TypeError, KeyError, RecursionError, OverflowError) as exc:
            return Finding(hypothesis, pending, f"UNVERIFIABLE: unsupported verification capability ({type(exc).__name__}: {str(exc)[:180]}); no confirmation or repair authorized.")
