"""Persist cumulative repairs after successful isolated validation."""

import difflib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .models import DebugRun
from .reports import render_repair_report
from .workspace import parse_unified_diff, _safe_patch_path


def remember_originals(workspace: Path, patch: str, originals: dict[str, bytes | None]) -> None:
    """Capture each touched file before its first repair attempt."""
    for file_diff in parse_unified_diff(patch):
        for name in (file_diff.old_name, file_diff.new_name):
            if name == "/dev/null":
                continue
            path = _safe_patch_path(workspace, name)
            relative = path.relative_to(workspace.resolve()).as_posix()
            if relative not in originals:
                originals[relative] = path.read_bytes() if path.is_file() else None


def save_validated_repair(run: DebugRun, workspace: Path, originals: dict[str, bytes | None]) -> tuple[Path, Path]:
    """Diff original content against validated content, never concatenate retries."""
    if run.validation is None or not run.validation.passed:
        raise ValueError("Only validated repairs can be saved")
    chunks: list[str] = []
    changed: list[str] = []
    for name, before in sorted(originals.items()):
        path = _safe_patch_path(workspace, name)
        after = path.read_bytes() if path.is_file() else None
        if before == after:
            continue
        changed.append(name)
        lines = difflib.unified_diff(
            before.decode("utf-8").splitlines(keepends=True) if before is not None else [],
            after.decode("utf-8").splitlines(keepends=True) if after is not None else [],
            fromfile=f"a/{name}" if before is not None else "/dev/null",
            tofile=f"b/{name}" if after is not None else "/dev/null",
        )
        for line in lines:
            chunks.append(line)
            if not line.endswith("\n"):
                chunks.append("\n\\ No newline at end of file\n")
    patch = "".join(chunks)
    project = run.initial_context.project.root.resolve()
    folder = project / ".aidebug"
    if folder.is_symlink() or folder.resolve() != folder:
        raise ValueError("Repair artifact directory must stay inside the project")
    folder.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S_%f") + "_" + uuid4().hex[:12]
    patch_path = folder / f"validated_patch_{stamp}.diff"
    report_path = folder / f"debug_report_{stamp}.md"
    report = render_repair_report(run, patch, changed)
    with patch_path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(patch)
    try:
        with report_path.open("x", encoding="utf-8", newline="") as handle:
            handle.write(report)
    except OSError:
        patch_path.unlink()
        raise
    return patch_path, report_path
