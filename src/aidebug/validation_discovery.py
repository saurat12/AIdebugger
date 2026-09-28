"""Extensible project-native check adapters; discovery never installs tools."""

import ast
import configparser
import json
import re
import tomllib
from pathlib import Path

from .models import CheckSpec


EXCLUDED = {".git", ".aidebug", ".venv", "venv", "node_modules", "__pycache__", "site-packages", ".pytest-temp", ".pytest_cache"}


def python_sources(root):
    """Bound traversal and never follow directory symlinks or environments."""
    import os
    count = 0
    for directory, folders, files in os.walk(root, followlinks=False):
        count += len(folders)
        if count > 3000:
            return
        folders[:] = sorted(name for name in folders if name not in EXCLUDED
                            and not (Path(directory) / name).is_symlink()
                            and not (Path(directory) / name / "pyvenv.cfg").is_file())
        for name in sorted(files):
            count += 1
            if count > 3000:
                return
            path = Path(directory) / name
            if path.suffix == ".py" and not path.is_symlink():
                yield path


def configuration(root):
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("Unable to read project validation configuration in pyproject.toml") from exc


def python_adapter(root, config):
    from .discovery import _project_python
    sources = list(python_sources(root))
    if not sources and not any((root / name).exists() for name in
                              ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "pytest.ini", "pyvenv.cfg")):
        return (), ()
    tools = config.get("tool", {})
    ini = configparser.ConfigParser()
    try:
        ini.read(root / "setup.cfg", encoding="utf-8")
    except configparser.Error:
        pass
    pytest_present = "pytest" in tools or (root / "pytest.ini").is_file() or ini.has_section("tool:pytest")
    dependencies = list(config.get("project", {}).get("dependencies", []))
    for group in config.get("project", {}).get("optional-dependencies", {}).values():
        dependencies.extend(group)
    for group in config.get("dependency-groups", {}).values():
        dependencies.extend(item for item in group if isinstance(item, str))
    for path in root.glob("*requirements*.txt"):
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 120000:
            dependencies.extend(path.read_text(encoding="utf-8").splitlines())
    pytest_present |= any(isinstance(dep, str) and re.match(r"^\s*pytest(?:\s|[<>=!~;\[]|$)", dep) for dep in dependencies)
    unittest_dirs = set()
    for path in sources:
        if not (path.name.startswith("test_") or path.name.endswith("_test.py") or path.name == "conftest.py"):
            continue
        try:
            if path.stat().st_size > 120000:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError):
            continue
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
        uses_unittest = "unittest" in imports
        if uses_unittest:
            unittest_dirs.add(path.parent.relative_to(root).as_posix())
        if "pytest" in imports or path.name == "conftest.py" or (
            not uses_unittest and any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                                      and node.name.startswith("test_") for node in ast.walk(tree))):
            pytest_present = True
    specs = []
    if pytest_present:
        specs.append(("pytest", ("pytest",), "Configured/discovered pytest tests"))
    elif unittest_dirs:
        for directory in sorted(unittest_dirs):
            specs.append(("unittest" if len(unittest_dirs) == 1 else f"unittest:{directory}",
                          ("unittest", "discover", "-s", directory), "Discovered unittest tests"))
    if "ruff" in tools or any((root / name).is_file() for name in ("ruff.toml", ".ruff.toml")):
        specs.append(("ruff", ("ruff", "check", "."), "Configured Python linting"))
    if "mypy" in tools or any((root / name).is_file() for name in ("mypy.ini", ".mypy.ini")) or ini.has_section("mypy"):
        specs.append(("mypy", ("mypy", "."), "Configured Python type checks"))
    if not specs:
        return ("python",), ()
    try:
        python = _project_python(root)
        blocked = None
    except ValueError as exc:
        python, blocked = "<project-python-unavailable>", str(exc)
    return ("python",), tuple(CheckSpec(name, (python, "-m", *args), reason, blocked) for name, args, reason in specs)


def node_adapter(root, config):
    path = root / "package.json"
    if not path.is_file():
        return (), ()
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ("node",), ()
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    types = ("node", "react") if {"react", "react-dom"} & dependencies.keys() else ("node",)
    scripts = package.get("scripts", {})
    return types, tuple(CheckSpec(f"npm {name}", ("npm", "run", name), "Declared package script")
                        for name in ("test", "lint", "build") if isinstance(scripts.get(name), str))


def native_adapter(root, config):
    types, checks = [], []
    for marker, kind, commands in (
        ("pom.xml", "java", (("maven test", ("mvn", "test")),)),
        ("go.mod", "go", (("go test", ("go", "test", "./...")),)),
        ("Cargo.toml", "rust", (("cargo check", ("cargo", "check")), ("cargo test", ("cargo", "test")))),
    ):
        if (root / marker).is_file():
            types.append(kind)
            checks.extend(CheckSpec(name, command, f"Project manifest: {marker}") for name, command in commands)
    if any((root / name).is_file() for name in ("build.gradle", "build.gradle.kts")):
        types.append("java")
        checks.append(CheckSpec("gradle test", ("gradle", "test"), "Declared Gradle project"))
    return tuple(dict.fromkeys(types)), tuple(checks)


def custom_adapter(root, config):
    """User-declared argument vectors only; never model-generated shell text."""
    checks = config.get("tool", {}).get("aidebug", {}).get("checks", {})
    if not isinstance(checks, dict):
        raise ValueError("tool.aidebug.checks must be a table of names to command argument arrays")
    result = []
    for name, command in checks.items():
        if not isinstance(command, list) or not command or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command):
            raise ValueError(f"Invalid explicitly configured validation command: {name}")
        if Path(command[0]).name.lower() in {"sh", "bash", "cmd", "cmd.exe", "powershell", "pwsh"}:
            raise ValueError("Declare a validation executable and argument array, not a shell command")
        result.append(CheckSpec(name, tuple(command), "Explicit tool.aidebug.checks command"))
    return (("custom",) if result else ()), tuple(result)


ADAPTERS = [python_adapter, node_adapter, native_adapter, custom_adapter]


def discover_checks(root):
    config = configuration(root)
    types, checks = [], []
    for adapter in ADAPTERS:
        found_types, found_checks = adapter(root, config)
        types.extend(found_types)
        checks.extend(found_checks)
    names = [check.name for check in checks]
    if len(names) != len(set(names)):
        raise ValueError("Validation check names must be unique")
    return tuple(dict.fromkeys(types)) or ("unknown",), tuple(checks)
