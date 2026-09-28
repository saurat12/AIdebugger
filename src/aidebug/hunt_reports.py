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
             "Verification plans are strict JSON operation lists. They can call public local symbols, construct objects, inspect state and argument mutation, capture bounded stdout/stderr, use deterministic random stubs, virtual files, and enforce a subprocess deadline. The worker runs in an isolated project copy with a minimal environment and interprets an AST allowlist; it does not import target modules or execute generated Python/shell. The optional model planner only proposes plan data, which is schema-validated before execution.", "",
             "Verification fallback: run an existing structured reproduction first; if missing, ask the planner for a restricted operation plan; registered specialized/static verifiers may supply additional independent evidence. If no supported safe path establishes or contradicts the claim, retain it as HIGH_CONFIDENCE or UNVERIFIABLE. A generated harness is never run as arbitrary source code.", "",
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
                  f"- Plans requiring replanning: {run.verification_metrics['plans_requiring_replanning']}",
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
                          f"Category: {record['category']}; confidence: {record['confidence']:.0%}", "",
                          f"Hypothesis: {record['hypothesis']}", "", f"Evidence: {record['evidence']}", "",
                          f"Reproduction strategy: {record['reproduction_strategy']}", "",
                          f"Verification evidence: {record['verification_evidence']}", "",
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
