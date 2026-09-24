import subprocess
import os
import sys

from aidebug.context import build_debug_context, select_relevant_files
from aidebug.discovery import discover_repository
from aidebug.git_integration import collect_evidence
from aidebug.models import CheckResult, ProjectInfo


def test_node_project_works_without_git(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node test.js"}}', encoding="utf-8")

    project = discover_repository(tmp_path)

    assert project.root == tmp_path.resolve()
    assert project.project_types == ("node",)
    assert project.checks[0].command == ("npm", "run", "test")


def test_nested_project_uses_supplied_directory_not_git_root(tmp_path, local_project_python):
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    nested = tmp_path / "backend"
    nested.mkdir()
    (nested / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts":{"build":"vite build"}}', encoding="utf-8")

    import shutil
    shutil.copytree(tmp_path / ".venv", nested / ".venv")
    project = discover_repository(nested)

    assert project.root == nested.resolve()
    assert project.project_types == ("python",)


def test_discovery_from_nested_file_uses_nearest_project_marker(tmp_path, local_project_python):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    nested = tmp_path / "src" / "myapp"
    nested.mkdir(parents=True)
    service = nested / "service.py"
    service.write_text("def run():\n    return 1\n", encoding="utf-8")

    from_file = discover_repository(service)
    from_directory = discover_repository(nested)

    assert from_file.root == tmp_path.resolve()
    assert from_directory.root == tmp_path.resolve()


def test_project_local_python_interpreter_is_preferred(tmp_path, monkeypatch):
    monkeypatch.setattr("aidebug.discovery._usable_python", lambda path: path.is_file())
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    relative = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    local_python = tmp_path.joinpath(".venv", *relative)
    local_python.parent.mkdir(parents=True)
    local_python.write_text("placeholder", encoding="utf-8")
    (tmp_path / "tests").mkdir()

    project = discover_repository(tmp_path)

    assert project.checks[0].command == (str(local_python.resolve()), "-m", "pytest")
    assert project.checks[0].command[0] != sys.executable


def test_git_evidence_distinguishes_unavailable_from_clean(tmp_path, monkeypatch):
    import aidebug.git_integration as git_integration

    monkeypatch.setattr(git_integration.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("git unavailable")))
    changed, diff = collect_evidence(tmp_path)
    assert changed == set()
    assert diff is None

    monkeypatch.undo()
    subprocess.run(("git", "init", "-q"), cwd=tmp_path, check=True)
    changed, diff = collect_evidence(tmp_path)
    assert changed == set()
    assert diff == ""


def test_python_imports_cover_direct_src_and_relative_packages(tmp_path):
    (tmp_path / "database.py").write_text("query = 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("import database\nfrom database import query\n", encoding="utf-8")
    package = tmp_path / "src" / "myapp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "app.py").write_text("from .database import query\nfrom ..config import settings\n", encoding="utf-8")
    (package / "database.py").write_text("query = 2\n", encoding="utf-8")
    (tmp_path / "src" / "config.py").write_text("settings = {}\n", encoding="utf-8")
    project = ProjectInfo(tmp_path, ("python",), ())

    direct = select_relevant_files(project, CheckResult("test", ("test",), 1, "", "File app.py:1", 0.1))
    src = select_relevant_files(project, CheckResult("test", ("test",), 1, "", "File src/myapp/app.py:1", 0.1))

    assert tmp_path / "database.py" in direct
    assert package / "database.py" in src
    assert tmp_path / "src" / "config.py" in src
    assert all(path.is_relative_to(tmp_path) for path in (*direct, *src))


def test_context_max_files_bounds_recursive_imports(tmp_path):
    for index in range(10):
        current = tmp_path / f"module{index}.py"
        next_name = f"module{index + 1}" if index < 9 else "module0"
        current.write_text(f"import {next_name}\n", encoding="utf-8")
    project = ProjectInfo(tmp_path, ("python",), ())
    result = CheckResult("test", ("test",), 1, "", "File module0.py:1", 0.1)

    selected = select_relevant_files(project, result, max_files=3)

    assert len(selected) == 3


def test_context_prompt_distinguishes_git_evidence_states(tmp_path):
    project = ProjectInfo(tmp_path, ("unknown",), ())
    result = CheckResult("test", ("test",), 1, "", "failed", 0.1)
    unavailable = build_debug_context(project, result)
    clean = build_debug_context(project, result, git_diff="")

    from aidebug.context import build_agent_prompt

    assert "Git evidence unavailable" in build_agent_prompt(unavailable)
    assert "working tree clean" in build_agent_prompt(clean)