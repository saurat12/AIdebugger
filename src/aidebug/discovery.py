"""Project and project-type discovery."""

import json
import os
import subprocess
import tomllib
from pathlib import Path

from .models import CheckSpec, ProjectInfo

_PROJECT_MARKERS = (
    "pyvenv.cfg",
    "pyproject.toml",
    "package.json",
    "requirements.txt",
    "setup.py",
    "setup.cfg",
    "pytest.ini",
)
_PYTHON_MARKERS = ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "pytest.ini", "pyvenv.cfg")


def find_project_root(start: Path | str) -> Path:
    """Find the nearest ancestor containing a project marker without Git."""

    start_path = Path(start).expanduser().resolve()
    directory = start_path if start_path.is_dir() else start_path.parent
    for candidate in (directory, *directory.parents):
        if any((candidate / marker).is_file() for marker in _PROJECT_MARKERS):
            return candidate
    return directory


def _usable_python(candidate: Path) -> bool:
    """Reject missing or broken environments without using the tool's runtime."""
    if not candidate.is_file():
        return False
    try:
        result = subprocess.run(
            (str(candidate), "-I", "-c", "pass"),
            capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _project_python(root: Path) -> str:
    relative = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    environments = [root / ".venv", root / "venv"]
    if (root / "pyvenv.cfg").is_file():
        environments.append(root)
    for environment in environments:
        candidate = environment.joinpath(*relative).absolute()
        if _usable_python(candidate):
            # Do not resolve Unix venv symlinks to the base interpreter.
            return str(candidate)
    raise ValueError(
        "No usable project-local Python interpreter found. Create or repair .venv or venv "
        "in the target project and install its check dependencies there. "
        "AIdebugger will not use its own interpreter or install project dependencies."
    )


def discover_repository(start: Path | str = ".") -> ProjectInfo:
    """Detect the active project and its safe checks."""

    root = find_project_root(start)
    types: list[str] = []
    checks: list[CheckSpec] = []

    if any((root / name).exists() for name in _PYTHON_MARKERS):
        types.append("python")
        pyproject_tools = _pyproject_tools(root / "pyproject.toml")
        python_checks = []
        if (root / "tests").is_dir() or "pytest" in pyproject_tools or (root / "pytest.ini").exists():
            python_checks.append(("pytest", ("pytest",), "Python tests"))
        if "ruff" in pyproject_tools or (root / "ruff.toml").exists():
            python_checks.append(("ruff", ("ruff", "check", "."), "Python linting"))
        if "mypy" in pyproject_tools or (root / "mypy.ini").exists():
            python_checks.append(("mypy", ("mypy", "."), "Python type checking"))
        if python_checks:
            python_executable = _project_python(root)
            checks.extend(CheckSpec(name, (python_executable, "-m", *args), reason) for name, args, reason in python_checks)

    package_json = root / "package.json"
    if package_json.exists():
        types.append("node")
        package = _read_package_json(package_json)
        dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
        if "react" in dependencies or "react-dom" in dependencies:
            types.append("react")
        scripts = package.get("scripts", {})
        for name, reason in (("test", "Node tests"), ("lint", "JavaScript linting"), ("build", "Project build")):
            if name in scripts:
                checks.append(CheckSpec(f"npm {name}", ("npm", "run", name), reason))

    if not types:
        types.append("unknown")
    return ProjectInfo(root=root, project_types=tuple(types), checks=tuple(checks))


def _read_package_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _pyproject_tools(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    tool_table = document.get("tool", {})
    return set(tool_table) if isinstance(tool_table, dict) else set()
