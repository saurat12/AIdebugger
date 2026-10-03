"""Interpret explicit reproduction data and bind calls without executing source."""

import ast
import inspect

from ._verification_worker import trusted_constructor_adapters


class ReproductionError(ValueError):
    def __init__(self, field, reason, failure_kind="schema violation"):
        self.field, self.reason, self.failure_kind = field, reason, failure_kind
        super().__init__(f"{failure_kind} at {field}: {reason}")


def validation_diagnostic(error):
    """Persist known validation details without exposing provider error bodies."""
    if isinstance(error, ReproductionError):
        return {"field": error.field, "failure_kind": error.failure_kind, "reason": error.reason[:240]}
    if isinstance(error, ValueError):
        return {"field": "verification_spec", "failure_kind": "schema violation", "reason": str(error)[:240]}
    return {"field": "verification_plan", "failure_kind": "unsupported operation",
            "reason": f"Verification planning unavailable ({type(error).__name__})"}


def local_name(value, field):
    if not isinstance(value, str) or not value.isidentifier() or value.startswith("__"):
        raise ReproductionError(field, "a local identifier without dunder traversal is required", "unsupported operation")
    return value


def build_reproduction(data, suspected_symbol):
    """Translate only documented data shapes; strings are never interpreted."""
    if not isinstance(data, dict):
        return None
    if set(data) == {"verification_spec"}:
        return data["verification_spec"]
    if "kind" in data:
        return data
    if "structured_inputs" in data or "structured_input" in data:
        inputs = data.get("structured_inputs")
        if inputs is None and "structured_input" in data:
            inputs = [{"type": data.get("required_adapter"), "value": data["structured_input"], "as": "input"}]
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= 10:
            raise ReproductionError("structured_inputs", "structured input metadata insufficient", "invalid value")
        adapters = trusted_constructor_adapters()
        steps = []
        for index, entry in enumerate(inputs):
            if not isinstance(entry, dict) or set(entry) != {"type", "value", "as"}:
                raise ReproductionError(f"structured_inputs[{index}]", "structured input metadata insufficient", "invalid value")
            name, value, alias = entry["type"], entry["value"], entry["as"]
            if "required_adapter" in data and name != data["required_adapter"]:
                raise ReproductionError("required_adapter", "structured input adapter does not match declared requirement", "target mismatch")
            adapter = adapters.get(name) if isinstance(name, str) else None
            if adapter is None:
                raise ReproductionError(f"structured_inputs[{index}].type", "constructor adapter unavailable for required structured input", "unsupported operation")
            local_name(alias, f"structured_inputs[{index}].as")
            if alias in {step["as"] for step in steps} or type(value) is not adapter.input_shape or len(value) > adapter.maximum_size:
                raise ReproductionError(f"structured_inputs[{index}].value", "structured input does not match registered adapter schema", "invalid value")
            if not adapter.allow_nested and any(type(child) in (list, dict) for child in (value.values() if type(value) is dict else value)):
                raise ReproductionError(f"structured_inputs[{index}].value", "structured input does not match registered adapter schema", "invalid value")
            steps.append({"op": "construct_value", "constructor": name, "value": value, "as": alias})
        target = local_name(data.get("verification_target", data.get("target", suspected_symbol)), "verification_target")
        args = data.get("args", [{"$ref": step["as"]} for step in steps])
        if not isinstance(args, list):
            raise ReproductionError("args", "structured input call arguments must be a list", "invalid value")
        steps.append({"op": "call", "target": target, "args": args, "kwargs": data.get("kwargs", {}), "as": "result"})
        expectation = data.get("expected_behavior", {"return_value": data["expected"]} if "expected" in data else None)
        if expectation is None:
            raise ReproductionError("expected_behavior", "structured input metadata insufficient: expected behavior is required", "invalid value")
        allowed = {"structured_inputs", "structured_input", "required_adapter", "target", "verification_target", "args", "kwargs", "expected", "expected_behavior", "timeout_ms"}
        if set(data) - allowed:
            raise ReproductionError("structured_inputs", "structured input metadata contains unsupported fields", "unsupported operation")
        return {"kind": "verification_plan", "steps": steps, "assertions": expectation_assertions(expectation, steps),
                "timeout_ms": data.get("timeout_ms", 500), "verification_target": target}
    approximate = data.get("expected_behavior")
    if isinstance(approximate, dict) and "approx" in approximate and not ({"abs_tol", "rel_tol"} & approximate.keys()):
        raise ReproductionError("expected_behavior", "approximate assertion unavailable: missing structured tolerance", "unsupported operation")
    for field, reason in (("required_adapter", "constructor adapter unavailable"),
                          ("time_source", "deterministic time source cannot be substituted"),
                          ("required_state", "module state is not safely observable"),
                          ("required_helper", "helper dependency cannot be stubbed"),
                          ("required_input_type", "required input type is unsupported")):
        if field in data:
            steps = data.get("steps", [])
            steps = steps if isinstance(steps, list) else []
            stubs = data.get("stubs", {})
            stubs = stubs if isinstance(stubs, dict) else {}
            supported = (
                field == "required_adapter" and any(isinstance(step, dict) and step.get("op") == "construct_value" and step.get("constructor") == data[field] for step in steps) or
                field == "time_source" and isinstance(data.get("clock"), dict) and data["clock"].get("source") == data[field] or
                field == "required_state" and any(isinstance(step, dict) and step.get("symbol") == data[field] and step.get("op") in {"state_setup", "observe_state"} for step in steps) or
                field == "required_helper" and data[field] in stubs
            )
            if not supported:
                raise ReproductionError(field, reason, "unsupported operation")
    if isinstance(data.get("steps"), list) and data["steps"] and all(isinstance(step, str) for step in data["steps"]):
        return None  # Narrative steps are evidence for the planner, never DSL operations.
    allowed = {"target", "verification_target", "args", "kwargs", "expected", "expected_behavior", "expected_exception",
               "expected_args", "expected_open_resources", "predicate", "steps", "assertions",
               "timeout_ms", "seed", "random_values", "random_boundaries", "files", "repair_plan", "stubs", "clock",
               "required_adapter", "time_source", "required_state", "required_helper"}
    if not set(data) & allowed:
        return None
    if set(data) - allowed:
        raise ReproductionError("reproduction", "structured reproduction contains unsupported operations", "unsupported operation")
    controls = {key: data[key] for key in ("timeout_ms", "seed", "random_values", "random_boundaries", "files", "repair_plan", "stubs", "clock") if key in data}
    expectation = data.get("expected_behavior")
    declared_target = data.get("verification_target", data.get("target", suspected_symbol))
    if "target" in data and "verification_target" in data and data["target"] != data["verification_target"]:
        raise ReproductionError("verification_target", "suspected symbol / verification target mismatch: conflicting target metadata", "target mismatch")
    if "steps" not in data or "verification_target" in data or "target" in data:
        local_name(declared_target, "verification_target")
    if "expected_behavior" in data and not isinstance(expectation, dict):
        raise ReproductionError("expected_behavior", "use a structured return_value, exception, or comparison", "invalid value")
    if "steps" in data:
        if not isinstance(data["steps"], list) or not 1 <= len(data["steps"]) <= 40:
            raise ReproductionError("steps", "verification plans require bounded steps", "invalid value")
        steps = list(data["steps"]) if isinstance(data["steps"], list) else data["steps"]
        assertions = data.get("assertions")
        if assertions is None:
            assertions = expectation_assertions(expectation, steps)
        return {"kind": "verification_plan", "steps": steps, "assertions": assertions,
                "timeout_ms": 500, **({"verification_target": declared_target} if declared_target.isidentifier() else {}), **controls}
    inputs = {key: data[key] for key in ("args", "kwargs") if key in data}
    outcomes = {key: data[key] for key in ("expected", "expected_exception", "expected_args", "expected_open_resources", "predicate") if key in data}
    if expectation is not None:
        if set(expectation) == {"return_value"}:
            if "expected" in outcomes and outcomes["expected"] != expectation["return_value"]:
                raise ReproductionError("expected_behavior", "conflicting return expectations")
            outcomes["expected"] = expectation["return_value"]
        elif set(expectation) == {"exception"}:
            if "expected_exception" in outcomes and outcomes["expected_exception"] != expectation["exception"]:
                raise ReproductionError("expected_behavior", "conflicting exception expectations")
            outcomes["expected_exception"] = expectation["exception"]
        else:
            if outcomes:
                raise ReproductionError("expected_behavior", "conflicting or ambiguous expectation representations")
            outcomes = None
    if not outcomes and expectation is None:
        raise ReproductionError("expected_behavior", "ambiguous expected behavior: an explicit behavioral expectation is required", "invalid value")
    target = declared_target
    explicit_target = target != suspected_symbol
    if outcomes is not None and not explicit_target:
        kind = ("file_resource_check" if "expected_open_resources" in outcomes else
                "mutation_check" if "expected_args" in outcomes else "predicate" if "predicate" in outcomes else
                "deterministic_random" if {"random_values", "random_boundaries"} & controls.keys() else
                "expected_exception" if "expected_exception" in outcomes else "function_call")
        return {"kind": kind, "verification_target": target, **inputs, **outcomes, **controls}
    local_name(target, "target")
    steps = [{"op": "call", "target": target, **inputs, "as": "result"}]
    if expectation is None:
        if "expected" in outcomes:
            expectation = {"return_value": outcomes["expected"]}
        else:
            raise ReproductionError("expected_behavior", "a different target requires an explicit positive return/state expectation", "unsupported operation")
    return {"kind": "verification_plan", "steps": steps, "assertions": expectation_assertions(expectation, steps),
            "timeout_ms": 500, "verification_target": target, **controls}


