# AI Debugger

AI Debugger is a CLI that detects your project (Python, Node, or React), runs the checks it already uses (pytest, lint, build), and — when one fails — proposes a fix, validates it in an isolated copy of your source, and hands you a ready-to-apply patch. It can also proactively hunt for bugs you haven't hit yet, including ones with no failing test.

**Your real repository is never modified automatically.** Every proposed fix is applied and validated in a temporary workspace; you review and apply the resulting diff yourself.

**Verification is not "ask the model if it's right."** Fixes and hunted bugs are checked by a restricted AST interpreter running in an isolated `-I -S` Python subprocess with no imports, network, subprocess, or real filesystem access — not `eval`/`exec` on model output. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how that works.

## Features

- **Auto-repair** — runs your project's existing checks; on failure, an LLM proposes a unified diff, which is applied and re-validated in an isolated workspace before you ever see it
- **Proactive bug hunting** (`aidebug hunt`) — static analysis, LLM source review, generated edge cases, property/invariant checks, coverage-gap analysis, and cross-function consistency checks, combined into one report
- **Independent verification** — hypotheses are only marked `confirmed` after reproduction in a sandboxed interpreter, not on model confidence alone
- **Zero-config detection** — supports Python (pytest/unittest/ruff/mypy), Node/React (npm scripts), Java (Maven/Gradle), Go, and Rust, plus custom checks via `pyproject.toml`
- **Saved, reviewable artifacts** — validated repairs are written to `.aidebug/` as a diff + report pair; your source files stay untouched until you apply them

## Quickstart

```bash
# macOS / Linux
python -m venv .venv
source .venv/bin/activate
pip install -e .
aidebug configure   # stores your OpenAI API key in the system keyring
aidebug             # run from inside the project you want to check
```

```powershell
# Windows
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m aidebug configure
.\.venv\Scripts\python.exe -m aidebug
```

A working system keyring backend is required for `configure`; credentials are never written to project files. `OPENAI_API_KEY`, if set, overrides the saved key.

```bash
aidebug hunt              # full proactive bug hunt on the current project
aidebug hunt --quick      # faster, narrower pass
aidebug --no-ai           # run checks only, skip repair
aidebug --json            # machine-readable output
```

## Example output

```
$ aidebug hunt --quick

Project: ./src  (python)
Running existing checks... pytest: FAIL (1 failure)

Proactive findings: 3 high-confidence, 1 confirmed

CONFIRMED  src/main.py :: find_max
  For an all-negative input, the function returns 0, which is not
  an item of the input list.
  Repro: find_max([-5, -2, -10]) -> 0, expected -2
  Verified: restricted-ast-runtime, structured spec "equals"

Saved: .aidebug/hunt_report_20260104T0915.md
Saved: .aidebug/validated_patch_20260104T0915.diff

Exit code: 1 (confirmed bug found)
```

## How it works

```
DebugOrchestrator
       |
   +---+---+
   |       |
Analyzer  Fixer
   |       |
context  unified diff
           |
       Validator
           |
   tests / lint / build
```

- **Analyzer** proposes a root-cause hypothesis from captured failure context
- **Fixer** proposes a unified diff
- **Validator** applies the diff in an isolated workspace copy and reruns checks
- **DebugOrchestrator** retries up to a bounded attempt limit and never touches your active repository

Bug hunting follows a parallel path: hypotheses go through independent, sandboxed verification before anything is eligible for repair. Full details — strategy coverage, every numeric bound, the structured verification spec format, extension points, and repair-acceptance rules — are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Supported checks

| Language | Checks |
|---|---|
| Python | pytest (when configured/used), unittest discovery, ruff/mypy when configured |
| Node / React | `npm run test`, `lint`, `build`, when present in `package.json` |
| Java | Maven / Gradle |
| Go | `go test ./...` |
| Rust | `cargo check`, `cargo test` |
| Custom | Explicit argument arrays under `[tool.aidebug.checks]` |

## Development

```bash
pytest
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the extension points (`detector_registry()`, `verifier_registry()`) if you're adding a new hunt strategy or verifier.
