import subprocess
import sys

from aidebug.context import build_agent_prompt, build_debug_context, select_relevant_files
from aidebug.code_tools import CodeTools
from aidebug.discovery import discover_repository
from aidebug.agent import DebugOrchestrator
from aidebug.models import AnalysisReport, CheckResult, PatchProposal, ProjectInfo, ValidationReport
from aidebug.openai_agent import OpenAIAgent
from aidebug.runner import run_check
from aidebug.workspace import apply_unified_diff, isolated_workspace


def init_repository(path):
    subprocess.run(("git", "init", "-q"), cwd=path, check=True)


def test_discovers_python_and_react_checks(tmp_path, local_project_python):
    init_repository(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"react":"latest"},"scripts":{"test":"vitest","build":"vite build"}}',
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()

    project = discover_repository(tmp_path)

    assert project.project_types == ("python", "node", "react")
    assert [check.command for check in project.checks] == [
        (local_project_python, "-m", "pytest"),
        ("npm", "run", "test"),
        ("npm", "run", "build"),
    ]


def test_discovers_project_without_git(tmp_path, local_project_python):
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()

    project = discover_repository(tmp_path)

    assert project.root == tmp_path.resolve()
    assert project.project_types == ("python",)
    assert project.checks[0].name == "pytest"


def test_runner_captures_failure_and_timeout(tmp_path):
    project = type("Project", (), {"root": tmp_path})()
    check = type("Check", (), {"name": "broken", "command": (sys.executable, "-c", "print('bad'); raise SystemExit(3)")})()

    result = run_check(project, check)

    assert result.returncode == 3
    assert result.stdout.strip() == "bad"
    assert not result.passed


def test_context_prompt_contains_traceback_and_changed_file(tmp_path):
    init_repository(tmp_path)
    source = tmp_path / "app.py"
    source.write_text("raise RuntimeError()\n", encoding="utf-8")
    project = type("Project", (), {"root": tmp_path, "project_types": ("python",)})()
    result = CheckResult(
        name="pytest",
        command=("pytest",),
        returncode=1,
        stdout="",
        stderr=f'File "{source}", line 1\nRuntimeError: broken',
        duration_seconds=0.1,
    )

    context = build_debug_context(project, result)
    prompt = build_agent_prompt(context)

    assert source in context.relevant_files
    assert "RuntimeError: broken" in prompt
    assert "app.py" in prompt


