"""Concise repair reports grounded in captured checks and patch scope."""

import re

from .models import CheckResult, DebugRun
from .workspace import parse_unified_diff


def _fence(text: str, language: str = "") -> str:
    fence = "`" * max(3, max((len(m.group()) for m in re.finditer(r"`+", text)), default=0) + 1)
    return f"{fence}{language}\n{text}" + ("" if text.endswith("\n") else "\n") + fence


def _finding(run: DebugRun) -> tuple[str, str]:
    failure = run.initial_context.failed_check
    evidence = (failure.stdout + "\n" + failure.stderr).lower()
    setup = any(term in evidence for term in ("error at setup", "errors during collection", "error collecting", "fixture "))
    phase = ("Tests failed during setup/collection; this failure does not demonstrate an application assertion failure."
             if setup else "Failure phase is not established by the captured output.")
    if any(term in evidence for term in ("modulenotfounderror", "no module named", "distributionnotfound", "cannot import name")):
        return "Dependency/import failure", phase
    if failure.returncode in (124, 127) or any(term in evidence for term in ("permissionerror", "permission denied", "no python at", "command not found")):
        return "Environment failure", phase
    if any(term in evidence for term in ("configurationerror", "configparser", "tomldecodeerror", "unrecognized arguments", "invalid configuration")):
        return "Configuration failure", phase
    if setup or any(term in evidence for term in ("pytest.internalerror", "internalerror>", "pluginvalidationerror")):
        return "Test-infrastructure failure", phase
    if "assertionerror" in evidence or re.search(r"(?m)^\s*e\s+assert\b", evidence):
        return "Application/test expectation mismatch (suspected application bug)", "An application assertion failed; the test expectation may also require review."
    return "Undetermined", phase


def _check(result: CheckResult) -> str:
    output = result.stdout + "\n" + result.stderr
    # Take the last occurrence for each label to avoid counting progress twice.
    counts = {}
    for match in re.finditer(r"\b(\d+)\s+(passed|failed|skipped|deselected|xfailed|xpassed|errors?|warnings?)\b", output, re.I):
        counts[match.group(2).lower()] = int(match.group(1))
    summary = ", ".join(f"{value} {label}" for label, value in counts.items()) or "test counts unavailable"
    return f"- {result.status} {result.name}: exit {result.returncode}; {result.duration_seconds:g}s; {summary}."


def validation_sections(report):
    if report is None:
        return [("Final Repair Status", "REPAIR FAILED: no validation performed")]
    target = report.targeted
    return [
        ("Targeted Verification", _check(target) if target else "Not separately recorded"),
        ("Expected Corrected Behavior", report.expected_behavior),
        ("Observed Corrected Behavior", _fence((target.stdout + "\n" + target.stderr).strip()[:8000]) if target else "Not recorded"),
        ("Syntax Check", _check(report.syntax) if report.syntax else "NOT AVAILABLE"),
        ("Project-Wide Validation", report.project_status + "\n\n" + "\n".join(_check(r) for r in report.project_results)),
        ("Validation Strategy", "Pinned targeted reproduction first; native checks compared to an isolated pre-repair baseline. Syntax compilation does not execute code and is not behavioral validation."),
        ("Confirmation Verifier", report.confirmation_verifier),
        ("Repair Verification Verifier", report.repair_verifier),
        ("Pinned Plan Reused", str(report.plan_reused).lower()),
        ("Checks Used", "\n".join(f"- {r.name}: {' '.join(r.command)}" for r in report.results) or "None"),
        ("Checks Skipped", "\n".join(report.skipped) or "None recorded. Discovery and syntax inspection are bounded; undiscovered checks were not run."),
        ("Blocked Checks", "\n".join(f"- {r.name}: {r.blocked_reason}" for r in (*report.project_results, *((report.syntax,) if report.syntax else ())) if r.blocked_reason) or "None"),
        ("Baseline Comparison", "\n".join(report.comparisons) or "No comparable baseline results"),
        ("Regressions", "\n".join(report.regressions) or "No demonstrated regression in comparable recorded checks; this is not proof of absence."),
        ("Final Repair Status", report.final_status),
        ("Validation Scope / Limitations", report.scope + " Unchanged pre-existing failures and blocked checks remain unresolved. Bounded checks do not establish untested behavior, production safety, or security."),
    ]


