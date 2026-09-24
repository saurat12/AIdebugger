"""Provider-neutral Analyzer -> Fixer -> Validator debugging agent."""

from pathlib import Path
from dataclasses import replace
from typing import Protocol

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
from .workspace import apply_unified_diff, isolated_workspace


class Analyzer(Protocol):
    """Find and explain the likely root cause."""

    def analyze(self, context: DebugContext) -> AnalysisReport: ...


class Fixer(Protocol):
    """Generate a patch from evidence and an analysis."""

    def propose(self, context: DebugContext, analysis: AnalysisReport) -> PatchProposal: ...


class Validator(Protocol):
    """Run checks against a patched workspace."""

    def validate(self, project: ProjectInfo) -> ValidationReport: ...


class CheckValidator:
    """Default validator backed by the repository's discovered checks."""

    def __init__(self, timeout_seconds: float = 120) -> None:
        self.timeout_seconds = timeout_seconds

    def validate(self, project: ProjectInfo) -> ValidationReport:
        results = run_checks(project, timeout_seconds=self.timeout_seconds)
        return ValidationReport(
            passed=bool(results) and all(result.passed for result in results),
            results=results,
            workspace=project.root,
        )


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
    ) -> DebugRun:
        """Try to repair one failed check without modifying the active repository."""

        context = build_debug_context(project, failure, changed_files=changed_files, git_diff=git_diff)
        initial_context = context
        analysis: AnalysisReport | None = None
        proposal: PatchProposal | None = None
        proposals: list[PatchProposal] = []
        validation: ValidationReport | None = None
        originals: dict[str, bytes | None] = {}

        with isolated_workspace(project.root) as workspace:
            isolated_project = ProjectInfo(
                root=workspace,
                project_types=project.project_types,
                checks=project.checks,
            )
            for attempt in range(1, self.max_attempts + 1):
                analysis = self.analyzer.analyze(context)
                proposal = self.fixer.propose(context, analysis)
                if proposal.status == "no_patch":
                    proposals.append(proposal)
                    return DebugRun(initial_context, analysis, tuple(proposals), validation, attempt)
                assert proposal.unified_diff is not None
                remember_originals(workspace, proposal.unified_diff, originals)
                apply_unified_diff(workspace, proposal.unified_diff)
                proposals.append(proposal)
                validation = self.validator.validate(isolated_project)
                if validation.passed:
                    run = DebugRun(initial_context, analysis, tuple(proposals), validation, attempt)
                    patch_path, report_path = save_validated_repair(run, workspace, originals)
                    return replace(run, validated_patch_path=patch_path, debug_report_path=report_path)
                failed = next((result for result in validation.results if not result.passed), None)
                if failed is None:
                    break
                context = build_debug_context(isolated_project, failed)

        assert analysis is not None and proposal is not None and validation is not None
        return DebugRun(initial_context, analysis, tuple(proposals), validation, self.max_attempts)
