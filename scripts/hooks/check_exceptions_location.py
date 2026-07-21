#!/usr/bin/env python3
"""Require custom Exception subclasses to live in exceptions/exceptions.py."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from _ast_utils import FindingCollector, for_each_parsed_file, main_runner, walk_with_stack
from baseline import is_baselined, load_baseline

_EXCEPTION_BASE_NAMES: frozenset[str] = frozenset({"Exception", "BaseException"})
_BASELINE = load_baseline("check_exceptions_location")


def _is_allowed_file(path: Path) -> bool:
    """Return True if ``path`` is the allowed exceptions module.

    Args:
        path (Path): Source file path.

    Returns:
        bool: Whether this is exceptions/exceptions.py.
    """
    return path.name == "exceptions.py" and path.parent.name == "exceptions"


def _base_identifier(base: ast.expr) -> str | None:
    """Return the Name.id or Attribute.attr for a ClassDef base, if present.

    Args:
        base (ast.expr): A ClassDef base expression.

    Returns:
        str | None: Identifier used for matching, or None if unsupported form.
    """
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _base_is_exception_like(base: ast.expr) -> bool:
    """Return True if ``base`` is Exception, BaseException, or *Error.

    Args:
        base (ast.expr): A ClassDef base expression.

    Returns:
        bool: Whether the base looks like an exception type.
    """
    name = _base_identifier(base)
    if name is None:
        return False
    return name in _EXCEPTION_BASE_NAMES or name.endswith("Error")


def check_file(path: Path, tree: ast.AST, collector: FindingCollector) -> None:
    """Flag exception-like subclasses defined outside exceptions/exceptions.py.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
    """
    if _is_allowed_file(path):
        return

    for node, _stack in walk_with_stack(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if any(_base_is_exception_like(base) for base in node.bases):
            if is_baselined(path, node.lineno, _BASELINE):
                continue
            collector.report(
                path,
                node.lineno,
                f"custom Exception subclass '{node.name}' must be defined in exceptions/exceptions.py",
            )


def run_check(paths: list[str], collector: FindingCollector) -> None:
    """Run the exceptions location check on the given paths.

    Args:
        paths (list[str]): File paths from pre-commit (empty = whole repo).
        collector (FindingCollector): Finding sink.
    """
    for_each_parsed_file(paths, collector, check_file)


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
