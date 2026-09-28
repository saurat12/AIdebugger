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

Python checks use the target project's `.venv`, then `venv`, then its root environment if it contains `pyvenv.cfg`. AIdebugger probes each interpreter and skips broken environments. If configured Python checks have no usable local interpreter or their validation module is missing, those checks are `BLOCKED`. Hunting and restricted targeted verification can continue. AIdebugger does not fall back to its own pipx interpreter, PATH, or an activated environment, and does not install dependencies automatically.

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

### Structured verification

Hunter hypotheses may now include `verification_spec`, a JSON object rather than Python code. The independent verifier resolves the hypothesis's project-relative file and public function/class symbol in an isolated source copy. A separate `-I -S` Python worker interprets an allowlisted AST subset; it never imports or executes target modules, evaluates generated Python, or exposes host reflection, processes, network, or arbitrary file access.

| Kind | Structured assertion |
| --- | --- |
| `function_call`, `equals` | `args`, optional `kwargs`, and intended `expected` result |
| `expected_exception` | `args` and `expected_exception`: the unexpected exception claimed as the bug |
| `predicate`, `invariant` | `predicate: {"op": "ge", "value": 0}`; comparison operators, `contains`, and `length_equals` |
| `mutation_check` | `expected_args`: intended post-call positional arguments |
| `deterministic_random` | `seed`, optional `random_values` lists for `randint`, `randrange`, or `random`; plus `expected` or `expected_exception` |
| `class_state_check` | `constructors` argument lists, `calls` with instance/method/args, `observe` with instance/attribute, and `expected` |
| `timeout` | `timeout_ms` between 50 and 2000; measured after worker readiness |
| `file_resource_check` | `files` mapping relative virtual filenames to text, and `expected_open_resources` |

For example, `{"kind":"equals","args":[[-5,-2,-9]],"expected":-2}` can reproduce a maximum function that incorrectly returns zero for all-negative inputs. Expectations and valid inputs must be grounded in the project contract. Confirmation means the engine observed a counterexample to that structured claim; it does not independently prove that a model's intended result is the correct specification. A missing claimed exception or matching intended result rejects the hypothesis. Unsupported code, invalid stubs, and mismatched exception types remain unconfirmed/high-confidence rather than authorizing repair.

The new worker supports bounded JSON collections, indexing, equality/identity, loops, simple classes, mutation, safe built-ins, and virtual-file `read`/`write`/`close`/context-manager operations. Random stubs must obey real API ranges, and observed draws are recorded. Virtual resources never read or write real files. Timeout confirmation records bounded non-completion, not a proof of an infinite loop. All workers are terminated/reaped after completion or deadline. Inputs are limited to 64 KB, nesting depth 8, collections of 1000 items, bounded numbers, and source files of 120 KB/5000 AST nodes. Unsupported imports, decorators, inheritance, custom object protocols, private/reflection attributes, and standard-library call forms remain unverifiable.

Observed results, exceptions, argument mutations, random draws, and resource counts are included in finding verification evidence and persisted hunt reports. Only confirmed findings reach repair; the exact structured spec is rerun after repair alongside existing checks. Normal `aidebug` behavior is unchanged.

The structured loader supports nested source paths such as `src/main.py` without importing the module. It indexes declarations and loads only the requested function/class and dependencies actually referenced by the restricted execution. Local helper functions, literal/constant-expression globals, and safe defaults are supported. Unrelated imports, decorators, startup calls, and `__main__` guards are not executed. Dependencies affected by ambiguous bindings, top-level mutations, cyclic initialization, or required unsafe initializers remain unsupported rather than being guessed.

Allowlisted standard-library adapters support `random` (`randint`, `randrange`, `random`), `math` (`sqrt`, `floor`, `ceil`, `fabs`, `isfinite`, `isnan`, `pi`, `e`), and `statistics` (`mean`, `median`), including import aliases and `from` imports. These are fixed adapters, not permission to import arbitrary modules or access arbitrary attributes. Required network, shell, package-install, credential, destructive filesystem, and subprocess operations remain blocked. Unsupported diagnostics describe the dependency restriction without echoing source or credentials.

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

- Python: pytest only with configuration, a declared pytest dependency, or pytest-style test evidence; unittest discovery for unittest-only tests; ruff/mypy when configured. An empty tests directory or Python source alone does not imply pytest.
- Node and React: `npm run test`, `npm run lint`, and `npm run build` when those scripts exist in `package.json`.
- Java: Maven/Gradle tests from project manifests. Go: `go test ./...` from go.mod. Rust: `cargo check` and `cargo test` from Cargo.toml.
- Custom checks are explicit argument arrays under `[tool.aidebug.checks]`, for example `test = ["make", "test"]` or `compile = [".venv/Scripts/python.exe", "-m", "compileall", "src"]`. Shell command strings are not accepted. These are project-authorized executable checks, not model-generated commands. Imports/build scripts can have side effects; isolation is a source copy, not an operating-system sandbox.

### Extending hunt discovery and verification

