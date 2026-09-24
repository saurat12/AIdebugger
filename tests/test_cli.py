from types import SimpleNamespace
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from aidebug import cli
from aidebug.models import CheckResult, ProjectInfo


def _failed_result():
    return CheckResult("test", ("test",), 1, "", "failed", 0.01)


def _project(tmp_path):
    return ProjectInfo(tmp_path, ("python",), ())


@pytest.mark.parametrize("key_source", ["environment", "keyring"])
def test_cli_runs_ai_by_default_after_failure(tmp_path, monkeypatch, key_source):
    calls = {"agent": 0, "run": 0}

    class FakeOpenAIAgent:
        def __init__(self, model=None):
            assert model == "test-model"
            calls["agent"] += 1

    class FakeOrchestrator:
        def __init__(self, analyzer, fixer, max_attempts):
            pass

        def run(self, *args, **kwargs):
            calls["run"] += 1
            return SimpleNamespace(
                analysis=SimpleNamespace(root_cause="Wrong value"),
                validation=SimpleNamespace(passed=False, results=(_failed_result(),)),
                attempts=1,
                proposals=[SimpleNamespace(explanation="proposed fix")],
            )

    monkeypatch.setattr(cli, "discover_repository", lambda path: _project(tmp_path))
    monkeypatch.setattr(cli, "run_checks", lambda *args, **kwargs: (_failed_result(),))
    monkeypatch.setattr(cli, "collect_evidence", lambda root: (set(), None))
    monkeypatch.setattr(cli, "OpenAIAgent", FakeOpenAIAgent)
    monkeypatch.setattr(cli, "DebugOrchestrator", FakeOrchestrator)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    if key_source == "keyring":
        monkeypatch.delenv("OPENAI_API_KEY")
        monkeypatch.setattr("aidebug.credentials.keyring.get_password", lambda *args: "stored-key")
    monkeypatch.setattr("sys.argv", ["aidebug", "--model", "test-model"])

    assert cli.main() == 1
    assert calls == {"agent": 1, "run": 1}


def test_cli_reports_missing_api_key_when_ai_is_needed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "discover_repository", lambda path: _project(tmp_path))
    monkeypatch.setattr(cli, "run_checks", lambda *args, **kwargs: (_failed_result(),))
    monkeypatch.setattr(cli, "collect_evidence", lambda root: (set(), None))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["aidebug"])

    with pytest.raises(SystemExit) as error:
        cli.main()

    stderr = capsys.readouterr().err
    assert error.value.code == 2
    assert "OPENAI_API_KEY" in stderr
    assert "--no-ai" in stderr


def test_cli_no_ai_never_constructs_openai_agent(tmp_path, monkeypatch):
    class UnexpectedOpenAIAgent:
        def __init__(self, *args, **kwargs):
            raise AssertionError("OpenAI must not be constructed with --no-ai")

    monkeypatch.setattr(cli, "discover_repository", lambda path: _project(tmp_path))
    monkeypatch.setattr(cli, "run_checks", lambda *args, **kwargs: (_failed_result(),))
    monkeypatch.setattr(cli, "collect_evidence", lambda root: (set(), None))
    monkeypatch.setattr(cli, "OpenAIAgent", UnexpectedOpenAIAgent)
    monkeypatch.setattr("sys.argv", ["aidebug", "--no-ai"])

    assert cli.main() == 1


def test_existing_path_and_flags(tmp_path, monkeypatch, capsys):
    discover = Mock(return_value=_project(tmp_path))
    checks = Mock(return_value=())
    monkeypatch.setattr(cli, "discover_repository", discover)
    monkeypatch.setattr(cli, "run_checks", checks)
    monkeypatch.setattr(cli, "collect_evidence", lambda root: (set(), None))
    monkeypatch.setattr(cli, "resolve_api_key", Mock(side_effect=AssertionError("No credentials needed")))
    monkeypatch.setattr("sys.argv", ["aidebug", "./configure", "--no-ai", "--json", "--all", "--timeout", "42"])
    assert cli.main() == 0
    discover.assert_called_once_with(Path("./configure"))
    checks.assert_called_once_with(_project(tmp_path), 42.0, stop_on_failure=False)
    assert json.loads(capsys.readouterr().out)["results"] == []


