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

Run `aidebug hunt [path]` to inspect source even when existing checks pass. `--model`, `--timeout`, and `--json` are supported. Existing checks run in a temporary source copy; the Hunter uses the read-only code tools to propose structured hypotheses. CLI output separates those existing-check results from proactive findings.

An independent verifier assigns `confirmed`, `high_confidence`, `unconfirmed`, or `rejected`. The first verification strategy checks declared numeric Python doctest examples for simple undecorated functions, using a bounded AST evaluator rather than executing source or model-generated reproduction code. It supports scalar arithmetic, comparisons, assignments, and conditionals; unsupported languages, functions, or reproduction strategies remain unconfirmed/high-confidence and are not automatically repaired. A matching example rejects that reproduction, not every possible bug in the function.

Only a confirmed mismatch enters Analyzer → Fixer → isolated Validator. Validation must pass both existing checks and the declared examples, and cannot pass by changing the reproduction docstring. Successful repairs use the same cumulative `.aidebug/` patch/report artifacts as normal debugging. Repairs for separate findings are independent, not a jointly validated batch. Real source files remain unchanged. `hunt` exits 1 if existing checks fail or confirmed findings exist (including findings with saved repairs), 0 otherwise, and 2 for command errors. Existing-test failures are reported separately; use normal `aidebug` to repair them.

`DetectionStrategy` and `VerificationStrategy` define extension points for future static analysis, generated tests, property testing, coverage, and runtime evidence. These additional strategies are not implemented yet. No findings does not mean the project is bug-free.

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
