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
from .validation import capture, evaluate, syntax_check, syntax_rejection, summary
from .workspace import InvalidUnifiedDiffError, PatchApplicabilityError, isolated_workspace


@dataclass(frozen=True)
class BugHypothesis:
    suspected_file: str
    suspected_symbol: str
    description: str
    evidence: str | dict
    confidence: float
    reproduction_strategy: str | dict
    category: str = "llm_review"
    reproduction: dict | None = None
    verification_spec: dict | None = None
    root_cause_key: str | None = None
    finding_kind: str = "bug"
    behavioral_failure: str | None = None
    verification_plan: dict | None = None
    verification_unsupported: str | None = None
    verification_target: str | None = None


@dataclass(frozen=True)
class Finding:
    hypothesis: BugHypothesis
    status: Literal["confirmed", "high_confidence", "unconfirmed", "rejected"]
    evidence: str
    check: CheckResult | None = None
    signals: tuple[dict, ...] = ()
    verification_plan: dict | None = None
    repairability: str | None = None

    @property
    def is_bug(self):
        from .finding_scope import is_bug
        return is_bug(self)

    @property
    def finding_id(self) -> str:
        hypothesis = self.hypothesis
        if hypothesis.root_cause_key:
            identity = {"file": hypothesis.suspected_file.replace("\\", "/"),
                        "symbol": hypothesis.suspected_symbol,
                        "root_cause_key": " ".join(hypothesis.root_cause_key.casefold().split())}
            data = json.dumps(identity, sort_keys=True)
            return "hunt_" + hashlib.sha256(data.encode()).hexdigest()[:16]
        data = json.dumps(asdict(self.hypothesis), sort_keys=True)
        return "hunt_" + hashlib.sha256(data.encode()).hexdigest()[:16]

    @property
    def verification_state(self):
        if not self.is_bug:
            return "NON_BUG_OBSERVATION"
        if self.status == "rejected":
            return "REJECTED"
        if self.status != "confirmed":
            return "UNVERIFIABLE"
        if self.repairability == "authorized":
            return "CONFIRMED_AND_REPAIRABLE"
        if self.repairability == "blocked_expected_behavior_unknown":
            return "CONFIRMED_BUT_EXPECTED_BEHAVIOR_UNKNOWN"
        return "CONFIRMED_DEFECT"

    @property
    def repair_authorized(self):
        return self.verification_state == "CONFIRMED_AND_REPAIRABLE"

    def record(self) -> dict:
        hypothesis = self.hypothesis
        return dict(finding_id=self.finding_id, file=hypothesis.suspected_file,
                    symbol=hypothesis.suspected_symbol, suspected_symbol=hypothesis.suspected_symbol,
                    verification_target=(hypothesis.verification_target or (self.verification_plan or {}).get("verification_target") or
                                         (hypothesis.verification_plan.get("verification_target") if isinstance(hypothesis.verification_plan, dict) else None) or
                                         (hypothesis.verification_spec.get("verification_target") if isinstance(hypothesis.verification_spec, dict) else None) or
                                         (hypothesis.reproduction.get("verification_target", hypothesis.reproduction.get("target"))
                                          if isinstance(hypothesis.reproduction, dict) else None) or hypothesis.suspected_symbol),
                    category=hypothesis.category,
                    hypothesis=hypothesis.description, evidence=hypothesis.evidence,
                    confidence=hypothesis.confidence, reproduction_strategy=hypothesis.reproduction_strategy,
                    verification_status=self.status, verification_evidence=self.evidence,
                    status=self.status, reproduction=hypothesis.reproduction, verification_spec=hypothesis.verification_spec,
                    check=asdict(self.check) if self.check else None, signals=list(self.signals),
                    root_cause_key=hypothesis.root_cause_key, finding_kind="bug" if self.is_bug else "observation",
                    behavioral_failure=hypothesis.behavioral_failure, verification_plan=self.verification_plan,
                    verification_state=self.verification_state,
                    repair_authorization="NOT_APPLICABLE" if not self.is_bug else "AUTHORIZED" if self.repair_authorized else "BLOCKED",
                    repairability=self.repairability)


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
    artifact_error: str | None = None
    include_quality: bool = False

    @property
    def bug_findings(self):
        return tuple(f for f in self.findings if f.is_bug)

    @property
    def observations(self):
        return tuple(f for f in self.findings if not f.is_bug)

    def record(self) -> dict:
        record = {**asdict(self), "findings": [finding.record() for finding in self.bug_findings], "metrics": self.metrics,
                  "bug_metrics": self.bug_metrics, "verification_metrics": self.verification_metrics}
        if self.include_quality:
            record["quality_observations"] = [finding.record() for finding in self.observations]
        return record

    @property
    def metrics(self):
        return {"bugs_discovered": len(self.bug_findings),
                "bugs_confirmed": sum(f.status == "confirmed" for f in self.bug_findings),
                "bugs_repairable": sum(f.repair_authorized for f in self.bug_findings),
                "bugs_blocked_by_unspecified_expected_behavior": sum(f.verification_state == "CONFIRMED_BUT_EXPECTED_BEHAVIOR_UNKNOWN" for f in self.bug_findings),
                "bugs_unverifiable": sum(f.status in ("unconfirmed", "high_confidence") for f in self.bug_findings),
                "bugs_rejected": sum(f.status == "rejected" for f in self.bug_findings),
                "non_bug_observations": len(self.observations)}

    @property
    def bug_metrics(self):
        return self.metrics

    @property
    def verification_metrics(self):
        planned = [f for f in self.bug_findings if f.verification_plan and f.verification_plan.get("plan")]
        validations = [repair.validation for repair in self.repairs if repair.validation]
        unsupported = [f for f in self.bug_findings if (f.verification_plan and f.verification_plan.get("unsupported_reason"))
                       or "unverifiable:" in f.evidence.casefold() or "unsupported" in f.evidence.casefold()]
        return {"verification_coverage": len(planned) / len(self.bug_findings) if self.bug_findings else 1.0,
                "verification_plans_generated": len(planned),
                "verification_plans_executed": sum(f.status in ("confirmed", "rejected") for f in planned),
                "cached_plan_conclusions": sum(f.status in ("confirmed", "rejected") and
                    (f.verification_plan or {}).get("source") == "validated_cache" for f in self.bug_findings),
                "structured_spec_conclusions": sum(f.status in ("confirmed", "rejected") and
                    (f.verification_plan or {}).get("source") in {"hunter_plan", "structured_hypothesis"} for f in self.bug_findings),
                "reproduction_builder_conclusions": sum(f.status in ("confirmed", "rejected") and
                    (f.verification_plan or {}).get("source") == "deterministic_reproduction_builder" for f in self.bug_findings),
                "pinned_plans_reused": sum(report.plan_reused for report in validations),
                "unsupported_ast_capabilities": len(unsupported),
                "unsupported_module_fragment_cases": sum(bool(f.verification_plan and
                    (f.verification_plan.get("plan") or {}).get("kind") == "module_fragment") and f in unsupported for f in self.bug_findings),
                "module_fragment_cases": sum((f.verification_plan.get("plan") or {}).get("kind") == "module_fragment" for f in planned),
                "hidden_edge_case_findings": sum(f.hypothesis.category == "generated_edge_case" for f in self.bug_findings),
                "verification_unsupported": len(unsupported)}


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
Each hypothesis must include finding_kind="bug" or "observation". For a bug include
behavioral_failure describing a concrete wrong result, crash, hang, invalid state,
security risk, data loss, resource leak, API misuse or other incorrect behavior.
Coverage gaps, style, verbosity, maintainability, behavior-neutral dead code, generic
smells and performance suggestions without demonstrated defects are observations.
A mutable default alone is an observation: identify incorrect state leakage to claim a bug.
High confidence must concern a concrete failure, never suspicious syntax alone.
Bug hypotheses are open-ended: do not limit review to the verification kinds below
or a predefined catalog of bugs. Category is a descriptive label, not an eligibility gate.
Use category="other" for defects outside familiar classes; custom category strings are allowed.
Include structured evidence={"summary": "...", "locations": [...], "observations": [...]} and
reproduction_strategy={"approach": "...", "steps": [...], "expected_behavior": "..."} where useful.
Review state/mutation, resource lifetimes, timeout/nontermination, and cross-module contracts
as well as other semantic defects supported by source evidence. Unsupported hypotheses
must still be returned with verification_spec=null and a concrete reproduction strategy.
Optionally supply root_cause_key: a precise source-grounded causal identity shared only
by hypotheses about the same defect in the same symbol. Do not group independent defects.
Return JSON: {"hypotheses": [...]} with at most 10 hypotheses, or an empty list.
Each hypothesis must contain suspected_file (project-relative), suspected_symbol,
description, evidence, confidence (0 to 1), reproduction_strategy, verification_spec.
verification_spec must be null if no safe concrete reproduction can be specified,
otherwise a JSON object (never Python code) with kind and bounded JSON args/kwargs.
Supported kinds and extra fields:
- function_call or equals: expected (the intended return value)
- expected_exception: expected_exception (the exact unexpected exception claimed as a bug)
  plus expected (correct return value), or postcondition (a complete structured mutation_check,
  predicate, equals, or other supported positive verification spec). An exception alone cannot validate a repair.
