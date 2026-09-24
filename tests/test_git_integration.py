import subprocess

from aidebug.git_integration import collect_evidence


def init_git(root):
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)


def stage(root):
    subprocess.run(("git", "add", "-A"), cwd=root, check=True)


def test_collect_evidence_when_project_is_git_root(tmp_path):
    init_git(tmp_path)
    tracked = tmp_path / "app.py"
    tracked.write_text("return 1\n", encoding="utf-8")
    stage(tmp_path)
    tracked.write_text("return 2\n", encoding="utf-8")

    changed, diff = collect_evidence(tmp_path)

    assert changed == {tracked.resolve()}
    assert "app.py" in diff


def test_collect_evidence_maps_nested_project_paths_from_git_root(tmp_path):
    init_git(tmp_path)
    project = tmp_path / "backend"
    project.mkdir()
    tracked = project / "service.py"
    tracked.write_text("return 1\n", encoding="utf-8")
    stage(tmp_path)
    tracked.write_text("return 2\n", encoding="utf-8")

    changed, diff = collect_evidence(project)

    assert changed == {tracked.resolve()}
    assert "backend/service.py" in diff


def test_collect_evidence_excludes_changes_outside_nested_project(tmp_path):
    init_git(tmp_path)
    project = tmp_path / "backend"
    project.mkdir()
    inside = project / "service.py"
    outside = tmp_path / "frontend.js"
    inside.write_text("return 1\n", encoding="utf-8")
    outside.write_text("const value = 1;\n", encoding="utf-8")
    stage(tmp_path)
    inside.write_text("return 2\n", encoding="utf-8")
    outside.write_text("const value = 2;\n", encoding="utf-8")

    changed, diff = collect_evidence(project)

    assert changed == {inside.resolve()}
    assert "backend/service.py" in diff
    assert "frontend.js" not in diff


def test_collect_evidence_returns_unavailable_for_non_git_project(tmp_path, monkeypatch):
    (tmp_path / "service.py").write_text("return 1\n", encoding="utf-8")
    monkeypatch.setattr("aidebug.git_integration.find_git_root", lambda root: None)

    changed, diff = collect_evidence(tmp_path)

    assert changed == set()
    assert diff is None
