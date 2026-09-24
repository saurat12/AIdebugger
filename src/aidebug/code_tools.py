"""Bounded code-inspection tools exposed to the debugging model."""

import os
import re
from itertools import islice
from pathlib import Path
from typing import Any

_MAX_READ_BYTES = 20_000
_MAX_RESULTS = 50
_ALLOWED_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".toml", ".ini", ".cfg", ".txt"}
_IGNORED_DIRS = {".git", ".aidebug", ".venv", "venv", ".test-venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}


class CodeTools:
    """Safe read-only operations constrained to one project root."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def read_file(self, relative_path: str, start_line: int = 1, end_line: int = 240) -> str:
        path = self._safe_path(relative_path)
        if not path.is_file():
            return f"File not found: {relative_path}"
        if start_line < 1 or end_line < start_line:
            return "Invalid line range"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            return f"Unable to read {relative_path}: {exc}"
        selected = lines[start_line - 1 : end_line]
        content = "\n".join(f"{number}: {line}" for number, line in enumerate(selected, start=start_line))
        return content[:_MAX_READ_BYTES]

    def search_code(self, query: str, max_results: int = _MAX_RESULTS) -> str:
        if not query.strip():
            return "Search query must not be empty"
        try:
            pattern = re.compile(query)
        except re.error:
            pattern = re.compile(re.escape(query))
        results: list[str] = []
        for path in self._files():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(lines, start=1):
                if pattern.search(line):
                    results.append(f"{path.relative_to(self.root)}:{line_number}: {line[:500]}")
                    if len(results) >= min(max_results, _MAX_RESULTS):
                        return "\n".join(results)
        return "\n".join(results) or "No matches found"

    def list_files(self, directory: str = ".") -> str:
        path = self._safe_path(directory)
        if not path.is_dir():
            return f"Directory not found: {directory}"
        files = [str(candidate.relative_to(self.root)) for candidate in islice(self._files(path), _MAX_RESULTS)]
        return "\n".join(files[:_MAX_RESULTS]) or "No files found"

    def find_symbol(self, name: str) -> str:
        escaped = re.escape(name)
        query = rf"(?:def|class|function|const|let|var)\s+{escaped}\b|{escaped}\s*="
        return self.search_code(query)

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        operations = {
            "read_file": self.read_file,
            "search_code": self.search_code,
            "list_files": self.list_files,
            "find_symbol": self.find_symbol,
        }
        operation = operations.get(name)
        if operation is None:
            return f"Unknown code tool: {name}"
        try:
            return str(operation(**arguments))
        except (TypeError, ValueError) as exc:
            return f"Invalid arguments for {name}: {exc}"

    @staticmethod
    def specifications() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": "read_file",
                "description": "Read a bounded line range from a project-relative source or configuration file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "relative_path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    "required": ["relative_path"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "search_code",
                "description": "Search project source files with a regular expression and return bounded matching lines.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "list_files",
                "description": "List project files under a project-relative directory.",
                "parameters": {
                    "type": "object",
                    "properties": {"directory": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "find_symbol",
                "description": "Find likely definitions or assignments for a symbol in project source files.",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        ]

    def _safe_path(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Path must stay inside the project root") from exc
        return candidate

    def _files(self, root: Path | None = None):
        base = root or self.root
        if base.is_file():
            yield base
            return
        if any(part in _IGNORED_DIRS for part in base.parts):
            return
        try:
            entries = sorted(os.scandir(base), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            if entry.name in _IGNORED_DIRS:
                continue
            candidate = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                yield from self._files(candidate)
            elif entry.is_file(follow_symlinks=False) and candidate.suffix.lower() in _ALLOWED_SUFFIXES:
                yield candidate