- predicate or invariant: predicate={"op":"ge","value":0}; ops eq,ne,lt,le,gt,ge,contains,length_equals
- mutation_check: expected_args (intended post-call positional arguments)
- deterministic_random: seed (integer), random_values (optional lists for randint/randrange/random),
  and expected or expected_exception; stubs must respect each random API's real bounds
  Include expected or a positive postcondition even when claiming an exception.
  random_boundaries may map randint/randrange/random to lists of "lower"/"upper" to test valid API boundaries.
- class_state_check: constructors (1-5 argument lists), calls (instance index, method, args),
  observe (instance index, attribute), expected; use the class as suspected_symbol
- timeout: timeout_ms (50-2000); claim bounded non-completion, not proven infinity
- file_resource_check: files (relative virtual filename to text), expected_open_resources
No eval, scripts, arbitrary imports, external files, or expressions in verification specs.
Expected results and valid inputs must be grounded in the inspected project contract.
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
            fields = ("suspected_file", "suspected_symbol", "description")
            if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key].strip() for key in fields):
                raise ValueError("Invalid Hunter response: missing hypothesis text fields")
            confidence = item.get("confidence")
            if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("Invalid Hunter response: confidence must be between 0 and 1")
            _source_path(project.root, item["suspected_file"])
            for key in ("evidence", "reproduction_strategy"):
                value = item.get(key)
                if isinstance(value, dict):
                    primary = "summary" if key == "evidence" else "approach"
                    if not isinstance(value.get(primary), str) or not value[primary].strip():
                        raise ValueError(f"Invalid Hunter response: structured {key} requires {primary}")
                    if len(json.dumps(value, allow_nan=False)) > 16000:
                        raise ValueError("Invalid Hunter response: structured evidence exceeds limit")
                elif not isinstance(value, str) or not value.strip():
                    raise ValueError(f"Invalid Hunter response: missing {key}")
            category = item.get("category", "llm_review")
            finding_kind = item.get("finding_kind", "bug")
            behavioral_failure = item.get("behavioral_failure")
            if finding_kind not in ("bug", "observation") or behavioral_failure is not None and (
                not isinstance(behavioral_failure, str) or not behavioral_failure.strip() or len(behavioral_failure) > 4000
            ):
                raise ValueError("Invalid Hunter response: invalid finding scope or behavioral failure")
            cause = item.get("root_cause_key")
            if not isinstance(category, str) or not category.strip() or len(category) > 120:
                raise ValueError("Invalid Hunter response: category must be a bounded non-empty label")
            if cause is not None and (not isinstance(cause, str) or not cause.strip() or len(cause) > 300):
                raise ValueError("Invalid Hunter response: invalid root_cause_key")
            spec = item.get("verification_spec")
            plan = item.get("verification_plan")
            if spec is not None and not isinstance(spec, dict):
                raise ValueError("Invalid Hunter response: verification_spec must be an object or null")
            if plan is not None and not isinstance(plan, dict):
                raise ValueError("Invalid Hunter response: verification_plan must be an object or null")
            hypotheses.append(BugHypothesis(**{key: item[key] for key in (*fields, "evidence", "reproduction_strategy")},
                                           confidence=confidence, verification_spec=spec, category=category, root_cause_key=cause,
                                           finding_kind=finding_kind, behavioral_failure=behavioral_failure, verification_plan=plan))
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
            return Finding(hypothesis, pending, "UNVERIFIABLE: declared doctest adapter supports only bounded numeric examples in simple undecorated functions; no confirmation or repair authorized.")


