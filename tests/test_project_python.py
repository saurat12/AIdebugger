import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aidebug import discovery
from aidebug.agent import CheckValidator
from aidebug.models import CheckSpec, ProjectInfo
from aidebug.workspace import isolated_workspace


def interpreter_path(root):
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@pytest.mark.parametrize("environment", [".venv", "venv", "."])
def test_local_environment_selected_without_git(tmp_path, monkeypatch, environment):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n[tool.ruff]\n[tool.mypy]\n")
    env = tmp_path / environment
    interpreter = interpreter_path(env)
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    (env / "pyvenv.cfg").touch()
    probe = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(discovery.subprocess, "run", probe)
    monkeypatch.setattr(sys, "executable", "C:/pipx/venvs/aidebugger/Scripts/python.exe")
    project = discovery.discover_repository(tmp_path)
    assert len(project.checks) == 3
    assert all(check.command[0] == str(interpreter.absolute()) for check in project.checks)
    probe.assert_called_once()
    assert probe.call_args.args[0] == (str(interpreter.absolute()), "-I", "-c", "pass")


def test_missing_environment_never_falls_back_to_pipx_or_path(tmp_path, monkeypatch):
    (tmp_path / "pytest.ini").touch()
    monkeypatch.setattr(sys, "executable", "C:/pipx/venvs/aidebugger/Scripts/python.exe")
    probe = Mock()
    monkeypatch.setattr(discovery.subprocess, "run", probe)
    with pytest.raises(ValueError, match="No usable project-local Python"):
        discovery.discover_repository(tmp_path)
    probe.assert_not_called()


def test_broken_environment_tries_next_local_candidate(tmp_path, monkeypatch):
    for name in (".venv", "venv"):
        interpreter = interpreter_path(tmp_path / name)
        interpreter.parent.mkdir(parents=True)
        interpreter.touch()
    probe = Mock(side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)])
    monkeypatch.setattr(discovery.subprocess, "run", probe)
    assert discovery._project_python(tmp_path) == str(interpreter_path(tmp_path / "venv"))
    assert probe.call_count == 2


@pytest.mark.parametrize("failure", [OSError("not executable"), subprocess.TimeoutExpired("python", 5)])
def test_unusable_environment_is_explicit(tmp_path, monkeypatch, failure):
    interpreter = interpreter_path(tmp_path / ".venv")
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    monkeypatch.setattr(discovery.subprocess, "run", Mock(side_effect=failure))
    with pytest.raises(ValueError, match="No usable project-local Python"):
        discovery._project_python(tmp_path)


@pytest.mark.parametrize("environment", [".venv", "venv", "."])
def test_isolated_validation_reuses_environment_without_copying_it(tmp_path, environment):
    env = tmp_path / environment
    subprocess.run((sys.executable, "-m", "venv", "--without-pip", str(env)), check=True, capture_output=True)
    interpreter = interpreter_path(env)
    assert discovery._project_python(tmp_path) == str(interpreter)
    source = tmp_path / "app.py"
    source.write_text("VALUE = 'original'\n")
    check = CheckSpec("environment", (str(interpreter), "-c", "import app, sys; print(app.VALUE); print(sys.prefix)"), "Verify target environment")
    with isolated_workspace(tmp_path) as workspace:
        assert not (workspace / ".venv").exists()
        assert not (workspace / "venv").exists()
        assert not (workspace / "pyvenv.cfg").exists()
        assert not interpreter_path(workspace).exists()
        (workspace / "app.py").write_text("VALUE = 'isolated'\n")
        result = CheckValidator().validate(ProjectInfo(workspace, ("python",), (check,)))
        assert result.passed
        output = result.results[0].stdout.splitlines()
        assert output[0] == "isolated"
        assert Path(output[1]) == env
    assert source.read_text() == "VALUE = 'original'\n"


def test_python_project_without_checks_needs_no_interpreter(tmp_path):
    (tmp_path / "requirements.txt").touch()
    project = discovery.discover_repository(tmp_path)
    assert project.project_types == ("python",)
    assert project.checks == ()
