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
        """Initialize an empty findings list."""
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
        """Return True if any findings have been recorded.

        Returns:
            bool: Whether the collector has one or more findings.
        """
        return bool(self.findings)

    def __len__(self) -> int:
        """Return the number of recorded findings.

        Returns:
            int: Count of findings.
        """
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


def collect_typing_aliases(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """Collect ``typing`` module and ``TYPE_CHECKING`` name aliases from imports.

    Always includes ``typing`` and ``TYPE_CHECKING`` so bare
    ``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:`` keep working.
    Also records:

    - ``import typing as <alias>`` → ``<alias>.TYPE_CHECKING``
    - ``from typing import TYPE_CHECKING`` / ``... as <alias>``

    Args:
        tree (ast.AST): Parsed module AST.

    Returns:
        tuple[frozenset[str], frozenset[str]]:
        ``(typing_module_aliases, type_checking_names)``.
    """
    typing_aliases: set[str] = {"typing"}
    type_checking_names: set[str] = {"TYPE_CHECKING"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "typing":
                    typing_aliases.add(alias.asname or "typing")
        elif isinstance(node, ast.ImportFrom) and node.module == "typing":
            for alias in node.names:
                if alias.name == "TYPE_CHECKING":
                    type_checking_names.add(alias.asname or "TYPE_CHECKING")
    return frozenset(typing_aliases), frozenset(type_checking_names)


def is_type_checking_test(
    test: ast.expr,
    typing_aliases: frozenset[str] | None = None,
    type_checking_names: frozenset[str] | None = None,
) -> bool:
    """Return True if ``test`` is a TYPE_CHECKING guard (including aliases).

    Recognizes bare names in ``type_checking_names`` (default
    ``TYPE_CHECKING``) and ``<typing_alias>.TYPE_CHECKING`` where
    ``typing_alias`` is in ``typing_aliases`` (default ``typing``).

    Args:
        test (ast.expr): The ``ast.If`` test expression.
        typing_aliases (frozenset[str] | None): Names bound to the typing
            module. Defaults to ``frozenset({"typing"})``.
        type_checking_names (frozenset[str] | None): Names bound to
            ``TYPE_CHECKING``. Defaults to ``frozenset({"TYPE_CHECKING"})``.

    Returns:
        bool: Whether the test is a TYPE_CHECKING guard.
    """
    if typing_aliases is None:
        typing_aliases = frozenset({"typing"})
    if type_checking_names is None:
        type_checking_names = frozenset({"TYPE_CHECKING"})

    if isinstance(test, ast.Name) and test.id in type_checking_names:
        return True
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id in typing_aliases
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


def is_import_time_expression(stack: list[ast.AST], node: ast.AST) -> bool:
    """Return True if ``node`` evaluates at import or definition time.

    Returns False only when ``node`` is inside a ``FunctionDef`` /
    ``AsyncFunctionDef`` body or a ``Lambda`` body (deferred until call).
    Expressions in ``decorator_list``, ``defaults``, ``kw_defaults``,
    annotations, and ``returns`` evaluate at definition time and count as
    import-time even when nested under a function ancestor.

    Nested function definitions inside an outer function body are deferred:
    defaults/decorators on the inner function still run only when the outer
    body executes.

    Args:
        stack (list[ast.AST]): Ancestors from root to parent of ``node``.
        node (ast.AST): The expression node being classified.

    Returns:
        bool: Whether ``node`` evaluates at import/definition time.
    """
    for i, anc in enumerate(stack):
        if isinstance(anc, (ast.FunctionDef, ast.AsyncFunctionDef)):
            child = stack[i + 1] if i + 1 < len(stack) else node
            if child in anc.body:
                return False
        elif isinstance(anc, ast.Lambda):
            child = stack[i + 1] if i + 1 < len(stack) else node
            if child is anc.body:
                return False
    return True


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
        """Yield ``node`` with the live ancestor stack, then recurse into children.

        The shared ``stack`` lists ancestors from the walk root to the parent of
        the current node (excluding ``node`` itself). Before descending,
        ``node`` is appended; after all children are visited, it is popped.
        Callers must treat the yielded list as read-only and valid only for the
        current iteration.

        Args:
            node (ast.AST): Current AST node to yield and recurse into.

        Yields:
            tuple[ast.AST, list[ast.AST]]: ``(node, stack)`` where ``stack`` is
            the live ancestor list (root → parent) shared across the walk.
        """
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
