"""Trusted-code extension points for detection and restricted verification.

Registrations are application code, never instructions supplied by a model.
Categories are metadata; verifier selection depends on executable capabilities.
"""

import json
import re
from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class DetectorEntry:
    name: str
    factory: object
    quick: bool = False


@dataclass(frozen=True)
class VerifierEntry:
    name: str
    supports: object
    factory: object


@dataclass(frozen=True)
class CapabilityVerifierEntry:
    name: str
    supports: object
    factory: object


class DetectorRegistry:
    def __init__(self):
        self.entries = []

    def register(self, name, factory, *, quick=False):
        if any(entry.name == name for entry in self.entries):
            raise ValueError("Duplicate detector registration: " + name)
        self.entries.append(DetectorEntry(name, factory, quick))

    def build(self, agent, quick=False):
        return tuple(entry.factory(agent, quick) for entry in self.entries if not quick or entry.quick)


class VerifierRegistry:
    def __init__(self):
        self.entries = []
        self.capabilities = []

    def register(self, name, supports, factory):
        if any(entry.name == name for entry in self.entries):
            raise ValueError("Duplicate verifier registration: " + name)
        self.entries.append(VerifierEntry(name, supports, factory))

    def resolve(self, item, quick=False):
        for entry in self.entries:
            if entry.supports(item):
                return entry.factory(quick)
        return None

    def register_capability(self, name, supports, factory):
        if any(entry.name == name for entry in self.capabilities):
            raise ValueError("Duplicate verifier capability: " + name)
        self.capabilities.append(CapabilityVerifierEntry(name, supports, factory))

    def resolve_capability(self, project, item, quick=False):
        for entry in self.capabilities:
            if entry.supports(project, item):
                return entry.factory(quick)
        return None


def unsupported(item, reason):
    from .hunt import Finding
    return Finding(item, "high_confidence" if item.confidence >= .85 else "unconfirmed", reason)


class RegistryVerifier:
    def __init__(self, quick=False, registry=None):
        self.quick = quick
        self.registry = registry if registry is not None else verifier_registry()

    def prepare_repair(self, project, item):
        from .static_absence import absence_spec
        if absence_spec(item) is not None:
            return None  # Absence alone supplies no positive post-repair behavior.
        handler = self.registry.resolve(item, self.quick)
        if handler is not None and hasattr(handler, "prepare_repair"):
            return handler.prepare_repair(project, item)
        # A reproducer without a positive post-repair oracle cannot authorize a patch.
        return None

    def contract(self, project, item):
        handler = self.registry.resolve(item, self.quick)
        if handler is not None and hasattr(handler, "contract"):
            return handler.contract(project, item)
        # Keep the existing source declaration pins for built-in verification.
        from .hunt_strategies import LegacyVerifier
        return LegacyVerifier(self.quick).contract(project, item)

    def verify(self, project, item):
        from .finding_scope import is_non_bug_hypothesis
        if is_non_bug_hypothesis(item):
            from .hunt import Finding
            reason = ("A coverage gap is not proof of a behavioral defect." if item.category.lower() == "coverage_gap" or
                      (item.reproduction or {}).get("kind") == "coverage_gap" else
                      "Non-bug observation is excluded from behavioral bug verification.")
            return Finding(item, "unconfirmed", reason)
        try:
            capability = self.registry.resolve_capability(project, item, self.quick)
            if capability is not None:
                result = capability.verify(project, item)
                if result is not None:
                    return result
            handler = self.registry.resolve(item, self.quick)
            if handler is None:
                return unsupported(item, "No registered verifier supports this reproduction; hypothesis retained, no automatic repair authorized.")
            result = handler.verify(project, item)
            from .hunt import Finding
            if not isinstance(result, Finding) or result.hypothesis != item or result.status not in ("confirmed", "rejected", "unconfirmed", "high_confidence"):
                return unsupported(item, "Verifier returned an unsupported result; no automatic repair authorized.")
        except Exception as exc:
            return unsupported(item, f"Verifier could not evaluate this hypothesis ({type(exc).__name__}); no automatic repair authorized.")
        # A label alone is never enough to authorize repair.
        if result.status == "confirmed" and (result.check is None or result.check.passed):
            return unsupported(item, "Verifier supplied no failing independent reproduction; confirmation withheld.")
        return result


