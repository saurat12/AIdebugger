"""Target acceptance, bounded baselines, and conservative regression comparison."""

import re
from dataclasses import replace

from .models import CheckResult, ValidationReport
from .runner import classify_availability
from .validation_discovery import python_sources


def capture(results):
    return tuple(replace(classify_availability(result), stdout=result.stdout[:12000], stderr=result.stderr[:12000])
                 for result in results)


def identity(result):
    return result.name, result.command


def project_status(results):
    return ("NOT AVAILABLE" if not results else "BLOCKED" if any(r.blocked_reason for r in results)
            else "FAIL" if any(not r.passed for r in results) else "PASS")


def failure_evidence(result):
    text = result.stdout + "\n" + result.stderr
    # Normalize known volatile runner details only. Retain exception messages,
    # test names and source locations rather than comparing exit codes alone.
    text = re.sub(r"[A-Za-z]:[^\s\"']*\\aidebug-[^\\\s]+\\|/[^\s\"']*/aidebug-[^/\s]+/", "<workspace>/", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:seconds|secs|ms|s)\b", "<duration>", text)
    text = re.sub(r"0x[0-9a-fA-F]+", "<address>", text)
    return text.strip()


def compare_baseline(before, after):
    previous = {identity(item): item for item in before}
    current = {identity(item): item for item in after}
    comparisons, regressions = [], []
    for key, result in current.items():
        old = previous.get(key)
        if result.blocked_reason:
            description = "BLOCKED: " + result.blocked_reason
        elif result.passed:
            description = "resolved failure" if old and not old.passed else "PASS"
        elif old is None or old.passed:
            description = "newly introduced failure (regression)"
            regressions.append(result.name + ": " + description)
        elif old.blocked_reason:
            description = "newly observed failure; baseline could not execute (regression cannot be excluded)"
            regressions.append(result.name + ": " + description)
        elif failure_evidence(old) == failure_evidence(result) and old.returncode == result.returncode:
            description = "unchanged pre-existing failure"
        else:
            description = "worsened/changed failure (possible regression; review required)"
            regressions.append(result.name + ": " + description)
        comparisons.append(result.name + ": " + description)
    for key in previous.keys() - current.keys():
        message = previous[key].name + ": baseline check result missing (regression cannot be excluded)"
        regressions.append(message)
        comparisons.append(message)
    return tuple(comparisons), tuple(regressions)


def syntax_check(project):
    if "python" not in project.project_types:
        return None
    count = 0
    try:
        for path in python_sources(project.root):
            if path.stat().st_size > 120000:
                return CheckResult("Python syntax", ("bounded-compile",), 1, "", "Source exceeds syntax inspection limit", 0,
                                   "Source exceeds syntax inspection limit")
            # Compile bytes without importing, executing, or writing bytecode.
            compile(path.read_bytes(), path.relative_to(project.root).as_posix(), "exec")
            count += 1
    except SyntaxError as exc:
        return CheckResult("Python syntax", ("bounded-compile",), 1, "", f"{exc.filename}:{exc.lineno}: {exc.msg}", 0)
    except (OSError, ValueError) as exc:
        return CheckResult("Python syntax", ("bounded-compile",), 1, "", type(exc).__name__, 0, "Source could not be inspected")
    return CheckResult("Python syntax", ("bounded-compile",), 0, f"{count} Python files compiled without execution; bounded source scan", "", 0)


def evaluate(project, target, results, baseline=(), syntax=None, baseline_syntax=None, expected="Original failing check passes"):
    results, baseline = capture(results), capture(baseline)
    comparisons, regressions = compare_baseline(baseline, results)
    if syntax is not None and baseline_syntax is not None:
        syntax_comparison, syntax_regression = compare_baseline(capture((baseline_syntax,)), capture((syntax,)))
        comparisons += syntax_comparison
        regressions += syntax_regression
    status = project_status(results)
    accepted = target.passed and not regressions
    final = ("REPAIR FAILED" if not target.passed else "REGRESSION DETECTED" if regressions
             else "FULLY VALIDATED" if status == "PASS"
             else "TARGET FIX VERIFIED / PROJECT VALIDATION BLOCKED" if status == "BLOCKED"
             else "TARGET FIX VERIFIED")
    return ValidationReport(accepted, (*results, target), project.root, target, results, baseline, syntax,
                            status, final, comparisons, regressions,
                            ("No project-wide checks discovered",) if not results else (), expected)


def summary(report):
    lines = [f"Targeted Verification: {report.targeted.status if report.targeted else 'NOT AVAILABLE'}",
             f"Syntax Check: {report.syntax.status if report.syntax else 'NOT AVAILABLE'}",
             f"Project-wide validation: {report.project_status}",
             f"Final Repair Status: {report.final_status}"]
    lines.extend("Reason: " + result.blocked_reason for result in report.project_results if result.blocked_reason)
    return "\n".join(lines)
