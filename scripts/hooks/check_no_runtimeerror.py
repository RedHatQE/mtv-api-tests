#!/usr/bin/env python3
"""Ban raise RuntimeError outside pytest hooks in conftest.py."""

from __future__ import annotations

import ast
import sys
from collections import Counter
from functools import partial
from pathlib import Path

from _ast_utils import (
    FindingCollector,
    for_each_parsed_file,
    is_pytest_hook_in_conftest,
    main_runner,
    walk_with_stack,
)
from baseline import is_baselined, load_baseline_for_check


def _is_runtime_error_raise(node: ast.Raise) -> bool:
    """Return True if ``node`` raises RuntimeError (call or bare name).

    Args:
        node (ast.Raise): Raise AST node.

    Returns:
        bool: Whether the raised exception is RuntimeError.
    """
    exc = node.exc
    if exc is None:
        return False

    if isinstance(exc, ast.Call):
        func = exc.func
        if isinstance(func, ast.Name) and func.id == "RuntimeError":
            return True
        return isinstance(func, ast.Attribute) and func.attr == "RuntimeError"

    if isinstance(exc, ast.Name) and exc.id == "RuntimeError":
        return True
    return isinstance(exc, ast.Attribute) and exc.attr == "RuntimeError"


def _check_file(
    path: Path,
    tree: ast.AST,
    collector: FindingCollector,
    baseline: Counter[tuple[str, str]],
) -> None:
    """Flag non-allowlisted ``raise RuntimeError`` in one file.

    Args:
        path (Path): Source file path.
        tree (ast.AST): Parsed AST.
        collector (FindingCollector): Finding sink.
        baseline (Counter[tuple[str, str]]): Grandfathered path/fingerprint
            occurrence counts.
    """
    for node, stack in walk_with_stack(tree):
        if not isinstance(node, ast.Raise):
            continue
        if not _is_runtime_error_raise(node):
            continue
        if is_pytest_hook_in_conftest(path, stack):
            continue
        if is_baselined(path, node.lineno, baseline, end_lineno=node.end_lineno):
            continue
        collector.report(
            path,
            node.lineno,
            "forbidden 'raise RuntimeError'; use ValueError/custom exceptions "
            "(allowed only in pytest_* hooks in conftest.py)",
        )


def run_check(paths: list[str], collector: FindingCollector) -> None:
    """Run the RuntimeError raise check on the given paths.

    Args:
        paths (list[str]): File paths from pre-commit (empty = whole repo).
        collector (FindingCollector): Finding sink.
    """
    baseline = load_baseline_for_check("check_no_runtimeerror", collector.report)
    if baseline is None:
        return

    for_each_parsed_file(paths, collector, partial(_check_file, baseline=baseline))


if __name__ == "__main__":
    sys.exit(main_runner(run_check))
