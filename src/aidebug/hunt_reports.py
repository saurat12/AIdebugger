"""Project-local records of all hunt outcomes, including non-confirmed findings."""

import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4


def save_hunt_report(root, run):
    folder = root.resolve() / ".aidebug"
    if folder.is_symlink() or folder.resolve() != folder:
        raise ValueError("Hunt artifact directory must stay inside the project")
    folder.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S_%f") + "_" + uuid4().hex[:12]
    run = replace(run, findings_path=folder / f"hunt_findings_{stamp}.json", report_path=folder / f"hunt_report_{stamp}.md")
    counts = Counter(finding.status for finding in run.findings)
    lines = ["# AIdebug Hunt Report", "", f"Mode: **{run.mode}**", "", "## Strategy Coverage", "",
             *[f"- {strategy}" for strategy in run.strategies], "",
             "Read-only review uses bounded source excerpts and code tools. Python static strategies scan up to 300 entries / 1 MB; generated checks use at most 64 numeric cases per function. Strategies do not cover every language or behavior.", "",
             "Coverage-gap analysis uses coverage.json when available; otherwise it checks direct Python test references as a heuristic, not measured coverage. Supplied coverage can be stale; indirect test calls can be missed.", "",
             "Generated cases use a restricted AST evaluator, never arbitrary generated scripts. Only declared doctest, invariant, totality, or equivalence contradictions authorize repair. Other suspicious patterns remain non-confirmed.", "",
             "## Existing Surfaced Failures", ""]
    failures = [result for result in run.existing_checks if not result.passed]
    lines.extend(f"- {result.name}: exit {result.returncode}" for result in failures)
    if not failures:
        lines.append("None surfaced by recorded checks." if run.existing_checks else "No existing checks were discovered.")
    lines.extend(["", "## Existing Check Results", "", *[f"- {'PASS' if result.passed else 'FAIL'} {result.name}: exit {result.returncode}" for result in run.existing_checks],
                  "", "## Proactively Discovered Findings", "",
                  f"Confirmed bugs: {counts['confirmed']}; high confidence: {counts['high_confidence']}; unconfirmed: {counts['unconfirmed']}; rejected hypotheses: {counts['rejected']}.", ""])
    for finding in run.findings:
        record = finding.record()
        lines.extend([f"### {record['finding_id']} — {record['verification_status']}", "",
                      f"File: {record['file']} — symbol: {record['symbol']}", "",
                      f"Category: {record['category']}; confidence: {record['confidence']:.0%}", "",
                      f"Hypothesis: {record['hypothesis']}", "", f"Evidence: {record['evidence']}", "",
                      f"Reproduction strategy: {record['reproduction_strategy']}", "",
                      f"Verification evidence: {record['verification_evidence']}", ""])
    lines.extend(["## Repair Outcomes", ""])
    for repair in run.repairs:
        lines.append(f"- {'Validated' if repair.validation and repair.validation.passed else 'Not validated'} after {repair.attempts} attempt(s); patch: {repair.validated_patch_path}; report: {repair.debug_report_path}")
    lines.extend(f"- {error}" for error in run.repair_errors)
    lines.extend(["", "Non-confirmed findings were not auto-repaired. Separate repairs are independently validated, not a combined batch. Passing checks or no findings does not establish that the project is bug-free.", ""])
    with run.findings_path.open("x", encoding="utf-8") as handle:
        json.dump(run.record(), handle, default=str, indent=2)
    with run.report_path.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return run