class VerifiedRepairValidator:
    def __init__(self, verifier: VerificationStrategy, hypothesis: BugHypothesis, timeout: float, original: ProjectInfo,
                 baseline=(), baseline_syntax=None, pinned_plan=None):
        self.verifier, self.hypothesis, self.timeout = verifier, hypothesis, timeout
        self.pinned_plan = pinned_plan
        self.contract = self._contract(original)
        self.baseline, self.baseline_syntax = capture(baseline), baseline_syntax
        self.repair_hypothesis = hypothesis
        if pinned_plan and isinstance(pinned_plan.get("plan"), dict):
            from .verification import validate_spec
            exact = validate_spec(pinned_plan["plan"])
            self.repair_hypothesis = replace(hypothesis, verification_spec=exact, verification_plan=exact)
            if hasattr(verifier, "prepare_repair"):
                prepared = verifier.prepare_repair(original, self.repair_hypothesis)
                self.repair_hypothesis = (replace(prepared, verification_plan=prepared.verification_spec)
                                          if prepared is not None else None)
        elif hasattr(verifier, "prepare_repair"):
            self.repair_hypothesis = verifier.prepare_repair(original, hypothesis)
        elif hypothesis.verification_spec is not None:
            from .verification import prepare_repair_hypothesis
            self.repair_hypothesis = prepare_repair_hypothesis(original, hypothesis)

    def _contract(self, project: ProjectInfo):
        spec = (self.pinned_plan or {}).get("plan") or self.hypothesis.verification_spec or {}
        if spec.get("kind") == "python_syntax":
            return None  # Syntax verification needs no successfully parsed callable contract.
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
        mechanism = (self.pinned_plan or {}).get("verifier", "restricted-ast-runtime")
        syntax = syntax_check(project)
        if syntax is not None and not syntax.passed:
            return syntax_rejection(project, syntax, self.baseline, confirmation_verifier=mechanism,
                                    repair_verifier=mechanism, plan_reused=bool(self.pinned_plan))
        finding = self.verifier.verify(project, self.repair_hypothesis) if self.repair_hypothesis else None
        if finding is None:
            finding = Finding(self.hypothesis, "unconfirmed", "No positive corrected behavior was specified; absence of the old exception cannot validate a repair")
        reproduction = finding.check or CheckResult("hunt:targeted-verification", ("restricted-verifier",), 1, "", finding.evidence or "Targeted verification unavailable", 0)
        if finding.check is None:
            reproduction = replace(reproduction, stderr=finding.evidence)
        if self._contract(project) != self.contract:
            reproduction = replace(reproduction, returncode=1, stderr="Declared reproduction contract changed; repair cannot be validated")
        results = run_checks(project, self.timeout, stop_on_failure=False)
        expected = json.dumps(self.repair_hypothesis.verification_spec, sort_keys=True) if self.repair_hypothesis and self.repair_hypothesis.verification_spec else "Pinned declared verification plan must pass"
        report = evaluate(project, reproduction, results, self.baseline, syntax, self.baseline_syntax, expected)
        blocked = bool(finding.status in ("unconfirmed", "high_confidence") and
                       (finding.evidence.startswith("UNVERIFIABLE:") or "unsupported" in finding.evidence.casefold()
                        or "not defined" in finding.evidence.casefold()))
        return replace(report, final_status="TARGETED VERIFICATION BLOCKED" if blocked else report.final_status,
                       passed=False if blocked else report.passed,
                       confirmation_verifier=mechanism,
                       repair_verifier=mechanism, plan_reused=bool(self.pinned_plan))