def test_context_selection_expands_local_dependencies_and_configuration(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("from helpers import calculate\ncalculate()\n", encoding="utf-8")
    (tmp_path / "helpers.py").write_text("def calculate():\n    return 1\n", encoding="utf-8")
    project = ProjectInfo(tmp_path, ("python",), ())
    failure = CheckResult("pytest", ("pytest",), 1, "", "File app.py:2\nfailed", 0.01)

    selected = select_relevant_files(project, failure)

    assert selected[:2] == (tmp_path / "app.py", tmp_path / "pyproject.toml")
    assert (tmp_path / "helpers.py") in selected


def test_code_tools_are_bounded_to_project_root(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def execute_query():\n    return 1\n", encoding="utf-8")
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "dependency.js").write_text("execute_query = 2\n", encoding="utf-8")
    tools = CodeTools(tmp_path)

    assert "execute_query" in tools.find_symbol("execute_query")
    assert "dependency.js" not in tools.search_code("execute_query")
    assert "app.py" in tools.list_files()
    assert "Path must stay inside" in tools.execute("read_file", {"relative_path": "../outside.py"})


class FakeAnalyzer:
    def __init__(self):
        self.calls = 0

    def analyze(self, context):
        self.calls += 1
        return AnalysisReport("return value is wrong", 0.9, "The function returns the old value.")


class FakeFixer:
    def __init__(self, source_name="app.py"):
        self.source_name = source_name
        self.calls = 0

    def propose(self, context, analysis):
        self.calls += 1
        old_value, new_value = ("1", "2") if self.calls == 1 else ("2", "3")
        return PatchProposal(
            f"--- a/{self.source_name}\n+++ b/{self.source_name}\n"
            "@@ -1 +1 @@\n"
            f"-value = {old_value}\n"
            f"+value = {new_value}\n",
            "Return the expected value.",
        )


class FakeValidator:
    def __init__(self):
        self.calls = 0

    def validate(self, project):
        self.calls += 1
        result = CheckResult("test", ("test",), 0, "", "", 0.01)
        return ValidationReport(True, (result,), project.root)


def test_orchestrator_applies_patch_in_isolated_workspace(tmp_path):
    init_repository(tmp_path)
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    project = ProjectInfo(tmp_path, ("python",), ())
    failure = CheckResult("test", ("test",), 1, "", "failed", 0.01)
    analyzer = FakeAnalyzer()
    validator = FakeValidator()

    result = DebugOrchestrator(analyzer, FakeFixer(), validator).run(project, failure)

    assert result.validation.passed
    assert result.attempts == 1
    assert analyzer.calls == 1
    assert validator.calls == 1
    assert source.read_text(encoding="utf-8") == "value = 1\n"


def test_isolated_workspace_applies_patch_without_git(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    patch = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
    )

    with isolated_workspace(tmp_path) as workspace:
        apply_unified_diff(workspace, patch)
        assert (workspace / "app.py").read_text(encoding="utf-8") == "value = 2\n"

    assert source.read_text(encoding="utf-8") == "value = 1\n"


def test_patch_rejects_path_traversal(tmp_path):
    patch = (
        "--- a/app.py\n"
        "+++ b/../../outside.txt\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
    )

    with isolated_workspace(tmp_path) as workspace:
        (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        try:
            apply_unified_diff(workspace, patch)
        except ValueError as exc:
            assert "Patch path escapes workspace" in str(exc)
        else:
            raise AssertionError("path traversal patch was accepted")


def test_patch_rejects_absolute_target_path(tmp_path):
    patch = (
        "--- a/app.py\n"
        f"+++ {tmp_path.drive}\\outside.txt\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
    )

    with isolated_workspace(tmp_path) as workspace:
        (workspace / "app.py").write_text("value = 1\n", encoding="utf-8")
        try:
            apply_unified_diff(workspace, patch)
        except ValueError as exc:
            assert "Patch path escapes workspace" in str(exc)
        else:
            raise AssertionError("absolute-path patch was accepted")


def test_orchestrator_retries_until_limit(tmp_path):
    init_repository(tmp_path)
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    project = ProjectInfo(tmp_path, ("python",), ())
    failure = CheckResult("test", ("test",), 1, "", "failed", 0.01)

    class AlwaysFailingValidator:
        def __init__(self):
            self.calls = 0

        def validate(self, project):
            self.calls += 1
            result = CheckResult("test", ("test",), 1, "", "still failed", 0.01)
            return ValidationReport(False, (result,), project.root)

    validator = AlwaysFailingValidator()
    fixer = FakeFixer()
    result = DebugOrchestrator(FakeAnalyzer(), fixer, validator, max_attempts=2).run(project, failure)

    assert not result.validation.passed
    assert result.attempts == 2
    assert fixer.calls == 2
    assert validator.calls == 2


def test_orchestrator_tracks_all_cumulative_proposals_and_initial_git_evidence(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    project = ProjectInfo(tmp_path, ("python",), ())
    failure = CheckResult("test", ("test",), 1, "", "failed", 0.01)

    class RecordingAnalyzer(FakeAnalyzer):
        def __init__(self):
            super().__init__()
            self.contexts = []

        def analyze(self, context):
            self.contexts.append(context)
            return super().analyze(context)

    class PassOnSecondValidator:
        def __init__(self):
            self.calls = 0

        def validate(self, project):
            self.calls += 1
            result = CheckResult("test", ("test",), 0 if self.calls == 2 else 1, "", "", 0.01)
            return ValidationReport(result.passed, (result,), project.root)

    analyzer = RecordingAnalyzer()
    fixer = FakeFixer()
    result = DebugOrchestrator(analyzer, fixer, PassOnSecondValidator(), max_attempts=2).run(
        project,
        failure,
        changed_files={source},
        git_diff="diff --git a/app.py b/app.py",
    )

    assert result.validation.passed
    assert result.attempts == 2
    assert len(result.proposals) == 2
    assert result.proposals[0].unified_diff != result.proposals[1].unified_diff
    assert analyzer.contexts[0].git_diff == "diff --git a/app.py b/app.py"
    assert source.read_text(encoding="utf-8") == "value = 1\n"


def test_openai_adapter_parses_analysis_and_patch_without_network(tmp_path):
    init_repository(tmp_path)
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    project = ProjectInfo(tmp_path, ("python",), ())
    failure = CheckResult("test", ("test",), 1, "", "failed", 0.01)
    context = build_debug_context(project, failure)

    class FakeResponses:
        def __init__(self):
            self.inputs = []

        def create(self, **kwargs):
            self.inputs.append(kwargs)
            if len(self.inputs) == 1:
                text = '{"root_cause":"wrong return","confidence":0.9,"reasoning":"test expects 2","suspected_files":["app.py"]}'
            else:
                text = '{"status":"patch","unified_diff":"--- a/app.py\\n+++ b/app.py\\n@@ -1 +1 @@\\n-value = 1\\n+value = 2\\n","explanation":"Fix return value"}'
            return type("Response", (), {"output_text": text})()

    responses = FakeResponses()
    client = type("Client", (), {"responses": responses})()
    agent = OpenAIAgent(model="test-model", client=client)
    default_agent = OpenAIAgent(client=client)

    analysis = agent.analyze(context)
    proposal = agent.propose(context, analysis)

    assert analysis.root_cause == "wrong return"
    assert proposal.unified_diff.startswith("--- ")
    assert all(call["model"] == "test-model" for call in responses.inputs)
    assert all(call["reasoning"] == {"effort": "medium"} for call in responses.inputs)
    assert default_agent.model == "gpt-6-sol"


def test_openai_agent_can_request_progressive_file_inspection(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def execute_query():\n    return 1\n", encoding="utf-8")
    project = ProjectInfo(tmp_path, ("python",), ())
    failure = CheckResult("test", ("test",), 1, "", "failed", 0.01)
    context = build_debug_context(project, failure)

    class ToolResponses:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                call = type(
                    "FunctionCall",
                    (),
                    {
                        "type": "function_call",
                        "name": "read_file",
                        "arguments": '{"relative_path":"app.py"}',
                        "call_id": "call-1",
                    },
                )()
                return type("Response", (), {"id": "response-1", "output": [call], "output_text": ""})()
            assert kwargs["input"][0]["type"] == "function_call_output"
            assert "execute_query" in kwargs["input"][0]["output"]
            return type(
                "Response",
                (),
                {
                    "id": "response-2",
                    "output": [],
                    "output_text": '{"root_cause":"query result is wrong","confidence":0.8,"reasoning":"source inspected","suspected_files":["app.py"]}',
                },
            )()

    responses = ToolResponses()
    client = type("Client", (), {"responses": responses})()
    analysis = OpenAIAgent(client=client).analyze(context)

    assert analysis.root_cause == "query result is wrong"
    assert len(responses.calls) == 2
    assert responses.calls[1]["previous_response_id"] == "response-1"