def expectation_assertions(expectation, steps):
    if not isinstance(expectation, dict):
        raise ReproductionError("expected_behavior", "ambiguous expected behavior: explicit assertions are required", "invalid value")
    if "approx" in expectation:
        if set(expectation) - {"approx", "abs_tol", "rel_tol"} or not ({"abs_tol", "rel_tol"} & set(expectation)):
            raise ReproductionError("expected_behavior", "approximate assertion unavailable: missing structured tolerance", "unsupported operation")
        return [{"source": "return", "op": "approx", "expected": expectation["approx"],
                 **{key: expectation[key] for key in ("abs_tol", "rel_tol") if key in expectation}}]
    if set(expectation) == {"return_value"}:
        return [{"source": "return", "op": "eq", "expected": expectation["return_value"]}]
    if {"field", "expected"} <= set(expectation) and set(expectation) <= {"field", "op", "expected", "abs_tol", "rel_tol"}:
        field_path = expectation["field"]
        if not isinstance(field_path, str) or len(field_path.split(".")) > 3:
            raise ReproductionError("expected_behavior.field", "field path must be bounded", "unsupported operation")
        for part in field_path.split("."):
            if not part.isdecimal():
                local_name(part, "expected_behavior.field")
        if not isinstance(steps, list) or not steps or not isinstance(steps[-1], dict) or not isinstance(steps[-1].get("as"), str):
            raise ReproductionError("steps", "returned-field observation requires a named call result")
        names = {step.get("as") for step in steps if isinstance(step, dict)}
        name = "observed_field"
        while name in names:
            name += "_"
        steps.append({"op": "observe", "target": steps[-1]["as"] + "." + field_path, "as": name})
        return [{"source": name, "op": expectation.get("op", "eq"), "expected": expectation["expected"],
                 **{key: expectation[key] for key in ("abs_tol", "rel_tol") if key in expectation}}]
    if {"source", "op", "expected"} <= set(expectation) and set(expectation) <= {"source", "op", "expected", "abs_tol", "rel_tol"}:
        source = expectation["source"]
        if isinstance(source, str) and "." in source and source not in {"exception.type", "exception.message"}:
            parts = source.split(".")
            if len(parts) > 4:
                raise ReproductionError("expected_behavior.source", "observation path exceeds bounded depth", "unsupported operation")
            for part in parts:
                if not part.isdecimal():
                    local_name(part, "expected_behavior.source")
            names = {step.get("as") for step in steps if isinstance(step, dict)}
            name = "observed_field"
            while name in names:
                name += "_"
            steps.append({"op": "observe", "target": source, "as": name})
            return [{"source": name, "op": expectation["op"], "expected": expectation["expected"],
                     **{key: expectation[key] for key in ("abs_tol", "rel_tol") if key in expectation}}]
        return [dict(expectation)]
    raise ReproductionError("expected_behavior", "expectation is not representable by a supported comparison", "unsupported operation")