def hunt_project(project: ProjectInfo, detectors: tuple[DetectionStrategy, ...], verifier: VerificationStrategy,
                 agent: OpenAIAgent, timeout: float = 120, quick: bool = False, include_quality: bool = False) -> HuntRun:
    findings = []
    from .verification import load_cached_plans
    cached_plans = load_cached_plans(project.root)
    # Even existing checks run on a copy in hunt mode.
    with isolated_workspace(project.root) as workspace:
        isolated = replace(project, root=workspace)
        results = capture(run_checks(isolated, timeout, stop_on_failure=False))
        baseline_syntax = syntax_check(isolated)
        from .hunt_registry import collect_findings
        findings = collect_findings(isolated, results, detectors, verifier, cached_plans)
    repairs = []
    errors = []
    for finding in tuple(findings):
        if not finding.is_bug or not finding.repair_authorized or finding.status != "confirmed" or finding.check is None or finding.check.passed:
            continue
        try:
            validator = VerifiedRepairValidator(verifier, finding.hypothesis, timeout, project, results, baseline_syntax,
                                                pinned_plan=finding.verification_plan)
            syntax_context = ("\nBounded syntax evidence: " + json.dumps(finding.hypothesis.evidence, ensure_ascii=True)[:1800]
                              if (finding.verification_plan or {}).get("capability") == "parse_compile" else "")
            repair = DebugOrchestrator(agent, agent, validator).run(
                project, replace(finding.check, stderr=finding.check.stderr + "\nTarget hypothesis: " + finding.hypothesis.description
                                 + syntax_context + "\nPinned verification contract: " + json.dumps(finding.hypothesis.verification_spec or finding.hypothesis.reproduction)),
                changed_files={Path(finding.hypothesis.suspected_file)}, finding_id=finding.finding_id)
            repairs.append(replace(repair, finding_id=finding.finding_id))
            if (finding.verification_plan or {}).get("capability") == "parse_compile" and repair.validation and repair.validation.passed and repair.validated_patch_path:
                blocked = [(index, pending) for index, pending in enumerate(findings)
                           if pending.hypothesis.suspected_file == finding.hypothesis.suspected_file
                           and pending.finding_id != finding.finding_id
                           and pending.status in ("high_confidence", "unconfirmed")
                           and (pending.verification_plan or {}).get("unsupported_reason") == "Behavioral verification blocked by a separate syntax defect"]
                if blocked:
                    from .hunt_registry import collect_findings, same_cause
                    from .workspace import apply_unified_diff

                    class RetryDetector:
                        def hunt(self, _project):
                            return tuple(pending.hypothesis for _, pending in blocked)

                    try:
                        with isolated_workspace(project.root) as retry_workspace:
                            apply_unified_diff(retry_workspace, repair.validated_patch_path.read_text(encoding="utf-8"))
                            retried = collect_findings(replace(project, root=retry_workspace), results,
                                                       (RetryDetector(),), verifier, cached_plans)
                        for index, pending in blocked:
                            result = next((candidate for candidate in retried if same_cause(candidate, pending)), None)
                            if result is None:
                                continue
                            marker = {**(result.verification_plan or {}), "retried_after_syntax_repair": finding.finding_id,
                                      "retry_workspace_only": True}
                            repairability = ("blocked_pending_syntax_apply" if result.repair_authorized else result.repairability)
                            findings[index] = replace(result, signals=(*pending.signals, *result.signals),
                                                      verification_plan=marker, repairability=repairability,
                                                      evidence=result.evidence + " Retried after validated syntax repair in an isolated workspace; source project remains unchanged.")
                    except (OSError, ValueError, InvalidUnifiedDiffError, PatchApplicabilityError) as exc:
                        errors.append(f"{finding.finding_id}: dependent verification retry unavailable ({type(exc).__name__})")
        except (InvalidUnifiedDiffError, PatchApplicabilityError) as exc:
            errors.append(f"{finding.finding_id}: repair failed ({type(exc).__name__}): {exc}; no validated repair recorded")
        except Exception as exc:
            errors.append(f"{finding.finding_id}: repair failed ({type(exc).__name__}); no validated repair recorded")
    from .hunt_reports import save_hunt_report
    run = HuntRun(results, tuple(findings), tuple(repairs), "quick" if quick else "full",
                  ("existing_checks", *(type(detector).__name__ for detector in detectors)), repair_errors=tuple(errors), include_quality=include_quality)
    try:
        return save_hunt_report(project.root, run)
    except (OSError, ValueError) as exc:
        return replace(run, artifact_error=f"Artifact-save error ({type(exc).__name__}): could not persist hunt findings/report. Check the project .aidebug directory and permissions.")


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
    parser.add_argument("--include-quality", action="store_true", help="Show non-bug code-quality observations separately")
    args = parser.parse_args(argv)
    api_key = ""
    try:
        project = discover_repository(args.path)
        api_key = resolve_api_key() or ""
        if not api_key:
            parser.error("Hunting requires an API key. Run aidebug configure or set OPENAI_API_KEY.")
        agent = OpenAIAgent(model=args.model)
        from .hunt_strategies import build_detectors, HuntVerifier
        run = hunt_project(project, build_detectors(agent, args.quick), HuntVerifier(args.quick), agent, args.timeout, quick=args.quick,
                           include_quality=args.include_quality)
    except Exception as exc:
        parser.error(_safe_agent_error(exc, api_key))
    if args.as_json:
        print(json.dumps(run.record(), default=str, indent=2))
    else:
        print(render_hunt_summary(run))
    return int(any(not result.passed for result in run.existing_checks) or any(finding.status == "confirmed" for finding in run.bug_findings))


