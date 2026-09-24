import json
import traceback
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug.agent import DebugOrchestrator
from aidebug.context import build_debug_context
from aidebug.models import AnalysisReport, CheckResult, ProjectInfo, ValidationReport
from aidebug.openai_agent import FixerResponseError, OpenAIAgent
from aidebug.workspace import InvalidUnifiedDiffError, PatchApplicabilityError, apply_unified_diff, isolated_workspace
from aidebug.models import PatchProposal


PATCH = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-return 1\n+return 2\n"
ANALYSIS = AnalysisReport("wrong return", 0.9, "Expected another value")
SECRET = "private-credential-never-display"


def fixer_json(payload):
    """Build a response using the required Fixer contract."""
    if isinstance(payload, dict):
        payload = {"status": "patch", "explanation": "Fix return", **payload}
    return json.dumps(payload)


def setup_fixer(tmp_path, text, **response_fields):
    (tmp_path / "app.py").write_text("return 1\n", encoding="utf-8")
    project = ProjectInfo(tmp_path, ("python",), ())
    failure = CheckResult("test", ("test",), 1, "", "failed", 0.01)
    context = build_debug_context(project, failure)
    response = SimpleNamespace(output_text=text, output=[], **response_fields)
    client = SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=response)))
    return OpenAIAgent(client=client), context


@pytest.mark.parametrize("text", [
    fixer_json({"unified_diff": PATCH, "explanation": "Fix return"}),
    fixer_json({"unified_diff": PATCH}),
    "```json\n" + fixer_json({"unified_diff": PATCH, "explanation": "Fix return"}) + "\n```",
    fixer_json({"unified_diff": "```diff\n" + PATCH + "```", "explanation": "Fix return"}),
])
def test_valid_fixer_response_applies_in_isolation(tmp_path, text):
    agent, context = setup_fixer(tmp_path, text)
    proposal = agent.propose(context, ANALYSIS)
    assert proposal.unified_diff == PATCH
    assert isinstance(proposal.explanation, str)
    with isolated_workspace(tmp_path) as workspace:
        apply_unified_diff(workspace, proposal.unified_diff)
        assert (workspace / "app.py").read_text() == "return 2\n"
    assert (tmp_path / "app.py").read_text() == "return 1\n"


@pytest.mark.parametrize("payload, diagnostic", [
    ({"explanation": SECRET}, "'unified_diff' is missing"),
    ({"unified_diff": None}, "'unified_diff' must be a string"),
    ({"unified_diff": [SECRET]}, "'unified_diff' must be a string"),
    ({"unified_diff": "  "}, "'unified_diff' must not be empty"),
    ({"unified_diff": PATCH, "explanation": {"token": SECRET}}, "'explanation' must be a non-empty string"),
    ({"unified_diff": "```diff\n\n```"}, "'unified_diff' must not be empty"),
    ([SECRET], "JSON object"),
])
def test_invalid_fields_have_safe_diagnostics(tmp_path, payload, diagnostic):
    agent, context = setup_fixer(tmp_path, fixer_json(payload))
    with pytest.raises(FixerResponseError, match="Invalid Fixer response envelope") as error:
        agent.propose(context, ANALYSIS)
    assert diagnostic in str(error.value)
    assert SECRET not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("text", [SECRET, '{"unified_diff": "' + SECRET, "Here is a patch:\n" + PATCH])
def test_non_json_or_truncated_response_is_not_salvaged(tmp_path, text):
    agent, context = setup_fixer(tmp_path, text)
    with pytest.raises(FixerResponseError, match="invalid JSON at line") as error:
        agent.propose(context, ANALYSIS)
    assert SECRET not in "".join(traceback.format_exception(error.value))


def test_tool_followup_keeps_fixer_instructions(tmp_path):
    agent, context = setup_fixer(tmp_path, PATCH)
    tool_call = SimpleNamespace(type="function_call", name="read_file", arguments='{"relative_path":"app.py"}', call_id="call-1")
    agent.client.responses.create.side_effect = [
        SimpleNamespace(id="response-1", output=[tool_call], output_text=""),
        SimpleNamespace(output=[], output_text=fixer_json({"unified_diff": PATCH})),
    ]
    assert agent.propose(context, ANALYSIS).unified_diff == PATCH
    first, second = [call.kwargs for call in agent.client.responses.create.call_args_list]
    assert second["instructions"] == first["instructions"]
    assert "Return a plain unified diff only" not in first["instructions"]
    assert "Return JSON only" in first["instructions"]
    assert second["previous_response_id"] == "response-1"


