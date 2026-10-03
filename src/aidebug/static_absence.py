"""Bounded, read-only proof of explicitly scoped project-local absence."""

import ast
import os
import stat
from pathlib import Path, PurePosixPath

from .code_tools import _IGNORED_DIRS
from .hunt import Finding
from .models import CheckResult


MAX_FILES = 500
MAX_SOURCE_BYTES = 120_000


def absence_spec(hypothesis):
    for value in (hypothesis.verification_spec, hypothesis.verification_plan, hypothesis.reproduction):
        if isinstance(value, dict) and value.get("kind") == "static_absence":
            return value
    return None


def _relative(value, *, allow_root=False):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("static absence requires a project-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", "..") for part in path.parts) or (value == "." and not allow_root):
        raise ValueError("static absence path must stay within the project scope")
    return path


def _inside(root, relative):
    candidate = root.joinpath(*relative.parts)
    if not candidate.resolve().is_relative_to(root) or any((part.is_symlink() or getattr(part, "is_junction", lambda: False)())
                                                         for part in (candidate, *candidate.parents)
                                                         if part != root and part.is_relative_to(root)):
        raise ValueError("static absence path is unsafe or escapes the project")
    return candidate


def _is_file(path):
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False


def _is_dir(path):
    try:
        return stat.S_ISDIR(path.stat().st_mode)
    except FileNotFoundError:
        return False


def _index(scope, root):
    """Complete source index or an explicit incomplete-search reason."""
    found = []
    errors = []
    for directory, dirs, files in os.walk(scope, followlinks=False, onerror=errors.append):
        dirs[:] = sorted(name for name in dirs if name not in _IGNORED_DIRS)
        base = Path(directory)
        if any((base / name).is_symlink() or getattr(base / name, "is_junction", lambda: False)() for name in dirs):
            return found, "static absence search incomplete: symlinked source directory"
        for name in sorted(files):
            path = base / name
            if path.suffix != ".py":
                continue
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                return found, "static absence search incomplete: symlinked source file"
            found.append(path)
            if len(found) > MAX_FILES:
                return found, "static absence search incomplete: source file limit"
    return found, "static absence search incomplete: source directory could not be read" if errors else None