def _repair_outcome_counts(run):
    """Count one terminal repair outcome per confirmed bug for concise CLI use."""
    repairs = {repair.finding_id: repair for repair in run.repairs}
    counts = {"verified": 0, "failed": 0, "blocked": 0}
    for finding in (item for item in run.bug_findings if item.status == "confirmed"):
        repair = repairs.get(finding.finding_id)
        if repair is None:
            errors = any(error.startswith(finding.finding_id + ":") for error in run.repair_errors)
            counts["failed" if errors else "blocked"] += 1
            continue
        validation = repair.validation
        if validation and validation.passed:
            counts["verified"] += 1
        elif validation and ("BLOCKED" in validation.final_status or
                             (validation.targeted and validation.targeted.blocked_reason)):
            counts["blocked"] += 1
        else:
            counts["failed"] += 1
    return counts


def _short_terminal_hypothesis(value: str, limit: int = 120) -> str:
    """Collapse a finding to one display line without changing stored text."""
    compact = " ".join(str(value).split())
    if len(compact) <= limit:
        return compact
    boundary = compact.rfind(" ", 0, limit - 1)
    if boundary < limit // 2:
        boundary = limit - 1
    return compact[:boundary].rstrip(" ,;:") + "…"


def render_hunt_summary(run: HuntRun) -> str:
    """Render only the concise default terminal summary; artifacts retain detail."""
    if not run.existing_checks:
        baseline = "NOT AVAILABLE"
        failed_names = ()
    elif all(result.passed for result in run.existing_checks):
        baseline = "PASS"
        failed_names = ()
    else:
        baseline = "FAIL"
        failed_names = tuple(dict.fromkeys(result.name for result in run.existing_checks if not result.passed))
    lines = ["AIdebug Hunt", "", "Baseline: " + baseline]
    if failed_names:
        lines[-1] += " (" + ", ".join(failed_names) + ")"
    metrics = run.bug_metrics
    lines.extend(["", f"Bugs found: {metrics['bugs_discovered']}"])
    if metrics["bugs_confirmed"] > 0:
        lines.append(f"Confirmed: {metrics['bugs_confirmed']}")
    if metrics["bugs_unverifiable"] > 0:
        lines.append(f"Unverifiable: {metrics['bugs_unverifiable']}")

    repaired = {repair.finding_id for repair in run.repairs if repair.validation and repair.validation.passed}
    unresolved = [finding for finding in run.bug_findings
                  if finding.status != "rejected" and
                  (finding.status != "confirmed" or finding.finding_id not in repaired)]
    if unresolved:
        lines.extend(["", "Unresolved:"])
        for finding in unresolved:
            symbol = finding.hypothesis.suspected_symbol or "<module>"
            lines.append(f"- {finding.hypothesis.suspected_file}:{symbol} — "
                         f"{_short_terminal_hypothesis(finding.hypothesis.description)}")

    outcomes = _repair_outcome_counts(run)
    repair_lines = [("Repairs verified", outcomes["verified"]),
                    ("Repairs failed", outcomes["failed"]),
                    ("Repairs blocked", outcomes["blocked"])]
    visible_repairs = [(label, count) for label, count in repair_lines if count > 0]
    if visible_repairs:
        lines.append("")
        lines.extend(f"{label}: {count}" for label, count in visible_repairs)
    lines.extend(["", "Report:", str(run.report_path.resolve()) if run.report_path else "unavailable"])
    return "\n".join(lines)


