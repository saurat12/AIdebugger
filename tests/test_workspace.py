import pytest

from aidebug.workspace import apply_unified_diff, isolated_workspace


def patch(text):
    return text.replace("\\n", "\n")


def test_patch_engine_modifies_existing_file(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("one\ntwo\n", encoding="utf-8")
    apply_unified_diff(tmp_path, patch("--- a/app.py\\n+++ b/app.py\\n@@ -1,2 +1,2 @@\\n-one\\n+ONE\\n two\\n"))
    assert source.read_text(encoding="utf-8") == "ONE\ntwo\n"


def test_patch_engine_creates_and_deletes_files(tmp_path):
    apply_unified_diff(tmp_path, patch("--- /dev/null\\n+++ b/new.py\\n@@ -0,0 +1 @@\\n+print('new')\\n"))
    assert (tmp_path / "new.py").exists()
    apply_unified_diff(tmp_path, patch("--- a/new.py\\n+++ /dev/null\\n@@ -1 +0,0 @@\\n-print('new')\\n"))
    assert not (tmp_path / "new.py").exists()


def test_patch_engine_supports_multiple_hunks_and_files(tmp_path):
    (tmp_path / "a.txt").write_text("a1\na2\na3\na4\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b1\n", encoding="utf-8")
    apply_unified_diff(tmp_path, patch(
        "--- a/a.txt\\n+++ b/a.txt\\n@@ -1 +1 @@\\n-a1\\n+A1\\n@@ -4 +4 @@\\n-a4\\n+A4\\n"
        "--- a/b.txt\\n+++ b/b.txt\\n@@ -1 +1 @@\\n-b1\\n+B1\\n"
    ))
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "A1\na2\na3\nA4\n"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "B1\n"


def test_patch_engine_rejects_malformed_or_stale_patches(tmp_path):
    (tmp_path / "app.py").write_text("return 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        apply_unified_diff(tmp_path, "")
    with pytest.raises(ValueError, match="Unsupported or invalid"):
        apply_unified_diff(tmp_path, patch("diff --git a/app.py b/app.py\\nindex abc..def 100644\\n--- a/app.py\\n+++ b/app.py\\n@@ -1 +1 @@\\n-return 1\\n+return 2\\n"))
    with pytest.raises(ValueError):
        apply_unified_diff(tmp_path, patch("--- a/app.py\\n+++ b/app.py\\n@@ -1 +1 @@\\n-return wrong\\n+return 2\\n"))
    apply_unified_diff(tmp_path, patch("--- a/app.py\\n+++ b/app.py\\n@@ -1 +1 @@\\n-return 1\\n+return 2\\n"))
    with pytest.raises(ValueError):
        apply_unified_diff(tmp_path, patch("--- a/app.py\\n+++ b/app.py\\n@@ -1 +1 @@\\n-return 1\\n+return 3\\n"))


def test_patch_engine_handles_no_newline_marker_and_second_patch(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("return 1", encoding="utf-8")
    apply_unified_diff(tmp_path, patch("--- a/app.py\\n+++ b/app.py\\n@@ -1 +1 @@\\n-return 1\\n+return 2\\n\\ No newline at end of file\\n"))
    assert source.read_text(encoding="utf-8") == "return 2"
    apply_unified_diff(tmp_path, patch("--- a/app.py\\n+++ b/app.py\\n@@ -1 +1 @@\\n-return 2\\n+return 3\\n"))
    assert source.read_text(encoding="utf-8") == "return 3\n"


def test_isolated_workspace_cleans_up_and_preserves_original(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("return 1\n", encoding="utf-8")
    workspace_path = None
    with isolated_workspace(tmp_path) as workspace:
        workspace_path = workspace
        apply_unified_diff(workspace, patch("--- a/app.py\\n+++ b/app.py\\n@@ -1 +1 @@\\n-return 1\\n+return 2\\n"))
        assert (workspace / "app.py").read_text(encoding="utf-8") == "return 2\n"
    assert not workspace_path.exists()
    assert source.read_text(encoding="utf-8") == "return 1\n"