@pytest.mark.parametrize("key_source", ["environment", "keyring"])
@pytest.mark.parametrize("stage", ["construction", "run"])
@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_cli_agent_errors_show_details_without_keys(tmp_path, monkeypatch, capsys, key_source, stage, flags):
    api_key = "private-test-credential"

    class AuthenticationError(Exception):
        pass

    failure = AuthenticationError(
        f"Error code: 401 - Incorrect API key provided: {api_key}. "
        f"Authorization: Bearer {api_key}; masked: sk-proj-abc***xyz; "
        "code: invalid_api_key"
    )
    monkeypatch.setattr(cli, "discover_repository", lambda path: _project(tmp_path))
    monkeypatch.setattr(cli, "run_checks", lambda *args, **kwargs: (_failed_result(),))
    monkeypatch.setattr(cli, "collect_evidence", lambda root: (set(), None))
    if key_source == "environment":
        monkeypatch.setenv("OPENAI_API_KEY", api_key)
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr("aidebug.credentials.keyring.get_password", lambda *args: api_key)
    monkeypatch.setattr(cli, "OpenAIAgent", Mock(side_effect=failure) if stage == "construction" else Mock())
    monkeypatch.setattr(cli, "DebugOrchestrator", Mock(return_value=SimpleNamespace(run=Mock(side_effect=failure))))
    monkeypatch.setattr("sys.argv", ["aidebug", *flags])

    with pytest.raises(SystemExit) as error:
        cli.main()

    captured = capsys.readouterr()
    assert error.value.code == 2
    assert captured.out == ""
    assert "OpenAI agent failed: AuthenticationError: Error code: 401" in captured.err
    assert "code: invalid_api_key" in captured.err
    assert "[REDACTED]" in captured.err
    assert api_key not in captured.err
    assert "sk-proj-abc" not in captured.err
    assert "xyz" not in captured.err


@pytest.mark.parametrize("message", ["Connection timed out.", "Error code: 429 - Rate limit exceeded.", "Model unavailable."])
def test_safe_agent_error_preserves_non_secret_details(message):
    assert cli._safe_agent_error(RuntimeError(message), "private-key") == f"RuntimeError: {message}"


def test_safe_agent_error_redacts_escaped_credentials():
    key = 'private"key\\value'
    message = f"{key} {json.dumps(key)} {key!r} sk-other-credential"
    safe = cli._safe_agent_error(ValueError(message), key)
    assert "private" not in safe
    assert "sk-other-credential" not in safe
    assert safe.startswith("ValueError:")


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_reports_no_patch(tmp_path, monkeypatch, capsys, as_json):
    from aidebug.context import build_debug_context
    from aidebug.models import AnalysisReport, DebugRun, PatchProposal

    project = _project(tmp_path)
    failure = _failed_result()
    reason = "Insufficient evidence to propose a safe repair."
    result = DebugRun(
        build_debug_context(project, failure),
        AnalysisReport("Unknown cause", 0.1, "Insufficient evidence"),
        (PatchProposal(None, reason, "no_patch"),),
        None,
        1,
    )
    monkeypatch.setattr(cli, "discover_repository", Mock(return_value=project))
    monkeypatch.setattr(cli, "run_checks", Mock(return_value=(failure,)))
    monkeypatch.setattr(cli, "collect_evidence", Mock(return_value=(set(), None)))
    monkeypatch.setattr(cli, "OpenAIAgent", Mock())
    monkeypatch.setattr(cli, "DebugOrchestrator", Mock(return_value=SimpleNamespace(run=Mock(return_value=result))))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("sys.argv", ["aidebug", *(["--json"] if as_json else [])])
    assert cli.main() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    if as_json:
        run = json.loads(captured.out)["agent_run"]
        assert run["validation"] is None
        assert run["proposals"][0] == {"status": "no_patch", "unified_diff": None, "explanation": reason}
    else:
        assert "no safe patch proposed" in captured.out
        assert reason in captured.out
