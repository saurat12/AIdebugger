"""Validate declarative verification data and interpret source in a subprocess."""

import json
import hashlib
import math
import operator
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from dataclasses import replace

from .hunt import Finding, _source_path
from .models import CheckResult
from .workspace import isolated_workspace


KINDS = {"function_call", "expected_exception", "equals", "predicate", "invariant", "mutation_check",
         "deterministic_random", "class_state_check", "timeout", "file_resource_check", "verification_plan", "module_fragment", "doctest"}
EXCEPTIONS = {"IndexError", "ZeroDivisionError", "ValueError", "TypeError", "KeyError", "RuntimeError", "AssertionError", "NameError"}


def validate_spec(spec):
    """No expressions, code strings, module names, or arbitrary calls in specs."""
    if not isinstance(spec, dict) or spec.get("kind") not in KINDS:
        raise ValueError("Invalid verification kind")
    allowed = {"kind", "args", "kwargs", "expected", "expected_exception", "predicate", "expected_args", "target_lines", "required_bindings",
               "seed", "random_values", "random_boundaries", "postcondition", "constructors", "calls", "observe", "timeout_ms", "files", "expected_open_resources",
               "steps", "assertions", "repair_plan"}
    if set(spec) - allowed:
        raise ValueError("Unknown verification fields")
    if spec["kind"] in ("verification_plan", "module_fragment"):
        _validate_plan(spec)
        return spec
    if spec["kind"] == "doctest":
        if set(spec) != {"kind"}:
            raise ValueError("Explicit doctest adapter accepts only {kind: doctest}")
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
    if not isinstance(spec.get("args", []), list) or not isinstance(spec.get("kwargs", {}), dict):
        raise ValueError("Invalid call arguments")
    if type(spec.get("timeout_ms", 500)) is not int or not 50 <= spec.get("timeout_ms", 500) <= 2000:
        raise ValueError("Timeout must be 50 to 2000 milliseconds")
    if "expected_exception" in spec and spec["expected_exception"] not in EXCEPTIONS:
        raise ValueError("Unsupported expected exception")
    if spec["kind"] == "expected_exception" and "expected_exception" not in spec:
        raise ValueError("Exception reproduction requires an exception type")
    if spec["kind"] in ("equals", "function_call", "class_state_check") and "expected" not in spec:
        raise ValueError("A declared expected result is required")
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
            if not isinstance(operation, dict) or set(operation) - {"instance", "method", "args", "attribute"} or type(operation.get("instance")) is not int or not 0 <= operation["instance"] < len(constructors) or not isinstance(name, str) or not name.isidentifier() or name.startswith("_") or not isinstance(operation.get("args", []), list):
                raise ValueError("Invalid class operation")
    return spec