def _normalized(text):
    return re.sub(r"\s+", " ", text).strip().casefold()


def same_cause(left, right):
    if left.is_bug != right.is_bug:
        return False
    from .syntax_findings import syntax_identity
    left_syntax, right_syntax = syntax_identity(left), syntax_identity(right)
    if left_syntax is not None or right_syntax is not None:
        return left_syntax is not None and left_syntax == right_syntax
    a, b = left.hypothesis, right.hypothesis
    if (a.suspected_file.replace("\\", "/"), a.suspected_symbol) != (b.suspected_file.replace("\\", "/"), b.suspected_symbol):
        return False
    # A shared explicit root cause is canonical even when verifiers disagree;
    # the merge below withholds confirmation on a confirmed/rejected conflict.
    if a.root_cause_key and b.root_cause_key:
        return _normalized(a.root_cause_key) == _normalized(b.root_cause_key)
    return _normalized(a.description) == _normalized(b.description)


def collect_findings(project, checks, detectors, verifier, cached_plans=None):
    from .verification import KINDS, _syntax_evidence_matches, resolve_verification_spec
    from .syntax_findings import discover_syntax_findings
    from .finding_scope import is_non_bug_hypothesis
    from .hunt import Finding
    from .hunt import DoctestVerifier
    framework_routed = isinstance(verifier, RegistryVerifier) or isinstance(verifier, DoctestVerifier)
    if isinstance(verifier, RegistryVerifier):
        active_verifier = verifier
    elif isinstance(verifier, DoctestVerifier):
        # The former scalar doctest verifier is not a hunt fallback. Its safe
        # specialized capabilities are registered separately when supported.
        active_verifier = RegistryVerifier()
    else:
        # A caller-supplied verifier is a trusted adapter. Register it through
        # the same registry dispatch used by built-in adapters.
        adapter_registry = VerifierRegistry()
        adapter_registry.register("injected_adapter", lambda item: True, lambda quick: verifier)
        active_verifier = RegistryVerifier(registry=adapter_registry)
    findings = list(discover_syntax_findings(project))
    for index, syntax in enumerate(findings):
        location = syntax.hypothesis.evidence
        for check in checks:
            if check.passed or check.blocked_reason:
                continue
            output = (check.stdout + "\n" + check.stderr).replace("\\", "/")[:12000]
            marker = rf"(?<![\w/]){re.escape(location['file'])}:{location['line']}:{location['column']}:\s*{re.escape(location['parser_error'])}:"
            if re.search(marker, output):
                signal = {"detector": "ExistingCheck", "check": check.name,
                          "hypothesis": asdict(syntax.hypothesis), "verification_status": "confirmed",
                          "verification_evidence": f"{check.name} reported the same parser location and error class"}
                findings[index] = replace(findings[index], signals=(*findings[index].signals, signal))
    for detector in detectors:
        items = detector.hunt_with_checks(project, checks) if hasattr(detector, "hunt_with_checks") else detector.hunt(project)
        for item in items:
            if is_non_bug_hypothesis(item):
                finding = Finding(item, "unconfirmed", "Non-bug observation retained separately; it was not sent to behavioral verification.")
                signal = {"detector": type(detector).__name__, "hypothesis": asdict(item),
                          "verification_status": finding.status, "verification_evidence": finding.evidence,
                          "finding_id": finding.finding_id}
                finding = replace(finding, signals=(signal,), verification_plan={
                    "schema_version": 1, "file": item.suspected_file, "symbol": item.suspected_symbol,
                    "plan": None, "unsupported_reason": "Non-bug observations are excluded from bug verification."})
                match = next((i for i, previous in enumerate(findings) if same_cause(previous, finding)), None)
                if match is None:
                    findings.append(finding)
                else:
                    previous = findings[match]
                    findings[match] = replace(previous, signals=(*previous.signals, signal))
                continue
            if item.verification_spec is None and item.verification_plan is None and isinstance(item.evidence, dict):
                declared_error = item.evidence.get("parser_error", item.evidence.get("error_type"))
                if isinstance(declared_error, str) and declared_error in {"SyntaxError", "IndentationError", "TabError"}:
                    matching_syntax = next((candidate for candidate in findings
                                            if (candidate.verification_plan or {}).get("capability") == "parse_compile"
                                            and candidate.hypothesis.suspected_file == item.suspected_file
                                            and isinstance(candidate.hypothesis.evidence, dict)
                                            and candidate.hypothesis.evidence.get("parser_error") == declared_error
                                            and _syntax_evidence_matches(item, candidate.hypothesis.evidence["line"],
                                                                         candidate.hypothesis.evidence["column"])), None)
                    if matching_syntax is not None:
                        item = replace(item, verification_spec=matching_syntax.hypothesis.verification_spec)
            supplied_plan = item.verification_plan is not None or item.verification_spec is not None or (
                isinstance(item.reproduction, dict) and item.reproduction.get("kind") in
                KINDS - {"verification_plan", "doctest"})
            try:
                finding = active_verifier.verify(project, item)
            except Exception as exc:
                finding = unsupported(item, f"Verification unavailable ({type(exc).__name__}); hypothesis retained without confirmation.")
            deterministic_terminal = finding.status in ("confirmed", "rejected")
            capability_handled = bool(finding.verification_plan and
                                      finding.verification_plan.get("source") == "deterministic_verifier")
            try:
                plan = finding.verification_plan if capability_handled else resolve_verification_spec(project, item, cached_plans)
                if plan and plan.get("plan") is not None and not capability_handled:
                    item = replace(item, verification_spec=plan["plan"],
                                   verification_target=plan.get("verification_target", item.verification_target))
            except Exception as exc:
                if deterministic_terminal:
                    plan = finding.verification_plan or {"schema_version": 1, "file": item.suspected_file,
                                                         "symbol": item.suspected_symbol, "plan": None,
                                                         "source": "deterministic_verifier"}
                else:
                    from .reproduction import validation_diagnostic
                    diagnostic = validation_diagnostic(exc)
                    reason = f"UNVERIFIABLE: {diagnostic['failure_kind']} at {diagnostic['field']}: {diagnostic['reason']}"
                    plan = {"schema_version": 1, "file": item.suspected_file, "symbol": item.suspected_symbol,
                            "plan": None, "source": "structured_spec_resolution" if supplied_plan else "deterministic_reproduction_builder",
                            "unsupported_reason": reason, "validation_error": diagnostic}
                    item = replace(item, verification_unsupported=reason)
                    finding = unsupported(item, reason)
            if plan is not None and plan.get("plan") is not None and not capability_handled:
                plan.setdefault("verifier", "declared-doctest-adapter" if plan["plan"].get("kind") == "doctest" else "restricted-ast-runtime")
                if not deterministic_terminal and (item.verification_spec != plan["plan"] or finding.hypothesis != item):
                    try:
                        finding = active_verifier.verify(project, item)
                    except Exception as exc:
                        finding = unsupported(item, f"Verification unavailable ({type(exc).__name__}); hypothesis retained without confirmation.")
            signal = {"detector": type(detector).__name__, "hypothesis": asdict(item),
                      "verification_status": finding.status, "verification_evidence": finding.evidence,
                      "finding_id": finding.finding_id}
            if plan is None and finding.verification_plan is not None:
                plan = finding.verification_plan
            if plan is None:
                plan = {"schema_version": 1, "file": item.suspected_file, "symbol": item.suspected_symbol,
                        "plan": None,
                        "source": ("deterministic_verifier" if finding.status in ("confirmed", "rejected") else
                                   "unresolved_without_safe_plan"),
                        "verifier": "deterministic-capability" if finding.status in ("confirmed", "rejected") else "restricted-ast-runtime",
                        "unsupported_reason": item.verification_unsupported or
                                              (finding.evidence if finding.status not in ("confirmed", "rejected") else None)}
            repairability = None
            if finding.status == "confirmed" and finding.is_bug:
                try:
                    prepared = active_verifier.prepare_repair(project, item) if hasattr(active_verifier, "prepare_repair") else None
                    if prepared is not None:
                        repairability = "authorized"
                    else:
                        spec = item.verification_spec or {}
                        has_expected = bool({"expected", "expected_args", "predicate", "postcondition", "expected_open_resources"} & spec.keys())
                        if spec.get("kind") == "verification_plan":
                            has_expected = ("repair_plan" in spec or not any(
                                assertion["source"] == "timed_out" and assertion["expected"] is True
                                for assertion in spec.get("assertions", [])))
                        repairability = "blocked_positive_oracle_unsupported" if has_expected else "blocked_expected_behavior_unknown"
                except Exception:
                    repairability = "blocked_positive_oracle_unsupported"
            finding = replace(finding, hypothesis=item, signals=(signal,), verification_plan=plan, repairability=repairability)
            match = next((i for i, previous in enumerate(findings) if same_cause(previous, finding)), None)
            if match is None:
                findings.append(finding)
                continue
            previous = findings[match]
            # The representative retains its own confidence, oracle and proof.
            # Other detector claims remain independently recorded as signals.
            rank = lambda value: (value.status == "confirmed", value.repairability == "authorized",
                                  bool(value.hypothesis.root_cause_key), value.status == "rejected", value.hypothesis.confidence)
            representative = finding if rank(finding) > rank(previous) else previous
            merged = replace(representative, signals=(*previous.signals, signal))
            if "rejected" in {previous.status, finding.status} and previous.status != finding.status:
                merged = replace(merged, status="unconfirmed", check=None, repairability=None,
                                 evidence="Conflicting independent verification outcomes for the same root cause; confirmation withheld.")
            findings[match] = merged
    return findings


