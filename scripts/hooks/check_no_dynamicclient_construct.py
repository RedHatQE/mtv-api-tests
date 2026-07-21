#!/usr/bin/env python3
"""Ban constructing DynamicClient directly; use get_client() instead."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from _ast_utils import (
    FindingCollector,
    iter_py_files,
    main_runner,
    parse_file,
    walk_with_stack,
)


def _is_dynamic_client_call(func: ast.expr) -> bool:
    """Return True if ``func`` is ``DynamicClient`` or ``*.DynamicClient``.

    Args:
        func (ast.expr): The callee expression of a Call node.

    Returns:
        bool: Whether the callee constructs DynamicClient.
    """
    if isinstance(func, ast.Name) and func.id == "DynamicClient":
        return True
    return isinstance(func, ast.Attribute) and func.attr == "DynamicClient"


def check_file(path: Path, tree: ast.AST, collector: FindingCollector) -> None:
    """Flag DynamicClient(...) constructions in one file.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
    """
    for node, _stack in walk_with_stack(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_dynamic_client_call(node.func):
            collector.report(
                path,
                node.lineno,
                "forbidden DynamicClient(...) construction; use get_client() instead",
            )


def run_check(paths: list[str], collector: FindingCollector) -> None:
    """Run the DynamicClient construction check on the given paths.

    Args:
        paths (list[str]): File paths from pre-commit (empty = whole repo).
        collector (FindingCollector): Finding sink.
    """
    for path in iter_py_files(paths):
        tree = parse_file(path, collector)
        if tree is None:
            continue
        check_file(path, tree, collector)


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
