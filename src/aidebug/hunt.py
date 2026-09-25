"""Proactive hypotheses, independent verification, and gated isolated repairs."""

import ast
import doctest
import json
import argparse
import math
import operator
import hashlib
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Literal, Protocol

from .agent import DebugOrchestrator
from .code_tools import CodeTools
from .models import CheckResult, DebugRun, ProjectInfo, ValidationReport
from .openai_agent import OpenAIAgent, _json_object
from .runner import run_checks
from .workspace import isolated_workspace


@dataclass(frozen=True)
class BugHypothesis:
    suspected_file: str
    suspected_symbol: str
    description: str
    evidence: str
    confidence: float
    reproduction_strategy: str
    category: str = "llm_review"
    reproduction: dict | None = None


@dataclass(frozen=True)
class Finding:
    hypothesis: BugHypothesis
    status: Literal["confirmed", "high_confidence", "unconfirmed", "rejected"]
    evidence: str
    check: CheckResult | None = None

    @property
    def finding_id(self) -> str:
        data = json.dumps(asdict(self.hypothesis), sort_keys=True)
        return "hunt_" + hashlib.sha256(data.encode()).hexdigest()[:16]

    def record(self) -> dict:
        hypothesis = self.hypothesis
        return dict(finding_id=self.finding_id, file=hypothesis.suspected_file,
                    symbol=hypothesis.suspected_symbol, category=hypothesis.category,
                    hypothesis=hypothesis.description, evidence=hypothesis.evidence,
                    confidence=hypothesis.confidence, reproduction_strategy=hypothesis.reproduction_strategy,
                    verification_status=self.status, verification_evidence=self.evidence,
                    status=self.status, reproduction=hypothesis.reproduction,
                    check=asdict(self.check) if self.check else None)


@dataclass(frozen=True)
class HuntRun:
    existing_checks: tuple[CheckResult, ...]
    findings: tuple[Finding, ...]
    repairs: tuple[DebugRun, ...]
    mode: str = "full"
    strategies: tuple[str, ...] = ()
    findings_path: Path | None = None
    report_path: Path | None = None
    repair_errors: tuple[str, ...] = ()

    def record(self) -> dict:
        return {**asdict(self), "findings": [finding.record() for finding in self.findings]}


class DetectionStrategy(Protocol):
    def hunt(self, project: ProjectInfo) -> tuple[BugHypothesis, ...]: ...


class VerificationStrategy(Protocol):
    def verify(self, project: ProjectInfo, hypothesis: BugHypothesis) -> Finding: ...


class BugHunter:
    def __init__(self, agent: OpenAIAgent, quick: bool = False) -> None:
        self.agent = agent
        self.quick = quick

    def hunt(self, project: ProjectInfo) -> tuple[BugHypothesis, ...]:
        tools = CodeTools(project.root)
        limit = 12 if self.quick else 80
        excerpts = []
        budget = 16_000 if self.quick else 80_000
        for index, path in enumerate(tools._files()):
            if index >= limit or budget <= 0:
                break
            relative = path.relative_to(project.root.resolve()).as_posix()
            excerpt = f"FILE: {relative}\n" + tools.read_file(relative, end_line=100)
            excerpts.append(excerpt[:budget])
            budget -= len(excerpts[-1])
        response = self.agent._complete(
            """You are the Bug Hunter. Inspect source using the read-only code tools, even if tests pass.
Find concrete behavioral defects, not style issues. Do not propose patches.
Return JSON: {"hypotheses": [...]} with at most 10 hypotheses, or an empty list.
Each hypothesis must contain suspected_file (project-relative), suspected_symbol,
description, evidence, confidence (0 to 1), reproduction_strategy.
Ground evidence in inspected source and declared behavior. Inspect docstrings and
doctest examples when present; these can be independently verified. Do not invent
expected results or claim that a hypothesis is confirmed.""",
            ("Quick review: prioritize obvious defects.\n" if self.quick else
             "Deep review: also examine boundaries, invariants, exceptions, test gaps, and cross-function contracts.\n")
            + "Source excerpts (bounded; use read tools for more):\n" + "\n\n".join(excerpts), project.root,
        )
        data = _json_object(response)
        items = data.get("hypotheses")
        if not isinstance(items, list) or len(items) > 10:
            raise ValueError("Invalid Hunter response: hypotheses must be a list of at most 10 entries")
        hypotheses = []
        for item in items:
            fields = ("suspected_file", "suspected_symbol", "description", "evidence", "reproduction_strategy")
            if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key].strip() for key in fields):
                raise ValueError("Invalid Hunter response: missing hypothesis text fields")
            confidence = item.get("confidence")
            if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("Invalid Hunter response: confidence must be between 0 and 1")
            _source_path(project.root, item["suspected_file"])
            hypotheses.append(BugHypothesis(**{key: item[key] for key in fields}, confidence=confidence))
        return tuple(hypotheses)


