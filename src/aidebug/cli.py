"""Command-line interface for running the AI Debugger workflow."""

import argparse
import getpass
import json
import re
import sys
import warnings
from dataclasses import asdict
from pathlib import Path

from .agent import DebugOrchestrator
from .context import build_agent_prompt, build_debug_context
from .credentials import CredentialError, resolve_api_key, save_api_key
from .discovery import discover_repository
from .git_integration import collect_evidence
from .openai_agent import OpenAIAgent
from .runner import run_checks
from .validation import capture, summary


def _safe_agent_error(exc: Exception, api_key: str) -> str:
    """Retain diagnostics without echoing credentials or masked key fragments."""
    message = f"{type(exc).__name__}: {exc}"
    # Also cover escaped credentials in JSON bodies and exception reprs.
    for secret in sorted({api_key, json.dumps(api_key)[1:-1], repr(api_key)[1:-1]}, key=len, reverse=True):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    # Authentication responses can include a partially masked OpenAI key.
    return re.sub(r"sk-[A-Za-z0-9_*.-]+", "[REDACTED]", message)


def main() -> int:
    if sys.argv[1:2] == ["hunt"]:
        from .hunt import hunt_main

        return hunt_main(sys.argv[2:])
    parser = argparse.ArgumentParser(
        description="Detect and investigate failures in a local software project.",
        epilog="Run aidebug configure to save your OpenAI API key in the system keyring.",
    )
    if sys.argv[1:2] == ["configure"]:
        configure_parser = argparse.ArgumentParser(prog=f"{parser.prog} configure", description="Save your OpenAI API key in the system keyring.")
        configure_parser.parse_args(sys.argv[2:])
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                api_key = getpass.getpass("OpenAI API key: ")
            save_api_key(api_key)
        except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
            configure_parser.error("Configuration cancelled: secure input is unavailable or interrupted.")
        except CredentialError as exc:
            configure_parser.error(str(exc))
        print("Configuration saved.")
        return 0
    parser.add_argument("path", nargs="?", default=".", type=Path, help="Project directory or file inside a project")
    parser.add_argument("--timeout", type=float, default=120, help="Timeout per check in seconds")
    parser.add_argument("--all", action="store_true", help="Run every discovered check after failures")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print machine-readable results")
    ai_group = parser.add_mutually_exclusive_group()
    ai_group.add_argument("--no-ai", action="store_true", help="Run checks without invoking the AI debugger")
    ai_group.add_argument("--openai", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--model", help="OpenAI model name (defaults to OPENAI_MODEL or gpt-6-sol)")
    args = parser.parse_args()

    try:
        project = discover_repository(args.path)
    except ValueError as exc:
        parser.error(str(exc))

    results = capture(run_checks(project, args.timeout, stop_on_failure=not args.all))
    failed = next((result for result in results if not result.passed), None)
    changed_files, git_diff = collect_evidence(project.root)
    debug_run = None
    if failed and not failed.blocked_reason and not args.no_ai:
        try:
            api_key = resolve_api_key()
        except CredentialError as exc:
            parser.error(str(exc))
        if not api_key:
            parser.error("AI debugging requires an API key. Run aidebug configure, set OPENAI_API_KEY, or use --no-ai.")
        try:
            openai_agent = OpenAIAgent(model=args.model)
            debug_run = DebugOrchestrator(openai_agent, openai_agent, max_attempts=3).run(
                project,
                failed,
                changed_files=changed_files,
                git_diff=git_diff,
            )
        except Exception as exc:
            parser.error(f"OpenAI agent failed: {_safe_agent_error(exc, api_key)}")
    if args.as_json:
        payload = {"project": asdict(project), "results": [asdict(result) for result in results]}
        if failed:
            payload["debug_prompt"] = build_agent_prompt(build_debug_context(project, failed, changed_files, git_diff))
        if debug_run:
            payload["agent_run"] = asdict(debug_run)
        print(json.dumps(payload, default=str, indent=2))
    else:
        print(f"Project: {project.root}")
        print(f"Detected: {', '.join(project.project_types)}")
        print("\nInitial check:")
        for result in results:
            status = result.status
            print(f"[{status}] {result.name}: {' '.join(result.command)}")
            if result.blocked_reason:
                print(f"Project-wide validation: BLOCKED\nReason: {result.blocked_reason}")
        if not results:
            print("Project-wide validation: NOT AVAILABLE")
        if failed:
            if debug_run:
                print(f"\nAI analysis:\nRoot cause: {debug_run.analysis.root_cause}")
                if debug_run.proposals and getattr(debug_run.proposals[-1], "status", "patch") == "no_patch":
                    print("\nRepair result:\nno safe patch proposed.")
                else:
                    passed = debug_run.validation and debug_run.validation.passed
                    print("\nValidated repair:" if passed else "\nIsolated validation:")
                    if debug_run.validation:
                        if getattr(debug_run.validation, "targeted", None):
                            print(summary(debug_run.validation))
                        for result in debug_run.validation.results:
                            status = "PASS" if result.passed else "FAIL"
                            print(f"[{status}] {result.name} in isolated workspace")
                    attempts = debug_run.attempts
                    outcome = "Repair validated" if passed else "Repair not validated"
                    print(f"\n{outcome} after {attempts} {'attempt' if attempts == 1 else 'attempts'}.")
                if debug_run.proposals:
                    print(debug_run.proposals[-1].explanation)
                if getattr(debug_run, "validated_patch_path", None):
                    print(f"Validated patch saved to:\n{debug_run.validated_patch_path}")
                if getattr(debug_run, "debug_report_path", None):
                    print(f"Debug report saved to:\n{debug_run.debug_report_path}")
                if getattr(debug_run, "artifact_error", None):
                    print(debug_run.artifact_error)
            else:
                print("\nFailure captured. Agent prompt:\n")
                print(build_agent_prompt(build_debug_context(project, failed, changed_files, git_diff)))
    return 1 if failed else 0