def repair_risk(patch: str) -> tuple[str, str]:
    files = parse_unified_diff(patch) if patch.strip() else ()
    changed_lines = sum(line.startswith(("+", "-")) for file in files for hunk in file.hunks for line in hunk.lines)
    deleted = any(file.new_name == "/dev/null" for file in files)
    if deleted or len(files) > 5 or changed_lines > 200:
        level = "high"
    elif len(files) > 1 or changed_lines > 20 or any(file.old_name == "/dev/null" for file in files):
        level = "medium"
    else:
        level = "low"
    return level, (
        f"Scope: {len(files)} file(s), {changed_lines} added/removed lines. "
        "High: any deletion, >5 files, or >200 changed lines. "
        "Otherwise medium: any creation, >1 file, or >20 changed lines. Otherwise low. "
        "This scope rating does not measure semantic or security risk."
    )


def render_repair_report(run: DebugRun, patch: str, changed: list[str]) -> str:
    failure = run.initial_context.failed_check
    finding, phase = _finding(run)
    reasons: dict[str, list[str]] = {name: [] for name in changed}
    for index, proposal in enumerate(run.proposals, 1):
        if not proposal.unified_diff:
            continue
        for file in parse_unified_diff(proposal.unified_diff):
            for name in set((file.old_name, file.new_name)) & reasons.keys():
                reason = f"Attempt {index}: {proposal.explanation}"
                if reason not in reasons[name]:
                    reasons[name].append(reason)
    files = "\n".join(f"- {name}: {'; '.join(reasons[name]) or 'No file-specific rationale was supplied.'}" for name in changed) or "No net file changes."
    evidence = (failure.stderr + "\n" + failure.stdout).strip()
    evidence = evidence[:2000] + ("\n[Evidence truncated]" if len(evidence) > 2000 else "")
    after = run.validation.results if run.validation else ()
    risk, criteria = repair_risk(patch)
    sections = [
        ("Finding ID", run.finding_id or "Surfaced project failure"),
        ("Project", str(run.initial_context.project.root)),
        ("Finding Type", f"{finding}. Classification uses captured failure signals, not a confirmed diagnosis.\n\n{phase}"),
        ("Initial Failure", ("Proactively discovered mismatch in a declared source example; not an existing-test failure.\n\n" if failure.name.startswith("hunt:") else "") + f"{failure.name}: {' '.join(failure.command)} (exit {failure.returncode})."),
        ("Failure Evidence", _fence(evidence) if evidence else "No stdout/stderr evidence was captured."),
        ("Root Cause", run.analysis.root_cause),
        ("Confidence", f"Analyzer confidence: {run.analysis.confidence:.0%}. Model estimate, not a calibrated probability.\n\n{run.analysis.reasoning}"),
        ("Repair", "\n".join(f"- Attempt {i}: {p.explanation}" for i, p in enumerate(run.proposals, 1))),
        ("Files Changed", files + "\n\nRationales are the Fixer's explanations for attempts touching each file; multi-file explanations may be shared."),
        ("Validation Before Repair", "\n".join(_check(r) for r in run.validation.baseline) if run.validation and run.validation.baseline else _check(failure) + "\n\nOnly the triggering failed check is retained here; other initial results are not available in this report."),
        ("Validation After Repair", f"Isolated validation passed after {run.attempts} attempt(s).\n\n" + ("\n".join(_check(result) for result in after) or "No individual check results were recorded.")),
        ("What Was Verified", f"{len(after)} recorded check(s), {sum(result.passed for result in after)} passed in the isolated workspace.\n\n" + "\n".join(f"- {' '.join(result.command)}" for result in after)),
        ("What Was Not Verified", "Passing discovered checks does not establish that the whole project is bug-free. Undiscovered tests, untested behavior, production execution, and security/performance properties were not established. The repair has not been applied to or revalidated in the real project. Skipped/deselected tests, if reported, were not verified."),
        ("Repair Risk", f"{risk.capitalize()}. {criteria}"),
        *validation_sections(run.validation),
        ("Validated Patch", _fence(patch, "diff")),
    ]
    return "# AIdebug Validated Repair\n\n" + "\n\n".join(f"## {title}\n\n{body}" for title, body in sections) + "\n"
