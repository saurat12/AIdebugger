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

    def register(self, name, supports, factory):
        if any(entry.name == name for entry in self.entries):
            raise ValueError("Duplicate verifier registration: " + name)
        self.entries.append(VerifierEntry(name, supports, factory))

    def resolve(self, item, quick=False):
        for entry in self.entries:
            if entry.supports(item):
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
    a, b = left.hypothesis, right.hypothesis
    if (a.suspected_file.replace("\\", "/"), a.suspected_symbol) != (b.suspected_file.replace("\\", "/"), b.suspected_symbol):
        return False
    # A shared explicit root cause is canonical even when verifiers disagree;
    # the merge below withholds confirmation on a confirmed/rejected conflict.
    if a.root_cause_key and b.root_cause_key:
        return _normalized(a.root_cause_key) == _normalized(b.root_cause_key)
    return _normalized(a.description) == _normalized(b.description)


def collect_findings(project, checks, detectors, verifier, cached_plans=None, planner_agent=None):
    from .verification import VerificationPlanner
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
    findings = []
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
            planner = VerificationPlanner(planner_agent)
            try:
                plan = planner.plan(project, item, cached_plans)
                if plan and plan.get("plan") is not None:
                    item = replace(item, verification_spec=plan["plan"])
            except Exception as exc:
                plan = {"schema_version": 1, "file": item.suspected_file, "symbol": item.suspected_symbol,
                        "plan": None, "unsupported_reason": f"Planner unavailable or plan failed strict validation ({type(exc).__name__}: {str(exc)[:180]})."}
                item = replace(item, verification_unsupported=plan["unsupported_reason"])
            if plan is None:
                reason = "Planner returned no safe structured plan; expected behavior or a bounded reproduction is unsupported."
                if framework_routed:
                    item = replace(item, verification_unsupported=reason)
            elif plan.get("plan") is not None:
                plan.setdefault("verifier", "declared-doctest-adapter" if plan["plan"].get("kind") == "doctest" else "restricted-ast-runtime")
            try:
                finding = active_verifier.verify(project, item)
            except Exception as exc:
                finding = unsupported(item, f"Verification unavailable ({type(exc).__name__}); hypothesis retained without confirmation.")
            signal = {"detector": type(detector).__name__, "hypothesis": asdict(item),
                      "verification_status": finding.status, "verification_evidence": finding.evidence,
                      "finding_id": finding.finding_id}
            if plan is None:
                plan = {"schema_version": 1, "file": item.suspected_file, "symbol": item.suspected_symbol,
                        "plan": None, "verifier": "restricted-ast-runtime",
                        "unsupported_reason": item.verification_unsupported}
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
            finding = replace(finding, signals=(signal,), verification_plan=plan, repairability=repairability)
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
        from .hunt import DoctestVerifier
        registry = VerifierRegistry()
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