class StaticAbsenceVerifier:
    """Verify explicit file/module/symbol claims without importing or executing code."""

    def verify(self, project, hypothesis):
        spec = absence_spec(hypothesis)
        if spec is None:
            return None
        pending = "high_confidence" if hypothesis.confidence >= .85 else "unconfirmed"
        plan = {"schema_version": 1, "source": "deterministic_verifier", "capability": "static_absence",
                "verifier": "project-source-index", "file": hypothesis.suspected_file,
                "symbol": hypothesis.suspected_symbol, "plan": spec}
        try:
            if set(spec) - {"kind", "subject", "target", "scope", "local"} or spec.get("subject") not in {"file", "module", "symbol", "import"}:
                raise ValueError("static absence requires subject, target, and scope")
            root = Path(project.root).resolve()
            scope_name = _relative(spec.get("scope", "."), allow_root=True)
            scope = _inside(root, scope_name)
            if not _is_dir(scope):
                raise ValueError("static absence search incomplete: declared project scope is unavailable")
            subject, target = spec["subject"], spec.get("target")
            if subject in {"module", "import"}:
                reference_locations = []
                if not isinstance(target, str) or not target or target.startswith("..") or not all(part.isidentifier() and not part.startswith("__") for part in target.lstrip(".").split(".")):
                    raise ValueError("static absence import target must be a dotted local module")
                if spec.get("local") is not True and not target.startswith("."):
                    raise ValueError("static absence cannot classify an installed dependency as project-local without local=true")
                module = target.lstrip(".").replace(".", "/")
                lookup_scope = scope
                if target.startswith("."):
                    reference_path = _inside(root, _relative(hypothesis.suspected_file))
                    if not _is_file(reference_path):
                        raise ValueError("static absence search incomplete: referencing source is unavailable")
                    if not reference_path.is_relative_to(scope):
                        raise ValueError("static absence search incomplete: referencing source is outside declared scope")
                    lookup_scope = reference_path.parent
                    reference_locations.append(reference_path.relative_to(root).as_posix())
                if not target.startswith("."):
                    package = module.split("/", 1)[0]
                    package_root = _inside(root, lookup_scope.relative_to(root) / package)
                    if "/" not in module or not _is_dir(package_root):
                        raise ValueError("static absence cannot distinguish an absent local import from a missing installed dependency")
                if subject == "import":
                    source = _inside(root, _relative(hypothesis.suspected_file))
                    if not _is_file(source) or source.stat().st_size > MAX_SOURCE_BYTES:
                        raise ValueError("static absence search incomplete: referencing source is unavailable")
                    if source.relative_to(root).as_posix() not in reference_locations:
                        reference_locations.append(source.relative_to(root).as_posix())
                    try:
                        tree = ast.parse(source.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, SyntaxError):
                        raise ValueError("static absence search incomplete: referencing source could not be indexed") from None
                    referenced = any((isinstance(node, ast.Import) and any(alias.name == target for alias in node.names)) or
                                     (isinstance(node, ast.ImportFrom) and node.module == target.lstrip(".") and
                                      ((target.startswith(".") and node.level == 1) or (not target.startswith(".") and node.level == 0)))
                                     for node in ast.walk(tree))
                    if not referenced:
                        raise ValueError("static absence import target has no matching local source reference")
                candidates = [_inside(root, lookup_scope.relative_to(root) / (module + ".py")),
                              _inside(root, lookup_scope.relative_to(root) / module / "__init__.py")]
                namespace_directory = _inside(root, lookup_scope.relative_to(root) / module)
                searched = reference_locations + [path.relative_to(root).as_posix() for path in candidates]
                searched.append(namespace_directory.relative_to(root).as_posix() + "/")
                present = any([_is_file(path) for path in candidates] + [_is_dir(namespace_directory)])
                incomplete = None
            elif subject == "file":
                path = _inside(root, scope.relative_to(root) / _relative(target))
                searched = [path.relative_to(root).as_posix()]
                present, incomplete = _is_file(path), None
            else:
                if not isinstance(target, str) or not target.isidentifier() or target.startswith("__"):
                    raise ValueError("static absence symbol must be a local identifier")
                reference = _inside(root, _relative(hypothesis.suspected_file))
                if not _is_file(reference) or reference.stat().st_size > MAX_SOURCE_BYTES:
                    raise ValueError("static absence search incomplete: referencing source is unavailable")
                try:
                    reference_tree = ast.parse(reference.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, SyntaxError):
                    raise ValueError("static absence search incomplete: referencing source could not be indexed") from None
                if not any((isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == target) or
                           (isinstance(node, ast.Attribute) and node.attr == target)
                           for node in ast.walk(reference_tree)):
                    raise ValueError("unresolved local symbol after bounded project search: no matching source reference")
                files, incomplete = _index(scope, root)
                searched = [reference.relative_to(root).as_posix()]
                present = False
                if incomplete is None:
                    for path in files:
                        relative = path.relative_to(root).as_posix()
                        if relative not in searched:
                            searched.append(relative)
                        try:
                            if path.stat().st_size > MAX_SOURCE_BYTES:
                                incomplete = "static absence search incomplete: source file size limit"
                                break
                            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                        except (OSError, UnicodeError, SyntaxError):
                            incomplete = "static absence search incomplete: source could not be indexed"
                            break
                        if any((isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == target) or
                               (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == target)
                               for node in ast.walk(tree)):
                            present = True
                            break
            evidence = (f"Searched project scope {scope.relative_to(root).as_posix()}; locations: {', '.join(searched)}. "
                        + (incomplete or ("Target exists." if present else "Project-local target is absent.")))
            status = pending if incomplete else "rejected" if present else "confirmed"
            check = None if incomplete else CheckResult("hunt:static_absence", ("project-source-index", str(scope.relative_to(root)), subject, target),
                                                       0 if present else 1, evidence if present else "", evidence if not present else "", 0)
            return Finding(hypothesis, status, ("UNVERIFIABLE: " if incomplete else "") + evidence, check,
                           verification_plan=plan)
        except (OSError, ValueError) as error:
            return Finding(hypothesis, pending, "UNVERIFIABLE: " + str(error), verification_plan=plan)
