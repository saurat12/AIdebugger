"""Compile-only, standalone Python syntax findings for proactive hunts."""

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path

from .hunt import BugHypothesis, Finding
from .models import CheckResult
from .validation_discovery import python_sources


MAX_SOURCE_BYTES = 120_000
MAX_CONTEXT_LINE = 160


def syntax_identity(finding):
    plan = (finding.verification_plan or {}).get("plan") or finding.hypothesis.verification_spec or {}
    if plan.get("kind") != "python_syntax":
        return None
    message = re.sub(r"\s+", " ", str(plan.get("message_contains", ""))).strip().casefold()[:80]
    return (finding.hypothesis.suspected_file.replace("\\", "/"),
            plan.get("line"), plan.get("column"),
            (finding.verification_plan or {}).get("parser_error_type", "SyntaxError"), message)


def _bounded_context(source, line):
    lines = source.splitlines()
    if not isinstance(line, int) or not 1 <= line <= len(lines):
        return None
    start, stop = max(1, line - 2), min(len(lines), line + 2)
    def safe_line(value):
        value = value[:MAX_CONTEXT_LINE]
        value = re.sub(r"'[^']*'?|\"[^\"]*\"?", "<string literal>", value)
        value = re.sub(r"(?i)\b(?:OPENAI_API_KEY|API_KEY|PASSWORD|TOKEN|SECRET)\b\s*=\s*[^\s,;]+", "<credential assignment>", value)
        return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "<credential>", value)

    return [{"line": number, "text": safe_line(lines[number - 1])}
            for number in range(start, stop + 1)]


def discover_syntax_findings(project):
    """Find parser failures in bounded project source without importing it."""
    if "python" not in project.project_types:
        return ()
    root = Path(project.root).resolve()
    findings = []
    for path in python_sources(root):
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or path.is_symlink():
            continue
        try:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                continue
            data = path.read_bytes()
            source = data.decode("utf-8", errors="replace")
            compile(data, path.relative_to(root).as_posix(), "exec")
        except SyntaxError as error:
            relative = path.relative_to(root).as_posix()
            line, column = error.lineno, error.offset
            message = re.sub(r"\s+", " ", error.msg).strip()[:200]
            context = _bounded_context(source, line)
            repairable = bool(type(line) is int and line > 0 and type(column) is int
                              and 0 < column <= MAX_CONTEXT_LINE and context)
            spec = {"kind": "python_syntax", "line": line, "column": column,
                    "message_contains": message} if type(line) is int and line > 0 else None
            fingerprint = hashlib.sha256(json.dumps((relative, line, column, type(error).__name__, message.casefold())).encode()).hexdigest()[:20]
            evidence = {"file": relative, "line": line, "column": column, "parser_error": type(error).__name__,
                        "parser_message": message, "source_context": context or [],
                        "confirmation_source": "deterministic compile-only parser"}
            hypothesis = BugHypothesis(relative, "module", f"Python source cannot compile: {message}", evidence,
                                       1.0, {"approach": "Compile the affected project source without execution",
                                             "file": relative, "line": line}, category="syntax", verification_spec=spec,
                                       root_cause_key="syntax:" + fingerprint,
                                       behavioral_failure="Python source fails to compile")
            detail = f"{relative}:{line}:{column}: {type(error).__name__}: {message}"
            check = CheckResult("hunt:python_syntax", ("compile", relative), 1, "", detail, 0)
            plan = {"schema_version": 1, "file": relative, "symbol": "module", "source": "deterministic_verifier",
                    "verifier": "python-parse", "capability": "parse_compile", "plan": spec,
                    "syntax_fingerprint": fingerprint, "parser_error_type": type(error).__name__}
            findings.append(Finding(hypothesis, "confirmed", detail + f". Bounded context: {context or []}.", check,
                                    signals=({"detector": "DeterministicSyntaxScan", "hypothesis": asdict(hypothesis),
                                              "verification_status": "confirmed", "verification_evidence": detail},),
                                    verification_plan=plan,
                                    repairability="authorized" if repairable else "blocked_syntax_location_unavailable"))
        except (OSError, UnicodeError, ValueError):
            continue
    return tuple(findings)
