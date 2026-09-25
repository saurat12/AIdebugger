# AI Debugger

AI Debugger detects the active project folder, identifies Python, Node, and React projects, runs the checks advertised by that project, and captures the evidence an AI agent needs to investigate a failure. Git is optional integration.

## Run locally

From the project root:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m aidebug
```

Run `aidebug configure` from any directory to enter your OpenAI API key with hidden input and save it in the system keyring. A working system keyring backend is required; credentials are never saved to project files. `OPENAI_API_KEY`, if set, overrides the saved key. Do not commit keys to `.env`, source files, or Git:



The default model is `gpt-6-sol` with medium reasoning effort. If a check fails, the OpenAI model automatically analyzes the captured failure and proposes a unified diff; the debugger applies that diff only in a temporary workspace and reruns the checks. Use `--no-ai` to run checks without invoking OpenAI. The active repository is not modified automatically.

Python checks use the target project's `.venv`, then `venv`, then its root environment if it contains `pyvenv.cfg`. AIdebugger probes each interpreter and skips broken environments. If Python checks are discovered but no usable local interpreter exists, the command stops with a setup error. Create or repair the target environment and install the project's check dependencies there; AIdebugger does not fall back to its own pipx interpreter, PATH, or an activated environment, and does not install dependencies automatically.

Isolated validation reuses that environment's absolute interpreter path with the temporary source directory as its working directory. Virtual environments are excluded from the copy. Installed dependencies remain shared with the target environment; projects with editable installs or absolute import paths may require their own test configuration to ensure imports use the temporary source tree.

The CLI stops at the first failed check and prints the project root, project types, command output, relevant files, an optional Git diff, and an agent-ready investigation prompt. Use `--all` to run every discovered check or `--json` for an integration-friendly result.

Successful repairs are saved under the target project's `.aidebug/` directory as a uniquely timestamped `validated_patch_*.diff` and `debug_report_*.md` pair. The diff combines all repair attempts from the original contents to the validated result; real source files remain unchanged. The CLI prints both saved paths (or includes them in `agent_run` with `--json`). Failed validation and `no_patch` outcomes do not create validated artifacts. `.aidebug/` is excluded from temporary workspace copies and ignored by this repository's Git configuration.

```powershell
aidebug --json
aidebug --no-ai
aidebug path\inside\the\project --timeout 300
```

## Current workflow

### Proactive hunting

`aidebug hunt [path]` runs the full pipeline by default. `aidebug hunt --quick [path]` is the faster, narrower alternative; there is no need to run both. Both support `--model`, `--timeout`, and `--json`.

| Strategy | Quick | Full (default) |
| --- | --- | --- |
| Existing checks in an isolated source copy | Yes | Yes |
| Lightweight Python static patterns | Yes | Yes |
| Read-only LLM source review | 12 source excerpts / 16 KB | 80 excerpts / 80 KB, plus read tools |
| Declared numeric doctest verification | Yes | Yes |
| Coverage-gap analysis | No | coverage.json or static test-reference heuristic |
| Generated boundary cases | No | Up to 64 numeric cases per supported function |
| Declared property/invariant checks | No | Yes |
| Exception-path analysis | No | Bare handlers and generated arithmetic failures |
| Cross-function consistency | No | Local call signatures and declared equivalence |

All hypotheses pass through independent verification. Only `confirmed` findings enter Analyzer ? Fixer ? isolated Validator. `high_confidence`, `unconfirmed`, and `rejected` remain in the report without automatic repair. Static patterns such as mutable defaults, broad handlers, and call-arity mismatches are report-only suspicions; coverage gaps never prove a bug. Findings have stable IDs, source file/symbol, category, hypothesis/evidence, confidence, reproduction strategy, and verification status/evidence.

Verification uses a restricted AST interpreter on isolated source, not arbitrary model-generated scripts or imported project modules. It supports simple undecorated Python functions with scalar arithmetic, comparisons, assignments, and conditionals. Generated inputs include small integers and nearby numeric boundaries in source. Numeric doctests provide expected results. Additional explicit docstring contracts are supported:

```python
def magnitude(x):
    """aidebug invariant: result >= 0"""
    ...
```

`aidebug total` declares that a function should not raise on the generated numeric inputs. `aidebug equivalent: other_function` declares equal results for the same inputs, with the reference function in the same file. These declarations must come from project source; the LLM cannot invent verification oracles. Unsupported code remains unconfirmed, and arithmetic exceptions without a declared input/behavior contract are high-confidence suspicions rather than confirmed bugs. Existing declarations and function signatures are pinned during repair validation, which reruns existing checks plus the reproduction and bounded generated cases.

Static scans are bounded to 300 entries / 1 MB and 50 findings per strategy. Coverage JSON may be stale; direct-test-reference analysis can miss indirect coverage. Other languages can receive LLM review but do not yet have independent executable verification strategies. Full means all implemented strategies, not exhaustive verification.

Every completed hunt saves `.aidebug/hunt_findings_<timestamp>.json` and `hunt_report_<timestamp>.md`, even if no findings exist. Reports separate existing surfaced failures, proactive findings, confirmed bugs, rejected hypotheses, and repair outcomes. Confirmed repairs retain cumulative validated patch/debug-report artifacts and leave real source unchanged. A repair error is recorded without dropping its finding. Separate repairs are independently validated, not jointly applied. Existing-test failures are surfaced separately; use normal `aidebug` to repair them.

`hunt` exits 1 for existing-check failures or confirmed bugs (including saved repairs), 0 otherwise, and 2 for command errors. No findings and passing checks do not establish that the project is bug-free. `DetectionStrategy` and `VerificationStrategy` remain extension points for future runtime, coverage, property, and generated-test integrations.

Discovery and evidence capture are deterministic and do not require an API key or Git. When Git is available, changed files and the current diff are included as extra evidence; otherwise the debugger uses the supplied folder and continues without them.

Context selection starts with failure paths and changed files, then adds project configuration, nearby test files, and local Python or JavaScript/TypeScript imports. It is bounded to 40 files and 120 KB of source evidence, with individual files capped at 12 KB, so broader dependency context does not overwhelm the model request.

The OpenAI agent can also investigate progressively through bounded read-only tools: `read_file`, `search_code`, `list_files`, and `find_symbol`. Tool paths are restricted to the active project root, generated/dependency directories are excluded, and results are capped. The model can request deeper evidence only when the initial context is insufficient.

## Agent architecture

The agent loop is implemented in `aidebug.agent`:

```text
DebugOrchestrator
			 |
	+----+----+
	|         |
Analyzer  Fixer
	|         |
context  unified diff
			 |
	 Validator
			 |
	tests/lint/build
```

- `Analyzer.analyze(context)` returns a root-cause hypothesis and confidence.
- `Fixer.propose(context, analysis)` returns an explanation and unified diff.
- `Validator.validate(project)` runs checks in the isolated workspace.
- `DebugOrchestrator` retries up to `max_attempts` and never modifies the active repository.

OpenAI debugging runs automatically after a failed check unless `--no-ai` is supplied. The safety-sensitive patch application and validation loop stays local and testable.

Supported checks are inferred conservatively:

- Python: `pytest`, `ruff check .`, and `mypy .` when the repository has the relevant markers.
- Node and React: `npm run test`, `npm run lint`, and `npm run build` when those scripts exist in `package.json`.

Run the test suite with:

```powershell
.\.venv\Scripts\python.exe -m pytest
```