def _validate_plan(plan, *, allow_repair=True):
    """Validate a tiny operation language; plans contain no source/code fields."""
    if plan.get("kind") == "module_fragment":
        if set(plan) - {"kind", "target_lines", "required_bindings", "expected", "timeout_ms", "repair_plan"}:
            raise ValueError("Module fragment has unsupported fields")
        lines, bindings, expected = plan.get("target_lines"), plan.get("required_bindings", []), plan.get("expected")
        if (not isinstance(lines, list) or len(lines) != 2 or any(type(line) is not int or line < 1 for line in lines)
                or lines[1] < lines[0] or lines[1] - lines[0] > 20):
            raise ValueError("Module fragment target_lines must be a bounded line range")
        if (not isinstance(bindings, list) or len(bindings) > 30 or any(not isinstance(name, str) or not name.isidentifier() or name.startswith("_") for name in bindings)):
            raise ValueError("Module fragment required_bindings must be public local names")
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
    if set(plan) - {"kind", "steps", "assertions", "timeout_ms", "repair_plan", "files", "seed", "random_values", "random_boundaries"} or not {"kind", "steps", "assertions", "timeout_ms"} <= set(plan):
        raise ValueError("Plan fields must be kind, steps, assertions, timeout_ms, and optional repair_plan")
    if type(plan["timeout_ms"]) is not int or not 50 <= plan["timeout_ms"] <= 2000:
        raise ValueError("Plan timeout must be 50 to 2000 milliseconds")
    steps, assertions = plan["steps"], plan["assertions"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 40 or not isinstance(assertions, list) or not 1 <= len(assertions) <= 40:
        raise ValueError("Verification plans require bounded steps and assertions")
    names = set()
    for step in steps:
        if not isinstance(step, dict) or step.get("op") not in ("call", "construct", "observe"):
            raise ValueError("Unsupported verification plan operation")
        op = step["op"]
        expected = {"call": {"op", "target", "args", "kwargs", "as"},
                    "construct": {"op", "symbol", "args", "kwargs", "as"},
                    "observe": {"op", "target", "as"}}[op]
        if set(step) - expected or "as" not in step or not isinstance(step["as"], str) or not step["as"].isidentifier() or step["as"] in names:
            raise ValueError("Invalid verification plan step fields or result name")
        names.add(step["as"])
        target = step.get("target", step.get("symbol", ""))
        if not isinstance(target, str) or not target or any(not part.isidentifier() or part.startswith("_") for part in target.split(".")):
            raise ValueError("Plan targets must be public project symbols")
        if op != "observe" and (not isinstance(step.get("args", []), list) or not isinstance(step.get("kwargs", {}), dict)):
            raise ValueError("Plan call arguments must be JSON collections")
    allowed_sources = names | {"return", "stdout", "stderr", "exception.type", "exception.message", "resources_open", "resources_created", "args_after", "timed_out"}
    for assertion in assertions:
        if (not isinstance(assertion, dict) or set(assertion) != {"source", "op", "expected"}
                or assertion["source"] not in allowed_sources
                or assertion["op"] not in ("eq", "ne", "lt", "le", "gt", "ge", "contains", "is_null")):
            raise ValueError("Unsupported verification plan assertion")
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


class VerificationPlanner:
    """Turn structured hypothesis evidence into validated declarative plans."""

    def __init__(self, agent=None):
        self.agent = agent

    def plan(self, project, hypothesis, cached_plans=None):
        source_path = _source_path(project.root, hypothesis.suspected_file)
        if source_path.stat().st_size > 120_000:
            raise ValueError("Unsupported source file")
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        identity = {"source_sha256": source_hash, "file": hypothesis.suspected_file.replace("\\", "/"),
                    "symbol": hypothesis.suspected_symbol, "category": hypothesis.category,
                    "hypothesis": hypothesis.description}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        cached = (cached_plans or {}).get(fingerprint)
        if cached is not None:
            normalized = validate_spec(cached)
            return {"schema_version": 1, **identity, "fingerprint": fingerprint, "plan": normalized, "source": "validated_cache"}
        explicit = hypothesis.verification_plan
        if explicit is not None:
            normalized = validate_spec(explicit)
            return {"schema_version": 1, **identity, "fingerprint": fingerprint,
                    "plan": normalized, "source": "hunter_plan"}
        spec = hypothesis.verification_spec
        if spec is None:
            reproduction = hypothesis.reproduction
            if isinstance(reproduction, dict) and reproduction.get("kind") in KINDS - {"verification_plan", "doctest"}:
                spec = reproduction
        if spec is None:
            # String strategies are evidence for a human, not executable plans.
            # Ask the optional planner model to express an evidence-based oracle
            # in the restricted operation language. Its output is still data.
            if self.agent is None:
                return None
            source = source_path.read_text(encoding="utf-8")
            response = self.agent._complete(
                """Convert this bug hypothesis into a safe verification plan, or return {\"verification_plan\": null}.
Return JSON only. Never return Python, shell, expressions, imports, or code strings.
Use only the fixed project file/symbol and public local symbols in the supplied source.
Inputs must be bounded JSON. Expected behavior must follow from concrete evidence.
Schema: {\"verification_plan\": {\"kind\":\"verification_plan\",\"steps\":[...],\"assertions\":[...],\"timeout_ms\":500,
optional \"files\":{\"relative.txt\":\"virtual text\"}, \"seed\":0, \"random_values\":{...},
optional \"repair_plan\": another plan with the same reproduction inputs and positive corrected-behavior assertions}.
Step forms: {\"op\":\"call\",\"target\":\"public_function\",\"args\":[],\"kwargs\":{},\"as\":\"result_name\"};
{\"op\":\"construct\",\"symbol\":\"PublicClass\",\"args\":[],\"kwargs\":{},\"as\":\"instance_name\"};
{\"op\":\"call\",\"target\":\"instance_name.public_method\",\"args\":[],\"kwargs\":{},\"as\":\"result_name\"};
{\"op\":\"observe\",\"target\":\"instance_name.public_attribute\",\"as\":\"state_name\"}.
Assertions compare an observed source with expected using eq, ne, lt, le, gt, ge, contains, or is_null.
Sources are step result names, return, stdout, stderr, exception.type, exception.message,
args_after, resources_open, resources_created, timed_out. Assertions describe correct behavior;
a failed assertion is evidence of the claimed bug. Do not guess expected behavior.
For a module-level finding with no function target, select only the smallest relevant top-level statement using
{"kind":"module_fragment","target_lines":[start,end],"required_bindings":["safe_preceding_name"],
"expected":{"exception":null},"timeout_ms":500}. Set expected fields from the hypothesis evidence; do not assume no exception is always correct.
Use {"kind":"doctest"} only when a declared docstring doctest is the canonical reproduction plan.
If no safe, discriminating plan can be justified, return null.""",
                json.dumps({"file": identity["file"], "symbol": identity["symbol"], "category": identity["category"],
                            "hypothesis": hypothesis.description, "evidence": hypothesis.evidence,
                            "source": source[:80_000]}, ensure_ascii=True), project.root)
            from .openai_agent import _json_object
            parsed = _json_object(response)
            spec = parsed.get("verification_plan")
            if spec is None:
                return None
            if not isinstance(spec, dict):
                raise ValueError("Planner verification_plan must be an object or null")
            normalized = validate_spec(spec)
            return {"schema_version": 1, **identity, "fingerprint": fingerprint,
                    "plan": normalized, "source": "model_planner"}
        normalized = validate_spec(spec)
        return {"schema_version": 1, **identity, "fingerprint": fingerprint,
                "plan": normalized, "source": "structured_hypothesis"}


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
            observed = observe(path.read_text(encoding="utf-8"), hypothesis.suspected_symbol, spec, workspace)
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
            planner = VerificationPlanner()
            plan = planner.plan(project, hypothesis)
            if plan is None:
                reason = hypothesis.verification_unsupported or "No safe structured input or reproduction strategy is available; free-form reproduction text is never executed."
                return Finding(hypothesis, pending, "UNVERIFIABLE: " + reason)
            spec = validate_spec(plan["plan"])
            module_fragment = spec["kind"] == "module_fragment"
            if not module_fragment and (not hypothesis.suspected_symbol.isidentifier() or hypothesis.suspected_symbol.startswith("_")):
                raise ValueError("Only public project symbols can be resolved")
            _source_path(project.root, hypothesis.suspected_file)
            with isolated_workspace(project.root) as workspace:
                path = _source_path(workspace, hypothesis.suspected_file)
                if path.suffix != ".py" or path.stat().st_size > 120_000:
                    raise ValueError("Unsupported source file")
                observation = observe(path.read_text(encoding="utf-8"), hypothesis.suspected_symbol, spec, workspace)
            if observation.get("unsupported"):
                return Finding(hypothesis, pending, observation["unsupported"] + "; no confirmation or repair authorized.")
            if observation.get("timed_out"):
                timed_out_claim = (spec["kind"] == "verification_plan" and any(
                    assertion["source"] == "timed_out" and assertion["expected"] is True for assertion in spec["assertions"]))
                status = "confirmed" if spec["kind"] == "timeout" or timed_out_claim else "unconfirmed"
                evidence = f"Restricted project call exceeded {observation['deadline_ms']} ms after worker readiness; worker terminated. This is a bounded non-completion observation, not proof of infinite execution."
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