_detectors = None
_verifiers = None


def detector_registry():
    global _detectors
    if _detectors is None:
        from .hunt import BugHunter
        from .hunt_strategies import (StaticAnalysis, CoverageGapAnalysis, GeneratedEdgeCases,
                                     PropertyChecks, ExceptionPathAnalysis, CrossFunctionChecks)
        from .hunt_signals import StateMutationAnalysis, ResourceAnalysis, NonterminationAnalysis, CrossModuleAnalysis, NativeValidationEvidence
        registry = DetectorRegistry()
        registry.register("static", lambda agent, quick: StaticAnalysis(), quick=True)
        registry.register("semantic", lambda agent, quick: BugHunter(agent, quick), quick=True)
        for name, factory in (("coverage", CoverageGapAnalysis), ("edge_cases", GeneratedEdgeCases),
                              ("properties", PropertyChecks), ("exceptions", ExceptionPathAnalysis),
                              ("cross_function", CrossFunctionChecks), ("state_mutation", StateMutationAnalysis),
                              ("resources", ResourceAnalysis), ("nontermination", NonterminationAnalysis),
                              ("cross_module", CrossModuleAnalysis)):
            registry.register(name, lambda agent, quick, factory=factory: factory())
        registry.register("native_validation", lambda agent, quick: NativeValidationEvidence())
        _detectors = registry
    return _detectors