def bind_call(node, args, kwargs, field, *, bound=False):
    """Use AST defaults as presence markers; never evaluate default expressions."""
    if node is None:
        if args or kwargs:
            raise ReproductionError(field, "reproduction arguments do not match target signature: constructor takes no arguments", "target mismatch")
        return
    parameters = node.args
    if node.decorator_list or parameters.vararg or parameters.kwarg:
        raise ReproductionError(field, "unsupported callable signature/decorators", "unsupported operation")
    names = parameters.posonlyargs + parameters.args
    if bound and not names:
        raise ReproductionError(field, "unsupported callable: bound method has no receiver parameter", "unsupported operation")
    default_start = len(names) - len(parameters.defaults)
    signature = []
    for index, arg in enumerate(names):
        if bound and index == 0:
            continue
        kind = inspect.Parameter.POSITIONAL_ONLY if index < len(parameters.posonlyargs) else inspect.Parameter.POSITIONAL_OR_KEYWORD
        signature.append(inspect.Parameter(arg.arg, kind, default=None if index >= default_start else inspect.Parameter.empty))
    for arg, default in zip(parameters.kwonlyargs, parameters.kw_defaults):
        signature.append(inspect.Parameter(arg.arg, inspect.Parameter.KEYWORD_ONLY,
                                           default=None if default is not None else inspect.Parameter.empty))
    positional_only = {parameter.name for parameter in signature if parameter.kind == inspect.Parameter.POSITIONAL_ONLY}
    keyword_names = {parameter.name for parameter in signature if parameter.kind != inspect.Parameter.POSITIONAL_ONLY}
    if set(kwargs) & positional_only:
        raise ReproductionError(field + ".kwargs", "reproduction arguments do not match target signature: positional-only arguments cannot be supplied as keywords", "target mismatch")
    if set(kwargs) - keyword_names:
        raise ReproductionError(field + ".kwargs", "reproduction arguments do not match target signature: unexpected keyword argument", "target mismatch")
    try:
        inspect.Signature(signature).bind(*args, **kwargs)
    except TypeError as error:
        reason = str(error)
        if reason.startswith("got an unexpected keyword argument"):
            reason = "unexpected keyword argument"
        raise ReproductionError(field, "reproduction arguments do not match target signature: " + reason, "target mismatch") from None


