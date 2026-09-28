"""Bounded Python detection and independent, non-executing verification strategies.

Generated cases are data evaluated by a restricted AST interpreter in a source
copy. They never become executable model-generated scripts.
"""

import ast
import itertools
import json
import re

from .code_tools import CodeTools
from .hunt import (BugHunter, BugHypothesis, DoctestVerifier, Finding, UnsupportedEvidence,
                   _body, _expression, _source_path)
from .models import CheckResult


def modules(project):
    budget = 1_000_000
    for index, path in enumerate(CodeTools(project.root)._files()):
        if index >= 300 or budget <= 0:
            break
        if path.suffix != ".py" or path.stat().st_size > 120_000:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            budget -= len(source)
            tree = ast.parse(source)
            if sum(1 for _ in ast.walk(tree)) <= 5000:
                yield path.relative_to(project.root.resolve()).as_posix(), tree
        except (OSError, UnicodeError, SyntaxError, RecursionError):
            continue


def functions(tree):
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def hypothesis(file, symbol, category, description, evidence, reproduction=None, confidence=0.7):
    return BugHypothesis(file, symbol, description, evidence, confidence,
                         "Recheck source evidence" if reproduction is None else json.dumps(reproduction, sort_keys=True),
                         category, reproduction)


class StaticAnalysis:
    def hunt(self, project):
        findings = []
        for file, tree in modules(project):
            for name, function in functions(tree).items():
                if any(isinstance(value, (ast.List, ast.Dict, ast.Set)) for value in function.args.defaults):
                    findings.append(hypothesis(file, name, "static_analysis", "Mutable default may leak state between calls",
                                               f"Line {function.lineno}: mutable argument default", {"kind": "mutable_default"}, 0.9))
                for node in ast.walk(function):
                    if isinstance(node, ast.Dict):
                        keys = [key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, (str, int))]
                        if len(keys) != len(set(keys)):
                            findings.append(hypothesis(file, name, "static_analysis", "Duplicate literal dictionary key discards an entry",
                                                       f"Line {node.lineno}: repeated dictionary key", {"kind": "duplicate_key"}, 0.9))
                            break
        return tuple(findings[:50])


class CoverageGapAnalysis:
    def _static_gaps(self, project):
        parsed = list(modules(project))
        tested_symbols = set()
        tests_found = False
        for file, tree in parsed:
            if file.startswith(("tests/", "test/")) or file.split("/")[-1].startswith("test_"):
                tests_found = True
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        if isinstance(node.func, ast.Name):
                            tested_symbols.add(node.func.id)
                        elif isinstance(node.func, ast.Attribute):
                            tested_symbols.add(node.func.attr)
        findings = []
        for file, tree in parsed:
            if file.startswith(("tests/", "test/")) or file.split("/")[-1].startswith("test_"):
                continue
            for name, function in functions(tree).items():
                if name not in tested_symbols and ">>>" not in (ast.get_docstring(function) or ""):
                    findings.append(hypothesis(file, name, "coverage_gap", "No direct test reference found in inspected Python tests",
                                               "Static test-reference heuristic only; indirect calls may cover this symbol. " +
                                               ("Test files inspected." if tests_found else "No Python test files found in the bounded scan."),
                                               {"kind": "coverage_gap"}, 0.4))
        return tuple(findings[:50])

    def hunt(self, project):
        # Consume coverage.py JSON if the project's checks export it. Never claim
        # static references constitute measured runtime coverage.
        try:
            path = _source_path(project.root, "coverage.json")
            if path.stat().st_size > 2_000_000:
                return self._static_gaps(project)
            data = json.loads(path.read_text(encoding="utf-8"))
            records = data.get("files", {})
            if not isinstance(records, dict):
                return self._static_gaps(project)
        except (ValueError, OSError, UnicodeError):
            return self._static_gaps(project)
        findings = []
        for file, tree in modules(project):
            record = records.get(file, records.get(file.replace("/", "\\"), {}))
            missing = record.get("missing_lines", []) if isinstance(record, dict) else []
            if not isinstance(missing, list):
                continue
            for name, function in functions(tree).items():
                gaps = [line for line in missing if type(line) is int and function.lineno <= line <= function.end_lineno]
                if gaps:
                    findings.append(hypothesis(file, name, "coverage_gap", "Recorded coverage leaves this function partly untested",
                                               f"coverage.json missing lines: {gaps[:20]}; report freshness is not established", {"kind": "coverage_gap"}, 0.5))
        return tuple(findings[:50])


def contract(function):
    doc = ast.get_docstring(function) or ""
    invariant = re.search(r"(?m)^\s*aidebug invariant:\s*(.+)$", doc)
    equivalent = re.search(r"(?m)^\s*aidebug equivalent:\s*([A-Za-z_]\w*)\s*$", doc)
    return {"invariant": invariant.group(1) if invariant else None,
            "equivalent": equivalent.group(1) if equivalent else None,
            "total": bool(re.search(r"(?m)^\s*aidebug total\s*$", doc))}