By default, `aidebug hunt` shows behavioral bug hypotheses only. Use `aidebug hunt --include-quality` to additionally show a separate **Code Quality Observations** section in CLI, JSON, and Markdown output. Coverage gaps, style/verbosity, maintainability, behavior-neutral dead code, generic smells, and unsubstantiated performance suggestions do not contribute to bug counts. Mutable-default and bare-except patterns alone are observations, as is acquiring a file outside a context manager without evidence of a leak. Independently demonstrated resource leaks, hangs, invalid state, incorrect results, and security defects remain bugs.

Detectors can set `finding_kind` to `bug` or `observation`; semantic bug hypotheses should include a concrete `behavioral_failure`. Known quality-only signals are excluded regardless of confidence. Independent failing behavioral verification can establish a bug despite a quality label; a quality label or high confidence alone never authorizes repair. Default reports group **Bug Findings**, **Confirmed Bugs**, **High-Confidence Unverified Bugs**, and **Rejected Bug Hypotheses**. Counts report bugs discovered, confirmed, unverifiable, and rejected after conservative deduplication. The legacy JSON `metrics.findings_*` fields now also count bugs only; `bug_metrics` supplies explicit names. Quality observations are exported separately only with the flag.

The semantic Hunter accepts arbitrary source-grounded hypotheses, including `category="other"` or domain-specific category labels. Categories do not select or authorize execution. Evidence may be a structured object with `summary`, `locations`, and `observations`; reproduction strategies may contain `approach`, `steps`, and `expected_behavior`. Existing string fields remain supported. A hypothesis outside current verification capabilities remains visible with separate confidence, evidence, and verification status.

Full hunt combines static and semantic review, edge/property checks, coverage gaps, exception paths, state/mutation patterns, resource lifetimes, nontermination patterns, cross-function/module checks, and captured native-check failures. Additional source patterns and native failures are report-only suspicions until independently reproduced. These bounded strategies are not exhaustive and do not establish intended behavior merely from a pattern.

Trusted Python extensions register detector factories through `hunt_registry.detector_registry().register(name, factory, quick=False)`; factories receive `(agent, quick)` and return an object with `hunt(project)`. Detectors needing the captured baseline can implement `hunt_with_checks(project, checks)`. Verifier extensions register `(name, supports, factory)` through `verifier_registry()`; factories receive `quick`. A handler supplies `verify(project, hypothesis)`, optionally `contract(...)`, and `prepare_repair(...)` to pin positive corrected behavior. New structured oracles without repair criteria cannot authorize repair acceptance. Registrations are application code, never model-provided executable code. Existing restricted interpreters, path boundaries, and isolated workspaces remain in force.

Deduplication is conservative: it groups matching source file/symbol and explicit `root_cause_key`, or matching descriptions and reproduction contracts. Opposing rejected/non-rejected results remain separate. Ambiguous paraphrases remain separate rather than merging unrelated defects. Every merged detector signal retains its hypothesis, confidence, evidence, and verification outcome. Only the independently confirmed representative can enter repair; grouping never promotes an unsupported claim. JSON and Markdown expose separate deduplicated discovered, confirmed, rejected, and unverifiable counts (unverifiable includes high-confidence and unconfirmed).

### Repair acceptance

Hunt captures a bounded isolated baseline before repair. Targeted verification must establish positive corrected behavior: an expected return value, mutation, state, or declared invariant. Exception-only and timeout-only hypotheses can be confirmed but cannot establish repair success without an `expected` value or positive `postcondition` verification spec. Postconditions preserve the reproduction inputs. Random boundary checks replay lower/upper boundary intent against the repaired API range; arbitrary stale random values are not clamped or accepted.

Python syntax is compiled without importing, executing, or writing bytecode, separately from behavioral checks. Source scans are bounded to 3,000 directory/file entries and 120 KB per inspected file. A syntax pass is not a behavioral guarantee.

Previously passing checks that fail after repair are regressions. Existing failures are compared using exit code and bounded output, retaining exception messages and locations while normalizing temporary workspace paths, durations, and memory addresses. Unchanged failures are reported but do not block a verified target. Changed failures are conservatively treated as possible regressions requiring review; this comparison cannot prove causality. Missing check results also prevent acceptance.

`NOT AVAILABLE` means no relevant project-wide check was discovered. `BLOCKED` means a discovered check could not execute due to unavailable tooling/environment. Environment blockers are not sent back to the Fixer for dependency-file changes.

Final statuses are `REPAIR FAILED`, `REGRESSION DETECTED`, `TARGET FIX VERIFIED`, `TARGET FIX VERIFIED / PROJECT VALIDATION BLOCKED`, and `FULLY VALIDATED`. A target verified without demonstrated regressions may be saved even with unchanged baseline failures or blocked/unavailable project checks. `FULLY VALIDATED` means the targeted reproduction and all discovered relevant project checks passed, not that the project is bug-free. Reports and hunt JSON retain baseline results, blocked checks, comparisons, positive expectations, observed behavior, and scope. Existing patch/report persistence is retained; this checkout does not implement persistent workspace snapshots or an apply command.

Run the test suite with:

```powershell
.\.venv\Scripts\python.exe -m pytest
```
