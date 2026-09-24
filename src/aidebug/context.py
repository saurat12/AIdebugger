"""Build compact debugging evidence from a failed check."""

import ast
import re
from pathlib import Path

from .models import CheckResult, DebugContext, ProjectInfo

_PATH_PATTERN = re.compile(r"(?:File |at |in )?[\"']?([^\"'\s:]+\.(?:py|js|jsx|ts|tsx|mjs|cjs))(?::\d+)?")
_JS_IMPORT_PATTERN = re.compile(r"(?:from|import)\s*[\(]?\s*[\"']([^\"']+)[\"']")
_CONFIG_NAMES = {
    "package.json",
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "setup.py",
    "requirements.txt",
    "tsconfig.json",
    "vite.config.js",
    "vite.config.ts",
    "webpack.config.js",
}


def build_debug_context(
    project: ProjectInfo,
    result: CheckResult,
    changed_files: set[Path] | None = None,
    git_diff: str | None = None,
    max_files: int = 40,
) -> DebugContext:
    """Collect failure evidence without requiring Git."""

    relevant_files = select_relevant_files(project, result, changed_files, max_files=max_files)
    return DebugContext(
        project=project,
        failed_check=result,
        relevant_files=relevant_files,
        git_diff=git_diff,
    )


def select_relevant_files(
    project: ProjectInfo,
    result: CheckResult,
    changed_files: set[Path] | None = None,
    max_files: int = 40,
) -> tuple[Path, ...]:
    """Select bounded evidence using failure paths, config, tests, and local imports."""

    if max_files < 1:
        return ()
    output = f"{result.stdout}\n{result.stderr}"
    selected: list[Path] = []
    selected_set: set[Path] = set()

    def add(path: Path | None) -> None:
        if path is None or path in selected_set or not path.is_file() or len(selected) >= max_files:
            return
        selected.append(path)
        selected_set.add(path)

    for match in _PATH_PATTERN.findall(output):
        add(_resolve_candidate(project.root, match))
    for path in sorted(changed_files or set()):
        add(_resolve_candidate(project.root, str(path)))
    for path in _configuration_files(project.root):
        add(path)

    # Recursively expand local dependencies within the file budget.
    # a failing file is available even when its path never appears in the error.
    index = 0
    while index < len(selected) and len(selected) < max_files:
        for dependency in _local_dependencies(project.root, selected[index]):
            add(dependency)
        index += 1

    for path in _nearby_tests(project.root, selected, result):
        add(path)
    return tuple(selected)


def build_agent_prompt(context: DebugContext) -> str:
    """Render evidence as a prompt for a debugging model or VS Code agent."""

    result = context.failed_check
    files = "\n".join(f"- {path.relative_to(context.project.root)}" for path in context.relevant_files)
    return "\n".join(
        (
            "Investigate this failed project check and propose the smallest root-cause fix.",
            f"Project root: {context.project.root}",
            f"Project types: {', '.join(context.project.project_types)}",
            f"Check: {' '.join(result.command)} (exit code {result.returncode})",
            "Relevant files:",
            files or "- None identified",
            "Failure output:",
            result.stdout.rstrip() or "(no stdout)",
            result.stderr.rstrip() or "(no stderr)",
            "Version-control diff:",
            "(Git evidence unavailable)" if context.git_diff is None else context.git_diff or "(working tree clean)",
            "Return: root cause, proposed patch, and a validation command.",
        )
    )


def _resolve_candidate(root: Path, value: str) -> Path | None:
    path = Path(value.strip().strip("()[]{}"))
    if path.is_absolute():
        candidate = path.resolve()
    else:
        candidate = (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _configuration_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.iterdir() if path.is_file() and path.name in _CONFIG_NAMES))


def _nearby_tests(root: Path, selected: list[Path], result: CheckResult) -> tuple[Path, ...]:
    if result.name not in {"pytest", "test", "npm test"}:
        return ()
    selected_names = {path.stem.lower() for path in selected}
    test_files = sorted(
        path
        for directory in (root / "tests", root / "test")
        if directory.is_dir()
        for path in directory.rglob("*")
        if path.is_file() and path.suffix in {".py", ".js", ".jsx", ".ts", ".tsx"}
    )
    matching = [path for path in test_files if path.stem.lower() in selected_names]
    return tuple(matching + [path for path in test_files if path not in matching][:5])


def _local_dependencies(root: Path, path: Path) -> tuple[Path, ...]:
    if path.suffix == ".py":
        return _python_dependencies(root, path)
    if path.suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs"}:
        return tuple(
            dependency
            for specifier in _JS_IMPORT_PATTERN.findall(_read_text(path))
            if specifier.startswith(".")
            for dependency in (_resolve_javascript_import(path, specifier),)
            if dependency is not None
        )
    return ()


def _python_dependencies(root: Path, path: Path) -> tuple[Path, ...]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return ()
    dependencies: list[Path] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports = [(alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports = [(node.module, node.level)]
            else:
                imports = [(alias.name, node.level) for alias in node.names]
        else:
            continue
        for name, level in imports:
            dependency = _resolve_python_import(root, name, path, level)
            if dependency is not None and dependency != path:
                dependencies.append(dependency)
    return tuple(dict.fromkeys(dependencies))


def _resolve_python_import(root: Path, name: str, source: Path | None = None, level: int = 0) -> Path | None:
    if not name:
        return None
    parts = name.split(".")
    if source is not None and level:
        relative_root = source.parent
        for _ in range(level - 1):
            relative_root = relative_root.parent
        search_roots = (relative_root,)
    else:
        search_roots = (root, root / "src")
    for search_root in search_roots:
        module = search_root.joinpath(*parts)
        for candidate in (module.with_suffix(".py"), module / "__init__.py"):
            if candidate.is_file():
                try:
                    candidate.resolve().relative_to(root.resolve())
                except ValueError:
                    return None
                return candidate.resolve()
    return None


def _resolve_javascript_import(source: Path, specifier: str) -> Path | None:
    base = (source.parent / specifier).resolve()
    extensions = ("", ".js", ".jsx", ".ts", ".tsx", ".mjs")
    for extension in extensions:
        candidate = Path(f"{base}{extension}")
        if candidate.is_file():
            return candidate
    for extension in extensions[1:]:
        candidate = base / f"index{extension}"
        if candidate.is_file():
            return candidate
    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""