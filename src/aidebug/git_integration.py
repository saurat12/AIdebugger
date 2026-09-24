"""Optional Git integration for the Git-independent AI Debugger core."""

import subprocess
from pathlib import Path


def find_git_root(start: Path | str) -> Path | None:
    """Return the Git root for *start*, or None when Git is unavailable."""

    path = Path(start).expanduser().resolve()
    probe = path if path.is_dir() else path.parent
    try:
        result = subprocess.run(
            ("git", "rev-parse", "--show-toplevel"),
            cwd=probe,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    output = result.stdout.strip()
    return Path(output).resolve() if output else None


def collect_evidence(project_root: Path) -> tuple[set[Path], str | None]:
    """Return Git evidence limited to project_root, when Git is available."""

    project_root = project_root.expanduser().resolve()
    git_root = find_git_root(project_root)
    if git_root is None:
        return set(), None
    try:
        project_path = project_root.relative_to(git_root).as_posix()
    except ValueError:
        return set(), None
    pathspec = project_path or "."

    try:
        changed = subprocess.run(
            ("git", "diff", "--name-only", "--diff-filter=ACMRT", "--", pathspec),
            cwd=git_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        diff = subprocess.run(
            ("git", "diff", "--no-ext-diff", "--unified=80", "--", pathspec),
            cwd=git_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return set(), None
    files = {_resolve_file(git_root, project_root, line.strip()) for line in changed.splitlines() if line.strip()}
    return {path for path in files if path is not None}, diff


def _resolve_file(git_root: Path, project_root: Path, value: str) -> Path | None:
    candidate = (git_root / value).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None