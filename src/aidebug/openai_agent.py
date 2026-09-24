"""OpenAI adapter for the provider-neutral debugging agent."""

import json
import os
import re
from pathlib import Path
from typing import Any

from .context import build_agent_prompt
from .code_tools import CodeTools
from .credentials import resolve_api_key
from .models import AnalysisReport, DebugContext, PatchProposal

DEFAULT_MODEL = "gpt-6-sol"
DEFAULT_REASONING_EFFORT = "medium"
MAX_SOURCE_BYTES = 120_000
MAX_FILE_BYTES = 12_000


class FixerResponseError(ValueError):
    """The model response envelope cannot be converted into a proposal."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"Invalid Fixer response envelope: {detail}")


class OpenAIAgent:
    """Use an OpenAI model for both root-cause analysis and patch proposals."""

    def __init__(self, model: str | None = None, client: Any | None = None) -> None:
        self.model = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
        self.client = client

    def analyze(self, context: DebugContext) -> AnalysisReport:
        response = self._complete(
            """You are the Analyzer in a software debugging agent. Inspect the repository evidence.
Return JSON only with these keys:
root_cause: concise likely root cause
confidence: number from 0 to 1
reasoning: short evidence-based explanation
suspected_files: array of repository-relative file paths
Do not invent files or claim certainty beyond the evidence.""",
            _evidence_prompt(context),
            context.project.root,
        )
        data = _json_object(response)
        try:
            confidence = float(data["confidence"])
            if not 0 <= confidence <= 1:
                raise ValueError("confidence must be between 0 and 1")
            files = tuple(_safe_relative_file(context.project.root, value) for value in data.get("suspected_files", []))
            return AnalysisReport(
                root_cause=str(data["root_cause"]),
                confidence=confidence,
                reasoning=str(data["reasoning"]),
                suspected_files=tuple(path for path in files if path is not None),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"OpenAI Analyzer returned invalid analysis JSON: {data}") from exc

    def propose(self, context: DebugContext, analysis: AnalysisReport) -> PatchProposal:
        response = self._complete(
            """You are the Fixer in a software debugging agent. Propose the smallest safe code change.
