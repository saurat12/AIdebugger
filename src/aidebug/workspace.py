"""Temporary workspaces used to test proposed patches safely."""

import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class InvalidUnifiedDiffError(ValueError):
    """The patch has invalid structure or unsafe paths."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"Invalid unified diff: {detail}")


class PatchApplicabilityError(ValueError):
    """The patch cannot be applied to the current workspace contents."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"Patch does not apply to the isolated workspace: {detail}")


@contextmanager
def isolated_workspace(root: Path) -> Iterator[Path]:
    """Copy a project into a temporary workspace and always remove the copy."""

    temporary = Path(tempfile.mkdtemp(prefix="aidebug-"))
    workspace = temporary / root.name
    try:
        def ignore(directory: str, names: list[str]) -> set[str]:
            ignored = set(names) & {".git", ".aidebug", ".venv", "venv", "node_modules", "__pycache__"}
            folder = Path(directory)
            ignored.update(name for name in names if (folder / name / "pyvenv.cfg").is_file())
            if folder == root and (root / "pyvenv.cfg").is_file():
                ignored.update(set(names) & {"pyvenv.cfg", "Scripts", "bin", "Lib", "lib", "lib64", "Include", "include", "share"})
            return ignored

        # Checks retain the original environment's absolute interpreter path;
        # only source files are copied and subprocess cwd is the isolated root.
        shutil.copytree(root, workspace, ignore=ignore)
        yield workspace
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def apply_unified_diff(root: Path, unified_diff: str) -> None:
    """Apply one proposed patch to the current state of an isolated workspace."""

    if not unified_diff.strip():
        raise InvalidUnifiedDiffError("Fixer returned an empty patch")
    _apply_unified_diff(root, unified_diff)


@dataclass(frozen=True)
class DiffHunk:
    old_start: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class FileDiff:
    old_name: str
    new_name: str
    hunks: tuple[DiffHunk, ...]


def parse_unified_diff(diff: str) -> tuple[FileDiff, ...]:
    """Parse file boundaries using hunk counts, not content resembling headers."""
    lines = diff.splitlines(keepends=True)
    index = 0
    files = []
    while index < len(lines):
        if not lines[index].startswith("--- "):
            raise InvalidUnifiedDiffError(f"Unsupported or invalid plain unified diff header at line {index + 1}")
        old_name = _diff_path(lines[index][4:])
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise InvalidUnifiedDiffError("Fixer patch is missing a unified diff target")
        new_name = _diff_path(lines[index + 1][4:])
        index += 2
        hunks = []
        while index < len(lines) and lines[index].startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[index])
            if not match:
                raise InvalidUnifiedDiffError(f"Invalid unified diff hunk at line {index + 1}")
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_count = int(match.group(4) or "1")
            header_line = index + 1
            index += 1
            body = []
            consumed_old = consumed_new = 0
            while consumed_old < old_count or consumed_new < new_count:
                if index >= len(lines):
                    raise InvalidUnifiedDiffError(f"Patch hunk line counts do not match its header at line {header_line}: expected old={old_count}, new={new_count}; observed old={consumed_old}, new={consumed_new} at end of patch")
                line = lines[index]
                prefix = line[:1]
                if prefix == " ":
                    consumed_old += 1
                    consumed_new += 1
                elif prefix == "-":
                    consumed_old += 1
                elif prefix == "+":
                    consumed_new += 1
                elif prefix == "\\":
                    if line.rstrip("\r\n") != "\\ No newline at end of file" or not body or body[-1].startswith("\\"):
                        raise InvalidUnifiedDiffError(f"Invalid unified diff marker at line {index + 1}")
                else:
                    raise InvalidUnifiedDiffError(f"Invalid unified diff line at line {index + 1}")
                if consumed_old > old_count or consumed_new > new_count:
                    raise InvalidUnifiedDiffError(f"Patch hunk line counts do not match its header at line {header_line}: expected old={old_count}, new={new_count}; observed old={consumed_old}, new={consumed_new} at line {index + 1}")
                body.append(line)
                index += 1
            if index < len(lines) and lines[index].startswith("\\"):
                if lines[index].rstrip("\r\n") != "\\ No newline at end of file" or not body:
                    raise InvalidUnifiedDiffError(f"Invalid unified diff marker at line {index + 1}")
                body.append(lines[index])
                index += 1
            hunks.append(DiffHunk(old_start, tuple(body)))
            if index < len(lines) and not lines[index].startswith(("@@", "--- ")):
                raise InvalidUnifiedDiffError(f"Invalid unified diff line at line {index + 1}")
        if not hunks:
            raise InvalidUnifiedDiffError("Patch file is missing hunks")
        files.append(FileDiff(old_name, new_name, tuple(hunks)))
    if not files:
        raise InvalidUnifiedDiffError("Patch contains no file changes")
    return tuple(files)


