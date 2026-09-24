"""Run project checks and capture their output."""

import subprocess
import time

from .models import CheckResult, CheckSpec, ProjectInfo


def run_check(project: ProjectInfo, check: CheckSpec, timeout_seconds: float = 120) -> CheckResult:
    """Run one check from the repository root."""

    started = time.monotonic()
    try:
        completed = subprocess.run(
            check.command,
            cwd=project.root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return CheckResult(
            name=check.name,
            command=check.command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        return CheckResult(
            name=check.name,
            command=check.command,
            returncode=124,
            stdout=_text(exc.stdout),
            stderr=f"Check timed out after {timeout_seconds:g} seconds.\n{_text(exc.stderr)}",
            duration_seconds=time.monotonic() - started,
        )
    except OSError as exc:
        return CheckResult(
            name=check.name,
            command=check.command,
            returncode=127,
            stdout="",
            stderr=str(exc),
            duration_seconds=time.monotonic() - started,
        )


def run_checks(
    project: ProjectInfo,
    timeout_seconds: float = 120,
    stop_on_failure: bool = True,
) -> tuple[CheckResult, ...]:
    """Run discovered checks in order, stopping at the first failure by default."""

    results: list[CheckResult] = []
    for check in project.checks:
        result = run_check(project, check, timeout_seconds)
        results.append(result)
        if stop_on_failure and not result.passed:
            break
    return tuple(results)


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value