def _source_path(root: Path, name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or PureWindowsPath(name).drive or ".." in path.parts:
        raise ValueError("Hypothesis path must be project-relative")
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        raise ValueError("Hypothesis source must be a file inside the project")
    return candidate


class UnsupportedEvidence(ValueError):
    pass


def _expression(node: ast.AST, values: dict):
    """Interpret bounded scalar arithmetic only; never import or execute source."""
    if isinstance(node, ast.Constant) and type(node.value) in (int, float, bool, type(None)):
        value = node.value
    elif isinstance(node, ast.Name) and node.id in values:
        value = values[node.id]
    elif isinstance(node, ast.BinOp) and type(node.op) in (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod):
        operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                      ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
        value = operations[type(node.op)](_expression(node.left, values), _expression(node.right, values))
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
        operation = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Not: operator.not_}[type(node.op)]
        value = operation(_expression(node.operand, values))
    elif isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE):
        operations = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}
        value = operations[type(node.ops[0])](_expression(node.left, values), _expression(node.comparators[0], values))
    else:
        raise UnsupportedEvidence()
    if type(value) not in (int, float, bool, type(None)) or (isinstance(value, (int, float)) and abs(value) > 10**12):
        raise UnsupportedEvidence()
    return value


def _body(statements: list[ast.stmt], values: dict):
    for statement in statements:
        if isinstance(statement, ast.Return):
            return True, _expression(statement.value, values) if statement.value else None
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            continue
        if isinstance(statement, ast.If):
            returned, value = _body(statement.body if _expression(statement.test, values) else statement.orelse, values)
            if returned:
                return True, value
        elif isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
            values[statement.targets[0].id] = _expression(statement.value, values)
        else:
            raise UnsupportedEvidence()
    return False, None


class DoctestVerifier:
    """Verify declared numeric doctests for simple, undecorated Python functions."""

    def verify(self, project: ProjectInfo, hypothesis: BugHypothesis) -> Finding:
        pending = "high_confidence" if hypothesis.confidence >= 0.85 else "unconfirmed"
        try:
            path = _source_path(project.root, hypothesis.suspected_file)
            if path.suffix != ".py" or path.stat().st_size > 120_000:
                raise UnsupportedEvidence()
            module = ast.parse(path.read_text(encoding="utf-8"))
            if sum(1 for _ in ast.walk(module)) > 5000:
                raise UnsupportedEvidence()
            functions = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == hypothesis.suspected_symbol]
            if len(functions) != 1:
                raise UnsupportedEvidence()
            function = functions[0]
            args = function.args
            if function.decorator_list or args.defaults or args.kwonlyargs or args.vararg or args.kwarg:
                raise UnsupportedEvidence()
            examples = doctest.DocTestParser().get_examples(ast.get_docstring(function) or "")
            if not examples or len(examples) > 20:
                raise UnsupportedEvidence()
            matched = 0
            for example in examples:
                if example.options or example.exc_msg:
                    raise UnsupportedEvidence()
                call = ast.parse(example.source.strip(), mode="eval").body
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != function.name or call.keywords:
                    raise UnsupportedEvidence()
                names = [arg.arg for arg in args.posonlyargs + args.args]
                if len(names) != len(call.args):
                    raise UnsupportedEvidence()
                values = dict(zip(names, (_expression(arg, {}) for arg in call.args)))
                expected = _expression(ast.parse(example.want.strip(), mode="eval").body, {})
                try:
                    _, actual = _body(function.body, values)
                    correct = actual == expected
                    observed = repr(actual)
                except (ZeroDivisionError, OverflowError):
                    correct, observed = False, "arithmetic exception"
                if not correct:
                    evidence = f"{hypothesis.suspected_file}:{function.lineno}: declared doctest expected {expected!r}; observed {observed}."
                    check = CheckResult("hunt:doctest", ("safe-doctest", hypothesis.suspected_file, function.name), 1, "", "AssertionError: " + evidence, 0)
                    return Finding(hypothesis, "confirmed", evidence, check)
                matched += 1
            check = CheckResult("hunt:doctest", ("safe-doctest", hypothesis.suspected_file, function.name), 0, f"{matched} declared examples matched", "", 0)
            return Finding(hypothesis, "rejected", "Declared reproduction examples matched; this strategy did not reproduce the suspected bug.", check)
        except (ValueError, OSError, UnicodeError, TypeError, RecursionError, ArithmeticError):
            return Finding(hypothesis, pending, "No supported independent reproduction: this verifier requires bounded scalar Python doctests. No repair authorized.")


