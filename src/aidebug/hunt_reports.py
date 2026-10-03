"""Project-local records of all hunt outcomes, including non-confirmed findings."""

import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4


def save_hunt_report(root, run):
    from .validation import project_status
    folder = root.resolve() / ".aidebug"
    if folder.is_symlink() or folder.resolve() != folder:
        raise ValueError("Hunt artifact directory must stay inside the project")
    folder.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S_%f") + "_" + uuid4().hex[:12]
    run = replace(run, findings_path=folder / f"hunt_findings_{stamp}.json", report_path=folder / f"hunt_report_{stamp}.md")
    counts = Counter(finding.status for finding in run.bug_findings)
    lines = ["# AIdebug Hunt Report", "", f"Mode: **{run.mode}**", "", "## Strategy Coverage", "",
             *[f"- {strategy}" for strategy in run.strategies], "",
             "Read-only review uses bounded source excerpts and code tools. Python static strategies scan up to 300 entries / 1 MB; generated checks use at most 64 numeric cases per function. Strategies do not cover every language or behavior.", "",
             "Coverage-gap analysis uses coverage.json when available; otherwise it checks direct Python test references as a heuristic, not measured coverage. Supplied coverage can be stale; indirect test calls can be missed.", "",
             "Verification plans are strict JSON operation lists. They can call safe local symbols, construct objects, inspect state and argument mutation, capture bounded stdout/stderr, use deterministic stubs, virtual files, and enforce a subprocess deadline. The worker runs in an isolated project copy with a minimal environment and interprets an AST allowlist; it does not import target modules or execute generated Python/shell.", "",
             "Verification routes through deterministic capabilities, supplied or cached structured plans, and bounded reproduction data. If no supported safe path establishes or contradicts the claim, retain it as HIGH_CONFIDENCE or UNVERIFIABLE. A generated harness is never run as arbitrary source code.", "",
             "## Existing Surfaced Failures", ""]
    failures = [result for result in run.existing_checks if not result.passed]
    lines.extend([f"Project-wide validation: {project_status(run.existing_checks)}", ""])
    lines.extend(f"- {result.name}: exit {result.returncode}" for result in failures)
    if not failures:
        lines.append("None surfaced by recorded checks." if run.existing_checks else "No existing checks were discovered.")
    lines.extend(["", "## Existing Check Results", "", *[f"- {'PASS' if result.passed else 'FAIL'} {result.name}: exit {result.returncode}" for result in run.existing_checks],
                  "", "## Bug Findings", "",
                  f"Confirmed bugs: {counts['confirmed']}; high confidence: {counts['high_confidence']}; unconfirmed: {counts['unconfirmed']}; rejected hypotheses: {counts['rejected']}.", ""])
    metric_labels = {"bugs_discovered": "Bugs discovered", "bugs_confirmed": "Bugs confirmed",
                     "bugs_repairable": "Bugs repairable",
                     "bugs_blocked_by_unspecified_expected_behavior": "Bugs blocked by unspecified expected behavior",
                     "bugs_unverifiable": "Bugs unverifiable", "bugs_rejected": "Bugs rejected",
                     "non_bug_observations": "Non-bug observations"}
    lines.extend(["## Finding Metrics", "", *[f"- {metric_labels[name]}: {value}" for name, value in run.bug_metrics.items()],
                  "", "Metrics count deduplicated behavioral bug hypotheses only; quality observations are excluded. Unverifiable includes high-confidence and unconfirmed hypotheses; confidence is not confirmation.", ""])
    lines.extend(["## Verification Coverage", "",
                  f"- Safe plan coverage: {run.verification_metrics['verification_coverage']:.0%}",
                  f"- Plans independently executed to a conclusion: {run.verification_metrics['verification_plans_executed']}",
                  f"- Pinned plans reused after repair: {run.verification_metrics['pinned_plans_reused']}",
                  f"- Cached-plan conclusions: {run.verification_metrics['cached_plan_conclusions']}",
                  f"- Structured-spec conclusions: {run.verification_metrics['structured_spec_conclusions']}",
                  f"- Reproduction-builder conclusions: {run.verification_metrics['reproduction_builder_conclusions']}",
                  f"- Unsupported AST capabilities: {run.verification_metrics['unsupported_ast_capabilities']}",
                  f"- Unsupported module-fragment cases: {run.verification_metrics['unsupported_module_fragment_cases']}",
                  f"- Module-fragment cases: {run.verification_metrics['module_fragment_cases']}",
                  f"- Hidden edge-case findings: {run.verification_metrics['hidden_edge_case_findings']}", ""])
    groups = [("Confirmed Bugs", [f for f in run.bug_findings if f.status == "confirmed"]),
              ("High-Confidence Unverified Bugs", [f for f in run.bug_findings if f.status == "high_confidence"]),
              ("Unconfirmed Bug Hypotheses", [f for f in run.bug_findings if f.status == "unconfirmed"]),
              ("Rejected Bug Hypotheses", [f for f in run.bug_findings if f.status == "rejected"])]
    if run.include_quality:
        groups.append(("Code Quality Observations", run.observations))
    for heading, findings in groups:
        lines.extend(["", "## " + heading, ""])
        if not findings:
            lines.append("None recorded.")
        for finding in findings:
            record = finding.record()
            lines.extend([f"### {record['finding_id']} — {record['verification_status']}", "",
                          f"File: {record['file']} — symbol: {record['symbol']}", "",
                          f"Verification target: {record['verification_target']}", "",
                          f"Category: {record['category']}; confidence: {record['confidence']:.0%}", "",
                          f"Hypothesis: {record['hypothesis']}", "", f"Evidence: {record['evidence']}", "",
                          f"Reproduction strategy: {record['reproduction_strategy']}", "",
                          f"Verification evidence: {record['verification_evidence']}", "",
                          f"Confirmation source: {(record.get('verification_plan') or {}).get('source', 'not recorded')}", "",
                          f"Verification state: {record['verification_state']}",
                          f"Repair authorization: {record['repair_authorization']}", ""])
            if record.get("verification_plan"):
                lines.extend(["Normalized verification plan:", "", "```json", json.dumps(record["verification_plan"], ensure_ascii=True, indent=2), "```", ""])
            if finding.signals:
                lines.extend(["Detector signals (independent evidence and confidence):", ""])
                for signal in finding.signals:
                    claim = signal["hypothesis"]
                    lines.extend([f"- {signal['detector']}: {claim['description']} (confidence {claim['confidence']:.0%}; {signal['verification_status']})",
                                  f"  Evidence: {json.dumps(claim['evidence'], ensure_ascii=True)}",
                                  f"  Verification: {signal['verification_evidence']}"])
    lines.extend(["", "## Unverified Bugs", ""])
    unresolved = [finding for finding in run.bug_findings if finding.status in ("high_confidence", "unconfirmed")]
    lines.extend(f"- {finding.finding_id}: {finding.hypothesis.suspected_file}: {finding.evidence}" for finding in unresolved)
    if not unresolved:
        lines.append("None recorded.")
    lines.extend(["", "## Syntax-Blocked Finding Retries", ""])
    retried = [finding for finding in run.bug_findings if (finding.verification_plan or {}).get("retried_after_syntax_repair")]
    pending_syntax = [finding for finding in unresolved if (finding.verification_plan or {}).get("unsupported_reason") ==
                      "Behavioral verification blocked by a separate syntax defect"]
    lines.extend(f"- {finding.finding_id}: retried after syntax repair {(finding.verification_plan or {})['retried_after_syntax_repair']}; "
                 f"result {finding.status}; verified in an isolated workspace only." for finding in retried)
    lines.extend(f"- {finding.finding_id}: not retried; no validated syntax repair was available." for finding in pending_syntax)
    if not retried and not pending_syntax:
        lines.append("No findings were awaiting a syntax repair.")
    lines.extend(["## Repair Outcomes", ""])
    for repair in run.repairs:
        lines.append(f"- {repair.finding_id}: {'Validated' if repair.validation and repair.validation.passed else 'Not validated'} after {repair.attempts} attempt(s)")
        for label, path in (("Validated patch", repair.validated_patch_path), ("Debug report", repair.debug_report_path)):
            if path:
                lines.append(f"- {label}: {path}")
        lines.extend(f"- {repair.finding_id}: {error}" for error in repair.patch_errors)
        if repair.artifact_error:
            lines.append(f"- {repair.finding_id}: {repair.artifact_error}")
        from .reports import validation_sections
        for title, body in validation_sections(repair.validation):
            lines.extend(["", f"### {repair.finding_id}: {title}", "", body])
    lines.extend(f"- {error}" for error in run.repair_errors)
    lines.extend(["", "Non-confirmed findings were not auto-repaired. Separate repairs are independently validated, not a combined batch. Passing checks or no findings does not establish that the project is bug-free.", ""])
    created = []
    try:
        with run.findings_path.open("x", encoding="utf-8") as handle:
            created.append(run.findings_path)
            json.dump(run.record(), handle, default=str, indent=2)
        with run.report_path.open("x", encoding="utf-8") as handle:
            created.append(run.report_path)
            handle.write("\n".join(lines))
    except OSError:
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return run
