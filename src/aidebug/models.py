"""Data models shared by discovery, execution, and AI integrations."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class CheckSpec:
    """A project-local command that can be run by the debugger."""

    name: str
    command: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ProjectInfo:
    """Detected project and the checks appropriate for it."""

    root: Path
    project_types: tuple[str, ...]
    checks: tuple[CheckSpec, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CheckResult:
    """Captured result of one check command."""

    name: str
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def passed(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class DebugContext:
    """Evidence supplied to a debugging agent after a failed check."""

    project: ProjectInfo
    failed_check: CheckResult
    relevant_files: tuple[Path, ...]
    git_diff: str | None = None


@dataclass(frozen=True)
class AnalysisReport:
    """Analyzer's structured hypothesis about the failure."""

    root_cause: str
    confidence: float
    reasoning: str
    suspected_files: tuple[Path, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PatchProposal:
    """A proposed diff, or an explicit decision that no safe patch is available."""

    unified_diff: str | None
    explanation: str
    status: Literal["patch", "no_patch"] = "patch"

    def __post_init__(self) -> None:
        if self.status not in ("patch", "no_patch"):
            raise ValueError("Fixer status must be 'patch' or 'no_patch'")
        if not isinstance(self.explanation, str) or not self.explanation.strip():
            raise ValueError("Fixer explanation must be a non-empty string")
        if self.status == "no_patch":
            if self.unified_diff is not None:
                raise ValueError("Fixer unified_diff must be null for no_patch")
        elif not isinstance(self.unified_diff, str) or not self.unified_diff.strip():
            raise ValueError("Fixer unified_diff must be a non-empty string for patch")


@dataclass(frozen=True)
class ValidationReport:
    """Validator's result after a proposed patch is applied."""

    passed: bool
    results: tuple[CheckResult, ...]
    workspace: Path


@dataclass(frozen=True)
class DebugRun:
    """Complete result of one orchestrated debugging attempt."""

    initial_context: DebugContext
    analysis: AnalysisReport
    proposals: tuple[PatchProposal, ...]
    validation: ValidationReport | None
    attempts: int
    validated_patch_path: Path | None = None
    debug_report_path: Path | None = None
