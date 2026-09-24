import pytest

from aidebug.models import AnalysisReport, CheckResult, DebugContext, DebugRun, PatchProposal, ProjectInfo, ValidationReport
from aidebug.reports import render_repair_report, repair_risk


PATCH = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"


def report_run(tmp_path, evidence):
    failure = CheckResult("pytest", ("python", "-m", "pytest"), 1, "", evidence, 0.2)
    after = CheckResult("pytest", ("python", "-m", "pytest"), 0, "12 passed, 2 skipped", "", 0.1)
    return DebugRun(DebugContext(ProjectInfo(tmp_path, ("python",)), failure, ()),
                    AnalysisReport("Bad import", 0.85, "Import fails before execution"),
                    (PatchProposal(PATCH, "Correct the import."),), ValidationReport(True, (after,), tmp_path), 1)


@pytest.mark.parametrize("evidence, category", [
    ("ERROR at setup of test_app\nModuleNotFoundError: No module named 'dep'\n1 error", "Dependency/import failure"),
    ("PermissionError: permission denied", "Environment failure"),
    ("TOMLDecodeError: invalid configuration", "Configuration failure"),
    ("ERROR at setup: fixture 'client' not found", "Test-infrastructure failure"),
    ("AssertionError: expected 2", "suspected application bug"),
    ("unknown failure", "Undetermined"),
])
def test_evidence_based_finding_types(tmp_path, evidence, category):
    report = render_repair_report(report_run(tmp_path, evidence), PATCH, ["app.py"])
    assert category in report
    if "setup" in evidence:
        assert "Tests failed during setup/collection" in report
        assert "does not demonstrate an application assertion failure" in report


def test_report_counts_confidence_limits_and_raw_patch(tmp_path):
    run = report_run(tmp_path, "1 failed, 11 passed\nAssertionError")
    report = render_repair_report(run, PATCH, ["app.py"])
    assert "85%" in report
    assert "1 failed, 11 passed" in report
    assert "12 passed, 2 skipped" in report
    assert "FAIL pytest: exit 1" in report
    assert "PASS pytest: exit 0" in report
    assert "app.py: Attempt 1: Correct the import." in report
    assert "does not establish that the whole project is bug-free" in report
    assert "other initial results are not available" in report
    assert report.endswith(PATCH + "```\n")


@pytest.mark.parametrize("patch, expected", [
    (PATCH, "low"),
    (PATCH + PATCH.replace("app.py", "other.py"), "medium"),
    ("--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+new\n", "medium"),
    ("--- a/app.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n", "high"),
    ("".join(PATCH.replace("app.py", f"file{i}.py") for i in range(6)), "high"),
])
def test_risk_is_deterministic_and_explained(patch, expected):
    level, explanation = repair_risk(patch)
    assert level == expected
    assert "High: any deletion, >5 files, or >200 changed lines" in explanation
    assert "does not measure semantic or security risk" in explanation


def test_missing_counts_are_not_invented(tmp_path):
    run = report_run(tmp_path, "")
    report = render_repair_report(run, PATCH, ["app.py"])
    assert "test counts unavailable" in report
    assert "No stdout/stderr evidence was captured" in report