def execute(function, args):
    parameters = function.args
    names = [arg.arg for arg in parameters.posonlyargs + parameters.args]
    if (function.decorator_list or parameters.defaults or parameters.kwonlyargs or parameters.vararg
            or parameters.kwarg or len(names) != len(args)):
        raise UnsupportedEvidence()
    return _body(function.body, dict(zip(names, args)))[1]


def cases(function):
    names = function.args.posonlyargs + function.args.args
    if len(names) > 2:
        return ()
    values = {-2, -1, 0, 1, 2}
    for node in ast.walk(function):
        if isinstance(node, ast.Constant) and type(node.value) is int and abs(node.value) <= 1000:
            values.update((node.value - 1, node.value, node.value + 1))
    scalar = sorted(values, key=lambda value: (abs(value), value))[:12]
    domains = []
    for parameter in names:
        name = parameter.arg
        used_as_collection = any(
            (isinstance(node, ast.For) and isinstance(node.iter, ast.Name) and node.iter.id == name)
            or (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == name)
            or (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"len", "sum", "min", "max", "all", "any", "list", "tuple", "set"}
                and any(isinstance(arg, ast.Name) and arg.id == name for arg in node.args))
            for node in ast.walk(function))
        used_as_mapping = any(isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == name
                              and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)
                              for node in ast.walk(function))
        domain = list(scalar)
        if used_as_collection:
            domain += [[], [0], [0, 0], [1], [""]]
        if used_as_mapping:
            keys = {node.slice.value for node in ast.walk(function) if isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name) and node.value.id == name
                    and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)}
            domain += [{}, *[{key: 0} for key in sorted(keys)]]
        # Preserve candidate order while avoiding duplicate values.
        unique = []
        for candidate in domain:
            if candidate not in unique:
                unique.append(candidate)
        domains.append(unique)
    return tuple(itertools.islice(itertools.product(*domains), 64))


def evaluate_case(function, funcs, args, kind):
    declarations = contract(function)
    try:
        result = execute(function, args)
    except (ZeroDivisionError, OverflowError):
        return ("confirmed" if declarations["total"] or declarations["invariant"] else "high_confidence",
                f"Generated input {args!r} raises an arithmetic exception" +
                (" contrary to the declared contract" if declarations["total"] or declarations["invariant"] else "; valid input domain is not declared"))
    if kind == "invariant" and declarations["invariant"]:
        names = function.args.posonlyargs + function.args.args
        values = dict(zip((arg.arg for arg in names), args)) | {"result": result}
        passed = _expression(ast.parse(declarations["invariant"], mode="eval").body, values)
        if type(passed) is not bool:
            raise UnsupportedEvidence()
        return ("rejected" if passed else "confirmed", f"Input {args!r}, result {result!r}; declared invariant evaluates to {passed}")
    if kind == "equivalent" and declarations["equivalent"] in funcs:
        expected = execute(funcs[declarations["equivalent"]], args)
        return ("rejected" if result == expected else "confirmed", f"Input {args!r}: result {result!r}, declared equivalent function result {expected!r}")
    return "rejected", f"Input {args!r} returns {result!r}; no exception reproduced"


class GeneratedEdgeCases:
    category = "generated_edge_case"
    kind = "edge"

    def hunt(self, project):
        findings = []
        for file, tree in modules(project):
            funcs = functions(tree)
            for name, function in funcs.items():
                if self.kind == "invariant" and not contract(function)["invariant"]:
                    continue
                if self.kind == "equivalent" and not contract(function)["equivalent"]:
                    continue
                for args in cases(function):
                    try:
                        status, evidence = evaluate_case(function, funcs, args, self.kind)
                    except (ValueError, TypeError, ArithmeticError, RecursionError):
                        continue
                    if status != "rejected":
                        trigger = json.dumps(args, sort_keys=True, ensure_ascii=True)
                        findings.append(hypothesis(file, name, self.category, "Generated case exposes a behavioral inconsistency",
                                                   evidence, {"kind": self.kind, "args": list(args)}, 0.9 if status == "confirmed" else 0.8))
                        findings[-1] = BugHypothesis(**{**findings[-1].__dict__, "root_cause_key": f"{file}:{name}:{trigger}"})
                        if self.kind == "equivalent":
                            break  # One violated equivalence is one root cause; other detectors retain distinct faults.
                if len(findings) >= 50:
                    return tuple(findings)
        return tuple(findings)


class PropertyChecks(GeneratedEdgeCases):
    category = "property_invariant"
    kind = "invariant"


class CrossFunctionChecks(GeneratedEdgeCases):
    category = "cross_function_consistency"
    kind = "equivalent"

    def hunt(self, project):
        findings = list(super().hunt(project))
        for file, tree in modules(project):
            funcs = functions(tree)
            for name, function in funcs.items():
                if bad_local_call(function, funcs):
                    findings.append(hypothesis(file, name, self.category, "Local call argument count disagrees with the callee signature",
                                               f"Line {function.lineno}: positional call cannot bind to declared local function",
                                               {"kind": "call_arity"}, 0.9))
        return tuple(findings[:50])


