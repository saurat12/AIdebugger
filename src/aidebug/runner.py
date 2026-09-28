"""Run project checks and capture their output."""

import subprocess
import time
import re
from dataclasses import replace

from .models import CheckResult, CheckSpec, ProjectInfo


def run_check(project: ProjectInfo, check: CheckSpec, timeout_seconds: float = 120) -> CheckResult:
    """Run one check from the repository root."""

    started = time.monotonic()
    if getattr(check, "blocked_reason", None):
        return CheckResult(check.name, check.command, 127, "", check.blocked_reason, 0, check.blocked_reason)
    try:
        completed = subprocess.run(
            check.command,
            cwd=project.root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        result = CheckResult(
            name=check.name,
            command=check.command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
        )
        return classify_availability(result)
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
            blocked_reason="Configured validation executable could not start: " + str(exc),
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


def classify_availability(result: CheckResult) -> CheckResult:
    """Recognize launch/tool failures, not application test failures."""
    if result.passed or result.blocked_reason:
        return result
    output = result.stderr + "\n" + result.stdout
    missing = re.search(r"(?:^|\n)[^\n]*: No module named ([\w.]+)\s*(?:\n|$)", output)
    module = result.command[result.command.index("-m") + 1] if "-m" in result.command and result.command.index("-m") + 1 < len(result.command) else None
    if missing and missing.group(1) == module:
        return replace(result, blocked_reason=f"{module} is unavailable in the project environment")
    if result.returncode == 127 or re.search(r"(?:command not found|is not recognized as an internal or external command)", output, re.I):
        return replace(result, blocked_reason="Configured validation tool is unavailable: " + output.strip()[:1000])
    return result
