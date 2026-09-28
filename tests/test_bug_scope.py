import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug.hunt import BugHypothesis, Finding, HuntRun, hunt_main, hunt_project
from aidebug.hunt_registry import collect_findings
from aidebug.hunt_strategies import HuntVerifier, StaticAnalysis
from aidebug.models import ProjectInfo


def hypothesis(category="other", **kwargs):
    return BugHypothesis("app.py", "f", "Incorrect behavior", "Source evidence", .9, "Reproduce safely", category, **kwargs)


@pytest.mark.parametrize("category", ["coverage_gap", "style", "verbosity", "maintainability", "dead_code", "code_smell", "performance_suggestion"])
def test_quality_is_excluded_from_bug_metrics(category):
    finding = Finding(hypothesis(category), "high_confidence", "Pattern only")
    run = HuntRun((), (finding,), ())
    assert not finding.is_bug
    assert run.metrics["bugs_discovered"] == 0
    assert all(value == 0 for key, value in run.bug_metrics.items() if key != "non_bug_observations")
    assert run.bug_metrics["non_bug_observations"] == 1
    assert run.record()["findings"] == []
    assert "quality_observations" not in run.record()
    assert len(replace(run, include_quality=True).record()["quality_observations"]) == 1


def test_mutable_default_pattern_does_not_establish_bug(tmp_path):
    (tmp_path / "app.py").write_text("def f(values=[]):\n    return len(values)\n")
    project = ProjectInfo(tmp_path, ("python",))
    item, = StaticAnalysis().hunt(project)
    finding = HuntVerifier().verify(project, item)
    assert finding.status != "confirmed"
    assert not finding.is_bug
    assert not HuntRun((), (finding,), ()).bug_findings


@pytest.mark.parametrize("source,spec", [
    ("def f():\n    return [1][2]\n", {"kind": "expected_exception", "expected_exception": "IndexError"}),
    ("def f():\n    return 0\n", {"kind": "equals", "expected": 1}),
    ("def f():\n    handle = open('input.txt')\n    return handle.read()\n", {"kind": "file_resource_check", "files": {"input.txt": "data"}, "expected_open_resources": 0}),
    ("def f():\n    while True:\n        pass\n", {"kind": "timeout", "timeout_ms": 100}),
])
def test_concrete_failures_are_counted(tmp_path, source, spec):
    (tmp_path / "app.py").write_text(source)
    finding = HuntVerifier().verify(ProjectInfo(tmp_path, ("python",)), hypothesis(verification_spec=spec))
    assert finding.is_bug and finding.status == "confirmed", finding.evidence
    assert HuntRun((), (finding,), ()).bug_metrics["bugs_confirmed"] == 1


def test_security_risk_with_concrete_failure_stays_bug():
    finding = Finding(hypothesis("security", behavioral_failure="Unauthenticated requests can retrieve another user's private records"),
                      "high_confidence", "Source shows ownership check missing before private-record lookup")
    assert finding.is_bug
    assert HuntRun((), (finding,), ()).bug_metrics["bugs_unverifiable"] == 1


def test_overlapping_bug_signals_merge_but_quality_is_separate(tmp_path):
    (tmp_path / "app.py").write_text("def f():\n    return 0\n")
    first = hypothesis(verification_spec={"kind": "equals", "expected": 1}, root_cause_key="wrong-constant")
    duplicate = replace(first, category="semantic", description="Wrong constant in return")
    observation = replace(first, category="coverage_gap", verification_spec=None, description="Missing tests")
    findings = collect_findings(ProjectInfo(tmp_path, ("python",)), (),
                                (SimpleNamespace(hunt=lambda p: (first, duplicate, observation)),), HuntVerifier())
    run = HuntRun((), tuple(findings), ())
    assert len(run.bug_findings) == len(run.observations) == 1
    assert len(run.bug_findings[0].signals) == 2
    assert run.bug_metrics["bugs_discovered"] == run.bug_metrics["bugs_confirmed"] == 1


@pytest.mark.parametrize("include_quality", [False, True])
def test_cli_reports_and_json_separate_observations(tmp_path, monkeypatch, capsys, include_quality):
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    project = ProjectInfo(tmp_path, ("python",))
    observations = (replace(hypothesis("coverage_gap"), description="Missing test coverage"),
                    replace(hypothesis("style"), description="Unnecessary verbosity"))
    detector = SimpleNamespace(hunt=lambda p: observations)
    monkeypatch.setattr("aidebug.hunt.run_checks", lambda *a, **k: ())
    agent = Mock()
    run = hunt_project(project, (detector,), HuntVerifier(), agent, include_quality=include_quality)
    agent.propose.assert_not_called()
    report = run.report_path.read_text(encoding="utf-8")
    for heading in ("Bug Findings", "Confirmed Bugs", "High-Confidence Unverified Bugs", "Rejected Bug Hypotheses"):
        assert "## " + heading in report
    assert ("## Code Quality Observations" in report) == include_quality
    assert ("Unnecessary verbosity" in report) == include_quality
    record = json.loads(run.findings_path.read_text())
    assert not record["findings"]
    assert ("quality_observations" in record) == include_quality
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("aidebug.discovery.discover_repository", lambda p: project)
    mocked = Mock(return_value=run)
    monkeypatch.setattr("aidebug.hunt.hunt_project", mocked)
    hunt_main([str(tmp_path), *(["--include-quality"] if include_quality else [])])
    output = capsys.readouterr().out
    assert mocked.call_args.kwargs["include_quality"] == include_quality
    assert ("Code Quality Observations:" in output) == include_quality
    assert ("Unnecessary verbosity" in output) == include_quality
    for label in ("Bugs discovered", "Bugs confirmed", "Bugs unverifiable", "Bugs rejected"):
        assert label + ": 0" in output