def bad_local_call(function, funcs):
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id not in funcs:
            continue
        callee = funcs[node.func.id]
        args = callee.args
        if callee.decorator_list or args.vararg or args.kwarg or args.kwonlyargs or node.keywords or any(isinstance(arg, ast.Starred) for arg in node.args):
            continue
        maximum = len(args.posonlyargs) + len(args.args)
        if not maximum - len(args.defaults) <= len(node.args) <= maximum:
            return True
    return False


class ExceptionPathAnalysis:
    def hunt(self, project):
        findings = []
        for file, tree in modules(project):
            for name, function in functions(tree).items():
                if any(isinstance(node, ast.ExceptHandler) and node.type is None for node in ast.walk(function)):
                    findings.append(hypothesis(file, name, "exception_path", "Bare exception handler may hide unexpected failures",
                                               f"Line {function.lineno}: function contains a bare except", {"kind": "bare_except"}))
        return tuple(findings[:50])


class LegacyVerifier(DoctestVerifier):
    def __init__(self, quick=False):
        self.quick = quick

    def contract(self, project, item):
        try:
            tree = ast.parse(_source_path(project.root, item.suspected_file).read_text(encoding="utf-8"))
            # Pin all declarations, including the reference for equivalence checks.
            return tuple((name, ast.get_docstring(function), ast.dump(function.args)) for name, function in functions(tree).items())
        except (ValueError, OSError):
            return ()

    def prepare_repair(self, project, item):
        kind = (item.reproduction or {}).get("kind")
        # These adapters compare declared invariant/example behavior after the
        # change; their independently failing oracle is also the repair oracle.
        if not self.quick and kind in ("edge", "invariant", "equivalent"):
            return item if self.contract(project, item) else None
        return None

    def verify(self, project, item):
        if item.verification_spec is not None:
            from .verification import StructuredVerifier
            return StructuredVerifier().verify(project, item)
        reproduction = item.reproduction or {}
        kind = reproduction.get("kind")
        if kind == "doctest":
            raise UnsupportedEvidence("doctest must be selected through the registered explicit adapter")
        if kind is None:
            raise UnsupportedEvidence("no explicit verification adapter selected")
        if kind == "coverage_gap":
            return Finding(item, "unconfirmed", "A coverage/test-reference gap is not proof of a bug. Recorded coverage may be stale; static references may miss indirect calls.")
        try:
            tree = ast.parse(_source_path(project.root, item.suspected_file).read_text(encoding="utf-8"))
            funcs = functions(tree)
            function = funcs[item.suspected_symbol]
            if kind in ("mutable_default", "duplicate_key", "bare_except", "call_arity"):
                if kind == "mutable_default":
                    exists = any(isinstance(value, (ast.List, ast.Dict, ast.Set)) for value in function.args.defaults)
                elif kind == "bare_except":
                    exists = any(isinstance(node, ast.ExceptHandler) and node.type is None for node in ast.walk(function))
                elif kind == "call_arity":
                    exists = bad_local_call(function, funcs)
                else:
                    exists = False
                    for node in ast.walk(function):
                        if isinstance(node, ast.Dict):
                            keys = [key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, (str, int))]
                            exists |= len(keys) != len(set(keys))
                return Finding(item, "high_confidence" if exists else "rejected",
                               "Static pattern independently reproduced; intended behavior is not established, so no automatic repair." if exists else "Static pattern was not reproduced.")
            if self.quick or kind not in ("edge", "invariant", "equivalent"):
                raise UnsupportedEvidence()
            args = reproduction.get("args")
            if not isinstance(args, list) or len(args) > 2 or any(type(value) not in (int, float, bool) or abs(value) > 10**6 for value in args):
                raise UnsupportedEvidence()
            status, evidence = evaluate_case(function, funcs, tuple(args), kind)
            # A removed contract is not a passing reproduction.
            if kind in ("invariant", "equivalent") and not contract(function)[kind]:
                raise UnsupportedEvidence()
            if status == "rejected":
                verified = 1
                for generated in cases(function):
                    if generated == tuple(args):
                        continue
                    status, evidence = evaluate_case(function, funcs, generated, kind)
                    verified += 1
                    if status != "rejected":
                        break
                if status == "rejected":
                    evidence = f"{verified} bounded generated cases matched the declared checks; no counterexample reproduced."
            check = CheckResult("hunt:" + kind, ("bounded-generated-case", item.suspected_file, item.suspected_symbol),
                                0 if status == "rejected" else 1, evidence if status == "rejected" else "",
                                "" if status == "rejected" else "AssertionError: " + evidence, 0)
            return Finding(item, status, evidence, check)
        except (ValueError, KeyError, OSError, TypeError, ArithmeticError, RecursionError):
            return Finding(item, "unconfirmed", "Independent check is unsupported or no longer reproducible; no repair authorized.")


from .hunt_registry import RegistryVerifier


class HuntVerifier(RegistryVerifier):
    """Compatibility entry point backed by registered verifier capabilities."""


def build_detectors(agent, quick=False):
    from .hunt_registry import detector_registry
    return detector_registry().build(agent, quick)