def verifier_registry():
    global _verifiers
    if _verifiers is None:
        from .verification import StructuredVerifier, KINDS
        from .static_absence import StaticAbsenceVerifier, absence_spec
        from .hunt import DoctestVerifier
        registry = VerifierRegistry()
        from .verification import PythonSyntaxVerifier
        registry.register_capability("static_absence", lambda project, item: absence_spec(item) is not None,
                                     lambda quick: StaticAbsenceVerifier())
        registry.register_capability("python_parse", lambda project, item: item.suspected_file.casefold().endswith(".py"),
                                     lambda quick: PythonSyntaxVerifier())
        registry.register("declared_doctest_adapter", lambda item: isinstance(item.verification_spec, dict) and item.verification_spec.get("kind") == "doctest",
                          lambda quick: DoctestVerifier())
        registry.register("structured", lambda item: isinstance(item.verification_spec, dict) and item.verification_spec.get("kind") in KINDS - {"doctest"},
                          lambda quick: StructuredVerifier())
        kinds = {"mutable_default", "duplicate_key", "bare_except", "call_arity", "edge", "invariant", "equivalent"}
        if kinds:
            from .hunt_strategies import LegacyVerifier
            registry.register("specialized_static_adapter", lambda item: item.verification_spec is None and (item.reproduction or {}).get("kind") in kinds,
                              lambda quick: LegacyVerifier(quick))
        registry.register("generic_plan", lambda item: True, lambda quick: StructuredVerifier())
        _verifiers = registry
    return _verifiers