Return JSON only with these keys:
status: "patch" or "no_patch"
unified_diff: a plain, applicable unified diff string with project-relative paths, or null
explanation: a non-empty concise explanation
Use status="patch" whenever a concrete repair can be proposed safely; unified_diff
must be a non-empty plain unified diff and explanation must describe the repair.
Use status="no_patch" only when no safe concrete repair can be proposed;
unified_diff must be null and explanation must state why no safe patch could be produced.
Never return an empty unified_diff string.
The project may contain changes from earlier repair attempts.
Inspect the current project state before proposing the next patch.
The proposed patch must apply to the current workspace state.
The unified_diff JSON string must contain only a plain unified diff, with newlines
escaped as required by JSON. Each file change inside that string must begin directly with:
--- <old path>
+++ <new path>
Do not include diff --git, index, new file mode, deleted file mode, similarity index, or rename metadata.
Use /dev/null for file creation and deletion. Do not include markdown fences.""",
            _evidence_prompt(context)
            + "\nAnalyzer report:\n"
            + json.dumps(
                {
                    "root_cause": analysis.root_cause,
                    "confidence": analysis.confidence,
                    "reasoning": analysis.reasoning,
                    "suspected_files": [str(path.relative_to(context.project.root)) for path in analysis.suspected_files],
                }
            ),
            context.project.root,
        )
        return _patch_proposal(response)

    def _complete(self, system: str, evidence: str, root: Path) -> str:
        if self.client is None:
            api_key = resolve_api_key()
            if not api_key:
                raise ValueError("No API key available; run aidebug configure or set OPENAI_API_KEY")
            from openai import OpenAI

            self.client = OpenAI(api_key=api_key)
        tools = CodeTools(root)
        response = self.client.responses.create(
            model=self.model,
            reasoning={"effort": DEFAULT_REASONING_EFFORT},
            instructions=system,
            input=evidence,
            tools=CodeTools.specifications(),
        )
        for _ in range(8):
            calls = [item for item in (getattr(response, "output", ()) or ()) if getattr(item, "type", "") == "function_call"]
            if not calls:
                break
            outputs = []
            for call in calls:
                try:
                    arguments = json.loads(call.arguments)
                except (AttributeError, json.JSONDecodeError):
                    result = "Invalid tool arguments"
                else:
                    result = tools.execute(call.name, arguments)
                outputs.append({"type": "function_call_output", "call_id": call.call_id, "output": result})
            response = self.client.responses.create(
                model=self.model,
                reasoning={"effort": DEFAULT_REASONING_EFFORT},
                instructions=system,
                previous_response_id=response.id,
                input=outputs,
                tools=CodeTools.specifications(),
            )
        if any(getattr(item, "type", "") == "function_call" for item in (getattr(response, "output", ()) or ())):
            raise ValueError("OpenAI response still contains pending tool calls after the tool-call limit")
        if getattr(response, "status", None) in {"incomplete", "failed", "cancelled"}:
            raise ValueError("OpenAI response did not complete; no final proposal is available")
        output = getattr(response, "output_text", None)
        if not isinstance(output, str) or not output.strip():
            raise ValueError("OpenAI returned no text output")
        return output


def _evidence_prompt(context: DebugContext) -> str:
    files: list[str] = []
    total_bytes = 0
    for path in context.relevant_files:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        remaining = MAX_SOURCE_BYTES - total_bytes
        if remaining <= 0:
            break
        excerpt = content[: min(MAX_FILE_BYTES, remaining)]
        total_bytes += len(excerpt.encode("utf-8"))
        files.append(f"FILE: {path.relative_to(context.project.root)}\n{excerpt}")
    return build_agent_prompt(context) + "\nSource files:\n" + "\n\n".join(files)


def _json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"OpenAI returned invalid JSON at line {exc.lineno}, column {exc.colno}") from None
    if not isinstance(value, dict):
        raise ValueError("OpenAI response must be a JSON object")
    return value


def _unwrap_fence(text: str) -> str:
    """Remove one complete wrapper, never extract a patch from surrounding prose."""
    match = re.fullmatch(r"\s*```(?:json|diff|patch)?[^\S\r\n]*\r?\n(.*?)\r?\n```\s*", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1) + "\n"
    return text


def _patch_proposal(response: str) -> PatchProposal:
    """Normalize supported envelopes; the workspace engine validates/applies hunks."""
    text = _unwrap_fence(response).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FixerResponseError(
            f"expected a JSON object; invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from None
    if not isinstance(data, dict):
        raise FixerResponseError("expected a JSON object")
    for field in ("status", "unified_diff", "explanation"):
        if field not in data:
            raise FixerResponseError(f"field '{field}' is missing")
    status = data["status"]
    if status not in ("patch", "no_patch"):
        raise FixerResponseError("field 'status' must be 'patch' or 'no_patch'")
    explanation = data["explanation"]
    if not isinstance(explanation, str) or not explanation.strip():
        raise FixerResponseError("field 'explanation' must be a non-empty string")
    unified_diff = data["unified_diff"]
    if status == "no_patch":
        if unified_diff is not None:
            raise FixerResponseError("field 'unified_diff' must be null for status 'no_patch'")
    else:
        if not isinstance(unified_diff, str):
            raise FixerResponseError("field 'unified_diff' must be a string for status 'patch'")
        unified_diff = _unwrap_fence(unified_diff).lstrip()
        if not unified_diff.strip():
            raise FixerResponseError("field 'unified_diff' must not be empty for status 'patch'")
    return PatchProposal(unified_diff=unified_diff, explanation=explanation, status=status)


def _safe_relative_file(root: Path, value: Any) -> Path | None:
    if not isinstance(value, str):
        return None
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None