@pytest.mark.parametrize("status", ["incomplete", "failed", "cancelled"])
def test_incomplete_response_is_rejected_even_with_parseable_text(tmp_path, status):
    agent, context = setup_fixer(tmp_path, PATCH, status=status)
    with pytest.raises(ValueError, match="did not complete"):
        agent.propose(context, ANALYSIS)


@pytest.mark.parametrize("patch, diagnostic", [
    (PATCH.replace("@@ -1 +1 @@", "@@ broken " + SECRET), "Invalid unified diff hunk"),
    (PATCH.replace("@@ -1 +1 @@", "@@ -1,2 +1 @@"), "line counts"),
    (PATCH + SECRET + "\n", "Invalid unified diff line"),
    (PATCH.replace("b/app.py", "b/../../" + SECRET), "escapes workspace"),
    (PATCH.replace("-return 1", "-stale " + SECRET), "does not match"),
])
def test_malformed_or_unsafe_patches_never_reach_validator(tmp_path, patch, diagnostic):
    agent, context = setup_fixer(tmp_path, fixer_json({"unified_diff": patch}))
    analyzer = SimpleNamespace(analyze=Mock(return_value=ANALYSIS))
    validator = SimpleNamespace(validate=Mock())
    with pytest.raises(ValueError, match=diagnostic) as error:
        DebugOrchestrator(analyzer, agent, validator).run(context.project, context.failed_check)
    assert SECRET not in "".join(traceback.format_exception(error.value))
    validator.validate.assert_not_called()
    assert (tmp_path / "app.py").read_text() == "return 1\n"


def test_openai_fixer_cumulative_retry_patches(tmp_path):
    agent, context = setup_fixer(tmp_path, PATCH)
    second_patch = PATCH.replace("-return 1", "-return 2").replace("+return 2", "+return 3")
    agent.client.responses.create.side_effect = [
        SimpleNamespace(output=[], output_text=fixer_json({"unified_diff": PATCH})),
        SimpleNamespace(output=[], output_text=fixer_json({"unified_diff": second_patch})),
    ]

    class Validator:
        calls = 0

        def validate(self, project):
            self.calls += 1
            assert (project.root / "app.py").read_text() == f"return {self.calls + 1}\n"
            result = CheckResult("test", ("test",), 0 if self.calls == 2 else 1, "", "failed", 0.01)
            return ValidationReport(result.passed, (result,), project.root)

    analyzer = SimpleNamespace(analyze=Mock(return_value=ANALYSIS))
    result = DebugOrchestrator(analyzer, agent, Validator()).run(context.project, context.failed_check)
    assert result.validation.passed
    assert result.attempts == 2
    assert [p.unified_diff for p in result.proposals] == [PATCH, second_patch]
    assert (tmp_path / "app.py").read_text() == "return 1\n"


def test_pending_tool_calls_are_not_treated_as_a_final_patch(tmp_path):
    agent, context = setup_fixer(tmp_path, PATCH)
    call = SimpleNamespace(type="function_call", name="read_file", arguments='{"relative_path":"app.py"}', call_id="call-1")
    agent.client.responses.create.return_value = SimpleNamespace(id="response-1", output=[call], output_text=fixer_json({"unified_diff": PATCH}))
    with pytest.raises(ValueError, match="pending tool calls"):
        agent.propose(context, ANALYSIS)
    assert agent.client.responses.create.call_count == 9