def render_repair_output(run):
    """One result per confirmed finding; artifact failure is not target failure."""
    repairs = {repair.finding_id: repair for repair in run.repairs}
    counts = {"verified": 0, "failed": 0, "blocked": 0}
    lines = []
    confirmed = {finding.finding_id: finding for finding in run.bug_findings if finding.status == "confirmed"}
    for identifier, finding in confirmed.items():
        repair = repairs.get(identifier)
        lines.extend(["", f"{identifier}: repair result", f"File: {finding.hypothesis.suspected_file}",
                      f"Symbol: {finding.hypothesis.suspected_symbol}"])
        if repair is None:
            if finding.verification_state == "CONFIRMED_BUT_EXPECTED_BEHAVIOR_UNKNOWN":
                counts["blocked"] += 1
                lines.extend(["Verification: DEFECT REPRODUCED", "Repair Authorization: BLOCKED",
                              "Reason: expected behavior is unspecified"])
                continue
            if finding.status == "confirmed" and not finding.repair_authorized:
                counts["blocked"] += 1
                lines.extend(["Verification: DEFECT REPRODUCED", "Repair Authorization: BLOCKED",
                              "Reason: no safe positive corrected-behavior oracle is available"])
                continue
            errors = [error for error in run.repair_errors if error.startswith(identifier + ":")]
            if errors:
                counts["failed"] += 1
                lines.extend(["Final Repair Status: REPAIR FAILED", *[error.split(":", 1)[1].strip() for error in errors]])
            else:
                counts["blocked"] += 1
                lines.extend(["Final Repair Status: VALIDATION BLOCKED", "No actionable confirmed reproduction was available for repair."])
            continue
        validation = repair.validation
        if validation and validation.passed:
            counts["verified"] += 1
        elif validation and (validation.final_status == "VALIDATION BLOCKED" or
                             validation.targeted and validation.targeted.blocked_reason):
            counts["blocked"] += 1
        else:
            counts["failed"] += 1
        lines.append(f"Attempts: {repair.attempts}")
        lines.append(summary(validation) if validation else "Final Repair Status: REPAIR FAILED")
        lines.extend(repair.patch_errors)
        if repair.proposals and repair.proposals[-1].status == "no_patch":
            lines.append("No safe patch: " + repair.proposals[-1].explanation)
        if repair.artifact_error:
            lines.append(repair.artifact_error)
        for label, path in (("Validated patch", repair.validated_patch_path), ("Debug report", repair.debug_report_path)):
            if path:
                lines.extend([label + " saved to:", str(path.resolve())])
        if validation and validation.passed and not repair.artifact_error and not (
            repair.validated_patch_path and repair.debug_report_path
        ):
            lines.append("Artifact-save error: verified repair has incomplete patch/report paths.")
    lines.extend(["", f"Confirmed bugs: {len(confirmed)}", f"Repairs verified: {counts['verified']}",
                  f"Repairs failed: {counts['failed']}", f"Repairs blocked: {counts['blocked']}"])
    return "\n".join(lines)
