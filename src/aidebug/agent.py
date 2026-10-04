"""Provider-neutral Analyzer -> Fixer -> Validator debugging agent."""

from pathlib import Path
from dataclasses import replace
from typing import Protocol, runtime_checkable

from .context import build_debug_context
from .artifacts import remember_originals, save_validated_repair
from .models import (
    AnalysisReport,
    CheckResult,
    DebugContext,
    DebugRun,
    PatchProposal,
    ProjectInfo,
    ValidationReport,
)
from .runner import run_checks
from .validation import capture, evaluate, identity, syntax_check, syntax_rejection
from .workspace import InvalidUnifiedDiffError, PatchApplicabilityError, apply_unified_diff, isolated_workspace


class Analyzer(Protocol):
    """Find and explain the likely root cause."""

    def analyze(self, context: DebugContext) -> AnalysisReport: ...


class Fixer(Protocol):
    """Generate a patch from evidence and an analysis."""

    def propose(self, context: DebugContext, analysis: AnalysisReport) -> PatchProposal: ...


class Validator(Protocol):
    """Run checks against a patched workspace."""

    def validate(self, project: ProjectInfo) -> ValidationReport: ...


@runtime_checkable
class PreparableValidator(Validator, Protocol):
    """A validator that captures a baseline before repair attempts."""

    baseline: tuple[CheckResult, ...]

    def prepare(self, project: ProjectInfo, failure: CheckResult) -> None: ...


class CheckValidator:
    """Default validator backed by the repository's discovered checks."""

    def __init__(self, timeout_seconds: float = 120) -> None:
        self.timeout_seconds = timeout_seconds
        self.baseline = ()
        self.baseline_syntax = None
        self.failure = None

    def prepare(self, project, failure):
        self.failure = failure
        self.baseline = capture(run_checks(project, self.timeout_seconds, stop_on_failure=False))
        self.baseline_syntax = syntax_check(project)

    def validate(self, project: ProjectInfo) -> ValidationReport:
        syntax = syntax_check(project)
        if syntax is not None and not syntax.passed:
            return syntax_rejection(project, syntax, self.baseline)
        results = capture(run_checks(project, timeout_seconds=self.timeout_seconds, stop_on_failure=False))
        target = next((r for r in results if self.failure and identity(r) == identity(self.failure)), None)
        if self.failure is None:
            target = CheckResult("discovered checks", (), 0 if results and all(r.passed for r in results) else 1, "", "", 0)
        elif target is None:
            target = CheckResult(self.failure.name, self.failure.command, 1, "", "Original failing check was not executed", 0)
        return evaluate(project, target, results, self.baseline, syntax, self.baseline_syntax)