@pytest.mark.parametrize("patch", [
    SECRET,
    "diff --git a/app.py b/app.py\n" + PATCH,
    "--- a/app.py\n",
    "--- a/app.py\n+++ b/app.py\n",
    PATCH.replace("+++ b/app.py", "invalid target " + SECRET),
    PATCH.replace("@@ -1 +1 @@", "not a hunk " + SECRET),
    PATCH.replace("@@ -1 +1 @@", "@@ -1,2 +1 @@"),
    PATCH.replace("b/app.py", "b/../../" + SECRET),
])
def test_json_envelope_defers_all_diff_validation_to_workspace(tmp_path, patch):
    agent, context = setup_fixer(tmp_path, fixer_json({"unified_diff": patch}))
    proposal = agent.propose(context, ANALYSIS)
    assert proposal.unified_diff == patch
    with isolated_workspace(tmp_path) as workspace:
        with pytest.raises(InvalidUnifiedDiffError, match="Invalid unified diff") as error:
            apply_unified_diff(workspace, proposal.unified_diff)
    assert SECRET not in str(error.value)
    assert (tmp_path / "app.py").read_text() == "return 1\n"


def test_plain_response_requires_structured_status(tmp_path):
    agent, context = setup_fixer(tmp_path, PATCH)
    with pytest.raises(FixerResponseError, match="expected a JSON object"):
        agent.propose(context, ANALYSIS)


@pytest.mark.parametrize("patch", [
    PATCH.replace("-return 1", "-stale " + SECRET),
    PATCH.replace("a/app.py", "a/missing.py"),
    PATCH.replace("@@ -1 +1 @@", "@@ -100 +100 @@"),
])
def test_non_applicable_patches_have_distinct_errors(tmp_path, patch):
    agent, context = setup_fixer(tmp_path, fixer_json({"unified_diff": patch}))
    proposal = agent.propose(context, ANALYSIS)
    with isolated_workspace(tmp_path) as workspace:
        with pytest.raises(PatchApplicabilityError, match="Patch does not apply to the isolated workspace") as error:
            apply_unified_diff(workspace, proposal.unified_diff)
    assert SECRET not in str(error.value)
    assert (tmp_path / "app.py").read_text() == "return 1\n"


@pytest.mark.parametrize("payload", [
    {"unified_diff": PATCH, "explanation": "repair"},
    {"status": "unknown", "unified_diff": PATCH, "explanation": "repair"},
    {"status": [], "unified_diff": PATCH, "explanation": "repair"},
    {"status": "patch", "unified_diff": "", "explanation": "repair"},
    {"status": "patch", "unified_diff": None, "explanation": "repair"},
    {"status": "no_patch", "unified_diff": "", "explanation": "unsafe"},
    {"status": "no_patch", "unified_diff": PATCH, "explanation": "unsafe"},
    {"status": "no_patch", "unified_diff": None, "explanation": " "},
    {"status": "no_patch", "unified_diff": None},
])
def test_invalid_status_contract(tmp_path, payload):
    agent, context = setup_fixer(tmp_path, json.dumps(payload))
    with pytest.raises(FixerResponseError):
        agent.propose(context, ANALYSIS)


@pytest.mark.parametrize("after_patch", [False, True])
def test_no_patch_stops_without_applying_or_validating(tmp_path, monkeypatch, after_patch):
    reason = "The available evidence does not identify a safe repair."
    response = json.dumps({"status": "no_patch", "unified_diff": None, "explanation": reason})
    agent, context = setup_fixer(tmp_path, response)
    proposal = agent.propose(context, ANALYSIS)
    assert proposal == PatchProposal(None, reason, "no_patch")
    responses = [SimpleNamespace(output=[], output_text=response)]
    if after_patch:
        responses.insert(0, SimpleNamespace(output=[], output_text=fixer_json({"unified_diff": PATCH})))
    agent.client.responses.create.side_effect = responses
    apply = Mock(wraps=apply_unified_diff)
    monkeypatch.setattr("aidebug.agent.apply_unified_diff", apply)
    validation = ValidationReport(False, (context.failed_check,), tmp_path)
    validator = SimpleNamespace(validate=Mock(return_value=validation))
    analyzer = SimpleNamespace(analyze=Mock(return_value=ANALYSIS))
    result = DebugOrchestrator(analyzer, agent, validator).run(context.project, context.failed_check)
    assert result.proposals[-1].status == "no_patch"
    assert result.proposals[-1].explanation == reason
    assert result.attempts == (2 if after_patch else 1)
    assert result.validation == (validation if after_patch else None)
    assert apply.call_count == int(after_patch)
    assert validator.validate.call_count == int(after_patch)
    assert (tmp_path / "app.py").read_text() == "return 1\n"