def _apply_unified_diff(root: Path, diff: str) -> None:
    # Validate every file before writing any of them. Rejected retries must not
    # leave a partially applied proposal in the cumulative workspace.
    staged: dict[Path, str | None] = {}
    for file_diff in parse_unified_diff(diff):
        source_path = _safe_patch_path(root, file_diff.old_name) if file_diff.old_name != "/dev/null" else None
        target_path = _safe_patch_path(root, file_diff.new_name) if file_diff.new_name != "/dev/null" else None
        if source_path is not None and (staged[source_path] is None if source_path in staged else not source_path.is_file()):
            raise PatchApplicabilityError("Patch source does not exist in the workspace")
        try:
            content = staged[source_path] if source_path in staged else source_path.read_text(encoding="utf-8") if source_path else ""
            original = content.splitlines(keepends=True)
        except (OSError, UnicodeDecodeError):
            raise PatchApplicabilityError("Unable to read patch source") from None
        updated: list[str] = []
        source_index = 0
        for hunk in file_diff.hunks:
            hunk_start = max(hunk.old_start - 1, 0)
            if hunk_start > len(original):
                raise PatchApplicabilityError("Patch hunk starts beyond the current file")
            updated.extend(original[source_index:hunk_start])
            source_index = hunk_start
            previous_prefix = None
            for line in hunk.lines:
                if line.startswith(" "):
                    _expect_line(original, source_index, line[1:])
                    updated.append(original[source_index])
                    source_index += 1
                elif line.startswith("-"):
                    _expect_line(original, source_index, line[1:])
                    source_index += 1
                elif line.startswith("+"):
                    updated.append(line[1:])
                elif line.startswith("\\") and previous_prefix in (" ", "+"):
                    updated[-1] = updated[-1].rstrip("\r\n")
                previous_prefix = line[:1]
        updated.extend(original[source_index:])
        if target_path is None:
            if source_path:
                staged[source_path] = None
        else:
            staged[target_path] = "".join(updated)
    for path, content in staged.items():
        if content is None:
            if path.exists():
                path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


def _diff_path(value: str) -> str:
    value = value.rstrip("\r\n").split("\t", 1)[0]
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if not value or value in {".", ".."}:
        raise InvalidUnifiedDiffError("Invalid plain unified diff path")
    return value


def _safe_patch_path(root: Path, name: str) -> Path:
    """Resolve a patch path and reject anything outside the workspace."""

    if not name or name in {".", ".."}:
        raise InvalidUnifiedDiffError("Invalid patch path")
    workspace = root.resolve()
    candidate = (workspace / name).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError:
        raise InvalidUnifiedDiffError("Patch path escapes workspace") from None
    return candidate


def _expect_line(lines: list[str], index: int, expected: str) -> None:
    if index >= len(lines) or lines[index].rstrip("\r\n") != expected.rstrip("\r\n"):
        raise PatchApplicabilityError("Fixer patch does not match the isolated workspace")