def validate_targets(source, suspected_symbol, spec, evidence=None):
    """Preflight every external DSL call, never calls inside the project body."""
    if spec["kind"] in {"python_syntax", "module_fragment", "doctest"}:
        return spec
    tree = ast.parse(source)
    definitions = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            definitions[node.name] = node if node.name not in definitions else None
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = node.value if target.id not in definitions else None
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            definitions[node.target.id] = node.value if node.target.id not in definitions else None
        elif isinstance(node, ast.ImportFrom) and not node.level:
            for alias in node.names:
                name = alias.asname or alias.name
                definitions[name] = node if name not in definitions else None

    def resolve(name, field):
        local_name(name, field)
        node = definitions.get(name)
        visited = {name}
        while isinstance(node, ast.Name):
            if node.id in visited or len(visited) >= 32:
                raise ReproductionError(field, "cyclic or excessive local callable alias dependency", "unsupported operation")
            visited.add(node.id)
            node = definitions.get(node.id)
        if isinstance(node, ast.ImportFrom):
            if node.module not in {"math", "statistics", "types", "random"}:
                raise ReproductionError(field, "imported callable is not exposed by the safe dependency policy", "unsupported operation")
            return node  # Worker admits only explicitly allowlisted members.
        if not isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            raise ReproductionError(field, "target is not defined locally or is not an unambiguous local function/class", "unsupported operation")
        if isinstance(node, ast.ClassDef) and (node.bases or node.decorator_list or node.keywords):
            raise ReproductionError(field, "unsupported callable: class inheritance/decorators", "unsupported operation")
        return node

    def method(cls, name, field):
        candidates = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name]
        if not candidates and name == "__init__":
            return None
        if len(candidates) != 1:
            raise ReproductionError(field, "unsupported callable: local method is not unambiguously defined", "unsupported operation")
        return candidates[0]

    def call(node, args, kwargs, field):
        if isinstance(node, ast.ImportFrom):
            return  # Signature and safety are enforced by the trusted worker adapter.
        if isinstance(node, ast.ClassDef):
            bind_call(method(node, "__init__", field), args, kwargs, field, bound=True)
        else:
            bind_call(node, args, kwargs, field)

    verification_target = spec.get("verification_target", suspected_symbol)
    if verification_target != suspected_symbol:
        alternate = resolve(verification_target, "verification_target")
        suspected = definitions.get(suspected_symbol)
        if not isinstance(alternate, (ast.FunctionDef, ast.ClassDef)):
            raise ReproductionError("verification_target", "verification target is not a local function or class", "target mismatch")
        linked = (isinstance(evidence, dict) and evidence.get("reproduction_target", evidence.get("verification_target")) == verification_target)
        if isinstance(suspected, ast.FunctionDef) and isinstance(alternate, ast.FunctionDef):
            linked = linked or any(isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == verification_target for child in ast.walk(suspected))
            linked = linked or any(isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == suspected_symbol for child in ast.walk(alternate))
        if spec["kind"] == "verification_plan":
            call_targets = {step.get("target") for step in spec["steps"] if step["op"] == "call"}
            linked = linked or {suspected_symbol, verification_target} <= call_targets
            if verification_target not in call_targets:
                raise ReproductionError("verification_target", "verification target is not exercised by the supplied plan", "target mismatch")
        if not linked:
            raise ReproductionError("verification_target", "verification target is not evidence-supported by a local call relationship or explicit reproduction evidence", "target mismatch")

    if spec["kind"] == "verification_plan":
        instances = {}
        bound = set()
        direct_targets = set()
        def references(value):
            if isinstance(value, dict):
                if set(value) == {"$ref"}:
                    yield value["$ref"]
                else:
                    for child in value.values():
                        yield from references(child)
            elif isinstance(value, list):
                for child in value:
                    yield from references(child)
        for index, step in enumerate(spec["steps"]):
            field = f"steps[{index}]"
            if step["op"] in {"state_setup", "observe_state"}:
                symbol = step["symbol"]
                node = definitions.get(symbol)
                if not isinstance(node, (ast.Constant, ast.Dict, ast.List, ast.Set)):
                    raise ReproductionError(field + ".symbol", "module state is not safely observable: local literal binding required", "unsupported operation")
            if step["op"] == "construct":
                node = resolve(step["symbol"], field + ".symbol")
                if not isinstance(node, ast.ClassDef):
                    raise ReproductionError(field + ".symbol", "construct requires a local class", "target mismatch")
                call(node, step.get("args", []), step.get("kwargs", {}), field)
                instances[step["as"]] = node
            elif step["op"] == "bind_callable":
                node = resolve(step["symbol"], field + ".symbol")
                if not isinstance(node, (ast.FunctionDef, ast.ImportFrom)):
                    raise ReproductionError(field, "bound callable must be a safe local function", "unsupported operation")
                bound.add(step["as"])
            elif step["op"] == "call":
                target = step["target"]
                if "." in target:
                    parts = target.split(".")
                    if len(parts) != 2 or parts[0] not in instances:
                        raise ReproductionError(field + ".target", "unsupported callable: method owner is not a constructed local instance", "unsupported operation")
                    bind_call(method(instances[parts[0]], parts[1], field), step.get("args", []), step.get("kwargs", {}), field, bound=True)
                else:
                    if target in bound:
                        node = resolve(next(item["symbol"] for item in spec["steps"] if item["op"] == "bind_callable" and item["as"] == target), field + ".target")
                    else:
                        node = resolve(target, field + ".target")
                    call(node, step.get("args", []), step.get("kwargs", {}), field)
                    if isinstance(node, ast.FunctionDef):
                        direct_targets.add(node.name)
            if step["op"] in {"construct_value", "call", "construct"}:
                for value in (step.get("value"), step.get("args"), step.get("kwargs")):
                    for reference in references(value):
                        if not isinstance(reference, str) or reference not in {previous.get("as") for previous in spec["steps"][:index]}:
                            raise ReproductionError(field, "plan reference must name an earlier bound result", "unsupported operation")
        for symbol in spec.get("stubs", {}):
            node = resolve(symbol, "stubs." + symbol)
            if not isinstance(node, ast.FunctionDef) or symbol in direct_targets:
                raise ReproductionError("stubs." + symbol, "stub must replace a local helper, not a directly observed target", "unsupported operation")
            reachable = set(direct_targets)
            frontier = list(direct_targets)
            while frontier and len(reachable) <= 64:
                current = definitions.get(frontier.pop())
                if not isinstance(current, ast.FunctionDef):
                    continue
                for child in ast.walk(current):
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in definitions and child.func.id not in reachable:
                        reachable.add(child.func.id)
                        frontier.append(child.func.id)
            if symbol not in reachable:
                raise ReproductionError("stubs." + symbol, "stubbed helper is not reachable from a selected local call target", "unsupported operation")
        return spec
    node = resolve(verification_target, "verification_target")
    if spec["kind"] == "class_state_check":
        if not isinstance(node, ast.ClassDef):
            raise ReproductionError("target", "class state verification requires a local class", "target mismatch")
        for index, args in enumerate(spec["constructors"]):
            call(node, args, {}, f"constructors[{index}]")
        for index, operation in enumerate(spec["calls"]):
            bind_call(method(node, operation["method"], f"calls[{index}]"), operation.get("args", []), {}, f"calls[{index}]", bound=True)
    else:
        call(node, spec.get("args", []), spec.get("kwargs", {}), "verification_target.args/kwargs")
    return spec