class VerifiedRepairValidator:
    def __init__(self, verifier: VerificationStrategy, hypothesis: BugHypothesis, timeout: float, original: ProjectInfo):
        self.verifier, self.hypothesis, self.timeout = verifier, hypothesis, timeout
        self.contract = self._contract(original)

    def _contract(self, project: ProjectInfo):
        if hasattr(self.verifier, "contract"):
            return self.verifier.contract(project, self.hypothesis)
        if not isinstance(self.verifier, DoctestVerifier):
            return None
        try:
            tree = ast.parse(_source_path(project.root, self.hypothesis.suspected_file).read_text(encoding="utf-8"))
            return tuple(ast.get_docstring(node) for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == self.hypothesis.suspected_symbol)
        except (OSError, ValueError):
            return ()

    def validate(self, project: ProjectInfo) -> ValidationReport:
        results = run_checks(project, self.timeout, stop_on_failure=False)
        finding = self.verifier.verify(project, self.hypothesis)
        reproduction = finding.check or CheckResult("hunt:doctest", ("safe-doctest",), 1, "", "Reproduction could not be verified after repair", 0)
        if self._contract(project) != self.contract:
            reproduction = replace(reproduction, returncode=1, stderr="Declared reproduction contract changed; repair cannot be validated")
        results = (*results, reproduction)
        return ValidationReport(all(result.passed for result in results), results, project.root)


def hunt_project(project: ProjectInfo, detectors: tuple[DetectionStrategy, ...], verifier: VerificationStrategy,
                 agent: OpenAIAgent, timeout: float = 120, quick: bool = False) -> HuntRun:
    findings = []
    # Even existing checks run on a copy in hunt mode.
    with isolated_workspace(project.root) as workspace:
        isolated = replace(project, root=workspace)
        results = run_checks(isolated, timeout, stop_on_failure=False)
        for detector in detectors:
            for hypothesis in detector.hunt(isolated):
                finding = verifier.verify(isolated, hypothesis)
                if not any(previous.finding_id == finding.finding_id for previous in findings):
                    findings.append(finding)
    repairs = []
    errors = []
    for finding in findings:
        if finding.status != "confirmed" or finding.check is None or finding.check.passed:
            continue
        validator = VerifiedRepairValidator(verifier, finding.hypothesis, timeout, project)
        try:
            repairs.append(DebugOrchestrator(agent, agent, validator).run(project, finding.check))
        except Exception as exc:
            errors.append(f"{finding.finding_id}: repair failed ({type(exc).__name__}); no validated repair recorded")
    from .hunt_reports import save_hunt_report
    run = HuntRun(results, tuple(findings), tuple(repairs), "quick" if quick else "full",
                  ("existing_checks", *(type(detector).__name__ for detector in detectors)), repair_errors=tuple(errors))
    return save_hunt_report(project.root, run)


def hunt_main(argv: list[str]) -> int:
    from .cli import _safe_agent_error
    from .credentials import resolve_api_key
    from .discovery import discover_repository

    parser = argparse.ArgumentParser(prog="aidebug hunt", description="Inspect source and independently verify bug hypotheses before repair.")
    parser.add_argument("path", nargs="?", type=Path, default=Path("."))
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--quick", action="store_true", help="Use fewer source excerpts, lightweight static analysis, and basic verification only")
    args = parser.parse_args(argv)
    api_key = ""
    try:
        project = discover_repository(args.path)
        api_key = resolve_api_key() or ""
        if not api_key:
            parser.error("Hunting requires an API key. Run aidebug configure or set OPENAI_API_KEY.")
        agent = OpenAIAgent(model=args.model)
        from .hunt_strategies import build_detectors, HuntVerifier
        run = hunt_project(project, build_detectors(agent, args.quick), HuntVerifier(args.quick), agent, args.timeout, quick=args.quick)
    except Exception as exc:
        parser.error(_safe_agent_error(exc, api_key))
    if args.as_json:
        print(json.dumps(run.record(), default=str, indent=2))
    else:
        print("Existing checks (isolated workspace):")
        for result in run.existing_checks:
            print(f"[{'PASS' if result.passed else 'FAIL'}] {result.name}")
        print("\nProactive findings:")
        if not run.findings:
            print("No hypotheses returned; this does not establish that the project is bug-free.")
        for finding in run.findings:
            print(f"[{finding.status}] {finding.hypothesis.suspected_file}: {finding.hypothesis.suspected_symbol}: {finding.hypothesis.description}")
            print(finding.evidence)
        print(f"\nConfirmed bugs: {sum(f.status == 'confirmed' for f in run.findings)}")
        print(f"Rejected hypotheses: {sum(f.status == 'rejected' for f in run.findings)}")
        for repair in run.repairs:
            passed = repair.validation is not None and repair.validation.passed
            print(f"\nProactive repair: {'validated' if passed else 'not validated'} after {repair.attempts} attempt(s).")
            if repair.validated_patch_path:
                print(f"Validated patch saved to:\n{repair.validated_patch_path}")
                print(f"Debug report saved to:\n{repair.debug_report_path}")
        for error in run.repair_errors:
            print(error)
        print(f"Hunt mode: {run.mode}. Evidence is bounded; unsupported behavior remains unconfirmed.")
        if run.findings_path:
            print(f"Hunt findings saved to:\n{run.findings_path}\nHunt report saved to:\n{run.report_path}")
    return int(any(not result.passed for result in run.existing_checks) or any(finding.status == "confirmed" for finding in run.findings))
