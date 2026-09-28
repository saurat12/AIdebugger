"""Additional read-only signals; patterns are suspicions, never confirmations."""

import ast
import re
from pathlib import PurePosixPath

from .hunt import BugHypothesis
from .hunt_strategies import modules, functions


def signal(file, symbol, category, description, line, observation, strategy):
    return BugHypothesis(file, symbol, description,
                         {"summary": observation, "locations": [{"file": file, "line": line}], "observations": [observation]},
                         .7, {"approach": strategy, "steps": ["Establish the intended contract", "Reproduce with bounded inputs"],
                              "expected_behavior": "Must be established from project contracts before confirmation"},
                         category, {"kind": "source_review"}, root_cause_key=f"{category}:{line}",
                         finding_kind="observation" if category == "resource_handling" else "bug",
                         behavioral_failure=None if category == "resource_handling" else description)


class StateMutationAnalysis:
    def hunt(self, project):
        result = []
        for file, tree in modules(project):
            for name, function in functions(tree).items():
                for loop in (n for n in ast.walk(function) if isinstance(n, ast.For)):
                    iterated = {n.id for n in ast.walk(loop.iter) if isinstance(n, ast.Name)}
                    for call in (n for statement in loop.body for n in ast.walk(statement) if isinstance(n, ast.Call)):
                        fn = call.func
                        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id in iterated and fn.attr in {"pop", "remove", "clear", "append", "extend"}:
                            result.append(signal(file, name, "state_mutation", "Iteration may be invalidated by collection mutation", call.lineno,
                                                 f"Loop iterates over {fn.value.id} while calling {fn.attr}", "Verify iteration results and post-call collection state"))
        return tuple(result[:50])


class ResourceAnalysis:
    def hunt(self, project):
        result = []
        for file, tree in modules(project):
            for name, function in functions(tree).items():
                managed = {id(n) for node in ast.walk(function) if isinstance(node, ast.With)
                           for item in node.items for n in ast.walk(item.context_expr)}
                for node in ast.walk(function):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open" and id(node) not in managed:
                        result.append(signal(file, name, "resource_handling", "File lifetime may lack guaranteed cleanup", node.lineno,
                                             "open() is used outside a context-manager acquisition; manual cleanup and ownership require review",
                                             "Verify resource ownership and cleanup on normal and exceptional paths using virtual resources"))
        return tuple(result[:50])


class NonterminationAnalysis:
    def hunt(self, project):
        result = []
        for file, tree in modules(project):
            for name, function in functions(tree).items():
                for node in ast.walk(function):
                    if isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value is True and not any(
                        isinstance(child, (ast.Break, ast.Return, ast.Raise)) for child in ast.walk(node)
                    ):
                        result.append(signal(file, name, "nontermination", "Loop has no visible exit", node.lineno,
                                             "Literal while True loop has no syntactic break, return, or raise; called functions and intended behavior remain unverified",
                                             "Establish an expected completion contract, then run a bounded isolated timeout check"))
        return tuple(result[:50])


class CrossModuleAnalysis:
    def hunt(self, project):
        parsed = dict(modules(project))
        result = []
        for file, tree in parsed.items():
            imports = {}
            for node in tree.body:
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                stem = node.module.replace(".", "/")
                if node.level:
                    parent = PurePosixPath(file).parent
                    for _ in range(node.level - 1):
                        parent = parent.parent
                    candidates = (str(parent / (stem + ".py")),)
                else:
                    candidates = (stem + ".py", "src/" + stem + ".py")
                target = next((parsed[c] for c in candidates if c in parsed), None)
                if target is not None:
                    for alias in node.names:
                        callee = functions(target).get(alias.name)
                        if callee is not None:
                            imports[alias.asname or alias.name] = callee
            for name, function in functions(tree).items():
                for node in ast.walk(function):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id not in imports:
                        continue
                    callee = imports[node.func.id]
                    args = callee.args
                    if callee.decorator_list or args.vararg or args.kwarg or args.kwonlyargs or node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
                        continue
                    maximum = len(args.posonlyargs) + len(args.args)
                    if not maximum - len(args.defaults) <= len(node.args) <= maximum:
                        result.append(signal(file, name, "cross_module_consistency", "Imported call disagrees with local source signature", node.lineno,
                                             f"Call to imported {node.func.id} has {len(node.args)} positional arguments; declaration accepts {maximum - len(args.defaults)} to {maximum}",
                                             "Check binding and aliases, then reproduce the call with safe project dependencies"))
        return tuple(result[:50])


class NativeValidationEvidence:
    def hunt(self, project):
        return ()  # Only captured baseline evidence; never rerun commands here.

    def hunt_with_checks(self, project, checks):
        findings = []
        for check in checks:
            if check.passed:
                continue
            text = (check.stderr + "\n" + check.stdout).strip()[:12000]
            paths = re.findall(r"(?:[\w.-]+[/\\])*[\w.-]+\.(?:py|js|ts|java|go|rs)\b", text)
            file = next((p.replace("\\", "/") for p in paths if (project.root / p).is_file()
                         and (project.root / p).resolve().is_relative_to(project.root.resolve())), "<project>")
            findings.append(BugHypothesis(file, check.name, "Project-native check could not pass: " + check.name,
                                          {"summary": check.blocked_reason or "Captured baseline validation failure",
                                           "observations": [text], "check": check.name, "command": list(check.command), "status": check.status},
                                          .5, {"approach": "Investigate captured native check evidence",
                                               "steps": ["Separate environment/setup issues from application assertions", "Establish a targeted reproduction"],
                                               "expected_behavior": "The declared check completes successfully"},
                                          "project_validation", {"kind": "native_evidence"}))
        return tuple(findings)