class DebugOrchestrator:
    """Coordinate analysis, patching, isolated validation, and bounded retries."""

    def __init__(
        self,
        analyzer: Analyzer,
        fixer: Fixer,
        validator: Validator | None = None,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.analyzer = analyzer
        self.fixer = fixer
        self.validator = validator or CheckValidator()
        self.max_attempts = max_attempts

    def run(
        self,
        project: ProjectInfo,
        failure: CheckResult,
        changed_files: set[Path] | None = None,
        git_diff: str | None = None,
        finding_id: str | None = None,
    ) -> DebugRun:
        """Try to repair one failed check without modifying the active repository."""

        initial_context = build_debug_context(project, failure, changed_files=changed_files, git_diff=git_diff)
        with isolated_workspace(project.root) as workspace:
            isolated_project, context = self._prepare_isolated_context(project, initial_context, workspace)
            blocked_run = self._prepare_validator(isolated_project, failure, initial_context)
            if blocked_run is not None:
                return blocked_run
            return self._attempt_repairs(initial_context, isolated_project, context, workspace, finding_id)

    def _prepare_isolated_context(
        self, project: ProjectInfo, initial_context: DebugContext, workspace: Path
    ) -> tuple[ProjectInfo, DebugContext]:
        isolated_project = ProjectInfo(root=workspace, project_types=project.project_types, checks=project.checks)
        context = replace(
            initial_context,
            project=isolated_project,
            relevant_files=tuple(workspace / p.relative_to(project.root) for p in initial_context.relevant_files),
        )
        return isolated_project, context

    def _prepare_validator(
        self, project: ProjectInfo, failure: CheckResult, initial_context: DebugContext
    ) -> DebugRun | None:
        if not isinstance(self.validator, PreparableValidator):
            return None
        self.validator.prepare(project, failure)
        blocked = next(
            (r for r in self.validator.baseline if identity(r) == identity(failure) and r.blocked_reason), None
        )
        if blocked is None:
            return None
        validation = evaluate(project, blocked, self.validator.baseline, self.validator.baseline)
        return DebugRun(
            initial_context,
            AnalysisReport("Validation environment is unavailable", 1.0, blocked.blocked_reason),
            (), validation, 0,
        )

    def _attempt_repairs(
        self,
        initial_context: DebugContext,
        isolated_project: ProjectInfo,
        context: DebugContext,
        workspace: Path,
        finding_id: str | None,
    ) -> DebugRun:
        analysis: AnalysisReport | None = None
        proposal: PatchProposal | None = None
        proposals: list[PatchProposal] = []
        validation: ValidationReport | None = None
        originals: dict[str, bytes | None] = {}
        patch_errors: list[str] = []

        for attempt in range(1, self.max_attempts + 1):
            analysis = self.analyzer.analyze(context)
            proposal = self.fixer.propose(context, analysis)
            if proposal.status == "no_patch":
                proposals.append(proposal)
                return DebugRun(initial_context, analysis, tuple(proposals), validation, attempt,
                                patch_errors=tuple(patch_errors))
            assert proposal.unified_diff is not None
            try:
                remember_originals(workspace, proposal.unified_diff, originals)
                apply_unified_diff(workspace, proposal.unified_diff)
            except (InvalidUnifiedDiffError, PatchApplicabilityError) as exc:
                patch_errors.append(f"Attempt {attempt}: {type(exc).__name__}: {exc}")
                if attempt == self.max_attempts:
                    raise type(exc)("Retry limit reached. " + " | ".join(patch_errors)) from None
                context = replace(context, patch_feedback=tuple(patch_errors))
                continue
            proposals.append(proposal)
            validation = self._validate_repair(isolated_project, originals, attempt, patch_errors)
            if validation.passed:
                run = DebugRun(initial_context, analysis, tuple(proposals), validation, attempt,
                               patch_errors=tuple(patch_errors), finding_id=finding_id)
                return self._persist_validated_run(run, workspace, originals)
            failed = validation.targeted if validation.targeted and not validation.targeted.passed else next(
                (result for result in validation.results if not result.passed and not result.blocked_reason
                 and (not validation.targeted or any(item.startswith(result.name + ":") for item in validation.regressions))), None)
            if failed is None and validation.syntax and not validation.syntax.passed and not validation.syntax.blocked_reason:
                failed = validation.syntax
            if failed is not None and failed.blocked_reason:
                break
            if failed is None:
                break
            context = replace(build_debug_context(isolated_project, failed), patch_feedback=tuple(patch_errors))

        assert analysis is not None and proposal is not None and validation is not None
        return DebugRun(initial_context, analysis, tuple(proposals), validation, attempt,
                        patch_errors=tuple(patch_errors), finding_id=finding_id)

    def _validate_repair(
        self, project: ProjectInfo, originals: dict[str, bytes | None], attempt: int, patch_errors: list[str]
    ) -> ValidationReport:
        syntax = syntax_check(project, originals)
        if not syntax.passed:
            patch_errors.append(f"Attempt {attempt}: repair syntax pre-validation failed: {syntax.stderr}")
            pin = getattr(self.validator, "pinned_plan", None) or {}
            mechanism = pin.get("verifier", "restricted-ast-runtime")
            return syntax_rejection(project, syntax, getattr(self.validator, "baseline", ()),
                                    confirmation_verifier=mechanism, repair_verifier=mechanism)
        validation = self.validator.validate(project)
        if validation.syntax is None:
            validation = replace(validation, syntax=syntax)
        return validation

    def _persist_validated_run(
        self, run: DebugRun, workspace: Path, originals: dict[str, bytes | None]
    ) -> DebugRun:
        try:
            patch_path, report_path = save_validated_repair(run, workspace, originals)
        except (OSError, ValueError) as exc:
            return replace(run, artifact_error=f"Artifact-save error ({type(exc).__name__}): could not persist the repair patch/report pair. Check the project .aidebug directory and permissions.")
        return replace(run, validated_patch_path=patch_path, debug_report_path=report_path)
