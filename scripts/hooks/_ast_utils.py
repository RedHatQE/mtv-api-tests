"""Shared helpers for AGENTS.md AST-based pre-commit hooks."""

from __future__ import annotations

import ast
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

_SKIP_DIR_NAMES: frozenset[str] = frozenset({
    ".venv",
    ".git",
    "docs",
    "__pycache__",
    ".tox",
    ".pi",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "node_modules",
    "site-packages",
})


class FindingCollector:
    """Collect and print path:lineno findings for hook scripts."""

    def __init__(self) -> None:
        self.findings: list[tuple[Path, int, str]] = []

    def report(self, path: Path, lineno: int, msg: str) -> None:
        """Record and print a finding.

        Args:
            path (Path): File containing the violation.
            lineno (int): 1-based line number (0 if unknown).
            msg (str): Human-readable violation message.
        """
        self.findings.append((path, lineno, msg))
        print(f"{path}:{lineno}: {msg}")

    def __bool__(self) -> bool:
        return bool(self.findings)

    def __len__(self) -> int:
        return len(self.findings)


def iter_py_files(paths: list[str]) -> Iterator[Path]:
    """Yield Python files from argv paths or by walking the repository.

    If ``paths`` is empty, walk the current working directory for ``*.py``
    files, excluding ``.venv``, ``.git``, ``docs/``, ``__pycache__``, and
    other tooling directories (``.tox``, ``.pi``, caches, ``site-packages``).

    Args:
        paths (list[str]): Explicit file or directory paths from pre-commit.

    Yields:
        Path: Absolute or relative paths to Python source files.
    """
    if paths:
        for raw in paths:
            path = Path(raw)
            if path.is_file() and path.suffix == ".py":
                yield path
            elif path.is_dir():
                yield from _walk_py_files(path)
        return

    yield from _walk_py_files(Path("."))


def _walk_py_files(root: Path) -> Iterator[Path]:
    """Recursively yield ``*.py`` files under ``root``, skipping excluded dirs.

    Uses ``os.walk`` and prunes ``dirnames`` in-place so excluded directories
    (``.venv``, ``.git``, ``docs/``, caches, etc.) are never descended into.

    Args:
        root (Path): Directory to walk.

    Yields:
        Path: Python source files.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIR_NAMES)
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def parse_file(path: Path, collector: FindingCollector) -> ast.AST | None:
    """Parse a Python file into an AST, reporting read/parse failures as findings.

    Args:
        path (Path): Path to the Python source file.
        collector (FindingCollector): Finding sink for parse failures.

    Returns:
        ast.AST | None: Parsed module AST, or None if the file could not be
        read or has a syntax error (failures are recorded as findings).
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as err:
        collector.report(
            path,
            0,
            f"failed to read: {type(err).__name__}: {err}",
        )
        return None

    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError as err:
        collector.report(
            path,
            err.lineno or 0,
            f"failed to parse: SyntaxError: {err.msg}",
        )
        return None


def is_pytest_hook_in_conftest(path: Path, stack: list[ast.AST]) -> bool:
    """Return True if the current node is inside a ``pytest_*`` hook in conftest.py.

    Args:
        path (Path): Source file path.
        stack (list[ast.AST]): Ancestor nodes from root to parent of current.

    Returns:
        bool: Whether this position is an allowlisted pytest hook in conftest.py.
    """
    if path.name != "conftest.py":
        return False
    func_name = enclosing_function_name(stack)
    return func_name is not None and func_name.startswith("pytest_")


def is_type_checking_test(test: ast.expr) -> bool:
    """Return True if ``test`` is ``TYPE_CHECKING`` or ``typing.TYPE_CHECKING``.

    Args:
        test (ast.expr): The ``ast.If`` test expression.

    Returns:
        bool: Whether the test is a TYPE_CHECKING guard.
    """
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
    )


def enclosing_function_name(stack: list[ast.AST]) -> str | None:
    """Return the nearest enclosing function name from an AST walk stack.

    Args:
        stack (list[ast.AST]): Nodes visited from root to current.

    Returns:
        str | None: Function name, or None if not inside a function.
    """
    for node in reversed(stack):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return None


def is_module_level(stack: list[ast.AST]) -> bool:
    """Return True if the current node is at module scope (not in a function).

    Nested under module-level ``If`` / ``With`` / ``Try`` / ``ClassDef`` still
    counts as module-level: class bodies run at import time. Only
    ``FunctionDef``, ``AsyncFunctionDef``, and ``Lambda`` nest out of module
    scope.

    Args:
        stack (list[ast.AST]): Nodes visited from root to parent of current.

    Returns:
        bool: Whether the current position is module-level.
    """
    return not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) for node in stack)


def walk_with_stack(tree: ast.AST) -> Iterator[tuple[ast.AST, list[ast.AST]]]:
    """Walk an AST yielding each node with its ancestor stack (excluding self).

    Yields the live ancestor list (read-only for callers). Do not retain or
    mutate the stack across iterations — it is reused and mutated as the walk
    proceeds. Callers must only read it within the same loop iteration.

    Args:
        tree (ast.AST): Root AST node.

    Yields:
        tuple[ast.AST, list[ast.AST]]: (node, ancestors from root to parent).
    """
    stack: list[ast.AST] = []

    def _visit(node: ast.AST) -> Iterator[tuple[ast.AST, list[ast.AST]]]:
        yield node, stack
        stack.append(node)
        for child in ast.iter_child_nodes(node):
            yield from _visit(child)
        stack.pop()

    yield from _visit(tree)


def for_each_parsed_file(
    paths: list[str],
    collector: FindingCollector,
    check_file: Callable[[Path, ast.AST, FindingCollector], None],
) -> None:
    """Iterate Python files, parse each, and invoke ``check_file``.

    Args:
        paths (list[str]): File paths from pre-commit (empty = whole repo).
        collector (FindingCollector): Finding sink for parse failures and
            check findings.
        check_file (Callable[[Path, ast.AST, FindingCollector], None]):
            Per-file checker invoked as ``check_file(path, tree, collector)``.
    """
    for path in iter_py_files(paths):
        tree = parse_file(path, collector)
        if tree is None:
            continue
        check_file(path, tree, collector)


def main_runner(check_fn: Callable[[list[str], FindingCollector], None]) -> int:
    """Run a check function against argv file paths and exit with status.

    Args:
        check_fn (Callable[[list[str], FindingCollector], None]): Check that
            records findings into the collector.

    Returns:
        int: 0 if clean, 1 if any findings.
    """
    paths = sys.argv[1:]
    collector = FindingCollector()
    check_fn(paths, collector)
    return 1 if collector else 0
